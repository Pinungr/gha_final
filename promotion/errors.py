"""Failure taxonomy.

Every failure mode listed in BRD section 15 maps to exactly one code here, so a
run's outcome can be traced back to the requirement it enforces.
"""

from __future__ import annotations

# --- Input / configuration -------------------------------------------------
E_NO_TARGET = "E_NO_TARGET"
E_BAD_TARGET = "E_BAD_TARGET"
E_NO_INPUT = "E_NO_INPUT"
E_BAD_CONFIG = "E_BAD_CONFIG"

# --- Path validation -------------------------------------------------------
E_ABS_PATH = "E_ABS_PATH"
E_TRAVERSAL = "E_TRAVERSAL"
E_BAD_PATH = "E_BAD_PATH"
E_DUP_PATH = "E_DUP_PATH"
E_CONFLICT_PATH = "E_CONFLICT_PATH"

# --- Repository state ------------------------------------------------------
E_BRANCH_MISSING = "E_BRANCH_MISSING"
E_MISSING_SOURCE = "E_MISSING_SOURCE"
E_NOT_A_FILE = "E_NOT_A_FILE"
E_BAD_DELETE = "E_BAD_DELETE"
E_BRANCH_EXISTS = "E_BRANCH_EXISTS"

# --- Change-set integrity --------------------------------------------------
E_UNEXPECTED_CHANGE = "E_UNEXPECTED_CHANGE"
E_WFLIST_SYNC = "E_WFLIST_SYNC"
E_NO_CHANGES = "E_NO_CHANGES"
E_PROTECTED_BRANCH = "E_PROTECTED_BRANCH"

# --- External tooling ------------------------------------------------------
E_GIT = "E_GIT"
E_GH = "E_GH"


class PromotionError(Exception):
    """A promotion failure that carries enough context to act on.

    ``details`` holds one entry per offending item -- BRD section 15 requires
    failures to list *every* problem (all missing source files, all duplicates)
    rather than stopping at the first.

    ``remedy`` states what the operator should change and re-run.
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: list[str] | None = None,
        remedy: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = list(details or [])
        self.remedy = remedy

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        parts.extend(f"  - {d}" for d in self.details)
        if self.remedy:
            parts.append(f"  fix: {self.remedy}")
        return "\n".join(parts)
