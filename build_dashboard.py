#!/usr/bin/env python3
"""
يبني لوحة متابعة HTML ثابتة تُنشر عبر GitHub Pages من مجلد docs/.

يقرأ `latest_run.json` (مخرجات build_report.py) ويضيفه إلى أرشيف
`docs/data/history.json`، ثم يولّد `docs/index.html` صفحةً **مكتفية بذاتها**
(كل البيانات مضمّنة داخلها) — فتفتح فوراً بلا أي طلبات شبكة، وتعمل حتى لو
حُفظت على الجهاز.

اللوحة تعرض: مؤشرات اليوم، وشريط أيام للتنقل بين التقارير السابقة، وجداول
التغييرات مقسّمة حسب الكاتوجري (أسعار / عروض / منتجات مضافة)، مع بحث فوري.
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
<title>لوحة متابعة أسعار مكعب</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --ink:#16181d; --muted:#6b7280; --line:#e5e7eb;
    --up:#b42318; --up-bg:#fef3f2; --down:#067647; --down-bg:#ecfdf3;
    --brand:#1f2937; --accent:#3538cd; --accent-bg:#eef4ff;
    --radius:14px;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:"SF Arabic","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
    line-height:1.6; -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:1120px;margin:0 auto;padding:28px 20px 72px}
  header.top{display:flex;flex-wrap:wrap;gap:14px;align-items:baseline;justify-content:space-between;margin-bottom:6px}
  h1{font-size:24px;margin:0;letter-spacing:-.02em}
  .sub{color:var(--muted);font-size:14px;margin:2px 0 22px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:22px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px}
  .kpi .label{font-size:12.5px;color:var(--muted);margin-bottom:6px}
  .kpi .value{font-size:26px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
  .kpi .note{font-size:11.5px;color:var(--muted);margin-top:2px}
  .days{display:flex;gap:8px;overflow-x:auto;padding:4px 0 14px;scrollbar-width:thin}
  .day{flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:999px;
       padding:7px 14px;font-size:13px;cursor:pointer;white-space:nowrap;transition:.15s}
  .day:hover{border-color:#c7cbd1}
  .day.active{background:var(--brand);color:#fff;border-color:var(--brand)}
  .day .n{opacity:.65;font-size:11.5px;margin-inline-start:6px}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:6px 0 18px}
  input[type=search]{flex:1;min-width:220px;padding:11px 14px;border:1px solid var(--line);
       border-radius:10px;font:inherit;font-size:14px;background:var(--card);color:inherit}
  input[type=search]:focus{outline:2px solid var(--accent-bg);border-color:var(--accent)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
        padding:18px 18px 6px;margin-bottom:18px}
  .card > h2{font-size:17px;margin:0 0 4px;letter-spacing:-.01em}
  .card > .cnt{font-size:12.5px;color:var(--muted);margin-bottom:14px}
  h3{font-size:14px;margin:20px 0 9px;display:flex;align-items:center;gap:8px}
  h3 .badge{background:var(--accent-bg);color:var(--accent);border-radius:999px;
            padding:1px 9px;font-size:11.5px;font-weight:600}
  .scroll{overflow-x:auto;margin:0 -4px}
  table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:520px}
  th{text-align:right;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;
     color:var(--muted);font-weight:600;padding:0 10px 8px;border-bottom:1px solid var(--line)}
  td{padding:11px 10px;border-bottom:1px solid #f1f2f4;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .name{font-weight:500;max-width:420px}
  .num{font-variant-numeric:tabular-nums;white-space:nowrap}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
        font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
  .pill.up{background:var(--up-bg);color:var(--up)}
  .pill.down{background:var(--down-bg);color:var(--down)}
  .tag{display:inline-block;padding:2px 9px;border-radius:6px;font-size:12px;
       background:#f3f4f6;color:#374151;white-space:nowrap}
  .old{color:var(--muted);text-decoration:line-through}
  a.link{color:var(--accent);text-decoration:none;font-size:12.5px;white-space:nowrap}
  a.link:hover{text-decoration:underline}
  .empty{background:var(--card);border:1px dashed var(--line);border-radius:var(--radius);
         padding:44px 20px;text-align:center;color:var(--muted)}
  .empty .big{font-size:17px;color:var(--ink);margin-bottom:6px;font-weight:600}
  footer{color:var(--muted);font-size:12.5px;text-align:center;margin-top:36px;line-height:1.9}
  footer a{color:var(--muted)}
  @media (max-width:560px){ h1{font-size:20px} .wrap{padding:20px 14px 56px} }
  @media print{ .days,.toolbar{display:none} body{background:#fff} }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>لوحة متابعة أسعار مكعب</h1>
      <p class="sub" id="sub">—</p>
    </div>
  </header>

  <div class="kpis" id="kpis"></div>
  <div class="days" id="days"></div>
  <div class="toolbar">
    <input type="search" id="q" placeholder="ابحث باسم المنتج…" autocomplete="off">
  </div>
  <div id="body"></div>

  <footer>
    تُحدَّث تلقائياً كل صباح عبر GitHub Actions — لا حاجة لأي تدخل يدوي.<br>
    آخر تحديث للبيانات: <span id="stamp">—</span>
  </footer>
</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const RUNS = JSON.parse(document.getElementById('payload').textContent);
let current = 0, query = '';

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const CUR = {SAR:'ر.س', USD:'$', AED:'د.إ'};
const money = (v, c) => v == null ? '—' : (Math.round(v*100)/100).toLocaleString('en-US') + ' ' + (CUR[c] || c || 'ر.س');
const fmtDay = d => { try { return new Date(d+'T00:00:00Z').toLocaleDateString('ar', {weekday:'short', day:'numeric', month:'short'}); } catch(e){ return d; } };
const fmtFull = t => { try { return new Date(t).toLocaleString('ar', {dateStyle:'full', timeStyle:'short'}); } catch(e){ return t; } };

function runTotal(r){ const t = r.totals || {}; return (t.price_changes||0)+(t.offer_changes||0)+(t.new_arrivals||0); }

function renderKpis(r){
  const s = r.stats || {}, t = r.totals || {};
  const tiles = [
    ['المنتجات المفحوصة', (s.scanned||0).toLocaleString('en-US'), 'من كتالوج مكعب'],
    ['نسبة قراءة المتاح', (s.coverage ?? 0) + '%', (s.failed||0) + ' فشل تقني'],
    ['تغيّرت أسعارها', (t.price_changes||0).toLocaleString('en-US'), 'زيادة أو نقص'],
    ['تغيّرت عروضها', (t.offer_changes||0).toLocaleString('en-US'), 'بدأ / انتهى / تغيّر'],
    ['منتجات مضافة', (t.new_arrivals||0).toLocaleString('en-US'), 'جديدة على المتجر'],
  ];
  document.getElementById('kpis').innerHTML = tiles.map(([l,v,n]) =>
    `<div class="kpi"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div><div class="note">${esc(n)}</div></div>`
  ).join('');
}

function renderDays(){
  document.getElementById('days').innerHTML = RUNS.map((r,i) => {
    const n = runTotal(r);
    return `<button class="day ${i===current?'active':''}" data-i="${i}">${esc(fmtDay(r.day))}<span class="n">${n}</span></button>`;
  }).join('');
  document.querySelectorAll('.day').forEach(b =>
    b.onclick = () => { current = +b.dataset.i; render(); });
}

function match(row){
  if (!query) return true;
  return (row.name || '').toLowerCase().includes(query);
}

function priceTable(rows){
  return `<div class="scroll"><table><thead><tr>
    <th>المنتج</th><th>السعر السابق</th><th>السعر الحالي</th><th>التغيّر</th><th></th>
  </tr></thead><tbody>` + rows.map(r => {
    const up = r.price > r.old_price;
    const pct = r.pct == null ? '' : (r.pct>0?'+':'') + r.pct.toFixed(1) + '%';
    return `<tr>
      <td class="name">${esc(r.name)}</td>
      <td class="num old">${esc(money(r.old_price, r.currency))}</td>
      <td class="num"><strong>${esc(money(r.price, r.currency))}</strong></td>
      <td><span class="pill ${up?'up':'down'}">${up?'▲':'▼'} ${esc(pct)}</span></td>
      <td><a class="link" href="${esc(r.url)}" target="_blank" rel="noopener">فتح ↗</a></td>
    </tr>`;
  }).join('') + `</tbody></table></div>`;
}

function offerTable(rows){
  return `<div class="scroll"><table><thead><tr>
    <th>المنتج</th><th>نوع التغيير</th><th>السعر الأصلي</th><th>سعر العرض</th><th>الخصم</th><th></th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
      <td class="name">${esc(r.name)}</td>
      <td><span class="tag">${esc(r.offer_kind || '—')}</span></td>
      <td class="num old">${esc(money(r.regular_price, r.currency))}</td>
      <td class="num"><strong>${esc(money(r.price, r.currency))}</strong></td>
      <td>${r.discount ? `<span class="pill down">${Math.round(r.discount)}%−</span>` : '—'}</td>
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

function render(){
  const r = RUNS[current];
  if (!r) return;
  document.getElementById('sub').textContent =
    (r.seeding ? 'تشغيل تأسيسي — حُفظ الكتالوج كمرجع' : 'تقرير ') + (r.seeding ? '' : fmtFull(r.generated_at));
  document.getElementById('stamp').textContent = fmtFull(RUNS[0].generated_at);
  renderKpis(r); renderDays();

  const cats = (r.categories || []).map(c => ({
    name: c.name,
    p: (c.price_changes||[]).filter(match),
    o: (c.offer_changes||[]).filter(match),
    a: (c.new_arrivals||[]).filter(match),
  })).filter(c => c.p.length + c.o.length + c.a.length > 0);

  const body = document.getElementById('body');
  if (!cats.length){
    body.innerHTML = `<div class="empty">
      <div class="big">${query ? 'لا نتائج مطابقة للبحث' : 'لا توجد تغييرات في هذا اليوم'}</div>
      <div>${query ? 'جرّب كلمة أخرى.' : 'فُحص الكتالوج كاملاً ولم يتغيّر أي سعر أو عرض ولم يُضف منتج.'}</div>
    </div>`;
    return;
  }

  body.innerHTML = cats.map(c => {
    let h = `<section class="card"><h2>${esc(c.name)}</h2>
      <div class="cnt">${c.p.length} تغيّر سعر · ${c.o.length} عرض · ${c.a.length} منتج مضاف</div>`;
    if (c.p.length) h += `<h3>💰 تغيّر الأسعار <span class="badge">${c.p.length}</span></h3>` + priceTable(c.p);
    if (c.o.length) h += `<h3>🏷️ العروض <span class="badge">${c.o.length}</span></h3>` + offerTable(c.o);
    if (c.a.length) h += `<h3>🆕 منتجات تمت إضافتها <span class="badge">${c.a.length}</span></h3>` + newTable(c.a);
    return h + `</section>`;
  }).join('');
}

document.getElementById('q').addEventListener('input', e => {
  query = e.target.value.trim().toLowerCase();
  render();
});

render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
