from __future__ import annotations

import importlib.util
import json
import logging
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
        self.sales = [{"invoice_id": 100, "date": "2026-01-01", "product": {"id": 102602697, "name": "Discord Boosts"}}]
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
                "name": "delivered-secret-content",
                "amount": 100,
                "currency_type": "RUB",
                "buyer_info": {"email": "buyer@example.com"},
                "options": [{"id": 5699177, "user_data": "https://discord.gg/example"}, {"id": 5699176, "user_data": "5 boosts", "user_data_id": 32074498}],
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
    def test_transport_logging_never_emits_tokenized_urls(self):
        loggers = [logging.getLogger("httpx"), logging.getLogger("httpcore")]
        previous_levels = [logger.level for logger in loggers]
        try:
            with mock.patch.object(runtime.logging, "basicConfig"):
                runtime._setup_logging("INFO")
            self.assertEqual([logger.level for logger in loggers], [logging.WARNING, logging.WARNING])
        finally:
            for logger, previous_level in zip(loggers, previous_levels, strict=True):
                logger.setLevel(previous_level)

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


class ClientRetryTests(unittest.TestCase):
    def test_read_requests_retry_after_a_network_failure(self):
        client = runtime.GGSelClient(config(Path("state.sqlite3")))
        client.token = "token"
        request = runtime.httpx.Request("GET", "https://seller.ggsel.com/test")
        client.http = mock.Mock()
        client.http.request.side_effect = [runtime.httpx.ReadTimeout("timeout", request=request), runtime.httpx.Response(200, json={"retval": 0}, request=request)]
        with mock.patch.object(runtime.time, "sleep"):
            self.assertEqual(client._request("GET", "test", authenticated=True), {"retval": 0})
        self.assertEqual(client.http.request.call_count, 2)

    def test_message_send_is_not_retried_after_an_unknown_result(self):
        client = runtime.GGSelClient(config(Path("state.sqlite3")))
        client.token = "token"
        request = runtime.httpx.Request("POST", "https://seller.ggsel.com/test")
        client.http = mock.Mock()
        client.http.request.side_effect = runtime.httpx.ReadTimeout("timeout", request=request)
        with self.assertRaises(runtime.ApiError) as failure:
            client.send_message(42, "Hello")
        self.assertEqual(failure.exception.code, "outcome_unknown")
        self.assertEqual(client.http.request.call_count, 1)

    def test_product_catalog_waits_out_rate_limits_and_reuses_the_result(self):
        client = runtime.GGSelClient(config(Path("state.sqlite3")))
        client.token = "token"
        request = runtime.httpx.Request("GET", "https://seller.ggsel.com/test")
        client.http = mock.Mock()
        client.http.request.side_effect = [
            runtime.httpx.Response(429, request=request),
            runtime.httpx.Response(429, request=request),
            runtime.httpx.Response(200, json={"product": {"id": 42}}, request=request),
        ]
        with mock.patch.object(runtime.time, "sleep") as sleep:
            first = client.product_data("42")
            second = client.product_data("42")
        self.assertEqual(first, {"product": {"id": 42}})
        self.assertEqual(second, first)
        self.assertEqual(client.http.request.call_count, 3)
        self.assertGreaterEqual(sleep.call_count, 2)

    def test_goods_catalog_reuses_a_recent_success(self):
        client = runtime.GGSelClient(config(Path("state.sqlite3")))
        client.token = "token"
        request = runtime.httpx.Request("POST", "https://seller.ggsel.com/test")
        client.http = mock.Mock()
        client.http.request.return_value = runtime.httpx.Response(
            200,
            json={"goods": [{"id": 42, "name": "Discord"}]},
            request=request,
        )
        with mock.patch.object(runtime.time, "sleep"):
            self.assertEqual(client.goods(), [{"id": 42, "name": "Discord"}])
            self.assertEqual(client.goods(), [{"id": 42, "name": "Discord"}])
        self.assertEqual(client.http.request.call_count, 1)

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


