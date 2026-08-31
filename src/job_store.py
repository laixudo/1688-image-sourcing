# -*- coding: utf-8 -*-
"""大批量任务的 SQLite 断点存储。"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

PENDING = "待处理"
SUCCESS_STATUSES = {"已完成", "已完成（本地缓存）"}

METADATA_NONE = "none"
METADATA_VALID = "valid"
METADATA_TTL_EXPIRED = "ttl_expired"
METADATA_REJECTED = "rejected"
METADATA_FALLBACK_REUPLOADED = "fallback_reuploaded"
METADATA_FALLBACK_FAILED = "fallback_failed"
METADATA_STATUSES = {
    METADATA_NONE,
    METADATA_VALID,
    METADATA_TTL_EXPIRED,
    METADATA_REJECTED,
    METADATA_FALLBACK_REUPLOADED,
    METADATA_FALLBACK_FAILED,
}

STAGE_PENDING = "pending"
STAGE_HASHED = "hashed"
STAGE_UPLOADED = "uploaded"
STAGE_RESULT_OPENED = "result_opened"
STAGE_COMPLETED = "completed"
STAGE_LOCAL_IO_FAILED = "local_io_failed"

REFRESH_EVENT_TYPES = {"page_reload", "new_page"}
REFRESH_OUTCOMES = {"reserved", "completed", "failed", "timed_out"}
REFRESH_WINDOW_MS = 30 * 60 * 1000
REFRESH_RESERVATION_TIMEOUT_MS = 60 * 1000
RECOVERY_ARCHIVE_AFTER_MS = 72 * 60 * 60 * 1000

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_JOBS_DIR = _PLUGIN_ROOT / "storage" / "jobs"


def normalize_task_item_id(image_folder: str | Path, image_path: str | Path) -> str:
    """Return a stable Windows task identity without collapsing real filenames.

    Rules are deliberately explicit: relative to the configured root, Unicode NFC,
    separators converted to '/', dot segments resolved by Path, and case-folded
    because the supported Windows filesystem is case-insensitive. Meaningful spaces
    and full-width characters are preserved to avoid false collisions.
    """
    root = Path(image_folder).resolve()
    path = Path(image_path).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"图片不在任务目录内: {path}") from exc
    return unicodedata.normalize("NFC", relative.as_posix()).casefold()


def stream_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def build_image_fingerprint(task_item_id: str, content_sha256: str) -> str:
    raw = f"{task_item_id}\0{content_sha256}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _safe_filename_part(value: str, *, fallback: str = "图片") -> str:
    """Make a readable Windows-safe filename part while preserving Chinese text."""
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value.strip())
    value = re.sub(r"\s+", "_", value).strip(" ._")
    return (value or fallback)[:80]


def create_checkpoint_path(image_folder: str | Path, *, max_images: int) -> Path:
    """Return a new, readable and collision-safe checkpoint path.

    Example: 断点_E_图片_20260827_103015_123456_最多2000张.sqlite3
    """
    resolved = str(Path(image_folder).resolve())
    folder_part = _safe_filename_part(resolved.replace(":", "_"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    limit_part = f"最多{max_images}张" if max_images > 0 else "全部图片"
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return _JOBS_DIR / f"断点_{folder_part}_{timestamp}_{limit_part}.sqlite3"


@dataclass(frozen=True)
class StoredCounts:
    total: int = 0
    processed: int = 0
    success: int = 0
    failed: int = 0
    cache_hits: int = 0


class ResultJobStore:
    """每张图片一行持久化，服务退出后可从未完成位置继续。"""

    def __init__(
        self,
        image_folder: str | Path,
        *,
        top_n: int,
        scope: str = "all",
        database_path: str | Path | None = None,
        require_existing: bool = False,
    ) -> None:
        resolved_folder = str(Path(image_folder).resolve())
        if database_path:
            self.path = Path(database_path).expanduser().resolve()
        else:
            max_images = int(scope.removeprefix("max=")) if scope.startswith("max=") else 0
            self.path = create_checkpoint_path(resolved_folder, max_images=max_images)
        if self.path.suffix.lower() not in {".sqlite3", ".sqlite", ".db"}:
            raise ValueError("断点库必须使用 .sqlite3、.sqlite 或 .db 扩展名")
        existed = self.path.is_file()
        if require_existing and not existed:
            raise FileNotFoundError(f"填写的断点库不存在: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        except sqlite3.Error as exc:
            raise ValueError(f"无法打开断点库 {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._create_schema()
            if existed:
                self._validate_compatibility(resolved_folder, top_n=top_n, scope=scope)
            self.set_meta("image_folder", resolved_folder)
            self.set_meta("top_n", str(top_n))
            self.set_meta("scope", scope)
            self.set_meta("processing_mode", "per_file_network")
            self.set_meta("content_cache_enabled", "false")
        except Exception:
            self._conn.close()
            raise

    def _validate_compatibility(self, image_folder: str, *, top_n: int, scope: str) -> None:
        """Refuse a user-selected checkpoint belonging to another task."""
        stored_folder = self.get_meta("image_folder")
        stored_top_n = self.get_meta("top_n")
        stored_scope = self.get_meta("scope")
        if not stored_folder or not stored_top_n:
            raise ValueError(f"所选文件不是本插件创建的有效断点库: {self.path}")
        if str(Path(stored_folder).resolve()).casefold() != image_folder.casefold():
            raise ValueError(
                f"断点库的图片目录不匹配：库内={stored_folder}，当前={image_folder}"
            )
        if int(stored_top_n) != int(top_n):
            raise ValueError(f"断点库的最低价店铺数为 {stored_top_n}，当前设置为 {top_n}")
        # Legacy v3 databases have no scope metadata; keep them selectable for migration.
        if stored_scope and stored_scope != scope:
            raise ValueError(f"断点库的处理范围为 {stored_scope}，当前设置为 {scope}")

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    image_path TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    image_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '待处理',
                    offers_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    scan_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    task_item_id TEXT PRIMARY KEY,
                    image_address TEXT NOT NULL DEFAULT '',
                    image_id TEXT NOT NULL DEFAULT '',
                    file_id TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    candidates TEXT NOT NULL DEFAULT '',
                    uploaded_at_utc_ms INTEGER NOT NULL DEFAULT 0,
                    metadata_status TEXT NOT NULL DEFAULT 'none',
                    fallback_reupload_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS recovery_events (
                    event_id TEXT PRIMARY KEY,
                    image_seq INTEGER NOT NULL DEFAULT 0,
                    event_type TEXT NOT NULL,
                    reserved_at_utc_ms INTEGER NOT NULL,
                    finished_at_utc_ms INTEGER,
                    outcome TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
                CREATE INDEX IF NOT EXISTS idx_items_seq ON items(seq);
                CREATE INDEX IF NOT EXISTS idx_recovery_occurred
                    ON recovery_events(reserved_at_utc_ms);
                CREATE INDEX IF NOT EXISTS idx_recovery_image_seq
                    ON recovery_events(image_seq);
                CREATE INDEX IF NOT EXISTS idx_recovery_type_time
                    ON recovery_events(event_type,reserved_at_utc_ms);
                """
            )
            self._migrate_items_schema()

    def _migrate_items_schema(self) -> None:
        """Add 0.6.21 columns without invalidating existing Chinese checkpoints."""
        existing = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(items)").fetchall()
        }
        additions = {
            "task_item_id": "TEXT NOT NULL DEFAULT ''",
            "relative_path": "TEXT NOT NULL DEFAULT ''",
            "content_sha256": "TEXT NOT NULL DEFAULT ''",
            "image_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "stage": "TEXT NOT NULL DEFAULT 'pending'",
            "local_error_kind": "TEXT NOT NULL DEFAULT ''",
            "rehash_required": "INTEGER NOT NULL DEFAULT 0",
            "source_changed": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in additions.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE items ADD COLUMN {column} {declaration}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_task_item_id "
            "ON items(task_item_id) WHERE task_item_id<>''"
        )

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def prepare_items(self, images: Sequence[Path], *, prune: bool) -> StoredCounts:
        """同步本轮文件清单；未变化图片保留已完成结果。"""
        scan_id = str(time.time_ns())
        image_folder = self.get_meta("image_folder")
        rows: list[tuple[int, str, str, int, int, str, str, str]] = []
        for seq, path in enumerate(images, start=1):
            task_item_id = normalize_task_item_id(image_folder, path)
            relative_path = unicodedata.normalize("NFC", Path(path).resolve().relative_to(Path(image_folder).resolve()).as_posix())
            try:
                stat = path.stat()
                file_size, mtime_ns, local_error = int(stat.st_size), int(stat.st_mtime_ns), ""
            except OSError as exc:
                file_size, mtime_ns = -1, 0
                local_error = f"{type(exc).__name__}: {exc}"
            rows.append((seq, str(path), path.name, file_size, mtime_ns, scan_id, task_item_id, relative_path, local_error))

        sql = """
            INSERT INTO items(seq,image_path,image_name,file_size,mtime_ns,scan_id,task_item_id,relative_path,error,status,stage,local_error_kind)
            VALUES(?,?,?,?,?,?,?,?,?,CASE WHEN ?='' THEN '待处理' ELSE '本地文件失败' END,CASE WHEN ?='' THEN 'pending' ELSE 'local_io_failed' END,CASE WHEN ?='' THEN '' ELSE 'stat_failed' END)
            ON CONFLICT(image_path) DO UPDATE SET
                seq=excluded.seq,
                image_name=excluded.image_name,
                task_item_id=excluded.task_item_id,
                relative_path=excluded.relative_path,
                fingerprint=CASE
                    WHEN items.file_size=excluded.file_size AND items.mtime_ns=excluded.mtime_ns
                    THEN items.fingerprint
                    WHEN items.file_size=excluded.file_size AND items.content_sha256<>''
                    THEN items.fingerprint ELSE '' END,
                status=CASE
                    WHEN items.file_size=excluded.file_size AND items.mtime_ns=excluded.mtime_ns
                    THEN items.status
                    WHEN items.file_size=excluded.file_size AND items.content_sha256<>''
                    THEN items.status ELSE '待处理' END,
                offers_json=CASE
                    WHEN items.file_size=excluded.file_size AND items.mtime_ns=excluded.mtime_ns
                    THEN items.offers_json
                    WHEN items.file_size=excluded.file_size AND items.content_sha256<>''
                    THEN items.offers_json ELSE '[]' END,
                error=CASE
                    WHEN excluded.local_error_kind<>'' THEN excluded.error
                    WHEN items.file_size=excluded.file_size AND items.mtime_ns=excluded.mtime_ns
                    THEN items.error
                    WHEN items.file_size=excluded.file_size AND items.content_sha256<>''
                    THEN items.error ELSE '' END,
                content_sha256=CASE
                    WHEN items.file_size=excluded.file_size AND items.mtime_ns=excluded.mtime_ns
                    THEN items.content_sha256
                    WHEN items.file_size=excluded.file_size AND items.content_sha256<>''
                    THEN items.content_sha256 ELSE '' END,
                image_fingerprint=CASE
                    WHEN items.file_size=excluded.file_size AND items.mtime_ns=excluded.mtime_ns
                    THEN items.image_fingerprint
                    WHEN items.file_size=excluded.file_size AND items.content_sha256<>''
                    THEN items.image_fingerprint ELSE '' END,
                stage=CASE
                    WHEN excluded.local_error_kind<>'' THEN 'local_io_failed'
                    WHEN items.file_size=excluded.file_size AND items.mtime_ns=excluded.mtime_ns
                    THEN items.stage
                    WHEN items.file_size=excluded.file_size AND items.content_sha256<>''
                    THEN items.stage ELSE 'pending' END,
                rehash_required=CASE
                    WHEN excluded.local_error_kind='' AND items.file_size=excluded.file_size
                         AND items.mtime_ns<>excluded.mtime_ns AND items.content_sha256<>''
                    THEN 1 ELSE 0 END,
                source_changed=CASE
                    WHEN excluded.local_error_kind<>'' THEN 1
                    WHEN items.file_size<>excluded.file_size THEN 1
                    WHEN items.mtime_ns<>excluded.mtime_ns AND items.content_sha256='' THEN 1
                    ELSE 0 END,
                local_error_kind=excluded.local_error_kind,
                file_size=excluded.file_size,
                mtime_ns=excluded.mtime_ns,
                scan_id=excluded.scan_id,
                updated_at=CURRENT_TIMESTAMP
        """
        expanded_rows = [
            (*row[:8], row[8], row[8], row[8], row[8]) for row in rows
        ]
        with self._lock, self._conn:
            for start in range(0, len(expanded_rows), 5000):
                self._conn.executemany(sql, expanded_rows[start : start + 5000])
            # Size changes (or legacy rows without a comparable hash) are known
            # changes immediately. Never let their old imageAddress survive.
            self._conn.execute(
                "DELETE FROM uploads WHERE task_item_id IN "
                "(SELECT task_item_id FROM items WHERE source_changed=1)"
            )
            if prune:
                self._conn.execute("DELETE FROM items WHERE scan_id<>?", (scan_id,))
            # 上次的失败/环境异常在新一轮允许重试；成功与缓存结果直接续用。
            placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
            self._conn.execute(
                f"UPDATE items SET status=?,error='' WHERE status NOT IN ({placeholders}) AND local_error_kind=''",
                (PENDING, *sorted(SUCCESS_STATUSES)),
            )
        # mtime-only changes remain provisionally intact until a full hash proves
        # whether content changed. This runs in the caller's worker thread.
        self._revalidate_mtime_only_changes()
        return self.counts()

    def _revalidate_mtime_only_changes(self) -> None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,image_path,task_item_id,content_sha256 FROM items "
                "WHERE rehash_required=1 ORDER BY seq"
            ).fetchall()
        for row in rows:
            seq = int(row[0])
            path = Path(str(row[1]))
            task_item_id = str(row[2])
            old_hash = str(row[3] or "")
            try:
                new_hash = stream_sha256(path)
            except OSError as exc:
                message = f"{type(exc).__name__}: {exc}"
                with self._lock, self._conn:
                    self._conn.execute(
                        "UPDATE items SET status='本地文件失败',stage=?,local_error_kind='read_failed',"
                        "error=?,rehash_required=0,source_changed=1,updated_at=CURRENT_TIMESTAMP WHERE seq=?",
                        (STAGE_LOCAL_IO_FAILED, message, seq),
                    )
                    self._conn.execute(
                        "DELETE FROM uploads WHERE task_item_id=?", (task_item_id,)
                    )
                continue

            if new_hash == old_hash:
                # Only mtime changed. Keep completion, offers, fingerprint and
                # upload metadata exactly as they were.
                with self._lock, self._conn:
                    self._conn.execute(
                        "UPDATE items SET rehash_required=0,source_changed=0,"
                        "local_error_kind='',updated_at=CURRENT_TIMESTAMP WHERE seq=?",
                        (seq,),
                    )
                continue

            fingerprint = build_image_fingerprint(task_item_id, new_hash)
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE items SET content_sha256=?,image_fingerprint=?,fingerprint=?,"
                    "status=?,offers_json='[]',error='',stage=?,local_error_kind='',"
                    "rehash_required=0,source_changed=1,updated_at=CURRENT_TIMESTAMP WHERE seq=?",
                    (new_hash, fingerprint, fingerprint, PENDING, STAGE_HASHED, seq),
                )
                self._conn.execute(
                    "DELETE FROM uploads WHERE task_item_id=?", (task_item_id,)
                )

    def ensure_content_identity(self, *, seq: int, image_path: str | Path) -> tuple[str, str, str]:
        """Hash one imminent item and atomically persist its stable identity."""
        with self._lock:
            row = self._conn.execute(
                "SELECT task_item_id,file_size,mtime_ns,content_sha256 FROM items WHERE seq=?",
                (seq,),
            ).fetchone()
        if row is None:
            raise KeyError(f"断点库不存在第 {seq} 项")
        path = Path(image_path)
        try:
            stat = path.stat()
            size, mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
            saved_hash = str(row[3] or "")
            if saved_hash and size == int(row[1]) and mtime_ns == int(row[2]):
                content_hash = saved_hash
            else:
                content_hash = stream_sha256(path)
        except OSError as exc:
            message = f"{type(exc).__name__}: {exc}"
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE items SET status='本地文件失败',stage=?,local_error_kind='read_failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE seq=?",
                    (STAGE_LOCAL_IO_FAILED, message, seq),
                )
            from .errors import LocalImageIOError
            raise LocalImageIOError(f"本地图片读取失败 {path}: {message}") from exc
        task_item_id = str(row[0])
        fingerprint = build_image_fingerprint(task_item_id, content_hash)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE items SET file_size=?,mtime_ns=?,content_sha256=?,image_fingerprint=?,fingerprint=?,stage=?,local_error_kind='',updated_at=CURRENT_TIMESTAMP WHERE seq=?",
                (size, mtime_ns, content_hash, fingerprint, fingerprint, STAGE_HASHED, seq),
            )
        return task_item_id, content_hash, fingerprint

    def set_stage(self, *, seq: int, stage: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE items SET stage=?,updated_at=CURRENT_TIMESTAMP WHERE seq=?", (stage, seq)
            )

    def get_upload_metadata(self, task_item_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM uploads WHERE task_item_id=?", (task_item_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["imageAddress"] = result.pop("image_address")
        result["imageId"] = result.pop("image_id")
        result["fileId"] = result.pop("file_id")
        if result.pop("candidates", ""):
            result["imageAddressCandidates"] = row["candidates"]
        return result

    def reusable_upload_metadata(
        self, task_item_id: str, *, ttl_ms: int, now_utc_ms: int | None = None
    ) -> dict[str, Any] | None:
        """Apply TTL only after content identity has already been confirmed."""
        metadata = self.get_upload_metadata(task_item_id)
        if not metadata:
            return None
        if metadata.get("metadata_status") not in {
            METADATA_VALID,
            METADATA_FALLBACK_REUPLOADED,
        }:
            return None
        now = int(now_utc_ms or time.time() * 1000)
        age_ms = now - int(metadata.get("uploaded_at_utc_ms") or 0)
        if 0 <= age_ms <= int(ttl_ms):
            return metadata
        self.set_upload_metadata_status(task_item_id, METADATA_TTL_EXPIRED)
        return None

    def save_upload_metadata(
        self, *, task_item_id: str, metadata: dict[str, Any], status: str = METADATA_VALID,
        fallback_reupload: bool = False,
    ) -> None:
        if status not in METADATA_STATUSES:
            raise ValueError(f"未知 metadata_status: {status}")
        now_ms = int(time.time() * 1000)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO uploads(task_item_id,image_address,image_id,file_id,url,candidates,uploaded_at_utc_ms,metadata_status,fallback_reupload_count)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_item_id) DO UPDATE SET
                    image_address=excluded.image_address,image_id=excluded.image_id,file_id=excluded.file_id,
                    url=excluded.url,candidates=excluded.candidates,uploaded_at_utc_ms=excluded.uploaded_at_utc_ms,
                    metadata_status=excluded.metadata_status,
                    fallback_reupload_count=uploads.fallback_reupload_count+excluded.fallback_reupload_count,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    task_item_id, str(metadata.get("imageAddress") or ""),
                    str(metadata.get("imageId") or ""), str(metadata.get("fileId") or ""),
                    str(metadata.get("url") or ""), str(metadata.get("imageAddressCandidates") or ""),
                    now_ms, status, 1 if fallback_reupload else 0,
                ),
            )

    def set_upload_metadata_status(self, task_item_id: str, status: str) -> None:
        if status not in METADATA_STATUSES:
            raise ValueError(f"未知 metadata_status: {status}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE uploads SET metadata_status=?,updated_at=CURRENT_TIMESTAMP WHERE task_item_id=?",
                (status, task_item_id),
            )

    def reconcile_stale_refresh_reservations(self, *, now_utc_ms: int | None = None) -> int:
        now = int(now_utc_ms or time.time() * 1000)
        cutoff = now - REFRESH_RESERVATION_TIMEOUT_MS
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE recovery_events SET outcome='timed_out',finished_at_utc_ms=? "
                "WHERE outcome='reserved' AND reserved_at_utc_ms<=?",
                (now, cutoff),
            )
        return int(cursor.rowcount)

    def reserve_refresh(self, *, image_seq: int, event_type: str, now_utc_ms: int | None = None,
                        max_in_window: int = 3) -> str | None:
        if event_type not in REFRESH_EVENT_TYPES:
            raise ValueError(f"未知刷新事件: {event_type}")
        now = int(now_utc_ms or time.time() * 1000)
        conservative_until = int(self.get_meta("clock_conservative_until_utc_ms", "0") or 0)
        if now < conservative_until:
            return None
        self.reconcile_stale_refresh_reservations(now_utc_ms=now)
        with self._lock, self._conn:
            active = self._conn.execute(
                "SELECT 1 FROM recovery_events WHERE outcome='reserved' LIMIT 1"
            ).fetchone()
            count = int(self._conn.execute(
                "SELECT COUNT(*) FROM recovery_events WHERE event_type IN ('page_reload','new_page') AND reserved_at_utc_ms>=?",
                (now - REFRESH_WINDOW_MS,),
            ).fetchone()[0])
            if active or count >= max_in_window:
                return None
            event_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO recovery_events(event_id,image_seq,event_type,reserved_at_utc_ms,outcome) VALUES(?,?,?,?, 'reserved')",
                (event_id, image_seq, event_type, now),
            )
            return event_id

    def check_wall_clock(self, *, now_utc_ms: int | None = None) -> tuple[str, int]:
        """Persist wall time and detect a backwards jump larger than two minutes."""
        now = int(now_utc_ms or time.time() * 1000)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='last_wall_utc_ms'"
            ).fetchone()
            previous = int(row[0]) if row else 0
            delta = now - previous if previous else 0
            if previous and delta < -120_000:
                return "CLOCK_UNCERTAIN", delta
            # Small rollback is clamped: never move the persisted watermark back.
            persisted = max(now, previous)
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES('last_wall_utc_ms',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(persisted),),
            )
        return ("CLOCK_CLAMPED" if delta < 0 else "CLOCK_OK"), delta

    def record_clock_rebased(self, *, now_utc_ms: int | None = None) -> None:
        now = int(now_utc_ms or time.time() * 1000)
        event_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO recovery_events(event_id,image_seq,event_type,reserved_at_utc_ms,finished_at_utc_ms,outcome,details_json) "
                "VALUES(?,0,'clock_rebased',?,?,'completed','{}')",
                (event_id, now, now),
            )
            for key, value in (
                ("last_wall_utc_ms", now),
                ("clock_conservative_until_utc_ms", now + REFRESH_WINDOW_MS),
            ):
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

    def finish_recovery_event(self, event_id: str, *, outcome: str, details: dict[str, Any] | None = None,
                              now_utc_ms: int | None = None) -> None:
        if outcome not in REFRESH_OUTCOMES - {"reserved"}:
            raise ValueError(f"未知恢复结果: {outcome}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE recovery_events SET outcome=?,finished_at_utc_ms=?,details_json=? WHERE event_id=?",
                (outcome, int(now_utc_ms or time.time() * 1000), json.dumps(details or {}, ensure_ascii=False), event_id),
            )

    def archive_old_recovery_events(self, *, now_utc_ms: int | None = None) -> int:
        """Compact details older than 72h while retaining timestamps for audit."""
        cutoff = int(now_utc_ms or time.time() * 1000) - RECOVERY_ARCHIVE_AFTER_MS
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE recovery_events SET details_json='{}' WHERE reserved_at_utc_ms<? AND details_json<>'{}'",
                (cutoff,),
            )
        return int(cursor.rowcount)

    def completed_sequences(self) -> set[int]:
        placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT seq FROM items WHERE status IN ({placeholders})",
                tuple(sorted(SUCCESS_STATUSES)),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def record_result(
        self,
        *,
        seq: int,
        fingerprint: str,
        status: str,
        offers: Iterable[dict[str, Any]],
        error: str = "",
    ) -> None:
        payload = json.dumps(list(offers), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE items SET fingerprint=?,status=?,offers_json=?,error=?,updated_at=CURRENT_TIMESTAMP
                WHERE seq=?
                """,
                (fingerprint, status, payload, error, seq),
            )
            if status in SUCCESS_STATUSES:
                self._conn.execute(
                    "UPDATE items SET stage=? WHERE seq=?", (STAGE_COMPLETED, seq)
                )

    def counts(self) -> StoredCounts:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status,COUNT(*) AS n FROM items GROUP BY status"
            ).fetchall()
        grouped = {str(row[0]): int(row[1]) for row in rows}
        total = sum(grouped.values())
        success = sum(grouped.get(status, 0) for status in SUCCESS_STATUSES)
        cache_hits = grouped.get("已完成（本地缓存）", 0)
        processed = total - grouped.get(PENDING, 0)
        return StoredCounts(
            total=total,
            processed=processed,
            success=success,
            failed=max(0, processed - success),
            cache_hits=cache_hits,
        )

    def daily_requests(self, local_date: str) -> int:
        return int(self.get_meta(f"network_requests:{local_date}", "0") or 0)

    def increment_daily_requests(self, local_date: str) -> int:
        key = f"network_requests:{local_date}"
        with self._lock, self._conn:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            value = int(row[0]) + 1 if row else 1
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
        return value

    def close(self) -> None:
        with self._lock:
            self._conn.close()
