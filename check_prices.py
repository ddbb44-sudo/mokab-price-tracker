#!/usr/bin/env python3
"""
مراقب أسعار كامل كتالوج مكعب (mokab.com) — حوالي 5000 منتج، عبر خريطة الموقع (sitemap).

بدل تصفّح 6 أقسام محددة يدوياً، السكربت الآن:
  1) يجيب https://mokab.com/sitemap.xml (فهرس الخرائط sitemap index).
  2) يفحص كل خريطة فرعية غير خاصة بالمدونة (blog)، ويجمع كل رابط منتج
     (أي رابط ينتهي بـ /p<رقم>) — هذا يغطي كامل الكتالوج تلقائياً حتى لو
     تغيّر عدد/تقسيم الخرائط الفرعية مستقبلاً.
  3) لكل رابط منتج، يعمل طلب HTTP عادي (بدون متصفح آلي) ويقرأ meta tags
     من نوع Open Graph/Product المضمّنة بالـ HTML الخام (مثل
     product:sale_price:amount، product:price:amount، product:availability،
     وأيضاً product:category — تصنيف مكعب الفعلي لهذا المنتج، نأخذ أول
     مستوى منه كـ"الفئة الرئيسية" لتجميع التقرير حسبها).
  4) يقارن بخط الأساس المحفوظ في baseline.json، ويبني تقرير بجدولين
     (تغيّرات الأسعار + المنتجات الجديدة)، وكل جدول مقسّم لأقسام فرعية
     حسب فئة مكعب الحقيقية لكل منتج.

يستخدم ThreadPoolExecutor بعدد اتصالات متوازية محدود جداً (MAX_WORKERS) مع
تأخير بسيط بين الطلبات، لأن محاولة أولى بعدد اتصالات أعلى تسببت بحظر فوري
من مكعب (429 Too Many Requests) لمعظم الطلبات. عند حدوث 429 يعاد المحاولة
مع انتظار أطول تدريجياً (backoff) بدل إعادة المحاولة الفورية.

حماية من الإنذارات الكاذبة: إذا فشل الفحص بشكل كبير (أقل من 50% من المنتجات
قُرئت بنجاح)، نتوقف بخطأ واضح بدل فتح Issue فيه بيانات غير موثوقة، وبدون
تحديث baseline.
"""
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

BASELINE_PATH = "mokab_price_baseline.json"
SITEMAP_INDEX_URL = "https://mokab.com/sitemap.xml"

# مكعب يحظر بسرعة عند عدد اتصالات متزامنة مرتفع (جُرِّب MAX_WORKERS=8 وفشل
# فوراً بأخطاء 429). لذلك: عدد اتصالات منخفض جداً + تأخير بين كل طلب وآخر.
MAX_WORKERS = 2
REQUEST_TIMEOUT = 20
MAX_RETRIES_PER_PRODUCT = 3
# انتظار تصاعدي بعد كل 429 (بالثواني) — كل محاولة تالية تنتظر أطول
RETRY_BACKOFF_SECONDS = [3, 8, 15]
# تأخير إضافي بعد كل طلب (ناجح أو فاشل) لتخفيف معدل الطلبات على مكعب
REQUEST_DELAY_SECONDS = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}

PRODUCT_URL_RE = re.compile(r"/p(\d+)$")
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
SITEMAP_TAG_RE = re.compile(r"<sitemap>(.*?)</sitemap>", re.S)

UNCATEGORIZED = "غير مصنّف"


def http_get(url, timeout=REQUEST_TIMEOUT):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def discover_product_urls():
    """يرجع dict: productID -> url، بجمع كل رابط منتج من كل خرائط الموقع
    الفرعية (يتجاهل خرائط المدونة/blog، ويقبل أي رابط شكل .../p<رقم>)."""
    try:
        index_xml = http_get(SITEMAP_INDEX_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"فشل تحميل sitemap index: {exc}")
        return {}

    sub_sitemaps = []
    for block in SITEMAP_TAG_RE.findall(index_xml):
        m = LOC_RE.search(block)
        if not m:
            continue
        loc = m.group(1).strip()
        if "blog" in loc.lower():
            continue  # مو منتجات
        sub_sitemaps.append(loc)

    if not sub_sitemaps:
        # مو فهرس فيه خرائط فرعية — ربما ملف واحد يحتوي كل الروابط مباشرة
        sub_sitemaps = [SITEMAP_INDEX_URL]

    products = {}
    for sitemap_url in sub_sitemaps:
        print(f"قراءة خريطة الموقع: {sitemap_url}")
        try:
            xml = http_get(sitemap_url)
        except Exception as exc:  # noqa: BLE001
            print(f"  فشل: {exc}")
            continue
        locs = LOC_RE.findall(xml)
        found_here = 0
        for loc in locs:
            m = PRODUCT_URL_RE.search(loc)
            if not m:
                continue
            pid = m.group(1)
            products[pid] = loc
            found_here += 1
        print(f"  روابط منتجات موجودة بهذه الخريطة: {found_here}")

    return products


