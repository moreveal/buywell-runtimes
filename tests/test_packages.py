from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_packages", ROOT / "tools" / "build_packages.py"
)
build_packages = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(build_packages)


class PackageTests(unittest.TestCase):
    def test_manifests_reference_existing_files_and_versions_match_runtime(self):
        for name in build_packages.PACKAGE_DIRECTORIES:
            directory = ROOT / name
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            for relative in build_packages._referenced_files(manifest):
                if name in build_packages.RUNTIME_BUNDLES and relative == manifest["package"]["artifact"]["path"]:
                    continue
                self.assertTrue((directory / relative).is_file(), f"{name}: {relative}")
            artifact_path = manifest["package"]["artifact"]["path"]
            if name == "ggsel":
                runtime_text = (directory / "runtime" / "ggsel_runtime.py").read_text(
                    encoding="utf-8"
                )
            else:
                runtime_text = (directory / artifact_path).read_text(encoding="utf-8")
            self.assertIn(
                f'MODULE_VERSION = "{manifest["module"]["version"]}"'
                if name == "ggsel"
                else f'VERSION = "{manifest["module"]["version"]}"',
                runtime_text,
            )

    def test_ggsel_events_offer_durable_buyer_input_collection(self):
        manifest = json.loads((ROOT / "ggsel" / "manifest.json").read_text(encoding="utf-8"))
        for event in manifest["events"]:
            resolver = next(
                item
                for item in event.get("inputResolvers", [])
                if item["id"] == "ggsel.seller.collect-input"
            )
            self.assertEqual(resolver["mode"], "deferred")
            self.assertEqual(resolver["abstractionId"], "messaging.collect-input")
            self.assertEqual(resolver["requiredContext"], [{"source": "scope", "path": "chatId"}])

    def test_ggsel_1_2_adds_stable_product_catalog(self):
        manifest = json.loads((ROOT / "ggsel" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["module"]["version"], "1.2.3")
        self.assertEqual(
            [(event["type"], event["version"]) for event in manifest["events"]],
            [
                ("commerce.purchase.created", "1.1.0"),
                ("messaging.message.received", "1.0.0"),
            ],
        )
        self.assertEqual(
            [(node["type"], node["version"]) for node in manifest["nodes"]],
            [("ggsel.seller/send-message", "1.0.0")],
        )
        self.assertEqual(
            manifest["nodes"][0]["inputSchema"]["properties"],
            {"message": {"type": "string"}},
        )

    def test_funpay_1_3_loads_category_fields_through_cardinal(self):
        manifest = json.loads((ROOT / "funpay-cardinal" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["module"]["version"], "1.3.0")
        purchase_events = [event for event in manifest["events"] if event["type"].startswith("commerce.purchase")]
        self.assertTrue(purchase_events)
        for event in purchase_events:
            self.assertEqual(event["version"], "1.3.0")
            self.assertEqual(event["bindingCatalogs"][0]["id"], "funpay.categories")
            self.assertEqual(event["bindingCatalogs"][0]["scope"]["selectorId"], "category-id")

    def test_ggsel_changelogs_date_every_release(self):
        for locale in ("ru", "en"):
            text = (ROOT / "ggsel" / "guides" / f"CHANGELOG.{locale}.md").read_text(encoding="utf-8")
            headings = [line for line in text.splitlines() if line.startswith("## ")]
            self.assertTrue(headings)
            self.assertTrue(all(" — " in heading for heading in headings))

    def test_build_is_deterministic_and_contains_only_package_files(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_output = build_packages.build(ROOT / "ggsel", Path(first))
            second_output = build_packages.build(ROOT / "ggsel", Path(second))
            self.assertEqual(
                hashlib.sha256(first_output.read_bytes()).digest(),
                hashlib.sha256(second_output.read_bytes()).digest(),
            )
            with zipfile.ZipFile(first_output) as archive:
                names = archive.namelist()
                runtime_archive = archive.read("runtime/ggsel-seller-runtime-1.2.3.zip")
            self.assertIn("manifest.json", names)
            self.assertIn("runtime/ggsel-seller-runtime-1.2.3.zip", names)
            self.assertNotIn("runtime/ggsel_runtime.py", names)
            self.assertNotIn("install.bat", names)
            with zipfile.ZipFile(BytesIO(runtime_archive)) as runtime:
                runtime_names = runtime.namelist()
                self.assertEqual(
                    runtime_names,
                    sorted(build_packages.RUNTIME_BUNDLES["ggsel"]),
                )
                self.assertIn(b'MODULE_VERSION = "1.2.3"', runtime.read("ggsel_runtime.py"))
                self.assertTrue(runtime.getinfo("install.sh").external_attr >> 16 & 0o111)
                self.assertTrue(runtime.getinfo("install-service.sh").external_attr >> 16 & 0o111)
                self.assertTrue(runtime.getinfo("run.sh").external_attr >> 16 & 0o111)


if __name__ == "__main__":
    unittest.main()
