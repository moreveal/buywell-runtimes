import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_runtime():
    funpay = types.ModuleType("FunPayAPI")
    funpay_types = types.ModuleType("FunPayAPI.types")
    funpay_types.MessageTypes = types.SimpleNamespace(NON_SYSTEM=object())
    funpay_types.OrderStatuses = types.SimpleNamespace(PAID=object())
    updater = types.ModuleType("FunPayAPI.updater")
    events = types.ModuleType("FunPayAPI.updater.events")
    for name in ("LastChatMessageChangedEvent", "NewMessageEvent", "NewOrderEvent", "OrderStatusChangedEvent"):
        setattr(events, name, type(name, (), {}))
    telebot = types.ModuleType("telebot")
    telebot.types = types.SimpleNamespace(CallbackQuery=object)
    telebot_types = types.ModuleType("telebot.types")
    telebot_types.InlineKeyboardButton = type("InlineKeyboardButton", (), {})
    telebot_types.InlineKeyboardMarkup = type("InlineKeyboardMarkup", (), {})
    tg_bot = types.ModuleType("tg_bot")
    tg_bot.CBT = types.SimpleNamespace(PLUGIN_SETTINGS="plugin-settings")
    static_keyboards = types.ModuleType("tg_bot.static_keyboards")
    modules = {
        "FunPayAPI": funpay,
        "FunPayAPI.types": funpay_types,
        "FunPayAPI.updater": updater,
        "FunPayAPI.updater.events": events,
        "telebot": telebot,
        "telebot.types": telebot_types,
        "tg_bot": tg_bot,
        "tg_bot.static_keyboards": static_keyboards,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        path = Path(__file__).parents[1] / "funpay-cardinal" / "runtime" / "buywell_runtime.py"
        spec = importlib.util.spec_from_file_location("funpay_buywell_runtime_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FunpayCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = load_runtime()

    def test_category_url_is_restricted_to_funpay_lots(self):
        self.assertEqual(
            self.runtime._category_url("https://funpay.com/lots/1334/"),
            ("1334", "https://funpay.com/lots/1334/"),
        )
        for value in ("http://funpay.com/lots/1334/", "https://evil.test/lots/1334/", "https://funpay.com/users/1334/"):
            with self.assertRaises(ValueError):
                self.runtime._category_url(value)

    def test_catalog_parses_custom_fields_and_choice_values(self):
        page = """
          <h1>Discord boosts</h1>
          <div class="lot-fields" data-fields='[{"id":"server","name":"Server URL","type":"text"},{"id":"duration","name":"Duration","type":"select"}]'>
            <div class="lot-field" data-id="server"><input name="fields[server]" type="text"></div>
            <div class="lot-field" data-id="duration"><select name="fields[duration]"><option value="">Choose</option><option value="1_month">One month</option><option value="3_months">Three months</option></select></div>
          </div>
        """
        session = types.SimpleNamespace(get=lambda url, timeout: Response(page))
        cardinal = types.SimpleNamespace(account=types.SimpleNamespace(session=session))
        result = self.runtime._catalog_job(cardinal, {
            "requestId": "00000000-0000-4000-8000-000000000001",
            "catalogId": "funpay.categories",
            "catalogVersion": "1.0.0",
            "operation": "get-scope",
            "scopeKey": "1334",
        })
        self.assertEqual(result["status"], "success")
        fields = result["value"]["fields"]
        self.assertEqual(fields[0], {"key": "server", "label": "Server URL", "kind": "text"})
        self.assertEqual(fields[1]["choices"], [
            {"key": "1_month", "label": "1_month"},
            {"key": "3_months", "label": "3_months"},
        ])


if __name__ == "__main__":
    unittest.main()
