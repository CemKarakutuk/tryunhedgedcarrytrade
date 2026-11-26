"""Dataset registry: which provider fills which store file, how far back, with which
validation rules, and how each dataset is seeded from the historical files in
data/raw/ (the hand-downloaded files that built the site until automation)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from providers import boe, boj, ecb, evds, nyfed
from store import STORE

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
CURS = ("USD", "EUR", "GBP", "JPY")

TLREF_COLS = ["TP_BISTTLREF_ORAN"]
FX_COLS = [f"TP_DK_{c}_{s}_YTL" for c in CURS for s in "AS"]
WEEKLY_COLS = ["TP_AB_C1", "TP_AB_C2", "TP_AB_TOPLAM", "TP_AB_N06"]
BALANCE_COLS = ["TP_AB_A02", "TP_AB_A10"]
SWAP_COLS = ["TP_SWAPTEKTAR_TOTALSTOKALIMYONLU", "TP_SWAPTEKTAR_TOTALSTOKSATIMYONLU"]
IRFCL_COLS = ["TP_DOVVARNC_K14", "TP_DOVVARNC_K23"]


def codes(cols):
    return [c.replace("_", ".") for c in cols]


@dataclass
class Dataset:
    key: str
    file: str
    columns: list[str]
    kind: str                                   # daily | weekly | monthly
    provider: str
    url: str
    fetch: Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame]
    backfill_start: str = "2019-01-01"
    lookback_days: int = 60
    future_days: int = 0                        # EVDS FX rows are dated the day they apply (up to +4 days)
    ranges: dict = field(default_factory=dict)
    min_start: dict = field(default_factory=dict)
    min_count: dict = field(default_factory=dict)
    seed: Callable[[], pd.DataFrame | None] | None = None
    unit: str = ""

    @property
    def path(self) -> Path:
        return STORE / self.file


# ── Seeds from data/raw (existing manual downloads) ──────────────────────────
def _raw(prefix: str, exclude_reserve=True) -> Path | None:
    order = {".xlsx": 0, ".xls": 1, ".csv": 2}
    hits = [p for p in RAW.iterdir() if p.name.lower().startswith(prefix) and p.suffix.lower() in order
            and (not exclude_reserve or not any(w in p.name.lower() for w in ("reserv", "rezerv")))] if RAW.exists() else []
    return sorted(hits, key=lambda p: (order[p.suffix.lower()], p.name))[0] if hits else None


def _evds_raw(cols: list[str], reserve=False) -> pd.DataFrame | None:
    p = next((q for q in sorted(RAW.glob("*")) if any(w in q.name.lower() for w in ("reserv", "rezerv"))
              and q.suffix.lower() in (".xlsx", ".xls", ".csv")), None) if reserve else _raw("evds")
    if p is None:
        return None
    df = pd.read_csv(p) if p.suffix.lower() == ".csv" else pd.read_excel(p)
    df = df[df.iloc[:, 0].astype(str).str.match(r"^\d{2}-\d{2}-\d{4}$")].copy()
    df.index = pd.DatetimeIndex(pd.to_datetime(df.iloc[:, 0], format="%d-%m-%Y"), name="date")
    df.columns = [str(c).replace(".", "_") for c in df.columns]
    have = [c for c in cols if c in df.columns]
    if not have:
        return None
    return df[have].apply(pd.to_numeric, errors="coerce").astype("float64").dropna(how="all").sort_index()


def _series_seed(prefix: str, reader_name: str):
    def seed():
        import update_data as U
        p = _raw(prefix)
        if p is None:
            return None
        s = getattr(U, reader_name)(p)
        df = s.astype("float64").to_frame("rate")
        df.index.name = "date"
        return df
    return seed


# ── EVDS fetch helpers (independent requests per group) ─────────────────────
def _evds(cols, kind):
    return lambda a, b: evds.fetch(codes(cols), a, b, kind=kind)


RATE_RANGE = (-5.0, 150.0)
DATASETS: list[Dataset] = [
    Dataset("tlref", "evds_tlref.csv", TLREF_COLS, "daily", "CBRT EVDS", evds.PORTAL, _evds(TLREF_COLS, "daily"),
            ranges={"TP_BISTTLREF_ORAN": (0.0, 150.0)}, min_start={"TP_BISTTLREF_ORAN": "2019-01-02"},
            min_count={"TP_BISTTLREF_ORAN": 1500}, seed=lambda: _evds_raw(TLREF_COLS), unit="percent, ACT/365"),
    Dataset("fx", "evds_fx.csv", FX_COLS, "daily", "CBRT EVDS", evds.PORTAL, _evds(FX_COLS, "daily"),
            future_days=5,
            ranges={**{c: (1.0, 500.0) for c in FX_COLS}},
            min_start={c: "2019-01-02" for c in FX_COLS}, min_count={c: 1500 for c in FX_COLS},
            seed=lambda: _evds_raw(FX_COLS), unit="TRY per unit (JPY: per 100 JPY)"),
    Dataset("sofr", "sofr.csv", ["rate"], "daily", "Federal Reserve Bank of New York", nyfed.DOCS, nyfed.fetch,
            ranges={"rate": (-1.0, 25.0)}, min_start={"rate": "2019-01-02"}, min_count={"rate": 1500},
            seed=_series_seed("sofr", "read_sofr"), unit="percent, ACT/360"),
    Dataset("estr", "estr.csv", ["rate"], "daily", "European Central Bank", ecb.DOCS, ecb.fetch,
            backfill_start="2019-10-01",
            ranges={"rate": (-2.0, 25.0)}, min_start={"rate": "2019-10-01"}, min_count={"rate": 1500},
            seed=_series_seed("estr", "read_estr"), unit="percent, ACT/360"),
    Dataset("sonia", "sonia.csv", ["rate"], "daily", "Bank of England", boe.DOCS, lambda a, b: boe.fetch(a),
            ranges={"rate": (-1.0, 25.0)}, min_start={"rate": "2019-01-02"}, min_count={"rate": 1500},
            seed=_series_seed("sonia", "read_sonia"), unit="percent, ACT/365"),
    Dataset("tona", "tona.csv", ["rate"], "daily", "Bank of Japan", boj.DOCS, boj.fetch,
            ranges={"rate": (-1.0, 25.0)}, min_start={"rate": "2019-01-04"}, min_count={"rate": 1400},
            seed=_series_seed("tona", "read_tona"), unit="percent, ACT/365"),
    Dataset("reserves_weekly", "evds_reserves_weekly.csv", WEEKLY_COLS, "weekly", "CBRT EVDS", evds.PORTAL,
            _evds(WEEKLY_COLS, "weekly"), lookback_days=120,
            ranges={"TP_AB_C1": (0, 400_000), "TP_AB_C2": (-50_000, 400_000), "TP_AB_TOPLAM": (0, 500_000),
                    "TP_AB_N06": (-1e13, 1e13)},
            min_start={c: "2019-01-04" for c in WEEKLY_COLS}, min_count={c: 300 for c in WEEKLY_COLS},
            seed=lambda: _evds_raw(WEEKLY_COLS, reserve=True),
            unit="C1/C2/TOPLAM: million USD; N06: thousand TRY"),
    Dataset("balance_daily", "evds_balance_daily.csv", BALANCE_COLS, "daily", "CBRT EVDS", evds.PORTAL,
            _evds(BALANCE_COLS, "daily"),
            ranges={c: (0, 1e14) for c in BALANCE_COLS}, unit="thousand TRY (to be confirmed on first live run)"),
    Dataset("swaps_daily", "evds_swaps_daily.csv", SWAP_COLS, "daily", "CBRT EVDS", evds.PORTAL,
            _evds(SWAP_COLS, "daily"),
            ranges={c: (0, 1e12) for c in SWAP_COLS}, unit="identified from reference values (see update_reserves.py)"),
    Dataset("irfcl_monthly", "evds_irfcl_monthly.csv", IRFCL_COLS, "monthly", "CBRT EVDS", evds.PORTAL,
            _evds(IRFCL_COLS, "monthly"), lookback_days=400,
            ranges={c: (-1e7, 1e7) for c in IRFCL_COLS}, unit="million USD (IRFCL template, Section II)"),
]
BY_KEY = {d.key: d for d in DATASETS}


# ── Status items shown in the Data Status panel ──────────────────────────────
# tol: maximum lag before "stale" (business days for daily, calendar days otherwise)
STATUS_ITEMS = [
    {"id": "TLREF", "ds": "tlref", "cols": TLREF_COLS, "freq": "daily", "tol": 7,
     "lag_tr": "Aynı gün belirlenir, EVDS'de genellikle ertesi iş günü", "lag_en": "Fixed same day; usually in EVDS next business day"},
    {"id": "SOFR", "ds": "sofr", "cols": ["rate"], "freq": "daily", "tol": 4,
     "lag_tr": "Ertesi ABD iş günü 08:00 (New York)", "lag_en": "Next US business day, 08:00 New York"},
    {"id": "€STR", "ds": "estr", "cols": ["rate"], "freq": "daily", "tol": 4,
     "lag_tr": "Ertesi TARGET2 iş günü 08:00 (CET)", "lag_en": "Next TARGET2 business day, 08:00 CET"},
    {"id": "SONIA", "ds": "sonia", "cols": ["rate"], "freq": "daily", "tol": 4,
     "lag_tr": "Ertesi Londra iş günü 09:00", "lag_en": "Next London business day, 09:00"},
    {"id": "TONA", "ds": "tona", "cols": ["rate"], "freq": "daily", "tol": 5,
     "lag_tr": "Ertesi Tokyo iş günü", "lag_en": "Next Tokyo business day"},
    *[{"id": f"{c}/TRY", "ds": "fx", "cols": [f"TP_DK_{c}_A_YTL", f"TP_DK_{c}_S_YTL"], "freq": "daily", "tol": 7,
       "lag_tr": "15:30'da belirlenir, ertesi iş günü tarihiyle yayımlanır", "lag_en": "Set at 15:30, published dated the next business day"}
      for c in CURS],
    {"id": "balance", "ds": "balance_daily", "cols": BALANCE_COLS, "freq": "daily", "tol": 7,
     "label_tr": "Analitik bilanço", "label_en": "Analytical balance sheet",
     "lag_tr": "Ertesi iş günü", "lag_en": "Next business day"},
    {"id": "reserves", "ds": "reserves_weekly", "cols": WEEKLY_COLS, "freq": "weekly", "tol": 21,
     "label_tr": "Haftalık rezervler", "label_en": "Weekly reserves",
     "lag_tr": "Cuma verisi izleyen perşembe", "lag_en": "Friday data released the following Thursday"},
    {"id": "swaps", "ds": "swaps_daily", "cols": SWAP_COLS, "freq": "daily", "tol": 7,
     "label_tr": "Yurt içi swap stokları", "label_en": "Domestic swaps",
     "lag_tr": "Ertesi iş günü", "lag_en": "Next business day"},
    {"id": "irfcl", "ds": "irfcl_monthly", "cols": IRFCL_COLS, "freq": "monthly", "tol": 75,
     "label_tr": "Aylık yurt dışı swap girdileri (IRFCL II.2, II.3)", "label_en": "Monthly foreign swap inputs (IRFCL II.2, II.3)",
     "lag_tr": "Ay sonu verisi izleyen ayın sonuna doğru", "lag_en": "Month-end data released towards the end of the next month"},
]
