#!/usr/bin/env python3
"""
يدمج نتائج كل الشرائح (shard_*.json)، يقارنها بخط الأساس، ويبني التقرير.

شكل التقرير (حسب طلب المستخدم): التقرير مقسّم حسب **الكاتوجري** (تصنيف مكعب
الفعلي لكل منتج)، وتحت كل كاتوجري ثلاثة جداول:
  1) 💰 تغيّر الأسعار (زيادة أو نقص) — السعر قبل وبعد + نسبة التغيّر + الرابط
  2) 🏷️ العروض — عرض جديد بدأ، أو عرض انتهى، أو تغيّرت قيمة العرض
  3) 🆕 منتجات تمت إضافتها — السعر + الرابط

المنتجات اللي ما تغيّر فيها شيء لا تُذكر إطلاقاً، والكاتوجري اللي ما فيه أي
تغيير لا يظهر أصلاً.

يُخرج أيضاً `latest_run.json` بصيغة منظّمة يقرأها `build_dashboard.py` لبناء
لوحة المتابعة — ويُكتب في كل الحالات (حتى بلا تغييرات) لتعكس اللوحة حالة اليوم.

ملاحظات مهمة على الدقة:
  - المنتجات اللي فشلت قراءتها في هذا التشغيل لا تُقارَن ولا تُحدَّث، وتبقى
    قيمتها القديمة في خط الأساس كما هي — فلا يمكن أن تُنتج إنذاراً كاذباً.
  - المنتجات اللي تعذّرت قراءتها في التشغيل السابق (سجل "unread") لا تُبلَّغ
    كـ"منتجات مضافة" عند أول قراءة ناجحة لها — لأنها ليست جديدة، بل فاتنا
    قراءتها فقط. بدون هذا كانت مئات المنتجات القديمة ستظهر كأنها مضافة.
  - روابط sitemap اللي تفتح صفحة عامة فارغة (منتجات محذوفة/غير متاحة) تُحسب
    "غير متاح" وليست فشلاً تقنياً، فنسبة القراءة تُحسب على المتاح فعلاً.
  - التشغيل الأول على كامل الكتالوج يُعتبر "تأسيس خط أساس" (seeding): نحفظ
    كل المنتجات بدون أن نعتبر آلاف المنتجات "جديدة".
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

BASELINE_PATH = "mokab_price_baseline.json"
ISSUE_BODY_PATH = "issue_body.md"
FULL_REPORT_PATH = "full_report.md"
# مخرجات منظّمة تقرأها لوحة المتابعة (build_dashboard.py)
LATEST_RUN_PATH = "latest_run.json"

# حد GitHub لجسم الـ Issue = 65536 حرف. نترك هامش أمان.
ISSUE_BODY_LIMIT = 60000

UNCATEGORIZED = "غير مصنّف"


def log(msg):
    print(msg, flush=True)


def load_baseline():
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"generated_at": None, "products": {}}


def md_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe = [str(c).replace("|", "\\|").replace("\n", " ") for c in row]
        lines.append("| " + " | ".join(safe) + " |")
    return "\n".join(lines)


def pct_change(old_v, new_v):
    if not old_v:
        return 0.0
    return (float(new_v) - float(old_v)) / float(old_v) * 100


def money(value, currency):
    if value is None:
        return "—"
    return f"{float(value):g} {currency}"


def discount_pct(regular, price):
    if not regular or price is None or float(regular) <= 0:
        return None
    return (float(regular) - float(price)) / float(regular) * 100


def merge_shards():
    products, gone, failed = {}, [], []
    shard_files = sorted(glob.glob("shards/**/shard_*.json", recursive=True)) or sorted(
        glob.glob("shard_*.json")
    )
    if not shard_files:
        return None, None, None, []

    for path in shard_files:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        products.update(data.get("products", {}))
        gone.extend(data.get("gone", []))
        failed.extend(data.get("failed", []))
        log(
            f"  {path}: قُرئ {len(data.get('products', {}))} | "
            f"غير متاح {len(data.get('gone', []))} | "
            f"فشل {len(data.get('failed', []))} | "
            f"429: {data.get('rate_limit_hits', 0)}"
        )
    return products, gone, failed, shard_files


def classify_changes(scanned, old_products, old_unread):
    """يقارن المفحوص بخط الأساس ويرجع التغييرات مصنّفة.

    old_unread: منتجات تعذّرت قراءتها في التشغيل السابق، فهي غير موجودة في خط
    الأساس رغم أنها ليست جديدة. أول قراءة ناجحة لها تُحفظ بصمت ولا تُبلَّغ
    كـ"منتج مضاف" — وإلا لظهرت مئات المنتجات القديمة كأنها جديدة.
    """
    price_changes, offer_changes, new_arrivals = [], [], []

    for pid, p in scanned.items():
        old = old_products.get(pid)
        if old is None:
            if pid not in old_unread:
                new_arrivals.append(p)
            continue

        old_price = old.get("price")
        try:
            old_price = float(old_price) if old_price is not None else None
        except (TypeError, ValueError):
            old_price = None
        new_price = p["price"]

        if old_price is not None and new_price is not None and old_price != new_price:
            price_changes.append({**p, "old_price": old_price})

        # مقارنة حالة العرض
        old_on_offer = bool(old.get("on_offer"))
        old_regular = old.get("regular_price")
        try:
            old_regular = float(old_regular) if old_regular is not None else None
        except (TypeError, ValueError):
            old_regular = None
        new_on_offer = bool(p.get("on_offer"))

        kind = None
        if new_on_offer and not old_on_offer:
            kind = "بدأ عرض"
        elif old_on_offer and not new_on_offer:
            kind = "انتهى العرض"
        elif old_on_offer and new_on_offer:
            old_d = discount_pct(old_regular, old_price)
            new_d = discount_pct(p.get("regular_price"), new_price)
            if old_d is not None and new_d is not None and abs(old_d - new_d) >= 0.5:
                kind = "تغيّرت قيمة العرض"

        # "بدأ عرض" على منتج لم نكن نعرف حالته سابقاً (خط أساس قديم بدون الحقل)
        # يُتجاهل لتفادي ضجيج أول تشغيل بعد تغيير الصيغة
        if kind and not (kind == "بدأ عرض" and "on_offer" not in old):
            offer_changes.append({**p, "offer_kind": kind, "old_price": old_price})

    return price_changes, offer_changes, new_arrivals


def group_by_category(items):
    groups = {}
    for it in items:
        groups.setdefault(it.get("category") or UNCATEGORIZED, []).append(it)
    return groups


def _row(c, kind):
    """يحوّل عنصر تغيير إلى صيغة مبسّطة تصلح للعرض في اللوحة."""
    d = {
        "name": c["name"],
        "url": c["url"],
        "price": c["price"],
        "regular_price": c.get("regular_price"),
        "currency": c["currency"],
        "on_offer": bool(c.get("on_offer")),
    }
    if kind in ("price", "offer"):
        d["old_price"] = c.get("old_price")
    if kind == "offer":
        d["offer_kind"] = c.get("offer_kind")
        d["discount"] = discount_pct(c.get("regular_price"), c["price"])
    if kind == "price" and c.get("old_price"):
        d["pct"] = pct_change(c["old_price"], c["price"])
    return d


def write_latest_run(stats, seeding, price_changes, offer_changes, new_arrivals):
    """يكتب latest_run.json — مصدر بيانات لوحة المتابعة، يُكتب في كل الحالات
    (حتى لو لا توجد تغييرات) حتى تعرض اللوحة حالة اليوم بدقة."""
    g_p = group_by_category(price_changes)
    g_o = group_by_category(offer_changes)
    g_a = group_by_category(new_arrivals)
    cats = sorted(
        set(g_p) | set(g_o) | set(g_a),
        key=lambda c: (
            -(len(g_p.get(c, [])) + len(g_o.get(c, [])) + len(g_a.get(c, []))),
            c,
        ),
    )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seeding": seeding,
        "stats": stats,
        "totals": {
            "price_changes": len(price_changes),
            "offer_changes": len(offer_changes),
            "new_arrivals": len(new_arrivals),
        },
        "categories": [
            {
                "name": c,
                "price_changes": [_row(x, "price") for x in g_p.get(c, [])],
                "offer_changes": [_row(x, "offer") for x in g_o.get(c, [])],
                "new_arrivals": [_row(x, "new") for x in g_a.get(c, [])],
            }
            for c in cats
        ],
    }
    with open(LATEST_RUN_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    log(f"كُتب {LATEST_RUN_PATH} للوحة المتابعة.")


def build_category_section(cat, prices, offers, arrivals):
    parts = [f"## 📂 {cat}\n"]

    if prices:
        prices.sort(
            key=lambda c: abs(pct_change(c["old_price"], c["price"])), reverse=True
        )
        parts.append(f"### 💰 تغيّر الأسعار ({len(prices)})\n")
        rows = []
        for c in prices:
            pc = pct_change(c["old_price"], c["price"])
            arrow = "⬆️" if c["price"] > c["old_price"] else "⬇️"
            rows.append(
                [
                    c["name"],
                    money(c["old_price"], c["currency"]),
                    money(c["price"], c["currency"]),
                    f"{arrow} {pc:+.1f}%",
                    f"[رابط]({c['url']})",
                ]
            )
        parts.append(
            md_table(
                ["المنتج", "السعر السابق", "السعر الحالي", "التغيّر", "الرابط"], rows
            )
        )
        parts.append("")

    if offers:
        parts.append(f"### 🏷️ العروض ({len(offers)})\n")
        rows = []
        for c in offers:
            d = discount_pct(c.get("regular_price"), c["price"])
            rows.append(
                [
                    c["name"],
                    c["offer_kind"],
                    money(c.get("regular_price"), c["currency"]),
                    money(c["price"], c["currency"]),
                    f"{d:.0f}%" if d else "—",
                    f"[رابط]({c['url']})",
                ]
            )
        parts.append(
            md_table(
                [
                    "المنتج",
                    "نوع التغيير",
                    "السعر الأصلي",
                    "سعر العرض",
                    "نسبة الخصم",
                    "الرابط",
                ],
                rows,
            )
        )
        parts.append("")

    if arrivals:
        parts.append(f"### 🆕 منتجات تمت إضافتها ({len(arrivals)})\n")
        rows = []
        for c in arrivals:
            rows.append(
                [
                    c["name"],
                    money(c["price"], c["currency"]),
                    "نعم" if c.get("on_offer") else "لا",
                    f"[رابط]({c['url']})",
                ]
            )
        parts.append(md_table(["المنتج", "السعر", "عليه عرض؟", "الرابط"], rows))
        parts.append("")

    return "\n".join(parts)


def main():
    baseline = load_baseline()
    old_products = baseline.get("products", {})
    # None = خط أساس قديم لا يحمل سجل "غير مقروء" بعد (يُشتق تلقائياً أدناه)
    old_unread = set(baseline["unread"]) if "unread" in baseline else None

    log("دمج نتائج الشرائح:")
    scanned, gone, failed, shard_files = merge_shards()
    if scanned is None:
        log("⚠️ لم يُعثر على أي ملف شريحة (shard_*.json). نتوقف.")
        return 1

    # نسبة القراءة تُحسب على المنتجات الموجودة فعلاً؛ الروابط المحذوفة/غير
    # المتاحة (gone) ليست فشلاً وإنما منتجات لم تعد موجودة على الموقع.
    live_total = len(scanned) + len(failed)
    coverage = len(scanned) / live_total * 100 if live_total else 0
    log(
        f"\nالإجمالي: قُرئ {len(scanned)} | غير متاح/محذوف {len(gone)} | "
        f"فشل تقني {len(failed)} | نسبة قراءة المنتجات المتاحة {coverage:.2f}%"
    )

    if not scanned:
        log("⚠️ لم يُقرأ أي منتج. لن يُحدَّث خط الأساس ولن يُفتح Issue.")
        return 1

    # خط أساس قديم لا يحمل سجل "غير مقروء": نشتقه الآن = كل منتج ظهر في خريطة
    # الموقع ولم يدخل خط الأساس بعد (تعذّرت قراءته في التشغيل السابق). بدون
    # هذا ستظهر مئات المنتجات القديمة كأنها "مضافة حديثاً". يحدث مرة واحدة فقط.
    if old_unread is None:
        seen_now = set(scanned) | set(gone) | set(failed)
        old_unread = seen_now - set(old_products)
        log(
            f"ℹ️ خط أساس بدون سجل 'غير مقروء' — اشتُق تلقائياً: {len(old_unread)} "
            "منتج لن يُبلَّغ عنه كجديد في هذا التشغيل."
        )

    # تشغيل تأسيسي: خط الأساس أصغر بكثير من حجم الفحص (أول مرة على كامل الكتالوج)
    seeding = len(old_products) < len(scanned) * 0.5

    if seeding:
        price_changes, offer_changes, new_arrivals = [], [], []
        log(
            f"\nℹ️ تشغيل تأسيسي: خط الأساس كان فيه {len(old_products)} منتج فقط "
            f"مقابل {len(scanned)} مفحوص — نحفظ الكتالوج كخط أساس بدون اعتبار "
            "كل المنتجات 'جديدة'."
        )
    else:
        price_changes, offer_changes, new_arrivals = classify_changes(
            scanned, old_products, old_unread
        )
        recovered = len(
            [p for p in scanned if p not in old_products and p in old_unread]
        )
        if recovered:
            log(
                f"استُدركت {recovered} منتج تعذّرت قراءتها سابقاً — حُفظت بصمت "
                "ولم تُبلَّغ كمنتجات جديدة."
            )
        log(
            f"تغيّر أسعار: {len(price_changes)} | تغيّر عروض: {len(offer_changes)} | "
            f"منتجات جديدة: {len(new_arrivals)}"
        )

    # تحديث خط الأساس: المفحوص فقط يُحدَّث، والباقي يبقى كما هو
    new_products = dict(old_products)
    for pid, p in scanned.items():
        new_products[pid] = {
            "name": p["name"],
            "price": p["price"],
            "regular_price": p.get("regular_price"),
            "on_offer": bool(p.get("on_offer")),
            "currency": p["currency"],
            "availability": p.get("availability"),
            "category": p.get("category"),
            "categories": p.get("categories", []),
            "url": p["url"],
        }

    baseline["generated_at"] = datetime.now(timezone.utc).isoformat()
    baseline["coverage"] = {
        "scanned": len(scanned),
        "gone": len(gone),
        "failed": len(failed),
        "percent": round(coverage, 2),
    }
    baseline["products"] = new_products
    # نحفظ قائمة اللي تعذّرت قراءته حتى لا يُبلَّغ عنها كـ"منتجات جديدة" عند
    # أول قراءة ناجحة لها في التشغيل القادم.
    baseline["unread"] = sorted(failed)
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    log(f"حُفظ خط الأساس: {len(new_products)} منتج.")

    total_changes = len(price_changes) + len(offer_changes) + len(new_arrivals)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"changes_count={total_changes}\n")
            f.write(f"coverage={coverage:.2f}\n")

    # يُكتب قبل أي خروج مبكر حتى تعكس اللوحة حالة اليوم دائماً
    write_latest_run(
        {
            "scanned": len(scanned),
            "gone": len(gone),
            "failed": len(failed),
            "coverage": round(coverage, 2),
            "catalog": len(new_products),
        },
        seeding,
        price_changes,
        offer_changes,
        new_arrivals,
    )

    header = [
        "## 🔔 تحديثات مكعب",
        "",
        f"- المنتجات المفحوصة: **{len(scanned)}** من كتالوج مكعب "
        f"(نسبة قراءة المنتجات المتاحة {coverage:.1f}%)",
    ]
    if gone:
        header.append(
            f"- **{len(gone)}** رابط في خريطة الموقع لمنتجات لم تعد متاحة "
            "(صفحاتها فارغة أو محذوفة) — طبيعي وليس خطأً"
        )
    if failed:
        header.append(
            f"- تعذّرت قراءة **{len(failed)}** منتج في هذا التشغيل "
            "(لم تُقارَن ولم تُحدَّث — ستُعاد قراءتها في التشغيل القادم)"
        )
    header.append("")

    if seeding:
        cats = {}
        for p in scanned.values():
            key = p.get("category") or UNCATEGORIZED
            cats[key] = cats.get(key, 0) + 1
        top = sorted(cats.items(), key=lambda kv: -kv[1])[:15]
        body = "\n".join(
            header
            + [
                "تم تأسيس خط الأساس لكامل الكتالوج بنجاح. ابتداءً من التشغيل القادم "
                "ستصلك التغييرات فقط (تغيّر الأسعار، العروض، المنتجات المضافة) "
                "مقسّمة حسب الكاتوجري.",
                "",
                f"### أكبر الأقسام ({len(cats)} قسم إجمالاً)\n",
                md_table(["القسم", "عدد المنتجات"], [[c, n] for c, n in top]),
            ]
        )
        with open(ISSUE_BODY_PATH, "w", encoding="utf-8") as f:
            f.write(body)
        log("كُتب تقرير التأسيس.")
        return 0

    if total_changes == 0:
        for path in (ISSUE_BODY_PATH, FULL_REPORT_PATH):
            if os.path.exists(path):
                os.remove(path)
        log("لا توجد أي تغييرات — لن يُفتح Issue.")
        return 0

    g_prices = group_by_category(price_changes)
    g_offers = group_by_category(offer_changes)
    g_arrivals = group_by_category(new_arrivals)

    all_cats = set(g_prices) | set(g_offers) | set(g_arrivals)
    # ترتيب الأقسام: الأكثر تغييراً أولاً
    ordered = sorted(
        all_cats,
        key=lambda c: (
            -(
                len(g_prices.get(c, []))
                + len(g_offers.get(c, []))
                + len(g_arrivals.get(c, []))
            ),
            c,
        ),
    )

    summary = md_table(
        ["القسم", "تغيّر أسعار", "عروض", "منتجات جديدة"],
        [
            [
                c,
                len(g_prices.get(c, [])),
                len(g_offers.get(c, [])),
                len(g_arrivals.get(c, [])),
            ]
            for c in ordered
        ],
    )

    intro = "\n".join(
        header
        + [
            f"### 📊 ملخّص حسب القسم ({len(ordered)} قسم فيه تغييرات)\n",
            summary,
            "",
            "---",
            "",
        ]
    )

    sections = [
        build_category_section(
            c, g_prices.get(c, []), g_offers.get(c, []), g_arrivals.get(c, [])
        )
        for c in ordered
    ]

    full = intro + "\n".join(sections)
    with open(FULL_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(full)

    # الالتزام بحد حجم الـ Issue
    if len(full) <= ISSUE_BODY_LIMIT:
        body = full
    else:
        body = intro
        included = 0
        for sec in sections:
            if len(body) + len(sec) > ISSUE_BODY_LIMIT - 400:
                break
            body += sec + "\n"
            included += 1
        body += (
            f"\n> ⚠️ التقرير أطول من الحد الأقصى لحجم الـ Issue في GitHub، "
            f"فعُرض {included} قسم من أصل {len(ordered)}. "
            "التقرير الكامل مرفق كملف في صفحة التشغيل (Artifacts → full-report)."
        )
    with open(ISSUE_BODY_PATH, "w", encoding="utf-8") as f:
        f.write(body)

    log(f"كُتب التقرير ({len(body)} حرف) عبر {len(ordered)} قسم.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
