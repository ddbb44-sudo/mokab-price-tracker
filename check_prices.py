#!/usr/bin/env python3
"""
فاحص شريحة (shard) من كتالوج مكعب (mokab.com).

الفكرة: بدل فحص ~5000 منتج من runner واحد (اللي سبّب حظر 429 من مكعب)، نوزّع
الكتالوج على عدة شرائح، كل شريحة تشتغل في job منفصل على GitHub Actions —
وبالتالي بعنوان IP مختلف — وبمعدل طلبات بطيء ولطيف جداً. النتيجة: تغطية شبه
كاملة بدون حظر، حتى لو أخذ وقتاً أطول (الدقة أهم من السرعة).

كل شريحة:
  1) تقرأ كل روابط المنتجات من sitemap مكعب (نفس القائمة لكل الشرائح).
  2) تأخذ نصيبها بالتناوب: sorted(pids)[SHARD_INDEX::SHARD_COUNT] — التناوب
     يضمن أن كل شريحة تحصل على خليط من كل الأقسام وليس قسماً واحداً.
  3) تفحص منتجاتها بالتتابع (worker واحد) مع:
       - جلسة HTTP واحدة (keep-alive) لتقليل الحمل وزمن الاتصال
       - تأخير تكيّفي: يبطئ تلقائياً عند أي 429 ويعود يتسارع تدريجياً عند
         النجاح المتواصل، مع احترام ترويسة Retry-After
       - إعادة محاولة لكل منتج مع انتظار تصاعدي
  4) تعيد الكرّة (كنسات/sweeps) على المنتجات اللي فشلت، مع تأخير أطول، حتى
     تصفير الفشل أو استنفاد عدد الكنسات — هذا اللي يرفع الدقة لأقصى حد.
  5) تحفظ النتيجة في shard_<INDEX>.json ليقوم build_report.py بدمجها لاحقاً.

ملاحظة مهمة: بعض روابط sitemap ترجع 200 لكن بصفحة عامة فارغة بلا أي بيانات
منتج (soft-404) — أي منتجات محذوفة/غير متاحة بقي رابطها في الخريطة. هذه
تُصنّف "غير متاح" وليس "فشل" لأن إعادة المحاولة معها لن تنفع أبداً.

المخرجات لكل منتج: الاسم، السعر الحالي (سعر العرض إن وُجد)، السعر الأصلي،
هل عليه عرض، حالة التوفر، الفئات (كما يصنّفها مكعب نفسه)، والرابط.
"""
import json
import os
import random
import re
import sys
import time

import requests

SITEMAP_INDEX_URL = "https://mokab.com/sitemap.xml"

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))
OUTPUT_PATH = os.environ.get("SHARD_OUTPUT", f"shard_{SHARD_INDEX}.json")

REQUEST_TIMEOUT = 25

# التأخير التكيّفي (بالثواني) بين كل طلب وآخر داخل الشريحة الواحدة
BASE_DELAY = 0.6
MIN_DELAY = 0.45
MAX_DELAY = 10.0
# عند 429: نضرب التأخير الحالي بهذا المعامل (تباطؤ فوري)
SLOWDOWN_FACTOR = 1.8
# بعد هذا العدد من النجاحات المتتالية نخفّف التأخير تدريجياً
SPEEDUP_AFTER_SUCCESSES = 40
SPEEDUP_FACTOR = 0.9

# محاولات لكل منتج داخل الكنسة الواحدة
ATTEMPTS_PER_PRODUCT = 3
BACKOFF_SECONDS = [5, 15, 35]

# عدد الكنسات: الأولى للكل، والباقي لإعادة محاولة اللي فشل فقط.
# بيانات التشغيل #8: الكنسة 1 استرجعت منتجاً واحداً، والكنستان 2 و3 صفر
# بينما كلفت كل واحدة ~25 دقيقة. فكنستا إعادة محاولة تكفيان تماماً، والباقي
# هدر وقت بلا أي مكسب في الدقة.
MAX_SWEEPS = 3
SWEEP_PAUSE_SECONDS = 90

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en;q=0.8",
    "Connection": "keep-alive",
}

PRODUCT_URL_RE = re.compile(r"/p(\d+)$")
# وجود أي وسم product:* يعني أن الصفحة صفحة منتج حقيقية
PRODUCT_META_RE = re.compile(r"property=[\"']product:")
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
SITEMAP_TAG_RE = re.compile(r"<sitemap>(.*?)</sitemap>", re.S)


