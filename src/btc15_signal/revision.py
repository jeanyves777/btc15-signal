"""What source the running process is actually executing.

"432c388 plus fifteen changed files" is not a release identifier - it does not
say which fifteen, or what was in them, and it cannot be checked afterwards.
A deployed trading service has to be able to answer "what code is this?" from
its own runtime state, not from whatever the working tree happens to look like
when somebody asks.

TWO ANSWERS, because either alone can lie:

    commit      what git says HEAD is, and whether the tree was dirty when the
                process started. Reproducible by anyone with the repository.
    fingerprint a SHA-256 over the package's own `.py` files, in path order.
                Independent of git entirely, so it still identifies the build
                on a machine with no repository, and it catches an edit made
                after the commit - which is the case `git rev-parse` alone
                reports as clean.

The fingerprint is taken ONCE, at import, from the files the interpreter
actually loaded. An edit after startup changes the working tree and not this,
which is the point: it describes the running process, not the directory.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
PROJECT = PACKAGE.parents[1]


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=PROJECT, capture_output=True, text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def source_fingerprint() -> str:
    """SHA-256 over every `.py` in the package, path-ordered. 12 hex chars."""
    digest = hashlib.sha256()
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(PACKAGE).as_posix().encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()[:12]


def describe() -> dict:
    """Everything needed to identify this build, resolved once at startup."""
    commit = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    return {
        "commit": commit or "unknown",
        "short": (commit[:7] if commit else "unknown"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty": dirty,
        "fingerprint": source_fingerprint(),
        "subject": _git("log", "-1", "--format=%s"),
    }


# Resolved at import: this is the source the interpreter loaded, and a later
# edit to the working tree must not change what the running process reports.
REVISION = describe()


def line() -> str:
    state = "DIRTY" if REVISION["dirty"] else "clean"
    return (
        f"revision {REVISION['short']} ({REVISION['branch']}, {state}) "
        f"source {REVISION['fingerprint']}"
    )
