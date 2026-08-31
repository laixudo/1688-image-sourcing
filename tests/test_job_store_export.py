from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from src import job_store, pipeline
from src.excel_io import ShopOffer, export_job_store_to_excel
from src.job_store import ResultJobStore


class _FakeSessionForJobStore:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class JobStoreExportTest(unittest.TestCase):
    def test_daily_limit_then_resume_to_complete_export(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            images = [root / f"daily-{i}.jpg" for i in range(3)]
            for image in images:
                image.write_bytes(image.name.encode())
            output = root / "daily-result.xlsx"
            cfg = pipeline.PipelineConfig(
                image_folder=str(root),
                output_excel=str(output),
                daily_network_limit=2,
            )

            async def fake_process(_session, image_path, _cfg):
                offer = ShopOffer(
                    url=f"https://detail.1688.com/{image_path.name}",
                    sku_name=image_path.name,
                    price="1.00",
                    spec="店铺",
                )
                return {"shop_objects": [offer]}

            async def no_delay(_seconds, *, should_stop=None):  # noqa: ARG001
                return True

            patches = (
                patch.object(job_store, "_JOBS_DIR", root / "jobs"),
                patch.object(pipeline, "process_one_image", side_effect=fake_process),
                patch.object(pipeline, "_cooperative_delay", side_effect=no_delay),
            )
            with patches[0], patches[1], patches[2]:
                first = asyncio.run(
                    pipeline.run_pipeline(cfg, session=_FakeSessionForJobStore())
                )
                self.assertEqual(first["processed"], 2)
                self.assertEqual(first["pending"], 1)
                self.assertTrue(first["partialExport"])
                self.assertEqual(first["exportedRows"], 2)
                self.assertTrue(output.exists())

                store = ResultJobStore(
                    root,
                    top_n=3,
                    database_path=cfg.checkpoint_db,
                    require_existing=True,
                )
                today = datetime.now().astimezone().date().isoformat()
                store.set_meta(f"network_requests:{today}", "0")
                store.close()

                second = asyncio.run(
                    pipeline.run_pipeline(cfg, session=_FakeSessionForJobStore())
                )
                self.assertEqual(second["processed"], 3)
                self.assertEqual(second["pending"], 0)
                self.assertEqual(second["resumed"], 2)
                self.assertEqual(second["exportedRows"], 3)
                self.assertTrue(output.is_file())

    def test_identical_file_contents_are_each_searched(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            images = [root / "copy-a.jpg", root / "copy-b.jpg", root / "copy-c.jpg"]
            for image in images:
                image.write_bytes(b"exactly-the-same-image-content")
            output = root / "per-file-result.xlsx"
            cfg = pipeline.PipelineConfig(
                image_folder=str(root),
                output_excel=str(output),
                daily_network_limit=10,
            )
            searched: list[str] = []

            async def fake_process(_session, image_path, _cfg):
                searched.append(image_path.name)
                return {
                    "shop_objects": [
                        ShopOffer(
                            url=f"https://detail.1688.com/{image_path.name}",
                            sku_name=image_path.name,
                            price="1.00",
                            spec="店铺",
                        )
                    ]
                }

            async def no_delay(_seconds, *, should_stop=None):  # noqa: ARG001
                return True

            with (
                patch.object(job_store, "_JOBS_DIR", root / "jobs"),
                patch.object(pipeline, "process_one_image", side_effect=fake_process),
                patch.object(pipeline, "_cooperative_delay", side_effect=no_delay),
            ):
                result = asyncio.run(
                    pipeline.run_pipeline(cfg, session=_FakeSessionForJobStore())
                )

            self.assertEqual(searched, [image.name for image in images])
            self.assertEqual(result["processed"], 3)
            self.assertEqual(result["success"], 3)
            self.assertEqual(result["cache_hits"], 0)
            self.assertEqual(result["exportedRows"], 3)

    def test_user_interruption_exports_completed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            images = [root / f"stop-{i}.jpg" for i in range(4)]
            for image in images:
                image.write_bytes(image.name.encode())
            output = root / "stopped-result.xlsx"
            cfg = pipeline.PipelineConfig(
                image_folder=str(root),
                output_excel=str(output),
                daily_network_limit=10,
            )
            completed = 0

            async def fake_process(_session, image_path, _cfg):
                nonlocal completed
                completed += 1
                return {
                    "shop_objects": [
                        ShopOffer(
                            url=f"https://detail.1688.com/{image_path.name}",
                            sku_name=image_path.name,
                            price="1.00",
                            spec="店铺",
                        )
                    ]
                }

            async def no_delay(_seconds, *, should_stop=None):  # noqa: ARG001
                return True

            with (
                patch.object(job_store, "_JOBS_DIR", root / "jobs"),
                patch.object(pipeline, "process_one_image", side_effect=fake_process),
                patch.object(pipeline, "_cooperative_delay", side_effect=no_delay),
            ):
                result = asyncio.run(
                    pipeline.run_pipeline(
                        cfg,
                        session=_FakeSessionForJobStore(),
                        should_stop=lambda: completed >= 1,
                    )
                )

            self.assertFalse(result["ok"])
            self.assertTrue(result["partialExport"])
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["exportedRows"], 1)
            self.assertTrue(output.is_file())

    def test_resume_and_changed_file_reset(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            images = [root / f"{i}.jpg" for i in range(5)]
            for index, image in enumerate(images):
                image.write_bytes(f"image-{index}".encode())

            with patch.object(job_store, "_JOBS_DIR", root / "jobs"):
                store = ResultJobStore(root, top_n=3)
                store.prepare_items(images, prune=True)
                store.record_result(
                    seq=1, fingerprint="a", status="已完成", offers=[]
                )
                store.record_result(
                    seq=2, fingerprint="b", status="已完成（本地缓存）", offers=[]
                )
                checkpoint = store.path
                store.close()

                resumed = ResultJobStore(
                    root,
                    top_n=3,
                    database_path=checkpoint,
                    require_existing=True,
                )
                counts = resumed.prepare_items(images, prune=True)
                self.assertEqual(resumed.completed_sequences(), {1, 2})
                self.assertEqual(counts.success, 2)

                images[0].write_bytes(b"changed-image-content")
                changed = resumed.prepare_items(images, prune=True)
                self.assertEqual(resumed.completed_sequences(), {2})
                self.assertEqual(changed.success, 1)
                resumed.close()

    def test_blank_checkpoint_creates_unique_chinese_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "中文图片目录"
            root.mkdir()
            with patch.object(job_store, "_JOBS_DIR", root / "断点"):
                first = job_store.create_checkpoint_path(root, max_images=2000)
                second = job_store.create_checkpoint_path(root, max_images=2000)
                self.assertNotEqual(first, second)
                self.assertIn("中文图片目录", first.name)
                self.assertIn("最多2000张", first.name)
                self.assertEqual(first.suffix, ".sqlite3")

    def test_selected_checkpoint_rejects_wrong_image_folder(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_a = root / "图片甲"
            source_b = root / "图片乙"
            source_a.mkdir()
            source_b.mkdir()
            database = root / "中文断点库.sqlite3"
            store = ResultJobStore(source_a, top_n=3, database_path=database)
            store.close()
            with self.assertRaisesRegex(ValueError, "图片目录不匹配"):
                ResultJobStore(
                    source_b,
                    top_n=3,
                    database_path=database,
                    require_existing=True,
                )

    def test_streaming_export_preserves_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            total = 5000
            images = [root / f"image-{i:05d}.jpg" for i in range(total)]
            for image in images:
                image.write_bytes(b"same-content")
            output = root / "all-results.xlsx"

            with patch.object(job_store, "_JOBS_DIR", root / "jobs"):
                store = ResultJobStore(root, top_n=3)
                store.prepare_items(images, prune=True)
                offer = {
                    "url": "https://detail.1688.com/offer/test.html",
                    "sku_name": "测试商品",
                    "price": "1.23",
                    "spec": "测试店铺",
                    "weight": "",
                }
                for seq in range(1, total + 1):
                    store.record_result(
                        seq=seq,
                        fingerprint="same",
                        status="已完成（本地缓存）",
                        offers=[offer],
                    )
                database = store.path
                store.close()

                exported, rows = export_job_store_to_excel(database, output)
                self.assertEqual(rows, total)
                self.assertTrue(exported.is_file())
                wb = load_workbook(exported, read_only=True, data_only=True)
                try:
                    ws = wb["图搜结果"]
                    rows_read = 0
                    first_name = ""
                    last_name = ""
                    for row_index, row in enumerate(ws.iter_rows(values_only=True), start=1):
                        rows_read = row_index
                        if row_index == 3:
                            first_name = str(row[1])
                        if row_index >= 3:
                            last_name = str(row[1])
                    self.assertEqual(rows_read, total + 2)
                    self.assertEqual(first_name, "image-00000.jpg")
                    self.assertEqual(last_name, f"image-{total - 1:05d}.jpg")
                finally:
                    wb.close()


if __name__ == "__main__":
    unittest.main()
