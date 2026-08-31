# -*- coding: utf-8 -*-
"""1688 offer 详情解析。"""

from __future__ import annotations

import re

from loguru import logger
from playwright.async_api import Page

from .browser import BrowserSession
from .excel_io import ShopOffer
from .image_search import SearchHit, _is_shipping_blob, format_price, parse_price

WEIGHT_RE = re.compile(
    r"(?:重量|净重|毛重)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:kg|KG|g|G|千克|克))",
    re.I,
)
SPEC_RE = re.compile(
    r"(?:规格|尺寸|型号)\s*[:：]?\s*([^\n\r]{2,40})",
)


async def _text_or_empty(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0:
                text = (await loc.inner_text(timeout=2000) or "").strip()
                if text:
                    return re.sub(r"\s+", " ", text)
        except Exception:  # noqa: BLE001
            continue
    return ""


async def _extract_price_from_page(page: Page, hit: SearchHit) -> tuple[str, float | None]:
    """从详情页提取商品标价，排除运费/起批数量误伤。"""
    blobs: list[str] = []

    # meta（商品价）
    try:
        meta = await page.locator('meta[property="product:price:amount"]').get_attribute(
            "content"
        )
        if meta:
            blobs.append(f"价格:¥{meta}")
    except Exception:  # noqa: BLE001
        pass

    # 精确选择器优先
    for sel in (
        ".price-text",
        ".price-num",
        "[class*='price-num']",
        "[class*='PriceNum']",
        ".offer-price",
        "[class*='price-original']",
        "span.price",
        "[class*='module-od-price']",
    ):
        t = await _text_or_empty(page, [sel])
        if t and not _is_shipping_blob(t):
            blobs.append(t)

    # 收集页面上带 ¥ 的短文本块（跳过运费模块）
    try:
        yen_texts = await page.evaluate(
            """() => {
              const out = [];
              const nodes = document.querySelectorAll(
                '[class*="price"], [class*="Price"], .module-od-price, .od-price'
              );
              for (const n of nodes) {
                const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!t || t.length > 80) continue;
                // 运费模块直接跳过
                if (/(运费|邮费|快递费|物流费|配送费)/.test(t) && !/(价格|单价|批发价|售价)/.test(t)) {
                  continue;
                }
                if (/[¥￥]/.test(t) || /价格|单价|批发价|售价/.test(t)) out.push(t);
              }
              return out.slice(0, 20);
            }"""
        )
        if isinstance(yen_texts, list):
            blobs.extend(str(x) for x in yen_texts if x and not _is_shipping_blob(str(x)))
    except Exception:  # noqa: BLE001
        pass

    # 不再把整段 body 丢进候选（里面常有运费 ¥，会污染 min）
    # 仅在上面都失败时，从 body 里抽「非运费」的 ¥
    body = ""
    try:
        body = await page.inner_text("body", timeout=5000)
    except Exception:  # noqa: BLE001
        body = ""

    candidates: list[float] = []
    source_blobs: list[str] = []
    for blob in blobs:
        if _is_shipping_blob(blob):
            continue
        v = parse_price(blob)
        if v is None:
            continue
        candidates.append(v)
        source_blobs.append(blob)

    if not candidates and body:
        # 最后回退：只解析 body 前部，且 parse_price 已会剔运费 ¥
        head = body[:1800]
        v = parse_price(head)
        if v is not None:
            candidates.append(v)
            source_blobs.append(head)

    if hit.price is not None:
        # 列表价仅作补充，不覆盖已抽到的详情价
        if not candidates:
            candidates.append(hit.price)

    if not candidates:
        return hit.price_text or "", hit.price

    value = min(candidates)
    shown = ""
    for blob in source_blobs:
        m = re.search(r"[¥￥]\s*" + re.escape(format_price(value)), blob)
        if m:
            # 左侧是运费标签则跳过（避免展示运费金额）
            left = blob[max(0, m.start() - 14) : m.start()]
            if re.search(r"(运费|邮费|快递费|物流费|配送费)\s*[:：]?\s*$", left):
                continue
            shown = m.group(0).replace(" ", "")
            break
    if not shown:
        shown = format_price(value, fallback=hit.price_text)
    return shown, value


async def fetch_offer_detail(session: BrowserSession, hit: SearchHit) -> ShopOffer:
    page = await session.start()
    url = hit.url
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await session.delay()
        await page.wait_for_timeout(1200)

        title = await _text_or_empty(
            page,
            [
                "h1",
                ".title-text",
                "[class*='title'] h1",
                "meta[property='og:title']",
            ],
        )
        if not title:
            title = hit.title

        if not title:
            try:
                title = await page.locator("meta[property='og:title']").get_attribute(
                    "content"
                ) or ""
            except Exception:  # noqa: BLE001
                title = ""

        price_out, price_val = await _extract_price_from_page(page, hit)
        # 同步回 hit，便于后续排序
        if price_val is not None:
            hit.price = price_val
            hit.price_text = price_out

        body = ""
        try:
            body = await page.inner_text("body", timeout=5000)
        except Exception:  # noqa: BLE001
            body = ""

        weight = ""
        wm = WEIGHT_RE.search(body)
        if wm:
            weight = wm.group(1).replace(" ", "")

        spec = ""
        sm = SPEC_RE.search(body)
        if sm:
            spec = sm.group(1).strip()
        if not spec:
            dm = re.search(
                r"\d+(?:\.\d+)?\s*[xX×\*]\s*\d+(?:\.\d+)?(?:\s*[xX×\*]\s*\d+(?:\.\d+)?)?",
                body,
            )
            if dm:
                spec = (
                    dm.group(0)
                    .replace(" ", "")
                    .replace("×", "*")
                    .replace("x", "*")
                    .replace("X", "*")
                )
                # 避免把尺寸后的克重粘进规格：440*130*34 22g
                spec = re.split(r"(?<=\d)(?=\d{2,}g\b)", spec, maxsplit=1)[0]

        offer = ShopOffer(
            url=url,
            sku_name=(title or hit.title or "")[:200],
            price=price_out,
            spec=spec[:80],
            weight=weight[:40],
        )
        logger.info("详情完成 {} price={} parsed={}", url, offer.price, price_val)
        return offer
    except Exception as exc:  # noqa: BLE001
        logger.exception("详情失败 {}: {}", url, exc)
        return ShopOffer(
            url=url,
            sku_name=hit.title,
            price=hit.price_text or format_price(hit.price),
        )


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def hits_to_top_offers(
    hits: list[SearchHit],
    *,
    top_n: int = 3,
    similarity_pool: int = 12,
) -> list[ShopOffer]:
    """用图搜结果页数据取 TopN，不进详情页。

    1) 页面顺序≈相似度，以前 similarity_pool 条为候选
    2) 用最相似若干条的价格中位数锚定，丢掉明显偏低的配件/贴纸价
    3) 在过滤后的候选里取最低价 TopN（同款比价）
    """
    if top_n <= 0:
        return []

    pool_n = max(int(similarity_pool), top_n)
    pool = [h for h in hits[:pool_n] if h.price is not None]
    if not pool:
        pool = [h for h in hits if h.price is not None]
    if not pool:
        return []

    prices = [float(h.price) for h in pool if h.price is not None]
    # 用最相似的前几条估「本品」价位，避免被池子里的 ¥0.2 贴纸带偏
    anchor_n = min(5, len(pool))
    anchor = _median([float(h.price) for h in pool[:anchor_n] if h.price is not None])
    pool_med = _median(prices)
    center = max(anchor, pool_med * 0.8)
    pmax = max(prices) if prices else 0.0

    # 双峰：前排混进贴纸/配件，后排才有主机等主商品 → 优先高价簇
    hi_cluster: list[SearchHit] = []
    if pmax >= 80 and pmax >= max(pool_med * 6, 80):
        hi_cut = max(pool_med * 4, pmax * 0.25, 50.0)
        hi_cluster = [h for h in pool if h.price is not None and h.price >= hi_cut]
        # 全量命中里再找一波高价簇（相似度稍低但像本品）
        if len(hi_cluster) < top_n:
            for h in hits:
                if h.price is None or h in hi_cluster:
                    continue
                if h.price >= hi_cut:
                    hi_cluster.append(h)
                if len(hi_cluster) >= max(top_n * 3, 9):
                    break

    if len(hi_cluster) >= top_n:
        filtered = hi_cluster
        low_cut = hi_cut if hi_cluster else 0.05
        logger.info(
            "检测到高低价双峰，改用高价簇比价 hi_cut≈{:.2f} n={}",
            low_cut,
            len(filtered),
        )
    else:
        # 低于锚定价 40% 的视为配件/错配
        low_cut = max(center * 0.4, 0.05)
        if center < 8:
            low_cut = max(pool_med * 0.5, 0.05)
        filtered = [h for h in pool if h.price is not None and h.price >= low_cut]

    if len(filtered) < top_n:
        # 回退：直接取最相似的 top_n（宁可相关，不要离谱低价）
        chosen = pool[:top_n]
        logger.info(
            "价格过滤后不足，回退相似度 Top{}（anchor≈{:.2f} low_cut≈{:.2f}）",
            top_n,
            center,
            low_cut,
        )
    else:
        chosen = sorted(filtered, key=lambda h: float(h.price or 1e18))[:top_n]
        logger.info(
            "同款比价选型 pool={} filtered={} anchor≈{:.2f} low_cut≈{:.2f}",
            len(pool),
            len(filtered),
            center,
            low_cut,
        )

    offers: list[ShopOffer] = []
    for hit in chosen:
        offers.append(
            ShopOffer(
                url=hit.url,
                sku_name=(hit.title or "")[:200],
                price=hit.price_text or format_price(hit.price),
                spec=(hit.shop_name or "")[:80],  # 店铺名称
                weight=(hit.extra or "")[:80],  # 备注/卡片摘要
            )
        )
        logger.info(
            "结果页选型 price={} shop={!r} title={!r} url={}",
            hit.price_text or hit.price,
            (hit.shop_name or "")[:30],
            (hit.title or "")[:40],
            hit.url,
        )
    return offers

async def fetch_top_offers(
    session: BrowserSession,
    hits: list[SearchHit],
    *,
    top_n: int = 3,
    visit_detail: bool = False,
) -> list[ShopOffer]:
    """按价格选 TopN。默认只用地图搜结果页数据；visit_detail=True 才进详情。"""
    if top_n <= 0:
        return []

    if not visit_detail:
        return hits_to_top_offers(hits, top_n=top_n)

    def hit_sort_key(h: SearchHit) -> tuple[bool, float]:
        return (h.price is None, h.price if h.price is not None else 1e18)

    ranked = sorted(hits, key=hit_sort_key)
    pool = ranked[: max(top_n * 3, top_n)]

    offers: list[ShopOffer] = []
    for hit in pool:
        offer = await fetch_offer_detail(session, hit)
        offers.append(offer)
        await session.delay()

    def offer_sort_key(o: ShopOffer) -> tuple[bool, float]:
        p = parse_price(o.price)
        return (p is None, p if p is not None else 1e18)

    offers.sort(key=offer_sort_key)
    with_price = [o for o in offers if parse_price(o.price) is not None]
    without = [o for o in offers if parse_price(o.price) is None]
    return (with_price + without)[:top_n]
