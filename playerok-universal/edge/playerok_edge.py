from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator
from edge.vendor.playerokapi.account import Account
from edge.vendor.playerokapi.enums import EventTypes, ItemStatuses
from edge.vendor.playerokapi.exceptions import (
    BotCheckDetectedException,
    UnauthorizedError,
)
from edge.vendor.playerokapi.listener.listener import EventListener

from buywell_edge_sdk import Health, HealthState, configuration_field, module


class PlayerokConfiguration(BaseModel):
    token: SecretStr | None = configuration_field(
        label={"ru": "Токен аккаунта", "en": "Account token"},
        default=None,
        description="Playerok account token used by existing Playerok Universal setups",
    )
    ddg5: SecretStr | None = configuration_field(
        label={"ru": "Cookie __ddg5_", "en": "__ddg5_ cookie"},
        default=None,
        description="Optional __ddg5_ cookie bound to the same IP and User-Agent",
    )
    cookies: SecretStr | None = configuration_field(
        label={"ru": "Cookie аккаунта", "en": "Account cookies"},
        default=None,
        description="Full Playerok Cookie header; an alternative to token and ddg5",
    )
    user_agent: str = configuration_field(
        label={"ru": "User-Agent браузера", "en": "Browser User-Agent"},
        min_length=20,
        max_length=1_000,
    )
    proxy: SecretStr | None = configuration_field(
        label={"ru": "Прокси", "en": "Proxy"},
        default=None,
    )
    request_timeout_seconds: int = configuration_field(
        label={"ru": "Тайм-аут запросов, секунд", "en": "Request timeout, seconds"},
        default=30,
        ge=5,
        le=120,
    )

    @model_validator(mode="after")
    def require_session(self) -> "PlayerokConfiguration":
        if not self.token and not self.cookies:
            raise ValueError("Provide either the Playerok token or full cookies")
        return self


class SendMessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)


extension = module(
    extension_id="playerok.universal",
    version="1.0.6",
    display_name={"ru": "Playerok", "en": "Playerok"},
    description={
        "ru": "Продажи и сообщения Playerok без Playerok Universal и Telegram-бота",
        "en": "Playerok sales and messages without Playerok Universal or a Telegram bot",
    },
    publisher="Buywell",
    entrypoint="edge.playerok_edge:extension",
    config_model=PlayerokConfiguration,
    network_domains=["playerok.com", "www.playerok.com"],
    dependencies=[
        "certifi==2026.7.22",
        "cffi==2.1.0",
        "charset-normalizer==3.4.4",
        "curl-cffi==0.13.0",
        "idna==3.11",
        "pycparser==3.0",
        "requests==2.32.3",
        "tqdm==4.67.1",
        "urllib3==2.6.3",
        "websocket-client==1.8.0",
        "wrapper-tls-requests==1.1.4",
    ],
    legacy_manifest=Path(__file__).parents[1] / "manifest.json",
    guides={"ru": "edge/README.ru.md", "en": "edge/README.en.md"},
    changelog={"ru": "edge/CHANGELOG.ru.md", "en": "edge/CHANGELOG.en.md"},
)


@dataclass
class ConnectionState:
    account: Account | None = None
    listener: EventListener | None = None
    task: asyncio.Task[None] | None = None
    error: Exception | None = None
    last_success_at: str | None = None
    catalog_cache: tuple[float, list[Any]] = field(
        default_factory=lambda: (0.0, [])
    )


_states: dict[str, ConnectionState] = {}


def _connect_account(config: PlayerokConfiguration) -> Account:
    # PlayerokAPI constructs TLS clients synchronously, so both construction
    # and the initial probe must stay outside the Edge asyncio event loop.
    account = Account(
        token=config.token.get_secret_value() if config.token else None,
        ddg5=config.ddg5.get_secret_value() if config.ddg5 else "",
        cookies=config.cookies.get_secret_value() if config.cookies else None,
        user_agent=config.user_agent,
        proxy=config.proxy.get_secret_value() if config.proxy else None,
        requests_timeout=config.request_timeout_seconds,
    )
    account.get()
    return account


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result else None


def _identifier(value: Any) -> str | None:
    return _text(getattr(value, "id", value))


