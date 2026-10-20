import asyncio
import logging
import random
import string
import os
from datetime import datetime, time, timezone, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import aiosqlite

API_TOKEN = os.getenv("BOT_TOKEN")

DB_FILE = "social_rating.db"
TIMEZONE_KYIV = timezone(timedelta(hours=2))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- БАЗА ДАНИХ ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                pb REAL DEFAULT 0.0001,
                prb_given_to INTEGER,
                nrb_given_to INTEGER,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_bot INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invited_keys (
                secret_key TEXT PRIMARY KEY,
                created_by INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_subscribers (
                telegram_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('treasury', 1000000.0)")
        await db.commit()

async def get_treasury():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT value FROM system_state WHERE key = 'treasury'") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1000000.0

async def update_treasury(amount):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE system_state SET value = value + ? WHERE key = 'treasury'", (amount,))
        await db.commit()

async def get_user_stats(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT pb, username, telegram_id, is_bot FROM users WHERE telegram_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
            if not user:
                return None
            
        async with db.execute("SELECT COUNT(*) FROM users WHERE prb_given_to = ?", (user_id,)) as cursor:
            prb_received = (await cursor.fetchone())[0]
            
        async with db.execute("SELECT COUNT(*) FROM users WHERE nrb_given_to = ?", (user_id,)) as cursor:
            nrb_received = (await cursor.fetchone())[0]
            
        return {
            "pb": user[0],
            "username": user[1],
            "telegram_id": user[2],
            "is_bot": user[3],
            "prb_received": prb_received,
            "nrb_received": nrb_received
        }

# --- КЛАВІАТУРИ ---
def main_menu_kb(is_subscribed=False):
    broadcast_btn = InlineKeyboardButton(text="🔕 Вимкнути трансляцію ботів", callback_data="toggle_broadcast") if is_subscribed else InlineKeyboardButton(text="📡 Увімкнути трансляцію дій ботів", callback_data="toggle_broadcast")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Мій профіль та рейтинги", callback_data="profile")],
        [InlineKeyboardButton(text="📈 Статистика системи", callback_data="system_stats")],
        [InlineKeyboardButton(text="💸 Переказати ПБ", callback_data="transfer_pb")],
        [InlineKeyboardButton(text="🤝 Керування ПРБ / НРБ", callback_data="manage_reputation")],
        [InlineKeyboardButton(text="🎟 Згенерувати запрошення", callback_data="create_invite")],
        [broadcast_btn]
    ])

class TransferState(StatesGroup):
    waiting_for_target = State()
    waiting_for_amount = State()

class RepState(StatesGroup):
    waiting_for_target = State()

# --- ХЕНДЛЕРИ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT id FROM users WHERE telegram_id = ?", (message.from_user.id,)) as cursor:
            user = await cursor.fetchone()
            if not user:
                await db.execute(
                    "INSERT INTO users (telegram_id, username, pb) VALUES (?, ?, ?)",
                    (message.from_user.id, message.from_user.username or f"user_{message.from_user.id}", 0.0001)
                )
                await db.commit()
                
        async with db.execute("SELECT telegram_id FROM broadcast_subscribers WHERE telegram_id = ?", (message.from_user.id,)) as cursor:
            is_sub = await cursor.fetchone() is not None

    await message.answer(
        "Вітаємо у соціальній економічній системі ПРБ/НРБ/ПБ!\n"
        "Використовуйте меню нижче для взаємодії:",
        reply_markup=main_menu_kb(is_sub)
    )

@dp.callback_query(F.data == "profile")
async def profile_callback(callback: types.CallbackQuery):
    stats = await get_user_stats(callback.from_user.id)
    if not stats:
        await callback.answer("Акаунт не знайдено.", show_alert=True)
        return
    
    treasury = await get_treasury()
    text = (
        f"👤 **Профіль:** @{stats['username']}\n"
        f"💰 **Платівний бал (ПБ):** {stats['pb']:.4f}\n"
        f"🌟 **Отримано ПРБ:** {stats['prb_received']}\n"
        f"⚠️ **Отримано НРБ:** {stats['nrb_received']}\n\n"
        f"🏛 **Казна системи (емісія):** {treasury:.4f} ПБ"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "system_stats")
async def system_stats_callback(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_bot = 1") as cursor:
            bots_count = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_bot = 0") as cursor:
            users_count = (await cursor.fetchone())[0]
        async with db.execute("SELECT SUM(pb) FROM users") as cursor:
            total_pb_in_circulation = (await cursor.fetchone())[0] or 0.0
            
        async with db.execute("SELECT username, pb FROM users ORDER BY pb DESC LIMIT 3") as cursor:
            top_rich = await cursor.fetchall()
            
    treasury = await get_treasury()
    top_str = "\n".join([f"  • @{u[0]}: {u[1]:.4f} ПБ" for u in top_rich])
    
    text = (
        f"🌐 **Глобальна статистика системи:**\n\n"
        f"👥 **Реальних користувачів:** {users_count}\n"
        f"🤖 **Активних ботів-симуляторів:** {bots_count}\n"
        f"💸 **ПБ в обігу у гравців:** {total_pb_in_circulation:.4f} ПБ\n"
        f"🏛 **Всього у казні:** {treasury:.4f} ПБ\n\n"
        f"🏆 **ТОП-3 за балансом ПБ:**\n{top_str}"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "toggle_broadcast")
async def toggle_broadcast_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT telegram_id FROM broadcast_subscribers WHERE telegram_id = ?", (user_id,)) as cursor:
            exists = await cursor.fetchone()
            
        if exists:
            await db.execute("DELETE FROM broadcast_subscribers WHERE telegram_id = ?", (user_id,))
            is_sub = False
            msg = "🔕 Трансляцію дій ботів вимкнено."
        else:
            await db.execute("INSERT INTO broadcast_subscribers (telegram_id) VALUES (?)", (user_id,))
            is_sub = True
            msg = "📡 Трансляцію дій ботів увімкнено! Кожні 15 секунд ви отримуватимете звіт у чат."
            
        await db.commit()
        
    await callback.answer(msg, show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=main_menu_kb(is_sub))

@dp.callback_query(F.data == "transfer_pb")
async def transfer_start(callback: types.CallbackQuery, state: FSMContext):
    now = datetime.now(TIMEZONE_KYIV).time()
    if time(12, 0) <= now <= time(12, 5):
        await callback.answer("⏳ З 12:00 до 12:05 за київським часом перекази технічно заборонені!", show_alert=True)
        return
        
    await callback.message.answer("Введіть Telegram ID або username отримувача ПБ:")
    await state.set_state(TransferState.waiting_for_target)
    await callback.answer()

@dp.message(TransferState.waiting_for_target)
async def transfer_target_received(message: types.Message, state: FSMContext):
    target_str = message.text.strip().replace("@", "")
    async with aiosqlite.connect(DB_FILE) as db:
        if target_str.isdigit():
            async with db.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (int(target_str),)) as cursor:
                target = await cursor.fetchone()
        else:
            async with db.execute("SELECT telegram_id FROM users WHERE username = ?", (target_str,)) as cursor:
                target = await cursor.fetchone()
                
    if not target:
        await message.answer("Користувача не знайдено в системі.")
        return
        
    if target[0] == message.from_user.id:
        await message.answer("Ви не можете переказувати кошти самому собі.")
        return
        
    await state.update_data(target_id=target[0])
    await message.answer("Введіть суму переказу (мінімум 0.0001 ПБ):")
    await state.set_state(TransferState.waiting_for_amount)

@dp.message(TransferState.waiting_for_amount)
async def transfer_amount_received(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Будь ласка, введіть коректне число.")
        return
        
    if amount < 0.0001:
        await message.answer("Мінімальна сума переказу — 0.0001 ПБ.")
        return
        
    data = await state.get_data()
    sender_id = message.from_user.id
    target_id = data["target_id"]
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT pb FROM users WHERE telegram_id = ?", (sender_id,)) as cursor:
            sender = await cursor.fetchone()
            
        if not sender or sender[0] - amount < 0.0001:
            await message.answer("Недостатньо коштів! Мін. залишок на балансі має становити 0.0001 ПБ.")
            await state.clear()
            return
            
        await db.execute("UPDATE users SET pb = pb - ? WHERE telegram_id = ?", (amount, sender_id))
        await db.execute("UPDATE users SET pb = pb + ? WHERE telegram_id = ?", (amount, target_id))
        await db.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE telegram_id = ?", (sender_id,))
        await db.commit()
        
    await message.answer(f"✅ Успішно переказано {amount} ПБ!", reply_markup=main_menu_kb())
    await state.clear()

@dp.callback_query(F.data == "manage_reputation")
async def manage_rep(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌟 Віддати ПРБ", callback_data="give_prb"), InlineKeyboardButton(text="🚫 Віддати НРБ", callback_data="give_nrb")],
        [InlineKeyboardButton(text="↩️ Відкликати репутацію", callback_data="revoke_rep")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="profile")]
    ])
    stats = await get_user_stats(callback.from_user.id)
    await callback.message.edit_text(
        f"Ваш поточний статус репутації:\n"
        f"Ви віддали ПРБ користувачу з ID: {stats.get('prb_given_to', 'Нікому')}\n"
        f"Ви віддали НРБ користувачу з ID: {stats.get('nrb_given_to', 'Нікому')}",
        reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(F.data.in_({"give_prb", "give_nrb", "revoke_rep"}))
async def handle_rep_action(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data
    if action == "revoke_rep":
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE users SET prb_given_to = NULL, nrb_given_to = NULL WHERE telegram_id = ?", (callback.from_user.id,))
            await db.commit()
        await callback.answer("Репутацію успішно відкликано!", show_alert=True)
        return
        
    rep_type = "ПРБ" if action == "give_prb" else "НРБ"
    await state.update_data(rep_type=rep_type)
    await callback.message.answer(f"Введіть ID або username користувача, якому хочете передати свій {rep_type}:")
    await state.set_state(RepState.waiting_for_target)
    await callback.answer()

@dp.message(RepState.waiting_for_target)
async def rep_target_received(message: types.Message, state: FSMContext):
    target_str = message.text.strip().replace("@", "")
    async with aiosqlite.connect(DB_FILE) as db:
        if target_str.isdigit():
            async with db.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (int(target_str),)) as cursor:
                target = await cursor.fetchone()
        else:
            async with db.execute("SELECT telegram_id FROM users WHERE username = ?", (target_str,)) as cursor:
                target = await cursor.fetchone()
                
    if not target:
        await message.answer("Користувача не знайдено.")
        return
        
    target_id = target[0]
    data = await state.get_data()
    rep_type = data["rep_type"]
    
    field = "prb_given_to" if rep_type == "ПРБ" else "nrb_given_to"
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (target_id, message.from_user.id))
        await db.commit()
        
    await message.answer(f"✅ Ви успішно передали свій {rep_type} користувачу!", reply_markup=main_menu_kb())
    await state.clear()

