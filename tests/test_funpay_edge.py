from __future__ import annotations

import importlib.util
import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


if os.getenv("BUYWELL_EDGE_CONTRACTS_SKIP") == "1":
    raise unittest.SkipTest("Edge contracts run in the isolated Python 3.12 job")

ROOT = Path(__file__).parents[1]
EDGE_ROOT = Path(
    os.getenv("BUYWELL_EDGE_SOURCE", ROOT.parent / "buywell-edge" / "src")
)
sys.path.insert(0, str(EDGE_ROOT))
SOURCE = ROOT / "funpay-cardinal" / "edge" / "funpay_edge.py"
spec = importlib.util.spec_from_file_location("funpay_edge_test", SOURCE)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def account() -> SimpleNamespace:
    return SimpleNamespace(is_initiated=True, runner=None)


class FunPayRunnerTests(unittest.TestCase):
    def test_runner_worker_is_started_for_the_connection(self) -> None:
        started = threading.Event()
        state = module.ConnectionState()

        with patch.object(module._ObservedRunner, "loop", side_effect=started.set):
            runner = module._start_runner(account(), state, "connection")

        self.assertTrue(started.wait(1))
        self.assertIs(state.runner, runner)
        self.assertIsNotNone(state.runner_thread)
        self.assertTrue(state.runner_thread.daemon)

    def test_successful_poll_refreshes_health_timestamp(self) -> None:
        state = module.ConnectionState(error=RuntimeError("old error"))
        runner = module._ObservedRunner(account(), state)

        with patch.object(module.Runner, "get_updates", return_value={"objects": []}):
            self.assertEqual(runner.get_updates(), {"objects": []})

        self.assertIsNone(state.error)
        self.assertIsNotNone(state.last_success_at)
        self.assertIsNotNone(state.last_success_monotonic)

    def test_failed_poll_is_exposed_to_health(self) -> None:
        state = module.ConnectionState()
        runner = module._ObservedRunner(account(), state)
        error = RuntimeError("poll failed")

        with (
            patch.object(module.Runner, "get_updates", side_effect=error),
            self.assertRaisesRegex(RuntimeError, "poll failed"),
        ):
            runner.get_updates()

        self.assertIs(state.error, error)


class FunPayHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_poll_is_degraded(self) -> None:
        state = module.ConnectionState(
            account=account(),
            last_success_at="2026-07-25T00:00:00+00:00",
            last_success_monotonic=module.time.monotonic() - 91,
        )
        module._states["connection"] = state
        try:
            result = await module.health(SimpleNamespace(connection_id="connection"))
        finally:
            module._states.clear()

        self.assertEqual(result.state, module.HealthState.DEGRADED)
        self.assertEqual(result.message, "FunPay event polling is not responding")

    async def test_recent_poll_is_healthy(self) -> None:
        state = module.ConnectionState(
            account=account(),
            last_success_at="2026-07-25T00:00:00+00:00",
            last_success_monotonic=module.time.monotonic(),
        )
        module._states["connection"] = state
        try:
            result = await module.health(SimpleNamespace(connection_id="connection"))
        finally:
            module._states.clear()

        self.assertEqual(result.state, module.HealthState.HEALTHY)
        self.assertEqual(result.last_success_at, state.last_success_at)


class FunPayOrderPayloadTests(unittest.TestCase):
    def test_dynamic_lot_fields_are_emitted_with_the_purchase(self) -> None:
        order = SimpleNamespace(
            id="SZTD7WTZ",
            status=SimpleNamespace(name="PAID"),
            fields={"summary": object(), "period": object(), "method": object()},
            get_field_value_any=lambda key: {
                "period": "1 месяц",
                "method": "Ссылкой",
            }.get(key),
            description="Discord Boost, 1 месяц, Ссылкой",
            subcategory=SimpleNamespace(
                id=1334,
                name="Discord",
                fullname="Discord Boost",
                type=SimpleNamespace(name="COMMON"),
                category=SimpleNamespace(id=12, name="Social"),
            ),
        )
        shortcut = SimpleNamespace(id="SZTD7WTZ", date=None, amount=4, price=1)
        profile = SimpleNamespace(
            get_sorted_lots=lambda _: SimpleNamespace(
                get=lambda *_: {
                    73104640: SimpleNamespace(
                        id=73104640,
                        server=None,
                        side=None,
                        description="Discord Boost",
                    )
                },
            )
        )
        source = SimpleNamespace(
            id=10385604,
            get_order=lambda _: order,
            get_user=lambda _: profile,
        )

        payload, _ = module._order_payload(source, shortcut)

        self.assertEqual(
            payload["lotFields"],
            {"period": "1 месяц", "method": "Ссылкой"},
        )
        self.assertEqual(payload["lotId"], "73104640")

    def test_order_details_preserve_server_side_and_review(self) -> None:
        review = SimpleNamespace(stars=5, text="ok", reply=None)
        order = SimpleNamespace(
            id="ORDER",
            status=SimpleNamespace(name="PAID"),
            fields={},
            subcategory=None,
            server=SimpleNamespace(id=1, name="EU"),
            side=SimpleNamespace(id=2, name="Alliance"),
            review=review,
        )
        source = SimpleNamespace(
            id=1,
            get_order=lambda _: order,
            get_user=lambda _: SimpleNamespace(
                get_sorted_lots=lambda _: SimpleNamespace(get=lambda *_: {})
            ),
        )

        payload, _ = module._order_payload(source, SimpleNamespace(id="ORDER"))

        self.assertEqual(payload["server"], {"id": 1, "name": "EU"})
        self.assertEqual(payload["side"], {"id": 2, "name": "Alliance"})
        self.assertEqual(payload["review"]["stars"], 5)


class FunPayInputResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_platform_message_is_consumed_only_once(self) -> None:
        state = module.ConnectionState(account=SimpleNamespace(id=10385604))

        class Message:
            by_bot = False
            author_id = 20724137
            type = module.MessageTypes.NON_SYSTEM
            chat_id = 276588969
            id = 123
            author = "buyer"

            def __str__(self) -> str:
                return "https://discord.gg/example"

        session = SimpleNamespace(capture_specification=None)
        await module._emit_event(session, state, module.NewMessageEvent("test", Message()))
        await module._emit_event(session, state, module.NewMessageEvent("test", Message()))

        buffered = state.recent_messages["users-10385604-20724137"]
        self.assertEqual(len(buffered), 1)
        self.assertEqual(buffered[0][0], 123)
        self.assertEqual(buffered[0][2], "https://discord.gg/example")

    async def test_recent_message_resolves_later_input_without_prompt(self) -> None:
        sent: list[tuple[str, str]] = []
        state = module.ConnectionState(
            account=SimpleNamespace(
                id=10385604,
                send_message=lambda conversation, prompt, **_: sent.append((conversation, prompt)) or True,
            )
        )

        class Message:
            by_bot = False
            author_id = 20724137
            type = module.MessageTypes.NON_SYSTEM
            chat_id = 276588969
            id = 123
            author = "buyer"

            def __str__(self) -> str:
                return "https://discord.gg/example"

        await module._emit_event(
            SimpleNamespace(capture_specification=None),
            state,
            module.NewMessageEvent("test", Message()),
        )

        module._states["connection"] = state
        try:
            result = await module.collect_input(
                SimpleNamespace(connection_id="connection"),
                {
                    "idempotencyKey": "execution:url",
                    "collection": {
                        "conversationKey": "users-10385604-20724137",
                        "prompt": "Send URL",
                        "timeoutSeconds": 60,
                    },
                },
            )
        finally:
            module._states.clear()

        self.assertEqual(result, "https://discord.gg/example")
        self.assertEqual(sent, [])

    async def test_participant_conversation_key_resolves_input(self) -> None:
        state = module.ConnectionState(account=SimpleNamespace(id=10385604))
        future = asyncio.get_running_loop().create_future()
        state.pending_inputs["users-10385604-20724137"] = module.PendingInput(
            idempotency_key="execution:url",
            future=future,
            deadline=module.time.monotonic() + 60,
        )
        class Message:
            by_bot = False
            author_id = 20724137
            type = module.MessageTypes.NON_SYSTEM
            chat_id = 276588969
            id = 123
            author = "buyer"

            def __str__(self) -> str:
                return "https://discord.gg/example"

        await module._emit_event(
            SimpleNamespace(capture_specification=None),
            state,
            module.NewMessageEvent("test", Message()),
        )

        self.assertEqual(future.result(), "https://discord.gg/example")

    async def test_redelivery_reuses_wait_without_sending_prompt_twice(self) -> None:
        sent: list[tuple[str, str]] = []
        state = module.ConnectionState(
            account=SimpleNamespace(
                send_message=lambda conversation, prompt, **_: sent.append((conversation, prompt)) or True,
            )
        )
        module._states["connection"] = state
        context = SimpleNamespace(connection_id="connection")
        job = {
            "idempotencyKey": "execution:url",
            "collection": {
                "conversationKey": "users-10385604-20724137",
                "prompt": "Send URL",
                "timeoutSeconds": 60,
            },
        }
        try:
            first = asyncio.create_task(module.collect_input(context, job))
            await asyncio.sleep(0)
            second = asyncio.create_task(module.collect_input(context, job))
            await asyncio.sleep(0)
            pending = state.pending_inputs["users-10385604-20724137"]
            pending.future.set_result("https://discord.gg/example")
            self.assertEqual(await first, "https://discord.gg/example")
            self.assertEqual(await second, "https://discord.gg/example")
        finally:
            module._states.clear()

        self.assertEqual(sent, [("users-10385604-20724137", "Send URL")])


if __name__ == "__main__":
    unittest.main()
