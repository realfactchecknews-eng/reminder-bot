# Telegram-бот напоминаний

Простой бот для установки напоминаний прямо в Telegram. Поддерживает разовые и повторяющиеся напоминания (ежедневно, еженедельно, ежемесячно).

## Возможности

- `/start` — начало работы
- `/add` — добавить напоминание
- `/list` — список активных напоминаний
- `/delete` — удалить напоминание
- `/help` — помощь

## Деплой на BotHost.ru

1. Получите токен бота у [@BotFather](https://t.me/BotFather).
2. На BotHost.ru укажите:
   - **Ссылка на репозиторий**: `https://github.com/ВАШ_НИКНЕЙМ/reminder-bot`
   - **Файл запуска**: `main.py`
   - **Переменные окружения**:
     - `BOT_TOKEN` — токен от @BotFather
     - `DEFAULT_TIMEZONE` — часовой пояс (по умолчанию `Europe/Moscow`)
3. Запустите бота.

## Локальный запуск

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
export BOT_TOKEN="ваш_токен"
python main.py
```

## Переменные окружения

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `BOT_TOKEN` | Токен бота от @BotFather | — |
| `DEFAULT_TIMEZONE` | Часовой пояс для напоминаний | `Europe/Moscow` |
| `DB_PATH` | Путь к файлу SQLite | `database.db` |
