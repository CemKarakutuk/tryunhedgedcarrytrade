"""Provider parsing, HTTP retry behaviour and secret handling (all offline)."""
import io
import json
import urllib.error

import pandas as pd
import pytest

from providers import boe, boj, ecb, evds, http, nyfed


class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, ctype="application/json", status=200):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, retry_after=None):
    hdrs = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://example.org", code, "err", hdrs, None)


def script(monkeypatch, steps):
    """Fake opener that plays back a list of responses/exceptions and records requests."""
    calls, sleeps = [], []

    def opener(req, timeout):
        calls.append(req)
        step = steps[min(len(calls) - 1, len(steps) - 1)]
        if isinstance(step, Exception):
            raise step
        return step
    monkeypatch.setattr(http, "opener", opener)
    monkeypatch.setattr(http, "sleeper", sleeps.append)
    return calls, sleeps


# ── HTTP layer ────────────────────────────────────────────────────────────
def test_retries_then_succeeds(monkeypatch):
    calls, sleeps = script(monkeypatch, [http_error(503), urllib.error.URLError("reset"), FakeResp(b"ok")])
    r = http.get("https://example.org/x", retries=3, label="t")
    assert r.body == b"ok" and len(calls) == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0] - 1          # exponential backoff (with jitter)


def test_rate_limit_honours_retry_after(monkeypatch):
    calls, sleeps = script(monkeypatch, [http_error(429, "7"), FakeResp(b"ok")])
    http.get("https://example.org/x", retries=2, label="t")
    assert sleeps == [7.0]


def test_client_error_not_retried(monkeypatch):
    calls, _ = script(monkeypatch, [http_error(404)])
    with pytest.raises(http.ProviderError):
        http.get("https://example.org/x", retries=4, label="t")
    assert len(calls) == 1


def test_gives_up_after_retries(monkeypatch):
    calls, _ = script(monkeypatch, [TimeoutError("slow")])
    with pytest.raises(http.ProviderError, match="after 3 attempts"):
        http.get("https://example.org/x", retries=2, label="t")


# ── EVDS ──────────────────────────────────────────────────────────────────
EVDS_PAYLOAD = {"totalCount": 4, "items": [
    {"Tarih": "02-01-2019", "TP_DK_USD_A_YTL": "5.2810", "TP_DK_JPY_A_YTL": "4.7830", "UNIXTIME": {"$numberLong": "1"}},
    {"Tarih": "05-01-2019", "TP_DK_USD_A_YTL": None, "TP_DK_JPY_A_YTL": None},     # weekend row
    {"Tarih": "03-01-2019", "TP_DK_USD_A_YTL": "5.3316", "TP_DK_JPY_A_YTL": "4.8751"},
    {"Tarih": "03-01-2019", "TP_DK_USD_A_YTL": "5.3317", "TP_DK_JPY_A_YTL": "4.8751"},  # duplicate date
]}


def test_evds_parse_daily():
    df = evds.parse(json.dumps(EVDS_PAYLOAD), ["TP.DK.USD.A.YTL", "TP.DK.JPY.A.YTL"], "daily")
    assert list(df.columns) == ["TP_DK_USD_A_YTL", "TP_DK_JPY_A_YTL"]
    assert list(df.index.strftime("%Y-%m-%d")) == ["2019-01-02", "2019-01-03"]   # sorted, weekend dropped
    assert df.loc["2019-01-03", "TP_DK_USD_A_YTL"] == 5.3317                  # duplicate: last wins
    assert df.loc["2019-01-02", "TP_DK_JPY_A_YTL"] == 4.783                   # native per-100-yen kept


def test_evds_weekly_and_monthly_dates():
    w = evds.parse(json.dumps({"items": [{"Tarih": "31-12-2018", "TP_AB_N06": "-5"}]}), ["TP.AB.N06"], "weekly")
    assert w.index[0] == pd.Timestamp("2019-01-04") and w.iloc[0, 0] == -5.0   # Friday anchor, negative kept
    m = evds.parse(json.dumps({"items": [{"Tarih": "2026-2", "TP_DOVVARNC_K14": "-1.5"}]}), ["TP.DOVVARNC.K14"], "monthly")
    assert m.index[0] == pd.Timestamp("2026-02-28")


@pytest.mark.parametrize("body,msg", [
    ("<html>EVDS</html>", "not JSON"),
    (json.dumps({"items": []}), "no items"),
    (json.dumps({"items": [{"Tarih": "02-01-2019", "OTHER": "1"}]}), "not returned"),
    (json.dumps({"items": [{"Tarih": "02-01-2019", "TP_X": None}]}), "empty"),
])
def test_evds_rejects_malformed(body, msg):
    with pytest.raises(http.ProviderError, match=msg):
        evds.parse(body, ["TP.X"], "daily")


