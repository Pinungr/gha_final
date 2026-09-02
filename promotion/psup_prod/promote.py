"""PSUP and PROD Pull Request planning.

Both routes create a timestamped release branch from their protected target and
open the staging Pull Request into that release branch.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import Config, Environment
from ..errors import E_BRANCH_EXISTS, PromotionError


def plan_pull_request(
    *,
    env: Environment,
    heads: dict[str, str],
    timestamp: str,
    cfg: Config,
    assert_push_allowed: Callable[[str, Config], None],
    log: Callable[[str], None],
) -> tuple[str, str]:
    """Create the PSUP/PROD release-branch plan without mutating Git."""
    release_branch = f"release/{timestamp}_{env.slug}"
    if release_branch in heads:
        raise PromotionError(
            E_BRANCH_EXISTS,
            f"The generated release branch '{release_branch}' already exists.",
            remedy="Re-run the workflow; a fresh release branch name will be "
            "generated from the new execution timestamp.",
        )
    assert_push_allowed(release_branch, cfg)
    log(f"Release branch: {release_branch}")
    return release_branch, release_branch
