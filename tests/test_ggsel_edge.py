from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


if os.getenv("BUYWELL_EDGE_CONTRACTS_SKIP") == "1":
    raise unittest.SkipTest("Edge contracts run in the isolated Python 3.12 job")

ROOT = Path(__file__).parents[1]
EDGE_ROOT = Path(os.getenv("BUYWELL_EDGE_SOURCE", ROOT.parent / "buywell-edge" / "src"))
sys.path.insert(0, str(ROOT / "ggsel"))
sys.path.insert(0, str(EDGE_ROOT))
SOURCE = ROOT / "ggsel" / "edge" / "ggsel_edge.py"
spec = importlib.util.spec_from_file_location("ggsel_edge_test", SOURCE)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class GGSelParityTests(unittest.TestCase):
    def test_sales_are_registered_as_chats_before_message_polling(self) -> None:
        storage = SimpleNamespace(
            setting=lambda _: "0",
            remember_purchase=Mock(return_value=True),
            remember_chat=Mock(),
            set_setting=Mock(),
        )
        state = SimpleNamespace(
            storage=storage,
            client=SimpleNamespace(last_sales=lambda: [{"invoice_id": 42}]),
        )

        self.assertEqual(module._poll_sales(state), [])

        storage.remember_chat.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