def log(msg):
    """طباعة فورية — بدون flush ما تظهر السطور في سجل GitHub Actions إلا بالنهاية."""
    print(msg, flush=True)


class Throttle:
    """تأخير تكيّفي: يبطئ عند الحظر ويتسارع تدريجياً عند النجاح."""

    def __init__(self):
        self.delay = BASE_DELAY
        self.consecutive_ok = 0
        self.total_429 = 0

    def wait(self):
        time.sleep(self.delay + random.uniform(0, 0.25))

    def on_success(self):
        self.consecutive_ok += 1
        if self.consecutive_ok >= SPEEDUP_AFTER_SUCCESSES:
            self.consecutive_ok = 0
            self.delay = max(MIN_DELAY, self.delay * SPEEDUP_FACTOR)

    def on_rate_limited(self, retry_after=None):
        self.total_429 += 1
        self.consecutive_ok = 0
        self.delay = min(MAX_DELAY, self.delay * SLOWDOWN_FACTOR)
        if retry_after:
            try:
                time.sleep(min(120, float(retry_after)))
            except (TypeError, ValueError):
                pass


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url, timeout=REQUEST_TIMEOUT):
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def discover_product_urls(session):
    """يرجع dict: productID -> url لكل منتجات الموقع من خرائط الموقع."""
    try:
        index_xml = fetch_text(session, SITEMAP_INDEX_URL)
    except Exception as exc:  # noqa: BLE001
        log(f"فشل تحميل sitemap index: {exc}")
        return {}

    sub_sitemaps = []
    for block in SITEMAP_TAG_RE.findall(index_xml):
        m = LOC_RE.search(block)
        if not m:
            continue
        loc = m.group(1).strip()
        if "blog" in loc.lower():
            continue
        sub_sitemaps.append(loc)

    if not sub_sitemaps:
        sub_sitemaps = [SITEMAP_INDEX_URL]

    products = {}
    for sitemap_url in sub_sitemaps:
        try:
            xml = fetch_text(session, sitemap_url)
        except Exception as exc:  # noqa: BLE001
            log(f"  فشل قراءة {sitemap_url}: {exc}")
            continue
        for loc in LOC_RE.findall(xml):
            m = PRODUCT_URL_RE.search(loc)
            if m:
                products[m.group(1)] = loc
    return products


