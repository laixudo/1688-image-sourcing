# -*- coding: utf-8 -*-
"""最小自检：文件夹扫图 + 结果表表头写入。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.excel_io import (  # noqa: E402
    ShopOffer,
    append_result_row,
    create_result_workbook,
    list_image_files,
    resolve_output_excel,
    save_workbook,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "imgs"
        folder.mkdir()
        (folder / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        (folder / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (folder / "note.txt").write_text("x", encoding="utf-8")

        files = list_image_files(folder)
        assert [f.name for f in files] == ["a.jpg", "b.png"], files

        out = resolve_output_excel(str(Path(tmp) / "out.xlsx"), image_folder=folder)
        assert out.name == "out.xlsx"

        auto = resolve_output_excel("", image_folder=folder)
        assert auto.parent.name == "1688_results"
        assert auto.suffix == ".xlsx"

        wb, ws = create_result_workbook()
        append_result_row(
            ws,
            status="已完成",
            image_path=files[0],
            shops=[
                ShopOffer(
                    url="https://detail.1688.com/offer/1.html",
                    sku_name="demo",
                    price="1.2",
                )
            ],
        )
        written = save_workbook(wb, out)
        assert written.is_file()
        print("OK", written)


if __name__ == "__main__":
    main()
