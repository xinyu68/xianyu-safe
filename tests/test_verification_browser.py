"""Opt-in Chromium integration using intercepted fixtures, never live Xianyu."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_cli_helpers import MODULE as cli


HTML = """<!doctype html><title>Offline verification fixture</title><script>
window.fixtureCalls = 0;
window.lib = {mtop: {request: async payload => {
  window.fixtureCalls++;
  const response = await fetch('/h5/' + payload.api + '/1.0/', {method:'POST', body:'fixture'});
  const data = await response.json();
  if (data.ret[0].startsWith('FAIL_SYS_USER_VALIDATE')) return await new Promise(() => {});
  return data;
}}};
</script>"""


@unittest.skipUnless(os.environ.get("XIANYU_BROWSER_TESTS") == "1", "Set XIANYU_BROWSER_TESTS=1 for offline browser fixtures")
class BrowserVerificationTest(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, blocked):
        with tempfile.TemporaryDirectory() as directory:
            manager, context = await cli._open_context(Path(directory), "chrome", True, verification_timeout=0)
            page = await cli._active_page(context)
            requests = []
            async def fixture(route):
                requests.append(route.request.url)
                if "/h5/" in route.request.url:
                    result = {"ret": ["FAIL_SYS_USER_VALIDATE::fixture"] if blocked else ["SUCCESS::fixture"],
                              "data": {"fixture": True}}
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                elif route.request.resource_type == "document":
                    await route.fulfill(status=200, content_type="text/html", body=HTML)
                else:
                    await route.abort()
            await context.route("**/*", fixture)
            try:
                await page.goto("https://www.goofish.com/item?id=123456789012")
                if blocked:
                    with self.assertRaises(cli.XianyuError) as error:
                        await asyncio.wait_for(cli._mtop_request(page, "mtop.taobao.idle.pc.detail", {"itemId": "123456789012"}), timeout=8)
                    self.assertEqual(error.exception.code, "human_verification_required")
                    self.assertEqual(await page.evaluate("window.fixtureCalls"), 1)
                    self.assertEqual(len([url for url in requests if "/h5/" in url]), 1)
                    await cli._close_context(manager, context)
                    self.assertEqual(error.exception.details["verification"]["state"], "headless_requires_verify")
                else:
                    for _ in range(2):
                        self.assertEqual(await cli._mtop_request(page, "mtop.taobao.idle.pc.detail", {}), {"fixture": True})
                    self.assertEqual(await page.evaluate("window.fixtureCalls"), 2)
            finally:
                await cli._close_context(manager, context)

    async def test_real_response_event_interrupts_stuck_sdk(self):
        await self.exercise(True)

    async def test_normal_api_queries_continue_without_window_handoff(self):
        await self.exercise(False)

    async def test_handoff_destroys_real_pending_write_document_and_uses_read_only_proof(self):
        html = HTML.replace("return await new Promise(() => {});", "{ await new Promise(resolve => window.resumeOldWrite = resolve); return await (await fetch('/h5/' + payload.api + '/1.0/', {method:'POST'})).json(); }")
        with tempfile.TemporaryDirectory() as directory:
            manager, context = await cli._open_context(Path(directory), "chrome", True, verification_timeout=1)
            page = await cli._active_page(context)
            monitor = context._xianyu_verification
            monitor.headless = False  # Exercise handoff navigation without showing a test window.
            documents = 0
            writes = 0
            async def fixture(route):
                nonlocal documents, writes
                if "/h5/" in route.request.url:
                    write = cli.FAVORITE_APIS["favorite"] in route.request.url
                    writes += int(write)
                    value = {"ret": ["FAIL_SYS_USER_VALIDATE::fixture"] if write else ["SUCCESS::fixture"],
                             "data": {"url": "https://h5api.m.goofish.com/_____tmd_____/punish?writeIntent=PRIVATE_FIXTURE"} if write else {"fixture": True}}
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps(value))
                elif route.request.resource_type == "document":
                    documents += 1
                    auto_read = "<script>window.lib.mtop.request({api:'mtop.taobao.idle.pc.detail'});</script>" if documents > 1 else ""
                    await route.fulfill(status=200, content_type="text/html", body=html + auto_read)
                else:
                    await route.abort()
            await context.route("**/*", fixture)
            try:
                await page.goto("https://www.goofish.com/item?id=123456789012")
                with self.assertRaises(cli.XianyuError):
                    await cli._mtop_request(page, cli.FAVORITE_APIS["favorite"], {"itemId": "123456789012"})
                self.assertEqual(await page.evaluate("typeof window.resumeOldWrite"), "function")
                with patch.object(monitor, "show", AsyncMock(return_value=True)), patch("sys.stderr"):
                    result = await monitor.handoff(page)
                self.assertEqual(result["state"], "completed")
                self.assertTrue(result["original_document_discarded"])
                self.assertEqual(await page.evaluate("typeof window.resumeOldWrite"), "undefined")
                self.assertEqual(writes, 1)
                self.assertNotIn("PRIVATE_FIXTURE", page.url)
            finally:
                await cli._close_context(manager, context)
