# -*- coding: utf-8 -*-
import re
from pathlib import Path

html = Path(r"e:\1688chajian\storage\debug\no_hits_0.html").read_text(encoding="utf-8", errors="ignore")

# Find searchOfferItem blocks
pat = re.compile(
    r'class="[^"]*searchOfferItem[^"]*"[^>]*>',
    re.I,
)
ms = list(pat.finditer(html))
print("searchOfferItem tags", len(ms))
if ms:
    start = ms[0].start()
    # take until next item or 3000 chars
    chunk = html[start : start + 3500]
    Path(r"e:\1688chajian\storage\debug\_card0.txt").write_text(chunk, encoding="utf-8")
    print("card0 written", len(chunk))
    print("card0 has offerId", "offerId" in chunk)
    print("card0 detail.1688", "detail.1688" in chunk)
    # any numeric price-like
    print("numbers", re.findall(r">\s*(\d+\.?\d*)\s*<", chunk)[:20])
    # title row
    t = re.search(r'offerTitleRow[^<]*</[^>]+>([^<]{0,200})', chunk)
    print("title-ish", t.group(0)[:200] if t else None)

# Find all searchOfferItemLink hrefs
hrefs = re.findall(
    r'class="[^"]*searchOfferItemLink[^"]*"[^>]*href="([^"]+)"',
    html,
    re.I,
)
print("itemLink hrefs", len(hrefs), hrefs[:5])

# Maybe href is on parent - get surrounding for first item
if ms:
    # look backwards for <a
    i = ms[0].start()
    window = html[max(0, i - 500) : i + 200]
    ah = re.findall(r'<a[^>]+>', window, re.I)
    print("nearby a tags", ah[:5])

# price symbol class content
ps = re.findall(
    r'offerPreviewPrice[^"]*"[^>]*>(.*?)</div>',
    html[:200000],
    re.I | re.S,
)
print("price divs sample", [re.sub(r"<[^>]+>", "", x)[:40] for x in ps[:5]])

# title row contents
trs = re.findall(
    r'offerTitleRow--[^"]*"[^>]*>(.*?)</div>',
    html,
    re.I | re.S,
)
print("title rows", len(trs))
for t in trs[:5]:
    plain = re.sub(r"<[^>]+>", " ", t)
    plain = re.sub(r"\s+", " ", plain).strip()
    print(" TITLE:", plain[:100])

# company-like
for kw in ["company", "Company", "shopName", "loginId", "memberName", "offerDesc"]:
    print(kw, html.count(kw))
