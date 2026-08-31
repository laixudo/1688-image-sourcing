# -*- coding: utf-8 -*-
"""长驻 FastAPI 服务。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field

from .browser import get_resident_session, stop_resident_session
from .excel_io import resolve_output_excel, summarize_folder
from .job_store import ResultJobStore, create_checkpoint_path
from .pipeline import (
    config_from_params,
    get_progress,
    pacing_config_dict,
    record_technical_event,
    request_resume,
    request_stop,
    run_pipeline,
    set_active_pacing,
)
from .upload_diagnostics import UploadDiagnostics

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_INDEX_HTML = _PLUGIN_ROOT / "index.html"


class RunBody(BaseModel):
    imageFolder: str = Field(..., description="本地图片文件夹路径")
    outputExcel: str = Field("", description="结果 Excel 路径或目录")
    checkpointDb: str = Field("", description="现有断点库路径；留空则新建")
    topN: int = 3
    maxImages: int = 0
    headless: bool = False
    imageDelayMinSec: int = 2
    imageDelayMaxSec: int = 4
    batchSize: int = 50
    batchCooldownMinSec: int = 45
    batchCooldownMaxSec: int = 75
    longBreakEvery: int = 500
    longBreakMinSec: int = 120
    longBreakMaxSec: int = 180
    dailyNetworkLimit: int = 0


class PacingBody(BaseModel):
    imageDelayMinSec: int = Field(2, ge=1, le=120)
    imageDelayMaxSec: int = Field(4, ge=1, le=180)
    batchSize: int = Field(50, ge=1, le=100)
    batchCooldownMinSec: int = Field(45, ge=30, le=3600)
    batchCooldownMaxSec: int = Field(75, ge=30, le=7200)
    longBreakEvery: int = Field(500, ge=20, le=5000)
    longBreakMinSec: int = Field(120, ge=60, le=7200)
    longBreakMaxSec: int = Field(180, ge=60, le=14400)
    dailyNetworkLimit: int = Field(0, ge=0, le=1_000_000_000)


def _apply_pacing(cfg: Any, body: PacingBody) -> dict[str, int]:
    values = body.model_dump()
    cfg.image_delay_min_sec = values["imageDelayMinSec"]
    cfg.image_delay_max_sec = values["imageDelayMaxSec"]
    cfg.batch_size = values["batchSize"]
    cfg.batch_cooldown_min_sec = values["batchCooldownMinSec"]
    cfg.batch_cooldown_max_sec = values["batchCooldownMaxSec"]
    cfg.long_break_every = values["longBreakEvery"]
    cfg.long_break_min_sec = values["longBreakMinSec"]
    cfg.long_break_max_sec = values["longBreakMaxSec"]
    cfg.daily_network_limit = values["dailyNetworkLimit"]
    return set_active_pacing(cfg)


def create_app(plugin_id: str) -> FastAPI:
    app = FastAPI(title="1688 图搜比价", version="0.6.22")
    state: dict[str, Any] = {
        "task": None,
        "active_config": None,
        "manual_upload_diagnostics": None,
    }

    @app.get("/")
    @app.get("/index.html")
    def index_page() -> FileResponse:
        return FileResponse(_INDEX_HTML, media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        """避免浏览器自动请求站点图标时在控制台留下无关 404。"""
        return Response(status_code=204)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "plugin_id": plugin_id,
            "mode": "resident",
            "name": "1688 图搜比价",
        }

    @app.get("/status")
    def status() -> dict[str, Any]:
        session = get_resident_session()
        login_window = session.last_login_window
        return {
            "ok": True,
            "browser_ready": session.ready,
            "login_window_confirmed": session.login_window_confirmed,
            "login_window_evidence": login_window.to_dict() if login_window else None,
            "progress": get_progress(),
        }

    @app.post("/login")
    async def login() -> dict[str, Any]:
        session = get_resident_session(headless=False)
        try:
            evidence = await session.open_login()
        except Exception as exc:  # noqa: BLE001
            logger.exception("打开可见登录窗口失败: {}", exc)
            return {
                "ok": False,
                "message": "登录窗口启动失败，未进入下一步。",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not evidence.ok:
            return {
                "ok": False,
                "message": "未确认登录窗口可见，未进入下一步。",
                "evidence": evidence.to_dict(),
            }
        return {
            "ok": True,
            "message": (
                "自动检查已通过，请核对附带截图并确认你确实看到登录窗口，然后点击“我看到登录页面”。"
            ),
            "evidence": evidence.to_dict(),
            "requires_user_confirmation": True,
        }

    @app.post("/confirm-login-visible")
    def confirm_login_visible() -> dict[str, Any]:
        session = get_resident_session()
        evidence = session.confirm_login_window_visible()
        if not evidence.ok:
            return {"ok": False, "error": evidence.error, "evidence": evidence.to_dict()}
        return {"ok": True, "message": "已记录用户确认，可继续检查登录状态。", "evidence": evidence.to_dict()}

    @app.post("/diagnostics/manual-upload/start")
    def start_manual_upload_diagnostics() -> dict[str, Any]:
        active = state.get("manual_upload_diagnostics")
        if active is not None:
            return {"ok": False, "error": "手动上传抓包已经在运行"}
        session = get_resident_session()
        if not session.ready or session.page is None:
            return {"ok": False, "error": "浏览器未就绪"}
        diagnostics = UploadDiagnostics(session.page)
        diagnostics.start()
        state["manual_upload_diagnostics"] = diagnostics
        return {"ok": True, "message": "已开始监听手动上传请求", "log_path": str(diagnostics.path)}

    @app.post("/diagnostics/manual-upload/stop")
    def stop_manual_upload_diagnostics() -> dict[str, Any]:
        diagnostics = state.get("manual_upload_diagnostics")
        if diagnostics is None:
            return {"ok": False, "error": "没有正在运行的手动上传抓包"}
        state["manual_upload_diagnostics"] = None
        return {"ok": True, "summary": diagnostics.stop()}

    @app.get("/login-check")
    async def login_check() -> dict[str, Any]:
        session = get_resident_session()
        evidence = await session.check_logged_in()
        return {"ok": True, "logged_in": evidence.authenticated, "evidence": evidence.to_dict()}

    @app.post("/preview-folder")
    def preview_folder(body: dict[str, Any]) -> dict[str, Any]:
        path = str(body.get("imageFolder") or "").strip()
        if not path:
            return {"ok": False, "error": "imageFolder 不能为空"}
        try:
            return {"ok": True, "summary": summarize_folder(path)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("图片文件夹预览失败 path={!r}: {}", path, exc)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc or '未提供错误详情'}",
                "path": path,
            }

    @app.post("/run")
    async def run(body: RunBody) -> dict[str, Any]:
        if state.get("task") and not state["task"].done():
            return {"ok": False, "error": "已有任务在运行，请先等待或停止"}

        params = body.model_dump()
        cfg = config_from_params(params)
        if cfg.headless:
            return {
                "ok": False,
                "error": "安全运行模式要求使用可见浏览器，以便用户登录并在需要时手动完成验证。",
            }
        # 预先固定输出路径，避免「自动命名」在消息与实际写入时产生两个时间戳
        cfg.output_excel = str(
            resolve_output_excel(cfg.output_excel, image_folder=cfg.image_folder)
        )
        out_preview = cfg.output_excel
        scope = f"max={cfg.max_images}" if cfg.max_images > 0 else "all"
        selected_checkpoint = bool(cfg.checkpoint_db)
        try:
            if selected_checkpoint:
                checkpoint_probe = ResultJobStore(
                    cfg.image_folder,
                    top_n=cfg.top_n,
                    scope=scope,
                    database_path=cfg.checkpoint_db,
                    require_existing=True,
                )
                cfg.checkpoint_db = str(checkpoint_probe.path)
                checkpoint_probe.close()
            else:
                cfg.checkpoint_db = str(
                    create_checkpoint_path(cfg.image_folder, max_images=cfg.max_images)
                )
                cfg.checkpoint_is_new = True
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"断点库检查失败：{exc}"}
        session = get_resident_session(headless=cfg.headless)
        if not session.login_window_confirmed:
            return {"ok": False, "error": "请先打开登录页并点击“我看到登录页面”完成可见性确认。"}
        evidence = await session.check_logged_in()
        if not evidence.authenticated:
            return {
                "ok": False,
                "error": "未取得受限页面返回的账户信息，拒绝开始图片处理。请先完成登录后重新检查。",
                "evidence": evidence.to_dict(),
            }

        async def _job() -> dict[str, Any]:
            return await run_pipeline(cfg, session=session)

        state["active_config"] = cfg
        task = asyncio.create_task(_job())
        state["task"] = task

        def _done(t: asyncio.Task) -> None:
            state["active_config"] = None
            try:
                result = t.result()
                logger.info("任务结束 ok={} out={}", result.get("ok"), result.get("outputExcel"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("任务异常: {}", exc)

        task.add_done_callback(_done)
        return {
            "ok": True,
            "message": f"登录已确认（{evidence.account_name}），已开始处理；结果将写入: {out_preview}",
            "outputExcel": out_preview,
            "checkpointDb": cfg.checkpoint_db,
            "pacing": pacing_config_dict(cfg),
        }

    @app.post("/pacing")
    def update_pacing(body: PacingBody) -> dict[str, Any]:
        cfg = state.get("active_config")
        task = state.get("task")
        if cfg is None or task is None or task.done():
            return {
                "ok": False,
                "error": "当前没有运行中的任务。参数已保存在页面，下次点击开始处理时生效。",
            }
        applied = _apply_pacing(cfg, body)
        record_technical_event("pacing_config_updated", **applied)
        return {
            "ok": True,
            "message": "节奏参数已更新；从下一次等待/限额判断开始生效，当前倒计时保持不变。",
            "pacing": applied,
        }

    @app.post("/stop")
    async def stop() -> dict[str, Any]:
        request_stop()
        # 关闭当前会话会立即取消正在等待的页面导航；下一轮不会再处理图片。
        await stop_resident_session()
        task = state.get("task")
        if task is not None and not task.done():
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=120)
            except asyncio.TimeoutError:
                return {
                    "ok": True,
                    "message": "任务已停止，正在后台生成阶段 Excel，请查看进度状态。",
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": f"任务已停止，但阶段 Excel 生成异常：{exc}",
                }
            return {
                "ok": True,
                "message": result.get("message") or "任务已停止并生成阶段 Excel",
                "outputExcel": result.get("outputExcel", ""),
                "exportedRows": result.get("exportedRows", 0),
            }
        return {"ok": True, "message": "当前没有运行中的任务，浏览器会话已关闭"}

    @app.post("/resume")
    def resume() -> dict[str, Any]:
        if not request_resume():
            return {"ok": False, "error": "任务已停止，请重新开始任务"}
        return {
            "ok": True,
            "message": "已继续。请确认已在浏览器完成验证或登录，当前图片将重新处理。",
        }

    @app.post("/shutdown-browser")
    async def shutdown_browser() -> dict[str, Any]:
        await stop_resident_session()
        return {"ok": True, "message": "浏览器已关闭，可重新打开登录"}

    return app
