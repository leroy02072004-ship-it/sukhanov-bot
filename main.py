import asyncio
import logging
import os
import io
import html
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BufferedInputFile,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── НАСТРОЙКИ ───────────────────────────────────────────────
try:
    import config as _cfg
except Exception:
    _cfg = None

def _cfg_get(name, default=None):
    if _cfg is not None and hasattr(_cfg, name):
        return getattr(_cfg, name)
    return os.getenv(name, default)

BOT_TOKEN = _cfg_get("BOT_TOKEN")
ADMIN_ID = int(_cfg_get("ADMIN_ID", 1017267579))
BOT_USERNAME = _cfg_get("BOT_USERNAME", "Trenajer_suhanova_bot")
# Файлы (лежат рядом с main.py на сервере)
PRESENTATION_FILE = _cfg_get("PRESENTATION_FILE", "presentation.pdf")  # презентация, БЕЗ водяного знака
TRAINER_FILE = _cfg_get("TRAINER_FILE", "trainer.pdf")                 # тренажёр — выдаётся С водяным знаком
BONUS_FILE = _cfg_get("BONUS_FILE", "bonus_120.pdf")                   # подарок «120+ каналов», БЕЗ водяного знака
SUPPORT_FILE = _cfg_get("SUPPORT_FILE", "Презентация_сопровождения.pdf")  # презентация сопровождения (сессия)
# Ссылки
CHANNEL_URL = _cfg_get("CHANNEL_URL", "ЗАГЛУШКА")
REVIEW_FORM_URL = _cfg_get("REVIEW_FORM_URL", "ЗАГЛУШКА")
DM_URL = _cfg_get("DM_URL", "ЗАГЛУШКА")
TRAINERS_PRESENTATION_URL = _cfg_get("TRAINERS_PRESENTATION_URL", "ЗАГЛУШКА")
ARTICLES_URL = _cfg_get("ARTICLES_URL", "ЗАГЛУШКА")
YUKASSA_URL = _cfg_get("YUKASSA_URL", "ЗАГЛУШКА")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Добавь config.py или переменную окружения BOT_TOKEN.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

FREE_HOURS = 48  # окно бесплатного доступа

# ─── БД ──────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                ref_id INTEGER,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                checklist_sent_at TIMESTAMP,
                review_sent BOOLEAN DEFAULT 0,
                bonus_sent BOOLEAN DEFAULT 0,
                stage TEXT DEFAULT 'new'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

async def add_user(user_id: int, username: str, full_name: str, ref_id: int = None):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, ref_id) VALUES (?, ?, ?, ?)",
            (user_id, username, full_name, ref_id),
        )
        await db.commit()

async def set_stage(user_id: int, stage: str):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET stage = ? WHERE user_id = ?", (stage, user_id))
        await db.commit()

