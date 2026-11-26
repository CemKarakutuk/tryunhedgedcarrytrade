"""End-to-end run of the whole pipeline against simulated official APIs.

Every provider is answered by a fake HTTP layer that returns responses in the
documented formats, including one new observation per series. The run must
extend every dataset by that observation, keep all history, produce datasets that
pass validation, and leave the carry numbers for existing dates unchanged.
"""
import json
import shutil
import urllib.parse

import pandas as pd

import fetch_data
import store
import update_data
import update_reserves
import validate_data
from providers import http
from sources import BY_KEY

from test_providers import FakeResp

NEW_DAY = pd.Timestamp("2026-11-25")       # TLREF / SOFR / €STR / SONIA / TONA trade date
NEW_FX = pd.Timestamp("2026-11-27")        # next EVDS FX row (rate set 26.11 at 15:30)
FX_BASE = {}


def fake_api(req, timeout):
    url = req.full_url
    if "evds3.tcmb.gov.tr" in url:
        assert req.get_header("Key") == "TESTKEY"
        series = urllib.parse.unquote(url.split("series=")[1].split("&")[0]).split("-")
        cols = [s.replace(".", "_") for s in series]
        if cols == ["TP_BISTTLREF_ORAN"]:
            items = [{"Tarih": "25-11-2026", "TP_BISTTLREF_ORAN": "39.5"}]
        elif cols[0].startswith("TP_DK_"):
            last = FX_BASE.setdefault("row", store.read(BY_KEY["fx"].path).iloc[-1])
            items = [{"Tarih": "27-11-2026", **{c: str(last[c] * 1.001) for c in cols}}]
        elif cols[0] == "TP_AB_C1":
            items = [{"Tarih": "20-11-2026", "TP_AB_C1": "130000", "TP_AB_C2": "60000", "TP_AB_TOPLAM": "190000",
                      "TP_AB_N06": "1800000000"}]
        elif cols[0] == "TP_AB_A02":
            items = [{"Tarih": d.strftime("%d-%m-%Y"), "TP_AB_A02": "9000000000", "TP_AB_A10": "8000000000"}
                     for d in pd.bdate_range("2026-04-01", "2026-11-25")]
        elif cols[0].startswith("TP_SWAPTEKTAR"):
            refs = {"06-04-2026": "13517", "26-05-2026": "3715", "30-09-2026": "3650", "25-11-2026": "4685"}
            items = [{"Tarih": d.strftime("%d-%m-%Y"),
                      "TP_SWAPTEKTAR_TOTALSTOKALIMYONLU": refs.get(d.strftime("%d-%m-%Y"), "9000"),
                      "TP_SWAPTEKTAR_TOTALSTOKSATIMYONLU": "800"} for d in pd.bdate_range("2026-04-01", "2026-11-25")]
        else:
            items = [{"Tarih": "2026-9", "TP_DOVVARNC_K14": "-40000", "TP_DOVVARNC_K23": "-2000"}]
        return FakeResp(json.dumps({"items": items, "totalCount": len(items)}).encode())
    if "newyorkfed" in url:
        return FakeResp(json.dumps({"refRates": [{"effectiveDate": "2026-11-25", "type": "SOFR", "percentRate": 3.93}]}).encode())
    if "data-api.ecb" in url:
        return FakeResp(b"KEY,TIME_PERIOD,OBS_VALUE\nEST.B.EU000A2X2A25.WT,2026-11-25,1.931\n", "text/csv")
    if "bankofengland" in url:
        return FakeResp(b"DATE,IUDSOIA\n25 Nov 2026,3.9851\n", "text/csv")
    if "stat-search.boj" in url:
        return FakeResp(json.dumps({"STATUS": 200, "NEXTPOSITION": None, "RESULTSET": [{
            "SERIES_CODE": "STRDCLUCON", "NAME_OF_TIME_SERIES": "Call Rate, Uncollateralized Overnight, Average (Daily)",
            "VALUES": {"SURVEY_DATES": [20261124, 20261125], "VALUES": [0.727, 0.728]}}]}).encode())
    raise AssertionError(url)


def test_full_automated_run(sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("EVDS_API_KEY", "TESTKEY")
    monkeypatch.setenv("PIPELINE_TODAY", "2026-11-26")        # freeze the clock for status ages
    monkeypatch.setattr(http, "opener", fake_api)
    FX_BASE.clear()
    base = tmp_path / "before"
    shutil.copytree(sandbox, base)
    old = json.loads((sandbox / "dataset.json").read_text(encoding="utf-8"))

    assert fetch_data.main(["--strict"]) == 0
    update_data.main()
    update_reserves.main()
    assert validate_data.main(["--baseline", str(base)]) == 0

    new = json.loads((sandbox / "dataset.json").read_text(encoding="utf-8"))
    n = len(old["dates"])
    assert new["dates"][:n] == old["dates"] and len(new["dates"]) == n + 1
    # carry inputs of all existing days are untouched (only the last open accrual may move)
    for c in ("USD", "EUR", "GBP", "JPY"):
        assert new["cur"][c]["buy"][:n] == old["cur"][c]["buy"]
        assert new["cur"][c]["idx"][:n - 1] == old["cur"][c]["idx"][:n - 1]
    assert new["tl"]["pub"][new["dates"].index("2026-11-25")] == 39.5
    st = json.loads((sandbox / "data_status.json").read_text(encoding="utf-8"))
    assert all(s["status"] == "current" for s in st["sources"]), [(s["id"], s["status"]) for s in st["sources"]]
    res = json.loads((sandbox / "reserves.json").read_text(encoding="utf-8"))
    assert res["series"]["net"]["dates"][-1] == "2026-11-20"
    assert res["series"]["domSwap"]["values"][-1] == 4.685
    assert "netExSwapEst" not in res["series"]          # gate stays closed until reviewed

    # a second run with nothing new changes no file at all (no empty commits)
    snap = {p: p.read_bytes() for p in sandbox.rglob("*") if p.is_file()}
    fetch_data.main([])
    update_data.main()
    update_reserves.main()
    changed = [str(p.name) for p, b in snap.items() if p.read_bytes() != b]
    assert changed == [], changed
