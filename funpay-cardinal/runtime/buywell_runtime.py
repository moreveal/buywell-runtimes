from __future__ import annotations

import json
import html
from html.parser import HTMLParser
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from FunPayAPI.types import MessageTypes, OrderStatuses
from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent, NewOrderEvent, OrderStatusChangedEvent
import telebot
from telebot.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from tg_bot import CBT
import tg_bot.static_keyboards
websocket = None

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "Buywell"
VERSION = "1.3.0"
PURCHASE_EVENT_VERSION = "1.3.0"
DESCRIPTION = "Связывает FunPay Cardinal с вашими сценариями Buywell."
CREDITS = "Buywell"
UUID = "f01c34c5-a7ea-43a4-9136-8ad5ce5c8154"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

# Use Cardinal's configured logger so opt-in runtime diagnostics follow its log routing.
logger = logging.getLogger("FPC")
STATE_DIR = Path("storage/plugins/buywell_runtime")
CONFIG_FILE = STATE_DIR / "config.json"
DATABASE_FILE = STATE_DIR / "runtime.sqlite3"
STOP = threading.Event()
CAPTURE_LOCK = threading.Lock()
CAPTURE_SPEC: dict[str, Any] = {"revision": 0, "digest": "", "subscriptions": []}
EXPECTED_EVENTS: dict[str, Any] = {"revision": 0, "subscriptions": []}
DEFAULT_BUYWELL_URL = os.environ.get("BUYWELL_API_URL", "https://buywell.pro/api").rstrip("/")
EDIT_KEY = "Buywell_EditKey"
KEY_EDITED = "Buywell_KeyEdited"
TOGGLE = "Buywell_Toggle"
BINDING_CATALOG_VERSION = "1.0.0"


class _CategoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True);self.depth=0;self.field_depth=None;self.current=None;self.fields={};self.descriptors={};self.in_h1=False;self.category=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs);classes=set(values.get("class","").split())
        if tag=="h1":self.in_h1=True
        if "lot-fields" in classes and values.get("data-fields"):
            try:
                for item in json.loads(html.unescape(values["data-fields"])):
                    if isinstance(item,dict) and item.get("id") is not None:self.descriptors[str(item["id"])]=item
            except (ValueError,TypeError):pass
        if "lot-field" in classes and values.get("data-id"):
            key=str(values["data-id"]);descriptor=self.descriptors.get(key,{})
            self.current={"key":key,"label":str(descriptor.get("name") or key),"choices":[],"siteType":str(descriptor.get("type") or "text")};self.fields[key]=self.current;self.field_depth=self.depth
        if self.current is not None:
            if tag in ("input","select","textarea") and values.get("name"):self.current["htmlName"]=values["name"]
            if tag=="select":self.current["siteType"]="choice"
            if values.get("value") and (tag=="option" or "lot-field-radio-box" in classes or tag=="button"):
                choice=str(values["value"])
                if choice not in self.current["choices"]:self.current["choices"].append(choice)
        self.depth+=1
    def handle_endtag(self,tag):
        self.depth=max(0,self.depth-1)
        if tag=="h1":self.in_h1=False
        if self.field_depth is not None and self.depth<=self.field_depth:self.current=None;self.field_depth=None
    def handle_data(self,data):
        if self.in_h1 and data.strip():self.category.append(data.strip())


def _category_url(value:str)->tuple[str,str]:
    parsed=urllib.parse.urlsplit(value.strip());match=__import__("re").fullmatch(r"/lots/(\d{1,20})/?",parsed.path)
    if parsed.scheme!="https" or parsed.hostname not in {"funpay.com","www.funpay.com"} or not match or parsed.query or parsed.fragment:raise ValueError("invalid_category_url")
    return match.group(1),f"https://funpay.com/lots/{match.group(1)}/"


def _category_catalog(cardinal:"Cardinal",category_id:str)->dict[str,Any]:
    url=f"https://funpay.com/lots/{category_id}/";session=getattr(cardinal.account,"session",None)
    if session is None or not hasattr(session,"get"):raise RuntimeError("funpay_session_unavailable")
    response=session.get(url,timeout=(10,30));response.raise_for_status();parser=_CategoryParser();parser.feed(response.text)
    if not parser.fields:raise RuntimeError("category_fields_unavailable")
    return{"key":category_id,"label":" ".join(parser.category).strip() or f"FunPay {category_id}","fields":list(parser.fields.values())}


