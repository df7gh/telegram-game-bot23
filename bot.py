"""Telegram virtual-coin game bot. No real-money betting or cash-out."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sqlite3
import time
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gamebot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "game.sqlite3")

START_COINS = 1000
MIN_STAKE = 20

GAMES = {
    "slot": "🎰 اسلات",
    "dice": "🎲 تاس",
    "bomb": "💣 انفجار",
    "highlow": "📈 های‌لو",
    "roulette": "🎡 رولت",
    "lucky": "🍀 چرخه شانس",
    "coin": "🪙 شیر یا خط",
    "number": "🔢 حدس عدد",
    "basket": "🏀 پرتاب بسکتبال",
    "goal": "⚽ پنالتی",
}


def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                coins INTEGER NOT NULL DEFAULT 1000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS duels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                host_id INTEGER NOT NULL,
                host_name TEXT NOT NULL,
                stake INTEGER NOT NULL,
                status TEXT NOT NULL,
                guest_id INTEGER,
                guest_name TEXT,
                created INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game TEXT NOT NULL,
                stake INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                result TEXT NOT NULL,
                created INTEGER NOT NULL
            )
        """)


def user_row(uid, name):
    name = (name or f"کاربر {uid}")[:80]
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(id,name,coins) VALUES(?,?,?)",
            (uid, name, START_COINS),
        )
        c.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
        return c.execute(
            "SELECT * FROM users WHERE id=?", (uid,)
        ).fetchone()


def balance(uid):
    with db() as c:
        r = c.execute(
            "SELECT coins FROM users WHERE id=?", (uid,)
        ).fetchone()
        return int(r["coins"]) if r else 0


def kb_main():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪙 موجودی", callback_data="bal"),
            InlineKeyboardButton("🏆 رتبه‌بندی", callback_data="top"),
        ],
        [InlineKeyboardButton("🎲 بازی‌ها", callback_data="games")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_row(u.id, u.full_name)
    msg = (
        "سلام! 🎮 به بازی‌های سکه‌ای خوش آمدی.\n"
        "سکه‌ها کاملاً مجازی‌اند و ارزش نقدی یا امکان برداشت ندارند.\n\n"
        "دستورها:\n"
        "/balance موجودی\n"
        "/games فهرست بازی‌ها\n"
        "/top رتبه‌بندی\n"
        "/transfer مقدار انتقال سکه در پاسخ به پیام گیرنده\n"
        "/play نام_بازی مقدار\n"
        "/duel مقدار بازی دونفره در گروه"
    )
    await update.effective_message.reply_text(msg, reply_markup=kb_main())


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    r = user_row(u.id, u.full_name)
    await update.effective_message.reply_text(
        f'🪙 موجودی شما: {r["coins"]} سکه'
    )


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"شناسه تلگرام: {update.effective_user.id}"
    )


async def top_text():
    with db() as c:
        rows = c.execute(
            "SELECT name,coins,wins FROM users "
            "ORDER BY coins DESC,wins DESC LIMIT 10"
        ).fetchall()

    if not rows:
        return "هنوز رتبه‌بندی‌ای وجود ندارد."

    return "🏆 رتبه‌بندی بازیکنان\n" + "\n".join(
        f'{i}. {r["name"]} — {r["coins"]} سکه | برد: {r["wins"]}'
        for i, r in enumerate(rows, 1)
    )


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(await top_text())


