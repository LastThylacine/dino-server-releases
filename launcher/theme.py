"""Shared visual theme and local Lucide icon handling for the launcher.

The palette is a dark jungle/obsidian base with deep green surfaces, a vivid
green reserved for the ready state, and amber/gold used for attention and
pending work.  Red is only ever used for a real failure.
"""

from __future__ import annotations

from pathlib import Path

import customtkinter as ctk
from PIL import Image


COLORS = {
    # Base canvas
    "window": "#0A130F",
    "window_alt": "#0E1913",
    "sidebar": "#0B1611",
    "sidebar_alt": "#101E17",
    # Elevated surfaces
    "surface": "#122019",
    "surface_2": "#182C23",
    "surface_3": "#20392D",
    "surface_hover": "#2A4A3A",
    "surface_glass": "#16261E",
    # Lines
    "border": "#2E5341",
    "border_soft": "#1E3A2D",
    "border_strong": "#40765C",
    # Type
    "text": "#EBF8F0",
    "text_soft": "#CDE5D8",
    "muted": "#9CBCA9",
    "faint": "#6E8F7D",
    # Ready / success
    "green": "#3DDC84",
    "green_bright": "#7CF7B4",
    "green_active": "#2CBB6C",
    "green_soft": "#0F2E1E",
    "green_glow": "#1B5636",
    "green_deep": "#ECFFF4",
    # Primary action
    "primary": "#178A4E",
    "primary_hover": "#12703F",
    "primary_bright": "#1FA75F",
    "on_primary": "#F2FFF7",
    # Failure
    "red": "#FF7B82",
    "red_hover": "#D8555D",
    "red_soft": "#3A1F22",
    "red_glow": "#5A2A2F",
    # Attention / limited-time
    "amber": "#F0C063",
    "amber_bright": "#FFD98A",
    "amber_soft": "#3A2F18",
    "amber_glow": "#5A4720",
    # Platform accents (iOS / Android must stay visually distinct)
    "ios": "#8FD3FF",
    "ios_soft": "#14283A",
    "android": "#9FE870",
    "android_soft": "#1B3218",
    # Misc
    "console": "#0B1611",
    "disabled": "#1B2822",
    "disabled_text": "#5E7A6B",
    "shadow": "#050B08",
}

SPACING = {
    "xs": 4,
    "sm": 8,
    "md": 12,
    "lg": 16,
    "xl": 22,
    "xxl": 30,
    "page": 18,
}

SIZES = {
    "sidebar": 196,
    "topbar": 72,
    "button": 38,
    "button_small": 32,
    "nav": 42,
    "radius": 14,
    "radius_small": 10,
    "radius_pill": 999,
    "content_max": 1120,
    "hero_glow": 78,
}

# Animation timings in milliseconds.  Every animation must be skipped when the
# reduced_motion setting is enabled.
MOTION = {
    "frame_ms": 16,
    "fast": 130,
    "normal": 210,
    "slow": 340,
    "pulse": 1500,
}

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Cascadia Mono"


def font(size: int, weight: str = "normal", family: str = FONT_FAMILY) -> tuple:
    return family, size, weight


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    red, green, blue = (max(0, min(255, int(round(channel)))) for channel in rgb)
    return f"#{red:02X}{green:02X}{blue:02X}"


def mix(start: str, end: str, position: float) -> str:
    """Blend two theme colours.  position 0 returns start, 1 returns end."""
    position = max(0.0, min(1.0, position))
    first = hex_to_rgb(start)
    second = hex_to_rgb(end)
    return rgb_to_hex(
        tuple(first[index] + (second[index] - first[index]) * position for index in range(3))
    )


def lighten(value: str, amount: float) -> str:
    return mix(value, "#FFFFFF", amount)


def darken(value: str, amount: float) -> str:
    return mix(value, "#000000", amount)


def ease_out_cubic(position: float) -> float:
    position = max(0.0, min(1.0, position))
    return 1 - (1 - position) ** 3


def ease_in_out(position: float) -> float:
    position = max(0.0, min(1.0, position))
    if position < 0.5:
        return 4 * position ** 3
    return 1 - (-2 * position + 2) ** 3 / 2


class IconLibrary:
    """Tint monochrome PNG masks from the bundled Lucide set using theme colours."""

    def __init__(self, asset_root: Path):
        self.asset_root = asset_root
        self._source_cache: dict[str, Image.Image] = {}
        self._cache: dict[tuple[str, int, str], ctk.CTkImage] = {}

    def get(self, name: str, size: int = 18, color: str | None = None) -> ctk.CTkImage:
        tint = color or COLORS["muted"]
        key = (name, size, tint)
        if key not in self._cache:
            source = self._source_cache.get(name)
            if source is None:
                source = Image.open(self.asset_root / "assets" / "icons" / "png" / f"{name}.png").convert("RGBA")
                self._source_cache[name] = source
            red, green, blue = hex_to_rgb(tint)
            tinted = Image.new("RGBA", source.size, (red, green, blue, 0))
            # ReportLab emits an opaque black canvas with white strokes. Its
            # luminance is therefore the correct anti-aliased icon mask.
            tinted.putalpha(source.convert("L"))
            self._cache[key] = ctk.CTkImage(
                light_image=tinted,
                dark_image=tinted,
                size=(size, size),
            )
        return self._cache[key]
