from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src import image_search
from src.errors import ManualActionRequired


class _Locator:
    def __init__(self, *, count: int = 0, visible: bool = False, text: str = "") -> None:
        self._count = count
        self._visible = visible
        self._text = text
        self.first = self

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible

    async def inner_text(self, timeout: int = 0) -> str:  # noqa: ARG002
        return self._text


class _Page:
    def __init__(self, *, url: str, title: str, body: str, captcha_visible: bool = False) -> None:
        self.url = url
        self._title = title
        self._body = body
        self._captcha_visible = captcha_visible

    async def title(self) -> str:
        return self._title

    def locator(self, selector: str) -> _Locator:
        if selector == "body":
            return _Locator(count=1, visible=True, text=self._body)
        if self._captcha_visible and selector == "#nocaptcha":
            return _Locator(count=1, visible=True)
        return _Locator()


class _Session:
    def __init__(self, page: _Page) -> None:
        self.page = page

    async def start(self) -> _Page:
        return self.page

    async def delay(self) -> None:
        await asyncio.sleep(0)


class CaptchaHandlingTest(unittest.TestCase):
    def test_normal_workbench_with_nc_token_is_not_captcha(self) -> None:
        page = _Page(
            url="https://work.1688.com/",
            title="1688-买家工作台",
            body="首页 交易 分销管理 nc_token 采购车",
        )
        self.assertFalse(asyncio.run(image_search._is_captcha_page(page)))

    def test_visible_captcha_component_is_captcha(self) -> None:
        page = _Page(
            url="https://work.1688.com/",
            title="1688-买家工作台",
            body="",
            captcha_visible=True,
        )
        self.assertTrue(asyncio.run(image_search._is_captcha_page(page)))

    def test_manual_action_is_not_wrapped_as_navigation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            image = Path(raw_tmp) / "one.jpg"
            image.write_bytes(b"test")
            page = _Page(
                url="https://work.1688.com/",
                title="1688-买家工作台",
                body="正常工作台",
            )
            session = _Session(page)
            expected = ManualActionRequired("需要人工验证")
            with patch.object(
                image_search,
                "_wait_captcha_cleared",
                new=AsyncMock(side_effect=expected),
            ):
                with self.assertRaises(ManualActionRequired):
                    asyncio.run(image_search.search_by_images(session, [str(image)], max_images=1))


if __name__ == "__main__":
    unittest.main()
