import asyncio
import logging
import os
import io
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
    from config import (
        BOT_TOKEN, ADMIN_ID, BOT_USERNAME,
        PRESENTATION_FILE, TRAINER_FILE, BONUS_FILE,
        CHANNEL_URL, REVIEW_FORM_URL, DM_URL,
        TRAINERS_PRESENTATION_URL, ARTICLES_URL, YUKASSA_URL,
    )
except ImportError:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = int(os.getenv("ADMIN_ID", "1017267579"))
    BOT_USERNAME = os.getenv("BOT_USERNAME", "Trenajer_suhanova_bot")
    # Файлы (лежат рядом с main.py на сервере)
    PRESENTATION_FILE = os.getenv("PRESENTATION_FILE", "presentation.pdf")  # презентация о тренажёре, БЕЗ водяного знака
    TRAINER_FILE = os.getenv("TRAINER_FILE", "trainer.pdf")                 # сам тренажёр — выдаётся С водяным знаком
    BONUS_FILE = os.getenv("BONUS_FILE", "bonus_120.pdf")                   # подарок «120+ каналов», БЕЗ водяного знака
    # Ссылки-заглушки (подставить в config.py)
    CHANNEL_URL = os.getenv("CHANNEL_URL", "ЗАГЛУШКА")
    REVIEW_FORM_URL = os.getenv("REVIEW_FORM_URL", "ЗАГЛУШКА")
    DM_URL = os.getenv("DM_URL", "ЗАГЛУШКА")
    TRAINERS_PRESENTATION_URL = os.getenv("TRAINERS_PRESENTATION_URL", "ЗАГЛУШКА")
    ARTICLES_URL = os.getenv("ARTICLES_URL", "ЗАГЛУШКА")
    YUKASSA_URL = os.getenv("YUKASSA_URL", "ЗАГЛУШКА")

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

