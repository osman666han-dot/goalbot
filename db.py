# -*- coding: utf-8 -*-
"""
Слой работы с базой данных (SQLite).

Модель кредитов:
- credits_balance — сколько целых кредитов куплено и ещё не открыто в цикл.
- cycle_active — открыт ли сейчас цикл проверки (после первого сообщения по цели).
- attempts_used — сколько попыток использовано в текущем открытом цикле (0-5).

Кредит списывается с credits_balance в момент открытия нового цикла (первое
сообщение после простоя/предыдущего успеха/предыдущего исчерпания попыток).
Цикл закрывается либо когда бот выдаёт финальную памятку об успехе (остаток
попыток сгорает), либо когда attempts_used достигает 5 без успеха (тоже сгорает).
Списание/открытие цикла происходит ТОЛЬКО после успешного ответа API — если
Anthropic вернул ошибку, ни кредит, ни попытка не тратятся.
"""
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import os

DB_PATH = os.getenv("DB_PATH", "goalbot.db")


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                credits_balance INTEGER NOT NULL DEFAULT 0,
                cycle_active INTEGER NOT NULL DEFAULT 0,
                attempts_used INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        # Миграция схемы для уже существующих баз (на живом сервере) —
        # CREATE TABLE IF NOT EXISTS не добавляет новые колонки в старую таблицу.
        for stmt in (
            "ALTER TABLE users ADD COLUMN last_free_credit_date TEXT",
            "ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass  # колонка уже существует
        c.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                user_message TEXT,
                bot_response TEXT,
                charged INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                credits_added INTEGER NOT NULL,
                stars_amount INTEGER NOT NULL,
                telegram_payment_charge_id TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS response_cache (
                text_hash TEXT PRIMARY KEY,
                response TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create_user(telegram_id: int, username: str | None) -> sqlite3.Row:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO users (telegram_id, username, credits_balance, cycle_active, attempts_used, created_at) "
                "VALUES (?, ?, 0, 0, 0, ?)",
                (telegram_id, username, int(time.time())),
            )
            row = c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        elif username and row["username"] != username:
            c.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (username, telegram_id))
        return row


