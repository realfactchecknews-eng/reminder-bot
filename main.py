import os
import logging
import sqlite3
import pytz
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Состояния диалога добавления напоминания
TEXT, DATETIME, REPEAT = range(3)

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
    conn.commit()
    conn.close()


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


# ---------------------- ПЛАНИРОВЩИК ----------------------

scheduler = AsyncIOScheduler(timezone=DEFAULT_TIMEZONE)


def compute_next_run(repeat_type: str, base: datetime) -> datetime | None:
    tz = pytz.timezone(DEFAULT_TIMEZONE)
    if repeat_type == "daily":
        return base + timedelta(days=1)
    if repeat_type == "weekly":
        return base + timedelta(weeks=1)
    if repeat_type == "monthly":
        # приблизительно +1 месяц
        try:
            return base.replace(month=base.month + 1)
        except ValueError:
            return base.replace(month=1, year=base.year + 1)
    return None


async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    reminder_id = job.data["reminder_id"]
    chat_id = job.data["chat_id"]
    text = job.data["text"]
    repeat_type = job.data["repeat_type"]

    try:
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ Напоминание:\n{text}")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
        return

    if repeat_type == "none":
        delete_reminder_by_id(reminder_id)
        return

    # Для повторяющихся пересчитываем следующий запуск
    now = datetime.now(pytz.timezone(DEFAULT_TIMEZONE))
    next_run = compute_next_run(repeat_type, now)
    if next_run:
        update_next_run(reminder_id, next_run)
        schedule_reminder(
            context.application,
            reminder_id,
            chat_id,
            text,
            next_run,
            repeat_type,
        )


def schedule_reminder(app: Application, reminder_id: int, chat_id: int, text: str, run_time: datetime, repeat_type: str):
    job_id = f"reminder_{reminder_id}_{run_time.timestamp()}"
    scheduler.add_job(
        send_reminder_job,
        trigger=DateTrigger(run_date=run_time),
        id=job_id,
        replace_existing=True,
        data={
            "reminder_id": reminder_id,
            "chat_id": chat_id,
            "text": text,
            "repeat_type": repeat_type,
        },
        job_kwargs={"misfire_grace_time": 3600},
    )