async def mark_trainer_opened(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "UPDATE users SET checklist_sent_at = CURRENT_TIMESTAMP, stage = 'trainer_opened' WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()

async def mark_review_sent(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET review_sent = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def mark_bonus_sent(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET bonus_sent = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def add_referral(referrer_id: int, referred_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id),
        )
        await db.commit()

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def get_referrals_list(referrer_id: int):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT u.username, u.full_name, u.user_id "
            "FROM referrals r LEFT JOIN users u ON u.user_id = r.referred_id "
            "WHERE r.referrer_id = ? ORDER BY r.created_at",
            (referrer_id,),
        ) as cur:
            return await cur.fetchall()

# ─── ОТПРАВКА ФАЙЛОВ (с защитой от отсутствия файла) ─────────
async def send_plain_file(chat_id: int, path: str, caption: str = "", reply_markup=None):
    """Отправляет файл без защиты (презентация, подарок — можно пересылать)."""
    if os.path.exists(path):
        await bot.send_document(chat_id, FSInputFile(path), caption=caption or None,
                                reply_markup=reply_markup)
    else:
        await bot.send_message(chat_id, (caption + "\n\n" if caption else "") +
                               f"[ЗАГЛУШКА: файл «{path}» ещё не загружен на сервер]", reply_markup=reply_markup)

async def send_trainer_protected(chat_id: int, user, caption: str = ""):
    """Отправляет тренажёр (без защиты — пересылка, сохранение и скриншоты разрешены)."""
    if not os.path.exists(TRAINER_FILE):
        await bot.send_message(chat_id, (caption + "\n\n" if caption else "") +
                               f"[ЗАГЛУШКА: файл тренажёра «{TRAINER_FILE}» ещё не загружен]")
        return
    await bot.send_document(chat_id, FSInputFile(TRAINER_FILE), caption=caption or None)

# ─── ССЫЛКА (заглушка-безопасно) ─────────────────────────────
def link_or_stub(url: str, name: str) -> str:
    return url if url and url != "ЗАГЛУШКА" else f"[ЗАГЛУШКА: ссылка «{name}» ещё не добавлена]"

# ─── ПЛАНИРОВЩИК ─────────────────────────────────────────────
def safe_remove(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

def schedule_start_dojims(user_id: int):
    base = datetime.now()
    scheduler.add_job(dojim, "date", run_date=base + timedelta(hours=2),  args=[user_id, "2h"],  id=f"dojim2_{user_id}",  replace_existing=True)
    scheduler.add_job(dojim, "date", run_date=base + timedelta(hours=22), args=[user_id, "22h"], id=f"dojim22_{user_id}", replace_existing=True)
    scheduler.add_job(dojim, "date", run_date=base + timedelta(hours=36), args=[user_id, "36h"], id=f"dojim36_{user_id}", replace_existing=True)
    scheduler.add_job(dojim, "date", run_date=base + timedelta(hours=46), args=[user_id, "46h"], id=f"dojim46_{user_id}", replace_existing=True)
    scheduler.add_job(access_closed, "date", run_date=base + timedelta(hours=FREE_HOURS), args=[user_id], id=f"close48_{user_id}", replace_existing=True)

def cancel_start_dojims(user_id: int):
    for jid in (f"dojim2_{user_id}", f"dojim22_{user_id}", f"dojim36_{user_id}", f"dojim46_{user_id}"):
        safe_remove(jid)

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────
def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Пройти тренажёр", callback_data="go_trainer_intro")],
        [InlineKeyboardButton(text="📋 О проекте", callback_data="about_project")],
    ])

def kb_to_trainer():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Пройти тренажёр", callback_data="go_trainer_intro")],
    ])

def kb_open_trainer():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть тренажёр", callback_data="open_trainer")],
    ])

def kb_after_trainer():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Поделиться отзывом", callback_data="share_review")],
        [InlineKeyboardButton(text="📈 Больше подсказок для прибыли", callback_data="more_value")],
    ])

def kb_get_bonus():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Я оставил отзыв, забрать подарок", callback_data="get_bonus")],
    ])

def kb_passed():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="passed_yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="passed_no")],
    ])

def kb_menu5(exclude=None):
    rows = [
        ("📢 Канал пользы + совет", "channel_benefit"),
        ("💼 Индивидуальная сессия", "session"),
        ("🤝 Партнёрство и спец. условия", "partnership"),
        ("📄 Полезные статьи", "articles"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=c)] for t, c in rows if c != exclude
    ])

async def more_value_menu(chat_id: int, exclude: str, intro: str = "Заберите ещё больше пользы!"):
    await bot.send_message(chat_id, intro, reply_markup=kb_menu5(exclude=exclude))

def kb_trainers_list():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Главные поставщики прибыли", callback_data="coming_soon")],
        [InlineKeyboardButton(text="Бизнес-процессы взрывного роста", callback_data="coming_soon")],
        [InlineKeyboardButton(text="Ассортимент максимальной прибыли", callback_data="coming_soon")],
        [InlineKeyboardButton(text="Быстрые деньги", callback_data="coming_soon")],
        [InlineKeyboardButton(text="Системный менеджмент — масштабирование", callback_data="coming_soon")],
    ])

