# -*- coding: utf-8 -*-
"""用已保存的 no_hits HTML 验证卡片解析（禁用 JS，避免页面脚本清空 DOM）。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from src.image_search import _collect_hits_from_page


async def main() -> None:
    html = (ROOT / "storage/debug/no_hits_0.html").read_text(encoding="utf-8", errors="ignore")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(java_script_enabled=False)
        page = await context.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        hits = await _collect_hits_from_page(page, limit=30)
        await browser.close()
    assert hits, "should parse hits from saved HTML"
    assert all(h.price is not None for h in hits)
    assert sum(1 for h in hits if h.title) >= 10
    assert sum(1 for h in hits if h.shop_name) >= 5
    msg = "OK %s first=%s %s %s" % (
        len(hits),
        (hits[0].price_text or "").replace("\xa5", "Y").replace("¥", "Y"),
        (hits[0].shop_name or "")[:20],
        (hits[0].title or "")[:30],
    )
    sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))


if __name__ == "__main__":
    asyncio.run(main())
