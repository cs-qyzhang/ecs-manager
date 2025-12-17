from __future__ import annotations

import os
import sys
from pathlib import Path

def _suppress_noisy_ssl_warnings() -> None:
    """
    Aliyun SDK (aliyunsdkcore) vendors an old urllib3/requests stack that may emit
    SNIMissingWarning on some Windows consoles even when requests succeed.

    Suppress it by default to reduce noise.
    Set ECS_SHOW_SSL_WARNINGS=1 to re-enable.
    """
    if os.getenv("ECS_SHOW_SSL_WARNINGS"):
        return
    try:
        import warnings

        from aliyunsdkcore.vendored.requests.packages.urllib3.exceptions import SNIMissingWarning

        warnings.filterwarnings("ignore", category=SNIMissingWarning)
    except Exception:
        pass


def _strip_inline_comment_unquoted(value: str) -> str:
    # Very small parser: treat ` #` as comment start only for unquoted values.
    in_single = False
    in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # Only treat as comment if preceded by whitespace
            if i > 0 and value[i - 1].isspace():
                return value[:i].rstrip()
    return value


def _parse_env_line(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("export "):
        s = s[len("export ") :].lstrip()
    if "=" not in s:
        return None
    key, value = s.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    value = _strip_inline_comment_unquoted(value)
    if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
        value = value[1:-1]
    return key, value


def _find_dotenv_upwards(start: Path) -> Path | None:
    cur = start.resolve()
    while True:
        candidate = cur / ".env"
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def _load_dotenv_file(path: Path, *, override: bool = False) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for line in text.splitlines():
        parsed = _parse_env_line(line)
        if not parsed:
            continue
        k, v = parsed
        if not override and k in os.environ:
            continue
        os.environ[k] = v


def _load_dotenv_early() -> None:
    # Load .env as early as possible so Typer/Click envvar options can see it.
    # - Default: search for `.env` from current working directory upwards.
    # - Override: set ECS_ENV_FILE to an explicit path.
    # - Does NOT override real environment variables by default.
    env_file = os.getenv("ECS_ENV_FILE")
    if env_file:
        _load_dotenv_file(Path(env_file).expanduser(), override=False)
        return
    found = _find_dotenv_upwards(Path.cwd())
    if found:
        _load_dotenv_file(found, override=False)


_load_dotenv_early()
_suppress_noisy_ssl_warnings()


def _sanitize_stuck_completion_env() -> None:
    """
    PowerShell completion invokes `ecs` in a subprocess with special env vars
    (e.g. _ECS_COMPLETE, _TYPER_COMPLETE_ARGS). If a completion invocation is
    interrupted, these vars can get stuck in the *parent shell*, making normal
    commands output nothing.

    For real user commands (argv has extra args like `scp`, `--help`, etc.),
    always ignore those vars so the CLI stays usable.
    """

    if len(sys.argv) > 1:
        os.environ.pop("_ECS_COMPLETE", None)
        os.environ.pop("_TYPER_COMPLETE_ARGS", None)
        os.environ.pop("_TYPER_COMPLETE_WORD_TO_COMPLETE", None)


_sanitize_stuck_completion_env()

import json
import subprocess
from typing import Any

import typer
from aliyunsdkcore.acs_exception.exceptions import ServerException

from .aliyun_ecs import (
    EcsError,
    allocate_public_ip_address,
    create_instance,
    delete_instance,
    describe_instance,
    list_instances,
    list_regions,
    start_instance,
    stop_instance,
    wait_instance,
    wait_instance_status,
)
from .state import default_config, default_state_path, load_state, resolve_state_path, save_state
from .ssh_config import SshConfigEntry, default_host_alias, remove as ssh_config_remove, ssh_config_path, upsert as ssh_config_upsert
from .util import coerce_value, format_cmd, normalize_region_id, now_iso_utc, null_device, sanitize_hostname


app = typer.Typer(
    help="Manage Codex sessions on Alibaba Cloud ECS (create/connect/rename/delete).",
    no_args_is_help=True,
)

config_app = typer.Typer(help="Manage defaults stored in the JSON state file.")
app.add_typer(config_app, name="config")

ssh_app = typer.Typer(help="Manage ~/.ssh/config entries for sessions.")
app.add_typer(ssh_app, name="ssh")

template_app = typer.Typer(help="Manage reusable create templates stored in the state file.")
app.add_typer(template_app, name="template")


def _die(message: str, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _get_state_path_from_ctx(ctx: typer.Context) -> Path:
    state_file_opt = None
    if ctx.obj and isinstance(ctx.obj, dict):
        state_file_opt = ctx.obj.get("state_file")
    return resolve_state_path(state_file_opt)


def _load(ctx: typer.Context) -> tuple[Path, dict[str, Any]]:
    path = _get_state_path_from_ctx(ctx)
    return path, load_state(path)


def _save(path: Path, state: dict[str, Any]) -> None:
    save_state(path, state)


def _require(value: str, key: str) -> str:
    if not value:
        _die(f"Missing required config: {key}. Set it via: ecs config set {key}=... ")
    return value


def _complete_session_names(incomplete: str) -> list[str]:
    try:
        # Prefer state file resolved from env/args during completion, so session-name completion
        # works even when user passes `--state-file ...` instead of setting ECS_STATE_FILE.
        state_path = default_state_path()
        try:
            import shlex

            args_str = os.getenv("_TYPER_COMPLETE_ARGS") or ""
            if args_str:
                parts = shlex.split(args_str, posix=False)
                # parts is a best-effort parse of the command line string.
                for i, p in enumerate(parts):
                    if p.startswith("--state-file="):
                        state_path = Path(p.split("=", 1)[1]).expanduser()
                        break
                    if p == "--state-file" and i + 1 < len(parts):
                        state_path = Path(parts[i + 1]).expanduser()
                        break
        except Exception:
            pass

        state = load_state(state_path)
        sessions = state.get("sessions") or {}
        if not isinstance(sessions, dict):
            return []
        names = sorted(str(k) for k in sessions.keys())
        return [n for n in names if n.startswith(incomplete)]
    except Exception:
        return []


def _complete_template_names(incomplete: str) -> list[str]:
    try:
        state_path = default_state_path()
        try:
            import shlex

            args_str = os.getenv("_TYPER_COMPLETE_ARGS") or ""
            if args_str:
                parts = shlex.split(args_str, posix=False)
                for i, p in enumerate(parts):
                    if p.startswith("--state-file="):
                        state_path = Path(p.split("=", 1)[1]).expanduser()
                        break
                    if p == "--state-file" and i + 1 < len(parts):
                        state_path = Path(parts[i + 1]).expanduser()
                        break
        except Exception:
            pass

        state = load_state(state_path)
        templates = state.get("templates") or {}
        if not isinstance(templates, dict):
            return []
        names = sorted(str(k) for k in templates.keys())
        return [n for n in names if n.startswith(incomplete)]
    except Exception:
        return []


@app.callback()
def _main(
    ctx: typer.Context,
    state_file: Path | None = typer.Option(
        None,
        "--state-file",
        envvar="ECS_STATE_FILE",
        help="Path to the JSON state file. Default: ~/.ecs/state.json",
    ),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["state_file"] = state_file


@app.command()
def path(ctx: typer.Context) -> None:
    """Print the resolved state file path."""
    typer.echo(str(_get_state_path_from_ctx(ctx)))


@template_app.command("list")
def template_list(ctx: typer.Context) -> None:
    """List templates (from the local JSON state file)."""
    _, state = _load(ctx)
    templates = state.get("templates") or {}
    if not isinstance(templates, dict):
        _die("State file is corrupted: templates is not a dict.")
    if not templates:
        typer.echo("No templates.")
        return

    rows: list[tuple[str, str]] = []
    for name, rec in templates.items():
        desc = ""
        if isinstance(rec, dict):
            d = rec.get("description")
            if isinstance(d, str):
                desc = d
        rows.append((str(name), desc))

    name_w = max(len(r[0]) for r in rows)
    typer.echo(f"{'NAME'.ljust(name_w)}  DESCRIPTION")
    typer.echo("-" * (name_w + 2 + len("DESCRIPTION")))
    for n, d in sorted(rows, key=lambda x: x[0]):
        typer.echo(f"{n.ljust(name_w)}  {d}")


@template_app.command("show")
def template_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_template_names),
) -> None:
    """Show one template record as JSON."""
    _, state = _load(ctx)
    templates = state.get("templates") or {}
    if not isinstance(templates, dict):
        _die("State file is corrupted: templates is not a dict.")
    rec = templates.get(name)
    if not isinstance(rec, dict):
        _die(f"Template not found: {name}")
    typer.echo(json.dumps(rec, ensure_ascii=False, indent=2))


@template_app.command("set")
def template_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Template name."),
    pairs: list[str] = typer.Argument(
        ...,
        help="One or more key=value pairs for create defaults, e.g. region_id=cn-hangzhou image_id=... instance_type=... spot_strategy=NoSpot",
    ),
    description: str | None = typer.Option(None, "--description", "-d", help="Optional description."),
) -> None:
    """Create or update a template."""
    path, state = _load(ctx)
    templates = state.get("templates")
    if not isinstance(templates, dict):
        templates = {}
        state["templates"] = templates

    rec = templates.get(name)
    if not isinstance(rec, dict):
        rec = {"name": name, "created_at": now_iso_utc(), "updated_at": now_iso_utc(), "description": "", "config": {}}
        templates[name] = rec

    cfg = rec.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
        rec["config"] = cfg

    if description is not None:
        rec["description"] = str(description)

    for raw in pairs:
        if "=" not in raw:
            _die(f"Invalid pair (expected key=value): {raw}")
        k, v = raw.split("=", 1)
        k = k.strip()
        if not k:
            _die(f"Invalid key in pair: {raw}")
        cfg[k] = coerce_value(v)

    rec["updated_at"] = now_iso_utc()
    _save(path, state)
    typer.echo("OK")


