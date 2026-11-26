"""CBRT EVDS (EVDS3) provider.

Service contract (EVDS3, in force since the EVDS2 host was retired):
  GET https://evds3.tcmb.gov.tr/igmevdsms-dis/series=A-B&startDate=DD-MM-YYYY&endDate=DD-MM-YYYY&type=json[&frequency=n&aggregationTypes=x]
  * parameters are appended to the path (no "?")
  * the API key travels ONLY in the HTTP request header named "key"
  * response: {"items": [{"Tarih": "...", "TP_X_Y": "1.23" | null, ...}], "totalCount": n}
  * dotted series codes come back with underscores (TP.DK.USD.A.YTL -> TP_DK_USD_A_YTL)

The key is read from the EVDS_API_KEY environment variable (a GitHub Actions secret
in CI). It is never logged, never put in a URL and is scrubbed from error messages.
"""
from __future__ import annotations

import json
import os
import re
from datetime import timedelta

import pandas as pd

from . import http

BASE_URL = os.environ.get("EVDS_BASE_URL", "https://evds3.tcmb.gov.tr/igmevdsms-dis/")
PORTAL = "https://evds3.tcmb.gov.tr/"
FREQ_CODES = {"daily": 1, "business": 2, "weekly": 3, "monthly": 5}


def api_key() -> str:
    key = os.environ.get("EVDS_API_KEY", "").strip()
    if not key:
        raise http.ProviderError("EVDS_API_KEY is not set; EVDS series were not retrieved")
    http.register_secret(key)
    return key


def col(code: str) -> str:
    return code.replace(".", "_")


def build_url(series: list[str], start: pd.Timestamp, end: pd.Timestamp,
              frequency: str | None = None, aggregation: str | None = None) -> str:
    q = (f"series={'-'.join(series)}&startDate={start:%d-%m-%Y}&endDate={end:%d-%m-%Y}&type=json")
    if frequency:
        q += f"&frequency={FREQ_CODES[frequency]}"
    if aggregation:
        q += f"&aggregationTypes={aggregation}"
    return BASE_URL + q


def parse_date(raw, kind: str) -> pd.Timestamp | None:
    s = str(raw).strip()
    if re.match(r"^\d{2}-\d{2}-\d{4}$", s):
        d = pd.to_datetime(s, format="%d-%m-%Y")
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        d = pd.to_datetime(s, format="%Y-%m-%d")
    elif m := re.match(r"^(\d{4})-(\d{1,2})$", s):
        d = pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)
    else:
        return None
    d = pd.Timestamp(d)
    if kind == "weekly":                 # anchor every weekly observation to the Friday of its week
        d = d - timedelta(days=d.weekday()) + timedelta(days=4)
    elif kind == "monthly":              # monthly observation = reference month-end
        d = d + pd.offsets.MonthEnd(0)
    return d


def parse(payload_text: str, series: list[str], kind: str) -> pd.DataFrame:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        raise http.ProviderError("EVDS: response is not JSON (service may have returned an HTML page)")
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise http.ProviderError("EVDS: response contains no items")
    cols = [col(s) for s in series]
    seen = {c: False for c in cols}
    rows = []
    for it in items:
        d = parse_date(it.get("Tarih"), kind)
        if d is None:
            continue
        row = {"date": d}
        for c in cols:
            if c in it:
                seen[c] = True
            v = it.get(c)
            row[c] = pd.to_numeric(v, errors="coerce") if v not in (None, "", "null") else float("nan")
        rows.append(row)
    missing = [c for c, ok in seen.items() if not ok]
    if missing:
        raise http.ProviderError(f"EVDS: series not returned by the service: {', '.join(missing)}")
    if not rows:
        raise http.ProviderError("EVDS: no dated rows in response")
    df = pd.DataFrame(rows).set_index("date").sort_index()
    df = df.groupby(level=0).last()           # duplicate dates: keep the last value
    df = df.dropna(how="all")
    if df.empty:
        raise http.ProviderError("EVDS: all returned values are empty")
    df.index.name = "date"
    return df.astype("float64")


def fetch(series: list[str], start: pd.Timestamp, end: pd.Timestamp, kind: str = "daily",
          frequency: str | None = None, aggregation: str | None = None) -> pd.DataFrame:
    key = api_key()
    url = build_url(series, start, end, frequency, aggregation)
    try:
        r = http.get(url, {"key": key, "Accept": "application/json"}, label="EVDS")
    except http.ProviderError:
        today = pd.Timestamp.today().normalize()
        if end <= today:
            raise
        # a future end date (used to catch tomorrow-dated FX rows) is retried as today
        r = http.get(build_url(series, start, today, frequency, aggregation),
                     {"key": key, "Accept": "application/json"}, label="EVDS")
    if "html" in r.content_type:
        raise http.ProviderError("EVDS: HTML page returned instead of JSON")
    return parse(r.text(), series, kind)
