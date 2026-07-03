import os
import logging
import sqlite3
import pytz
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ---------------------- НАСТРОЙКИ ----------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Europe/Moscow")
DB_PATH = os.environ.get("DB_PATH", "database.db")

if not BOT_TOKEN:
    raise ValueError("Укажите BOT_TOKEN в переменных окружения")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TZ = pytz.timezone(DEFAULT_TIMEZONE)

# Состояния диалогов
R_TEXT, R_CHOOSE_TIME, R_CUSTOM_TIME, R_REPEAT = range(4)
H_NAME, H_TYPE = range(4, 6)
W_AMOUNT, W_GOAL = range(6, 8)

# ---------------------- КЛАВИАТУРЫ ----------------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Напоминание", "📋 Мои напоминания"],
        ["🗑 Удалить напоминание", "🎯 Привычки"],
        ["💧 Вода", "❓ Помощь"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)


def reminder_time_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5 мин", callback_data="time_5"),
            InlineKeyboardButton("10 мин", callback_data="time_10"),
            InlineKeyboardButton("30 мин", callback_data="time_30"),
        ],
        [
            InlineKeyboardButton("2 часа", callback_data="time_120"),
            InlineKeyboardButton("6 часов", callback_data="time_360"),
            InlineKeyboardButton("12 часов", callback_data="time_720"),
        ],
        [InlineKeyboardButton("📅 Своё время", callback_data="time_custom")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def repeat_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Разово", callback_data="none")],
        [InlineKeyboardButton("Ежедневно", callback_data="daily")],
        [InlineKeyboardButton("Еженедельно", callback_data="weekly")],
        [InlineKeyboardButton("Ежемесячно", callback_data="monthly")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def habit_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Полезная", callback_data="good")],
        [InlineKeyboardButton("🚫 Вредная", callback_data="bad")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


def water_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("200 мл", callback_data="water_200"),
            InlineKeyboardButton("300 мл", callback_data="water_300"),
        ],
        [
            InlineKeyboardButton("500 мл", callback_data="water_500"),
            InlineKeyboardButton("750 мл", callback_data="water_750"),
        ],
        [InlineKeyboardButton("💧 Своё количество", callback_data="water_custom")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])


REPEAT_LABELS = {
    "none": "разово",
    "daily": "ежедневно",
    "weekly": "еженедельно",
    "monthly": "ежемесячно",
}


# ---------------------- БАЗА ДАННЫХ ----------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            next_run TEXT NOT NULL,
            repeat_type TEXT NOT NULL DEFAULT 'none',
            created_at TEXT NOT NULL
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            habit_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS habit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(habit_id, log_date)
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS water_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            amount_ml INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS water_goals (
            user_id INTEGER PRIMARY KEY,
            goal_ml INTEGER NOT NULL DEFAULT 2000
        )
        """
    )

    conn.commit()
    conn.close()


# ---------- Reminders ----------

def add_reminder(user_id: int, chat_id: int, text: str, next_run: datetime, repeat_type: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO reminders (user_id, chat_id, text, next_run, repeat_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, chat_id, text, next_run.isoformat(), repeat_type, datetime.now(pytz.utc).isoformat()),
    )
    reminder_id = c.lastrowid
    conn.commit()
    conn.close()
    return reminder_id


def get_user_reminders(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, text, next_run, repeat_type FROM reminders WHERE user_id = ? ORDER BY next_run",
        (user_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_reminder(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, chat_id, text, next_run, repeat_type FROM reminders WHERE id = ?", (reminder_id,))
    row = c.fetchone()
    conn.close()
    return row


def delete_reminder(reminder_id: int, user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def update_next_run(reminder_id: int, next_run: datetime):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE reminders SET next_run = ? WHERE id = ?", (next_run.isoformat(), reminder_id))
    conn.commit()
    conn.close()


def delete_reminder_by_id(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


# ---------- Habits ----------

def add_habit(user_id: int, name: str, habit_type: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO habits (user_id, name, habit_type, created_at) VALUES (?, ?, ?, ?)",
        (user_id, name, habit_type, datetime.now(pytz.utc).isoformat()),
    )
    habit_id = c.lastrowid
    conn.commit()
    conn.close()
    return habit_id


def get_user_habits(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, habit_type FROM habits WHERE user_id = ? ORDER BY id", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def delete_habit(habit_id: int, user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id))
    affected = c.rowcount
    if affected:
        c.execute("DELETE FROM habit_logs WHERE habit_id = ?", (habit_id,))
    conn.commit()
    conn.close()
    return affected > 0


def log_habit(habit_id: int, log_date: date, status: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO habit_logs (habit_id, log_date, status, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(habit_id, log_date) DO UPDATE SET status=excluded.status
        """,
        (habit_id, log_date.isoformat(), status, datetime.now(pytz.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_habit_logs(habit_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT log_date, status FROM habit_logs WHERE habit_id = ? ORDER BY log_date DESC",
        (habit_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def compute_streak(habit_type: str, logs: list) -> int:
    """Считает подряд идущие дни с нужным статусом, начиная со вчера/сегодня."""
    if not logs:
        return 0

    status_needed = "done" if habit_type == "good" else "not_done"
    dates = {date.fromisoformat(d): s == status_needed for d, s in logs}

    today = date.today()
    streak = 0
    check_day = today

    # Если сегодня ещё не отмечено — начинаем со вчера
    if not dates.get(check_day, False):
        check_day = today - timedelta(days=1)

    while dates.get(check_day, False):
        streak += 1
        check_day -= timedelta(days=1)

    return streak


# ---------- Water ----------

def add_water(user_id: int, amount_ml: int, log_date: date = None):
    if log_date is None:
        log_date = date.today()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO water_logs (user_id, log_date, amount_ml, created_at) VALUES (?, ?, ?, ?)",
        (user_id, log_date.isoformat(), amount_ml, datetime.now(pytz.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_water_today(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT SUM(amount_ml) FROM water_logs WHERE user_id = ? AND log_date = ?",
        (user_id, date.today().isoformat()),
    )
    result = c.fetchone()[0]
    conn.close()
    return result or 0


def set_water_goal(user_id: int, goal_ml: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO water_goals (user_id, goal_ml) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET goal_ml=excluded.goal_ml",
        (user_id, goal_ml),
    )
    conn.commit()
    conn.close()


def get_water_goal(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT goal_ml FROM water_goals WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 2000


def get_water_stats(user_id: int, days: int = 7):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT log_date, SUM(amount_ml)
        FROM water_logs
        WHERE user_id = ? AND log_date >= ?
        GROUP BY log_date
        ORDER BY log_date DESC
        """,
        (user_id, (date.today() - timedelta(days=days - 1)).isoformat()),
    )
    rows = c.fetchall()
    conn.close()
    return rows


# ---------------------- ПЛАНИРОВЩИК ----------------------

scheduler = AsyncIOScheduler(timezone=DEFAULT_TIMEZONE)


def compute_next_run(repeat_type: str, base: datetime) -> datetime | None:
    if repeat_type == "daily":
        return base + timedelta(days=1)
    if repeat_type == "weekly":
        return base + timedelta(weeks=1)
    if repeat_type == "monthly":
        return base + relativedelta(months=1)
    return None


async def send_reminder_job(context, application: Application):
    job = context.job
    reminder_id = job.data["reminder_id"]
    chat_id = job.data["chat_id"]
    text = job.data["text"]
    repeat_type = job.data["repeat_type"]

    try:
        await application.bot.send_message(chat_id=chat_id, text=f"⏰ Напоминание:\n{text}")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
        return

    if repeat_type == "none":
        delete_reminder_by_id(reminder_id)
        return

    row = get_reminder(reminder_id)
    if not row:
        return
    _, _, _, _, next_run_iso, _ = row
    last_run = datetime.fromisoformat(next_run_iso)
    next_run = compute_next_run(repeat_type, last_run)
    if not next_run:
        return
    update_next_run(reminder_id, next_run)
    schedule_reminder(application, reminder_id, chat_id, text, next_run, repeat_type)


def schedule_reminder(app: Application, reminder_id: int, chat_id: int, text: str, run_time: datetime, repeat_type: str):
    job_id = f"reminder_{reminder_id}"
    scheduler.add_job(
        send_reminder_job,
        trigger=DateTrigger(run_date=run_time),
        id=job_id,
        replace_existing=True,
        args=[app],
        data={
            "reminder_id": reminder_id,
            "chat_id": chat_id,
            "text": text,
            "repeat_type": repeat_type,
        },
        misfire_grace_time=3600,
    )


async def load_reminders_to_scheduler(app: Application):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, chat_id, text, next_run, repeat_type FROM reminders")
    rows = c.fetchall()
    conn.close()

    now = datetime.now(pytz.utc)
    for row in rows:
        reminder_id, chat_id, text, next_run_iso, repeat_type = row
        next_run = datetime.fromisoformat(next_run_iso)

        if next_run < now:
            if repeat_type == "none":
                delete_reminder_by_id(reminder_id)
                continue
            next_run = compute_next_run(repeat_type, datetime.now(TZ))
            if not next_run:
                continue
            update_next_run(reminder_id, next_run)

        schedule_reminder(app, reminder_id, chat_id, text, next_run, repeat_type)

    logger.info(f"Загружено напоминаний из БД: {len(rows)}")


# ---------------------- УТИЛИТЫ ----------------------

async def return_to_menu(update: Update, text: str):
    """Отправляет сообщение с главным меню, убирая inline-кнопки."""
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text)
        except Exception:
            pass
        await update.callback_query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(text, reply_markup=MAIN_MENU)


def menu_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="menu")]])


# ---------------------- ОБРАБОТЧИКИ: НАПОМИНАНИЯ ----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой помощник: напоминания, привычки и трекер воды.\n\n"
        "Всё управление — кнопками ниже 👇",
        reply_markup=MAIN_MENU,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Напоминания</b>\n"
        "• ➕ Напоминание — текст → время → периодичность\n"
        "• 📋 Мои напоминания — список\n"
        "• 🗑 Удалить напоминание\n\n"
        "<b>Привычки</b>\n"
        "• 👍 Полезные — отмечай, что сделал\n"
        "• 🚫 Вредные — отмечай, что НЕ делал\n"
        "• 🔥 Стрик покажет, сколько дней подряд\n\n"
        "<b>Вода</b>\n"
        "• 💧 Вода — быстро добавить выпитое\n"
        "• Цель по умолчанию — 2000 мл\n\n"
        f"Часовой пояс: <b>{DEFAULT_TIMEZONE}</b>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_MENU)


# ---------- Добавление напоминания ----------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите текст напоминания:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return R_TEXT


async def add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["reminder_text"] = update.message.text
    await update.message.reply_text(
        "Выберите время напоминания:",
        reply_markup=reminder_time_keyboard(),
    )
    return R_CHOOSE_TIME


async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await return_to_menu(update, "Отменено.")
        return ConversationHandler.END

    if data == "time_custom":
        await query.edit_message_text(
            "Введите дату и время в формате:\n"
            "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
            "Пример: <code>15.07.2026 14:30</code>",
            parse_mode="HTML",
        )
        return R_CUSTOM_TIME

    minutes = int(data.split("_")[1])
    run_time = datetime.now(TZ) + timedelta(minutes=minutes)
    context.user_data["reminder_time"] = run_time

    await query.edit_message_text(
        f"⏰ Время: <b>{run_time.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        f"Выберите периодичность:",
        parse_mode="HTML",
        reply_markup=repeat_keyboard(),
    )
    return R_REPEAT


async def custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        naive = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Введите так:\n<code>15.07.2026 14:30</code>",
            parse_mode="HTML",
        )
        return R_CUSTOM_TIME

    localized = TZ.localize(naive)
    if localized < datetime.now(TZ):
        await update.message.reply_text(
            "Это время уже прошло. Введите будущую дату и время:"
        )
        return R_CUSTOM_TIME

    context.user_data["reminder_time"] = localized
    await update.message.reply_text(
        f"⏰ Время: <b>{localized.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        f"Выберите периодичность:",
        parse_mode="HTML",
        reply_markup=repeat_keyboard(),
    )
    return R_REPEAT


async def add_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await return_to_menu(update, "Отменено.")
        return ConversationHandler.END

    repeat_type = query.data
    text = context.user_data["reminder_text"]
    run_time = context.user_data["reminder_time"]

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    reminder_id = add_reminder(user_id, chat_id, text, run_time, repeat_type)
    schedule_reminder(context.application, reminder_id, chat_id, text, run_time, repeat_type)

    await query.edit_message_text(
        f"✅ Напоминание установлено!\n\n"
        f"📝 Текст: {text}\n"
        f"📅 Время: {run_time.strftime('%d.%m.%Y %H:%M')}\n"
        f"🔄 Периодичность: {REPEAT_LABELS[repeat_type]}"
    )
    await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await return_to_menu(update, "Отменено.")
    return ConversationHandler.END


# ---------- Список и удаление ----------

async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_reminders(user_id)
    if not rows:
        await update.message.reply_text("У вас нет активных напоминаний.", reply_markup=MAIN_MENU)
        return

    lines = ["📋 Ваши напоминания:\n"]
    for rid, text, next_run_iso, repeat_type in rows:
        next_run = datetime.fromisoformat(next_run_iso).astimezone(TZ)
        lines.append(
            f"<b>#{rid}</b>\n"
            f"📝 {text}\n"
            f"📅 {next_run.strftime('%d.%m.%Y %H:%M')}\n"
            f"🔄 {REPEAT_LABELS.get(repeat_type, repeat_type)}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_MENU)


async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_reminders(user_id)
    if not rows:
        await update.message.reply_text(
            "У вас нет активных напоминаний для удаления.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    keyboard = []
    for rid, text, _, _ in rows:
        label = f"#{rid}: {text[:30]}{'...' if len(text) > 30 else ''}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"delr_{rid}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.message.reply_text(
        "Выберите напоминание для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return "CHOOSE_DELETE_REMINDER"


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await return_to_menu(update, "Отменено.")
        return ConversationHandler.END

    if not data.startswith("delr_"):
        return ConversationHandler.END

    reminder_id = int(data.split("_")[1])
    user_id = update.effective_user.id

    if delete_reminder(reminder_id, user_id):
        job_id = f"reminder_{reminder_id}"
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        await query.edit_message_text("✅ Напоминание удалено.")
    else:
        await query.edit_message_text("Не удалось удалить напоминание.")

    await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ---------------------- ОБРАБОТЧИКИ: ПРИВЫЧКИ ----------------------

async def habits_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Мои привычки", callback_data="habits_list")],
        [InlineKeyboardButton("➕ Добавить привычку", callback_data="habits_add")],
        [InlineKeyboardButton("🗑 Удалить привычку", callback_data="habits_delete")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
    ])

    text = "<b>🎯 Привычки</b>\n\nЗдесь можно отслеживать полезные и вредные привычки."
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def habits_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)

    if not habits:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить привычку", callback_data="habits_add")],
            [InlineKeyboardButton("🔙 Назад", callback_data="habits_menu")],
        ])
        await query.edit_message_text(
            "У вас пока нет привычек.",
            reply_markup=keyboard,
        )
        return

    lines = ["<b>🎯 Твои привычки</b>\n"]
    keyboard = []
    for hid, name, habit_type in habits:
        logs = get_habit_logs(hid)
        streak = compute_streak(habit_type, logs)
        icon = "👍" if habit_type == "good" else "🚫"
        fire = f" 🔥{streak}" if streak > 0 else ""
        lines.append(f"{icon} <b>{name}</b>{fire}")

        # Кнопка отметки
        if habit_type == "good":
            btn_text = f"✅ {name}"
            btn_data = f"logh_{hid}_done"
        else:
            btn_text = f"🙅 Не {name}"
            btn_data = f"logh_{hid}_not_done"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=btn_data)])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="habits_menu")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def log_habit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    habit_id = int(parts[1])
    status = parts[2]

    today = date.today()
    log_habit(habit_id, today, status)

    habit = get_user_habits(update.effective_user.id)
    name = next((h[1] for h in habit if h[0] == habit_id), "Привычка")

    if status == "done":
        await query.edit_message_text(f"✅ Отмечено: <b>{name}</b>", parse_mode="HTML")
    else:
        await query.edit_message_text(f"🙅 Отмечено: сегодня <b>НЕ {name}</b>", parse_mode="HTML")

    await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)


async def add_habit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите название привычки:\n\n"
        "Например: <code>Зарядка</code> или <code>Пить пиво</code>",
        parse_mode="HTML",
    )
    return H_NAME


async def add_habit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["habit_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Выберите тип привычки:",
        reply_markup=habit_type_keyboard(),
    )
    return H_TYPE


async def add_habit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await return_to_menu(update, "Отменено.")
        return ConversationHandler.END

    habit_type = query.data
    name = context.user_data["habit_name"]
    user_id = update.effective_user.id

    add_habit(user_id, name, habit_type)
    type_label = "полезная" if habit_type == "good" else "вредная"
    await query.edit_message_text(f"✅ Добавлена {type_label} привычка: <b>{name}</b>", parse_mode="HTML")
    await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def delete_habit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)

    if not habits:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить привычку", callback_data="habits_add")],
            [InlineKeyboardButton("🔙 Назад", callback_data="habits_menu")],
        ])
        await query.edit_message_text("У вас пока нет привычек для удаления.", reply_markup=keyboard)
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"{h[1]} ({'👍' if h[2] == 'good' else '🚫'})", callback_data=f"delh_{h[0]}")]
        for h in habits
    ]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await query.edit_message_text(
        "Выберите привычку для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return "CHOOSE_DELETE_HABIT"


async def delete_habit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await return_to_menu(update, "Отменено.")
        return ConversationHandler.END

    if not data.startswith("delh_"):
        return ConversationHandler.END

    habit_id = int(data.split("_")[1])
    user_id = update.effective_user.id

    if delete_habit(habit_id, user_id):
        await query.edit_message_text("✅ Привычка удалена.")
    else:
        await query.edit_message_text("Не удалось удалить привычку.")

    await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def habit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await return_to_menu(update, "Отменено.")
    return ConversationHandler.END


# ---------------------- ОБРАБОТЧИКИ: ВОДА ----------------------

async def water_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    today_ml = get_water_today(user_id)
    goal_ml = get_water_goal(user_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить воду", callback_data="water_add")],
        [InlineKeyboardButton("📊 Статистика", callback_data="water_stats")],
        [InlineKeyboardButton("🎯 Цель: " + str(goal_ml) + " мл", callback_data="water_goal")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
    ])

    text = (
        f"<b>💧 Вода сегодня</b>\n\n"
        f"Выпито: <b>{today_ml} мл</b> из {goal_ml} мл\n"
        f"Осталось: <b>{max(0, goal_ml - today_ml)} мл</b>"
    )
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def water_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Выберите сколько воды выпили:",
        reply_markup=water_keyboard(),
    )
    return W_AMOUNT


async def water_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await return_to_menu(update, "Отменено.")
        return ConversationHandler.END

    if data == "water_custom":
        await query.edit_message_text(
            "Введите количество воды в миллилитрах:\n"
            "Например: <code>350</code>",
            parse_mode="HTML",
        )
        return W_AMOUNT

    if data.startswith("water_"):
        amount = int(data.split("_")[1])
        user_id = update.effective_user.id
        add_water(user_id, amount)
        today_ml = get_water_today(user_id)
        goal_ml = get_water_goal(user_id)

        await query.edit_message_text(
            f"✅ Добавлено <b>{amount} мл</b>\n\n"
            f"Сегодня: <b>{today_ml} мл</b> из {goal_ml} мл",
            parse_mode="HTML",
        )
        await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    return W_AMOUNT


async def water_add_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text("Введите число, например <code>350</code>:", parse_mode="HTML")
        return W_AMOUNT

    if amount <= 0 or amount > 10000:
        await update.message.reply_text("Введите разумное число от 1 до 10000 мл:")
        return W_AMOUNT

    user_id = update.effective_user.id
    add_water(user_id, amount)
    today_ml = get_water_today(user_id)
    goal_ml = get_water_goal(user_id)

    await update.message.reply_text(
        f"✅ Добавлено <b>{amount} мл</b>\n\n"
        f"Сегодня: <b>{today_ml} мл</b> из {goal_ml} мл",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def water_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    goal_ml = get_water_goal(user_id)
    stats = get_water_stats(user_id, days=7)

    lines = ["<b>📊 Вода за последние 7 дней</b>\n"]
    for d, amount in stats:
        bar = "█" * int((amount / goal_ml) * 10) if goal_ml else ""
        lines.append(f"{d}: <b>{amount} мл</b> {bar}")

    if not stats:
        lines.append("Пока нет записей.")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="water_menu")],
    ])
    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


async def water_goal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите цель в миллилитрах:\n"
        "Например: <code>2500</code>",
        parse_mode="HTML",
    )
    return W_GOAL


async def water_goal_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        goal = int(text)
    except ValueError:
        await update.message.reply_text("Введите число, например <code>2500</code>:", parse_mode="HTML")
        return W_GOAL

    if goal <= 0 or goal > 10000:
        await update.message.reply_text("Введите разумное число от 1 до 10000 мл:")
        return W_GOAL

    user_id = update.effective_user.id
    set_water_goal(user_id, goal)
    await update.message.reply_text(
        f"✅ Цель установлена: <b>{goal} мл</b>",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def water_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await return_to_menu(update, "Отменено.")
    return ConversationHandler.END


# ---------------------- ЗАПУСК ----------------------

def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # Напоминания
    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex("^➕ Напоминание$"), add_start),
        ],
        states={
            R_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_text)],
            R_CHOOSE_TIME: [
                CallbackQueryHandler(choose_time, pattern="^time_(5|10|30|120|360|720|custom)$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            R_CUSTOM_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_time)],
            R_REPEAT: [
                CallbackQueryHandler(add_repeat, pattern="^(none|daily|weekly|monthly)$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    delete_conv = ConversationHandler(
        entry_points=[
            CommandHandler("delete", delete_start),
            MessageHandler(filters.Regex("^🗑 Удалить напоминание$"), delete_start),
        ],
        states={
            "CHOOSE_DELETE_REMINDER": [
                CallbackQueryHandler(delete_confirm, pattern="^(delr_\\d+|cancel)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Привычки
    habit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("habits", habits_menu),
            MessageHandler(filters.Regex("^🎯 Привычки$"), habits_menu),
            CallbackQueryHandler(add_habit_start, pattern="^habits_add$"),
        ],
        states={
            H_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_habit_name)],
            H_TYPE: [
                CallbackQueryHandler(add_habit_type, pattern="^(good|bad)$"),
                CallbackQueryHandler(habit_cancel, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", habit_cancel)],
    )

    delete_habit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_habit_start, pattern="^habits_delete$")],
        states={
            "CHOOSE_DELETE_HABIT": [
                CallbackQueryHandler(delete_habit_confirm, pattern="^(delh_\\d+|cancel)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", habit_cancel)],
    )

    # Вода
    water_conv = ConversationHandler(
        entry_points=[
            CommandHandler("water", water_menu),
            MessageHandler(filters.Regex("^💧 Вода$"), water_menu),
            CallbackQueryHandler(water_add_start, pattern="^water_add$"),
        ],
        states={
            W_AMOUNT: [
                CallbackQueryHandler(water_add_amount, pattern="^(water_\\d+|water_custom|cancel)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, water_add_custom_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", water_cancel)],
    )

    water_goal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(water_goal_start, pattern="^water_goal$")],
        states={
            W_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_goal_set)],
        },
        fallbacks=[CommandHandler("cancel", water_cancel)],
    )

    # Callback-обработчики вне ConversationHandler
    application.add_handler(CallbackQueryHandler(habits_menu, pattern="^habits_menu$"))
    application.add_handler(CallbackQueryHandler(habits_list, pattern="^habits_list$"))
    application.add_handler(CallbackQueryHandler(log_habit_handler, pattern="^logh_\\d+_(done|not_done)$"))
    application.add_handler(CallbackQueryHandler(water_menu, pattern="^water_menu$"))
    application.add_handler(CallbackQueryHandler(water_stats, pattern="^water_stats$"))
    application.add_handler(CallbackQueryHandler(start, pattern="^menu$"))

    # Команды и reply-кнопки
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_reminders))
    application.add_handler(MessageHandler(filters.Regex("^📋 Мои напоминания$"), list_reminders))
    application.add_handler(MessageHandler(filters.Regex("^❓ Помощь$"), help_command))

    # Conversation handlers
    application.add_handler(add_conv)
    application.add_handler(delete_conv)
    application.add_handler(habit_conv)
    application.add_handler(delete_habit_conv)
    application.add_handler(water_conv)
    application.add_handler(water_goal_conv)

    scheduler.start()
    application.post_init = load_reminders_to_scheduler

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
