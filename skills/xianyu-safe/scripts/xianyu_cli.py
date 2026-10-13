from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus


HOME_URL = "https://www.goofish.com/"
SEARCH_URL = "https://www.goofish.com/search?q={keyword}"
MESSAGES_URL = "https://www.goofish.com/im"
PUBLISH_URL = "https://www.goofish.com/publish"
PERSONAL_URL = "https://www.goofish.com/personal"
ORDERS_URL = "https://www.goofish.com/bought"
DEFAULT_TIMEOUT_MS = 45_000
SCHEMA_VERSION = "0.3.0"
FAVORITES_URL = "https://www.goofish.com/collection"
FAVORITES_LIST_API = "mtop.taobao.idle.web.favor.item.list"
FAVORITE_APIS = {"favorite": "mtop.taobao.idle.collect.item", "unfavorite": "com.taobao.idle.unfavor.item"}
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
BACKGROUND_BROWSER_ARGS = (
    "--start-minimized",
    "--window-position=-32000,-32000",
    "--window-size=1280,900",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


class XianyuError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _emit(payload: dict[str, Any]) -> None:
    payload.setdefault("meta", {"schema_version": SCHEMA_VERSION})
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _default_profile_dir() -> Path:
    override = os.environ.get("XIANYU_PROFILE_DIR")
    if override:
        return Path(override).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "xianyu-safe" / "chrome-profile"
    return Path.cwd() / ".xianyu-profile"


def _storage_state_path(profile_dir: Path) -> Path:
    return profile_dir / "xianyu-storage-state.json"


def _idempotency_path(profile_dir: Path) -> Path:
    return profile_dir / "xianyu-idempotency.json"


def _validate_item_id(value: str) -> str:
    item_id = str(value or "").strip()
    if not re.fullmatch(r"\d{6,20}", item_id):
        raise XianyuError("invalid_item_id", "item ID 必须为 6-20 位数字")
    return item_id


def _normalize_money(value: Any, field: str, *, required: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise XianyuError("invalid_price", f"{field} 不能为空")
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise XianyuError("invalid_price", f"{field} 必须为有效金额") from exc
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise XianyuError("invalid_price", f"{field} 必须大于 0 且最多两位小数")
    return format(amount.normalize(), "f")


def _validate_images(values: list[str] | None, *, required: bool = False) -> list[str]:
    images: list[str] = []
    for value in values or []:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            raise XianyuError("invalid_image", f"不支持的图片格式：{path.suffix}")
        if not path.is_file():
            raise XianyuError("invalid_image", f"图片文件不存在：{path}")
        images.append(str(path))
    if required and not images:
        raise XianyuError("invalid_image", "发布商品至少需要 1 张图片")
    if len(images) > 9:
        raise XianyuError("invalid_image", "发布商品最多支持 9 张图片")
    return images


def _payload_fingerprint(action: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({"action": action, "payload": payload}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_idempotency(profile_dir: Path) -> dict[str, Any]:
    path = _idempotency_path(profile_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _idempotency_lookup(
    profile_dir: Path,
    key: str,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    record = _load_idempotency(profile_dir).get(key)
    if not record:
        return None
    if record.get("fingerprint") != _payload_fingerprint(action, payload):
        raise XianyuError("idempotency_conflict", "同一 idempotency key 已用于不同操作")
    result = record.get("result")
    return dict(result) if isinstance(result, dict) else None


def _idempotency_record(
    profile_dir: Path,
    key: str,
    action: str,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    records = _load_idempotency(profile_dir)
    records[key] = {
        "fingerprint": _payload_fingerprint(action, payload),
        "result": result,
    }
    path = _idempotency_path(profile_dir)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


async def _restore_storage_state(context, profile_dir: Path) -> None:
    state_path = _storage_state_path(profile_dir)
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = state.get("cookies") or []
        if cookies:
            await context.add_cookies(cookies)
    except (OSError, ValueError, TypeError):
        return


async def _save_storage_state(context, profile_dir: Path) -> None:
    state_path = _storage_state_path(profile_dir)
    await context.storage_state(path=str(state_path))
    try:
        state_path.chmod(0o600)
    except OSError:
        pass


def _safe_title(lines: list[str]) -> str:
    ignored = re.compile(r"^(?:[¥￥]\s*)?\d+(?:\.\d+)?$|想要|包邮|已售")
    for line in lines:
        candidate = re.sub(r"\s+", " ", line).strip()
        if candidate and not ignored.search(candidate):
            return candidate[:200]
    return ""


def _normalize_items(raw_items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items:
        item_id = str(raw.get("id") or "").strip()
        url = str(raw.get("url") or "").strip()
        key = item_id or url
        if not key or key in seen:
            continue
        seen.add(key)

        lines = [str(value) for value in raw.get("lines") or [] if str(value).strip()]
        title = str(raw.get("title") or "").strip() or _safe_title(lines)
        if not title:
            continue

        normalized.append(
            {
                "id": item_id or None,
                "title": title[:200],
                "price": raw.get("price"),
                "wanted": raw.get("wanted"),
                "condition": raw.get("condition"),
                "brand": raw.get("brand"),
                "location": raw.get("location"),
                "url": url,
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def _browser_args(background: bool) -> list[str]:
    args = ["--no-first-run", "--no-default-browser-check"]
    if background:
        args.extend(BACKGROUND_BROWSER_ARGS)
    return args


async def _open_context(
    profile_dir: Path,
    channel: str,
    headless: bool,
    *,
    background: bool = False,
):
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise XianyuError(
            "dependency_missing",
            "缺少 Playwright，请先在项目目录执行：python -m pip install -e .",
        ) from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    manager = await async_playwright().start()
    launch_options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "locale": "zh-CN",
        "args": _browser_args(background and not headless),
    }
    if channel != "chromium":
        launch_options["channel"] = channel

    try:
        context = await manager.chromium.launch_persistent_context(**launch_options)
    except Exception as exc:
        await manager.stop()
        raise XianyuError(
            "browser_start_failed",
            f"无法启动浏览器通道 {channel}：{exc}",
            details={"profile_dir": str(profile_dir)},
        ) from exc
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    await _restore_storage_state(context, profile_dir)
    return manager, context


async def _active_page(context):
    pages = context.pages
    return pages[-1] if pages else await context.new_page()


async def _navigate(page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        await page.wait_for_timeout(1_500)
    except Exception as exc:
        raise XianyuError("navigation_failed", f"打开闲鱼页面失败：{exc}") from exc


async def _mtop_request(page, api: str, data: dict[str, Any], *, require_login: bool = False) -> dict[str, Any]:
    try:
        await page.wait_for_function(
            "() => Boolean(window.lib && window.lib.mtop && window.lib.mtop.request)",
            timeout=10_000,
        )
        result = await page.evaluate(
            """
            async payload => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              try {
                const response = await window.lib.mtop.request({
                  api: payload.api,
                  data: payload.data,
                  type: 'POST',
                  v: '1.0',
                  dataType: 'json',
                  needLogin: false,
                  needLoginPC: payload.requireLogin === true,
                  sessionOption: 'AutoLoginOnly',
                  ecode: 0
                });
                return { ok: true, response };
              } catch (error) {
                return {
                  ok: false,
                  ret: Array.isArray(error?.ret) ? error.ret : [],
                  message: clean(error?.message || error)
                };
              }
            }
            """,
            {"api": api, "data": data, "requireLogin": require_login},
        )
    except Exception as exc:
        raise XianyuError("mtop_unavailable", "闲鱼页面接口尚未初始化") from exc

    if not result or not result.get("ok"):
        ret = " | ".join(str(value) for value in (result or {}).get("ret") or [])
        message = ret or str((result or {}).get("message") or "未知错误")
        code = message.split("::", 1)[0]
        if "SESSION" in code or "TOKEN" in code:
            raise XianyuError("not_logged_in", "闲鱼登录态已失效，请重新运行 login")
        raise XianyuError("mtop_request_failed", f"闲鱼页面接口请求失败：{message}")

    response = result.get("response") or {}
    ret_values = response.get("ret") or []
    ret_code = str(ret_values[0]).split("::", 1)[0] if ret_values else "SUCCESS"
    if ret_code != "SUCCESS":
        message = " | ".join(str(value) for value in ret_values)
        if "SESSION" in ret_code or "TOKEN" in ret_code:
            raise XianyuError("not_logged_in", "闲鱼登录态已失效，请重新运行 login")
        raise XianyuError("mtop_response_error", f"闲鱼页面接口返回错误：{message}")
    response_data = response.get("data")
    if not isinstance(response_data, dict):
        raise XianyuError("malformed_response", "闲鱼页面接口未返回有效数据")
    return response_data


def _label_value(labels: list[dict[str, Any]], name: str) -> str | None:
    for label in labels:
        if str(label.get("propertyText") or "").strip() == name:
            value = str(label.get("text") or "").strip()
            return value or None
    return None


async def _fetch_item_detail(page, item_id: str) -> dict[str, Any]:
    item_id = _validate_item_id(item_id)
    await _navigate(page, f"https://www.goofish.com/item?id={item_id}")
    data = await _mtop_request(page, "mtop.taobao.idle.pc.detail", {"itemId": item_id})
    item = data.get("itemDO") or {}
    seller = data.get("sellerDO") or {}
    labels = item.get("itemLabelExtList") or []
    images = [
        entry.get("url")
        for entry in (item.get("imageInfos") or [])
        if isinstance(entry, dict) and entry.get("url")
    ]
    title = str(item.get("title") or "").strip()
    if not title:
        raise XianyuError("item_not_found", f"没有找到商品：{item_id}")
    price = item.get("soldPrice") or item.get("defaultPrice")
    return {
        "item_id": str(item.get("itemId") or item_id),
        "title": title,
        "description": str(item.get("desc") or "").strip(),
        "price": str(price) if price is not None else None,
        "original_price": str(item.get("originalPrice") or "").strip() or None,
        "want_count": item.get("wantCnt"),
        "collect_count": item.get("collectCnt"),
        "is_favorited": _favorite_state(data),
        "browse_count": item.get("browseCnt"),
        "status": str(item.get("itemStatusStr") or "").strip() or None,
        "condition": _label_value(labels, "成色"),
        "brand": _label_value(labels, "品牌"),
        "category": _label_value(labels, "分类"),
        "location": str(seller.get("publishCity") or seller.get("city") or "").strip() or None,
        "seller_name": str(seller.get("nick") or seller.get("uniqueName") or "").strip() or None,
        "seller_id": str(seller.get("sellerId") or "").strip() or None,
        "seller_score": str(seller.get("xianyuSummary") or "").strip() or None,
        "reply_ratio_24h": str(seller.get("replyRatio24h") or "").strip() or None,
        "reply_interval": str(seller.get("replyInterval") or "").strip() or None,
        "item_url": f"https://www.goofish.com/item?id={item_id}",
        "seller_url": (
            f"https://www.goofish.com/personal?userId={seller.get('sellerId')}"
            if seller.get("sellerId")
            else None
        ),
        "image_count": len(images),
        "image_urls": images,
    }


async def _dismiss_compatibility_prompt(page) -> bool:
    """Dismiss Xianyu's optional browser compatibility gate when it appears."""
    continue_button = page.get_by_text("继续访问", exact=True)
    try:
        if await continue_button.count() and await continue_button.first.is_visible():
            await continue_button.first.click()
            await page.wait_for_timeout(2_000)
            return True
    except Exception:
        return False
    return False


async def _login_snapshot(context, page) -> dict[str, Any]:
    page_data = await page.evaluate(
        """
        () => {
          const clean = value => (value || '').replace(/\s+/g, ' ').trim();
          const personal = Array.from(document.querySelectorAll('a[href*="/personal"]'))
            .map(a => clean(a.innerText || a.textContent))
            .filter(Boolean);
          const exactLogin = Array.from(document.querySelectorAll('a,button,span,div'))
            .some(el => clean(el.innerText || el.textContent) === '登录' && el.offsetParent !== null);
          return {
            personal,
            exact_login_visible: exactLogin,
            title: document.title,
            url: location.href
          };
        }
        """
    )
    cookies = await context.cookies(HOME_URL)
    cookie_names = {cookie.get("name") for cookie in cookies}
    personal = page_data.get("personal") or []
    logged_in = bool(personal) or ("unb" in cookie_names and not page_data.get("exact_login_visible"))

    nickname = None
    excluded = {"闲鱼号", "个人主页", "我的", "我的闲鱼"}
    for value in personal:
        value = str(value).strip()
        if value and value not in excluded:
            nickname = value[:80]
            break

    return {
        "logged_in": logged_in,
        "nickname": nickname,
        "url": page_data.get("url"),
        "title": page_data.get("title"),
    }


async def _run_login(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(profile_dir, args.channel, headless=False)
    try:
        page = await _active_page(context)
        await _navigate(page, HOME_URL)
        snapshot = await _login_snapshot(context, page)
        if snapshot["logged_in"]:
            return {"success": True, "message": "当前浏览器已登录", **snapshot}

        print(
            "[xianyu-safe] 请在打开的 Chrome 闲鱼页面中扫码，并在手机端确认登录。",
            file=sys.stderr,
        )
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            await page.wait_for_timeout(1_500)
            if page.is_closed():
                raise XianyuError("browser_closed", "浏览器已关闭，登录未完成")
            snapshot = await _login_snapshot(context, page)
            if snapshot["logged_in"]:
                await page.wait_for_timeout(2_000)
                await page.reload(wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
                await page.wait_for_timeout(1_500)
                verified = await _login_snapshot(context, page)
                if verified["logged_in"]:
                    await _save_storage_state(context, profile_dir)
                    return {"success": True, "message": "扫码登录成功，刷新复核通过", **verified}

        raise XianyuError(
            "login_timeout",
            f"在 {args.timeout} 秒内未检测到登录成功，请重新运行 login",
        )
    finally:
        await context.close()
        await manager.stop()


async def _run_status(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir,
        args.channel,
        args.headless,
        background=not args.headless,
    )
    try:
        page = await _active_page(context)
        await _navigate(page, HOME_URL)
        snapshot = await _login_snapshot(context, page)
        if snapshot["logged_in"]:
            await _save_storage_state(context, profile_dir)
        return {"success": True, **snapshot}
    finally:
        await context.close()
        await manager.stop()


async def _extract_items(page) -> list[dict[str, Any]]:
    return await page.evaluate(
        r"""
        () => {
          const clean = value => (value || '').replace(/\s+/g, ' ').trim();
          const anchors = Array.from(document.querySelectorAll('a[href*="/item?id="], a[href*="/item?"]'));
          return anchors.map(anchor => {
            const url = anchor.href || '';
            const id = (url.match(/[?&]id=(\d+)/) || [])[1] || null;
            if (!id) return null;
            const lines = (anchor.innerText || anchor.getAttribute('aria-label') || '')
              .split(/\n+/).map(clean).filter(Boolean);
            const text = lines.join(' ');
            const priceMatch = text.match(/[¥￥]\s*(\d+(?:\.\d+)?)/);
            const wantedMatch = text.match(/(\d+(?:\.\d+)?万?)\s*人?想要/);
            const titleNode = anchor.querySelector('[title]');
            const title = clean(titleNode?.getAttribute('title'));
            return {
              id,
              url,
              title,
              lines,
              price: priceMatch ? priceMatch[1] : null,
              wanted: wantedMatch ? wantedMatch[1] : null
            };
          }).filter(Boolean);
        }
        """
    )


async def _detect_challenge(page) -> str | None:
    text = await page.locator("body").inner_text(timeout=10_000)
    markers = ("请完成验证", "滑动验证", "访问频繁", "安全验证", "扫码登录")
    return next((marker for marker in markers if marker in text), None)


async def _run_search(args: argparse.Namespace) -> dict[str, Any]:
    min_price = _normalize_money(args.min_price, "min-price")
    max_price = _normalize_money(args.max_price, "max-price")
    if min_price is not None and max_price is not None:
        if Decimal(min_price) > Decimal(max_price):
            raise XianyuError("invalid_price_range", "最低价不能高于最高价")

    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir,
        args.channel,
        args.headless,
        background=not args.headless,
    )
    try:
        page = await _active_page(context)
        await _navigate(page, HOME_URL)
        snapshot = await _login_snapshot(context, page)
        if not snapshot["logged_in"]:
            raise XianyuError("not_logged_in", "尚未登录，请先运行 login 完成扫码登录")

        await _navigate(page, SEARCH_URL.format(keyword=quote_plus(args.keyword)))
        await _dismiss_compatibility_prompt(page)

        search_filter = ""
        if min_price is not None or max_price is not None:
            search_filter = f"priceRange:{min_price or '0'},{max_price or '99999999'};"
        province = str(args.province or "").strip()
        city = str(args.city or "").strip()
        extra_filter = "{}"
        if province or city:
            extra_filter = json.dumps(
                {
                    "divisionList": [{"province": province, "city": city}],
                    "excludeMultiPlacesSellers": "0",
                    "extraDivision": "",
                },
                ensure_ascii=False,
            )

        collected: list[dict[str, Any]] = []
        page_number = 1
        while len(collected) < args.limit:
            data = await _mtop_request(
                page,
                "mtop.taobao.idlemtopsearch.pc.search",
                {
                    "pageNumber": page_number,
                    "keyword": args.keyword,
                    "fromFilter": bool(search_filter or extra_filter != "{}"),
                    "rowsPerPage": 30,
                    "sortValue": "",
                    "sortField": "",
                    "customDistance": "",
                    "gps": "",
                    "propValueStr": {"searchFilter": search_filter} if search_filter else {},
                    "customGps": "",
                    "searchReqFromPage": "pcSearch",
                    "extraFilterValue": extra_filter,
                    "userPositionJson": "{}",
                },
            )
            rows = data.get("resultList")
            if not isinstance(rows, list):
                raise XianyuError("malformed_response", "闲鱼搜索结果缺少 resultList")
            if not rows:
                break
            for entry in rows:
                node = ((entry or {}).get("data") or {}).get("item") or {}
                main = node.get("main") or {}
                click_args = (main.get("clickParam") or {}).get("args") or {}
                ex = main.get("exContent") or node.get("exContent") or {}
                item_id = str(click_args.get("item_id") or click_args.get("id") or "").strip()
                title = str(ex.get("title") or (ex.get("detailParams") or {}).get("title") or "").strip()
                if not item_id or not title:
                    continue
                price = str(click_args.get("price") or click_args.get("displayPrice") or "").strip()
                location = str(click_args.get("p_city") or ex.get("area") or "").strip()
                collected.append(
                    {
                        "id": item_id,
                        "title": title,
                        "price": price or None,
                        "wanted": str(click_args.get("wantNum") or ex.get("want") or "").strip() or None,
                        "condition": str(ex.get("condition") or ex.get("stuffStatus") or "").strip() or None,
                        "brand": str(ex.get("brand") or ex.get("brandName") or "").strip() or None,
                        "location": location or None,
                        "url": f"https://www.goofish.com/item?id={item_id}",
                    }
                )
                if len(collected) >= args.limit:
                    break
            if len(rows) < 30:
                break
            page_number += 1

        items = _normalize_items(collected, args.limit)

        await _save_storage_state(context, profile_dir)
        return {
            "success": True,
            "keyword": args.keyword,
            "count": len(items),
            "items": items,
            "filters": {
                "min_price": min_price,
                "max_price": max_price,
                "province": province or None,
                "city": city or None,
            },
            "source_url": page.url,
        }
    finally:
        await context.close()
        await manager.stop()


async def _selected_conversation(page, nickname: str) -> dict[str, Any]:
    data = await page.evaluate(
        r"""
        nickname => {
          const clean = value => (value || '').replace(/\s+/g, ' ').trim();
          const profile = Array.from(document.querySelectorAll('a[href*="/personal?userId="]'))
            .find(link => link.offsetParent !== null);
          const item = Array.from(document.querySelectorAll('a[href*="/item?id="]'))
            .find(link => link.offsetParent !== null);
          const profileUrl = profile?.href || '';
          const itemUrl = item?.href || '';
          return {
            nickname,
            user_id: (profileUrl.match(/[?&]userId=(\d+)/) || [])[1] || null,
            profile_url: profileUrl || null,
            item_id: (itemUrl.match(/[?&]id=(\d+)/) || [])[1] || null,
            item_url: itemUrl || null,
            item_summary: item ? clean(item.innerText || item.textContent).slice(0, 300) : null
          };
        }
        """,
        nickname,
    )
    return dict(data or {})


async def _open_named_conversation(page, nickname: str, index: int) -> dict[str, Any]:
    matches = page.get_by_text(nickname, exact=True)
    count = await matches.count()
    if count == 0:
        raise XianyuError("user_not_found", f"消息列表中没有找到用户：{nickname}")
    if index >= count:
        raise XianyuError(
            "conversation_not_found",
            f"用户 {nickname} 只有 {count} 个可见会话，无法选择索引 {index}",
        )
    await matches.nth(index).click()
    await page.wait_for_timeout(2_000)
    selected = await _selected_conversation(page, nickname)
    selected["conversation_index"] = index
    selected["matching_conversations"] = count
    return selected


async def _run_find_user(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir,
        args.channel,
        args.headless,
        background=not args.headless,
    )
    try:
        page = await _active_page(context)
        await _navigate(page, HOME_URL)
        if not (await _login_snapshot(context, page))["logged_in"]:
            raise XianyuError("not_logged_in", "尚未登录，请先运行 login 完成扫码登录")
        await _navigate(page, MESSAGES_URL)
        await page.wait_for_timeout(3_000)

        matches = page.get_by_text(args.nickname, exact=True)
        count = await matches.count()
        conversations: list[dict[str, Any]] = []
        for index in range(count):
            conversations.append(await _open_named_conversation(page, args.nickname, index))

        await _save_storage_state(context, profile_dir)
        return {
            "success": True,
            "nickname": args.nickname,
            "count": len(conversations),
            "conversations": conversations,
        }
    finally:
        await context.close()
        await manager.stop()


async def _run_send_message(args: argparse.Namespace) -> dict[str, Any]:
    content = args.content.strip()
    if not content or len(content) > 500:
        raise XianyuError("invalid_message", "消息内容必须为 1-500 个字符")
    if not args.confirm_send:
        return {
            "success": True,
            "dry_run": True,
            "action": "send-message",
            "preview": {
                "nickname": args.nickname,
                "conversation_index": args.conversation_index,
                "content": content,
            },
        }

    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir,
        args.channel,
        args.headless,
        background=not args.headless,
    )
    try:
        page = await _active_page(context)
        await _navigate(page, HOME_URL)
        if not (await _login_snapshot(context, page))["logged_in"]:
            raise XianyuError("not_logged_in", "尚未登录，请先运行 login 完成扫码登录")
        await _navigate(page, MESSAGES_URL)
        await page.wait_for_timeout(3_000)
        selected = await _open_named_conversation(page, args.nickname, args.conversation_index)

        editor = page.locator('textarea[placeholder*="请输入消息"]').first
        if not await editor.count() or not await editor.is_visible():
            raise XianyuError("message_editor_not_found", "没有找到当前会话的消息输入框")
        await editor.fill(content)
        if await editor.input_value() != content:
            raise XianyuError("message_fill_failed", "消息写入输入框失败，未执行发送")

        await editor.press("Enter")
        await page.wait_for_timeout(2_000)
        delivered = await page.get_by_text(content, exact=True).count() > 0
        if not delivered:
            raise XianyuError(
                "delivery_unconfirmed",
                "已触发一次发送，但页面未能确认送达；为避免重复消息，不自动重试",
                details={"target": selected},
            )

        await _save_storage_state(context, profile_dir)
        return {
            "success": True,
            "sent": True,
            "message": content,
            "target": selected,
        }
    finally:
        await context.close()
        await manager.stop()


def _capabilities() -> dict[str, Any]:
    return {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "read": [
            "status",
            "search",
            "item",
            "my-items",
            "inbox",
            "read-messages",
            "find-user",
            "orders",
            "favorites",
        ],
        "write": ["send-message", "publish", "edit-item", "manage-item", "favorite", "unfavorite"],
        "safety": {
            "write_default": "dry-run",
            "confirmation_required": True,
            "idempotency_required": ["publish", "edit-item", "manage-item", "favorite", "unfavorite"],
            "automatic_retry_after_submit": False,
        },
        "unsupported": ["purchase", "payment", "captcha-bypass", "bulk-outreach"],
    }


async def _run_item(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir, args.channel, args.headless, background=not args.headless
    )
    try:
        page = await _active_page(context)
        detail = await _fetch_item_detail(page, args.item_id)
        await _save_storage_state(context, profile_dir)
        return {"success": True, "item": detail}
    finally:
        await context.close()
        await manager.stop()


async def _run_my_items(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir, args.channel, args.headless, background=not args.headless
    )
    try:
        page = await _active_page(context)
        await _navigate(page, PERSONAL_URL)
        snapshot = await _login_snapshot(context, page)
        if not snapshot["logged_in"]:
            raise XianyuError("not_logged_in", "尚未登录，请先运行 login")
        rows = await page.evaluate(
            """
            limit => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const seen = new Set();
              const result = [];
              for (const link of document.querySelectorAll('a[href*="/item?id="]')) {
                const match = (link.href || '').match(/[?&]id=(\d+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);
                const text = clean(link.innerText || link.textContent);
                const price = (text.match(/[¥￥]\s*(\d+(?:\.\d+)?)/) || [])[1] || null;
                result.push({
                  item_id: match[1],
                  title: clean(text.replace(/[¥￥]\s*\d+(?:\.\d+)?.*$/, '')),
                  price,
                  url: link.href
                });
                if (result.length >= limit) break;
              }
              return result;
            }
            """,
            args.limit,
        )
        await _save_storage_state(context, profile_dir)
        return {"success": True, "count": len(rows), "items": rows}
    finally:
        await context.close()
        await manager.stop()


async def _extract_inbox_rows(page, limit: int) -> list[dict[str, Any]]:
    return await page.evaluate(
        """
        limit => {
          const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
          const leafTexts = root => Array.from(root.querySelectorAll('div,span'))
            .filter(node => !Array.from(node.children || []).some(child => ['DIV','SPAN'].includes(child.tagName)))
            .map(node => clean(node.textContent)).filter(Boolean);
          const rows = Array.from(document.querySelectorAll('#conv-list-scrollable [class*="conversation-item"]'));
          return rows.slice(0, limit).map((row, index) => {
            const texts = leafTexts(row);
            const unreadNode = row.querySelector('sup,[class*="badge"],[class*="unread"]');
            const unreadText = clean(unreadNode?.getAttribute('title') || unreadNode?.textContent || '');
            const unreadCount = Number.parseInt(unreadText.replace(/\D/g, ''), 10) || (unreadText ? 1 : 0);
            const timePattern = /^(刚刚|\d+分钟前|\d+小时前|昨天|前天|\d{1,2}:\d{2}|\d{2}-\d{2}|\d{4}-\d{2}-\d{2})$/;
            const time = [...texts].reverse().find(text => timePattern.test(text)) || '';
            const filtered = texts.filter(text => text !== time && text !== unreadText && !/^\d+$/.test(text));
            return {
              rank: index + 1,
              nickname: filtered[0] || '',
              last_message: filtered[1] || '',
              time: time || null,
              unread: unreadCount > 0,
              unread_count: unreadCount
            };
          }).filter(row => row.nickname || row.last_message);
        }
        """,
        limit,
    )


async def _run_inbox(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir, args.channel, args.headless, background=not args.headless
    )
    try:
        page = await _active_page(context)
        await _navigate(page, MESSAGES_URL)
        await page.wait_for_timeout(2_000)
        rows = await _extract_inbox_rows(page, args.limit)
        if args.resolve_ids:
            locator = page.locator('#conv-list-scrollable [class*="conversation-item"]')
            for row in rows:
                index = row["rank"] - 1
                if index >= await locator.count():
                    continue
                await locator.nth(index).click()
                await page.wait_for_timeout(800)
                selected = await _selected_conversation(page, row["nickname"])
                row.update(
                    {
                        "user_id": selected.get("user_id"),
                        "item_id": selected.get("item_id"),
                        "profile_url": selected.get("profile_url"),
                        "item_url": selected.get("item_url"),
                    }
                )
        if args.unread_only:
            rows = [row for row in rows if row.get("unread")]
        await _save_storage_state(context, profile_dir)
        return {"success": True, "count": len(rows), "conversations": rows}
    finally:
        await context.close()
        await manager.stop()


async def _run_read_messages(args: argparse.Namespace) -> dict[str, Any]:
    if not args.nickname and not args.rank:
        raise XianyuError("invalid_target", "必须提供 --nickname 或 --rank")
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir, args.channel, args.headless, background=not args.headless
    )
    try:
        page = await _active_page(context)
        await _navigate(page, MESSAGES_URL)
        await page.wait_for_timeout(2_000)
        if args.rank:
            rows = page.locator('#conv-list-scrollable [class*="conversation-item"]')
            index = args.rank - 1
            if index < 0 or index >= await rows.count():
                raise XianyuError("conversation_not_found", f"不存在会话序号：{args.rank}")
            await rows.nth(index).click()
            await page.wait_for_timeout(1_500)
            target = await _selected_conversation(page, args.nickname or "")
        else:
            target = await _open_named_conversation(page, args.nickname, args.conversation_index)
        messages = await page.evaluate(
            """
            limit => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const root = document.querySelector('#message-list-scrollable') || document;
              const rows = Array.from(root.querySelectorAll('[class*="message-row"]'));
              return rows.map((row, index) => {
                const bubble = row.querySelector('[class*="message-text"]');
                const content = clean(bubble?.textContent || '');
                if (!content) return null;
                const bubbleClass = String(bubble.className || '');
                const senderNode = Array.from(row.querySelectorAll('div')).find(node =>
                  node !== bubble && getComputedStyle(node).fontSize === '12px' && clean(node.textContent)
                );
                const status = clean(row.querySelector('[class*="read-status"]')?.textContent || '');
                return {
                  index: index + 1,
                  direction: bubbleClass.includes('right') ? 'outgoing' : 'incoming',
                  sender: clean(senderNode?.textContent || ''),
                  text: content,
                  status: status || null
                };
              }).filter(Boolean).slice(-limit);
            }
            """,
            args.limit,
        )
        await _save_storage_state(context, profile_dir)
        return {"success": True, "target": target, "count": len(messages), "messages": messages}
    finally:
        await context.close()
        await manager.stop()


async def _run_orders(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(
        profile_dir, args.channel, args.headless, background=not args.headless
    )
    try:
        page = await _active_page(context)
        await _navigate(page, ORDERS_URL)
        snapshot = await _login_snapshot(context, page)
        if not snapshot["logged_in"]:
            raise XianyuError("not_logged_in", "尚未登录，请先运行 login")
        orders = await page.evaluate(
            """
            limit => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const result = [];
              const seen = new Set();
              for (const link of document.querySelectorAll('a[href*="/order-detail?orderId="]')) {
                const match = link.href.match(/[?&]orderId=(\d+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);
                const card = link.closest('[class*="order"],li,[class*="card"]') || link.parentElement;
                const text = clean(card?.innerText || card?.textContent || link.textContent);
                const chat = card?.querySelector('a[href*="/im?itemId="]');
                const itemId = (chat?.href.match(/[?&]itemId=(\d+)/) || [])[1] || null;
                const seller = card?.querySelector('a[href*="/personal?userId="]');
                const sellerId = (seller?.href.match(/[?&]userId=(\d+)/) || [])[1] || null;
                const price = (text.match(/[¥￥]\s*(\d+(?:\.\d+)?)/) || [])[1] || null;
                const status = ['待付款','待发货','待收货','待评价','退款中','交易成功','交易关闭']
                  .find(value => text.includes(value)) || null;
                result.push({
                  order_id: match[1], item_id: itemId, seller_id: sellerId,
                  seller_name: clean(seller?.textContent || '') || null,
                  status, price, summary: text.slice(0, 500), url: link.href
                });
                if (result.length >= limit) break;
              }
              return result;
            }
            """,
            args.limit,
        )
        if args.status:
            orders = [order for order in orders if order.get("status") == args.status]
        await _save_storage_state(context, profile_dir)
        return {"success": True, "count": len(orders), "orders": orders}
    finally:
        await context.close()
        await manager.stop()


def _require_idempotency_key(value: str | None) -> str:
    key = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        raise XianyuError(
            "idempotency_required",
            "执行写操作必须提供 8-128 位 --idempotency-key",
        )
    return key


async def _verify_owned_item(context, page, item_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    await _navigate(page, HOME_URL)
    snapshot = await _login_snapshot(context, page)
    if not snapshot["logged_in"]:
        raise XianyuError("not_logged_in", "尚未登录，请先运行 login")
    detail = await _fetch_item_detail(page, item_id)
    if snapshot.get("nickname") and detail.get("seller_name") != snapshot.get("nickname"):
        raise XianyuError("ownership_mismatch", "目标商品不属于当前登录账号")
    return snapshot, detail


async def _fill_listing_form(page, payload: dict[str, Any], *, editing: bool) -> None:
    images = payload.get("images") or []
    if images:
        file_input = page.locator('input[type="file"]').first
        if not await file_input.count():
            raise XianyuError("publish_form_changed", "没有找到图片上传控件")
        await file_input.set_input_files(images)
        await page.wait_for_timeout(4_000)

    editor = page.locator('[contenteditable="true"]').first
    if not await editor.count():
        raise XianyuError("publish_form_changed", "没有找到宝贝描述输入框")
    if payload.get("combined_text") is not None:
        await editor.fill(payload["combined_text"])

    price_inputs = page.locator('input[placeholder="0.00"]')
    if payload.get("price") is not None:
        if not await price_inputs.count():
            raise XianyuError("publish_form_changed", "没有找到价格输入框")
        await price_inputs.nth(0).fill(payload["price"])
    if payload.get("original_price") is not None and await price_inputs.count() > 1:
        await price_inputs.nth(1).fill(payload["original_price"])

    delivery = payload.get("delivery")
    if delivery:
        option = page.get_by_text(delivery, exact=True).first
        if not await option.count():
            raise XianyuError("publish_form_changed", f"没有找到发货方式：{delivery}")
        await option.click()
    if payload.get("pickup"):
        pickup = page.get_by_text("支持自提", exact=True).first
        if await pickup.count():
            await pickup.click()
    await page.wait_for_timeout(2_000)


async def _submit_listing_once(page) -> tuple[str | None, str]:
    button = page.get_by_text("发布", exact=True).last
    if not await button.count() or not await button.is_enabled():
        raise XianyuError("publish_precondition_failed", "发布按钮不可用，表单可能缺少必填项")
    await button.click()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        await page.wait_for_timeout(1_000)
        match = re.search(r"[?&]id=(\d+)", page.url)
        if match and "/item" in page.url:
            return match.group(1), page.url
        body = await page.locator("body").inner_text(timeout=5_000)
        error = next(
            (marker for marker in ("发布失败", "违规", "安全验证", "请完成验证") if marker in body),
            None,
        )
        if error:
            raise XianyuError("user_action_required", f"发布页面要求人工处理：{error}")
    return None, page.url


async def _run_publish(args: argparse.Namespace) -> dict[str, Any]:
    title = re.sub(r"\s+", " ", args.title).strip()
    description = args.description.strip()
    if not title or len(title) > 200 or not description or len(description) > 1500:
        raise XianyuError("invalid_listing", "标题需为 1-200 字，描述需为 1-1500 字")
    images = _validate_images(args.images, required=True)
    payload = {
        "title": title,
        "description": description,
        "combined_text": f"{title}\n\n{description}",
        "price": _normalize_money(args.price, "price", required=True),
        "original_price": _normalize_money(args.original_price, "original-price"),
        "images": images,
        "delivery": args.delivery,
        "pickup": bool(args.pickup),
    }
    if not args.confirm_publish:
        return {"success": True, "dry_run": True, "action": "publish", "preview": payload}

    key = _require_idempotency_key(args.idempotency_key)
    profile_dir = Path(args.profile_dir).resolve()
    previous = _idempotency_lookup(profile_dir, key, "publish", payload)
    if previous:
        return {**previous, "idempotent_replay": True}
    manager, context = await _open_context(profile_dir, args.channel, False, background=True)
    try:
        page = await _active_page(context)
        await _navigate(page, PUBLISH_URL)
        if not (await _login_snapshot(context, page))["logged_in"]:
            raise XianyuError("not_logged_in", "尚未登录，请先运行 login")
        await _fill_listing_form(page, payload, editing=False)
        item_id, final_url = await _submit_listing_once(page)
        if not item_id:
            result = {
                "success": False,
                "error": "receipt_uncertain",
                "message": "已点击一次发布，但未取得商品回执；不要自动重试",
                "url": final_url,
            }
            _idempotency_record(profile_dir, key, "publish", payload, result)
            raise XianyuError("receipt_uncertain", result["message"], details={"url": final_url})
        detail = await _fetch_item_detail(page, item_id)
        result = {"success": True, "published": True, "item": detail}
        _idempotency_record(profile_dir, key, "publish", payload, result)
        await _save_storage_state(context, profile_dir)
        return result
    finally:
        await context.close()
        await manager.stop()


async def _run_edit_item(args: argparse.Namespace) -> dict[str, Any]:
    item_id = _validate_item_id(args.item_id)
    if not any(
        [
            args.title,
            args.description,
            args.price,
            args.original_price,
            args.images,
            args.delivery,
            args.pickup,
        ]
    ):
        raise XianyuError("invalid_edit", "至少提供一个要修改的字段")
    images = _validate_images(args.images)
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(profile_dir, args.channel, False, background=True)
    try:
        page = await _active_page(context)
        _, before = await _verify_owned_item(context, page, item_id)
        title = re.sub(r"\s+", " ", args.title).strip() if args.title else before["title"]
        description = args.description.strip() if args.description else before.get("description") or ""
        payload = {
            "item_id": item_id,
            "title": title,
            "description": description,
            "combined_text": f"{title}\n\n{description}" if args.title or args.description else None,
            "price": _normalize_money(args.price, "price"),
            "original_price": _normalize_money(args.original_price, "original-price"),
            "images": images,
            "delivery": args.delivery,
            "pickup": bool(args.pickup),
        }
        preview = {"before": before, "changes": {k: v for k, v in payload.items() if v is not None and k != "combined_text"}}
        if not args.confirm_edit:
            return {"success": True, "dry_run": True, "action": "edit-item", "preview": preview}
        key = _require_idempotency_key(args.idempotency_key)
        previous = _idempotency_lookup(profile_dir, key, "edit-item", payload)
        if previous:
            return {**previous, "idempotent_replay": True}
        await _navigate(page, f"{PUBLISH_URL}?itemId={item_id}")
        await page.wait_for_timeout(2_000)
        await _fill_listing_form(page, payload, editing=True)
        receipt_id, final_url = await _submit_listing_once(page)
        if not receipt_id and "发布成功" not in await page.locator("body").inner_text(timeout=5_000):
            result = {
                "success": False,
                "error": "receipt_uncertain",
                "message": "已点击一次保存，但未取得确定回执；不要自动重试",
                "url": final_url,
            }
            _idempotency_record(profile_dir, key, "edit-item", payload, result)
            raise XianyuError("receipt_uncertain", result["message"], details={"url": final_url})
        after = await _fetch_item_detail(page, receipt_id or item_id)
        result = {"success": True, "edited": True, "before": before, "after": after}
        _idempotency_record(profile_dir, key, "edit-item", payload, result)
        await _save_storage_state(context, profile_dir)
        return result
    finally:
        await context.close()
        await manager.stop()


async def _run_manage_item(args: argparse.Namespace) -> dict[str, Any]:
    item_id = _validate_item_id(args.item_id)
    payload = {"item_id": item_id, "action": args.action}
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(profile_dir, args.channel, False, background=True)
    try:
        page = await _active_page(context)
        _, before = await _verify_owned_item(context, page, item_id)
        if not args.confirm_action:
            return {
                "success": True,
                "dry_run": True,
                "action": "manage-item",
                "preview": {"item": before, "requested_action": args.action},
            }
        if args.action == "delete" and args.confirm_delete != item_id:
            raise XianyuError("confirmation_required", "删除商品必须传入 --confirm-delete <item_id>")
        key = _require_idempotency_key(args.idempotency_key)
        previous = _idempotency_lookup(profile_dir, key, "manage-item", payload)
        if previous:
            return {**previous, "idempotent_replay": True}

        if args.action == "relist":
            await _navigate(page, f"{PUBLISH_URL}?itemId={item_id}")
            receipt_id, final_url = await _submit_listing_once(page)
            confirmed = bool(receipt_id or "发布成功" in await page.locator("body").inner_text(timeout=5_000))
        else:
            label = {"unlist": "下架", "mark-sold": "卖掉了", "delete": "删除"}[args.action]
            button = page.get_by_text(label, exact=True).first
            if not await button.count():
                raise XianyuError("action_unavailable", f"当前商品页面没有可用的“{label}”操作")
            await button.click()
            await page.wait_for_timeout(800)
            confirm = page.get_by_text(re.compile(r"^(确认|确定|删除|下架)$")).last
            if await confirm.count() and await confirm.is_visible():
                await confirm.click()
            await page.wait_for_timeout(2_000)
            final_url = page.url
            body = await page.locator("body").inner_text(timeout=5_000)
            confirmed = any(marker in body for marker in ("成功", "已下架", "已卖出")) or args.action == "delete"
        if not confirmed:
            result = {
                "success": False,
                "error": "receipt_uncertain",
                "message": "已触发一次商品状态操作，但页面回执不明确；不要自动重试",
                "url": final_url,
            }
            _idempotency_record(profile_dir, key, "manage-item", payload, result)
            raise XianyuError("receipt_uncertain", result["message"], details={"url": final_url})
        result = {"success": True, "changed": True, "item_id": item_id, "action": args.action, "url": final_url}
        _idempotency_record(profile_dir, key, "manage-item", payload, result)
        await _save_storage_state(context, profile_dir)
        return result
    finally:
        await context.close()
        await manager.stop()


def _favorite_state(data: dict[str, Any]) -> bool | None:
    buyer = data.get("buyerDO")
    state = buyer.get("isCollected") if isinstance(buyer, dict) else None
    return state if type(state) is bool else None


def _favorite_records(profile_dir: Path) -> dict[str, Any]:
    path = profile_dir / "xianyu-favorites-receipts.json"
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, dict) or len(records) > 1000:
            raise ValueError()
        for key, record in records.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", key):
                raise ValueError()
            if not isinstance(record, dict) or set(record) != {"item_id", "action", "result"}:
                raise ValueError()
            _validate_item_id(record["item_id"])
            result = record["result"]
            if record["action"] not in FAVORITE_APIS or not isinstance(result, dict):
                raise ValueError()
            if (set(result) != {"success", "state", "item_id", "action", "is_favorited", "changed", "error"}
                    or result["state"] not in {"reserved", "unknown", "verified", "already_satisfied"}
                    or type(result["success"]) is not bool or type(result["changed"]) is not bool
                    or result["item_id"] != record["item_id"] or result["action"] != record["action"]
                    or result["is_favorited"] is not None and type(result["is_favorited"]) is not bool
                    or result["error"] not in {None, "receipt_uncertain"}):
                raise ValueError()
        return records
    except (OSError, ValueError, TypeError, XianyuError) as exc:
        raise XianyuError("favorite_receipts_invalid", "收藏回执文件无法可靠读取，已停止；不会忽略记录重新提交") from exc


def _save_favorite_records(profile_dir: Path, records: dict[str, Any]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=profile_dir,
                                         prefix="xianyu-favorites-", suffix=".json.tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(records, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, profile_dir / "xianyu-favorites-receipts.json")
    except OSError as exc:
        raise XianyuError("favorite_receipts_failed", "收藏回执保存失败，已停止；请只读核对商品状态，不要自动重试") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


async def _favorite_login(context, page) -> None:
    await _navigate(page, HOME_URL)
    snapshot = await _login_snapshot(context, page)
    if snapshot.get("logged_in") is not True:
        raise XianyuError("not_logged_in", "尚未登录，请先运行 login 完成扫码登录")


async def _favorite_detail(page, item_id: str) -> dict[str, Any]:
    data = await _mtop_request(page, "mtop.taobao.idle.pc.detail", {"itemId": item_id}, require_login=True)
    item = data.get("itemDO") or {}
    if not isinstance(item, dict) or str(item.get("itemId")) != item_id or not str(item.get("title") or "").strip():
        raise XianyuError("item_not_found", "无法核验目标商品，已停止")
    state = _favorite_state(data)
    if state is None:
        raise XianyuError("favorite_state_unknown", "无法识别当前账号的收藏状态，已停止；收藏人数不能代替本人收藏状态")
    return {"item_id": item_id, "title": str(item["title"]).strip(), "is_favorited": state}


def _favorite_result(item_id: str, action: str, state: str, current: bool | None = None) -> dict[str, Any]:
    return {"success": state in {"verified", "already_satisfied"}, "state": state, "item_id": item_id,
            "action": action, "is_favorited": current, "changed": state == "verified",
            "error": None if state in {"verified", "already_satisfied"} else "receipt_uncertain"}


async def _run_favorite(args: argparse.Namespace) -> dict[str, Any]:
    item_id = _validate_item_id(args.item_id)
    action = args.command
    desired = action == "favorite"
    key = str(args.idempotency_key or "")
    if args.confirm_action and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", key):
        raise XianyuError("invalid_idempotency_key", "真实收藏操作必须提供 1-64 位字母、数字、下划线或连字符幂等键")
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(profile_dir, args.channel, args.headless, background=not args.headless)
    try:
        # The exclusive persistent-profile lock also serializes receipt updates.
        records = _favorite_records(profile_dir) if args.confirm_action else {}
        previous = records.get(key)
        if previous:
            if previous["item_id"] != item_id or previous["action"] != action:
                raise XianyuError("idempotency_conflict", "该幂等键已绑定不同商品或收藏操作")
            return {**previous["result"], "replayed": True, "message": "返回原操作回执，未再次提交；不代表当前实时收藏状态"}
        if args.confirm_action and len(records) >= 1000:
            raise XianyuError("favorite_receipts_full", "收藏回执已达上限，未删除旧记录，拒绝新的写操作")
        page = await _active_page(context)
        await _favorite_login(context, page)
        await _navigate(page, f"https://www.goofish.com/item?id={item_id}")
        detail = await _favorite_detail(page, item_id)
        current = detail["is_favorited"]
        if not args.confirm_action:
            return {"success": True, "dry_run": True, "action": action, **detail,
                    "desired_state": desired, "would_change": current != desired, "submitted": False}
        if current == desired:
            result = _favorite_result(item_id, action, "already_satisfied", current)
            records[key] = {"item_id": item_id, "action": action, "result": result}
            _save_favorite_records(profile_dir, records)
            return result
        result = _favorite_result(item_id, action, "reserved")
        records[key] = {"item_id": item_id, "action": action, "result": result}
        _save_favorite_records(profile_dir, records)
        try:
            # These are the explicit add/remove APIs observed in the official
            # item page, not a toggle whose repeated use reverses the state.
            await _mtop_request(page, FAVORITE_APIS[action], {"itemId": item_id}, require_login=True)
            for attempt in range(3):
                verified = await _favorite_detail(page, item_id)
                if verified["is_favorited"] == desired:
                    result = _favorite_result(item_id, action, "verified", desired)
                    break
                if attempt < 2:
                    await page.wait_for_timeout(500)
            else:
                result = _favorite_result(item_id, action, "unknown")
        except Exception:
            result = _favorite_result(item_id, action, "unknown")
        records[key]["result"] = result
        _save_favorite_records(profile_dir, records)
        return result
    finally:
        await context.close()
        await manager.stop()


def _normalize_favorites(rows: list[Any]) -> list[dict[str, Any]]:
    items = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise XianyuError("favorites_response_invalid", "收藏列表包含无法识别的商品数据")
        item_id = _validate_item_id(row.get("id"))
        if item_id in seen:
            continue
        seen.add(item_id)
        title = row.get("title")
        if not isinstance(title, str) or not title.strip():
            raise XianyuError("favorites_response_invalid", "收藏商品缺少标题，不能将异常列表当作空收藏")
        price = row.get("price")
        status = row.get("itemStatus")
        items.append({"id": item_id, "title": title.strip(),
                      "price": str(price) if type(price) in {str, int, float} else None,
                      "item_status": status if type(status) in {str, int} else None,
                      "url": f"https://www.goofish.com/item?id={item_id}"})
    return items


async def _run_favorites(args: argparse.Namespace) -> dict[str, Any]:
    profile_dir = Path(args.profile_dir).resolve()
    manager, context = await _open_context(profile_dir, args.channel, args.headless, background=not args.headless)
    try:
        page = await _active_page(context)
        await _favorite_login(context, page)
        await _navigate(page, FAVORITES_URL)
        items, seen = [], set()
        total = None
        more = False
        for page_number in range(1, 11):
            data = await _mtop_request(page, FAVORITES_LIST_API,
                                       {"pageNumber": page_number, "rowsPerPage": 20, "type": "DEFAULT"}, require_login=True)
            rows = data.get("items")
            if not isinstance(rows, list) or type(data.get("nextPage")) is not bool:
                raise XianyuError("favorites_response_invalid", "收藏列表格式已变化，未将异常结果当作空收藏")
            count = data.get("totalCount")
            if count is not None and (type(count) is not int or count < 0):
                raise XianyuError("favorites_response_invalid", "收藏列表总数格式不受支持")
            total = count if page_number == 1 else total
            normalized = _normalize_favorites(rows)
            unique = [item for item in normalized if item["id"] not in seen]
            remaining = args.limit - len(items)
            for item in unique[:remaining]:
                seen.add(item["id"])
                items.append(item)
            more = data["nextPage"] or len(unique) > remaining
            if len(items) >= args.limit or not data["nextPage"]:
                break
            if not unique:
                raise XianyuError("favorites_pagination_stalled", "收藏分页没有新商品，已停止继续翻页")
        return {"success": True, "count": len(items), "total": total, "has_more": more, "items": items}
    finally:
        await context.close()
        await manager.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全操作闲鱼：搜索、消息、订单与商品管理")
    parser.add_argument(
        "--profile-dir",
        default=str(_default_profile_dir()),
        help="独立 Chrome 登录态目录（也可设置 XIANYU_PROFILE_DIR）",
    )
    parser.add_argument(
        "--channel",
        choices=("chrome", "msedge", "chromium"),
        default="chrome",
        help="Playwright 浏览器通道",
    )
    parser.add_argument("--headless", action="store_true", help="无界面运行；遇到验证时请关闭此选项")

    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="打开官方闲鱼页面并等待扫码登录")
    login.add_argument("--timeout", type=int, default=600, help="等待扫码的秒数")

    commands.add_parser("capabilities", help="输出当前能力、限制和安全约束")
    commands.add_parser("status", help="检查当前独立浏览器登录状态")

    search = commands.add_parser("search", help="按关键词搜索商品")
    search.add_argument("--keyword", required=True, help="搜索关键词")
    search.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    search.add_argument("--max-scrolls", type=int, default=6, choices=range(0, 21), metavar="0-20")
    search.add_argument("--min-price", help="最低价格")
    search.add_argument("--max-price", help="最高价格")
    search.add_argument("--province", help="省级地区，例如北京")
    search.add_argument("--city", help="城市，例如北京")

    item = commands.add_parser("item", help="读取商品详情")
    item.add_argument("--item-id", required=True, help="闲鱼商品 ID")

    favorites = commands.add_parser("favorites", help="查看当前账号的收藏商品列表")
    favorites.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    for command, label in (("favorite", "收藏"), ("unfavorite", "取消收藏")):
        favorite = commands.add_parser(command, help=f"预览或{label}指定商品；默认仅 dry-run")
        favorite.add_argument("--item-id", required=True, help="闲鱼商品 ID")
        favorite.add_argument("--confirm-action", action="store_true", help=f"确认真实{label}")
        favorite.add_argument("--idempotency-key", help="真实操作必填；同一键不重复提交")

    my_items = commands.add_parser("my-items", help="列出当前账号发布的商品")
    my_items.add_argument("--limit", type=int, default=50, choices=range(1, 101), metavar="1-100")

    inbox = commands.add_parser("inbox", help="列出消息会话")
    inbox.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    inbox.add_argument("--unread-only", action="store_true", help="仅返回未读会话")
    inbox.add_argument("--resolve-ids", action="store_true", help="逐项解析 itemId 与 peerUserId")

    history = commands.add_parser("read-messages", help="读取已有会话的最近消息")
    history.add_argument("--nickname", help="精确匹配的用户昵称；省略时按会话序号选择")
    history.add_argument("--rank", type=int, help="find-user 返回的 rank")
    history.add_argument("--conversation-index", type=int, default=0, help="会话序号或同名会话索引")
    history.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="1-200")

    find_user = commands.add_parser("find-user", help="在已有消息会话中查找昵称")
    find_user.add_argument("--nickname", required=True, help="精确匹配的用户昵称")

    send = commands.add_parser("send-message", help="向已有会话发送一条消息")
    send.add_argument("--nickname", required=True, help="精确匹配的用户昵称")
    send.add_argument("--content", required=True, help="要发送的消息，最多 500 字符")
    send.add_argument("--conversation-index", type=int, default=0, help="同名会话索引，0 表示最近一条")
    send.add_argument("--confirm-send", action="store_true", help="确认执行真实发送")

    orders = commands.add_parser("orders", help="读取我的订单列表")
    orders.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    orders.add_argument(
        "--status",
        choices=("待付款", "待发货", "待收货", "待评价", "退款中", "交易成功", "交易关闭"),
        help="按页面上的订单状态筛选",
    )

    publish = commands.add_parser("publish", help="预览或发布商品；默认仅 dry-run")
    publish.add_argument("--title", required=True, help="商品标题")
    publish.add_argument("--description", required=True, help="商品描述")
    publish.add_argument("--price", required=True, help="售价")
    publish.add_argument("--original-price", help="原价")
    publish.add_argument("--images", nargs="+", required=True, help="本地图片路径，最多 9 张")
    publish.add_argument("--delivery", choices=("包邮", "按距离计费", "一口价", "无需邮寄"), default="包邮")
    publish.add_argument("--pickup", action="store_true", help="开启同城自提")
    publish.add_argument("--confirm-publish", action="store_true", help="确认真实发布一次")
    publish.add_argument("--idempotency-key", help="真实发布必填；相同键只提交一次")

    edit = commands.add_parser("edit-item", help="预览或编辑自己的商品；默认仅 dry-run")
    edit.add_argument("--item-id", required=True, help="闲鱼商品 ID")
    edit.add_argument("--title", help="新标题")
    edit.add_argument("--description", help="新描述")
    edit.add_argument("--price", help="新售价")
    edit.add_argument("--original-price", help="新原价")
    edit.add_argument("--images", nargs="+", help="追加这些本地图片，最多 9 张")
    edit.add_argument("--delivery", choices=("包邮", "按距离计费", "一口价", "无需邮寄"))
    edit.add_argument("--pickup", action="store_true", help="开启同城自提")
    edit.add_argument("--confirm-edit", action="store_true", help="确认真实保存一次")
    edit.add_argument("--idempotency-key", help="真实编辑必填；相同键只提交一次")

    manage = commands.add_parser("manage-item", help="预览或变更自己的商品状态；默认仅 dry-run")
    manage.add_argument("--item-id", required=True, help="闲鱼商品 ID")
    manage.add_argument("--action", required=True, choices=("unlist", "mark-sold", "relist", "delete"))
    manage.add_argument("--confirm-action", action="store_true", help="确认执行一次状态变更")
    manage.add_argument("--confirm-delete", help="删除时必须再次传入完全相同的商品 ID")
    manage.add_argument("--idempotency-key", help="真实状态变更必填；相同键只提交一次")
    return parser


async def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "login":
        return await _run_login(args)
    if args.command == "capabilities":
        return _capabilities()
    if args.command == "status":
        return await _run_status(args)
    if args.command == "search":
        return await _run_search(args)
    if args.command == "item":
        return await _run_item(args)
    if args.command == "favorites":
        return await _run_favorites(args)
    if args.command in FAVORITE_APIS:
        return await _run_favorite(args)
    if args.command == "my-items":
        return await _run_my_items(args)
    if args.command == "inbox":
        return await _run_inbox(args)
    if args.command == "read-messages":
        return await _run_read_messages(args)
    if args.command == "find-user":
        return await _run_find_user(args)
    if args.command == "send-message":
        return await _run_send_message(args)
    if args.command == "orders":
        return await _run_orders(args)
    if args.command == "publish":
        return await _run_publish(args)
    if args.command == "edit-item":
        return await _run_edit_item(args)
    if args.command == "manage-item":
        return await _run_manage_item(args)
    raise XianyuError("unknown_command", f"未知命令：{args.command}")


def main() -> None:
    _configure_stdio()
    args = _parser().parse_args()
    try:
        result = asyncio.run(_dispatch(args))
    except XianyuError as exc:
        _emit({"success": False, "error": exc.code, "message": exc.message, **exc.details})
        raise SystemExit(2) from None
    except Exception as exc:
        _emit({"success": False, "error": "unexpected_error", "message": str(exc)})
        raise SystemExit(3) from None
    except KeyboardInterrupt:
        _emit({"success": False, "error": "cancelled", "message": "操作已取消"})
        raise SystemExit(130) from None
    if result.get("success") is False and args.command in FAVORITE_APIS:
        _emit(result)
        raise SystemExit(2)
    _emit(result)


if __name__ == "__main__":
    main()
