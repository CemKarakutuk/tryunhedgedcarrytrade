"""Validate the processed datasets before they are published.

    python scripts/validate_data.py --baseline /tmp/data_before [--restore-on-fail]

Compares data/ with a baseline copy taken before the update (the last known good
state). Checks schema, array lengths, date order, coverage (no truncation of first
date, last date or length), plausible values, preservation of negative funding
rates, and that no secret value appears in any public file. With --restore-on-fail
every data file is put back from the baseline and the failure is recorded in
data/data_status.json, so the website keeps serving the last valid data.
Exit code 1 on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PUBLIC_GLOBS = ["data/*.json", "data/*.csv", "data/store/*.csv", "index.html", "assets/js/*.js", "README.md"]


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def check_dataset(js, base, errs):
    for k in ("meta", "dates", "tl", "cur"):
        if k not in js:
            errs.append(f"dataset.json: key '{k}' missing")
            return
    n = len(js["dates"])
    if n < 1500:
        errs.append(f"dataset.json: only {n} valuation days")
    if js["dates"] != sorted(set(js["dates"])):
        errs.append("dataset.json: dates not strictly increasing")
    for k in ("pub", "used", "idx"):
        if len(js["tl"][k]) != n:
            errs.append(f"dataset.json: tl.{k} length {len(js['tl'][k])} != {n}")
    if any(x is None or x <= 0 for x in js["tl"]["idx"]):
        errs.append("dataset.json: TLREF index has missing or non-positive values")
    for c in ("USD", "EUR", "GBP", "JPY"):
        cur = js["cur"].get(c)
        if not cur:
            errs.append(f"dataset.json: currency {c} missing")
            continue
        for k in ("buy", "sell", "pub", "used", "idx"):
            if len(cur[k]) != n:
                errs.append(f"dataset.json: {c}.{k} length {len(cur[k])} != {n}")
        if any(b is None or s is None or b <= 0 or s < b for b, s in zip(cur["buy"], cur["sell"])):
            errs.append(f"dataset.json: {c} buy/sell rates missing, non-positive or crossed")
        if c == "JPY" and max(cur["sell"]) > 5:
            errs.append("dataset.json: JPY rates look like per-100-yen quotes (division by 100 missing)")
        if base and c in base.get("cur", {}):
            if cur["start"] != base["cur"][c]["start"]:
                errs.append(f"dataset.json: {c} start moved {base['cur'][c]['start']} -> {cur['start']}")
            neg_before = sum(1 for x in base["cur"][c]["pub"] if x is not None and x < 0)
            neg_now = sum(1 for x in cur["pub"] if x is not None and x < 0)
            if neg_now < neg_before:
                errs.append(f"dataset.json: {c} negative rate observations fell {neg_before} -> {neg_now}")
    if base:
        bd = base["dates"]
        if js["dates"][0] != bd[0]:
            errs.append(f"dataset.json: first date moved {bd[0]} -> {js['dates'][0]}")
        if js["dates"][-1] < bd[-1] or n < len(bd):
            errs.append(f"dataset.json: history truncated ({len(bd)} days to {bd[-1]} -> {n} days to {js['dates'][-1]})")
        common = min(len(bd), n) - 30          # the last weeks may legitimately be revised
        if common > 0 and js["dates"][:common] == bd[:common]:
            for c in ("USD", "EUR", "GBP", "JPY"):
                a, b = base["cur"][c]["buy"][:common], js["cur"][c]["buy"][:common]
                diff = sum(1 for x, y in zip(a, b) if x != y)
                if diff > 10:
                    errs.append(f"dataset.json: {diff} historical {c} buying rates changed")


def check_csv_rows(path: Path, expected: int, errs):
    if path.exists():
        rows = sum(1 for _ in path.open(encoding="utf-8")) - 1
        if rows != expected:
            errs.append(f"{path.name}: {rows} rows, expected {expected}")


def check_reserves(js, base, errs):
    if js is None:
        errs.append("reserves.json missing")
        return
    ser = js.get("series", {})
    for k in ("net", "gross"):
        if k not in ser:
            errs.append(f"reserves.json: series '{k}' missing")
    for k, s in ser.items():
        if len(s["dates"]) != len(s["values"]):
            errs.append(f"reserves.json: {k} dates/values length mismatch")
        if s["dates"] != sorted(set(s["dates"])):
            errs.append(f"reserves.json: {k} dates not strictly increasing")
        if k in ("gross", "gold", "fx") and any(v is not None and not (0 <= v <= 500) for v in s["values"]):
            errs.append(f"reserves.json: {k} outside 0–500 USD bn")
        if k == "net" and any(v is not None and abs(v) > 300 for v in s["values"]):
            errs.append("reserves.json: net reserves outside ±300 USD bn")
        if s.get("kind") == "estimate" and not s.get("label_en", "").lower().startswith("estimated"):
            errs.append(f"reserves.json: estimate {k} is not labelled as an estimate")
    if base:
        for k, s in base.get("series", {}).items():
            if k in ser and (ser[k]["dates"][:1] != s["dates"][:1] or len(ser[k]["dates"]) < len(s["dates"])):
                errs.append(f"reserves.json: series {k} truncated")


def check_secrets(errs):
    key = os.environ.get("EVDS_API_KEY", "").strip()
    if not key:
        return
    for g in PUBLIC_GLOBS:
        for p in ROOT.glob(g):
            if key in p.read_text(encoding="utf-8", errors="ignore"):
                errs.append(f"SECRET LEAK: API key found in {p.relative_to(ROOT)}")


def validate(baseline: Path | None) -> list[str]:
    errs: list[str] = []
    js = load(DATA / "dataset.json")
    base = load(baseline / "dataset.json") if baseline else None
    if js is None:
        return ["dataset.json missing"]
    check_dataset(js, base, errs)
    check_csv_rows(DATA / "dataset.csv", len(js.get("dates", [])), errs)
    rj = load(DATA / "reserves.json")
    check_reserves(rj, load(baseline / "reserves.json") if baseline else None, errs)
    if rj:
        check_csv_rows(DATA / "reserves.csv", len(rj["series"]["net"]["dates"]) if "net" in rj["series"] else -1, errs)
    st = load(DATA / "data_status.json")
    if st is None or not isinstance(st.get("sources"), list) or len(st["sources"]) != 13:
        errs.append("data_status.json missing or incomplete (13 sources expected)")
    check_secrets(errs)
    return errs


def restore(baseline: Path, errs: list[str]) -> None:
    status = DATA / "data_status.json"
    keep = status.read_text(encoding="utf-8") if status.exists() else None
    for p in baseline.rglob("*"):
        if p.is_file():
            dest = DATA / p.relative_to(baseline)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
    for p in list(DATA.rglob("*")):                 # files created by the failed run
        rel = p.relative_to(DATA)
        if p.is_file() and not (baseline / rel).exists() and rel.parts[0] != "raw":
            p.unlink()
    if keep:
        st = json.loads(keep)
        st["pipelineError"] = "Processed data failed validation; previous valid datasets kept: " + " | ".join(errs)[:2000]
        status.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, help="copy of data/ taken before the update")
    ap.add_argument("--restore-on-fail", action="store_true")
    a = ap.parse_args(argv)
    errs = validate(a.baseline)
    if errs:
        for e in errs:
            print(f"::error::{e}")
        if a.restore_on_fail and a.baseline:
            restore(a.baseline, errs)
            print("Previous valid datasets restored.")
        return 1
    st = DATA / "data_status.json"
    if st.exists():                                   # clear an old pipeline error once data validate again
        js = json.loads(st.read_text(encoding="utf-8"))
        if js.pop("pipelineError", None) is not None:
            st.write_text(json.dumps(js, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