@template_app.command("unset")
def template_unset(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_template_names),
    keys: list[str] = typer.Argument(..., help="One or more keys to remove from the template."),
) -> None:
    """Remove keys from a template."""
    path, state = _load(ctx)
    templates = state.get("templates") or {}
    if not isinstance(templates, dict):
        _die("State file is corrupted: templates is not a dict.")
    rec = templates.get(name)
    if not isinstance(rec, dict):
        _die(f"Template not found: {name}")
    cfg = rec.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
        rec["config"] = cfg
    for k in keys:
        cfg.pop(str(k), None)
    rec["updated_at"] = now_iso_utc()
    _save(path, state)
    typer.echo("OK")


@template_app.command("delete")
def template_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_template_names),
) -> None:
    """Delete a template."""
    path, state = _load(ctx)
    templates = state.get("templates") or {}
    if not isinstance(templates, dict):
        _die("State file is corrupted: templates is not a dict.")
    if name not in templates:
        _die(f"Template not found: {name}")
    templates.pop(name, None)
    _save(path, state)
    typer.echo("OK")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show current default config."""
    _, state = _load(ctx)
    typer.echo(json.dumps(state.get("config", {}), ensure_ascii=False, indent=2))


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    pairs: list[str] = typer.Argument(
        ...,
        help="One or more key=value pairs, e.g. region_id=cn-hangzhou image_id=... ssh_private_key_path=C:\\key.pem",
    ),
) -> None:
    """Set default config values (stored in the JSON state file)."""
    path, state = _load(ctx)
    cfg = state.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
        state["config"] = cfg

    allowed = set(default_config().keys())
    updates: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            _die(f"Invalid pair: {p!r}. Expected key=value.")
        k, v = p.split("=", 1)
        k = k.strip()
        if k not in allowed:
            _die(f"Unknown config key: {k}. Allowed keys: {', '.join(sorted(allowed))}")
        updates[k] = coerce_value(v)

    # Help users avoid a common misconfig: passing ZoneId as region_id.
    if "region_id" in updates and isinstance(updates["region_id"], str):
        normalized, original = normalize_region_id(updates["region_id"])
        if original:
            typer.echo(
                f"Warning: region_id {original!r} looks like a ZoneId; using RegionId examples like "
                f"{normalized!r} (not {original!r}).",
                err=True,
            )

    cfg.update(updates)
    _save(path, state)
    typer.echo("OK")


@app.command("list")
def list_sessions(ctx: typer.Context) -> None:
    """List known sessions (from the local JSON file)."""
    _, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict) or not sessions:
        typer.echo("(no sessions)")
        return

    rows = []
    for name, s in sessions.items():
        if not isinstance(s, dict):
            continue
        rows.append(
            (
                str(name),
                str(s.get("status") or "-"),
                str(s.get("public_ip") or "-"),
                str(s.get("instance_id") or "-"),
            )
        )

    name_w = max(len(r[0]) for r in rows)
    status_w = max(len(r[1]) for r in rows)
    ip_w = max(len(r[2]) for r in rows)
    header = (
        f"{'NAME'.ljust(name_w)}  {'STATUS'.ljust(status_w)}  {'PUBLIC_IP'.ljust(ip_w)}  INSTANCE_ID"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in sorted(rows, key=lambda x: x[0]):
        typer.echo(f"{r[0].ljust(name_w)}  {r[1].ljust(status_w)}  {r[2].ljust(ip_w)}  {r[3]}")


@app.command()
def info(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
) -> None:
    """Show one session record as JSON."""
    _, state = _load(ctx)
    sess = (state.get("sessions") or {}).get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")
    typer.echo(json.dumps(sess, ensure_ascii=False, indent=2))


@app.command()
def create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Session name (used as the ECS InstanceName)."),
    template: str | None = typer.Option(
        None,
        "--template",
        autocompletion=_complete_template_names,
        help="Template name (from `ecs template`). Template config is used as defaults for create; CLI flags override.",
    ),
    hostname: str | None = typer.Option(
        None,
        "--hostname",
        help="Set ECS HostName (instance OS hostname). If omitted, it can be derived from the session name (see --hostname-to-session).",
    ),
    hostname_to_session: bool | None = typer.Option(
        None,
        "--hostname-to-session/--no-hostname-to-session",
        help="Set HostName to a sanitized session name. Default from config set_hostname_to_session.",
    ),
    # Optional overrides (default from config):
    region_id: str | None = typer.Option(None, "--region-id"),
    image_id: str | None = typer.Option(None, "--image-id"),
    instance_type: str | None = typer.Option(None, "--instance-type"),
    security_group_id: str | None = typer.Option(None, "--security-group-id"),
    v_switch_id: str | None = typer.Option(None, "--v-switch-id"),
    key_pair_name: str | None = typer.Option(None, "--key-pair-name"),
    system_disk_category: str | None = typer.Option(
        None,
        "--system-disk-category",
        help="System disk category (e.g. cloud_auto|cloud_essd|cloud_ssd|cloud_efficiency). Default from config system_disk_category.",
    ),
    system_disk_size: int | None = typer.Option(
        None,
        "--system-disk-size",
        help="System disk size in GB. Default from config system_disk_size.",
    ),
    system_disk_performance_level: str | None = typer.Option(
        None,
        "--system-disk-performance-level",
        help="ESSD performance level: PL0|PL1|PL2|PL3. Default from config system_disk_performance_level.",
    ),
    allocate_public_ip: bool | None = typer.Option(
        None,
        "--allocate-public-ip/--no-allocate-public-ip",
        help="If enabled and no public IP is assigned, ecs will call AllocatePublicIpAddress. Default from config auto_allocate_public_ip.",
    ),
    internet_max_bandwidth_out: int | None = typer.Option(None, "--internet-max-bandwidth-out"),
    internet_charge_type: str | None = typer.Option(None, "--internet-charge-type"),
    spot_strategy: str | None = typer.Option(
        None,
        "--spot-strategy",
        help="NoSpot | SpotAsPriceGo | SpotWithPriceLimit. Default from config spot_strategy.",
    ),
    spot_price_limit: str | None = typer.Option(
        None,
        "--spot-price-limit",
        help="Max hourly price (only for SpotWithPriceLimit). Default from config spot_price_limit.",
    ),
    spot_duration: int | None = typer.Option(
        None,
        "--spot-duration",
        help="Stable duration hours for spot instances (1-6). Default from config spot_duration.",
    ),
    spot_interruption_behavior: str | None = typer.Option(
        None,
        "--spot-interruption-behavior",
        help="Optional (e.g. Terminate). Default from config spot_interruption_behavior.",
    ),
    ssh_user: str | None = typer.Option(None, "--ssh-user", help="Saved into session record for connect."),
    timeout_seconds: int | None = typer.Option(None, "--timeout-seconds"),
    poll_interval_seconds: int | None = typer.Option(None, "--poll-interval-seconds"),
) -> None:
    """Create a new ECS instance for a Codex session and record it locally."""
    path, state = _load(ctx)
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
        state["sessions"] = sessions

    if name in sessions:
        _die(f"Session already exists: {name}")

    cfg = state.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    effective_cfg: dict[str, Any] = dict(cfg)
    template_name = (template or "").strip() or None
    if template_name:
        templates = state.get("templates") or {}
        if not isinstance(templates, dict):
            _die("State file is corrupted: templates is not a dict.")
        trec = templates.get(template_name)
        if not isinstance(trec, dict):
            _die(f"Template not found: {template_name}")
        tcfg = trec.get("config") or {}
        if not isinstance(tcfg, dict):
            _die(f"Template {template_name!r} is corrupted: config is not a dict.")
        # Merge: global config < template config < CLI flags
        effective_cfg.update(tcfg)

    region = region_id or effective_cfg.get("region_id") or ""
    image = image_id or effective_cfg.get("image_id") or ""
    itype = instance_type or effective_cfg.get("instance_type") or ""
    sg = security_group_id or effective_cfg.get("security_group_id") or ""
    vsw = v_switch_id or effective_cfg.get("v_switch_id") or ""
    keypair = key_pair_name or effective_cfg.get("key_pair_name") or ""

    region = _require(str(region), "region_id")
    normalized_region, original_zone = normalize_region_id(region)
    if original_zone:
        typer.echo(
            f"Warning: region_id {original_zone!r} looks like a ZoneId. "
            f"Using region_id={normalized_region!r} for ECS API endpoint.",
            err=True,
        )
        region = normalized_region
    image = _require(str(image), "image_id")
    itype = _require(str(itype), "instance_type")
    sg = _require(str(sg), "security_group_id")
    vsw = _require(str(vsw), "v_switch_id")
    keypair = _require(str(keypair), "key_pair_name")

    bw = (
        internet_max_bandwidth_out
        if internet_max_bandwidth_out is not None
        else effective_cfg.get("internet_max_bandwidth_out")
    )
    charge_type = internet_charge_type or effective_cfg.get("internet_charge_type") or "PayByTraffic"
    allocate_public_ip_final = (
        bool(allocate_public_ip)
        if allocate_public_ip is not None
        else bool(effective_cfg.get("auto_allocate_public_ip", True))
    )

    sys_disk_cat = (
        system_disk_category if system_disk_category is not None else effective_cfg.get("system_disk_category")
    )
    if sys_disk_cat is not None:
        sys_disk_cat = str(sys_disk_cat).strip() or None
    sys_disk_size = system_disk_size if system_disk_size is not None else effective_cfg.get("system_disk_size")
    sys_disk_pl = (
        system_disk_performance_level
        if system_disk_performance_level is not None
        else effective_cfg.get("system_disk_performance_level")
    )
    if sys_disk_pl is not None:
        sys_disk_pl = str(sys_disk_pl).strip() or None

    spot_strategy_final = spot_strategy or effective_cfg.get("spot_strategy") or "SpotAsPriceGo"
    spot_price_limit_final = (
        spot_price_limit if spot_price_limit is not None else effective_cfg.get("spot_price_limit")
    )
    spot_duration_final = spot_duration if spot_duration is not None else effective_cfg.get("spot_duration")
    spot_interruption_behavior_final = (
        spot_interruption_behavior
        if spot_interruption_behavior is not None
        else effective_cfg.get("spot_interruption_behavior")
    )

    ssh_user_final = ssh_user or effective_cfg.get("ssh_user") or "root"
    timeout_final = int(timeout_seconds or effective_cfg.get("timeout_seconds") or 600)
    poll_final = int(poll_interval_seconds or effective_cfg.get("poll_interval_seconds") or 5)

    hostname_to_session_final = (
        bool(hostname_to_session)
        if hostname_to_session is not None
        else bool(effective_cfg.get("set_hostname_to_session", True))
    )
    hostname_final: str | None = None
    hostname_raw = hostname if hostname is not None else effective_cfg.get("hostname")
    if hostname_raw is not None and str(hostname_raw).strip() != "":
        hostname_final = sanitize_hostname(str(hostname_raw))
        if hostname_final != str(hostname_raw).strip().lower():
            typer.echo(
                f"Warning: hostname normalized to {hostname_final!r} from {str(hostname_raw)!r}",
                err=True,
            )
    elif hostname_to_session_final:
        hostname_final = sanitize_hostname(name)
        if hostname_final != name.strip().lower():
            typer.echo(
                f"Info: hostname set to {hostname_final!r} (sanitized from session name {name!r})",
            )

    def _try_create_with_disk_category(cat: str | None) -> str:
        return create_instance(
            region_id=region,
            image_id=image,
            instance_type=itype,
            security_group_id=sg,
            v_switch_id=vsw,
            key_pair_name=keypair,
            instance_name=name,
            hostname=hostname_final,
            tags=[
                {"Key": "ecs", "Value": "true"},
                {"Key": "ecs_session", "Value": name},
            ],
            system_disk_category=cat,
            system_disk_size=int(sys_disk_size) if sys_disk_size is not None else None,
            system_disk_performance_level=sys_disk_pl,
            internet_charge_type=str(charge_type),
            internet_max_bandwidth_out=int(bw) if bw is not None else None,
            spot_strategy=str(spot_strategy_final) if spot_strategy_final else None,
            spot_price_limit=spot_price_limit_final,
            spot_duration=int(spot_duration_final) if spot_duration_final is not None else None,
            spot_interruption_behavior=str(spot_interruption_behavior_final)
            if spot_interruption_behavior_final
            else None,
        )

    try:
        instance_id = _try_create_with_disk_category(sys_disk_cat)
    except EcsError as e:
        _die(str(e))
    except ServerException as e:
        code = e.get_error_code() if hasattr(e, "get_error_code") else None
        # Common: some instance families/regions require ESSD and reject default categories.
        if code == "InvalidSystemDiskCategory.ValueNotSupported" and sys_disk_cat is None:
            last_err: Exception = e
            for fallback_cat in ("cloud_auto", "cloud_essd"):
                typer.echo(
                    f"Warning: default system disk category not supported; retrying with system_disk_category={fallback_cat!r}",
                    err=True,
                )
                try:
                    instance_id = _try_create_with_disk_category(fallback_cat)
                    sys_disk_cat = fallback_cat
                    break
                except ServerException as e2:
                    last_err = e2
                    code2 = e2.get_error_code() if hasattr(e2, "get_error_code") else None
                    if code2 != "InvalidSystemDiskCategory.ValueNotSupported":
                        raise
            else:
                _die(
                    f"Aliyun API error: {last_err}\n"
                    f"Tip: try setting `system_disk_category=cloud_essd` in config, e.g.:\n"
                    f"  ecs config set system_disk_category=cloud_essd"
                )
        else:
            _die(
                f"Aliyun API error: {e}\n"
                f"Tip: if you see InvalidSystemDiskCategory, try:\n"
                f"  ecs config set system_disk_category=cloud_auto\n"
                f"or:\n"
                f"  ecs config set system_disk_category=cloud_essd"
            )
    except Exception as e:
        _die(f"Aliyun API error: {e}")

    record: dict[str, Any] = {
        "name": name,
        "template": template_name,
        "region_id": region,
        "instance_id": instance_id,
        "image_id": image,
        "instance_type": itype,
        "instance_name": name,
        "hostname": hostname_final,
        "key_pair_name": keypair,
        "system_disk_category": sys_disk_cat,
        "system_disk_size": int(sys_disk_size) if sys_disk_size is not None else None,
        "system_disk_performance_level": sys_disk_pl,
        "created_at": now_iso_utc(),
        "status": "Created",
        "public_ip": None,
        "private_ip": None,
        "ssh_user": ssh_user_final,
        "last_refresh_at": None,
        "last_error": None,
    }
    sessions[name] = record
    _save(path, state)

    try:
        start_instance(region_id=region, instance_id=instance_id)
    except EcsError as e:
        record["status"] = "StartFailed"
        record["last_error"] = str(e)
        _save(path, state)
        _die(str(e))
    except Exception as e:
        record["status"] = "StartFailed"
        record["last_error"] = str(e)
        _save(path, state)
        _die(f"Aliyun API error: {e}")

    record["status"] = "Starting"
    _save(path, state)

    typer.echo(f"Created instance: {instance_id} (starting; waiting for Running...)")

    try:
        info_obj = wait_instance(
            region_id=region,
            instance_id=instance_id,
            timeout_seconds=timeout_final,
            poll_interval_seconds=poll_final,
            require_public_ip=False,
        )
        record["status"] = info_obj.status
        record["public_ip"] = info_obj.public_ip
        record["private_ip"] = info_obj.private_ip
        record["last_refresh_at"] = now_iso_utc()
        _save(path, state)

        if allocate_public_ip_final and not record.get("public_ip"):
            bw_int = int(bw or 0)
            if bw_int <= 0:
                typer.echo(
                    "Warning: internet_max_bandwidth_out is 0, so a public IP cannot be allocated. "
                    "Set it to >0 to enable public access.",
                    err=True,
                )
            else:
                try:
                    typer.echo("No public IP yet; allocating public IP via AllocatePublicIpAddress...")
                    ip = allocate_public_ip_address(region_id=region, instance_id=instance_id)
                    record["public_ip"] = ip
                    record["last_refresh_at"] = now_iso_utc()
                    _save(path, state)

                    # Best-effort refresh from DescribeInstances (eventual consistency).
                    try:
                        info2 = wait_instance(
                            region_id=region,
                            instance_id=instance_id,
                            timeout_seconds=min(timeout_final, 120),
                            poll_interval_seconds=poll_final,
                            require_public_ip=True,
                        )
                        record["public_ip"] = info2.public_ip or record["public_ip"]
                        record["private_ip"] = info2.private_ip or record["private_ip"]
                        record["status"] = info2.status or record["status"]
                        record["last_refresh_at"] = now_iso_utc()
                        _save(path, state)
                    except TimeoutError:
                        pass
                except Exception as e:
                    typer.echo(
                        f"Warning: failed to allocate public IP: {e}\n"
                        f"Tip: you can still use `ecs connect {name} --private`, or bind an EIP.",
                        err=True,
                    )

        if record.get("public_ip"):
            typer.echo(f"Ready: {name} -> {ssh_user_final}@{record['public_ip']}")
        elif record.get("private_ip"):
            typer.echo(
                f"Ready (no public ip): {name} -> {ssh_user_final}@{record['private_ip']} "
                f"(use `ecs connect {name} --private`)"
            )
        else:
            typer.echo(f"Ready: {name} -> instance {instance_id}")

        # Auto-add ssh config entry
        if bool(cfg.get("auto_ssh_config", True)):
            ip = record.get("public_ip") or record.get("private_ip")
            key_path_str = str(cfg.get("ssh_private_key_path") or "").strip()
            if ip and key_path_str:
                alias = default_host_alias(name, prefix=str(cfg.get("ssh_config_host_prefix") or "ecs-"))
                entry = SshConfigEntry(
                    session_name=name,
                    host_alias=alias,
                    host_name=str(ip),
                    user=str(record.get("ssh_user") or cfg.get("ssh_user") or "root"),
                    identity_file=key_path_str,
                    forward_agent=True,
                    identities_only=True,
                    strict_host_key_checking=bool(cfg.get("ssh_strict_host_key_checking")),
                )
                try:
                    ssh_config_upsert(ssh_config_path(), entry)
                    typer.echo(f"SSH config added: Host {alias} (file: {ssh_config_path()})")
                except Exception as e:
                    typer.echo(f"Warning: failed to update ~/.ssh/config: {e}", err=True)
    except TimeoutError as e:
        typer.echo(str(e), err=True)
        typer.echo(
            "Tip: you can still run `ecs info <name>`, `ecs connect <name> --refresh`, "
            "or `ecs public-ip <name>` later."
        )


@app.command("public-ip")
def public_ip(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until DescribeInstances shows the public IP."),
    timeout_seconds: int = typer.Option(180, "--timeout-seconds", help="Max seconds to wait for public IP."),
    poll_interval_seconds: int = typer.Option(5, "--poll-interval-seconds", help="Polling interval seconds."),
) -> None:
    """Allocate an ephemeral public IP for an existing session (AllocatePublicIpAddress)."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    sess = sessions.get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")

    region = str(sess.get("region_id") or "")
    instance_id = str(sess.get("instance_id") or "")
    if not region or not instance_id:
        _die(f"Session record missing region_id/instance_id: {name}")

    # Refresh first
    try:
        info = describe_instance(region_id=region, instance_id=instance_id)
        if info:
            sess["status"] = info.status
            if info.public_ip:
                sess["public_ip"] = info.public_ip
            if info.private_ip:
                sess["private_ip"] = info.private_ip
            sess["last_refresh_at"] = now_iso_utc()
            _save(path, state)
    except Exception:
        pass

    if sess.get("public_ip"):
        typer.echo(str(sess["public_ip"]))
        return

    try:
        ip = allocate_public_ip_address(region_id=region, instance_id=instance_id)
        sess["public_ip"] = ip
        sess["last_refresh_at"] = now_iso_utc()
        _save(path, state)
        typer.echo(f"Allocated public ip: {ip}")
    except Exception as e:
        _die(f"Failed to allocate public IP: {e}")

    if wait:
        try:
            info2 = wait_instance(
                region_id=region,
                instance_id=instance_id,
                timeout_seconds=int(timeout_seconds),
                poll_interval_seconds=int(poll_interval_seconds),
                require_public_ip=True,
            )
            if info2.public_ip:
                sess["public_ip"] = info2.public_ip
            if info2.private_ip:
                sess["private_ip"] = info2.private_ip
            sess["status"] = info2.status
            sess["last_refresh_at"] = now_iso_utc()
            _save(path, state)
        except TimeoutError:
            pass


