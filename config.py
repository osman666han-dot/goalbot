# -*- coding: utf-8 -*-
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# Telegram ID владельца — единственный, кто получает доступ к /admin.
# Узнать свой ID можно у бота @userinfobot.
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))

# Юзернейм для команды /support (с собакой, например @osman666han)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@osman666han")

# Путь к PDF-мануалу, который раздаёт /manual
MANUAL_FILE_PATH = os.getenv("MANUAL_FILE_PATH", "manual.pdf")

# Стоимость 1 кредита в звёздах Telegram Stars
CREDIT_PRICE_STARS = int(os.getenv("CREDIT_PRICE_STARS", "100"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Проверь .env")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY не задан. Проверь .env")
if OWNER_TELEGRAM_ID == 0:
    raise RuntimeError("OWNER_TELEGRAM_ID не задан. Узнай свой ID у @userinfobot и впиши в .env")
