"""Sample test file. Promoted like any other file -- the pipeline is content-agnostic."""

SCHEMA_VERSION = "1.0.0"


def test_customer_id_prefix() -> None:
    assert "CUST-000123".startswith("CUST-")


def test_schema_version_is_pinned() -> None:
    assert SCHEMA_VERSION.count(".") == 2
