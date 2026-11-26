# 个人助理：多 BOT 网关（Telegram + OpenAI 兼容模型）

手机端用 Telegram，后台这个服务跑在 VPS / NAS 的 Docker 里。每个 BOT 有自己的 token、模型和人设，可以在手机上随时增删、切换模型。

## 功能

- 多个 BOT 同时在线，各自绑定不同的大模型（任何 OpenAI 兼容接口：DeepSeek、Kimi、OpenAI、Ollama 等）
- 手机上通过"管理 BOT"新增、删除 BOT、切换模型，无需重启
- 语音消息：自动转写后再交给模型回答（Whisper 兼容接口，如 Groq、OpenAI）
- 联网搜索：模型按需调用自建 SearXNG，并在回答末尾附来源链接；模型不支持工具调用时自动退回纯对话
- 对话历史按 BOT 和聊天分别存在 SQLite
- 用户白名单，默认拒绝所有人

## 部署

1. 在 Telegram 里找 @BotFather，`/newbot` 创建一个**管理 BOT**，再按需创建若干**聊天 BOT**，记下 token。
2. 在 VPS/NAS 上：
   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env      # 填 token 和模型密钥
   # 修改 searxng/settings.yml 里的 secret_key 为随机字符串
   docker compose up -d --build
   docker compose logs -f
   ```
3. 给任意一个 BOT 发 `/start`，会回显你的用户 ID；填进 `.env` 的 `ALLOWED_USER_IDS`（多人用逗号分隔），`docker compose up -d` 重启生效。

不需要语音或搜索时，删掉 `config.yaml` 里对应的 `transcription` / `search` 段即可，搜索还可以把 compose 里的 `searxng` 服务一起去掉。

## 在手机上管理 BOT（给管理 BOT 发）

| 命令 | 作用 |
|---|---|
| `/bots` | 列出所有 BOT 及在线状态 |
| `/providers` | 列出可用模型服务 |
| `/addbot 名称 token provider 模型 [人设]` | 新增并立即上线 |
| `/setmodel 名称 provider 模型` | 切换模型，立即生效 |
| `/delbot 名称` | 下线并删除 |

## 在聊天 BOT 里

`/reset` 清空上下文，`/model [名称]` 查看/切换模型，`/prompt [文本]` 查看/设置人设。直接发文字或语音即可对话。

## 安全提示

- 不设置 `ALLOWED_USER_IDS` 时所有人都会被拒绝，避免陌生人白嫖你的 API 额度。
- `/addbot` 的消息里有 token，程序会尝试删除该消息；建议只私聊使用。
- `.env`、`config.yaml`、`data/bots.db`（里面存了 token）已在 `.gitignore` 中，不要提交，也不要对公网暴露。
- 国内 VPS/NAS 访问 Telegram 需要能连通 `api.telegram.org`（可在 compose 里给 botgw 加 `HTTPS_PROXY` 环境变量）。
- 语音和搜索关键词会发给对应的第三方服务（转写服务、搜索引擎），请按需选择。

## 后续可扩展

图片理解、更多工具（日历、提醒、文件）、流式输出（边生成边编辑消息）、按 BOT 设置用户白名单。

## 协议

MIT