def kb_buy():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить тренажёр", callback_data="buy_trainer")],
    ])

def kb_final_dojim():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ Закрутился, напомни позже", callback_data="remind_later")],
        [InlineKeyboardButton(text="🔔 Сообщать о спец. предложениях", callback_data="notify_offers")],
        [InlineKeyboardButton(text="🚫 Больше не беспокоить", callback_data="dont_disturb")],
    ])

def kb_new_referral():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Посмотреть всех рефералов", callback_data="show_my_referrals")],
        [InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="my_ref_link")],
    ])

# ─── СОСТОЯНИЯ ───────────────────────────────────────────────
class Flow(StatesGroup):
    waiting_audio_question = State()

# ─── /start ──────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    user_id = user.id
    username = user.username or ""
    full_name = user.full_name or ""
    first_name = user.first_name or "друг"

    existing = await get_user(user_id)

    # реферал
    ref_id = None
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            ref_id = int(args[1][3:])
        except ValueError:
            ref_id = None
    if existing is None and ref_id is not None and ref_id != user_id:
        await add_referral(ref_id, user_id)
        ref_name = f"@{username}" if username else (full_name or f"id{user_id}")
        try:
            await bot.send_message(ref_id, f"🎉 У вас новый реферал {ref_name}!", reply_markup=kb_new_referral())
        except Exception:
            pass

    await add_user(user_id, username, full_name, ref_id)

    if existing is None:
        schedule_start_dojims(user_id)

    await message.answer(
        f"{html.escape(first_name)}, приветствую, рад вам! 👋\n\n"
        "Меня зовут АТОМ, я бот-помощник Сергея Суханова и администратор вашего взрывного роста.\n\n"
        "<b>Ценим ваше время, поэтому сразу к делу!</b>\n\n"
        "❗ Важно: для вашей защиты от сомнений, игр разума и отложенных решений прохождение "
        "тренажёра БЕЗ ОПЛАТЫ возможно только в течение 48 часов.\n\n"
        "Выберите, с чего начнём:",
        reply_markup=kb_start(),
        parse_mode="HTML",
    )

