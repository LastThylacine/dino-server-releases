"""Reusable presentation widgets for the Dino Server launcher.

Everything in this module is purely visual.  No widget here reads settings,
touches the server, or performs any file access, so the redesign cannot change
runtime behaviour.  All animations run through :class:`Animator`, which becomes
a no-op when the user enables reduced motion.
"""

from __future__ import annotations

import math
import time
import tkinter as tk

import customtkinter as ctk

from .theme import (
    COLORS,
    MONO_FAMILY,
    MOTION,
    SIZES,
    ease_in_out,
    ease_out_cubic,
    lighten,
    mix,
)


class Animator:
    """Frame-driven animation helper bound to one Tk widget."""

    def __init__(self, widget: tk.Misc, enabled: bool = True):
        self.widget = widget
        self.enabled = enabled
        self._jobs: dict[str, str] = {}

    def _cancel_job(self, key: str) -> None:
        job = self._jobs.pop(key, None)
        if job is None:
            return
        try:
            self.widget.after_cancel(job)
        except (tk.TclError, ValueError):
            pass

    def cancel(self, key: str) -> None:
        self._cancel_job(key)

    def cancel_all(self) -> None:
        for key in list(self._jobs):
            self._cancel_job(key)

    def animate(self, key, duration_ms, step, done=None, easing=ease_out_cubic) -> None:
        """Call ``step(progress)`` until progress reaches 1.0."""
        self._cancel_job(key)
        if not self.enabled or duration_ms <= 0:
            step(1.0)
            if done:
                done()
            return
        started = time.monotonic()

        def tick():
            elapsed = (time.monotonic() - started) * 1000.0
            raw = min(1.0, elapsed / duration_ms)
            try:
                step(easing(raw))
            except tk.TclError:
                self._jobs.pop(key, None)
                return
            if raw >= 1.0:
                self._jobs.pop(key, None)
                if done:
                    done()
                return
            try:
                self._jobs[key] = self.widget.after(MOTION["frame_ms"], tick)
            except tk.TclError:
                self._jobs.pop(key, None)

        tick()

    def loop(self, key, period_ms, step) -> None:
        """Run ``step(phase)`` forever with phase cycling through 0..1."""
        self._cancel_job(key)
        if not self.enabled:
            step(0.0)
            return
        started = time.monotonic()

        def tick():
            phase = ((time.monotonic() - started) * 1000.0 % period_ms) / period_ms
            try:
                step(phase)
                self._jobs[key] = self.widget.after(MOTION["frame_ms"] * 2, tick)
            except tk.TclError:
                self._jobs.pop(key, None)

        tick()


