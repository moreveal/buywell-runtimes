from __future__ import annotations

from logging import getLogger

from playerokapi.enums import EventTypes

from .bridge import runtime
from .meta import *
from .telegram_ui import on_telegram_bot_init, router


logger = getLogger(NAME)
_module = None


def get_module():
    return _module


async def on_module_enabled(module):
    global _module
    _module = module
    runtime.enable()
    logger.info("%s Модуль подключен и активен", PREFIX)


async def on_module_disabled(module):
    del module
    runtime.disable()


async def on_playerok_bot_init(bot):
    runtime.attach_bot(bot)


async def on_new_deal(bot, event):
    runtime.handle_purchase(bot, event)


async def on_new_message(bot, event):
    runtime.handle_message(bot, event)


BOT_EVENT_HANDLERS = {
    "ON_MODULE_ENABLED": [on_module_enabled],
    "ON_MODULE_DISABLED": [on_module_disabled],
    "ON_PLAYEROK_BOT_INIT": [on_playerok_bot_init],
    "ON_TELEGRAM_BOT_INIT": [on_telegram_bot_init],
}

PLAYEROK_EVENT_HANDLERS = {
    EventTypes.NEW_DEAL: [on_new_deal],
    EventTypes.NEW_MESSAGE: [on_new_message],
}

TELEGRAM_BOT_ROUTERS = [router]
