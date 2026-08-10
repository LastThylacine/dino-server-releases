"""Offline release-notes source for the "what changed" dialog.

The bundled ``CHANGELOG.md`` is the authoritative source: it ships inside the
same signed-and-hashed update payload as the executable, works without a
network connection, and always describes the build that is actually installed.
GitHub release bodies are untrusted remote text, so they are only ever reduced
to plain lines through :func:`sanitize_remote_notes`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


MAX_CHANGELOG_BYTES = 400_000
MAX_SECTION_ITEMS = 40
MAX_ITEM_CHARS = 400
MAX_REMOTE_CHARS = 4_000

_HEADING_RE = re.compile(
    r"^##\s+(?P<title>.*?(?P<version>\d+\.\d+\.\d+).*?)\s*$"
)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class ReleaseSection:
    """One version block parsed out of the changelog."""

    version: str
    heading: str
    items: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.items


def _clean_inline(text: str) -> str:
    text = _LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    return " ".join(text.split())


def read_changelog_text(candidates: tuple[Path, ...]) -> str:
    """Return the first readable changelog, or an empty string."""
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > MAX_CHANGELOG_BYTES:
                continue
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return ""


def parse_changelog(text: str) -> list[ReleaseSection]:
    """Split changelog markdown into ordered version sections."""
    sections: list[ReleaseSection] = []
    heading = ""
    version = ""
    items: list[str] = []

    def flush() -> None:
        if version:
            sections.append(
                ReleaseSection(
                    version=version,
                    heading=heading,
                    items=tuple(items[:MAX_SECTION_ITEMS]),
                )
            )

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _HEADING_RE.match(line)
        if match:
            flush()
            heading = _clean_inline(match.group("title"))
            version = match.group("version")
            items = []
            continue
        if not version:
            continue
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            items.append(_clean_inline(stripped[2:])[:MAX_ITEM_CHARS])
        elif stripped and items and raw_line.startswith((" ", "\t")):
            # Continuation of the previous wrapped bullet.
            merged = f"{items[-1]} {_clean_inline(stripped)}"
            items[-1] = merged[:MAX_ITEM_CHARS]
    flush()
    return sections


def section_for_version(sections: list[ReleaseSection], version: str) -> ReleaseSection | None:
    for section in sections:
        if section.version == version:
            return section
    return None


def load_release_section(candidates: tuple[Path, ...], version: str) -> ReleaseSection | None:
    """Read the bundled changelog and return the entry for one version."""
    sections = parse_changelog(read_changelog_text(candidates))
    return section_for_version(sections, version)


def sanitize_remote_notes(notes: str, limit: int = MAX_REMOTE_CHARS) -> list[str]:
    """Reduce a GitHub release body to safe, plain, bounded text lines.

    Markdown emphasis, links, inline code and any HTML are removed.  Nothing is
    rendered as markup and no URL is ever followed automatically.
    """
    lines: list[str] = []
    for raw_line in str(notes or "")[:limit].splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        bullet = stripped.startswith(("- ", "* ", "+ "))
        if bullet:
            stripped = stripped[2:]
        stripped = stripped.lstrip("#").strip()
        cleaned = _clean_inline(stripped).replace("**", "").replace("__", "")
        if not cleaned:
            continue
        lines.append(f"• {cleaned}" if bullet else cleaned)
        if len(lines) >= MAX_SECTION_ITEMS:
            break
    return lines
