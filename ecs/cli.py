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


def _normalize_typer_value(value: Any) -> Any:
    if isinstance(value, typer.models.OptionInfo):
        return None
    return value


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
import fnmatch
import re
import subprocess
import textwrap
from typing import Any

import typer
from aliyunsdkcore.acs_exception.exceptions import ServerException

from .aliyun_ecs import (
    EcsError,
    allocate_public_ip_address,
    authorize_security_group_rule,
    assert_instance_type_supports_erdma,
    resolve_security_group_id_from_vswitch,
    create_instance,
    create_erdma_network_interface,
    delete_instance,
    delete_network_interface,
    describe_instance,
    attach_network_interface,
    set_network_interface_delete_on_release,
    get_instance_security_group_id,
    list_instances,
    list_regions,
    list_security_group_rules,
    revoke_security_group_rule,
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
    context_settings={"help_option_names": ["-h", "--help"]},
)

config_app = typer.Typer(
    help="Manage defaults stored in the JSON state file.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(config_app, name="config")

ssh_app = typer.Typer(
    help="Manage ~/.ssh/config entries for sessions.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(ssh_app, name="ssh")

template_app = typer.Typer(
    help="Manage reusable create templates stored in the state file.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(template_app, name="template")

port_app = typer.Typer(
    help="Manage security group port rules for instances.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(port_app, name="port")

cluster_app = typer.Typer(
    help="Manage clusters (groups of instances created from a template).",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(cluster_app, name="cluster")


_SAFE_TEMPLATE_FILE = re.compile(r"[^a-zA-Z0-9._-]+")


def _templates_dir_for_state_file(state_path: Path, config: dict[str, Any] | None = None) -> Path:
    # Keep templates alongside the state file for easy syncing/backups by default.
    # Example: ~/.ecs/state.json -> ~/.ecs/.ecs-templates/<name>.json
    # If config.template_dir is set, use that path instead. Relative paths are
    # resolved relative to the state file directory.
    template_dir = ""
    if isinstance(config, dict):
        raw = config.get("template_dir")
        if raw is not None:
            template_dir = str(raw).strip()
    if template_dir:
        candidate = Path(template_dir).expanduser()
        if not candidate.is_absolute():
            candidate = state_path.parent / candidate
        return candidate
    return state_path.parent / ".ecs-templates"


def _template_file_path(state_path: Path, name: str, config: dict[str, Any] | None = None) -> Path:
    safe = _SAFE_TEMPLATE_FILE.sub("_", (name or "").strip())
    if not safe:
        safe = "template"
    return _templates_dir_for_state_file(state_path, config) / f"{safe}.json"


def _parse_editor_cmd() -> list[str] | None:
    raw = (os.getenv("VISUAL") or os.getenv("EDITOR") or "").strip()
    if not raw:
        return None
    try:
        import shlex

        parts = shlex.split(raw, posix=(os.name != "nt"))
        return [p for p in parts if p.strip()]
    except Exception:
        return None


def _open_in_editor(path: Path) -> None:
    cmd = _parse_editor_cmd()
    if cmd:
        try:
            subprocess.check_call([*cmd, str(path)])
            return
        except FileNotFoundError:
            _die(f"Editor not found: {cmd[0]!r}. Set VISUAL or EDITOR to a valid command.")
        except subprocess.CalledProcessError as e:
            _die(f"Editor exited with code {e.returncode}: {format_cmd(cmd)}")

    # Fallbacks
    if os.name == "nt":
        try:
            subprocess.check_call(["notepad", str(path)])
        except subprocess.CalledProcessError as e:
            _die(f"Editor exited with code {e.returncode}: notepad")
        return

    if sys.platform == "darwin":
        try:
            subprocess.check_call(["open", "-W", str(path)])
        except subprocess.CalledProcessError as e:
            _die(f"Editor exited with code {e.returncode}: open")
        return

    # Linux/other: best-effort common editors
    for fallback in ("nano", "vi"):
        try:
            subprocess.check_call([fallback, str(path)])
            return
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as e:
            _die(f"Editor exited with code {e.returncode}: {fallback}")
    _die("No editor found. Set VISUAL or EDITOR.")


def _write_template_file(path: Path, *, name: str, description: str, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "description": description, "config": config}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_template_file(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _die(f"Template file not found: {path}")
    except json.JSONDecodeError as e:
        _die(f"Template file is not valid JSON: {path}\n{e}")

    if not isinstance(raw, dict):
        _die(f"Template file must be a JSON object: {path}")

    desc = raw.get("description") or ""
    if not isinstance(desc, str):
        _die(f"Template file field 'description' must be a string: {path}")

    cfg = raw.get("config")
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        _die(f"Template file field 'config' must be an object: {path}")

    return desc, cfg


def _default_erdma_driver_user_data() -> str:
    return textwrap.dedent(
        """\
        #!/bin/bash
        set -euxo pipefail
        exec > >(tee -a /var/log/erdma-install.log) 2>&1

        INSTALLER_URL="http://mirrors.cloud.aliyuncs.com/erdma/env_setup.sh"
        INSTALLER_PATH="/root/env_setup.sh"

        if command -v curl >/dev/null 2>&1; then
          curl -fsSL -o "$INSTALLER_PATH" "$INSTALLER_URL"
        elif command -v wget >/dev/null 2>&1; then
          wget -O "$INSTALLER_PATH" "$INSTALLER_URL"
        else
          echo "Neither curl nor wget is installed; cannot download eRDMA installer."
          exit 1
        fi

        /bin/bash "$INSTALLER_PATH"
        """
    )


def _template_starter_config() -> dict[str, Any]:
    # A small, practical starter set for `ecs create`.
    # Required fields are left empty so users must fill them in.
    return {
        "region_id": "",
        "image_id": "",
        "instance_type": "",
        "v_switch_id": "",
        "key_pair_name": "qyzhang-PDSL",
        # eRDMA (ERI)
        "enable_erdma": False,
        "erdma_v_switch_id": "",
        "auto_install_erdma_driver": True,
        # Optional startup script / cloud-init passed to CreateInstance.
        "user_data": "",
        # Public IP behavior
        "auto_allocate_public_ip": True,
        "internet_charge_type": "PayByTraffic",
        "internet_max_bandwidth_out": 10,
        # System disk (optional)
        "system_disk_category": "cloud_essd",
        "system_disk_size": 40,
        "system_disk_performance_level": "PL0",
        # Spot (optional)
        "spot_strategy": "SpotAsPriceGo",
        "spot_price_limit": None,
        "spot_duration": None,
        "spot_interruption_behavior": None,
        # Convenience
        "ssh_user": "root",
    }


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


def _session_name_matches_pattern(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern)


def _resolve_session_targets(sessions: dict[str, Any], pattern: str) -> list[str]:
    target = str(pattern or "").strip()
    if not target:
        _die("Missing session name or pattern.")

    names = sorted(str(k) for k in sessions.keys())
    if any(ch in target for ch in "*?["):
        matched = [name for name in names if _session_name_matches_pattern(name, target)]
        if not matched:
            _die(f"No sessions match pattern: {target}")
        return matched

    if target not in sessions:
        _die(f"Session not found: {target}")
    return [target]


def _stop_one_session(
    *,
    path: Path,
    state: dict[str, Any],
    name: str,
    force: bool,
    mode: str,
    wait: bool,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> None:
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
            typer.echo(f"OK (Stopped): {name}")
        except TimeoutError as e:
            typer.echo(str(e), err=True)
            typer.echo(f"Tip: run `ecs sync` or `ecs info {name}` later.")


def _start_one_session(
    *,
    path: Path,
    state: dict[str, Any],
    name: str,
    wait: bool,
    timeout_seconds: int,
    poll_interval_seconds: int,
    allocate_public_ip: bool | None,
) -> None:
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

            typer.echo(f"OK (Running): {name}")
        except TimeoutError as e:
            typer.echo(str(e), err=True)
            typer.echo(f"Tip: run `ecs sync` or `ecs info {name}` later.")


def _complete_cluster_names(incomplete: str) -> list[str]:
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
        clusters = state.get("clusters") or {}
        if not isinstance(clusters, dict):
            return []
        names = sorted(str(k) for k in clusters.keys())
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


@template_app.command("show", no_args_is_help=True)
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


@template_app.command("edit", no_args_is_help=True)
def template_edit(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_template_names, help="Template name."),
) -> None:
    """Edit a template in $VISUAL/$EDITOR (or a platform fallback) and save back to state.json."""
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

    desc = rec.get("description") or ""
    if not isinstance(desc, str):
        desc = str(desc)

    cfg_root = state.get("config") if isinstance(state.get("config"), dict) else None
    tpath = _template_file_path(path, name, cfg_root)
    _write_template_file(tpath, name=name, description=desc, config=cfg)
    typer.echo(f"Opening editor: {tpath}")
    _open_in_editor(tpath)

    new_desc, new_cfg = _read_template_file(tpath)
    rec["description"] = new_desc
    rec["config"] = new_cfg
    rec["updated_at"] = now_iso_utc()
    _save(path, state)
    typer.echo("OK")


@template_app.command("set", no_args_is_help=True)
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


@template_app.command("create", no_args_is_help=True)
def template_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Template name."),
    pairs: list[str] | None = typer.Argument(
        None,
        help="Optional key=value pairs for create defaults. If omitted, use --edit to fill in via editor.",
    ),
    description: str | None = typer.Option(None, "--description", "-d", help="Optional description."),
    edit: bool = typer.Option(False, "--edit", help="Open the template file in an editor after creating."),
) -> None:
    """Create a new template (fails if it already exists)."""
    path, state = _load(ctx)
    templates = state.get("templates")
    if not isinstance(templates, dict):
        templates = {}
        state["templates"] = templates

    if name in templates:
        _die(f"Template already exists: {name} (use `ecs template set` or `ecs template edit`)")

    cfg: dict[str, Any] = {}
    for raw in (pairs or []):
        if "=" not in raw:
            _die(f"Invalid pair (expected key=value): {raw}")
        k, v = raw.split("=", 1)
        k = k.strip()
        if not k:
            _die(f"Invalid key in pair: {raw}")
        cfg[k] = coerce_value(v)

    if not cfg and not edit:
        _die("No key=value pairs provided. Use `ecs template create <name> k=v ...` or add --edit.")

    # When creating with --edit, provide a starter skeleton so users have a clear list of common keys.
    file_cfg: dict[str, Any] = dict(cfg)
    if edit:
        for k, v in _template_starter_config().items():
            file_cfg.setdefault(k, v)

    rec = {
        "name": name,
        "created_at": now_iso_utc(),
        "updated_at": now_iso_utc(),
        "description": str(description or ""),
        "config": cfg,
    }
    templates[name] = rec
    _save(path, state)

    if edit:
        cfg_root = state.get("config") if isinstance(state.get("config"), dict) else None
        tpath = _template_file_path(path, name, cfg_root)
        _write_template_file(tpath, name=name, description=str(rec["description"]), config=file_cfg)
        typer.echo(f"Opening editor: {tpath}")
        _open_in_editor(tpath)
        new_desc, new_cfg = _read_template_file(tpath)
        rec["description"] = new_desc
        rec["config"] = new_cfg
        rec["updated_at"] = now_iso_utc()
        _save(path, state)

    typer.echo("OK")


@template_app.command("unset", no_args_is_help=True)
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


@template_app.command("delete", no_args_is_help=True)
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
    # Best-effort cleanup of the exported template file (if it exists).
    try:
        cfg_root = state.get("config") if isinstance(state.get("config"), dict) else None
        _template_file_path(path, name, cfg_root).unlink(missing_ok=True)
    except Exception:
        pass
    typer.echo("OK")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show current default config."""
    _, state = _load(ctx)
    typer.echo(json.dumps(state.get("config", {}), ensure_ascii=False, indent=2))


@config_app.command("set", no_args_is_help=True)
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

    # Keep output stable, but sort cluster nodes together in rank order.
    rows: list[tuple[str, str, str, str, str | None, int | None]] = []
    for name, s in sessions.items():
        if not isinstance(s, dict):
            continue
        cluster_name = s.get("cluster_name")
        if cluster_name is not None and not isinstance(cluster_name, str):
            cluster_name = str(cluster_name)
        cluster_rank: int | None = None
        if s.get("cluster_rank") is not None:
            try:
                cluster_rank = int(s.get("cluster_rank"))
            except Exception:
                cluster_rank = None
        cluster_name_final = cluster_name.strip() if isinstance(cluster_name, str) else None
        rows.append(
            (
                str(name),
                str(s.get("status") or "-"),
                str(s.get("public_ip") or "-"),
                str(s.get("instance_id") or "-"),
                cluster_name_final or None,
                cluster_rank,
            )
        )

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    status_w = max(len("STATUS"), max(len(r[1]) for r in rows))
    ip_w = max(len("PUBLIC_IP"), max(len(r[2]) for r in rows))
    cluster_w = max(len("CLUSTER"), max(len(r[4] or "") for r in rows))

    def _cell(
        value: str,
        width: int,
        *,
        fg: str | None = None,
        bold: bool = False,
    ) -> str:
        padded = value.ljust(width)
        if fg is None and not bold:
            return padded
        return typer.style(padded, fg=fg, bold=bold)

    def _status_cell(status: str) -> str:
        normalized = status.strip().lower()
        color = None
        bold = False
        if normalized == "running":
            color = typer.colors.GREEN
            bold = True
        elif normalized in {"starting", "pending", "stopping"}:
            color = typer.colors.YELLOW
        elif normalized == "stopped":
            color = typer.colors.BRIGHT_BLACK
        elif normalized in {"error", "failed", "terminated", "missing"}:
            color = typer.colors.RED
            bold = True
        return _cell(status, status_w, fg=color, bold=bold)

    header_plain = (
        f"{'NAME'.ljust(name_w)}  {'STATUS'.ljust(status_w)}  {'PUBLIC_IP'.ljust(ip_w)}  {'CLUSTER'.ljust(cluster_w)}  INSTANCE_ID"
    )
    header = "  ".join(
        [
            _cell("NAME", name_w, fg=typer.colors.CYAN, bold=True),
            _cell("STATUS", status_w, fg=typer.colors.CYAN, bold=True),
            _cell("PUBLIC_IP", ip_w, fg=typer.colors.CYAN, bold=True),
            _cell("CLUSTER", cluster_w, fg=typer.colors.CYAN, bold=True),
            typer.style("INSTANCE_ID", fg=typer.colors.CYAN, bold=True),
        ]
    )
    typer.echo(header)
    typer.echo(typer.style("-" * len(header_plain), fg=typer.colors.BRIGHT_BLACK))

    def _sort_key(r: tuple[str, str, str, str, str | None, int | None]) -> tuple[Any, ...]:
        cname = r[4]
        if cname:
            return (0, cname, r[5] if r[5] is not None else 10**9, r[0])
        return (1, r[0])

    for r in sorted(rows, key=_sort_key):
        cluster = r[4] or ""
        typer.echo(
            "  ".join(
                [
                    _cell(r[0], name_w, fg=typer.colors.WHITE, bold=True),
                    _status_cell(r[1]),
                    _cell(r[2], ip_w, fg=typer.colors.BLUE if r[2] != "-" else typer.colors.BRIGHT_BLACK),
                    _cell(cluster, cluster_w, fg=typer.colors.MAGENTA if cluster else None),
                    typer.style(r[3], fg=typer.colors.BRIGHT_BLACK),
                ]
            )
        )


@app.command(no_args_is_help=True)
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


@app.command(no_args_is_help=True)
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
    erdma: bool | None = typer.Option(
        None,
        "--erdma/--no-erdma",
        help="Enable eRDMA by attaching an Elastic RDMA Interface (ERI). Default from config enable_erdma.",
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
    template = _normalize_typer_value(template)
    hostname = _normalize_typer_value(hostname)
    hostname_to_session = _normalize_typer_value(hostname_to_session)
    region_id = _normalize_typer_value(region_id)
    image_id = _normalize_typer_value(image_id)
    instance_type = _normalize_typer_value(instance_type)
    security_group_id = _normalize_typer_value(security_group_id)
    v_switch_id = _normalize_typer_value(v_switch_id)
    key_pair_name = _normalize_typer_value(key_pair_name)
    system_disk_category = _normalize_typer_value(system_disk_category)
    system_disk_size = _normalize_typer_value(system_disk_size)
    system_disk_performance_level = _normalize_typer_value(system_disk_performance_level)
    allocate_public_ip = _normalize_typer_value(allocate_public_ip)
    erdma = _normalize_typer_value(erdma)
    internet_max_bandwidth_out = _normalize_typer_value(internet_max_bandwidth_out)
    internet_charge_type = _normalize_typer_value(internet_charge_type)
    spot_strategy = _normalize_typer_value(spot_strategy)
    spot_price_limit = _normalize_typer_value(spot_price_limit)
    spot_duration = _normalize_typer_value(spot_duration)
    spot_interruption_behavior = _normalize_typer_value(spot_interruption_behavior)
    ssh_user = _normalize_typer_value(ssh_user)
    timeout_seconds = _normalize_typer_value(timeout_seconds)
    poll_interval_seconds = _normalize_typer_value(poll_interval_seconds)

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
    vsw = v_switch_id or effective_cfg.get("v_switch_id") or ""
    erdma_vsw = effective_cfg.get("erdma_v_switch_id") or ""
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
    vsw = _require(str(vsw), "v_switch_id")
    erdma_vsw = str(erdma_vsw).strip() or vsw
    keypair = _require(str(keypair), "key_pair_name")

    sg = security_group_id or effective_cfg.get("security_group_id") or ""
    sg = str(sg).strip()
    if not sg:
        try:
            sg = resolve_security_group_id_from_vswitch(region_id=region, v_switch_id=vsw)
            typer.echo(f"Info: security_group_id auto-selected from v_switch_id: {sg}")
        except EcsError as e:
            _die(
                "Missing required config: security_group_id. Set it via: "
                "ecs config set security_group_id=... \n"
                f"Auto-detect failed: {e}"
            )
        except Exception as e:
            _die(
                "Missing required config: security_group_id. Set it via: "
                "ecs config set security_group_id=... \n"
                f"Aliyun API error while auto-detecting: {e}"
            )

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
    erdma_final = bool(erdma) if erdma is not None else bool(effective_cfg.get("enable_erdma", False))
    erdma_auto_install_final = bool(effective_cfg.get("auto_install_erdma_driver", True))

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
    user_data_final = effective_cfg.get("user_data")
    if user_data_final is not None:
        user_data_final = str(user_data_final)
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

    if erdma_final:
        try:
            assert_instance_type_supports_erdma(region_id=region, instance_type=itype)
        except EcsError as e:
            _die(str(e))
        except Exception as e:
            _die(f"Aliyun API error: {e}")

        if user_data_final and user_data_final.strip():
            typer.echo(
                "eRDMA enabled; custom user_data detected, so built-in driver auto-install is skipped.",
                err=True,
            )
        elif erdma_auto_install_final:
            user_data_final = _default_erdma_driver_user_data()
            typer.echo(
                "eRDMA enabled; guest driver/software stack will be auto-installed on first boot (Linux images).",
                err=True,
            )
        else:
            typer.echo(
                "Note: eRDMA will configure the ERI network interface only; "
                "guest driver auto-install is disabled by config.",
                err=True,
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
            user_data=user_data_final,
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

    erdma_network_interface_id: str | None = None
    erdma_attached = False

    if erdma_final:
        typer.echo("eRDMA enabled; creating ERI network interface...")
        try:
            erdma_network_interface_id = create_erdma_network_interface(
                region_id=region,
                v_switch_id=erdma_vsw,
                security_group_id=sg,
                name=f"erdma-{name}",
                description=f"ERI for ecs session {name}",
                tags=[
                    {"Key": "ecs", "Value": "true"},
                    {"Key": "ecs_session", "Value": name},
                    {"Key": "ecs_erdma", "Value": "true"},
                ],
            )
        except Exception as e:
            _die(f"Failed to create ERI network interface for eRDMA: {e}")

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
        "erdma_enabled": erdma_final,
        "erdma_v_switch_id": erdma_vsw if erdma_final else None,
        "erdma_network_interface_id": erdma_network_interface_id,
        "erdma_attached": False,
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

    if erdma_final and erdma_network_interface_id:
        try:
            attach_network_interface(
                region_id=region,
                instance_id=instance_id,
                network_interface_id=erdma_network_interface_id,
            )
            try:
                set_network_interface_delete_on_release(
                    region_id=region,
                    network_interface_id=erdma_network_interface_id,
                    delete_on_release=True,
                )
            except Exception as e:
                typer.echo(f"Warning: failed to set DeleteOnRelease for ERI: {e}", err=True)
            record["erdma_attached"] = True
            _save(path, state)
            erdma_attached = True
        except ServerException as e:
            code = e.get_error_code() if hasattr(e, "get_error_code") else None
            # Some regions may require the instance to be Running before attaching.
            if code != "IncorrectInstanceStatus":
                # Best-effort cleanup for detached ENI.
                try:
                    delete_network_interface(region_id=region, network_interface_id=erdma_network_interface_id)
                except Exception:
                    pass
                _die(f"Failed to attach ERI network interface for eRDMA: {e}")
        except Exception as e:
            try:
                delete_network_interface(region_id=region, network_interface_id=erdma_network_interface_id)
            except Exception:
                pass
            _die(f"Failed to attach ERI network interface for eRDMA: {e}")

    try:
        start_instance(region_id=region, instance_id=instance_id)
    except EcsError as e:
        record["status"] = "StartFailed"
        record["last_error"] = str(e)
        _save(path, state)
        if erdma_network_interface_id and not erdma_attached:
            try:
                delete_network_interface(region_id=region, network_interface_id=erdma_network_interface_id)
            except Exception:
                pass
        _die(str(e))
    except Exception as e:
        record["status"] = "StartFailed"
        record["last_error"] = str(e)
        _save(path, state)
        if erdma_network_interface_id and not erdma_attached:
            try:
                delete_network_interface(region_id=region, network_interface_id=erdma_network_interface_id)
            except Exception:
                pass
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

        if erdma_final and erdma_network_interface_id and not record.get("erdma_attached"):
            typer.echo("Attaching ERI network interface for eRDMA (instance is Running)...")
            try:
                attach_network_interface(
                    region_id=region,
                    instance_id=instance_id,
                    network_interface_id=erdma_network_interface_id,
                )
                try:
                    set_network_interface_delete_on_release(
                        region_id=region,
                        network_interface_id=erdma_network_interface_id,
                        delete_on_release=True,
                    )
                except Exception as e:
                    typer.echo(f"Warning: failed to set DeleteOnRelease for ERI: {e}", err=True)
                record["erdma_attached"] = True
                _save(path, state)
            except Exception as e:
                record["last_error"] = f"eRDMA attach failed: {e}"
                _save(path, state)
                _die(f"eRDMA requested but failed to attach ERI: {e}")

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


@app.command("public-ip", no_args_is_help=True)
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
        True,
        "--prune/--no-prune",
        help="Remove local sessions whose instances no longer exist (default: enabled).",
    ),
    import_new: bool = typer.Option(
        True,
        "--import/--no-import",
        help="Import instances that are not present in local state (default: enabled).",
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
    - Automatically remove local sessions whose instances no longer exist (default: enabled)
    - Automatically import instances from the cloud that are not in local state (default: enabled, only instances tagged ecs=true)
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
        clusters = state.get("clusters") or {}
        if not isinstance(clusters, dict):
            clusters = {}
        cluster_by_instance_id: dict[str, tuple[str, int, str]] = {}
        for cname, crec in clusters.items():
            if not isinstance(crec, dict):
                continue
            for rank, node in _cluster_nodes_from_record(crec):
                instance_id = str(node.get("instance_id") or "").strip()
                if not instance_id:
                    continue
                node_name = str(node.get("name") or _cluster_node_name(str(cname), int(rank)))
                cluster_by_instance_id[instance_id] = (str(cname), int(rank), node_name)

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

            cluster_hint = cluster_by_instance_id.get(info.instance_id)
            if cluster_hint:
                cname, crank, preferred_name = cluster_hint
                base = preferred_name.strip() or (info.instance_name or "").strip() or info.instance_id
                new_name = base
                if new_name in sessions:
                    new_name = f"{base}-{info.instance_id}"
            else:
                base = (info.instance_name or "").strip() or info.instance_id
                new_name = base
                if new_name in sessions:
                    new_name = f"{base}-{info.instance_id}"

            rec: dict[str, Any] = {
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
            if cluster_hint:
                rec["cluster_name"] = cname
                rec["cluster_rank"] = int(crank)

            sessions[new_name] = rec
            imported += 1

    # Best-effort: ensure sessions carry cluster metadata if cluster records exist.
    clusters_final = state.get("clusters") or {}
    if isinstance(clusters_final, dict) and clusters_final:
        by_id: dict[str, tuple[str, int]] = {}
        for cname, crec in clusters_final.items():
            if not isinstance(crec, dict):
                continue
            for rank, node in _cluster_nodes_from_record(crec):
                iid = str(node.get("instance_id") or "").strip()
                if iid:
                    by_id[iid] = (str(cname), int(rank))
        for rec in sessions.values():
            if not isinstance(rec, dict):
                continue
            iid = str(rec.get("instance_id") or "").strip()
            if not iid:
                continue
            hint = by_id.get(iid)
            if not hint:
                continue
            if not rec.get("cluster_name"):
                rec["cluster_name"] = hint[0]
            if rec.get("cluster_rank") is None:
                rec["cluster_rank"] = int(hint[1])

    _save(path, state)

    if prune_missing and removed:
        typer.echo(f"Removed {len(removed)} missing sessions: {', '.join(removed)}")
    if marked_missing:
        typer.echo(f"Marked missing: {marked_missing}")
    typer.echo(f"Updated: {updated}, Imported: {imported}")


@app.command(no_args_is_help=True)
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

    # If this is a cluster node, keep the cluster record in sync.
    if isinstance(rec, dict):
        cname = rec.get("cluster_name")
        crank = rec.get("cluster_rank")
        if cname:
            clusters = state.get("clusters") or {}
            if isinstance(clusters, dict):
                crec = clusters.get(str(cname))
                if isinstance(crec, dict):
                    nodes_map = crec.get("nodes")
                    if isinstance(nodes_map, dict):
                        rank_key = None
                        if crank is not None:
                            try:
                                rank_key = str(int(crank))
                            except Exception:
                                rank_key = None
                        if rank_key and rank_key in nodes_map and isinstance(nodes_map.get(rank_key), dict):
                            nodes_map[rank_key]["name"] = new
                        else:
                            for v in nodes_map.values():
                                if isinstance(v, dict) and str(v.get("name") or "") == old:
                                    v["name"] = new
                                    break
                    crec["updated_at"] = now_iso_utc()
    _save(path, state)
    typer.echo("OK")


@app.command(no_args_is_help=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
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


@app.command(no_args_is_help=True, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
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


@app.command(no_args_is_help=True)
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

    # If this session is part of a cluster, remove it from the cluster record so
    # `ecs cluster list/delete` stays consistent.
    clusters = state.get("clusters") or {}
    if isinstance(clusters, dict):
        cname = sess.get("cluster_name")
        if cname is not None and not isinstance(cname, str):
            cname = str(cname)
        if cname:
            crec = clusters.get(cname)
            if isinstance(crec, dict):
                nodes_map = crec.get("nodes")
                if isinstance(nodes_map, dict):
                    rank_key = None
                    if sess.get("cluster_rank") is not None:
                        try:
                            rank_key = str(int(sess.get("cluster_rank")))
                        except Exception:
                            rank_key = None
                    if rank_key and rank_key in nodes_map:
                        nodes_map.pop(rank_key, None)
                    else:
                        inst_id = str(sess.get("instance_id") or "")
                        for k in list(nodes_map.keys()):
                            v = nodes_map.get(k)
                            if not isinstance(v, dict):
                                continue
                            if str(v.get("name") or "") == name or (inst_id and str(v.get("instance_id") or "") == inst_id):
                                nodes_map.pop(k, None)
                    if not nodes_map:
                        clusters.pop(cname, None)
                    else:
                        crec["updated_at"] = now_iso_utc()

    if not keep_record:
        sessions.pop(name, None)
    else:
        # Instance is gone; clear cluster hints so `ecs list` doesn't group it as a live node.
        sess.pop("cluster_name", None)
        sess.pop("cluster_rank", None)

    _save(path, state)

    typer.echo("OK")


@app.command(no_args_is_help=True)
def stop(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names, help="Session name or wildcard pattern, e.g. * or mpi*."),
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
    """Stop one or more ECS instances for matching sessions (to save cost)."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")
    targets = _resolve_session_targets(sessions, name)
    failed: list[str] = []
    for target in targets:
        try:
            _stop_one_session(
                path=path,
                state=state,
                name=target,
                force=force,
                mode=mode,
                wait=wait,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        except typer.Exit:
            failed.append(target)
            if len(targets) == 1:
                raise
    if failed:
        _die(f"Failed to stop {len(failed)}/{len(targets)} sessions: {', '.join(failed)}")


@app.command(no_args_is_help=True)
def start(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names, help="Session name or wildcard pattern, e.g. * or mpi*."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until instance is Running."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Max seconds to wait for Running."),
    poll_interval_seconds: int = typer.Option(5, "--poll-interval-seconds", help="Polling interval seconds."),
    allocate_public_ip: bool | None = typer.Option(
        None,
        "--allocate-public-ip/--no-allocate-public-ip",
        help="If enabled and no public IP is assigned, call AllocatePublicIpAddress. Default from config auto_allocate_public_ip.",
    ),
) -> None:
    """Start one or more ECS instances for matching sessions."""
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")
    targets = _resolve_session_targets(sessions, name)
    failed: list[str] = []
    for target in targets:
        try:
            _start_one_session(
                path=path,
                state=state,
                name=target,
                wait=wait,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                allocate_public_ip=allocate_public_ip,
            )
        except typer.Exit:
            failed.append(target)
            if len(targets) == 1:
                raise
    if failed:
        _die(f"Failed to start {len(failed)}/{len(targets)} sessions: {', '.join(failed)}")


@ssh_app.command("add", no_args_is_help=True)
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


@ssh_app.command("del", no_args_is_help=True)
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


@port_app.command("list", no_args_is_help=True)
def port_list(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
) -> None:
    """List open ports (ingress rules) for a session's security group."""
    _, state = _load(ctx)
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
    
    try:
        security_group_id = get_instance_security_group_id(region_id=region, instance_id=instance_id)
        if not security_group_id:
            _die(f"Could not determine security group ID for instance {instance_id}")
        
        rules = list_security_group_rules(region_id=region, security_group_id=security_group_id)
        
        if not rules:
            typer.echo("No ingress rules found (all ports are closed).")
            return
        
        # Filter and sort by port
        tcp_rules = [r for r in rules if r.protocol == "tcp"]
        udp_rules = [r for r in rules if r.protocol == "udp"]
        other_rules = [r for r in rules if r.protocol not in ("tcp", "udp")]
        
        def parse_port(port_range: str) -> int:
            """Extract port number from port range like '80/80'."""
            try:
                parts = port_range.split("/")
                return int(parts[0]) if parts else 0
            except (ValueError, IndexError):
                return 0
        
        def format_rule(r) -> str:
            desc = f" ({r.description})" if r.description else ""
            return f"{r.port_range:12} {r.protocol.upper():6} {r.source_cidr:18}{desc}"
        
        typer.echo(f"Security Group: {security_group_id}")
        typer.echo(f"{'PORT RANGE':12} {'PROTO':6} {'SOURCE':18} DESCRIPTION")
        typer.echo("-" * 70)
        
        for r in sorted(tcp_rules + udp_rules + other_rules, key=lambda x: (parse_port(x.port_range), x.protocol)):
            typer.echo(format_rule(r))
    
    except EcsError as e:
        _die(str(e))
    except Exception as e:
        _die(f"Aliyun API error: {e}")


@port_app.command("open", no_args_is_help=True)
def port_open(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    port: int = typer.Argument(..., help="Port number to open (e.g., 80, 443, 8080)."),
    protocol: str = typer.Option("tcp", "--protocol", "-p", help="Protocol: tcp, udp, etc. Default: tcp"),
    source: str = typer.Option("0.0.0.0/0", "--source", "-s", help="Source CIDR block. Default: 0.0.0.0/0 (all IPs)"),
    description: str | None = typer.Option(None, "--description", "-d", help="Rule description."),
) -> None:
    """Open a port in the security group for a session."""
    _, state = _load(ctx)
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
    
    if port < 1 or port > 65535:
        _die(f"Invalid port number: {port}. Must be between 1 and 65535.")
    
    protocol_lower = protocol.lower().strip()
    if protocol_lower not in ("tcp", "udp", "icmp", "gre", "all"):
        typer.echo(f"Warning: protocol '{protocol}' may not be standard. Common protocols: tcp, udp", err=True)
    
    try:
        security_group_id = get_instance_security_group_id(region_id=region, instance_id=instance_id)
        if not security_group_id:
            _die(f"Could not determine security group ID for instance {instance_id}")
        
        authorize_security_group_rule(
            region_id=region,
            security_group_id=security_group_id,
            port=port,
            protocol=protocol_lower,
            source_cidr=source,
            description=description,
        )
        
        desc_str = f" ({description})" if description else ""
        typer.echo(f"OK: Opened port {port}/{protocol_lower} from {source}{desc_str}")
    
    except EcsError as e:
        _die(str(e))
    except Exception as e:
        _die(f"Aliyun API error: {e}")


@port_app.command("close", no_args_is_help=True)
def port_close(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_session_names),
    port: int = typer.Argument(..., help="Port number to close (e.g., 80, 443, 8080)."),
    protocol: str = typer.Option("tcp", "--protocol", "-p", help="Protocol: tcp, udp, etc. Default: tcp"),
    source: str = typer.Option("0.0.0.0/0", "--source", "-s", help="Source CIDR block. Default: 0.0.0.0/0"),
) -> None:
    """Close a port in the security group for a session."""
    _, state = _load(ctx)
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
    
    if port < 1 or port > 65535:
        _die(f"Invalid port number: {port}. Must be between 1 and 65535.")
    
    protocol_lower = protocol.lower().strip()
    
    try:
        security_group_id = get_instance_security_group_id(region_id=region, instance_id=instance_id)
        if not security_group_id:
            _die(f"Could not determine security group ID for instance {instance_id}")
        
        revoke_security_group_rule(
            region_id=region,
            security_group_id=security_group_id,
            port=port,
            protocol=protocol_lower,
            source_cidr=source,
        )
        
        typer.echo(f"OK: Closed port {port}/{protocol_lower} from {source}")
    
    except EcsError as e:
        _die(str(e))
    except Exception as e:
        _die(f"Aliyun API error: {e}")


def _ensure_clusters_dict(state: dict[str, Any]) -> dict[str, Any]:
    clusters = state.get("clusters")
    if not isinstance(clusters, dict):
        clusters = {}
        state["clusters"] = clusters
    return clusters


def _cluster_nodes_from_record(cluster: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    nodes_raw = cluster.get("nodes") or {}
    if not isinstance(nodes_raw, dict):
        return []

    out: list[tuple[int, dict[str, Any]]] = []
    for k, v in nodes_raw.items():
        try:
            rank = int(str(k).strip())
        except Exception:
            continue
        if not isinstance(v, dict):
            continue
        out.append((rank, v))
    out.sort(key=lambda x: x[0])
    return out


def _cluster_node_name(cluster_name: str, rank: int) -> str:
    return f"{cluster_name}-{rank}"


@cluster_app.command("create", no_args_is_help=True)
def cluster_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Cluster name. Nodes are named <cluster>-<rank>."),
    template: str = typer.Option(
        ...,
        "--template",
        "-t",
        autocompletion=_complete_template_names,
        help="Template name used for all nodes (from `ecs template`).",
    ),
    count: int = typer.Option(..., "--count", "-n", help="Number of nodes to create."),
    start_rank: int = typer.Option(0, "--start-rank", help="Start rank (default: 0)."),
    stop: bool = typer.Option(False, "--stop", help="Stop each node after creation completes."),
) -> None:
    """Create a cluster by creating multiple instances from a template."""
    if int(count) < 1:
        _die("--count must be >= 1")
    if int(start_rank) < 0:
        _die("--start-rank must be >= 0")
    path, state = _load(ctx)
    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    templates = state.get("templates") or {}
    if not isinstance(templates, dict):
        _die("State file is corrupted: templates is not a dict.")
    if template not in templates:
        _die(f"Template not found: {template}")

    clusters = _ensure_clusters_dict(state)
    if name in clusters:
        _die(f"Cluster already exists: {name}")

    # Prevent accidental overlap with existing records.
    for i in range(int(count)):
        r = int(start_rank) + i
        node = _cluster_node_name(name, r)
        if node in sessions:
            _die(f"Session already exists: {node} (cannot create cluster {name})")

    cluster_rec: dict[str, Any] = {
        "name": name,
        "template": template,
        "created_at": now_iso_utc(),
        "updated_at": now_iso_utc(),
        "nodes": {},
    }
    clusters[name] = cluster_rec
    _save(path, state)

    created = 0
    for i in range(int(count)):
        rank = int(start_rank) + i
        node_name = _cluster_node_name(name, rank)
        try:
            create(ctx, name=node_name, template=template)
        except typer.Exit:
            typer.echo(f"Cluster create stopped after {created}/{count} nodes.", err=True)
            raise

        # Re-load (create() persists state internally).
        path2, state2 = _load(ctx)
        sessions2 = state2.get("sessions") or {}
        if not isinstance(sessions2, dict):
            _die("State file is corrupted: sessions is not a dict.")
        sess = sessions2.get(node_name)
        if not isinstance(sess, dict):
            _die(f"Created session record missing: {node_name}")

        sess["cluster_name"] = name
        sess["cluster_rank"] = int(rank)

        clusters2 = _ensure_clusters_dict(state2)
        crec = clusters2.get(name)
        if not isinstance(crec, dict):
            crec = cluster_rec
            clusters2[name] = crec
        nodes = crec.get("nodes")
        if not isinstance(nodes, dict):
            nodes = {}
            crec["nodes"] = nodes
        nodes[str(rank)] = {
            "name": node_name,
            "instance_id": str(sess.get("instance_id") or ""),
            "region_id": str(sess.get("region_id") or ""),
        }
        if not crec.get("region_id"):
            crec["region_id"] = str(sess.get("region_id") or "")
        crec["updated_at"] = now_iso_utc()
        _save(path2, state2)

        if stop:
            _stop_one_session(
                path=path2,
                state=state2,
                name=node_name,
                force=False,
                mode="stop-charging",
                wait=True,
                timeout_seconds=300,
                poll_interval_seconds=5,
            )
        created += 1

    typer.echo(f"OK: created cluster {name} with {created} nodes")


@cluster_app.command("expand", no_args_is_help=True)
def cluster_expand(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_cluster_names, help="Cluster name."),
    count: int = typer.Option(..., "--count", "-n", help="Number of nodes to add."),
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        autocompletion=_complete_template_names,
        help="Optional template override. Default: cluster's template.",
    ),
    stop: bool = typer.Option(False, "--stop", help="Stop each new node after creation completes."),
) -> None:
    """Expand a cluster by adding more nodes."""
    if int(count) < 1:
        _die("--count must be >= 1")
    path, state = _load(ctx)
    clusters = _ensure_clusters_dict(state)
    crec = clusters.get(name)
    if not isinstance(crec, dict):
        _die(f"Cluster not found: {name}")

    templates = state.get("templates") or {}
    if not isinstance(templates, dict):
        _die("State file is corrupted: templates is not a dict.")

    template_final = str(template or crec.get("template") or "").strip()
    if not template_final:
        _die("Cluster record missing template; pass --template.")
    if template_final not in templates:
        _die(f"Template not found: {template_final}")

    nodes = _cluster_nodes_from_record(crec)
    next_rank = (nodes[-1][0] + 1) if nodes else 0

    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    for i in range(int(count)):
        node_name = _cluster_node_name(name, next_rank + i)
        if node_name in sessions:
            _die(f"Session already exists: {node_name} (cannot expand cluster {name})")

    added = 0
    for i in range(int(count)):
        rank = next_rank + i
        node_name = _cluster_node_name(name, rank)
        try:
            create(ctx, name=node_name, template=template_final)
        except typer.Exit:
            typer.echo(f"Cluster expand stopped after {added}/{count} new nodes.", err=True)
            raise

        path2, state2 = _load(ctx)
        sessions2 = state2.get("sessions") or {}
        if not isinstance(sessions2, dict):
            _die("State file is corrupted: sessions is not a dict.")
        sess = sessions2.get(node_name)
        if not isinstance(sess, dict):
            _die(f"Created session record missing: {node_name}")

        sess["cluster_name"] = name
        sess["cluster_rank"] = int(rank)

        clusters2 = _ensure_clusters_dict(state2)
        crec2 = clusters2.get(name)
        if not isinstance(crec2, dict):
            crec2 = {"name": name, "template": template_final, "created_at": now_iso_utc(), "updated_at": now_iso_utc(), "nodes": {}}
            clusters2[name] = crec2
        if not crec2.get("template"):
            crec2["template"] = template_final
        nodes2 = crec2.get("nodes")
        if not isinstance(nodes2, dict):
            nodes2 = {}
            crec2["nodes"] = nodes2
        nodes2[str(rank)] = {
            "name": node_name,
            "instance_id": str(sess.get("instance_id") or ""),
            "region_id": str(sess.get("region_id") or ""),
        }
        if not crec2.get("region_id"):
            crec2["region_id"] = str(sess.get("region_id") or "")
        crec2["updated_at"] = now_iso_utc()
        _save(path2, state2)

        if stop:
            _stop_one_session(
                path=path2,
                state=state2,
                name=node_name,
                force=False,
                mode="stop-charging",
                wait=True,
                timeout_seconds=300,
                poll_interval_seconds=5,
            )
        added += 1

    typer.echo(f"OK: expanded cluster {name} by {added} nodes")


@cluster_app.command("stop", no_args_is_help=True)
def cluster_stop(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_cluster_names, help="Cluster name."),
    force: bool = typer.Option(False, "--force", help="Force stop the instances."),
    mode: str = typer.Option(
        "stop-charging",
        "--mode",
        help="stop-charging (recommended) or keep-charging.",
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until instances are Stopped."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Max seconds to wait for each node."),
    poll_interval_seconds: int = typer.Option(5, "--poll-interval-seconds", help="Polling interval seconds."),
) -> None:
    """Stop all instances in a cluster."""
    path, state = _load(ctx)
    clusters = _ensure_clusters_dict(state)
    crec = clusters.get(name)
    if not isinstance(crec, dict):
        _die(f"Cluster not found: {name}")

    nodes = _cluster_nodes_from_record(crec)
    if not nodes:
        typer.echo("Cluster has no recorded nodes.")
        return

    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    failed: list[str] = []
    stopped = 0
    for rank, node in nodes:
        node_name = str(node.get("name") or _cluster_node_name(name, rank))
        if node_name not in sessions:
            failed.append(node_name)
            typer.echo(f"Warning: session record missing for node {node_name}; skipped.", err=True)
            continue
        try:
            _stop_one_session(
                path=path,
                state=state,
                name=node_name,
                force=force,
                mode=mode,
                wait=wait,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
            stopped += 1
        except typer.Exit:
            failed.append(node_name)

    crec["updated_at"] = now_iso_utc()
    _save(path, state)

    if failed:
        _die(f"Cluster stop incomplete. Stopped: {stopped}, Failed/skipped: {len(failed)}")

    typer.echo(f"OK: stopped cluster {name} ({stopped} nodes)")


@cluster_app.command("start", no_args_is_help=True)
def cluster_start(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_cluster_names, help="Cluster name."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until instances are Running."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Max seconds to wait for each node."),
    poll_interval_seconds: int = typer.Option(5, "--poll-interval-seconds", help="Polling interval seconds."),
    allocate_public_ip: bool | None = typer.Option(
        None,
        "--allocate-public-ip/--no-allocate-public-ip",
        help="If enabled and a node has no public IP, call AllocatePublicIpAddress. Default from config auto_allocate_public_ip.",
    ),
) -> None:
    """Start all instances in a cluster."""
    path, state = _load(ctx)
    clusters = _ensure_clusters_dict(state)
    crec = clusters.get(name)
    if not isinstance(crec, dict):
        _die(f"Cluster not found: {name}")

    nodes = _cluster_nodes_from_record(crec)
    if not nodes:
        typer.echo("Cluster has no recorded nodes.")
        return

    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    failed: list[str] = []
    started = 0
    for rank, node in nodes:
        node_name = str(node.get("name") or _cluster_node_name(name, rank))
        if node_name not in sessions:
            failed.append(node_name)
            typer.echo(f"Warning: session record missing for node {node_name}; skipped.", err=True)
            continue
        try:
            _start_one_session(
                path=path,
                state=state,
                name=node_name,
                wait=wait,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                allocate_public_ip=allocate_public_ip,
            )
            started += 1
        except typer.Exit:
            failed.append(node_name)

    crec["updated_at"] = now_iso_utc()
    _save(path, state)

    if failed:
        _die(f"Cluster start incomplete. Started: {started}, Failed/skipped: {len(failed)}")

    typer.echo(f"OK: started cluster {name} ({started} nodes)")


