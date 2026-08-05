#!/usr/bin/env python3
"""
يبني لوحة متابعة HTML ثابتة تُنشر عبر GitHub Pages من مجلد docs/.

يقرأ `latest_run.json` (مخرجات build_report.py) ويضيفه إلى أرشيف
`docs/data/history.json`، ثم يولّد `docs/index.html` صفحةً **مكتفية بذاتها**
(كل البيانات مضمّنة داخلها) — فتفتح فوراً بلا أي طلبات شبكة إضافية (باستثناء
خط IBM Plex Arabic من Google Fonts)، وتعمل حتى لو حُفظت على الجهاز.

اللوحة تعرض فقط البيانات الأساسية: مؤشرات، مقارنة حقيقية بين تاريخين
(اختيار من/إلى)، مع أعمدة موسومة بالتواريخ الفعلية المقارنة (ليس قبل/بعد
غامضة)، وألوان: صعود السعر = أخضر، نزوله = أحمر، وجدول منفصل مرتّب لكل
كاتوجري يضم الأسعار والنسب ونسبة الخصم، بلا أي نص تعريفي عن المشروع.
"""
import html
import json
import os
import sys
from datetime import datetime, timezone

LATEST_RUN_PATH = "latest_run.json"
DOCS_DIR = "docs"
DATA_DIR = os.path.join(DOCS_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
OUTPUT_PATH = os.path.join(DOCS_DIR, "index.html")

# نحتفظ بآخر 60 تشغيلاً حتى لا تتضخم الصفحة
MAX_RUNS = 60


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError) as exc:
            log(f"تعذّرت قراءة {path}: {exc} — نبدأ من جديد.")
    return default


def day_key(iso_ts):
    """يرجع التاريخ (YYYY-MM-DD) من طابع زمني ISO."""
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


KINDS = ("price_changes", "offer_changes", "new_arrivals")


def merge_day(prev, cur):
    """يوحّد تشغيلين في نفس اليوم: التغييرات اتحاد الاثنين (بلا تكرار)،
    والإحصاءات والوقت من الأحدث."""
    cats = {}
    for run in (prev, cur):
        for c in run.get("categories", []):
            slot = cats.setdefault(c["name"], {k: {} for k in KINDS})
            for kind in KINDS:
                for row in c.get(kind, []):
                    # المفتاح: الرابط + نوع تغيّر العرض (منتج قد يظهر بأكثر من نوع)
                    slot[kind][(row.get("url"), row.get("offer_kind"))] = row

    merged_cats = [
        {"name": name, **{k: list(v[k].values()) for k in KINDS}}
        for name, v in cats.items()
    ]
    merged_cats = [c for c in merged_cats if sum(len(c[k]) for k in KINDS) > 0]
    merged_cats.sort(key=lambda c: (-sum(len(c[k]) for k in KINDS), c["name"]))

    out = dict(cur)
    out["categories"] = merged_cats
    out["totals"] = {
        "price_changes": sum(len(c["price_changes"]) for c in merged_cats),
        "offer_changes": sum(len(c["offer_changes"]) for c in merged_cats),
        "new_arrivals": sum(len(c["new_arrivals"]) for c in merged_cats),
    }
    return out


def main():
    latest = load_json(LATEST_RUN_PATH, None)
    if latest is None:
        log(f"⚠️ لا يوجد {LATEST_RUN_PATH} — لن تُبنى اللوحة.")
        return 1

    history = load_json(HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []

    latest["day"] = day_key(latest.get("generated_at", ""))

    # لو فيه تشغيل سابق لنفس اليوم (تشغيل يدوي إضافي مثلاً)، نـدمج بدل ما نستبدل:
    # التشغيل الثاني يقارن بخط أساس حدّثه الأول، فلن يرى تغييرات الصباح مرة أخرى.
    # تغييرات اليوم = اتحاد تشغيلات اليوم، وإلا ضاعت تغييرات حقيقية من اللوحة.
    same_day = next((r for r in history if r.get("day") == latest["day"]), None)
    if same_day:
        latest = merge_day(same_day, latest)
        log("دُمج تشغيل اليوم مع تشغيل سابق لنفس اليوم (اتحاد التغييرات).")
    history = [r for r in history if r.get("day") != latest["day"]]
    history.insert(0, latest)
    history.sort(key=lambda r: r.get("generated_at", ""), reverse=True)
    history = history[:MAX_RUNS]

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)

    page = render(history)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(page)

    log(f"بُنيت اللوحة: {OUTPUT_PATH} ({len(page)} حرف، {len(history)} تقرير بالأرشيف).")
    return 0


