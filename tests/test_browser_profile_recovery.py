from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src import browser
from src.browser import LoginWindowEvidence


class BrowserProfileRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_occupied_profile_uses_new_recovery_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = browser.BrowserSession(profile_dir=root / "原配置")
            expected = (object(), object())
            launch_once = AsyncMock(
                side_effect=[
                    RuntimeError("专用 Chrome 进程在 CDP 就绪前退出；通常表示配置目录被其他 Chrome 占用。"),
                    expected,
                ]
            )
            with (
                patch.object(browser, "_PLUGIN_ROOT", root),
                patch.object(browser.tempfile, "gettempdir", return_value=str(root)),
                patch.object(session, "_launch_visible_chrome_once", launch_once),
            ):
                actual = await session._launch_visible_chrome()

            self.assertEqual(actual, expected)
            self.assertEqual(launch_once.await_count, 2)
            self.assertIn("visible_recovery_", session.profile_dir.name)
            self.assertEqual(
                session.profile_dir.parent,
                root / "1688ImageSourcing" / "browser_profiles",
            )

    async def test_blank_login_redirect_retries_protected_workbench(self) -> None:
        session = browser.BrowserSession()
        page = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        valid = LoginWindowEvidence(
            ok=True,
            screenshot_path="proof.png",
            screenshot_bytes=20_000,
            window_state="normal",
            width=1400,
            height=900,
        )
        session.start = AsyncMock(return_value=page)
        session._login_window_evidence = AsyncMock(
            side_effect=[
                LoginWindowEvidence(ok=False, error="截图或 CDP 校验失败: 登录页截图校验失败"),
                valid,
            ]
        )

        actual = await session.open_login()

        self.assertEqual(actual, valid)
        self.assertEqual(page.goto.await_count, 2)
        self.assertEqual(page.goto.await_args_list[1].args[0], "https://work.1688.com/")


if __name__ == "__main__":
    unittest.main()
