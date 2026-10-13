from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_cli_helpers import MODULE as cli


ITEM_ID = "123456789012"


def detail(state, item_id=ITEM_ID):
    return {"itemDO": {"itemId": item_id, "title": "测试商品", "collectCnt": 999},
            "buyerDO": {"isCollected": state, "favored": not state if type(state) is bool else True}}


class FavoritesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        self.args = SimpleNamespace(command="favorite", item_id=ITEM_ID, confirm_action=True,
                                    idempotency_key="test-001", profile_dir=str(self.profile),
                                    channel="chrome", headless=False, limit=20)
        self.page = SimpleNamespace(wait_for_timeout=AsyncMock())
        self.context = SimpleNamespace(close=AsyncMock())
        self.manager = SimpleNamespace(stop=AsyncMock())
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.open = self.stack.enter_context(patch.object(cli, "_open_context", AsyncMock(return_value=(self.manager, self.context))))
        self.stack.enter_context(patch.object(cli, "_active_page", AsyncMock(return_value=self.page)))
        self.stack.enter_context(patch.object(cli, "_navigate", AsyncMock()))
        self.login = self.stack.enter_context(patch.object(cli, "_favorite_login", AsyncMock()))
        self.request = self.stack.enter_context(patch.object(cli, "_mtop_request", AsyncMock()))

    def writes(self):
        return [call for call in self.request.call_args_list if call.args[1] in cli.FAVORITE_APIS.values()]

    async def test_preview_never_submits_or_creates_receipt(self):
        self.args.confirm_action = False
        self.request.return_value = detail(False)
        result = await cli._run_favorite(self.args)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["would_change"])
        self.assertFalse(result["submitted"])
        self.assertEqual(self.writes(), [])
        self.assertFalse(list(self.profile.iterdir()))

    async def test_add_and_remove_submit_explicit_api_once_after_durable_reservation(self):
        for command in ("favorite", "unfavorite"):
            with self.subTest(command=command):
                self.args.command = command
                self.args.idempotency_key = command + "-001"
                desired = command == "favorite"
                reads = 0
                async def request(page, api, data, **kwargs):
                    nonlocal reads
                    self.assertTrue(kwargs["require_login"])
                    self.assertEqual(data, {"itemId": ITEM_ID})
                    if api in cli.FAVORITE_APIS.values():
                        self.assertEqual(api, cli.FAVORITE_APIS[command])
                        record = cli._favorite_records(self.profile)[self.args.idempotency_key]
                        self.assertEqual(record["result"]["state"], "reserved")
                        return {}
                    reads += 1
                    return detail(not desired if reads == 1 else desired)
                self.request.reset_mock()
                self.request.side_effect = request
                result = await cli._run_favorite(self.args)
                self.assertEqual(result["state"], "verified")
                self.assertEqual(result["is_favorited"], desired)
                self.assertEqual(len(self.writes()), 1)
                count = self.request.call_count
                replay = await cli._run_favorite(self.args)
                self.assertTrue(replay["replayed"])
                self.assertEqual(self.request.call_count, count)

    async def test_already_satisfied_noop_for_both_states(self):
        for command in ("favorite", "unfavorite"):
            self.args.command = command
            self.args.idempotency_key = command
            self.request.return_value = detail(command == "favorite")
            result = await cli._run_favorite(self.args)
            self.assertEqual(result["state"], "already_satisfied")
            self.assertFalse(result["changed"])
        self.assertEqual(self.writes(), [])

    async def test_unknown_response_is_saved_and_never_resent(self):
        self.request.side_effect = [detail(False), TimeoutError("PRIVATE_TOKEN")]
        result = await cli._run_favorite(self.args)
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        count = self.request.call_count
        result = await cli._run_favorite(self.args)
        self.assertTrue(result["replayed"])
        self.assertEqual(self.request.call_count, count)
        self.assertEqual(len(self.writes()), 1)

    async def test_verification_mismatch_only_repeats_reads(self):
        self.request.side_effect = [detail(False), {}, detail(False), detail(False), detail(False)]
        result = await cli._run_favorite(self.args)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.page.wait_for_timeout.await_count, 2)

    async def test_validation_before_submission_preserves_not_submitted_and_no_receipt(self):
        self.request.side_effect = cli._verification_error(["FAIL_SYS_USER_VALIDATE"])
        with self.assertRaises(cli.XianyuError) as error:
            await cli._run_favorite(self.args)
        self.assertEqual(error.exception.code, "human_verification_required")
        self.assertFalse(error.exception.details["submitted"])
        self.assertEqual(error.exception.details["outcome"], "not_submitted")
        self.assertFalse(list(self.profile.iterdir()))
        self.assertEqual(self.writes(), [])

    async def test_validation_after_submission_saves_unknown_before_handoff_and_no_replay(self):
        self.request.side_effect = [detail(False), cli._verification_error(["FAIL_SYS_USER_VALIDATE"])]
        async def check_saved(*args):
            record = cli._favorite_records(self.profile)[self.args.idempotency_key]
            self.assertEqual(record["result"]["state"], "unknown")
        with patch.object(cli, "_close_context", AsyncMock(side_effect=check_saved)):
            with self.assertRaises(cli.XianyuError) as error:
                await cli._run_favorite(self.args)
        self.assertTrue(error.exception.details["submitted"])
        self.assertEqual(error.exception.details["outcome"], "unknown")
        count = self.request.call_count
        result = await cli._run_favorite(self.args)
        self.assertEqual(result["state"], "unknown")
        self.assertTrue(result["replayed"])
        self.assertEqual(self.request.call_count, count)

    async def test_unknown_state_and_wrong_item_stop_before_write(self):
        for data in (detail(None), detail(1), detail("true"), detail(False, "987654321012"),
                     {"itemDO": {"itemId": ITEM_ID, "title": "测试"}, "buyerDO": {"favored": True}}):
            self.request.return_value = data
            with self.assertRaises(cli.XianyuError):
                await cli._run_favorite(self.args)
        self.assertEqual(self.writes(), [])

    async def test_receipt_save_failure_prevents_write(self):
        self.request.return_value = detail(False)
        with patch.object(cli, "_save_favorite_records", side_effect=cli.XianyuError("favorite_receipts_failed", "failed")):
            with self.assertRaises(cli.XianyuError):
                await cli._run_favorite(self.args)
        self.assertEqual(self.writes(), [])

    async def test_corrupt_receipts_are_not_treated_as_absent(self):
        path = self.profile / "xianyu-favorites-receipts.json"
        for value in ("{", "[]", '{"test-001": {}}'):
            path.write_text(value, encoding="utf-8")
            with self.assertRaises(cli.XianyuError) as error:
                await cli._run_favorite(self.args)
            self.assertEqual(error.exception.code, "favorite_receipts_invalid")
        self.request.assert_not_called()

    async def test_invalid_id_and_missing_confirmation_key_do_not_launch_browser(self):
        for item_id, key in (("bad", "valid"), (ITEM_ID, ""), (ITEM_ID, "bad key")):
            self.args.item_id = item_id
            self.args.idempotency_key = key
            with self.assertRaises(cli.XianyuError):
                await cli._run_favorite(self.args)
        self.open.assert_not_called()

    async def test_conflicting_key_rejected_before_network(self):
        self.request.return_value = detail(True)
        await cli._run_favorite(self.args)
        self.request.reset_mock()
        self.args.command = "unfavorite"
        with self.assertRaises(cli.XianyuError) as error:
            await cli._run_favorite(self.args)
        self.assertEqual(error.exception.code, "idempotency_conflict")
        self.request.assert_not_called()

    async def test_login_failure_no_detail_or_write(self):
        self.login.side_effect = cli.XianyuError("not_logged_in", "login required")
        with self.assertRaises(cli.XianyuError):
            await cli._run_favorite(self.args)
        self.request.assert_not_called()
        self.context.close.assert_awaited()
        self.manager.stop.assert_awaited()

    async def test_list_empty_is_valid_and_does_not_query_recommendations(self):
        self.request.return_value = {"items": [], "nextPage": False, "totalCount": 0}
        result = await cli._run_favorites(self.args)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["has_more"])
        self.assertEqual(self.request.call_args.args[1], cli.FAVORITES_LIST_API)

    async def test_list_paginates_deduplicates_and_reports_truncation(self):
        def row(i):
            return {"id": str(100000000000 + i), "title": "商品" + str(i), "price": "100", "itemStatus": 0, "private": "SECRET"}
        self.args.limit = 3
        self.request.side_effect = [
            {"items": [row(1), row(2)], "nextPage": True, "totalCount": 4},
            {"items": [row(2), row(3), row(4)], "nextPage": False, "totalCount": 4},
        ]
        result = await cli._run_favorites(self.args)
        self.assertEqual([i["id"] for i in result["items"]], [str(100000000000 + i) for i in (1, 2, 3)])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["total"], 4)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self.request.call_args_list[1].args[2]["pageNumber"], 2)
        self.assertEqual(self.writes(), [])

    async def test_list_invalid_and_stalled_pages_are_not_empty_success(self):
        for data in ({}, {"items": [], "nextPage": "false"}, {"items": [None], "nextPage": False},
                     {"items": [], "nextPage": False, "totalCount": -1}):
            self.request.return_value = data
            with self.assertRaises(cli.XianyuError):
                await cli._run_favorites(self.args)
        row = {"id": ITEM_ID, "title": "测试"}
        self.request.side_effect = [{"items": [row], "nextPage": True}] * 2
        with self.assertRaises(cli.XianyuError) as error:
            await cli._run_favorites(self.args)
        self.assertEqual(error.exception.code, "favorites_pagination_stalled")

    async def test_parser_and_dispatch_expose_favorites(self):
        for command in ("favorite", "unfavorite"):
            args = cli._parser().parse_args([command, "--item-id", ITEM_ID])
            self.assertFalse(args.confirm_action)
            with patch.object(cli, "_run_favorite", AsyncMock(return_value={"success": True})) as run:
                await cli._dispatch(args)
                run.assert_awaited_once_with(args)
        args = cli._parser().parse_args(["favorites", "--limit", "5"])
        self.assertEqual(args.limit, 5)
        self.assertIn("favorites", cli._capabilities()["read"])
        self.assertIn("unfavorite", cli._capabilities()["write"])


if __name__ == "__main__":
    unittest.main()
