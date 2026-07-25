from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_VENDOR_ROOT = Path(__file__).parent / "vendor"
if str(_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ROOT))

from FunPayAPI import Account
from FunPayAPI.common.enums import MessageTypes, OrderStatuses
from FunPayAPI.common.exceptions import UnauthorizedError
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent, OrderStatusChangedEvent
from FunPayAPI.updater.runner import Runner
from pydantic import BaseModel, Field, SecretStr

from buywell_edge_sdk import Health, HealthState, configuration_field, module


class FunPayConfiguration(BaseModel):
    golden_key: SecretStr = configuration_field(
        label={"ru": "Golden Key (cookie аккаунта FunPay)", "en": "Golden Key (FunPay account cookie)"},
    )
    user_agent: str | None = configuration_field(
        label={"ru": "User-Agent браузера", "en": "Browser User-Agent"},
        default=None,
    )
    poll_interval_seconds: float = configuration_field(
        label={"ru": "Интервал проверки, секунд", "en": "Polling interval, seconds"},
        default=6,
        ge=2,
        le=60,
    )


class SendMessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)


extension = module(
    extension_id="funpay.cardinal",
    version="1.3.2",
    display_name={"ru": "FunPay", "en": "FunPay"},
    description={
        "ru": "Продажи и сообщения FunPay без отдельного Telegram-бота",
        "en": "FunPay sales and messages without a separate Telegram bot",
    },
    publisher="Buywell",
    entrypoint="edge.funpay_edge:extension",
    config_model=FunPayConfiguration,
    network_domains=["funpay.com", "www.funpay.com"],
    dependencies=[
        "beautifulsoup4==4.15.0",
        "certifi==2026.7.22",
        "charset-normalizer==2.1.1",
        "idna==3.18",
        "lxml==6.1.1",
        "requests==2.28.1",
        "requests-toolbelt==0.10.1",
        "soupsieve==2.9.1",
        "typing_extensions==4.16.0",
        "urllib3==1.26.20",
    ],
    legacy_manifest=Path(__file__).parents[1] / "manifest.json",
    guides={"ru": "edge/README.ru.md", "en": "edge/README.en.md"},
    changelog={"ru": "edge/CHANGELOG.ru.md", "en": "edge/CHANGELOG.en.md"},
)


@dataclass
class ConnectionState:
    account: Account | None = None
    runner: Runner | None = None
    runner_thread: threading.Thread | None = None
    task: asyncio.Task[None] | None = None
    error: Exception | None = None
    last_success_at: str | None = None
    last_success_monotonic: float | None = None
    poll_interval_seconds: float = 6
    pending_inputs: dict[str, asyncio.Future[str]] = field(default_factory=dict)


_states: dict[str, ConnectionState] = {}


def _mark_success(state: ConnectionState) -> None:
    state.error = None
    state.last_success_at = datetime.now(UTC).isoformat()
    state.last_success_monotonic = time.monotonic()


class _ObservedRunner(Runner):
    def __init__(self, account: Account, state: ConnectionState):
        super().__init__(account)
        self._state = state

    def get_updates(self) -> dict:
        try:
            updates = super().get_updates()
        except Exception as error:
            self._state.error = error
            raise
        _mark_success(self._state)
        return updates


def _start_runner(
    account: Account,
    state: ConnectionState,
    connection_id: str,
) -> _ObservedRunner:
    runner = _ObservedRunner(account, state)
    thread = threading.Thread(
        target=runner.loop,
        name=f"funpay-runner-{connection_id}",
        daemon=True,
    )
    state.runner = runner
    state.runner_thread = thread
    thread.start()
    return runner


def _error_message(error: Exception | None, fallback: str) -> str:
    if error is None:
        return fallback
    short_str = getattr(error, "short_str", None)
    value = short_str() if callable(short_str) else str(error)
    return str(value)[:500] or fallback


def _enum_slug(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name if name is not None else value).strip().lower().replace("_", "-")


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _captured(session: Any, event_type: str, event_version: str) -> bool:
    specification = session.capture_specification
    if not specification:
        return False
    return any(
        item.get("eventType") == event_type and item.get("eventVersion") == event_version
        for item in specification.get("subscriptions", [])
    )