def _catalog_job(cardinal:"Cardinal",job:dict[str,Any])->dict[str,Any]:
    try:
        if job.get("catalogId")!="funpay.categories" or job.get("catalogVersion")!=BINDING_CATALOG_VERSION:raise ValueError("unsupported_catalog")
        identity={"protocolVersion":BINDING_CATALOG_VERSION,"requestId":job["requestId"],"catalogId":job["catalogId"],"catalogVersion":job["catalogVersion"]}
        if job.get("operation")=="list-scopes":
            category_id,_=_category_url(str(job.get("query","")));catalog=_category_catalog(cardinal,category_id)
            value={**identity,"operation":"list-scopes","scopes":[{"key":catalog["key"],"label":catalog["label"]}]}
        elif job.get("operation")=="get-scope":
            category_id=str(job.get("scopeKey",""));catalog=_category_catalog(cardinal,category_id);fields=[]
            for field in catalog["fields"]:
                choices=[{"key":choice,"label":choice} for choice in field["choices"]]
                fields.append({"key":field["key"],"label":field["label"],"kind":"choice" if choices else "text",**({"choices":choices} if choices else {})})
            value={**identity,"operation":"get-scope","scope":{"key":catalog["key"],"label":catalog["label"]},"fields":fields}
        else:raise ValueError("unsupported_operation")
        return{"status":"success","value":value}
    except Exception as error:return{"status":"error","error":{"code":str(error)[:128] or "catalog_failed","message":"Could not read the FunPay category"}}


def _websocket_client() -> Any:
    global websocket
    if websocket is not None:
        return websocket
    try:
        import websocket as installed
    except ImportError:
        logger.info("Installing websocket-client into %s", sys.executable)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "websocket-client"],
            check=True,
            timeout=180,
        )
        import websocket as installed
    websocket = installed
    return installed


def _read_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {"enabled": False, "buywell_url": DEFAULT_BUYWELL_URL, "connection_token": ""}
    with CONFIG_FILE.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    config["buywell_url"] = DEFAULT_BUYWELL_URL
    return config


def _debug_logging_enabled() -> bool:
    env_value = os.environ.get("BUYWELL_RUNTIME_DEBUG", "").strip().lower()
    if env_value in ("1", "true", "yes", "on"):
        return True
    try:
        return bool(_read_config().get("debug_logging", False))
    except Exception:
        return False


def _diagnostic(message: str, *args: Any) -> None:
    if _debug_logging_enabled():
        logger.warning("[Buywell debug] " + message, *args)


def _write_config(config: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)


def _connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DATABASE_FILE, timeout=10)
    database.execute("PRAGMA journal_mode=WAL")
    database.execute(
        "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, body TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL)"
    )
    database.execute(
        "CREATE TABLE IF NOT EXISTS actions (idempotency_key TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT, updated_at REAL NOT NULL)"
    )
    database.execute(
        "CREATE TABLE IF NOT EXISTS capture_specification (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)"
    )
    database.execute("CREATE TABLE IF NOT EXISTS input_waits (correlation_token TEXT PRIMARY KEY, conversation_key TEXT UNIQUE NOT NULL, deadline TEXT NOT NULL)")
    database.execute("CREATE TABLE IF NOT EXISTS input_candidates (candidate_id TEXT PRIMARY KEY, correlation_token TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT '', value TEXT NOT NULL)")
    columns = {row[1] for row in database.execute("PRAGMA table_info(input_candidates)")}
    if "candidate_id" not in columns: database.execute("ALTER TABLE input_candidates ADD COLUMN candidate_id TEXT NOT NULL DEFAULT ''")
    if "observed_at" not in columns: database.execute("ALTER TABLE input_candidates ADD COLUMN observed_at TEXT NOT NULL DEFAULT ''")
    primary = next((row[1] for row in database.execute("PRAGMA table_info(input_candidates)") if row[5]), None)
    if primary == "correlation_token":
        database.execute("ALTER TABLE input_candidates RENAME TO input_candidates_legacy")
        database.execute("CREATE TABLE input_candidates (candidate_id TEXT PRIMARY KEY, correlation_token TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT '', value TEXT NOT NULL)")
        database.execute("INSERT OR IGNORE INTO input_candidates(candidate_id,correlation_token,observed_at,value) SELECT candidate_id,correlation_token,observed_at,value FROM input_candidates_legacy WHERE candidate_id<>''")
        database.execute("DROP TABLE input_candidates_legacy")
    database.commit()
    return database


def _load_capture_specification() -> None:
    global CAPTURE_SPEC
    with _connect() as database:
        row = database.execute("SELECT value FROM capture_specification WHERE singleton=1").fetchone()
    if row:
        with CAPTURE_LOCK:
            CAPTURE_SPEC = json.loads(row[0])


def _apply_capture_specification(specification: dict[str, Any]) -> None:
    global CAPTURE_SPEC
    value = {
        "revision": int(specification["revision"]),
        "digest": str(specification["digest"]),
        "subscriptions": list(specification.get("subscriptions", [])),
    }
    with _connect() as database:
        database.execute("INSERT INTO capture_specification(singleton,value) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET value=excluded.value", (json.dumps(value, ensure_ascii=False),))
    with CAPTURE_LOCK:
        CAPTURE_SPEC = value


