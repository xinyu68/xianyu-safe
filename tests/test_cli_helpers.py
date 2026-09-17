from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "xianyu-safe"
    / "scripts"
    / "xianyu_cli.py"
)
SPEC = importlib.util.spec_from_file_location("xianyu_cli", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

XianyuError = MODULE.XianyuError
_browser_args = MODULE._browser_args
_capabilities = MODULE._capabilities
_default_profile_dir = MODULE._default_profile_dir
_idempotency_lookup = MODULE._idempotency_lookup
_idempotency_record = MODULE._idempotency_record
_normalize_items = MODULE._normalize_items
_normalize_money = MODULE._normalize_money
_parser = MODULE._parser
_validate_images = MODULE._validate_images
_validate_item_id = MODULE._validate_item_id


class NormalizeItemsTest(unittest.TestCase):
    def test_deduplicates_and_limits_results(self) -> None:
        raw = [
            {
                "id": "100",
                "url": "https://www.goofish.com/item?id=100",
                "lines": ["MacBook Air M2", "¥5000", "12人想要"],
                "price": "5000",
                "wanted": "12",
            },
            {
                "id": "100",
                "url": "https://www.goofish.com/item?id=100",
                "title": "duplicate",
            },
            {
                "id": "200",
                "url": "https://www.goofish.com/item?id=200",
                "title": "iPhone 15",
                "price": "3999",
            },
        ]

        result = _normalize_items(raw, limit=2)

        self.assertEqual(["100", "200"], [item["id"] for item in result])
        self.assertEqual("MacBook Air M2", result[0]["title"])
        self.assertNotIn("lines", result[0])

    def test_uses_explicit_profile_override(self) -> None:
        with patch.dict(os.environ, {"XIANYU_PROFILE_DIR": "D:/temp/xianyu-profile"}, clear=False):
            self.assertEqual(Path("D:/temp/xianyu-profile").resolve(), _default_profile_dir())

    def test_background_browser_starts_outside_foreground(self) -> None:
        args = _browser_args(background=True)

        self.assertIn("--start-minimized", args)
        self.assertIn("--window-position=-32000,-32000", args)
        self.assertIn("--disable-renderer-backgrounding", args)

    def test_login_browser_has_no_background_switches(self) -> None:
        args = _browser_args(background=False)

        self.assertNotIn("--start-minimized", args)
        self.assertNotIn("--window-position=-32000,-32000", args)

    def test_money_and_item_id_validation(self) -> None:
        self.assertEqual("12.5", _normalize_money("12.50", "price", required=True))
        self.assertEqual("123456", _validate_item_id("123456"))
        with self.assertRaises(XianyuError):
            _normalize_money("0", "price", required=True)
        with self.assertRaises(XianyuError):
            _validate_item_id("abc")

    def test_image_validation_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "sample.jpg"
            image.write_bytes(b"fixture")
            self.assertEqual([str(image.resolve())], _validate_images([str(image)], required=True))
            with self.assertRaises(XianyuError):
                _validate_images([str(Path(temp_dir) / "missing.jpg")], required=True)

    def test_idempotency_replays_same_payload_and_rejects_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir)
            payload = {"item_id": "123456", "action": "unlist"}
            result = {"success": True, "changed": True}
            _idempotency_record(profile, "test-key-123", "manage-item", payload, result)
            self.assertEqual(
                result,
                _idempotency_lookup(profile, "test-key-123", "manage-item", payload),
            )
            with self.assertRaises(XianyuError):
                _idempotency_lookup(
                    profile,
                    "test-key-123",
                    "manage-item",
                    {"item_id": "654321", "action": "delete"},
                )

    def test_parser_exposes_extended_commands(self) -> None:
        parser = _parser()
        search = parser.parse_args(
            ["search", "--keyword", "无人机", "--min-price", "500", "--city", "北京"]
        )
        publish = parser.parse_args(
            [
                "publish",
                "--title",
                "测试",
                "--description",
                "仅测试",
                "--price",
                "1",
                "--images",
                "sample.jpg",
            ]
        )
        self.assertEqual("北京", search.city)
        self.assertFalse(publish.confirm_publish)
        self.assertIn("publish", _capabilities()["write"])


if __name__ == "__main__":
    unittest.main()