META_RE_TEMPLATE = r'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']*)["\']'


def extract_meta(html, prop):
    # ترتيب attributes بالـ HTML الفعلي ممكن يكون property قبل content أو العكس
    pattern = META_RE_TEMPLATE.format(prop=re.escape(prop))
    m = re.search(pattern, html)
    if m:
        return m.group(1)
    # جرّب الترتيب المعاكس (content قبل property)
    alt_pattern = (
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']'
        + re.escape(prop)
        + r'["\']'
    )
    m = re.search(alt_pattern, html)
    return m.group(1) if m else None


def extract_main_category(html):
    """يرجع أول مستوى من product:category (مثلاً من "وصلات وكيابل,كيبل
    ايفون Lightning,اكسبرس" يرجع "وصلات وكيابل")، أو None إذا ما وُجد."""
    raw = extract_meta(html, "product:category")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts[0] if parts else None


def fetch_product(pid, url):
    """يجيب صفحة المنتج ويستخرج السعر الحالي والأصلي والتوفر والاسم والفئة.
    يرجع dict أو None لو فشل نهائياً بعد إعادة المحاولة."""
    last_exc = None
    html = None
    for attempt in range(MAX_RETRIES_PER_PRODUCT):
        try:
            html = http_get(url)
            break
        except requests.exceptions.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            if status == 429:
                wait_s = RETRY_BACKOFF_SECONDS[
                    min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)
                ]
                time.sleep(wait_s)
            else:
                time.sleep(1)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1)
    else:
        print(f"  [{pid}] فشل بعد {MAX_RETRIES_PER_PRODUCT} محاولات: {last_exc}")
        time.sleep(REQUEST_DELAY_SECONDS)
        return None

    # تأخير بعد كل طلب ناجح أيضاً، لتخفيف معدل الطلبات الكلي على مكعب
    time.sleep(REQUEST_DELAY_SECONDS + random.uniform(0, 0.3))

    sale_price = extract_meta(html, "product:sale_price:amount")
    list_price = extract_meta(html, "product:price:amount")
    currency = (
        extract_meta(html, "product:sale_price:currency")
        or extract_meta(html, "product:price:currency")
        or "SAR"
    )
    availability = extract_meta(html, "product:availability")
    title = extract_meta(html, "og:title")
    category = extract_main_category(html)

    # السعر الحالي الفعلي: لو فيه سعر عرض (sale_price) هذا اللي يدفعه العميل،
    # وإلا السعر العادي (price) هو الحالي.
    current_price = sale_price if sale_price else list_price
    if current_price is None:
        return None  # ما قدرنا نلقى أي سعر بهذي الصفحة — تجاهلها

    if title:
        title = title.split("|")[0].strip()

    return {
        "productID": pid,
        "name": title or pid,
        "price": current_price,
        "currency": currency,
        "availability": availability,
        "category": category,
        "url": url,
    }


def load_baseline():
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"generated_at": None, "products": {}}


def save_baseline(baseline):
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)


def pct_change(old_v, new_v):
    if not old_v:
        return 0
    return (float(new_v) - float(old_v)) / float(old_v) * 100


def md_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe_row = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        lines.append("| " + " | ".join(safe_row) + " |")
    return "\n".join(lines)


