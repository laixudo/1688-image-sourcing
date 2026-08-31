from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
import unicodedata
from pathlib import Path

from src.errors import LocalImageIOError
from src.job_store import (
    METADATA_FALLBACK_FAILED,
    METADATA_FALLBACK_REUPLOADED,
    METADATA_NONE,
    METADATA_REJECTED,
    METADATA_TTL_EXPIRED,
    METADATA_VALID,
    ResultJobStore,
    build_image_fingerprint,
    normalize_task_item_id,
)
from src.recovery import PageState, classify_page, is_exact_login_location


class _Page:
    def __init__(self, url: str = "https://www.1688.com/") -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False

    def locator(self, _selector: str):
        return self

    async def count(self) -> int:
        return 0

    async def inner_text(self, **_kwargs) -> str:
        return ""


class RecoveryStateTest(unittest.TestCase):
    def test_task_identity_normalizes_case_separator_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            composed = "图" + unicodedata.normalize("NFC", "片é.JPG")
            path = root / composed
            path.write_bytes(b"x")
            identity = normalize_task_item_id(root, path)
            self.assertEqual(identity, unicodedata.normalize("NFC", composed).casefold())
            self.assertNotIn("\\", identity)

    def test_same_content_different_paths_have_different_fingerprints(self) -> None:
        content_hash = "a" * 64
        self.assertNotEqual(
            build_image_fingerprint("a.jpg", content_hash),
            build_image_fingerprint("b.jpg", content_hash),
        )

    def test_local_io_failure_is_persisted_without_recovery_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            image = root / "会消失.jpg"
            image.write_bytes(b"content")
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            store.prepare_items([image], prune=True)
            image.unlink()
            with self.assertRaises(LocalImageIOError):
                store.ensure_content_identity(seq=1, image_path=image)
            row = store._conn.execute("SELECT status,stage FROM items WHERE seq=1").fetchone()
            events = store._conn.execute("SELECT COUNT(*) FROM recovery_events").fetchone()[0]
            self.assertEqual(tuple(row), ("本地文件失败", "local_io_failed"))
            self.assertEqual(events, 0)
            store.close()

    def test_upload_metadata_roundtrip_and_enum(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            store.save_upload_metadata(
                task_item_id="中文/图片.jpg",
                metadata={"imageAddress": "https://cdn.example/a.jpg", "fileId": "42"},
                status=METADATA_VALID,
            )
            meta = store.get_upload_metadata("中文/图片.jpg")
            self.assertEqual(meta["imageAddress"], "https://cdn.example/a.jpg")
            self.assertEqual(meta["metadata_status"], METADATA_VALID)
            with self.assertRaises(ValueError):
                store.set_upload_metadata_status("中文/图片.jpg", "invented")
            store.close()

    def test_all_metadata_status_transitions_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            store.save_upload_metadata(
                task_item_id="a.jpg",
                metadata={"imageAddress": "https://cdn.example/a.jpg"},
            )
            for status in (
                METADATA_VALID,
                METADATA_TTL_EXPIRED,
                METADATA_REJECTED,
                METADATA_FALLBACK_REUPLOADED,
                METADATA_FALLBACK_FAILED,
                METADATA_NONE,
            ):
                store.set_upload_metadata_status("a.jpg", status)
                self.assertEqual(
                    store.get_upload_metadata("a.jpg")["metadata_status"], status
                )
            store.close()

    def test_mtime_only_change_preserves_result_and_metadata_then_applies_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            image = root / "只改时间.jpg"
            image.write_bytes(b"unchanged-content")
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            store.prepare_items([image], prune=True)
            task_id, old_hash, fingerprint = store.ensure_content_identity(
                seq=1, image_path=image
            )
            store.record_result(
                seq=1,
                fingerprint=fingerprint,
                status="已完成",
                offers=[{"url": "https://detail.1688.com/offer/1.html"}],
            )
            store.save_upload_metadata(
                task_item_id=task_id,
                metadata={"imageAddress": "https://cdn.example/unchanged.jpg"},
            )
            uploaded_at = store.get_upload_metadata(task_id)["uploaded_at_utc_ms"]
            stat = image.stat()
            os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

            counts = store.prepare_items([image], prune=True)
            row = store._conn.execute(
                "SELECT content_sha256,status,offers_json FROM items WHERE seq=1"
            ).fetchone()
            self.assertEqual(row[0], old_hash)
            self.assertEqual(row[1], "已完成")
            self.assertNotEqual(row[2], "[]")
            self.assertEqual(counts.success, 1)
            self.assertIsNotNone(store.get_upload_metadata(task_id))

            # Content identity is already confirmed. TTL is a separate decision:
            # within 15 minutes reuse; outside it mark expired and re-upload.
            self.assertIsNotNone(
                store.reusable_upload_metadata(
                    task_id, ttl_ms=15 * 60 * 1000,
                    now_utc_ms=uploaded_at + 14 * 60 * 1000,
                )
            )
            self.assertIsNone(
                store.reusable_upload_metadata(
                    task_id, ttl_ms=15 * 60 * 1000,
                    now_utc_ms=uploaded_at + 16 * 60 * 1000,
                )
            )
            self.assertEqual(
                store.get_upload_metadata(task_id)["metadata_status"],
                METADATA_TTL_EXPIRED,
            )
            store.close()

    def test_same_size_content_change_invalidates_result_and_upload(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            image = root / "内容变化.jpg"
            image.write_bytes(b"AAAA")
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            store.prepare_items([image], prune=True)
            task_id, old_hash, fingerprint = store.ensure_content_identity(seq=1, image_path=image)
            store.record_result(seq=1, fingerprint=fingerprint, status="已完成", offers=[{"url": "x"}])
            store.save_upload_metadata(
                task_item_id=task_id, metadata={"imageAddress": "https://cdn.example/old.jpg"}
            )
            old_stat = image.stat()
            image.write_bytes(b"BBBB")
            os.utime(image, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 5_000_000_000))

            counts = store.prepare_items([image], prune=True)
            row = store._conn.execute(
                "SELECT content_sha256,status,offers_json FROM items WHERE seq=1"
            ).fetchone()
            self.assertNotEqual(row[0], old_hash)
            self.assertEqual(row[1], "待处理")
            self.assertEqual(row[2], "[]")
            self.assertEqual(counts.success, 0)
            self.assertIsNone(store.get_upload_metadata(task_id))
            store.close()

    def test_refresh_reservation_is_sliding_and_stale_reserved_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            base = 2_000_000_000_000
            ids = []
            for offset in (0, 1_000, 2_000):
                event_id = store.reserve_refresh(
                    image_seq=1, event_type="page_reload", now_utc_ms=base + offset
                )
                self.assertIsNotNone(event_id)
                ids.append(event_id)
                store.finish_recovery_event(
                    event_id, outcome="completed", now_utc_ms=base + offset + 100
                )
            self.assertIsNone(
                store.reserve_refresh(
                    image_seq=2, event_type="page_reload", now_utc_ms=base + 3_000
                )
            )
            # Sliding window expires the first event, allowing exactly one slot.
            self.assertIsNotNone(
                store.reserve_refresh(
                    image_seq=2,
                    event_type="page_reload",
                    now_utc_ms=base + 30 * 60 * 1000 + 1,
                )
            )
            store.close()

    def test_reserved_refresh_becomes_timed_out_after_sixty_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            base = 2_000_000_000_000
            event_id = store.reserve_refresh(
                image_seq=7, event_type="page_reload", now_utc_ms=base
            )
            self.assertEqual(
                store.reconcile_stale_refresh_reservations(now_utc_ms=base + 60_001), 1
            )
            row = store._conn.execute(
                "SELECT outcome,finished_at_utc_ms FROM recovery_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("timed_out", base + 60_001))
            # timed_out still occupies one slot in the 30-minute quota.
            count = store._conn.execute(
                "SELECT COUNT(*) FROM recovery_events WHERE reserved_at_utc_ms>=?",
                (base - 1,),
            ).fetchone()[0]
            self.assertEqual(count, 1)
            store.close()

    def test_clock_rollback_threshold_and_rebase_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            base = 2_000_000_000_000
            self.assertEqual(store.check_wall_clock(now_utc_ms=base)[0], "CLOCK_OK")
            self.assertEqual(
                store.check_wall_clock(now_utc_ms=base - 30_000)[0], "CLOCK_CLAMPED"
            )
            self.assertEqual(
                store.check_wall_clock(now_utc_ms=base - 121_000)[0], "CLOCK_UNCERTAIN"
            )
            anchor = base + 1_000
            store.record_clock_rebased(now_utc_ms=anchor)
            self.assertEqual(
                int(store.get_meta("clock_conservative_until_utc_ms")),
                anchor + 30 * 60 * 1000,
            )
            store.close()

    def test_seventy_two_hour_compaction_keeps_event_row_and_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = ResultJobStore(root, top_n=3, database_path=root / "断点.sqlite3")
            now = 2_000_000_000_000
            old = now - 73 * 60 * 60 * 1000
            with store._conn:
                store._conn.execute(
                    "INSERT INTO recovery_events(event_id,image_seq,event_type,reserved_at_utc_ms,finished_at_utc_ms,outcome,details_json) VALUES('old',9,'page_reload',?,?, 'failed',?)",
                    (old, old + 100, json.dumps({"error": "detail"})),
                )
            self.assertEqual(store.archive_old_recovery_events(now_utc_ms=now), 1)
            row = store._conn.execute(
                "SELECT image_seq,event_type,reserved_at_utc_ms,finished_at_utc_ms,outcome,details_json FROM recovery_events WHERE event_id='old'"
            ).fetchone()
            self.assertEqual(tuple(row), (9, "page_reload", old, old + 100, "failed", "{}"))
            store.close()

    def test_legacy_checkpoint_migration_preserves_completed_progress_but_has_no_upload_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            image = root / "旧完成.jpg"
            image.write_bytes(b"legacy")
            stat = image.stat()
            database = root / "旧断点.sqlite3"
            conn = sqlite3.connect(database)
            conn.executescript(
                """
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE items(
                    image_path TEXT PRIMARY KEY,seq INTEGER NOT NULL,image_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '待处理',offers_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',scan_id TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                (("image_folder", str(root.resolve())), ("top_n", "3"), ("scope", "all")),
            )
            conn.execute(
                "INSERT INTO items(image_path,seq,image_name,file_size,mtime_ns,fingerprint,status,offers_json) VALUES(?,?,?,?,?,?,?,?)",
                (str(image), 1, image.name, stat.st_size, stat.st_mtime_ns, "legacy", "已完成", '[{"url":"x"}]'),
            )
            conn.commit()
            conn.close()

            store = ResultJobStore(
                root, top_n=3, database_path=database, require_existing=True
            )
            counts = store.prepare_items([image], prune=True)
            row = store._conn.execute(
                "SELECT status,content_sha256,image_fingerprint,task_item_id FROM items WHERE seq=1"
            ).fetchone()
            self.assertEqual(counts.success, 1)
            self.assertEqual(row[0], "已完成")
            self.assertEqual(row[1], "")
            self.assertEqual(row[2], "")
            self.assertTrue(row[3])
            self.assertIsNone(store.get_upload_metadata(row[3]))
            store.close()

    def test_login_matching_is_exact_and_network_classification(self) -> None:
        self.assertTrue(is_exact_login_location("https://login.1688.com/login.htm"))
        self.assertFalse(is_exact_login_location("https://www.1688.com/marketing/login-sale"))
        result = asyncio.run(
            classify_page(_Page(), RuntimeError("Page.evaluate: TypeError: Failed to fetch"))
        )
        self.assertEqual(result.state, PageState.NETWORK)


if __name__ == "__main__":
    unittest.main()