# ─── О ПРОЕКТЕ (презентация — в начале, без водяного знака) ──
@dp.callback_query(F.data == "about_project")
async def cb_about_project(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.answer(
        "Супер, люблю системный подход! Здесь можно ознакомиться с полной презентацией проекта "
        "«АТОМНЫЕ БИЗНЕС-ТРЕНАЖЁРЫ»:"
    )
    await send_plain_file(uid, PRESENTATION_FILE, caption="📋 Презентация проекта", reply_markup=kb_to_trainer())
    # «Время быстрой пользы истекает» — через 5 минут, если не нажали «Пройти тренажёр»
    scheduler.add_job(time_pressure, "date", run_date=datetime.now() + timedelta(minutes=5),
                      args=[uid], id=f"about5_{uid}", replace_existing=True)
    await callback.answer()

# ─── ВВОДНАЯ ПЕРЕД ТРЕНАЖЁРОМ ────────────────────────────────
@dp.callback_query(F.data == "go_trainer_intro")
async def cb_go_trainer_intro(callback: CallbackQuery):
    uid = callback.from_user.id
    cancel_start_dojims(uid)
    safe_remove(f"about5_{uid}")
    await callback.message.answer("Отлично, быстрые решения — основа успеха!")
    await callback.message.answer(
        "<b>Совет!</b>\n"
        "✅ Сразу фиксируйте все мысли и инсайты произвольным списком (лучше пройти 2–3 раза).\n"
        "✅ Затем выберите 3 наиболее важных и приоритетных действия и запускайте первый шаг.\n"
        "✅ Не забудьте забрать подарок: «120+ актуальных каналов трафика 2026».",
        reply_markup=kb_open_trainer(),
        parse_mode="HTML",
    )
    await callback.answer()

# ─── ВЫДАЧА ТРЕНАЖЁРА ────────────────────────────────────────
@dp.callback_query(F.data == "open_trainer")
async def cb_open_trainer(callback: CallbackQuery):
    user = callback.from_user
    cancel_start_dojims(user.id)
    await mark_trainer_opened(user.id)
    await callback.message.answer("⚛️ Запускаю тренажёр 👇")
    await send_trainer_protected(user.id, user, caption="🔍 Тренажёр «Золотые каналы трафика»")
    # через 10 минут — запрос отзыва
    scheduler.add_job(ask_passed, "date", run_date=datetime.now() + timedelta(minutes=10),
                      args=[user.id], id=f"review_{user.id}", replace_existing=True)
    await callback.answer()

# ─── ОТЗЫВ ───────────────────────────────────────────────────
@dp.callback_query(F.data == "share_review")
async def cb_share_review(callback: CallbackQuery):
    safe_remove(f"menu1h_{callback.from_user.id}")
    await callback.message.answer(
        "Поделитесь коротким отзывом в произвольной форме по ссылке:\n"
        f"{link_or_stub(REVIEW_FORM_URL, 'анкета отзыва')}\n\n"
        "Как оставите отзыв — вернитесь сюда и нажмите кнопку ниже, и я пришлю ваш подарок 🎁",
        reply_markup=kb_get_bonus(),
    )
    await callback.answer()

@dp.callback_query(F.data == "get_bonus")
async def cb_get_bonus(callback: CallbackQuery):
    uid = callback.from_user.id
    await mark_review_sent(uid)
    await callback.message.answer("Спасибо за отзыв! Забирайте подарок 🎁")
    await send_plain_file(uid, BONUS_FILE, caption="🎁 120+ каналов трафика 2026")
    await mark_bonus_sent(uid)
    await callback.message.answer(
        "Переходите к следующему шагу — у нас ещё много важных подсказок для вашей прибыли 👇",
        reply_markup=kb_menu5(),
    )
    await callback.answer()

@dp.callback_query(F.data == "more_value")
async def cb_more_value(callback: CallbackQuery):
    safe_remove(f"menu1h_{callback.from_user.id}")
    await show_menu5(callback.from_user.id, "🤗 СУПЕР, наш человек)!")
    await callback.answer()

@dp.callback_query(F.data == "passed_yes")
async def cb_passed_yes(callback: CallbackQuery):
    uid = callback.from_user.id
    safe_remove(f"menu1h_{uid}")
    await callback.message.answer(
        "🔥 КЛАСС, хорошая заявка на лидерство!\n\n"
        "❓️Как вам тренажёр? Поделитесь коротким отзывом и забирайте подарок — "
        "чек-лист «120+ актуальных каналов трафика 2026»."
    )
    await callback.message.answer(
        "Переходите к следующему шагу — у нас ещё много важных подсказок для вашей прибыли🎁",
        reply_markup=kb_after_trainer(),
    )
    await callback.answer()

@dp.callback_query(F.data == "passed_no")
async def cb_passed_no(callback: CallbackQuery):
    uid = callback.from_user.id
    safe_remove(f"menu1h_{uid}")
    await callback.message.answer(
        "Не откладывайте — пройдите тренажёр, это всего 5 минут в день. "
        "А дальше вас ждёт ещё много пользы! 🚀",
        reply_markup=kb_open_trainer(),
    )
    await callback.answer()

# ─── МЕНЮ 5 НАПРАВЛЕНИЙ ──────────────────────────────────────
async def show_menu5(chat_id: int, intro: str):
    await bot.send_message(
        chat_id,
        intro + "\n\nПодготовили для вас ещё больше пользы:\n\n"
        "📍 экспертный канал + подарок (индивидуальный аудио-совет до 3 минут, доступно 24 часа)\n"
        "📍 записаться на индивидуальную часовую сессию по теме «Атомный маркетинг» (постоплата)\n"
        "📍 обсудить варианты партнёрства и спец. условий\n"
        "📍 полезные статьи: «ТОП скрытых факапов, которые ежедневно режут трафик и прибыль»",
        reply_markup=kb_menu5(),
    )

def channel_chat_id():
    """Достаёт @username канала из CHANNEL_URL для проверки подписки."""
    url = CHANNEL_URL or ""
    if "t.me/" in url:
        uname = url.rstrip("/").split("t.me/")[-1].split("?")[0]
        if uname and not uname.startswith("+"):
            return "@" + uname
    return None

async def is_subscribed(user_id: int):
    """True — подписан, False — точно не подписан, None — проверить нельзя (бот не админ/приватный канал)."""
    chat = channel_chat_id()
    if not chat:
        return None
    try:
        m = await bot.get_chat_member(chat, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(f"Проверка подписки не удалась: {e}")
        return None

def kb_check_sub():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я подписался, проверить", callback_data="check_sub")],
    ])

