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
- 查看自己的商品、消息会话、消息历史和订单
- 查找已有联系人，并在明确确认后发送单条消息
- 预览或执行商品发布、编辑、下架、标记卖出、重新发布和删除
- 写操作默认 dry-run；商品写操作要求确认参数和幂等键
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
