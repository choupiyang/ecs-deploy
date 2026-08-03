---
name: youngsinsight-subscriber
description: 订阅 Young's Insight（https://mindresonance.online/youngsinsight/）的更新推送。当用户说"订阅 Young's Insight"、"订阅youngsinsight"、"检查youngsinsight有没有更新"、或提到该网站时使用。订阅后定期拉取 feed，有新内容自动给用户摘要。
---

# Young's Insight 订阅技能

Young's Insight 是一个深度阅读博客：AI 日报、政策分析、商业洞察、认知升级（"别读热点，读裂缝"）。
本技能让你的 AI 助手定期收到该站的新文章 / 新信息卡片摘要。

## 一、订阅（仅首次执行）

当用户表达订阅意愿时：

1. 调用订阅接口（`<你的工具名>` 填 openclaw / hermes / claude-code / codex / cursor / 其他，`<用户标识>` 填用户昵称或 ID）：

```bash
curl -s -X POST "https://mindresonance.online/youngsinsight/api/subscribe" \
  -H "Content-Type: application/json" \
  -d '{"agent":"<你的工具名>","user":"<用户标识>","contact":"<可选:邮箱/微信/其他联系方式>"}'
```

2. 从返回 JSON 中取出 `sid` 和 `feed_url`，**持久保存**（配置文件 / 技能目录 / 记忆均可），同时记录 `cursor` 为空字符串。
3. 告知用户："已订阅 Young's Insight ✅ 之后每次会话或定时任务会自动检查更新并给你摘要。"

> 可选进阶：若你的平台能接收外部 HTTP 回调，可在订阅时加 `"callback_url":"https://你的可接收webhook地址"`，站点更新时会主动推送。

## 二、检查更新（日常）

- **时机**：每次会话开始、用户问"有什么新文章/更新"、或定时任务触发时。推荐每 6 小时检查一次。
- **命令**（用保存的 sid 和 cursor）：

```bash
curl -s "https://mindresonance.online/youngsinsight/api/feed?sid=<sid>&since=<cursor>&limit=20"
```

- **处理返回**：
  - `items` 为空 → 无更新，不打扰用户。
  - `items` 非空 → 整理摘要推给用户，每条格式：
    - 📄 文章 / 🃏 信息卡片 · 标题 · 日期
    - 摘要（excerpt，截断到 80 字）
    - 链接（原样提供，含统计参数）
  - 用返回的 `cursor` 覆盖本地保存的 `cursor`。

- **定时化建议**：
  - Hermes：`cronjob` 创建定时任务，脚本调 curl 存 cursor 到本地文件。
  - OpenClaw / Claude Code / Codex：用平台 cron / hooks / scheduled tasks。
  - 通用：写一个 shell 脚本 + crontab（`0 */6 * * *`），脚本保存 cursor 到 `~/.ys-sub-cursor`。

## 三、退订

```bash
curl -s -X POST "https://mindresonance.online/youngsinsight/api/unsubscribe" \
  -H "Content-Type: application/json" \
  -d '{"sid":"<sid>"}'
```

## 四、说明

- feed 中的链接经过站点统计跳转（用户点击即记为阅读），请原样提供给用户。
- 接口免费、无需鉴权，`sid` 即身份标识，请勿泄露给第三方。
- 站点：https://mindresonance.online/youngsinsight/ ｜ 订阅页：https://mindresonance.online/youngsinsight/subscribe.html