async def channel_offer(chat_id: int):
    sub = await is_subscribed(chat_id)
    if sub is False:
        # точно не подписан — просим подписаться
        await bot.send_message(
            chat_id,
            "📢 Подпишитесь на наш экспертный канал пользы, чтобы забрать подарок:\n"
            f"{link_or_stub(CHANNEL_URL, 'канал пользы')}\n\n"
            "После подписки нажмите кнопку ниже 👇",
            reply_markup=kb_check_sub(),
        )
    else:
        # подписан или проверить нельзя — выдаём кодовое слово
        await bot.send_message(
            chat_id,
            "✅ Отлично, видим вашу подписку!\n\n"
            "Чтобы получить индивидуальный аудио-совет (до 3 минут), напишите эксперту "
            "кодовое слово: \"СОВЕТ\" (действует 24 часа)\n"
            f"{link_or_stub(DM_URL, 'эксперт')}"
        )
        await more_value_menu(chat_id, "channel_benefit")

@dp.callback_query(F.data == "channel_benefit")
async def cb_channel_benefit(callback: CallbackQuery):
    await channel_offer(callback.from_user.id)
    await callback.answer()

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    await channel_offer(callback.from_user.id)
    await callback.answer()

@dp.message(Flow.waiting_audio_question)
async def process_audio_question(message: Message, state: FSMContext):
    await state.clear()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🎧 Запрос аудио-совета\nОт: @{message.from_user.username} ({message.from_user.full_name})\n"
            f"ID: {message.from_user.id}\n\nВопрос:\n{message.text}",
        )
    except Exception:
        pass
    await message.answer("Принял ваш вопрос! Сергей подготовит аудио-совет и пришлёт его в течение 24 часов. 🙌")

