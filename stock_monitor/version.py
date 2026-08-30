"""Build identification, surfaced in /health and the dashboard header.

Lets you tell at a glance which revision a running instance is on — the
difference between "the feature is broken" and "this server is on an older
deploy" is otherwise invisible from the browser.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

# Bump when a change should be visibly identifiable even without git metadata.
FEATURES_VERSION = "4.3"


@lru_cache(maxsize=1)
def git_revision() -> str:
    """Short commit SHA, or 'unknown' when git metadata is unavailable.

    Render exposes the deployed commit as RENDER_GIT_COMMIT; locally we ask git
    directly. Both paths are best-effort — this is diagnostics, never a
    dependency.
    """
    render_sha = os.environ.get("RENDER_GIT_COMMIT", "")
    if render_sha:
        return render_sha[:7]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


#: How long startup spent before the HTTP port opened, and on what. Set once by
#: run_app; surfaced in /health because that is the endpoint whose timeout
#: raises the question, and reading it should not mean going to the host's logs.
startup_timings: dict = {}


def record_startup(total: float, phases: dict) -> None:
    startup_timings.clear()
    startup_timings.update({"seconds": round(total, 2), "phases": phases})


def build_info() -> dict:
    info = {"version": FEATURES_VERSION, "revision": git_revision()}
    if startup_timings:
        info["startup"] = startup_timings
    return info


def build_label() -> str:
    info = build_info()
    return f"v{info['version']} ({info['revision']})"
