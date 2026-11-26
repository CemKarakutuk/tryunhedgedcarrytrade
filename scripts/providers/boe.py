"""Bank of England: SONIA (Interactive Database, official CSV download facility, anonymous).

  GET https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes
        &Datefrom=DD/Mon/YYYY&Dateto=now&SeriesCodes=IUDSOIA&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N
  -> "DATE,IUDSOIA\n02 Jan 2019,0.7044\n..."

IUDSOIA = Daily Sterling overnight index average (SONIA) rate, percent, ACT/365.
The date is the business day the rate applies to (published 09:00 London on the
next business day). VPD=Y includes provisional observations, as on the BoE site.
The IADB tends to answer non-browser clients with an HTML error page; that case is
detected and rejected.
"""
from __future__ import annotations

import io

import pandas as pd

from . import http

SERIES = "IUDSOIA"
BASE = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
DOCS = "https://www.bankofengland.co.uk/boeapps/database/help.asp"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def parse(text: str) -> pd.DataFrame:
    t = text.lstrip()
    if t.startswith("<") or "<html" in t[:500].lower():
        raise http.ProviderError("BoE: HTML page returned instead of CSV")
    df = pd.read_csv(io.StringIO(t), dtype=str)
    if df.shape[1] < 2 or SERIES not in [c.strip().upper() for c in df.columns]:
        raise http.ProviderError(f"BoE: column {SERIES} missing from CSV")
    vcol = [c for c in df.columns if c.strip().upper() == SERIES][0]
    d = pd.to_datetime(df.iloc[:, 0].str.strip(), format="%d %b %Y", errors="coerce")
    v = pd.to_numeric(df[vcol], errors="coerce")
    s = pd.Series(v.values, index=d).dropna()
    s = s[s.index.notna()]
    if s.empty:
        raise http.ProviderError("BoE: no observations in CSV")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    out = s.astype("float64").to_frame("rate")
    out.index.name = "date"
    return out


def fetch(start: pd.Timestamp, end: pd.Timestamp | None = None) -> pd.DataFrame:
    dto = "now" if end is None else f"{end:%d/%b/%Y}"
    url = (f"{BASE}?csv.x=yes&Datefrom={start:%d/%b/%Y}&Dateto={dto}&SeriesCodes={SERIES}"
           f"&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
    r = http.get(url, {"User-Agent": BROWSER_UA, "Accept": "text/csv,*/*"}, label="BoE SONIA")
    return parse(r.text())
