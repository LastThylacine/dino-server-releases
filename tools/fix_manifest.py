#!/usr/bin/env python3
"""Rebuild the JPB manifest for a local or remote server address."""

import argparse
import datetime
import hashlib
import json
import os
import shutil
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOLS_DIR.parent
CONFIG_DIR = BASE_DIR / "config"
CACHE_DIR = BASE_DIR / "cache_android"
DEFAULT_SOURCE = CONFIG_DIR / "fixed_manifest.json"
DEFAULT_OUTPUT = CONFIG_DIR / "fixed_manifest2.json"


def file_md5(path):
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9943)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument(
        "--build-from-cache",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    cache_dir = args.cache_dir.resolve()
    source = args.source.resolve()
    output = args.output.resolve()
    if not cache_dir.is_dir():
        raise SystemExit(f"cache directory not found: {cache_dir}")
    if not source.is_file():
        raise SystemExit(f"source manifest not found: {source}")
    source_data = json.loads(source.read_text(encoding="utf-8-sig"), strict=False)
    if not isinstance(source_data, list):
        raise SystemExit("source manifest root must be a JSON array")

    checked = 0
    missing = []
    changed_hashes = 0
    data = []
    seen_filenames = set()
    for item in source_data:
        if not isinstance(item, dict) or not item.get("filename"):
            continue
        filename = str(item["filename"])
        if filename in seen_filenames:
            raise SystemExit(f"duplicate filename in source manifest: {filename}")
        seen_filenames.add(filename)
        asset_path = cache_dir / filename
        if not asset_path.is_file():
            missing.append(filename)
            continue
        item["url"] = f"http://{args.host}:{args.port}/jp/local_path/{filename}"
        checked += 1
        item["size"] = str(asset_path.stat().st_size)
        checksum = file_md5(asset_path)
        if item.get("checksum") != checksum:
            item["checksum"] = checksum
            changed_hashes += 1
        data.append(item)

    if not data:
        raise SystemExit(f"no source-manifest assets found in cache directory: {cache_dir}")

    output.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if output.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = output.with_name(f"{output.name}.backup-{stamp}")
        shutil.copy2(output, backup)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(data, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, output)

    print(f"manifest: {output}")
    print(f"server: http://{args.host}:{args.port}")
    print(f"source entries: {len(source_data)}")
    print(f"entries checked: {checked}")
    print(f"hashes updated: {changed_hashes}")
    print(f"missing assets: {len(missing)}")
    if backup:
        print(f"backup: {backup}")
    if missing:
        print("first missing assets:")
        for filename in missing[:20]:
            print(f"  {filename}")


if __name__ == "__main__":
    main()
