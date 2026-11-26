"""CBRT reserve datasets: data/reserves.json, reserves.csv, reserves_daily.csv,
reserves_metadata.json. Reads data/store/ only (filled by fetch_data.py).

Series offered on the chart (USD billions):
  gross        TP.AB.TOPLAM  weekly, million USD -> / 1e3
  net          TP.AB.N06     weekly, thousand TRY (Stand-By net international reserves)
                             -> / USDTRY buying rate / 1e6, valuation-date rate (see below)
  netExSwapEst daily estimate of net reserves excluding swaps (below), only when published
Gold (TP.AB.C1) and FX (TP.AB.C2) stay in reserves.csv; domestic swap stocks
(TP.SWAPTEKTAR.TOTALSTOK{ALIM,SATIM}YONLU, million USD -> / 1e3) go to reserves_daily.csv.

Valuation-date exchange rate: the CBRT values its balance sheet at the indicative
USD/TRY buying rate set at 15:30 on the balance-sheet date. EVDS dates that rate one
business day later (the day it applies), so the rate determined on day d is taken
from the next EVDS FX row - the same timing convention the carry model uses. If no
rate was set on d (holiday) the last rate set before d is used.

Estimated net reserves excluding swaps (daily, NOT an official series):
  net_abs(d)            = (A02(d) - A10(d)) / usd_buy(d) / 1e6                [USD bn]
  total_net_swap(M)     = -(II.2(M) + II.3(M)) / 1e3                             [USD bn]
  domestic_net_monthend = domestic_buy(month-end M) - domestic_sell(month-end M) [USD bn]
  foreign_swap_est(M)   = total_net_swap(M) - domestic_net_monthend(M)
  net_ex_swap(d)        = net_abs(d) - foreign_swap_est(M*(d)) - domestic_buy(d) + domestic_sell(d)
  M*(d) = latest month whose IRFCL release is assumed available on d. Release dates
  are not in the API, so a conservative rule is used: month M becomes usable on the
  first day of month M+2 (no look-ahead; values are flagged as approximate
  point-in-time). Monthly values are held constant, never interpolated.
The estimate is written only if every automatic check passes AND
PUBLISH_SWAP_ESTIMATE is switched on after a human review of the diagnostics this
script prints. Otherwise it is computed for the log only and left out of the public
files, as required when foreign swaps cannot yet be isolated reliably.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import store
from sources import BALANCE_COLS, BY_KEY, FX_COLS, IRFCL_COLS, SWAP_COLS, WEEKLY_COLS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
METHOD_VERSION = "2.0.0"
PUBLISH_SWAP_ESTIMATE = os.environ.get("PUBLISH_SWAP_ESTIMATE", "0") == "1"
IRFCL_LAG_MONTHS = 2          # month M usable from the 1st day of month M + 2

SERIES = {
    "foreign_assets": "TP.AB.A02",
    "total_fx_liabilities": "TP.AB.A10",
    "usd_buy": "TP.DK.USD.A.YTL",
    "domestic_buy": "TP.SWAPTEKTAR.TOTALSTOKALIMYONLU",
    "domestic_sell": "TP.SWAPTEKTAR.TOTALSTOKSATIMYONLU",
    "irfcl_ii2": "TP.DOVVARNC.K14",
    "irfcl_ii3": "TP.DOVVARNC.K23",
}

# Reported reference values (external, not ground truth): used only to confirm the
# million-USD unit of the domestic swap stocks and to print diagnostics.
DOM_SWAP_REFS = {"2026-04-06": 13.517, "2026-05-26": 3.715, "2026-09-30": 3.650, "2026-11-25": 4.685}
NET_EX_SWAP_REFS = {"2023-06-08": -77.7, "2026-01-29": 82.4, "2026-11-25": 1.6}
REF_TOL = 0.02


def usd_rates() -> tuple[pd.Series, pd.Series]:
    """USD/TRY buying and mid rates indexed by the day they were SET (15:30)."""
    fx = store.read(BY_KEY["fx"].path, FX_COLS)
    rows = fx[fx.notna().all(axis=1)]
    shifted = rows.iloc[1:].copy()
    shifted.index = rows.index[:-1]
    buy = shifted["TP_DK_USD_A_YTL"]
    mid = (shifted["TP_DK_USD_A_YTL"] + shifted["TP_DK_USD_S_YTL"]) / 2
    return buy, mid


def at_dates(rate: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """Rate set on each date, or the last one set before it."""
    return rate.reindex(rate.index.union(dates)).ffill().reindex(dates)


def compare_refs(busd: pd.Series, refs: dict) -> list:
    """(date, our value, reference) for every reference date inside the coverage."""
    busd = busd.dropna()
    rows = []
    for d, ref in refs.items():
        d = pd.Timestamp(d)
        if busd.empty or d < busd.index[0] or d > busd.index[-1] + pd.Timedelta(days=3):
            continue
        k = busd.index.searchsorted(d, side="right") - 1
        if k >= 0 and (d - busd.index[k]).days <= 3:
            rows.append((d, float(busd.iloc[k]), ref))
    return rows


def round_list(s: pd.Series, nd=3):
    return [None if pd.isna(x) else round(float(x), nd) for x in s]


def series_entry(s: pd.Series, freq: str, kind: str, tr: str, en: str, code: str, **extra) -> dict:
    s = s.dropna()
    return {"label_tr": tr, "label_en": en, "freq": freq, "kind": kind, "code": code,
            "dates": [d.strftime("%Y-%m-%d") for d in s.index], "values": round_list(s), **extra}


def foreign_swap_estimate(irfcl: pd.DataFrame, dom_net: pd.Series) -> pd.DataFrame:
    m = irfcl.dropna()
    if m.empty or dom_net.dropna().empty:
        return pd.DataFrame(columns=["total_net_swap", "dom_net_monthend", "foreign_swap", "available_from"])
    total = -(m["TP_DOVVARNC_K14"] + m["TP_DOVVARNC_K23"]) / 1e3
    dn = dom_net.dropna()
    me = pd.Series([dn[dn.index <= d].iloc[-1] if (dn.index <= d).any() and (d - dn.index[dn.index <= d][-1]).days <= 5
                    else np.nan for d in m.index], index=m.index)
    out = pd.DataFrame({"total_net_swap": total, "dom_net_monthend": me})
    out["foreign_swap"] = out["total_net_swap"] - out["dom_net_monthend"]      # foreign_swap_est
    out["available_from"] = [d + pd.offsets.MonthBegin(IRFCL_LAG_MONTHS) for d in m.index]
    return out.dropna(subset=["foreign_swap"])


def main() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    buy, mid = usd_rates()
    w = store.read(BY_KEY["reserves_weekly"].path, WEEKLY_COLS)
    warnings: list[str] = []
    series: dict = {}

    # ── Weekly official reserves ──────────────────────────────────────────
    wk = pd.DataFrame(index=w.index)
    wk["gross_musd"], wk["net_ktl"] = w["TP_AB_TOPLAM"], w["TP_AB_N06"]
    wk["usd_mid"] = at_dates(mid, w.index)
    wk["gross_busd"] = w["TP_AB_TOPLAM"] / 1e3
    wk["usd_buy"] = at_dates(buy, w.index)
    wk["net_busd"] = w["TP_AB_N06"] / wk["usd_buy"] / 1e6
    wk["gold_musd"], wk["fx_musd"] = w["TP_AB_C1"], w["TP_AB_C2"]
    wk["gold_busd"], wk["fx_busd"] = w["TP_AB_C1"] / 1e3, w["TP_AB_C2"] / 1e3
    wk = wk[wk["net_busd"].notna() | wk["gross_busd"].notna()]
    series["net"] = series_entry(wk["net_busd"], "weekly", "official", "Net uluslararası rezervler (haftalık)",
                                 "Net international reserves (weekly)", "TP.AB.N06",
                                 note_en="Converted from thousand TRY at the valuation-date CBRT USD buying rate",
                                 note_tr="Bin TL'den bilanço tarihinde belirlenen TCMB USD alış kuruyla çevrilmiştir")
    series["gross"] = series_entry(wk["gross_busd"], "weekly", "official", "Brüt rezervler (haftalık)",
                                   "Gross reserves (weekly)", "TP.AB.TOPLAM")

    # ── Daily balance sheet, domestic swaps and monthly IRFCL ─────────────
    bal = store.read(BY_KEY["balance_daily"].path, BALANCE_COLS).dropna()
    sw = store.read(BY_KEY["swaps_daily"].path, SWAP_COLS)
    ir = store.read(BY_KEY["irfcl_monthly"].path, IRFCL_COLS)
    # Convert domestic swaps from million USD to billion USD
    domestic_buy = sw["TP_SWAPTEKTAR_TOTALSTOKALIMYONLU"] / 1e3
    domestic_sell = sw["TP_SWAPTEKTAR_TOTALSTOKSATIMYONLU"] / 1e3
    daily = pd.DataFrame({"dom_buy_busd": domestic_buy, "dom_sell_busd": domestic_sell}).dropna(how="all")
    ref_rows = compare_refs(domestic_buy, DOM_SWAP_REFS)
    est_info = {"published": False, "checks": [], "reason": ""}

    checks = []
    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok
    have = check("inputs: analytical balance sheet A02/A10", not bal.empty) & \
        check("inputs: domestic swap stocks", not daily.empty) & \
        check("inputs: monthly IRFCL II.2 and II.3", not ir.dropna().empty)
    if not daily.empty:
        bad = [(d, v, r) for d, v, r in ref_rows if abs(v / r - 1) > REF_TOL]
        check("unit: domestic swaps in million USD agree with reported values",
              not bad, "; ".join(f"{d:%Y-%m-%d}: {v:.3f} vs {r:.3f}" for d, v, r in (bad or ref_rows)) or "no reference date in coverage")
    if have:
        dom_net = daily["dom_buy_busd"] - daily["dom_sell_busd"]
        fs = foreign_swap_estimate(ir, dom_net)
        d = bal.index
        # Net analytical balance sheet position (USD billion)
        net_abs = (bal["TP_AB_A02"] - bal["TP_AB_A10"]) / at_dates(buy, d) / 1e6
        check("unit: analytical balance sheet difference within ±300 USD bn", net_abs.dropna().abs().max() < 300,
              f"max |net_abs| = {net_abs.abs().max():.1f}")
        check("foreign swap estimate available for at least one month", not fs.empty)
        if not fs.empty:
            avail = pd.Series(fs["foreign_swap"].values, index=pd.DatetimeIndex(fs["available_from"])).sort_index()
            avail = avail[~avail.index.duplicated(keep="last")]
            foreign_swap_d = avail.reindex(avail.index.union(d)).ffill().reindex(d)   # held constant, no interpolation
            ref_m = pd.Series(fs.index, index=pd.DatetimeIndex(fs["available_from"])).sort_index()
            ref_m = ref_m[~ref_m.index.duplicated(keep="last")].reindex(ref_m.index.union(d)).ffill().reindex(d)
            dom_buy_d = daily["dom_buy_busd"].reindex(daily.index.union(d)).ffill(limit=5).reindex(d)
            dom_sell_d = daily["dom_sell_busd"].reindex(daily.index.union(d)).ffill(limit=5).reindex(d)
            # Daily estimated net reserves excluding swaps
            net_ex_swap = (net_abs - foreign_swap_d - dom_buy_d + dom_sell_d).dropna()
            print("Swap-adjusted estimate diagnostics (not official, not published unless enabled):")
            for rd, rv in NET_EX_SWAP_REFS.items():
                k = net_ex_swap.index.searchsorted(pd.Timestamp(rd), side="right") - 1
                if k >= 0 and (pd.Timestamp(rd) - net_ex_swap.index[k]).days <= 5:
                    print(f"  {rd}: estimate {net_ex_swap.iloc[k]:8.2f} USD bn vs external {rv:8.2f} "
                          f"(diff {net_ex_swap.iloc[k] - rv:+.2f}), foreign swap month {ref_m.loc[net_ex_swap.index[k]]:%Y-%m}")
                else:
                    print(f"  {rd}: no estimate (inputs not available on that date)")
            for dd, v, ref in ref_rows:
                print(f"  domestic buy-side swap {dd:%Y-%m-%d}: {v:.3f} vs reported {ref:.3f}")
            ok_all = all(c["ok"] for c in checks)
            if ok_all and PUBLISH_SWAP_ESTIMATE:
                series["netExSwapEst"] = series_entry(
                    net_ex_swap, "daily", "estimate", "Swap hariç net rezerv tahmini (günlük)",
                    "Estimated net reserves excluding swaps (daily)", "calculated",
                    pointInTime=f"approximate: monthly IRFCL assumed usable from the 1st day of month M+{IRFCL_LAG_MONTHS}")
                daily = daily.join(pd.DataFrame({"net_abs_busd": net_abs, "foreign_swap_est_busd": foreign_swap_d,
                                                 "net_ex_swap_est_busd": net_ex_swap}), how="outer")
                est_info = {"published": True, "foreignSwapReferenceMonth": fs.index[-1].strftime("%Y-%m"),
                            "firstDate": net_ex_swap.index[0].strftime("%Y-%m-%d") if len(net_ex_swap) else None}
            else:
                est_info["reason"] = ("automatic checks failed" if not ok_all else
                                      "awaiting manual review (PUBLISH_SWAP_ESTIMATE is off)")
    if not est_info.get("published"):
        est_info["reason"] = est_info.get("reason") or "required inputs are not available yet"
        warnings.append("Swap-adjusted net reserve estimate not published: " + est_info["reason"] + ".")
    est_info["checks"] = checks

    # ── Write outputs ─────────────────────────────────────────────────────
    js = {"meta": {"generated": now[:10], "unit": "USD bn", "methodologyVersion": METHOD_VERSION,
                   "last": {k: v["dates"][-1] for k, v in series.items() if v["dates"]}},
          "series": series}
    store.write_json_if_changed(DATA / "reserves.json", js, volatile=("generated",),
                                ensure_ascii=False, separators=(",", ":"))
    cols = ["gross_musd", "net_ktl", "usd_mid", "gross_busd", "net_busd", "gold_musd", "fx_musd", "usd_buy",
            "gold_busd", "fx_busd"]
    out = wk[cols].copy()
    out.index.name = "date"
    store.write_if_changed(DATA / "reserves.csv", out.to_csv(float_format="%.10g", lineterminator="\n"))
    if not daily.empty:
        daily.index.name = "date"
        store.write_if_changed(DATA / "reserves_daily.csv",
                               daily.dropna(how="all").to_csv(float_format="%.10g", lineterminator="\n"))

    def cov(s):
        c = store.column_coverage(s)
        return {"firstObs": c["first"], "lastObs": c["last"], "count": c["count"]}
    meta = {
        "generated": now, "methodologyVersion": METHOD_VERSION, "unit": "USD billion",
        "sources": {
            "TP.AB.TOPLAM": {"description": "Gross reserves (gold + foreign currency)", "nativeUnit": "million USD",
                             "frequency": "weekly", "conversion": "/ 1,000", **cov(w["TP_AB_TOPLAM"])},
            "TP.AB.C1": {"description": "Gold reserves", "nativeUnit": "million USD", "frequency": "weekly",
                         "conversion": "/ 1,000", **cov(w["TP_AB_C1"])},
            "TP.AB.C2": {"description": "Foreign currency reserves", "nativeUnit": "million USD", "frequency": "weekly",
                         "conversion": "/ 1,000", **cov(w["TP_AB_C2"])},
            "TP.AB.N06": {"description": "2A Net international reserves (1+2+3), Stand-By definition",
                          "nativeUnit": "thousand TRY", "frequency": "weekly",
                          "conversion": "/ USDTRY buying rate set at 15:30 on the balance-sheet date / 1,000,000",
                          **cov(w["TP_AB_N06"])},
            "TP.DK.USD.A.YTL": {"description": "CBRT indicative USD/TRY buying rate", "nativeUnit": "TRY per USD",
                                "frequency": "daily", "timing": "EVDS date = day the rate applies; set 15:30 previous business day"},
            "TP.AB.A02": {"description": "Analytical balance sheet: foreign assets", "nativeUnit": "thousand TRY (expected)",
                          "frequency": "daily", **cov(bal["TP_AB_A02"] if not bal.empty else pd.Series(dtype="float64"))},
            "TP.AB.A10": {"description": "Analytical balance sheet: total FX liabilities", "nativeUnit": "thousand TRY (expected)",
                          "frequency": "daily", **cov(bal["TP_AB_A10"] if not bal.empty else pd.Series(dtype="float64"))},
            "TP.SWAPTEKTAR.TOTALSTOKALIMYONLU": {"description": "Domestic buy-side swap stock", "frequency": "daily",
                                                 "nativeUnit": "million USD", "conversion": "/ 1,000",
                                                 **cov(sw["TP_SWAPTEKTAR_TOTALSTOKALIMYONLU"])},
            "TP.SWAPTEKTAR.TOTALSTOKSATIMYONLU": {"description": "Domestic sell-side (reverse) swap stock", "frequency": "daily",
                                                  "nativeUnit": "million USD", "conversion": "/ 1,000",
                                                  **cov(sw["TP_SWAPTEKTAR_TOTALSTOKSATIMYONLU"])},
            "TP.DOVVARNC.K14": {"description": "IRFCL II.2 forwards, futures and swaps (net)", "nativeUnit": "million USD",
                                "frequency": "monthly", **cov(ir["TP_DOVVARNC_K14"])},
            "TP.DOVVARNC.K23": {"description": "IRFCL II.3 other predetermined net drains", "nativeUnit": "million USD",
                                "frequency": "monthly", **cov(ir["TP_DOVVARNC_K23"])},
        },
        "sourceUrl": "https://evds3.tcmb.gov.tr/",
        "publicationDates": "not provided by the EVDS API; observation dates are reported",
        "swapAdjustedEstimate": {**est_info, "irfclLagRule": f"month M usable from day 1 of month M+{IRFCL_LAG_MONTHS}",
                                 "formula": "NetABS - ForeignSwap(M*) - DomBuy + DomSell; ForeignSwap = -(II.2+II.3) - DomNet(month-end)"},
        "warnings": warnings,
    }
    store.write_json_if_changed(DATA / "reserves_metadata.json", meta, volatile=("generated",),
                                ensure_ascii=False, indent=1)
    n = series["net"]
    print(f"reserves: {len(n['dates'])} weekly obs {n['dates'][0]} -> {n['dates'][-1]}, latest net {n['values'][-1]:.2f} USD bn")
    for wmsg in warnings:
        print("  note:", wmsg)


if __name__ == "__main__":
    main()
