# -*- coding: utf-8 -*-
"""本地输入图片的结果缓存。

缓存只避免对完全相同的本地图片重复发起图搜请求；不保存登录 Cookie，
也不下载或保存站点页面中的图片。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PLUGIN_ROOT / "storage" / "safe_run_cache.sqlite3"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_result_cache (
            fingerprint TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def fingerprint_image(path: Path) -> str:
    """按文件内容计算稳定指纹，改名或重新运行仍可命中缓存。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_cached_offers(fingerprint: str) -> list[dict[str, Any]] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT result_json FROM image_result_cache WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def save_cached_offers(
    fingerprint: str, image_path: Path, offers: list[dict[str, Any]]
) -> None:
    payload = json.dumps(offers, ensure_ascii=False, separators=(",", ":"))
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO image_result_cache(fingerprint, source_path, result_json)
            VALUES (?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
              source_path = excluded.source_path,
              result_json = excluded.result_json,
              created_at = CURRENT_TIMESTAMP
            """,
            (fingerprint, str(image_path), payload),
        )
