import asyncio
import os
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
)
from dotenv import load_dotenv

from questions import QUESTIONS

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Создай файл .env и вставь туда токен.")

bot = Bot(TOKEN)
dp = Dispatcher()

DB_PATH = "progress.db"
ACTIVE = {}

AD_IMAGE = Path(__file__).resolve().parent / "assets" / "ad.jpg"
AD_URL = "https://egetraining.com"
AD_EVERY = 5


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            total INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0,
            last_day TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mistakes (
            user_id INTEGER,
            question_id INTEGER,
            wrong_count INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, question_id)
        )
    """)
    conn.commit()
    return conn


def ensure_user(user_id: int):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def update_streak(user_id: int):
    today = date.today()
    conn = db()
    row = conn.execute("SELECT streak, last_day FROM users WHERE user_id=?", (user_id,)).fetchone()
    streak, last_day = row if row else (0, None)

    if last_day == today.isoformat():
        conn.close()
        return

    if last_day == (today - timedelta(days=1)).isoformat():
        streak += 1
    else:
        streak = 1

    conn.execute(
        "UPDATE users SET streak=?, last_day=? WHERE user_id=?",
        (streak, today.isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def record_answer(user_id: int, question_id: int, is_correct: bool):
    conn = db()
    conn.execute("UPDATE users SET total=total+1 WHERE user_id=?", (user_id,))
    if is_correct:
        conn.execute("UPDATE users SET correct=correct+1 WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM mistakes WHERE user_id=? AND question_id=?", (user_id, question_id))
    else:
        conn.execute("""
            INSERT INTO mistakes(user_id, question_id, wrong_count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, question_id)
            DO UPDATE SET wrong_count=wrong_count+1
        """, (user_id, question_id))
    conn.commit()
    conn.close()


def get_stats(user_id: int):
    conn = db()
    row = conn.execute(
        "SELECT total, correct, streak FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row or (0, 0, 0)


def get_mistake_questions(user_id: int):
    conn = db()
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT question_id FROM mistakes WHERE user_id=? ORDER BY wrong_count DESC",
            (user_id,),
        ).fetchall()
    ]
    conn.close()
    mapping = {q["id"]: q for q in QUESTIONS}
    return [mapping[i] for i in ids if i in mapping]


menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎯 5 вопросов"), KeyboardButton(text="🧪 10 вопросов")],
        [KeyboardButton(text="📚 По теме"), KeyboardButton(text="❌ Мои ошибки")],
        [KeyboardButton(text="📊 Мой прогресс")],
    ],
    resize_keyboard=True,
)


def answer_keyboard(question):
    rows = []
    for i, option in enumerate(question["options"]):
        letter = chr(65 + i)
        rows.append([
            InlineKeyboardButton(
                text=f"{letter}. {option}",
                callback_data=f"ans:{question['id']}:{i}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_text(question, number=None, total=None):
    prefix = ""
    if number is not None and total is not None:
        prefix = f"🧠 Вопрос {number}/{total}\n"
    return (
        f"{prefix}"
        f"📌 Тема: {question['topic']}\n\n"
        f"{question['question']}"
    )


async def send_ad(chat_id: int):
    """Показывает рекламный блок с картинкой и кнопкой."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Перейти на egetraining.com", url=AD_URL)]
        ]
    )
    await bot.send_photo(
        chat_id=chat_id,
        photo=FSInputFile(AD_IMAGE),
        caption=(
            "📚 Тренажёр ЕГЭ по обществознанию\n"
            "1000+ авторских заданий для подготовки.\n\n"
            "👇 Подробнее на сайте"
        ),
        reply_markup=keyboard,
    )


async def send_next(user_id: int, chat_id: int):
    session = ACTIVE.get(user_id)
    if not session:
        return

    idx = session["index"]
    questions = session["questions"]

    if idx >= len(questions):
        score = session["score"]
        total = len(questions)
        ACTIVE.pop(user_id, None)
        update_streak(user_id)
        await bot.send_message(
            chat_id,
            f"✅ Тренировка закончена!\n\n"
            f"Результат: {score}/{total}\n"
            f"Нажми «📊 Мой прогресс», чтобы посмотреть общую статистику.",
            reply_markup=menu,
        )
        return

    # Реклама идёт перед 1-м вопросом и затем перед 6-м, 11-м, 16-м...
    # last_ad_index защищает от повторной отправки рекламы,
    # если send_next случайно будет вызван дважды на одном шаге.
    if idx % AD_EVERY == 0 and session.get("last_ad_index") != idx:
        await send_ad(chat_id)
        session["last_ad_index"] = idx

    q = questions[idx]
    await bot.send_message(
        chat_id,
        question_text(q, idx + 1, len(questions)),
        reply_markup=answer_keyboard(q),
    )


