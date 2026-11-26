"""Canonical observation store: merge, validation and atomic writes.

Each dataset lives in data/store/<name>.csv with a "date" column (YYYY-MM-DD) and
one column per series in the native unit of the source. Missing values are empty
cells; zeros are never substituted. Floats are written in shortest round-trip form,
so reading a file back reproduces exactly the values that were written.

Safety rules implemented here (the "never lose history" requirement):
  * merge() is cell-wise: a new non-missing value adds or revises an observation, a
    missing or absent value never deletes one. A short recent download can
    therefore never truncate history.
  * validate() rejects: unsorted/duplicate dates, future dates, values outside a
    plausible range, any shrinkage of a column's first date, last date or count,
    coverage that starts later than the configured minimum, and suspiciously many
    revisions of old history (a sign of a malformed response).
  * write_atomic() writes to a temporary file in the same folder, reads it back,
    checks the round trip and only then replaces the target with os.replace, so
    readers never see a partially written file.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "store"

REVISION_WINDOW_DAYS = 120     # revisions inside this window are routine
MAX_OLD_REVISIONS = 10         # revised cells older than the window before an update is refused
REL_TOL = 1e-9


def today() -> pd.Timestamp:
    """Current date; PIPELINE_TODAY=YYYY-MM-DD overrides it (tests, replays)."""
    v = os.environ.get("PIPELINE_TODAY")
    return pd.Timestamp(v) if v else pd.Timestamp.today().normalize()


def read(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        df = pd.DataFrame(columns=columns or [], dtype="float64")
        df.index = pd.DatetimeIndex([], name="date")
        return df
    df = pd.read_csv(path, dtype={"date": str})
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("date"), format="%Y-%m-%d"), name="date")
    df = df.apply(pd.to_numeric, errors="coerce").astype("float64")
    for c in columns or []:
        if c not in df.columns:
            df[c] = np.nan
    return df[columns] if columns else df


def to_csv_text(df: pd.DataFrame) -> str:
    out = df.copy()
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    return out.to_csv(lineterminator="\n")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if Path(tmp).read_text(encoding="utf-8") != text:
            raise OSError(f"round-trip check failed for {path.name}")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_frame(path: Path, df: pd.DataFrame) -> None:
    text = to_csv_text(df)
    write_atomic(path, text)
    back = read(path, list(df.columns))
    if not back.index.equals(df.index) or not np.array_equal(back.values, df.values, equal_nan=True):
        raise OSError(f"store round-trip mismatch for {path.name}")


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    write_atomic(path, text)
    return True


def write_json_if_changed(path: Path, obj, volatile: tuple[str, ...] = (), **dump_kw) -> bool:
    """Write JSON only if something other than the volatile top-level/meta keys changed."""
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in volatile}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    if path.exists():
        try:
            if strip(json.loads(path.read_text(encoding="utf-8"))) == strip(json.loads(json.dumps(obj))):
                return False
        except json.JSONDecodeError:
            pass
    write_atomic(path, json.dumps(obj, **dump_kw))
    return True


@dataclass
class MergeReport:
    added: int = 0
    revised: int = 0
    old_revised: int = 0
    duplicates: int = 0
    revised_examples: list = field(default_factory=list)

    def summary(self) -> str:
        s = f"+{self.added} new, {self.revised} revised"
        if self.duplicates:
            s += f", {self.duplicates} duplicate dates collapsed"
        return s


def normalise(new: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Sort, coerce to float and collapse duplicate dates (last value wins)."""
    new = new.copy()
    new.index = pd.DatetimeIndex(new.index).normalize()
    new.index.name = "date"
    dups = int(new.index.duplicated().sum())
    if dups:
        new = new.groupby(level=0).last()
    new = new.apply(pd.to_numeric, errors="coerce").astype("float64").sort_index()
    return new.dropna(how="all"), dups


