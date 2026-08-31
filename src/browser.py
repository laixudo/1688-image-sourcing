# -*- coding: utf-8 -*-
"""Playwright 浏览器会话与登录态。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# 1688 登录会跳淘宝；短信依赖阿里云 nocaptcha，自动化 Chromium 上常失败
LOGIN_URL = (
    "https://login.taobao.com/?"
    "redirect_url=https%3A%2F%2Flogin.1688.com%2Fmember%2Fjump.htm"
    "%3Ftarget%3Dhttps%253A%252F%252Fwork.1688.com%252F"
    "&style=tao_custom&from=1688web"
)

_LOGIN_HOSTS = {"login.taobao.com", "login.1688.com", "login.alibaba.com"}
_GENERIC_ACCOUNT_LABELS = {
    "我的阿里", "账号管理", "退出", "登录", "消息", "采购车", "免费注册", "立即注册",
}


@dataclass(frozen=True)
class LoginEvidence:
    authenticated: bool
    account_name: str = ""
    endpoint: str = "https://work.1688.com/"
    final_url: str = ""
    http_status: int | None = None
    elapsed_ms: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LoginWindowEvidence:
    ok: bool
    screenshot_path: str = ""
    screenshot_bytes: int = 0
    window_state: str = ""
    width: int = 0
    height: int = 0
    corrected: bool = False
    warning: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BrowserSession:
    """长驻/任务共用的浏览器会话。"""

    def __init__(
        self,
        *,
        profile_dir: str | Path | None = None,
        headless: bool = False,
    ) -> None:
        # 不复用历史开发/测试会话，避免把未知账号误当成当前用户已登录。
        # 旧 storage/browser_profile 保留，绝不自动删除。
        # Chrome 151's Windows GPU sandbox can fail with STATUS_ACCESS_DENIED
        # when user-data-dir is on a non-system drive. Keep the dedicated
        # profile under the user TEMP directory on C:; project data/checkpoints remain on E:.
        browser_data_root = Path(tempfile.gettempdir()) / "1688ImageSourcing"
        default_profile = browser_data_root / "browser_profile_v1_visible"
        env_profile = os.getenv("BROWSER_PROFILE_DIR", str(default_profile))
        raw = Path(profile_dir or env_profile)
        self.profile_dir = raw if raw.is_absolute() else (_PLUGIN_ROOT / raw)
        self.requested_headless = headless
        # 可视性校验依赖真实窗口；禁止任何调用方启用无头模式。
        self.headless = False
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome_process: subprocess.Popen[Any] | None = None
        self._cdp_port: int | None = None
        self._lock = asyncio.Lock()
        self._last_login_window: LoginWindowEvidence | None = None
        self._login_window_confirmed = False

    @property
    def _cdp_metadata_path(self) -> Path:
        return self.profile_dir / ".plugin_cdp.json"

    async def _cdp_is_ready(self, port: int, *, timeout: float = 1.0) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"http://127.0.0.1:{port}/json/version")
            return response.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def _connect_existing_visible_chrome(self) -> tuple[Browser, BrowserContext] | None:
        """服务重启后复用仍在运行的专用 Chrome，避免配置目录单例冲突。"""
        metadata = self._cdp_metadata_path
        if not metadata.is_file():
            return None
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            port = int(payload["port"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("VISIBLE_BROWSER_REUSE metadata_invalid path={} error={}", metadata, exc)
            metadata.unlink(missing_ok=True)
            return None
        if not await self._cdp_is_ready(port):
            logger.warning("VISIBLE_BROWSER_REUSE endpoint_stale port={} path={}", port, metadata)
            metadata.unlink(missing_ok=True)
            return None
        assert self._pw is not None
        browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        if not browser.contexts:
            await browser.close()
            metadata.unlink(missing_ok=True)
            return None
        self._cdp_port = port
        logger.info("VISIBLE_BROWSER_REUSE connected port={} profile={}", port, self.profile_dir)
        return browser, browser.contexts[0]

    @property
    def page(self) -> Page | None:
        return self._page

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    def adopt_page(self, page: Page) -> None:
        """图搜若打开新标签，切换会话主页面。"""
        self._page = page

    @property
    def ready(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def start(self) -> Page:
        async with self._lock:
            if self.ready and self._page is not None:
                return self._page

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                "BROWSER_LAUNCH headless_effective=False source=hardcoded_visibility_requirement "
                "headless_requested={} env.HEADLESS={!r} env.BROWSER_HEADLESS={!r} env.CI={!r} env.DISPLAY={!r}",
                self.requested_headless,
                os.getenv("HEADLESS"),
                os.getenv("BROWSER_HEADLESS"),
                os.getenv("CI"),
                os.getenv("DISPLAY"),
            )
            self._pw = await async_playwright().start()
            try:
                existing = await self._connect_existing_visible_chrome()
                self._browser, self._context = (
                    existing if existing is not None else await self._launch_visible_chrome()
                )
            except Exception:
                # 启动失败不能留下半初始化的 Playwright；否则下一次点击会继续
                # 堆积驱动进程并掩盖真正错误。
                await self._pw.stop()
                self._pw = None
                self._browser = None
                self._context = None
                self._chrome_process = None
                self._cdp_port = None
                raise
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            logger.info(
                "浏览器已启动 profile={} headless={}",
                self.profile_dir,
                self.headless,
            )
            return self._page

    @staticmethod
    def _find_chrome_executable() -> Path:
        configured = os.getenv("VISIBLE_CHROME_EXE", "").strip()
        candidates = [
            configured,
            shutil.which("chrome") or "",
            shutil.which("msedge") or "",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for raw in candidates:
            if raw and Path(raw).is_file():
                return Path(raw)
        raise FileNotFoundError("未找到 Chrome/Edge 可执行文件；可通过 VISIBLE_CHROME_EXE 指定")

    @staticmethod
    def _free_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def _launch_visible_chrome(self) -> tuple[Browser, BrowserContext]:
        """Launch the dedicated browser and recover from an orphaned profile lock."""
        original_profile = self.profile_dir
        try:
            return await self._launch_visible_chrome_once()
        except RuntimeError as exc:
            if "配置目录被其他 Chrome 占用" not in str(exc):
                raise
            recovery_root = Path(tempfile.gettempdir()) / "1688ImageSourcing" / "browser_profiles"
            recovery_root.mkdir(parents=True, exist_ok=True)
            # Keep this internal path ASCII-only for maximum Chromium compatibility.
            self.profile_dir = recovery_root / f"visible_recovery_{int(time.time() * 1000)}"
            logger.warning(
                "VISIBLE_BROWSER_PROFILE_RECOVERY occupied_profile={} recovery_profile={} error={}",
                original_profile,
                self.profile_dir,
                exc,
            )
            return await self._launch_visible_chrome_once()

    async def _launch_visible_chrome_once(self) -> tuple[Browser, BrowserContext]:
        assert self._pw is not None
        executable = self._find_chrome_executable()
        self._cdp_port = self._free_local_port()
        args = [
            str(executable),
            f"--remote-debugging-port={self._cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.profile_dir}",
            "--new-window",
            "--window-position=50,50",
            "--window-size=1400,900",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "about:blank",
        ]
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1  # Win32 SW_SHOWNORMAL
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        logger.info(
            "VISIBLE_BROWSER_PROCESS exe={} cdp_port={} window=SW_SHOWNORMAL profile={}",
            executable,
            self._cdp_port,
            self.profile_dir,
        )
        self._chrome_process = subprocess.Popen(
            args,
            cwd=str(executable.parent),
            startupinfo=startup,
            creationflags=flags,
            close_fds=True,
        )
        endpoint = f"http://127.0.0.1:{self._cdp_port}"
        last_error = ""
        async with httpx.AsyncClient(timeout=1.0) as client:
            for _ in range(40):
                if self._chrome_process.poll() is not None:
                    raise RuntimeError(
                        "专用 Chrome 进程在 CDP 就绪前退出；通常表示配置目录被其他 "
                        f"Chrome 占用。exit_code={self._chrome_process.returncode} "
                        f"profile={self.profile_dir}"
                    )
                try:
                    response = await client.get(f"{endpoint}/json/version")
                    if response.status_code == 200:
                        break
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError(f"可见浏览器已启动，但 CDP 未就绪: {last_error}")
        browser = await self._pw.chromium.connect_over_cdp(endpoint)
        if not browser.contexts:
            raise RuntimeError("CDP 已连接但没有浏览器上下文")
        self._cdp_metadata_path.write_text(
            json.dumps(
                {"port": self._cdp_port, "pid": self._chrome_process.pid, "profile": str(self.profile_dir)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info(
            "VISIBLE_BROWSER_PROCESS ready pid={} cdp_port={} metadata={}",
            self._chrome_process.pid,
            self._cdp_port,
            self._cdp_metadata_path,
        )
        return browser, browser.contexts[0]

    async def stop(self) -> None:
        async with self._lock:
            launched = self._chrome_process
            try:
                if self._browser is not None:
                    await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭浏览器上下文失败: {}", exc)
            try:
                if self._pw is not None:
                    await self._pw.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止 Playwright 失败: {}", exc)
            if launched is not None and launched.poll() is None:
                try:
                    launched.terminate()
                    await asyncio.to_thread(launched.wait, 5)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("终止专用可见 Chrome 失败，将强制结束 pid={}: {}", launched.pid, exc)
                    try:
                        launched.kill()
                    except Exception as kill_exc:  # noqa: BLE001
                        logger.warning("强制结束专用 Chrome 失败 pid={}: {}", launched.pid, kill_exc)
            self._page = None
            self._context = None
            self._browser = None
            self._pw = None
            self._chrome_process = None
            self._cdp_port = None
            self._cdp_metadata_path.unlink(missing_ok=True)
            logger.info("浏览器已关闭")

    async def _capture_login_screenshot(self, page: Page, *, suffix: str = "") -> tuple[Path, int]:
        debug_dir = _PLUGIN_ROOT / "storage" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        path = debug_dir / f"login_visible_check_{stamp}{suffix}.png"
        try:
            await page.screenshot(path=str(path), full_page=True, timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            # 淘宝登录页偶尔会让 Playwright 的 full_page 截图卡在字体/布局
            # 稳定等待。改用同一页面的 CDP 原生截图作为可验证兜底，不能
            # 因截图黑盒超时而让用户等 30 秒且得不到任何证据。
            logger.warning("LOGIN_VISIBILITY playwright_screenshot_failed fallback=cdp error={}", exc)
            assert self._context is not None
            cdp = await self._context.new_cdp_session(page)
            captured = await cdp.send(
                "Page.captureScreenshot",
                {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            )
            path.write_bytes(base64.b64decode(str(captured["data"])))
        size = path.stat().st_size if path.is_file() else 0
        logger.info("LOGIN_VISIBILITY screenshot path={} bytes={}", path, size)
        if size <= 10 * 1024:
            raise RuntimeError(f"登录页截图校验失败：文件大小 {size}B，不满足大于 10KB 的要求")
        return path, size

    async def _login_window_evidence(self, page: Page) -> LoginWindowEvidence:
        """截图 + CDP 窗口状态；任一环节失败都返回不可确认。"""
        networkidle_error = ""
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception as exc:  # noqa: BLE001
            # 登录页常保留长连接；仍必须继续截图和 CDP 取证，最终不会因此返回成功。
            networkidle_error = f"登录页未达到 networkidle: {type(exc).__name__}: {exc}"
            logger.warning("LOGIN_VISIBILITY {}", networkidle_error)
        try:
            assert self._context is not None
            cdp = await self._context.new_cdp_session(page)
            info = await cdp.send("Browser.getWindowForTarget")
            window_id = int(info["windowId"])
            bounds = await cdp.send("Browser.getWindowBounds", {"windowId": window_id})
            raw = bounds.get("bounds") or {}
            state = str(raw.get("windowState") or "normal")
            width, height = int(raw.get("width") or 0), int(raw.get("height") or 0)
            corrected = False
            logger.info("LOGIN_VISIBILITY cdp_before windowId={} state={} width={} height={}", window_id, state, width, height)
            # 即使 CDP 报 normal，窗口也可能沿用上次关闭前的屏外坐标。
            # 每次登录都把专用窗口放回主屏左上方，并把当前标签带到前台。
            await cdp.send("Page.bringToFront")
            if state != "normal" or width < 300 or height < 300:
                corrected = True
                await cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {
                            "windowState": "normal", "left": 50, "top": 50,
                            "width": 1400, "height": 900,
                        },
                    },
                )
                await page.wait_for_timeout(600)
                bounds = await cdp.send("Browser.getWindowBounds", {"windowId": window_id})
                raw = bounds.get("bounds") or {}
                state = str(raw.get("windowState") or "normal")
                width, height = int(raw.get("width") or 0), int(raw.get("height") or 0)
                logger.info("LOGIN_VISIBILITY cdp_after state={} width={} height={}", state, width, height)
            else:
                corrected = True
                await cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {
                            "windowState": "normal", "left": 50, "top": 50,
                            "width": 1400, "height": 900,
                        },
                    },
                )
                await page.wait_for_timeout(300)
                bounds = await cdp.send("Browser.getWindowBounds", {"windowId": window_id})
                raw = bounds.get("bounds") or {}
                state = str(raw.get("windowState") or "normal")
                width, height = int(raw.get("width") or 0), int(raw.get("height") or 0)
                logger.info("LOGIN_VISIBILITY cdp_repositioned state={} width={} height={}", state, width, height)
            screenshot, size = await self._capture_login_screenshot(
                page, suffix="_after_restore" if corrected else ""
            )
            if state != "normal" or width < 300 or height < 300:
                return LoginWindowEvidence(
                    ok=False, screenshot_path=str(screenshot), screenshot_bytes=size,
                    window_state=state, width=width, height=height, corrected=corrected,
                    error="CDP 未确认正常可见窗口状态",
                )
            return LoginWindowEvidence(
                ok=True, screenshot_path=str(screenshot), screenshot_bytes=size,
                window_state=state, width=width, height=height, corrected=corrected,
                warning=networkidle_error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("LOGIN_VISIBILITY failed: {}", exc)
            return LoginWindowEvidence(ok=False, error=f"截图或 CDP 校验失败: {type(exc).__name__}: {exc}")

    async def open_login(self) -> LoginWindowEvidence:
        """打开登录页后必须完成截图和 CDP 窗口校验，绝不只依据 URL 返回成功。"""
        page = await self.start()
        self._login_window_confirmed = False
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            evidence = LoginWindowEvidence(ok=False, error=f"登录页导航失败: {type(exc).__name__}: {exc}")
            self._last_login_window = evidence
            return evidence
        evidence = await self._login_window_evidence(page)
        if not evidence.ok and "截图校验失败" in evidence.error:
            # An authenticated Taobao session can briefly render a solid blank
            # page when LOGIN_URL redirects away. Navigate to the protected
            # 1688 workbench: signed-out users return to login; signed-in users
            # see their account page. Then repeat the same hard evidence checks.
            logger.warning(
                "LOGIN_VISIBILITY blank_redirect_retry target=https://work.1688.com/ first_error={}",
                evidence.error,
            )
            try:
                await page.goto(
                    "https://work.1688.com/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await page.wait_for_timeout(1000)
                evidence = await self._login_window_evidence(page)
            except Exception as exc:  # noqa: BLE001
                evidence = LoginWindowEvidence(
                    ok=False,
                    error=f"空白登录跳转页恢复失败: {type(exc).__name__}: {exc}",
                )
        self._last_login_window = evidence
        logger.info("LOGIN_VISIBILITY result={}", evidence.to_dict())
        return evidence

    def confirm_login_window_visible(self) -> LoginWindowEvidence:
        evidence = self._last_login_window
        if evidence is None or not evidence.ok:
            return LoginWindowEvidence(ok=False, error="没有通过自动可见性校验的登录页证据，不能确认")
        self._login_window_confirmed = True
        logger.info("LOGIN_VISIBILITY user_confirmed screenshot={}", evidence.screenshot_path)
        return evidence

    @property
    def login_window_confirmed(self) -> bool:
        return self._login_window_confirmed

    @property
    def last_login_window(self) -> LoginWindowEvidence | None:
        """Return the latest automatic visibility evidence for UI state recovery."""
        return self._last_login_window

    async def check_logged_in(self) -> LoginEvidence:
        """严格验证登录：访问需登录的买家工作台，并取得页面实际展示的账号名。

        不读取 Cookie，不依据首页关键词，也不把“能打开页面”单独当作已登录。
        1688 开放平台的会员 API 需要应用 appKey/OAuth session，浏览器 Cookie
        不能合法替代该授权；因此此处使用工作台的真实受限页面作为验证资源。
        """
        page = await self.start()
        endpoint = "https://work.1688.com/"
        started = time.perf_counter()
        try:
            response = await page.goto(endpoint, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(800)
        except Exception as exc:  # noqa: BLE001
            evidence = LoginEvidence(
                authenticated=False,
                endpoint=endpoint,
                final_url=page.url or "",
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                reason=f"受限页面请求失败: {type(exc).__name__}: {exc}",
            )
            logger.warning("AUTH_CHECK {}", evidence.to_dict())
            return evidence

        final_url = page.url or ""
        host = (urlparse(final_url).hostname or "").lower()
        status = response.status if response is not None else None
        challenge_detected = False
        try:
            challenge_selectors = (
                "iframe[src*='captcha'], iframe[src*='punish'], #nc_1_wrapper, "
                "[class*='nc-container'], [class*='captcha'], [id*='captcha']"
            )
            locator = page.locator(challenge_selectors)
            for index in range(min(await locator.count(), 10)):
                if await locator.nth(index).is_visible():
                    box = await locator.nth(index).bounding_box()
                    if box and box.get("width", 0) > 1 and box.get("height", 0) > 1:
                        challenge_detected = True
                        break
        except Exception as exc:  # noqa: BLE001
            logger.warning("AUTH_CHECK challenge detection failed: {}", exc)
        account_name = ""
        try:
            candidates = await page.locator(
                "a[href*='member.1688.com'], [class*='member'] a, [class*='user'] a"
            ).all_inner_texts()
            for raw in candidates:
                name = re.sub(r"\s+", " ", raw or "").strip()
                if 2 <= len(name) <= 64 and name not in _GENERIC_ACCOUNT_LABELS:
                    account_name = name
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("AUTH_CHECK account extraction failed: {}", exc)

        redirected_to_login = host in _LOGIN_HOSTS or "/login" in final_url.lower()
        authenticated = bool(
            status and 200 <= status < 400 and not redirected_to_login
            and not challenge_detected and account_name
        )
        if challenge_detected:
            reason = "受限页面出现人工验证组件"
        elif redirected_to_login:
            reason = "受限页面重定向至登录页"
        elif not status or not 200 <= status < 400:
            reason = f"受限页面 HTTP 状态异常: {status}"
        elif not account_name:
            reason = "受限页面未取得可验证的账号名，不将其判定为已登录"
        else:
            reason = "受限页面返回账号信息"
        evidence = LoginEvidence(
            authenticated=authenticated,
            account_name=account_name,
            endpoint=endpoint,
            final_url=final_url,
            http_status=status,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            reason=reason,
        )
        logger.info("AUTH_CHECK {}", evidence.to_dict())
        return evidence

    async def delay(self) -> None:
        sec = float(os.getenv("ACTION_DELAY_SEC", "1.5"))
        await asyncio.sleep(max(sec, 0.2))


_resident_session: BrowserSession | None = None


def get_resident_session(*, headless: bool = False) -> BrowserSession:
    global _resident_session
    if _resident_session is None:
        _resident_session = BrowserSession(headless=headless)
    return _resident_session


async def stop_resident_session() -> None:
    global _resident_session
    if _resident_session is not None:
        await _resident_session.stop()
        _resident_session = None
