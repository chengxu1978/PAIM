"""多 BOT 网关：Telegram 作为手机端 IM，后台为每个 BOT 绑定不同的大模型。

- 每个 BOT = 一个 Telegram Bot token + 一个 provider（OpenAI 兼容接口）+ 一个模型 + 一段人设
- 所有 BOT 在同一个进程里并发运行，可在运行时通过"管理 BOT"增删、改模型，无需重启
- 对话历史按 (bot, chat) 存在 SQLite
"""
import asyncio
import json
import logging
import os
import sqlite3
import time

import httpx
import yaml
from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx 的日志会把带 token 的 URL 打出来，压低级别
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("botgw")

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")
DB_PATH = os.getenv("DB_PATH", "/data/bots.db")
TG_LIMIT = 4000  # Telegram 单条消息上限 4096，留点余量

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索最新信息。需要实时、最新或你不确定的事实时调用。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
    },
}
SEARCH_HINT = (
    "你可以调用 web_search 获取最新信息。引用搜索结果时，在回答末尾用 Markdown 链接"
    "列出来源，不要编造链接。"
)


def parse_ids(s: str) -> set[int]:
    return {int(x) for x in s.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


ALLOWED = parse_ids(os.getenv("ALLOWED_USER_IDS", ""))
ADMINS = parse_ids(os.getenv("ADMIN_USER_IDS", "")) or ALLOWED


# ---------------------------------------------------------------- 存储
class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS bots(
                name TEXT PRIMARY KEY, token TEXT NOT NULL, provider TEXT NOT NULL,
                model TEXT NOT NULL, system_prompt TEXT NOT NULL DEFAULT '',
                max_history INTEGER NOT NULL DEFAULT 20);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, chat_id INTEGER,
                role TEXT, content TEXT, ts REAL);
            CREATE INDEX IF NOT EXISTS idx_msg ON messages(bot, chat_id, id);
            """
        )

    def upsert_bot(self, spec: dict, overwrite: bool = True):
        verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
        self.c.execute(
            f"{verb} INTO bots(name,token,provider,model,system_prompt,max_history)"
            " VALUES(?,?,?,?,?,?)",
            (
                spec["name"], spec["token"], spec["provider"], spec["model"],
                spec.get("system_prompt", ""), spec.get("max_history", 20),
            ),
        )
        self.c.commit()

    def get_bot(self, name: str):
        return self.c.execute("SELECT * FROM bots WHERE name=?", (name,)).fetchone()

    def all_bots(self):
        return self.c.execute("SELECT * FROM bots ORDER BY name").fetchall()

    def del_bot(self, name: str):
        self.c.execute("DELETE FROM bots WHERE name=?", (name,))
        self.c.commit()

    def update_bot(self, name: str, **fields):
        for k, v in fields.items():
            self.c.execute(f"UPDATE bots SET {k}=? WHERE name=?", (v, name))
        self.c.commit()

    def add_msg(self, bot, chat_id, role, content):
        self.c.execute(
            "INSERT INTO messages(bot,chat_id,role,content,ts) VALUES(?,?,?,?,?)",
            (bot, chat_id, role, content, time.time()),
        )
        self.c.commit()

    def history(self, bot, chat_id, n):
        rows = self.c.execute(
            "SELECT role,content FROM messages WHERE bot=? AND chat_id=?"
            " ORDER BY id DESC LIMIT ?",
            (bot, chat_id, n),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear(self, bot, chat_id):
        self.c.execute("DELETE FROM messages WHERE bot=? AND chat_id=?", (bot, chat_id))
        self.c.commit()


# ---------------------------------------------------------------- 网关
class Gateway:
    def __init__(self, cfg: dict, db: DB):
        self.cfg = cfg
        self.db = db
        self.providers = cfg.get("providers", {})
        self.clients: dict[str, AsyncOpenAI] = {}
        self.running: dict[str, Application] = {}
        # 联网搜索（SearXNG）与语音转写（OpenAI 兼容的 /audio/transcriptions）
        self.search = cfg.get("search") or {}
        self.search_url = (self.search.get("searxng_url") or "").rstrip("/")
        self.tr = cfg.get("transcription") or {}

    # --- LLM
    def client(self, provider: str) -> AsyncOpenAI:
        if provider not in self.clients:
            p = self.providers[provider]
            key = os.getenv(p["api_key_env"], "")
            if not key:
                raise RuntimeError(f"环境变量 {p['api_key_env']} 未设置")
            self.clients[provider] = AsyncOpenAI(
                base_url=p["base_url"], api_key=key, timeout=120
            )
        return self.clients[provider]

    async def web_search(self, query: str) -> str:
        n = int(self.search.get("max_results", 5))
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.get(
                    f"{self.search_url}/search",
                    params={"q": query, "format": "json"},
                )
                r.raise_for_status()
                items = r.json().get("results", [])[:n]
        except Exception as e:  # noqa: BLE001
            return f"搜索失败：{type(e).__name__}"
        if not items:
            return "没有搜索结果。"
        return "\n\n".join(
            f"[{i}] {it.get('title', '')}\n{it.get('url', '')}\n{(it.get('content') or '')[:400]}"
            for i, it in enumerate(items, 1)
        )

    async def run_tool(self, name: str, arguments: str) -> str:
        if name == "web_search":
            try:
                q = json.loads(arguments or "{}").get("query", "")
            except json.JSONDecodeError:
                q = ""
            return await self.web_search(q) if q else "缺少 query 参数。"
        return f"未知工具：{name}"

    async def answer(self, name: str, chat_id: int, text: str) -> str:
        spec = self.db.get_bot(name)
        self.db.add_msg(name, chat_id, "user", text)
        sys_prompt = spec["system_prompt"]
        tools = [SEARCH_TOOL] if self.search_url else None
        if tools:
            sys_prompt = (sys_prompt + "\n\n" if sys_prompt else "") + SEARCH_HINT
        msgs = []
        if sys_prompt:
            msgs.append({"role": "system", "content": sys_prompt})
        msgs += self.db.history(name, chat_id, spec["max_history"])
        client = self.client(spec["provider"])

        out = ""
        for rnd in range(4):
            if rnd == 3:  # 最多 3 轮工具调用，最后一轮必须给出文字答案
                tools = None
            kwargs = {"model": spec["model"], "messages": msgs}
            if tools:
                kwargs["tools"] = tools
            try:
                resp = await client.chat.completions.create(**kwargs)
            except Exception:
                if not tools:
                    raise
                # 该模型可能不支持工具调用，退回纯对话
                log.warning("%s 不支持工具调用，退回纯对话", spec["model"])
                tools = None
                continue
            m = resp.choices[0].message
            if tools and m.tool_calls:
                msgs.append(
                    {
                        "role": "assistant",
                        "content": m.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in m.tool_calls
                        ],
                    }
                )
                for tc in m.tool_calls:
                    result = await self.run_tool(tc.function.name, tc.function.arguments)
                    msgs.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result}
                    )
                continue
            out = (m.content or "").strip()
            break
        out = out or "（模型返回为空）"
        self.db.add_msg(name, chat_id, "assistant", out)
        return out

    async def transcribe(self, data: bytes) -> str:
        kwargs = {"model": self.tr["model"], "file": ("voice.ogg", data)}
        if self.tr.get("language"):
            kwargs["language"] = self.tr["language"]
        r = await self.client(self.tr["provider"]).audio.transcriptions.create(**kwargs)
        return (r.text or "").strip()

    # --- 聊天 BOT 生命周期
    async def start_bot(self, spec) -> None:
        name = spec["name"]
        if name in self.running:
            await self.stop_bot(name)
        app = Application.builder().token(spec["token"]).build()
        app.bot_data["name"] = name
        app.add_handler(CommandHandler("start", self.h_start))
        app.add_handler(CommandHandler("reset", self.h_reset))
        app.add_handler(CommandHandler("model", self.h_model))
        app.add_handler(CommandHandler("prompt", self.h_prompt))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.h_text))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.h_voice))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        self.running[name] = app
        log.info("bot started: %s (%s / %s)", name, spec["provider"], spec["model"])

    async def stop_bot(self, name: str) -> None:
        app = self.running.pop(name, None)
        if not app:
            return
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("bot stopped: %s", name)

    # --- 权限
    async def guard(self, update: Update) -> bool:
        uid = update.effective_user.id
        if uid in ALLOWED:
            return True
        await update.message.reply_text(
            f"你还没有被授权使用这个 BOT。你的用户 ID 是 {uid}，"
            "请管理员把它加到 ALLOWED_USER_IDS。"
        )
        return False

    # --- 聊天 BOT 的处理器
    async def h_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        spec = self.db.get_bot(ctx.application.bot_data["name"])
        await update.message.reply_text(
            f"我是 {spec['name']}，当前模型：{spec['model']}。\n"
            "直接发消息即可对话。\n/reset 清空上下文\n/model [名称] 查看或切换模型\n/prompt [文本] 查看或设置人设"
        )

    async def h_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        self.db.clear(ctx.application.bot_data["name"], update.effective_chat.id)
        await update.message.reply_text("上下文已清空。")

    async def h_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        name = ctx.application.bot_data["name"]
        if ctx.args:
            self.db.update_bot(name, model=ctx.args[0])
        spec = self.db.get_bot(name)
        await update.message.reply_text(f"{spec['provider']} / {spec['model']}")

    async def h_prompt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        name = ctx.application.bot_data["name"]
        if ctx.args:
            self.db.update_bot(name, system_prompt=" ".join(ctx.args))
            self.db.clear(name, update.effective_chat.id)
            await update.message.reply_text("人设已更新，上下文已清空。")
        else:
            await update.message.reply_text(
                self.db.get_bot(name)["system_prompt"] or "（未设置人设）"
            )

    async def respond(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
        name = ctx.application.bot_data["name"]
        chat_id = update.effective_chat.id
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            out = await self.answer(name, chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.exception("llm error")
            out = f"调用模型出错：{type(e).__name__}: {str(e)[:300]}"
        for i in range(0, len(out), TG_LIMIT):
            await update.message.reply_text(out[i : i + TG_LIMIT])

    async def h_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        await self.respond(update, ctx, update.message.text)

    async def h_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        if not self.tr:
            await update.message.reply_text("还没有配置语音转写（config.yaml 的 transcription）。")
            return
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            media = update.message.voice or update.message.audio
            f = await media.get_file()
            text = await self.transcribe(bytes(await f.download_as_bytearray()))
        except Exception as e:  # noqa: BLE001
            log.exception("transcribe error")
            await update.message.reply_text(f"语音转写出错：{type(e).__name__}: {str(e)[:200]}")
            return
        if not text:
            await update.message.reply_text("没听清，请再说一次。")
            return
        await update.message.reply_text(f"🎙 {text}")
        await self.respond(update, ctx, text)

    # --- 管理 BOT（相当于 BotBoss）
    async def run_manager(self, token: str):
        app = Application.builder().token(token).build()

        async def admin(update: Update) -> bool:
            if update.effective_user.id in ADMINS:
                return True
            await update.message.reply_text("仅管理员可用。")
            return False

        async def start(update, ctx):
            if not await admin(update):
                return
            await update.message.reply_text(
                "管理命令：\n"
                "/bots 列出所有 BOT\n"
                "/providers 列出可用模型服务\n"
                "/addbot <名称> <BotFather的token> <provider> <模型> [人设...]\n"
                "/setmodel <名称> <provider> <模型>\n"
                "/delbot <名称>\n\n"
                "先在 @BotFather 里 /newbot 拿到 token，再用 /addbot 注册。"
            )

        async def bots(update, ctx):
            if not await admin(update):
                return
            rows = self.db.all_bots()
            if not rows:
                await update.message.reply_text("还没有 BOT。")
                return
            lines = [
                f"{'🟢' if r['name'] in self.running else '🔴'} {r['name']} · "
                f"{r['provider']}/{r['model']}"
                for r in rows
            ]
            await update.message.reply_text("\n".join(lines))

        async def providers(update, ctx):
            if not await admin(update):
                return
            await update.message.reply_text("\n".join(self.providers) or "（无）")

        async def addbot(update, ctx):
            if not await admin(update):
                return
            try:  # 消息里带 token，尽量删掉
                await update.message.delete()
            except Exception:  # noqa: BLE001
                pass
            a = ctx.args
            if len(a) < 4:
                await ctx.bot.send_message(
                    update.effective_chat.id,
                    "用法：/addbot <名称> <token> <provider> <模型> [人设...]",
                )
                return
            name, token, provider, model = a[:4]
            if provider not in self.providers:
                await ctx.bot.send_message(
                    update.effective_chat.id,
                    f"未知 provider：{provider}，可用：{', '.join(self.providers)}",
                )
                return
            spec = dict(
                name=name, token=token, provider=provider, model=model,
                system_prompt=" ".join(a[4:]),
            )
            try:
                await self.start_bot(spec)
            except Exception as e:  # noqa: BLE001
                await ctx.bot.send_message(
                    update.effective_chat.id, f"启动失败（token 是否正确？）：{type(e).__name__}"
                )
                return
            self.db.upsert_bot(spec)
            await ctx.bot.send_message(
                update.effective_chat.id, f"✅ BOT「{name}」已上线（{provider}/{model}）。"
            )

        async def setmodel(update, ctx):
            if not await admin(update):
                return
            if len(ctx.args) != 3 or not self.db.get_bot(ctx.args[0]):
                await update.message.reply_text("用法：/setmodel <名称> <provider> <模型>")
                return
            name, provider, model = ctx.args
            if provider not in self.providers:
                await update.message.reply_text(f"未知 provider：{provider}")
                return
            self.db.update_bot(name, provider=provider, model=model)
            await update.message.reply_text(f"「{name}」已切换到 {provider}/{model}")

        async def delbot(update, ctx):
            if not await admin(update):
                return
            if len(ctx.args) != 1 or not self.db.get_bot(ctx.args[0]):
                await update.message.reply_text("用法：/delbot <名称>")
                return
            await self.stop_bot(ctx.args[0])
            self.db.del_bot(ctx.args[0])
            await update.message.reply_text(f"已删除「{ctx.args[0]}」")

        for cmd, fn in [
            ("start", start), ("bots", bots), ("providers", providers),
            ("addbot", addbot), ("setmodel", setmodel), ("delbot", delbot),
        ]:
            app.add_handler(CommandHandler(cmd, fn))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("manager bot started")


async def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db = DB(DB_PATH)
    gw = Gateway(cfg, db)

    # 把 config.yaml 里的 BOT 种进数据库（已存在的不覆盖，运行时改动优先）
    for b in cfg.get("bots", []):
        token = os.getenv(b["token_env"], "")
        if not token:
            log.warning("跳过 %s：环境变量 %s 未设置", b["name"], b["token_env"])
            continue
        db.upsert_bot({**b, "token": token}, overwrite=False)

    for row in db.all_bots():
        try:
            await gw.start_bot(dict(row))
        except Exception:  # noqa: BLE001
            log.exception("启动 %s 失败", row["name"])

    mgr = os.getenv("MANAGER_BOT_TOKEN")
    if mgr:
        await gw.run_manager(mgr)
    else:
        log.info("未设置 MANAGER_BOT_TOKEN，管理 BOT 未启用")

    if not ALLOWED:
        log.warning("ALLOWED_USER_IDS 为空：所有人都会被拒绝，并看到自己的用户 ID")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