def group_by_category(items, category_key):
    """يرجّع list من (اسم_الفئة, [عناصر]) مرتبة تنازلياً حسب عدد العناصر
    ثم أبجدياً، لعرض الفئات الأكبر أولاً."""
    groups = {}
    for item in items:
        cat = item.get(category_key) or UNCATEGORIZED
        groups.setdefault(cat, []).append(item)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def main():
    baseline = load_baseline()
    old_products = baseline.get("products", {})
    new_products = dict(old_products)

    product_urls = discover_product_urls()
    print(f"\nإجمالي روابط المنتجات المكتشفة من sitemap: {len(product_urls)}")

    if not product_urls:
        print("⚠️ ما قدرنا نكتشف أي رابط منتج من sitemap. نتوقف بدون فتح Issue.")
        return 1

    price_changes = []
    new_arrivals = []
    failed_count = 0
    total_scanned = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_product, pid, url): pid
            for pid, url in product_urls.items()
        }
        for i, future in enumerate(as_completed(futures), start=1):
            pid = futures[future]
            try:
                p = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{pid}] استثناء غير متوقع: {exc}")
                p = None

            if p is None:
                failed_count += 1
                continue

            total_scanned += 1
            old = old_products.get(pid)

            if old is None:
                new_arrivals.append(p)
            elif old.get("price") is not None and p["price"] is not None:
                try:
                    old_price_f = float(old["price"])
                    new_price_f = float(p["price"])
                except (TypeError, ValueError):
                    old_price_f = new_price_f = None
                if old_price_f is not None and old_price_f != new_price_f:
                    price_changes.append(
                        {
                            "name": p["name"],
                            "old_price": old_price_f,
                            "new_price": new_price_f,
                            "currency": p["currency"],
                            "category": p.get("category"),
                            "url": p["url"],
                        }
                    )

            new_products[pid] = {
                "name": p["name"],
                "price": p["price"],
                "currency": p["currency"],
                "availability": p.get("availability"),
                "category": p.get("category"),
                "url": p["url"],
            }

            if i % 500 == 0:
                print(f"  تقدّم: {i}/{len(product_urls)}")

    print(f"\nنجح قراءتهم: {total_scanned} — فشلوا: {failed_count}")

    # حماية من الإنذارات الكاذبة: لو أغلب/كل الطلبات فشلت (حظر/عطل)، لا نبني
    # تقرير غير موثوق ولا نحدّث baseline.
    if total_scanned == 0 or total_scanned < len(product_urls) * 0.5:
        print(
            "\n⚠️ نسبة فشل عالية جداً بقراءة المنتجات "
            f"({failed_count}/{len(product_urls)} فشلوا). "
            "على الأغلب حظر مؤقت من مكعب. لن يُفتح أي Issue ولن يُحدَّث baseline."
        )
        return 1

    baseline["generated_at"] = datetime.now(timezone.utc).isoformat()
    baseline["products"] = new_products
    save_baseline(baseline)

    total_alerts = len(price_changes) + len(new_arrivals)

    print(f"تغيّرات سعر: {len(price_changes)}")
    print(f"منتجات جديدة: {len(new_arrivals)}")

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as gh_out:
        gh_out.write(f"changes_count={total_alerts}\n")

    if total_alerts:
        lines = ["## 🔔 تحديثات مكعب اليوم\n"]

        if price_changes:
            lines.append("### 💰 المنتجات اللي تغيّر سعرها\n")
            for cat, cat_items in group_by_category(price_changes, "category"):
                cat_items.sort(
                    key=lambda c: abs(pct_change(c["old_price"], c["new_price"])),
                    reverse=True,
                )
                lines.append(f"#### {cat} ({len(cat_items)})\n")
                rows = []
                for c in cat_items:
                    pct = pct_change(c["old_price"], c["new_price"])
                    arrow = "⬆️" if c["new_price"] > c["old_price"] else "⬇️"
                    rows.append(
                        [
                            c["name"],
                            f"{c['old_price']:g} {c['currency']}",
                            f"{c['new_price']:g} {c['currency']}",
                            f"{arrow} {pct:+.1f}%",
                            f"[رابط]({c['url']})",
                        ]
                    )
                lines.append(
                    md_table(
                        ["المنتج", "السعر القديم", "السعر الجديد", "التغيّر", "الرابط"],
                        rows,
                    )
                )
                lines.append("")

        if new_arrivals:
            lines.append("### 🆕 منتجات جديدة\n")
            for cat, cat_items in group_by_category(new_arrivals, "category"):
                lines.append(f"#### {cat} ({len(cat_items)})\n")
                rows = [
                    [c["name"], f"{c['price']} {c['currency']}", f"[رابط]({c['url']})"]
                    for c in cat_items
                ]
                lines.append(md_table(["المنتج", "السعر", "الرابط"], rows))
                lines.append("")

        body = "\n".join(lines)
        with open("issue_body.md", "w", encoding="utf-8") as f:
            f.write(body)
        print("\n" + body[:3000])
    else:
        if os.path.exists("issue_body.md"):
            os.remove("issue_body.md")

    return 0


if __name__ == "__main__":
    sys.exit(main())