async def load_reminders_to_scheduler(app: Application):
    """Загружает все будущие напоминания из БД в планировщик при старте."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, chat_id, text, next_run, repeat_type FROM reminders")
    rows = c.fetchall()
    conn.close()

    now = datetime.now(pytz.utc)
    for row in rows:
        reminder_id, chat_id, text, next_run_iso, repeat_type = row
        next_run = datetime.fromisoformat(next_run_iso)
        # Если время уже прошло, либо отправляем сразу, либо пересчитываем для повторяющихся
        if next_run < now:
            if repeat_type == "none":
                delete_reminder_by_id(reminder_id)
                continue
            tz = pytz.timezone(DEFAULT_TIMEZONE)
            next_run = compute_next_run(repeat_type, datetime.now(tz))
            update_next_run(reminder_id, next_run)
        schedule_reminder(app, reminder_id, chat_id, text, next_run, repeat_type)


# ---------------------- ОБРАБОТЧИКИ ----------------------

START_TEXT = (
    "Привет! Я бот для напоминаний.\n\n"
    "Команды:\n"
    "/add — добавить напоминание\n"
    "/list — список напоминаний\n"
    "/delete — удалить напоминание\n"
    "/help — помощь"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Формат добавления:\n"
        "1. Введите текст напоминания\n"
        "2. Введите дату и время в формате: ДД.ММ.ГГГГ ЧЧ:ММ\n"
        "   Пример: 15.07.2026 14:30\n"
        "3. Выберите периодичность: разово, ежедневно, еженедельно, ежемесячно\n\n"
        f"Часовой пояс по умолчанию: {DEFAULT_TIMEZONE}"
    )


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите текст напоминания:")
    return TEXT


async def add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["reminder_text"] = update.message.text
    await update.message.reply_text(
        "Введите дату и время напоминания в формате:\n"
        "<b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n\n"
        "Пример: <code>15.07.2026 14:30</code>",
        parse_mode="HTML",
    )
    return DATETIME


async def add_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        naive = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Введите дату и время так:\n<code>15.07.2026 14:30</code>",
            parse_mode="HTML",
        )
        return DATETIME

    tz = pytz.timezone(DEFAULT_TIMEZONE)
    localized = tz.localize(naive)
    if localized < datetime.now(tz):
        await update.message.reply_text(
            "Указанное время уже прошло. Введите будущую дату и время:"
        )
        return DATETIME

    context.user_data["reminder_time"] = localized

    keyboard = [
        [InlineKeyboardButton("Разово", callback_data="none")],
        [InlineKeyboardButton("Ежедневно", callback_data="daily")],
        [InlineKeyboardButton("Еженедельно", callback_data="weekly")],
        [InlineKeyboardButton("Ежемесячно", callback_data="monthly")],
    ]
    await update.message.reply_text(
        "Выберите периодичность:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return REPEAT


async def add_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    repeat_type = query.data
    text = context.user_data["reminder_text"]
    run_time = context.user_data["reminder_time"]

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    reminder_id = add_reminder(user_id, chat_id, text, run_time, repeat_type)
    schedule_reminder(context.application, reminder_id, chat_id, text, run_time, repeat_type)

    repeat_labels = {
        "none": "разово",
        "daily": "ежедневно",
        "weekly": "еженедельно",
        "monthly": "ежемесячно",
    }

    await query.edit_message_text(
        f"✅ Напоминание установлено!\n\n"
        f"📝 Текст: {text}\n"
        f"📅 Время: {run_time.strftime('%d.%m.%Y %H:%M')}\n"
        f"🔄 Периодичность: {repeat_labels[repeat_type]}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добавление напоминания отменено.")
    return ConversationHandler.END


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_reminders(user_id)
    if not rows:
        await update.message.reply_text("У вас нет активных напоминаний.")
        return

    lines = ["Ваши напоминания:\n"]
    repeat_labels = {
        "none": "разово",
        "daily": "ежедневно",
        "weekly": "еженедельно",
        "monthly": "ежемесячно",
    }
    for rid, text, next_run_iso, repeat_type in rows:
        next_run = datetime.fromisoformat(next_run_iso).astimezone(pytz.timezone(DEFAULT_TIMEZONE))
        lines.append(
            f"<b>#{rid}</b>\n"
            f"📝 {text}\n"
            f"📅 {next_run.strftime('%d.%m.%Y %H:%M')}\n"
            f"🔄 {repeat_labels.get(repeat_type, repeat_type)}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_reminders(user_id)
    if not rows:
        await update.message.reply_text("У вас нет активных напоминаний для удаления.")
        return ConversationHandler.END

    keyboard = []
    for rid, text, _, _ in rows:
        label = f"#{rid}: {text[:30]}{'...' if len(text) > 30 else ''}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"del_{rid}")])

    await update.message.reply_text(
        "Выберите напоминание для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return "CHOOSE_DELETE"


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("del_"):
        return ConversationHandler.END

    reminder_id = int(data.split("_")[1])
    user_id = update.effective_user.id

    if delete_reminder(reminder_id, user_id):
        # Удаляем запланированные job'ы для этого reminder_id
        for job in scheduler.get_jobs():
            if job.data and job.data.get("reminder_id") == reminder_id:
                job.remove()
        await query.edit_message_text("✅ Напоминание удалено.")
    else:
        await query.edit_message_text("Не удалось удалить напоминание.")
    return ConversationHandler.END


async def delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Удаление отменено.")
    return ConversationHandler.END


# ---------------------- ЗАПУСК ----------------------

def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_text)],
            DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_datetime)],
            REPEAT: [CallbackQueryHandler(add_repeat, pattern="^(none|daily|weekly|monthly)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    delete_conv = ConversationHandler(
        entry_points=[CommandHandler("delete", delete_start)],
        states={
            "CHOOSE_DELETE": [CallbackQueryHandler(delete_confirm, pattern="^del_\\d+$")],
        },
        fallbacks=[CommandHandler("cancel", delete_cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_reminders))
    application.add_handler(conv_handler)
    application.add_handler(delete_conv)

    scheduler.start()
    application.post_init = load_reminders_to_scheduler

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
