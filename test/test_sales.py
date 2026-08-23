"""Sample test file."""

SCHEMA_VERSION = "1.0.0"

TAX_RATE = 0.18


def test_tax_is_applied() -> None:
    assert round(100 * (1 + TAX_RATE), 2) == 118.00


def test_schema_version_is_pinned() -> None:
    assert SCHEMA_VERSION.count(".") == 2
