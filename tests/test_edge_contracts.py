from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDGE_SOURCE = Path(
    os.environ.get("BUYWELL_EDGE_SOURCE", str(ROOT.parent / "buywell-edge" / "src"))
)


@unittest.skipIf(
    os.environ.get("BUYWELL_EDGE_CONTRACTS_SKIP") == "1",
    "Edge contracts run in the isolated Python 3.12 job",
)
class EdgeContractTests(unittest.TestCase):
    def _manifest(self, source: Path, module: str) -> dict:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(EDGE_SOURCE), str(source), environment.get("PYTHONPATH", "")]
        )
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                f"import json; import {module} as value; "
                "print(json.dumps(value.extension.manifest()))",
            ],
            cwd=source,
            env=environment,
            text=True,
        )
        return json.loads(output)

    def test_edge_modules_preserve_published_v1_manifests(self) -> None:
        packages = [
            (
                ROOT / "funpay-cardinal",
                ROOT / "funpay-cardinal",
                "edge.funpay_edge",
            ),
            (ROOT / "ggsel", ROOT / "ggsel", "edge.ggsel_edge"),
            (
                ROOT / "playerok-universal",
                ROOT / "playerok-universal",
                "edge.playerok_edge",
            ),
        ]
        for package, source, module in packages:
            with self.subTest(package=package.name):
                generated = self._manifest(source, module)
                legacy = json.loads((package / "manifest.json").read_text("utf-8"))
                self.assertEqual(
                    generated["compatibility"]["contractMode"],
                    "preserve-v1",
                )
                self.assertEqual(
                    generated["compatibility"]["moduleManifest"],
                    legacy,
                )
                self.assertEqual(generated["extension"]["id"], legacy["module"]["id"])
                self.assertEqual(
                    generated["extension"]["version"],
                    legacy["module"]["version"],
                )

    def test_ns_gifts_exposes_every_published_adapter_contract(self) -> None:
        generated = self._manifest(ROOT / "ns-gifts", "ns_gifts_edge")
        expected = {
            "adapter.ns-gifts/action-11111111",
            "adapter.ns-gifts/action-22222222",
            "adapter.ns-gifts/action-33333333",
            "adapter.ns-gifts/action-44444444",
            "adapter.ns-gifts/action-55555555",
            "adapter.ns-gifts/action-66666666",
            "adapter.ns-gifts/action-77777777",
            "adapter.ns-gifts/steam-88888888",
            "adapter.ns-gifts/steam-gifts-99999999",
        }
        actual = {
            item["id"]
            for item in generated["contracts"]["adapterOperations"]
        }
        self.assertEqual(actual, expected)

    def test_vendored_playerok_api_matches_locked_upstream(self) -> None:
        lock = json.loads((ROOT / "upstreams.lock.json").read_text("utf-8"))
        expected = lock["sources"]["playerok-api"]["sourceTreeSha256"]
        root = ROOT / "playerok-universal" / "edge" / "vendor" / "playerokapi"
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix in {".pyc", ".pyo"}
            ):
                continue
            relative = path.relative_to(root).as_posix()
            if relative == "listener/__init__.py":
                continue
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        self.assertEqual(digest.hexdigest(), expected)

    def test_vendored_funpay_api_matches_locked_upstream(self) -> None:
        lock = json.loads((ROOT / "upstreams.lock.json").read_text("utf-8"))
        expected = lock["sources"]["funpay-api"]["sourceTreeSha256"]
        root = ROOT / "funpay-cardinal" / "edge" / "vendor" / "FunPayAPI"
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix in {".pyc", ".pyo"}
            ):
                continue
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        self.assertEqual(digest.hexdigest(), expected)

    def test_playerok_edge_accepts_legacy_token_without_requiring_cookie_header(
        self,
    ) -> None:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(EDGE_SOURCE),
                str(ROOT / "playerok-universal"),
                environment.get("PYTHONPATH", ""),
            ]
        )
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from edge.playerok_edge import PlayerokConfiguration;"
                    "value=PlayerokConfiguration(token='legacy-token',"
                    "user_agent='Mozilla/5.0 compatible Playerok test agent');"
                    "assert value.token and value.cookies is None"
                ),
            ],
            cwd=ROOT / "playerok-universal",
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
