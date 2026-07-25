from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from buywell_edge_sdk import Health, HealthState, adapter_driver, configuration_field

BASE_URL = "https://api.ns.gifts"
TERMINAL_STATUSES = {2: "completed", 5: "canceled", 7: "refunded"}


class NSGiftsConfiguration(BaseModel):
    user_id: SecretStr = configuration_field(
        label={"ru": "ID пользователя", "en": "User ID"},
    )
    login: SecretStr = configuration_field(
        label={"ru": "Логин", "en": "Login"},
    )
    password: SecretStr = configuration_field(
        label={"ru": "Пароль", "en": "Password"},
    )
    api_secret: SecretStr = configuration_field(
        label={"ru": "Секрет API", "en": "API secret"},
    )
    totp_secret: SecretStr | None = configuration_field(
        label={"ru": "Секрет TOTP", "en": "TOTP secret"},
        default=None,
    )


class DynamicField(BaseModel):
    key: str
    value: Any


class StockInput(BaseModel):
    pass


class CreateOrderInput(BaseModel):
    service_id: int
    fields: list[DynamicField]
    custom_id: str | None = None


class PayOrderInput(BaseModel):
    custom_id: str


class OrderInfoInput(BaseModel):
    custom_id: str


class FulfillOrderInput(CreateOrderInput):
    timeout_seconds: int = Field(default=180, ge=5, le=900)


class ProviderOutput(BaseModel):
    model_config = ConfigDict(extra="allow")


class NSGiftsError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status


def canonical_body(value: dict[str, Any] | None) -> bytes:
    return b"" if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def canonical_string(method: str, path: str, query: str, body: bytes, timestamp: str, token: str | None) -> bytes:
    parts = [method.upper(), path, query, timestamp]
    if token is not None:
        parts.append(token)
    parts.append(hashlib.sha256(body).hexdigest())
    return "\n".join(parts).encode("utf-8")


