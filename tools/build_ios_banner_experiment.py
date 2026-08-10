#!/usr/bin/env python3
"""Prepare the consolidated iOS cache and build its manifest.

The recovered iOS cache does not contain the three imgbannerwide_hd package
files.  The Android-compatible cache contains a structurally valid replacement
whose texture payload is uncompressed RGBA8.  iOS supports RGBA8, but the
package headers must use the iOS client's LPKG version (4), not the Android
test version (6).

This script writes the three patched banner files directly into ``cache_ios``
and builds the separate iOS manifest from that consolidated directory.  The
Android source cache is never changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOLS_DIR.parent
CONFIG_DIR = BASE_DIR / "config"

DEFAULT_SOURCE_MANIFEST = CONFIG_DIR / "fixed_manifest.json"
DEFAULT_IOS_CACHE = BASE_DIR / "cache_ios"
DEFAULT_ANDROID_CACHE = BASE_DIR / "cache_android"
DEFAULT_OUTPUT_MANIFEST = CONFIG_DIR / "fixed_manifest_ios.json"
DEFAULT_REPORT = BASE_DIR / "experiments" / "ios_cache_build" / "report.json"

BANNER_FILES = (
    "imgbannerwide_hd.dab",
    "imgbannerwide_hd.dhr",
    "imgbannerwide_hd.dsb",
)

EXPECTED_BLOCK_TAGS = {
    ".dab": b"ADAT",
    ".dhr": b"HDR\0",
    ".dsb": b"SDAT",
}

HISTORICAL_IOS = {
    "imgbannerwide_hd.dab": {
        "size": 4944,
        "md5": "8acd7c47524aa14a04623ec5522536bb",
    },
    "imgbannerwide_hd.dhr": {
        "size": 4096,
        "md5": "d1485abda45e3496b8abf76205952468",
    },
    "imgbannerwide_hd.dsb": {
        "size": 4576,
        "md5": "dbdc5682744d7a20af7fe1c7a06570d1",
    },
}


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def patch_lpkg_for_ios(source: Path, destination: Path) -> dict[str, object]:
    data = source.read_bytes()
    suffix = source.suffix.lower()
    expected_tag = EXPECTED_BLOCK_TAGS.get(suffix)

    if len(data) < 16 or data[4:8] != b"LPKG":
        raise ValueError(f"not an LPKG file: {source}")
    if expected_tag is None or data[12:16] != expected_tag:
        raise ValueError(
            f"unexpected {suffix} block tag in {source}: {data[12:16]!r}"
        )
    if data[0] != data[3]:
        raise ValueError(
            f"mismatched LPKG version bytes in {source}: {data[0]} and {data[3]}"
        )

    patched = bytearray(data)
    source_version = patched[0]
    patched[0] = 4
    patched[3] = 4
    output = bytes(patched)
    atomic_write_bytes(destination, output)

    historical = HISTORICAL_IOS[source.name]
    return {
        "filename": source.name,
        "source": str(source),
        "destination": str(destination),
        "source_version": source_version,
        "output_version": 4,
        "size": len(output),
        "source_md5": bytes_md5(data),
        "output_md5": bytes_md5(output),
        "historical_ios_size": historical["size"],
        "historical_ios_md5": historical["md5"],
        "matches_historical_ios": (
            len(output) == historical["size"]
            and bytes_md5(output) == historical["md5"]
        ),
    }


def build_manifest(
    source_manifest: Path,
    output_manifest: Path,
    ios_cache: Path,
    host: str,
    port: int,
) -> dict[str, object]:
    source_data = json.loads(
        source_manifest.read_text(encoding="utf-8-sig"),
        strict=False,
    )
    if not isinstance(source_data, list):
        raise ValueError("source manifest root must be a JSON array")

    output_data: list[dict[str, object]] = []
    missing: list[str] = []
    seen: set[str] = set()

    for source_item in source_data:
        if not isinstance(source_item, dict) or not source_item.get("filename"):
            continue
        filename = str(source_item["filename"])
        if filename in seen:
            raise ValueError(f"duplicate filename in source manifest: {filename}")
        seen.add(filename)

        asset_path = ios_cache / filename
        if not asset_path.is_file():
            missing.append(filename)
            continue

        item = dict(source_item)
        item["url"] = f"http://{host}:{port}/jp/local_path/{filename}"
        item["size"] = str(asset_path.stat().st_size)
        item["checksum"] = file_md5(asset_path)
        output_data.append(item)

    output_names = {str(item["filename"]) for item in output_data}
    absent_banner_files = sorted(set(BANNER_FILES) - output_names)
    if absent_banner_files:
        raise ValueError(
            "banner files missing from generated manifest: "
            + ", ".join(absent_banner_files)
        )

    atomic_write_text(
        output_manifest,
        json.dumps(output_data, ensure_ascii=True, separators=(",", ":")),
    )
    return {
        "source_entries": len(source_data),
        "output_entries": len(output_data),
        "resolved_from_cache": len(output_data),
        "missing_assets": len(missing),
        "first_missing_assets": missing[:20],
        "output_manifest": str(output_manifest),
        "server": f"http://{host}:{port}",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9943)
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE_MANIFEST)
    parser.add_argument("--ios-cache", type=Path, default=DEFAULT_IOS_CACHE)
    parser.add_argument("--android-cache", type=Path, default=DEFAULT_ANDROID_CACHE)
    parser.add_argument("--output-manifest", type=Path, default=DEFAULT_OUTPUT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Validate the already prepared iOS cache and build only its manifest.",
    )
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    source_manifest = args.source_manifest.resolve()
    ios_cache = args.ios_cache.resolve()
    android_cache = args.android_cache.resolve()
    output_manifest = args.output_manifest.resolve()
    report_path = args.report.resolve()

    if not source_manifest.is_file():
        raise SystemExit(f"source manifest not found: {source_manifest}")
    if not ios_cache.is_dir():
        raise SystemExit(f"iOS cache not found: {ios_cache}")
    if not args.manifest_only and not android_cache.is_dir():
        raise SystemExit(f"Android-compatible cache not found: {android_cache}")

    package_results = []
    if args.manifest_only:
        missing_banners = [name for name in BANNER_FILES if not (ios_cache / name).is_file()]
        if missing_banners:
            raise SystemExit(
                "prepared iOS banner files missing: " + ", ".join(missing_banners)
            )
    else:
        for filename in BANNER_FILES:
            source = android_cache / filename
            if not source.is_file():
                raise SystemExit(f"source banner file not found: {source}")
            package_results.append(
                patch_lpkg_for_ios(source, ios_cache / filename)
            )

    manifest_result = build_manifest(
        source_manifest,
        output_manifest,
        ios_cache,
        args.host,
        args.port,
    )
    report = {
        "operation": (
            "validate prepared iOS cache and build manifest"
            if args.manifest_only
            else "prepare consolidated iOS cache and manifest"
        ),
        "ios_cache": str(ios_cache),
        "android_source_cache": str(android_cache),
        "android_source_cache_unchanged": True,
        "manifest_only": args.manifest_only,
        "package_files": package_results,
        "manifest": manifest_result,
    }
    atomic_write_text(
        report_path,
        json.dumps(report, ensure_ascii=False, indent=2),
    )

    print(f"iOS cache: {ios_cache}")
    print(f"manifest: {output_manifest}")
    print(f"report: {report_path}")
    for result in package_results:
        print(
            f"{result['filename']}: {result['size']} bytes, "
            f"MD5 {result['output_md5']}, "
            f"LPKG {result['source_version']} -> {result['output_version']}"
        )
    print(
        "manifest entries: "
        f"{manifest_result['output_entries']}"
    )
    print(f"missing assets: {manifest_result['missing_assets']}")


if __name__ == "__main__":
    main()