class GradientFrame(tk.Canvas):
    """Rounded gradient panel that can also host normal child widgets."""

    def __init__(
        self,
        master,
        colors: tuple[str, ...] = (COLORS["surface_2"], COLORS["surface"]),
        corner_radius: int = SIZES["radius"],
        border_color: str | None = COLORS["border_soft"],
        highlight: str | None = None,
        orient: str = "vertical",
        **kwargs,
    ):
        background = kwargs.pop("background", COLORS["window"])
        super().__init__(
            master,
            highlightthickness=0,
            bd=0,
            background=background,
            **kwargs,
        )
        self.gradient_colors = tuple(colors)
        self.corner_radius = corner_radius
        self.border_color = border_color
        self.highlight = highlight
        self.orient = orient
        self._last_size = (0, 0)
        self.bind("<Configure>", self._on_configure, add="+")

    def set_colors(self, colors: tuple[str, ...], highlight: str | None = None) -> None:
        self.gradient_colors = tuple(colors)
        self.highlight = highlight
        self._last_size = (0, 0)
        self._redraw()

    def _on_configure(self, event=None) -> None:
        size = (self.winfo_width(), self.winfo_height())
        if size == self._last_size:
            return
        self._last_size = size
        self._redraw()

    def _sample(self, position: float) -> str:
        stops = self.gradient_colors
        if len(stops) == 1:
            return stops[0]
        scaled = position * (len(stops) - 1)
        index = min(len(stops) - 2, int(scaled))
        return mix(stops[index], stops[index + 1], scaled - index)

    def _inset(self, distance: int, span: int) -> float:
        radius = min(self.corner_radius, span // 2)
        if radius <= 0:
            return 0.0
        if distance < radius:
            offset = radius - distance
        elif distance > span - radius - 1:
            offset = distance - (span - radius - 1)
        else:
            return 0.0
        offset = min(offset, radius)
        return radius - math.sqrt(max(0.0, radius * radius - offset * offset))

    def _redraw(self) -> None:
        self.delete("gradient")
        width = self.winfo_width()
        height = self.winfo_height()
        if width < 4 or height < 4:
            return
        if self.orient == "horizontal":
            for x in range(width):
                inset = self._inset(x, width)
                color = self._sample(x / max(1, width - 1))
                self.create_line(
                    x, inset, x + 1, height - inset,
                    fill=color, tags="gradient",
                )
        else:
            for y in range(height):
                inset = self._inset(y, height)
                color = self._sample(y / max(1, height - 1))
                self.create_line(
                    inset, y, width - inset, y + 1,
                    fill=color, tags="gradient",
                )
        if self.highlight:
            radius = min(self.corner_radius, height // 2)
            self.create_line(
                radius, 1, width - radius, 1,
                fill=self.highlight, tags="gradient",
            )
        if self.border_color:
            radius = min(self.corner_radius, min(width, height) // 2)
            self.create_arc(
                1, 1, 2 * radius, 2 * radius, start=90, extent=90,
                style="arc", outline=self.border_color, tags="gradient",
            )
            self.create_arc(
                width - 2 * radius - 2, 1, width - 2, 2 * radius, start=0, extent=90,
                style="arc", outline=self.border_color, tags="gradient",
            )
            self.create_arc(
                1, height - 2 * radius - 2, 2 * radius, height - 2,
                start=180, extent=90, style="arc",
                outline=self.border_color, tags="gradient",
            )
            self.create_arc(
                width - 2 * radius - 2, height - 2 * radius - 2, width - 2, height - 2,
                start=270, extent=90, style="arc",
                outline=self.border_color, tags="gradient",
            )
            self.create_line(
                radius, 1, width - radius, 1,
                fill=self.border_color, tags="gradient",
            )
            self.create_line(
                radius, height - 1, width - radius, height - 1,
                fill=self.border_color, tags="gradient",
            )
            self.create_line(
                1, radius, 1, height - radius,
                fill=self.border_color, tags="gradient",
            )
            self.create_line(
                width - 1, radius, width - 1, height - radius,
                fill=self.border_color, tags="gradient",
            )
        self.tag_lower("gradient")


class GradientBanner(GradientFrame):
    """Hero panel whose text is drawn directly on the gradient.

    Tk cannot make a child widget transparent, so any packed label over a
    gradient would show as a solid rectangle.  Drawing the copy as canvas items
    keeps the gradient unbroken.
    """

    def __init__(self, master, animator: Animator | None = None, **kwargs):
        super().__init__(master, **kwargs)
        self._animator = animator or Animator(self)
        self._eyebrow = ""
        self._title = ""
        self._detail = ""
        self._badge = ""
        self._accent = COLORS["green"]
        self._dot_color = COLORS["green"]
        self._pulsing = False

    def set_state(self, eyebrow: str, title: str, detail: str, badge: str,
                  accent: str, colors: tuple[str, ...], pulsing: bool = False) -> None:
        self._eyebrow = eyebrow
        self._title = title
        self._detail = detail
        self._badge = badge
        self._accent = accent
        self._dot_color = accent
        self._pulsing = pulsing
        self.gradient_colors = tuple(colors)
        self.highlight = mix(colors[0], accent, 0.35)
        self._last_size = (0, 0)
        self._redraw()

    def _redraw(self) -> None:
        super()._redraw()
        self.delete("banner")
        self._animator.cancel("banner_pulse")
        width = self.winfo_width()
        height = self.winfo_height()
        if width < 40 or height < 40:
            return
        left = 26
        base = self._sample(0.35)

        dot = self.create_oval(
            left, 25, left + 9, 34, outline="", fill=self._accent, tags="banner",
        )
        self.create_text(
            left + 17, 29, text=self._eyebrow, anchor="w", fill=self._accent,
            font=("Segoe UI", 9, "bold"), tags="banner",
        )
        self._title_item = self.create_text(
            left, 45, text=self._title, anchor="nw", fill=COLORS["text"],
            font=("Segoe UI", 22, "bold"), tags="banner",
        )
        title_bounds = self.bbox(self._title_item)
        detail_y = max(91, (title_bounds[3] if title_bounds else 82) + 7)
        self._detail_item = self.create_text(
            left, detail_y, text=self._detail, anchor="nw", fill=COLORS["muted"],
            font=("Segoe UI", 9), width=max(220, width - 300), tags="banner",
        )

        if self._badge:
            padding = 13
            text_id = self.create_text(
                0, 0, text=self._badge, anchor="w", fill=self._accent,
                font=("Segoe UI", 10, "bold"), tags="banner",
            )
            bounds = self.bbox(text_id)
            text_width = bounds[2] - bounds[0]
            badge_width = text_width + padding * 2
            x1 = width - 26 - badge_width
            y1 = 25
            self._rounded_rect(
                x1, y1, x1 + badge_width, y1 + 28, 13,
                fill=mix(base, self._accent, 0.16), outline=mix(base, self._accent, 0.45),
            )
            self.coords(text_id, x1 + padding, y1 + 14)
            self.tag_raise(text_id)

        if self._pulsing:
            def step(phase: float) -> None:
                strength = (math.sin(phase * math.tau) + 1) / 2
                self.itemconfigure(
                    dot, fill=mix(base, self._accent, 0.35 + 0.65 * strength)
                )

            self._animator.loop("banner_pulse", MOTION["pulse"], step)

    def _rounded_rect(self, x1, y1, x2, y2, radius, **options):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return self.create_polygon(
            points, smooth=True, splinesteps=18, tags="banner", **options
        )


class Card(ctk.CTkFrame):
    """Standard elevated surface used for every panel in the redesign."""

    def __init__(self, master, tone: str = "surface", accent: str | None = None, **kwargs):
        kwargs.pop("bg", None)
        kwargs.pop("highlightbackground", None)
        kwargs.pop("highlightthickness", None)
        self.tone = tone
        self.accent = accent
        super().__init__(
            master,
            fg_color=COLORS[tone],
            corner_radius=kwargs.pop("corner_radius", SIZES["radius"]),
            border_width=kwargs.pop("border_width", 1),
            border_color=kwargs.pop("border_color", COLORS["border_soft"]),
            **kwargs,
        )
        self._accent_strip = None
        if accent:
            self._accent_strip = ctk.CTkFrame(
                self, fg_color=accent, height=3, corner_radius=2,
            )
            self._accent_strip.place(relx=0.5, y=0, anchor="n", relwidth=0.94)

    def set_accent(self, color: str | None) -> None:
        self.accent = color
        if self._accent_strip is None:
            return
        if color:
            self._accent_strip.configure(fg_color=color)
            self._accent_strip.place(relx=0.5, y=0, anchor="n", relwidth=0.94)
        else:
            self._accent_strip.place_forget()


class PulseDot(tk.Canvas):
    """Small status dot with an optional breathing halo."""

    def __init__(self, master, color: str = COLORS["green"], size: int = 16,
                 background: str = COLORS["surface"], animator: Animator | None = None):
        super().__init__(
            master, width=size, height=size, highlightthickness=0, bd=0,
            background=background,
        )
        self._size = size
        self._background = background
        self._color = color
        self._animator = animator or Animator(self)
        self._pulsing = False
        self._halo = self.create_oval(0, 0, size, size, outline="", fill=background)
        margin = size * 0.28
        self._core = self.create_oval(
            margin, margin, size - margin, size - margin, outline="", fill=color,
        )

    def configure_background(self, background: str) -> None:
        self._background = background
        self.configure(background=background)
        self.itemconfigure(self._halo, fill=background)

    def set_state(self, color: str, pulsing: bool = False) -> None:
        self._color = color
        self.itemconfigure(self._core, fill=color)
        if pulsing == self._pulsing:
            if not pulsing:
                self.itemconfigure(self._halo, fill=self._background)
            return
        self._pulsing = pulsing
        if not pulsing:
            self._animator.cancel("pulse")
            self.itemconfigure(self._halo, fill=self._background)
            return

        def step(phase: float) -> None:
            strength = (math.sin(phase * math.tau) + 1) / 2
            self.itemconfigure(
                self._halo,
                fill=mix(self._background, self._color, 0.12 + 0.30 * strength),
            )

        self._animator.loop("pulse", MOTION["pulse"], step)

    def stop(self) -> None:
        self._animator.cancel("pulse")


class StatusPill(ctk.CTkFrame):
    """Compact rounded status chip with a coloured dot."""

    def __init__(self, master, text: str = "", color: str = COLORS["muted"],
                 background: str = COLORS["surface_2"], animator: Animator | None = None,
                 font_size: int = 11, dot_size: int = 14, **kwargs):
        super().__init__(
            master,
            fg_color=background,
            corner_radius=kwargs.pop("corner_radius", SIZES["radius_small"]),
            border_width=1,
            border_color=COLORS["border_soft"],
            **kwargs,
        )
        self.dot = PulseDot(self, color, dot_size, background, animator)
        self.dot.pack(side="left", padx=(10, 6), pady=6)
        self.label = ctk.CTkLabel(
            self, text=text, text_color=color,
            font=ctk.CTkFont("Segoe UI", font_size, "bold"),
        )
        self.label.pack(side="left", padx=(0, 12))

    def set_state(self, text: str, color: str, background: str,
                  border: str | None = None, pulsing: bool = False) -> None:
        self.configure(fg_color=background, border_color=border or COLORS["border_soft"])
        self.dot.configure_background(background)
        self.dot.set_state(color, pulsing)
        self.label.configure(text=text, text_color=color)


class MetricTile(ctk.CTkFrame):
    """Label/value tile used by the dashboard metric strip."""

    def __init__(self, master, label: str, variable: tk.StringVar,
                 icon=None, accent: str = COLORS["green"], **kwargs):
        super().__init__(
            master,
            fg_color=COLORS["surface_2"],
            corner_radius=SIZES["radius_small"],
            border_width=1,
            border_color=COLORS["border_soft"],
            **kwargs,
        )
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=13, pady=(11, 0))
        if icon is not None:
            ctk.CTkLabel(head, text="", image=icon, width=15).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(
            head, text=label, text_color=COLORS["faint"],
            font=ctk.CTkFont("Segoe UI", 9, "bold"), anchor="w",
        ).pack(side="left")
        self.value_label = ctk.CTkLabel(
            self, textvariable=variable, text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 14, "bold"), anchor="w",
        )
        self.value_label.pack(fill="x", padx=13, pady=(2, 11))
        self._accent = accent

    def set_accent(self, color: str) -> None:
        self._accent = color
        self.value_label.configure(text_color=color)


class Spinner(tk.Canvas):
    """Rotating arc used while a background operation runs."""

    def __init__(self, master, size: int = 18, color: str = COLORS["amber"],
                 background: str = COLORS["surface"], animator: Animator | None = None):
        super().__init__(
            master, width=size, height=size, highlightthickness=0, bd=0,
            background=background,
        )
        self._size = size
        self._color = color
        self._animator = animator or Animator(self)
        self._arc = self.create_arc(
            2, 2, size - 2, size - 2, start=0, extent=110,
            style="arc", outline=color, width=2,
        )
        self._running = False
        self.stop()

    def start(self, color: str | None = None) -> None:
        if color:
            self._color = color
            self.itemconfigure(self._arc, outline=color)
        if self._running:
            return
        self._running = True
        self.itemconfigure(self._arc, state="normal")

        def step(phase: float) -> None:
            self.itemconfigure(self._arc, start=-phase * 360)

        self._animator.loop("spin", 1100, step)

    def stop(self) -> None:
        self._running = False
        self._animator.cancel("spin")
        self.itemconfigure(self._arc, state="hidden")


class ProgressTrack(ctk.CTkFrame):
    """Thin determinate progress bar with an animated fill."""

    def __init__(self, master, height: int = 5, color: str = COLORS["green"],
                 track: str = COLORS["surface_3"], animator: Animator | None = None):
        super().__init__(master, fg_color=track, corner_radius=height, height=height)
        self.pack_propagate(False)
        self._animator = animator or Animator(self)
        self._value = 0.0
        self.fill = ctk.CTkFrame(self, fg_color=color, corner_radius=height)
        self.fill.place(relx=0, rely=0, relwidth=0.0, relheight=1.0)

    def set_value(self, value: float, color: str | None = None) -> None:
        value = max(0.0, min(1.0, value))
        if color:
            self.fill.configure(fg_color=color)
        start = self._value
        self._value = value

        def step(progress: float) -> None:
            self.fill.place_configure(relwidth=start + (value - start) * progress)

        self._animator.animate("fill", MOTION["normal"], step, easing=ease_in_out)


class Skeleton(tk.Canvas):
    """Shimmering placeholder shown while a list is still loading."""

    def __init__(self, master, width: int = 220, height: int = 12,
                 background: str = COLORS["surface"], animator: Animator | None = None):
        super().__init__(
            master, width=width, height=height, highlightthickness=0, bd=0,
            background=background,
        )
        self._width = width
        self._background = background
        self._bar = self.create_rectangle(
            0, 0, width, height, outline="", fill=COLORS["surface_3"],
        )
        self._animator = animator or Animator(self)

        def step(phase: float) -> None:
            strength = (math.sin(phase * math.tau) + 1) / 2
            self.itemconfigure(
                self._bar,
                fill=mix(COLORS["surface_2"], COLORS["surface_hover"], strength),
            )

        self._animator.loop("shimmer", 1400, step)

    def stop(self) -> None:
        self._animator.cancel("shimmer")


class SlidingIndicator(tk.Frame):
    """Selection marker that glides between navigation rows."""

    def __init__(self, master, width: int = 3, color: str = COLORS["green"],
                 animator: Animator | None = None):
        super().__init__(master, bg=color, width=width, highlightthickness=0, bd=0)
        self._animator = animator or Animator(self)
        self._target = None
        self._current_y = 0
        self._height = 0

    def move_to(self, y: int, height: int, animate: bool = True) -> None:
        start_y = self._current_y
        start_height = self._height or height
        self._target = (y, height)

        def step(progress: float) -> None:
            current = start_y + (y - start_y) * progress
            current_height = start_height + (height - start_height) * progress
            self.place_configure(x=0, y=int(current), height=max(1, int(current_height)))

        self._current_y = y
        self._height = height
        if animate:
            self._animator.animate("slide", MOTION["normal"], step, easing=ease_out_cubic)
        else:
            step(1.0)


class SectionHeading(tk.Frame):
    """Uppercase section label followed by a hairline rule."""

    def __init__(self, master, text: str, background: str = COLORS["surface"],
                 color: str = COLORS["text"], size: int = 12):
        super().__init__(master, bg=background)
        tk.Label(
            self, text=text, bg=background, fg=color,
            font=("Segoe UI", size, "bold"),
        ).pack(side="left")
        tk.Frame(self, bg=COLORS["border_soft"], height=1).pack(
            side="left", fill="x", expand=True, padx=(14, 0)
        )


class HoverCard:
    """Attach a subtle hover lift to any CTk frame."""

    def __init__(self, card: ctk.CTkFrame, base: str, hover: str | None = None,
                 animator: Animator | None = None):
        self.card = card
        self.base = base
        self.hover = hover or lighten(base, 0.06)
        self._animator = animator or Animator(card)
        card.bind("<Enter>", lambda _event: self._to(self.hover), add="+")
        card.bind("<Leave>", lambda _event: self._to(self.base), add="+")

    def _to(self, target: str) -> None:
        start = self.card.cget("fg_color")
        if not isinstance(start, str) or not start.startswith("#"):
            self.card.configure(fg_color=target)
            return

        def step(progress: float) -> None:
            self.card.configure(fg_color=mix(start, target, progress))

        self._animator.animate("hover", MOTION["fast"], step)


def mono_font(size: int, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(MONO_FAMILY, size, weight)
