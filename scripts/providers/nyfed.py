"""Federal Reserve Bank of New York: SOFR (Markets Data API, anonymous).

  GET https://markets.newyorkfed.org/api/rates/secured/sofr/search.json?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
  -> {"refRates": [{"effectiveDate": "2026-11-24", "type": "SOFR", "percentRate": 3.94, ...}]}

effectiveDate is the business day the rate applies to (the rate is published at
08:00 ET on the following business day). percentRate is in percent, ACT/360.
"""
from __future__ import annotations

import json

import pandas as pd

from . import http

BASE = "https://markets.newyorkfed.org/api/rates/secured/sofr/search.json"
DOCS = "https://markets.newyorkfed.org/static/docs/markets-api.html"


def parse(text: str) -> pd.DataFrame:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise http.ProviderError("NY Fed: response is not JSON")
    rates = payload.get("refRates") if isinstance(payload, dict) else None
    if not isinstance(rates, list):
        raise http.ProviderError("NY Fed: 'refRates' missing from response")
    rows = []
    for r in rates:
        if str(r.get("type", "SOFR")).upper() != "SOFR":
            continue
        d = pd.to_datetime(r.get("effectiveDate"), format="%Y-%m-%d", errors="coerce")
        v = pd.to_numeric(r.get("percentRate"), errors="coerce")
        if pd.notna(d) and pd.notna(v):
            rows.append((d, float(v)))
    if not rows:
        raise http.ProviderError("NY Fed: no SOFR observations in response")
    s = pd.Series(dict(rows)).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    df = s.to_frame("rate")
    df.index.name = "date"
    return df


def fetch(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    # Long back-fills are split by calendar year to keep each response small.
    parts, a = [], pd.Timestamp(start)
    while a <= end:
        b = min(pd.Timestamp(a.year, 12, 31), pd.Timestamp(end))
        url = f"{BASE}?startDate={a:%Y-%m-%d}&endDate={b:%Y-%m-%d}"
        r = http.get(url, {"Accept": "application/json"}, label="NY Fed SOFR")
        parts.append(parse(r.text()))
        a = b + pd.Timedelta(days=1)
    df = pd.concat(parts).sort_index()
    return df[~df.index.duplicated(keep="last")]
