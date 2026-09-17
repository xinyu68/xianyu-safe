---
name: xianyu-safe
description: "Safely operate Xianyu through an isolated local Chrome profile: visible QR login, filtered product search and detail lookup, own listings, inbox and message history, order lookup, explicitly approved one-off messaging, and approval-gated listing publish/edit/state management. Use for 闲鱼登录、搜索、商品详情、消息、订单或商品管理；do not use for purchasing, payment, captcha bypass, bulk outreach, or unattended interaction."
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
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py search --keyword "iPhone 15" --province 北京 --city 北京 --min-price 3000 --max-price 5000 --limit 20
<runtime-python> <SKILL_DIR>/scripts/xianyu_cli.py item --item-id 1234567890
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
- All non-login commands start a normal Chrome renderer minimized and outside the
  visible desktop by default. This preserves headed-browser compatibility without
  taking focus. Use `--headless` only as an explicit fallback because Xianyu may
  reject true headless browsing.
- For read requests, ensure `status` reports `logged_in: true`. Use `search`
  filters instead of filtering an unrelated page locally. Summarize structured
  JSON rather than dumping raw page text.
- For messaging, use `find-user` to resolve exact nickname matches in existing
  conversations. Verify the user ID and item before sending. First run without
  `--confirm-send` for a dry-run preview. Add `--confirm-send` only after the user
  explicitly authorizes the exact recipient and exact message. Never retry a
  send whose receipt is uncertain.
- `publish`, `edit-item`, and `manage-item` are dry-run by default. For a real
  write, show the preview, obtain explicit approval, then add the matching
  `--confirm-*` flag and a unique `--idempotency-key`. For deletion, also pass
  `--confirm-delete` with the exact item ID. Submit once; an uncertain receipt is
  not permission to retry. Before any real write, read
  [write safety](references/safety.md).
- Treat login challenges, captchas, device confirmation, and risk-control pages
  as user actions. Never bypass them or ask the user to paste cookies.
- Stop after repeated page failures and report the diagnostic message. Do not
  broaden a failed operation into purchasing, payment, bulk outreach, or another
  account change.

## Current boundary

This version supports login status, interactive QR-code login, server-filtered
search, item details, own-listing lookup, inbox and message history, order lookup,
explicitly confirmed one-off messages, and approval-gated publish/edit/unlist/
mark-sold/relist/delete operations. Purchasing, payment, automatic replies,
automatic delivery, bulk messaging, captcha bypass, and unattended operation are
not supported.

For implementation provenance and upstream comparisons, read
[GitHub references](references/github-projects.md) only when maintaining this
skill.