META_RE_TEMPLATE = r'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']*)["\']'


def extract_meta(html, prop):
    m = re.search(META_RE_TEMPLATE.format(prop=re.escape(prop)), html)
    if m:
        return m.group(1)
    alt = (
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']'
        + re.escape(prop)
        + r'["\']'
    )
    m = re.search(alt, html)
    return m.group(1) if m else None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_product(pid, url, html):
    """يستخرج بيانات المنتج من HTML الخام. يرجع dict أو None لو ما فيه سعر."""
    regular = to_float(extract_meta(html, "product:price:amount"))
    sale = to_float(extract_meta(html, "product:sale_price:amount"))
    currency = (
        extract_meta(html, "product:sale_price:currency")
        or extract_meta(html, "product:price:currency")
        or "SAR"
    )
    availability = extract_meta(html, "product:availability")
    title = extract_meta(html, "og:title")
    raw_cat = extract_meta(html, "product:category") or ""

    categories = [c.strip() for c in raw_cat.split(",") if c.strip()]

    # السعر اللي يدفعه العميل فعلياً
    if sale is not None:
        current = sale
    else:
        current = regular
    if current is None:
        return None

    # يوجد عرض فقط إذا السعر الأصلي أعلى فعلاً من سعر البيع
    on_offer = regular is not None and sale is not None and sale < regular

    if title:
        title = title.split("|")[0].strip()

    return {
        "productID": pid,
        "name": title or pid,
        "price": current,
        "regular_price": regular,
        "on_offer": on_offer,
        "currency": currency,
        "availability": availability,
        "categories": categories,
        "category": categories[0] if categories else None,
        "url": url,
    }


def scan_one(session, throttle, pid, url):
    """يحاول قراءة منتج واحد.
    يرجع ("ok", data) أو ("gone", None) أو ("fail", None)."""
    for attempt in range(ATTEMPTS_PER_PRODUCT):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
        except Exception:  # noqa: BLE001
            time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
            continue

        if resp.status_code == 200:
            throttle.on_success()
            html = resp.text
            data = parse_product(pid, url, html)
            throttle.wait()
            if data is not None:
                return "ok", data
            # صفحة رجعت 200 لكن بدون أي بيانات منتج إطلاقاً = صفحة عامة
            # (soft-404): المنتج محذوف/غير متاح رغم بقاء رابطه في sitemap.
            # هذي ليست مشكلة تقنية وإعادة المحاولة لن تنفع، فنعتبره "مفقود".
            if not PRODUCT_META_RE.search(html):
                return "gone", None
            log(f"  [{pid}] صفحة فيها بيانات منتج لكن بدون سعر مقروء.")
            return "fail", None

        if resp.status_code == 404:
            # المنتج لم يعد موجوداً — ليس فشلاً تقنياً
            throttle.on_success()
            throttle.wait()
            return "gone", None

        if resp.status_code == 429 or resp.status_code >= 500:
            throttle.on_rate_limited(resp.headers.get("Retry-After"))
            time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
            continue

        # أي حالة أخرى غير متوقعة
        throttle.wait()
        return "fail", None

    return "fail", None


def main():
    session = make_session()
    throttle = Throttle()

    all_products = discover_product_urls(session)
    if not all_products:
        log("⚠️ ما قدرنا نكتشف أي رابط منتج من sitemap.")
        return 1

    all_pids = sorted(all_products.keys())
    my_pids = all_pids[SHARD_INDEX::SHARD_COUNT]

    log(
        f"الشريحة {SHARD_INDEX + 1}/{SHARD_COUNT}: "
        f"{len(my_pids)} منتج من أصل {len(all_pids)} بالكتالوج."
    )

    scanned = {}
    gone = []
    pending = list(my_pids)

    for sweep in range(MAX_SWEEPS):
        if not pending:
            break
        if sweep > 0:
            log(
                f"\n🔁 كنسة إعادة محاولة رقم {sweep}: "
                f"{len(pending)} منتج لم يُقرأ بعد. ننتظر {SWEEP_PAUSE_SECONDS}ث..."
            )
            time.sleep(SWEEP_PAUSE_SECONDS)
            # نبدأ الكنسة الجديدة بتأخير أعلى لتفادي تكرار الحظر
            throttle.delay = min(MAX_DELAY, max(throttle.delay, BASE_DELAY) * 1.6)

        still_failing = []
        for i, pid in enumerate(pending, start=1):
            status, data = scan_one(session, throttle, pid, all_products[pid])
            if status == "ok":
                scanned[pid] = data
            elif status == "gone":
                gone.append(pid)
            else:
                still_failing.append(pid)

            if i % 100 == 0:
                log(
                    f"  تقدّم [كنسة {sweep}]: {i}/{len(pending)} — "
                    f"نجح: {len(scanned)} | غير متاح: {len(gone)} | "
                    f"فشل حالي: {len(still_failing)} | "
                    f"429 حتى الآن: {throttle.total_429} | "
                    f"التأخير: {throttle.delay:.2f}ث"
                )

        pending = still_failing
        log(
            f"نهاية الكنسة {sweep}: نجح تراكمياً {len(scanned)} — "
            f"غير متاح {len(gone)} — متبقٍ للإعادة {len(pending)}"
        )

    total = len(my_pids)
    ok = len(scanned)
    live_total = ok + len(pending)
    coverage = ok / live_total * 100 if live_total else 100.0

    log(
        f"\n✅ الشريحة {SHARD_INDEX + 1}/{SHARD_COUNT} انتهت: "
        f"قُرئ {ok} من {total} رابط | غير متاح/محذوف {len(gone)} | "
        f"فشل تقني {len(pending)} | "
        f"نسبة قراءة المتاح {coverage:.1f}% | إجمالي 429: {throttle.total_429}"
    )

    result = {
        "shard_index": SHARD_INDEX,
        "shard_count": SHARD_COUNT,
        "assigned": total,
        "products": scanned,
        "gone": gone,
        "failed": pending,
        "rate_limit_hits": throttle.total_429,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    log(f"حُفظت نتيجة الشريحة في {OUTPUT_PATH}")

    # ما نفشل الـ job حتى لو فيه فشل جزئي — job الدمج يقرر بناءً على التغطية
    # الكلية، والمنتجات اللي ما قُرئت ببساطة ما تُقارَن (فلا إنذارات كاذبة).
    return 0


if __name__ == "__main__":
    sys.exit(main())
