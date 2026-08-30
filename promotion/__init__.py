"""Automated release promotion pipeline.

Reads a validated ``promotion.txt`` from a user-created temporary branch,
applies source files there, creates a release branch from the configured target,
and opens a Pull Request into that release branch. Source-of-truth branches are
never written to.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
