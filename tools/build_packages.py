from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIRECTORIES = ("ggsel", "funpay-cardinal")
FIXED_TIME = (2020, 1, 1, 0, 0, 0)
RUNTIME_BUNDLES = {
    "ggsel": [
        "config.example.json",
        "configure.py",
        "ggsel_runtime.py",
        "install.bat",
        "install.sh",
        "install-service.bat",
        "install-service.ps1",
        "install-service.sh",
        "requirements.txt",
        "run.bat",
        "run.sh",
    ],
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


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, FIXED_TIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o100755 if path.endswith(".sh") else 0o100644
    info.external_attr = mode << 16
    return info


def _runtime_bundle(directory: Path) -> bytes | None:
    files = RUNTIME_BUNDLES.get(directory.name)
    if files is None:
        return None
    missing = [path for path in files if not (directory / "runtime" / path).is_file()]
    if missing:
        raise FileNotFoundError(
            f"{directory.name} runtime bundle is missing: {', '.join(missing)}"
        )
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(files):
            archive.writestr(
                _zip_info(path),
                (directory / "runtime" / path).read_bytes(),
                compresslevel=9,
            )
    return output.getvalue()


def build(directory: Path, output_directory: Path) -> Path:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    module = manifest["module"]
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / (
        f"{module['id']}-{module['version']}.buywell-module.zip"
    )
    artifact_path = manifest["package"]["artifact"]["path"]
    runtime_bundle = _runtime_bundle(directory)
    package_files = _referenced_files(manifest)
    entries: list[tuple[str, Path | bytes]] = [("manifest.json", manifest_path)]
    for path in package_files:
        entries.append(
            (path, runtime_bundle)
            if runtime_bundle is not None and path == artifact_path
            else (path, directory / path)
        )
    missing = [
        path for path, source in entries if isinstance(source, Path) and not source.is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"{directory.name} references missing files: {', '.join(missing)}"
        )

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for archive_path, source in sorted(entries):
            content = source.read_bytes() if isinstance(source, Path) else source
            archive.writestr(_zip_info(archive_path), content, compresslevel=9)
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
