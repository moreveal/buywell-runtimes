from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websocket

from .config import ConfigStore, RuntimeConfig
from .state import RuntimeState


MODULE_ID = "playerok.universal"
MODULE_VERSION = "1.0.4"
PURCHASE_EVENT = "commerce.purchase.created"
MESSAGE_EVENT = "messaging.message.received"
EVENT_VERSION = "1.0.0"
CATALOG_ID = "playerok.categories"
CATALOG_VERSION = "1.0.0"
CATALOG_PROTOCOL_VERSION = "1.0.0"
SEND_MESSAGE_NODE = "playerok.universal/send-message"

logger = logging.getLogger("buywell_playerok")


class RuntimeFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _clean(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value if item is not None]
    if hasattr(value, "name") and not isinstance(value, str):
        return str(value.name)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value)
    return result if result else None


def _identifier(value: Any) -> str | None:
    return _text(getattr(value, "id", value))


def _enum_name(value: Any) -> str | None:
    return _text(getattr(value, "name", value))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_time(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _path(source: dict[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _condition(
    condition: dict[str, Any],
    payload: dict[str, Any],
    scope: dict[str, Any],
) -> bool:
    kind = condition.get("kind")
    if kind == "all":
        return all(
            _condition(item, payload, scope)
            for item in condition.get("conditions", [])
        )
    if kind == "any":
        return any(
            _condition(item, payload, scope)
            for item in condition.get("conditions", [])
        )
    if kind == "not":
        return not _condition(condition.get("condition", {}), payload, scope)
    source = scope if condition.get("source") == "scope" else payload
    actual = _path(source, str(condition.get("path", "")))
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


def _captured(
    specification: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    scope: dict[str, Any],
) -> bool:
    for subscription in specification.get("subscriptions", []):
        if (
            subscription.get("eventType") != event_type
            or subscription.get("eventVersion") != EVENT_VERSION
        ):
            continue
        conditions = subscription.get("conditions", [])
        if not conditions or any(
            _condition(condition, payload, scope) for condition in conditions
        ):
            return True
    return False


def _buywell_request(
    config: RuntimeConfig, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{config.buywell_url.rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.connection_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _socket_url(config: RuntimeConfig) -> str:
    parsed = urllib.parse.urlsplit(config.buywell_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.netloc
    if parsed.hostname == "localhost":
        host = host.replace("localhost", "127.0.0.1", 1)
    return urllib.parse.urlunsplit(
        (
            scheme,
            host,
            parsed.path.rstrip("/") + "/v1/module-runtime/socket",
            "",
            "",
        )
    )


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
        values["__obtaining_type"] = (
            _text(getattr(obtaining, "name", None)) or obtaining_id
        )
        choice_ids["__obtaining_type"] = obtaining_id
    attributes = getattr(item, "attributes", None)
    category = getattr(item, "category", None)
    options = getattr(category, "options", None) or []
    if isinstance(attributes, dict):
        for raw_key, raw_value in attributes.items():
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
            values[key] = (
                _text(getattr(selected, "label", None))
                or _text(raw_value)
                or ""
            )
            choice_ids[key] = _text(raw_value) or ""
    for field in getattr(deal, "obtaining_fields", None) or []:
        if bool(getattr(field, "hidden", False)):
            continue
        key = _identifier(field)
        value = getattr(field, "value", None)
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
    category = getattr(item, "category", None)
    game = getattr(item, "game", None)
    return _clean(
        {
            "itemId": _identifier(item),
            "categoryId": _identifier(category),
            "gameId": _identifier(game),
        }
    )


class RuntimeBridge:
    def __init__(self, root: Path | None = None):
        module_root = root or Path(__file__).resolve().parent
        data_root = module_root / "module_data"
        self.config_store = ConfigStore(data_root / "config.json")
        self.state = RuntimeState(data_root / "buywell-runtime.sqlite3")
        self.bot: Any = None
        self._enabled = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._connected = False
        self._last_error = ""
        self._catalog_lock = threading.Lock()
        self._catalog_cache: tuple[float, list[Any]] = (0.0, [])

    def enable(self) -> None:
        if self.state.get_setting("activated_at") is None:
            self.state.set_setting("activated_at", str(time.time()))
        with self._lock:
            self._enabled = True
        self._ensure_worker()

    def disable(self) -> None:
        with self._lock:
            self._enabled = False
        self._stop_worker()

    def attach_bot(self, bot: Any) -> None:
        self.bot = bot
        self._ensure_worker()

    def status(self) -> dict[str, Any]:
        try:
            configured = self.config_store.load().configured
        except Exception:
            configured = False
        with self._lock:
            return {
                "configured": configured,
                "connected": self._connected,
                "last_error": self._last_error,
            }

    def configure_token(self, token: str) -> None:
        self.config_store.set_token(token)
        self.restart()

    def disconnect(self) -> None:
        self.config_store.clear_token()
        self._stop_worker()

    def restart(self) -> None:
        self._stop_worker()
        self._ensure_worker()

    def check_connection(self) -> None:
        config = self.config_store.load()
        if not config.configured:
            raise RuntimeFailure("not_configured", "Connection key is not configured")
        _buywell_request(
            config,
            "/v1/module-runtime/connect",
            {"moduleId": MODULE_ID, "moduleVersion": MODULE_VERSION},
        )

    def _ensure_worker(self) -> None:
        with self._lock:
            if (
                not self._enabled
                or self.bot is None
                or self._thread is not None
                and self._thread.is_alive()
            ):
                return
            try:
                if not self.config_store.load().configured:
                    return
            except Exception as error:
                self._last_error = str(error)
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._worker,
                name="buywell-playerok-runtime",
                daemon=True,
            )
            self._thread.start()

    def _stop_worker(self) -> None:
        with self._lock:
            stop = self._stop
            thread = self._thread
            self._thread = None
        stop.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            self._connected = False

    def _worker(self) -> None:
        retry = 1.0
        try:
            self.state.prune()
            while not self._stop.is_set():
                try:
                    self._connect_socket(self.config_store.load())
                    retry = 1.0
                except urllib.error.HTTPError as error:
                    self._set_error(f"Buywell rejected the connection: HTTP {error.code}")
                except Exception as error:
                    self._set_error(str(error) or "Buywell connection lost")
                self._set_connected(False)
                delay = min(60.0, retry) + random.uniform(0, min(1.0, retry / 4))
                if self._stop.wait(delay):
                    return
                retry = min(60.0, retry * 2)
        finally:
            self._set_connected(False)

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            self._connected = value
            if value:
                self._last_error = ""

    def _set_error(self, value: str) -> None:
        with self._lock:
            self._last_error = value[:500]
        logger.warning("Buywell runtime: %s", value)

    def _connect_socket(self, config: RuntimeConfig) -> None:
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
                raise RuntimeFailure(
                    str(initial.get("code", "capture_specification_required")),
                    "Buywell did not provide a capture specification",
                )
            self._apply_capture_spec(channel, initial["specification"])
            while not self._stop.is_set():
                message = json.loads(channel.recv())
                if message.get("type") == "ready":
                    break
                if (
                    message.get("type") == "capture-spec.applied.accepted"
                    and not message.get("accepted")
                ):
                    raise RuntimeFailure(
                        "capture_specification_rejected",
                        "Buywell rejected the capture specification",
                    )
            self._set_connected(True)
            logger.info("Connected to Buywell as %s@%s", MODULE_ID, MODULE_VERSION)
            channel.settimeout(1)
            heartbeat_at = 0.0
            pending: tuple[str, list[tuple[int, str]]] | None = None
            while not self._stop.is_set():
                now = time.time()
                if now >= heartbeat_at:
                    channel.send(json.dumps({"type": "heartbeat"}))
                    heartbeat_at = now + 30
                if pending is None:
                    pending = self._send_outbox(channel)
                try:
                    message = json.loads(channel.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                message_type = message.get("type")
                if (
                    message_type == "event.batch.accepted"
                    and pending
                    and message.get("batchId") == pending[0]
                ):
                    outcomes = {
                        str(item.get("eventId")): bool(item.get("accepted"))
                        for item in message.get("results", [])
                    }
                    self.state.accept_events(
                        row_id
                        for row_id, event_id in pending[1]
                        if outcomes.get(event_id)
                    )
                    self.state.retry_events(
                        row_id
                        for row_id, event_id in pending[1]
                        if not outcomes.get(event_id)
                    )
                    pending = None
                elif message_type == "event.batch.rejected" and pending:
                    self.state.retry_events(row_id for row_id, _ in pending[1])
                    pending = None
                elif message_type == "action.request":
                    job = message["job"]
                    channel.send(
                        json.dumps(
                            {
                                "type": "action.result",
                                "jobId": job["jobId"],
                                "leaseToken": job["leaseToken"],
                                "result": self.execute_action(job),
                            },
                            ensure_ascii=False,
                        )
                    )
                elif message_type == "catalog.request":
                    job = message["job"]
                    channel.send(
                        json.dumps(
                            {
                                "type": "catalog.result",
                                "jobId": job["jobId"],
                                "leaseToken": job["leaseToken"],
                                "result": self.execute_catalog(job),
                            },
                            ensure_ascii=False,
                        )
                    )
                elif message_type == "capture-spec.replace":
                    self._apply_capture_spec(channel, message["specification"])
                elif message_type == "expected-events.replace":
                    continue
        finally:
            channel.close()

    def _apply_capture_spec(
        self, channel: Any, specification: dict[str, Any]
    ) -> None:
        value = {
            "revision": int(specification["revision"]),
            "digest": str(specification["digest"]),
            "subscriptions": list(specification.get("subscriptions", [])),
        }
        self.state.save_capture_spec(value)
        channel.send(
            json.dumps(
                {
                    "type": "capture-spec.applied",
                    "revision": value["revision"],
                    "digest": value["digest"],
                }
            )
        )

    def _send_outbox(
        self, channel: Any
    ) -> tuple[str, list[tuple[int, str]]] | None:
        specification = self.state.capture_spec()
        events: list[dict[str, Any]] = []
        identities: list[tuple[int, str]] = []
        stale: list[int] = []
        for row_id, event in self.state.outbox():
            if _captured(
                specification,
                str(event.get("eventType", "")),
                event.get("payload", {}),
                event.get("scope", {}),
            ):
                events.append(event)
                identities.append((row_id, str(event["eventId"])))
            else:
                stale.append(row_id)
        self.state.accept_events(stale)
        if not events:
            return None
        batch_id = str(uuid.uuid4())
        channel.send(
            json.dumps(
                {
                    "type": "event.batch",
                    "batchId": batch_id,
                    "captureSpecRevision": int(specification.get("revision", 0)),
                    "events": events,
                },
                ensure_ascii=False,
            )
        )
        return batch_id, identities

    def _enqueue(
        self,
        event_type: str,
        event_id: str,
        payload: dict[str, Any],
        scope: dict[str, Any],
    ) -> bool:
        specification = self.state.capture_spec()
        if not _captured(specification, event_type, payload, scope):
            return False
        return self.state.enqueue_once(
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

    def _is_after_activation(self, created_at: Any) -> bool:
        baseline = self.state.get_setting("activated_at")
        observed_at = _event_time(created_at)
        if baseline is None or observed_at is None:
            return True
        try:
            return observed_at > float(baseline)
        except ValueError:
            return True

    def handle_purchase(self, bot: Any, event: Any) -> bool:
        deal = getattr(event, "deal", None)
        account = getattr(bot, "account", None)
        if not deal or not account:
            return False
        deal_id = _identifier(deal)
        buyer = getattr(deal, "user", None)
        if (
            not deal_id
            or not _identifier(buyer)
            or _identifier(buyer) == _identifier(account)
            or _enum_name(getattr(deal, "direction", None)) != "OUT"
            or not self._is_after_activation(getattr(deal, "created_at", None))
        ):
            return False
        item = getattr(deal, "item", None)
        if not _identifier(item):
            return False
        if not getattr(item, "category", None) or not getattr(item, "game", None):
            try:
                item = account.get_item(id=_identifier(item))
            except Exception:
                pass
        chat = getattr(event, "chat", None) or getattr(deal, "chat", None)
        if not _identifier(chat):
            return False
        values, choice_ids = _item_values(item, deal)
        item_payload = _item_payload(item)
        payload = _clean(
            {
                "dealId": _identifier(deal),
                "status": _enum_name(getattr(deal, "status", None)) or "PAID",
                "createdAt": _text(getattr(deal, "created_at", None)) or _iso_now(),
                "item": item_payload,
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
            }
        )
        return self._enqueue(
            PURCHASE_EVENT,
            f"playerok:purchase:{deal_id}",
            payload,
            scope,
        )

    def handle_message(self, bot: Any, event: Any) -> bool:
        message = getattr(event, "message", None)
        chat = getattr(event, "chat", None)
        account = getattr(bot, "account", None)
        sender = getattr(message, "user", None)
        if not message or not chat or not account or not sender:
            return False
        message_id = _identifier(message)
        if _identifier(sender) == _identifier(account):
            return False
        if (
            not message_id
            or not self._is_after_activation(getattr(message, "created_at", None))
        ):
            return False
        if _identifier(chat) in {
            _text(getattr(account, "support_chat_id", None)),
            _text(getattr(account, "system_chat_id", None)),
        }:
            return False
        if getattr(message, "event", None) is not None:
            return False
        deal = getattr(message, "deal", None)
        if deal is None:
            candidates = [
                candidate
                for candidate in (getattr(chat, "deals", None) or [])
                if _identifier(getattr(candidate, "user", None)) != _identifier(account)
                and _enum_name(getattr(candidate, "direction", None)) in {None, "OUT"}
            ]
            if len(candidates) == 1:
                deal = candidates[0]
        item = getattr(deal, "item", None) if deal else getattr(message, "item", None)
        if _identifier(item) and (
            not getattr(item, "category", None) or not getattr(item, "game", None)
        ):
            try:
                item = account.get_item(id=_identifier(item))
            except Exception:
                pass
        values, choice_ids = _item_values(item, deal) if item else ({}, {})
        images = [
            str(url)
            for image in (getattr(message, "images", None) or [])
            if (url := getattr(image, "url", None))
        ]
        payload = _clean(
            {
                "messageId": _identifier(message),
                "text": _text(getattr(message, "text", None)) or "",
                "createdAt": _text(getattr(message, "created_at", None)) or _iso_now(),
                "images": images,
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
                "chatId": _identifier(chat),
                **(_scope_from_item(item) if _identifier(item) else {}),
                "buyerId": _identifier(sender),
            }
        )
        return self._enqueue(
            MESSAGE_EVENT,
            f"playerok:message:{message_id}",
            payload,
            scope,
        )

    def execute_action(self, job: dict[str, Any]) -> dict[str, Any]:
        key = str(job.get("idempotencyKey", ""))
        previous = self.state.action(key) if key else None
        if previous:
            state, result = previous
            if state == "terminal" and result is not None:
                return result
            unknown = self._error(
                "outcome_unknown",
                "A previous delivery attempt ended without a confirmed result",
                retryable=False,
            )
            self.state.finish_action(key, unknown)
            return unknown
        try:
            if job.get("nodeType") != SEND_MESSAGE_NODE:
                raise RuntimeFailure("unsupported_action", "Unsupported action")
            inputs = job.get("inputs", {})
            context = job.get("context", {})
            scope = context.get("eventScope", {}) if isinstance(context, dict) else {}
            chat_id = str(scope.get("chatId", "")).strip()
            message = str(inputs.get("message", "")) if isinstance(inputs, dict) else ""
            if not key:
                raise RuntimeFailure(
                    "invalid_job", "Action job does not contain an idempotency key"
                )
            if not chat_id:
                raise RuntimeFailure(
                    "missing_context", "This action requires a Playerok chat context"
                )
            if not message.strip():
                raise RuntimeFailure("invalid_input", "Message must not be empty")
            if len(message) > 4000:
                raise RuntimeFailure(
                    "invalid_input", "Message must be at most 4000 characters"
                )
            if not self.state.begin_action(key):
                return self._error(
                    "outcome_unknown",
                    "The action is already in progress",
                    retryable=False,
                )
            try:
                self.bot.account.send_message(chat_id=chat_id, text=message)
            except Exception as error:
                result = self._error(
                    "outcome_unknown",
                    str(error)[:500] or "Playerok did not confirm message delivery",
                    retryable=False,
                )
                self.state.finish_action(key, result)
                return result
            result = {"status": "success", "outputs": {}}
            self.state.finish_action(key, result)
            return result
        except RuntimeFailure as error:
            result = self._error(error.code, str(error), retryable=error.retryable)
            if key:
                if self.state.action(key) is None:
                    self.state.begin_action(key)
                self.state.finish_action(key, result)
            return result
        except Exception as error:
            logger.exception("Playerok action failed")
            return self._error(
                "temporary_failure",
                str(error)[:500] or "Playerok action failed",
                retryable=True,
            )

    @staticmethod
    def _error(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
        return {
            "status": "error",
            "error": {
                "code": code,
                "message": message[:500],
                "retryable": retryable,
            },
        }

    def execute_catalog(self, job: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"status": "success", "value": self.catalog_result(job)}
        except RuntimeFailure as error:
            return {
                "status": "error",
                "error": {"code": error.code, "message": str(error)[:500]},
            }
        except Exception as error:
            logger.exception("Playerok catalog request failed")
            return {
                "status": "error",
                "error": {
                    "code": "temporary_failure",
                    "message": str(error)[:500] or "Could not read Playerok items",
                },
            }

    def catalog_result(self, job: dict[str, Any]) -> dict[str, Any]:
        identity = {
            "protocolVersion": CATALOG_PROTOCOL_VERSION,
            "requestId": str(job.get("requestId", "")),
            "catalogId": str(job.get("catalogId", "")),
            "catalogVersion": str(job.get("catalogVersion", "")),
        }
        if (
            identity["catalogId"] != CATALOG_ID
            or identity["catalogVersion"] != CATALOG_VERSION
        ):
            raise RuntimeFailure("unsupported_catalog", "Unsupported binding catalog")
        operation = str(job.get("operation", ""))
        if operation == "list-scopes":
            query = str(job.get("query", "")).casefold().strip()
            categories: dict[str, dict[str, Any]] = {}
            for item in self._items():
                category = getattr(item, "category", None)
                key = _identifier(category)
                if not key:
                    continue
                entry = categories.setdefault(
                    key,
                    {
                        "key": key,
                        "label": self._category_label(item)[:500],
                        "items": [],
                    },
                )
                entry["items"].append(item)
            scopes = []
            for entry in categories.values():
                haystack = " ".join(
                    [
                        entry["key"],
                        entry["label"],
                        *[
                            _text(getattr(item, "name", None))
                            for item in entry["items"]
                        ],
                    ]
                ).casefold()
                if not query or query in haystack:
                    scopes.append(
                        {"key": entry["key"], "label": entry["label"]}
                    )
            scopes.sort(key=lambda item: (item["label"].casefold(), item["key"]))
            try:
                offset = max(0, int(job.get("cursor", 0)))
            except (TypeError, ValueError):
                offset = 0
            page = scopes[offset : offset + 100]
            value = {**identity, "operation": operation, "scopes": page}
            if offset + len(page) < len(scopes):
                value["nextCursor"] = str(offset + len(page))
            return value
        if operation == "get-scope":
            scope_key = str(job.get("scopeKey", "")).strip()
            if not scope_key:
                raise RuntimeFailure("invalid_scope", "Category ID is required")
            items = [
                item
                for item in self._items()
                if _identifier(getattr(item, "category", None)) == scope_key
            ]
            if not items:
                raise RuntimeFailure(
                    "scope_unavailable",
                    "No seller items are available in the selected Playerok category",
                )
            return {
                **identity,
                "operation": operation,
                "scope": {
                    "key": scope_key,
                    "label": self._category_label(items[0])[:500],
                },
                "fields": self._catalog_fields(items)[:200],
            }
        raise RuntimeFailure("unsupported_operation", "Unsupported catalog operation")

    def _items(self) -> list[Any]:
        now = time.monotonic()
        with self._catalog_lock:
            expires_at, cached = self._catalog_cache
            if expires_at > now:
                return list(cached)
            try:
                from playerokapi.enums import ItemStatuses

                statuses = [
                    ItemStatuses.PENDING_APPROVAL,
                    ItemStatuses.PENDING_MODERATION,
                    ItemStatuses.APPROVED,
                    ItemStatuses.EXPIRED,
                    ItemStatuses.SOLD,
                    ItemStatuses.DRAFT,
                ]
                items: list[Any] = []
                cursor = None
                while len(items) < 2000:
                    page = self.bot.account.get_my_items(
                        statuses=statuses,
                        count=24,
                        after_cursor=cursor,
                    )
                    page_items = list(getattr(page, "items", None) or [])
                    items.extend(page_items)
                    page_info = getattr(page, "page_info", None)
                    if not page_info or not getattr(page_info, "has_next_page", False):
                        break
                    cursor = getattr(page_info, "end_cursor", None)
                    if not cursor:
                        break
                detailed: list[Any] = []
                for item in items:
                    try:
                        detailed.append(
                            self.bot.account.get_item(id=_identifier(item))
                        )
                    except Exception:
                        detailed.append(item)
                self._catalog_cache = (now + 60, detailed)
                return list(detailed)
            except Exception:
                self._catalog_cache = (0.0, [])
                raise

    @staticmethod
    def _item_label(item: Any) -> str:
        game = _text(getattr(getattr(item, "game", None), "name", None))
        category = _text(getattr(getattr(item, "category", None), "name", None))
        name = _text(getattr(item, "name", None)) or _identifier(item) or "Playerok item"
        return " · ".join(value for value in (game, category, name) if value)

    @staticmethod
    def _category_label(item: Any) -> str:
        game = _text(getattr(getattr(item, "game", None), "name", None))
        category = _text(
            getattr(getattr(item, "category", None), "name", None)
        )
        return " · ".join(
            value for value in (game, category) if value
        ) or _identifier(getattr(item, "category", None)) or "Playerok category"

    @staticmethod
    def _item_choice_label(item: Any) -> str:
        name = _text(getattr(item, "name", None)) or _identifier(item) or "Item"
        price = getattr(item, "price", None)
        obtaining = _text(
            getattr(getattr(item, "obtaining_type", None), "name", None)
        )
        status = {
            "DRAFT": "Черновик",
            "PENDING_APPROVAL": "На проверке",
            "PENDING_MODERATION": "Изменения на модерации",
            "EXPIRED": "Истёк",
            "SOLD": "Продан",
        }.get(_enum_name(getattr(item, "status", None)), "")
        details = [
            status,
            f"{price:g} ₽" if isinstance(price, (int, float)) else "",
            obtaining,
        ]
        suffix = " · ".join(value for value in details if value)
        return f"{name} — {suffix}" if suffix else name

    def _catalog_fields(self, items: list[Any]) -> list[dict[str, Any]]:
        category = getattr(items[0], "category", None)
        fields: list[dict[str, Any]] = [
            {
                "key": "__item",
                "label": "Товар Playerok",
                "kind": "choice",
                "choices": [
                    {
                        "key": _identifier(item),
                        "label": self._item_choice_label(item)[:500],
                    }
                    for item in items
                    if _identifier(item)
                ][:500],
            }
        ]
        obtaining_choices = []
        for item in items:
            obtaining = getattr(item, "obtaining_type", None)
            key = _identifier(obtaining)
            if key and not any(choice["key"] == key for choice in obtaining_choices):
                obtaining_choices.append(
                    {
                        "key": key,
                        "label": (
                            _text(getattr(obtaining, "name", None)) or key
                        )[:500],
                    }
                )
        if obtaining_choices:
            fields.append(
                {
                    "key": "__obtaining_type",
                    "label": "Способ получения",
                    "kind": "choice",
                    "choices": obtaining_choices[:500],
                }
            )
        grouped: dict[str, dict[str, Any]] = {}
        for option in getattr(category, "options", None) or []:
            key = _text(getattr(option, "field", None))
            if not key:
                continue
            group = grouped.setdefault(
                key,
                {
                    "key": key,
                    "label": _text(getattr(option, "group", None))
                    or _text(getattr(option, "label", None))
                    or key,
                    "kind": "choice",
                    "choices": [],
                },
            )
            choice_key = _text(getattr(option, "value", None))
            if choice_key and not any(
                choice["key"] == choice_key for choice in group["choices"]
            ):
                group["choices"].append(
                    {
                        "key": choice_key,
                        "label": (
                            _text(getattr(option, "label", None)) or choice_key
                        )[:500],
                    }
                )
        fields.extend(grouped.values())
        category_id = _identifier(category)
        obtaining_ids = {
            _identifier(getattr(item, "obtaining_type", None))
            for item in items
            if _identifier(getattr(item, "obtaining_type", None))
        }
        if category_id and obtaining_ids:
            for obtaining_id in sorted(obtaining_ids):
                cursor = None
                while len(fields) < 200:
                    try:
                        page = self.bot.account.get_game_category_data_fields(
                            game_category_id=category_id,
                            obtaining_type_id=obtaining_id,
                            count=24,
                            after_cursor=cursor,
                        )
                    except Exception:
                        break
                    for field in getattr(page, "data_fields", None) or []:
                        if bool(getattr(field, "hidden", False)):
                            continue
                        key = _identifier(field)
                        if key and not any(item["key"] == key for item in fields):
                            fields.append(
                                {
                                    "key": key,
                                    "label": (
                                        _text(getattr(field, "label", None)) or key
                                    )[:500],
                                    "kind": "text",
                                    **(
                                        {"required": True}
                                        if bool(getattr(field, "required", False))
                                        else {}
                                    ),
                                }
                            )
                    page_info = getattr(page, "page_info", None)
                    if (
                        not page_info
                        or not getattr(page_info, "has_next_page", False)
                    ):
                        break
                    cursor = getattr(page_info, "end_cursor", None)
                    if not cursor:
                        break
        for field in fields:
            if field.get("choices"):
                field["choices"] = field["choices"][:500]
            elif field.get("kind") == "choice":
                field["kind"] = "text"
                field.pop("choices", None)
        return fields


runtime = RuntimeBridge()