@dp.message(CommandStart())
async def start(message: Message):
    ensure_user(message.from_user.id)
    await message.answer(
        "Привет! Я бесплатный тренажёр по обществознанию для ЕГЭ.\n\n"
        "Здесь нет платного ИИ: вопросы, ответы и объяснения заранее записаны в базе.\n\n"
        "Начни с «🎯 5 вопросов» или выбери «📚 По теме».",
        reply_markup=menu,
    )


@dp.message(F.text == "🎯 5 вопросов")
async def training(message: Message):
    ensure_user(message.from_user.id)
    selected = random.sample(QUESTIONS, min(5, len(QUESTIONS)))
    ACTIVE[message.from_user.id] = {
        "questions": selected,
        "index": 0,
        "score": 0,
        "last_ad_index": None,
    }
    await send_next(message.from_user.id, message.chat.id)




@dp.message(F.text == "🧪 10 вопросов")
async def ten_questions(message: Message):
    ensure_user(message.from_user.id)
    selected = random.sample(QUESTIONS, min(10, len(QUESTIONS)))
    ACTIVE[message.from_user.id] = {
        "questions": selected,
        "index": 0,
        "score": 0,
        "last_ad_index": None,
    }
    await send_next(message.from_user.id, message.chat.id)


@dp.message(F.text == "❌ Мои ошибки")
async def mistakes(message: Message):
    ensure_user(message.from_user.id)
    qs = get_mistake_questions(message.from_user.id)

    if not qs:
        await message.answer(
            "Пока сохранённых ошибок нет. Пройди тренировку — неправильные ответы появятся здесь.",
            reply_markup=menu,
        )
        return

    selected = qs[:5]
    ACTIVE[message.from_user.id] = {
        "questions": selected,
        "index": 0,
        "score": 0,
    }
    await message.answer("Разберём твои ошибки ещё раз 👇")
    await send_next(message.from_user.id, message.chat.id)


@dp.message(F.text == "📊 Мой прогресс")
async def stats(message: Message):
    ensure_user(message.from_user.id)
    total, correct, streak = get_stats(message.from_user.id)
    percent = round(correct / total * 100) if total else 0
    await message.answer(
        f"📊 Твой прогресс\n\n"
        f"Решено: {total}\n"
        f"Правильно: {correct}\n"
        f"Точность: {percent}%\n"
        f"🔥 Серия дней: {streak}",
        reply_markup=menu,
    )


@dp.message(F.text == "📚 По теме")
async def topics(message: Message):
    topic_names = sorted({q["topic"] for q in QUESTIONS})
    rows = [[InlineKeyboardButton(text=t, callback_data=f"topic:{i}")] for i, t in enumerate(topic_names)]
    await message.answer("Выбери блок. Я дам 5 вопросов только по нему:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("topic:"))
async def topic_training(callback: CallbackQuery):
    topic_names = sorted({q["topic"] for q in QUESTIONS})
    try:
        topic = topic_names[int(callback.data.split(":")[1])]
    except (ValueError, IndexError):
        await callback.answer("Не удалось выбрать тему.")
        return
    pool = [q for q in QUESTIONS if q["topic"] == topic]
    selected = random.sample(pool, min(5, len(pool)))
    ACTIVE[callback.from_user.id] = {
        "questions": selected,
        "index": 0,
        "score": 0,
        "last_ad_index": None,
    }
    await callback.answer()
    await callback.message.answer(f"📚 Тема: {topic}")
    await send_next(callback.from_user.id, callback.message.chat.id)


@dp.callback_query(F.data.startswith("ans:"))
async def answer(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = ACTIVE.get(user_id)

    if not session:
        await callback.answer("Эта тренировка уже закончена.")
        return

    _, qid_s, answer_s = callback.data.split(":")
    qid = int(qid_s)
    chosen = int(answer_s)

    idx = session["index"]
    q = session["questions"][idx]

    if q["id"] != qid:
        await callback.answer("Этот вопрос уже не активен.")
        return

    is_correct = chosen == q["correct"]
    record_answer(user_id, q["id"], is_correct)

    if is_correct:
        session["score"] += 1
        result = "✅ Правильно!"
    else:
        correct_letter = chr(65 + q["correct"])
        correct_text = q["options"][q["correct"]]
        result = f"❌ Неправильно.\nПравильный ответ: {correct_letter}. {correct_text}"

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"{result}\n\n💡 {q['explanation']}"
    )

    session["index"] += 1
    await callback.answer()
    await send_next(user_id, callback.message.chat.id)


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Используй кнопки меню 👇",
        reply_markup=menu,
    )


async def main():
    print("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
