# Codex 闲鱼安全助手

这是一个面向 Codex 的闲鱼 Skill，通过隔离的本地 Chrome 登录态操作闲鱼官方网站。登录时显示浏览器，其他操作默认在后台运行。

## 让智能体一句话安装

直接对 Codex 说：

> 从 https://github.com/xinyu68/xianyu-safe/tree/main/skills/xianyu-safe 安装这个技能

也可以使用标准安装器：

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" --repo xinyu68/xianyu-safe --path skills/xianyu-safe
```

首次运行会在 Skill 目录的 `.runtime` 中创建隔离 Python 环境并安装锁定版本的 Playwright，不会污染系统 Python。

## 支持的功能

- 扫码登录和登录状态检查
- 按关键词、价格与地区搜索商品，读取商品详情
- 查看收藏列表和商品的本人收藏状态；预览或执行收藏、取消收藏
- 查看自己的商品、消息会话、消息历史和订单
- 查找已有联系人，并在明确确认后发送单条消息
- 预览或执行商品发布、编辑、下架、标记卖出、重新发布和删除
- 写操作默认 dry-run；商品写操作要求确认参数和幂等键
- 收藏/取消收藏也要求确认参数和幂等键；状态已符合要求时不提交
- 除登录或人工验证外，浏览器默认最小化并移出可见桌面

## 更新技能

已安装的 Skill 不会自动跟随 GitHub 更新。更新时删除旧目录并重新安装：

```powershell
$SkillPath = Join-Path $env:USERPROFILE '.codex\skills\xianyu-safe'
if (Test-Path -LiteralPath $SkillPath) {
    Remove-Item -LiteralPath $SkillPath -Recurse -Force
}
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" --repo xinyu68/xianyu-safe --path skills/xianyu-safe
```

## 安全边界

本项目不支持购买、付款、批量私信、自动回复、自动发货、验证码绕过或无人值守操作。登录态保存在用户本机的独立 Chrome Profile 中，程序不会输出 Cookie、Token 或 storage state。

闲鱼页面结构和同源接口可能变化。提交后的页面回执不明确时，工具会停止并禁止自动重试，以降低重复发布或重复发送风险。

## 收藏功能（CLI schema 0.3.0）

使用已安装 Skill 的隔离 Python 运行 `scripts/xianyu_cli.py`：

```text
xianyu_cli.py favorites --limit 20
xianyu_cli.py item --item-id 1234567890
xianyu_cli.py favorite --item-id 1234567890
xianyu_cli.py unfavorite --item-id 1234567890
```

`favorites` 返回收藏商品的 ID、标题、价格、原始商品状态及链接，并提供 `total` 和 `has_more`。`item` 增加 `is_favorited` 字段，表示当前账号是否收藏；`collect_count` 仍表示商品收藏人数，两者不同。

`favorite` / `unfavorite` 默认只预览。用户明确指定商品和操作后，增加 `--confirm-action --idempotency-key <唯一编号>` 执行。例如，在明确授权后：

```text
xianyu_cli.py favorite --item-id 1234567890 --confirm-action --idempotency-key favorite-example-001
xianyu_cli.py unfavorite --item-id 1234567890 --confirm-action --idempotency-key unfavorite-example-001
```

通过浏览器会话调用官网商品页实际使用的明确“添加/取消”接口，并读取详情复核，不使用盲目切换按钮。已经处于目标状态时返回 `already_satisfied`，不提交请求。提交前原子保存脱敏回执；同一编号返回旧回执、不再次提交。`unknown` / `reserved` 表示结果不确定，可用 `item` 只读核验，不自动换编号重试。损坏或达到上限的回执文件会阻止新的写操作。

收藏列表及状态读取可在线只读验证；真实写操作应使用用户明确指定的商品测试，开发过程不会随机收藏推荐商品。

2026-10-13 验证：23 项离线测试通过，当前账号的收藏列表在线只读读取成功。商品详情预览随后触发官网人工验证（`FAIL_SYS_USER_VALIDATE`），已停止继续尝试；真实收藏/取消收藏尚未在线提交验证，需完成官网验证后使用用户指定商品实测。
