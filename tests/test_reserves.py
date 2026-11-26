"""Reserve calculations, mixed-frequency handling and the data-safety pipeline."""
import json

import numpy as np
import pandas as pd
import pytest

import fetch_data
import store
import update_reserves
import validate_data
from providers.http import ProviderError
from sources import BY_KEY


# ── Units, dates and the published weekly series ──────────────────────────
def test_net_reserves_conversion_and_valuation_date(sandbox):
    update_reserves.main()
    csv = pd.read_csv(sandbox / "reserves.csv", index_col=0)
    row = csv.loc["2023-05-19"]
    # thousand TRY / USDTRY buying rate set on 19.05.2023 (EVDS row dated 22.05.2023) / 1e6
    fx = store.read(BY_KEY["fx"].path)
    assert row["usd_buy"] == fx.loc["2023-05-22", "TP_DK_USD_A_YTL"]
    assert row["net_busd"] == pytest.approx(row["net_ktl"] / row["usd_buy"] / 1e6, rel=1e-9)  # CSV keeps 10 significant digits
    assert round(row["net_busd"], 3) == -0.151
    assert row["gross_busd"] == row["gross_musd"] / 1e3
    js = json.loads((sandbox / "reserves.json").read_text(encoding="utf-8"))
    assert {"net", "gross", "gold", "fx"} <= set(js["series"])
    assert all(s["kind"] == "official" for s in js["series"].values())
    assert "netExSwapEst" not in js["series"]                 # inputs missing -> no estimate
    meta = json.loads((sandbox / "reserves_metadata.json").read_text(encoding="utf-8"))
    assert meta["swapAdjustedEstimate"]["published"] is False


def test_valuation_rate_uses_last_rate_set_on_or_before_date():
    rate = pd.Series([10.0, 11.0], index=pd.to_datetime(["2024-01-05", "2024-01-09"]))
    got = update_reserves.at_dates(rate, pd.DatetimeIndex(pd.to_datetime(["2024-01-08", "2024-01-09"])))
    assert got.tolist() == [10.0, 11.0]


def test_swap_unit_identification():
    raw = pd.Series([13517.0, 3715.0], index=pd.to_datetime(["2026-04-06", "2026-05-26"]))
    unit, rows = update_reserves.identify_unit(raw, update_reserves.DOM_SWAP_REFS)
    assert unit == "million USD" and len(rows) == 2
    unit, _ = update_reserves.identify_unit(raw * 1.3, update_reserves.DOM_SWAP_REFS)
    assert unit is None                                       # no forced fit
    assert update_reserves.identify_unit(raw, {"2020-01-01": 1.0})[0] is None


def test_foreign_swap_monthly_no_lookahead():
    ir = pd.DataFrame({"TP_DOVVARNC_K14": [-20000.0, -18000.0], "TP_DOVVARNC_K23": [-1000.0, -500.0]},
                      index=pd.to_datetime(["2026-01-31", "2026-02-28"]))
    dom = pd.Series([5.0, 4.0], index=pd.to_datetime(["2026-01-30", "2026-02-27"]))
    fs = update_reserves.foreign_swap_estimate(ir, dom)
    assert fs.loc["2026-01-31", "total_net_swap"] == 21.0
    assert fs.loc["2026-01-31", "foreign_swap"] == 16.0      # 21 - domestic net at month-end
    assert fs.loc["2026-01-31", "available_from"] == pd.Timestamp("2026-03-01")
    assert fs.loc["2026-02-28", "available_from"] == pd.Timestamp("2026-04-01")