@app.command()
def sync(
    ctx: typer.Context,
    region_id: list[str] = typer.Option(
        [],
        "--region-id",
        "-r",
        help="RegionId(s) to query. If omitted, uses region_id from config and all sessions.",
    ),
    all_regions: bool = typer.Option(
        False,
        "--all-regions",
        help="Query all regions returned by DescribeRegions (may take longer).",
    ),
    prune_missing: bool = typer.Option(
        False,
        "--prune/--no-prune",
        help="Remove local sessions whose instances no longer exist.",
    ),
    import_new: bool = typer.Option(
        False,
        "--import/--no-import",
        help="Import instances that are not present in local state.",
    ),
    import_all: bool = typer.Option(
        False,
        "--import-all",
        help="With --import: import all instances (otherwise only those tagged ecs=true).",
    ),
) -> None:
    """
    Sync local state with Aliyun ECS.

    - Refresh status/IP for sessions in state.json
    - Detect instances deleted manually (mark NotFound or prune)
    - Optionally import instances from the cloud into state.json
    """
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    cfg = state.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    regions: list[str] = []
    if region_id:
        regions.extend(region_id)
    elif all_regions:
        # Need a seed region for the endpoint; prefer config/sessions, otherwise fall back to cn-hangzhou.
        seed = str(cfg.get("region_id") or "").strip()
        if not seed:
            for s in sessions.values():
                if isinstance(s, dict):
                    seed = str(s.get("region_id") or "").strip()
                    if seed:
                        break
        if not seed:
            seed = "cn-hangzhou"
        try:
            regions = list_regions(seed_region_id=seed)
        except Exception as e:
            _die(f"Failed to list regions via DescribeRegions: {e}")
    else:
        cfg_region = str(cfg.get("region_id") or "").strip()
        if cfg_region:
            regions.append(cfg_region)
        for s in sessions.values():
            if isinstance(s, dict):
                r = str(s.get("region_id") or "").strip()
                if r:
                    regions.append(r)

    # Normalize + unique
    normalized_regions: list[str] = []
    seen: set[str] = set()
    for r in regions:
        nr, _ = normalize_region_id(r)
        if nr and nr not in seen:
            normalized_regions.append(nr)
            seen.add(nr)

    if not normalized_regions:
        _die("No region_id found. Set config region_id or pass --region-id (or use --all-regions).")

    typer.echo(f"Syncing regions: {', '.join(normalized_regions)}")

    # instance_id -> (region, InstanceInfo)
    instances_by_id: dict[str, tuple[str, Any]] = {}
    for r in normalized_regions:
        try:
            for info in list_instances(region_id=r):
                if info.instance_id:
                    instances_by_id[info.instance_id] = (r, info)
        except Exception as e:
            typer.echo(f"Warning: failed to list instances in {r}: {e}", err=True)

    updated = 0
    marked_missing = 0
    removed: list[str] = []

    # Update existing sessions
    for name in list(sessions.keys()):
        rec = sessions.get(name)
        if not isinstance(rec, dict):
            continue
        instance_id = str(rec.get("instance_id") or "")
        if not instance_id:
            continue

        found = instances_by_id.get(instance_id)
        if found is None:
            if prune_missing:
                sessions.pop(name, None)
                removed.append(name)
            else:
                if rec.get("missing_since") is None:
                    rec["missing_since"] = now_iso_utc()
                rec["status"] = "NotFound"
                rec["last_refresh_at"] = now_iso_utc()
                marked_missing += 1
            continue
        inst_region, info = found

        # Refresh
        rec["status"] = info.status
        rec["public_ip"] = info.public_ip
        rec["private_ip"] = info.private_ip
        if info.instance_name:
            rec["instance_name"] = info.instance_name
        rec["region_id"] = inst_region
        rec["last_refresh_at"] = now_iso_utc()
        rec.pop("missing_since", None)
        updated += 1

    imported = 0
    if import_new:
        candidates: list[tuple[str, Any]] = []
        if import_all:
            candidates = list(instances_by_id.values())
        else:
            # Only import instances tagged ecs=true
            for r in normalized_regions:
                try:
                    for info in list_instances(
                        region_id=r,
                        tags=[{"Key": "ecs", "Value": "true"}],
                    ):
                        candidates.append((r, info))
                except Exception as e:
                    typer.echo(f"Warning: failed to list tagged instances in {r}: {e}", err=True)

        existing_ids = {
            str(v.get("instance_id"))
            for v in sessions.values()
            if isinstance(v, dict) and v.get("instance_id") is not None and str(v.get("instance_id")).strip()
        }
        for item in candidates:
            if isinstance(item, tuple) and len(item) == 2:
                r, info = item
            else:
                # Backward compatibility if list_instances returns directly (shouldn't happen now).
                r, info = normalized_regions[0], item
            if not info.instance_id or info.instance_id in existing_ids:
                continue
            base = (info.instance_name or "").strip() or info.instance_id
            new_name = base
            if new_name in sessions:
                new_name = f"{base}-{info.instance_id}"

            sessions[new_name] = {
                "name": new_name,
                "region_id": r,
                "instance_id": info.instance_id,
                "image_id": info.image_id,
                "instance_type": info.instance_type,
                "instance_name": info.instance_name,
                "key_pair_name": None,
                "created_at": now_iso_utc(),
                "status": info.status,
                "public_ip": info.public_ip,
                "private_ip": info.private_ip,
                "ssh_user": cfg.get("ssh_user") or "root",
                "last_refresh_at": now_iso_utc(),
                "imported_at": now_iso_utc(),
            }
            imported += 1

    _save(path, state)

    if prune_missing and removed:
        typer.echo(f"Removed {len(removed)} missing sessions: {', '.join(removed)}")
    if marked_missing:
        typer.echo(f"Marked missing: {marked_missing}")
    typer.echo(f"Updated: {updated}, Imported: {imported}")