def signature(api_secret: str, method: str, path: str, query: str, body: bytes, timestamp: str, token: str | None) -> str:
    digest = hmac.new(
        base64.b64decode(api_secret, validate=True),
        canonical_string(method, path, query, body, timestamp, token),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def totp(secret: str, at: int | None = None) -> str:
    normalized = secret.replace(" ", "").upper()
    key = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
    counter = (at if at is not None else int(time.time())) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


@dataclass(frozen=True)
class Credentials:
    user_id: str
    login: str
    password: str
    api_secret: str
    totp_secret: str | None


class NSGiftsClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        client: httpx.AsyncClient | None = None,
        clock: callable = time.time,
    ) -> None:
        self.credentials = credentials
        self.client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=30,
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
        )
        self._owns_client = client is None
        self.clock = clock
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.clock_offset = 0.0
        self.last_timestamp = 0
        self.last_success_at: str | None = None
        self._lock = asyncio.Lock()

    def _timestamp(self) -> str:
        current = int(self.clock() + self.clock_offset)
        value = max(current, self.last_timestamp + 1)
        if value - current > 55:
            raise NSGiftsError("CLOCK_SKEW", "Too many signed requests are waiting for a unique timestamp", retryable=True)
        self.last_timestamp = value
        return str(value)

    def _observe_date(self, response: httpx.Response) -> None:
        value = response.headers.get("date")
        if not value:
            return
        try:
            remote = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return
        measured = remote - self.clock()
        if abs(measured) <= 300:
            self.clock_offset = measured

    async def login(self) -> None:
        body = canonical_body({"login": self.credentials.login, "password": self.credentials.password})
        timestamp = self._timestamp()
        response = await self.client.post(
            "/api/v2/get_token",
            content=body,
            headers={
                "X-User-Id": self.credentials.user_id,
                "X-Timestamp": timestamp,
                "X-Signature": signature(self.credentials.api_secret, "POST", "/api/v2/get_token", "", body, timestamp, None),
                "Content-Type": "application/json",
            },
        )
        self._observe_date(response)
        payload = self._payload(response)
        self.token = str(payload["token"])
        self.token_expires_at = self.clock() + int(payload.get("expires_in", 7200))
        self.last_success_at = datetime.now(UTC).isoformat()

    async def ensure_token(self) -> None:
        if not self.token or self.token_expires_at - self.clock() < 300:
            await self.login()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, Any]] | None = None,
        json_body: dict[str, Any] | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            await self.ensure_token()
            query = urlencode([(key, str(value)) for key, value in (params or [])])
            body = canonical_body(json_body)
            timestamp = self._timestamp()
            headers = {
                "X-User-Id": self.credentials.user_id,
                "X-Timestamp": timestamp,
                "X-Token": self.token or "",
                "X-Signature": signature(self.credentials.api_secret, method, path, query, body, timestamp, self.token),
                "Content-Type": "application/json",
            }
            url = path + (f"?{query}" if query else "")
            try:
                response = await self.client.request(method, url, content=body, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                raise NSGiftsError("PROVIDER_UNAVAILABLE", "NSGifts did not respond", retryable=True) from error
            self._observe_date(response)
            if response.status_code == 401 and refresh:
                self.token = None
                await self.login()
                return await self._request_without_lock(method, path, query, body)
            payload = self._payload(response)
            self.last_success_at = datetime.now(UTC).isoformat()
            return payload

    async def _request_without_lock(self, method: str, path: str, query: str, body: bytes) -> dict[str, Any]:
        timestamp = self._timestamp()
        headers = {
            "X-User-Id": self.credentials.user_id,
            "X-Timestamp": timestamp,
            "X-Token": self.token or "",
            "X-Signature": signature(self.credentials.api_secret, method, path, query, body, timestamp, self.token),
            "Content-Type": "application/json",
        }
        url = path + (f"?{query}" if query else "")
        try:
            response = await self.client.request(method, url, content=body, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise NSGiftsError("PROVIDER_UNAVAILABLE", "NSGifts did not respond", retryable=True) from error
        self._observe_date(response)
        payload = self._payload(response)
        self.last_success_at = datetime.now(UTC).isoformat()
        return payload

    def _payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = str(payload.get("detail") or payload.get("message") or f"NSGifts returned HTTP {response.status_code}")
        if response.status_code == 403:
            normalized = message.casefold()
            if normalized.startswith("ip ") or any(
                marker in normalized
                for marker in ("ip address", "client ip", "source ip", "whitelist", "allowlist")
            ):
                raise NSGiftsError("IP_NOT_WHITELISTED", message, status=403)
            raise NSGiftsError(
                "ACCESS_FORBIDDEN",
                message if message != "NSGifts returned HTTP 403" else "NSGifts denied API access; verify the IP whitelist and API v2 permissions",
                status=403,
            )
        if response.status_code == 428:
            raise NSGiftsError("TOTP_REQUIRED", "NSGifts requires a current TOTP code", status=428)
        if response.status_code == 409:
            raise NSGiftsError("ORDER_CONFLICT", message, status=409)
        if response.status_code == 401:
            raise NSGiftsError("AUTH_REQUIRED", "NSGifts credentials, signature, or session were rejected", status=401)
        if response.status_code == 404:
            raise NSGiftsError("ORDER_NOT_FOUND", message, status=404)
        if response.status_code == 429 or response.status_code >= 500:
            raise NSGiftsError("PROVIDER_UNAVAILABLE", message, retryable=True, status=response.status_code)
        if not response.is_success:
            code = "INSUFFICIENT_FUNDS" if "insufficient" in message.lower() else "PROVIDER_REJECTED"
            raise NSGiftsError(code, message, status=response.status_code)
        if not isinstance(payload, dict):
            raise NSGiftsError("MALFORMED_RESPONSE", "NSGifts returned an unexpected response")
        return payload

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


extension = adapter_driver(
    extension_id="adapter.ns-gifts",
    version="1.0.2",
    display_name={"ru": "NSGifts Wholesale", "en": "NSGifts Wholesale"},
    description={
        "ru": "Оптовые подарочные карты через IP вашего Buywell Edge",
        "en": "Wholesale gift cards through your Buywell Edge IP",
    },
    publisher="Buywell",
    entrypoint="ns_gifts_edge:extension",
    config_model=NSGiftsConfiguration,
    network_domains=["api.ns.gifts"],
    dependencies=["httpx==0.28.1"],
    guides={"ru": "README.ru.md", "en": "README.en.md"},
    changelog={"ru": "CHANGELOG.ru.md", "en": "CHANGELOG.en.md"},
)

_clients: dict[str, NSGiftsClient] = {}


def _credentials(session: Any) -> Credentials:
    source = session.secrets
    missing = [name for name in ("user_id", "login", "password", "api_secret") if not source.get(name)]
    if missing:
        raise NSGiftsError("CONFIGURATION_REQUIRED", f"Missing Edge secrets: {', '.join(missing)}")
    return Credentials(
        user_id=str(source["user_id"]),
        login=str(source["login"]),
        password=str(source["password"]),
        api_secret=str(source["api_secret"]),
        totp_secret=str(source["totp_secret"]) if source.get("totp_secret") else None,
    )


def _client(session: Any) -> NSGiftsClient:
    client = _clients.get(session.connection_id)
    if not client:
        client = NSGiftsClient(_credentials(session))
        _clients[session.connection_id] = client
    return client


def _state_file(context: Any, custom_id: str) -> Path:
    return Path(context.state) / f"order-{custom_id}.json"


def _save_state(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


async def _order_info(client: NSGiftsClient, custom_id: str) -> dict[str, Any]:
    return await client.request("GET", f"/api/v2/order_info/{custom_id}")


@extension.operation(
    "adapter.ns-gifts/stock-11111111",
    "1.0.0",
    input_model=StockInput,
    output_model=ProviderOutput,
    display_name={"ru": "Остатки", "en": "Stock"},
)
async def stock(context: Any, _input: StockInput) -> dict[str, Any]:
    return await _client(context).request("GET", "/api/v2/stock")


@extension.operation(
    "adapter.ns-gifts/create-order-22222222",
    "1.0.0",
    input_model=CreateOrderInput,
    output_model=ProviderOutput,
    display_name={"ru": "Создать заказ", "en": "Create order"},
)
async def create_order(context: Any, value: CreateOrderInput) -> dict[str, Any]:
    custom_id = value.custom_id or str(uuid.uuid4())
    uuid.UUID(custom_id, version=4)
    path = _state_file(context, custom_id)
    body = {"service_id": value.service_id, "custom_id": custom_id, "fields": [field.model_dump() for field in value.fields]}
    _save_state(path, {"custom_id": custom_id, "phase": "creating", "body": body})
    try:
        result = await _client(context).request("POST", "/api/v2/create_order", json_body=body)
    except NSGiftsError as error:
        if error.code not in ("ORDER_CONFLICT", "PROVIDER_UNAVAILABLE"):
            raise
        try:
            result = await _order_info(_client(context), custom_id)
        except NSGiftsError:
            raise error
    _save_state(path, {"custom_id": custom_id, "phase": "created", "body": body, "result": result})
    return result


@extension.operation(
    "adapter.ns-gifts/pay-order-33333333",
    "1.0.0",
    input_model=PayOrderInput,
    output_model=ProviderOutput,
    display_name={"ru": "Оплатить заказ", "en": "Pay order"},
)
async def pay_order(context: Any, value: PayOrderInput) -> dict[str, Any]:
    client = _client(context)
    body: dict[str, Any] = {"custom_id": value.custom_id}
    if client.credentials.totp_secret:
        body["totp_code"] = totp(client.credentials.totp_secret)
    path = _state_file(context, value.custom_id)
    _save_state(path, {"custom_id": value.custom_id, "phase": "paying"})
    try:
        result = await client.request("POST", "/api/v2/pay_order", json_body=body)
    except NSGiftsError as error:
        if error.code == "TOTP_REQUIRED" and client.credentials.totp_secret:
            body["totp_code"] = totp(client.credentials.totp_secret)
            result = await client.request("POST", "/api/v2/pay_order", json_body=body)
        elif error.code in ("ORDER_CONFLICT", "PROVIDER_UNAVAILABLE"):
            result = await _order_info(client, value.custom_id)
            reconciled_status = result.get("status")
            if reconciled_status in (0, "0", "created", "unpaid"):
                raise NSGiftsError(
                    "PAYMENT_NOT_APPLIED",
                    "NSGifts confirms that the order is still unpaid",
                    retryable=True,
                ) from error
            if reconciled_status is None:
                raise NSGiftsError(
                    "PAYMENT_STATE_UNKNOWN",
                    "NSGifts did not confirm whether the payment was applied",
                    retryable=True,
                ) from error
        else:
            raise
    _save_state(path, {"custom_id": value.custom_id, "phase": "paid", "result": result})
    if str(result.get("status", "")).lower() == "insufficient":
        raise NSGiftsError("INSUFFICIENT_FUNDS", "NSGifts balance is insufficient")
    return result


@extension.operation(
    "adapter.ns-gifts/order-info-44444444",
    "1.0.0",
    input_model=OrderInfoInput,
    output_model=ProviderOutput,
    display_name={"ru": "Статус заказа", "en": "Order info"},
)
async def order_info(context: Any, value: OrderInfoInput) -> dict[str, Any]:
    return await _order_info(_client(context), value.custom_id)


@extension.operation(
    "adapter.ns-gifts/fulfill-order-55555555",
    "1.0.0",
    input_model=FulfillOrderInput,
    output_model=ProviderOutput,
    display_name={"ru": "Исполнить заказ", "en": "Fulfill order"},
)
async def fulfill_order(context: Any, value: FulfillOrderInput) -> dict[str, Any]:
    custom_id = value.custom_id or str(uuid.uuid4())
    created = await create_order(
        context,
        CreateOrderInput(
            service_id=value.service_id,
            fields=value.fields,
            custom_id=custom_id,
        ),
    )
    custom_id = str(created.get("custom_id") or custom_id)
    paid = await pay_order(context, PayOrderInput(custom_id=custom_id))
    if paid.get("status") != "in_progress":
        return paid
    deadline = time.monotonic() + value.timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        current = await _order_info(_client(context), custom_id)
        status = current.get("status")
        if status in TERMINAL_STATUSES:
            return {**current, "status": TERMINAL_STATUSES[status]}
    raise NSGiftsError("ORDER_PENDING", "NSGifts order is still in progress", retryable=True)


@extension.health
async def health(session: Any) -> Health:
    try:
        client = _client(session)
        await client.ensure_token()
        return Health(
            state=HealthState.HEALTHY,
            session_expires_at=datetime.fromtimestamp(client.token_expires_at, UTC).isoformat(),
            last_success_at=client.last_success_at,
        )
    except NSGiftsError as error:
        state = HealthState.AUTH_REQUIRED if error.code in ("AUTH_REQUIRED", "CONFIGURATION_REQUIRED", "TOTP_REQUIRED") else HealthState.DEGRADED
        return Health(state=state, message=str(error))


@extension.on_stop
async def stop(session: Any) -> None:
    client = _clients.pop(session.connection_id, None)
    if client:
        await client.close()