def render(history):
    data_json = json.dumps(history, ensure_ascii=False)
    updated = history[0].get("generated_at", "") if history else ""
    return TEMPLATE.replace("__DATA__", html.escape(data_json, quote=False)).replace(
        "__UPDATED__", html.escape(updated)
    )


TEMPLATE = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>مكعب — لوحة الأسعار</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#f7f7f8; --card:#fff; --ink:#15171c; --muted:#71767f; --faint:#9a9fa6; --line:#e8e9ec;
    --up:#b3261e; --up-bg:#fdeceb; --down:#0e7a4f; --down-bg:#e8f8f0;
    --brand:#15171c; --accent:#3d5afe; --accent-bg:#edf0ff;
    --radius:16px; --radius-sm:10px;
    --ease-out:cubic-bezier(.23,1,.32,1);
    --ease-in-out:cubic-bezier(.77,0,.175,1);
  }
  *{box-sizing:border-box}
  html{color-scheme:light}
  body{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:"IBM Plex Sans Arabic","Tahoma",system-ui,-apple-system,Arial,sans-serif;
    line-height:1.6; -webkit-font-smoothing:antialiased; font-variant-numeric:tabular-nums;
  }
  .wrap{max-width:1180px;margin:0 auto;padding:26px 20px 60px}

  header.top{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin-bottom:20px}
  .brand{display:flex;align-items:center;gap:10px}
  .brand .dot{width:9px;height:9px;border-radius:50%;background:var(--accent)}
  h1{font-size:19px;margin:0;font-weight:600;letter-spacing:-.01em}
  .stamp{color:var(--faint);font-size:12.5px}

  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;
       transition:border-color 160ms var(--ease-out), transform 160ms var(--ease-out);
       opacity:0; transform:translateY(6px); animation:rise 380ms var(--ease-out) forwards}
  .kpi .label{font-size:12px;color:var(--muted);margin-bottom:6px}
  .kpi .value{font-size:25px;font-weight:700;letter-spacing:-.02em}
  .kpi .value.up{color:var(--up)} .kpi .value.down{color:var(--down)}

  .rangebar{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
            padding:14px 16px;margin-bottom:14px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
  .rangebar .grp{display:flex;align-items:center;gap:8px}
  .rangebar label{font-size:12.5px;color:var(--muted);white-space:nowrap}
  select{
    font:inherit; font-size:13.5px; padding:8px 12px; border:1px solid var(--line); border-radius:var(--radius-sm);
    background:var(--card); color:var(--ink); cursor:pointer; transition:border-color 140ms var(--ease-out), box-shadow 140ms var(--ease-out);
    appearance:none; -webkit-appearance:none;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path d='M1 1l4 4 4-4' stroke='%2371767f' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>");
    background-repeat:no-repeat; background-position:left 10px center; padding-left:26px;
  }
  select:focus{outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-bg)}
  .arrow{color:var(--faint); font-size:13px}
  .presets{display:flex;gap:6px;flex-wrap:wrap;margin-inline-start:auto}
  .chip{
    background:transparent; border:1px solid var(--line); border-radius:999px; color:var(--muted);
    font:inherit; font-size:12.5px; padding:6px 12px; cursor:pointer; white-space:nowrap;
    transition:background-color 140ms var(--ease-out), color 140ms var(--ease-out), border-color 140ms var(--ease-out), transform 120ms var(--ease-out);
  }
  .chip:hover{border-color:#c7cbd1; color:var(--ink)}
  .chip:active{transform:scale(.96)}
  .chip.active{background:var(--brand); color:#fff; border-color:var(--brand)}

  .toolbar{margin:0 0 16px}
  input[type=search]{
    width:100%; padding:11px 14px; border:1px solid var(--line); border-radius:var(--radius-sm);
    font:inherit; font-size:14px; background:var(--card); color:inherit;
    transition:border-color 140ms var(--ease-out), box-shadow 140ms var(--ease-out);
  }
  input[type=search]:focus{outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-bg)}

  #body{display:flex;flex-direction:column;gap:14px}
  .card{
    background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:16px 16px 4px; opacity:0; transform:translateY(8px);
    animation:rise 420ms var(--ease-out) forwards;
  }
  .card > .chead{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:2px}
  .card > .chead h2{font-size:15.5px;margin:0;font-weight:600;letter-spacing:-.005em}
  .card > .chead .cnt{font-size:12px;color:var(--faint);white-space:nowrap}

  h3{font-size:13px;margin:18px 0 8px;display:flex;align-items:center;gap:7px;color:var(--ink);font-weight:600}
  h3 .badge{background:var(--accent-bg);color:var(--accent);border-radius:999px;
            padding:1px 8px;font-size:11px;font-weight:600}
  .scroll{overflow-x:auto;margin:0 -2px}
  table{width:100%;border-collapse:collapse;font-size:13px;min-width:560px}
  th{text-align:right;font-size:11px;letter-spacing:.03em;
     color:var(--faint);font-weight:600;padding:0 9px 8px;border-bottom:1px solid var(--line)}
  td{padding:10px 9px;border-bottom:1px solid #f1f2f4;vertical-align:middle}
  tbody tr{transition:background-color 120ms var(--ease-out)}
  tbody tr:hover{background:#fafafb}
  tr:last-child td{border-bottom:none}
  .name{font-weight:500;max-width:400px}
  .num{font-variant-numeric:tabular-nums;white-space:nowrap}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11.5px;
        font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
  .pill.up{background:var(--up-bg);color:var(--up)}
  .pill.down{background:var(--down-bg);color:var(--down)}
  /* اتجاه السعر: نزول = أحمر (تحذير)، صعود = أخضر — عكس ألوان pill.up/down أعلاه عمداً */
  .pill.pos{background:var(--down-bg);color:var(--down)}
  .pill.neg{background:var(--up-bg);color:var(--up)}
  .tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11.5px;
       background:#f2f3f5;color:#3f434a;white-space:nowrap}
  .old{color:var(--faint);text-decoration:line-through}
  a.link{color:var(--accent);text-decoration:none;font-size:12px;white-space:nowrap;
         transition:opacity 120ms var(--ease-out)}
  a.link:hover{opacity:.7;text-decoration:underline}
  .empty{background:var(--card);border:1px dashed var(--line);border-radius:var(--radius);
         padding:48px 20px;text-align:center;color:var(--muted)}
  .empty .big{font-size:15.5px;color:var(--ink);margin-bottom:5px;font-weight:600}
  .empty .small{font-size:13px}

  footer{color:var(--faint);font-size:11.5px;text-align:center;margin-top:30px}

  @keyframes rise{ to{ opacity:1; transform:translateY(0) } }
  .card:nth-of-type(1){animation-delay:0ms}
  .card:nth-of-type(2){animation-delay:30ms}
  .card:nth-of-type(3){animation-delay:60ms}
  .card:nth-of-type(4){animation-delay:90ms}
  .card:nth-of-type(5){animation-delay:120ms}
  .card:nth-of-type(n+6){animation-delay:140ms}
  .kpi:nth-child(1){animation-delay:0ms} .kpi:nth-child(2){animation-delay:30ms}
  .kpi:nth-child(3){animation-delay:60ms} .kpi:nth-child(4){animation-delay:90ms}
  .kpi:nth-child(5){animation-delay:120ms}

  @media (max-width:640px){
    h1{font-size:17px}
    .wrap{padding:18px 14px 48px}
    .rangebar{flex-direction:column;align-items:stretch}
    .presets{margin-inline-start:0}
  }
  @media print{ .rangebar,.toolbar{display:none} body{background:#fff} }
  @media (prefers-reduced-motion: reduce){
    .kpi,.card{animation:none; opacity:1; transform:none}
    *{transition-duration:1ms !important}
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand"><span class="dot"></span><h1>مكعب — لوحة الأسعار</h1></div>
    <div class="stamp" id="stamp">—</div>
  </header>

  <div class="kpis" id="kpis"></div>

  <div class="rangebar">
    <div class="grp">
      <label for="from">من</label>
      <select id="from"></select>
    </div>
    <span class="arrow">←</span>
    <div class="grp">
      <label for="to">إلى</label>
      <select id="to"></select>
    </div>
    <div class="presets" id="presets"></div>
  </div>

  <div class="toolbar">
    <input type="search" id="q" placeholder="ابحث باسم المنتج…" autocomplete="off">
  </div>

  <div id="body"></div>

  <footer><span id="range-note"></span></footer>
</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const RUNS_DESC = JSON.parse(document.getElementById('payload').textContent); // الأحدث أولاً
const RUNS_ASC = RUNS_DESC.slice().reverse(); // الأقدم أولاً
const DAYS = RUNS_ASC.map(r => r.day);
let query = '';
let fromDay = DAYS.length > 1 ? DAYS[DAYS.length - 2] : (DAYS[0] || '');
let toDay = DAYS[DAYS.length - 1] || '';

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const CUR = {SAR:'ر.س', USD:'$', AED:'د.إ'};
const money = (v, c) => v == null ? '—' : (Math.round(v*100)/100).toLocaleString('en-US') + ' ' + (CUR[c] || c || 'ر.س');
const fmtDay = d => { try { return new Date(d+'T00:00:00Z').toLocaleDateString('ar', {day:'numeric', month:'short', year:'numeric'}); } catch(e){ return d; } };
const fmtFull = t => { try { return new Date(t).toLocaleString('ar', {dateStyle:'medium', timeStyle:'short'}); } catch(e){ return t; } };

function populateSelects(){
  const opts = DAYS.map(d => `<option value="${esc(d)}">${esc(fmtDay(d))}</option>`).join('');
  document.getElementById('from').innerHTML = opts;
  document.getElementById('to').innerHTML = opts;
  document.getElementById('from').value = fromDay;
  document.getElementById('to').value = toDay;
}

function runsInRange(from, to){
  // from استثنائي (حالة البداية)، to شامل — التغييرات المتراكمة بين التاريخين
  return RUNS_ASC.filter(r => r.day > from && r.day <= to);
}

function aggregateRange(runs){
  const cats = new Map();
  for (const r of runs){
    for (const c of (r.categories || [])){
      if (!cats.has(c.name)) cats.set(c.name, {price: new Map(), offer: new Map(), add: new Map()});
      const slot = cats.get(c.name);
      for (const row of (c.price_changes || [])){
        const key = row.url;
        const existing = slot.price.get(key);
        if (!existing) slot.price.set(key, {name: row.name, url: row.url, currency: row.currency,
          start: row.old_price, end: row.price, on_offer: row.on_offer, regular_price: row.regular_price});
        else { existing.end = row.price; existing.on_offer = row.on_offer; existing.regular_price = row.regular_price; existing.name = row.name; }
      }
      for (const row of (c.offer_changes || [])){
        slot.offer.set(row.url + '|' + (row.offer_kind || ''), row); // يبقى آخر ظهور (الأحدث بترتيب تصاعدي)
      }
      for (const row of (c.new_arrivals || [])){
        if (!slot.add.has(row.url)) slot.add.set(row.url, row); // يبقى أول ظهور
      }
    }
  }
  const out = [];
  for (const [name, slot] of cats){
    const price = Array.from(slot.price.values())
      .filter(e => e.start !== e.end && e.start != null && e.end != null)
      .map(e => ({
        name: e.name, url: e.url, currency: e.currency, old_price: e.start, price: e.end,
        pct: e.start ? (e.end - e.start) / e.start * 100 : null,
        discount: (e.on_offer && e.regular_price) ? (e.regular_price - e.end) / e.regular_price * 100 : null,
      }));
    const offer = Array.from(slot.offer.values());
    const add = Array.from(slot.add.values());
    if (price.length + offer.length + add.length > 0) out.push({name, price, offer, add});
  }
  out.sort((a, b) => (b.price.length + b.offer.length + b.add.length) - (a.price.length + a.offer.length + a.add.length) || a.name.localeCompare(b.name, 'ar'));
  return out;
}

function match(row){
  if (!query) return true;
  return (row.name || '').toLowerCase().includes(query);
}

function priceTable(rows, fromLabel, toLabel){
  return `<div class="scroll"><table><thead><tr>
    <th>المنتج</th><th>السعر بتاريخ ${esc(fromLabel)}</th><th>السعر بتاريخ ${esc(toLabel)}</th><th>نسبة التغيّر</th><th>الخصم</th><th></th>
  </tr></thead><tbody>` + rows.map(r => {
    const up = r.price > r.old_price;
    const trend = up ? 'pos' : 'neg'; // صعود = أخضر، نزول = أحمر
    const pct = r.pct == null ? '' : (r.pct>0?'+':'') + r.pct.toFixed(1) + '%';
    return `<tr>
      <td class="name">${esc(r.name)}</td>
      <td class="num old">${esc(money(r.old_price, r.currency))}</td>
      <td class="num"><strong>${esc(money(r.price, r.currency))}</strong></td>
      <td><span class="pill ${trend}">${up?'▲':'▼'} ${esc(pct)}</span></td>
      <td class="num">${r.discount ? `%${Math.round(r.discount)}−` : '—'}</td>
      <td><a class="link" href="${esc(r.url)}" target="_blank" rel="noopener">فتح ↗</a></td>
    </tr>`;
  }).join('') + `</tbody></table></div>`;
}

function offerTable(rows){
  return `<div class="scroll"><table><thead><tr>
    <th>المنتج</th><th>نوع التغيير</th><th>السعر الأصلي</th><th>سعر العرض</th><th>نسبة الخصم</th><th></th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
      <td class="name">${esc(r.name)}</td>
      <td><span class="tag">${esc(r.offer_kind || '—')}</span></td>
      <td class="num old">${esc(money(r.regular_price, r.currency))}</td>
      <td class="num"><strong>${esc(money(r.price, r.currency))}</strong></td>
      <td class="num">${r.discount ? `<span class="pill down">%${Math.round(r.discount)}−</span>` : '—'}</td>
      <td><a class="link" href="${esc(r.url)}" target="_blank" rel="noopener">فتح ↗</a></td>
    </tr>`).join('') + `</tbody></table></div>`;
}

function newTable(rows){
  return `<div class="scroll"><table><thead><tr>
    <th>المنتج</th><th>السعر</th><th>عليه عرض؟</th><th></th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
      <td class="name">${esc(r.name)}</td>
      <td class="num"><strong>${esc(money(r.price, r.currency))}</strong></td>
      <td>${r.on_offer ? '<span class="pill down">نعم</span>' : '<span class="tag">لا</span>'}</td>
      <td><a class="link" href="${esc(r.url)}" target="_blank" rel="noopener">فتح ↗</a></td>
    </tr>`).join('') + `</tbody></table></div>`;
}

function renderKpis(runs, cats){
  const latest = RUNS_DESC[0] || {};
  const s = latest.stats || {};
  const priceCount = cats.reduce((n, c) => n + c.price.length, 0);
  const offerCount = cats.reduce((n, c) => n + c.offer.length, 0);
  const addCount = cats.reduce((n, c) => n + c.add.length, 0);
  const tiles = [
    ['المنتجات المفحوصة', (s.scanned||0).toLocaleString('en-US'), ''],
    ['نسبة القراءة', (s.coverage ?? 0) + '%', ''],
    ['تغيّرت أسعارها', priceCount.toLocaleString('en-US'), 'up'],
    ['تغيّرت عروضها', offerCount.toLocaleString('en-US'), 'down'],
    ['منتجات مضافة', addCount.toLocaleString('en-US'), 'down'],
  ];
  document.getElementById('kpis').innerHTML = tiles.map(([l,v,cls]) =>
    `<div class="kpi"><div class="label">${esc(l)}</div><div class="value ${cls}">${esc(v)}</div></div>`
  ).join('');
}

function renderPresets(){
  const last = DAYS[DAYS.length - 1] || '';
  const prev = DAYS.length > 1 ? DAYS[DAYS.length - 2] : last;
  const idx7 = Math.max(0, DAYS.length - 8);
  const presets = [
    ['آخر تحديث', prev, last],
    ['اليوم فقط', last, last],
    ['آخر ٧ أيام', DAYS[idx7] || last, last],
    ['كل الأرشيف', '', last],
  ];
  document.getElementById('presets').innerHTML = presets.map(([label, f, t]) =>
    `<button class="chip" data-from="${esc(f)}" data-to="${esc(t)}">${esc(label)}</button>`
  ).join('');
  document.querySelectorAll('.chip').forEach(b => b.onclick = () => {
    fromDay = b.dataset.from; toDay = b.dataset.to;
    document.getElementById('from').value = fromDay || DAYS[0];
    document.getElementById('to').value = toDay;
    render();
  });
}

function markActivePreset(){
  document.querySelectorAll('.chip').forEach(b => {
    b.classList.toggle('active', b.dataset.from === fromDay && b.dataset.to === toDay);
  });
}

function render(){
  document.getElementById('stamp').textContent = RUNS_DESC[0] ? ('آخر تحديث: ' + fmtFull(RUNS_DESC[0].generated_at)) : '';

  const runs = runsInRange(fromDay, toDay);
  const cats = aggregateRange(runs);
  renderKpis(runs, cats);
  markActivePreset();

  const fromLabel = fromDay ? fmtDay(fromDay) : 'بداية الأرشيف';
  const toLabel = fmtDay(toDay);
  document.getElementById('range-note').textContent = `المقارنة من ${fromLabel} إلى ${toLabel}`;

  const filtered = cats.map(c => ({
    name: c.name,
    p: c.price.filter(match),
    o: c.offer.filter(match),
    a: c.add.filter(match),
  })).filter(c => c.p.length + c.o.length + c.a.length > 0);

  const body = document.getElementById('body');
  if (!filtered.length){
    body.innerHTML = `<div class="empty">
      <div class="big">${query ? 'لا نتائج مطابقة للبحث' : 'لا تغييرات بين هذين التاريخين'}</div>
      <div class="small">${query ? 'جرّب كلمة أخرى.' : 'اختر مدى تاريخ أوسع لرؤية تغييرات أكثر.'}</div>
    </div>`;
    return;
  }

  body.innerHTML = filtered.map(c => {
    let h = `<section class="card"><div class="chead"><h2>${esc(c.name)}</h2>
      <div class="cnt">${c.p.length} سعر · ${c.o.length} عرض · ${c.a.length} إضافة</div></div>`;
    if (c.p.length) h += `<h3>💰 تغيّر الأسعار <span class="badge">${c.p.length}</span></h3>` + priceTable(c.p, fromLabel, toLabel);
    if (c.o.length) h += `<h3>🏷️ العروض <span class="badge">${c.o.length}</span></h3>` + offerTable(c.o);
    if (c.a.length) h += `<h3>🆕 منتجات مضافة <span class="badge">${c.a.length}</span></h3>` + newTable(c.a);
    return h + `</section>`;
  }).join('');
}

document.getElementById('q').addEventListener('input', e => {
  query = e.target.value.trim().toLowerCase();
  render();
});
document.getElementById('from').addEventListener('change', e => { fromDay = e.target.value; render(); });
document.getElementById('to').addEventListener('change', e => { toDay = e.target.value; render(); });

populateSelects();
renderPresets();
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