def get_user(telegram_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()


def _today_kyiv() -> str:
    """Текущая дата по Киеву (ISO-строка) — используется для дневного сброса
    бесплатного кредита. Заменяет предыдущий день ровно в полночь по Киеву."""
    return datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()


def has_free_credit_today(telegram_id: int) -> bool:
    """Доступен ли ещё не использованный сегодня бесплатный дневной кредит."""
    user = get_user(telegram_id)
    if user is None:
        return True
    return user["last_free_credit_date"] != _today_kyiv()


def mark_blocked(telegram_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET is_blocked = 1 WHERE telegram_id = ?", (telegram_id,))


def mark_unblocked(telegram_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET is_blocked = 0 WHERE telegram_id = ?", (telegram_id,))


def has_available_attempt(telegram_id: int) -> bool:
    """Может ли пользователь отправить ещё одно сообщение прямо сейчас
    (открытый цикл с оставшимися попытками, ИЛИ бесплатный дневной кредит,
    ИЛИ купленный кредит на новый цикл)."""
    user = get_user(telegram_id)
    if user is None:
        return True  # ещё не создан — бесплатный дневной кредит точно доступен
    if user["cycle_active"] and user["attempts_used"] < 5:
        return True
    if not user["cycle_active"]:
        if user["last_free_credit_date"] != _today_kyiv():
            return True
        if user["credits_balance"] > 0:
            return True
    return False



def commit_attempt(telegram_id: int, goal_reached: bool) -> dict:
    """Вызывается ПОСЛЕ успешного ответа API — фиксирует списание попытки/кредита.

    Приоритет источников при открытии НОВОГО цикла: сначала бесплатный дневной
    кредит (если ещё не использован сегодня), затем купленные кредиты.

    Возвращает словарь:
    - reason: 'success' | 'continued' | 'exhausted_no_credits' | 'ongoing'
    - credits_balance: текущий остаток КУПЛЕННЫХ кредитов после операции
    - attempts_used: попыток использовано в текущем (возможно новом) цикле
    - used_free_credit: True, если именно в этом вызове был потрачен дневной бесплатный кредит

    'success' — цель прошла рамку, цикл закрыт, остаток попыток сгорает.
    'continued' — 5 попыток исчерпаны без успеха, но есть ещё купленные кредиты:
      следующий подключён автоматически, диалог продолжается бесшовно.
      (Бесплатный дневной кредит на 'continued' не расходуется повторно —
      он всего один в сутки.)
    'exhausted_no_credits' — 5 попыток исчерпаны без успеха, и купленных
      кредитов больше нет (дневной уже использован сегодня).
    'ongoing' — попытка использована, цикл продолжается (меньше 5 попыток, не успех).
    """
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row is None:
            return {"reason": "ongoing", "credits_balance": 0, "attempts_used": 0, "used_free_credit": False}

        cycle_active = row["cycle_active"]
        attempts_used = row["attempts_used"]
        credits_balance = row["credits_balance"]
        last_free = row["last_free_credit_date"]
        today = _today_kyiv()
        used_free_credit = False

        if not cycle_active:
            if last_free != today:
                last_free = today
                used_free_credit = True
                cycle_active = 1
                attempts_used = 1
            elif credits_balance > 0:
                credits_balance -= 1
                cycle_active = 1
                attempts_used = 1
            else:
                # Не должно случаться, если has_available_attempt проверен заранее,
                # но подстрахуемся от гонки состояний.
                return {
                    "reason": "exhausted_no_credits",
                    "credits_balance": credits_balance,
                    "attempts_used": 0,
                    "used_free_credit": False,
                }
        else:
            attempts_used += 1

        if goal_reached:
            cycle_active = 0
            attempts_used = 0
            reason = "success"
        elif attempts_used >= 5:
            if credits_balance > 0:
                credits_balance -= 1
                attempts_used = 0
                cycle_active = 1
                reason = "continued"
            else:
                cycle_active = 0
                attempts_used = 0
                reason = "exhausted_no_credits"
        else:
            reason = "ongoing"

        c.execute(
            "UPDATE users SET credits_balance = ?, cycle_active = ?, attempts_used = ?, "
            "last_free_credit_date = ? WHERE telegram_id = ?",
            (credits_balance, cycle_active, attempts_used, last_free, telegram_id),
        )

        return {
            "reason": reason,
            "credits_balance": credits_balance,
            "attempts_used": attempts_used,
            "used_free_credit": used_free_credit,
        }


def log_request(telegram_id: int, mode: str, user_message: str, bot_response: str, charged: bool) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO requests (telegram_id, mode, user_message, bot_response, charged, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, mode, user_message, bot_response, int(charged), int(time.time())),
        )


def add_credits(telegram_id: int, amount: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET credits_balance = credits_balance + ? WHERE telegram_id = ?",
            (amount, telegram_id),
        )


def set_credits(telegram_id: int, amount: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET credits_balance = ? WHERE telegram_id = ?",
            (amount, telegram_id),
        )


def log_payment(telegram_id: int, credits_added: int, stars_amount: int, charge_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO payments (telegram_id, credits_added, stars_amount, telegram_payment_charge_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, credits_added, stars_amount, charge_id, int(time.time())),
        )


# ---- Кэш ответов (для консистентности при повторе одной формулировки, /check) ----

def get_cached_response(text_hash: str) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT response FROM response_cache WHERE text_hash = ?", (text_hash,)
        ).fetchone()
        return row["response"] if row else None


def cache_response(text_hash: str, response: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO response_cache (text_hash, response, created_at) VALUES (?, ?, ?)",
            (text_hash, response, int(time.time())),
        )


# ---- Рассылка ----

def get_all_user_ids() -> list[int]:
    with _conn() as c:
        rows = c.execute("SELECT telegram_id FROM users").fetchall()
        return [r["telegram_id"] for r in rows]


# ---- Админ-статистика ----

def admin_stats() -> dict:
    with _conn() as c:
        total_users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        total_credits_bought = c.execute("SELECT COALESCE(SUM(credits_added), 0) AS n FROM payments").fetchone()["n"]
        total_stars = c.execute("SELECT COALESCE(SUM(stars_amount), 0) AS n FROM payments").fetchone()["n"]
        total_requests = c.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
        active_balance = c.execute("SELECT COALESCE(SUM(credits_balance), 0) AS n FROM users").fetchone()["n"]
        blocked_count = c.execute("SELECT COUNT(*) AS n FROM users WHERE is_blocked = 1").fetchone()["n"]
        return {
            "total_users": total_users,
            "total_credits_bought": total_credits_bought,
            "total_stars_earned": total_stars,
            "total_requests": total_requests,
            "active_credits_balance": active_balance,
            "blocked_count": blocked_count,
        }


def recent_requests(limit: int = 15) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def top_users(limit: int = 15) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT telegram_id, username, credits_balance FROM users "
            "ORDER BY telegram_id DESC LIMIT ?", (limit,)
        ).fetchall()


def all_users_full() -> list[sqlite3.Row]:
    """Все пользователи со всеми колонками, без ограничения — для CSV-выгрузки."""
    with _conn() as c:
        return c.execute(
            "SELECT telegram_id, username, credits_balance, cycle_active, attempts_used, "
            "last_free_credit_date, is_blocked, created_at FROM users ORDER BY telegram_id"
        ).fetchall()


def user_history(telegram_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM requests WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()


def get_request_by_id(request_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
