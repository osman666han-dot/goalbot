# -*- coding: utf-8 -*-
"""
Telegram-бот "Самоосуществлятор целей" — платная проверка целей по рамке
через Claude API. Оплата — Telegram Stars. Админка — команды в самом боте.

Запуск: python bot.py
"""
import asyncio
import hashlib
import logging
import os

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, FSInputFile, BotCommand,
)

import db
from config import (
    TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID, SUPPORT_USERNAME,
    MANUAL_FILE_PATH, CREDIT_PRICE_STARS, CHANNEL_USERNAME,
)
from prompts import (
    WELCOME_TEXT, STEP_START_TEXT, BALANCE_TEXT_TEMPLATE, NO_CREDITS_TEXT,
    MANUAL_CAPTION, SUPPORT_TEXT, HELP_TEXT, BALANCE_CHANGED_NOTICE,
    CREDIT_CONTINUED_NOTICE, SUBSCRIBE_REQUIRED_TEXT, SUBSCRIBE_STILL_MISSING_TEXT,
)
from claude_client import check_goal_once, step_dialogue, goal_reached

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

router = Router()

step_histories: dict[int, list[dict]] = {}

TELEGRAM_MESSAGE_LIMIT = 4000


class GoalStates(StatesGroup):
    waiting_check = State()
    in_step_dialogue = State()


def buy_credits_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Купить 1 кредит ({CREDIT_PRICE_STARS} Stars)", callback_data="buy_1")],
    ])


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Быстрая проверка", callback_data="mode_check")],
        [InlineKeyboardButton(text="🧭 Пошаговый разбор", callback_data="mode_step")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="show_balance")],
        [InlineKeyboardButton(text="📄 Бесплатный мануал", callback_data="show_manual")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="show_support")],
    ])


def is_owner(telegram_id: int) -> bool:
    return telegram_id == OWNER_TELEGRAM_ID


def subscribe_gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Я подписался, проверить", callback_data="recheck_sub")],
    ])


async def is_subscribed(bot: Bot, telegram_id: int) -> bool:
    """Проверяет подписку на канал через Telegram Bot API. Бот должен быть
    администратором канала — иначе проверка всегда будет падать с ошибкой.
    Fail-safe: если сама проверка технически не удалась (сбой API, таймаут) —
    пропускаем пользователя, а не блокируем из-за временной неполадки."""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=telegram_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        log.warning(f"Не удалось проверить подписку для {telegram_id}, пропускаем (fail-safe)")
        return True


async def require_subscription(event, bot: Bot) -> bool:
    """Гейт-функция, вызывается в начале КАЖДОГО пользовательского действия
    (кроме админ-команд). Возвращает True, если можно продолжать; если False —
    сама уже отправила пользователю сообщение с просьбой подписаться."""
    telegram_id = event.from_user.id
    if is_owner(telegram_id):
        return True  # владелец не гейтится, чтобы не потерять доступ к своему же боту
    if await is_subscribed(bot, telegram_id):
        return True
    if isinstance(event, CallbackQuery):
        await event.message.answer(SUBSCRIBE_REQUIRED_TEXT, reply_markup=subscribe_gate_kb())
        await event.answer()
    else:
        await event.answer(SUBSCRIBE_REQUIRED_TEXT, reply_markup=subscribe_gate_kb())
    return False