@cluster_app.command("delete", no_args_is_help=True)
def cluster_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., autocompletion=_complete_cluster_names, help="Cluster name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt for confirmation."),
    force: bool = typer.Option(True, "--force/--no-force", help="Use Force=True for DeleteInstance."),
) -> None:
    """Delete all instances in a cluster and remove local records."""
    path, state = _load(ctx)
    clusters = _ensure_clusters_dict(state)
    crec = clusters.get(name)
    if not isinstance(crec, dict):
        _die(f"Cluster not found: {name}")

    nodes = _cluster_nodes_from_record(crec)
    if not nodes:
        if not yes:
            confirmed = typer.confirm(f"Cluster {name} has no recorded nodes. Remove cluster record anyway?")
            if not confirmed:
                raise typer.Exit(1)
        clusters.pop(name, None)
        _save(path, state)
        typer.echo("OK")
        return

    if not yes:
        confirmed = typer.confirm(f"Delete cluster {name} with {len(nodes)} nodes?")
        if not confirmed:
            raise typer.Exit(1)

    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        _die("State file is corrupted: sessions is not a dict.")

    failed: list[str] = []
    deleted: list[str] = []

    for rank, node in nodes:
        node_name = str(node.get("name") or _cluster_node_name(name, rank))
        instance_id = str(node.get("instance_id") or "")
        region_id = str(node.get("region_id") or crec.get("region_id") or "")

        # Prefer session record for region/instance_id if present.
        sess = sessions.get(node_name)
        if isinstance(sess, dict):
            if not instance_id:
                instance_id = str(sess.get("instance_id") or "")
            if not region_id:
                region_id = str(sess.get("region_id") or "")

        if not region_id or not instance_id:
            failed.append(node_name)
            typer.echo(f"Warning: missing region_id/instance_id for node {node_name}; skipped.", err=True)
            continue

        try:
            delete_instance(region_id=region_id, instance_id=instance_id, force=force)
            deleted.append(node_name)
        except Exception as e:
            failed.append(node_name)
            typer.echo(f"Warning: failed to delete {node_name} ({instance_id}): {e}", err=True)
            continue

        try:
            ssh_config_remove(ssh_config_path(), node_name)
        except Exception:
            pass

        sessions.pop(node_name, None)
        # Remove from cluster nodes map as we go.
        nodes_map = crec.get("nodes")
        if isinstance(nodes_map, dict):
            nodes_map.pop(str(rank), None)

    if not failed:
        clusters.pop(name, None)
    crec["updated_at"] = now_iso_utc()
    _save(path, state)

    if failed:
        _die(f"Cluster delete incomplete. Deleted: {len(deleted)}, Failed/skipped: {len(failed)}")

    typer.echo("OK")


