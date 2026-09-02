"""MASTER Pull Request planning.

MASTER opens a Pull Request directly from the staging branch to ``master`` and
does not create a release branch.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import Environment


def plan_pull_request(
    *, env: Environment, log: Callable[[str], None]
) -> tuple[None, str]:
    """Return the direct MASTER Pull Request plan."""
    log(f"PR target: {env.target} (no release branch for {env.name})")
    return None, env.target
