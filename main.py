import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── НАСТРОЙКИ ───────────────────────────────────────────────
try:
    from config import BOT_TOKEN, ADMIN_ID, CHECKLIST_URL, BONUS_CHECKLIST_URL, PRESENTATION_URL, CHANNEL_URL
except ImportError:
    BOT_TOKEN = "ВСТАВЬ_ТОКЕН_СЮДА"
    ADMIN_ID = 1017267579
    CHECKLIST_URL = "https://example.com/checklist.pdf"
    BONUS_CHECKLIST_URL = "https://example.com/bonus.pdf"
    PRESENTATION_URL = "https://example.com/presentation.pdf"
    CHANNEL_URL = "https://t.me/atomicprofit"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

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
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def add_user(user_id: int, username: str, full_name: str, ref_id: int = None):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, full_name, ref_id)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, full_name, ref_id))
        await db.commit()

async def set_checklist_sent(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "UPDATE users SET checklist_sent_at = CURRENT_TIMESTAMP, stage = 'checklist_sent' WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

async def mark_review_sent(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET review_sent = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def mark_bonus_sent(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET bonus_sent = 1, stage = 'bonus_received' WHERE user_id = ?", (user_id,))
        await db.commit()

async def add_referral(referrer_id: int, referred_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id)
        )
        await db.commit()

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

# ─── КЛАВИАТУРЫ ──────────────────────────────────────────────
def kb_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Пройти тренажёр — бесплатно", callback_data="start_trainer")],
        [InlineKeyboardButton(text="📋 Что это такое — презентация", callback_data="show_presentation")],
    ])

def kb_after_ogo():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать тренажёр", callback_data="get_checklist")],
    ])

def kb_after_checklist():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Отправить отзыв и получить подарок 🎁", callback_data="send_review")],
        [InlineKeyboardButton(text="📢 Вступить в канал", url=CHANNEL_URL)],
    ])

def kb_presentation():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Показать все тренажёры", callback_data="show_all_trainers")],
        [InlineKeyboardButton(text="🔍 Пройти тренажёр сейчас", callback_data="start_trainer")],
        [InlineKeyboardButton(text="🤝 Узнать про сопровождение", callback_data="show_accompaniment")],
    ])

def kb_all_trainers():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Пройти тренажёр №1 — бесплатно", callback_data="start_trainer")],
        [InlineKeyboardButton(text="💰 Тренажёр №2 — Система продаж (скоро)", callback_data="coming_soon")],
        [InlineKeyboardButton(text="👥 Тренажёр №3 — Команда (скоро)", callback_data="coming_soon")],
        [InlineKeyboardButton(text="📊 Тренажёр №4 — Финансы (скоро)", callback_data="coming_soon")],
    ])

def kb_review():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_review")],
    ])

def kb_after_review():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Получить чек-лист 120 каналов", callback_data="get_bonus")],
    ])

def kb_day3_reminder():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Открыть чек-лист", callback_data="resend_checklist")],
        [InlineKeyboardButton(text="✍️ Отправить отзыв и получить подарок 🎁", callback_data="send_review")],
    ])

def kb_day5_dojim():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Узнать про индивидуальную сессию", callback_data="show_session")],
        [InlineKeyboardButton(text="📚 Купить следующий тренажёр", callback_data="buy_next_trainer")],
    ])

def kb_session():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Записаться на сессию", callback_data="book_session")],
        [InlineKeyboardButton(text="❓ Подробнее", callback_data="session_details")],
    ])

def kb_warmup_day3():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Узнать про индивидуальную сессию", callback_data="show_session")],
    ])

def kb_warmup_day7():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Записаться на сессию — 30 000 ₽", callback_data="book_session")],
        [InlineKeyboardButton(text="❓ Подробнее", callback_data="session_details")],
    ])

def kb_ref_share(user_id: int):
    ref_link = f"https://t.me/Trenajer_suhanova_bot?start=ref{user_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться своей ссылкой", url=f"https://t.me/share/url?url={ref_link}&text=Пройди бесплатный бизнес-тренажёр — найди скрытые потери в своём бизнесе")],
        [InlineKeyboardButton(text="🔗 Моя реферальная ссылка", callback_data="my_ref_link")],
    ])

