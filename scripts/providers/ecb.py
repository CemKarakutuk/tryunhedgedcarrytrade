"""European Central Bank: €STR (ECB Data Portal API, anonymous, SDMX REST).

  GET https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?startPeriod=YYYY-MM-DD&endPeriod=YYYY-MM-DD&format=csvdata
  -> CSV with columns KEY, FREQ, ..., TIME_PERIOD, OBS_VALUE, ...

Series EST.B.EU000A2X2A25.WT = euro short-term rate, volume-weighted trimmed mean,
percent. TIME_PERIOD is the trading day the rate refers to (published at 08:00 CET
on the next TARGET2 business day). Negative values are genuine observations.
The API answers HTTP 404 when the requested window holds no observations.
"""
from __future__ import annotations

import io

import pandas as pd

from . import http

FLOW, KEY = "EST", "B.EU000A2X2A25.WT"
BASE = f"https://data-api.ecb.europa.eu/service/data/{FLOW}/{KEY}"
DOCS = "https://data.ecb.europa.eu/help/api/data"


def parse(text: str) -> pd.DataFrame:
    if text.lstrip().startswith("<"):
        raise http.ProviderError("ECB: response is not CSV")
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str)
    except Exception:
        raise http.ProviderError("ECB: CSV could not be parsed")
    if not {"TIME_PERIOD", "OBS_VALUE"} <= set(df.columns):
        raise http.ProviderError("ECB: TIME_PERIOD/OBS_VALUE columns missing")
    if "KEY" in df.columns and not df["KEY"].astype(str).str.endswith(f"{FLOW}.{KEY}").all():
        raise http.ProviderError("ECB: response contains an unexpected series key")
    d = pd.to_datetime(df["TIME_PERIOD"], format="%Y-%m-%d", errors="coerce")
    v = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    s = pd.Series(v.values, index=d).dropna()
    s = s[s.index.notna()]
    if s.empty:
        raise http.ProviderError("ECB: no observations in response")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    out = s.astype("float64").to_frame("rate")
    out.index.name = "date"
    return out


def fetch(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    url = f"{BASE}?startPeriod={start:%Y-%m-%d}&endPeriod={end:%Y-%m-%d}&format=csvdata"
    r = http.get(url, {"Accept": "text/csv"}, label="ECB €STR")
    return parse(r.text())