def hash_text(text: str) -> str:
    normalized = text.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def send_long_message(bot: Bot, chat_id: int, text: str, reply_markup=None):
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return

    chunks = []
    remaining = text
    while len(remaining) > TELEGRAM_MESSAGE_LIMIT:
        split_at = remaining.rfind("\n\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at == -1:
            split_at = TELEGRAM_MESSAGE_LIMIT
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await bot.send_message(chat_id, chunk, reply_markup=reply_markup if is_last else None)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    db.get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "recheck_sub")
async def cb_recheck_sub(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if await is_subscribed(bot, callback.from_user.id):
        await state.clear()
        db.get_or_create_user(callback.from_user.id, callback.from_user.username)
        await callback.message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
    else:
        await callback.message.answer(SUBSCRIBE_STILL_MISSING_TEXT, reply_markup=subscribe_gate_kb())
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


def _balance_text(telegram_id: int, user) -> str:
    free_status = "доступен" if db.has_free_credit_today(telegram_id) else "использован сегодня"
    return BALANCE_TEXT_TEMPLATE.format(free_status=free_status, credits=user["credits_balance"])


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(_balance_text(message.from_user.id, user), reply_markup=buy_credits_kb())


@router.callback_query(F.data == "show_balance")
async def cb_show_balance(callback: CallbackQuery):
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    await callback.message.answer(_balance_text(callback.from_user.id, user), reply_markup=buy_credits_kb())
    await callback.answer()


async def _send_manual(chat_id: int, bot: Bot):
    if os.path.exists(MANUAL_FILE_PATH):
        await bot.send_document(chat_id, FSInputFile(MANUAL_FILE_PATH), caption=MANUAL_CAPTION)
    else:
        await bot.send_message(chat_id, "Мануал временно недоступен, напиши в /support.")


@router.message(Command("manual"))
async def cmd_manual(message: Message, bot: Bot):
    if not await require_subscription(message, bot):
        return
    await _send_manual(message.chat.id, bot)


@router.callback_query(F.data == "show_manual")
async def cb_show_manual(callback: CallbackQuery, bot: Bot):
    if not await require_subscription(callback, bot):
        return
    await _send_manual(callback.message.chat.id, bot)
    await callback.answer()


@router.message(Command("support"))
async def cmd_support(message: Message):
    await message.answer(SUPPORT_TEXT.format(support_username=SUPPORT_USERNAME))


@router.callback_query(F.data == "show_support")
async def cb_show_support(callback: CallbackQuery):
    await callback.message.answer(SUPPORT_TEXT.format(support_username=SUPPORT_USERNAME))
    await callback.answer()


@router.callback_query(F.data == "buy_1")
async def cb_buy_1(callback: CallbackQuery, bot: Bot):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="1 кредит — Самоосуществлятор целей",
        description="1 кредит = 5 попыток разобрать одну цель.",
        payload=f"credit_1_{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="1 кредит", amount=CREDIT_PRICE_STARS)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    db.get_or_create_user(message.from_user.id, message.from_user.username)
    db.add_credits(message.from_user.id, 1)
    db.log_payment(
        telegram_id=message.from_user.id,
        credits_added=1,
        stars_amount=payment.total_amount,
        charge_id=payment.telegram_payment_charge_id,
    )
    user = db.get_user(message.from_user.id)
    await message.answer(f"Зачислено. Баланс: {user['credits_balance']} кредитов.")


@router.callback_query(F.data == "mode_check")
async def cb_mode_check(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await require_subscription(callback, bot):
        return
    await state.set_state(GoalStates.waiting_check)
    await callback.message.answer("Пришли формулировку цели целиком одним сообщением.")
    await callback.answer()


@router.callback_query(F.data == "mode_step")
async def cb_mode_step(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await require_subscription(callback, bot):
        return
    step_histories[callback.from_user.id] = []
    await state.set_state(GoalStates.in_step_dialogue)
    await callback.message.answer(STEP_START_TEXT)
    await callback.answer()


@router.message(Command("check"))
async def cmd_check(message: Message, state: FSMContext, bot: Bot):
    if not await require_subscription(message, bot):
        return
    await state.set_state(GoalStates.waiting_check)
    await message.answer("Пришли формулировку цели целиком одним сообщением.")


@router.message(Command("step"))
async def cmd_step(message: Message, state: FSMContext, bot: Bot):
    if not await require_subscription(message, bot):
        return
    step_histories[message.from_user.id] = []
    await state.set_state(GoalStates.in_step_dialogue)
    await message.answer(STEP_START_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    step_histories.pop(message.from_user.id, None)
    await message.answer("Прервал. /check — быстрая проверка, /step — пошаговый разбор.")


async def _reject_no_credits(message: Message):
    await message.answer(NO_CREDITS_TEXT.format(price=CREDIT_PRICE_STARS), reply_markup=buy_credits_kb())


@router.message(GoalStates.waiting_check, ~F.text.startswith("/"))
async def handle_check(message: Message, bot: Bot, state: FSMContext):
    telegram_id = message.from_user.id
    db.get_or_create_user(telegram_id, message.from_user.username)

    if not await require_subscription(message, bot):
        await state.clear()
        return

    if not db.has_available_attempt(telegram_id):
        await _reject_no_credits(message)
        await state.clear()
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    text_hash = hash_text(message.text)
    cached = db.get_cached_response(text_hash)
    if cached is not None:
        result = cached
    else:
        try:
            result = await check_goal_once(message.text)
        except Exception:
            log.exception("Claude API error in check mode")
            await message.answer("Что-то сломалось на стороне нейросети. Попробуй ещё раз — попытка не списана.")
            return
        db.cache_response(text_hash, result)

    reached = goal_reached(result)
    outcome = db.commit_attempt(telegram_id, goal_reached=reached)
    db.log_request(telegram_id, "check", message.text, result, charged=True)

    await send_long_message(bot, message.chat.id, result)

    if outcome["reason"] == "success":
        await state.clear()
    elif outcome["reason"] == "continued":
        await message.answer(CREDIT_CONTINUED_NOTICE.format(credits=outcome["credits_balance"]))
    elif outcome["reason"] == "exhausted_no_credits":
        await message.answer("Попытки на эту цель закончились. /balance для пополнения.")
        await state.clear()


@router.message(GoalStates.in_step_dialogue, ~F.text.startswith("/"))
async def handle_step(message: Message, bot: Bot, state: FSMContext):
    telegram_id = message.from_user.id
    db.get_or_create_user(telegram_id, message.from_user.username)

    if not await require_subscription(message, bot):
        await state.clear()
        step_histories.pop(telegram_id, None)
        return

    if not db.has_available_attempt(telegram_id):
        await _reject_no_credits(message)
        await state.clear()
        step_histories.pop(telegram_id, None)
        return

    history = step_histories.setdefault(telegram_id, [])
    history.append({"role": "user", "content": message.text})

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        reply = await step_dialogue(history)
    except Exception:
        log.exception("Claude API error in step mode")
        history.pop()
        await message.answer("Что-то сломалось на стороне нейросети. Попробуй ещё раз — попытка не списана.")
        return

    history.append({"role": "assistant", "content": reply})

    reached = goal_reached(reply)
    outcome = db.commit_attempt(telegram_id, goal_reached=reached)
    db.log_request(telegram_id, "step", message.text, reply, charged=True)

    await send_long_message(bot, message.chat.id, reply)

    if outcome["reason"] == "success":
        await state.clear()
        step_histories.pop(telegram_id, None)
    elif outcome["reason"] == "continued":
        await message.answer(CREDIT_CONTINUED_NOTICE.format(credits=outcome["credits_balance"]))
    elif outcome["reason"] == "exhausted_no_credits":
        await message.answer("Попытки на эту цель закончились. /balance для пополнения.")
        await state.clear()
        step_histories.pop(telegram_id, None)


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Последние запросы", callback_data="admin_recent")],
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_topusers")],
    ])


def _admin_stats_text() -> str:
    stats = db.admin_stats()
    return (
        f"Пользователей: {stats['total_users']}\n"
        f"Заблокировали бота: {stats['blocked_count']}\n"
        f"Куплено кредитов всего: {stats['total_credits_bought']}\n"
        f"Заработано Stars всего: {stats['total_stars_earned']}\n"
        f"Запросов к API всего: {stats['total_requests']}\n"
        f"Сумма непотраченных кредитов у юзеров: {stats['active_credits_balance']}\n\n"
        f"Команды:\n"
        f"/recent — последние запросы\n"
        f"/topusers — список пользователей и балансов\n"
        f"/userhistory <telegram_id> — история конкретного юзера\n"
        f"/viewrequest <id> — полный текст одного запроса\n"
        f"/setbalance <telegram_id> <число> — выставить баланс вручную\n"
        f"/broadcast <текст> — разослать сообщение всем пользователям"
    )


def _recent_text() -> str:
    rows = db.recent_requests(15)
    if not rows:
        return "Пока пусто."
    lines = [f"[{r['id']}] {r['telegram_id']} ({r['mode']}): {r['user_message'][:80]}" for r in rows]
    return "\n".join(lines)


def _topusers_text() -> str:
    rows = db.top_users(20)
    if not rows:
        return "Пока пусто."
    lines = [f"{r['telegram_id']} (@{r['username']}): {r['credits_balance']} кредитов" for r in rows]
    return "\n".join(lines)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        text = _admin_stats_text()
    except Exception as e:
        log.exception("Error in /admin")
        text = f"Ошибка в /admin: {type(e).__name__}: {e}"
    await message.answer(text, reply_markup=admin_panel_kb())


@router.callback_query(F.data == "admin_recent")
async def cb_admin_recent(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        return
    await callback.message.answer(_recent_text())
    await callback.answer()


@router.callback_query(F.data == "admin_topusers")
async def cb_admin_topusers(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        return
    await callback.message.answer(_topusers_text())
    await callback.answer()


@router.message(Command("recent"))
async def cmd_recent(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer(_recent_text())


@router.message(Command("topusers"))
async def cmd_topusers(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer(_topusers_text())


@router.message(Command("userhistory"))
async def cmd_userhistory(message: Message):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /userhistory <telegram_id>")
        return
    telegram_id = int(parts[1])
    rows = db.user_history(telegram_id, 20)
    if not rows:
        await message.answer("История пуста.")
        return
    lines = []
    for r in rows:
        lines.append(f"[{r['mode']}] {r['user_message'][:100]}")
    await message.answer("\n".join(lines))


@router.message(Command("viewrequest"))
async def cmd_viewrequest(message: Message, bot: Bot):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /viewrequest <id>")
        return
    request_id = int(parts[1])
    row = db.get_request_by_id(request_id)
    if not row:
        await message.answer(f"Запрос с id {request_id} не найден.")
        return
    text = (
        f"[{row['id']}] telegram_id: {row['telegram_id']}\n"
        f"Режим: {row['mode']}\n\n"
        f"— Сообщение пользователя —\n{row['user_message']}\n\n"
        f"— Ответ бота —\n{row['bot_response']}"
    )
    await send_long_message(bot, message.chat.id, text)


@router.message(Command("setbalance"))
async def cmd_setbalance(message: Message, bot: Bot):
    if not is_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Использование: /setbalance <telegram_id> <число>")
        return
    telegram_id = int(parts[1])
    amount = int(parts[2])
    db.get_or_create_user(telegram_id, None)
    db.set_credits(telegram_id, amount)
    await message.answer(f"Баланс {telegram_id} установлен: {amount} кредитов.")

    try:
        await bot.send_message(telegram_id, BALANCE_CHANGED_NOTICE.format(credits=amount))
        db.mark_unblocked(telegram_id)
    except TelegramForbiddenError:
        db.mark_blocked(telegram_id)
        log.info(f"Пользователь {telegram_id} заблокировал бота")
    except Exception:
        log.warning(f"Не удалось уведомить пользователя {telegram_id} об изменении баланса")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    if not is_owner(message.from_user.id):
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Использование: /broadcast <текст сообщения>")
        return

    user_ids = db.get_all_user_ids()
    await message.answer(f"Начинаю рассылку на {len(user_ids)} пользователей...")

    sent = 0
    failed = 0
    for telegram_id in user_ids:
        try:
            await bot.send_message(telegram_id, text)
            db.mark_unblocked(telegram_id)
            sent += 1
        except TelegramForbiddenError:
            db.mark_blocked(telegram_id)
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(f"Рассылка завершена. Доставлено: {sent}. Не доставлено: {failed}.")


@router.message()
async def fallback(message: Message):
    await message.answer(
        "Не понял. /check — быстрая проверка, /step — пошаговый разбор, /balance — баланс."
    )


async def _set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="check", description="Быстрая проверка цели"),
        BotCommand(command="step", description="Пошаговый разбор"),
        BotCommand(command="balance", description="Баланс и пополнение"),
        BotCommand(command="manual", description="Бесплатный мануал"),
        BotCommand(command="support", description="Поддержка"),
        BotCommand(command="help", description="Список команд"),
    ])


async def main():
    db.init_db()
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await _set_commands(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