def _enum_name(value: Any) -> str | None:
    return _text(getattr(value, "name", value))


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value if item is not None]
    if hasattr(value, "name") and not isinstance(value, str):
        return str(value.name)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _item_values(item: Any, deal: Any = None) -> tuple[dict[str, Any], dict[str, str]]:
    values: dict[str, Any] = {}
    choice_ids: dict[str, str] = {}
    item_id = _identifier(item)
    if item_id:
        values["__item"] = _text(getattr(item, "name", None)) or item_id
        choice_ids["__item"] = item_id
    obtaining = getattr(item, "obtaining_type", None)
    obtaining_id = _identifier(obtaining)
    if obtaining_id:
        values["__obtaining_type"] = _text(getattr(obtaining, "name", None)) or obtaining_id
        choice_ids["__obtaining_type"] = obtaining_id
    options = getattr(getattr(item, "category", None), "options", None) or []
    for raw_key, raw_value in (getattr(item, "attributes", None) or {}).items():
        key = str(raw_key)
        selected = next(
            (
                option
                for option in options
                if str(getattr(option, "field", "")) == key
                and str(getattr(option, "value", "")) == str(raw_value)
            ),
            None,
        )
        values[key] = _text(getattr(selected, "label", None)) or _text(raw_value) or ""
        choice_ids[key] = _text(raw_value) or ""
    for data_field in getattr(deal, "obtaining_fields", None) or []:
        if bool(getattr(data_field, "hidden", False)):
            continue
        key = _identifier(data_field)
        value = getattr(data_field, "value", None)
        if key and value is not None:
            values[key] = _clean(value)
    return values, choice_ids


def _item_payload(item: Any) -> dict[str, Any]:
    category = getattr(item, "category", None)
    game = getattr(item, "game", None)
    obtaining = getattr(item, "obtaining_type", None)
    return _clean(
        {
            "id": _identifier(item),
            "name": _text(getattr(item, "name", None)),
            "price": getattr(item, "price", None),
            "gameId": _identifier(game),
            "gameName": _text(getattr(game, "name", None)),
            "categoryId": _identifier(category),
            "categoryName": _text(getattr(category, "name", None)),
            "obtainingTypeId": _identifier(obtaining),
            "obtainingTypeName": _text(getattr(obtaining, "name", None)),
        }
    )


def _scope_from_item(item: Any) -> dict[str, Any]:
    return _clean(
        {
            "itemId": _identifier(item),
            "categoryId": _identifier(getattr(item, "category", None)),
            "gameId": _identifier(getattr(item, "game", None)),
        }
    )


def _event_version(event_type: str) -> str:
    return "1.1.0" if event_type in {
        "commerce.purchase.created",
        "messaging.message.received",
    } else "1.0.0"


def _captured(session: Any, event_type: str) -> bool:
    specification = session.capture_specification
    return bool(specification) and any(
        item.get("eventType") == event_type
        and item.get("eventVersion") == _event_version(event_type)
        for item in specification.get("subscriptions", [])
    )


async def _purchase(session: Any, state: ConnectionState, event: Any) -> None:
    account = state.account
    deal = getattr(event, "deal", None)
    chat = getattr(event, "chat", None) or getattr(deal, "chat", None)
    buyer = getattr(deal, "user", None)
    if (
        not account
        or not deal
        or not _identifier(deal)
        or not _identifier(chat)
        or not _identifier(buyer)
        or _identifier(buyer) == _identifier(account)
        or _enum_name(getattr(deal, "direction", None)) != "OUT"
    ):
        return
    item = getattr(deal, "item", None)
    if not _identifier(item):
        return
    if not getattr(item, "category", None) or not getattr(item, "game", None):
        with contextlib.suppress(Exception):
            item = await asyncio.to_thread(account.get_item, id=_identifier(item))
    values, choice_ids = _item_values(item, deal)
    payload = _clean(
        {
            "dealId": _identifier(deal),
            "status": _enum_name(getattr(deal, "status", None)) or "PAID",
            "createdAt": _text(getattr(deal, "created_at", None))
            or datetime.now(UTC).isoformat(),
            "item": _item_payload(item),
            "buyer": {
                "id": _identifier(buyer),
                "username": _text(getattr(buyer, "username", None)),
            },
            "fieldValues": values,
            "fieldChoiceIds": choice_ids,
        }
    )
    scope = _clean(
        {
            "dealId": _identifier(deal),
            "chatId": _identifier(chat),
            **_scope_from_item(item),
            "buyerId": _identifier(buyer),
            "returnUrl": f"https://playerok.com/deal/{_identifier(deal)}",
        }
    )
    if _captured(session, "commerce.purchase.created"):
        await session.emit_event(
            "commerce.purchase.created",
            "1.1.0",
            payload,
            scope,
            event_id=f"playerok:purchase:{_identifier(deal)}",
        )