@app.command()
def rename(
    ctx: typer.Context,
    old: str = typer.Argument(..., autocompletion=_complete_session_names),
    new: str = typer.Argument(..., help="New session name (local record only)."),
) -> None:
    """Rename a session (local record only)."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")
    if old not in sessions:
        _die(f"Session not found: {old}")
    if new in sessions:
        _die(f"Session already exists: {new}")

    rec = sessions.pop(old)
    if isinstance(rec, dict):
        rec["name"] = new
    sessions[new] = rec
    _save(path, state)
    typer.echo("OK")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def connect(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    private: bool = typer.Option(False, "--private", help="Use private IP instead of public IP."),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Refresh IP/status from Aliyun before SSH."),
    user: str | None = typer.Option(None, "--user", help="SSH username (default from session/config)."),
    key_file: Path | None = typer.Option(
        None,
        "--key-file",
        help="Path to SSH private key (.pem). Default from env ECS_SSH_KEY or config ssh_private_key_path.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print ssh command and exit."),
) -> None:
    """SSH into the ECS for this session (uses `ssh -A`). Extra args after `--` are passed to ssh."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    sess = sessions.get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")

    cfg = state.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    region = str(sess.get("region_id") or cfg.get("region_id") or "")
    instance_id = str(sess.get("instance_id") or "")
    if not instance_id:
        _die(f"Session record missing instance_id: {name}")

    if refresh:
        try:
            info_obj = describe_instance(region_id=region, instance_id=instance_id)
            if info_obj:
                sess["status"] = info_obj.status
                if info_obj.public_ip:
                    sess["public_ip"] = info_obj.public_ip
                if info_obj.private_ip:
                    sess["private_ip"] = info_obj.private_ip
                sess["last_refresh_at"] = now_iso_utc()
                _save(path, state)
        except Exception as e:
            typer.echo(f"Warning: refresh failed: {e}", err=True)

    host = sess.get("private_ip") if private else sess.get("public_ip")
    if not host:
        ip_kind = "private" if private else "public"
        _die(f"No {ip_kind} ip recorded for {name}. Try `ecs connect {name} --refresh`.")

    ssh_user_final = user or sess.get("ssh_user") or cfg.get("ssh_user") or "root"

    key_path = key_file
    if key_path is None:
        env_key = os.environ.get("ECS_SSH_KEY")
        if env_key:
            key_path = Path(env_key)
    if key_path is None:
        key_path_str = str(cfg.get("ssh_private_key_path") or "")
        key_path = Path(key_path_str) if key_path_str else None
    if key_path is None:
        _die(
            "Missing SSH key file. Set it via env ECS_SSH_KEY, "
            "or `ecs config set ssh_private_key_path=...`, or pass --key-file."
        )

    strict = bool(cfg.get("ssh_strict_host_key_checking"))
    extra_ssh = cfg.get("ssh_extra_args") or []
    if not isinstance(extra_ssh, list):
        extra_ssh = []
    extra_ssh = [str(x) for x in extra_ssh]

    target = f"{ssh_user_final}@{host}"
    cmd: list[str] = [
        "ssh",
        "-A",
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ForwardAgent=yes",
    ]
    if not strict:
        cmd += [
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"UserKnownHostsFile={null_device()}",
        ]
    cmd += extra_ssh
    cmd.append(target)

    # Pass-through extra args after `--`
    cmd += list(ctx.args)

    if dry_run:
        typer.echo(format_cmd(cmd))
        raise typer.Exit(0)

    typer.echo(f"Connecting to {target} (instance {instance_id}) ...")
    raise typer.Exit(subprocess.call(cmd))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def scp(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    source: str = typer.Argument(..., help="SOURCE path. Use ':/path' to mean remote path on the session instance."),
    destination: str = typer.Argument(
        ...,
        help="DEST path. Use ':/path' to mean remote path on the session instance.",
    ),
    private: bool = typer.Option(False, "--private", help="Use private IP instead of public IP."),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Refresh IP/status from Aliyun before SCP."),
    user: str | None = typer.Option(None, "--user", help="SSH username (default from session/config)."),
    key_file: Path | None = typer.Option(
        None,
        "--key-file",
        help="Path to SSH private key (.pem). Default from env ECS_SSH_KEY or config ssh_private_key_path.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print scp command and exit."),
) -> None:
    """
    Copy files between local machine and the session instance using `scp`.

    Exactly one of SOURCE/DEST must be remote, indicated by a leading ':'.
    Examples:
      - Upload:   ecs scp my-session .\\file.txt :/root/file.txt
      - Download: ecs scp my-session :/root/file.txt .\\file.txt

    Extra args after `--` are passed to scp (e.g. `-- -r`).
    """
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    sess = sessions.get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")

    cfg = state.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    region = str(sess.get("region_id") or cfg.get("region_id") or "")
    instance_id = str(sess.get("instance_id") or "")
    if not instance_id:
        _die(f"Session record missing instance_id: {name}")

    if refresh:
        try:
            info_obj = describe_instance(region_id=region, instance_id=instance_id)
            if info_obj:
                sess["status"] = info_obj.status
                if info_obj.public_ip:
                    sess["public_ip"] = info_obj.public_ip
                if info_obj.private_ip:
                    sess["private_ip"] = info_obj.private_ip
                sess["last_refresh_at"] = now_iso_utc()
                _save(path, state)
        except Exception as e:
            typer.echo(f"Warning: refresh failed: {e}", err=True)

    host = sess.get("private_ip") if private else sess.get("public_ip")
    if not host:
        ip_kind = "private" if private else "public"
        _die(
            f"No {ip_kind} ip recorded for {name}. Try `ecs scp {name} --refresh ...` "
            f"or use `--private`."
        )

    ssh_user_final = user or sess.get("ssh_user") or cfg.get("ssh_user") or "root"

    key_path = key_file
    if key_path is None:
        env_key = os.environ.get("ECS_SSH_KEY")
        if env_key:
            key_path = Path(env_key)
    if key_path is None:
        key_path_str = str(cfg.get("ssh_private_key_path") or "")
        key_path = Path(key_path_str) if key_path_str else None
    if key_path is None:
        _die(
            "Missing SSH key file. Set it via env ECS_SSH_KEY, "
            "or `ecs config set ssh_private_key_path=...`, or pass --key-file."
        )

    strict = bool(cfg.get("ssh_strict_host_key_checking"))
    extra_ssh = cfg.get("ssh_extra_args") or []
    if not isinstance(extra_ssh, list):
        extra_ssh = []
    extra_ssh = [str(x) for x in extra_ssh]

    def _is_remote_spec(p: str) -> bool:
        return isinstance(p, str) and p.startswith(":") and len(p) > 1

    src_is_remote = _is_remote_spec(source)
    dst_is_remote = _is_remote_spec(destination)
    if src_is_remote == dst_is_remote:
        _die(
            "Exactly one of SOURCE or DEST must start with ':' to indicate the remote path on the session.\n"
            "Examples:\n"
            "  ecs scp my-session .\\file.txt :/root/file.txt\n"
            "  ecs scp my-session :/root/file.txt .\\file.txt"
        )

    remote_prefix = f"{ssh_user_final}@{host}"
    src = f"{remote_prefix}{source}" if src_is_remote else source
    dst = f"{remote_prefix}{destination}" if dst_is_remote else destination

    cmd: list[str] = [
        "scp",
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ForwardAgent=yes",
    ]
    if not strict:
        cmd += [
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"UserKnownHostsFile={null_device()}",
        ]
    cmd += extra_ssh

    # Pass-through extra args after `--` (must come before src/dst).
    cmd += list(ctx.args)
    cmd += [src, dst]

    if dry_run:
        typer.echo(format_cmd(cmd))
        raise typer.Exit(0)

    try:
        rc = subprocess.call(cmd)
        if rc != 0:
            typer.echo(f"scp failed (exit code {rc}).", err=True)
            typer.echo(f"Command: {format_cmd(cmd)}", err=True)
            typer.echo("Tip: add verbose flags, e.g. `ecs scp ... -- -v`.", err=True)
        raise typer.Exit(rc)
    except FileNotFoundError:
        _die("`scp` not found in PATH. Install OpenSSH client (Windows: Optional Features) or ensure scp is available.")


