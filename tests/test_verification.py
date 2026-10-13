from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_cli_helpers import MODULE as cli


API = "mtop.taobao.idle.pc.detail"
SECRET = "PRIVATE_CHALLENGE_TOKEN"


class VerificationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = SimpleNamespace(on=Mock(), close=AsyncMock(), new_cdp_session=AsyncMock())
        self.manager = SimpleNamespace(stop=AsyncMock())
        # Match Playwright's hashable page identity while preserving AsyncMock
        # methods directly (wrapping them in Mock loses their await semantics).
        self.page = type("Page", (), {})()
        self.page.context = self.context
        self.page.url = "https://www.goofish.com/item?id=123456789012&unused=PRIVATE_TOKEN"
        self.page.wait_for_function = AsyncMock()
        self.page.evaluate = AsyncMock()
        self.page.is_closed = Mock(return_value=False)
        self.page.goto = AsyncMock()
        self.page.bring_to_front = AsyncMock()
        self.monitor = cli._VerificationMonitor(self.context, headless=False, timeout=0)
        self.context._xianyu_verification = self.monitor

    def response(self, codes, *, page=None, api=API, challenge=None, host="h5api.m.goofish.com"):
        value = {"ret": codes, "data": {"url": challenge} if challenge else {}}
        return SimpleNamespace(url=f"https://{host}/h5/{api}/1.0/?sign={SECRET}", headers={},
                               body=AsyncMock(return_value=json.dumps(value).encode()),
                               frame=SimpleNamespace(page=page or self.page))

    async def test_network_validation_interrupts_sdk_wait_and_preserves_cause(self):
        calls = 0
        async def operation():
            nonlocal calls
            calls += 1
            await self.monitor.observe(self.response(["FAIL_SYS_USER_VALIDATE::验证", "RGV587_ERROR::blocked"]))
            await asyncio.Event().wait()
        with self.assertRaises(cli.XianyuError) as error:
            await self.monitor.request(self.page, operation())
        self.assertEqual(calls, 1)
        self.assertEqual(error.exception.code, "human_verification_required")
        self.assertEqual(error.exception.details["reason_codes"], ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR"])
        self.assertNotIn(SECRET, str(error.exception.details))

    async def test_preexisting_validation_does_not_start_sdk_operation(self):
        self.monitor.note(self.page, ["FAIL_SYS_USER_VALIDATE"])
        operation = AsyncMock()
        with self.assertRaises(cli.XianyuError):
            await self.monitor.request(self.page, operation())
        operation.assert_not_awaited()

    async def test_no_validation_record_remains_normal_across_multiple_requests(self):
        self.page.evaluate.return_value = {"ok": True, "response": {"ret": ["SUCCESS::调用成功"], "data": {"value": 1}}}
        for _ in range(2):
            self.assertEqual(await cli._mtop_request(self.page, API, {"itemId": "123456789012"}), {"value": 1})
        self.assertFalse(self.monitor.errors)
        await cli._close_context(self.manager, self.context)
        self.page.bring_to_front.assert_not_called()

    async def test_plain_timeout_is_not_mislabeled_validation_or_outage(self):
        self.page.evaluate.return_value = {"ok": False, "ret": ["TIMEOUT::接口超时"]}
        with self.assertRaises(cli.XianyuError) as error:
            await cli._mtop_request(self.page, API, {})
        self.assertEqual(error.exception.code, "mtop_timeout")
        self.assertFalse(self.monitor.errors)

    async def test_direct_sdk_validation_is_also_classified(self):
        self.page.evaluate.return_value = {"ok": False, "ret": ["FAIL_SYS_USER_VALIDATE::验证"]}
        with self.assertRaises(cli.XianyuError) as error:
            await cli._mtop_request(self.page, API, {})
        await cli._close_context(self.manager, self.context)
        self.assertEqual(error.exception.code, "human_verification_required")
        self.assertEqual(error.exception.details["verification"]["state"], "waiting_disabled")
        self.context.close.assert_awaited_once()
        self.manager.stop.assert_awaited_once()

    async def test_validation_in_page_load_stops_before_sdk_dispatch(self):
        await self.monitor.observe(self.response(["RGV587_ERROR::blocked"]))
        with self.assertRaises(cli.XianyuError):
            await cli._mtop_request(self.page, API, {})
        self.page.evaluate.assert_not_awaited()

    async def test_unrelated_hosts_and_pages_do_not_interrupt(self):
        await self.monitor.observe(self.response(["FAIL_SYS_USER_VALIDATE"], host="goofish.com.evil.example"))
        other = Mock()
        await self.monitor.observe(self.response(["FAIL_SYS_USER_VALIDATE"], page=other))
        self.monitor.check(self.page)
        self.assertNotIn(self.page, self.monitor.records)

    async def test_only_success_for_the_blocked_api_proves_completion(self):
        await self.monitor.observe(self.response(["FAIL_SYS_USER_VALIDATE::验证"]))
        await self.monitor.observe(self.response(["SUCCESS::成功"], api="other.api"))
        self.assertFalse(self.monitor.records[self.page]["completed"])
        await self.monitor.observe(self.response(["FAIL_BIZ_ERROR::失败"]))
        self.assertFalse(self.monitor.records[self.page]["completed"])
        await self.monitor.observe(self.response(["SUCCESS::成功"]))
        self.assertTrue(self.monitor.records[self.page]["completed"])
        await self.monitor.observe(self.response(["RGV587_ERROR::新验证"]))
        self.assertFalse(self.monitor.records[self.page]["completed"])

    async def test_window_restore_moves_offscreen_window_back_before_focus(self):
        session = SimpleNamespace(send=AsyncMock(side_effect=[{"windowId": 17}, {}, {}]), detach=AsyncMock())
        self.context.new_cdp_session.return_value = session
        self.assertTrue(await self.monitor.show(self.page))
        calls = session.send.call_args_list
        self.assertEqual(calls[1].args[1]["bounds"], {"windowState": "normal"})
        self.assertEqual(calls[2].args[1]["bounds"]["left"], 80)
        self.page.bring_to_front.assert_awaited_once()
        session.detach.assert_awaited_once()

    async def test_handoff_waits_for_human_then_saves_session_without_business_replay(self):
        self.monitor.timeout = 1
        self.monitor.profile_dir = "test-profile"
        challenge = "https://h5api.m.goofish.com/_____tmd_____/punish?token=" + SECRET
        await self.monitor.observe(self.response(["FAIL_SYS_USER_VALIDATE::验证"], challenge=challenge))
        with self.assertRaises(cli.XianyuError) as error:
            self.monitor.check(self.page)
        async def human_completion(*args, **kwargs):
            if args[0] == challenge:
                await self.monitor.observe(self.response(["SUCCESS::成功"]))
        with patch.object(self.monitor, "show", AsyncMock(return_value=True)) as show, \
             patch.object(cli, "_save_storage_state", AsyncMock()) as save, \
             patch.object(self.page, "goto", AsyncMock(side_effect=human_completion)), \
             patch("sys.stderr") as stderr:
            result = await self.monitor.handoff(self.page)
            show.assert_awaited_once()
            save.assert_awaited_once()
            displayed = "".join(str(c.args[0]) for c in stderr.write.call_args_list)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["cookies_saved"])
        self.assertFalse(result["business_actions_resumed"])
        self.assertTrue(result["original_document_discarded"])
        self.assertEqual(error.exception.details["verification"]["state"], "completed")
        self.assertNotIn(SECRET, displayed + str(error.exception.details))
        self.page.evaluate.assert_not_awaited()
        self.assertIs(await self.monitor.handoff(self.page), result)

    async def test_interrupted_write_document_is_destroyed_and_write_challenge_never_opened(self):
        self.monitor.timeout = 1
        challenge = "https://h5api.m.goofish.com/_____tmd_____/punish?writeIntent=" + SECRET
        await self.monitor.observe(self.response(["FAIL_SYS_USER_VALIDATE"], api=cli.FAVORITE_APIS["favorite"], challenge=challenge))
        async def read_success(url, **kwargs):
            if url.startswith("https://www.goofish.com/item?"):
                await self.monitor.observe(self.response(["SUCCESS::成功"], api=API))
        self.page.goto.side_effect = read_success
        with patch.object(self.monitor, "show", AsyncMock(return_value=True)), patch("sys.stderr"):
            result = await self.monitor.handoff(self.page)
        urls = [call.args[0] for call in self.page.goto.call_args_list]
        self.assertEqual(urls, ["about:blank", "https://www.goofish.com/item?id=123456789012"])
        self.assertNotIn(challenge, urls)
        self.assertEqual(result["state"], "completed")
        self.page.evaluate.assert_not_awaited()

    async def test_cannot_discard_original_document_does_not_expose_live_write(self):
        self.monitor.timeout = 1
        self.monitor.note(self.page, ["FAIL_SYS_USER_VALIDATE"], api=cli.FAVORITE_APIS["favorite"])
        self.page.goto.side_effect = RuntimeError("navigation failed")
        with patch.object(self.monitor, "show", AsyncMock()) as show:
            result = await self.monitor.handoff(self.page)
        self.assertEqual(result["state"], "navigation_failed")
        self.assertFalse(result["original_document_discarded"])
        show.assert_not_awaited()

    async def test_headless_reports_explicit_visible_verify_command(self):
        self.monitor.headless = True
        self.monitor.timeout = 180
        self.monitor.note(self.page, ["FAIL_SYS_USER_VALIDATE"])
        with self.assertRaises(cli.XianyuError) as error:
            self.monitor.check(self.page)
        result = await self.monitor.handoff(self.page)
        self.assertEqual(result["state"], "headless_requires_verify")
        self.assertIn("verify --item-id", error.exception.message)
        self.page.bring_to_front.assert_not_called()

    async def test_closed_page_and_show_failure_do_not_claim_completion(self):
        self.monitor.timeout = 1
        self.page.is_closed.return_value = True
        self.assertEqual((await self.monitor.handoff(self.page))["state"], "browser_closed")
        self.monitor.handoff_result = None
        self.page.is_closed.return_value = False
        with patch.object(self.monitor, "show", AsyncMock(return_value=False)):
            self.assertEqual((await self.monitor.handoff(self.page))["state"], "show_failed")

    async def test_deadline_closes_context_and_never_calls_sdk(self):
        self.monitor.timeout = 1
        self.monitor.note(self.page, ["FAIL_SYS_USER_VALIDATE"])
        with self.assertRaises(cli.XianyuError):
            self.monitor.check(self.page)
        with patch.object(self.monitor, "show", AsyncMock(return_value=True)), \
             patch.object(cli.time, "monotonic", side_effect=[0, 2]), patch("sys.stderr"):
            await cli._close_context(self.manager, self.context)
        self.assertEqual(self.monitor.handoff_result["state"], "timed_out")
        self.page.evaluate.assert_not_awaited()
        self.context.close.assert_awaited_once()

    async def test_sdk_deadline_is_bounded_and_cancels_wait(self):
        operation = AsyncMock()
        with patch.object(cli.asyncio, "wait", AsyncMock(return_value=(set(), set()))):
            with self.assertRaises(cli.XianyuError) as error:
                await self.monitor.request(self.page, operation())
        self.assertEqual(error.exception.code, "mtop_timeout")

    async def test_verify_command_forces_visible_same_profile_and_never_favorites(self):
        args = cli._parser().parse_args(["--headless", "verify", "--item-id", "123456789012", "--timeout", "30"])
        with patch.object(cli, "_open_context", AsyncMock(return_value=(self.manager, self.context))) as opened, \
             patch.object(cli, "_active_page", AsyncMock(return_value=self.page)), \
             patch.object(cli, "_navigate", AsyncMock()), \
             patch.object(self.monitor, "handoff", AsyncMock(return_value={"state": "completed"})), \
             patch.object(cli, "_mtop_request", AsyncMock()) as request:
            result = await cli._dispatch(args)
        self.assertTrue(result["success"])
        self.assertFalse(result["submitted"])
        self.assertFalse(opened.call_args.args[2])
        self.assertFalse(opened.call_args.kwargs["background"])
        request.assert_not_awaited()

    def test_verification_url_requires_official_https_challenge_and_never_api(self):
        good = "https://h5api.m.goofish.com/_____tmd_____/punish?token=" + SECRET
        self.assertEqual(cli._official_verification_url(good), good)
        for value in ("https://evil.example/captcha", "https://goofish.com.evil.example/punish", "http://www.goofish.com/punish",
                      "https://www.goofish.com:444/punish", "https://user@www.goofish.com/punish", "https://h5api.m.goofish.com/h5/mtop.taobao.idle.collect.item/1.0/"):
            self.assertIsNone(cli._official_verification_url(value))


if __name__ == "__main__":
    unittest.main()