async def _message(session: Any, state: ConnectionState, event: Any) -> None:
    account = state.account
    message = getattr(event, "message", None)
    chat = getattr(event, "chat", None)
    sender = getattr(message, "user", None)
    if (
        not account
        or not message
        or not chat
        or not sender
        or _identifier(sender) == _identifier(account)
        or getattr(message, "event", None) is not None
    ):
        return
    chat_id = _identifier(chat)
    if not chat_id or chat_id in {
        _text(getattr(account, "support_chat_id", None)),
        _text(getattr(account, "system_chat_id", None)),
    }:
        return
    text = _text(getattr(message, "text", None)) or ""
    deal = getattr(message, "deal", None)
    item = getattr(deal, "item", None) if deal else getattr(message, "item", None)
    if _identifier(item) and (
        not getattr(item, "category", None) or not getattr(item, "game", None)
    ):
        with contextlib.suppress(Exception):
            item = await asyncio.to_thread(account.get_item, id=_identifier(item))
    values, choice_ids = _item_values(item, deal) if item else ({}, {})
    payload = _clean(
        {
            "messageId": _identifier(message),
            "text": text,
            "createdAt": _text(getattr(message, "created_at", None))
            or datetime.now(UTC).isoformat(),
            "images": [
                str(url)
                for image in (getattr(message, "images", None) or [])
                if (url := getattr(image, "url", None))
            ],
            "sender": {
                "id": _identifier(sender),
                "username": _text(getattr(sender, "username", None)),
            },
            "item": _item_payload(item) if _identifier(item) else None,
            "fieldValues": values,
            "fieldChoiceIds": choice_ids,
        }
    )
    scope = _clean(
        {
            "messageId": _identifier(message),
            "chatId": chat_id,
            **(_scope_from_item(item) if _identifier(item) else {}),
            "buyerId": _identifier(sender),
            "returnUrl": (
                f"https://playerok.com/deal/{_identifier(deal)}"
                if _identifier(deal)
                else "https://playerok.com/chats"
            ),
        }
    )
    if _captured(session, "messaging.message.received"):
        await session.emit_event(
            "messaging.message.received",
            "1.1.0",
            payload,
            scope,
            event_id=f"playerok:message:{_identifier(message)}",
        )