def _order_payload(account: Account, shortcut: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        order = account.get_order(str(shortcut.id))
    except Exception:
        order = shortcut
    subcategory = getattr(order, "subcategory", None)
    category = getattr(subcategory, "category", None)
    payload = _clean({
        "orderId": str(order.id),
        "status": _enum_slug(order.status),
        "createdAt": getattr(getattr(shortcut, "date", None), "isoformat", lambda: None)(),
        "locale": getattr(order, "locale", None),
        "buyer": {
            "id": getattr(order, "buyer_id", None),
            "username": getattr(order, "buyer_username", None),
        },
        "seller": {
            "id": getattr(order, "seller_id", None),
            "username": getattr(order, "seller_username", None),
        },
        "chatId": getattr(order, "chat_id", None),
        "title": getattr(order, "short_description", None) or getattr(shortcut, "description", None),
        "fullDescription": getattr(order, "full_description", None),
        "quantity": getattr(shortcut, "amount", None),
        "price": getattr(order, "sum", None) or getattr(shortcut, "price", None),
        "currency": _enum_slug(getattr(order, "currency", "unknown")),
        "categoryId": str(getattr(subcategory, "id", "")) or None,
        "player": getattr(order, "player", None),
        "subcategory": {
            "id": str(getattr(subcategory, "id", "")) or None,
            "name": getattr(subcategory, "name", None),
            "fullName": getattr(subcategory, "fullname", None),
            "type": _enum_slug(getattr(subcategory, "type", "")),
            "category": {
                "id": str(getattr(category, "id", "")) or None,
                "name": getattr(category, "name", None),
            },
        } if subcategory else None,
    })
    scope = _clean({
        "orderId": str(order.id),
        "chatId": getattr(order, "chat_id", None),
        "buyerId": getattr(order, "buyer_id", None),
        "buyerUsername": getattr(order, "buyer_username", None),
        "categoryId": payload.get("categoryId"),
        "title": payload.get("title"),
    })
    return payload, scope


async def _emit_event(session: Any, state: ConnectionState, event: Any) -> None:
    account = state.account
    if not account:
        return
    if isinstance(event, (NewOrderEvent, OrderStatusChangedEvent)):
        event_type = (
            "commerce.purchase.created"
            if isinstance(event, NewOrderEvent)
            else "commerce.purchase.status-changed"
        )
        version = "1.3.0"
        if isinstance(event, NewOrderEvent) and event.order.status is not OrderStatuses.PAID:
            return
        if not _captured(session, event_type, version):
            return
        payload, scope = await asyncio.to_thread(_order_payload, account, event.order)
        await session.emit_event(
            event_type,
            version,
            payload,
            scope,
            event_id=f"funpay:{account.id}:order:{event.order.id}:{payload['status']}",
        )
        return
    if not isinstance(event, NewMessageEvent):
        return
    message = event.message
    if (
        getattr(message, "by_bot", False)
        or getattr(message, "author_id", 0) in (0, account.id)
        or message.type is not MessageTypes.NON_SYSTEM
    ):
        return
    text = str(message).strip()
    if not text:
        return
    conversation = str(message.chat_id)
    pending = state.pending_inputs.get(conversation)
    if pending and not pending.done():
        pending.set_result(text)
        return
    if not _captured(session, "messaging.message.received", "1.0.0"):
        return
    await session.emit_event(
        "messaging.message.received",
        "1.0.0",
        {
            "messageId": getattr(message, "id", 0),
            "chatId": message.chat_id,
            "text": text,
            "author": {"username": getattr(message, "author", None)},
        },
        {"chatId": message.chat_id},
        event_id=f"funpay:{account.id}:message:{message.id}",
    )


async def _run(session: Any, state: ConnectionState) -> None:
    while True:
        try:
            config = FunPayConfiguration.model_validate({**session.config, **session.secrets})
            account = Account(
                config.golden_key.get_secret_value(),
                user_agent=config.user_agent,
                requests_timeout=30,
            )
            await asyncio.to_thread(account.get)
            state.account = account
            state.poll_interval_seconds = config.poll_interval_seconds
            _mark_success(state)
            runner = _start_runner(account, state, session.connection_id)
            iterator = runner.listen(config.poll_interval_seconds, ignore_exceptions=True)
            while True:
                event = await asyncio.to_thread(next, iterator)
                await _emit_event(session, state, event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.error = error
            state.account = None
            state.runner = None
            state.runner_thread = None
            await asyncio.sleep(10 if isinstance(error, UnauthorizedError) else 5)


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
    if state and isinstance(state.error, UnauthorizedError):
        return Health(state=HealthState.AUTH_REQUIRED, message="Sign in to FunPay again")
    if not state or not state.account:
        return Health(
            state=HealthState.DEGRADED,
            message=_error_message(state.error, "Connecting to FunPay") if state else "Connecting to FunPay",
        )
    stale_after = max(90.0, state.poll_interval_seconds * 5)
    if (
        state.error is not None
        or state.last_success_monotonic is None
        or time.monotonic() - state.last_success_monotonic > stale_after
    ):
        return Health(
            state=HealthState.DEGRADED,
            message=_error_message(state.error, "FunPay event polling is not responding"),
            last_success_at=state.last_success_at,
        )
    return Health(state=HealthState.HEALTHY, last_success_at=state.last_success_at)


@extension.action(
    "funpay.cardinal/send-message",
    "1.0.0",
    input_model=SendMessageInput,
    display_name={"ru": "Отправить сообщение", "en": "Send message"},
)
async def send_message(context: Any, value: SendMessageInput) -> dict[str, Any]:
    state = _states.get(context.connection_id)
    if not state or not state.account:
        raise RuntimeError("FunPay session is unavailable")
    chat_id = context.event_scope.get("chatId")
    if chat_id is None:
        raise ValueError("FunPay chat context is unavailable")
    sent = await asyncio.to_thread(
        state.account.send_message,
        chat_id,
        value.message,
        add_to_ignore_list=False,
    )
    if not sent:
        raise RuntimeError("FunPay did not confirm the message")
    return {}


class _CategoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.field_depth: int | None = None
        self.current: dict[str, Any] | None = None
        self.fields: dict[str, dict[str, Any]] = {}
        self.descriptors: dict[str, dict[str, Any]] = {}
        self.in_h1 = False
        self.category: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "h1":
            self.in_h1 = True
        if "lot-fields" in classes and values.get("data-fields"):
            try:
                for item in json.loads(html.unescape(values["data-fields"] or "")):
                    if isinstance(item, dict) and item.get("id") is not None:
                        self.descriptors[str(item["id"])] = item
            except (ValueError, TypeError):
                pass
        if "lot-field" in classes and values.get("data-id"):
            key = str(values["data-id"])
            descriptor = self.descriptors.get(key, {})
            self.current = {
                "key": key,
                "label": str(descriptor.get("name") or key),
                "choices": [],
            }
            self.fields[key] = self.current
            self.field_depth = self.depth
        if self.current is not None and values.get("value") and (
            tag == "option" or "lot-field-radio-box" in classes or tag == "button"
        ):
            choice = str(values["value"])
            if choice not in self.current["choices"]:
                self.current["choices"].append(choice)
        self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        self.depth = max(0, self.depth - 1)
        if tag == "h1":
            self.in_h1 = False
        if self.field_depth is not None and self.depth <= self.field_depth:
            self.current = None
            self.field_depth = None

    def handle_data(self, data: str) -> None:
        if self.in_h1 and data.strip():
            self.category.append(data.strip())


def _category_id(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    matched = re.fullmatch(r"/lots/(\d{1,20})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"funpay.com", "www.funpay.com"}
        or not matched
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter a FunPay category URL such as https://funpay.com/lots/123/")
    return matched.group(1)


def _category_catalog(account: Account, category_id: str) -> dict[str, Any]:
    session = getattr(account, "session", None)
    if session is None or not hasattr(session, "get"):
        raise RuntimeError("FunPay session is unavailable")
    response = session.get(
        f"https://funpay.com/lots/{category_id}/",
        timeout=(10, 30),
    )
    response.raise_for_status()
    parser = _CategoryParser()
    parser.feed(response.text)
    if not parser.fields:
        raise RuntimeError("FunPay category fields are unavailable")
    return {
        "key": category_id,
        "label": " ".join(parser.category).strip() or f"FunPay {category_id}",
        "fields": list(parser.fields.values()),
    }


@extension.binding_catalog("funpay.categories", "1.0.0")
async def category_catalog(context: Any, job: dict[str, Any]) -> dict[str, Any]:
    state = _states.get(context.connection_id)
    if not state or not state.account:
        raise RuntimeError("FunPay session is unavailable")
    operation = str(job.get("operation") or "")
    identity = {
        "protocolVersion": "1.0.0",
        "requestId": str(job.get("requestId") or ""),
        "catalogId": "funpay.categories",
        "catalogVersion": "1.0.0",
    }
    if operation == "list-scopes":
        category_id = _category_id(str(job.get("query") or ""))
        catalog = await asyncio.to_thread(
            _category_catalog,
            state.account,
            category_id,
        )
        return {
            **identity,
            "operation": operation,
            "scopes": [{"key": catalog["key"], "label": catalog["label"]}],
        }
    if operation == "get-scope":
        category_id = str(job.get("scopeKey") or "")
        if not re.fullmatch(r"\d{1,20}", category_id):
            raise ValueError("FunPay category ID is invalid")
        catalog = await asyncio.to_thread(
            _category_catalog,
            state.account,
            category_id,
        )
        return {
            **identity,
            "operation": operation,
            "scope": {"key": catalog["key"], "label": catalog["label"]},
            "fields": [
                {
                    "key": item["key"],
                    "label": item["label"],
                    "kind": "choice" if item["choices"] else "text",
                    **(
                        {
                            "choices": [
                                {"key": choice, "label": choice}
                                for choice in item["choices"]
                            ]
                        }
                        if item["choices"]
                        else {}
                    ),
                }
                for item in catalog["fields"]
            ],
        }
    raise ValueError("Unsupported FunPay catalog operation")


@extension.input_resolver("funpay.cardinal.collect-input", "1.0.0")
async def collect_input(context: Any, job: dict[str, Any]) -> str:
    state = _states.get(context.connection_id)
    if not state or not state.account:
        raise RuntimeError("FunPay session is unavailable")
    collection = job.get("collection") or {}
    conversation = str(collection.get("conversationKey") or "")
    if not conversation:
        raise ValueError("FunPay conversation is unavailable")
    if conversation in state.pending_inputs:
        raise RuntimeError("Another input request is active in this chat")
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    state.pending_inputs[conversation] = future
    try:
        prompt = str(collection.get("prompt") or "")
        if prompt:
            await asyncio.to_thread(
                state.account.send_message,
                conversation,
                prompt,
                add_to_ignore_list=False,
            )
        return await asyncio.wait_for(
            future,
            timeout=min(840, int(collection.get("timeoutSeconds") or 300)),
        )
    finally:
        state.pending_inputs.pop(conversation, None)
