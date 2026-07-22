from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import logging
import os
import signal
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import websocket
except ImportError as error:  # pragma: no cover - exercised by the install guide
    raise SystemExit(
        "Missing dependency websocket-client. Run: python -m pip install -r requirements.txt"
    ) from error


MODULE_ID = "ggsel.seller"
MODULE_VERSION = "1.1.0"
PROTOCOL_VERSION = "1.0.0"
PURCHASE_EVENT = "commerce.purchase.created"
MESSAGE_EVENT = "messaging.message.received"
EVENT_VERSION = "1.0.0"
SEND_MESSAGE_NODE = "ggsel.seller/send-message"

STOP = threading.Event()
READY = threading.Event()
SPEC_LOCK = threading.RLock()
CAPTURE_SPEC: dict[str, Any] = {"revision": 0, "digest": "", "subscriptions": []}
EXPECTED_EVENTS: dict[str, Any] = {"revision": 0, "subscriptions": []}

logger = logging.getLogger("buywell.ggsel")


class ConfigurationError(ValueError):
    pass


class ApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _is_loopback(host: str | None) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


def _validated_url(value: Any, name: str, *, allow_local_http: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty URL")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ConfigurationError(f"{name} must not contain credentials, query, or fragment")
    if parsed.scheme != "https" and not (
        allow_local_http and parsed.scheme == "http" and _is_loopback(parsed.hostname)
    ):
        raise ConfigurationError(f"{name} must use HTTPS")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


@dataclass(frozen=True)
class Config:
    buywell_url: str
    connection_token: str
    seller_id: int
    api_key: str
    ggsel_api_url: str
    database_path: Path
    poll_interval_seconds: float
    message_poll_interval_seconds: float
    sales_window: int
    request_timeout_seconds: float
    emit_existing_on_first_start: bool
    log_level: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ConfigurationError(
                f"Configuration file not found: {path}. Copy config.example.json to config.json."
            ) from error
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"Invalid JSON in {path}: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigurationError("Configuration root must be an object")

        allowed = {
            "buywell_url",
            "connection_token",
            "seller_id",
            "api_key",
            "ggsel_api_url",
            "database_path",
            "poll_interval_seconds",
            "message_poll_interval_seconds",
            "sales_window",
            "request_timeout_seconds",
            "emit_existing_on_first_start",
            "log_level",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ConfigurationError(f"Unknown configuration fields: {', '.join(unknown)}")

        token = str(raw.get("connection_token", "")).strip()
        if not token.startswith("bwapi_"):
            raise ConfigurationError("connection_token must be a complete Buywell API key")
        try:
            seller_id = int(raw.get("seller_id"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("seller_id must be a positive integer") from error
        if seller_id <= 0:
            raise ConfigurationError("seller_id must be a positive integer")
        api_key = str(raw.get("api_key", "")).strip()
        if not api_key:
            raise ConfigurationError("api_key is required")

        database_value = raw.get("database_path", "state/ggsel-runtime.sqlite3")
        if not isinstance(database_value, str) or not database_value.strip():
            raise ConfigurationError("database_path must be a non-empty path")
        database_path = Path(database_value)
        if not database_path.is_absolute():
            database_path = (path.parent / database_path).resolve()

        def bounded_number(name: str, default: float, minimum: float, maximum: float) -> float:
            value = raw.get(name, default)
            if isinstance(value, bool):
                raise ConfigurationError(f"{name} must be a number")
            try:
                number = float(value)
            except (TypeError, ValueError) as error:
                raise ConfigurationError(f"{name} must be a number") from error
            if not minimum <= number <= maximum:
                raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
            return number

        sales_window_raw = raw.get("sales_window", 100)
        if isinstance(sales_window_raw, bool):
            raise ConfigurationError("sales_window must be an integer")
        try:
            sales_window = int(sales_window_raw)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("sales_window must be an integer") from error
        if not 1 <= sales_window <= 1000:
            raise ConfigurationError("sales_window must be between 1 and 1000")

        emit_existing = raw.get("emit_existing_on_first_start", False)
        if not isinstance(emit_existing, bool):
            raise ConfigurationError("emit_existing_on_first_start must be boolean")
        log_level = str(raw.get("log_level", "INFO")).upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigurationError("log_level must be DEBUG, INFO, WARNING, or ERROR")

        return cls(
            buywell_url=_validated_url(
                raw.get("buywell_url", "https://buywell.pro/api"),
                "buywell_url",
                allow_local_http=True,
            ),
            connection_token=token,
            seller_id=seller_id,
            api_key=api_key,
            ggsel_api_url=_validated_url(
                raw.get("ggsel_api_url", "https://seller.ggsel.com/api_sellers/api"),
                "ggsel_api_url",
            ),
            database_path=database_path,
            poll_interval_seconds=bounded_number(
                "poll_interval_seconds", 30, 5, 3600
            ),
            message_poll_interval_seconds=bounded_number(
                "message_poll_interval_seconds", 10, 2, 3600
            ),
            sales_window=sales_window,
            request_timeout_seconds=bounded_number(
                "request_timeout_seconds", 30, 1, 300
            ),
            emit_existing_on_first_start=emit_existing,
            log_level=log_level,
        )


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as database:
            database.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    body TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    idempotency_key TEXT PRIMARY KEY,
                    terminal INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS purchases (
                    invoice_id TEXT PRIMARY KEY,
                    first_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    emit_existing INTEGER NOT NULL DEFAULT 0,
                    last_message_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    first_seen_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS input_waits (
                    correlation_token TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL UNIQUE,
                    deadline TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS input_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    correlation_token TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    value TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        database = sqlite3.connect(self.path, timeout=30)
        try:
            database.execute("PRAGMA busy_timeout=30000")
            yield database
            database.commit()
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()

    def get_setting(self, key: str) -> str | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as database:
            database.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def remember_purchase(self, invoice_id: str) -> bool:
        with self.connect() as database:
            cursor = database.execute(
                "INSERT OR IGNORE INTO purchases(invoice_id,first_seen_at) VALUES(?,?)",
                (invoice_id, time.time()),
            )
        return cursor.rowcount == 1

    def remember_chat(self, chat_id: int, *, emit_existing: bool) -> None:
        with self.connect() as database:
            database.execute(
                "INSERT OR IGNORE INTO chats(chat_id,emit_existing) VALUES(?,?)",
                (chat_id, int(emit_existing)),
            )

    def chats(self) -> list[tuple[int, bool, bool, int | None]]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT chat_id,initialized,emit_existing,last_message_id FROM chats ORDER BY chat_id"
            ).fetchall()
        return [
            (int(row[0]), bool(row[1]), bool(row[2]), row[3]) for row in rows
        ]

    def initialize_chat(self, chat_id: int, last_message_id: int | None) -> None:
        with self.connect() as database:
            database.execute(
                "UPDATE chats SET initialized=1,last_message_id=? WHERE chat_id=?",
                (last_message_id, chat_id),
            )

    def remember_message(self, chat_id: int, message_id: int) -> bool:
        with self.connect() as database:
            cursor = database.execute(
                "INSERT OR IGNORE INTO messages(chat_id,message_id,first_seen_at) VALUES(?,?,?)",
                (chat_id, message_id, time.time()),
            )
            database.execute(
                "UPDATE chats SET last_message_id=CASE "
                "WHEN last_message_id IS NULL OR last_message_id<? THEN ? ELSE last_message_id END "
                "WHERE chat_id=?",
                (message_id, message_id, chat_id),
            )
        return cursor.rowcount == 1

    def enqueue(self, event_id: str, body: dict[str, Any]) -> None:
        with self.connect() as database:
            database.execute(
                "INSERT OR IGNORE INTO outbox(event_id,body,available_at) VALUES(?,?,?)",
                (event_id, json.dumps(body, ensure_ascii=False), time.time()),
            )

    def outbox(self, limit: int = 25) -> list[tuple[int, dict[str, Any]]]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT id,body FROM outbox WHERE available_at<=? ORDER BY id LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        return [(int(row[0]), json.loads(row[1])) for row in rows]

    def accept_events(self, row_ids: Iterable[int]) -> None:
        with self.connect() as database:
            database.executemany("DELETE FROM outbox WHERE id=?", ((item,) for item in row_ids))

    def retry_events(self, row_ids: Iterable[int]) -> None:
        available_at = time.time() + 30
        with self.connect() as database:
            database.executemany(
                "UPDATE outbox SET attempts=attempts+1,available_at=? WHERE id=?",
                ((available_at, item) for item in row_ids),
            )

    def action_result(self, key: str) -> dict[str, Any] | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT terminal,result FROM actions WHERE idempotency_key=?", (key,)
            ).fetchone()
        return json.loads(row[1]) if row and row[0] else None

    def save_action_result(self, key: str, result: dict[str, Any], *, terminal: bool) -> None:
        with self.connect() as database:
            database.execute(
                "INSERT INTO actions(idempotency_key,terminal,result,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                "terminal=excluded.terminal,result=excluded.result,updated_at=excluded.updated_at",
                (key, int(terminal), json.dumps(result, ensure_ascii=False), time.time()),
            )

    def save_input_wait(
        self, correlation_token: str, conversation_key: str, deadline: str
    ) -> None:
        with self.connect() as database:
            database.execute(
                "INSERT INTO input_waits(correlation_token,conversation_key,deadline) VALUES(?,?,?) "
                "ON CONFLICT(conversation_key) DO UPDATE SET "
                "correlation_token=excluded.correlation_token,deadline=excluded.deadline",
                (correlation_token, conversation_key, deadline),
            )

    def replace_input_waits(self, waits: Iterable[dict[str, Any]]) -> None:
        with self.connect() as database:
            database.execute("DELETE FROM input_waits")
            database.executemany(
                "INSERT INTO input_waits(correlation_token,conversation_key,deadline) VALUES(?,?,?)",
                (
                    (
                        str(wait["correlationToken"]),
                        str(wait["conversationKey"]),
                        str(wait["deadline"]),
                    )
                    for wait in waits
                ),
            )
            database.execute(
                "DELETE FROM input_candidates WHERE correlation_token NOT IN "
                "(SELECT correlation_token FROM input_waits)"
            )

    def input_wait_for_conversation(self, conversation_key: str) -> str | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT correlation_token FROM input_waits WHERE conversation_key=?",
                (conversation_key,),
            ).fetchone()
        return str(row[0]) if row else None

    def conversation_for_input_wait(self, correlation_token: str) -> str | None:
        with self.connect() as database:
            row = database.execute(
                "SELECT conversation_key FROM input_waits WHERE correlation_token=?",
                (correlation_token,),
            ).fetchone()
        return str(row[0]) if row else None

    def save_input_candidate(
        self, candidate_id: str, correlation_token: str, value: str
    ) -> None:
        observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.connect() as database:
            database.execute(
                "INSERT OR IGNORE INTO input_candidates"
                "(candidate_id,correlation_token,observed_at,value) VALUES(?,?,?,?)",
                (candidate_id, correlation_token, observed_at, value),
            )

    def input_candidates(self) -> list[tuple[str, str, str, str]]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT correlation_token,candidate_id,observed_at,value "
                "FROM input_candidates ORDER BY rowid"
            ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]

    def delete_input_candidate(self, candidate_id: str) -> None:
        with self.connect() as database:
            database.execute(
                "DELETE FROM input_candidates WHERE candidate_id=?", (candidate_id,)
            )

    def complete_input_wait(self, correlation_token: str) -> None:
        with self.connect() as database:
            database.execute(
                "DELETE FROM input_candidates WHERE correlation_token=?",
                (correlation_token,),
            )
            database.execute(
                "DELETE FROM input_waits WHERE correlation_token=?",
                (correlation_token,),
            )


class GGSelClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.token: str | None = None
        self.token_lock = threading.Lock()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        authenticated: bool = False,
        headers: dict[str, str] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        query = dict(params or {})
        if authenticated:
            if not self.token:
                self.login()
            query["token"] = self.token
        encoded_query = urllib.parse.urlencode(query)
        url = f"{self.config.ggsel_api_url}/{path.lstrip('/')}"
        if encoded_query:
            url += "?" + encoded_query
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.config.request_timeout_seconds,
                context=ssl.create_default_context(),
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            if authenticated and error.code in {401, 403} and retry_auth:
                self.token = None
                self.login()
                return self._request(
                    method,
                    path,
                    params=params,
                    body=body,
                    authenticated=True,
                    headers=headers,
                    retry_auth=False,
                )
            retryable = error.code in {408, 425, 429, 500, 502, 503, 504}
            raise ApiError(
                "rate_limited" if error.code == 429 else "http_error",
                f"GGSel returned HTTP {error.code}",
                retryable=retryable,
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ApiError("temporary_failure", "GGSel request failed", retryable=True) from error
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ApiError(
                "invalid_response", "GGSel returned a non-JSON response", retryable=False
            ) from error

    def login(self) -> None:
        with self.token_lock:
            if self.token:
                return
            timestamp = str(int(time.time()))
            sign = hashlib.sha256(
                f"{self.config.api_key}{timestamp}".encode("utf-8")
            ).hexdigest()
            data = self._request(
                "POST",
                "apilogin",
                body={
                    "seller_id": self.config.seller_id,
                    "timestamp": timestamp,
                    "sign": sign,
                },
            )
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token.strip():
                description = data.get("retdesc") if isinstance(data, dict) else None
                raise ApiError(
                    "unauthorized",
                    str(description or "GGSel authentication failed"),
                    retryable=False,
                )
            self.token = token.strip()

    def last_sales(self) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "seller-last-sales",
            params={"top": self.config.sales_window},
            authenticated=True,
            headers={"locale": "ru"},
        )
        if not isinstance(data, dict) or data.get("retval") != 0:
            raise ApiError("invalid_response", "Could not read recent sales", retryable=True)
        sales = data.get("sales", [])
        return [item for item in sales if isinstance(item, dict)] if isinstance(sales, list) else []

    def purchase(self, invoice_id: int) -> dict[str, Any]:
        data = self._request(
            "GET", f"purchase/info/{invoice_id}", authenticated=True
        )
        if not isinstance(data, dict) or data.get("retval") != 0:
            raise ApiError("invalid_response", "Could not read purchase", retryable=True)
        return data

    def chats_with_new_messages(self) -> list[int]:
        data = self._request(
            "GET",
            "debates/v2/chats",
            params={"filter_new": 1, "pagesize": 100, "page": 1},
            authenticated=True,
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        result: list[int] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    chat_id = int(item.get("id_i"))
                except (TypeError, ValueError):
                    continue
                if chat_id > 0:
                    result.append(chat_id)
        return result

    def messages(self, chat_id: int, after: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"id_i": chat_id, "count": 100}
        if after is not None:
            params.update({"id_from": after, "newer": 1})
        data = self._request("GET", "debates/v2", params=params, authenticated=True)
        messages = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []
        return [item for item in messages if isinstance(item, dict)] if isinstance(messages, list) else []

    def send_message(self, chat_id: int, message: str) -> None:
        cleaned = "".join(
            character
            for character in message
            if character in "\n\r\t" or ord(character) >= 32
        ).strip()
        if not cleaned:
            raise ApiError("invalid_input", "Message must not be empty", retryable=False)
        if len(cleaned) > 4000:
            raise ApiError(
                "invalid_input", "Message must be at most 4000 characters", retryable=False
            )
        data = self._request(
            "POST",
            "debates/v2",
            params={"id_i": chat_id},
            body={"message": cleaned},
            authenticated=True,
        )
        if not isinstance(data, dict) or data.get("retval") != 0:
            raise ApiError("outcome_unknown", "GGSel did not confirm delivery", retryable=True)


def _path(source: dict[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _condition(condition: dict[str, Any], payload: dict[str, Any], scope: dict[str, Any]) -> bool:
    kind = condition.get("kind")
    if kind == "all":
        return all(_condition(item, payload, scope) for item in condition.get("conditions", []))
    if kind == "any":
        return any(_condition(item, payload, scope) for item in condition.get("conditions", []))
    if kind == "not":
        return not _condition(condition.get("condition", {}), payload, scope)
    actual = _path(scope if condition.get("source") == "scope" else payload, str(condition.get("path", "")))
    expected = condition.get("value")
    operator = condition.get("operator")
    if operator == "exists":
        return actual is not None
    if operator == "equals":
        return actual == expected
    if operator == "not-equals":
        return actual != expected
    if operator == "in":
        return isinstance(expected, list) and actual in expected
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    if operator == "contains":
        return expected in actual
    if operator == "starts-with":
        return actual.startswith(expected)
    if operator == "ends-with":
        return actual.endswith(expected)
    if operator == "matches":
        import fnmatch

        return fnmatch.fnmatchcase(actual, expected)
    return False


def _captured(event_type: str, payload: dict[str, Any], scope: dict[str, Any]) -> bool:
    with SPEC_LOCK:
        subscriptions = list(CAPTURE_SPEC.get("subscriptions", []))
    return any(
        item.get("eventType") == event_type
        and item.get("eventVersion") == EVENT_VERSION
        and any(_condition(rule, payload, scope) for rule in item.get("conditions", []))
        for item in subscriptions
    )


def _expected(event_type: str) -> bool:
    with SPEC_LOCK:
        revision = int(EXPECTED_EVENTS.get("revision", 0))
        subscriptions = list(EXPECTED_EVENTS.get("subscriptions", []))
    if revision <= 0:
        return True
    return any(
        item.get("eventType") == event_type and item.get("eventVersion") == EVENT_VERSION
        for item in subscriptions
    )


def _enqueue_event(
    state: State,
    event_type: str,
    event_id: str,
    payload: dict[str, Any],
    scope: dict[str, Any],
) -> None:
    if not _expected(event_type) or not _captured(event_type, payload, scope):
        return
    state.enqueue(
        event_id,
        {
            "moduleId": MODULE_ID,
            "moduleVersion": MODULE_VERSION,
            "eventType": event_type,
            "eventVersion": EVENT_VERSION,
            "eventId": event_id,
            "payload": payload,
            "scope": scope,
        },
    )


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _purchase_event(config: Config, invoice_id: int, response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    content = response.get("content", {})
    if not isinstance(content, dict):
        content = {}
    buyer = content.get("buyer_info", {})
    if not isinstance(buyer, dict):
        buyer = {}
    product = {
        "id": content.get("item_id"),
        "contentId": content.get("content_id"),
        "name": content.get("name"),
    }
    payload = _clean(
        {
            "invoiceId": str(invoice_id),
            "status": str(content.get("invoice_state", "unknown")),
            "createdAt": content.get("purchase_date"),
            "paidAt": content.get("date_pay"),
            "amount": content.get("amount"),
            "amountRub": content.get("amount_rub"),
            "amountUsd": content.get("amount_usd"),
            "currency": content.get("currency_type"),
            "profit": content.get("profit", content.get("seller_profit")),
            "product": product,
            "buyer": {
                "id": buyer.get("id", content.get("id_buyer")),
                "email": buyer.get("email"),
                "account": buyer.get("account"),
            },
            "options": content.get("options", content.get("purchase_options")),
        }
    )
    scope = _clean(
        {
            "invoiceId": str(invoice_id),
            "chatId": invoice_id,
            "sellerId": config.seller_id,
            "productId": product.get("id"),
            "buyerEmail": buyer.get("email"),
        }
    )
    return payload, scope


class Poller:
    def __init__(self, config: Config, state: State, client: GGSelClient) -> None:
        self.config = config
        self.state = state
        self.client = client
        self.next_sales_at = 0.0
        self.next_messages_at = 0.0

    def run(self) -> None:
        while not STOP.is_set():
            if not READY.wait(1):
                continue
            now = time.monotonic()
            try:
                if now >= self.next_sales_at:
                    self.poll_sales()
                    self.next_sales_at = now + self.config.poll_interval_seconds
                if now >= self.next_messages_at:
                    self.poll_messages()
                    self.next_messages_at = now + self.config.message_poll_interval_seconds
            except ApiError as error:
                level = logging.WARNING if error.retryable else logging.ERROR
                logger.log(level, "GGSel polling error (%s): %s", error.code, error)
                STOP.wait(5 if error.retryable else 30)
            except Exception:
                logger.exception("Unexpected GGSel polling error")
                STOP.wait(10)
            STOP.wait(0.25)

    def poll_sales(self) -> None:
        sales = self.client.last_sales()
        first_scan = self.state.get_setting("sales_initialized") != "1"
        emit_existing = self.config.emit_existing_on_first_start or not first_scan
        for sale in sorted(sales, key=lambda item: str(item.get("date", ""))):
            try:
                invoice_id = int(sale.get("invoice_id"))
            except (TypeError, ValueError):
                continue
            if invoice_id <= 0:
                continue
            is_new = self.state.remember_purchase(str(invoice_id))
            self.state.remember_chat(invoice_id, emit_existing=emit_existing and is_new)
            if not is_new or not emit_existing:
                continue
            response = self.client.purchase(invoice_id)
            payload, scope = _purchase_event(self.config, invoice_id, response)
            _enqueue_event(
                self.state,
                PURCHASE_EVENT,
                f"ggsel:{self.config.seller_id}:purchase:{invoice_id}",
                payload,
                scope,
            )
        if first_scan:
            self.state.set_setting("sales_initialized", "1")

    def poll_messages(self) -> None:
        first_scan = self.state.get_setting("messages_initialized") != "1"
        for chat_id in self.client.chats_with_new_messages():
            self.state.remember_chat(
                chat_id,
                emit_existing=self.config.emit_existing_on_first_start or not first_scan,
            )
        for chat_id, initialized, emit_existing, last_message_id in self.state.chats():
            messages = self.client.messages(chat_id, last_message_id if initialized else None)
            parsed: list[tuple[int, dict[str, Any]]] = []
            for message in messages:
                try:
                    message_id = int(message.get("id"))
                except (TypeError, ValueError):
                    continue
                if message_id > 0:
                    parsed.append((message_id, message))
            parsed.sort(key=lambda item: item[0])
            if not initialized and not emit_existing:
                for message_id, _ in parsed:
                    self.state.remember_message(chat_id, message_id)
                self.state.initialize_chat(
                    chat_id, parsed[-1][0] if parsed else last_message_id
                )
                continue
            for message_id, message in parsed:
                if not self.state.remember_message(chat_id, message_id):
                    continue
                if not bool(message.get("buyer")) or bool(message.get("deleted")):
                    continue
                text = str(message.get("message", "")).strip()
                if not text and not message.get("is_file"):
                    continue
                correlation_token = self.state.input_wait_for_conversation(str(chat_id))
                if correlation_token:
                    if text:
                        self.state.save_input_candidate(
                            f"ggsel:{self.config.seller_id}:chat:{chat_id}:message:{message_id}",
                            correlation_token,
                            text,
                        )
                    continue
                payload = _clean(
                    {
                        "messageId": str(message_id),
                        "chatId": chat_id,
                        "text": text,
                        "createdAt": message.get("date_written"),
                        "file": {
                            "name": message.get("filename"),
                            "url": message.get("url"),
                            "previewUrl": message.get("preview"),
                            "isImage": bool(message.get("is_img")),
                        }
                        if message.get("is_file")
                        else None,
                    }
                )
                scope = {"chatId": chat_id, "invoiceId": str(chat_id)}
                _enqueue_event(
                    self.state,
                    MESSAGE_EVENT,
                    f"ggsel:{self.config.seller_id}:chat:{chat_id}:message:{message_id}",
                    payload,
                    scope,
                )
            if not initialized:
                self.state.initialize_chat(
                    chat_id, parsed[-1][0] if parsed else last_message_id
                )
        if first_scan:
            self.state.set_setting("messages_initialized", "1")


def _buywell_request(config: Config, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{config.buywell_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.connection_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _socket_url(config: Config) -> str:
    parsed = urllib.parse.urlsplit(config.buywell_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.netloc
    if parsed.hostname == "localhost":
        host = host.replace("localhost", "127.0.0.1", 1)
    return urllib.parse.urlunsplit(
        (scheme, host, parsed.path.rstrip("/") + "/v1/module-runtime/socket", "", "")
    )


def _check_buywell_connection(config: Config) -> None:
    _buywell_request(
        config,
        "/v1/module-runtime/connect",
        {"moduleId": MODULE_ID, "moduleVersion": MODULE_VERSION},
    )
    channel = websocket.create_connection(_socket_url(config), timeout=10)
    try:
        channel.send(
            json.dumps(
                {
                    "type": "authenticate",
                    "token": config.connection_token,
                    "moduleId": MODULE_ID,
                    "moduleVersion": MODULE_VERSION,
                }
            )
        )
        for _ in range(20):
            message = json.loads(channel.recv())
            message_type = message.get("type")
            if message_type == "capture-spec.replace":
                specification = message["specification"]
                channel.send(
                    json.dumps(
                        {
                            "type": "capture-spec.applied",
                            "revision": specification["revision"],
                            "digest": specification["digest"],
                        }
                    )
                )
            elif message_type == "capture-spec.applied.accepted" and not message.get(
                "accepted"
            ):
                raise RuntimeError("Buywell rejected the capture specification")
            elif message_type == "ready":
                return
            elif message_type == "error":
                raise RuntimeError(str(message.get("code", "Buywell connection failed")))
        raise RuntimeError("Buywell did not confirm runtime readiness")
    finally:
        channel.close()


def _apply_capture_spec(state: State, specification: dict[str, Any]) -> None:
    value = {
        "revision": int(specification["revision"]),
        "digest": str(specification["digest"]),
        "subscriptions": list(specification.get("subscriptions", [])),
    }
    with SPEC_LOCK:
        global CAPTURE_SPEC
        CAPTURE_SPEC = value
    state.set_setting("capture_spec", json.dumps(value, ensure_ascii=False))


def _load_capture_spec(state: State) -> None:
    raw = state.get_setting("capture_spec")
    if not raw:
        return
    try:
        specification = json.loads(raw)
        with SPEC_LOCK:
            global CAPTURE_SPEC
            CAPTURE_SPEC = specification
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid stored capture specification")


def _apply_expected_events(specification: dict[str, Any]) -> None:
    value = {
        "revision": int(specification.get("revision", 0)),
        "subscriptions": list(specification.get("subscriptions", [])),
    }
    with SPEC_LOCK:
        global EXPECTED_EVENTS
        if value["revision"] >= int(EXPECTED_EVENTS.get("revision", 0)):
            EXPECTED_EVENTS = value


def _execute_action(
    state: State, client: GGSelClient, job: dict[str, Any]
) -> dict[str, Any]:
    key = str(job.get("idempotencyKey", ""))
    previous = state.action_result(key)
    if previous is not None:
        return previous
    try:
        if job.get("nodeType") != SEND_MESSAGE_NODE:
            raise ApiError("unsupported_action", "Unsupported action", retryable=False)
        inputs = job.get("inputs", {})
        context = job.get("context", {})
        scope = context.get("eventScope", {}) if isinstance(context, dict) else {}
        try:
            chat_id = int(scope.get("chatId"))
        except (TypeError, ValueError) as error:
            raise ApiError(
                "missing_context", "This action requires a GGSel chat context", retryable=False
            ) from error
        message = str(inputs.get("message", "")) if isinstance(inputs, dict) else ""
        client.send_message(chat_id, message)
        result = {"status": "success", "outputs": {}}
        state.save_action_result(key, result, terminal=True)
        return result
    except ApiError as error:
        result = {
            "status": "error",
            "error": {
                "code": error.code,
                "message": str(error)[:500],
                "retryable": error.retryable,
            },
        }
        if not error.retryable:
            state.save_action_result(key, result, terminal=True)
        return result
    except Exception as error:
        logger.exception("Action execution failed")
        return {
            "status": "error",
            "error": {
                "code": "temporary_failure",
                "message": str(error)[:500] or "Action execution failed",
                "retryable": True,
            },
        }


def _connect_socket(config: Config, state: State, client: GGSelClient) -> None:
    _buywell_request(
        config,
        "/v1/module-runtime/connect",
        {"moduleId": MODULE_ID, "moduleVersion": MODULE_VERSION},
    )
    channel = websocket.create_connection(_socket_url(config), timeout=10)
    try:
        channel.send(
            json.dumps(
                {
                    "type": "authenticate",
                    "token": config.connection_token,
                    "moduleId": MODULE_ID,
                    "moduleVersion": MODULE_VERSION,
                }
            )
        )
        initial = json.loads(channel.recv())
        if initial.get("type") != "capture-spec.replace":
            raise RuntimeError(initial.get("code", "capture_specification_required"))
        specification = initial["specification"]
        _apply_capture_spec(state, specification)
        channel.send(
            json.dumps(
                {
                    "type": "capture-spec.applied",
                    "revision": specification["revision"],
                    "digest": specification["digest"],
                }
            )
        )
        while True:
            message = json.loads(channel.recv())
            if message.get("type") == "ready":
                break
            if message.get("type") == "expected-events.replace":
                _apply_expected_events(message["specification"])
            if (
                message.get("type") == "capture-spec.applied.accepted"
                and not message.get("accepted")
            ):
                raise RuntimeError("capture_specification_rejected")

        READY.set()
        logger.info("Connected to Buywell as %s@%s", MODULE_ID, MODULE_VERSION)
        channel.settimeout(1)
        heartbeat_at = 0.0
        pending_batch: tuple[str, list[tuple[int, str]]] | None = None
        pending_input_jobs: dict[str, dict[str, Any]] = {}
        submitted_candidates: set[str] = set()
        while not STOP.is_set():
            now = time.time()
            if now >= heartbeat_at:
                channel.send(json.dumps({"type": "heartbeat"}))
                heartbeat_at = now + 30
            if pending_batch is None:
                rows = state.outbox()
                events: list[dict[str, Any]] = []
                identities: list[tuple[int, str]] = []
                stale: list[int] = []
                for row_id, event in rows:
                    if _captured(
                        event["eventType"], event.get("payload", {}), event.get("scope", {})
                    ):
                        events.append(event)
                        identities.append((row_id, event["eventId"]))
                    else:
                        stale.append(row_id)
                if stale:
                    state.accept_events(stale)
                if events:
                    batch_id = str(uuid.uuid4())
                    pending_batch = (batch_id, identities)
                    with SPEC_LOCK:
                        capture_revision = int(CAPTURE_SPEC.get("revision", 0))
                    channel.send(
                        json.dumps(
                            {
                                "type": "event.batch",
                                "batchId": batch_id,
                                "captureSpecRevision": capture_revision,
                                "events": events,
                            },
                            ensure_ascii=False,
                        )
                    )
            for correlation_token, candidate_id, observed_at, value in state.input_candidates():
                if candidate_id not in submitted_candidates:
                    channel.send(
                        json.dumps(
                            {
                                "type": "input.candidate",
                                "correlationToken": correlation_token,
                                "candidateId": candidate_id,
                                "observedAt": observed_at,
                                "value": value,
                            },
                            ensure_ascii=False,
                        )
                    )
                    submitted_candidates.add(candidate_id)
            try:
                message = json.loads(channel.recv())
            except websocket.WebSocketTimeoutException:
                continue
            message_type = message.get("type")
            if (
                message_type == "event.batch.accepted"
                and pending_batch
                and message.get("batchId") == pending_batch[0]
            ):
                results = {
                    item.get("eventId"): bool(item.get("accepted"))
                    for item in message.get("results", [])
                }
                accepted = [
                    row_id
                    for row_id, event_id in pending_batch[1]
                    if results.get(event_id)
                ]
                rejected = [
                    row_id
                    for row_id, event_id in pending_batch[1]
                    if not results.get(event_id)
                ]
                state.accept_events(accepted)
                state.retry_events(rejected)
                pending_batch = None
            elif message_type == "event.batch.rejected" and pending_batch:
                state.retry_events(row_id for row_id, _ in pending_batch[1])
                pending_batch = None
            elif message_type == "action.request":
                job = message["job"]
                channel.send(
                    json.dumps(
                        {
                            "type": "action.result",
                            "jobId": job["jobId"],
                            "leaseToken": job["leaseToken"],
                            "result": _execute_action(state, client, job),
                        },
                        ensure_ascii=False,
                    )
                )
            elif message_type == "input.request":
                job = message["job"]
                pending_input_jobs[str(job["jobId"])] = job
                channel.send(
                    json.dumps(
                        {
                            "type": "input.waiting",
                            "jobId": job["jobId"],
                            "leaseToken": job["leaseToken"],
                        }
                    )
                )
            elif message_type == "input.waiting.accepted" and message.get("accepted"):
                job = pending_input_jobs.pop(str(message.get("jobId")), None)
                if job:
                    conversation_key = str(message["conversationKey"])
                    state.save_input_wait(
                        str(message["correlationToken"]),
                        conversation_key,
                        str(message["deadline"]),
                    )
                    prompt = str(message.get("prompt", "")).strip()
                    if prompt:
                        client.send_message(int(conversation_key), prompt)
            elif message_type == "input.waits.replace":
                state.replace_input_waits(message.get("waiting", []))
            elif message_type == "input.candidate.result":
                correlation_token = str(message.get("correlationToken", ""))
                candidate_id = str(message.get("candidateId", ""))
                submitted_candidates.discard(candidate_id)
                outcome = message.get("outcome")
                if outcome == "retry":
                    state.delete_input_candidate(candidate_id)
                    invalid_message = str(message.get("message", "")).strip()
                    conversation_key = state.conversation_for_input_wait(correlation_token)
                    if conversation_key and invalid_message:
                        client.send_message(int(conversation_key), invalid_message)
                elif outcome in {"resolved", "failed"}:
                    state.complete_input_wait(correlation_token)
                elif not message.get("accepted"):
                    state.delete_input_candidate(candidate_id)
            elif message_type == "capture-spec.replace":
                specification = message["specification"]
                _apply_capture_spec(state, specification)
                channel.send(
                    json.dumps(
                        {
                            "type": "capture-spec.applied",
                            "revision": specification["revision"],
                            "digest": specification["digest"],
                        }
                    )
                )
            elif message_type == "expected-events.replace":
                _apply_expected_events(message["specification"])
    finally:
        READY.clear()
        channel.close()


def _buywell_worker(config: Config, state: State, client: GGSelClient) -> None:
    retry = 1
    while not STOP.is_set():
        try:
            _connect_socket(config, state, client)
            retry = 1
        except urllib.error.HTTPError as error:
            logger.error("Buywell rejected the connection: HTTP %s", error.code)
        except Exception as error:
            logger.warning("Buywell connection lost: %s", error)
        READY.clear()
        STOP.wait(retry)
        retry = min(60, retry * 2)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GGSel runtime for Buywell")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config.json",
        help="Path to config.json",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="Verify read access to GGSel API V1 and exit",
    )
    parser.add_argument(
        "--check-buywell",
        action="store_true",
        help="Verify the authenticated Buywell WebSocket handshake and exit",
    )
    arguments = parser.parse_args(argv)
    try:
        config = Config.load(arguments.config.resolve())
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 2
    _setup_logging(config.log_level)
    if arguments.check_config:
        logger.info("Configuration is valid for %s@%s", MODULE_ID, MODULE_VERSION)
        return 0

    client = GGSelClient(config)
    if arguments.check_api:
        try:
            client.login()
            client.last_sales()
            client.chats_with_new_messages()
        except ApiError as error:
            logger.error(
                "GGSel API V1 access check failed (%s): %s. "
                "Use a seller key with V1 orders and chats access.",
                error.code,
                error,
            )
            return 3
        logger.info("GGSel API V1 orders and chats access is available")
        return 0

    if arguments.check_buywell:
        try:
            _check_buywell_connection(config)
        except Exception as error:
            logger.error("Buywell connection check failed: %s", error)
            return 4
        logger.info("Buywell accepted %s@%s and confirmed runtime readiness", MODULE_ID, MODULE_VERSION)
        return 0

    state = State(config.database_path)
    _load_capture_spec(state)

    def stop(*_: Any) -> None:
        STOP.set()
        READY.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    poller = Poller(config, state, client)
    polling_thread = threading.Thread(
        target=poller.run, name="ggsel-poller", daemon=True
    )
    polling_thread.start()
    logger.info("Starting %s@%s", MODULE_ID, MODULE_VERSION)
    _buywell_worker(config, state, client)
    polling_thread.join(timeout=5)
    logger.info("Stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
