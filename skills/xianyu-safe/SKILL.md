---
name: xianyu-safe
description: "Safely operate Xianyu through an isolated local Chrome profile: QR login, product search/details, favorites, own listings, messages, orders, and approved account writes. Use for 闲鱼登录、搜索、商品详情、收藏/取消收藏/收藏列表、消息、订单或商品管理；do not use for purchasing, payment, captcha bypass, bulk outreach, or unattended interaction."
---

# Xianyu Safe

Use the bundled CLI for every supported operation. It drives the official
`goofish.com` website and its same-origin page APIs through a dedicated
persistent Chrome profile. Never print cookies, tokens, or storage-state data.

Resolve this skill's directory as `SKILL_DIR` before constructing commands.

## Runtime

If the isolated runtime is absent, or `capabilities` cannot import Playwright,
run:

```text
python <SKILL_DIR>/scripts/bootstrap.py
```

Use the Python path printed by the bootstrap script. It is normally
`<SKILL_DIR>/.runtime/Scripts/python.exe` on Windows and
`<SKILL_DIR>/.runtime/bin/python` on macOS or Linux.

## Commands

Run commands with the isolated runtime Python:

```powershell
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py login
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py capabilities
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py status
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py verify --item-id 1234567890 --timeout 180
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py search --keyword "iPhone 15" --province 北京 --city 北京 --min-price 3000 --max-price 5000 --limit 20
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py item --item-id 1234567890
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py favorites --limit 20
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py favorite --item-id 1234567890
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py unfavorite --item-id 1234567890
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py my-items --limit 20
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py inbox --limit 20
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py read-messages --nickname "卖家昵称" --limit 50
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py find-user --nickname "卖家昵称"
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py send-message --nickname "卖家昵称" --content "消息"
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py orders --status 待发货 --limit 20
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py publish --title "标题" --description "描述" --price 99 --images D:/images/1.jpg
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py edit-item --item-id 1234567890 --price 89
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py manage-item --item-id 1234567890 --action unlist
```

## Workflow

- For login requests, run `login`. Tell the user that a dedicated Chrome window
  will open, then wait for them to scan the official QR code and confirm on the
  phone. Report only the returned nickname and status.
- Non-login queries and writes start Chrome minimized and outside the visible
  desktop. `verify` is visible, and a detected human-verification requirement
  restores the dedicated window for manual handling. Use `--headless` only as
  an explicit fallback because Xianyu may reject true headless browsing.
- For read requests, ensure `status` reports `logged_in: true`. Use `search`
  filters instead of filtering an unrelated page locally. Summarize structured
  JSON rather than dumping raw page text.
- For messaging, use `find-user` to resolve exact nickname matches in existing
  conversations. Verify the user ID and item before sending. First run without
  `--confirm-send` for a dry-run preview. Add `--confirm-send` only after the user
  explicitly authorizes the exact recipient and exact message. Never retry a
  send whose receipt is uncertain.
- `favorites` reads only the current account's collection, not recommendations.
  `item` includes `is_favorited` (null means unknown, never infer it from the
  collection count). `favorite` and `unfavorite` preview by default. An explicit
  user request for a specific item authorizes the matching operation; inspect
  its preview, then use `--confirm-action --idempotency-key <unique-key>`.
  Do not ask again when that item and action are already authorized. Already
  matching states are no-ops. Writes use explicit add/remove page APIs and
  verify via item detail. Reusing a key returns the original receipt without
  submitting again; it is not a current-state query. For unknown/reserved
  receipts, read `item` and report uncertainty; do not automatically retry with
  a new key. Adding this capability does not authorize favoriting arbitrary
  products as a live test.
- `publish`, `edit-item`, and `manage-item` are dry-run by default. For a real
  write, show the preview, obtain explicit approval, then add the matching
  `--confirm-*` flag and a unique `--idempotency-key`. For deletion, also pass
  `--confirm-delete` with the exact item ID. Submit once; an uncertain receipt is
  not permission to retry. Before any real write, read
  [write safety](references/safety.md).
- Treat login challenges, captchas, device confirmation, and risk-control pages
  as user actions. Never bypass them or ask the user to paste cookies.
- `human_verification_required` preserves the original `FAIL_SYS_USER_VALIDATE`
  / `RGV587_ERROR` response instead of reporting only the SDK's later timeout.
  The CLI shows the dedicated Chrome and waits up to 180 seconds by default.
  If stderr says the verification window is open while the command is still
  running, promptly tell the user to handle it and keep that process alive;
  don't start another browser using the same profile. The global option
  `--verification-timeout 0` (before the command) disables showing/waiting.
  Headless runs report that a visible `verify --item-id ID` is needed.
- Manual handling never resumes or resends the interrupted business action.
  The old SDK document is discarded first so its pending JavaScript cannot
  resume a write. A success response for the blocked read API (or a fresh item
  detail read after a blocked favorite write) confirms access and saves
  the session. Closing a dialog alone is not proof. The window closes after
  completion, user close, or the deadline; `verification.state` distinguishes
  these outcomes. `verify` can reopen the exact item page without favoriting.
  After completion, re-run a read or preview. If a favorite error says
  `submitted:true` / `outcome:unknown`, preserve its receipt and inspect the
  item; do not resend it. `submitted:false` means no collection request was
  dispatched by that attempt.
- `mtop_timeout` without a captured validation response has an undetermined
  cause. Do not call it an outage, promise that waiting will fix it, or claim
  human verification is certain. Report the diagnostic and stop repeated calls.
- Stop after repeated page failures and report the diagnostic message. Do not
  broaden a failed operation into purchasing, payment, bulk outreach, or another
  account change.

## Current boundary

This version supports login status, interactive QR-code login, server-filtered
search, item details, favorite-list lookup, approved favorite/unfavorite,
own-listing lookup, inbox and message history, order lookup,
explicitly confirmed one-off messages, and approval-gated publish/edit/unlist/
mark-sold/relist/delete operations. Purchasing, payment, automatic replies,
automatic delivery, bulk messaging, captcha bypass, and unattended operation are
not supported.

For implementation provenance and upstream comparisons, read
[GitHub references](references/github-projects.md) only when maintaining this
skill.
