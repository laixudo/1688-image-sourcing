# -*- coding: utf-8 -*-
import re
from pathlib import Path

html = Path(r"e:\1688chajian\storage\debug\no_hits_0.html").read_text(encoding="utf-8", errors="ignore")

# offerId occurrences
oids = re.findall(r"offerId[=:\"]+(\d{8,})", html, re.I)
print("offerId count", len(oids), "unique", len(set(oids)), "sample", list(dict.fromkeys(oids))[:15])

# find context around offerlist
idx = html.lower().find("offerlist")
print("first offerlist idx", idx)
if idx >= 0:
    Path(r"e:\1688chajian\storage\debug\_offerlist_chunk.txt").write_text(
        html[idx : idx + 2500], encoding="utf-8"
    )

# data-offer / offer-id attributes
for pat in [
    r'data-offer-id=["\']?(\d{8,})',
    r'data-offerid=["\']?(\d{8,})',
    r'"offerId"\s*:\s*"?(\d{8,})"?',
    r'offerId=(\d{8,})',
    r'detail\.1688\.com[^"\']{0,80}',
    r'href=["\']([^"\']{0,120}1688[^"\']{0,80})["\']',
]:
    ms = re.findall(pat, html, re.I)
    print(pat[:50], "->", len(ms), "sample", ms[:5])

# Look for price near offerId
# grab a larger JSON-like blob
m = re.search(r"offerList[^\{]{0,40}(\[|\{)", html)
if m:
    start = m.start()
    Path(r"e:\1688chajian\storage\debug\_offerlist_jsonish.txt").write_text(
        html[start : start + 5000], encoding="utf-8"
    )
    print("jsonish written")

# Find elements with class containing offer
classes = set(re.findall(r'class="([^"]*offer[^"]*)"', html, re.I))
print("offer classes", list(classes)[:20])

# yen context samples
yen_pos = [m.start() for m in re.finditer(r"[¥￥]\s*\d+", html)]
print("yen positions", len(yen_pos))
if yen_pos:
    i = yen_pos[5] if len(yen_pos) > 5 else yen_pos[0]
    Path(r"e:\1688chajian\storage\debug\_yen_chunk.txt").write_text(
        html[max(0, i - 300) : i + 200], encoding="utf-8"
    )
