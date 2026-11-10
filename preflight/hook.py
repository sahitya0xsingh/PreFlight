"""Install or remove the ``pre-push`` hook that runs ``preflight check``."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from . import gitinfo

MARKER = "# installed by preflight"


def hook_path(root: Path) -> Path:
    hooks = gitinfo.git(root, "rev-parse", "--git-path", "hooks")
    p = Path(hooks)
    return (p if p.is_absolute() else root / p) / "pre-push"


def install(root: Path, extra_args: list[str], force: bool) -> Path:
    path = hook_path(root)
    if path.exists() and MARKER not in path.read_text() and not force:
        raise SystemExit(
            f"preflight: {path} already exists and was not written by preflight; "
            "rerun with --force to replace it"
        )
    args = " ".join(shlex.quote(a) for a in extra_args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"{MARKER}\n"
        "# Skip once with: PREFLIGHT_SKIP=1 git push   (or git push --no-verify)\n"
        '[ "$PREFLIGHT_SKIP" = "1" ] && exit 0\n'
        f"exec {shlex.quote(sys.executable)} -m preflight check --hook {args}\n"
    )
    path.chmod(0o755)
    return path


def uninstall(root: Path) -> bool:
    path = hook_path(root)
    if path.exists() and MARKER in path.read_text():
        path.unlink()
        return True
    return False
