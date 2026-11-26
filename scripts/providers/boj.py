"""Bank of Japan: TONA (BOJ Time-Series Data Search API, anonymous, launched Feb 2026).

  GET https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=json&lang=en&db=FM01
        &code=STRDCLUCON&startDate=YYYYMM&endDate=YYYYMM[&startPosition=n]

Series FM01'STRDCLUCON = "Call Rate, Uncollateralized Overnight, Average (Daily)",
i.e. TONA, percent, ACT/365 - the exact series of the existing data/raw/tona.csv.
The code is passed WITHOUT the "FM01'" prefix (API manual). Daily series take YYYYMM
dates in requests and return YYYYMMDD in SURVEY_DATES; missing values are null.
Paging: a numeric NEXTPOSITION means more data; pass it back as startPosition.
The manual asks for a sufficient interval between requests; one request per run is
the normal case. Credit line required by the BoJ is shown on the website.
"""
from __future__ import annotations

import json

import pandas as pd

from . import http

DB, CODE = "FM01", "STRDCLUCON"
BASE = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
DOCS = "https://www.stat-search.boj.or.jp/info/api_manual_en.pdf"
NAME_TOKENS = ("uncollateralized overnight", "average")


def _find(obj, key):
    """Depth-first search for a key in nested dicts/lists (the JSON layout is only
    shown as images in the manual, so the parser does not hard-code nesting)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.upper() == key:
                return v
        for v in obj.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def parse(text: str) -> tuple[pd.DataFrame, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise http.ProviderError("BoJ: response is not JSON")
    status = _find(payload, "STATUS")
    if status is not None and str(status) != "200":
        raise http.ProviderError(f"BoJ: API status {status} ({_find(payload, 'MESSAGEID')})")
    name = str(_find(payload, "NAME_OF_TIME_SERIES") or "")
    code = str(_find(payload, "SERIES_CODE") or "")
    if CODE not in code.upper():
        raise http.ProviderError(f"BoJ: unexpected series code in response ({code or 'none'})")
    if name and not all(t in name.lower() for t in NAME_TOKENS):
        raise http.ProviderError(f"BoJ: series name '{name}' is not the uncollateralized overnight call rate average")
    dates = _find(payload, "SURVEY_DATES")
    vals = _find(payload, "VALUES")
    if isinstance(vals, dict):                    # VALUES may wrap both arrays
        dates = dates or _find(vals, "SURVEY_DATES")
        vals = _find(vals, "VALUES")
    if not isinstance(dates, list) or not isinstance(vals, list) or len(dates) != len(vals):
        raise http.ProviderError("BoJ: SURVEY_DATES/VALUES arrays missing or of unequal length")
    d = pd.to_datetime([str(x) for x in dates], format="%Y%m%d", errors="coerce")
    v = pd.to_numeric(pd.Series(vals, dtype="object"), errors="coerce").values
    s = pd.Series(v, index=d).dropna()
    s = s[s.index.notna()]
    out = s[~s.index.duplicated(keep="last")].sort_index().astype("float64").to_frame("rate")
    out.index.name = "date"
    return out, _find(payload, "NEXTPOSITION")


def fetch(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    parts, pos = [], None
    for _ in range(50):
        url = (f"{BASE}?format=json&lang=en&db={DB}&code={CODE}"
               f"&startDate={start:%Y%m}&endDate={end:%Y%m}")
        if pos:
            url += f"&startPosition={pos}"
        r = http.get(url, {"Accept": "application/json"}, label="BoJ TONA")
        df, nxt = parse(r.text())
        parts.append(df)
        if nxt in (None, "", "null"):
            break
        pos = int(nxt)
        http.sleeper(2)                            # be gentle between paged requests
    out = pd.concat(parts).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    if out.empty:
        raise http.ProviderError("BoJ: no observations in response")
    return out
