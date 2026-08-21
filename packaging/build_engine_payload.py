"""Build a deterministic, immutable source payload for the private engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import zipfile


SCHEMA = 1
MANIFEST_NAME = "payload-manifest.json"
_ROOT_FILES = ("LICENSE", "README.md", "pyproject.toml", "uv.lock")
_EXCLUDED_PACKAGE_PARTS = {"ui", "data", "runtime", "logs", "user", "cache", "__pycache__"}
_EXCLUDED_PACKAGE_FILES = {"server_qwen.py"}


def _payload_paths(source_root: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for relative in _ROOT_FILES:
        path = source_root / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        entries.append((relative, path))

    package_root = source_root / "opencohost"
    if not package_root.is_dir():
        raise FileNotFoundError(package_root)
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package_root)
        if any(part in _EXCLUDED_PACKAGE_PARTS for part in relative.parts):
            continue
        if relative.name in _EXCLUDED_PACKAGE_FILES:
            continue
        archive_name = PurePosixPath("opencohost", *relative.parts).as_posix()
        entries.append((archive_name, path))
    return sorted(entries, key=lambda item: item[0])


def _file_record(archive_name: str, path: Path) -> dict[str, str | int]:
    data = path.read_bytes()
    return {
        "path": archive_name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _zip_info(name: str, size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 0
    info.external_attr = (0o644 & 0xFFFF) << 16
    info.file_size = size
    return info


def build_payload(source_root: Path | str, output_path: Path | str) -> dict:
    """Write a deterministic ZIP payload and return its canonical manifest."""
    source_root = Path(source_root).resolve()
    output_path = Path(output_path)
    entries = _payload_paths(source_root)
    manifest = {
        "schema": SCHEMA,
        "format": "zip",
        "files": [_file_record(name, path) for name, path in entries],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
            files = {name: path.read_bytes() for name, path in entries}
            files[MANIFEST_NAME] = manifest_bytes
            for name in sorted(files):
                data = files[name]
                archive.writestr(_zip_info(name, len(data)), data)
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return manifest


def _read_version(source_root: Path) -> str:
    init_py = source_root / "opencohost" / "__init__.py"
    if init_py.is_file():
        text = init_py.read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            v = m.group(1)
            m2 = re.match(r'^(\d+\.\d+\.\d+)a(\d+)$', v)
            return f"{m2.group(1)}-alpha.{m2.group(2)}" if m2 else v
    return "0.0.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", nargs="?", type=Path, default=Path("."))
    parser.add_argument("output", nargs="?", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory to place engine payload in")
    args = parser.parse_args(argv)

    source_root = args.source_root
    output_path = args.output
    if output_path is None:
        if args.output_dir:
            semver = _read_version(source_root)
            output_path = args.output_dir / f"engine-{semver}.zip"
        else:
            parser.error("Either 'output' positional argument or '--output-dir' must be specified.")

    build_payload(source_root, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
