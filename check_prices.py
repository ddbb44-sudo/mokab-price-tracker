#!/usr/bin/env python3
"""
مراقب أسعار مكعب (mokab.com) — داش كام، اكسسوارات سيارة، براند بيسوس، براند DDPAI، منتجات التنقل.
يفحص الصفحات المستهدفة، يستخرج بيانات JSON-LD (schema.org Product) المضمنة في HTML،
يقارنها بخط أساس محفوظ في baseline.json، ويفتح GitHub Issue عند أي تغيّر بالسعر.

يعمل بمرحلتين لكل صفحة:
  1) طلب HTTP عادي بمكتبة requests (سريع ومجاني تماماً).
  2) إذا لم يُعثر على بيانات JSON-LD في الاستجابة (يعني الصفحة تحتاج JavaScript)،
     تشغيل متصفح Chromium بدون واجهة عبر Playwright كخطة بديلة.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

BASELINE_PATH = "mokab_price_baseline.json"

SOURCES = [
    ("https://mokab.com/DashCam/c51904340", "dashcam"),
    ("https://mokab.com/%D8%A7%D9%83%D8%B3%D8%B3%D9%88%D8%A7%D8%B1%D8%A7%D8%AA-%D8%A7%D9%84%D8%B3%D9%8A%D8%A7%D8%B1%D8%A9/c402772330", "car_accessories"),
    ("https://mokab.com/ar/baseus/brand-455286563", "baseus_brand"),
    ("https://mokab.com/ar/ddpai/brand-1171611633", "ddpai_brand"),
    ("https://mokab.com/ar/navee/brand-2096663475", "mobility_navee"),
    ("https://mokab.com/ar/airwheel/brand-1293951655", "mobility_airwheel"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.8",
}


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
                products.append(
                    {
                        "productID": pid,
                        "name": item.get("name"),
                        "price": offers.get("price"),
                        "originalPrice": price_spec.get("price", offers.get("price")),
                        "currency": offers.get("priceCurrency", "SAR"),
                        "url": item.get("url"),
                    }
                )
    return products


def fetch_via_requests(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        return extract_products_from_html(resp.text)
    except Exception as exc:  # noqa: BLE001
        print(f"  [requests] فشل: {exc}")
        return []


def fetch_via_playwright(url: str):
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


def load_baseline():
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"generated_at": None, "products": {}}


def save_baseline(baseline):
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)


def main():
    baseline = load_baseline()
    old_products = baseline.get("products", {})
    new_products = dict(old_products)  # نبدأ من القديم، نحدّث فوق
    changes = []
    total_scanned = 0
    used_fallback = []

    for url, category in SOURCES:
        print(f"فحص: {category} -> {url}")
        products = fetch_via_requests(url)
        if not products:
            print("  لم يُعثر على بيانات عبر الطلب العادي، تجربة المتصفح الآلي...")
            products = fetch_via_playwright(url)
            if products:
                used_fallback.append(category)

        seen_ids = set()
        for p in products:
            pid = p["productID"]
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            total_scanned += 1

            old = old_products.get(pid)
            if old and old.get("price") is not None and p["price"] is not None:
                if float(old["price"]) != float(p["price"]):
                    changes.append(
                        {
                            "name": p["name"],
                            "old_price": old["price"],
                            "new_price": p["price"],
                            "currency": p["currency"],
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
                "sourceCategories": sorted(categories),
            }

    baseline["generated_at"] = datetime.now(timezone.utc).isoformat()
    baseline["products"] = new_products
    save_baseline(baseline)

    print(f"\nإجمالي المنتجات المفحوصة (بدون تكرار لكل مصدر): {total_scanned}")
    print(f"عدد التغيّرات المكتشفة: {len(changes)}")
    if used_fallback:
        print(f"مصادر احتاجت متصفح آلي: {', '.join(used_fallback)}")

    # نخرج بيانات للخطوة التالية في GitHub Actions
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as gh_out:
        gh_out.write(f"changes_count={len(changes)}\n")

    if changes:
        changes.sort(
            key=lambda c: abs((c["new_price"] or 0) - (c["old_price"] or 0))
            / max(c["old_price"] or 1, 1),
            reverse=True,
        )
        lines = ["## 🔔 تغيّرات أسعار مكعب اليوم\n"]
        cat_names = {
            "dashcam": "داش كام",
            "car_accessories": "اكسسوارات سيارة",
            "baseus_brand": "براند بيسوس",
            "ddpai_brand": "براند DDPAI",
            "mobility_navee": "تنقل - NAVEE",
            "mobility_airwheel": "تنقل - Airwheel",
        }
        for c in changes:
            diff = (c["new_price"] or 0) - (c["old_price"] or 0)
            pct = (diff / c["old_price"] * 100) if c["old_price"] else 0
            arrow = "⬆️" if diff > 0 else "⬇️"
            lines.append(
                f"- {arrow} **{c['name']}** ({cat_names.get(c['category'], c['category'])})\n"
                f"  {c['old_price']} ← {c['new_price']} {c['currency']} "
                f"({pct:+.1f}%)\n  {c['url']}\n"
            )
        body = "\n".join(lines)
        with open("issue_body.md", "w", encoding="utf-8") as f:
            f.write(body)
        print("\n" + body)
    else:
        # لا تغييرات — لا حاجة لملف issue
        if os.path.exists("issue_body.md"):
            os.remove("issue_body.md")

    return 0


if __name__ == "__main__":
    sys.exit(main())