def applyExpectedEvents(specification: dict[str, Any]) -> None:
    """Atomically apply a full optional expected-events hint; stale revisions are ignored."""
    global EXPECTED_EVENTS
    revision = int(specification.get("revision", 0))
    with CAPTURE_LOCK:
        if revision < int(EXPECTED_EVENTS.get("revision", 0)):
            return
        EXPECTED_EVENTS = {"revision": revision, "subscriptions": list(specification.get("subscriptions", []))}


def isExpected(event_type: str, event_version: str) -> bool:
    """Return True when unsupported/uninitialized, or when the current hint includes the event."""
    with CAPTURE_LOCK:
        if int(EXPECTED_EVENTS.get("revision", 0)) <= 0:
            return True
        return any(item.get("eventType") == event_type and item.get("eventVersion") == event_version for item in EXPECTED_EVENTS.get("subscriptions", []))


def currentRevision() -> int:
    with CAPTURE_LOCK:
        return int(EXPECTED_EVENTS.get("revision", 0))


def projectEvent(event_type: str, event_version: str, payload: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any] | None:
    """Build a nested projected envelope fragment from the current server specification."""
    with CAPTURE_LOCK:
        revision = int(EXPECTED_EVENTS.get("revision", 0))
        subscription = next((item for item in EXPECTED_EVENTS.get("subscriptions", []) if item.get("eventType") == event_type and item.get("eventVersion") == event_version), None)
    if revision <= 0 or subscription is None:
        return None
    def project(source: dict[str, Any], paths: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for path in paths:
            parts, value = path.split("."), source
            for part in parts:
                if not isinstance(value, dict) or part not in value: value = None; break
                value = value[part]
            if value is None: continue
            target = result
            for part in parts[:-1]: target = target.setdefault(part, {})
            target[parts[-1]] = value
        return result
    return {"projection": "projected", "expectedEventsRevision": revision, "payload": project(payload, subscription.get("payloadPaths", [])), "scope": project(scope, subscription.get("scopePaths", []))}


def _path(source: dict[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _capture_condition(condition: dict[str, Any], payload: dict[str, Any], scope: dict[str, Any]) -> bool:
    kind = condition.get("kind")
    if kind == "all": return all(_capture_condition(child, payload, scope) for child in condition.get("conditions", []))
    if kind == "any": return any(_capture_condition(child, payload, scope) for child in condition.get("conditions", []))
    if kind == "not": return not _capture_condition(condition.get("condition", {}), payload, scope)
    actual = _path(scope if condition.get("source") == "scope" else payload, str(condition.get("path", "")))
    expected, operator = condition.get("value"), condition.get("operator")
    if operator == "exists": return actual is not None
    if operator == "equals": return actual == expected
    if operator == "not-equals": return actual != expected
    if operator == "in": return isinstance(expected, list) and actual in expected
    if not isinstance(actual, str) or not isinstance(expected, str): return False
    if operator == "contains": return expected in actual
    if operator == "starts-with": return actual.startswith(expected)
    if operator == "ends-with": return actual.endswith(expected)
    if operator == "matches":
        import fnmatch
        return fnmatch.fnmatchcase(actual, expected)
    return False


def _captured(event_type: str, event_version: str, payload: dict[str, Any], scope: dict[str, Any]) -> bool:
    with CAPTURE_LOCK:
        subscriptions = list(CAPTURE_SPEC.get("subscriptions", []))
    return any(
        item.get("eventType") == event_type and item.get("eventVersion") == event_version
        and any(_capture_condition(condition, payload, scope) for condition in item.get("conditions", []))
        for item in subscriptions
    )


def _enum_slug(value: Any) -> str:
    """Return a stable, readable code instead of an SDK enum's numeric value."""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name.lower().replace("_", "-")
    return str(value).strip().lower().replace("_", "-")


def _currency_code(value: Any) -> str:
    try:
        code = getattr(value, "code", None)
    except Exception:
        code = None
    return str(code).lower() if code else _enum_slug(value)


def _find_lot(cardinal: "Cardinal", order: Any) -> tuple[str | None, str | None]:
    try:
        lots = cardinal.profile.get_sorted_lots(2).get(order.subcategory, {}).values()
        for lot in sorted(lots, key=lambda item: len(", ".join(value for value in [item.server, item.side, item.description] if value)), reverse=True):
            description = ", ".join(value for value in [lot.server, lot.side, lot.description] if value)
            if description and description in order.description:
                category = getattr(getattr(lot, "subcategory", None), "id", None)
                return str(lot.id), str(category) if category is not None else None
    except Exception:
        logger.debug("Could not match the FunPay lot", exc_info=True)
    category = getattr(getattr(order, "subcategory", None), "id", None)
    return None, str(category) if category is not None else None


def _order_payload(cardinal: "Cardinal", order: Any, event: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    lot_id = getattr(event, "lot_id", None) if event is not None else None
    found_lot_id, category_id = _find_lot(cardinal, order)
    lot_id = lot_id or found_lot_id
    payload: dict[str, Any] = {
        "orderId": str(order.id),
        "status": _enum_slug(order.status),
        "buyer": {
            "id": getattr(order, "buyer_id", None),
            "username": getattr(order, "buyer_username", None),
        },
        "chatId": getattr(order, "chat_id", None),
        "title": getattr(order, "description", None),
        "quantity": getattr(order, "amount", None),
        "price": getattr(order, "price", None),
        "currency": _currency_code(getattr(order, "currency", "")),
        "lotId": str(lot_id) if lot_id is not None else None,
        "categoryId": category_id,
        "createdAt": getattr(getattr(order, "date", None), "isoformat", lambda: None)(),
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    payload["buyer"] = {key: value for key, value in payload["buyer"].items() if value is not None}
    scope = {
        "orderId": str(order.id),
        "chatId": getattr(order, "chat_id", None),
        "buyerId": getattr(order, "buyer_id", None),
        "buyerUsername": getattr(order, "buyer_username", None),
        "lotId": str(lot_id) if lot_id is not None else None,
        "categoryId": category_id,
        "title": getattr(order, "description", None),
    }
    return payload, {key: value for key, value in scope.items() if value is not None}


def _clean_object(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _enrich_order_payload(payload: dict[str, Any], scope: dict[str, Any], order: Any) -> None:
    subcategory = getattr(order, "subcategory", None)
    category = getattr(subcategory, "category", None)
    if subcategory is not None:
        payload["subcategory"] = _clean_object({
            "id": str(getattr(subcategory, "id", "")) or None,
            "name": getattr(subcategory, "name", None),
            "fullName": getattr(subcategory, "fullname", None),
            "type": _enum_slug(getattr(subcategory, "type", "")) or None,
            "category": _clean_object({
                "id": str(getattr(category, "id", "")) or None,
                "name": getattr(category, "name", None),
            }),
        })
        category_id = payload["subcategory"].get("id")
        if category_id:
            payload["categoryId"] = category_id
            scope["categoryId"] = category_id
    payload["seller"] = _clean_object({"id": getattr(order, "seller_id", None), "username": getattr(order, "seller_username", None)})
    payload["title"] = getattr(order, "short_description", None) or payload.get("title")
    payload["fullDescription"] = getattr(order, "full_description", None)
    payload["quantity"] = getattr(order, "amount", None)
    payload["price"] = getattr(order, "sum", None)
    payload["currency"] = _currency_code(getattr(order, "currency", ""))
    payload["locale"] = getattr(order, "locale", None)
    payload["player"] = getattr(order, "player", None)
    for name in ("server", "side"):
        item = getattr(order, name, None)
        if item is not None:
            payload[name] = _clean_object({"id": getattr(item, "id", None), "name": getattr(item, "name", None)})
    review = getattr(order, "review", None)
    if review is not None:
        payload["review"] = _clean_object({
            "stars": getattr(review, "stars", None), "text": getattr(review, "text", None),
            "reply": getattr(review, "reply", None), "anonymous": getattr(review, "anonymous", None),
            "hidden": getattr(review, "hidden", None), "author": getattr(review, "author", None),
            "authorId": getattr(review, "author_id", None), "byBot": getattr(review, "by_bot", None),
            "replyByBot": getattr(review, "reply_by_bot", None),
        })
    lot_fields: dict[str, Any] = {}
    for key in getattr(order, "fields", {}):
        if key in ("summary", "desc", "payment_msg"):
            continue
        value = order.get_field_value_any(key)
        if value is None:
            continue
        lot_fields[str(key)] = value if isinstance(value, (str, int, float, bool)) else str(value)
    if lot_fields:
        payload["lotFields"] = lot_fields
    for key in list(payload):
        if payload[key] is None:
            del payload[key]


def _prepare_purchase(cardinal: "Cardinal", event: Any, event_type: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    payload, scope = _order_payload(cardinal, event.order, event)
    if not isExpected(event_type, PURCHASE_EVENT_VERSION) or not _captured(event_type, PURCHASE_EVENT_VERSION, payload, scope):
        return None
    try:
        full_order = cardinal.get_order_from_object(event.order)
        if full_order is not None:
            _enrich_order_payload(payload, scope, full_order)
    except Exception:
        logger.warning("Could not load full FunPay order %s; sending shortcut fields", getattr(event.order, "id", ""), exc_info=True)
    return payload, scope


def _enqueue(event_type: str, event_id: str, payload: dict[str, Any], scope: dict[str, Any], event_version: str = "1.0.0") -> None:
    expected = isExpected(event_type, event_version)
    captured = _captured(event_type, event_version, payload, scope)
    if not expected or not captured:
        _diagnostic(
            "event rejected before outbox: event_type=%s event_id=%s expected=%r "
            "expected_revision=%r captured=%r capture_revision=%r",
            event_type, event_id, expected, currentRevision(), captured, CAPTURE_SPEC.get("revision", 0),
        )
        return
    body = {
        "moduleId": "funpay.cardinal",
        "moduleVersion": VERSION,
        "eventType": event_type,
        "eventVersion": event_version,
        "eventId": event_id,
        "payload": payload,
        "scope": scope,
    }
    with _connect() as database:
        database.execute(
            "INSERT OR IGNORE INTO outbox(event_id, body, available_at) VALUES(?, ?, ?)",
            (event_id, json.dumps(body, ensure_ascii=False), time.time()),
        )


def _request(config: dict[str, Any], path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{config['buywell_url'].rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {config['connection_token']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _ping(config: dict[str, Any]) -> None:
    _request(config, "/v1/module-runtime/connect", {"moduleId": "funpay.cardinal", "moduleVersion": VERSION})


def _socket_url(config: dict[str, Any]) -> str:
    base = config["buywell_url"].rstrip("/")
    socket_base = "wss://" + base[8:] if base.startswith("https://") else "ws://" + base.removeprefix("http://")
    # websocket-client resolves localhost to ::1 first on some Windows hosts and
    # does not retry 127.0.0.1 when that connection is denied. The local Caddy
    # development endpoint is IPv4-bound, so make the loopback choice explicit.
    socket_base = socket_base.replace("ws://localhost", "ws://127.0.0.1", 1).replace("wss://localhost", "wss://127.0.0.1", 1)
    return socket_base + "/v1/module-runtime/socket"


def _worker(cardinal: "Cardinal") -> None:
    _diagnostic("worker started")
    retry = 1
    while not STOP.is_set():
        config = _read_config()
        if not config.get("enabled") or not config.get("connection_token"):
            STOP.wait(1)
            continue
        try:
            # Register the runtime on every startup/reconnect. The WebSocket
            # records live presence, while /connect also promotes an imported
            # package to connected and reconciles its runtime specifications.
            _ping(config)
            websocket_client = _websocket_client()
            channel = websocket_client.create_connection(_socket_url(config), timeout=10)
            channel.send(json.dumps({"type": "authenticate", "token": config["connection_token"], "moduleId": "funpay.cardinal", "moduleVersion": VERSION}))
            initial = json.loads(channel.recv())
            if initial.get("type") != "capture-spec.replace": raise RuntimeError(initial.get("code", "capture_specification_required"))
            specification = initial["specification"]
            _apply_capture_specification(specification)
            channel.send(json.dumps({"type": "capture-spec.applied", "revision": specification["revision"], "digest": specification["digest"]}))
            ready = json.loads(channel.recv())
            while ready.get("type") != "ready":
                if ready.get("type") == "capture-spec.applied.accepted" and not ready.get("accepted"): raise RuntimeError("capture_specification_rejected")
                if ready.get("type") == "expected-events.replace": applyExpectedEvents(ready["specification"])
                ready = json.loads(channel.recv())
            channel.settimeout(1); retry = 1; heartbeat_at = 0.0; pending_batch: tuple[str, list[tuple[int, str]]] | None = None; pending_input_jobs: dict[str, dict[str, Any]] = {}; submitted_candidates: set[str] = set()
            while not STOP.is_set():
                now = time.time()
                if now >= heartbeat_at:
                    channel.send(json.dumps({"type": "heartbeat"})); heartbeat_at = now + 30
                if pending_batch is None:
                    with _connect() as database:
                        rows = database.execute("SELECT id, body FROM outbox WHERE available_at <= ? ORDER BY id LIMIT 25", (now,)).fetchall()
                    events, identities, stale = [], [], []
                    for row in rows:
                        event = json.loads(row[1])
                        if _captured(event["eventType"], event["eventVersion"], event["payload"], event["scope"]):
                            events.append(event); identities.append((row[0], event["eventId"]))
                        else: stale.append(row[0])
                    if stale:
                        with _connect() as database: database.executemany("DELETE FROM outbox WHERE id=?", ((item,) for item in stale))
                    if events:
                        batch_id = str(uuid.uuid4()); pending_batch = (batch_id, identities)
                        with CAPTURE_LOCK: capture_revision = int(CAPTURE_SPEC.get("revision", 0))
                        channel.send(json.dumps({"type": "event.batch", "batchId": batch_id, "captureSpecRevision": capture_revision, "events": events}, ensure_ascii=False))
                with _connect() as database:
                    candidates = database.execute("SELECT correlation_token,candidate_id,observed_at,value FROM input_candidates ORDER BY rowid").fetchall()
                for correlation_token, candidate_id, observed_at, value in candidates:
                    if candidate_id not in submitted_candidates:
                        channel.send(json.dumps({"type": "input.candidate", "correlationToken": correlation_token, "candidateId": candidate_id, "observedAt": observed_at, "value": value}, ensure_ascii=False)); submitted_candidates.add(candidate_id)
                try: message = json.loads(channel.recv())
                except websocket_client.WebSocketTimeoutException: continue
                if message.get("type") == "event.batch.accepted" and pending_batch and message.get("batchId") == pending_batch[0]:
                    _diagnostic("event batch accepted: batch_id=%s results=%r", pending_batch[0], message.get("results", []))
                    results = {item.get("eventId"): item.get("accepted", False) for item in message.get("results", [])}
                    with _connect() as database:
                        for row_id, event_id in pending_batch[1]:
                            if results.get(event_id): database.execute("DELETE FROM outbox WHERE id=?", (row_id,))
                            else: database.execute("UPDATE outbox SET attempts=attempts+1,available_at=? WHERE id=?", (now + min(300, 2 ** 8), row_id))
                    pending_batch = None
                elif message.get("type") == "event.batch.rejected" and pending_batch:
                    with _connect() as database: database.executemany("UPDATE outbox SET attempts=attempts+1,available_at=? WHERE id=?", ((now + min(300, 2 ** 8), row_id) for row_id, _ in pending_batch[1]))
                    pending_batch = None
                elif message.get("type") == "action.request":
                    job = message["job"]
                    channel.send(json.dumps({"type": "action.result", "jobId": job["jobId"], "leaseToken": job["leaseToken"], "result": _execute_job_result(cardinal, job)}))
                elif message.get("type") == "catalog.request":
                    job=message["job"]
                    channel.send(json.dumps({"type":"catalog.result","jobId":job["jobId"],"leaseToken":job["leaseToken"],"result":_catalog_job(cardinal,job)},ensure_ascii=False))
                elif message.get("type") == "input.request":
                    job = message["job"]; pending_input_jobs[job["jobId"]] = job
                    channel.send(json.dumps({"type": "input.waiting", "jobId": job["jobId"], "leaseToken": job["leaseToken"]}))
                elif message.get("type") == "input.waiting.accepted" and message.get("accepted"):
                    job = pending_input_jobs.pop(str(message.get("jobId")), None)
                    if job:
                        with _connect() as database:
                            database.execute("INSERT INTO input_waits(correlation_token,conversation_key,deadline) VALUES(?,?,?) ON CONFLICT(conversation_key) DO UPDATE SET correlation_token=excluded.correlation_token,deadline=excluded.deadline", (message["correlationToken"], message["conversationKey"], message["deadline"]))
                        if message.get("prompt"):
                            cardinal.send_message(message["conversationKey"], message["prompt"], watermark=False)
                elif message.get("type") == "input.waits.replace":
                    with _connect() as database:
                        database.execute("DELETE FROM input_waits")
                        for wait in message.get("waiting", []): database.execute("INSERT INTO input_waits(correlation_token,conversation_key,deadline) VALUES(?,?,?)", (wait["correlationToken"], wait["conversationKey"], wait["deadline"]))
                        database.execute("DELETE FROM input_candidates WHERE correlation_token NOT IN (SELECT correlation_token FROM input_waits)")
                elif message.get("type") == "input.candidate.result":
                    correlation_token = str(message.get("correlationToken", "")); candidate_id = str(message.get("candidateId", "")); submitted_candidates.discard(candidate_id)
                    if message.get("outcome") == "retry":
                        with _connect() as database: database.execute("DELETE FROM input_candidates WHERE candidate_id=?", (candidate_id,))
                        with _connect() as database: row = database.execute("SELECT conversation_key FROM input_waits WHERE correlation_token=?", (correlation_token,)).fetchone()
                        if row and message.get("message"): cardinal.send_message(row[0], message["message"], watermark=False)
                    elif message.get("outcome") in ("resolved", "failed"):
                        with _connect() as database:
                            database.execute("DELETE FROM input_candidates WHERE correlation_token=?", (correlation_token,)); database.execute("DELETE FROM input_waits WHERE correlation_token=?", (correlation_token,))
                    elif not message.get("accepted"):
                        with _connect() as database: database.execute("DELETE FROM input_candidates WHERE candidate_id=?", (candidate_id,))
                elif message.get("type") == "capture-spec.replace":
                    specification = message["specification"]
                    _apply_capture_specification(specification)
                    channel.send(json.dumps({"type": "capture-spec.applied", "revision": specification["revision"], "digest": specification["digest"]}))
                elif message.get("type") == "expected-events.replace":
                    applyExpectedEvents(message["specification"])
        except Exception as error:
            logger.warning("Buywell WebSocket disconnected: %s", error)
            try: channel.close()
            except Exception: pass
            STOP.wait(retry); retry = min(60, retry * 2)


def _execute_job_result(cardinal: "Cardinal", job: dict[str, Any]) -> dict[str, Any]:
    key = job["idempotencyKey"]
    with _connect() as database:
        previous = database.execute("SELECT status, result FROM actions WHERE idempotency_key = ?", (key,)).fetchone()
    if previous and previous[0] in ("succeeded", "failed"):
        result = json.loads(previous[1])
    else:
        try:
            if job["nodeType"] != "funpay.cardinal/send-message":
                raise ValueError("unsupported_action")
            text = str(job["inputs"]["message"]).strip()
            if not text:
                raise ValueError("invalid_input")
            message = cardinal.send_message(job["context"]["eventScope"]["chatId"], text, watermark=False)
            if not message:
                raise RuntimeError("outcome_unknown")
            message_id = getattr(message, "id", None)
            result = {"status": "success", "outputs": {}}
            with _connect() as database:
                database.execute(
                    "INSERT OR REPLACE INTO actions VALUES(?, 'succeeded', ?, ?)",
                    (key, json.dumps(result), time.time()),
                )
        except Exception as error:
            code = _error_code(error)
            result = {"status": "error", "error": {"code": code, "message": str(error)[:500]}}
            if code not in ("temporary_failure", "rate_limited"):
                with _connect() as database:
                    database.execute("INSERT OR REPLACE INTO actions VALUES(?, 'failed', ?, ?)", (key, json.dumps(result), time.time()))
    return result


def _error_code(error: Exception) -> str:
    value = str(error).lower()
    if "unauthor" in value or "golden" in value or "session" in value:
        return "unauthorized"
    if "chat" in value and "not" in value:
        return "chat_not_found"
    if "429" in value or "rate" in value and "limit" in value:
        return "rate_limited"
    if any(marker in value for marker in ("timeout", "temporar", "503", "connection")):
        return "temporary_failure"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "temporary_failure"
    if isinstance(error, ValueError):
        return str(error)
    return "outcome_unknown"


def init(cardinal: "Cardinal") -> None:
    STOP.clear()
    _connect().close()
    _load_capture_specification()
    _diagnostic("plugin initialized: enabled=%r capture_revision=%r expected_revision=%r", _read_config().get("enabled", False), CAPTURE_SPEC.get("revision", 0), currentRevision())
    threading.Thread(target=_worker, args=(cardinal,), name="buywell-runtime", daemon=True).start()
    telegram = cardinal.telegram
    if telegram is None:
        return
    bot = telegram.bot

    def keyboard() -> Keyboard:
        config = _read_config()
        markup = Keyboard()
        markup.row(Button("🟢 Включено" if config.get("enabled") else "🔴 Выключено", callback_data=TOGGLE))
        public_url = str(config.get("buywell_url", "")).rstrip("/")
        if public_url.startswith("https://"):
            markup.row(Button("🌐 Получить ключ в Buywell", url=f"{public_url}/modules#buywell-api-keys"))
        markup.row(Button("🔑 Ввести ключ подключения", callback_data=EDIT_KEY))
        markup.row(Button("◀️ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
        return markup

    def open_settings(call: telebot.types.CallbackQuery) -> None:
        config = _read_config()
        state = "Cardinal подключён к Buywell." if config.get("enabled") and config.get("connection_token") else "1. Нажмите «Получить ключ в Buywell».\n2. Создайте ключ API и скопируйте его.\n3. Вернитесь сюда и нажмите «Ввести ключ подключения»."
        bot.edit_message_text(state, call.message.chat.id, call.message.id, reply_markup=keyboard())
        bot.answer_callback_query(call.id)

    def edit_key(call: telebot.types.CallbackQuery) -> None:
        sent = bot.send_message(call.message.chat.id, "Отправьте ключ подключения из Buywell.", reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
        telegram.set_state(call.message.chat.id, sent.id, call.from_user.id, KEY_EDITED)
        bot.answer_callback_query(call.id)

    def key_edited(message: telebot.types.Message) -> None:
        telegram.clear_state(message.chat.id, message.from_user.id, True)
        key = (message.text or "").strip()
        if not key.startswith("bwapi_"):
            bot.reply_to(message, "Ключ не подходит. Скопируйте его полностью из Buywell.")
            return
        config = _read_config(); config["connection_token"] = key; config["enabled"] = True
        try:
            _ping(config)
        except urllib.error.HTTPError as error:
            logger.warning("Buywell connection check failed: HTTP %s", error.code, exc_info=True)
            try:
                response_code = json.loads(error.read().decode("utf-8")).get("error", {}).get("code")
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_code = None
            if error.code == 401:
                bot.reply_to(message, "Ключ подключения недействителен или отозван. Создайте новый ключ в Buywell и попробуйте ещё раз.")
            elif error.code == 403 and response_code == "MODULE_NOT_AVAILABLE":
                bot.reply_to(message, f"Версия runtime {VERSION} ещё не добавлена в вашем аккаунте Buywell. Добавьте FunPay Cardinal этой версии и попробуйте ещё раз.")
            else:
                bot.reply_to(message, "Buywell отклонил проверку подключения. Попробуйте ещё раз позже.")
            return
        except (OSError, ValueError) as error:
            logger.warning("Buywell connection check failed: %s", error, exc_info=True)
            bot.reply_to(message, "Не удалось проверить подключение. Проверьте адрес Buywell и ключ, затем попробуйте ещё раз.")
            return
        _write_config(config)
        bot.reply_to(message, "✅ Cardinal подключён к Buywell.")

    def toggle(call: telebot.types.CallbackQuery) -> None:
        config = _read_config()
        if not config.get("connection_token"):
            bot.answer_callback_query(call.id, "Сначала введите ключ подключения.", show_alert=True); return
        config["enabled"] = not config.get("enabled"); _write_config(config); open_settings(call)

    telegram.cbq_handler(open_settings, lambda call: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in call.data)
    telegram.cbq_handler(edit_key, lambda call: call.data == EDIT_KEY)
    telegram.cbq_handler(toggle, lambda call: call.data == TOGGLE)
    telegram.msg_handler(key_edited, func=lambda message: telegram.check_state(message.chat.id, message.from_user.id, KEY_EDITED))


def on_new_order(cardinal: "Cardinal", event: NewOrderEvent) -> None:
    if event.order.status is not OrderStatuses.PAID:
        return
    prepared = _prepare_purchase(cardinal, event, "commerce.purchase.created")
    if prepared is None:
        return
    payload, scope = prepared
    _enqueue("commerce.purchase.created", f"funpay:{cardinal.account.id}:order:{event.order.id}:paid", payload, scope, PURCHASE_EVENT_VERSION)


def on_order_status_changed(cardinal: "Cardinal", event: OrderStatusChangedEvent) -> None:
    prepared = _prepare_purchase(cardinal, event, "commerce.purchase.status-changed")
    if prepared is None:
        return
    payload, scope = prepared
    _enqueue("commerce.purchase.status-changed", f"funpay:{cardinal.account.id}:order:{event.order.id}:{payload['status']}", payload, scope, PURCHASE_EVENT_VERSION)


def _handle_message_event(cardinal: "Cardinal", event: NewMessageEvent | LastChatMessageChangedEvent) -> None:
    _diagnostic("handler entered: event_class=%s", type(event).__name__)
    if isinstance(event, LastChatMessageChangedEvent):
        chat = event.chat
        messages = cardinal.account.get_chat_history(chat.id, interlocutor_username=chat.name)
        message = next((item for item in reversed(messages) if item.id == chat.node_msg_id), None)
        if message is None:
            _diagnostic("changed message was not found in history: message_id=%s chat_id=%s", chat.node_msg_id, chat.id)
            return
    else:
        message = event.message
    if getattr(message, "by_bot", False) or getattr(message, "author_id", 0) in (0, getattr(cardinal.account, "id", None)) or message.type is not MessageTypes.NON_SYSTEM:
        return
    text = str(message).strip()
    if not text:
        return
    conversation_keys = [str(getattr(message, "chat_id", ""))]
    author_id = getattr(message, "author_id", None)
    account_id = getattr(cardinal.account, "id", None)
    if author_id not in (None, 0) and account_id not in (None, 0):
        participant_key = "users-" + "-".join(str(value) for value in sorted((int(author_id), int(account_id))))
        if participant_key not in conversation_keys:
            conversation_keys.append(participant_key)
    with _connect() as database:
        wait = None
        for conversation_key in conversation_keys:
            wait = database.execute("SELECT correlation_token FROM input_waits WHERE conversation_key=?", (conversation_key,)).fetchone()
            if wait:
                break
        if wait:
            message_id = getattr(message, "id", None)
            if message_id is None:
                _diagnostic("input response has no stable platform message id: chat_id=%s", message.chat_id)
                return
            candidate_id = f"funpay:{cardinal.account.id}:{message.chat_id}:{message_id}"
            database.execute("INSERT OR IGNORE INTO input_candidates(candidate_id,correlation_token,observed_at,value) VALUES(?,?,?,?)", (candidate_id, wait[0], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), text))
            return
    payload = {
        "messageId": getattr(message, "id", 0),
        "chatId": message.chat_id,
        "text": text,
        "author": {"username": getattr(message, "author", None)},
    }
    scope = {"chatId": message.chat_id}
    _enqueue("messaging.message.received", f"funpay:{cardinal.account.id}:message:{payload['messageId']}", payload, scope)


def on_new_message(cardinal: "Cardinal", event: NewMessageEvent) -> None:
    _handle_message_event(cardinal, event)


def on_last_chat_message_changed(cardinal: "Cardinal", event: LastChatMessageChangedEvent) -> None:
    _handle_message_event(cardinal, event)


def cleanup(*_: Any) -> None:
    STOP.set()


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [on_last_chat_message_changed]