@dp.callback_query(F.data == "create_invite")
async def create_invite_handler(callback: types.CallbackQuery):
    stats = await get_user_stats(callback.from_user.id)
    if stats["prb_received"] < 100:
        await callback.answer("❌ Запрошувати нових учасників можуть лише ті, хто набрав 100+ ПРБ!", show_alert=True)
        return
        
    secret_key = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO invited_keys (secret_key, created_by) VALUES (?, ?)", (secret_key, callback.from_user.id))
        await db.commit()
        
    await callback.message.edit_text(
        f"🎟 Створено новий аккаунт-запрошення!\n"
        f"Пароль/ключ: `{secret_key}`\n"
        f"Передайте цей ключ новому учаснику.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )

# --- ФОНОВІ ЗАВДАННЯ ---
async def daily_system_task():
    while True:
        try:
            now = datetime.now(TIMEZONE_KYIV)
            target_time = time(12, 0)
            target_datetime = datetime.combine(now.date(), target_time, tzinfo=TIMEZONE_KYIV)
            if now >= target_datetime:
                target_datetime += timedelta(days=1)
                
            wait_seconds = (target_datetime - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            
            async with aiosqlite.connect(DB_FILE) as db:
                treasury = await get_treasury()
                async with db.execute("""
                    SELECT u.telegram_id, (SELECT COUNT(*) FROM users WHERE prb_given_to = u.telegram_id) as prb_count
                    FROM users u HAVING prb_count >= 100
                """) as cursor:
                    lucky_users = await cursor.fetchall()
                    
                for user in lucky_users:
                    if treasury > 0:
                        await db.execute("UPDATE users SET pb = pb + 1.0 WHERE telegram_id = ?", (user[0],))
                        await update_treasury(-1.0)
                        treasury -= 1.0
                await db.commit()
                
            await asyncio.sleep(1)
            
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("""
                    SELECT u.telegram_id, (SELECT COUNT(*) FROM users WHERE nrb_given_to = u.telegram_id) as nrb_count
                    FROM users u HAVING nrb_count >= 100
                """) as cursor:
                    bad_users = await cursor.fetchall()
                    
                for user in bad_users:
                    async with db.execute("SELECT pb FROM users WHERE telegram_id = ?", (user[0],)) as cursor_pb:
                        pb_val = (await cursor_pb.fetchone())[0]
                    if pb_val >= 1.0:
                        await db.execute("UPDATE users SET pb = pb - 1.0 WHERE telegram_id = ?", (user[0],))
                        await update_treasury(1.0)
                await db.commit()
                
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Помилка у daily_system_task: {e}")
            await asyncio.sleep(5)

async def init_simulation_bots():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_bot = 1") as cursor:
            count = (await cursor.fetchone())[0]
            
        if count < 100:
            for i in range(100 - count):
                bot_id = 9000000 + i
                await db.execute(
                    "INSERT OR IGNORE INTO users (telegram_id, username, pb, is_bot) VALUES (?, ?, ?, 1)",
                    (bot_id, f"bot_player_{i}", 1.0)
                )
            await db.commit()
            logging.info("100 симуляційних ботів успішно створено в БД!")

async def broadcast_to_subscribers(text):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT telegram_id FROM broadcast_subscribers") as cursor:
            subscribers = [row[0] for row in await cursor.fetchall()]
            
    for sub_id in subscribers:
        try:
            await bot.send_message(sub_id, text, parse_mode="Markdown")
        except Exception:
            pass

async def bots_activity_loop():
    await init_simulation_bots()
    while True:
        try:
            await asyncio.sleep(15)
            
            now = datetime.now(TIMEZONE_KYIV).time()
            if time(12, 0) <= now <= time(12, 5):
                continue
                
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT telegram_id, username, pb FROM users WHERE is_bot = 1") as cursor:
                    bots = await cursor.fetchall()
                    
                if len(bots) < 2:
                    continue
                    
                sender = random.choice(bots)
                target = random.choice([b for b in bots if b[0] != sender[0]])
                
                action_type = random.choice(["transfer", "prb", "nrb"])
                log_msg = ""
                
                if action_type == "transfer":
                    if sender[2] > 0.0002:
                        amount = round(random.uniform(0.0001, 0.005), 4)
                        new_sender_pb = sender[2] - amount
                        new_target_pb = target[2] + amount
                        
                        await db.execute("UPDATE users SET pb = ? WHERE telegram_id = ?", (new_sender_pb, sender[0]))
                        await db.execute("UPDATE users SET pb = ? WHERE telegram_id = ?", (new_target_pb, target[0]))
                        await db.commit()
                        
                        log_msg = (
                            f"🤖 **Дія ботів:**\n"
                            f"💸 @{sender[1]} переказав {amount:.4f} ПБ ➡️ @{target[1]}\n"
                            f"📊 Нові баланси:\n"
                            f"• @{sender[1]}: {new_sender_pb:.4f} ПБ\n"
                            f"• @{target[1]}: {new_target_pb:.4f} ПБ"
                        )
                elif action_type == "prb":
                    await db.execute("UPDATE users SET prb_given_to = ? WHERE telegram_id = ?", (target[0], sender[0]))
                    await db.commit()
                    log_msg = f"🌟 **Дія ботів:** @{sender[1]} віддав свій **ПРБ** ➡️ @{target[1]}"
                elif action_type == "nrb":
                    await db.execute("UPDATE users SET nrb_given_to = ? WHERE telegram_id = ?", (target[0], sender[0]))
                    await db.commit()
                    log_msg = f"🚫 **Дія ботів:** @{sender[1]} віддав свій **НРБ** ➡️ @{target[1]}"
                    
                if log_msg:
                    await broadcast_to_subscribers(log_msg)
        except Exception as e:
            logging.error(f"Помилка у bots_activity_loop: {e}")
            await asyncio.sleep(5)

async def main():
    await init_db()
    asyncio.create_task(daily_system_task())
    asyncio.create_task(bots_activity_loop())
    
    logging.info("Бот запущено успішно!")
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as err:
            logging.error(f"Глобальний збій бота: {err}. Перезапуск за 3 секунди...")
            import time
            time.sleep(3)