@cluster_app.command("list")
def cluster_list(
    ctx: typer.Context,
    show_nodes: bool = typer.Option(True, "--nodes/--no-nodes", help="Show node details."),
) -> None:
    """List clusters from the local state file."""
    _, state = _load(ctx)
    clusters = state.get("clusters") or {}
    if not isinstance(clusters, dict) or not clusters:
        typer.echo("(no clusters)")
        return

    sessions = state.get("sessions") or {}
    if not isinstance(sessions, dict):
        sessions = {}

    rows: list[tuple[str, str, int]] = []
    node_rows: list[tuple[str, str, str, str, str]] = []
    for cname, crec in clusters.items():
        if not isinstance(crec, dict):
            continue
        template = str(crec.get("template") or "-")
        nodes = _cluster_nodes_from_record(crec)
        rows.append((str(cname), template, len(nodes)))

        for rank, node in nodes:
            node_name = str(node.get("name") or _cluster_node_name(str(cname), rank))
            sess = sessions.get(node_name)
            status = "-"
            ip = "-"
            instance_id = str(node.get("instance_id") or "-")
            if isinstance(sess, dict):
                status = str(sess.get("status") or "-")
                ip = str(sess.get("public_ip") or sess.get("private_ip") or "-")
                if instance_id == "-" or not instance_id.strip():
                    instance_id = str(sess.get("instance_id") or "-")
            node_rows.append((str(rank), node_name, status, ip, instance_id))

    if not rows:
        typer.echo("(no clusters)")
        return

    name_w = max(len("CLUSTER"), max(len(r[0]) for r in rows))
    tmpl_w = max(len("TEMPLATE"), max(len(r[1]) for r in rows))
    nodes_w = max(len("NODES"), max(len(str(r[2])) for r in rows))
    rank_w = max(len("RANK"), max((len(r[0]) for r in node_rows), default=1))
    node_name_w = max(len("NAME"), max((len(r[1]) for r in node_rows), default=1))
    node_status_w = max(len("STATUS"), max((len(r[2]) for r in node_rows), default=1))
    node_ip_w = max(len("IP"), max((len(r[3]) for r in node_rows), default=1))

    def _cell(
        value: str,
        width: int,
        *,
        align: str = "left",
        fg: str | None = None,
        bold: bool = False,
        dim: bool = False,
    ) -> str:
        padded = value.ljust(width) if align == "left" else value.rjust(width)
        if fg is None and not bold and not dim:
            return padded
        return typer.style(padded, fg=fg, bold=bold, dim=dim)

    def _status_cell(status: str) -> str:
        normalized = status.strip().lower()
        color = None
        bold = False
        if normalized == "running":
            color = typer.colors.GREEN
            bold = True
        elif normalized in {"starting", "pending", "stopping"}:
            color = typer.colors.YELLOW
        elif normalized in {"stopped"}:
            color = typer.colors.BRIGHT_BLACK
        elif normalized in {"error", "failed", "terminated", "missing"}:
            color = typer.colors.RED
            bold = True
        return _cell(status, node_status_w, fg=color, bold=bold)

    header_plain = f"{'CLUSTER'.ljust(name_w)}  {'TEMPLATE'.ljust(tmpl_w)}  {'NODES'.rjust(nodes_w)}"
    header = "  ".join(
        [
            _cell("CLUSTER", name_w, fg=typer.colors.CYAN, bold=True),
            _cell("TEMPLATE", tmpl_w, fg=typer.colors.CYAN, bold=True),
            _cell("NODES", nodes_w, align="right", fg=typer.colors.CYAN, bold=True),
        ]
    )
    typer.echo(header)
    typer.echo(typer.style("-" * len(header_plain), fg=typer.colors.BRIGHT_BLACK))

    sorted_rows = sorted(rows, key=lambda x: x[0])
    for idx, (cname, tmpl, n) in enumerate(sorted_rows):
        typer.echo(
            "  ".join(
                [
                    _cell(cname, name_w, fg=typer.colors.BRIGHT_CYAN, bold=True),
                    _cell(tmpl, tmpl_w, fg=typer.colors.MAGENTA),
                    _cell(str(n), nodes_w, align="right", fg=typer.colors.YELLOW, bold=True),
                ]
            )
        )
        if not show_nodes:
            if idx != len(sorted_rows) - 1:
                typer.echo("")
            continue

        crec = clusters.get(cname)
        if not isinstance(crec, dict):
            continue
        nodes = _cluster_nodes_from_record(crec)
        if nodes:
            node_header_plain = (
                f"  {'RANK'.rjust(rank_w)}  {'NAME'.ljust(node_name_w)}  {'STATUS'.ljust(node_status_w)}  {'IP'.ljust(node_ip_w)}  INSTANCE_ID"
            )
            typer.echo(
                "  "
                + "  ".join(
                    [
                        _cell("RANK", rank_w, align="right", fg=typer.colors.BRIGHT_BLACK, bold=True),
                        _cell("NAME", node_name_w, fg=typer.colors.BRIGHT_BLACK, bold=True),
                        _cell("STATUS", node_status_w, fg=typer.colors.BRIGHT_BLACK, bold=True),
                        _cell("IP", node_ip_w, fg=typer.colors.BRIGHT_BLACK, bold=True),
                        typer.style("INSTANCE_ID", fg=typer.colors.BRIGHT_BLACK, bold=True),
                    ]
                )
            )
        for rank, node in nodes:
            node_name = str(node.get("name") or _cluster_node_name(cname, rank))
            sess = sessions.get(node_name)
            status = "-"
            ip = "-"
            instance_id = str(node.get("instance_id") or "-")
            if isinstance(sess, dict):
                status = str(sess.get("status") or "-")
                ip = str(sess.get("public_ip") or sess.get("private_ip") or "-")
                if instance_id == "-" or not instance_id.strip():
                    instance_id = str(sess.get("instance_id") or "-")
            typer.echo(
                "  "
                + "  ".join(
                    [
                        _cell(str(rank), rank_w, align="right", fg=typer.colors.BRIGHT_BLACK),
                        _cell(node_name, node_name_w, fg=typer.colors.WHITE),
                        _status_cell(status),
                        _cell(ip, node_ip_w, fg=typer.colors.BLUE if ip != "-" else typer.colors.BRIGHT_BLACK),
                        typer.style(instance_id, fg=typer.colors.BRIGHT_BLACK),
                    ]
                )
            )
        if idx != len(sorted_rows) - 1:
            typer.echo("")

