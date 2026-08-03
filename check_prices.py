#!/usr/bin/env python3
"""
مراقب أسعار ومنتجات مكعب (mokab.com) — داش كام، اكسسوارات سيارة، براند بيسوس، براند DDPAI، منتجات التنقل.

يفحص الصفحات المستهدفة (مع تصفّح كل صفحات الترقيم pagination لكل قسم)، يستخرج بيانات
JSON-LD (schema.org Product) المضمنة في HTML، يقارنها بخط أساس محفوظ في baseline.json،
ويفتح GitHub Issue عند أي من هذه التغييرات:
  - تغيّر السعر الحالي (offers.price)
  - منتج جديد ظهر لأول مرة
  - منتج كان موجوداً واختفى تماماً من كل الأقسام المفحوصة (غالباً نفاد/إيقاف)
  - تغيّر نسبة الخصم (السعر الأصلي originalPrice تغيّر حتى لو السعر النهائي ما تغيّر)
  - تغيّر حالة التوفر (متوفر / نفد)

يعمل بمرحلتين لكل صفحة:
  1) طلب HTTP عادي بمكتبة requests (سريع ومجاني تماماً).
  2) إذا لم يُعثر على بيانات JSON-LD في الاستجابة (يعني الصفحة تحتاج JavaScript)،
     تشغيل متصفح Chromium بدون واجهة عبر Playwright كخطة بديلة.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

BASELINE_PATH = "mokab_price_baseline.json"
MAX_PAGES_PER_SOURCE = 8  # سقف أمان لعدد صفحات الترقيم لكل قسم

SOURCES = [
    ("https://mokab.com/DashCam/c51904340", "dashcam"),
    ("https://mokab.com/%D8%A7%D9%83%D8%B3%D8%B3%D9%88%D8%A7%D8%B1%D8%A7%D8%AA-%D8%A7%D9%84%D8%B3%D9%8A%D8%A7%D8%B1%D8%A9/c402772330", "car_accessories"),
    ("https://mokab.com/ar/baseus/brand-455286563", "baseus_brand"),
    ("https://mokab.com/ar/ddpai/brand-1171611633", "ddpai_brand"),
    ("https://mokab.com/ar/navee/brand-2096663475", "mobility_navee"),
    ("https://mokab.com/ar/airwheel/brand-1293951655", "mobility_airwheel"),
]

CAT_NAMES = {
    "dashcam": "داش كام",
    "car_accessories": "اكسسوارات سيارة",
    "baseus_brand": "براند بيسوس",
    "ddpai_brand": "براند DDPAI",
    "mobility_navee": "تنقل - NAVEE",
    "mobility_airwheel": "تنقل - Airwheel",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}


def with_page_param(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    q["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def extract_products_from_html(html: str):
    """يبحث عن كل <script type="application/ld+json"> ويستخرج عناصر ItemList من نوع Product."""
    products = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S,
    ):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for block in candidates:
            if not isinstance(block, dict):
                continue
            if block.get("@type") != "ItemList":
                continue
            for li in block.get("itemListElement", []):
                item = li.get("item") if isinstance(li, dict) else None
                if not item or item.get("@type") != "Product":
                    continue
                offers = item.get("offers", {}) or {}
                price_spec = offers.get("priceSpecification", {}) or {}
                pid = str(item.get("productID") or "")
                if not pid:
                    continue
                availability = (offers.get("availability") or "").rsplit("/", 1)[-1] or None
                products.append(
                    {
                        "productID": pid,
                        "name": item.get("name"),
                        "price": offers.get("price"),
                        "originalPrice": price_spec.get("price", offers.get("price")),
                        "currency": offers.get("priceCurrency", "SAR"),
                        "url": item.get("url"),
                        "availability": availability,
                    }
                )
    return products


def fetch_page_via_requests(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        return extract_products_from_html(resp.text)
    except Exception as exc:  # noqa: BLE001
        print(f"  [requests] فشل: {exc}")
        return []


def fetch_page_via_playwright(url: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [playwright] غير مثبت — تخطي الخطة البديلة.")
        return []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=45000, wait_until="networkidle")
            html = page.content()
            browser.close()
        return extract_products_from_html(html)
    except Exception as exc:  # noqa: BLE001
        print(f"  [playwright] فشل: {exc}")
        return []


def fetch_all_products_for_source(base_url: str, category: str):
    """يتصفح صفحات الترقيم (pagination) للقسم حتى ما يلقى منتجات جديدة أو يوصل السقف."""
    all_products = {}
    used_fallback = False
    prev_ids = None

    for page_num in range(1, MAX_PAGES_PER_SOURCE + 1):
        page_url = with_page_param(base_url, page_num)
        products = fetch_page_via_requests(page_url)
        if not products:
            products = fetch_page_via_playwright(page_url)
            if products:
                used_fallback = True

        if not products:
            break  # صفحة فاضية أو فشل — نوقف الترقيم لهذا القسم

        page_ids = {p["productID"] for p in products}
        if prev_ids is not None and page_ids <= prev_ids:
            # نفس المنتجات تتكرر (يعني ما فيه ترقيم فعلي أو وصلنا آخر صفحة)
            break
        prev_ids = page_ids

        for p in products:
            all_products[p["productID"]] = p

        if page_num < MAX_PAGES_PER_SOURCE:
            time.sleep(1)  # أدب مع السيرفر

    return list(all_products.values()), used_fallback


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
    return (new_v - old_v) / old_v * 100


def main():
    baseline = load_baseline()
    old_products = baseline.get("products", {})
    new_products = dict(old_products)

    price_changes = []
    new_arrivals = []
    disappeared = []
    discount_changes = []
    availability_changes = []

    total_scanned = 0
    used_fallback_sources = []
    all_seen_ids = set()
    seen_by_category = {cat: set() for _, cat in SOURCES}

    for url, category in SOURCES:
        print(f"فحص: {category} -> {url}")
        products, used_fallback = fetch_all_products_for_source(url, category)
        if used_fallback:
            used_fallback_sources.append(category)
        print(f"  عدد المنتجات المكتشفة: {len(products)}")

        for p in products:
            pid = p["productID"]
            all_seen_ids.add(pid)
            seen_by_category[category].add(pid)
            total_scanned += 1

            old = old_products.get(pid)

            if old is None:
                new_arrivals.append(
                    {
                        "name": p["name"],
                        "price": p["price"],
                        "currency": p["currency"],
                        "category": category,
                        "url": p["url"],
                    }
                )
            else:
                if old.get("price") is not None and p["price"] is not None:
                    if float(old["price"]) != float(p["price"]):
                        price_changes.append(
                            {
                                "name": p["name"],
                                "old_price": old["price"],
                                "new_price": p["price"],
                                "currency": p["currency"],
                                "category": category,
                                "url": p["url"],
                            }
                        )

                old_orig = old.get("originalPrice")
                new_orig = p.get("originalPrice")
                if (
                    old_orig is not None
                    and new_orig is not None
                    and float(old_orig) != float(new_orig)
                ):
                    discount_changes.append(
                        {
                            "name": p["name"],
                            "old_original": old_orig,
                            "new_original": new_orig,
                            "current_price": p["price"],
                            "currency": p["currency"],
                            "category": category,
                            "url": p["url"],
                        }
                    )

                old_avail = old.get("availability")
                new_avail = p.get("availability")
                if old_avail and new_avail and old_avail != new_avail:
                    availability_changes.append(
                        {
                            "name": p["name"],
                            "old_status": old_avail,
                            "new_status": new_avail,
                            "category": category,
                            "url": p["url"],
                        }
                    )

            existing = new_products.get(pid, {})
            categories = set(existing.get("sourceCategories", []))
            categories.add(category)
            new_products[pid] = {
                "name": p["name"],
                "price": p["price"],
                "originalPrice": p["originalPrice"],
                "currency": p["currency"],
                "url": p["url"],
                "availability": p.get("availability"),
                "sourceCategories": sorted(categories),
            }

    # اختفاء منتج: كان موجوداً بخط الأساس، ومو موجود إطلاقاً في أي قسم بهذا الفحص
    for pid, old in old_products.items():
        if pid not in all_seen_ids:
            disappeared.append(
                {
                    "name": old.get("name"),
                    "last_price": old.get("price"),
                    "currency": old.get("currency", "SAR"),
                    "categories": old.get("sourceCategories", []),
                    "url": old.get("url"),
                }
            )

    baseline["generated_at"] = datetime.now(timezone.utc).isoformat()
    baseline["products"] = new_products
    save_baseline(baseline)

    total_alerts = (
        len(price_changes)
        + len(new_arrivals)
        + len(disappeared)
        + len(discount_changes)
        + len(availability_changes)
    )

    print(f"\nإجمالي المنتجات المفحوصة: {total_scanned}")
    print(f"تغيّرات سعر: {len(price_changes)}")
    print(f"منتجات جديدة: {len(new_arrivals)}")
    print(f"منتجات اختفت: {len(disappeared)}")
    print(f"تغيّرات خصم: {len(discount_changes)}")
    print(f"تغيّرات توفر: {len(availability_changes)}")
    if used_fallback_sources:
        print(f"مصادر احتاجت متصفح آلي: {', '.join(used_fallback_sources)}")

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as gh_out:
        gh_out.write(f"changes_count={total_alerts}\n")

    if total_alerts:
        lines = ["## 🔔 تحديثات مكعب اليوم\n"]

        if price_changes:
            price_changes.sort(
                key=lambda c: abs(pct_change(c["old_price"], c["new_price"])),
                reverse=True,
            )
            lines.append("### 💰 تغيّرات الأسعار\n")
            for c in price_changes:
                pct = pct_change(c["old_price"], c["new_price"])
                arrow = "⬆️" if c["new_price"] > c["old_price"] else "⬇️"
                lines.append(
                    f"- {arrow} **{c['name']}** ({CAT_NAMES.get(c['category'], c['category'])})\n"
                    f"  {c['old_price']} ← {c['new_price']} {c['currency']} ({pct:+.1f}%)\n"
                    f"  {c['url']}\n"
                )

        if discount_changes:
            lines.append("\n### 🏷️ تغيّرات نسبة الخصم/العرض\n")
            for c in discount_changes:
                lines.append(
                    f"- **{c['name']}** ({CAT_NAMES.get(c['category'], c['category'])})\n"
                    f"  السعر الأصلي: {c['old_original']} ← {c['new_original']} {c['currency']}"
                    f" (السعر الحالي: {c['current_price']})\n  {c['url']}\n"
                )

        if availability_changes:
            lines.append("\n### 📦 تغيّرات حالة التوفر\n")
            for c in availability_changes:
                lines.append(
                    f"- **{c['name']}** ({CAT_NAMES.get(c['category'], c['category'])})\n"
                    f"  {c['old_status']} ← {c['new_status']}\n  {c['url']}\n"
                )

        if new_arrivals:
            lines.append("\n### 🆕 منتجات جديدة\n")
            for c in new_arrivals:
                lines.append(
                    f"- **{c['name']}** ({CAT_NAMES.get(c['category'], c['category'])}) — "
                    f"{c['price']} {c['currency']}\n  {c['url']}\n"
                )

        if disappeared:
            lines.append("\n### ❌ منتجات اختفت (على الأغلب نفد المخزون أو أُوقفت)\n")
            for c in disappeared:
                cats = "، ".join(CAT_NAMES.get(x, x) for x in c["categories"])
                lines.append(
                    f"- **{c['name']}** ({cats}) — آخر سعر معروف: {c['last_price']} {c['currency']}\n"
                    f"  {c['url']}\n"
                )

        body = "\n".join(lines)
        with open("issue_body.md", "w", encoding="utf-8") as f:
            f.write(body)
        print("\n" + body)
    else:
        if os.path.exists("issue_body.md"):
            os.remove("issue_body.md")

    return 0


if __name__ == "__main__":
    sys.exit(main())
