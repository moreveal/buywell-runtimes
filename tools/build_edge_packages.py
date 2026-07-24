from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

from buywell_edge_sdk.contracts import ExtensionDefinition
from buywell_edge_sdk.package import (
    build_package,
    generate_signing_key,
    load_signing_key,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "funpay-cardinal": {
        "source": ROOT / "funpay-cardinal",
        "entrypoint": "edge.funpay_edge:extension",
        "include": (
            "manifest.json",
            "edge/*.py",
            "edge/*.md",
            "edge/vendor/FunPayAPI/*.py",
            "edge/vendor/FunPayAPI/common/*.py",
            "edge/vendor/FunPayAPI/updater/*.py",
        ),
    },
    "ggsel": {
        "source": ROOT / "ggsel",
        "entrypoint": "edge.ggsel_edge:extension",
        "include": (
            "manifest.json",
            "edge/*.py",
            "edge/*.md",
            "runtime/ggsel_runtime.py",
            "README.md",
            "guides/*.md",
        ),
    },
    "playerok-universal": {
        "source": ROOT / "playerok-universal",
        "entrypoint": "edge.playerok_edge:extension",
        "include": (
            "manifest.json",
            "edge/*.py",
            "edge/*.md",
            "edge/vendor/*.py",
            "edge/vendor/PLAYEROKAPI_LICENSE",
            "edge/vendor/playerokapi/*.py",
            "edge/vendor/playerokapi/*.pem",
            "edge/vendor/playerokapi/listener/*.py",
            "README.md",
            "guides/*.md",
        ),
    },
    "ns-gifts": {
        "source": ROOT / "ns-gifts",
        "entrypoint": "ns_gifts_edge:extension",
        "include": ("*.py", "README*.md", "CHANGELOG*.md"),
    },
}


def build(name: str, output: Path, key_path: Path) -> Path:
    specification = PACKAGES[name]
    source = specification["source"]
    sys.path.insert(0, str(source))
    module_name, _, object_name = specification["entrypoint"].partition(":")
    extension = getattr(importlib.import_module(module_name), object_name)
    if not isinstance(extension, ExtensionDefinition):
        raise TypeError(f"{name} entrypoint is not an Edge extension")
    key = load_signing_key(key_path) if key_path.exists() else generate_signing_key(key_path)
    target = output / f"{extension.extension_id}-{extension.version}.buywell-edge.zip"
    inspection = build_package(
        extension,
        source,
        target,
        signing_key=key,
        include=specification["include"],
    )
    print(f"{target.name}  sha256:{inspection.digest}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Build signed Buywell Edge packages")
    parser.add_argument("packages", nargs="*", choices=sorted(PACKAGES))
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "edge")
    parser.add_argument(
        "--key",
        type=Path,
        default=ROOT / ".build" / "developer-key.pem",
        help="Ed25519 signing key; CI should pass the official release key",
    )
    parser.add_argument("--isolated", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    selected = arguments.packages or sorted(PACKAGES)
    if len(selected) > 1 and not arguments.isolated:
        for name in selected:
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    name,
                    "--output",
                    str(arguments.output.resolve()),
                    "--key",
                    str(arguments.key.resolve()),
                    "--isolated",
                ],
                check=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        return 0
    for name in selected:
        build(name, arguments.output.resolve(), arguments.key.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
