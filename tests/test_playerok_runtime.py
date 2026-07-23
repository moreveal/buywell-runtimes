from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as Object


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "playerok-universal" / "runtime" / "buywell_playerok"


def load_runtime():
    package = types.ModuleType("buywell_playerok")
    package.__path__ = [str(RUNTIME)]
    sys.modules["buywell_playerok"] = package
    websocket = types.ModuleType("websocket")
    websocket.WebSocketTimeoutException = TimeoutError
    websocket.create_connection = lambda *args, **kwargs: None
    sys.modules.setdefault("websocket", websocket)
    loaded = {}
    for name in ("config", "state", "bridge"):
        spec = importlib.util.spec_from_file_location(
            f"buywell_playerok.{name}", RUNTIME / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


modules = load_runtime()
bridge_module = modules["bridge"]
config_module = modules["config"]
state_module = modules["state"]


def value(identifier: str, **properties):
    return Object(id=identifier, **properties)


def capture(event_type: str, conditions=None):
    return {
        "revision": 1,
        "digest": "test",
        "subscriptions": [
            {
                "eventType": event_type,
                "eventVersion": "1.0.0",
                "conditions": conditions or [],
            }
        ],
    }


class Account:
    def __init__(self):
        self.id = "seller"
        self.support_chat_id = "support"
        self.system_chat_id = "system"
        self.sent = []
        self.items = []

    def get_item(self, id):
        return next(item for item in self.items if item.id == id)

    def send_message(self, **arguments):
        self.sent.append(arguments)


class PlayerokRuntimeTests(unittest.TestCase):
    def make_bridge(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = bridge_module.RuntimeBridge(Path(temporary.name))
        account = Account()
        runtime.bot = Object(account=account)
        return runtime, account

    def test_purchase_is_filtered_bound_and_deduplicated_after_restart(self):
        runtime, account = self.make_bridge()
        game = value("game-1", name="Minecraft")
        category = value("category-1", name="Keys", options=[])
        item = value(
            "item-1",
            name="Premium key",
            price=199,
            game=game,
            category=category,
            obtaining_type=value("delivery", name="Manual"),
            attributes={},
        )
        account.items = [item]
        deal = value(
            "deal-1",
            user=value("buyer-1", username="buyer"),
            item=item,
            status=Object(name="PAID"),
            direction=Object(name="OUT"),
            created_at="2026-07-23T10:00:00Z",
            obtaining_fields=[
                value("email", value="buyer@example.com", hidden=False),
                value("password", value="secret", hidden=True),
            ],
        )
        event = Object(deal=deal, chat=value("chat-1"))
        runtime.state.save_capture_spec(
            capture(
                bridge_module.PURCHASE_EVENT,
                [
                    {
                        "source": "scope",
                        "path": "itemId",
                        "operator": "in",
                        "value": ["item-1", "item-2"],
                    }
                ],
            )
        )
        self.assertTrue(runtime.handle_purchase(runtime.bot, event))
        self.assertFalse(runtime.handle_purchase(runtime.bot, event))
        restarted = state_module.RuntimeState(runtime.state.path)
        rows = restarted.outbox()
        self.assertEqual(len(rows), 1)
        body = rows[0][1]
        self.assertEqual(body["eventId"], "playerok:purchase:deal-1")
        self.assertEqual(body["scope"]["itemId"], "item-1")
        self.assertEqual(body["scope"]["categoryId"], "category-1")
        self.assertEqual(body["scope"]["gameId"], "game-1")
        self.assertEqual(body["payload"]["fieldChoiceIds"]["__item"], "item-1")
        self.assertEqual(
            body["payload"]["fieldChoiceIds"]["__obtaining_type"],
            "delivery",
        )
        self.assertEqual(
            body["payload"]["fieldValues"]["email"],
            "buyer@example.com",
        )
        self.assertNotIn("password", body["payload"]["fieldValues"])

    def test_first_activation_ignores_listener_history(self):
        runtime, account = self.make_bridge()
        runtime.enable()
        runtime.state.save_capture_spec(capture(bridge_module.PURCHASE_EVENT))
        item = value(
            "item-1",
            name="Key",
            game=value("game-1", name="Game"),
            category=value("category-1", name="Category", options=[]),
            obtaining_type=value("delivery", name="Manual"),
            attributes={},
        )
        account.items = [item]
        old_deal = value(
            "deal-old",
            user=value("buyer-1", username="buyer"),
            item=item,
            status=Object(name="PAID"),
            direction=Object(name="OUT"),
            created_at="2020-01-01T00:00:00Z",
            obtaining_fields=[],
        )
        self.assertFalse(
            runtime.handle_purchase(
                runtime.bot, Object(deal=old_deal, chat=value("chat-1"))
            )
        )
        self.assertEqual(runtime.state.outbox(), [])

    def test_messages_ignore_self_system_events_and_ambiguous_item_context(self):
        runtime, account = self.make_bridge()
        runtime.state.save_capture_spec(capture(bridge_module.MESSAGE_EVENT))
        buyer = value("buyer-1", username="buyer")
        message = value(
            "message-1",
            user=buyer,
            text="Hello",
            created_at="2026-07-23T10:00:00Z",
            images=[],
            event=None,
            deal=None,
        )
        chat = value("chat-1", deals=[value("deal-1", user=buyer), value("deal-2", user=buyer)])
        self.assertTrue(runtime.handle_message(runtime.bot, Object(message=message, chat=chat)))
        body = runtime.state.outbox()[0][1]
        self.assertNotIn("itemId", body["scope"])
        self.assertFalse(
            runtime.handle_message(
                runtime.bot,
                Object(
                    message=value(
                        "message-self",
                        user=account,
                        text="own",
                        images=[],
                        event=None,
                        deal=None,
                    ),
                    chat=value("chat-2", deals=[]),
                ),
            )
        )
        self.assertFalse(
            runtime.handle_message(
                runtime.bot,
                Object(
                    message=value(
                        "message-event",
                        user=buyer,
                        text="",
                        images=[],
                        event=Object(),
                        deal=None,
                    ),
                    chat=value("chat-3", deals=[]),
                ),
            )
        )

    def test_send_in_context_is_exact_and_idempotent(self):
        runtime, account = self.make_bridge()
        job = {
            "nodeType": bridge_module.SEND_MESSAGE_NODE,
            "idempotencyKey": "job-1",
            "inputs": {"message": "  exact text  "},
            "context": {"eventScope": {"chatId": "chat-1"}},
        }
        first = runtime.execute_action(job)
        second = runtime.execute_action(job)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second, first)
        self.assertEqual(
            account.sent, [{"chat_id": "chat-1", "text": "  exact text  "}]
        )
        runtime.state.begin_action("interrupted")
        unknown = runtime.execute_action({**job, "idempotencyKey": "interrupted"})
        self.assertEqual(unknown["error"]["code"], "outcome_unknown")
        self.assertEqual(len(account.sent), 1)

    def test_category_catalog_builds_item_matrix_and_dynamic_fields(self):
        runtime, account = self.make_bridge()
        game = value("game-1", name="Minecraft")
        category = value(
            "category-1",
            name="Keys",
            options=[
                Object(field="edition", value="java", label="Java", group="Edition"),
                Object(field="edition", value="bedrock", label="Bedrock", group="Edition"),
            ],
        )
        account.items = [
            value(
                f"item-{index}",
                name=f"Premium key {index}",
                game=game,
                category=category,
                obtaining_type=value("delivery", name="Manual"),
                attributes={"edition": "java"},
                status=Object(name="DRAFT" if index == 0 else "APPROVED"),
            )
            for index in range(105)
        ]
        runtime._catalog_cache = (float("inf"), account.items)
        account.get_game_category_data_fields = lambda **kwargs: Object(
            data_fields=[
                value("email", label="Email", required=True, hidden=False),
                value("password", label="Password", required=True, hidden=True),
            ],
            page_info=Object(has_next_page=False),
        )
        listed = runtime.catalog_result(
            {
                "requestId": "request-1",
                "catalogId": bridge_module.CATALOG_ID,
                "catalogVersion": bridge_module.CATALOG_VERSION,
                "operation": "list-scopes",
                "query": "MINECRAFT",
                "cursor": "0",
            }
        )
        self.assertEqual(
            listed["scopes"],
            [{"key": "category-1", "label": "Minecraft · Keys"}],
        )
        self.assertNotIn("nextCursor", listed)
        selected = runtime.catalog_result(
            {
                "requestId": "request-2",
                "catalogId": bridge_module.CATALOG_ID,
                "catalogVersion": bridge_module.CATALOG_VERSION,
                "operation": "get-scope",
                "scopeKey": "category-1",
            }
        )
        fields = {field["key"]: field for field in selected["fields"]}
        self.assertEqual(fields["__item"]["kind"], "choice")
        self.assertEqual(len(fields["__item"]["choices"]), 105)
        self.assertIn("Черновик", fields["__item"]["choices"][0]["label"])
        self.assertEqual(fields["__obtaining_type"]["kind"], "choice")
        self.assertEqual(fields["edition"]["kind"], "choice")
        self.assertEqual(fields["email"]["kind"], "text")
        self.assertTrue(fields["email"]["required"])
        self.assertNotIn("password", fields)

    def test_outbox_ack_retry_and_capture_spec_replacement(self):
        runtime, _ = self.make_bridge()
        runtime.state.save_capture_spec(capture(bridge_module.MESSAGE_EVENT))
        runtime.state.enqueue_once(
            "playerok:message:one",
            {
                "eventId": "playerok:message:one",
                "eventType": bridge_module.MESSAGE_EVENT,
                "eventVersion": "1.0.0",
                "payload": {},
                "scope": {},
            },
        )
        channel = Object(sent=[])
        channel.send = channel.sent.append
        batch = runtime._send_outbox(channel)
        self.assertIsNotNone(batch)
        sent = json.loads(channel.sent[-1])
        self.assertEqual(sent["type"], "event.batch")
        runtime.state.retry_events([batch[1][0][0]])
        self.assertEqual(runtime.state.outbox(), [])
        replacement = {"revision": 2, "digest": "next", "subscriptions": []}
        runtime._apply_capture_spec(channel, replacement)
        self.assertEqual(runtime.state.capture_spec(), replacement)

    def test_configuration_is_private_and_never_returns_the_key_in_status(self):
        runtime, _ = self.make_bridge()
        runtime.configure_token("buywell-test-secret")
        config = runtime.config_store.load()
        self.assertEqual(config.connection_token, "buywell-test-secret")
        self.assertEqual(os.stat(runtime.config_store.path).st_mode & 0o777, 0o600)
        self.assertNotIn("buywell-test-secret", json.dumps(runtime.status()))

    def test_telegram_key_handler_deletes_the_message_before_saving(self):
        source = (RUNTIME / "telegram_ui.py").read_text(encoding="utf-8")
        handler = source[source.index("async def receive_connection_key") :]
        self.assertLess(handler.index("await message.delete()"), handler.index("runtime.configure_token(token)"))
        self.assertIn('Command("buywell")', source)
        self.assertIn("_authorized", source)


if __name__ == "__main__":
    unittest.main()
