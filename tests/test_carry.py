"""Carry model regression tests.

1. The store-based pipeline reproduces the committed data/dataset.json and
   dataset.csv exactly (same inputs -> identical outputs).
2. A Python mirror of the browser engine (assets/js/app.js: position, fIdx,
   exitIndex, annualize) checks returns for all currencies, holding periods,
   spread options and borrowing margins, plus reference values computed when the
   model was first validated.
"""
import json
import math
from datetime import date

import pytest

from conftest import ROOT

DATA = json.loads((ROOT / "data" / "dataset.json").read_text(encoding="utf-8"))
DATES = DATA["dates"]
N = len(DATES)
D = [date.fromisoformat(x) for x in DATES]


def fidx(c, m):
    if not m:
        return c["idx"]
    i0 = DATES.index(c["start"])
    out = list(c["idx"])
    for t in range(i0 + 1, N):
        d = (D[t] - D[t - 1]).days
        out[t] = out[t - 1] * (c["idx"][t] / c["idx"][t - 1] + m / 1e4 * d / c["basis"])
    return out


def exit_index(i, n):
    target = date.fromordinal(D[i].toordinal() + n)
    for j in range(i + 1, N):
        if D[j] >= target:
            return j
    return None


def position(cur, i, j, spread=True, m=0, fi=None):
    c = DATA["cur"][cur]
    mid = lambda k: (c["buy"][k] + c["sell"][k]) / 2
    ent = c["buy"][i] if spread else mid(i)
    ext = c["sell"][j] if spread else mid(j)
    fi = fi or fidx(c, m)
    gtl = DATA["tl"]["idx"][j] / DATA["tl"]["idx"][i]
    return ent * gtl / ext - fi[j] / fi[i]


def annualize(r, n):
    return None if (r is None or n <= 0 or 1 + r <= 0) else (1 + r) ** (365 / n) - 1


def test_pipeline_reproduces_committed_dataset(sandbox):
    import update_data
    committed_json = json.loads((sandbox / "dataset.json").read_text(encoding="utf-8"))
    committed_csv = (sandbox / "dataset.csv").read_text(encoding="utf-8")
    (sandbox / "dataset.json").unlink()
    (sandbox / "dataset.csv").unlink()
    update_data.main()
    new_json = json.loads((sandbox / "dataset.json").read_text(encoding="utf-8"))
    for js in (committed_json, new_json):
        js["meta"].pop("generated")
    assert new_json == committed_json
    assert (sandbox / "dataset.csv").read_text(encoding="utf-8") == committed_csv


def test_store_and_raw_inputs_agree(sandbox):
    """Legacy data/raw files and the store give the same carry inputs."""
    import update_data
    _, from_store = update_data.load_inputs()
    for p in (sandbox / "store").glob("*.csv"):
        p.rename(p.with_suffix(".bak"))
    _, from_raw = update_data.load_inputs()
    raw_ev, st_ev = from_raw["evds"], from_store["evds"]
    for c in ["TLREF", "USD_A", "USD_S", "JPY_A", "JPY_S"]:
        a, b = raw_ev[c].dropna(), st_ev[c].dropna()
        a, b = a[a.index <= b.index[-1]], b[b.index <= a.index[-1]]
        assert a.equals(b), c
    for k in ("sofr", "estr", "sonia", "tona"):
        a, b = from_raw[k], from_store[k]
        assert a[a.index <= b.index[-1]].equals(b[b.index <= a.index[-1]]), k


@pytest.mark.parametrize("cur", ["USD", "EUR", "GBP", "JPY"])
@pytest.mark.parametrize("days", [1, 7, 30, 90, 180, 365, 730])
def test_all_currencies_and_holding_periods(cur, days):
    c = DATA["cur"][cur]
    i0 = DATES.index(c["start"])
    j = exit_index(i0, days)
    assert j is not None and (D[j] - D[i0]).days >= days
    for spread in (True, False):
        for m in (0, 25, 100, 300):
            r = position(cur, i0, j, spread, m)
            assert math.isfinite(r) and -1 < r < 5
            a = annualize(r, (D[j] - D[i0]).days)
            assert a is None or math.isfinite(a)
        # the FX spread can only hurt the position; a margin can only hurt it
        assert position(cur, i0, j, True) <= position(cur, i0, j, False)
        assert position(cur, i0, j, True, 100) < position(cur, i0, j, True, 0)


def test_reference_values():
    i = DATES.index("2019-01-02")
    j = exit_index(i, 365)
    assert DATES[j] == "2020-01-02"
    assert round(position("USD", i, j) * 100, 2) == 7.55
    assert round(position("USD", i, j, m=100) * 100, 2) == 6.51


def test_annualisation_and_cumulative_consistency():
    i = DATES.index("2019-01-02")
    j = exit_index(i, 730)
    r = position("GBP", i, j)
    n = (D[j] - D[i]).days
    assert abs((1 + annualize(r, n)) ** (n / 365) - 1 - r) < 1e-12


def test_negative_rates_and_jpy_denomination_preserved():
    eur, jpy = DATA["cur"]["EUR"], DATA["cur"]["JPY"]
    assert min(x for x in eur["pub"] if x is not None) < -0.5      # €STR below zero in 2019-2022
    assert min(x for x in jpy["pub"] if x is not None) < 0          # TONA below zero before 2024
    k = DATES.index("2019-01-04")
    assert 0.04 < jpy["buy"][k] < 0.06                              # TRY per yen, not per 100 yen
    # margin on a negative rate is added without a zero floor
    i0 = DATES.index(eur["start"])
    f0, f1 = fidx(eur, 0), fidx(eur, 25)
    assert f0[i0 + 30] < 1 and f1[i0 + 30] < 1
