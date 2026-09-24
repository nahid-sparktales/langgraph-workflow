"""Workspace file checks and change detection for hosts that verify files
themselves (the fixture host and the agent-driven host).

Paths are workspace-relative; absolute paths, ``..`` and symlinks that leave
the workspace are refused. Command checks are never run here: executing
shell commands belongs to the host's permissioned tool path.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

SNAPSHOT_LIMIT = 4096  # files; matches Locus's recovery snapshot bound
MAX_FILE_BYTES = 64 * 1024 * 1024
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".build", "build", "dist"}


def safe_path(root: Path, relative: str) -> Path:
    """Resolve a workspace-relative path; refuse traversal and symlink escape."""
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not relative.strip():
        raise PermissionError(f"unsafe path {relative!r}")
    resolved = (root / candidate).resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise PermissionError(f"path escapes workspace {relative!r}")
    return resolved


def check_file(root: Path, check: dict) -> tuple[str, str]:
    """Evaluate one declared check. Returns ``(state, detail)``."""
    kind = check.get("kind")
    if kind == "human_review":
        return "needs_review", "requires human review"
    if kind not in ("file_exists", "file_contains", "json_value"):
        return "unsupported", f"{kind!r} checks are not run by this host"
    try:
        path = safe_path(root, str(check.get("path", "")))
    except PermissionError as error:
        return "denied", str(error)
    if not path.is_file():
        return "failed", "missing"
    data = path.read_bytes()
    fingerprint = hashlib.sha256(data).hexdigest()[:16]
    if kind == "file_exists":
        return "passed", fingerprint
    if kind == "file_contains":
        ok = str(check.get("value", "")) in data.decode(errors="replace")
        return ("passed" if ok else "failed"), fingerprint
    try:
        value: Any = json.loads(data)
        for part in str(check.get("pointer", "")).split("/")[1:]:
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (ValueError, KeyError, IndexError, TypeError):
        return "failed", fingerprint
    return ("passed" if value == check.get("value") else "failed"), fingerprint


def snapshot(root: Path, limit: int = SNAPSHOT_LIMIT) -> dict[str, str] | None:
    """Content fingerprints of workspace files, or ``None`` above ``limit``.

    Git workspaces list tracked and untracked, non-ignored files; other
    folders are walked without following links, skipping build/dependency
    directories.
    """
    listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root,
                            capture_output=True, timeout=30)
    if listed.returncode == 0:
        paths = [p for p in listed.stdout.decode(errors="replace").split("\0") if p]
    else:
        paths = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            paths.extend(str((Path(directory) / f).relative_to(root)) for f in files)
            if len(paths) > limit:
                break
    if len(paths) > limit:
        return None
    result = {}
    for relative in sorted(paths):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            result[relative] = f"size:{path.stat().st_size}:{path.stat().st_mtime_ns}"
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def changed(before: dict[str, str] | None, after: dict[str, str] | None) -> list[str] | None:
    """Paths that differ, or ``None`` when either side was over the limit."""
    if before is None or after is None:
        return None
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