def merge(old: pd.DataFrame, new: pd.DataFrame, today: pd.Timestamp | None = None) -> tuple[pd.DataFrame, MergeReport]:
    today = globals()["today"]() if today is None else today
    new, dups = normalise(new)
    rep = MergeReport(duplicates=dups)
    cols = list(old.columns) + [c for c in new.columns if c not in old.columns]
    idx = old.index.union(new.index)
    out = old.reindex(index=idx, columns=cols)
    for c in new.columns:
        nv = new[c].dropna()
        if nv.empty:
            continue
        ov = out.loc[nv.index, c]
        is_new = ov.isna()
        changed = ~is_new & ~np.isclose(ov.values, nv.values, rtol=REL_TOL, atol=0.0)
        rep.added += int(is_new.sum())
        rep.revised += int(changed.sum())
        old_cut = today - pd.Timedelta(days=REVISION_WINDOW_DAYS)
        old_changed = changed & (nv.index < old_cut)
        rep.old_revised += int(old_changed.sum())
        for d in nv.index[changed][:3]:
            rep.revised_examples.append(f"{c} {d:%Y-%m-%d}: {out.at[d, c]} -> {nv.at[d]}")
        out.loc[nv.index, c] = nv.values
    out = out.dropna(how="all").sort_index()
    out.index.name = "date"
    return out.astype("float64"), rep


def column_coverage(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {"first": None, "last": None, "count": 0}
    return {"first": s.index[0].strftime("%Y-%m-%d"), "last": s.index[-1].strftime("%Y-%m-%d"), "count": int(s.size)}


def validate(merged: pd.DataFrame, old: pd.DataFrame, spec, rep: MergeReport | None = None,
             today: pd.Timestamp | None = None, accept_revisions: bool = False) -> list[str]:
    """Return a list of human-readable problems; empty list = accepted."""
    today = globals()["today"]() if today is None else today
    errs: list[str] = []
    if merged.empty:
        return ["dataset is empty"]
    if not merged.index.is_monotonic_increasing or merged.index.has_duplicates:
        errs.append("dates are not strictly increasing")
    horizon = today + pd.Timedelta(days=spec.future_days)
    if merged.index.max() > horizon:
        errs.append(f"observation dated in the future ({merged.index.max():%Y-%m-%d})")
    for c in spec.columns:
        s = merged[c].dropna() if c in merged else pd.Series(dtype="float64")
        lo, hi = spec.ranges.get(c, (-np.inf, np.inf))
        bad = s[(s < lo) | (s > hi)]
        if not bad.empty:
            errs.append(f"{c}: {len(bad)} value(s) outside plausible range [{lo}, {hi}], e.g. "
                        f"{bad.index[0]:%Y-%m-%d}={bad.iloc[0]}")
        o = old[c].dropna() if c in old else pd.Series(dtype="float64")
        if not o.empty:
            if s.empty:
                errs.append(f"{c}: all history would be lost")
                continue
            if s.index[0] > o.index[0]:
                errs.append(f"{c}: first date would move from {o.index[0]:%Y-%m-%d} to {s.index[0]:%Y-%m-%d}")
            if s.index[-1] < o.index[-1]:
                errs.append(f"{c}: last date would move back from {o.index[-1]:%Y-%m-%d} to {s.index[-1]:%Y-%m-%d}")
            if s.size < o.size:
                errs.append(f"{c}: observation count would fall from {o.size} to {s.size}")
            # existing negative observations must survive unchanged in sign
            neg = o[o < 0]
            if not neg.empty and (s.reindex(neg.index) >= 0).any():
                errs.append(f"{c}: negative observations would be altered")
        min_start = spec.min_start.get(c)
        if min_start is not None and not s.empty and s.index[0] > pd.Timestamp(min_start):
            errs.append(f"{c}: coverage starts {s.index[0]:%Y-%m-%d}, later than required {min_start}")
        min_count = spec.min_count.get(c)
        if min_count is not None and s.size < min_count:
            errs.append(f"{c}: only {s.size} observations, at least {min_count} expected")
    if rep is not None and rep.old_revised > MAX_OLD_REVISIONS and not accept_revisions:
        errs.append(f"{rep.old_revised} revisions of observations older than {REVISION_WINDOW_DAYS} days "
                    f"(e.g. {'; '.join(rep.revised_examples[:2])}); rerun with --accept-revisions if genuine")
    return errs