class BuywellProtocolTests(unittest.TestCase):
    def tearDown(self):
        runtime.STOP.clear()
        runtime.READY.clear()

    def test_connection_check_completes_authenticated_handshake(self):
        class Channel:
            def __init__(self):
                self.sent = []
                self.responses = [
                    {
                        "type": "capture-spec.replace",
                        "specification": {"revision": 3, "digest": "a" * 64},
                    },
                    {"type": "ready"},
                ]
                self.closed = False

            def send(self, value):
                self.sent.append(json.loads(value))

            def recv(self):
                return json.dumps(self.responses.pop(0))

            def close(self):
                self.closed = True

        channel = Channel()
        with (
            mock.patch.object(runtime, "_buywell_request") as request,
            mock.patch.object(runtime.websocket, "create_connection", return_value=channel),
        ):
            runtime._check_buywell_connection(config(Path("state.sqlite3")))

        request.assert_called_once_with(
            mock.ANY,
            "/v1/module-runtime/connect",
            {"moduleId": runtime.MODULE_ID, "moduleVersion": runtime.MODULE_VERSION},
        )
        self.assertEqual(channel.sent[0]["type"], "authenticate")
        self.assertEqual(channel.sent[1]["type"], "capture-spec.applied")
        self.assertTrue(channel.closed)

    def test_socket_delivers_event_action_and_durable_input_cycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = runtime.State(Path(temporary) / "state.sqlite3")
            state.enqueue(
                "ggsel:7:purchase:100",
                {
                    "moduleId": runtime.MODULE_ID,
                    "moduleVersion": runtime.MODULE_VERSION,
                    "eventType": runtime.PURCHASE_EVENT,
                    "eventVersion": runtime.PURCHASE_EVENT_VERSION,
                    "eventId": "ggsel:7:purchase:100",
                    "payload": {"invoiceId": "100", "status": "1"},
                    "scope": {"invoiceId": "100", "chatId": 100, "sellerId": 7},
                },
            )

            class Client:
                def __init__(self):
                    self.messages = []

                def send_message(self, chat_id, message):
                    self.messages.append((chat_id, message))
                    if message == "Question":
                        state.save_input_candidate("candidate-1", "correlation-1", "bad")
                    elif message == "Try again":
                        state.save_input_candidate("candidate-2", "correlation-1", "valid")

            client = Client()

            class Channel:
                def __init__(self):
                    self.stage = 0
                    self.sent = []
                    self.closed = False

                def send(self, value):
                    self.sent.append(json.loads(value))

                def recv(self):
                    self.stage += 1
                    if self.stage == 1:
                        return json.dumps(
                            {
                                "type": "capture-spec.replace",
                                "specification": {
                                    "revision": 1,
                                    "digest": "b" * 64,
                                    "subscriptions": [
                                        {
                                            "eventType": runtime.PURCHASE_EVENT,
                                            "eventVersion": runtime.EVENT_VERSION,
                                            "conditions": [
                                                {
                                                    "source": "scope",
                                                    "path": "sellerId",
                                                    "operator": "exists",
                                                }
                                            ],
                                        }
                                    ],
                                },
                            }
                        )
                    if self.stage == 2:
                        return json.dumps({"type": "ready"})
                    if self.stage == 3:
                        batch = next(item for item in self.sent if item["type"] == "event.batch")
                        return json.dumps(
                            {
                                "type": "event.batch.accepted",
                                "batchId": batch["batchId"],
                                "results": [
                                    {"eventId": "ggsel:7:purchase:100", "accepted": True}
                                ],
                            }
                        )
                    if self.stage == 4:
                        return json.dumps(
                            {
                                "type": "action.request",
                                "job": {
                                    "jobId": "action-1",
                                    "leaseToken": "lease-action",
                                    "idempotencyKey": "execution:node",
                                    "nodeType": runtime.SEND_MESSAGE_NODE,
                                    "inputs": {"message": "Workflow message"},
                                    "context": {"eventScope": {"chatId": 100}},
                                },
                            }
                        )
                    if self.stage == 5:
                        return json.dumps(
                            {
                                "type": "input.request",
                                "job": {"jobId": "input-1", "leaseToken": "lease-input"},
                            }
                        )
                    if self.stage == 6:
                        return json.dumps(
                            {
                                "type": "input.waiting.accepted",
                                "accepted": True,
                                "jobId": "input-1",
                                "correlationToken": "correlation-1",
                                "conversationKey": "100",
                                "deadline": "2026-07-23T00:00:00Z",
                                "prompt": "Question",
                            }
                        )
                    if self.stage == 7:
                        self.assert_candidate_sent("candidate-1")
                        return json.dumps(
                            {
                                "type": "input.candidate.result",
                                "accepted": True,
                                "correlationToken": "correlation-1",
                                "candidateId": "candidate-1",
                                "outcome": "retry",
                                "message": "Try again",
                            }
                        )
                    self.assert_candidate_sent("candidate-2")
                    runtime.STOP.set()
                    return json.dumps(
                        {
                            "type": "input.candidate.result",
                            "accepted": True,
                            "correlationToken": "correlation-1",
                            "candidateId": "candidate-2",
                            "outcome": "resolved",
                        }
                    )

                def assert_candidate_sent(self, candidate_id):
                    assert any(
                        item.get("type") == "input.candidate"
                        and item.get("candidateId") == candidate_id
                        for item in self.sent
                    )

                def settimeout(self, _timeout):
                    pass

                def close(self):
                    self.closed = True

            channel = Channel()
            runtime.STOP.clear()
            with (
                mock.patch.object(runtime, "_buywell_request"),
                mock.patch.object(runtime.websocket, "create_connection", return_value=channel),
            ):
                runtime._connect_socket(config(state.path), state, client)

            self.assertEqual(state.outbox(), [])
            self.assertIsNone(state.input_wait_for_conversation("100"))
            self.assertEqual(state.input_candidates(), [])
            self.assertEqual(
                client.messages,
                [(100, "Workflow message"), (100, "Question"), (100, "Try again")],
            )
            self.assertEqual(
                [item["type"] for item in channel.sent if item["type"] in {"action.result", "input.waiting"}],
                ["action.result", "input.waiting"],
            )
            self.assertTrue(channel.closed)