async def games_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"game:{key}")]
        for key, label in GAMES.items()
    ]
    await update.effective_message.reply_text(
        "یک بازی انتخاب کن. شرط هر دور از ۵ تا ۵۰۰ سکه است:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def transfer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    msg = update.effective_message

    if not context.args or not context.args[0].isdigit():
        await msg.reply_text(
            "روش استفاده: به پیام گیرنده پاسخ بده و بنویس /transfer 100"
        )
        return

    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target:
        await msg.reply_text(
            "برای انتقال، به پیام گیرنده پاسخ بده و بنویس /transfer 100."
        )
        return

    amount = int(context.args[0])
    if target.id == u.id or target.is_bot or amount < 1:
        await msg.reply_text("گیرنده یا مقدار معتبر نیست.")
        return

    user_row(u.id, u.full_name)
    user_row(target.id, target.full_name)

    with db() as c:
        sender = c.execute(
            "SELECT coins FROM users WHERE id=?", (u.id,)
        ).fetchone()

        if sender["coins"] < amount:
            await msg.reply_text("سکهٔ کافی نداری.")
            return

        c.execute(
            "UPDATE users SET coins=coins-? WHERE id=?", (amount, u.id)
        )
        c.execute(
            "UPDATE users SET coins=coins+? WHERE id=?", (amount, target.id)
        )

    await msg.reply_text(
        f"✅ {amount} سکه به {target.full_name} منتقل شد."
    )


async def duel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            "این فرمان را داخل گروه اجرا کن."
        )
        return

    u = update.effective_user
    user_row(u.id, u.full_name)

    stake = MIN_STAKE
    if context.args and context.args[0].isdigit():
        stake = int(context.args[0])

    if stake < MIN_STAKE or stake > 5000:
        await update.effective_message.reply_text(
            f"شرط باید بین {MIN_STAKE} تا ۵۰۰۰ سکه باشد. "
            "نمونه: /duel 100"
        )
        return

    with db() as c:
        row = c.execute(
            "SELECT coins FROM users WHERE id=?", (u.id,)
        ).fetchone()

        if row["coins"] < stake:
            await update.effective_message.reply_text("سکهٔ کافی نداری.")
            return

        c.execute(
            "UPDATE users SET coins=coins-? WHERE id=?", (stake, u.id)
        )
        cur = c.execute(
            "INSERT INTO duels"
            "(chat_id,host_id,host_name,stake,status,created) "
            "VALUES(?,?,?,?,?,?)",
            (
                update.effective_chat.id, u.id, u.full_name,
                stake, "open", int(time.time()),
            ),
        )
        did = cur.lastrowid

    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("🙋 پیوستن", callback_data=f"join:{did}"),
        InlineKeyboardButton("✖️ لغو بازی", callback_data=f"cancel:{did}"),
    ]])

    await update.effective_message.reply_text(
        f"⚔️ بازی دونفره\nبازیکن اول: {u.full_name}\n"
        f"شرط: {stake} سکه\nبرای شروع، یک نفر دیگر پیوست شود.",
        reply_markup=buttons,
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = q.from_user
    user_row(u.id, u.full_name)
    data = q.data

    if data == "bal":
        await q.edit_message_text(f"🪙 موجودی: {balance(u.id)} سکه")
        return

    if data == "top":
        await q.edit_message_text(await top_text())
        return

    if data == "games":
        buttons = [
            [InlineKeyboardButton(label, callback_data=f"game:{key}")]
            for key, label in GAMES.items()
        ]
        await q.edit_message_text(
            "بازی را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("game:"):
        key = data.split(":", 1)[1]
        if key not in GAMES:
            return
        await q.edit_message_text(
            f"{GAMES[key]}\nبرای بازی با شرط ۲۰ سکه بنویس:\n"
            f"/play {key} 20"
        )
        return

    if data.startswith("join:"):
        did = int(data.split(":")[1])

        with db() as c:
            d = c.execute(
                "SELECT * FROM duels WHERE id=?", (did,)
            ).fetchone()

            if not d or d["status"] != "open":
                await q.edit_message_text("این بازی دیگر در دسترس نیست.")
                return

            if d["host_id"] == u.id:
                await q.answer("نمی‌توانی با خودت بازی کنی.", show_alert=True)
                return

            guest = c.execute(
                "SELECT coins FROM users WHERE id=?", (u.id,)
            ).fetchone()

            if guest["coins"] < d["stake"]:
                await q.answer("سکهٔ کافی نداری.", show_alert=True)
                return

            c.execute(
                "UPDATE users SET coins=coins-? WHERE id=?",
                (d["stake"], u.id),
            )

            winner = random.choice([d["host_id"], u.id])
            prize = d["stake"] * 2
            c.execute(
                "UPDATE users SET coins=coins+?,wins=wins+1 WHERE id=?",
                (prize, winner),
            )

            loser = u.id if winner == d["host_id"] else d["host_id"]
            c.execute(
                "UPDATE users SET losses=losses+1 WHERE id=?", (loser,)
            )
            c.execute(
                'UPDATE duels SET status="done",guest_id=?,guest_name=? '
                "WHERE id=?",
                (u.id, u.full_name, did),
            )

        winner_name = d["host_name"] if winner == d["host_id"] else u.full_name
        await q.edit_message_text(
            f"⚔️ بازی تمام شد!\n{d['host_name']} در برابر {u.full_name}\n"
            f"🏆 برنده: {winner_name}\nجایزه: {prize} سکه"
        )
        return

    if data.startswith("cancel:"):
        did = int(data.split(":")[1])

        with db() as c:
            d = c.execute(
                "SELECT * FROM duels WHERE id=?", (did,)
            ).fetchone()

            if not d or d["status"] != "open":
                await q.edit_message_text("این بازی قبلاً تمام شده.")
                return

            if d["host_id"] != u.id and (
                not OWNER_ID.isdigit() or int(OWNER_ID) != u.id
            ):
                await q.answer(
                    "فقط سازنده می‌تواند لغو کند.", show_alert=True
                )
                return

            c.execute(
                "UPDATE users SET coins=coins+? WHERE id=?",
                (d["stake"], d["host_id"]),
            )
            c.execute(
                'UPDATE duels SET status="cancelled" WHERE id=?', (did,)
            )

        await q.edit_message_text("بازی لغو شد و سکهٔ شرط به سازنده برگشت.")
        return


async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user_row(u.id, u.full_name)
    msg = update.effective_message

    if (
        len(context.args) < 2
        or context.args[0] not in GAMES
        or not context.args[1].isdigit()
    ):
        await msg.reply_text(
            "روش: /play نام_بازی مقدار\n"
            "نمونه: /play dice 20\n"
            "بازی‌ها: " + ", ".join(GAMES)
        )
        return

    key = context.args[0]
    stake = int(context.args[1])

    if stake < 5 or stake > 500:
        await msg.reply_text("شرط باید بین ۵ تا ۵۰۰ سکه باشد.")
        return

    with db() as c:
        row = c.execute(
            "SELECT coins FROM users WHERE id=?", (u.id,)
        ).fetchone()

        if row["coins"] < stake:
            await msg.reply_text("سکهٔ کافی نداری.")
            return

        c.execute(
            "UPDATE users SET coins=coins-? WHERE id=?", (stake, u.id)
        )
        won = random.random() < 0.48
        delta = stake if won else -stake

        if won:
            c.execute(
                "UPDATE users SET coins=coins+?,wins=wins+1 WHERE id=?",
                (stake * 2, u.id),
            )
        else:
            c.execute(
                "UPDATE users SET losses=losses+1 WHERE id=?", (u.id,)
            )

        c.execute(
            "INSERT INTO rounds(user_id,game,stake,delta,result,created) "
            "VALUES(?,?,?,?,?,?)",
            (
                u.id, key, stake, delta,
                "win" if won else "loss", int(time.time()),
            ),
        )
        new = c.execute(
            "SELECT coins FROM users WHERE id=?", (u.id,)
        ).fetchone()["coins"]

    await msg.reply_text(
        f'{GAMES[key]}: {"🎉 بردی" if won else "😕 باختی"}\n'
        f"تغییر سکه: {'+' if won else '-'}{stake}\n"
        f"موجودی: {new} سکه"
    )


async def api_handler(request):
    if request.method == "GET":
        return web.json_response({"ok": True, "games": GAMES})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    return web.json_response({
        "error": "Mini App غیرفعال است؛ از دستورهای ربات استفاده کن."
    }, status=410)


async def post_init(app):
    init_db()
    webapp = web.Application()
    webapp.router.add_get("/api", api_handler)
    webapp.router.add_post("/api", api_handler)

    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    app.bot_data["web_runner"] = runner
    log.info("API server listening on port %s", PORT)


async def post_shutdown(app):
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")

    init_db()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    handlers = [
        ("start", start),
        ("balance", balance_cmd),
        ("id", id_cmd),
        ("top", top_cmd),
        ("games", games_cmd),
        ("transfer", transfer_cmd),
        ("duel", duel_cmd),
        ("play", play_cmd),
    ]
    for cmd, fn in handlers:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(callback))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