def synthetic_inputs(sandbox, publish, monkeypatch):
    days = pd.bdate_range("2025-12-01", "2026-05-29")
    store.write_frame(BY_KEY["balance_daily"].path, pd.DataFrame(
        {"TP_AB_A02": 4.0e9, "TP_AB_A10": 3.0e9}, index=days.rename("date")))
    sw = pd.DataFrame({"TP_SWAPTEKTAR_TOTALSTOKALIMYONLU": 13517.0, "TP_SWAPTEKTAR_TOTALSTOKSATIMYONLU": 1000.0},
                      index=days.rename("date"))
    sw.loc["2026-05-26", "TP_SWAPTEKTAR_TOTALSTOKALIMYONLU"] = 3715.0
    store.write_frame(BY_KEY["swaps_daily"].path, sw)
    store.write_frame(BY_KEY["irfcl_monthly"].path, pd.DataFrame(
        {"TP_DOVVARNC_K14": [-30000.0, -31000.0], "TP_DOVVARNC_K23": [-2000.0, -2000.0]},
        index=pd.DatetimeIndex(pd.to_datetime(["2025-12-31", "2026-01-31"]), name="date")))
    monkeypatch.setattr(update_reserves, "PUBLISH_SWAP_ESTIMATE", publish)
    update_reserves.main()
    return json.loads((sandbox / "reserves.json").read_text(encoding="utf-8"))


def test_estimate_withheld_without_manual_sign_off(sandbox, monkeypatch):
    js = synthetic_inputs(sandbox, False, monkeypatch)
    assert "domSwap" in js["series"] and js["series"]["domSwap"]["values"][0] == 13.517
    assert "netExSwapEst" not in js["series"]
    meta = json.loads((sandbox / "reserves_metadata.json").read_text(encoding="utf-8"))
    assert "manual review" in meta["swapAdjustedEstimate"]["reason"]


def test_estimate_starts_only_when_monthly_data_are_public(sandbox, monkeypatch):
    js = synthetic_inputs(sandbox, True, monkeypatch)
    est = js["series"]["netExSwapEst"]
    assert est["kind"] == "estimate" and est["label_en"].startswith("Estimated")
    # December 2025 IRFCL becomes usable on 1 Feb 2026: no estimate before that (no back-filled look-ahead)
    assert est["dates"][0] == "2026-02-02"
    d = pd.Timestamp("2026-02-02")
    rate = update_reserves.at_dates(update_reserves.usd_rates()[0], pd.DatetimeIndex([d])).iloc[0]
    net_abs = 1.0e9 / rate / 1e6
    foreign = 32.0 - (13.517 - 1.0)                                   # Dec-2025 value, held constant
    assert est["values"][0] == round(net_abs - foreign - 13.517 + 1.0, 3)
    # March 2026 (Jan IRFCL available) switches to the new monthly value - step, not interpolation
    k = est["dates"].index("2026-03-02")
    rate2 = update_reserves.at_dates(update_reserves.usd_rates()[0], pd.DatetimeIndex([pd.Timestamp("2026-03-02")])).iloc[0]
    assert est["values"][k] == round(1.0e9 / rate2 / 1e6 - (33.0 - 12.517) - 12.517, 3)


# ── Pipeline safety: failures, truncation, duplicates, missing data ───────
def run_fetch(monkeypatch, key, fetcher, *extra):
    ds = BY_KEY[key]
    monkeypatch.setattr(ds, "fetch", fetcher)
    return fetch_data.main(["--only", key, *extra])


def test_failed_api_keeps_previous_dataset(sandbox, monkeypatch):
    p = BY_KEY["sofr"].path
    before = p.read_bytes()
    def boom(a, b):
        raise ProviderError("NY Fed SOFR: HTTP 503 after 5 attempts")
    assert run_fetch(monkeypatch, "sofr", boom) == 0
    assert p.read_bytes() == before
    st = json.loads((sandbox / "data_status.json").read_text(encoding="utf-8"))
    sofr = next(s for s in st["sources"] if s["id"] == "SOFR")
    assert sofr["status"] == "unavailable" and "503" in sofr["message"] and sofr["lastObs"]
    assert run_fetch(monkeypatch, "sofr", boom, "--strict") == 1


def test_short_download_merges_without_truncation(sandbox, monkeypatch):
    p = BY_KEY["sofr"].path
    old = store.read(p)
    recent = pd.DataFrame({"rate": [3.90, 3.91]}, index=pd.DatetimeIndex(
        [old.index[-1], old.index[-1] + pd.offsets.BDay(1)], name="date"))
    run_fetch(monkeypatch, "sofr", lambda a, b: recent)
    new = store.read(p)
    assert new.index[0] == old.index[0] and len(new) == len(old) + 1
    assert new["rate"].iloc[-2] == 3.90                                  # recent revision accepted


