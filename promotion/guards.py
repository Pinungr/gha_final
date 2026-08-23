"""Safety assertions that back the BRD's non-negotiable invariants.

Section 14: the automation must never push to qa, psup or prod.
Sections 6 and 20: the change set must contain nothing the user did not request,
beyond the one permitted ``workflows_list.txt`` rebuild.
"""

from __future__ import annotations

from .config import Config
from .errors import E_NO_CHANGES, E_PROTECTED_BRANCH, E_UNEXPECTED_CHANGE, PromotionError


def assert_push_allowed(branch: str, cfg: Config) -> None:
    """Refuse to push to a source-of-truth branch."""
    if cfg.is_protected(branch):
        raise PromotionError(
            E_PROTECTED_BRANCH,
            f"Refusing to push to protected branch {branch!r}.",
            details=[
                "The pipeline only ever pushes generated temp/* and release/* "
                "branches.",
            ],
            remedy="This indicates a bug in the promotion pipeline. Check "
            "'protected_branches' and the generated branch names in the run log.",
        )


def assert_changes_expected(
    changes: list[tuple[str, str]],
    requested_paths: list[str],
    cfg: Config,
    allow_workflows_list: bool,
) -> None:
    """Verify the staged change set is exactly within the permitted path set."""
    if not changes:
        raise PromotionError(
            E_NO_CHANGES,
            "The requested promotion produces no change against the target branch.",
            details=[
                "Every requested file is already identical on the target branch.",
            ],
            remedy="Confirm the source branch actually contains the changes you "
            "expect, then re-run.",
        )

    allowed = set(requested_paths)
    if allow_workflows_list:
        allowed.add(cfg.workflows_list_file)

    unexpected = sorted({path for _, path in changes if path not in allowed})
    if unexpected:
        raise PromotionError(
            E_UNEXPECTED_CHANGE,
            f"{len(unexpected)} file(s) changed that were not requested.",
            details=unexpected,
            remedy="No Pull Request was created and no protected branch was "
            "modified. Report this: the pipeline must only touch requested paths.",
        )
