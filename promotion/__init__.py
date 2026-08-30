"""Automated release promotion pipeline.

Reads a validated ``promotion.txt`` from a user-created staging branch, applies
the source branch's files to that same branch, and opens a Pull Request to the
configured target. The source-of-truth branches are never written to.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
