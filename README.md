# Global Unhedged TRY Carry Trade

Türkçe ve İngilizce · *Site is bilingual (TR/EN); use the language button in the top-right corner.*

An interactive, open-data study of the classic carry trade into the Turkish lira: borrow overnight in **New York (SOFR)**, **Frankfurt (€STR)**, **London (SONIA)** or **Tokyo (TONA)**, convert into lira, and earn the lira overnight reference rate **TLREF**. Daily, from 2 January 2019 (€STR from 1 October 2019) to the present.

Companion to the deposit-based study [Carry trade returns and TL deposit rate policy](https://cemkarakutuk.github.io/trcarryreturn/), which uses weighted-average TL deposit rates back to 2002. This study uses the overnight rate the institutional market actually trades, over a shorter but daily window.

## What the site does

The visitor chooses the funding currency (USD, EUR, GBP, JPY) and a holding period in days. The chart shows, for every opening day since 2019, the gross return of a position opened that day and closed after that many days (the annualised figure appears on hover). Zooming works on the date axis. The FX change over the same period and event markers can be switched on and off.

Below the chart, the calculation section takes an opening date (typed, or picked by clicking the chart), a number of days and an amount, and lists the position on every valuation day: TLREF and funding rate, buying and selling rates, TL balance, value if converted, debt, profit/loss and return. Chart data, the calculation detail and the combined dataset can be downloaded as CSV.

## Method

Each reference rate applies from the trade date it refers to until the next trade date, accruing for calendar days (SOFR Index logic). SOFR, €STR, SONIA and TONA are published on the following business day; the calculation uses the trade date, not the publication date:

```
I(d_{i+1}) = I(d_i) · (1 + r_i · (d_{i+1} − d_i) / B)
R(t)       = A(t0) · [I_TL(t) / I_TL(t0)] / S(t) − I_F(t) / I_F(t0)
```

`A` and `S` are CBRT buying and selling rates, `B` is the day-count basis (TLREF, SONIA, TONA ACT/365; SOFR, €STR ACT/360). The return is relative to the foreign currency principal borrowed and is given for the holding period, not annualised.

**Exchange rate timing.** In EVDS, the CBRT indicative rate dated *t* is set at 15:30 on the previous business day and valid on *t* (the rate dated 22 Dec 2021 reflects the 21 Dec market, after KKM was announced). Rates are shifted back to the day they were set so they line up with that day's TLREF and funding rate.

Returns are **gross**: no taxes, fees.

## Repository layout

```
index.html                     Site
assets/css/styles.css          Styling
assets/js/app.js               Calculation engine, chart, bilingual UI, data status panel
data/dataset.json, .csv        Processed carry dataset the site reads
data/reserves.json, .csv       CBRT reserves in USD bn (weekly; daily series once available)
data/reserves_metadata.json    Series codes, native units, conversions, coverage, warnings
data/data_status.json          Per-source status shown in the "Data status" panel
data/events.json               Event markers on the main chart
data/store/                    Observation store: one CSV per source, native units (raw data)
data/raw/                      Original hand-downloaded files (archive; still accepted as uploads)
scripts/providers/             Official API clients: evds, nyfed, ecb, boe, boj (+ shared http)
scripts/fetch_data.py          Retrieves, merges and validates every source into data/store/
scripts/update_data.py         Builds the carry dataset (methodology unchanged)
scripts/update_reserves.py     Builds the reserve datasets
scripts/validate_data.py       Validates outputs against the last valid state, restores on failure
tests/                         Offline tests (providers, carry regression, reserves, end-to-end)
.github/workflows/             Scheduled update workflow
```

## Automated data updates

The workflow `.github/workflows/veri-guncelle.yml` runs every weekday at 09:30, 16:30 and
19:30 Türkiye time (06:30, 13:30, 16:30 UTC). It runs the tests, retrieves every source
independently from its official endpoint, merges new observations into `data/store/`
(history is never shortened), rebuilds the datasets, validates them against the previous
state and commits only when something actually changed. A failing source keeps its last
valid data and is flagged in the Data status panel; if the processed data fail validation
the previous files are restored and the run is marked as failed.

| Dataset | Endpoint | Series | Auth |
| --- | --- | --- | --- |
| TLREF, FX, reserves, balance sheet, swaps, IRFCL | `https://evds3.tcmb.gov.tr/igmevdsms-dis/` | `TP.BISTTLREF.ORAN`, `TP.DK.{USD,EUR,GBP,JPY}.{A,S}.YTL`, `TP.AB.{TOPLAM,C1,C2,N06,A02,A10}`, `TP.SWAPTEKTAR.TOTALSTOK{ALIM,SATIM}YONLU`, `TP.DOVVARNC.{K14,K23}` | `key` header |
| SOFR | `https://markets.newyorkfed.org/api/rates/secured/sofr/search.json` | SOFR | none |
| €STR | `https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT` | EST.B.EU000A2X2A25.WT | none |
| SONIA | `https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp` | IUDSOIA | none |
| TONA | `https://www.stat-search.boj.or.jp/api/v1/getDataCode` | FM01 `STRDCLUCON` | none |

### Configuring the EVDS key

1. Copy the API key from your EVDS3 profile page.
2. GitHub → repository **Settings → Secrets and variables → Actions → New repository secret**.
   Name: `EVDS_API_KEY`, value: the key. The key is passed to the job as an environment
   variable, sent only in the HTTP `key` header, masked in logs and never written to any file.
3. Locally: `cp .env.example .env`, put the key in `.env` (git-ignored), then `set -a; . ./.env; set +a`.

### Manual update

GitHub → **Actions → Veri güncelle → Run workflow**. Tick **full** for a complete
historical back-fill from 2019 (merged with the existing data; nothing is deleted). Tick
**accept_revisions** only if a run was refused because many old observations were revised
and you have checked that the revision is genuine.

Locally:

```bash
pip install -r requirements.txt
python -m pytest -q                       # offline tests
python scripts/fetch_data.py [--full]     # needs EVDS_API_KEY for the EVDS series
python scripts/update_data.py
python scripts/update_reserves.py
python scripts/validate_data.py
python -m http.server                     # then open http://localhost:8000
```

Uploading a file to `data/raw/` (the old manual route) still works: the workflow merges it
into the store instead of replacing anything, so a short download can no longer truncate history.

### Swap-adjusted net reserve estimate

`update_reserves.py` contains the analyst methodology (analytical balance sheet A02 − A10,
minus an estimated foreign swap stock from IRFCL II.2 + II.3, minus domestic buy-side plus
sell-side swap stocks). The estimate is **not published** until every automatic check
passes and a human has reviewed the diagnostics printed in the workflow log; then
change `PUBLISH_SWAP_ESTIMATE: "0"` to `"1"` in the "Build reserve datasets" step of the workflow.
Monthly inputs are used only from the first day of the second month after the reference
month (no look-ahead) and held constant, never interpolated.

## License

MIT — see [LICENSE](LICENSE). The underlying data belongs to its respective publishers (CBRT/EVDS, Borsa İstanbul for TLREF, Federal Reserve Bank of New York, European Central Bank, Bank of England, Bank of Japan). This service uses the API provided by the "Bank of Japan Time-Series Data Search." The Bank of Japan does not guarantee the content of the service.

## Contact

Cem Karakutuk · [cemkarakutuk@hotmail.com](mailto:cemkarakutuk@hotmail.com)

This project is for academic purposes and does not constitute investment advice.