# ─── ВОДЯНОЙ ЗНАК ────────────────────────────────────────────
def make_watermarked_pdf(src_path: str, label: str) -> bytes:
    """Накладывает именной водяной знак на каждую страницу PDF и возвращает байты."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    try:
        pdfmetrics.registerFont(TTFont("WMFont", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        font_name = "WMFont"
    except Exception:
        font_name = "Helvetica"

    reader = PdfReader(src_path)
    writer = PdfWriter()
    for page in reader.pages:
        W = float(page.mediabox.width)
        H = float(page.mediabox.height)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(W, H))
        c.setFont(font_name, 20)
        c.setFillColorRGB(1, 1, 1)
        c.setFillAlpha(0.16)
        step = 900
        y = 60
        while y < H:
            c.saveState()
            c.translate(W / 2, y)
            c.rotate(20)
            c.drawCentredString(0, 0, label)
            c.restoreState()
            y += step
        c.save()
        buf.seek(0)
        wm = PdfReader(buf).pages[0]
        page.merge_page(wm)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

# ─── ОТПРАВКА ФАЙЛОВ (с защитой от отсутствия файла) ─────────
async def send_plain_file(chat_id: int, path: str, caption: str = ""):
    """Отправляет файл без водяного знака (презентация, подарок)."""
    if os.path.exists(path):
        await bot.send_document(chat_id, FSInputFile(path), caption=caption or None)
    else:
        await bot.send_message(chat_id, (caption + "\n\n" if caption else "") +
                               f"[ЗАГЛУШКА: файл «{path}» ещё не загружен на сервер]")

async def send_trainer_protected(chat_id: int, user, caption: str = ""):
    """Отправляет тренажёр с именным водяным знаком и защитой от пересылки."""
    if not os.path.exists(TRAINER_FILE):
        await bot.send_message(chat_id, (caption + "\n\n" if caption else "") +
                               f"[ЗАГЛУШКА: файл тренажёра «{TRAINER_FILE}» ещё не загружен]")
        return
    uname = f"@{user.username}" if user.username else (user.full_name or "")
    label = f"{user.full_name} · {uname} · ID {user.id} · {datetime.now():%d.%m.%Y}"
    try:
        data = make_watermarked_pdf(TRAINER_FILE, label)
        await bot.send_document(
            chat_id,
            BufferedInputFile(data, filename="trainer.pdf"),
            caption=caption or None,
            protect_content=True,  # запрет пересылки и сохранения
        )
    except Exception as e:
        logger.error(f"Ошибка выдачи тренажёра {chat_id}: {e}")
        await bot.send_message(chat_id, "Не удалось сформировать тренажёр, мы уже разбираемся. Напишите /start ещё раз чуть позже.")

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

def kb_menu5():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Канал пользы", callback_data="channel_benefit")],
        [InlineKeyboardButton(text="💼 Индивидуальная сессия", callback_data="session")],
        [InlineKeyboardButton(text="📚 Продолжить тренажёры", callback_data="continue_trainers")],
        [InlineKeyboardButton(text="🤝 Партнёрство и спец. условия", callback_data="partnership")],
        [InlineKeyboardButton(text="📄 Полезные статьи", callback_data="articles")],
    ])

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
        f"{first_name}, приветствую, рад вам! 👋\n\n"
        "Меня зовут АТОМ, я бот-помощник Сергея Суханова и администратор вашего взрывного роста. "
        "Ценим ваше время, поэтому сразу к делу!\n\n"
        "❗ Важно: для вашей защиты от сомнений, игр разума и отложенных решений прохождение "
        "тренажёра БЕЗ ОПЛАТЫ возможно только в течение 48 часов.\n\n"
        "Выберите, с чего начнём:",
        reply_markup=kb_start(),
    )

# ─── О ПРОЕКТЕ (презентация — в начале, без водяного знака) ──
@dp.callback_query(F.data == "about_project")
async def cb_about_project(callback: CallbackQuery):
    await callback.message.answer(
        "Супер, люблю системный подход! Здесь можно ознакомиться с полной презентацией проекта "
        "«АТОМНЫЕ БИЗНЕС-ТРЕНАЖЁРЫ»:"
    )
    await send_plain_file(callback.from_user.id, PRESENTATION_FILE, caption="📋 Презентация проекта")
    await callback.message.answer(
        "⏳ Время быстрой пользы истекает.",
        reply_markup=kb_to_trainer(),
    )
    await callback.answer()

# ─── ВВОДНАЯ ПЕРЕД ТРЕНАЖЁРОМ ────────────────────────────────
@dp.callback_query(F.data == "go_trainer_intro")
async def cb_go_trainer_intro(callback: CallbackQuery):
    cancel_start_dojims(callback.from_user.id)
    await callback.message.answer(
        "Отлично, быстрые решения — основа успеха!\n\n"
        "Этот тренажёр поможет вам, отвечая на триггерные вопросы, всего за 15 минут в день:\n\n"
        "• активировать упущенные каналы трафика и прибыли\n"
        "• сократить расходы ресурсов и нецелевые затраты\n"
        "• вернуть «недошедших», но уже плативших\n"
        "• оставить только ключевые действия\n"
        "• увеличить КПД инвестиций в рекламу\n"
        "• забрать лучший опыт рынка и потенциальных партнёров из смежных отраслей\n\n"
        "Совет:\n"
        "• Сразу фиксируйте все мысли и инсайты произвольным списком (лучше пройти 2–3 раза).\n"
        "• Затем выберите 3 наиболее важных и приоритетных действия и запускайте первый шаг.\n"
        "• Не забудьте забрать подарок: «120+ актуальных каналов трафика 2026».",
        reply_markup=kb_open_trainer(),
    )
    await callback.answer()

# ─── ВЫДАЧА ТРЕНАЖЁРА ────────────────────────────────────────
@dp.callback_query(F.data == "open_trainer")
async def cb_open_trainer(callback: CallbackQuery):
    user = callback.from_user
    cancel_start_dojims(user.id)
    await mark_trainer_opened(user.id)
    await callback.message.answer("Запускаю тренажёр. Файл защищён от пересылки — открывайте прямо здесь 👇")
    await send_trainer_protected(user.id, user, caption="🔍 Тренажёр «Золотые каналы трафика»")
    # через 10 минут — запрос отзыва
    scheduler.add_job(review_prompt, "date", run_date=datetime.now() + timedelta(minutes=10),
                      args=[user.id], id=f"review_{user.id}", replace_existing=True)
    await callback.answer()

# ─── ОТЗЫВ ───────────────────────────────────────────────────
@dp.callback_query(F.data == "share_review")
async def cb_share_review(callback: CallbackQuery):
    safe_remove(f"menu1h_{callback.from_user.id}")
    await mark_review_sent(callback.from_user.id)
    await callback.message.answer(
        "Поделитесь коротким отзывом в произвольной форме по ссылке:\n"
        f"{link_or_stub(REVIEW_FORM_URL, 'анкета отзыва')}\n\n"
        "А вот ваш подарок — чек-лист «120+ актуальных каналов трафика 2026» 🎁"
    )
    await send_plain_file(callback.from_user.id, BONUS_FILE, caption="🎁 120+ каналов трафика 2026")
    await mark_bonus_sent(callback.from_user.id)
    await callback.message.answer(
        "Переходите к следующему шагу — у нас ещё много важных подсказок для вашей прибыли 👇",
        reply_markup=kb_menu5(),
    )
    await callback.answer()

@dp.callback_query(F.data == "more_value")
async def cb_more_value(callback: CallbackQuery):
    safe_remove(f"menu1h_{callback.from_user.id}")
    await show_menu5(callback.from_user.id, "Супер, наш человек!\n\nВот варианты дальнейшего взаимодействия:")
    await callback.answer()

# ─── МЕНЮ 5 НАПРАВЛЕНИЙ ──────────────────────────────────────
async def show_menu5(chat_id: int, intro: str):
    await bot.send_message(
        chat_id,
        intro + "\n\n"
        "• больше пользы БЕЗ ОПЛАТЫ — экспертный канал + подарок (индивидуальный аудио-совет до 3 минут, доступно 24 часа)\n"
        "• записаться на индивидуальную часовую сессию по теме «Атомный маркетинг» (постоплата)\n"
        "• продолжить прохождение Атомных бизнес-тренажёров (платно)\n"
        "• обсудить варианты партнёрства и спец. условий\n"
        "• полезные статьи: «ТОП скрытых факапов, которые ежедневно режут трафик и прибыль»",
        reply_markup=kb_menu5(),
    )

@dp.callback_query(F.data == "channel_benefit")
async def cb_channel_benefit(callback: CallbackQuery, state: FSMContext):
    # TODO: при наличии канала и прав админа — проверять подписку через getChatMember
    await callback.message.answer(
        "📢 Наш экспертный канал пользы:\n"
        f"{link_or_stub(CHANNEL_URL, 'канал пользы')}\n\n"
        "Супер, рады, что вы с нами! Для получения индивидуального аудио-совета напишите свой вопрос одним сообщением. "
        "Срок действия предложения — 24 часа!"
    )
    await state.set_state(Flow.waiting_audio_question)
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
    await callback.message.answer(
        "💼 Индивидуальная часовая сессия по теме «Атомный маркетинг» (постоплата).\n\n"
        "Напишите нам — пришлём анкету и презентацию сопровождения:\n"
        f"{link_or_stub(DM_URL, 'личка')}"
    )
    await callback.answer()

@dp.callback_query(F.data == "partnership")
async def cb_partnership(callback: CallbackQuery):
    await callback.message.answer(
        "🤝 Партнёрство и спец. условия.\n\n"
        "Напишите нам, обсудим варианты сотрудничества:\n"
        f"{link_or_stub(DM_URL, 'личка')}"
    )
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
    # TODO: доработать подветку статей по общему алгоритму
    await callback.message.answer(
        "📄 Полезные статьи Сергея Суханова:\n"
        f"{link_or_stub(ARTICLES_URL, 'статьи')}"
    )
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
        async with db.execute("SELECT COUNT(*) FROM referrals") as c:
            refs = (await c.fetchone())[0]
    await message.answer(
        f"📊 Статистика бота:\n\n👥 Всего пользователей: {total}\n"
        f"🔍 Открыли тренажёр: {opened}\n✍️ Оставили отзыв: {reviews}\n🔗 Рефералов: {refs}"
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

async def review_prompt(user_id: int):
    try:
        await bot.send_message(
            user_id,
            "КЛАСС, хорошая заявка на лидерство в рынке)!\n\n"
            "Как вам тренажёр? Поделитесь коротким отзывом и забирайте подарок — "
            "чек-лист «120+ актуальных каналов трафика 2026».\n\n"
            "Переходите к следующему шагу — у нас ещё много важных подсказок для вашей прибыли.",
            reply_markup=kb_after_trainer(),
        )
        scheduler.add_job(menu_nudge, "date", run_date=datetime.now() + timedelta(hours=1),
                          args=[user_id], id=f"menu1h_{user_id}", replace_existing=True)
    except Exception as e:
        logger.error(f"Ошибка review_prompt для {user_id}: {e}")

async def menu_nudge(user_id: int):
    try:
        await show_menu5(user_id, "Возможно, вас отвлекли) Вот варианты дальнейшего взаимодействия:")
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
