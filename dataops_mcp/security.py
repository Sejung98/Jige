"""Credential handling rules.

- Token/credential files are handled by **path only**; their contents are never read.
- Warn if file permissions are not 600 (owner only).
- Mask token-like strings in log tails returned in tool output.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

# GDC tokens are base64url-like strings hundreds of characters long. Mask runs of 40+ characters.
_TOKENISH = re.compile(r"[A-Za-z0-9_\-\.=+/]{40,}")
_KEYVAL = re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key)(\s*[:=]\s*)(\S+)")


def check_secret_file(path: str | os.PathLike | None, label: str) -> dict[str, Any]:
    """Check a credential file's status. Never opens its contents."""
    if not path:
        return {"label": label, "path": None, "exists": False, "ok": False,
                "warning": f"{label} path is not set in config."}
    p = Path(path)
    if not p.exists():
        return {"label": label, "path": str(p), "exists": False, "ok": False,
                "warning": f"{label} file not found: {p}"}
    mode = stat.S_IMODE(p.stat().st_mode)
    out: dict[str, Any] = {"label": label, "path": str(p), "exists": True, "mode": oct(mode)}
    if mode & 0o077:
        out["ok"] = False
        out["warning"] = (
            f"{label} file permissions are {oct(mode)}. It must be owner-only: chmod 600 {p}"
        )
    else:
        out["ok"] = True
    return out


# File names that look like credentials (used to block them as input files such as patient lists)
_SECRET_NAME = re.compile(r"(?i)(token|credential|creds|secret|passw|\.ngc$|\.pem$|\.key$|id_rsa|id_ed25519)")
_SECRET_DIRS = {".ssh", ".gdc", ".ega", ".aws", ".gnupg"}


def is_secret_path(path: str | os.PathLike, secret_paths: list[Path | None]) -> bool:
    """Whether the path is a credential path from config or looks like one. Never opens the file."""
    p = Path(os.path.expanduser(str(path))).resolve()
    for sp in secret_paths:
        if sp and p == Path(sp).expanduser().resolve():
            return True
    if _SECRET_DIRS.intersection(p.parts):
        return True
    return bool(_SECRET_NAME.search(p.name))


def nearest_existing(path: Path) -> Path:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def dir_exposure(path: Path) -> dict[str, Any]:
    """group/other access permissions of the target directory (or its nearest existing parent)."""
    p = nearest_existing(path)
    mode = stat.S_IMODE(p.stat().st_mode)
    return {
        "checked_path": str(p),
        "mode": oct(mode),
        "world_accessible": bool(mode & 0o007),
        "group_accessible": bool(mode & 0o070),
    }


# Long file names and paths are normal in job logs (e.g. TCGA BAM names, data_root paths) and must stay
# readable. Tokens (JWT, base64) never start with "/" and never end in a short file extension.
_FILE_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}(?:\.(?:gz|bz2|xz|zst))?$")


def _mask_tokenish(m: re.Match) -> str:
    s = m.group(0)
    if s.startswith("/") or s.startswith("./") or _FILE_EXT.search(s):
        return s
    return "***REDACTED***"


def redact(text: str) -> str:
    text = _KEYVAL.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _TOKENISH.sub(_mask_tokenish, text)


def script_has_inline_secret(text: str, secret_paths: list[Path]) -> bool:
    """Check whether a script contains a token/password value inline. Credentials must be passed only as file paths."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "X-Auth-Token" in stripped:
            return True
        if _KEYVAL.search(stripped) and not any(str(p) in stripped for p in secret_paths):
            return True
    return False
