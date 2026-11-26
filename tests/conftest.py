import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the network: any real HTTP attempt fails loudly."""
    from providers import http

    def refuse(req, timeout):
        raise AssertionError(f"unexpected network access in tests: {req.full_url}")
    monkeypatch.setattr(http, "opener", refuse)
    monkeypatch.setattr(http, "sleeper", lambda s: None)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Copy of data/ in a temp dir with every module pointed at it."""
    import fetch_data
    import sources
    import store
    import update_data
    import update_reserves
    import validate_data
    data = tmp_path / "data"
    shutil.copytree(ROOT / "data", data, ignore=shutil.ignore_patterns("raw"))
    shutil.copytree(ROOT / "data" / "raw", data / "raw")
    st = data / "store"
    monkeypatch.setattr(store, "STORE", st)
    monkeypatch.setattr(sources, "STORE", st)
    monkeypatch.setattr(sources, "RAW", data / "raw")
    monkeypatch.setattr(fetch_data, "STATUS_PATH", data / "data_status.json")
    monkeypatch.setattr(update_data, "STORE", st)
    monkeypatch.setattr(update_data, "RAW", data / "raw")
    monkeypatch.setattr(update_data, "OUT_JSON", data / "dataset.json")
    monkeypatch.setattr(update_data, "OUT_CSV", data / "dataset.csv")
    monkeypatch.setattr(update_reserves, "DATA", data)
    monkeypatch.setattr(validate_data, "DATA", data)
    return data