def kb_reactivation_30():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Прислать разбор", callback_data="send_breakdown")],
        [InlineKeyboardButton(text="❌ Не актуально", callback_data="not_relevant")],
    ])

# ─── СОСТОЯНИЯ ───────────────────────────────────────────────
class ReviewStates(StatesGroup):
    waiting_review = State()

# ─── ХЭНДЛЕРЫ ────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.full_name or ""

    ref_id = None
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            ref_id = int(args[1][3:])
            if ref_id != user_id:
                await add_referral(ref_id, user_id)
                ref_count = await get_referral_count(ref_id)
                try:
                    await bot.send_message(
                        ref_id,
                        f"🎉 По твоей ссылке только что зарегистрировался новый пользователь!\n\n"
                        f"Всего приглашено: {ref_count} чел."
                    )
                except Exception:
                    pass
        except ValueError:
            pass

    await add_user(user_id, username, full_name, ref_id)
    await message.answer(
        f"Привет, {full_name}! 👋\n\n"
        "Это бот Сергея Суханова — «Атомный бизнес-тренажёр».\n\n"
        "Выбери, с чего начнём:",
        reply_markup=kb_start()
    )

@dp.callback_query(F.data == "start_trainer")
async def cb_start_trainer(callback: CallbackQuery):
    await callback.message.answer(
        "Перед тем как начать — факт, который удивляет большинство предпринимателей:\n\n"
        "На рынке существует более 120 рабочих каналов привлечения клиентов.\n"
        "Большинство бизнесов используют 2–3.\n\n"
        "Это значит: ты работаешь на 2–3% от доступных возможностей.\n\n"
        "Тренажёр «Золотые каналы трафика» покажет, где именно ты теряешь деньги прямо сейчас. 👇",
        reply_markup=kb_after_ogo()
    )
    await callback.answer()

@dp.callback_query(F.data == "get_checklist")
async def cb_get_checklist(callback: CallbackQuery):
    user_id = callback.from_user.id
    expiry = (datetime.now() + timedelta(days=5)).strftime("%d.%m.%Y")

    await callback.message.answer(
        f"Держи чек-лист тренажёра «Золотые каналы трафика»:\n\n"
        f"📄 {CHECKLIST_URL}\n\n"
        f"⏱ Доступ открыт до: {expiry}\n\n"
        "Совет от Сергея: отвечай сразу и тут же фиксируй первое простое действие — "
        "так тренажёр приносит результат.\n\n"
        "🎁 Прошёл быстро и готов поделиться впечатлением?\n"
        "Напиши отзыв — получишь чек-лист 120+ каналов трафика в подарок!",
        reply_markup=kb_after_checklist()
    )
    await callback.message.answer(f"📢 Вступай в наш канал с полезными материалами:\n{CHANNEL_URL}")
    await set_checklist_sent(user_id)

    scheduler.add_job(send_day3_reminder, "date", run_date=datetime.now() + timedelta(days=3), args=[user_id], id=f"day3_{user_id}", replace_existing=True)
    scheduler.add_job(send_day5_dojim, "date", run_date=datetime.now() + timedelta(days=5), args=[user_id], id=f"day5_{user_id}", replace_existing=True)
    scheduler.add_job(send_warmup_day1, "date", run_date=datetime.now() + timedelta(days=1), args=[user_id], id=f"warmup1_{user_id}", replace_existing=True)
    scheduler.add_job(send_warmup_day3, "date", run_date=datetime.now() + timedelta(days=3, hours=2), args=[user_id], id=f"warmup3_{user_id}", replace_existing=True)
    scheduler.add_job(send_warmup_day7, "date", run_date=datetime.now() + timedelta(days=7), args=[user_id], id=f"warmup7_{user_id}", replace_existing=True)
    scheduler.add_job(send_reactivation_30, "date", run_date=datetime.now() + timedelta(days=30), args=[user_id], id=f"react30_{user_id}", replace_existing=True)
    scheduler.add_job(send_reactivation_60, "date", run_date=datetime.now() + timedelta(days=60), args=[user_id], id=f"react60_{user_id}", replace_existing=True)
    await callback.answer()

