import os
import re
import time
import random
import asyncio
import logging
import threading
import http.server
import socketserver
from collections import defaultdict

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

# Keep Alive Server (for Render/Host)
def keep_alive():
    try:
        port = int(os.environ.get("PORT", 10000))
    except Exception:
        port = 10000

    class SilentHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            return

    try:
        with socketserver.TCPServer(("", port), SilentHandler) as httpd:
            httpd.serve_forever()
    except Exception:
        return

threading.Thread(target=keep_alive, daemon=True).start()

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ Please set BOT_TOKEN in environment variables.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("word_clash")

JOIN_DURATION = 10  # seconds
ROUND_COUNT = 3
sessions = {}
scores = defaultdict(lambda: 0)

def normalize_text(t: str) -> str:
    return re.sub(r'[^a-z]', '', (t or "").lower())

def new_session(chat_id: int, mode: str):
    return {
        "state": "INIT",
        "round": 0,
        "joined": [],
        "active": [],
        "leader": None,
        "secret": None,
        "chat_id": chat_id,
        "mode": mode,
        "round_scores": {},
        "cancelled": False
    }

async def start_bot():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # /restart command
    @dp.message(Command("restart"))
    async def restart_command(message: types.Message):
        chat_id = message.chat.id
        if chat_id in sessions:
            await message.answer("♻ Game restarting in 6 seconds...")
            sessions[chat_id]["cancelled"] = True
            await asyncio.sleep(6)
            sessions.pop(chat_id, None)
        await start_join_phase(chat_id, "rapid")  # default mode on restart

    # /start command
    @dp.message(Command("start"))
    async def handle_start(message: types.Message):
        if message.chat.type == "private":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Add me to a group", url=f"https://t.me/{(await bot.me()).username}?startgroup=start")],
                [InlineKeyboardButton(text="📢 News Channel", url="https://t.me/WordClash_News")]
            ])
            await message.answer(
                "👋 Hi! I'm your Word Clash bot.\nChoose a game mode by adding me to your group.",
                reply_markup=kb
            )
        else:
            # clear any stuck session
            if message.chat.id in sessions:
                sessions.pop(message.chat.id, None)

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Rapid Mode", callback_data=f"mode:rapid:{message.chat.id}")],
                [InlineKeyboardButton(text="🎯 Round-wise Mode", callback_data=f"mode:round:{message.chat.id}")]
            ])
            await message.answer("🎮 Choose a game mode:", reply_markup=kb)

    # Mode selection
    @dp.callback_query(lambda c: c.data and c.data.startswith("mode:"))
    async def choose_mode(callback: types.CallbackQuery):
        _, mode, chat_id = callback.data.split(":")
        chat_id = int(chat_id)
        await callback.answer("✅ Mode selected")
        await start_join_phase(chat_id, mode)

    # Join game
    @dp.callback_query(lambda c: c.data and c.data.startswith("join:"))
    async def join_callback(callback: types.CallbackQuery):
        data = callback.data
        chat_id = int(data.split(":")[1])
        s = sessions.get(chat_id)
        if not s or s["state"] != "JOINING":
            await callback.answer("🚫 Join time is over.", show_alert=True)
            return
        uid = callback.from_user.id
        uname = callback.from_user.username or callback.from_user.full_name
        if uid in [u[0] for u in s["joined"]]:
            await callback.answer("😏 Already joined.", show_alert=True)
            return
        s["joined"].append((uid, uname))
        await callback.answer("🎯 Joined!")

    # Leader sets word
    @dp.message(Command("word"))
    async def word_handler(message: types.Message):
        s = sessions.get(message.chat.id) or sessions.get(message.from_user.id)
        if not s or not s["leader"] or s["leader"][0] != message.from_user.id:
            return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("❌ Please provide a word.")
            return
        s["secret"] = normalize_text(parts[1])
        await message.answer("✅ Word set successfully!")
        await bot.send_message(s["chat_id"], "💡 Word is set! Players, start guessing!")

    async def start_join_phase(chat_id: int, mode: str):
        # clear old session
        if chat_id in sessions:
            sessions.pop(chat_id, None)

        s = new_session(chat_id, mode)
        sessions[chat_id] = s
        s["state"] = "JOINING"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 JOIN Game", callback_data=f"join:{chat_id}")]
        ])
        msg = await bot.send_message(chat_id, f"🎮 *New Game ({mode.title()} Mode)*\nYou have *{JOIN_DURATION} seconds* to join.\n\nPlayers: _None yet_", reply_markup=kb, parse_mode="Markdown")
        s["join_msg_id"] = msg.message_id

        for t in range(JOIN_DURATION, 0, -1):
            if s.get("cancelled"):
                return
            joined_names = ", ".join([f"@{u[1]}" for u in s["joined"]]) or "_None yet_"
            try:
                await bot.edit_message_text(
                    f"🎮 *New Game ({mode.title()} Mode)*\nYou have *{t} seconds* to join.\n\nPlayers: {joined_names}",
                    chat_id, s["join_msg_id"], reply_markup=kb, parse_mode="Markdown"
                )
            except:
                pass
            await asyncio.sleep(1)

        min_players = 2 if mode == "rapid" else 3
        if len(s["joined"]) >= min_players:
            await start_game(chat_id)
        else:
            await bot.send_message(chat_id, "😴 Not enough players. Game cancelled.")
            sessions.pop(chat_id, None)

    async def start_game(chat_id: int):
        s = sessions.get(chat_id)
        if not s:
            return
        if s["mode"] == "rapid":
            await rapid_game(chat_id)
        else:
            await roundwise_game(chat_id)

    async def rapid_game(chat_id: int):
        s = sessions[chat_id]
        s["state"] = "IN_GAME"
        leader = s["joined"][0]
        s["leader"] = leader
        await bot.send_message(chat_id, f"👑 Leader: @{leader[1]}! (Check your DM)")
        await bot.send_message(leader[0], "📝 You are the leader!\nSend `/word yourword` to set the secret word.")

    async def roundwise_game(chat_id: int):
        s = sessions[chat_id]
        s["state"] = "IN_GAME"
        players = s["joined"][:]
        for r in range(1, ROUND_COUNT + 1):
            if len(players) < 3:
                break
            leader = players[0]
            s["leader"] = leader
            await bot.send_message(chat_id, f"🎯 Round {r}\n👑 Leader: @{leader[1]}! (Check your DM)")
            await bot.send_message(leader[0], "📝 You are the leader!\nSend `/word yourword` to set the secret word.")
            players = players[1:] + players[:1]  # rotate leader

        if scores:
            sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            msg = "🏆 Final Leaderboard:\n"
            for i, (uid, score) in enumerate(sorted_scores[:3], start=1):
                uname = next((u[1] for u in s["joined"] if u[0] == uid), uid)
                msg += f"{i}. @{uname} — {score} pts\n"
            if sorted_scores:
                loser = sorted_scores[-1]
                uname = next((u[1] for u in s["joined"] if u[0] == loser[0]), loser[0])
                msg += f"\n🤡 Loser: @{uname} — {loser[1]} pts"
            await bot.send_message(chat_id, msg)

    logger.info("Bot running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(start_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