@dp.callback_query(F.data == "session")
async def cb_session(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.answer(
        "💼 Индивидуальная часовая сессия по теме «Атомный маркетинг». Постоплата!\n\n"
        "Прикладываю презентацию сопровождения 👇"
    )
    await send_plain_file(uid, SUPPORT_FILE, caption="📑 Презентация сопровождения")
    await callback.message.answer(
        "Готовы обсудить детали и записаться? Напишите в службу заботы АТОМНОГО маркетинга\n"
        "@valerisuhanova"
    )
    await more_value_menu(uid, "session", intro="Всю пользу забрали?")
    await callback.answer()

@dp.callback_query(F.data == "partnership")
async def cb_partnership(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.answer(
        "🤝 Партнёрство и спец. условия.\n\n"
        "Напишите Сергею лично, чтобы обсудить детали:\n"
        "@suhanov888"
    )
    await more_value_menu(uid, "partnership")
    await callback.answer()

@dp.callback_query(F.data == "continue_trainers")
async def cb_continue_trainers(callback: CallbackQuery):
    await callback.message.answer(
        "📚 Линейка Атомных бизнес-тренажёров:\n"
        f"{link_or_stub(TRAINERS_PRESENTATION_URL, 'презентация линейки')}\n\n"
        "Выберите следующий тренажёр:",
        reply_markup=kb_trainers_list(),
    )
    await callback.answer()

@dp.callback_query(F.data == "articles")
async def cb_articles(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.message.answer(
        "📄 Полезные статьи Сергея Суханова:\n"
        f"{link_or_stub(ARTICLES_URL, 'статьи')}"
    )
    await more_value_menu(uid, "articles")
    await callback.answer()

@dp.callback_query(F.data == "coming_soon")
async def cb_coming_soon(callback: CallbackQuery):
    await callback.message.answer(
        "Этот тренажёр скоро появится! 🔜\n\n"
        "Хотите узнать о запуске первым — выберите «Сообщать о спец. предложениях» или напишите нам."
    )
    await callback.answer()

# ─── ПОКУПКА (касса — заглушка, позже ЮKassa) ────────────────
@dp.callback_query(F.data == "buy_trainer")
async def cb_buy_trainer(callback: CallbackQuery):
    # TODO: подключить ЮKassa — кнопку с реальной ссылкой оплаты
    await callback.message.answer(
        "💳 Оплата тренажёра.\n"
        f"{link_or_stub(YUKASSA_URL, 'оплата ЮKassa')}\n\n"
        "(Приём оплаты через ЮKassa будет подключён здесь.)"
    )
    await callback.answer()

# ─── ФИНАЛЬНЫЙ ДОЖИМ — КНОПКИ ────────────────────────────────
@dp.callback_query(F.data == "remind_later")
async def cb_remind_later(callback: CallbackQuery):
    await set_stage(callback.from_user.id, "remind_later")
    await callback.message.answer("Хорошо, напомню позже 👍 Если что — просто напишите /start.")
    await callback.answer()

@dp.callback_query(F.data == "notify_offers")
async def cb_notify_offers(callback: CallbackQuery):
    await set_stage(callback.from_user.id, "notify_offers")
    await callback.message.answer("Отлично! Будем сообщать вам о спец. предложениях. 🔔")
    await callback.answer()

@dp.callback_query(F.data == "dont_disturb")
async def cb_dont_disturb(callback: CallbackQuery):
    await set_stage(callback.from_user.id, "dont_disturb")
    safe_remove(f"close48_{callback.from_user.id}")
    await callback.message.answer("Понял, больше не беспокою 👍 Если понадоблюсь — напишите /start.")
    await callback.answer()

# ─── РЕФЕРАЛЬНЫЕ КОМАНДЫ/КНОПКИ ──────────────────────────────
@dp.callback_query(F.data == "show_my_referrals")
async def cb_show_my_referrals(callback: CallbackQuery):
    rows = await get_referrals_list(callback.from_user.id)
    if not rows:
        await callback.message.answer("У вас пока нет рефералов.")
        await callback.answer()
        return
    lines = []
    for username, full_name, uid in rows:
        if username:
            lines.append(f"@{username}")
        elif full_name:
            lines.append(full_name)
        else:
            lines.append(f"id{uid}")
    await callback.message.answer(f"👥 Ваши рефералы ({len(rows)}):\n\n" + "\n".join(lines))
    await callback.answer()

@dp.callback_query(F.data == "my_ref_link")
async def cb_my_ref_link(callback: CallbackQuery):
    user_id = callback.from_user.id
    ref_count = await get_referral_count(user_id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{user_id}"
    await callback.message.answer(f"🔗 Ваша реферальная ссылка:\n{ref_link}\n\nПриглашено друзей: {ref_count} чел.")
    await callback.answer()

@dp.message(Command("mystats"))
async def cmd_mystats(message: Message):
    user_id = message.from_user.id
    ref_count = await get_referral_count(user_id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{user_id}"
    await message.answer(f"📊 Ваша статистика:\n\nПриглашено друзей: {ref_count} чел.\n\nВаша реферальная ссылка:\n{ref_link}")

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE checklist_sent_at IS NOT NULL") as c:
            opened = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE review_sent = 1") as c:
            reviews = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE bonus_sent = 1") as c:
            bonuses = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM referrals") as c:
            refs = (await c.fetchone())[0]
        async with db.execute("SELECT stage, COUNT(*) FROM users GROUP BY stage") as c:
            stages = await c.fetchall()
    stage_names = {
        "new": "Только зашли",
        "trainer_opened": "Открыли тренажёр",
        "remind_later": "Напомнить позже",
        "notify_offers": "Согласны на предложения",
        "dont_disturb": "Не беспокоить",
    }
    stage_lines = "\n".join(f"  • {stage_names.get(s, s)}: {n}" for s, n in stages)
    await message.answer(
        f"📊 Статистика бота:\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"🔍 Открыли тренажёр: {opened}\n"
        f"✍️ Оставили отзыв: {reviews}\n"
        f"🎁 Забрали подарок: {bonuses}\n"
        f"🔗 Рефералов: {refs}\n\n"
        f"Этапы воронки:\n{stage_lines}"
    )

# ─── ОТЛОЖЕННЫЕ СООБЩЕНИЯ (дожимы) ───────────────────────────
DOJIM_TEXTS = {
    "2h":  "Приветствую, это АТОМ, нас, видимо, отвлекли) Готовы получить первую пользу БЕЗ ОПЛАТЫ, всего за 15 минут в день?",
    "22h": "Приветствую, это ваш АТОМ взрывного роста. Остаётся совсем немного времени для активации «ЗОЛОТЫХ» каналов трафика и прибыли БЕЗ ОПЛАТЫ.",
    "36h": "Приветствую, это сигнал, которого вы давно ждали)",
    "46h": "Это финальное напоминание — скоро возможность снова станет упущенной…",
}

async def dojim(user_id: int, key: str):
    try:
        await bot.send_message(user_id, DOJIM_TEXTS[key], reply_markup=kb_to_trainer())
    except Exception as e:
        logger.error(f"Ошибка дожима {key} для {user_id}: {e}")

async def time_pressure(user_id: int):
    try:
        await bot.send_message(user_id, "⏳ Время быстрой пользы истекает.", reply_markup=kb_to_trainer())
    except Exception as e:
        logger.error(f"Ошибка time_pressure для {user_id}: {e}")

async def ask_passed(user_id: int):
    try:
        await bot.send_message(user_id, "Удалось пройти тренажёр?", reply_markup=kb_passed())
        scheduler.add_job(menu_nudge, "date", run_date=datetime.now() + timedelta(hours=1),
                          args=[user_id], id=f"menu1h_{user_id}", replace_existing=True)
    except Exception as e:
        logger.error(f"Ошибка ask_passed для {user_id}: {e}")

async def menu_nudge(user_id: int):
    try:
        await show_menu5(user_id, "Возможно, вас отвлекли)")
    except Exception as e:
        logger.error(f"Ошибка menu_nudge для {user_id}: {e}")

async def access_closed(user_id: int):
    try:
        user = await get_user(user_id)
        if user and user[8] == "dont_disturb":
            return
        await bot.send_message(
            user_id,
            "Бесплатный доступ к тренажёру завершился ⏳\n\n"
            "Если хотите продолжить и забрать всю пользу — можно приобрести тренажёр:",
            reply_markup=kb_buy(),
        )
    except Exception as e:
        logger.error(f"Ошибка access_closed для {user_id}: {e}")

# ─── ЗАПУСК ──────────────────────────────────────────────────
async def main():
    await init_db()
    scheduler.start()
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
