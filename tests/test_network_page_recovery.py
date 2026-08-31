from __future__ import annotations

import unittest

from src.image_search import is_recoverable_page_network_error


class NetworkPageRecoveryTest(unittest.TestCase):
    def test_recognizes_browser_network_failures(self) -> None:
        messages = (
            "Page.evaluate: TypeError: Failed to fetch",
            "page.goto: net::ERR_CONNECTION_RESET",
            "Page has crashed",
            "Target closed",
            "Execution context was destroyed",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(is_recoverable_page_network_error(RuntimeError(message)))

    def test_does_not_refresh_business_or_verification_errors(self) -> None:
        messages = (
            "图搜无可信结果",
            "1688 页面要求人工验证",
            "没有解析到商品卡片",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(is_recoverable_page_network_error(RuntimeError(message)))


if __name__ == "__main__":
    unittest.main()
