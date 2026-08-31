# -*- coding: utf-8 -*-
"""批量编排：本地图片文件夹 → 1688 图搜 → TopN → 生成结果 Excel。"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from .browser import BrowserSession
from .errors import (
    EnvRetryError,
    ExcelLockedError,
    ImageSearchNavError,
    LocalImageIOError,
    ManualActionRequired,
    UploadMetadataRejected,
)
from .excel_io import (
    STATUS_DONE,
    STATUS_ENV_RETRY,
    STATUS_FAILED,
    ShopOffer,
    append_result_row,
    create_result_workbook,
    export_job_store_to_excel,
    list_image_files,
    resolve_output_excel,
    save_workbook,
)
from .image_search import (
    is_recoverable_page_network_error,
    recover_page_after_network_error,
    search_by_images,
)
from .job_store import (
    METADATA_FALLBACK_FAILED,
    METADATA_FALLBACK_REUPLOADED,
    METADATA_REJECTED,
    METADATA_VALID,
    STAGE_UPLOADED,
    STAGE_RESULT_OPENED,
    PENDING,
    ResultJobStore,
)
from .offer_detail import hits_to_top_offers
from .recovery import PageState, ProbeState, classify_page, strict_auth_probe


ShouldStop = Callable[[], bool]
UPLOAD_REUSE_TTL_MS = 15 * 60 * 1000
PAGE_REFRESH_BACKOFF_SECONDS = (15, 45)


def _call_supports_upload_metadata() -> bool:
    """Keep compatibility with tests/integrations that monkeypatch the old 3-arg hook."""
    try:
        params = inspect.signature(process_one_image).parameters
        return all(
            name in params
            for name in ("upload_metadata", "on_upload", "on_result_opened")
        )
    except (TypeError, ValueError):
        return False


async def _strict_resume_check(session: BrowserSession) -> tuple[bool, str]:
    """A click is not proof: require the protected-page probe to succeed."""
    checker = getattr(session, "check_logged_in", None)
    if checker is None:
        return True, "测试会话未提供登录探测"
    result = await strict_auth_probe(session)
    return result.state is ProbeState.AUTHENTICATED, result.reason


async def _wait_for_stable_clock(
    store: ResultJobStore, should_stop: ShouldStop | None
) -> bool:
    """After explicit manual resume, require 30 stable one-second samples."""
    previous = int(time.time() * 1000)
    for remaining in range(30, 0, -1):
        if _stop_flag.is_set() or (should_stop and should_stop()):
            return False
        with _progress_lock:
            _progress.message = f"系统时钟稳定性复核中，剩余 {remaining} 秒"
        await asyncio.sleep(1)
        current = int(time.time() * 1000)
        if current + 2_000 < previous:
            return False
        previous = current
    await asyncio.to_thread(store.record_clock_rebased)
    record_technical_event("clock_rebased", conservative_refresh_window_minutes=30)
    return True


@dataclass
class PipelineConfig:
    image_folder: str
    output_excel: str
    checkpoint_db: str = ""
    checkpoint_is_new: bool = False
    top_n: int = 3
    max_images: int = 0  # 0=全部
    headless: bool = False
    image_delay_min_sec: int = 2
    image_delay_max_sec: int = 4
    batch_size: int = 50
    batch_cooldown_min_sec: int = 45
    batch_cooldown_max_sec: int = 75
    long_break_every: int = 500
    # 较快档：每 500 次网络请求休息 2–3 分钟；遇到验证仍立即暂停等待人工。
    long_break_min_sec: int = 120
    long_break_max_sec: int = 180
    daily_network_limit: int = 0  # 0=不限制
    # 业务规则：每个输入文件都是独立任务。即使文件内容相同，也必须分别上传图搜。
    processing_mode: str = "per_file_network"


@dataclass
class RunProgress:
    running: bool = False
    current_sku: str = ""
    current_row: int = 0
    total: int = 0
    processed: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    cache_hits: int = 0
    resumed: int = 0
    daily_network_requests: int = 0
    paused: bool = False
    pause_reason: str = ""
    cooldown_remaining_sec: int = 0
    message: str = ""
    last_error: str = ""
    started_at: str = ""
    finished_at: str = ""
    output_excel: str = ""
    checkpoint_db: str = ""
    recent: list[dict[str, Any]] = field(default_factory=list)
    technical_events: list[dict[str, Any]] = field(default_factory=list)
    active_pacing: dict[str, int] = field(default_factory=dict)
    recovery_metrics: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "current_sku": self.current_sku,
            "current_row": self.current_row,
            "total": self.total,
            "processed": self.processed,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "cache_hits": self.cache_hits,
            "resumed": self.resumed,
            "daily_network_requests": self.daily_network_requests,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "cooldown_remaining_sec": self.cooldown_remaining_sec,
            "message": self.message,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_excel": self.output_excel,
            "checkpoint_db": self.checkpoint_db,
            "recent": self.recent[-20:],
            "technical_events": self.technical_events[-50:],
            "active_pacing": dict(self.active_pacing),
            "recovery_metrics": dict(self.recovery_metrics),
        }


_progress = RunProgress()
_progress_lock = threading.Lock()
_stop_flag = threading.Event()
_resume_flag = threading.Event()
_resume_flag.set()


def get_progress() -> dict[str, Any]:
    with _progress_lock:
        return _progress.to_dict()


def set_progress_message(message: str) -> None:
    with _progress_lock:
        _progress.message = message


def record_technical_event(step: str, **details: Any) -> None:
    """记录真实执行证据，供界面与文件日志同时核对。"""
    event = {"at": utc_now_iso(), "step": step, **details}
    with _progress_lock:
        _progress.technical_events.append(event)
    logger.info("AUDIT {}", json.dumps(event, ensure_ascii=False, default=str))


def increment_recovery_metric(name: str, amount: int = 1) -> None:
    with _progress_lock:
        _progress.recovery_metrics[name] = _progress.recovery_metrics.get(name, 0) + amount


def pacing_config_dict(cfg: PipelineConfig) -> dict[str, int]:
    """返回前端字段名对应的实际生效节奏参数。"""
    return {
        "imageDelayMinSec": cfg.image_delay_min_sec,
        "imageDelayMaxSec": cfg.image_delay_max_sec,
        "batchSize": cfg.batch_size,
        "batchCooldownMinSec": cfg.batch_cooldown_min_sec,
        "batchCooldownMaxSec": cfg.batch_cooldown_max_sec,
        "longBreakEvery": cfg.long_break_every,
        "longBreakMinSec": cfg.long_break_min_sec,
        "longBreakMaxSec": cfg.long_break_max_sec,
        "dailyNetworkLimit": cfg.daily_network_limit,
    }


def set_active_pacing(cfg: PipelineConfig) -> dict[str, int]:
    applied = pacing_config_dict(cfg)
    with _progress_lock:
        _progress.active_pacing = dict(applied)
    return applied


def request_stop() -> None:
    _stop_flag.set()
    _resume_flag.set()


def clear_stop() -> None:
    _stop_flag.clear()


def request_pause(reason: str) -> None:
    _resume_flag.clear()
    with _progress_lock:
        _progress.paused = True
        _progress.pause_reason = reason
        _progress.message = reason


def request_resume() -> bool:
    if _stop_flag.is_set():
        return False
    _resume_flag.set()
    with _progress_lock:
        _progress.paused = False
        _progress.pause_reason = ""
        _progress.message = "已收到继续指令，准备重试当前图片"
    return True


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _positive_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _nonnegative_int(value: Any, default: int, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 0), maximum)


def config_from_params(params: dict[str, Any]) -> PipelineConfig:
    return PipelineConfig(
        image_folder=str(params.get("imageFolder") or "").strip(),
        output_excel=str(params.get("outputExcel") or "").strip(),
        checkpoint_db=str(params.get("checkpointDb") or "").strip(),
        top_n=int(params.get("topN") or 3),
        max_images=int(params.get("maxImages") or 0),
        headless=_parse_bool(params.get("headless"), False),
        image_delay_min_sec=_positive_int(params.get("imageDelayMinSec"), 2, minimum=1, maximum=120),
        image_delay_max_sec=_positive_int(params.get("imageDelayMaxSec"), 4, minimum=1, maximum=180),
        batch_size=_positive_int(params.get("batchSize"), 50, minimum=1, maximum=100),
        batch_cooldown_min_sec=_positive_int(params.get("batchCooldownMinSec"), 45, minimum=30, maximum=3600),
        batch_cooldown_max_sec=_positive_int(params.get("batchCooldownMaxSec"), 75, minimum=30, maximum=7200),
        long_break_every=_positive_int(params.get("longBreakEvery"), 500, minimum=20, maximum=5000),
        long_break_min_sec=_positive_int(params.get("longBreakMinSec"), 120, minimum=60, maximum=7200),
        long_break_max_sec=_positive_int(params.get("longBreakMaxSec"), 180, minimum=60, maximum=14400),
        daily_network_limit=_nonnegative_int(params.get("dailyNetworkLimit"), 0, maximum=1_000_000_000),
    )


async def process_one_image(
    session: BrowserSession,
    image_path: Path,
    cfg: PipelineConfig,
    *,
    upload_metadata: dict[str, Any] | None = None,
    on_upload: Callable[[dict[str, str]], Any] | None = None,
    on_result_opened: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    with _progress_lock:
        _progress.message = f"1688图搜 {image_path.name}"

    hits = await search_by_images(
        session,
        [str(image_path)],
        max_images=1,
        upload_metadata=upload_metadata,
        on_upload=on_upload,
        on_result_opened=on_result_opened,
    )
    if not hits:
        raise ImageSearchNavError("图搜无可信结果（未进入结果页或无 offer）")

    with _progress_lock:
        _progress.message = f"结果页比价 {image_path.name}"
    # 不进详情页：直接用图搜结果卡片上的标题/价格选最低价 TopN
    offers = hits_to_top_offers(hits, top_n=cfg.top_n)
    if not offers:
        raise RuntimeError("结果页未解析到带有效价格的商品")

    return {
        "image": str(image_path),
        "name": image_path.name,
        "offers": [
            {
                "url": o.url,
                "product_name": o.sku_name,
                "price": o.price,
                "shop_name": o.spec,
                "note": o.weight,
            }
            for o in offers
        ],
        "shop_objects": offers,
    }


async def _wait_until_resumed(should_stop: ShouldStop | None = None) -> bool:
    """等待用户明确点击继续；等待期间不向网站发送任何请求。"""
    while not _resume_flag.is_set():
        if _stop_flag.is_set() or (should_stop and should_stop()):
            return False
        await asyncio.sleep(0.5)
    return not _stop_flag.is_set() and not (should_stop and should_stop())


async def _cooperative_delay(seconds: int, *, should_stop: ShouldStop | None = None) -> bool:
    """可中断的冷却时间，界面会显示剩余秒数。"""
    remaining = max(0, int(seconds))
    while remaining > 0:
        if _stop_flag.is_set() or (should_stop and should_stop()):
            return False
        if not _resume_flag.is_set():
            if not await _wait_until_resumed(should_stop):
                return False
        with _progress_lock:
            _progress.cooldown_remaining_sec = remaining
            _progress.message = f"安全冷却中，约剩 {remaining} 秒"
        await asyncio.sleep(min(1, remaining))
        remaining -= 1
    with _progress_lock:
        _progress.cooldown_remaining_sec = 0
    return True


async def _run_pipeline_legacy(
    cfg: PipelineConfig,
    *,
    session: BrowserSession | None = None,
    should_stop: ShouldStop | None = None,
) -> dict[str, Any]:
    if not cfg.image_folder:
        raise ValueError("imageFolder 不能为空")
    folder = Path(cfg.image_folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"图片文件夹不存在: {folder}")

    images = list_image_files(folder)
    if cfg.max_images > 0:
        images = images[: cfg.max_images]
    if not images:
        raise RuntimeError(f"文件夹内没有可处理的图片: {folder}")

    out_path = resolve_output_excel(cfg.output_excel, image_folder=folder)

    clear_stop()
    _resume_flag.set()
    own_session = session is None
    session = session or BrowserSession(headless=cfg.headless)

    with _progress_lock:
        _progress.running = True
        _progress.processed = 0
        _progress.success = 0
        _progress.failed = 0
        _progress.skipped = 0
        _progress.cache_hits = 0
        _progress.paused = False
        _progress.pause_reason = ""
        _progress.cooldown_remaining_sec = 0
        _progress.message = "启动中"
        _progress.last_error = ""
        _progress.started_at = utc_now_iso()
        _progress.finished_at = ""
        _progress.output_excel = str(out_path)
        _progress.recent = []
        _progress.technical_events = []
        _progress.active_pacing = pacing_config_dict(cfg)
        _progress.recovery_metrics = {}

    summary: dict[str, Any] = {
        "ok": True,
        "imageFolder": str(folder),
        "outputExcel": str(out_path),
        "total": len(images),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "cache_hits": 0,
        "errors": [],
    }

    wb, ws = create_result_workbook()

    try:
        # 先实际创建输出文件；失败则禁止访问 1688，避免“跑完却没有文件”。
        set_progress_message("正在验证结果 Excel 可写入")
        try:
            # openpyxl 保存、文件哈希和 SQLite 都是同步 I/O/CPU 操作。
            # 它们若直接跑在 FastAPI 主事件循环，会让 health/status/stop
            # 在大批缓存命中时长时间得不到调度。
            written = await asyncio.to_thread(save_workbook, wb, out_path)
        except ExcelLockedError as exc:
            summary["ok"] = False
            summary["message"] = f"输出预检失败：{exc}"
            set_progress_message(summary["message"])
            record_technical_event("output_preflight_failed", path=str(out_path), error=str(exc))
            return summary
        summary["outputExcel"] = str(written)
        record_technical_event("output_preflight", path=str(written), exists=written.is_file())
        if not written.is_file():
            raise ExcelLockedError(f"结果 Excel 写入后未找到文件: {written}")
        set_progress_message(f"结果 Excel 已创建：{written.name}")
        await session.start()
        logger.info("待处理图片 {} 张 → {}", len(images), out_path)

        network_requests = 0
        for idx, image_path in enumerate(images, start=1):
            if _stop_flag.is_set() or (should_stop and should_stop()):
                summary["ok"] = False
                summary["message"] = "用户中断"
                logger.warning("流水线中断")
                break

            with _progress_lock:
                _progress.current_sku = image_path.name
                _progress.current_row = idx
                _progress.message = f"处理 {image_path.name} ({idx}/{len(images)})"

            fingerprint = f"per-file:{image_path.resolve()}"
            used_network = False
            try:
                while True:
                    try:
                        used_network = True
                        result = await process_one_image(session, image_path, cfg)
                        break
                    except ManualActionRequired as exc:
                        reason = str(exc)
                        request_pause(reason)
                        logger.warning("任务已暂停，等待人工处理: {}", reason)
                        if not await _wait_until_resumed(should_stop):
                            summary["ok"] = False
                            summary["message"] = "用户中断"
                            break
                        with _progress_lock:
                            _progress.message = f"重新尝试 {image_path.name}"
                        # 用户确认后从当前图片重新开始，不连续重试验证页面。
                        continue
                if summary.get("message") == "用户中断":
                    break

                append_result_row(
                    ws,
                    status=STATUS_DONE,
                    image_path=image_path,
                    shops=result["shop_objects"],
                )
                written = await asyncio.to_thread(save_workbook, wb, out_path)
                summary["outputExcel"] = str(written)
                summary["success"] += 1
                with _progress_lock:
                    _progress.success += 1
                    _progress.processed += 1
                    _progress.output_excel = str(written)
                    _progress.recent.append(
                        {"image": image_path.name, "ok": True, "row": idx}
                    )
                logger.info("成功 {} → {}", image_path.name, written)
            except Exception as exc:  # noqa: BLE001
                logger.exception("图片失败 {}: {}", image_path.name, exc)
                err_msg = str(exc)
                if _stop_flag.is_set() or (should_stop and should_stop()):
                    summary["ok"] = False
                    summary["message"] = "用户中断"
                    logger.warning("当前图片已取消，不写入失败结果: {}", image_path.name)
                    break
                if isinstance(exc, ExcelLockedError):
                    status_to_write = "Excel锁定"
                    halt_batch = True
                elif isinstance(exc, (ImageSearchNavError, EnvRetryError)):
                    status_to_write = getattr(exc, "status", None) or STATUS_ENV_RETRY
                    halt_batch = False
                else:
                    status_to_write = STATUS_FAILED
                    halt_batch = False

                try:
                    append_result_row(
                        ws,
                        status=status_to_write,
                        image_path=image_path,
                        shops=[],
                        error=err_msg,
                    )
                    written = await asyncio.to_thread(save_workbook, wb, out_path)
                    summary["outputExcel"] = str(written)
                except ExcelLockedError as pe:
                    err_msg = f"{err_msg} | 另外无法写结果表: {pe}"
                    halt_batch = True
                    summary["ok"] = False
                    summary["message"] = str(pe)
                except PermissionError as pe:
                    err_msg = f"{err_msg} | 另外无法写结果表: {pe}"
                    halt_batch = True
                    summary["ok"] = False
                    summary["message"] = str(pe)

                err = {
                    "image": image_path.name,
                    "path": str(image_path),
                    "error": err_msg,
                    "status": status_to_write,
                }
                summary["failed"] += 1
                summary["errors"].append(err)
                with _progress_lock:
                    _progress.failed += 1
                    _progress.processed += 1
                    _progress.last_error = err_msg
                    _progress.recent.append({**err, "ok": False})
                if halt_batch:
                    summary["ok"] = False
                    summary["message"] = (
                        summary.get("message")
                        or f"环境异常熔断（{status_to_write}），已暂停后续图片"
                    )
                    logger.error("流水线熔断: {}", summary["message"])
                    break

            if used_network:
                network_requests += 1
                if network_requests % cfg.batch_size == 0:
                    low = min(cfg.batch_cooldown_min_sec, cfg.batch_cooldown_max_sec)
                    high = max(cfg.batch_cooldown_min_sec, cfg.batch_cooldown_max_sec)
                    cooldown = random.randint(low, high)
                    logger.info("已完成 {} 次图搜请求，进入批次冷却 {} 秒", network_requests, cooldown)
                else:
                    low = min(cfg.image_delay_min_sec, cfg.image_delay_max_sec)
                    high = max(cfg.image_delay_min_sec, cfg.image_delay_max_sec)
                    cooldown = random.randint(low, high)
                if not await _cooperative_delay(cooldown, should_stop=should_stop):
                    summary["ok"] = False
                    summary["message"] = "用户中断"
                    break

        with _progress_lock:
            _progress.paused = False
            _progress.cooldown_remaining_sec = 0
            if summary.get("message") == "用户中断":
                _progress.message = "已中断：未继续处理后续图片"
            elif summary["failed"]:
                _progress.message = (
                    f"失败：成功 {summary['success']}，失败 {summary['failed']}；"
                    f"请查看错误详情，输出文件={summary.get('outputExcel') or '未写入'}"
                )
            elif summary["success"]:
                _progress.message = f"已完成：成功写入 {summary['success']} 条到 {summary['outputExcel']}"
            else:
                _progress.message = "未完成：没有产生可写入的成功结果"
            _progress.finished_at = utc_now_iso()
            _progress.running = False

        if summary["failed"] and summary["success"] == 0 and not summary.get("message"):
            summary["ok"] = False
            summary["message"] = "全部失败"
        return summary
    finally:
        with _progress_lock:
            _progress.running = False
            if not _progress.finished_at:
                _progress.finished_at = utc_now_iso()
        if own_session:
            await session.stop()


async def run_pipeline(
    cfg: PipelineConfig,
    *,
    session: BrowserSession | None = None,
    should_stop: ShouldStop | None = None,
) -> dict[str, Any]:
    """可恢复的大批量流水线：SQLite逐条落盘，完成后流式导出Excel。"""
    if not cfg.image_folder:
        raise ValueError("imageFolder 不能为空")
    folder = Path(cfg.image_folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"图片文件夹不存在: {folder}")

    set_progress_message("正在扫描图片文件")
    images = await asyncio.to_thread(list_image_files, folder)
    if cfg.max_images > 0:
        images = images[: cfg.max_images]
    if not images:
        raise RuntimeError(f"文件夹内没有可处理的图片: {folder}")

    out_path = resolve_output_excel(cfg.output_excel, image_folder=folder)
    clear_stop()
    _resume_flag.set()
    own_session = session is None
    session = session or BrowserSession(headless=cfg.headless)
    checkpoint_was_selected = bool(cfg.checkpoint_db) and not cfg.checkpoint_is_new
    store = ResultJobStore(
        folder,
        top_n=cfg.top_n,
        scope=f"max={cfg.max_images}" if cfg.max_images > 0 else "all",
        database_path=cfg.checkpoint_db or None,
        require_existing=checkpoint_was_selected,
    )
    cfg.checkpoint_db = str(store.path)

    with _progress_lock:
        _progress.running = True
        _progress.current_sku = ""
        _progress.current_row = 0
        _progress.total = len(images)
        _progress.processed = 0
        _progress.success = 0
        _progress.failed = 0
        _progress.skipped = 0
        _progress.cache_hits = 0
        _progress.resumed = 0
        _progress.daily_network_requests = 0
        _progress.paused = False
        _progress.pause_reason = ""
        _progress.cooldown_remaining_sec = 0
        _progress.message = "正在建立断点清单"
        _progress.last_error = ""
        _progress.started_at = utc_now_iso()
        _progress.finished_at = ""
        _progress.output_excel = str(out_path)
        _progress.checkpoint_db = str(store.path)
        _progress.recent = []
        _progress.technical_events = []
        _progress.recovery_metrics = {}

    summary: dict[str, Any] = {
        "ok": True,
        "imageFolder": str(folder),
        "outputExcel": str(out_path),
        "checkpointDb": str(store.path),
        "total": len(images),
        "processed": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "cache_hits": 0,
        "resumed": 0,
        "pending": len(images),
        "errors": [],
    }

    try:
        record_technical_event("pacing_config_applied", **pacing_config_dict(cfg))
        counts = await asyncio.to_thread(
            store.prepare_items,
            images,
            prune=True,
        )
        completed = await asyncio.to_thread(store.completed_sequences)
        summary.update(
            processed=counts.processed,
            success=counts.success,
            failed=counts.failed,
            skipped=counts.cache_hits,
            cache_hits=counts.cache_hits,
            resumed=counts.success,
            pending=counts.total - counts.processed,
        )
        with _progress_lock:
            _progress.total = counts.total
            _progress.processed = counts.processed
            _progress.success = counts.success
            _progress.failed = counts.failed
            _progress.skipped = counts.cache_hits
            _progress.cache_hits = counts.cache_hits
            _progress.resumed = counts.success
        record_technical_event(
            "checkpoint_inventory",
            database=str(store.path),
            source="selected" if checkpoint_was_selected else "new",
            total=counts.total,
            resumed=counts.success,
            pending=counts.total - counts.processed,
            processing_mode=cfg.processing_mode,
            content_cache_enabled=False,
        )
        stale_reservations = await asyncio.to_thread(
            store.reconcile_stale_refresh_reservations
        )
        archived_events = await asyncio.to_thread(store.archive_old_recovery_events)
        clock_state, clock_delta_ms = await asyncio.to_thread(store.check_wall_clock)
        record_technical_event(
            "recovery_store_reconciled",
            stale_reservations=stale_reservations,
            archived_event_details=archived_events,
            clock_state=clock_state,
            clock_delta_ms=clock_delta_ms,
        )
        if clock_state == "CLOCK_UNCERTAIN":
            reason = (
                f"检测到系统时钟回拨 {abs(clock_delta_ms) // 1000} 秒；"
                "页面自动恢复已暂停。请校正系统时间后点击人工处理后继续。"
            )
            request_pause(reason)
            if not await _wait_until_resumed(should_stop):
                summary["ok"] = False
                summary["message"] = "用户中断；断点已保存"
                return summary
            if not await _wait_for_stable_clock(store, should_stop):
                summary["ok"] = False
                summary["message"] = "系统时钟仍不稳定；断点已保存"
                return summary

        # 只验证目录可写，不在处理中反复覆盖最终文件。
        probe = out_path.with_name(f".{out_path.name}.preflight.xlsx")
        wb_probe, _ws_probe = create_result_workbook()
        try:
            await asyncio.to_thread(save_workbook, wb_probe, probe)
            await asyncio.to_thread(probe.unlink, True)
        except ExcelLockedError as exc:
            summary["ok"] = False
            summary["message"] = f"输出目录预检失败：{exc}"
            record_technical_event("output_preflight_failed", path=str(probe), error=str(exc))
            return summary
        record_technical_event("output_preflight", directory=str(out_path.parent), ok=True)

        if len(completed) < len(images):
            await session.start()
        logger.info(
            "可恢复任务 total={} resumed={} pending={} checkpoint={} out={}",
            len(images),
            len(completed),
            len(images) - len(completed),
            store.path,
            out_path,
        )

        local_date = datetime.now().astimezone().date().isoformat()
        daily_requests = await asyncio.to_thread(store.daily_requests, local_date)
        with _progress_lock:
            _progress.daily_network_requests = daily_requests

        for idx, image_path in enumerate(images, start=1):
            if idx in completed:
                continue
            if _stop_flag.is_set() or (should_stop and should_stop()):
                summary["ok"] = False
                summary["message"] = "用户中断；断点已保存，下次从未完成图片继续"
                break

            with _progress_lock:
                _progress.current_sku = image_path.name
                _progress.current_row = idx
                _progress.message = f"处理 {image_path.name} ({idx}/{len(images)})"

            fingerprint = ""
            task_item_id = ""
            used_network = False
            try:
                # 本地 I/O 是独立的图片级阶段。失败不进入页面异常状态机，
                # 不消耗上传、刷新或登录探测次数。
                task_item_id, content_sha256, fingerprint = await asyncio.to_thread(
                    store.ensure_content_identity, seq=idx, image_path=image_path
                )
                record_technical_event(
                    "image_identity_ready",
                    row=idx,
                    task_item_id=task_item_id,
                    content_sha256=content_sha256,
                )
                if cfg.daily_network_limit > 0 and daily_requests >= cfg.daily_network_limit:
                    summary["ok"] = False
                    summary["message"] = (
                        f"当天新图搜请求已达到安全上限 {cfg.daily_network_limit}；"
                        "断点已保存，请次日继续"
                    )
                    with _progress_lock:
                        _progress.paused = True
                        _progress.pause_reason = summary["message"]
                        _progress.message = summary["message"]
                    break

                daily_requests = await asyncio.to_thread(
                    store.increment_daily_requests, local_date
                )
                used_network = True
                with _progress_lock:
                    _progress.daily_network_requests = daily_requests
                saved_metadata = await asyncio.to_thread(
                    store.get_upload_metadata, task_item_id
                )
                active_metadata: dict[str, Any] | None = None
                fallback_used = False
                current_upload_is_fallback = False
                if saved_metadata:
                    fallback_used = int(saved_metadata.get("fallback_reupload_count") or 0) >= 1
                    active_metadata = await asyncio.to_thread(
                        store.reusable_upload_metadata,
                        task_item_id,
                        ttl_ms=UPLOAD_REUSE_TTL_MS,
                    )
                    if active_metadata:
                        increment_recovery_metric("metadata_reuse_attempts")
                record_technical_event(
                    "per_file_search_started",
                    row=idx,
                    total=len(images),
                    file=image_path.name,
                    daily_request=daily_requests,
                    upload_metadata_reuse=bool(active_metadata),
                )

                network_recovery_attempts = 0
                while True:
                    try:
                        async def persist_upload(meta: dict[str, str]) -> None:
                            nonlocal active_metadata
                            status = (
                                METADATA_FALLBACK_REUPLOADED
                                if current_upload_is_fallback else METADATA_VALID
                            )
                            await asyncio.to_thread(
                                store.save_upload_metadata,
                                task_item_id=task_item_id,
                                metadata=meta,
                                status=status,
                                fallback_reupload=current_upload_is_fallback,
                            )
                            await asyncio.to_thread(
                                store.set_stage, seq=idx, stage=STAGE_UPLOADED
                            )
                            active_metadata = {
                                **meta,
                                "metadata_status": status,
                                "uploaded_at_utc_ms": int(time.time() * 1000),
                                "fallback_reupload_count": 1 if current_upload_is_fallback else 0,
                            }
                            record_technical_event(
                                "upload_metadata_saved",
                                row=idx,
                                status=status,
                                image_address=bool(meta.get("imageAddress")),
                            )

                        async def persist_result_opened(url: str) -> None:
                            await asyncio.to_thread(
                                store.set_stage, seq=idx, stage=STAGE_RESULT_OPENED
                            )
                            record_technical_event(
                                "result_page_opened", row=idx, url=url[:200]
                            )

                        if _call_supports_upload_metadata():
                            result = await process_one_image(
                                session,
                                image_path,
                                cfg,
                                upload_metadata=active_metadata,
                                on_upload=persist_upload,
                                on_result_opened=persist_result_opened,
                            )
                        else:
                            result = await process_one_image(session, image_path, cfg)
                        break
                    except UploadMetadataRejected as exc:
                        increment_recovery_metric("metadata_reuse_failures")
                        await asyncio.to_thread(
                            store.set_upload_metadata_status,
                            task_item_id,
                            METADATA_REJECTED,
                        )
                        record_technical_event(
                            "upload_metadata_rejected",
                            row=idx,
                            error=str(exc),
                            fallback_already_used=fallback_used,
                        )
                        if fallback_used:
                            await asyncio.to_thread(
                                store.set_upload_metadata_status,
                                task_item_id,
                                METADATA_FALLBACK_FAILED,
                            )
                            raise
                        # 明确失效只允许一次降级重传；不占用页面刷新配额。
                        fallback_used = True
                        current_upload_is_fallback = True
                        increment_recovery_metric("fallback_reuploads")
                        active_metadata = None
                        daily_requests = await asyncio.to_thread(
                            store.increment_daily_requests, local_date
                        )
                        with _progress_lock:
                            _progress.daily_network_requests = daily_requests
                        record_technical_event(
                            "fallback_reupload_started", row=idx, daily_request=daily_requests
                        )
                        continue
                    except ManualActionRequired as exc:
                        reason = str(exc)
                        request_pause(reason)
                        logger.warning("任务已暂停，等待人工处理: {}", reason)
                        if not await _wait_until_resumed(should_stop):
                            summary["ok"] = False
                            summary["message"] = "用户中断；断点已保存"
                            break
                        authenticated, probe_reason = await _strict_resume_check(session)
                        if not authenticated:
                            reason = f"人工继续未通过严格登录检查：{probe_reason}"
                            request_pause(reason)
                            record_technical_event(
                                "manual_continue_rejected", row=idx, reason=probe_reason
                            )
                            continue
                        with _progress_lock:
                            _progress.message = f"验证及登录已确认，重新尝试 {image_path.name}"
                    except Exception as exc:  # noqa: BLE001
                        if (
                            not is_recoverable_page_network_error(exc)
                            or network_recovery_attempts >= 2
                        ):
                            raise
                        classification = await classify_page(
                            getattr(session, "page", None), exc
                        )
                        increment_recovery_metric(
                            f"page_{classification.state.value}_classification"
                        )
                        record_technical_event(
                            "page_failure_classified",
                            row=idx,
                            classification=classification.state.value,
                            reason=classification.reason,
                            classification_local_ms=classification.local_ms,
                        )
                        if classification.state is not PageState.NETWORK:
                            reason = (
                                f"页面状态为 {classification.state.value}，"
                                "为避免误刷新已安全暂停，请人工检查可见浏览器。"
                            )
                            request_pause(reason)
                            if not await _wait_until_resumed(should_stop):
                                summary["ok"] = False
                                summary["message"] = "用户中断；断点已保存"
                                break
                            continue

                        # 探测失败与明确退出登录严格分桶；探测网络失败不冒充退出登录。
                        if hasattr(session, "check_logged_in"):
                            probe = await strict_auth_probe(session)
                            increment_recovery_metric(f"auth_{probe.state.value}")
                            record_technical_event(
                                "auth_probe_after_network_failure",
                                row=idx,
                                probe_state=probe.state.value,
                                attempts=probe.attempts,
                                probe_total_ms=probe.total_ms,
                                reason=probe.reason,
                                account_name=probe.account_name,
                            )
                            if probe.state is not ProbeState.AUTHENTICATED:
                                reason = (
                                    f"登录探测结果为 {probe.state.value}：{probe.reason}。"
                                    "自动刷新已暂停，请人工检查。"
                                )
                                request_pause(reason)
                                if not await _wait_until_resumed(should_stop):
                                    summary["ok"] = False
                                    summary["message"] = "用户中断；断点已保存"
                                    break
                                continue
                        next_attempt = network_recovery_attempts + 1
                        event_type = "page_reload"
                        current_page = getattr(session, "page", None)
                        if current_page is None or current_page.is_closed():
                            event_type = "new_page"
                        refresh_event_id = await asyncio.to_thread(
                            store.reserve_refresh,
                            image_seq=idx,
                            event_type=event_type,
                        )
                        if refresh_event_id is None:
                            increment_recovery_metric("global_refresh_circuit_open")
                            reason = "30分钟页面恢复熔断已生效；请检查网络/验证状态后人工继续"
                            request_pause(reason)
                            record_technical_event(
                                "page_refresh_circuit_open", row=idx, attempt=next_attempt
                            )
                            if not await _wait_until_resumed(should_stop):
                                summary["ok"] = False
                                summary["message"] = "用户中断；断点已保存"
                                break
                            continue
                        network_recovery_attempts = next_attempt
                        increment_recovery_metric("global_page_refreshes")
                        increment_recovery_metric("per_image_page_refreshes")
                        daily_requests = await asyncio.to_thread(
                            store.increment_daily_requests, local_date
                        )
                        with _progress_lock:
                            _progress.daily_network_requests = daily_requests
                            _progress.message = (
                                f"检测到1688网络异常，正在刷新页面并重试当前图片 "
                                f"({network_recovery_attempts}/2)"
                            )
                        record_technical_event(
                            "network_page_recovery_started",
                            row=idx,
                            file=image_path.name,
                            attempt=network_recovery_attempts,
                            error=f"{type(exc).__name__}: {exc}",
                            daily_request=daily_requests,
                        )
                        try:
                            backoff = PAGE_REFRESH_BACKOFF_SECONDS[
                                min(network_recovery_attempts - 1, len(PAGE_REFRESH_BACKOFF_SECONDS) - 1)
                            ]
                            if not await _cooperative_delay(backoff, should_stop=should_stop):
                                await asyncio.to_thread(
                                    store.finish_recovery_event,
                                    refresh_event_id,
                                    outcome="failed",
                                    details={"reason": "stopped_during_backoff"},
                                )
                                summary["ok"] = False
                                summary["message"] = "用户中断；断点已保存"
                                break
                            recovered_page = await recover_page_after_network_error(
                                session, exc
                            )
                        except ManualActionRequired as manual_exc:
                            await asyncio.to_thread(
                                store.finish_recovery_event,
                                refresh_event_id,
                                outcome="failed",
                                details={"reason": "challenge_detected"},
                            )
                            reason = str(manual_exc)
                            request_pause(reason)
                            logger.warning("刷新前检测到验证，暂停等待人工处理: {}", reason)
                            if not await _wait_until_resumed(should_stop):
                                summary["ok"] = False
                                summary["message"] = "用户中断；断点已保存"
                                break
                            continue
                        except Exception as recovery_exc:
                            await asyncio.to_thread(
                                store.finish_recovery_event,
                                refresh_event_id,
                                outcome="failed",
                                details={"error": f"{type(recovery_exc).__name__}: {recovery_exc}"},
                            )
                            raise
                        await asyncio.to_thread(
                            store.finish_recovery_event,
                            refresh_event_id,
                            outcome="completed",
                            details={"url": (recovered_page.url or "")[:200]},
                        )
                        # 上传阶段若已成功，恢复后从保存的 imageAddress 继续，
                        # 不从本地图片重新上传。
                        refreshed_metadata = await asyncio.to_thread(
                            store.get_upload_metadata, task_item_id
                        )
                        if refreshed_metadata and refreshed_metadata.get("metadata_status") in {
                            METADATA_VALID,
                            METADATA_FALLBACK_REUPLOADED,
                        }:
                            active_metadata = refreshed_metadata
                        record_technical_event(
                            "network_page_recovery_completed",
                            row=idx,
                            file=image_path.name,
                            attempt=network_recovery_attempts,
                            url=(recovered_page.url or "")[:200],
                        )
                        await asyncio.sleep(1)
                if summary.get("message", "").startswith("用户中断"):
                    break

                offers_payload = [
                    {
                        "url": offer.url,
                        "sku_name": offer.sku_name,
                        "price": offer.price,
                        "spec": offer.spec,
                        "weight": offer.weight,
                    }
                    for offer in result["shop_objects"]
                ]
                await asyncio.to_thread(
                    store.record_result,
                    seq=idx,
                    fingerprint=fingerprint,
                    status=STATUS_DONE,
                    offers=offers_payload,
                )
                with _progress_lock:
                    _progress.success += 1
                    _progress.processed += 1
                    _progress.recent.append({"image": image_path.name, "ok": True, "row": idx})
                record_technical_event(
                    "per_file_search_completed",
                    row=idx,
                    file=image_path.name,
                    offers=len(offers_payload),
                    cache_reuse=False,
                )
                logger.info("成功 {} → checkpoint={}", image_path.name, store.path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("图片失败 {}: {}", image_path.name, exc)
                err_msg = str(exc)
                if _stop_flag.is_set() or (should_stop and should_stop()):
                    summary["ok"] = False
                    summary["message"] = "用户中断；断点已保存"
                    break
                if isinstance(exc, LocalImageIOError):
                    status_to_write = exc.status
                elif isinstance(exc, (ImageSearchNavError, EnvRetryError)):
                    status_to_write = getattr(exc, "status", None) or STATUS_ENV_RETRY
                else:
                    status_to_write = STATUS_FAILED
                await asyncio.to_thread(
                    store.record_result,
                    seq=idx,
                    fingerprint=fingerprint,
                    status=status_to_write,
                    offers=[],
                    error=err_msg,
                )
                with _progress_lock:
                    _progress.failed += 1
                    _progress.processed += 1
                    _progress.last_error = err_msg
                    _progress.recent.append(
                        {"image": image_path.name, "path": str(image_path), "error": err_msg, "status": status_to_write, "ok": False}
                    )

            if used_network:
                if daily_requests % cfg.long_break_every == 0:
                    low = min(cfg.long_break_min_sec, cfg.long_break_max_sec)
                    high = max(cfg.long_break_min_sec, cfg.long_break_max_sec)
                    record_technical_event(
                        "long_break_started",
                        completed_requests=daily_requests,
                        min_seconds=low,
                        max_seconds=high,
                    )
                elif daily_requests % cfg.batch_size == 0:
                    low = min(cfg.batch_cooldown_min_sec, cfg.batch_cooldown_max_sec)
                    high = max(cfg.batch_cooldown_min_sec, cfg.batch_cooldown_max_sec)
                else:
                    low = min(cfg.image_delay_min_sec, cfg.image_delay_max_sec)
                    high = max(cfg.image_delay_min_sec, cfg.image_delay_max_sec)
                cooldown = random.randint(low, high)
                if not await _cooperative_delay(cooldown, should_stop=should_stop):
                    summary["ok"] = False
                    summary["message"] = "用户中断；断点已保存"
                    break

        counts = await asyncio.to_thread(store.counts)
        summary.update(
            total=counts.total,
            processed=counts.processed,
            success=counts.success,
            failed=counts.failed,
            skipped=counts.cache_hits,
            cache_hits=counts.cache_hits,
            pending=counts.total - counts.processed,
        )
        with _progress_lock:
            _progress.total = counts.total
            _progress.processed = counts.processed
            _progress.success = counts.success
            _progress.failed = counts.failed
            _progress.skipped = counts.cache_hits
            _progress.cache_hits = counts.cache_hits

        if summary["pending"] == 0:
            with _progress_lock:
                _progress.message = f"正在流式导出完整 Excel（{counts.total} 行）"
            exported, exported_rows = await asyncio.to_thread(
                export_job_store_to_excel, store.path, out_path
            )
            if exported_rows != counts.total:
                raise RuntimeError(
                    f"完整性校验失败：断点库 {counts.total} 行，Excel 导出 {exported_rows} 行"
                )
            summary["outputExcel"] = str(exported)
            summary["exportedRows"] = exported_rows
            record_technical_event(
                "final_export",
                checkpoint=str(store.path),
                output=str(exported),
                rows=exported_rows,
                exists=exported.is_file(),
                bytes=exported.stat().st_size if exported.is_file() else 0,
            )
            if counts.failed:
                summary["ok"] = False
                summary["message"] = (
                    f"已完整导出 {exported_rows} 行，其中失败 {counts.failed} 行可在错误列查看"
                )
            else:
                summary["ok"] = True
                summary["message"] = f"已完整导出 {exported_rows} 行"
        else:
            summary["ok"] = False
            base_message = summary.get("message") or (
                f"任务未完成，剩余 {summary['pending']} 张；断点已保存，可继续运行"
            )
            with _progress_lock:
                _progress.message = f"正在导出阶段 Excel（已处理 {counts.processed} 行）"
            try:
                exported, exported_rows = await asyncio.to_thread(
                    export_job_store_to_excel, store.path, out_path
                )
                summary["outputExcel"] = str(exported)
                summary["exportedRows"] = exported_rows
                summary["partialExport"] = True
                summary["message"] = (
                    f"{base_message}；已导出阶段 Excel {exported_rows} 行"
                )
                record_technical_event(
                    "partial_export",
                    checkpoint=str(store.path),
                    output=str(exported),
                    rows=exported_rows,
                    total=counts.total,
                    pending=summary["pending"],
                    exists=exported.is_file(),
                    bytes=exported.stat().st_size if exported.is_file() else 0,
                )
            except Exception as export_exc:  # noqa: BLE001
                summary["partialExport"] = False
                summary["partialExportError"] = str(export_exc)
                summary["message"] = f"{base_message}；阶段 Excel 导出失败：{export_exc}"
                record_technical_event(
                    "partial_export_failed",
                    checkpoint=str(store.path),
                    output=str(out_path),
                    error=f"{type(export_exc).__name__}: {export_exc}",
                )

        with _progress_lock:
            _progress.paused = summary["pending"] > 0 and "安全上限" in summary.get("message", "")
            _progress.cooldown_remaining_sec = 0
            _progress.message = summary.get("message", "")
            _progress.finished_at = utc_now_iso()
            _progress.running = False
            _progress.output_excel = summary.get("outputExcel", str(out_path))
        return summary
    finally:
        with _progress_lock:
            _progress.running = False
            if not _progress.finished_at:
                _progress.finished_at = utc_now_iso()
        await asyncio.to_thread(store.close)
        if own_session:
            await session.stop()
