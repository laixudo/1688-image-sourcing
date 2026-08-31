from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import excel_io


class FolderPreviewTest(unittest.TestCase):
    def test_preview_streams_without_full_file_listing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index in range(55):
                (root / f"图片_{index}.jpg").write_bytes(b"image")
            (root / "说明.txt").write_text("not an image", encoding="utf-8")
            (root / "nested.png").mkdir()

            with patch.object(
                excel_io,
                "list_image_files",
                side_effect=AssertionError("preview must not build and sort a full list"),
            ):
                summary = excel_io.summarize_folder(root)

            self.assertEqual(summary["total"], 55)
            self.assertEqual(len(summary["files"]), 50)
            self.assertTrue(summary["truncated"])
            self.assertGreaterEqual(summary["scan_elapsed_ms"], 0)


if __name__ == "__main__":
    unittest.main()