@app.command()
def delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt for confirmation."),
    force: bool = typer.Option(True, "--force/--no-force", help="Use Force=True for DeleteInstance."),
    keep_record: bool = typer.Option(False, "--keep-record", help="Do not remove local record after deletion."),
) -> None:
    """Delete the ECS instance and remove the local session record."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    sess = sessions.get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")

    region = str(sess.get("region_id") or "")
    instance_id = str(sess.get("instance_id") or "")
    if not region or not instance_id:
        _die(f"Session record missing region_id/instance_id: {name}")

    if not yes:
        confirmed = typer.confirm(f"Delete ECS instance {instance_id} for session {name}?")
        if not confirmed:
            raise typer.Exit(1)

    try:
        delete_instance(region_id=region, instance_id=instance_id, force=force)
    except EcsError as e:
        _die(str(e))
    except Exception as e:
        _die(f"Aliyun API error: {e}")

    # Best-effort remove ssh config entry even if we keep the record.
    try:
        ssh_config_remove(ssh_config_path(), name)
    except Exception:
        pass

    if not keep_record:
        sessions.pop(name, None)
        _save(path, state)

    typer.echo("OK")


@app.command()
def stop(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    force: bool = typer.Option(False, "--force", help="Force stop the instance."),
    mode: str = typer.Option(
        "stop-charging",
        "--mode",
        help="stop-charging (recommended) or keep-charging.",
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until instance is Stopped."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Max seconds to wait for Stopped."),
    poll_interval_seconds: int = typer.Option(5, "--poll-interval-seconds", help="Polling interval seconds."),
) -> None:
    """Stop the ECS instance for this session (to save cost)."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    sess = sessions.get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")

    region = str(sess.get("region_id") or "")
    instance_id = str(sess.get("instance_id") or "")
    if not region or not instance_id:
        _die(f"Session record missing region_id/instance_id: {name}")

    mode_norm = (mode or "").strip().lower()
    if mode_norm in {"stop-charging", "stopcharging", "stop_charging"}:
        stopped_mode = "StopCharging"
    elif mode_norm in {"keep-charging", "keepcharging", "keep_charging"}:
        stopped_mode = "KeepCharging"
    else:
        _die("Invalid --mode. Use: stop-charging or keep-charging.")

    try:
        stop_instance(region_id=region, instance_id=instance_id, force=force, stopped_mode=stopped_mode)
    except Exception as e:
        # If StopCharging isn't supported for this instance, suggest KeepCharging.
        if stopped_mode == "StopCharging":
            typer.echo(
                f"Stop failed with mode StopCharging: {e}\n"
                f"Tip: try `ecs stop {name} --mode keep-charging`.",
                err=True,
            )
        _die(f"Aliyun API error: {e}")

    sess["status"] = "Stopping"
    sess["last_refresh_at"] = now_iso_utc()
    _save(path, state)
    typer.echo(f"Stopping: {name} ({instance_id}) ...")

    if wait:
        try:
            info = wait_instance_status(
                region_id=region,
                instance_id=instance_id,
                desired_status="Stopped",
                timeout_seconds=int(timeout_seconds),
                poll_interval_seconds=int(poll_interval_seconds),
            )
            sess["status"] = info.status
            sess["public_ip"] = info.public_ip
            sess["private_ip"] = info.private_ip
            sess["last_refresh_at"] = now_iso_utc()
            _save(path, state)
            typer.echo("OK (Stopped)")
        except TimeoutError as e:
            typer.echo(str(e), err=True)
            typer.echo("Tip: run `ecs sync` or `ecs info <name>` later.")