async def _run(session: Any, state: ConnectionState) -> None:
    while True:
        try:
            config = PlayerokConfiguration.model_validate({**session.config, **session.secrets})
            account = await asyncio.to_thread(_connect_account, config)
            listener = EventListener(account)
            state.account = account
            state.listener = listener
            state.error = None
            state.last_success_at = datetime.now(UTC).isoformat()
            iterator = listener.listen(get_new_review_events=False)
            while True:
                event = await asyncio.to_thread(next, iterator)
                state.last_success_at = datetime.now(UTC).isoformat()
                if event.type in {EventTypes.NEW_DEAL, EventTypes.ITEM_PAID}:
                    await _purchase(session, state, event)
                elif event.type is EventTypes.NEW_MESSAGE:
                    await _message(session, state, event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.error = error
            state.account = None
            state.listener = None
            await asyncio.sleep(
                15
                if isinstance(error, (UnauthorizedError, BotCheckDetectedException))
                else 5
            )


@extension.on_start
async def start(session: Any) -> None:
    state = ConnectionState()
    _states[session.connection_id] = state
    state.task = asyncio.create_task(_run(session, state))


@extension.on_stop
async def stop(session: Any) -> None:
    state = _states.pop(session.connection_id, None)
    if state and state.task:
        state.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.task


@extension.health
async def health(session: Any) -> Health:
    state = _states.get(session.connection_id)
    if not state or not state.account:
        if state and isinstance(state.error, (UnauthorizedError, BotCheckDetectedException)):
            return Health(
                state=HealthState.AUTH_REQUIRED,
                message=(
                    "Playerok session is expired"
                    if isinstance(state.error, UnauthorizedError)
                    else "Playerok requires browser verification"
                ),
            )
        return Health(
            state=HealthState.DEGRADED,
            message=str(state.error)[:500] if state and state.error else "Connecting to Playerok",
        )
    return Health(state=HealthState.HEALTHY, last_success_at=state.last_success_at)


@extension.action(
    "playerok.universal/send-message",
    "1.0.0",
    input_model=SendMessageInput,
    display_name={"ru": "Отправить сообщение", "en": "Send message"},
)
async def send_message(context: Any, value: SendMessageInput) -> dict[str, Any]:
    state = _states.get(context.connection_id)
    if not state or not state.account:
        raise RuntimeError("Playerok session is unavailable")
    chat_id = str(context.event_scope.get("chatId") or "")
    if not chat_id:
        raise ValueError("Playerok chat context is unavailable")
    await asyncio.to_thread(
        state.account.send_message,
        chat_id=chat_id,
        text=value.message,
    )
    return {}


def _items(state: ConnectionState) -> list[Any]:
    now = time.monotonic()
    expires_at, cached = state.catalog_cache
    if expires_at > now:
        return list(cached)
    if not state.account:
        raise RuntimeError("Playerok session is unavailable")
    statuses = [
        ItemStatuses.PENDING_APPROVAL,
        ItemStatuses.PENDING_MODERATION,
        ItemStatuses.APPROVED,
        ItemStatuses.EXPIRED,
        ItemStatuses.SOLD,
        ItemStatuses.DRAFT,
    ]
    result: list[Any] = []
    cursor = None
    while len(result) < 2_000:
        page = state.account.get_my_items(
            statuses=statuses,
            count=24,
            after_cursor=cursor,
        )
        result.extend(list(getattr(page, "items", None) or []))
        page_info = getattr(page, "page_info", None)
        if not page_info or not getattr(page_info, "has_next_page", False):
            break
        cursor = getattr(page_info, "end_cursor", None)
        if not cursor:
            break
    detailed = []
    for item in result:
        try:
            detailed.append(state.account.get_item(id=_identifier(item)))
        except Exception:
            detailed.append(item)
    state.catalog_cache = (now + 60, detailed)
    return list(detailed)


def _category_label(item: Any) -> str:
    return " · ".join(
        value
        for value in (
            _text(getattr(getattr(item, "game", None), "name", None)),
            _text(getattr(getattr(item, "category", None), "name", None)),
        )
        if value
    ) or _identifier(getattr(item, "category", None)) or "Playerok category"


@extension.binding_catalog("playerok.categories", "1.0.0")
async def categories(context: Any, job: dict[str, Any]) -> dict[str, Any]:
    state = _states[context.connection_id]
    items = await asyncio.to_thread(_items, state)
    identity = {
        "protocolVersion": "1.0.0",
        "requestId": str(job.get("requestId", "")),
        "catalogId": "playerok.categories",
        "catalogVersion": "1.0.0",
    }
    operation = str(job.get("operation", ""))
    if operation == "list-scopes":
        query = str(job.get("query", "")).casefold().strip()
        scopes = {
            str(category_id): {
                "key": str(category_id),
                "label": _category_label(item)[:500],
            }
            for item in items
            if (category_id := _identifier(getattr(item, "category", None)))
        }
        filtered = [
            scope
            for scope in scopes.values()
            if not query or query in f"{scope['key']} {scope['label']}".casefold()
        ]
        filtered.sort(key=lambda item: (item["label"].casefold(), item["key"]))
        offset = max(0, int(job.get("cursor") or 0))
        page = filtered[offset : offset + 100]
        return {
            **identity,
            "operation": operation,
            "scopes": page,
            **({"nextCursor": str(offset + len(page))} if offset + len(page) < len(filtered) else {}),
        }
    if operation != "get-scope":
        raise ValueError("Unsupported Playerok catalog operation")
    scope_key = str(job.get("scopeKey") or "")
    scoped = [
        item
        for item in items
        if _identifier(getattr(item, "category", None)) == scope_key
    ]
    if not scoped:
        raise ValueError("No seller items are available in this Playerok category")
    return {
        **identity,
        "operation": operation,
        "scope": {"key": scope_key, "label": _category_label(scoped[0])[:500]},
        "fields": [
            {
                "key": "__item",
                "label": "Товар Playerok",
                "kind": "choice",
                "choices": [
                    {
                        "key": str(item_id),
                        "label": (
                            _text(getattr(item, "name", None)) or str(item_id)
                        )[:500],
                    }
                    for item in scoped
                    if (item_id := _identifier(item))
                ][:500],
            }
        ],
    }
