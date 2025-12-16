from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .util import now_iso_utc


ENV_STATE_FILE = "ECS_STATE_FILE"


def default_state_path() -> Path:
    env = os.getenv(ENV_STATE_FILE)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ecs" / "state.json"


def resolve_state_path(state_file: str | Path | None) -> Path:
    if state_file is None:
        return default_state_path()
    return Path(state_file).expanduser()


def default_config() -> dict[str, Any]:
    # Keep values JSON-friendly.
    return {
        # Aliyun ECS
        "region_id": "",
        "image_id": "",
        "instance_type": "",
        "security_group_id": "",
        "v_switch_id": "",
        "key_pair_name": "",
        # System disk (optional; leave null to let Aliyun decide defaults)
        # Common categories: cloud_efficiency | cloud_ssd | cloud_essd | cloud_auto
        "system_disk_category": None,
        "system_disk_size": None,  # GB
        "system_disk_performance_level": None,  # ESSD: PL0|PL1|PL2|PL3
        # Public IP allocation
        # If true, and instance has no public IP after Running, ecs will call AllocatePublicIpAddress.
        "auto_allocate_public_ip": True,
        "internet_charge_type": "PayByTraffic",
        "internet_max_bandwidth_out": 10,
        # Spot / preemptible instances
        # - NoSpot (default in ECS): normal pay-as-you-go
        # - SpotAsPriceGo: preemptible, auto bidding (recommended)
        # - SpotWithPriceLimit: preemptible with max price cap
        "spot_strategy": "SpotAsPriceGo",
        "spot_price_limit": None,  # only used when spot_strategy=SpotWithPriceLimit
        "spot_duration": None,  # stable duration (hours): 1-6, optional
        "spot_interruption_behavior": None,  # optional, e.g. Terminate
        # SSH
        "ssh_user": "root",
        "ssh_private_key_path": "",
        "ssh_strict_host_key_checking": False,
        # Polling / timeouts
        "timeout_seconds": 600,
        "poll_interval_seconds": 5,
        # Optional extra ssh args (list of strings), appended before user-supplied args.
        "ssh_extra_args": [],
        # Arbitrary metadata you want to carry around (dict).
        "meta": {},
    }


def new_state() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": now_iso_utc(),
        "config": default_config(),
        "sessions": {},  # name -> session record
    }


def normalize_state(raw: Any) -> dict[str, Any]:
    base = new_state()
    if isinstance(raw, dict):
        base.update(raw)

    cfg = base.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
    cfg_defaults = default_config()
    for k, v in cfg_defaults.items():
        cfg.setdefault(k, v)
    base["config"] = cfg

    sessions = base.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    base["sessions"] = sessions

    if "version" not in base:
        base["version"] = 1

    return base


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return new_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    return normalize_state(data)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_state(state)
    normalized["updated_at"] = now_iso_utc()

    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


