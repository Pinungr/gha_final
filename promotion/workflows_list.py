"""Safe, deterministic maintenance of ``workflows_list.txt``."""

from __future__ import annotations

from .config import Config
from .errors import E_WFLIST_SYNC, PromotionError

_WORKFLOWS_PREFIX = "workflows/"


def _relative_path(repository_path: str, cfg: Config) -> str:
    """Convert a repository workflow path to its list-file representation."""
    if not repository_path.startswith(_WORKFLOWS_PREFIX) or not cfg.is_workflow_path(
        repository_path
    ):
        raise PromotionError(
            E_WFLIST_SYNC,
            f"{cfg.workflows_list_file} cannot safely represent this workflow path.",
            details=[repository_path],
            remedy="Use repository workflow paths below workflows/.",
        )
    return repository_path[len(_WORKFLOWS_PREFIX) :]
def desired_content(
    existing: str,
    required_workflow_paths: list[str],
    available_workflow_paths: list[str],
    cfg: Config,
) -> str | None:
    """Build a fresh list from workflows actually promoted in this PR.

    ``existing`` and ``available_workflow_paths`` are retained in the signature
    for compatibility with callers, but are intentionally ignored. The list is
    a record of this promotion, not a historical workflow registry.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for repository_path in required_workflow_paths:
        path = _relative_path(repository_path, cfg)
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return "\n".join(ordered) + ("\n" if ordered else "")


def verify(actual: str, expected: str, cfg: Config) -> None:
    """Confirm the index holds the exact normalized merge."""
    if actual != expected:
        raise PromotionError(
            E_WFLIST_SYNC,
            f"{cfg.workflows_list_file} was not synchronized correctly.",
            details=["file content differs from the normalized expected content"],
            remedy="This indicates a bug in the promotion pipeline; no Pull "
            "Request was created and no protected branch was modified.",
        )
