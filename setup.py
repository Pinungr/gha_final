"""Sample packaging metadata."""

from setuptools import find_packages, setup

setup(
    name="billing-sample",
    version="1.0.0",
    description="Sample content for the automated release promotion pipeline",
    packages=find_packages(exclude=["test", "test.*"]),
    python_requires=">=3.9",
)
