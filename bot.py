"""
Бот приёма заявок на дизайн для медиа ВУЗа.

Что умеет:
- студент оставляет заявку (тип, описание, дедлайн, контакт);
- заявка приходит в чат админа с кнопками «Взять в работу» / «Готово»;
- заказчик получает уведомления о смене статуса;
- все заявки хранятся в базе SQLite (файл requests.db).
"""

import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

# ---------- Настройки ----------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

DB_PATH = BASE_DIR / "requests.db"
MAX_FIELD_LEN = 1500  # лимит сообщения в Telegram — 4096 символов

DESIGN_TYPES = {
    "post": "📱 Пост",
    "poster": "🖼 Афиша",
    "presentation": "📊 Презентация",
    "other": "✨ Другое",
}

STATUS_TEXT = {
    "new": "🆕 Новая",
    "in_work": "🛠 В работе",
    "done": "✅ Готово",
}

dp = Dispatcher()


# ---------- База данных ----------
def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                design_type TEXT,
                description TEXT,
                deadline TEXT,
                contact TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT
            )
            """
        )


def db_add_request(data: dict) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT INTO requests
               (user_id, username, design_type, description, deadline, contact, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data["user_id"],
                data["username"],
                data["design_type"],
                data["description"],
                data["deadline"],
                data["contact"],
                datetime.now().strftime("%d.%m.%Y %H:%M"),
            ),
        )
        return cur.lastrowid


def db_get_request(request_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()


def db_set_status(request_id: int, status: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))


def db_user_requests(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,)
        ).fetchall()


# ---------- Клавиатуры ----------
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Новая заявка")],
        [KeyboardButton(text="📋 Мои заявки"), KeyboardButton(text="ℹ️ О нас")],
    ],
    resize_keyboard=True,
)

cancel_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True
)


def types_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"type:{key}")]
        for key, title in DESIGN_TYPES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


confirm_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="🔄 Заново", callback_data="confirm:no"),
        ]
    ]
)


def admin_keyboard(request_id: int, status: str) -> InlineKeyboardMarkup | None:
    if status == "new":
        btn = InlineKeyboardButton(text="🛠 Взять в работу", callback_data=f"status:{request_id}:in_work")
    elif status == "in_work":
        btn = InlineKeyboardButton(text="✅ Готово", callback_data=f"status:{request_id}:done")
    else:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


# ---------- Шаги заявки (машина состояний) ----------
class RequestForm(StatesGroup):
    design_type = State()
    description = State()
    deadline = State()
    contact = State()
    confirm = State()


def esc(value) -> str:
    """Экранирует текст пользователя: иначе символы < > & ломают HTML-разметку и отправку."""
    return html.escape(str(value or ""))


def format_request(data, request_id: int | None = None, status: str | None = None) -> str:
    head = f"<b>Заявка №{request_id}</b>\n" if request_id else "<b>Проверь заявку:</b>\n"
    text = (
        f"{head}\n"
        f"<b>Тип:</b> {DESIGN_TYPES.get(data['design_type'], data['design_type'])}\n"
        f"<b>Описание:</b> {esc(data['description'])}\n"
        f"<b>Дедлайн:</b> {esc(data['deadline'])}\n"
        f"<b>Контакт:</b> {esc(data['contact'])}"
    )
    if status:
        text += f"\n<b>Статус:</b> {STATUS_TEXT[status]}"
    return text


# ---------- Обработчики ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Я бот медиа ВУЗа. Через меня можно заказать дизайн: пост, афишу или презентацию.\n\n"
        "Нажми «📝 Новая заявка», чтобы начать.",
        reply_markup=main_menu,
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    # Нужна один раз при настройке: узнать ID чата для ADMIN_CHAT_ID
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@dp.message(F.text == "ℹ️ О нас")
async def about(message: Message):
    await message.answer(
        "Мы — медиа ВУЗа 🎨\n"
        "Делаем посты, афиши и презентации для студенческих мероприятий.\n\n"
        "Совет: оставляй заявку минимум за 3–5 дней до дедлайна."
    )


@dp.message(F.text == "📋 Мои заявки")
async def my_requests(message: Message):
    rows = db_user_requests(message.from_user.id)
    if not rows:
        await message.answer("У тебя пока нет заявок.")
        return
    lines = [
        f"№{r['id']} · {DESIGN_TYPES.get(r['design_type'])} · {STATUS_TEXT[r['status']]}"
        for r in rows
    ]
    await message.answer("<b>Твои заявки:</b>\n\n" + "\n".join(lines))


@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заявка отменена.", reply_markup=main_menu)


@dp.message(F.text == "📝 Новая заявка")
async def new_request(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(RequestForm.design_type)
    await message.answer("Шаг 1 из 4. Что нужно сделать?", reply_markup=cancel_menu)
    await message.answer("Выбери тип:", reply_markup=types_keyboard())


@dp.callback_query(RequestForm.design_type, F.data.startswith("type:"))
async def choose_type(callback: CallbackQuery, state: FSMContext):
    design_type = callback.data.split(":")[1]
    await state.update_data(design_type=design_type)
    await state.set_state(RequestForm.description)
    await callback.message.edit_text(f"Тип: {DESIGN_TYPES[design_type]}")
    await callback.message.answer(
        "Шаг 2 из 4. Опиши задачу: что за мероприятие, какой текст должен быть, "
        "пожелания по стилю, размеры."
    )
    await callback.answer()


@dp.message(RequestForm.description, F.text)
async def get_description(message: Message, state: FSMContext):
    if len(message.text) < 10:
        await message.answer("Опиши чуть подробнее, хотя бы пару предложений 🙂")
        return
    await state.update_data(description=message.text[:MAX_FIELD_LEN])
    await state.set_state(RequestForm.deadline)
    await message.answer("Шаг 3 из 4. Когда нужно готово? Например: «15 ноября» или «до пятницы».")


@dp.message(RequestForm.deadline, F.text)
async def get_deadline(message: Message, state: FSMContext):
    await state.update_data(deadline=message.text[:200])
    await state.set_state(RequestForm.contact)
    username = message.from_user.username
    hint = f"Или просто напиши «ок», и я укажу твой ник @{username}." if username else ""
    await message.answer(f"Шаг 4 из 4. Как с тобой связаться? Телефон или ник в Telegram.\n{hint}")


@dp.message(RequestForm.contact, F.text)
async def get_contact(message: Message, state: FSMContext):
    contact = message.text[:300]
    if contact.lower() in ("ок", "ok") and message.from_user.username:
        contact = f"@{message.from_user.username}"
    await state.update_data(contact=contact)
    await state.set_state(RequestForm.confirm)
    data = await state.get_data()
    await message.answer(format_request(data), reply_markup=confirm_keyboard)


@dp.callback_query(RequestForm.confirm, F.data == "confirm:no")
async def confirm_no(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RequestForm.design_type)
    await callback.message.edit_text("Начнём заново. Выбери тип:", reply_markup=types_keyboard())
    await callback.answer()


@dp.callback_query(RequestForm.confirm, F.data == "confirm:yes")
async def confirm_yes(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    data["user_id"] = callback.from_user.id
    data["username"] = callback.from_user.username or ""
    request_id = db_add_request(data)
    await state.clear()

    await callback.message.edit_text(format_request(data, request_id, "new"))
    await callback.message.answer(
        f"Заявка №{request_id} отправлена! 🎉 Я напишу, когда её возьмут в работу.",
        reply_markup=main_menu,
    )
    await callback.answer()

    if not ADMIN_CHAT_ID:
        logging.warning("ADMIN_CHAT_ID не указан: заявка №%s сохранена только в базе", request_id)
        return
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            format_request(data, request_id, "new"),
            reply_markup=admin_keyboard(request_id, "new"),
        )
    except Exception:
        # Частые причины: бота удалили из группы или неверный ADMIN_CHAT_ID
        logging.exception("Не удалось отправить заявку №%s в админ-чат", request_id)


@dp.callback_query(F.data.startswith("status:"))
async def change_status(callback: CallbackQuery, bot: Bot):
    if callback.message.chat.id != ADMIN_CHAT_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    _, request_id, new_status = callback.data.split(":")
    request_id = int(request_id)
    db_set_status(request_id, new_status)
    row = db_get_request(request_id)

    await callback.message.edit_text(
        format_request(row, request_id, new_status)
        + f"\n<i>Изменил: {esc(callback.from_user.full_name)}</i>",
        reply_markup=admin_keyboard(request_id, new_status),
    )
    await callback.answer("Статус обновлён")

    notify = {
        "in_work": f"🛠 Твою заявку №{request_id} взяли в работу!",
        "done": f"✅ Заявка №{request_id} готова! Скоро с тобой свяжутся.",
    }
    try:
        await bot.send_message(row["user_id"], notify[new_status])
    except Exception as e:
        logging.warning("Не удалось уведомить пользователя: %s", e)


@dp.message(RequestForm.description)
@dp.message(RequestForm.deadline)
@dp.message(RequestForm.contact)
async def need_text(message: Message):
    await message.answer("Пожалуйста, ответь текстом ✍️")


@dp.message(RequestForm.design_type)
async def need_type(message: Message):
    await message.answer("Выбери тип кнопкой под сообщением выше 👆 или нажми «❌ Отмена».")


@dp.message(RequestForm.confirm)
async def need_confirm(message: Message):
    await message.answer("Нажми «✅ Отправить» или «🔄 Заново» под заявкой 👆")


@dp.callback_query()
async def stale_button(callback: CallbackQuery):
    # Кнопка из старого сообщения: иначе у пользователя бесконечно крутятся часики
    await callback.answer("Эта кнопка уже неактуальна. Начни заново: «📝 Новая заявка»", show_alert=True)


@dp.message()
async def fallback(message: Message):
    if message.chat.type != "private":
        return  # в группе админов бот не отвечает на обычную переписку
    await message.answer("Чтобы заказать дизайн, нажми «📝 Новая заявка».", reply_markup=main_menu)


# ---------- Запуск ----------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не найден BOT_TOKEN. Создай файл .env (см. README).")
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    logging.basicConfig(level=logging.INFO)
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    print("Бот запущен! Нажми Ctrl+C, чтобы остановить.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
