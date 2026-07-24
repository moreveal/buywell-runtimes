from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from buywell_edge_sdk import Health, HealthState, module
from runtime.ggsel_runtime import ApiError, Config, GGSelClient, _catalog_result, _clean, _purchase_event


class GGSelConfiguration(BaseModel):
    seller_id: int = Field(gt=0)
    api_key: SecretStr
    poll_interval_seconds: float = Field(default=30, ge=5, le=3600)
    message_poll_interval_seconds: float = Field(default=10, ge=2, le=3600)
    sales_window: int = Field(default=100, ge=1, le=1000)


class SendMessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)


extension = module(
    extension_id="ggsel.seller",
    version="1.2.3",
    display_name={"ru": "GGSel", "en": "GGSel"},
    description={
        "ru": "Продажи, сообщения и каталог GGSel через Buywell Edge",
        "en": "GGSel sales, messages, and catalog through Buywell Edge",
    },
    publisher="Buywell",
    entrypoint="edge.ggsel_edge:extension",
    config_model=GGSelConfiguration,
    network_domains=["seller.ggsel.com"],
    dependencies=["httpx==0.28.1", "websocket-client==1.9.0"],
    legacy_manifest=Path(__file__).parents[1] / "manifest.json",
    guides={"ru": "edge/README.ru.md", "en": "edge/README.en.md"},
    changelog={"ru": "edge/CHANGELOG.ru.md", "en": "edge/CHANGELOG.en.md"},
)


class ProviderState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as database:
            database.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS purchases(invoice_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY,last_message_id INTEGER);
                CREATE TABLE IF NOT EXISTS messages(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,PRIMARY KEY(chat_id,message_id));
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def setting(self, key: str) -> str | None:
        with self.connect() as database:
            row = database.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as database:
            database.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def remember_purchase(self, invoice_id: str) -> bool:
        with self.connect() as database:
            return database.execute(
                "INSERT OR IGNORE INTO purchases(invoice_id) VALUES(?)",
                (invoice_id,),
            ).rowcount == 1

    def remember_chat(self, chat_id: int) -> None:
        with self.connect() as database:
            database.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,))

    def chats(self) -> list[tuple[int, int | None]]:
        with self.connect() as database:
            return [(int(row[0]), int(row[1]) if row[1] is not None else None) for row in database.execute("SELECT chat_id,last_message_id FROM chats")]

    def remember_message(self, chat_id: int, message_id: int) -> bool:
        with self.connect() as database:
            return database.execute(
                "INSERT OR IGNORE INTO messages(chat_id,message_id) VALUES(?,?)",
                (chat_id, message_id),
            ).rowcount == 1

    def advance_chat(self, chat_id: int, message_id: int | None) -> None:
        with self.connect() as database:
            database.execute(
                "UPDATE chats SET last_message_id=? WHERE chat_id=?",
                (message_id, chat_id),
            )


@dataclass
class ConnectionState:
    config: Config
    client: GGSelClient
    storage: ProviderState
    task: asyncio.Task[None] | None = None
    error: Exception | None = None
    last_success_at: str | None = None
    pending_inputs: dict[str, asyncio.Future[str]] = field(default_factory=dict)


_states: dict[str, ConnectionState] = {}


def _captured(session: Any, event_type: str, event_version: str) -> bool:
    specification = session.capture_specification
    return bool(specification) and any(
        item.get("eventType") == event_type and item.get("eventVersion") == event_version
        for item in specification.get("subscriptions", [])
    )


def _poll_sales(state: ConnectionState) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    result = []
    initialized = state.storage.setting("sales_initialized") == "1"
    for sale in sorted(state.client.last_sales(), key=lambda item: str(item.get("date", ""))):
        try:
            invoice_id = int(sale.get("invoice_id"))
        except (TypeError, ValueError):
            continue
        if invoice_id <= 0 or not state.storage.remember_purchase(str(invoice_id)) or not initialized:
            continue
        payload, scope = _purchase_event(
            state.config,
            invoice_id,
            state.client.purchase(invoice_id),
            sale,
        )
        result.append((
            "commerce.purchase.created",
            f"ggsel:{state.config.seller_id}:purchase:{invoice_id}",
            payload,
            scope,
        ))
    state.storage.set_setting("sales_initialized", "1")
    return result


def _poll_messages(state: ConnectionState) -> list[tuple[int, int, dict[str, Any], dict[str, Any]]]:
    result = []
    initialized = state.storage.setting("messages_initialized") == "1"
    for chat_id in state.client.chats_with_new_messages():
        state.storage.remember_chat(chat_id)
    for chat_id, last_message_id in state.storage.chats():
        messages = state.client.messages(chat_id, last_message_id)
        parsed = []
        for message in messages:
            try:
                message_id = int(message.get("id"))
            except (TypeError, ValueError):
                continue
            if message_id > 0:
                parsed.append((message_id, message))
        parsed.sort(key=lambda item: item[0])
        for message_id, message in parsed:
            if not state.storage.remember_message(chat_id, message_id):
                continue
            if not initialized or not bool(message.get("buyer")) or bool(message.get("deleted")):
                continue
            text = str(message.get("message", "")).strip()
            if not text and not message.get("is_file"):
                continue
            payload = _clean({
                "messageId": str(message_id),
                "chatId": chat_id,
                "text": text,
                "createdAt": message.get("date_written"),
                "file": {
                    "name": message.get("filename"),
                    "url": message.get("url"),
                    "previewUrl": message.get("preview"),
                    "isImage": bool(message.get("is_img")),
                } if message.get("is_file") else None,
            })
            result.append((chat_id, message_id, payload, {"chatId": chat_id, "invoiceId": str(chat_id)}))
        if parsed:
            state.storage.advance_chat(chat_id, parsed[-1][0])
    state.storage.set_setting("messages_initialized", "1")
    return result


