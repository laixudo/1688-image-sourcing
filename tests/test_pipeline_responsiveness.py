from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import job_store, pipeline
from src.excel_io import ShopOffer
from src.errors import UploadMetadataRejected
from src.job_store import METADATA_FALLBACK_FAILED, ResultJobStore
from src.server import PacingBody, _apply_pacing


class _FakeSession:
    async def start(self) -> None:
        await asyncio.sleep(0)

    async def stop(self) -> None:
        await asyncio.sleep(0)


class PipelineResponsivenessTest(unittest.TestCase):
    def test_network_failure_refreshes_and_retries_current_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            image = tmp_path / "network-retry.jpg"
            image.write_bytes(b"test-image")
            output = tmp_path / "result.xlsx"
            cfg = pipeline.PipelineConfig(
                image_folder=str(tmp_path),
                output_excel=str(output),
                daily_network_limit=0,
            )
            attempts = 0
            recoveries = 0

            async def flaky_process(_session, image_path, _cfg):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")
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

            async def fake_recover(_session, _exc):
                nonlocal recoveries
                recoveries += 1

                class _Page:
                    url = "https://www.1688.com/"

                return _Page()

            async def no_delay(_seconds, *, should_stop=None):  # noqa: ARG001
                return True

            with (
                patch.object(pipeline, "process_one_image", side_effect=flaky_process),
                patch.object(
                    pipeline,
                    "recover_page_after_network_error",
                    side_effect=fake_recover,
                ),
                patch.object(pipeline, "_cooperative_delay", side_effect=no_delay),
                patch.object(job_store, "_JOBS_DIR", tmp_path / "jobs"),
            ):
                result = asyncio.run(pipeline.run_pipeline(cfg, session=_FakeSession()))

            self.assertEqual(attempts, 2)
            self.assertEqual(recoveries, 1)
            self.assertEqual(result["success"], 1)
            self.assertEqual(result["failed"], 0)

    def test_upload_metadata_is_reused_after_result_page_network_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            image = tmp_path / "stage-resume.jpg"
            image.write_bytes(b"test-image")
            output = tmp_path / "result.xlsx"
            cfg = pipeline.PipelineConfig(image_folder=str(tmp_path), output_excel=str(output))
            seen_metadata = []

            async def staged_process(
                _session, image_path, _cfg, *, upload_metadata=None, on_upload=None,
                on_result_opened=None,
            ):
                seen_metadata.append(upload_metadata)
                if upload_metadata is None:
                    await on_upload(
                        {
                            "imageAddress": "https://cdn.example/uploaded.jpg",
                            "fileId": "file-1",
                        }
                    )
                    raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")
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

            async def fake_recover(_session, _exc):
                class _Page:
                    url = "https://www.1688.com/"

                return _Page()

            async def no_delay(_seconds, *, should_stop=None):  # noqa: ARG001
                return True

            # A plain function preserves the extended signature so the pipeline
            # exercises the production metadata callback path.
            original = pipeline.process_one_image
            try:
                pipeline.process_one_image = staged_process
                with (
                    patch.object(pipeline, "recover_page_after_network_error", side_effect=fake_recover),
                    patch.object(pipeline, "_cooperative_delay", side_effect=no_delay),
                    patch.object(job_store, "_JOBS_DIR", tmp_path / "jobs"),
                ):
                    result = asyncio.run(pipeline.run_pipeline(cfg, session=_FakeSession()))
            finally:
                pipeline.process_one_image = original

            self.assertEqual(result["success"], 1)
            self.assertIsNone(seen_metadata[0])
            self.assertEqual(
                seen_metadata[1]["imageAddress"],
                "https://cdn.example/uploaded.jpg",
            )

    def test_rejected_metadata_allows_only_one_fallback_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            image = tmp_path / "single-fallback.jpg"
            image.write_bytes(b"test-image")
            output = tmp_path / "result.xlsx"
            checkpoint = tmp_path / "断点.sqlite3"
            seed = ResultJobStore(tmp_path, top_n=3, database_path=checkpoint)
            seed.prepare_items([image], prune=True)
            task_id, _content_hash, _fingerprint = seed.ensure_content_identity(
                seq=1, image_path=image
            )
            seed.save_upload_metadata(
                task_item_id=task_id,
                metadata={"imageAddress": "https://cdn.example/original.jpg"},
            )
            seed.close()
            cfg = pipeline.PipelineConfig(
                image_folder=str(tmp_path),
                output_excel=str(output),
                checkpoint_db=str(checkpoint),
            )
            calls = []

            async def always_rejected(
                _session, _image_path, _cfg, *, upload_metadata=None,
                on_upload=None, on_result_opened=None,
            ):
                calls.append(bool(upload_metadata))
                if upload_metadata is None:
                    await on_upload(
                        {"imageAddress": "https://cdn.example/fallback.jpg"}
                    )
                raise UploadMetadataRejected("metadata rejected")

            async def no_delay(_seconds, *, should_stop=None):  # noqa: ARG001
                return True

            original = pipeline.process_one_image
            try:
                pipeline.process_one_image = always_rejected
                with patch.object(pipeline, "_cooperative_delay", side_effect=no_delay):
                    result = asyncio.run(pipeline.run_pipeline(cfg, session=_FakeSession()))
            finally:
                pipeline.process_one_image = original

            self.assertEqual(calls, [True, False])
            self.assertEqual(result["failed"], 1)
            verify = ResultJobStore(
                tmp_path,
                top_n=3,
                database_path=checkpoint,
                require_existing=True,
            )
            metadata = verify.get_upload_metadata(task_id)
            self.assertEqual(metadata["metadata_status"], METADATA_FALLBACK_FAILED)
            self.assertEqual(metadata["fallback_reupload_count"], 1)
            verify.close()

    def test_live_pacing_update_mutates_active_config(self) -> None:
        cfg = pipeline.PipelineConfig(image_folder="E:/images", output_excel="")
        applied = _apply_pacing(
            cfg,
            PacingBody(
                imageDelayMinSec=7,
                imageDelayMaxSec=9,
                batchSize=33,
                batchCooldownMinSec=61,
                batchCooldownMaxSec=91,
                longBreakEvery=333,
                longBreakMinSec=181,
                longBreakMaxSec=241,
                dailyNetworkLimit=1234,
            ),
        )
        self.assertEqual(cfg.image_delay_min_sec, 7)
        self.assertEqual(cfg.batch_size, 33)
        self.assertEqual(cfg.long_break_every, 333)
        self.assertEqual(cfg.daily_network_limit, 1234)
        self.assertEqual(applied["batchCooldownMaxSec"], 91)

    def test_default_pacing_uses_balanced_fast_profile(self) -> None:
        cfg = pipeline.config_from_params({"imageFolder": "E:/images", "outputExcel": ""})
        self.assertEqual((cfg.image_delay_min_sec, cfg.image_delay_max_sec), (2, 4))
        self.assertEqual(cfg.batch_size, 50)
        self.assertEqual((cfg.batch_cooldown_min_sec, cfg.batch_cooldown_max_sec), (45, 75))
        self.assertEqual(cfg.long_break_every, 500)
        self.assertEqual(cfg.daily_network_limit, 0)
        self.assertEqual((cfg.long_break_min_sec, cfg.long_break_max_sec), (120, 180))

    def test_large_inventory_yields_to_event_loop(self) -> None:
        """同步目录扫描/Excel 操作再慢，也不能饿死 FastAPI 的主事件循环。"""
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            images = [tmp_path / f"{i}.jpg" for i in range(8)]
            for image in images:
                image.write_bytes(b"test-image")
            output = tmp_path / "result.xlsx"

            def slow_list(_folder: Path) -> list[Path]:
                time.sleep(0.20)
                return images

            def slow_save(_wb: object, path: Path) -> Path:
                time.sleep(0.03)
                path.write_bytes(b"xlsx")
                return path

            cfg = pipeline.PipelineConfig(
                image_folder=str(tmp_path), output_excel=str(output), daily_network_limit=20
            )

            async def fake_process(_session, image_path, _cfg):
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

            async def scenario() -> int:
                ticks = 0
                task = asyncio.create_task(
                    pipeline.run_pipeline(cfg, session=_FakeSession())
                )
                while not task.done():
                    ticks += 1
                    await asyncio.sleep(0.01)
                result = await task
                self.assertEqual(result["success"], len(images))
                return ticks

            with (
                patch.object(pipeline, "list_image_files", side_effect=slow_list),
                patch.object(pipeline, "resolve_output_excel", return_value=output),
                patch.object(
                    pipeline,
                    "create_result_workbook",
                    return_value=(object(), object()),
                ),
                patch.object(pipeline, "save_workbook", side_effect=slow_save),
                patch.object(pipeline, "process_one_image", side_effect=fake_process),
                patch.object(pipeline, "_cooperative_delay", side_effect=no_delay),
                patch.object(job_store, "_JOBS_DIR", tmp_path / "jobs"),
            ):
                # 若同步操作仍在事件循环执行，整个批次通常只能得到 1 次 tick。
                self.assertGreaterEqual(asyncio.run(scenario()), 10)


if __name__ == "__main__":
    unittest.main()
