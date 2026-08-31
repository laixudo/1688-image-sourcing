# -*- coding: utf-8 -*-
"""结果 Excel：从本地图片文件夹任务写出 Top3 店铺信息。"""

from __future__ import annotations

import os
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.workbook import Workbook as WorkbookT
from openpyxl.worksheet.worksheet import Worksheet

from .errors import ExcelLockedError

STATUS_DONE = "已完成"
STATUS_CACHED = "已完成（本地缓存）"
STATUS_FAILED = "失败"
STATUS_ENV_RETRY = "待重试(环境异常)"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# 第 2 行细表头（第 1 行为分组大标题）
HEADERS = [
    "状态",
    "图片文件名",
    "图片路径",
    "链接",
    "名称",
    "价格",
    "店铺",
    "备注",
    "链接",
    "名称",
    "价格",
    "店铺",
    "备注",
    "链接",
    "名称",
    "价格",
    "店铺",
    "备注",
    "错误信息",
]

_GROUP_HEADERS = (
    (1, 3, "图片信息", "FFE8EEF5"),
    (4, 8, "商品1 · 最低价", "FFFFE8D6"),
    (9, 13, "商品2", "FFE8F5EC"),
    (14, 18, "商品3", "FFF0E8F8"),
    (19, 19, "错误", "FFF5E8E8"),
)

# 数据区起始行（1=分组头，2=细表头）
_DATA_START_ROW = 3


@dataclass
class ShopOffer:
    url: str = ""
    sku_name: str = ""  # 商品名称
    price: str = ""
    spec: str = ""  # 复用为店铺名称
    weight: str = ""  # 复用为备注

    def as_tuple(self) -> tuple[str, str, str, str, str]:
        return (self.url, self.sku_name, self.price, self.spec, self.weight)


def list_image_files(folder: str | Path) -> list[Path]:
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"图片文件夹不存在: {root}")
    files = [
        p
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower())
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    return files


def resolve_output_excel(output_excel: str | Path, *, image_folder: str | Path) -> Path:
    """支持完整 xlsx 路径，或目录（自动生成带时间戳的文件名）。"""
    raw = Path(str(output_excel).strip() or ".")
    if raw.suffix.lower() == ".xlsx":
        raw.parent.mkdir(parents=True, exist_ok=True)
        return raw

    # 当作目录
    out_dir = raw if str(output_excel).strip() else Path(image_folder) / "1688_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return out_dir / f"1688_image_search_{stamp}.xlsx"


# 列宽：链接列只放「打开」，名称/店铺适中，整表更整齐
_COL_WIDTHS: dict[int, float] = {
    1: 9,
    2: 12,
    3: 26,
    4: 8,
    5: 34,
    6: 10,
    7: 22,
    8: 22,
    9: 8,
    10: 34,
    11: 10,
    12: 22,
    13: 22,
    14: 8,
    15: 34,
    16: 10,
    17: 22,
    18: 22,
    19: 22,
}

_GROUP_FILLS = {
    0: PatternFill("solid", fgColor="FFF7F9FC"),  # 图片信息
    1: PatternFill("solid", fgColor="FFFFF8F0"),  # 商品1
    2: PatternFill("solid", fgColor="FFF3FAF5"),  # 商品2
    3: PatternFill("solid", fgColor="FFFAF5FC"),  # 商品3
    4: PatternFill("solid", fgColor="FFFFF5F5"),  # 错误
}
_SUB_HEADER_FILL = PatternFill("solid", fgColor="FFF0F2F5")
_HEADER_FONT = Font(name="微软雅黑", bold=True, size=11, color="FF1F2937")
_SUB_HEADER_FONT = Font(name="微软雅黑", bold=True, size=10, color="FF4B5563")
_BODY_FONT = Font(name="微软雅黑", size=10, color="FF111827")
_PRICE_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFE65C00")
_LINK_FONT = Font(name="微软雅黑", size=10, color="FF0563C1", underline="single")
_THIN = Border(
    left=Side(style="thin", color="FFE5E7EB"),
    right=Side(style="thin", color="FFE5E7EB"),
    top=Side(style="thin", color="FFE5E7EB"),
    bottom=Side(style="thin", color="FFE5E7EB"),
)
_CENTER = Alignment(wrap_text=True, vertical="center", horizontal="center")
_LEFT = Alignment(wrap_text=True, vertical="center", horizontal="left")


def _group_index_for_col(col: int) -> int:
    if col <= 3:
        return 0
    if col <= 8:
        return 1
    if col <= 13:
        return 2
    if col <= 18:
        return 3
    return 4


