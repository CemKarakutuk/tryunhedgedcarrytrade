"""Retrieve every dataset from its official source and merge it into data/store/.

    python scripts/fetch_data.py                 # incremental update of all sources
    python scripts/fetch_data.py --full          # full historical back-fill (merged, never truncating)
    python scripts/fetch_data.py --only sofr,fx  # selected datasets
    python scripts/fetch_data.py --import-raw    # merge files uploaded to data/raw/ into the store
    python scripts/fetch_data.py --offline       # no network (status refresh / raw import only)

Each dataset is retrieved, validated and written independently: a failing provider
keeps its last valid store file untouched and is reported in data/data_status.json,
while the others continue. EVDS needs the EVDS_API_KEY environment variable
(GitHub secret); the other four sources are anonymous official endpoints.
Exit code is 0 unless --strict is given and at least one dataset failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import store
from providers.http import ProviderError, scrub
from sources import BY_KEY, DATASETS, STATUS_ITEMS

ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT / "data" / "data_status.json"
VOLATILE = ("generated", "lastAttempt", "ageDays")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def process(ds, new: pd.DataFrame, today, accept_revisions=False, origin="api") -> tuple[str, str, bool]:
    """Merge + validate + atomic write. Returns (result, message, changed)."""
    old = store.read(ds.path, ds.columns)
    unknown = [c for c in new.columns if c not in ds.columns]
    new = new.drop(columns=unknown)
    if new.dropna(how="all").empty:
        return "validation_failed", f"{origin}: response held no usable observations", False
    merged, rep = store.merge(old, new, today)
    merged = merged.reindex(columns=ds.columns)
    errs = store.validate(merged, old, ds, rep, today, accept_revisions)
    if errs:
        return "validation_failed", f"{origin}: " + " | ".join(errs), False
    changed = not (merged.index.equals(old.index) and np.array_equal(merged.values, old.reindex(columns=ds.columns).values, equal_nan=True))
    if changed:
        store.write_frame(ds.path, merged)
    return "ok", f"{origin}: {rep.summary()}", changed


def run(args) -> dict:
    today = store.today()
    keys = [k.strip() for k in args.only.split(",")] if args.only else [d.key for d in DATASETS]
    results, any_change = {}, False
    for key in keys:
        ds = BY_KEY[key]
        res = {"result": "skipped", "message": "", "attempt": None}
        if args.import_raw and ds.seed is not None:
            try:
                seed = ds.seed()
                if seed is not None:
                    r, m, ch = process(ds, seed, today, accept_revisions=True, origin="data/raw import")
                    any_change |= ch
                    res = {"result": r, "message": m, "attempt": now_iso()}
                    print(f"[{key}] {m}")
            except Exception as e:                                    # a broken upload must not stop the run
                res = {"result": "validation_failed", "message": f"data/raw import: {scrub(str(e))}", "attempt": now_iso()}
                print(f"[{key}] raw import failed: {res['message']}")
        if not args.offline:
            old = store.read(ds.path, ds.columns)
            if args.full or old.dropna(how="all").empty:
                start = pd.Timestamp(ds.backfill_start)
            else:
                start = old.dropna(how="all").index.max() - pd.Timedelta(days=ds.lookback_days)
            # EVDS FX rows carry the date the rate applies to (up to the next business day)
            end = today + pd.Timedelta(days=7 if ds.future_days else 0)
            attempt = now_iso()
            try:
                new = ds.fetch(start, end)
                r, m, ch = process(ds, new, today, args.accept_revisions)
                any_change |= ch
                res = {"result": r, "message": m, "attempt": attempt}
            except ProviderError as e:
                res = {"result": "unavailable", "message": str(e), "attempt": attempt}
            except Exception as e:                                    # unexpected bug: report, keep old data
                res = {"result": "unavailable", "message": scrub(f"{type(e).__name__}: {e}"), "attempt": attempt}
                traceback.print_exc(limit=2)
            print(f"[{key}] {res['result']}: {res['message']}")
        results[key] = res
    write_status(results, today, any_change)
    return results


def _age(last: pd.Timestamp, today: pd.Timestamp, freq: str) -> int:
    if last >= today:
        return 0
    if freq == "daily":   # weekdays elapsed after the last observation, up to and including today
        return int(np.busday_count((last + pd.Timedelta(days=1)).date(), (today + pd.Timedelta(days=1)).date()))
    return int((today - last).days)


def build_status(results: dict, today: pd.Timestamp, previous: dict) -> dict:
    prev = {s["id"]: s for s in previous.get("sources", [])}
    out = []
    for it in STATUS_ITEMS:
        ds = BY_KEY[it["ds"]]
        df = store.read(ds.path, ds.columns)
        cov = [store.column_coverage(df[c]) for c in it["cols"]]
        res = results.get(it["ds"], {"result": "skipped", "message": "", "attempt": None})
        p = prev.get(it["id"], {})
        entry = {
            "id": it["id"], "label_tr": it.get("label_tr", it["id"]), "label_en": it.get("label_en", it["id"]),
            "provider": ds.provider, "series": [c.replace("_", ".") if c != "rate" else ds.file[:-4].upper() for c in it["cols"]],
            "url": ds.url, "frequency": it["freq"], "unit": ds.unit,
            "expectedLag_tr": it["lag_tr"], "expectedLag_en": it["lag_en"],
            "firstObs": None, "lastObs": None, "count": 0,
            "lastAttempt": res["attempt"] or p.get("lastAttempt"),
            "lastSuccess": res["attempt"] if res["result"] == "ok" else p.get("lastSuccess"),
            "message": res["message"] if res["result"] in ("unavailable", "validation_failed") else "",
        }
        if all(c["count"] for c in cov):
            entry["firstObs"] = max(c["first"] for c in cov)
            entry["lastObs"] = min(c["last"] for c in cov)
            entry["count"] = min(c["count"] for c in cov)
            age = _age(pd.Timestamp(entry["lastObs"]), today, it["freq"])
            entry["ageDays"] = age
            fresh = "current" if age <= it["tol"] else "stale"
        else:
            fresh = "unavailable"
            if not entry["message"]:
                entry["message"] = "not retrieved yet"
        if res["result"] == "validation_failed":
            entry["status"] = "validation_failed"
        elif res["result"] == "unavailable":
            entry["status"] = "unavailable"
        elif res["result"] == "skipped" and p.get("status") in ("unavailable", "validation_failed") and fresh != "unavailable":
            entry["status"] = p["status"]          # offline run: keep the last retrieval verdict
            entry["message"] = p.get("message", "")
        else:
            entry["status"] = fresh
        out.append(entry)
    return {"generated": now_iso(), "schema": 1, "sources": out}


def write_status(results: dict, today: pd.Timestamp, data_changed: bool) -> None:
    try:
        previous = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.exists() else {}
    except json.JSONDecodeError:
        previous = {}
    status = build_status(results, today, previous)
    # lastSuccess only moves when data changed; otherwise a no-news run would commit every time
    if not data_changed:
        prev = {s["id"]: s for s in previous.get("sources", [])}
        for s in status["sources"]:
            if s["id"] in prev and s["status"] == prev[s["id"]].get("status"):
                s["lastSuccess"] = prev[s["id"]].get("lastSuccess")
                s["lastAttempt"] = prev[s["id"]].get("lastAttempt")
    vol = VOLATILE if not data_changed else ("generated",)
    if store.write_json_if_changed(STATUS_PATH, status, volatile=vol, ensure_ascii=False, indent=1):
        print("data_status.json updated")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="full historical back-fill")
    ap.add_argument("--only", default="", help="comma-separated dataset keys")
    ap.add_argument("--import-raw", action="store_true", help="merge files in data/raw/ into the store")
    ap.add_argument("--offline", action="store_true", help="no network requests")
    ap.add_argument("--accept-revisions", action="store_true", help="accept large revisions of old history")
    ap.add_argument("--strict", action="store_true", help="non-zero exit if any dataset failed")
    args = ap.parse_args(argv)
    results = run(args)
    failed = [k for k, r in results.items() if r["result"] in ("unavailable", "validation_failed")]
    if failed:
        print(f"::warning::datasets not updated (previous valid data kept): {', '.join(failed)}")
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    sys.exit(main())
