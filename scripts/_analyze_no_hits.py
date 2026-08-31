# -*- coding: utf-8 -*-
import re
from pathlib import Path

html = Path(r"e:\1688chajian\storage\debug\no_hits_0.html").read_text(encoding="utf-8", errors="ignore")
print("size", len(html))
print("offer links", len(re.findall(r"detail\.1688\.com/offer/\d+", html)))
print("offer/ ids", len(re.findall(r"offer/(\d{8,})", html)))
print("yen count", html.count("¥") + html.count("￥"))
print("空空如也", "空空如也" in html)
print("title sample", re.search(r"<title[^>]*>([^<]+)", html, re.I))
# price-like in json
for pat in [r'"price"\s*:\s*"?([\d.]+)"?', r'"offerId"\s*:\s*"?(\d+)"?', r'price["\']?\s*:\s*([\d.]+)']:
    ms = re.findall(pat, html)
    print(pat, "count", len(ms), "sample", ms[:8])

# sample hrefs
hrefs = re.findall(r'href=["\']([^"\']*offer[^"\']+)["\']', html, re.I)
print("href samples", hrefs[:10])

# check for __INIT_DATA / window data
for kw in ["__INIT", "offerList", "offerlist", "searchList", "itemList", "priceWap", "subject", "companyName", "loginId"]:
    print(kw, html.count(kw))

# extract a chunk around first offer id
m = re.search(r"offer/(\d{10,})", html)
if m:
    i = m.start()
    chunk = html[max(0, i - 200) : i + 400]
    Path(r"e:\1688chajian\storage\debug\_offer_chunk.txt").write_text(chunk, encoding="utf-8")
    print("chunk written", len(chunk))