def _apply_sheet_layout(ws: Worksheet) -> None:
    """双行分组表头 + 分色列块，打开即整齐可读。"""
    ws.freeze_panes = "D3"
    ws.row_dimensions[1].height = 26
    ws.row_dimensions[2].height = 22

    for start, end, title, color in _GROUP_HEADERS:
        fill = PatternFill("solid", fgColor=color)
        if start != end:
            ws.merge_cells(
                start_row=1,
                start_column=start,
                end_row=1,
                end_column=end,
            )
        cell = ws.cell(1, start)
        cell.value = title
        cell.font = _HEADER_FONT
        cell.fill = fill
        cell.alignment = _CENTER
        cell.border = _THIN
        for col in range(start, end + 1):
            c = ws.cell(1, col)
            c.fill = fill
            c.border = _THIN
            c.alignment = _CENTER

    for col, title in enumerate(HEADERS, start=1):
        cell = ws.cell(2, col)
        cell.value = title
        cell.font = _SUB_HEADER_FONT
        cell.fill = _SUB_HEADER_FILL
        cell.alignment = _CENTER
        cell.border = _THIN
        ws.column_dimensions[get_column_letter(col)].width = _COL_WIDTHS.get(col, 14)

    last = get_column_letter(len(HEADERS))
    ws.auto_filter.ref = f"A2:{last}2"


def _set_offer_link(cell, url: str) -> None:
    """链接列只显示「打开」，避免长 URL 撑乱版面。"""
    if url and str(url).startswith("http"):
        cell.value = "打开"
        cell.hyperlink = str(url)
        cell.font = _LINK_FONT
    else:
        cell.value = None


def _style_data_row(ws: Worksheet, row: int) -> None:
    ws.row_dimensions[row].height = 42
    price_cols = {6, 11, 16}
    center_cols = {1, 2, 4, 6, 9, 11, 14, 16}
    for col in range(1, len(HEADERS) + 1):
        cell = ws.cell(row, col)
        cell.fill = _GROUP_FILLS[_group_index_for_col(col)]
        cell.border = _THIN
        cell.alignment = _CENTER if col in center_cols else _LEFT
        if col in price_cols:
            cell.font = _PRICE_FONT
        elif col in (4, 9, 14):
            # 链接字体在 _set_offer_link 里设置
            if cell.value == "打开":
                cell.font = _LINK_FONT
            else:
                cell.font = _BODY_FONT
        else:
            cell.font = _BODY_FONT


def create_result_workbook() -> tuple[WorkbookT, Worksheet]:
    wb = Workbook()
    ws = wb.active
    ws.title = "图搜结果"
    _apply_sheet_layout(ws)
    return wb, ws


def append_result_row(
    ws: Worksheet,
    *,
    status: str,
    image_path: Path,
    shops: list[ShopOffer],
    error: str = "",
) -> int:
    # 跳过双行表头
    row = max(ws.max_row or 1, _DATA_START_ROW - 1) + 1
    if row < _DATA_START_ROW:
        row = _DATA_START_ROW

    ws.cell(row, 1).value = status
    ws.cell(row, 2).value = image_path.name
    ws.cell(row, 3).value = str(image_path)
    for i in range(3):
        offer = shops[i] if i < len(shops) else ShopOffer()
        base = 4 + i * 5
        _set_offer_link(ws.cell(row, base), offer.url)
        ws.cell(row, base + 1).value = offer.sku_name or None
        ws.cell(row, base + 2).value = offer.price or None
        ws.cell(row, base + 3).value = offer.spec or None
        ws.cell(row, base + 4).value = offer.weight or None
    ws.cell(row, 19).value = error or None
    _style_data_row(ws, row)
    return row


def result_sidecar_path(excel_path: str | Path) -> Path:
    path = Path(excel_path)
    return path.with_name(path.stem + ".plugin-result.xlsx")


def save_workbook(wb: WorkbookT, excel_path: str | Path) -> Path:
    """先写临时文件再 replace；占用则写旁路结果。"""
    path = Path(excel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing.xlsx")
    sidecar = result_sidecar_path(path)

    try:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        wb.save(tmp)
    except PermissionError as exc:
        raise ExcelLockedError(f"无法写入临时文件: {tmp}；原因: {exc}") from exc

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.35 * (attempt + 1))
        except OSError as exc:
            last_exc = exc
            time.sleep(0.35 * (attempt + 1))

    try:
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass
        os.replace(tmp, sidecar)
    except OSError:
        try:
            wb.save(sidecar)
        except Exception as exc:  # noqa: BLE001
            raise ExcelLockedError(
                f"目标文件占用且旁路也写失败: {path}；原因: {last_exc}"
            ) from exc

    raise ExcelLockedError(
        f"结果 Excel 正被占用，无法覆盖: {path}。"
        f"已写入旁路: {sidecar}。"
        + (f" 原因: {last_exc}" if last_exc else "")
    )


def _stream_cell(ws: Worksheet, value: Any, *, font: Font | None = None, fill: PatternFill | None = None, alignment: Alignment | None = None, hyperlink: str = ""):
    if value is None and not fill:
        return None
    cell = WriteOnlyCell(ws, value=value)
    if font is not None:
        cell.font = font
    if fill is not None:
        cell.fill = fill
    if alignment is not None:
        cell.alignment = alignment
    if hyperlink:
        cell.hyperlink = hyperlink
    return cell


