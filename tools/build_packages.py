from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIRECTORIES = ("ggsel", "funpay-cardinal")
FIXED_TIME = (2020, 1, 1, 0, 0, 0)
PACKAGE_EXTRAS = {
    "ggsel": [
        "config.example.json",
        "configure.py",
        "install.bat",
        "install.sh",
        "requirements.txt",
        "run.bat",
        "run.sh",
    ],
    "funpay-cardinal": [],
}


def _referenced_files(manifest: dict) -> list[str]:
    package = manifest["package"]
    files = {package["artifact"]["path"]}
    branding = package.get("branding")
    if branding:
        files.add(branding["icon"])
    guides = package["guides"]
    for name in ("installation", "readme", "changelog"):
        localized = guides.get(name)
        if localized:
            files.update(localized.values())
    return sorted(files)


def build(directory: Path, output_directory: Path) -> Path:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    module = manifest["module"]
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / (
        f"{module['id']}-{module['version']}.buywell-module.zip"
    )
    package_files = sorted(
        set(_referenced_files(manifest)) | set(PACKAGE_EXTRAS[directory.name])
    )
    entries = [("manifest.json", manifest_path), *(
        (path, directory / path) for path in package_files
    )]
    missing = [path for path, source in entries if not source.is_file()]
    if missing:
        raise FileNotFoundError(
            f"{directory.name} references missing files: {', '.join(missing)}"
        )

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for archive_path, source in sorted(entries):
            info = zipfile.ZipInfo(archive_path, FIXED_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o100755 if archive_path.endswith(".sh") else 0o100644
            info.external_attr = mode << 16
            archive.writestr(info, source.read_bytes(), compresslevel=9)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Buywell runtime packages")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "dist", help="Output directory"
    )
    parser.add_argument(
        "packages",
        nargs="*",
        metavar="PACKAGE",
    )
    arguments = parser.parse_args()
    unknown = sorted(set(arguments.packages) - set(PACKAGE_DIRECTORIES))
    if unknown:
        parser.error(
            f"unknown package {', '.join(unknown)}; choose from {', '.join(PACKAGE_DIRECTORIES)}"
        )
    for name in arguments.packages or PACKAGE_DIRECTORIES:
        output = build(ROOT / name, arguments.output.resolve())
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        print(f"{output.name}  sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
