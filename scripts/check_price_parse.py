# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.image_search import parse_price  # noqa: E402

# 用 unicode 转义，避免 Windows 控制台/编码把中文弄坏
MOQ = "1\u4ef6\u8d77\u6279"  # 1件起批
SHIP = "\u8fd0\u8d39"  # 运费
POST = "\u90ae\u8d39"  # 邮费
EXPRESS = "\u5feb\u9012\u8d39"  # 快递费
PRICE = "\u4ef7\u683c"  # 价格
UNIT = "\u5355\u4ef7"  # 单价

assert parse_price(MOQ) is None
assert parse_price(f"{MOQ} \u00a528.50") == 28.5
assert parse_price(f"{SHIP} \u00a58") is None, parse_price(f"{SHIP} \u00a58")
assert parse_price(f"{EXPRESS}\u00a512.00") is None
assert parse_price(f"{PRICE}\u00a536.8 {SHIP}\u00a55") == 36.8
assert parse_price(f"\u00a5120.00 {SHIP}\u00a58") == 120.0
assert parse_price(f"{POST}\uff1a\u00a510 {UNIT}\uff1a\u00a545") == 45.0
assert parse_price(f"\u00a58{SHIP}") is None
print("OK")
