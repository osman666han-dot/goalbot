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


def has_available_attempt(telegram_id: int) -> bool:
    """Может ли пользователь отправить ещё одно сообщение прямо сейчас
    (либо есть открытый цикл с оставшимися попытками, либо есть кредит на новый цикл)."""
    user = get_user(telegram_id)
    if user is None:
        return False
    if user["cycle_active"] and user["attempts_used"] < 5:
        return True
    if not user["cycle_active"] and user["credits_balance"] > 0:
        return True
    return False


def commit_attempt(telegram_id: int, goal_reached: bool) -> dict:
    """Вызывается ПОСЛЕ успешного ответа API — фиксирует списание попытки/кредита.

    Возвращает словарь:
    - reason: 'success' | 'continued' | 'exhausted_no_credits' | 'ongoing'
    - credits_balance: текущий остаток кредитов после операции
    - attempts_used: попыток использовано в текущем (возможно новом) цикле

    'success' — цель прошла рамку, цикл закрыт, остаток попыток сгорает.
    'continued' — 5 попыток исчерпаны без успеха, но есть ещё кредиты: следующий
      кредит подключён автоматически, диалог продолжается бесшовно.
    'exhausted_no_credits' — 5 попыток исчерпаны без успеха и кредитов больше нет.
    'ongoing' — попытка использована, цикл продолжается (меньше 5 попыток, не успех).
    """
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row is None:
            return {"reason": "ongoing", "credits_balance": 0, "attempts_used": 0}

        cycle_active = row["cycle_active"]
        attempts_used = row["attempts_used"]
        credits_balance = row["credits_balance"]

        if not cycle_active:
            credits_balance -= 1
            cycle_active = 1
            attempts_used = 1
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
            "UPDATE users SET credits_balance = ?, cycle_active = ?, attempts_used = ? WHERE telegram_id = ?",
            (credits_balance, cycle_active, attempts_used, telegram_id),
        )

        return {"reason": reason, "credits_balance": credits_balance, "attempts_used": attempts_used}


def log_request(telegram_id: int, mode: str, user_message: str, bot_response: str, charged: bool) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO requests (telegram_id, mode, user_message, bot_response, charged, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, mode, user_message[:2000], bot_response[:2000], int(charged), int(time.time())),
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
        return {
            "total_users": total_users,
            "total_credits_bought": total_credits_bought,
            "total_stars_earned": total_stars,
            "total_requests": total_requests,
            "active_credits_balance": active_balance,
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


def user_history(telegram_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM requests WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