async def _run(session: Any, state: ConnectionState) -> None:
    next_sales = 0.0
    next_messages = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            now = loop.time()
            contacted_provider = False
            if now >= next_sales:
                for event_type, event_id, payload, scope in await asyncio.to_thread(_poll_sales, state):
                    if _captured(session, event_type, "1.1.0"):
                        await session.emit_event(event_type, "1.1.0", payload, scope, event_id=event_id)
                next_sales = now + state.config.poll_interval_seconds
                contacted_provider = True
            if now >= next_messages:
                for chat_id, message_id, payload, scope in await asyncio.to_thread(_poll_messages, state):
                    pending = state.pending_inputs.get(str(chat_id))
                    if pending and not pending.done() and payload.get("text"):
                        pending.set_result(str(payload["text"]))
                    elif _captured(session, "messaging.message.received", "1.0.0"):
                        await session.emit_event(
                            "messaging.message.received",
                            "1.0.0",
                            payload,
                            scope,
                            event_id=f"ggsel:{state.config.seller_id}:chat:{chat_id}:message:{message_id}",
                        )
                next_messages = now + state.config.message_poll_interval_seconds
                contacted_provider = True
            if contacted_provider:
                state.error = None
                state.last_success_at = datetime.now(UTC).isoformat()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.error = error
            await asyncio.sleep(10 if isinstance(error, ApiError) and not error.retryable else 5)
        await asyncio.sleep(0.25)


@extension.on_start
async def start(session: Any) -> None:
    value = GGSelConfiguration.model_validate({**session.config, **session.secrets})
    config = Config(
        buywell_url="https://buywell.invalid",
        connection_token="bwapi_edge_managed",
        seller_id=value.seller_id,
        api_key=value.api_key.get_secret_value(),
        ggsel_api_url="https://seller.ggsel.com/api_sellers/api",
        database_path=Path(session.state_directory) / "legacy-unused.sqlite3",
        poll_interval_seconds=value.poll_interval_seconds,
        message_poll_interval_seconds=value.message_poll_interval_seconds,
        sales_window=value.sales_window,
        request_timeout_seconds=30,
        emit_existing_on_first_start=False,
        log_level="INFO",
    )
    client = GGSelClient(config)
    state = ConnectionState(
        config=config,
        client=client,
        storage=ProviderState(Path(session.state_directory) / "provider.sqlite3"),
    )
    _states[session.connection_id] = state
    try:
        await asyncio.to_thread(client.login)
    except Exception as error:
        state.error = error
    state.task = asyncio.create_task(_run(session, state))


@extension.on_stop
async def stop(session: Any) -> None:
    state = _states.pop(session.connection_id, None)
    if not state:
        return
    if state.task:
        state.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.task
    state.client.http.close()


@extension.health
async def health(session: Any) -> Health:
    state = _states.get(session.connection_id)
    if not state:
        return Health(state=HealthState.OFFLINE)
    if state.error:
        if isinstance(state.error, ApiError) and state.error.code == "unauthorized":
            return Health(state=HealthState.AUTH_REQUIRED, message="Check the GGSel API key")
        return Health(state=HealthState.DEGRADED, message=str(state.error)[:500])
    return Health(state=HealthState.HEALTHY, last_success_at=state.last_success_at)


@extension.action(
    "ggsel.seller/send-message",
    "1.0.0",
    input_model=SendMessageInput,
    display_name={"ru": "Отправить сообщение", "en": "Send message"},
)
async def send_message(context: Any, value: SendMessageInput) -> dict[str, Any]:
    state = _states[context.connection_id]
    chat_id = context.event_scope.get("chatId")
    if chat_id is None:
        raise ValueError("GGSel chat context is unavailable")
    await asyncio.to_thread(state.client.send_message, int(chat_id), value.message)
    return {}


@extension.binding_catalog("ggsel.products", "1.0.0")
async def product_catalog(context: Any, job: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_catalog_result, _states[context.connection_id].client, job)


@extension.input_resolver("ggsel.seller.collect-input", "1.0.0")
async def collect_input(context: Any, job: dict[str, Any]) -> str:
    state = _states[context.connection_id]
    collection = job.get("collection") or {}
    conversation = str(collection.get("conversationKey") or "")
    if not conversation:
        raise ValueError("GGSel conversation is unavailable")
    if conversation in state.pending_inputs:
        raise RuntimeError("Another input request is active in this chat")
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    state.pending_inputs[conversation] = future
    try:
        prompt = str(collection.get("prompt") or "")
        if prompt:
            await asyncio.to_thread(state.client.send_message, int(conversation), prompt)
        return await asyncio.wait_for(
            future,
            timeout=min(840, int(collection.get("timeoutSeconds") or 300)),
        )
    finally:
        state.pending_inputs.pop(conversation, None)
