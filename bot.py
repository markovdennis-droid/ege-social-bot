import asyncio
import html
import os
import random
import time
from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from dotenv import load_dotenv

from questions import QUESTIONS

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Создай файл .env и вставь туда токен.")

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else None

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("Не задан DATABASE_URL для PostgreSQL. Добавь его в Railway.")

bot = Bot(TOKEN)
dp = Dispatcher()

FEEDBACK_TIMEOUT = 600      # сколько секунд бот ждёт текст пожелания после нажатия кнопки
FEEDBACK_COOLDOWN = 30      # минимальный интервал между пожеланиями одного ученика
FEEDBACK_MAX_LEN = 2000

BTN_FIVE = "🎯 5 вопросов"
BTN_TEN = "🧪 10 вопросов"
BTN_TOPIC = "📚 По теме"
BTN_PROGRESS = "📊 Мой прогресс"
BTN_MISTAKES = "❌ Мои ошибки"
BTN_REVIEW = "🔁 Повтор ошибок"
BTN_FEEDBACK = "💬 Пожелания"
BTN_EGE = "🎓 Как на ЕГЭ"
BTN_ADMIN = "📈 Статистика бота"
MENU_TEXTS = {BTN_FIVE, BTN_TEN, BTN_TOPIC, BTN_PROGRESS, BTN_MISTAKES,
              BTN_REVIEW, BTN_FEEDBACK, BTN_EGE, BTN_ADMIN, "📊 Статистика"}


# ---------------------------------------------------------------------------
# Подготовка банка вопросов
# ---------------------------------------------------------------------------

# Шаблоны «к какому блоку относится понятие» на ЕГЭ не встречаются,
# а в одном из них ответ совпадал с заголовком «Тема». Исключаем их из тренировок.
BLOCK_MARKERS = (
    "относится к блоку",
    "относятся к блоку",
    "К какому содержательному блоку",
)


def is_block_question(q) -> bool:
    return any(marker in q["question"] for marker in BLOCK_MARKERS)


def normalize_yes_no(q):
    """В вопросах «Верно ли суждение» всегда ставим «Да» первой кнопкой."""
    if q["options"] == ["Нет", "Да"]:
        q = dict(q)
        q["options"] = ["Да", "Нет"]
        q["correct"] = 1 - q["correct"]
    return q


POOL = [normalize_yes_no(q) for q in QUESTIONS if not is_block_question(q)]
QMAP = {q["id"]: q for q in POOL}
TOPICS = sorted({q["topic"] for q in POOL})

# Словарь «определение -> понятие» из заданий «Какому понятию соответствует определение».
DEFINITIONS = {}
for _q in POOL:
    if _q["question"].startswith("Какому понятию соответствует определение: «"):
        _text = _q["question"].split("«", 1)[1].rsplit("»", 1)[0]
        DEFINITIONS[_text] = _q["options"][_q["correct"]]
TERMS = set(DEFINITIONS.values())


def concepts_of(q) -> frozenset:
    """Какие понятия «раскрывает» вопрос: их определения или названия видны в тексте.
    Нужно, чтобы в одной тренировке один вопрос не подсказывал ответ на другой."""
    blob = q["question"] + "\n" + "\n".join(q["options"])
    found = {term for text, term in DEFINITIONS.items() if text in blob}
    for term in TERMS:
        if f"«{term}»" in q["question"] or f"«{term} — " in q["question"]:
            found.add(term)
        if any(opt == term or opt.startswith(f"{term} — ") for opt in q["options"]):
            found.add(term)
    return frozenset(found)


CONCEPTS = {q["id"]: concepts_of(q) for q in POOL}


def pick_questions(pool, n):
    """Случайные вопросы без пересечения по понятиям (пока это возможно)."""
    candidates = pool[:]
    random.shuffle(candidates)
    chosen, used = [], set()
    for q in candidates:
        if len(chosen) >= n:
            break
        if CONCEPTS[q["id"]] & used:
            continue
        chosen.append(q)
        used |= CONCEPTS[q["id"]]
    if len(chosen) < n:  # запасной вариант для очень маленьких выборок
        rest = [q for q in candidates if q not in chosen]
        chosen += rest[: n - len(chosen)]
    return chosen


# ---------------------------------------------------------------------------
# Режим «Как на ЕГЭ» (лайт): «Выберите верные суждения», ответ — цифры
# ---------------------------------------------------------------------------

EGE_TASKS = 5            # заданий в одной тренировке
EGE_STATEMENTS = 5       # суждений в задании
EGE_MAX_POINTS = 2       # как в ЕГЭ: всё верно — 2, одна ошибка — 1, иначе 0

DEF_BY_TERM = {term: text for text, term in DEFINITIONS.items()}
TERM_TOPIC = {}
for _q in POOL:
    if _q["question"].startswith("Какому понятию соответствует определение: «"):
        TERM_TOPIC[_q["options"][_q["correct"]]] = _q["topic"]

# Общие «служебные» основы слов, по которым понятия НЕ считаем родственными.
_STEM_STOPLIST = {"социа", "полит", "госуд", "систе"}


def _stems(term: str) -> set:
    words = term.lower().replace("ё", "е").split()
    return {w[:5] for w in words if len(w) >= 4} - _STEM_STOPLIST


