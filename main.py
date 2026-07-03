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
from apscheduler.triggers.cron import CronTrigger

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
W_CUSTOM_AMOUNT, W_GOAL = range(6, 8)

HABIT_REMINDER_TIMES = [(9, 0), (21, 0)]

# ---------------------- КЛАВИАТУРЫ ----------------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["🎯 Привычки", "💧 Вода", "📅 Сегодня"],
        ["➕ Напоминание", "📋 Мои напоминания"],
        ["🗑 Удалить напоминание", "❓ Помощь"],
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
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
    ])


def repeat_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Разово", callback_data="none")],
        [InlineKeyboardButton("Ежедневно", callback_data="daily")],
        [InlineKeyboardButton("Еженедельно", callback_data="weekly")],
        [InlineKeyboardButton("Ежемесячно", callback_data="monthly")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
    ])


def habit_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Полезная", callback_data="good")],
        [InlineKeyboardButton("🚫 Вредная", callback_data="bad")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
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
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

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


# ---------- Users ----------

def ensure_user(user_id: int, chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO users (user_id, chat_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, updated_at=excluded.updated_at
        """,
        (user_id, chat_id, datetime.now(pytz.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_users_with_habits():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT h.user_id, u.chat_id FROM habits h JOIN users u ON h.user_id = u.user_id"
    )
    rows = c.fetchall()
    conn.close()
    return rows


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


def get_habit(habit_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, user_id, name, habit_type FROM habits WHERE id = ?", (habit_id,))
    row = c.fetchone()
    conn.close()
    return row


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


def get_habit_log_for_date(habit_id: int, log_date: date):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT status FROM habit_logs WHERE habit_id = ? AND log_date = ?",
        (habit_id, log_date.isoformat()),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def compute_streak(habit_type: str, logs: list) -> int:
    if not logs:
        return 0

    status_needed = "done" if habit_type == "good" else "not_done"
    dates = {date.fromisoformat(d): s == status_needed for d, s in logs}

    today = date.today()
    streak = 0
    check_day = today

    if not dates.get(check_day, False):
        check_day = today - timedelta(days=1)

    while dates.get(check_day, False):
        streak += 1
        check_day -= timedelta(days=1)

    return streak


def compute_habit_stats(habit_type: str, logs: list, days: int = 30) -> tuple:
    """Возвращает (успешных дней, всего дней с логами, процент)."""
    status_needed = "done" if habit_type == "good" else "not_done"
    cutoff = date.today() - timedelta(days=days - 1)
    filtered = [(d, s) for d, s in logs if date.fromisoformat(d) >= cutoff]
    total = len(filtered)
    success = sum(1 for _, s in filtered if s == status_needed)
    percent = round(success / total * 100) if total else 0
    return success, total, percent


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


# ---------- Habit reminders ----------

async def send_habit_reminder_job(context, application: Application):
    job = context.job
    user_id = job.data["user_id"]
    chat_id = job.data["chat_id"]

    habits = get_user_habits(user_id)
    if not habits:
        return

    # Собираем привычки без отметки сегодня
    pending_good = []
    pending_bad = []
    for hid, name, habit_type in habits:
        status = get_habit_log_for_date(hid, date.today())
        if status:
            continue
        if habit_type == "good":
            pending_good.append(name)
        else:
            pending_bad.append(name)

    if not pending_good and not pending_bad:
        return

    lines = ["⏰ Время отметить привычки!\n"]
    if pending_good:
        lines.append("👍 Полезные:")
        lines.extend(f"• {n}" for n in pending_good)
    if pending_bad:
        lines.append("\n🚫 Вредные (отметь, что сдержался):")
        lines.extend(f"• {n}" for n in pending_bad)
    lines.append("\n👉 Нажми 🎯 Привычки")

    try:
        await application.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания о привычках {user_id}: {e}")


def schedule_habit_reminders(app: Application, user_id: int, chat_id: int):
    for i, (hour, minute) in enumerate(HABIT_REMINDER_TIMES):
        job_id = f"habit_reminder_{user_id}_{i}"
        scheduler.add_job(
            send_habit_reminder_job,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=job_id,
            replace_existing=True,
            args=[app],
            data={"user_id": user_id, "chat_id": chat_id},
        )


async def load_habit_reminders(app: Application):
    rows = get_users_with_habits()
    for user_id, chat_id in rows:
        schedule_habit_reminders(app, user_id, chat_id)
    logger.info(f"Загружено напоминаний о привычках для {len(rows)} пользователей")


# ---------------------- УТИЛИТЫ ----------------------

async def back_to_menu(update: Update, text: str):
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text)
        except Exception:
            pass
        await update.callback_query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(text, reply_markup=MAIN_MENU)


async def ensure_user_from_update(update: Update):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    ensure_user(user_id, chat_id)
    return user_id, chat_id


# ---------------------- ОБРАБОТЧИКИ: ГЛАВНОЕ МЕНЮ ----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user_from_update(update)
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
        "• 🎯 Привычки — сразу открывает список с кнопками отметки\n"
        "• 👍 Полезные — жми ✅\n"
        "• 🚫 Вредные — жми 🔥 Держусь\n"
        "• Автоматические напоминания: 9:00 и 21:00\n\n"
        "<b>Вода</b>\n"
        "• 💧 Вода — быстро добавить +200/+300/+500 мл\n"
        "• 📅 Сегодня — дашборд дня\n\n"
        f"Часовой пояс: <b>{DEFAULT_TIMEZONE}</b>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_MENU)


async def today_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user_from_update(update)
    user_id = update.effective_user.id

    # Привычки
    habits = get_user_habits(user_id)
    today = date.today()
    habit_lines = []
    pending = 0
    for hid, name, habit_type in habits:
        status = get_habit_log_for_date(hid, today)
        if status:
            if habit_type == "good":
                habit_lines.append(f"✅ {name}")
            else:
                habit_lines.append(f"🔥 {name}")
        else:
            pending += 1
            if habit_type == "good":
                habit_lines.append(f"⬜ {name}")
            else:
                habit_lines.append(f"⬜ {name} (сдержаться)")

    # Вода
    today_ml = get_water_today(user_id)
    goal_ml = get_water_goal(user_id)

    # Напоминания
    reminders = get_user_reminders(user_id)[:3]
    reminder_lines = []
    for rid, text, next_run_iso, repeat_type in reminders:
        next_run = datetime.fromisoformat(next_run_iso).astimezone(TZ)
        reminder_lines.append(f"• {text} — {next_run.strftime('%d.%m %H:%M')}")

    lines = [f"<b>📅 Сегодня, {today.strftime('%d.%m.%Y')}</b>\n"]

    lines.append(f"<b>💧 Вода: {today_ml} / {goal_ml} мл</b>")
    lines.append("▰" * min(10, int((today_ml / goal_ml) * 10)) + "▱" * (10 - min(10, int((today_ml / goal_ml) * 10))))
    lines.append("")

    if habit_lines:
        lines.append(f"<b>🎯 Привычки ({len(habits) - pending}/{len(habits)}):</b>")
        lines.extend(habit_lines)
    else:
        lines.append("<b>🎯 Привычек пока нет</b>")

    if pending > 0:
        lines.append(f"\n<i>Осталось отметить: {pending}</i>")

    if reminder_lines:
        lines.append("\n<b>⏰ Ближайшие напоминания:</b>")
        lines.extend(reminder_lines)

    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_MENU)


# ---------------------- ОБРАБОТЧИКИ: НАПОМИНАНИЯ ----------------------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user_from_update(update)
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

    if data == "menu":
        await back_to_menu(update, "Главное меню")
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

    if query.data == "menu":
        await back_to_menu(update, "Главное меню")
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
    await back_to_menu(update, "Отменено")
    return ConversationHandler.END


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user_from_update(update)
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
    await ensure_user_from_update(update)
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
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="menu")])

    await update.message.reply_text(
        "Выберите напоминание для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return "CHOOSE_DELETE_REMINDER"


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu":
        await back_to_menu(update, "Главное меню")
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
    await ensure_user_from_update(update)
    query = update.callback_query
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)

    if not habits:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить привычку", callback_data="habits_add")],
            [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
        ])
        text = "У вас пока нет привычек. Добавьте первую!"
        if query:
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text, reply_markup=keyboard)
        return

    lines = ["<b>🎯 Твои привычки</b>\n"]
    keyboard = []
    for hid, name, habit_type in habits:
        logs = get_habit_logs(hid)
        streak = compute_streak(habit_type, logs)
        icon = "👍" if habit_type == "good" else "🚫"
        today_status = get_habit_log_for_date(hid, date.today())
        done_today = bool(today_status)

        fire = f" 🔥{streak}" if streak > 0 else ""
        check = " ✅" if done_today else ""
        lines.append(f"{icon} <b>{name}</b>{fire}{check}")

        # Кнопки отметки
        if habit_type == "good":
            btn = InlineKeyboardButton(f"✅ {name}", callback_data=f"logh_{hid}_done")
        else:
            btn = InlineKeyboardButton(f"🔥 Держусь", callback_data=f"logh_{hid}_not_done")
        keyboard.append([btn])

    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data="habits_add")])
    keyboard.append([
        InlineKeyboardButton("🗑 Удалить", callback_data="habits_delete"),
        InlineKeyboardButton("📊 Статистика", callback_data="habits_stats"),
    ])
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="menu")])

    text = "\n".join(lines)
    if query:
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def log_habit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("_")
    habit_id = int(parts[1])
    status = parts[2]

    today = date.today()
    log_habit(habit_id, today, status)

    habit = get_habit(habit_id)
    if habit:
        name = habit[2]
        habit_type = habit[3]
        logs = get_habit_logs(habit_id)
        streak = compute_streak(habit_type, logs)
        if habit_type == "good":
            msg = f"✅ Отмечено: <b>{name}</b>\n🔥 Стрик: {streak} дн."
        else:
            msg = f"🔥 Сдержался: <b>{name}</b>\nСтрик: {streak} дн."
        await query.edit_message_text(msg, parse_mode="HTML")
        await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)
    else:
        await query.edit_message_text("Привычка не найдена.")


async def habit_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    habits = get_user_habits(user_id)

    if not habits:
        await query.edit_message_text("У вас пока нет привычек.")
        return

    lines = ["<b>📊 Статистика привычек (30 дней)</b>\n"]
    for hid, name, habit_type in habits:
        logs = get_habit_logs(hid)
        streak = compute_streak(habit_type, logs)
        success, total, percent = compute_habit_stats(habit_type, logs, 30)
        icon = "👍" if habit_type == "good" else "🚫"
        lines.append(
            f"{icon} <b>{name}</b>\n"
            f"   🔥 Стрик: {streak} дн. | {success}/{total} ({percent}%)\n"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="habits_menu")],
    ])
    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


async def add_habit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите название привычки:\n\n"
        "Например: <code>Зарядка</code> или <code>Алкоголь</code>",
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

    if query.data == "menu":
        await back_to_menu(update, "Главное меню")
        return ConversationHandler.END

    habit_type = query.data
    name = context.user_data["habit_name"]
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    habit_id = add_habit(user_id, name, habit_type)
    schedule_habit_reminders(context.application, user_id, chat_id)

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
            [InlineKeyboardButton("➕ Добавить", callback_data="habits_add")],
            [InlineKeyboardButton("🔙 Назад", callback_data="habits_menu")],
        ])
        await query.edit_message_text("У вас пока нет привычек для удаления.", reply_markup=keyboard)
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"{h[1]} ({'👍' if h[2] == 'good' else '🚫'})", callback_data=f"delh_{h[0]}")]
        for h in habits
    ]
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="menu")])

    await query.edit_message_text(
        "Выберите привычку для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return "CHOOSE_DELETE_HABIT"


async def delete_habit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu":
        await back_to_menu(update, "Главное меню")
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
    await back_to_menu(update, "Отменено")
    return ConversationHandler.END


# ---------------------- ОБРАБОТЧИКИ: ВОДА ----------------------

async def water_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user_from_update(update)
    query = update.callback_query
    user_id = update.effective_user.id
    today_ml = get_water_today(user_id)
    goal_ml = get_water_goal(user_id)
    remaining = max(0, goal_ml - today_ml)
    percent = min(100, int((today_ml / goal_ml) * 100)) if goal_ml else 0

    bar = "▰" * (percent // 10) + "▱" * (10 - percent // 10)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("+200", callback_data="water_200"),
            InlineKeyboardButton("+300", callback_data="water_300"),
            InlineKeyboardButton("+500", callback_data="water_500"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="water_stats"),
            InlineKeyboardButton("🎯 Цель", callback_data="water_goal"),
        ],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu")],
    ])

    text = (
        f"<b>💧 Вода сегодня</b>\n\n"
        f"Выпито: <b>{today_ml} мл</b> из {goal_ml} мл\n"
        f"Осталось: <b>{remaining} мл</b>\n"
        f"{bar} {percent}%"
    )
    if query:
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def water_add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu":
        await back_to_menu(update, "Главное меню")
        return

    if data.startswith("water_"):
        amount = int(data.split("_")[1])
        user_id = update.effective_user.id
        add_water(user_id, amount)
        today_ml = get_water_today(user_id)
        goal_ml = get_water_goal(user_id)
        percent = min(100, int((today_ml / goal_ml) * 100)) if goal_ml else 0
        bar = "▰" * (percent // 10) + "▱" * (10 - percent // 10)

        await query.edit_message_text(
            f"✅ Добавлено <b>{amount} мл</b>\n\n"
            f"Сегодня: <b>{today_ml} мл</b> из {goal_ml} мл\n"
            f"{bar} {percent}%",
            parse_mode="HTML",
        )
        await query.message.reply_text("Главное меню:", reply_markup=MAIN_MENU)


async def water_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    goal_ml = get_water_goal(user_id)
    stats = get_water_stats(user_id, days=7)

    lines = ["<b>📊 Вода за 7 дней</b>\n"]
    for d, amount in stats:
        bar = "▰" * min(10, int((amount / goal_ml) * 10)) if goal_ml else ""
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
    await back_to_menu(update, "Отменено")
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
                CallbackQueryHandler(cancel, pattern="^menu$"),
            ],
            R_CUSTOM_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_time)],
            R_REPEAT: [
                CallbackQueryHandler(add_repeat, pattern="^(none|daily|weekly|monthly)$"),
                CallbackQueryHandler(cancel, pattern="^menu$"),
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
                CallbackQueryHandler(delete_confirm, pattern="^(delr_\\d+|menu)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Привычки
    habit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_habit_start, pattern="^habits_add$"),
        ],
        states={
            H_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_habit_name)],
            H_TYPE: [
                CallbackQueryHandler(add_habit_type, pattern="^(good|bad)$"),
                CallbackQueryHandler(habit_cancel, pattern="^menu$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", habit_cancel)],
    )

    delete_habit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_habit_start, pattern="^habits_delete$")],
        states={
            "CHOOSE_DELETE_HABIT": [
                CallbackQueryHandler(delete_habit_confirm, pattern="^(delh_\\d+|menu)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", habit_cancel)],
    )

    # Вода
    water_goal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(water_goal_start, pattern="^water_goal$")],
        states={
            W_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, water_goal_set)],
        },
        fallbacks=[CommandHandler("cancel", water_cancel)],
    )

    # Conversation handlers должны быть первыми, чтобы перехватывать свои callback'и
    application.add_handler(add_conv)
    application.add_handler(delete_conv)
    application.add_handler(habit_conv)
    application.add_handler(delete_habit_conv)
    application.add_handler(water_goal_conv)

    # Reply-кнопки главного меню
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_reminders))
    application.add_handler(CommandHandler("today", today_dashboard))
    application.add_handler(MessageHandler(filters.Regex("^📅 Сегодня$"), today_dashboard))
    application.add_handler(MessageHandler(filters.Regex("^🎯 Привычки$"), habits_menu))
    application.add_handler(MessageHandler(filters.Regex("^💧 Вода$"), water_menu))
    application.add_handler(MessageHandler(filters.Regex("^📋 Мои напоминания$"), list_reminders))
    application.add_handler(MessageHandler(filters.Regex("^❓ Помощь$"), help_command))

    # Callback-обработчики вне диалогов
    application.add_handler(CallbackQueryHandler(habits_menu, pattern="^habits_menu$"))
    application.add_handler(CallbackQueryHandler(habit_stats, pattern="^habits_stats$"))
    application.add_handler(CallbackQueryHandler(log_habit_handler, pattern="^logh_\\d+_(done|not_done)$"))
    application.add_handler(CallbackQueryHandler(water_menu, pattern="^water_menu$"))
    application.add_handler(CallbackQueryHandler(water_add_handler, pattern="^water_\\d+$"))
    application.add_handler(CallbackQueryHandler(water_stats, pattern="^water_stats$"))
    application.add_handler(CallbackQueryHandler(start, pattern="^menu$"))

    scheduler.start()

    async def post_init(app: Application):
        await load_reminders_to_scheduler(app)
        await load_habit_reminders(app)

    application.post_init = post_init

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
