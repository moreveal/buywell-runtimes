from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
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
                self.assertTrue((directory / relative).is_file(), f"{name}: {relative}")
            runtime_path = directory / manifest["package"]["artifact"]["path"]
            runtime_text = runtime_path.read_text(encoding="utf-8")
            self.assertIn(
                f'MODULE_VERSION = "{manifest["module"]["version"]}"'
                if name == "ggsel"
                else f'VERSION = "{manifest["module"]["version"]}"',
                runtime_text,
            )

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
            self.assertIn("manifest.json", names)
            self.assertIn("runtime/ggsel_runtime.py", names)
            self.assertIn("config.example.json", names)
            self.assertIn("install.bat", names)
            self.assertIn("install.sh", names)


if __name__ == "__main__":
    unittest.main()