def can_borrow(term: str, other: str) -> bool:
    """Можно ли приписать понятию term определение понятия other, чтобы получилось
    ОДНОЗНАЧНО неверное суждение. Исключаем родственные пары (Истина / Абсолютная истина,
    Власть / Политическая власть) и определения, которые сами начинаются с этого понятия
    (Демократия — «политический режим, …» для понятия «Политический режим»)."""
    if term == other:
        return False
    if _stems(term) & _stems(other):
        return False
    if term.lower() in DEF_BY_TERM[other].lower():
        return False
    return True


def make_ege_task(topic: str) -> dict:
    """Одно задание: 5 суждений «Понятие — определение», из них 2–3 верных."""
    terms_all = [t for t, tp in TERM_TOPIC.items() if tp == topic]
    for _ in range(200):
        terms = random.sample(terms_all, EGE_STATEMENTS)
        n_true = random.choice([2, 3])
        false_terms = set(random.sample(terms, EGE_STATEMENTS - n_true))
        statements, ok = [], True
        for term in terms:
            if term not in false_terms:
                statements.append({"term": term, "def": DEF_BY_TERM[term], "true": True, "real": term})
                continue
            donors = [o for o in terms_all if o not in terms and can_borrow(term, o)]
            if not donors:
                ok = False
                break
            donor = random.choice(donors)
            statements.append({"term": term, "def": DEF_BY_TERM[donor], "true": False, "real": donor})
        if ok:
            return {"topic": topic, "statements": statements}
    raise RuntimeError(f"Не удалось собрать задание по теме {topic}")


def make_ege_tasks(n: int = EGE_TASKS) -> list[dict]:
    topics = TOPICS[:]
    random.shuffle(topics)
    return [make_ege_task(topics[i % len(topics)]) for i in range(n)]


def correct_mask(task: dict) -> int:
    return sum(1 << i for i, st in enumerate(task["statements"]) if st["true"])


def mask_to_digits(mask: int) -> str:
    return "".join(str(i + 1) for i in range(EGE_STATEMENTS) if mask & (1 << i)) or "—"


def ege_points(chosen: int, right: int) -> int:
    """Критерий ЕГЭ: 0 ошибок — 2 балла; одна ошибка (лишняя или пропущенная цифра) — 1 балл."""
    errors = bin(chosen ^ right).count("1")
    return 2 if errors == 0 else 1 if errors == 1 else 0


BOT_DESCRIPTION = (
    '👋 Привет! Я бот-тренажёр для подготовки к ЕГЭ по обществознанию.\n'
    '\n'
    f'📚 {len(POOL)} заданий по {len(TERMS)} ключевым понятиям: человек и общество, '
    'экономика, социальные отношения, политика и право.\n'
    '\n'
    'Я помогу тебе:\n'
    '🎯 Тренироваться по 5 или 10 вопросов.\n'
    '🎓 Решать задания в формате ЕГЭ: выбрать верные суждения.\n'
    '📖 Разбирать правильные ответы и объяснения.\n'
    '🔁 Повторять вопросы, в которых были ошибки.\n'
    '📊 Следить за результатами и серией дней занятий.\n'
    '\n'
    'Нажми «Запустить» — и начнём!'
)

BOT_SHORT_DESCRIPTION = (
    f'Тренажёр ЕГЭ по обществознанию: {len(TERMS)} ключевых понятий, '
    'объяснения, повтор ошибок и статистика.'
)


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

def db():
    """Новое подключение; вызывающий код закрывает его через with."""
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=10,
        options="-c statement_timeout=15000",
    )


