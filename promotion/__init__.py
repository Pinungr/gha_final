"""Automated release promotion pipeline.

Assembles a user-specified set of file changes in a throwaway branch and opens a
Pull Request against a release branch. The source-of-truth branches (qa, psup,
prod) are never written to.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
