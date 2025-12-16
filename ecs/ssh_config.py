from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .util import null_device


_BEGIN_PREFIX = "# >>> ecs session:"
_END_PREFIX = "# <<< ecs session:"


def ssh_config_path() -> Path:
    # Windows: %USERPROFILE%\.ssh\config ; POSIX: ~/.ssh/config
    return Path.home() / ".ssh" / "config"


def _sanitize_host_alias(name: str) -> str:
    # SSH Host patterns: keep it simple for tab completion and portability
    s = (name or "").strip()
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "session"


@dataclass(frozen=True)
class SshConfigEntry:
    session_name: str
    host_alias: str
    host_name: str
    user: str = "root"
    identity_file: str | None = None
    forward_agent: bool = True
    identities_only: bool = True
    strict_host_key_checking: bool = False


def default_host_alias(session_name: str, prefix: str = "ecs-") -> str:
    return prefix + _sanitize_host_alias(session_name)


def render_entry(entry: SshConfigEntry) -> str:
    lines: list[str] = []
    lines.append(f"{_BEGIN_PREFIX} {entry.session_name}")
    lines.append(f"Host {entry.host_alias}")
    lines.append(f"  HostName {entry.host_name}")
    lines.append(f"  User {entry.user}")
    if entry.identity_file:
        # OpenSSH accepts Windows paths with backslashes, but forward slashes are safer.
        identity = entry.identity_file.replace("\\", "/")
        lines.append(f"  IdentityFile {identity}")
    if entry.forward_agent:
        lines.append("  ForwardAgent yes")
    if entry.identities_only:
        lines.append("  IdentitiesOnly yes")
    if not entry.strict_host_key_checking:
        lines.append("  StrictHostKeyChecking no")
        lines.append(f"  UserKnownHostsFile {null_device()}")
    lines.append(f"{_END_PREFIX} {entry.session_name}")
    return "\n".join(lines) + "\n"


def _remove_block(text: str, session_name: str) -> tuple[str, bool]:
    begin = f"{_BEGIN_PREFIX} {session_name}"
    end = f"{_END_PREFIX} {session_name}"
    lines = text.splitlines(True)  # keep newlines
    out: list[str] = []
    i = 0
    removed = False
    while i < len(lines):
        if lines[i].strip() == begin:
            removed = True
            i += 1
            while i < len(lines) and lines[i].strip() != end:
                i += 1
            if i < len(lines) and lines[i].strip() == end:
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "".join(out), removed


def upsert(path: Path, entry: SshConfigEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text2, _ = _remove_block(text, entry.session_name)
    # Keep a newline between existing content and our block.
    if text2 and not text2.endswith("\n"):
        text2 += "\n"
    text2 += render_entry(entry)
    path.write_text(text2, encoding="utf-8", newline="\n")


def remove(path: Path, session_name: str) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    text2, removed = _remove_block(text, session_name)
    if removed:
        path.write_text(text2, encoding="utf-8", newline="\n")
    return removed


