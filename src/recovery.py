# -*- coding: utf-8 -*-
"""Conservative page-state classification for recovery decisions.

Unknown state is intentionally a pause, never a refresh.  This module does not
solve or bypass verification; it only decides whether an automatic page reload
is safe enough to attempt.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from .image_search import _is_captcha_page, is_recoverable_page_network_error


class PageState(str, Enum):
    CHALLENGE = "challenge"
    SIGNED_OUT = "signed_out"
    NETWORK = "network"
    UNKNOWN = "unknown"
    HEALTHY = "healthy"


class ProbeState(str, Enum):
    AUTHENTICATED = "authenticated"
    SIGNED_OUT = "signed_out"
    CHALLENGE = "challenge"
    PROBE_NETWORK_ERROR = "probe_network_error"
    PROBE_UNEXPECTED_RESPONSE = "probe_unexpected_response"


LOGIN_HOSTS = {"login.taobao.com", "login.1688.com", "login.alibaba.com"}
LOGIN_PATHS = {"/", "/login.htm", "/member/jump.htm"}


@dataclass(frozen=True)
class Classification:
    state: PageState
    reason: str
    local_ms: int


@dataclass(frozen=True)
class ProbeResult:
    state: ProbeState
    reason: str
    attempts: int
    total_ms: int
    account_name: str = ""


def is_exact_login_location(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "/").lower()
    if host not in LOGIN_HOSTS:
        return False
    return path in LOGIN_PATHS or path.startswith("/login/")


async def classify_page(page: Any, exc: BaseException | None = None) -> Classification:
    started = time.perf_counter()
    url = str(getattr(page, "url", "") or "") if page is not None else ""
    try:
        if page is not None and not page.is_closed() and await _is_captcha_page(page):
            state, reason = PageState.CHALLENGE, "可见验证码组件或验证页特征"
        elif is_exact_login_location(url):
            state, reason = PageState.SIGNED_OUT, f"精确登录地址: {url[:160]}"
        elif exc is not None and is_recoverable_page_network_error(exc):
            state, reason = PageState.NETWORK, f"网络白名单错误: {type(exc).__name__}: {exc}"
        elif exc is None:
            state, reason = PageState.HEALTHY, "未发现验证、退出登录或网络错误"
        else:
            state, reason = PageState.UNKNOWN, f"未分类异常: {type(exc).__name__}: {exc}"
    except Exception as classify_exc:  # noqa: BLE001
        state, reason = PageState.UNKNOWN, f"分类器自身失败: {type(classify_exc).__name__}: {classify_exc}"
    return Classification(
        state=state,
        reason=reason,
        local_ms=round((time.perf_counter() - started) * 1000),
    )


async def strict_auth_probe(session: Any) -> ProbeResult:
    """Probe network failures are retried separately and never imply signed-out."""
    started = time.perf_counter()
    delays = (0, 3, 10)
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            await asyncio.sleep(delay)
        evidence = await session.check_logged_in()
        reason = str(getattr(evidence, "reason", "") or "")
        if bool(getattr(evidence, "authenticated", False)):
            return ProbeResult(
                ProbeState.AUTHENTICATED, reason, attempt,
                round((time.perf_counter() - started) * 1000),
                str(getattr(evidence, "account_name", "") or ""),
            )
        lowered = reason.lower()
        if "验证" in reason or "challenge" in lowered or "captcha" in lowered:
            state = ProbeState.CHALLENGE
        elif "重定向至登录页" in reason or "未登录" in reason:
            state = ProbeState.SIGNED_OUT
        elif any(marker in lowered for marker in ("timeout", "超时", "net::err", "请求失败", "连接")):
            state = ProbeState.PROBE_NETWORK_ERROR
        else:
            state = ProbeState.PROBE_UNEXPECTED_RESPONSE
        if state is not ProbeState.PROBE_NETWORK_ERROR or attempt == len(delays):
            return ProbeResult(
                state, reason, attempt, round((time.perf_counter() - started) * 1000)
            )
    raise AssertionError("unreachable")
