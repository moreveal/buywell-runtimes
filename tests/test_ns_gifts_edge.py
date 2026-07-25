from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

if os.getenv("BUYWELL_EDGE_CONTRACTS_SKIP") == "1":
    raise unittest.SkipTest("Edge contracts run in the isolated Python 3.12 job")

import httpx

EDGE_ROOT = Path(
    os.getenv("BUYWELL_EDGE_SOURCE", Path(__file__).parents[2] / "buywell-edge" / "src")
)
sys.path.insert(0, str(EDGE_ROOT))
SOURCE = Path(__file__).parents[1] / "ns-gifts" / "ns_gifts_edge.py"
spec = importlib.util.spec_from_file_location("ns_gifts_edge", SOURCE)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class NSGiftsEdgeTests(unittest.IsolatedAsyncioTestCase):
    def test_default_client_uses_outgoing_ipv4(self):
        transport = Mock()
        http = Mock()
        with (
            patch.object(module.httpx, "AsyncHTTPTransport", return_value=transport) as transport_factory,
            patch.object(module.httpx, "AsyncClient", return_value=http) as client_factory,
        ):
            client = module.NSGiftsClient(
                module.Credentials(
                    "123",
                    "login",
                    "password",
                    base64.b64encode(b"secret").decode(),
                    None,
                )
            )

        transport_factory.assert_called_once_with(local_address="0.0.0.0")
        client_factory.assert_called_once_with(
            base_url="https://api.ns.gifts",
            timeout=30,
            transport=transport,
        )
        self.assertIs(client.client, http)

    def test_canonical_signature_matches_documented_formula(self):
        secret_bytes = b"buywell-ns-gifts-test-secret"
        secret = base64.b64encode(secret_bytes).decode()
        body = (
            b'{"service_id":449,"custom_id":"5d3bcbda-6bb5-4e87-80e5-1fb5f548bb87",'
            b'"fields":[]}'
        )
        expected_string = (
            "POST\n/api/v2/create_order\n\n1720000000\ntoken-value\n"
            + hashlib.sha256(body).hexdigest()
        ).encode()
        expected = base64.b64encode(
            hmac.new(secret_bytes, expected_string, hashlib.sha256).digest()
        ).decode()
        self.assertEqual(
            module.signature(
                secret,
                "POST",
                "/api/v2/create_order",
                "",
                body,
                "1720000000",
                "token-value",
            ),
            expected,
        )

    def test_totp_matches_rfc_6238_sha1_vector(self):
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(module.totp(secret, 59), "287082")

    async def test_refreshes_expired_session_once_and_uses_new_signature(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/v2/get_token":
                return httpx.Response(
                    200,
                    json={"token": f"token-{len(requests)}", "expires_in": 7200},
                )
            if len([item for item in requests if item.url.path == "/api/v2/stock"]) == 1:
                return httpx.Response(401, json={"detail": "expired"})
            return httpx.Response(200, json={"categories": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://api.ns.gifts",
            transport=transport,
        ) as http:
            client = module.NSGiftsClient(
                module.Credentials(
                    "123",
                    "login",
                    "password",
                    base64.b64encode(b"secret").decode(),
                    None,
                ),
                client=http,
                clock=lambda: 1_720_000_000,
            )
            result = await client.request("GET", "/api/v2/stock")
        self.assertEqual(result, {"categories": []})
        self.assertEqual(
            [item.url.path for item in requests],
            [
                "/api/v2/get_token",
                "/api/v2/stock",
                "/api/v2/get_token",
                "/api/v2/stock",
            ],
        )
        self.assertEqual(
            len({item.headers["x-timestamp"] for item in requests}),
            4,
        )

    async def test_maps_whitelist_failure_without_retrying(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v2/get_token":
                return httpx.Response(403, json={"detail": "IP is not allowed"})
            raise AssertionError("unexpected request")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://api.ns.gifts",
            transport=transport,
        ) as http:
            client = module.NSGiftsClient(
                module.Credentials(
                    "123",
                    "login",
                    "password",
                    base64.b64encode(b"secret").decode(),
                    None,
                ),
                client=http,
            )
            with self.assertRaises(module.NSGiftsError) as captured:
                await client.ensure_token()
        self.assertEqual(captured.exception.code, "IP_NOT_WHITELISTED")
        self.assertEqual(str(captured.exception), "IP is not allowed")
        self.assertFalse(captured.exception.retryable)

    async def test_maps_missing_v2_permission_separately_from_whitelist(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v2/get_token":
                return httpx.Response(403, json={"detail": "API v2 permission is missing"})
            raise AssertionError("unexpected request")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://api.ns.gifts",
            transport=transport,
        ) as http:
            client = module.NSGiftsClient(
                module.Credentials(
                    "123",
                    "login",
                    "password",
                    base64.b64encode(b"secret").decode(),
                    None,
                ),
                client=http,
            )
            with self.assertRaises(module.NSGiftsError) as captured:
                await client.ensure_token()
        self.assertEqual(captured.exception.code, "ACCESS_FORBIDDEN")
        self.assertEqual(str(captured.exception), "API v2 permission is missing")

    async def test_concurrent_requests_use_unique_timestamps_and_exact_query_order(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/v2/get_token":
                return httpx.Response(200, json={"token": "token", "expires_in": 7200})
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://api.ns.gifts",
            transport=transport,
        ) as http:
            client = module.NSGiftsClient(
                module.Credentials(
                    "123",
                    "login",
                    "password",
                    base64.b64encode(b"secret").decode(),
                    None,
                ),
                client=http,
                clock=lambda: 1_720_000_000,
            )
            await module.asyncio.gather(
                client.request(
                    "GET",
                    "/api/v2/stock",
                    params=[("category", "gift"), ("region", "ru")],
                ),
                client.request("GET", "/api/v2/stock"),
            )
        self.assertEqual(requests[1].url.query, b"category=gift&region=ru")
        timestamps = [request.headers["x-timestamp"] for request in requests]
        self.assertEqual(len(timestamps), len(set(timestamps)))

    async def test_pay_conflict_reconciles_without_repeating_payment(self):
        class Client:
            credentials = module.Credentials(
                "123",
                "login",
                "password",
                base64.b64encode(b"secret").decode(),
                None,
            )

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def request(self, method: str, path: str, **_kwargs):
                self.calls.append((method, path))
                if path == "/api/v2/pay_order":
                    raise module.NSGiftsError(
                        "ORDER_CONFLICT",
                        "already paid",
                        status=409,
                    )
                return {
                    "custom_id": "a4cee2fe-ce8c-448b-bf2c-123456789012",
                    "status": 2,
                }

        client = Client()
        module._clients["connection"] = client
        try:
            with tempfile.TemporaryDirectory() as state:
                context = SimpleNamespace(
                    connection_id="connection",
                    state=Path(state),
                )
                result = await module.pay_order(
                    context,
                    module.PayOrderInput(
                        custom_id="a4cee2fe-ce8c-448b-bf2c-123456789012"
                    ),
                )
        finally:
            module._clients.clear()

        self.assertEqual(result["status"], 2)
        self.assertEqual(
            client.calls,
            [
                ("POST", "/api/v2/pay_order"),
                (
                    "GET",
                    "/api/v2/order_info/a4cee2fe-ce8c-448b-bf2c-123456789012",
                ),
            ],
        )
