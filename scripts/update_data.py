"""TLREF carry trade veri seti üretici.

data/raw/ klasöründeki kaynak dosyaları okur, gecelik faiz birikim endekslerini
(accrual index) hesaplar ve sitenin okuduğu data/dataset.json ile data/dataset.csv
dosyalarını üretir.

Beklenen dosyalar (adın bu kelimeyle başlaması yeterli, .xlsx veya .csv):
  evds   TCMB EVDS: TP.BISTTLREF.ORAN ve USD/EUR/GBP/JPY döviz alış-satış kurları (günlük)
  sofr   New York Fed SOFR (Effective Date, Rate (%))
  estr   ECB Data Portal €STR (DATE, değer)
  sonia  Bank of England IUDSOIA (Date, değer)
  tona   Bank of Japan FM01'STRDCLUCON (işlem tarihi, değer; NA = işlem günü değil)

Kullanım:  python scripts/update_data.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT_JSON = ROOT / "data" / "dataset.json"
OUT_CSV = ROOT / "data" / "dataset.csv"

START = pd.Timestamp("2019-01-01")

# Para birimi → fonlama (gecelik risksiz) faizi ve gün sayım esası
FUNDING = {
    "USD": {"rate": "SOFR", "src": "sofr", "basis": 360, "center": "New York", "cb": "Fed"},
    "EUR": {"rate": "€STR", "src": "estr", "basis": 360, "center": "Frankfurt", "cb": "ECB"},
    "GBP": {"rate": "SONIA", "src": "sonia", "basis": 365, "center": "London", "cb": "BoE"},
    "JPY": {"rate": "TONA", "src": "tona", "basis": 365, "center": "Tokyo", "cb": "BoJ"},
}
TL_BASIS = 365  # TLREF: ACT/365


def find_all(prefix: str) -> list[Path]:
    hits = sorted(p for p in RAW.iterdir()
                  if p.name.lower().startswith(prefix) and p.suffix.lower() in (".xlsx", ".xls", ".csv"))
    if not hits:
        sys.exit(f"data/raw içinde '{prefix}' ile başlayan dosya bulunamadı.")
    return hits


def pick_file(prefix: str) -> Path:
    """Aynı adla başlayan birden fazla dosya varsa (ör. sonia.xlsx ve sonia.csv) .xlsx dosyası kullanılır."""
    hits = find_all(prefix)
    order = {".xlsx": 0, ".xls": 1, ".csv": 2}
    chosen = sorted(hits, key=lambda p: (order.get(p.suffix.lower(), 9), p.name))[0]
    if len(hits) > 1:
        print(f"  uyarı: '{prefix}' için {len(hits)} dosya var ({', '.join(h.name for h in hits)}); {chosen.name} kullanıldı.")
    return chosen


def read_any(p: Path, **kw) -> pd.DataFrame:
    return pd.read_csv(p, **kw) if p.suffix.lower() == ".csv" else pd.read_excel(p, **kw)


def clean(s: pd.Series) -> pd.Series:
    """Tarih indeksli, sayısal, boşları atılmış, sıralı seri."""
    s = pd.to_numeric(s, errors="coerce").dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s.index >= START]


# ── Okuyucular ────────────────────────────────────────────────────────────
def read_evds(p: Path) -> pd.DataFrame:
    df = read_any(p)
    df = df[df.iloc[:, 0].astype(str).str.match(r"^\d{2}-\d{2}-\d{4}$")].copy()
    df.index = pd.to_datetime(df.iloc[:, 0], format="%d-%m-%Y")
    df.columns = [str(c).replace(".", "_") for c in df.columns]
    out = pd.DataFrame(index=df.index)
    out["TLREF"] = pd.to_numeric(df["TP_BISTTLREF_ORAN"], errors="coerce")
    for c in FUNDING:
        a = pd.to_numeric(df[f"TP_DK_{c}_A_YTL"], errors="coerce")
        s = pd.to_numeric(df[f"TP_DK_{c}_S_YTL"], errors="coerce")
        div = 100.0 if c == "JPY" else 1.0  # EVDS: 100 JPY = … TL
        out[f"{c}_A"], out[f"{c}_S"] = a / div, s / div
    return out[out.index >= START].sort_index()


def read_sofr(p: Path) -> pd.Series:
    df = read_any(p)
    df = df[df["Rate Type"].astype(str).str.upper() == "SOFR"] if "Rate Type" in df else df
    idx = pd.to_datetime(df["Effective Date"], format="%m/%d/%Y", errors="coerce")
    return clean(pd.Series(df["Rate (%)"].values, index=idx).loc[lambda s: s.index.notna()])


def read_estr(p: Path) -> pd.Series:
    df = read_any(p)
    idx = pd.to_datetime(df.iloc[:, 0], errors="coerce")
    return clean(pd.Series(df.iloc[:, -1].values, index=idx).loc[lambda s: s.index.notna()])


def read_sonia(p: Path) -> pd.Series:
    df = read_any(p, header=None)
    vals, dates = [], []
    for d, v in zip(df.iloc[:, 0], df.iloc[:, 1]):
        t = pd.to_datetime(str(d), format="%d %b %y", errors="coerce")
        if pd.isna(t):
            t = pd.to_datetime(d, errors="coerce") if not isinstance(d, str) else pd.NaT
        if pd.notna(t):
            dates.append(t); vals.append(v)
    return clean(pd.Series(vals, index=pd.DatetimeIndex(dates)))


def read_tona(p: Path) -> pd.Series:
    df = pd.read_csv(p, header=None, names=["d", "v"], dtype=str)
    df = df[df["d"].str.match(r"^\d{4}/\d{2}/\d{2}$", na=False)]
    idx = pd.to_datetime(df["d"], format="%Y/%m/%d")
    return clean(pd.Series(df["v"].replace({"NA": None, "": None}).values, index=idx))


# ── Birikim endeksi ───────────────────────────────────────────────────────
def accrual_index(fix: pd.Series, basis: int, at: pd.DatetimeIndex) -> pd.Series:
    """Gecelik faiz birikim endeksi (SOFR Index mantığı).

    Seriler yayım tarihine göre değil, oranın ait olduğu işlem tarihine göre dizilidir
    (SOFR, €STR, SONIA ve TONA ertesi iş günü yayımlanır). i işlem tarihine ait r_i,
    bir sonraki işlem tarihine kadar geçerlidir:
        I(d_{i+1}) = I(d_i) · (1 + r_i/100 · (d_{i+1} − d_i) / basis)
    İşlem tarihleri arasındaki bir D tarihi için:
        I(D) = I(d_i) · (1 + r_i/100 · (D − d_i) / basis),  d_i = D'den önceki ya da D'ye eşit son işlem tarihi.
    Endeks ilk işlem tarihinde 1'dir; daha önceki tarihler için boş döner.
    """
    d = fix.index
    lvl = [1.0]
    for i in range(1, len(fix)):
        days = (d[i] - d[i - 1]).days
        lvl.append(lvl[-1] * (1 + fix.iloc[i - 1] / 100 * days / basis))
    lvl = pd.Series(lvl, index=d)
    out = []
    for D in at:
        k = d.searchsorted(D, side="right") - 1
        if k < 0:
            out.append(None)
            continue
        out.append(lvl.iloc[k] * (1 + fix.iloc[k] / 100 * (D - d[k]).days / basis))
    return pd.Series(out, index=at, dtype="float64")


def last_fix(fix: pd.Series, at: pd.DatetimeIndex) -> pd.Series:
    """D tarihinde geçerli oran (D veya öncesindeki son işlem tarihine ait)."""
    return fix.reindex(fix.index.union(at)).ffill().reindex(at).where(at >= fix.index[0])


def main() -> None:
    readers = {"evds": read_evds, "sofr": read_sofr, "estr": read_estr, "sonia": read_sonia, "tona": read_tona}
    files = {k: pick_file(k) for k in readers}
    loaded = {k: reader(files[k]) for k, reader in readers.items()}
    ev = loaded["evds"]
    fixes = {
        "TLREF": clean(ev["TLREF"]),
        "USD": loaded["sofr"],
        "EUR": loaded["estr"],
        "GBP": loaded["sonia"],
        "JPY": loaded["tona"],
    }
    # Gelecek tarihli satırlar (ör. TONA dosyasındaki yıl sonuna kadar boş satırlar) zaten atıldı.
    today = pd.Timestamp(date.today())
    for k in fixes:
        fixes[k] = fixes[k][fixes[k].index <= today]

    # Kur zamanlaması: EVDS'de t tarihli TCMB gösterge kuru, bir önceki iş günü saat 15:30'da
    # belirlenip t gününde geçerli olan kurdur (ör. 22.12.2021 tarihli kur, KKM açıklamasından
    # sonraki 21.12.2021 piyasasını yansıtır). Kurlar belirlendikleri güne kaydırılır ki
    # aynı günün TLREF'i ve fonlama faiziyle eşleşsin.
    fx_cols = [f"{c}_{s}" for c in FUNDING for s in "AS"]
    fx_rows = ev[ev[fx_cols].notna().all(axis=1)]
    fx = fx_rows[fx_cols].iloc[1:].copy()
    fx.index = fx_rows.index[:-1]          # değerin belirlendiği gün = önceki kur satırının tarihi
    ev = ev[["TLREF"]].join(fx, how="outer")
    days = fx.index[fx.index >= fixes["TLREF"].index[0]]

    tl_idx = accrual_index(fixes["TLREF"], TL_BASIS, days)
    tl_used = last_fix(fixes["TLREF"], days)
    tl_pub = fixes["TLREF"].reindex(days)

    js = {
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%d"),
            "files": {k: v.name for k, v in files.items()},
            "last": {"TLREF": fixes["TLREF"].index[-1].strftime("%Y-%m-%d"),
                     "FX": days[-1].strftime("%Y-%m-%d"),
                     **{FUNDING[c]["rate"]: fixes[c].index[-1].strftime("%Y-%m-%d") for c in FUNDING}},
            "tlBasis": TL_BASIS,
        },
        "dates": [d.strftime("%Y-%m-%d") for d in days],
        "tl": {
            "pub": [None if pd.isna(x) else round(float(x), 4) for x in tl_pub],
            "used": [None if pd.isna(x) else round(float(x), 4) for x in tl_used],
            "idx": [round(float(x), 12) for x in tl_idx],
        },
        "cur": {},
    }
    csv = pd.DataFrame(index=days)
    csv.index.name = "date"
    csv["TLREF_fixing"] = tl_pub
    csv["TLREF_effective"] = tl_used
    csv["TLREF_index"] = tl_idx

    for c, f in FUNDING.items():
        fx = fixes[c]
        idx = accrual_index(fx, f["basis"], days)
        used = last_fix(fx, days)
        pub = fx.reindex(days)
        # Başlangıç: hem TLREF hem fonlama faizinin bulunduğu ilk işlem tarihi
        start = days[days >= max(fx.index[0], fixes["TLREF"].index[0])][0]
        js["cur"][c] = {
            **f,
            "start": start.strftime("%Y-%m-%d"),
            "buy": [round(float(x), 6) for x in ev.loc[days, f"{c}_A"]],
            "sell": [round(float(x), 6) for x in ev.loc[days, f"{c}_S"]],
            "pub": [None if pd.isna(x) else round(float(x), 4) for x in pub],
            "used": [None if pd.isna(x) else round(float(x), 4) for x in used],
            "idx": [None if pd.isna(x) else round(float(x), 12) for x in idx],
        }
        r = f["rate"].replace("€", "E")
        csv[f"{c}TRY_buy"] = ev.loc[days, f"{c}_A"]
        csv[f"{c}TRY_sell"] = ev.loc[days, f"{c}_S"]
        csv[f"{r}_fixing"] = pub
        csv[f"{r}_effective"] = used
        csv[f"{r}_index"] = idx

    OUT_JSON.write_text(json.dumps(js, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    csv.to_csv(OUT_CSV, float_format="%.10g")
    print(f"{len(days)} değerleme günü: {days[0].date()} → {days[-1].date()}")
    for k, s in fixes.items():
        print(f"  {k:6s} {len(s):5d} işlem günü, {s.index[0].date()} → {s.index[-1].date()}, son {s.iloc[-1]}")
    for c in FUNDING:
        print(f"  {c} başlangıç: {js['cur'][c]['start']}")


if __name__ == "__main__":
    main()
