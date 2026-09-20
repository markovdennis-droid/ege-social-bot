import asyncio
import os
import random
import psycopg
from datetime import date, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
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

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else None

bot = Bot(TOKEN)
dp = Dispatcher()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("Не задан DATABASE_URL для PostgreSQL. Добавь его в Railway.")
ACTIVE = {}

AD_IMAGE = Path(__file__).resolve().parent / "assets" / "ad.jpg"
AD_URL = "https://vk.ru/allateach"
AD_EVERY = 5

BOT_DESCRIPTION = (
    '👋 Привет! Я бот-тренажёр для подготовки к ЕГЭ по обществознанию.\n'
    '\n'
    '📚 1100 авторских вопросов по темам: человек и общество, экономика, социальные отношения, политика и право.\n'
    '\n'
    'Я помогу тебе:\n'
    '🎯 Тренироваться по 5 или 10 вопросов.\n'
    '📖 Разбирать правильные ответы и объяснения.\n'
    '🔁 Повторять вопросы, в которых были ошибки.\n'
    '📊 Следить за результатами и серией дней занятий.\n'
    '\n'
    'Нажми «Запустить» — и начнём!'
)

BOT_SHORT_DESCRIPTION = 'Подготовка к ЕГЭ по обществознанию: 1100 вопросов, объяснения, повтор ошибок и статистика.'


def db():
    """Новое подключение; вызывающий код закрывает его через with."""
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=10,
        options="-c statement_timeout=15000",
    )


def init_db():
    """Создаёт таблицы один раз при запуске, не очищая статистику."""
    with db() as conn:
        conn.execute('CREATE SCHEMA IF NOT EXISTS ege_bot;\nCREATE TABLE IF NOT EXISTS ege_bot.users (\n    user_id BIGINT PRIMARY KEY,\n    total BIGINT DEFAULT 0,\n    correct BIGINT DEFAULT 0,\n    streak INTEGER DEFAULT 0,\n    last_day TEXT\n);\nCREATE TABLE IF NOT EXISTS ege_bot.mistakes (\n    user_id BIGINT,\n    question_id INTEGER,\n    wrong_count BIGINT DEFAULT 1,\n    needs_review INTEGER DEFAULT 1,\n    last_wrong_option INTEGER,\n    last_wrong_at TEXT,\n    reviewed_correctly BIGINT DEFAULT 0,\n    PRIMARY KEY (user_id, question_id)\n);')


def ensure_user(user_id: int):
    with db() as conn:
        conn.execute('INSERT INTO ege_bot.users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING', (user_id,))


def update_streak(user_id: int):
    today = date.today()
    with db() as conn:
        row = conn.execute('SELECT streak, last_day FROM ege_bot.users WHERE user_id=%s FOR UPDATE', (user_id,)).fetchone()
        streak, last_day = row if row else (0, None)
        if last_day == today.isoformat():
            return
        if last_day == (today - timedelta(days=1)).isoformat():
            streak += 1
        else:
            streak = 1
        conn.execute('UPDATE ege_bot.users SET streak=%s, last_day=%s WHERE user_id=%s', (streak, today.isoformat(), user_id))


def record_answer(user_id: int, question_id: int, is_correct: bool, chosen_option: int | None=None, review_mode: bool=False):
    with db() as conn:
        conn.execute('UPDATE ege_bot.users SET total=total+1 WHERE user_id=%s', (user_id,))
        if is_correct:
            conn.execute('UPDATE ege_bot.users SET correct=correct+1 WHERE user_id=%s', (user_id,))
            if review_mode:
                conn.execute('\n                UPDATE ege_bot.mistakes\n                SET needs_review=0,\n                    reviewed_correctly=reviewed_correctly+1\n                WHERE user_id=%s AND question_id=%s\n            ', (user_id, question_id))
        else:
            conn.execute("\n            INSERT INTO ege_bot.mistakes(\n                user_id,\n                question_id,\n                wrong_count,\n                needs_review,\n                last_wrong_option,\n                last_wrong_at,\n                reviewed_correctly\n            )\n            VALUES (%s, %s, 1, 1, %s, to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), 0)\n            ON CONFLICT(user_id, question_id)\n            DO UPDATE SET\n                wrong_count=mistakes.wrong_count+1,\n                needs_review=1,\n                last_wrong_option=excluded.last_wrong_option,\n                last_wrong_at=to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')\n        ", (user_id, question_id, chosen_option))


