from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATH = ROOT / "ggsel" / "runtime" / "ggsel_runtime.py"

if "websocket" not in sys.modules:
    websocket_stub = types.ModuleType("websocket")
    websocket_stub.WebSocketTimeoutException = TimeoutError
    websocket_stub.create_connection = lambda *_args, **_kwargs: None
    sys.modules["websocket"] = websocket_stub

spec = importlib.util.spec_from_file_location("ggsel_runtime", RUNTIME_PATH)
runtime = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)


class FakeClient:
    def __init__(self) -> None:
        self.sales = [{"invoice_id": 100, "date": "2026-01-01"}]
        self.message_rows: dict[int, list[dict]] = {}
        self.purchase_calls: list[int] = []

    def last_sales(self):
        return list(self.sales)

    def purchase(self, invoice_id: int):
        self.purchase_calls.append(invoice_id)
        return {
            "retval": 0,
            "content": {
                "invoice_state": 1,
                "name": "Product",
                "amount": 100,
                "currency_type": "RUB",
                "buyer_info": {"email": "buyer@example.com"},
            },
        }

    def chats_with_new_messages(self):
        return list(self.message_rows)

    def messages(self, chat_id: int, after=None):
        return [
            item
            for item in self.message_rows.get(chat_id, [])
            if after is None or int(item["id"]) > after
        ]


def config(path: Path, *, emit_existing: bool = False):
    return runtime.Config(
        buywell_url="https://buywell.pro/api",
        connection_token="bwapi_test",
        seller_id=7,
        api_key="secret",
        ggsel_api_url="https://seller.ggsel.com/api_sellers/api",
        database_path=path,
        poll_interval_seconds=30,
        message_poll_interval_seconds=10,
        sales_window=100,
        request_timeout_seconds=30,
        emit_existing_on_first_start=emit_existing,
        log_level="INFO",
    )


class ConfigTests(unittest.TestCase):
    def test_valid_config_resolves_database_relative_to_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "buywell_url": "https://buywell.pro/api",
                        "connection_token": "bwapi_valid",
                        "seller_id": 12,
                        "api_key": "secret",
                    }
                ),
                encoding="utf-8",
            )
            value = runtime.Config.load(path)
            self.assertEqual(value.database_path, directory / "state/ggsel-runtime.sqlite3")

    def test_remote_plain_http_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "buywell_url": "http://buywell.example/api",
                        "connection_token": "bwapi_valid",
                        "seller_id": 12,
                        "api_key": "secret",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(runtime.ConfigurationError):
                runtime.Config.load(path)

    def test_unknown_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "connection_token": "bwapi_valid",
                        "seller_id": 12,
                        "api_key": "secret",
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(runtime.ConfigurationError):
                runtime.Config.load(path)


class ApiCheckTests(unittest.TestCase):
    def test_api_check_reads_sales_and_chats(self):
        client = mock.Mock()
        client.last_sales.return_value = []
        client.chats_with_new_messages.return_value = []
        with (
            mock.patch.object(runtime.Config, "load", return_value=config(Path("state.sqlite3"))),
            mock.patch.object(runtime, "GGSelClient", return_value=client),
        ):
            result = runtime.main(["--config", "config.json", "--check-api"])
        self.assertEqual(result, 0)
        client.login.assert_called_once_with()
        client.last_sales.assert_called_once_with()
        client.chats_with_new_messages.assert_called_once_with()

    def test_api_check_reports_missing_v1_access(self):
        client = mock.Mock()
        client.last_sales.side_effect = runtime.ApiError(
            "http_error", "GGSel returned HTTP 403", retryable=False
        )
        with (
            mock.patch.object(runtime.Config, "load", return_value=config(Path("state.sqlite3"))),
            mock.patch.object(runtime, "GGSelClient", return_value=client),
        ):
            result = runtime.main(["--config", "config.json", "--check-api"])
        self.assertEqual(result, 3)

class PollingTests(unittest.TestCase):
    def setUp(self):
        runtime.CAPTURE_SPEC = {
            "revision": 1,
            "digest": "a" * 64,
            "subscriptions": [
                {
                    "eventType": runtime.PURCHASE_EVENT,
                    "eventVersion": runtime.EVENT_VERSION,
                    "conditions": [
                        {"source": "scope", "path": "sellerId", "operator": "exists"}
                    ],
                },
                {
                    "eventType": runtime.MESSAGE_EVENT,
                    "eventVersion": runtime.EVENT_VERSION,
                    "conditions": [
                        {"source": "scope", "path": "chatId", "operator": "exists"}
                    ],
                },
            ],
        }
        runtime.EXPECTED_EVENTS = {"revision": 0, "subscriptions": []}

    def test_first_sales_scan_is_baseline_then_new_sale_emits(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = runtime.State(Path(temporary) / "state.sqlite3")
            client = FakeClient()
            poller = runtime.Poller(config(state.path), state, client)
            poller.poll_sales()
            self.assertEqual(state.outbox(), [])
            client.sales.append({"invoice_id": 101, "date": "2026-01-02"})
            poller.poll_sales()
            rows = state.outbox()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1]["eventId"], "ggsel:7:purchase:101")

    def test_only_buyer_messages_emit(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = runtime.State(Path(temporary) / "state.sqlite3")
            state.set_setting("messages_initialized", "1")
            state.remember_chat(100, emit_existing=True)
            client = FakeClient()
            client.message_rows[100] = [
                {"id": 1, "message": "seller", "seller": 1, "buyer": 0},
                {"id": 2, "message": "buyer", "seller": 0, "buyer": 1},
            ]
            runtime.Poller(config(state.path), state, client).poll_messages()
            rows = state.outbox()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1]["payload"]["text"], "buyer")


class ActionTests(unittest.TestCase):
    def test_terminal_result_is_reused_by_idempotency_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = runtime.State(Path(temporary) / "state.sqlite3")

            class Client:
                calls = 0

                def send_message(self, chat_id, message):
                    self.calls += 1

            client = Client()
            job = {
                "idempotencyKey": "workflow:revision:execution:node",
                "nodeType": runtime.SEND_MESSAGE_NODE,
                "inputs": {"message": "Hello"},
                "context": {"eventScope": {"chatId": 100}},
            }
            first = runtime._execute_action(state, client, job)
            second = runtime._execute_action(state, client, job)
            self.assertEqual(first, second)
            self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
