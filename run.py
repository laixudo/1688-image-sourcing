#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1688 图搜比价：本地图片文件夹 → 图搜 → 结果 Excel（FastAPI 双模式）。"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from loguru import logger

_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from src.pipeline import config_from_params, run_pipeline  # noqa: E402
from src.server import create_app  # noqa: E402

_stop = False


def _on_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
    global _stop
    _stop = True
    logger.warning("收到中断信号 {}", signum)


def load_stdin_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def resolve_plugin_params(payload: dict[str, Any]) -> dict[str, Any]:
    pp = payload.get("pluginParams")
    if isinstance(pp, dict) and pp:
        return pp
    rp = payload.get("runParams")
    if isinstance(rp, dict):
        return rp
    return {}


def _has_nonempty_values(params: Any) -> bool:
    if not isinstance(params, dict) or not params:
        return False
    for value in params.values():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            if value:
                return True
            continue
        if str(value).strip():
            return True
    return False


def detect_mode(payload: dict[str, Any]) -> str:
    """与宿主 is_task_mode_payload 对齐。"""
    export_path = str(payload.get("exportPath") or "").strip()
    if not export_path:
        return "resident"
    if _has_nonempty_values(payload.get("pluginParams")) or _has_nonempty_values(
        payload.get("runParams")
    ):
        return "task"
    return "resident"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _init_logger(log_file: Path) -> None:
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_file),
        level="DEBUG",
        encoding="utf-8",
        rotation="10 MB",
        retention=5,
    )


def _write_result(export_path: Path, result: dict[str, Any]) -> Path:
    target = export_path / "result.json"
    target.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def run_task_mode(payload: dict[str, Any]) -> int:
    import asyncio

    export_path = Path(str(payload["exportPath"]))
    export_path.mkdir(parents=True, exist_ok=True)
    _init_logger(export_path / "plugin.log")

    params = resolve_plugin_params(payload)
    image_folder = str(params.get("imageFolder") or "").strip()
    if not image_folder:
        err = {
            "ok": False,
            "error": "参数校验失败：imageFolder 不能为空",
            "message": "参数校验失败：imageFolder 不能为空",
            "finishedAt": utc_now_iso(),
        }
        target = _write_result(export_path, err)
        logger.error("校验失败，已写入 {}", target)
        return 1

    # 宿主可能注入 browserProfilePath
    profile = str(payload.get("browserProfilePath") or "").strip()
    if profile:
        from src.browser import BrowserSession

        session_factory = lambda: BrowserSession(  # noqa: E731
            profile_dir=profile,
            headless=bool(params.get("headless")),
        )
    else:
        session_factory = None

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:  # noqa: BLE001
        pass

    cfg = config_from_params(params)
    if cfg.headless:
        err = {
            "ok": False,
            "error": "安全运行模式要求关闭无头浏览器，以便用户登录并在需要时手动完成验证。",
            "message": "安全运行模式要求关闭无头浏览器。",
            "finishedAt": utc_now_iso(),
        }
        _write_result(export_path, err)
        return 1
    from src.excel_io import resolve_output_excel

    cfg.output_excel = str(
        resolve_output_excel(cfg.output_excel, image_folder=cfg.image_folder)
    )
    logger.info(
        "任务模式启动 folder={} out={} topN={}",
        cfg.image_folder,
        cfg.output_excel,
        cfg.top_n,
    )

    async def _run() -> dict[str, Any]:
        session = session_factory() if session_factory else None
        return await run_pipeline(
            cfg,
            session=session,
            should_stop=lambda: _stop,
        )

    try:
        summary = asyncio.run(_run())
        result = {
            "ok": bool(summary.get("ok", True)),
            "finishedAt": utc_now_iso(),
            "summary": summary,
        }
        target = _write_result(export_path, result)
        logger.info("已写入 {}", target)
        return 0 if result["ok"] else 1
    except Exception as exc:  # noqa: BLE001
        err = {
            "ok": False,
            "error": str(exc),
            "message": str(exc),
            "finishedAt": utc_now_iso(),
        }
        target = _write_result(export_path, err)
        logger.exception("任务失败，已写入 {}", target)
        return 1


def run_resident_mode(plugin_id: str, port: int) -> int:
    log_file = _PLUGIN_ROOT / "storage" / "logs" / "plugin.log"
    _init_logger(log_file)
    # 用 ASCII 日志避免 Windows 控制台 GBK 乱码；服务本身已是 UTF-8
    logger.warning(
        "resident mode started plugin_id={} port={} version={}",
        plugin_id,
        port,
        json.loads((_PLUGIN_ROOT / "manifest.json").read_text(encoding="utf-8")).get(
            "version", "?"
        ),
    )
    app = create_app(plugin_id)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    try:
        payload = load_stdin_payload()
    except json.JSONDecodeError as exc:
        logger.error("stdin JSON 无效，回退长驻: {}", exc)
        payload = {}

    if detect_mode(payload) == "task":
        return run_task_mode(payload)
    return run_resident_mode(args.plugin_id, args.port)


if __name__ == "__main__":
    sys.exit(main())