@dp.callback_query(F.data == "resend_checklist")
async def cb_resend_checklist(callback: CallbackQuery):
    expiry = (datetime.now() + timedelta(days=5)).strftime("%d.%m.%Y")
    await callback.message.answer(f"📄 Вот твой чек-лист:\n{CHECKLIST_URL}\n\n⏱ Доступ до: {expiry}")
    await callback.answer()

@dp.callback_query(F.data == "send_review")
async def cb_send_review(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    if user and user[6]:
        await callback.message.answer("Ты уже отправлял отзыв и получил подарок 🎁")
        await callback.answer()
        return
    await callback.message.answer(
        "Напиши отзыв о тренажёре в произвольной форме — что понравилось, "
        "какие инсайты получил, что планируешь внедрить.\n\n"
        "В ответ получишь чек-лист 120+ каналов трафика 🎁",
        reply_markup=kb_review()
    )
    await state.set_state(ReviewStates.waiting_review)
    await callback.answer()

@dp.callback_query(F.data == "cancel_review")
async def cb_cancel_review(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Хорошо, можешь вернуться к этому позже.")
    await callback.answer()

@dp.message(ReviewStates.waiting_review)
async def process_review(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    await mark_review_sent(user_id)
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📝 Новый отзыв!\n\nОт: @{message.from_user.username} ({message.from_user.full_name})\nID: {user_id}\n\nТекст:\n{message.text}"
        )
    except Exception:
        pass
    await message.answer("Спасибо за отзыв! 🙌\n\nДержи обещанный подарок 👇", reply_markup=kb_after_review())

@dp.callback_query(F.data == "get_bonus")
async def cb_get_bonus(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    if user and user[7]:
        await callback.message.answer(f"Ты уже получил чек-лист 120 каналов:\n{BONUS_CHECKLIST_URL}")
        await callback.answer()
        return
    await callback.message.answer(
        f"📋 Чек-лист 120+ каналов трафика:\n{BONUS_CHECKLIST_URL}\n\n"
        "Начислено 500 баллов — активны 24 часа.\n\n"
        "На что потратить:\n• Углублённый разбор по каналам трафика\n"
        "• ТОП-каналы с твоей ЦА + анализ конкурентов\n"
        "• Спецпредложение на следующий тренажёр"
    )
    await mark_bonus_sent(user_id)
    await callback.message.answer(
        "Поделись тренажёром с друзьями и коллегами.\nКаждый кто пройдёт по твоей ссылке — плюс к твоим бонусам 👇",
        reply_markup=kb_ref_share(user_id)
    )
    await callback.answer()

@dp.callback_query(F.data == "my_ref_link")
async def cb_my_ref_link(callback: CallbackQuery):
    user_id = callback.from_user.id
    ref_count = await get_referral_count(user_id)
    ref_link = f"https://t.me/Trenajer_suhanova_bot?start=ref{user_id}"
    await callback.message.answer(f"🔗 Твоя реферальная ссылка:\n{ref_link}\n\nПриглашено друзей: {ref_count} чел.")
    await callback.answer()

@dp.callback_query(F.data == "show_presentation")
async def cb_show_presentation(callback: CallbackQuery):
    await callback.message.answer(
        f"Держи краткий обзор — что такое тренажёр и как он работает:\n{PRESENTATION_URL}\n\nХочешь увидеть все продукты линейки?",
        reply_markup=kb_presentation()
    )
    await callback.answer()

@dp.callback_query(F.data == "show_all_trainers")
async def cb_show_all_trainers(callback: CallbackQuery):
    await callback.message.answer(
        "Линейка тренажёров:\n\n• Тренажёр №1 — Золотые каналы трафика (бесплатно)\n"
        "• Тренажёр №2 — Система продаж — 5 000 ₽ (скоро)\n"
        "• Тренажёр №3 — Команда и управление — 5 000 ₽ (скоро)\n"
        "• Тренажёр №4 — Финансы и юнит-экономика — 5 000 ₽ (скоро)\n\nНачнём с бесплатного? 👇",
        reply_markup=kb_all_trainers()
    )
    await callback.answer()

@dp.callback_query(F.data == "show_accompaniment")
async def cb_show_accompaniment(callback: CallbackQuery):
    await callback.message.answer(
        "🤝 Индивидуальное сопровождение\n\n→ 3 или 6 месяцев регулярной работы с экспертом\n"
        "→ Еженедельные сессии + поддержка в чате\n→ Внедрение изменений под контролем\n\nот 80 000 ₽\n\nЧтобы узнать подробности — напиши нам:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Написать об условиях", url="https://t.me/atomicprofit")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "coming_soon")
async def cb_coming_soon(callback: CallbackQuery):
    await callback.message.answer("Этот тренажёр скоро появится! 🔜\n\nПока пройди первый — «Золотые каналы трафика» — он бесплатный.")
    await callback.answer()

@dp.callback_query(F.data == "show_session")
async def cb_show_session(callback: CallbackQuery):
    await callback.message.answer(
        "💼 Индивидуальная сессия с экспертом\n\n2 часа работы с Сергеем Сухановым:\n"
        "✅ Разбор результатов тренажёра\n✅ Подбор каналов трафика под твою нишу\n"
        "✅ Приоритизированный план на 90 дней\n\nСтоимость: 30 000 ₽\nФормат: онлайн, Zoom",
        reply_markup=kb_session()
    )
    await callback.answer()

@dp.callback_query(F.data == "session_details")
async def cb_session_details(callback: CallbackQuery):
    await callback.message.answer(
        "На сессии мы:\n\n1. Разбираем результаты твоего тренажёра\n2. Определяем 1–2 главные точки роста\n"
        "3. Подбираем конкретные инструменты под твою нишу\n4. Составляем план действий на 90 дней\n\n"
        "Ты уходишь с:\n→ Пониманием что именно мешает росту\n→ Готовым списком действий по приоритетам\n\nСтоимость: 30 000 ₽",
        reply_markup=kb_session()
    )
    await callback.answer()

@dp.callback_query(F.data == "book_session")
async def cb_book_session(callback: CallbackQuery):
    await callback.message.answer("Отлично! Для записи на сессию — напиши напрямую:\nhttps://t.me/atomicprofit\n\nУкажи что хочешь записаться на индивидуальную сессию.")
    await callback.answer()

@dp.callback_query(F.data == "buy_next_trainer")
async def cb_buy_next_trainer(callback: CallbackQuery):
    await callback.message.answer("Следующие тренажёры пока в разработке 🔜\n\nКак только выйдут — ты получишь уведомление первым.\n\nПока можешь записаться на индивидуальную сессию:", reply_markup=kb_session())
    await callback.answer()

@dp.callback_query(F.data == "send_breakdown")
async def cb_send_breakdown(callback: CallbackQuery):
    await callback.message.answer(
        "Вот разбор по конверсии в повторную покупку:\n\n"
        "Большинство бизнесов тратят 80% бюджета на привлечение новых клиентов и только 20% на удержание.\n\n"
        "При этом продать существующему клиенту в 5–7 раз дешевле.\n\n"
        "3 первых шага:\n1. Сегментировать базу по давности последней покупки\n"
        "2. Создать серию из 3 касаний для «уснувших» клиентов\n3. Сделать специальный оффер только для них\n\n"
        "Хочешь разобрать конкретно твою ситуацию?",
        reply_markup=kb_session()
    )
    await callback.answer()

@dp.callback_query(F.data == "not_relevant")
async def cb_not_relevant(callback: CallbackQuery):
    await callback.message.answer("Понял, не буду беспокоить 👍\nЕсли понадоблюсь — просто напиши /start")
    await callback.answer()

@dp.message(Command("mystats"))
async def cmd_mystats(message: Message):
    user_id = message.from_user.id
    ref_count = await get_referral_count(user_id)
    ref_link = f"https://t.me/Trenajer_suhanova_bot?start=ref{user_id}"
    await message.answer(f"📊 Твоя статистика:\n\nПриглашено друзей: {ref_count} чел.\n\nТвоя реферальная ссылка:\n{ref_link}")

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE checklist_sent_at IS NOT NULL") as cursor:
            got_checklist = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE review_sent = 1") as cursor:
            sent_review = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM referrals") as cursor:
            total_refs = (await cursor.fetchone())[0]
    await message.answer(f"📊 Статистика бота:\n\n👥 Всего пользователей: {total}\n📄 Получили чек-лист: {got_checklist}\n✍️ Оставили отзыв: {sent_review}\n🔗 Реферальных переходов: {total_refs}")

# ─── НАПОМИНАНИЯ ─────────────────────────────────────────────

async def send_day3_reminder(user_id: int):
    try:
        await bot.send_message(user_id, "До конца доступа к тренажёру — 2 дня.\n\nУже прошёл? Зафиксировал главные инсайты?\n\nЕсли нет — сейчас хороший момент. Занимает 20–30 минут.", reply_markup=kb_day3_reminder())
    except Exception as e:
        logger.error(f"Ошибка день 3 для {user_id}: {e}")

async def send_day5_dojim(user_id: int):
    try:
        await bot.send_message(user_id, "Сегодня закрывается доступ к тренажёру «Золотые каналы трафика».\n\nЕсли хочешь продолжить:\n• Следующий тренажёр — 5 000 ₽\n• Или сразу к результату — индивидуальная сессия", reply_markup=kb_day5_dojim())
    except Exception as e:
        logger.error(f"Ошибка день 5 для {user_id}: {e}")

async def send_warmup_day1(user_id: int):
    try:
        await bot.send_message(user_id, "Артём, строительный бизнес.\nПрошёл тренажёр «Золотые каналы» — нашёл 3 слепые зоны.\n\nЧерез 6 недель: +2 канала трафика, конверсия с 8% до 19%, средний чек +25%.\n\nВсё началось с тех же вопросов, на которые ты только что отвечал.")
    except Exception as e:
        logger.error(f"Ошибка прогрев день 1 для {user_id}: {e}")

async def send_warmup_day3(user_id: int):
    try:
        await bot.send_message(user_id, "Один из ответов тренажёра обычно привлекает внимание больше всего.\n\nЕсли ты используешь 1–2 канала трафика — ты конкурируешь там, где уже тесно.\nОстальные 118 каналов — пустые.\n\nНа индивидуальной сессии разбираем конкретно твою ситуацию.", reply_markup=kb_warmup_day3())
    except Exception as e:
        logger.error(f"Ошибка прогрев день 3 для {user_id}: {e}")

async def send_warmup_day7(user_id: int):
    try:
        await bot.send_message(user_id, "Неделю назад ты прошёл тренажёр «Золотые каналы трафика».\n\nЕсли слепые зоны ещё не закрыты — это нормально.\n\nИндивидуальная сессия — 2 часа с экспертом:\n✅ Разбор результатов тренажёра\n✅ Подбор каналов под твою нишу\n✅ План на 90 дней\n\n30 000 ₽", reply_markup=kb_warmup_day7())
    except Exception as e:
        logger.error(f"Ошибка прогрев день 7 для {user_id}: {e}")

async def send_reactivation_30(user_id: int):
    try:
        await bot.send_message(user_id, "Месяц назад ты прошёл тренажёр «Золотые каналы трафика».\n\nСамая частая скрытая потеря — конверсия в повторную покупку.\nЕё игнорирование стоит в среднем 20–40% выручки.\n\nМогу прислать короткий разбор.", reply_markup=kb_reactivation_30())
    except Exception as e:
        logger.error(f"Ошибка реактивация 30 для {user_id}: {e}")

async def send_reactivation_60(user_id: int):
    try:
        await bot.send_message(user_id, "До конца недели — запись на индивидуальную сессию по специальной цене:\n22 000 ₽ вместо 30 000 ₽.\n\nДля тех, кто думал но откладывал.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Записаться по спеццене", callback_data="book_session")],
                [InlineKeyboardButton(text="❌ Не сейчас", callback_data="not_relevant")],
            ])
        )
    except Exception as e:
        logger.error(f"Ошибка реактивация 60 для {user_id}: {e}")

# ─── ЗАПУСК ───────────────────────────────────────────────────
async def main():
    await init_db()
    scheduler.start()
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