def get_stats(user_id: int):
    with db() as conn:
        row = conn.execute('SELECT total, correct, streak FROM ege_bot.users WHERE user_id=%s', (user_id,)).fetchone()
        return row or (0, 0, 0)


def get_admin_stats():
    with db() as conn:
        users = conn.execute('SELECT COUNT(*) FROM ege_bot.users').fetchone()[0]
        active_today = conn.execute('SELECT COUNT(*) FROM ege_bot.users WHERE last_day=%s', (date.today().isoformat(),)).fetchone()[0]
        total, correct = conn.execute('SELECT COALESCE(SUM(total), 0), COALESCE(SUM(correct), 0) FROM ege_bot.users').fetchone()
        mistakes = conn.execute('SELECT COALESCE(SUM(wrong_count), 0) FROM ege_bot.mistakes').fetchone()[0]
        accuracy = round(correct / total * 100) if total else 0
        return {'users': users, 'active_today': active_today, 'total': total, 'correct': correct, 'mistakes': mistakes, 'accuracy': accuracy}


def get_mistake_questions(user_id: int, only_active: bool=True):
    with db() as conn:
        if only_active:
            rows = conn.execute('\n            SELECT question_id\n            FROM ege_bot.mistakes\n            WHERE user_id=%s AND needs_review=1\n            ORDER BY wrong_count DESC, last_wrong_at DESC\n        ', (user_id,)).fetchall()
        else:
            rows = conn.execute('\n            SELECT question_id\n            FROM ege_bot.mistakes\n            WHERE user_id=%s\n            ORDER BY needs_review DESC, wrong_count DESC, last_wrong_at DESC\n        ', (user_id,)).fetchall()
        mapping = {q['id']: q for q in QUESTIONS}
        return [mapping[row[0]] for row in rows if row[0] in mapping]


def get_mistake_summary(user_id: int):
    with db() as conn:
        total_unique = conn.execute('SELECT COUNT(*) FROM ege_bot.mistakes WHERE user_id=%s', (user_id,)).fetchone()[0]
        active_unique = conn.execute('SELECT COUNT(*) FROM ege_bot.mistakes WHERE user_id=%s AND needs_review=1', (user_id,)).fetchone()[0]
        total_wrong = conn.execute('SELECT COALESCE(SUM(wrong_count), 0) FROM ege_bot.mistakes WHERE user_id=%s', (user_id,)).fetchone()[0]
        rows = conn.execute('\n        SELECT question_id, wrong_count, needs_review\n        FROM ege_bot.mistakes\n        WHERE user_id=%s\n        ORDER BY needs_review DESC, wrong_count DESC, last_wrong_at DESC\n    ', (user_id,)).fetchall()
        mapping = {q['id']: q for q in QUESTIONS}
        by_topic = {}
        for question_id, wrong_count, needs_review in rows:
            question = mapping.get(question_id)
            if not question:
                continue
            topic = question['topic']
            data = by_topic.setdefault(topic, {'active': 0, 'questions': 0, 'wrong_answers': 0})
            data['questions'] += 1
            data['wrong_answers'] += wrong_count
            if needs_review:
                data['active'] += 1
        return {'total_unique': total_unique, 'active_unique': active_unique, 'total_wrong': total_wrong, 'by_topic': by_topic}


def main_menu(user_id: int | None = None):
    rows = [
        [KeyboardButton(text="🎯 5 вопросов"), KeyboardButton(text="🧪 10 вопросов")],
        [KeyboardButton(text="📚 По теме"), KeyboardButton(text="📊 Мой прогресс")],
        [KeyboardButton(text="❌ Мои ошибки"), KeyboardButton(text="🔁 Повтор ошибок")],
    ]
    if ADMIN_ID is not None and user_id == ADMIN_ID:
        rows.append([KeyboardButton(text="📈 Статистика бота")])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def answer_keyboard(question):
    # Длинный текст варианта не помещаем в кнопку:
    # Telegram-клиент может визуально обрезать его троеточием.
    # На кнопках оставляем только буквы, а полный текст выводим в сообщении.
    buttons = []
    for i, _option in enumerate(question["options"]):
        letter = chr(65 + i)
        buttons.append(
            InlineKeyboardButton(
                text=letter,
                callback_data=f"ans:{question['id']}:{i}"
            )
        )

    # Для 4 вариантов — две строки по две кнопки.
    # Для 2 вариантов — одна строка.
    if len(buttons) == 4:
        rows = [buttons[:2], buttons[2:]]
    else:
        rows = [buttons]

    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_text(question, number=None, total=None):
    prefix = ""
    if number is not None and total is not None:
        prefix = f"🧠 Вопрос {number}/{total}\n"

    # Полностью выводим каждый вариант в тексте сообщения.
    # Никакого ручного сокращения и троеточий.
    options_text = "\n\n".join(
        f"{chr(65 + i)}. {option}"
        for i, option in enumerate(question["options"])
    )

    return (
        f"{prefix}"
        f"📌 Тема: {question['topic']}\n\n"
        f"{question['question']}\n\n"
        f"{options_text}\n\n"
        f"👇 Выбери букву ответа:"
    )


