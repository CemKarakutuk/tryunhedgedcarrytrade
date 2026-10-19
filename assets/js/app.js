/* TLREF carry trade — hesaplama ve arayüz
 * Veri: data/dataset.json (scripts/update_data.py üretir), data/events.json (elle düzenlenir)
 * Grafik: her açılış günü için girilen gün sayısı sonunda kapatılan pozisyonun getirisi.
 * Hesaplama: seçilen açılış tarihi ve gün sayısı için pozisyonun günlük dökümü.
 */
(() => {
  "use strict";

  // ── Dil (TR/EN) ───────────────────────────────────────────────────────
  const params = new URLSearchParams(location.search);
  const safeGet = (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } };
  const safeSet = (k, v) => { try { localStorage.setItem(k, v); } catch (_) { /* gizli mod */ } };
  let LANG = (params.get("lang") || safeGet("tlref-lang") || "tr") === "en" ? "en" : "tr";
  const isEN = () => LANG === "en";
  const L = (tr, en) => (isEN() ? en : tr);

  // ── Sabitler ──────────────────────────────────────────────────────────
  const CUR = {
    USD: { tr: "ABD doları", en: "US dollar", from: "New York'tan", city: "New York" },
    EUR: { tr: "euro", en: "euro", from: "Frankfurt'tan", city: "Frankfurt" },
    GBP: { tr: "İngiliz sterlini", en: "British pound", from: "Londra'dan", city: "London" },
    JPY: { tr: "Japon yeni", en: "Japanese yen", from: "Tokyo'dan", city: "Tokyo" },
  };
  const MONTHS_TR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"];
  const MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const MAIN = 2; // ana getiri çizgisinin iz numarası

  // ── Yardımcılar ───────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const nf = (x, d = 0) => (x == null || !isFinite(x)) ? "–"
    : x.toLocaleString(isEN() ? "en-US" : "tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (x, d = 1) => (x == null || !isFinite(x)) ? "–"
    : isEN() ? (x < 0 ? "−" : "") + nf(Math.abs(x * 100), d) + "%" : (x < 0 ? "−%" : "%") + nf(Math.abs(x * 100), d);
  const rate = (v, d = 2) => (v == null || !isFinite(v)) ? "–" : pct(v / 100, d);
  const fdate = (s) => isEN()
    ? +s.slice(8, 10) + " " + MONTHS_EN[+s.slice(5, 7) - 1] + " " + s.slice(0, 4)
    : s.slice(8, 10) + "." + s.slice(5, 7) + "." + s.slice(0, 4);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const cls = (x) => (x == null ? "" : x >= 0 ? "pos" : "neg");
  const dayDiff = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 864e5);
  const addDays = (s, n) => new Date(Date.parse(s) + n * 864e5).toISOString().slice(0, 10);
  const annualize = (r, n) => (r == null || n <= 0 || 1 + r <= 0) ? null : Math.pow(1 + r, 365 / n) - 1;
  const esc = (s) => String(s).replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
  const fxDp = (code) => (code === "JPY" ? 6 : 4);
  const intIn = (id, def, max) => Math.max(1, Math.min(max, Math.round(parseFloat($(id).value) || def)));

  // ── Durum ─────────────────────────────────────────────────────────────
  const S = { data: null, events: [], N: 0, dates: [], rows: [], calc: null };
  const cfg = () => ({
    cur: document.querySelector("#curSeg [aria-checked=true]").dataset.cur,
    days: intIn("days", 365, 2500),
    spread: $("spread").checked,
    showFx: $("showFx").checked,
    margin: (S.marginBps = Math.max(0, parseInt($("margin").value, 10) || 0)),
  });

  function prepare(data) {
    S.data = data;
    S.dates = data.dates;
    S.N = data.dates.length;
    for (const [code, c] of Object.entries(data.cur)) {
      c.code = code;
      c.i0 = S.dates.indexOf(c.start);
      c.mid = c.buy.map((b, i) => (b + c.sell[i]) / 2);
    }
  }
  const idxOnOrAfter = (d) => { const k = S.dates.findIndex((x) => x >= d); return k < 0 ? S.N - 1 : k; };
  // Aynı birikimi veren sabit gecelik oran (yıllık %)
  const avgRate = (idx, a, b, basis) => {
    const n = dayDiff(S.dates[a], S.dates[b]);
    return n > 0 ? (Math.pow(idx[b] / idx[a], 1 / n) - 1) * basis * 100 : null;
  };
  // Açılıştan en az n gün sonraki ilk değerleme günü (veri yetmezse null)
  const exitIndex = (i, n, from = i + 1) => {
    const target = addDays(S.dates[i], n);
    let j = Math.max(from, i + 1);
    while (j < S.N && S.dates[j] < target) j++;
    return j < S.N ? j : null;
  };

  /* Borçlanma marjı: fonlama faizine sabit marj eklenmiş birikim endeksi.
   * Her değerleme aralığında borç faktörü (I_F(t)/I_F(t−1)) + marj × gün / B olur; yani marj her
   * gecelik faize doğrudan eklenir. Sıfır alt sınırı uygulanmaz: negatif faiz + marj negatif kalabilir. */
  function fIdx(c) {
    const m = S.marginBps || 0;
    if (!m) return c.idx;
    const key = "_idx" + m;
    if (c[key]) return c[key];
    const out = c.idx.slice();
    for (let t = c.i0 + 1; t < S.N; t++) {
      const d = dayDiff(S.dates[t - 1], S.dates[t]);
      out[t] = out[t - 1] * (c.idx[t] / c.idx[t - 1] + (m / 1e4) * d / c.basis);
    }
    return (c[key] = out);
  }
  // Gösterilen fonlama faizi (yıllık %, marj dahil) ve etiketi
  const fRateAt = (c, i) => (c.used[i] == null ? null : c.used[i] + (S.marginBps || 0) / 100);
  const rateLabel = (c) => (S.marginBps ? `${c.rate} + ${S.marginBps} bps` : c.rate);

  /* i günü açılıp j günü kapanan pozisyon (1 birim döviz borç başına) */
  function position(c, i, j, spread) {
    const tl = S.data.tl;
    const ent = spread ? c.buy[i] : c.mid[i];
    const ext = spread ? c.sell[j] : c.mid[j];
    const fi = fIdx(c), gtl = tl.idx[j] / tl.idx[i], gf = fi[j] / fi[i];
    return { ent, ext, gtl, gf, r: ent * gtl / ext - gf, fx: ext / ent - 1 };
  }

  /* Ana grafik: her açılış günü için girilen gün sayısı sonundaki getiri */
  function compute(k) {
    const c = S.data.cur[k.cur], tl = S.data.tl, out = [];
    let j = c.i0 + 1;
    for (let i = c.i0; i < S.N - 1; i++) {
      j = exitIndex(i, k.days, j);
      if (j == null) break;
      const p = position(c, i, j, k.spread);
      const n = dayDiff(S.dates[i], S.dates[j]);
      out.push({ i, j, n, date: S.dates[i], exit: S.dates[j], ...p, ann: annualize(p.r, n),
        tlRate: tl.used[i], fRate: fRateAt(c, i),
        tlAvg: avgRate(tl.idx, i, j, S.data.meta.tlBasis), fAvg: avgRate(fIdx(c), i, j, c.basis) });
    }
    return out;
  }

  // ── Plotly ────────────────────────────────────────────────────────────
  function registerLocale() {
    Plotly.register({ moduleType: "locale", name: "tr", dictionary: {}, format: {
      days: ["Pazar", "Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi"],
      shortDays: ["Paz", "Pzt", "Sal", "Çar", "Per", "Cum", "Cmt"],
      months: ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"],
      shortMonths: MONTHS_TR, date: "%d.%m.%Y", decimal: ",", thousands: ".",
    } });
  }
  const plotConfig = () => ({ responsive: true, displaylogo: false, locale: isEN() ? "en" : "tr",
    modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d", "zoomIn2d", "zoomOut2d"] });

  function renderChart() {
    const k = cfg(), c = S.data.cur[k.cur], cur = CUR[k.cur];
    const s = compute(k);
    S.rows = s;
    $("chartTitle").textContent = L(`Carry trade getirisi: ${nf(k.days)} günlük vade`, `Carry trade return: ${nf(k.days)}-day holding period`);
    const gd = $("mainChart");
    if (!s.length) {
      Plotly.purge(gd); gd.__bound = false;
      gd.innerHTML = `<p class="chart-msg">${L("Bu gün sayısı için veri dönemi yeterince uzun değil. Daha kısa bir vade girin.",
        "The data period is not long enough for this number of days. Enter a shorter holding period.")}</p>`;
      $("stats").innerHTML = ""; $("mainCaption").textContent = "";
      return;
    }
    if (gd.querySelector(".chart-msg")) gd.innerHTML = "";

    const x = s.map((p) => p.date), y = s.map((p) => p.r * 100);
    const custom = s.map((p) => [pct(p.r, 2), pct(p.ann, 2), fdate(p.exit), nf(p.n), rate(p.tlAvg), rate(p.fAvg, 2), pct(p.fx, 2), pct(p.gtl - 1, 2)]);
    const gain = css("--gain"), loss = css("--loss");
    const hov = `<b>${L("Carry getirisi", "Carry return")}: %{customdata[0]}</b><br>` +
      `${L("Yıllıklandırılmış", "Annualised")}: %{customdata[1]}<br>` +
      `${L("Kapanış", "Close")}: %{customdata[2]} (%{customdata[3]} ${L("gün", "days")})<br>` +
      `${L("TL getirisi", "TL return")}: %{customdata[7]} · ${k.cur}/TRY: %{customdata[6]}<br>` +
      `TLREF ${L("ort.", "avg.")}: %{customdata[4]} · ${rateLabel(c)} ${L("ort.", "avg.")}: %{customdata[5]}<extra></extra>`;
    const traces = [
      { x, y: y.map((v) => Math.max(v, 0)), type: "scatter", mode: "lines", line: { width: 0 },
        fill: "tozeroy", fillcolor: gain + "59", hoverinfo: "skip", showlegend: false },
      { x, y: y.map((v) => Math.min(v, 0)), type: "scatter", mode: "lines", line: { width: 0 },
        fill: "tozeroy", fillcolor: loss + "59", hoverinfo: "skip", showlegend: false },
      { x, y, name: L(`Carry trade getirisi (${k.cur} borçlanarak, ${nf(k.days)} gün)`, `Carry trade return (${k.cur}-funded, ${nf(k.days)} days)`),
        type: "scatter", mode: "lines", line: { color: css("--accent"), width: 2 }, customdata: custom, hovertemplate: hov },
      { x, y: s.map((p) => p.fx * 100), name: L(`Kur değişimi (${k.cur}/TRY, ${nf(k.days)} gün)`, `FX change (${k.cur}/TRY, ${nf(k.days)} days)`),
        type: "scatter", mode: "lines", line: { color: css("--fx"), width: 1.4, dash: "dot" }, visible: k.showFx, hoverinfo: "skip" },
    ];
    const ink = css("--ink"), rule = css("--rule"), narrow = window.innerWidth < 700;
    const layout = {
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: css("--sans") || "IBM Plex Sans, sans-serif", color: ink, size: 12.5 },
      margin: { l: 58, r: 18, t: 40, b: 44 }, hovermode: "x unified", dragmode: "zoom",
      hoverlabel: { bgcolor: "#ffffff", bordercolor: rule, font: { color: ink, size: 12.5 } },
      legend: { orientation: "h", x: 0, y: 1.07, font: { color: ink, size: 12.5 } },
      xaxis: { type: "date", showgrid: true, gridcolor: css("--grid"), griddash: "dot", linecolor: css("--ink-2"), tickcolor: rule,
        hoverformat: L("%d.%m.%Y açılış", "Open: %d %b %Y"), dtick: narrow ? "M24" : "M12", tickformat: "%Y", ticklabelmode: "period" },
      // Yakınlaştırma yalnızca tarih ekseninde; y ekseni görünen aralığa göre rescaleY() ile ölçeklenir
      yaxis: { fixedrange: true, showgrid: true, gridcolor: css("--grid"), griddash: "dot", zerolinecolor: ink, zerolinewidth: 1.2,
        range: yRange(x[0], x[x.length - 1], k.showFx), autorange: false,
        title: { text: L(`${nf(k.days)} günlük getiri (%)`, `${nf(k.days)}-day return (%)`), font: { color: css("--muted"), size: 12 } } },
      shapes: [], annotations: [],
    };
    if ($("showEvents").checked) {
      const evs = S.events.filter((e) => (!e.cur || e.cur.includes(k.cur)) && e.date >= x[0] && e.date <= x[x.length - 1])
        .sort((a, b) => a.date.localeCompare(b.date));
      let prev = null, level = 0;
      for (const e of evs) {
        level = prev && dayDiff(prev, e.date) < 240 ? level + 1 : 0;
        prev = e.date;
        layout.shapes.push({ type: "line", xref: "x", yref: "paper", x0: e.date, x1: e.date, y0: 0, y1: 1,
          line: { color: css("--event"), width: 1, dash: "dot" } });
        if (narrow) continue;
        layout.annotations.push({ x: e.date, xref: "x", y: 1 - level * 0.2, yref: "paper", text: esc(isEN() ? (e.label_en || e.label) : e.label),
          showarrow: false, textangle: -90, xanchor: "right", yanchor: "top", xshift: -2, font: { size: 11, color: css("--ink-2") } });
      }
    }
    // Hesaplamada seçili açılış günü
    if (S.calc && S.calc.date >= x[0] && S.calc.date <= x[x.length - 1]) {
      layout.shapes.push({ type: "line", xref: "x", yref: "paper", x0: S.calc.date, x1: S.calc.date, y0: 0, y1: 1,
        line: { color: css("--accent"), width: 1.5 } });
    }
    // Yakınlaştırma korunur
    const prevRange = gd.layout && gd.layout.xaxis && gd.layout.xaxis.autorange !== true && gd.layout.xaxis.range;
    if (prevRange && S.keepZoom) layout.xaxis.range = prevRange.slice();
    Plotly.react(gd, traces, layout, plotConfig());
    if (!gd.__bound) {
      gd.on("plotly_relayout", onRelayout);
      gd.on("plotly_click", onClick);
      gd.__bound = true;
    }
    if (layout.xaxis.range) rescaleY();

    const sp = k.spread ? L("kur makası dahil (giriş alış, çıkış satış kuru)", "FX spread included (entry at buying, exit at selling rate)")
      : L("kur makası hariç (orta kur)", "FX spread excluded (mid rate)");
    $("mainCaption").textContent = isEN()
      ? `Figure 1. Gross return of borrowing ${cur.en} overnight at ${rateLabel(c)} in ${cur.city} and earning TLREF, held for ${nf(k.days)} days (in ${k.cur}, %, not annualised); ${sp}. Each point shows the position opened on that day.`
      : `Şekil 1. ${cur.from} ${rateLabel(c)} ile gecelik ${cur.tr} borçlanıp TLREF ile değerlendirilen pozisyonun ${nf(k.days)} günlük brüt getirisi (${k.cur} bazında, %, yıllıklandırılmamış); ${sp}. Her nokta, o gün açılan pozisyonu gösterir.`;
    updateStats();
  }

  /* Görünen tarih aralığındaki en düşük ve en yüksek değere göre y ekseni sınırları */
  function yRange(lo, hi, withFx) {
    let mn = Infinity, mx = -Infinity;
    for (const p of S.rows) {
      if (p.date < lo || p.date > hi) continue;
      const vals = withFx ? [p.r * 100, p.fx * 100] : [p.r * 100];
      for (const v of vals) { if (v < mn) mn = v; if (v > mx) mx = v; }
    }
    if (!isFinite(mn)) return [-1, 1];
    const pad = Math.max((mx - mn) * 0.08, 0.05);
    return [mn - pad, mx + pad];
  }
  function visible() {
    const gd = $("mainChart"), xa = gd.layout && gd.layout.xaxis, s = S.rows;
    const full = !xa || !xa.range || xa.autorange === true;
    return [full ? s[0].date : String(xa.range[0]).slice(0, 10), full ? s[s.length - 1].date : String(xa.range[1]).slice(0, 10)];
  }
  function rescaleY() {
    const gd = $("mainChart");
    if (!S.rows.length || !gd.layout) return;
    const [lo, hi] = visible();
    const days = dayDiff(lo, hi), narrow = window.innerWidth < 700;
    const xt = days > (narrow ? 1300 : 900) ? { "xaxis.dtick": narrow ? "M24" : "M12", "xaxis.tickformat": "%Y", "xaxis.ticklabelmode": "period" }
      : days > (narrow ? 500 : 300) ? { "xaxis.dtick": "M3", "xaxis.tickformat": "%b %Y", "xaxis.ticklabelmode": "instant" }
      : { "xaxis.dtick": "M1", "xaxis.tickformat": "%b %Y", "xaxis.ticklabelmode": "instant" };
    Plotly.relayout(gd, Object.assign({ "yaxis.range": yRange(lo, hi, $("showFx").checked), "yaxis.autorange": false }, xt));
  }
  function onRelayout(ev) {
    // Kendi y ekseni ve etiket güncellemelerimiz de bu olayı tetikler; yalnızca tarih aralığı değişince ölçekle
    const xKeys = Object.keys(ev || {}).some((key) => key.startsWith("xaxis.range") || key === "xaxis.autorange");
    if (!xKeys) return;
    rescaleY();
    updateStats();
  }

  // Tek tık hesaplamayı o günden başlatır; çift tık yalnızca yakınlaştırmadan çıkar
  let clickTimer = null, ignoreUntil = 0;
  function onClick(e) {
    if (Date.now() < ignoreUntil) return;
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; ignoreUntil = Date.now() + 450; return; }
    const p = e.points.find((q) => q.curveNumber === MAIN) || e.points[0];
    if (!p) return;
    clickTimer = setTimeout(() => {
      clickTimer = null;
      const row = S.rows[p.pointIndex];
      if (!row) return;
      $("cStart").value = row.date;
      $("cDays").value = cfg().days;
      S.keepZoom = true;
      renderCalc();
      renderChart();
      S.keepZoom = false;
    }, 330);
  }

  function describe(vals) {
    const v = vals.filter((x) => x != null && isFinite(x));
    if (!v.length) return null;
    const s = [...v].sort((a, b) => a - b), m = s.length >> 1;
    return { n: v.length, pos: v.filter((x) => x > 0).length / v.length, med: s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2,
      mean: v.reduce((a, b) => a + b, 0) / v.length, min: s[0], max: s[s.length - 1] };
  }
  function updateStats() {
    if (!S.rows.length) return;
    const [lo, hi] = visible();
    const inR = S.rows.filter((p) => p.date >= lo && p.date <= hi);
    if (!inR.length) return;
    const k = cfg();
    const row = (label, st) => !st ? "" :
      `<div class="row"><span class="k">${label}</span><span>${L("Pozitif", "Positive")}: <b>${pct(st.pos, 0)}</b></span>` +
      `<span>${L("Medyan", "Median")}: <b>${pct(st.med)}</b></span><span>${L("Ortalama", "Mean")}: <b>${pct(st.mean)}</b></span>` +
      `<span>${L("En düşük", "Min")}: <b>${pct(st.min)}</b></span><span>${L("En yüksek", "Max")}: <b>${pct(st.max)}</b></span></div>`;
    $("stats").innerHTML =
      `<div class="row"><span>${L("Görünen aralık", "Visible range")}: <b>${fdate(inR[0].date)} – ${fdate(inR[inR.length - 1].date)}</b></span>` +
      `<span>${L("Açılış günü sayısı", "Opening days")}: <b>${nf(inR.length)}</b></span></div>` +
      row(L(`Carry getirisi (${k.cur}, ${nf(k.days)} gün)`, `Carry return (${k.cur}, ${nf(k.days)} days)`), describe(inR.map((p) => p.r))) +
      row(L("Yıllıklandırılmış", "Annualised"), describe(inR.map((p) => p.ann)));
  }

  // ── Hesaplama ─────────────────────────────────────────────────────────
  function renderCalc() {
    const k = cfg(), c = S.data.cur[k.cur], tl = S.data.tl, code = k.cur, dp = fxDp(code);
    const amt = Math.max(1, parseFloat($("cAmount").value) || 1);
    $("amtLabel").textContent = L(`Tutar (${code})`, `Amount (${code})`);
    const n = intIn("cDays", 365, 2900);
    const min = S.dates[c.i0], max = S.dates[S.N - 2];
    $("cStart").min = min; $("cStart").max = max;
    let d = $("cStart").value || min;
    if (d < min) d = min;
    if (d > max) d = max;
    const i = idxOnOrAfter(d);
    $("cStart").value = S.dates[i];
    let j = exitIndex(i, n);
    const open = j == null;
    if (open) j = S.N - 1;
    S.calc = { date: S.dates[i] };
    const days = dayDiff(S.dates[i], S.dates[j]);
    const p = position(c, i, j, k.spread);
    const tl0 = amt * p.ent, tl1 = tl0 * p.gtl, back = tl1 / p.ext, debt = amt * p.gf, pnl = back - debt;

    const card = (t, v, sub, klass = "", primary = false) =>
      `<div class="card${primary ? " primary" : ""}"><h4>${t}</h4><div class="v ${klass}">${v}</div><div class="s">${sub}</div></div>`;
    $("cards").innerHTML =
      card(L(`Carry getirisi (${code})`, `Carry return (${code})`), pct(p.r, 2),
        L(`Yıllıklandırılmış ${pct(annualize(p.r, days), 2)}`, `Annualised ${pct(annualize(p.r, days), 2)}`), cls(p.r), true) +
      card(L("Brüt K/Z", "Gross P&L"), `${pnl >= 0 ? "+" : "−"}${nf(Math.abs(pnl))} ${code}`,
        L(`Döviz karşılığı ${nf(back)} ${code} − borç bakiyesi ${nf(debt)} ${code}`, `FX equivalent ${nf(back)} ${code} − outstanding debt ${nf(debt)} ${code}`), cls(pnl)) +
      card(L("TL bakiye", "TL balance"), `${nf(tl1)} TL`,
        L(`${nf(tl0)} TL → TL getirisi ${pct(p.gtl - 1, 2)}`, `${nf(tl0)} TL → TL return ${pct(p.gtl - 1, 2)}`)) +
      card(L(`Borç bakiyesi (${rateLabel(c)})`, `Outstanding debt (${rateLabel(c)})`), `${nf(debt)} ${code}`,
        L(`Fonlama maliyeti ${pct(p.gf - 1, 3)}`, `Funding cost ${pct(p.gf - 1, 3)}`)) +
      card(L(`Kur (${code}/TRY)`, `Exchange rate (${code}/TRY)`), pct(p.fx, 2), `${nf(p.ent, dp)} → ${nf(p.ext, dp)}`) +
      card(L("Dönem", "Period"), `${nf(days)} ${L("gün", "days")}`, `${fdate(S.dates[i])} → ${fdate(S.dates[j])}`);
    $("calcNote").textContent = open
      ? L(`Girilen ${nf(n)} günlük vade veri döneminin sonunu aşıyor; hesap son veri günü olan ${fdate(S.dates[j])} tarihine kadar yapıldı.`,
          `The ${nf(n)}-day holding period runs past the end of the data; the calculation runs to the last data day, ${fdate(S.dates[j])}.`)
      : (days !== n ? L(`Vade sonu tatil ya da hafta sonuna denk geldiğinden pozisyon ilk değerleme gününde, ${nf(days)}. günde kapatıldı.`,
          `The holding period ends on a weekend or holiday, so the position closes on the next valuation day, after ${nf(days)} days.`) : "");

    // Günlük döküm
    const rows = [];
    for (let t = i; t <= j; t++) {
      const q = position(c, i, t, k.spread);
      rows.push({ date: S.dates[t], day: dayDiff(S.dates[i], S.dates[t]), tlRate: tl.used[t], fRate: fRateAt(c, t),
        buy: c.buy[t], sell: c.sell[t], tlBal: tl0 * q.gtl, debt: amt * q.gf,
        value: t === i ? (k.spread ? amt * (c.buy[i] / c.sell[i]) : amt) : tl0 * q.gtl / q.ext, r: t === i ? null : q.r });
      rows[rows.length - 1].pnl = rows[rows.length - 1].value - rows[rows.length - 1].debt;
    }
    S.calcRows = rows;
    const head = `<thead><tr><th>${L("Tarih", "Date")}</th><th>${L("Gün", "Day")}</th><th>TLREF</th><th>${rateLabel(c)}</th>` +
      `<th>${L("Alış", "Buy")}</th><th>${L("Satış", "Sell")}</th><th>${L("TL bakiye", "TL balance")}</th>` +
      `<th>${L(`Döviz karşılığı (${code})`, `FX equivalent (${code})`)}</th><th>${L(`Borç bakiyesi (${code})`, `Outstanding debt (${code})`)}</th>` +
      `<th>${L(`Brüt K/Z (${code})`, `Gross P&L (${code})`)}</th><th>${L("Kümülatif getiri", "Cumulative return")}</th></tr></thead>`;
    const body = rows.map((r) => `<tr><td>${fdate(r.date)}</td><td>${nf(r.day)}</td><td>${rate(r.tlRate)}</td><td>${rate(r.fRate, 3)}</td>` +
      `<td>${nf(r.buy, dp)}</td><td>${nf(r.sell, dp)}</td><td>${nf(r.tlBal)}</td><td>${nf(r.value)}</td><td>${nf(r.debt)}</td>` +
      `<td class="${cls(r.pnl)}">${nf(r.pnl)}</td><td class="${cls(r.r)}">${r.r == null ? "–" : pct(r.r, 2)}</td></tr>`).join("");
    $("rollTable").innerHTML = head + "<tbody>" + body + "</tbody>";
  }

  // ── İndirme ───────────────────────────────────────────────────────────
  function download(name, head, rows) {
    const sep = isEN() ? "," : ";";
    const blob = new Blob(["﻿" + [head.join(sep)].concat(rows.map((r) => r.join(sep))).join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  }
  const f = (v, d) => (v == null || !isFinite(v)) ? "" : isEN() ? v.toFixed(d) : v.toFixed(d).replace(".", ",");
  function dlMain() {
    const k = cfg(), c = S.data.cur[k.cur];
    const head = isEN()
      ? ["Open", "Close", "Days", "TLREF (%)", `${rateLabel(c)} (%)`, `${k.cur}TRY entry`, `${k.cur}TRY exit`, "TL return (%)", "Funding cost (%)", "FX change (%)", "Carry return (%)", "Annualised (%)"]
      : ["Açılış", "Kapanış", "Gün", "TLREF (%)", `${rateLabel(c)} (%)`, `${k.cur}TRY giriş`, `${k.cur}TRY çıkış`, "TL getirisi (%)", "Fonlama maliyeti (%)", "Kur değişimi (%)", "Carry getirisi (%)", "Yıllıklandırılmış (%)"];
    const rows = S.rows.map((p) => [p.date, p.exit, p.n, f(p.tlRate, 4), f(p.fRate, 4), f(p.ent, 6), f(p.ext, 6),
      f((p.gtl - 1) * 100, 4), f((p.gf - 1) * 100, 4), f(p.fx * 100, 4), f(p.r * 100, 4), f(p.ann == null ? null : p.ann * 100, 4)]);
    download(`tlref_carry_${k.cur}_${k.days}${L("gun", "d")}${k.spread ? "" : L("_makassiz", "_nospread")}.csv`, head, rows);
  }
  function dlCalc() {
    const k = cfg(), c = S.data.cur[k.cur], rows = S.calcRows || [];
    const head = isEN()
      ? ["Date", "Day", "TLREF (%)", `${rateLabel(c)} (%)`, `${k.cur}TRY buy`, `${k.cur}TRY sell`, "TL balance", `FX equivalent (${k.cur})`, `Outstanding debt (${k.cur})`, `Gross P&L (${k.cur})`, "Cumulative return (%)"]
      : ["Tarih", "Gün", "TLREF (%)", `${rateLabel(c)} (%)`, `${k.cur}TRY alış`, `${k.cur}TRY satış`, "TL bakiye", `Döviz karşılığı (${k.cur})`, `Borç bakiyesi (${k.cur})`, `Brüt K/Z (${k.cur})`, "Kümülatif getiri (%)"];
    const out = rows.map((r) => [r.date, r.day, f(r.tlRate, 4), f(r.fRate, 4), f(r.buy, 6), f(r.sell, 6), f(r.tlBal, 2), f(r.value, 2), f(r.debt, 2), f(r.pnl, 2), f(r.r == null ? null : r.r * 100, 4)]);
    download(`tlref_carry_${k.cur}_${rows.length ? rows[0].date : ""}_${$("cDays").value}${L("gun", "d")}.csv`, head, out);
  }

  // ── Dil ve meta ───────────────────────────────────────────────────────
  function applyLanguage() {
    document.documentElement.lang = LANG;
    const b = $("langBtn");
    b.textContent = isEN() ? "Türkçe" : "English";
    b.setAttribute("aria-label", isEN() ? "Türkçeye geç" : "Switch to English");
    document.querySelector(".controls").setAttribute("aria-label", L("Analiz ayarları", "Analysis settings"));
    document.querySelectorAll("option[data-tr]").forEach((o) => { o.textContent = isEN() ? o.dataset.en : o.dataset.tr; });
  }
  function renderMeta() {
    const m = S.data.meta, d = S.dates;
    $("metaLine").textContent = L(
      `Veri: ${fdate(d[0])} – ${fdate(d[S.N - 1])} (günlük). Son gözlemler: TLREF ${fdate(m.last.TLREF)}, SOFR ${fdate(m.last.SOFR)}, €STR ${fdate(m.last["€STR"])}, SONIA ${fdate(m.last.SONIA)}, TONA ${fdate(m.last.TONA)}. Veri güncelleme: ${fdate(m.generated)}.`,
      `Data: ${fdate(d[0])} – ${fdate(d[S.N - 1])} (daily). Latest observations: TLREF ${fdate(m.last.TLREF)}, SOFR ${fdate(m.last.SOFR)}, €STR ${fdate(m.last["€STR"])}, SONIA ${fdate(m.last.SONIA)}, TONA ${fdate(m.last.TONA)}. Data updated: ${fdate(m.generated)}.`);
    $("metaFiles").textContent = L("Kaynak dosyalar (data/raw): ", "Source files (data/raw): ") + Object.values(m.files).join(", ") + ".";
  }
  function setLanguage(lang) {
    LANG = lang;
    safeSet("tlref-lang", lang);
    try {
      const u = new URL(location.href);
      if (lang === "en") u.searchParams.set("lang", "en"); else u.searchParams.delete("lang");
      history.replaceState(null, "", u);
    } catch (_) { /* gömülü görünüm */ }
    applyLanguage();
    if (S.data) { renderMeta(); renderCalc(); S.keepZoom = true; renderChart(); S.keepZoom = false; }
  }

  function renderAll() { renderCalc(); renderChart(); }

  function bind() {
    $("langBtn").addEventListener("click", () => setLanguage(isEN() ? "tr" : "en"));
    document.querySelectorAll("#curSeg button").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll("#curSeg button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      safeSet("tlref-cur", b.dataset.cur);
      renderAll();
    }));
    const debounce = (fn) => { let t = null; return () => { clearTimeout(t); t = setTimeout(fn, 450); }; };
    $("days").addEventListener("input", debounce(renderChart));
    $("days").addEventListener("change", () => { $("days").value = cfg().days; renderChart(); });
    $("spread").addEventListener("change", renderAll);
    $("margin").addEventListener("change", () => { cfg(); renderCalc(); S.keepZoom = true; renderChart(); S.keepZoom = false; });
    $("showEvents").addEventListener("change", () => { S.keepZoom = true; renderChart(); S.keepZoom = false; });
    $("showFx").addEventListener("change", () => {
      Plotly.restyle("mainChart", { visible: $("showFx").checked }, [3]);
      rescaleY();
    });
    const calcThenMark = () => { renderCalc(); S.keepZoom = true; renderChart(); S.keepZoom = false; };
    $("cStart").addEventListener("change", calcThenMark);
    $("cDays").addEventListener("input", debounce(renderCalc));
    $("cDays").addEventListener("change", () => { $("cDays").value = intIn("cDays", 365, 2900); renderCalc(); });
    $("cAmount").addEventListener("input", renderCalc);
    $("dlMain").addEventListener("click", dlMain);
    $("dlCalc").addEventListener("click", dlCalc);
  }

  async function init() {
    applyLanguage();
    const fail = (msg) => { $("mainChart").innerHTML = `<p class="chart-msg">${msg}</p>`; $("metaLine").textContent = ""; };
    if (typeof Plotly === "undefined") {
      fail(L("Grafik kütüphanesi (Plotly) yüklenemedi. İnternet bağlantısını veya içerik engelleyiciyi kontrol edin.",
        "The charting library (Plotly) could not be loaded. Check your internet connection or content blocker."));
      return;
    }
    let data = window.__TLREF_DATA__;
    if (!data) {
      try {
        const res = await fetch("data/dataset.json", { cache: "no-cache" });
        if (!res.ok) throw new Error(res.status);
        data = await res.json();
      } catch (_) {
        fail(location.protocol === "file:"
          ? L("Veri dosyası doğrudan dosyadan açılamıyor. Repo klasöründe <code>python -m http.server</code> çalıştırıp http://localhost:8000 adresini açın.",
              "The data file cannot be opened directly from disk. Run <code>python -m http.server</code> in the repo folder and open http://localhost:8000.")
          : L("Veri dosyası (data/dataset.json) yüklenemedi.", "The data file (data/dataset.json) could not be loaded."));
        return;
      }
    }
    S.events = window.__TLREF_EVENTS__ || [];
    if (!window.__TLREF_EVENTS__) {
      try { const r = await fetch("data/events.json", { cache: "no-cache" }); if (r.ok) S.events = await r.json(); } catch (_) { /* isteğe bağlı */ }
    }
    registerLocale();
    prepare(data);
    const saved = safeGet("tlref-cur");
    if (saved && data.cur[saved]) document.querySelectorAll("#curSeg button").forEach((x) => x.setAttribute("aria-checked", String(x.dataset.cur === saved)));
    // Varsayılan hesaplama: son tamamlanmış 365 günlük dönem
    $("cStart").value = addDays(S.dates[S.N - 1], -372);
    renderMeta();
    renderAll();
    bind();
  }

  init();
})();
