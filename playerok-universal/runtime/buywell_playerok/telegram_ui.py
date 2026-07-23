from __future__ import annotations

import asyncio
import html

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from settings import Settings as MainSettings
from tgbot.helpful import do_auth

from .bridge import runtime
from .meta import VERSION


router = Router()
CALLBACK_PREFIX = "buywell_playerok:"


class BuywellStates(StatesGroup):
    waiting_for_connection_key = State()


def _authorized(user_id: int | None) -> bool:
    if user_id is None:
        return False
    config = MainSettings.get("config")
    return user_id in config["telegram"]["bot"]["signed_users"]


def _keyboard() -> InlineKeyboardMarkup:
    status = runtime.status()
    buttons = [
        [
            InlineKeyboardButton(
                text="🔑 Подключить" if not status["configured"] else "🔑 Сменить ключ",
                callback_data=f"{CALLBACK_PREFIX}connect",
            ),
            InlineKeyboardButton(
                text="🔄 Проверить",
                callback_data=f"{CALLBACK_PREFIX}check",
            ),
        ]
    ]
    if status["configured"]:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⛔ Отключить",
                    callback_data=f"{CALLBACK_PREFIX}disconnect",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _status_text(note: str | None = None) -> str:
    status = runtime.status()
    if status["connected"]:
        connection = "🟢 Подключено"
    elif status["configured"]:
        connection = "🟡 Ключ сохранён, соединение устанавливается"
    else:
        connection = "⚪ Не настроено"
    parts = [
        "<b>Buywell · Playerok Universal</b>",
        f"Версия: <code>{VERSION}</code>",
        f"Состояние: <b>{connection}</b>",
    ]
    if status["last_error"]:
        parts.append(
            f"Последняя ошибка: <code>{html.escape(status['last_error'][:300])}</code>"
        )
    if note:
        parts.append(html.escape(note))
    parts.append(
        "Ключ хранится только на этом устройстве. Cookies Playerok в Buywell не передаются."
    )
    return "\n\n".join(parts)


async def _show(message: types.Message, note: str | None = None) -> None:
    await message.answer(
        _status_text(note),
        reply_markup=_keyboard(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.message(Command("buywell"))
async def command_buywell(message: types.Message, state: FSMContext):
    await state.clear()
    if not _authorized(message.from_user.id if message.from_user else None):
        return await do_auth(message, state)
    await _show(message)


@router.callback_query(F.data.startswith(CALLBACK_PREFIX))
async def callback_buywell(callback: types.CallbackQuery, state: FSMContext):
    if not _authorized(callback.from_user.id if callback.from_user else None):
        if callback.message:
            await do_auth(callback.message, state)
        return await callback.answer()
    action = (callback.data or "")[len(CALLBACK_PREFIX) :]
    if action == "connect":
        await state.set_state(BuywellStates.waiting_for_connection_key)
        if callback.message:
            await callback.message.answer(
                "<b>Отправьте ключ подключения Buywell.</b>\n\n"
                "Сообщение с ключом будет удалено сразу после чтения.",
                parse_mode="HTML",
            )
    elif action == "disconnect":
        await state.clear()
        runtime.disconnect()
        if callback.message:
            await _show(callback.message, "Соединение отключено. Локальная очередь сохранена.")
    elif action == "check":
        try:
            await asyncio.to_thread(runtime.check_connection)
            note = "Ключ принят Buywell."
        except Exception as error:
            note = f"Проверка не пройдена: {str(error)[:240]}"
        if callback.message:
            await _show(callback.message, note)
    await callback.answer()


@router.message(BuywellStates.waiting_for_connection_key)
async def receive_connection_key(message: types.Message, state: FSMContext):
    if not _authorized(message.from_user.id if message.from_user else None):
        return await do_auth(message, state)
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if len(token) < 8:
        return await message.answer(
            "Ключ слишком короткий. Отправьте полный ключ подключения Buywell."
        )
    try:
        runtime.configure_token(token)
    except Exception as error:
        return await message.answer(
            f"Не удалось сохранить ключ: <code>{html.escape(str(error)[:300])}</code>",
            parse_mode="HTML",
        )
    await state.clear()
    await _show(message, "Ключ сохранён. Модуль подключается к Buywell.")


async def on_telegram_bot_init(tgbot) -> None:
    try:
        commands = await tgbot.bot.get_my_commands()
        if not any(command.command == "buywell" for command in commands):
            commands.append(
                BotCommand(
                    command="buywell",
                    description="🔗 Подключение Playerok к Buywell",
                )
            )
            await tgbot.bot.set_my_commands(commands)
    except Exception:
        pass
