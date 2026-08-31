# -*- coding: utf-8 -*-
import re
from pathlib import Path

html = Path(r"e:\1688chajian\storage\debug\no_hits_0.html").read_text(encoding="utf-8", errors="ignore")

# shopName contexts
idx = html.find("shopName")
print("shopName idx", idx)
Path(r"e:\1688chajian\storage\debug\_shop_chunk.txt").write_text(html[idx-200:idx+800], encoding="utf-8")

# title row with more context - include parent
m = re.search(r'class="[^"]*offerTitleRow[^"]*"', html)
if m:
    Path(r"e:\1688chajian\storage\debug\_title_ctx.txt").write_text(
        html[max(0, m.start()-800): m.start()+1200], encoding="utf-8"
    )

# price related classes content
for cls in ["offerPreviewPrice", "PriceSymbol", "price--", "mainPrice", "salePrice"]:
    print(cls, html.count(cls))

# Find digits near price classes
for m in re.finditer(r'class="[^"]*(?:Price|price)[^"]*"', html):
    chunk = html[m.start(): m.start()+300]
    if re.search(r"\d+\.\d+|\d{2,}", chunk):
        Path(r"e:\1688chajian\storage\debug\_price_cls.txt").write_text(chunk, encoding="utf-8")
        print("found price cls chunk")
        break

# How is offer linked? look for data attributes on searchOfferWrapper
m = re.search(r'class="[^"]*searchOfferWrapper[^"]*"[^>]*>', html)
print("wrapper", m.group(0)[:300] if m else None)
if m:
    Path(r"e:\1688chajian\storage\debug\_wrapper.txt").write_text(
        html[m.start(): m.start()+2500], encoding="utf-8"
    )

# extract shopName values
shops = re.findall(r'shopName[^>]*>?\s*([^<]{2,60})', html)
print("shops raw", shops[:8])
shops2 = re.findall(r'"shopName"\s*:\s*"([^"]+)"', html)
print("shops json", shops2[:8])

# maybe react props in script
scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.I | re.S)
print("scripts", len(scripts), "lens", sorted([len(s) for s in scripts], reverse=True)[:10])
for s in scripts:
    if "offerId" in s and len(s) > 1000:
        Path(r"e:\1688chajian\storage\debug\_script_offers.txt").write_text(s[:8000], encoding="utf-8")
        print("script with offerId len", len(s))
        oids = re.findall(r'offerId["\']?\s*[:=]\s*["\']?(\d{8,})', s)
        print("oids in script", len(oids), oids[:5])
        break