async def send_ad(chat_id: int):
    """Показывает фото с приглашением подписаться на страницу ВКонтакте."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="👉 Перейти во ВКонтакте",
                url=AD_URL,
            )]
        ]
    )
    await bot.send_photo(
        chat_id=chat_id,
        photo=FSInputFile(AD_IMAGE),
        caption=(
            "📚 Подписывайтесь на страницу ВКонтакте и следите за новостями!\n\n"
            f"{AD_URL}"
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
        await asyncio.to_thread(update_streak, user_id)
        await bot.send_message(
            chat_id,
            f"✅ Тренировка закончена!\n\n"
            f"Результат: {score}/{total}\n"
            f"Нажми «📊 Мой прогресс», чтобы посмотреть общую статистику.",
            reply_markup=main_menu(user_id),
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
    await asyncio.to_thread(ensure_user, message.from_user.id)
    await message.answer(
        "👋 Привет! Я тренажёр ЕГЭ по обществознанию.\n\n"
        "📚 В базе 1100 авторских тренировочных вопросов "
        "с готовыми ответами и объяснениями.\n\n"

        "Что можно делать:\n"
        "🎯 5 вопросов — быстрая тренировка\n"
        "🧪 10 вопросов — расширенный тест\n"
        "📚 По теме — тренировка по выбранному блоку\n"
        "❌ Мои ошибки — посмотреть свои слабые места\n"
        "🔁 Повтор ошибок — отдельно отработать только ошибки\n"
        "📊 Мой прогресс — увидеть статистику, точность и серию дней\n\n"

        "Почему это полезно:\n"
        "• короткие тренировки удобно проходить каждый день;\n"
        "• неправильные ответы сохраняются и не теряются;\n"
        "• можно отдельно повторять проблемные вопросы;\n"
        "• статистика помогает видеть, где ошибок больше всего;\n"
        "• объяснение после ответа помогает сразу закрепить материал.\n\n"

        "👇 Выбери режим и начинай тренировку.",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(F.text == "🎯 5 вопросов")
async def training(message: Message):
    await asyncio.to_thread(ensure_user, message.from_user.id)
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
    await asyncio.to_thread(ensure_user, message.from_user.id)
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
    await asyncio.to_thread(ensure_user, message.from_user.id)
    summary = await asyncio.to_thread(get_mistake_summary, message.from_user.id)

    if summary["total_unique"] == 0:
        await message.answer(
            "У тебя пока нет сохранённых ошибок.\n\n"
            "Неправильные ответы будут автоматически попадать сюда.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    lines = [
        "❌ Мои ошибки",
        "",
        f"Нужно повторить: {summary['active_unique']}",
        f"Разных вопросов с ошибками за всё время: {summary['total_unique']}",
        f"Всего неправильных ответов: {summary['total_wrong']}",
        "",
        "По темам:",
    ]

    for topic, data in summary["by_topic"].items():
        lines.append(
            f"• {topic}: к повтору — {data['active']}; "
            f"ошибочных вопросов — {data['questions']}; "
            f"ошибок — {data['wrong_answers']}"
        )

    if summary["active_unique"] > 0:
        lines.extend([
            "",
            "Чтобы отработать их отдельно, нажми «🔁 Повтор ошибок».",
        ])
    else:
        lines.extend([
            "",
            "🎉 Все сохранённые ошибки сейчас отработаны.",
        ])

    await message.answer(
        "\n".join(lines),
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(F.text == "🔁 Повтор ошибок")
async def review_mistakes(message: Message):
    await asyncio.to_thread(ensure_user, message.from_user.id)
    questions = await asyncio.to_thread(get_mistake_questions, message.from_user.id, only_active=True)

    if not questions:
        await message.answer(
            "🎉 Сейчас нет ошибок, которые нужно повторить.\n\n"
            "Новые неправильные ответы автоматически появятся в этом разделе.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    selected = questions[:10]
    ACTIVE[message.from_user.id] = {
        "questions": selected,
        "index": 0,
        "score": 0,
        "last_ad_index": None,
        "review_mode": True,
    }

    await message.answer(
        f"🔁 Повтор ошибок\n\n"
        f"В этой тренировке: {len(selected)}.\n"
        f"Ответишь правильно — ошибка будет помечена как отработанная.\n"
        f"Ошибёшься снова — она останется в повторе."
    )
    await send_next(message.from_user.id, message.chat.id)


@dp.message(F.text == "📊 Мой прогресс")
async def user_stats(message: Message):
    await asyncio.to_thread(ensure_user, message.from_user.id)
    total, correct, streak = await asyncio.to_thread(get_stats, message.from_user.id)
    percent = round(correct / total * 100) if total else 0
    await message.answer(
        f"📊 Твой прогресс\n\n"
        f"Решено: {total}\n"
        f"Правильно: {correct}\n"
        f"Точность: {percent}%\n"
        f"🔥 Серия дней: {streak}",
        reply_markup=main_menu(message.from_user.id),
    )


async def send_admin_stats(message: Message):
    if ADMIN_ID is None:
        await message.answer(
            "⚠️ ADMIN_ID не настроен. Добавь числовой Telegram ID "
            "в переменные Railway."
        )
        return

    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Эта команда доступна только администратору.")
        return

    stats = await asyncio.to_thread(get_admin_stats)
    await message.answer(
        "📊 Статистика бота\n\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"🟢 Завершили тренировку сегодня: {stats['active_today']}\n"
        f"📝 Ответов всего: {stats['total']}\n"
        f"✅ Правильных ответов: {stats['correct']}\n"
        f"❌ Ошибок: {stats['mistakes']}\n"
        f"🎯 Общая точность: {stats['accuracy']}%",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(Command("stats"))
async def admin_stats_command(message: Message):
    await send_admin_stats(message)


@dp.message(F.text == "📊 Статистика")
@dp.message(F.text == "📈 Статистика бота")
async def admin_stats_button(message: Message):
    await send_admin_stats(message)


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
    await asyncio.to_thread(record_answer, user_id, q['id'], is_correct, chosen_option=chosen, review_mode=session.get('review_mode', False))

    if is_correct:
        session["score"] += 1
        result = "✅ Правильно!"
    else:
        correct_letter = chr(65 + q["correct"])
        correct_text = q["options"][q["correct"]]
        result = f"❌ Неправильно.\nПравильный ответ полностью: {correct_letter}. {correct_text}"

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"{result}\n\n💡 {q['explanation']}"
    )

    if session.get("review_mode", False) and is_correct:
        await callback.message.answer(
            "✅ Эта ошибка отработана и убрана из активного повтора."
        )

    session["index"] += 1
    await callback.answer()
    await send_next(user_id, callback.message.chat.id)


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Используй кнопки меню 👇",
        reply_markup=main_menu(message.from_user.id),
    )


async def configure_bot_description(client: Bot):
    """Описание для пустого чата до /start и краткий текст профиля."""
    # Обновляем общий вариант и русский: локализованный текст имеет приоритет.
    for language in ("", "ru"):
        await client.set_my_description(
            description=BOT_DESCRIPTION,
            language_code=language,
        )
        await client.set_my_short_description(
            short_description=BOT_SHORT_DESCRIPTION,
            language_code=language,
        )


async def main():
    await asyncio.to_thread(init_db)
    try:
        await configure_bot_description(bot)
        print("Описание бота в Telegram обновлено.", flush=True)
    except TelegramAPIError as error:
        # Сбой настройки профиля не должен останавливать тренировки.
        print(
            f"Не удалось обновить описание Telegram ({type(error).__name__}). "
            "Повторите настройку позже или через BotFather.",
            flush=True,
        )
    print("Бот запущен. Статистика хранится в PostgreSQL.", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