def export_job_store_to_excel(
    database_path: str | Path,
    excel_path: str | Path,
    *,
    include_pending: bool = False,
) -> tuple[Path, int]:
    """从断点库流式导出结果，内存占用不随33万行线性增长。"""
    db_path = Path(database_path)
    path = Path(excel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing.xlsx")
    sidecar = result_sidecar_path(path)
    if tmp.exists():
        tmp.unlink(missing_ok=True)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("图搜结果")
    ws.freeze_panes = "D3"
    for col, width in _COL_WIDTHS.items():
        ws.column_dimensions[get_column_letter(col)].width = width

    group_titles = {start: title for start, _end, title, _color in _GROUP_HEADERS}
    first_row = []
    for col in range(1, len(HEADERS) + 1):
        value = group_titles.get(col)
        first_row.append(
            _stream_cell(
                ws,
                value,
                font=_HEADER_FONT if value else None,
                fill=_GROUP_FILLS[_group_index_for_col(col)],
                alignment=_CENTER,
            )
        )
    ws.append(first_row)
    ws.append(
        [
            _stream_cell(
                ws,
                title,
                font=_SUB_HEADER_FONT,
                fill=_SUB_HEADER_FILL,
                alignment=_CENTER,
            )
            for title in HEADERS
        ]
    )
    ws.auto_filter.ref = f"A2:{get_column_letter(len(HEADERS))}2"

    where = "" if include_pending else "WHERE status<>'待处理'"
    count = 0
    conn = sqlite3.connect(db_path)
    cursor = None
    try:
        cursor = conn.execute(
            f"""
            SELECT seq,image_name,image_path,status,offers_json,error
            FROM items {where} ORDER BY seq
            """
        )
        for _seq, image_name, image_path, status, offers_json, error in cursor:
            try:
                offers = json.loads(offers_json or "[]")
            except (TypeError, json.JSONDecodeError):
                offers = []
            values: list[Any] = [status, image_name, image_path]
            links: dict[int, str] = {}
            for offer_index in range(3):
                offer = offers[offer_index] if offer_index < len(offers) else {}
                url = str(offer.get("url") or "") if isinstance(offer, dict) else ""
                base_col = 4 + offer_index * 5
                values.extend(
                    [
                        "打开" if url.startswith("http") else None,
                        str(offer.get("sku_name") or "") or None if isinstance(offer, dict) else None,
                        str(offer.get("price") or "") or None if isinstance(offer, dict) else None,
                        str(offer.get("spec") or "") or None if isinstance(offer, dict) else None,
                        str(offer.get("weight") or "") or None if isinstance(offer, dict) else None,
                    ]
                )
                if url.startswith("http"):
                    links[base_col] = url
            values.append(error or None)
            row = []
            for col, value in enumerate(values, start=1):
                font = _PRICE_FONT if col in (6, 11, 16) else (_LINK_FONT if col in links else _BODY_FONT)
                row.append(
                    _stream_cell(
                        ws,
                        value,
                        font=font if value is not None else None,
                        alignment=_CENTER if col in {1, 2, 4, 6, 9, 11, 14, 16} else _LEFT,
                        hyperlink=links.get(col, ""),
                    )
                )
            ws.append(row)
            count += 1
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()

    try:
        wb.save(tmp)
    except PermissionError as exc:
        raise ExcelLockedError(f"无法写入流式导出临时文件: {tmp}；原因: {exc}") from exc

    try:
        os.replace(tmp, path)
        return path, count
    except OSError as exc:
        try:
            if sidecar.exists():
                sidecar.unlink()
            os.replace(tmp, sidecar)
        except OSError as sidecar_exc:
            raise ExcelLockedError(
                f"最终 Excel 被占用且旁路写入失败: {path}；原因: {sidecar_exc}"
            ) from sidecar_exc
        raise ExcelLockedError(
            f"最终 Excel 正被占用，已把完整结果写入旁路: {sidecar}。原因: {exc}"
        ) from exc


def summarize_folder(folder: str | Path) -> dict[str, Any]:
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"图片文件夹不存在: {root}")

    # 预览只需要总数和少量示例。不能复用 list_image_files：对几十万
    # 个文件构造 Path、逐个 stat 再完整排序会超过 AgentOffice 代理的
    # 单次请求窗口，宿主随后会误报“插件正在处理请求”。scandir 可利用
    # Windows 目录项自带的类型信息，并且保持常量内存。
    started = time.perf_counter()
    total = 0
    samples: list[str] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if os.path.splitext(entry.name)[1].lower() not in IMAGE_EXTS:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            total += 1
            if len(samples) < 50:
                samples.append(entry.name)

    return {
        "folder": str(root),
        "total": total,
        "files": samples,
        "truncated": total > len(samples),
        "scan_elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