def test_truncated_or_empty_response_rejected(sandbox, monkeypatch):
    p = BY_KEY["estr"].path
    before = p.read_bytes()
    run_fetch(monkeypatch, "estr", lambda a, b: pd.DataFrame({"rate": [np.nan]}, index=pd.DatetimeIndex(["2026-11-20"])))
    assert p.read_bytes() == before
    st = json.loads((sandbox / "data_status.json").read_text(encoding="utf-8"))
    assert next(s for s in st["sources"] if s["id"] == "€STR")["status"] == "validation_failed"


def test_mass_history_rewrite_rejected(sandbox, monkeypatch):
    p = BY_KEY["sonia"].path
    old = store.read(p)
    bogus = old.copy() * 100                                             # wrong unit for all history
    before = p.read_bytes()
    run_fetch(monkeypatch, "sonia", lambda a, b: bogus)
    assert p.read_bytes() == before


def test_negative_rates_survive_merge(sandbox, monkeypatch):
    p = BY_KEY["estr"].path
    old = store.read(p)
    run_fetch(monkeypatch, "estr", lambda a, b: old.tail(5))
    new = store.read(p)
    assert (new["rate"] < 0).sum() == (old["rate"] < 0).sum() > 0


def test_duplicates_and_missing_values_in_merge():
    old = pd.DataFrame({"a": [1.0, 2.0]}, index=pd.DatetimeIndex(["2024-01-01", "2024-01-02"], name="date"))
    new = pd.DataFrame({"a": [np.nan, 5.0, 6.0, 7.0]},
                       index=pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-03", "2024-01-04"]))
    merged, rep = store.merge(old, new, pd.Timestamp("2024-02-01"))
    assert merged["a"].tolist() == [1.0, 2.0, 6.0, 7.0]                  # NaN never deletes, dup -> last
    assert rep.duplicates == 1 and rep.added == 2 and rep.revised == 0


def test_atomic_write_failure_leaves_file_intact(tmp_path, monkeypatch):
    p = tmp_path / "x.csv"
    p.write_text("date,a\n2024-01-01,1.0\n")
    def bad_replace(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(store.os, "replace", bad_replace)
    with pytest.raises(OSError):
        store.write_atomic(p, "date,a\n")
    assert p.read_text() == "date,a\n2024-01-01,1.0\n"
    assert not list(tmp_path.glob(".x.csv.*"))


def test_validate_restores_last_known_good(sandbox, tmp_path):
    import shutil
    base = tmp_path / "base"
    shutil.copytree(sandbox, base)
    js = json.loads((sandbox / "dataset.json").read_text(encoding="utf-8"))
    for k in ("buy", "sell", "pub", "used", "idx"):
        for c in js["cur"].values():
            c[k] = c[k][:100]
    js["dates"] = js["dates"][:100]
    (sandbox / "dataset.json").write_text(json.dumps(js))
    assert validate_data.main(["--baseline", str(base), "--restore-on-fail"]) == 1
    assert (sandbox / "dataset.json").read_bytes() == (base / "dataset.json").read_bytes()
    st = json.loads((sandbox / "data_status.json").read_text(encoding="utf-8"))
    assert "pipelineError" in st


def test_validate_passes_on_committed_data(sandbox):
    assert validate_data.main([]) == 0


def test_secret_leak_detected(sandbox, monkeypatch):
    monkeypatch.setenv("EVDS_API_KEY", "LEAKYKEY999")
    (sandbox / "leak.json").write_text('{"k": "LEAKYKEY999"}')
    monkeypatch.setattr(validate_data, "ROOT", sandbox.parent)
    monkeypatch.setattr(validate_data, "PUBLIC_GLOBS", ["data/*.json"])
    errs = validate_data.validate(None)
    assert any("SECRET LEAK" in e for e in errs)


def test_status_distinguishes_lags():
    today = pd.Timestamp("2026-11-30")                                   # Monday
    assert fetch_data._age(pd.Timestamp("2026-11-27"), today, "daily") == 1
    assert fetch_data._age(pd.Timestamp("2026-12-01"), today, "daily") == 0   # FX dated ahead
    assert fetch_data._age(pd.Timestamp("2026-11-13"), today, "weekly") == 17  # within 21-day tolerance
