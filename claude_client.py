# -*- coding: utf-8 -*-
"""
Обёртка над Anthropic API для проверки целей.
"""
from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from prompts import SYSTEM_PROMPT

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

MAX_TOKENS = 3000

# Строка-маркер финальной памятки — используется, чтобы код мог определить
# успешное прохождение рамки и закрыть цикл попыток.
SUCCESS_MARKER = "Цель полностью проходит рамку"


async def check_goal_once(goal_text: str) -> str:
    """Режим /check — разовая проверка целиком присланной формулировки."""
    response = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Вот моя формулировка цели, разбери её по всем пунктам "
                    "рамки цели (быстрая проверка целиком):\n\n" + goal_text
                ),
            }
        ],
    )
    return _extract_text(response)


async def step_dialogue(history: list[dict]) -> str:
    """
    Режим /step — пошаговый диалог.
    history — список сообщений в формате Anthropic messages API
    (роли 'user' и 'assistant'), без system-сообщения.
    """
    response = await client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    return _extract_text(response)


def goal_reached(bot_response: str) -> bool:
    """Проверяет, содержит ли ответ финальную памятку об успехе."""
    return SUCCESS_MARKER in bot_response


def _extract_text(response) -> str:
    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip() or "Не получилось сформировать ответ, попробуй ещё раз."
