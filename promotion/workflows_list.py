"""The ``workflows_list.txt`` rebuild rule (BRD section 10).

When a promotion request promotes one or more workflow paths, the list file is
rebuilt from scratch to contain exactly those paths -- previous contents are
discarded, not merged. When the request promotes none, the file is left alone.
"""

from __future__ import annotations

from .config import Config
from .errors import E_WFLIST_SYNC, PromotionError
from .inventory import Inventory


def desired_content(inventory: Inventory, cfg: Config) -> str | None:
    """The exact bytes the list file must hold, or ``None`` to leave it alone."""
    paths = inventory.workflow_promote_paths
    if not paths:
        return None

    ordered: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return "\n".join(ordered) + "\n"


def verify(actual: str, inventory: Inventory, cfg: Config) -> None:
    """Confirm the rebuilt list file satisfies every section 10 constraint."""
    expected = desired_content(inventory, cfg)
    if expected is None:  # pragma: no cover - caller does not verify in this case
        return

    lines = [line.strip() for line in actual.splitlines()]
    entries = [line for line in lines if line]
    requested = set(inventory.workflow_promote_paths)

    problems: list[str] = []

    if len(entries) != len(set(entries)):
        dupes = sorted({e for e in entries if entries.count(e) > 1})
        problems.append(f"duplicate entries: {', '.join(dupes)}")

    for entry in entries:
        if not cfg.is_workflow_path(entry):
            problems.append(
                f"{entry} does not match the workflow path pattern "
                f"'{cfg.workflow_path_pattern}'"
            )
        elif entry not in requested:
            problems.append(
                f"{entry} is not a promoted workflow path in this request "
                f"(stale content was not fully removed)"
            )

    missing = [p for p in inventory.workflow_promote_paths if p not in set(entries)]
    if missing:
        problems.append(f"missing requested workflow path(s): {', '.join(missing)}")

    if actual != expected and not problems:
        problems.append(
            "file content does not match the expected rebuild "
            "(whitespace or line-ending mismatch)"
        )

    if problems:
        raise PromotionError(
            E_WFLIST_SYNC,
            f"{cfg.workflows_list_file} was not rebuilt correctly.",
            details=problems,
            remedy="This indicates a bug in the promotion pipeline; no Pull "
            "Request was created and no protected branch was modified.",
        )
