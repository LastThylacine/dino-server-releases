"""Fast, read-only validation of the two user-supplied cache folders."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CacheSizeMismatch:
    filename: str
    expected: int
    actual: int


@dataclass(frozen=True)
class CacheValidationReport:
    profile_key: str
    cache_dir: Path
    folder_exists: bool
    catalog_available: bool
    expected_count: int
    present_count: int
    total_bytes: int
    missing: tuple[str, ...]
    wrong_size: tuple[CacheSizeMismatch, ...]
    unreadable: tuple[str, ...]

    @property
    def okay(self) -> bool:
        return (
            self.folder_exists
            and self.catalog_available
            and self.expected_count > 0
            and not self.missing
            and not self.wrong_size
            and not self.unreadable
        )

    @property
    def issue_count(self) -> int:
        return len(self.missing) + len(self.wrong_size) + len(self.unreadable)

    @property
    def required_present_count(self) -> int:
        """Catalog entries physically present in the cache folder."""
        return max(0, self.expected_count - len(self.missing))

    @property
    def additional_count(self) -> int:
        """Files present in the folder but not required by the catalog."""
        return max(0, self.present_count - self.required_present_count)


def _catalog_candidates(profile_key: str, config_dir: Path) -> tuple[Path, ...]:
    filename = f"cache_index_{profile_key}.json"
    bundled_root = Path(getattr(sys, "_MEIPASS", config_dir.parent))
    return (
        bundled_root / "config" / filename,
        config_dir / filename,
    )


def _load_catalog(profile_key: str, config_dir: Path) -> dict[str, int]:
    for candidate in _catalog_candidates(profile_key, config_dir):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, list):
            continue
        catalog: dict[str, int] = {}
        for item in payload:
            if not isinstance(item, dict) or not item.get("filename"):
                continue
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            catalog[str(item["filename"])] = max(0, size)
        if catalog:
            return catalog
    return {}


def validate_cache_folder(
    profile_key: str,
    cache_dir: Path,
    config_dir: Path,
) -> CacheValidationReport:
    """Compare filenames and sizes without hashing or modifying cache data."""
    catalog = _load_catalog(profile_key, config_dir)
    if not cache_dir.is_dir():
        return CacheValidationReport(
            profile_key,
            cache_dir,
            False,
            bool(catalog),
            len(catalog),
            0,
            0,
            tuple(sorted(catalog)),
            (),
            (),
        )

    present: dict[str, tuple[int, bool]] = {}
    total_bytes = 0
    try:
        children = tuple(cache_dir.iterdir())
    except OSError:
        children = ()
    for path in children:
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.read(1)
            readable = True
        except OSError:
            size = 0
            readable = False
        present[path.name] = (size, readable)
        total_bytes += size

    missing = tuple(sorted(name for name in catalog if name not in present))
    wrong_size = tuple(
        CacheSizeMismatch(name, expected, present[name][0])
        for name, expected in sorted(catalog.items())
        if name in present and expected > 0 and present[name][0] != expected
    )
    unreadable = tuple(
        sorted(name for name, (_size, readable) in present.items() if not readable)
    )
    return CacheValidationReport(
        profile_key,
        cache_dir,
        True,
        bool(catalog),
        len(catalog),
        len(present),
        total_bytes,
        missing,
        wrong_size,
        unreadable,
    )
