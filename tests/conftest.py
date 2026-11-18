import pytest


@pytest.fixture(autouse=True)
def _isolated_customer_value_dir(tmp_path, monkeypatch):
    """Tests never read the real ~/runs/sumbi-live/customer-value folder."""
    monkeypatch.setenv("SUMBI_CUSTOMER_VALUE_DIR", str(tmp_path / "customer-value-empty"))