def test_evds_key_only_in_header_and_scrubbed(monkeypatch):
    monkeypatch.setenv("EVDS_API_KEY", "SECRETKEY123")
    calls, _ = script(monkeypatch, [FakeResp(json.dumps(EVDS_PAYLOAD).encode())])
    evds.fetch(["TP.DK.USD.A.YTL", "TP.DK.JPY.A.YTL"], pd.Timestamp("2019-01-01"), pd.Timestamp("2019-01-10"))
    req = calls[0]
    assert "SECRETKEY123" not in req.full_url
    assert req.get_header("Key") == "SECRETKEY123"
    assert req.full_url.startswith("https://evds3.tcmb.gov.tr/igmevdsms-dis/series=TP.DK.USD.A.YTL-TP.DK.JPY.A.YTL&startDate=01-01-2019")
    err = http.ProviderError("failed with SECRETKEY123 in text")
    assert "SECRETKEY123" not in str(err)


def test_evds_missing_key(monkeypatch):
    monkeypatch.delenv("EVDS_API_KEY", raising=False)
    with pytest.raises(http.ProviderError, match="EVDS_API_KEY is not set"):
        evds.fetch(["TP.X"], pd.Timestamp("2019-01-01"), pd.Timestamp("2019-01-02"))


# ── NY Fed / ECB / BoE / BoJ ───────────────────────────────────────────────
def test_nyfed_parse():
    body = json.dumps({"refRates": [
        {"effectiveDate": "2026-11-24", "type": "SOFR", "percentRate": 3.94},
        {"effectiveDate": "2026-11-21", "type": "SOFR", "percentRate": 3.95},
        {"effectiveDate": "2026-11-24", "type": "SOFRAI", "percentRate": 9.99}]})
    df = nyfed.parse(body)
    assert df["rate"].tolist() == [3.95, 3.94]
    with pytest.raises(http.ProviderError):
        nyfed.parse(json.dumps({"refRates": []}))


def test_nyfed_backfill_split_by_year(monkeypatch):
    body = json.dumps({"refRates": [{"effectiveDate": "2019-01-02", "type": "SOFR", "percentRate": 3.0}]}).encode()
    calls, _ = script(monkeypatch, [FakeResp(body), FakeResp(body), FakeResp(body)])
    nyfed.fetch(pd.Timestamp("2019-01-01"), pd.Timestamp("2021-06-30"))
    assert len(calls) == 3 and "endDate=2019-12-31" in calls[0].full_url


ECB_CSV = ("KEY,FREQ,TIME_PERIOD,OBS_VALUE\n"
           "EST.B.EU000A2X2A25.WT,B,2019-10-01,-0.549\n"
           "EST.B.EU000A2X2A25.WT,B,2019-10-02,-0.551\n")


def test_ecb_parse_keeps_negative_rates():
    df = ecb.parse(ECB_CSV)
    assert df["rate"].tolist() == [-0.549, -0.551]
    with pytest.raises(http.ProviderError, match="unexpected series"):
        ecb.parse(ECB_CSV.replace("EU000A2X2A25.WT", "OTHER.WT"))


def test_boe_parse():
    df = boe.parse("DATE,IUDSOIA\n02 Jan 2019,0.7044\n03 Jan 2019,0.7048\n")
    assert df["rate"].tolist() == [0.7044, 0.7048]
    with pytest.raises(http.ProviderError, match="HTML"):
        boe.parse("<!DOCTYPE html><html><body>error</body></html>")


def boj_payload(name="Call Rate, Uncollateralized Overnight, Average (Daily)", nxt=None):
    return json.dumps({"STATUS": 200, "MESSAGEID": "M181000I", "NEXTPOSITION": nxt, "RESULTSET": [{
        "SERIES_CODE": "STRDCLUCON", "NAME_OF_TIME_SERIES": name, "UNIT": "% per annum", "FREQUENCY": "DAILY",
        "VALUES": {"SURVEY_DATES": [20190103, 20190104, 20190105], "VALUES": [None, -0.07, None]}}]})


def test_boj_parse_and_series_guard():
    df, nxt = boj.parse(boj_payload())
    assert df.index.tolist() == [pd.Timestamp("2019-01-04")] and df["rate"].iloc[0] == -0.07 and nxt is None
    with pytest.raises(http.ProviderError, match="not the uncollateralized"):
        boj.parse(boj_payload(name="Basic Discount Rate"))
    with pytest.raises(http.ProviderError, match="status 400"):
        boj.parse(json.dumps({"STATUS": 400, "MESSAGEID": "M181001E"}))


def test_boj_paging(monkeypatch):
    calls, _ = script(monkeypatch, [FakeResp(boj_payload(nxt=2).encode()), FakeResp(boj_payload().encode())])
    boj.fetch(pd.Timestamp("2019-01-01"), pd.Timestamp("2019-02-01"))
    assert len(calls) == 2 and "startPosition=2" in calls[1].full_url
    assert "code=STRDCLUCON" in calls[0].full_url and "db=FM01" in calls[0].full_url
