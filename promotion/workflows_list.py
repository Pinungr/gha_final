"""Safe, deterministic maintenance of ``workflows_list.txt``."""

from __future__ import annotations

import re

from .config import Config
from .errors import E_WFLIST_SYNC, PromotionError


def _normalize_entry(raw: str, cfg: Config) -> str | None:
    """Normalize harmless spelling variants without guessing different paths."""
    entry = raw.strip()
    if not entry:
        return None
    entry = entry.replace("\\", "/")
    while entry.startswith("./"):
        entry = entry[2:]
    entry = re.sub(r"/+", "/", entry)
    if entry.startswith("/"):
        entry = entry.lstrip("/")
    if not cfg.is_workflow_path(entry):
        raise PromotionError(
            E_WFLIST_SYNC,
            f"{cfg.workflows_list_file} contains an entry that cannot be safely normalized.",
            details=[raw],
            remedy="Use canonical workflow paths such as workflows/example.json.",
        )
    return entry


def desired_content(
    existing: str, required_workflow_paths: list[str], cfg: Config
) -> str | None:
    """Merge canonical existing entries with workflow paths in the final PR."""
    if not required_workflow_paths:
        return None
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in existing.splitlines():
        path = _normalize_entry(raw, cfg)
        if path is not None and path not in seen:
            seen.add(path)
            ordered.append(path)
    for path in required_workflow_paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return "\n".join(ordered) + "\n"


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
