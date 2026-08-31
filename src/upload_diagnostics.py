# -*- coding: utf-8 -*-
"""Playwright 上传链路诊断：真实 request/response/requestfailed 事件落盘。"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Page, Request, Response

_ROOT = Path(__file__).resolve().parent.parent
_SENSITIVE = ("cookie", "authorization", "token", "sign", "secret", "session")


def _safe_value(name: str, value: str) -> str:
    if any(mark in name.lower() for mark in _SENSITIVE):
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"<redacted len={len(value)} sha256={digest}>"
    return value


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, _safe_value(key, value))
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class UploadDiagnostics:
    def __init__(self, page: Page, image_path: Path | None = None) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.path = _ROOT / "storage" / "debug" / f"upload_diagnostics_{stamp}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.page = page
        self.image_path = image_path
        self.request_count = 0
        self.response_count = 0
        self.failed_count = 0
        self._lock = threading.Lock()

    @staticmethod
    def _relevant(url: str) -> bool:
        low = (url or "").lower()
        return "stream-upload.taobao.com" in low or "upload" in low

    def _write(self, event: str, **details: Any) -> None:
        item = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **details,
        }
        line = json.dumps(item, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def _on_request(self, request: Request) -> None:
        if not self._relevant(request.url):
            return
        self.request_count += 1
        headers = {key: _safe_value(key, value) for key, value in request.headers.items()}
        self._write(
            "request",
            method=request.method,
            url=_safe_url(request.url),
            resource_type=request.resource_type,
            headers=headers,
            post_data_bytes=len((request.post_data or "").encode("utf-8", errors="replace")),
        )

    def _on_response(self, response: Response) -> None:
        if not self._relevant(response.url):
            return
        self.response_count += 1
        headers = {key: _safe_value(key, value) for key, value in response.headers.items()}
        self._write("response", status=response.status, url=_safe_url(response.url), headers=headers)

    def _on_failed(self, request: Request) -> None:
        if not self._relevant(request.url):
            return
        self.failed_count += 1
        self._write(
            "requestfailed",
            method=request.method,
            url=_safe_url(request.url),
            failure=request.failure,
        )

    def start(self) -> None:
        self._write(
            "diagnostics_started",
            image_path=str(self.image_path) if self.image_path else "<manual-browser-upload>",
            image_bytes=self.image_path.stat().st_size if self.image_path else None,
        )
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_failed)

    def stop(self) -> dict[str, Any]:
        self.page.remove_listener("request", self._on_request)
        self.page.remove_listener("response", self._on_response)
        self.page.remove_listener("requestfailed", self._on_failed)
        summary = {
            "log_path": str(self.path),
            "request_count": self.request_count,
            "response_count": self.response_count,
            "failed_count": self.failed_count,
        }
        self._write("diagnostics_finished", **summary)
        return summary