def init_db():
    """Создаёт и дополняет таблицы, не очищая статистику."""
    with db() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS ege_bot")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ege_bot.users (
                user_id BIGINT PRIMARY KEY,
                total BIGINT DEFAULT 0,
                correct BIGINT DEFAULT 0,
                streak INTEGER DEFAULT 0,
                last_day TEXT
            )
        """)
        conn.execute("ALTER TABLE ege_bot.users ADD COLUMN IF NOT EXISTS trainings_done BIGINT DEFAULT 0")
        conn.execute("ALTER TABLE ege_bot.users ADD COLUMN IF NOT EXISTS help_msg_id BIGINT")
        conn.execute("ALTER TABLE ege_bot.users ADD COLUMN IF NOT EXISTS ege_tasks BIGINT DEFAULT 0")
        conn.execute("ALTER TABLE ege_bot.users ADD COLUMN IF NOT EXISTS ege_points BIGINT DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ege_bot.mistakes (
                user_id BIGINT,
                question_id INTEGER,
                wrong_count BIGINT DEFAULT 1,
                needs_review INTEGER DEFAULT 1,
                last_wrong_option INTEGER,
                last_wrong_at TEXT,
                reviewed_correctly BIGINT DEFAULT 0,
                PRIMARY KEY (user_id, question_id)
            )
        """)
        # Активные тренировки теперь хранятся в базе и переживают перезапуск/деплой.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ege_bot.sessions (
                user_id BIGINT PRIMARY KEY,
                session_id INTEGER NOT NULL,
                question_ids INTEGER[] NOT NULL,
                idx INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                review_mode BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ege_bot.ege_sessions (
                user_id BIGINT PRIMARY KEY,
                session_id INTEGER NOT NULL,
                tasks JSONB NOT NULL,
                idx INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ege_bot.feedback (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                text TEXT NOT NULL,
                admin_msg_id BIGINT,
                answered BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def ensure_user(user_id: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO ege_bot.users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )


def start_session(user_id: int, question_ids: list[int], review_mode: bool = False) -> int:
    session_id = random.randint(1, 2_000_000_000)
    with db() as conn:
        conn.execute(
            "INSERT INTO ege_bot.users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        conn.execute("""
            INSERT INTO ege_bot.sessions (user_id, session_id, question_ids, idx, score, review_mode, updated_at)
            VALUES (%s, %s, %s, 0, 0, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
                session_id=excluded.session_id,
                question_ids=excluded.question_ids,
                idx=0,
                score=0,
                review_mode=excluded.review_mode,
                updated_at=now()
        """, (user_id, session_id, question_ids, review_mode))
    return session_id


def apply_answer(user_id: int, session_id: int, question_id: int, chosen: int):
    """Проверяет и засчитывает ответ в одной транзакции.

    Строка сессии блокируется (FOR UPDATE), поэтому двойное нажатие
    не засчитает ответ дважды и не перескочит через вопрос.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT session_id, question_ids, idx, score, review_mode "
            "FROM ege_bot.sessions WHERE user_id=%s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if not row:
            return {"status": "no_session"}
        sid, qids, idx, score, review_mode = row
        if sid != session_id or idx >= len(qids) or qids[idx] != question_id:
            return {"status": "stale"}
        q = QMAP.get(question_id)
        if not q:
            return {"status": "stale"}

        is_correct = chosen == q["correct"]
        conn.execute(
            "INSERT INTO ege_bot.users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        conn.execute(
            "UPDATE ege_bot.users SET total=total+1, correct=correct+%s WHERE user_id=%s",
            (1 if is_correct else 0, user_id),
        )
        if is_correct:
            score += 1
            if review_mode:
                conn.execute("""
                    UPDATE ege_bot.mistakes
                    SET needs_review=0, reviewed_correctly=reviewed_correctly+1
                    WHERE user_id=%s AND question_id=%s
                """, (user_id, question_id))
        else:
            conn.execute("""
                INSERT INTO ege_bot.mistakes(
                    user_id, question_id, wrong_count, needs_review,
                    last_wrong_option, last_wrong_at, reviewed_correctly
                )
                VALUES (%s, %s, 1, 1, %s,
                        to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), 0)
                ON CONFLICT(user_id, question_id) DO UPDATE SET
                    wrong_count=mistakes.wrong_count+1,
                    needs_review=1,
                    last_wrong_option=excluded.last_wrong_option,
                    last_wrong_at=to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
            """, (user_id, question_id, chosen))

        idx += 1
        conn.execute(
            "UPDATE ege_bot.sessions SET idx=%s, score=%s, updated_at=now() WHERE user_id=%s",
            (idx, score, user_id),
        )
        return {
            "status": "ok",
            "is_correct": is_correct,
            "review_mode": review_mode,
            "idx": idx,
            "total": len(qids),
            "score": score,
            "next_qid": qids[idx] if idx < len(qids) else None,
        }


def _close_training(conn, user_id: int) -> int:
    """Обновляет серию дней и счётчик тренировок. Возвращает число завершённых тренировок."""
    today = date.today()
    row = conn.execute(
        "SELECT streak, last_day, trainings_done FROM ege_bot.users WHERE user_id=%s FOR UPDATE",
        (user_id,),
    ).fetchone()
    streak, last_day, done = row if row else (0, None, 0)
    streak = streak or 0
    done = (done or 0) + 1
    if last_day != today.isoformat():
        if last_day == (today - timedelta(days=1)).isoformat():
            streak += 1
        else:
            streak = 1
    conn.execute(
        "UPDATE ege_bot.users SET streak=%s, last_day=%s, trainings_done=%s WHERE user_id=%s",
        (streak, today.isoformat(), done, user_id),
    )
    return done


def finish_session(user_id: int) -> int:
    with db() as conn:
        conn.execute("DELETE FROM ege_bot.sessions WHERE user_id=%s", (user_id,))
        return _close_training(conn, user_id)


def start_ege_session(user_id: int, tasks: list[dict]) -> int:
    session_id = random.randint(1, 2_000_000_000)
    with db() as conn:
        conn.execute(
            "INSERT INTO ege_bot.users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        conn.execute("""
            INSERT INTO ege_bot.ege_sessions (user_id, session_id, tasks, idx, score, updated_at)
            VALUES (%s, %s, %s, 0, 0, now())
            ON CONFLICT (user_id) DO UPDATE SET
                session_id=excluded.session_id,
                tasks=excluded.tasks,
                idx=0,
                score=0,
                updated_at=now()
        """, (user_id, session_id, Jsonb(tasks)))
    return session_id


def apply_ege_answer(user_id: int, session_id: int, task_idx: int, chosen: int):
    """Засчитывает ответ на задание «Как на ЕГЭ» (защищено от двойного нажатия)."""
    with db() as conn:
        row = conn.execute(
            "SELECT session_id, tasks, idx, score FROM ege_bot.ege_sessions WHERE user_id=%s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if not row:
            return {"status": "no_session"}
        sid, tasks, idx, score = row
        if sid != session_id or idx != task_idx or idx >= len(tasks):
            return {"status": "stale"}
        task = tasks[idx]
        points = ege_points(chosen, correct_mask(task))
        score += points
        idx += 1
        conn.execute(
            "UPDATE ege_bot.ege_sessions SET idx=%s, score=%s, updated_at=now() WHERE user_id=%s",
            (idx, score, user_id),
        )
        conn.execute(
            "UPDATE ege_bot.users SET ege_tasks=ege_tasks+1, ege_points=ege_points+%s WHERE user_id=%s",
            (points, user_id),
        )
        finished = idx >= len(tasks)
        if finished:
            conn.execute("DELETE FROM ege_bot.ege_sessions WHERE user_id=%s", (user_id,))
            _close_training(conn, user_id)
        return {
            "status": "ok",
            "task": task,
            "points": points,
            "idx": idx,
            "total": len(tasks),
            "score": score,
            "next_task": None if finished else tasks[idx],
        }


def get_help_msg_id(user_id: int):
    with db() as conn:
        row = conn.execute("SELECT help_msg_id FROM ege_bot.users WHERE user_id=%s", (user_id,)).fetchone()
        return row[0] if row else None


def set_help_msg_id(user_id: int, message_id: int):
    with db() as conn:
        conn.execute("UPDATE ege_bot.users SET help_msg_id=%s WHERE user_id=%s", (message_id, user_id))


def get_stats(user_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT total, correct, streak, ege_tasks, ege_points FROM ege_bot.users WHERE user_id=%s",
            (user_id,),
        ).fetchone()
        return row or (0, 0, 0, 0, 0)


def get_admin_stats():
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) FROM ege_bot.users").fetchone()[0]
        active_today = conn.execute(
            "SELECT COUNT(*) FROM ege_bot.users WHERE last_day=%s",
            (date.today().isoformat(),),
        ).fetchone()[0]
        total, correct = conn.execute(
            "SELECT COALESCE(SUM(total), 0), COALESCE(SUM(correct), 0) FROM ege_bot.users"
        ).fetchone()
        mistakes = conn.execute(
            "SELECT COALESCE(SUM(wrong_count), 0) FROM ege_bot.mistakes"
        ).fetchone()[0]
        feedback_new = conn.execute(
            "SELECT COUNT(*) FROM ege_bot.feedback WHERE answered=FALSE"
        ).fetchone()[0]
        accuracy = round(correct / total * 100) if total else 0
        return {
            "users": users, "active_today": active_today, "total": total,
            "correct": correct, "mistakes": mistakes, "accuracy": accuracy,
            "feedback_new": feedback_new,
        }


def get_mistake_questions(user_id: int):
    with db() as conn:
        rows = conn.execute("""
            SELECT question_id
            FROM ege_bot.mistakes
            WHERE user_id=%s AND needs_review=1
            ORDER BY wrong_count DESC, last_wrong_at DESC
        """, (user_id,)).fetchall()
    return [QMAP[r[0]] for r in rows if r[0] in QMAP]


def get_mistake_summary(user_id: int):
    with db() as conn:
        rows = conn.execute("""
            SELECT question_id, wrong_count, needs_review
            FROM ege_bot.mistakes
            WHERE user_id=%s
            ORDER BY needs_review DESC, wrong_count DESC, last_wrong_at DESC
        """, (user_id,)).fetchall()
    summary = {"total_unique": 0, "active_unique": 0, "total_wrong": 0, "by_topic": {}}
    for question_id, wrong_count, needs_review in rows:
        q = QMAP.get(question_id)
        if not q:  # старые ошибки в исключённых «блочных» заданиях не показываем
            continue
        summary["total_unique"] += 1
        summary["total_wrong"] += wrong_count
        data = summary["by_topic"].setdefault(q["topic"], {"active": 0, "questions": 0, "wrong_answers": 0})
        data["questions"] += 1
        data["wrong_answers"] += wrong_count
        if needs_review:
            summary["active_unique"] += 1
            data["active"] += 1
    return summary


def save_feedback(user_id: int, username: str | None, full_name: str, text: str) -> int:
    with db() as conn:
        return conn.execute(
            "INSERT INTO ege_bot.feedback (user_id, username, full_name, text) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (user_id, username, full_name, text),
        ).fetchone()[0]


def set_feedback_admin_msg(feedback_id: int, admin_msg_id: int):
    with db() as conn:
        conn.execute(
            "UPDATE ege_bot.feedback SET admin_msg_id=%s WHERE id=%s",
            (admin_msg_id, feedback_id),
        )


def find_feedback_by_admin_msg(admin_msg_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT id, user_id FROM ege_bot.feedback WHERE admin_msg_id=%s",
            (admin_msg_id,),
        ).fetchone()


def mark_feedback_answered(feedback_id: int):
    with db() as conn:
        conn.execute("UPDATE ege_bot.feedback SET answered=TRUE WHERE id=%s", (feedback_id,))


# ---------------------------------------------------------------------------
# Клавиатуры и тексты
# ---------------------------------------------------------------------------

def main_menu(user_id: int | None = None):
    rows = [
        [KeyboardButton(text=BTN_FIVE), KeyboardButton(text=BTN_TEN)],
        [KeyboardButton(text=BTN_EGE)],
        [KeyboardButton(text=BTN_TOPIC), KeyboardButton(text=BTN_PROGRESS)],
        [KeyboardButton(text=BTN_MISTAKES), KeyboardButton(text=BTN_REVIEW)],
        [KeyboardButton(text=BTN_FEEDBACK)],
    ]
    if ADMIN_ID is not None and user_id == ADMIN_ID:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def answer_keyboard(question, session_id: int):
    # На кнопках только буквы (длинный текст Telegram обрезает троеточием),
    # а для «Верно ли суждение» — сами слова «Да» / «Нет».
    yes_no = question["options"] == ["Да", "Нет"]
    buttons = [
        InlineKeyboardButton(
            text=option if yes_no else chr(65 + i),
            callback_data=f"ans:{session_id}:{question['id']}:{i}",
        )
        for i, option in enumerate(question["options"])
    ]
    if len(buttons) == 4:
        rows = [buttons[:2], buttons[2:]]
    else:
        rows = [buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_text(question, number: int, total: int):
    head = f"🧠 Вопрос {number}/{total}\n📌 Тема: {question['topic']}\n\n{question['question']}"
    if question["options"] == ["Да", "Нет"]:
        return f"{head}\n\n👇 Выбери ответ:"
    options_text = "\n\n".join(
        f"{chr(65 + i)}. {option}" for i, option in enumerate(question["options"])
    )
    return f"{head}\n\n{options_text}\n\n👇 Выбери букву ответа:"


async def send_question(chat_id: int, q, session_id: int, number: int, total: int):
    await bot.send_message(
        chat_id,
        question_text(q, number, total),
        reply_markup=answer_keyboard(q, session_id),
    )


async def begin_training(user_id: int, chat_id: int, questions, review_mode: bool = False):
    AWAITING_FEEDBACK.pop(user_id, None)
    ids = [q["id"] for q in questions]
    session_id = await asyncio.to_thread(start_session, user_id, ids, review_mode)
    await send_question(chat_id, questions[0], session_id, 1, len(questions))


# ---------------------------------------------------------------------------
# Хендлеры: тренировки
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📌 <b>Как пользоваться ботом</b>\n\n"
    "Меню — внизу экрана. Если оно пропало, нажми /help.\n\n"
    "<b>🎯 5 вопросов / 🧪 10 вопросов</b> — быстрая тренировка по всем темам. "
    "Выбираешь букву ответа и сразу видишь объяснение.\n\n"
    "<b>🎓 Как на ЕГЭ</b> — задания в формате экзамена. Отмечай кнопками 1–5 все верные "
    "суждения (их может быть несколько) и жми <b>«Готово»</b>. "
    "Всё верно — 2 балла, одна ошибка — 1 балл.\n\n"
    "<b>📚 По теме</b> — 5 вопросов из одного блока: экономика, право, политика и другие.\n\n"
    "<b>❌ Мои ошибки</b> — где ты ошибаешься чаще всего.\n"
    "<b>🔁 Повтор ошибок</b> — отработка только ошибочных вопросов. "
    "Ответил верно — вопрос убирается из повтора.\n\n"
    "<b>📊 Мой прогресс</b> — точность, баллы и 🔥 серия дней.\n\n"
    "<b>💬 Пожелания</b> — напиши, что добавить или где нашёл ошибку. "
    "Автор бота прочитает и ответит.\n\n"
    "💡 Занимайся хотя бы 5 минут в день — серия не прервётся, а материал запомнится лучше."
)


async def send_pinned_help(user_id: int, chat_id: int):
    """Присылает инструкцию и закрепляет её вверху чата.
    Старую закреплённую инструкцию открепляем, чтобы закрепы не копились."""
    old_id = await asyncio.to_thread(get_help_msg_id, user_id)
    sent = await bot.send_message(chat_id, HELP_TEXT, parse_mode="HTML", reply_markup=main_menu(user_id))
    if old_id:
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=old_id)
        except TelegramAPIError:
            pass
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
        await asyncio.to_thread(set_help_msg_id, user_id, sent.message_id)
    except TelegramAPIError:
        pass  # не удалось закрепить — инструкция всё равно отправлена


INSTRUCTION_TEXT = (
    "📌 <b>Как пользоваться ботом</b>\n\n"
    "Меню с режимами — внизу экрана. Если оно пропало, нажми /start.\n\n"
    "<b>🎯 5 вопросов / 🧪 10 вопросов</b> — быстрая тренировка по всем темам. "
    "Выбираешь букву ответа и сразу видишь объяснение.\n\n"
    "<b>🎓 Как на ЕГЭ</b> — задания в формате экзамена. Отмечай кнопками 1–5 все верные "
    "суждения (их может быть несколько) и жми <b>«Готово»</b>. "
    "Всё верно — 2 балла, одна ошибка — 1 балл.\n\n"
    "<b>📚 По теме</b> — 5 вопросов из одного блока: экономика, право, политика и другие.\n\n"
    "<b>❌ Мои ошибки</b> — где ты ошибаешься чаще всего.\n"
    "<b>🔁 Повтор ошибок</b> — отработка только вопросов с ошибками. "
    "Ответил верно — вопрос убирается из повтора.\n\n"
    "<b>📊 Мой прогресс</b> — точность, баллы и 🔥 серия дней.\n\n"
    "<b>💬 Пожелания</b> — напиши, что добавить или где нашёл ошибку. "
    "Автор бота прочитает и ответит.\n\n"
    "💡 Занимайся хотя бы 5 минут в день — так серия не прервётся, "
    "а материал запомнится лучше.\n\n"
    "Эта инструкция закреплена вверху чата. Открыть её заново — /help"
)


async def send_pinned_instruction(message: Message):
    """Отправляет инструкцию и закрепляет её вверху чата ученика.
    Предыдущая инструкция открепляется, чтобы закрепы не копились."""
    user_id = message.from_user.id
    sent = await message.answer(
        INSTRUCTION_TEXT,
        parse_mode="HTML",
        reply_markup=main_menu(user_id),
    )
    old_id = await asyncio.to_thread(get_help_msg_id, user_id)
    if old_id and old_id != sent.message_id:
        try:
            await bot.unpin_chat_message(chat_id=message.chat.id, message_id=old_id)
        except TelegramAPIError:
            pass
    try:
        await bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=sent.message_id,
            disable_notification=True,
        )
        await asyncio.to_thread(set_help_msg_id, user_id, sent.message_id)
    except TelegramAPIError:
        pass  # не удалось закрепить — инструкция всё равно отправлена


@dp.message(CommandStart())
async def start(message: Message):
    AWAITING_FEEDBACK.pop(message.from_user.id, None)
    await asyncio.to_thread(ensure_user, message.from_user.id)
    await message.answer(
        "👋 Привет! Я тренажёр ЕГЭ по обществознанию.\n"
        f"📚 {len(TERMS)} ключевых понятий, {len(POOL)} заданий с ответами и объяснениями."
    )
    await send_pinned_instruction(message)


@dp.message(Command("help"))
async def help_command(message: Message):
    AWAITING_FEEDBACK.pop(message.from_user.id, None)
    await asyncio.to_thread(ensure_user, message.from_user.id)
    await send_pinned_instruction(message)


@dp.message(F.text == BTN_FIVE)
async def training(message: Message):
    await begin_training(message.from_user.id, message.chat.id, pick_questions(POOL, 5))


@dp.message(F.text == BTN_TEN)
async def ten_questions(message: Message):
    await begin_training(message.from_user.id, message.chat.id, pick_questions(POOL, 10))


@dp.message(F.text == BTN_TOPIC)
async def topics(message: Message):
    AWAITING_FEEDBACK.pop(message.from_user.id, None)
    rows = [[InlineKeyboardButton(text=t, callback_data=f"topic:{i}")] for i, t in enumerate(TOPICS)]
    await message.answer(
        "Выбери блок. Я дам 5 вопросов только по нему:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("topic:"))
async def topic_training(callback: CallbackQuery):
    try:
        topic = TOPICS[int(callback.data.split(":")[1])]
    except (ValueError, IndexError):
        await callback.answer("Не удалось выбрать тему.")
        return
    await callback.answer()
    pool = [q for q in POOL if q["topic"] == topic]
    await callback.message.answer(f"📚 Тема: {topic}")
    await begin_training(callback.from_user.id, callback.message.chat.id, pick_questions(pool, 5))


@dp.message(F.text == BTN_REVIEW)
async def review_mistakes(message: Message):
    AWAITING_FEEDBACK.pop(message.from_user.id, None)
    questions = await asyncio.to_thread(get_mistake_questions, message.from_user.id)
    if not questions:
        await message.answer(
            "🎉 Сейчас нет ошибок, которые нужно повторить.\n\n"
            "Новые неправильные ответы автоматически появятся в этом разделе.",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    selected = questions[:10]
    await message.answer(
        "🔁 Повтор ошибок\n\n"
        f"В этой тренировке: {len(selected)}.\n"
        "Ответишь правильно — ошибка будет помечена как отработанная.\n"
        "Ошибёшься снова — она останется в повторе."
    )
    await begin_training(message.from_user.id, message.chat.id, selected, review_mode=True)


@dp.callback_query(F.data.startswith("ans:"))
async def answer(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        _, sid_s, qid_s, answer_s = callback.data.split(":")
        session_id, qid, chosen = int(sid_s), int(qid_s), int(answer_s)
    except ValueError:
        # Кнопки из старой версии бота (до обновления) имеют другой формат.
        await callback.answer("Этот вопрос уже не активен. Начни новую тренировку.")
        return

    res = await asyncio.to_thread(apply_answer, user_id, session_id, qid, chosen)

    if res["status"] == "no_session":
        await callback.answer("Эта тренировка уже закончена.")
        return
    if res["status"] == "stale":
        await callback.answer("Этот вопрос уже не активен.")
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    q = QMAP[qid]
    if res["is_correct"]:
        result = "✅ Правильно!"
    else:
        correct_text = q["options"][q["correct"]]
        if q["options"] == ["Да", "Нет"]:
            result = f"❌ Неправильно. Правильный ответ: {correct_text}."
        else:
            result = f"❌ Неправильно.\nПравильный ответ: {chr(65 + q['correct'])}. {correct_text}"
    text = f"{result}\n\n💡 {q['explanation']}"
    if res["review_mode"] and res["is_correct"]:
        text += "\n\n✅ Эта ошибка отработана и убрана из повтора."
    await callback.message.answer(text)

    chat_id = callback.message.chat.id
    if res["next_qid"] is not None:
        next_number = res["idx"] + 1
        await send_question(chat_id, QMAP[res["next_qid"]], session_id, next_number, res["total"])
        return

    await asyncio.to_thread(finish_session, user_id)
    await bot.send_message(
        chat_id,
        "✅ Тренировка закончена!\n\n"
        f"Результат: {res['score']}/{res['total']}\n"
        "Нажми «📊 Мой прогресс», чтобы посмотреть общую статистику.",
        reply_markup=main_menu(user_id),
    )


# ---------------------------------------------------------------------------
# Хендлеры: режим «Как на ЕГЭ»
# ---------------------------------------------------------------------------

def ege_task_text(task: dict, number: int, total: int) -> str:
    lines = [
        f"🎓 Задание {number}/{total} · как на ЕГЭ",
        f"📌 Тема: {task['topic']}",
        "",
        "Выберите верные суждения о понятиях и запишите цифры, под которыми они указаны.",
        "",
    ]
    for i, st in enumerate(task["statements"]):
        lines.append(f"{i + 1}) {st['term']} — {st['def']}.")
        lines.append("")
    lines.append("👇 Отметь номера верных суждений и нажми «Готово».")
    return "\n".join(lines)


def ege_keyboard(session_id: int, task_idx: int, mask: int) -> InlineKeyboardMarkup:
    # Выбор хранится прямо в кнопках (mask), поэтому переключение не трогает базу
    # и переживает перезапуск бота.
    toggles = [
        InlineKeyboardButton(
            text=f"✅{i + 1}" if mask & (1 << i) else str(i + 1),
            callback_data=f"et:{session_id}:{task_idx}:{mask}:{i}",
        )
        for i in range(EGE_STATEMENTS)
    ]
    selected = mask_to_digits(mask) if mask else ""
    done = InlineKeyboardButton(
        text=f"Готово ✔️ {selected}".strip(),
        callback_data=f"ed:{session_id}:{task_idx}:{mask}",
    )
    return InlineKeyboardMarkup(inline_keyboard=[toggles, [done]])


def ege_result_text(task: dict, chosen: int, points: int) -> str:
    right = correct_mask(task)
    head = {2: "✅ Всё верно!", 1: "🟡 Почти: одна ошибка.", 0: "❌ Неверно."}[points]
    lines = [
        head,
        f"Верный ответ: {mask_to_digits(right)}",
        f"Твой ответ: {mask_to_digits(chosen)}",
        f"Баллы: {points} из {EGE_MAX_POINTS}",
        "",
        "💡 Разбор:",
    ]
    for i, st in enumerate(task["statements"]):
        if st["true"]:
            lines.append(f"{i + 1} — верно.")
        else:
            lines.append(
                f"{i + 1} — неверно: это определение понятия «{st['real']}». "
                f"«{st['term']}» — {DEF_BY_TERM.get(st['term'], '…')}."
            )
    return "\n".join(lines)


@dp.message(F.text == BTN_EGE)
async def ege_mode(message: Message):
    user_id = message.from_user.id
    AWAITING_FEEDBACK.pop(user_id, None)
    tasks = make_ege_tasks()
    session_id = await asyncio.to_thread(start_ege_session, user_id, tasks)
    await message.answer(
        "🎓 Как на ЕГЭ\n\n"
        f"{len(tasks)} заданий: в каждом 5 суждений, верных может быть несколько.\n"
        "Оценка как на экзамене: всё верно — 2 балла, одна ошибка "
        "(лишняя или пропущенная цифра) — 1 балл, больше — 0."
    )
    await message.answer(
        ege_task_text(tasks[0], 1, len(tasks)),
        reply_markup=ege_keyboard(session_id, 0, 0),
    )


@dp.callback_query(F.data.startswith("et:"))
async def ege_toggle(callback: CallbackQuery):
    try:
        _, sid_s, idx_s, mask_s, i_s = callback.data.split(":")
        session_id, task_idx, mask, i = int(sid_s), int(idx_s), int(mask_s), int(i_s)
    except ValueError:
        await callback.answer()
        return
    mask ^= 1 << i
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=ege_keyboard(session_id, task_idx, mask))
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("ed:"))
async def ege_done(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        _, sid_s, idx_s, mask_s = callback.data.split(":")
        session_id, task_idx, mask = int(sid_s), int(idx_s), int(mask_s)
    except ValueError:
        await callback.answer()
        return
    if mask == 0:
        await callback.answer("Отметь хотя бы одно суждение.", show_alert=True)
        return

    res = await asyncio.to_thread(apply_ege_answer, user_id, session_id, task_idx, mask)
    if res["status"] == "no_session":
        await callback.answer("Эта тренировка уже закончена.")
        return
    if res["status"] == "stale":
        await callback.answer("Это задание уже не активно.")
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.message.answer(ege_result_text(res["task"], mask, res["points"]))

    chat_id = callback.message.chat.id
    if res["next_task"] is not None:
        next_number = res["idx"] + 1
        await bot.send_message(
            chat_id,
            ege_task_text(res["next_task"], next_number, res["total"]),
            reply_markup=ege_keyboard(session_id, res["idx"], 0),
        )
        return

    await bot.send_message(
        chat_id,
        "🎓 Тренировка «Как на ЕГЭ» закончена!\n\n"
        f"Набрано: {res['score']} из {res['total'] * EGE_MAX_POINTS} баллов.\n"
        "Нажми «📊 Мой прогресс», чтобы посмотреть общую статистику.",
        reply_markup=main_menu(user_id),
    )


# ---------------------------------------------------------------------------
# Хендлеры: статистика и ошибки
# ---------------------------------------------------------------------------

@dp.message(F.text == BTN_MISTAKES)
async def mistakes(message: Message):
    AWAITING_FEEDBACK.pop(message.from_user.id, None)
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
            f"ошибочных вопросов — {data['questions']}; ошибок — {data['wrong_answers']}"
        )
    lines += ["", "Чтобы отработать их отдельно, нажми «🔁 Повтор ошибок»."
              if summary["active_unique"] else "🎉 Все сохранённые ошибки сейчас отработаны."]
    await message.answer("\n".join(lines), reply_markup=main_menu(message.from_user.id))


@dp.message(F.text == BTN_PROGRESS)
async def user_stats(message: Message):
    AWAITING_FEEDBACK.pop(message.from_user.id, None)
    total, correct, streak, ege_tasks, ege_pts = await asyncio.to_thread(get_stats, message.from_user.id)
    percent = round(correct / total * 100) if total else 0
    ege_line = ""
    if ege_tasks:
        ege_percent = round(ege_pts / (ege_tasks * EGE_MAX_POINTS) * 100)
        ege_line = f"\n\n🎓 Как на ЕГЭ\nЗаданий: {ege_tasks}\nБаллы: {ege_pts} из {ege_tasks * EGE_MAX_POINTS} ({ege_percent}%)"
    await message.answer(
        "📊 Твой прогресс\n\n"
        f"Решено вопросов: {total}\n"
        f"Правильно: {correct}\n"
        f"Точность: {percent}%\n"
        f"🔥 Серия дней: {streak}"
        f"{ege_line}",
        reply_markup=main_menu(message.from_user.id),
    )


async def send_admin_stats(message: Message):
    if ADMIN_ID is None:
        await message.answer("⚠️ ADMIN_ID не настроен. Добавь числовой Telegram ID в переменные Railway.")
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
        f"🎯 Общая точность: {stats['accuracy']}%\n"
        f"💬 Пожеланий без ответа: {stats['feedback_new']}",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(Command("stats"))
async def admin_stats_command(message: Message):
    await send_admin_stats(message)


@dp.message(F.text == "📊 Статистика")
@dp.message(F.text == BTN_ADMIN)
async def admin_stats_button(message: Message):
    await send_admin_stats(message)


# ---------------------------------------------------------------------------
# Пожелания учеников
# ---------------------------------------------------------------------------

AWAITING_FEEDBACK: dict[int, float] = {}   # user_id -> когда нажал кнопку
LAST_FEEDBACK: dict[int, float] = {}       # user_id -> когда отправил последнее пожелание


def is_awaiting_feedback(message: Message) -> bool:
    started = AWAITING_FEEDBACK.get(message.from_user.id)
    return started is not None and time.time() - started < FEEDBACK_TIMEOUT


@dp.message(F.text == BTN_FEEDBACK)
async def feedback_start(message: Message):
    if ADMIN_ID is None:
        await message.answer("Раздел пожеланий пока не настроен.", reply_markup=main_menu(message.from_user.id))
        return
    AWAITING_FEEDBACK[message.from_user.id] = time.time()
    await message.answer(
        "💬 Напиши одним сообщением, что добавить или улучшить в боте, "
        "или о какой ошибке в вопросе сообщить.\n\n"
        "Чтобы передумать, просто нажми любую кнопку меню.",
        reply_markup=main_menu(message.from_user.id),
    )


# Ответ администратора: reply на пересланное пожелание -> сообщение уходит ученику.
@dp.message(F.reply_to_message, F.text, F.from_user.id == ADMIN_ID if ADMIN_ID else F.from_user.id == -1)
async def admin_reply(message: Message):
    found = await asyncio.to_thread(find_feedback_by_admin_msg, message.reply_to_message.message_id)
    if not found:
        await message.answer("Не нашёл пожелание, к которому относится этот ответ.")
        return
    feedback_id, user_id = found
    try:
        await bot.send_message(user_id, f"💬 Ответ на твоё пожелание:\n\n{message.text}")
    except TelegramAPIError:
        await message.answer("⚠️ Не удалось доставить: возможно, ученик заблокировал бота.")
        return
    await asyncio.to_thread(mark_feedback_answered, feedback_id)
    await message.answer("✅ Ответ отправлен.")


@dp.message(F.text, is_awaiting_feedback)
async def feedback_receive(message: Message):
    user = message.from_user
    if message.text in MENU_TEXTS or message.text.startswith("/"):
        AWAITING_FEEDBACK.pop(user.id, None)
        await message.answer("Используй кнопки меню 👇", reply_markup=main_menu(user.id))
        return
    now = time.time()
    if now - LAST_FEEDBACK.get(user.id, 0) < FEEDBACK_COOLDOWN:
        await message.answer("Подожди немного перед следующим сообщением 🙂")
        return
    AWAITING_FEEDBACK.pop(user.id, None)
    LAST_FEEDBACK[user.id] = now

    text = message.text[:FEEDBACK_MAX_LEN]
    feedback_id = await asyncio.to_thread(save_feedback, user.id, user.username, user.full_name, text)

    who = html.escape(user.full_name or "Без имени")
    if user.username:
        who += f" (@{html.escape(user.username)})"
    try:
        sent = await bot.send_message(
            ADMIN_ID,
            f"💬 <b>Пожелание #{feedback_id}</b>\nОт: {who}\nID: <code>{user.id}</code>\n\n"
            f"{html.escape(text)}\n\n<i>Ответь на это сообщение (Reply), чтобы написать ученику.</i>",
            parse_mode="HTML",
        )
        await asyncio.to_thread(set_feedback_admin_msg, feedback_id, sent.message_id)
    except TelegramAPIError:
        pass  # пожелание всё равно сохранено в базе

    await message.answer("Спасибо! 🙌 Пожелание отправлено автору бота.", reply_markup=main_menu(user.id))


@dp.message()
async def fallback(message: Message):
    await message.answer("Используй кнопки меню 👇", reply_markup=main_menu(message.from_user.id))


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def configure_bot_description(client: Bot):
    for language in ("", "ru"):
        await client.set_my_description(description=BOT_DESCRIPTION, language_code=language)
        await client.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION, language_code=language)


async def main():
    await asyncio.to_thread(init_db)
    try:
        await configure_bot_description(bot)
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать и открыть меню"),
            BotCommand(command="help", description="Как пользоваться ботом"),
        ])
        print("Описание бота в Telegram обновлено.", flush=True)
    except TelegramAPIError as error:
        print(
            f"Не удалось обновить описание Telegram ({type(error).__name__}). "
            "Повторите настройку позже или через BotFather.",
            flush=True,
        )
    print(f"Бот запущен. В тренировках {len(POOL)} заданий по {len(TERMS)} понятиям.", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