class PollingTests(unittest.TestCase):
    def setUp(self):
        runtime.CAPTURE_SPEC = {
            "revision": 1,
            "digest": "a" * 64,
            "subscriptions": [
                {
                    "eventType": runtime.PURCHASE_EVENT,
                    "eventVersion": runtime.PURCHASE_EVENT_VERSION,
                    "conditions": [
                        {"source": "scope", "path": "sellerId", "operator": "exists"}
                    ],
                },
                {
                    "eventType": runtime.MESSAGE_EVENT,
                    "eventVersion": runtime.MESSAGE_EVENT_VERSION,
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
            client.sales.append({"invoice_id": 101, "date": "2026-01-02", "product": {"id": 102602697, "name": "Discord Boosts"}})
            poller.poll_sales()
            rows = state.outbox()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1]["eventId"], "ggsel:7:purchase:101")
            payload=rows[0][1]["payload"]
            self.assertEqual(payload["product"],{"id":102602697,"name":"Discord Boosts"})
            self.assertNotIn("delivered-secret-content",json.dumps(payload))
            self.assertEqual(payload["optionValues"]["5699176"],"5 boosts")
            self.assertEqual(payload["optionChoiceIds"]["5699176"],"32074498")

    def test_catalog_uses_option_and_variant_ids(self):
        client=mock.Mock()
        client.product_data.return_value={"product":{"id":102602697,"name":"Discord Boosts","options":[{"name":5699177,"label":"Invite","type":"text","variants":[]},{"name":5699176,"label":"Boosts","type":"radio","variants":[{"value":32074498,"text":"5 boosts · 1 month"}]}]}}
        result=runtime._catalog_result(client,{"requestId":"00000000-0000-4000-8000-000000000001","catalogId":"ggsel.products","catalogVersion":"1.0.0","operation":"get-scope","scopeKey":"102602697"})
        self.assertEqual(result["fields"][0],{"key":"5699177","label":"Invite","kind":"text"})
        self.assertEqual(result["fields"][1]["choices"],[{"key":"32074498","label":"5 boosts · 1 month"}])

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

    def test_waiting_buyer_reply_becomes_input_candidate_instead_of_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = runtime.State(Path(temporary) / "state.sqlite3")
            state.set_setting("messages_initialized", "1")
            state.remember_chat(100, emit_existing=True)
            state.save_input_wait("correlation-1", "100", "2026-07-23T00:00:00Z")
            client = FakeClient()
            client.message_rows[100] = [
                {"id": 2, "message": "buyer reply", "seller": 0, "buyer": 1},
            ]
            runtime.Poller(config(state.path), state, client).poll_messages()
            self.assertEqual(state.outbox(), [])
            candidates = state.input_candidates()
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0][0], "correlation-1")
            self.assertEqual(candidates[0][3], "buyer reply")

    def test_replacing_waits_removes_stale_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = runtime.State(Path(temporary) / "state.sqlite3")
            state.save_input_wait("old", "100", "2026-07-23T00:00:00Z")
            state.save_input_candidate("candidate-1", "old", "reply")
            state.replace_input_waits(
                [{"correlationToken": "new", "conversationKey": "200", "deadline": "2026-07-24T00:00:00Z"}]
            )
            self.assertIsNone(state.input_wait_for_conversation("100"))
            self.assertEqual(state.input_wait_for_conversation("200"), "new")
            self.assertEqual(state.input_candidates(), [])

    def test_wait_and_unsubmitted_reply_survive_state_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            state = runtime.State(path)
            state.save_input_wait("correlation-1", "100", "2026-07-23T00:00:00Z")
            state.save_input_candidate("candidate-1", "correlation-1", "reply")

            reopened = runtime.State(path)
            self.assertEqual(reopened.input_wait_for_conversation("100"), "correlation-1")
            self.assertEqual(reopened.input_candidates()[0][3], "reply")

    def test_unacknowledged_event_survives_state_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            state = runtime.State(path)
            state.enqueue("event-1", {"eventId": "event-1", "eventType": runtime.PURCHASE_EVENT})

            reopened = runtime.State(path)
            self.assertEqual(reopened.outbox()[0][1]["eventId"], "event-1")


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