@app.command()
def start(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until instance is Running."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Max seconds to wait for Running."),
    poll_interval_seconds: int = typer.Option(5, "--poll-interval-seconds", help="Polling interval seconds."),
    allocate_public_ip: bool | None = typer.Option(
        None,
        "--allocate-public-ip/--no-allocate-public-ip",
        help="If enabled and no public IP is assigned, call AllocatePublicIpAddress. Default from config auto_allocate_public_ip.",
    ),
) -> None:
    """Start the ECS instance for this session."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    sess = sessions.get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")

    cfg = state.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    region = str(sess.get("region_id") or "")
    instance_id = str(sess.get("instance_id") or "")
    if not region or not instance_id:
        _die(f"Session record missing region_id/instance_id: {name}")

    allocate_public_ip_final = (
        bool(allocate_public_ip)
        if allocate_public_ip is not None
        else bool(cfg.get("auto_allocate_public_ip", True))
    )

    try:
        start_instance(region_id=region, instance_id=instance_id)
    except Exception as e:
        _die(f"Aliyun API error: {e}")

    sess["status"] = "Starting"
    sess["last_refresh_at"] = now_iso_utc()
    _save(path, state)
    typer.echo(f"Starting: {name} ({instance_id}) ...")

    if wait:
        try:
            info = wait_instance_status(
                region_id=region,
                instance_id=instance_id,
                desired_status="Running",
                timeout_seconds=int(timeout_seconds),
                poll_interval_seconds=int(poll_interval_seconds),
            )
            sess["status"] = info.status
            sess["public_ip"] = info.public_ip
            sess["private_ip"] = info.private_ip
            sess["last_refresh_at"] = now_iso_utc()
            _save(path, state)

            if allocate_public_ip_final and not sess.get("public_ip"):
                bw = int(cfg.get("internet_max_bandwidth_out") or 0)
                if bw > 0:
                    try:
                        typer.echo("No public IP yet; allocating public IP via AllocatePublicIpAddress...")
                        ip = allocate_public_ip_address(region_id=region, instance_id=instance_id)
                        sess["public_ip"] = ip
                        sess["last_refresh_at"] = now_iso_utc()
                        _save(path, state)
                    except Exception as e:
                        typer.echo(
                            f"Warning: failed to allocate public IP: {e}\n"
                            f"Tip: you can still use `ecs connect {name} --private`, or bind an EIP.",
                            err=True,
                        )

            typer.echo("OK (Running)")
        except TimeoutError as e:
            typer.echo(str(e), err=True)
            typer.echo("Tip: run `ecs sync` or `ecs info <name>` later.")


@ssh_app.command("add")
def ssh_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    private: bool = typer.Option(False, "--private", help="Use private IP instead of public IP."),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="Refresh IP/status from Aliyun before writing config."),
    host_alias: str | None = typer.Option(None, "--host", help="Override Host alias written to ~/.ssh/config."),
) -> None:
    """Add/update one session entry in ~/.ssh/config."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")
    sess = sessions.get(name)
    if not isinstance(sess, dict):
        _die(f"Session not found: {name}")

    cfg = state.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    region = str(sess.get("region_id") or cfg.get("region_id") or "")
    instance_id = str(sess.get("instance_id") or "")
    if not instance_id:
        _die(f"Session record missing instance_id: {name}")

    if refresh:
        try:
            info_obj = describe_instance(region_id=region, instance_id=instance_id)
            if info_obj:
                sess["status"] = info_obj.status
                if info_obj.public_ip:
                    sess["public_ip"] = info_obj.public_ip
                if info_obj.private_ip:
                    sess["private_ip"] = info_obj.private_ip
                sess["last_refresh_at"] = now_iso_utc()
                _save(path, state)
        except Exception as e:
            typer.echo(f"Warning: refresh failed: {e}", err=True)

    ip = sess.get("private_ip") if private else sess.get("public_ip")
    if not ip:
        _die("No IP available for this session. Use --private or run `ecs public-ip <name>` first.")

    key_path_str = str(cfg.get("ssh_private_key_path") or "").strip()
    if not key_path_str:
        _die("Missing config ssh_private_key_path. Set it via: ecs config set ssh_private_key_path=...")

    alias = host_alias or default_host_alias(name, prefix=str(cfg.get("ssh_config_host_prefix") or "ecs-"))
    entry = SshConfigEntry(
        session_name=name,
        host_alias=alias,
        host_name=str(ip),
        user=str(sess.get("ssh_user") or cfg.get("ssh_user") or "root"),
        identity_file=key_path_str,
        forward_agent=True,
        identities_only=True,
        strict_host_key_checking=bool(cfg.get("ssh_strict_host_key_checking")),
    )
    ssh_config_upsert(ssh_config_path(), entry)
    typer.echo(f"OK (added): Host {alias} -> {entry.user}@{entry.host_name}")


@ssh_app.command("del")
def ssh_del(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
) -> None:
    """Remove one session entry from ~/.ssh/config."""
    removed = ssh_config_remove(ssh_config_path(), name)
    if removed:
        typer.echo("OK")
    else:
        typer.echo("Not found")


