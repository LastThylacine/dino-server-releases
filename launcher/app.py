"""Modern multilingual desktop interface for the file-based private server."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import qrcode
from PIL import Image, ImageTk

from .cache_validation import CacheValidationReport, validate_cache_folder
from .core import (
    BASE_DIR,
    CONFIG_DIR,
    LOG_DIR,
    SERVER_ERROR_FILE,
    SERVER_LOG_FILE,
    STATUS_URL,
    ServerAlreadyRunningError,
    ServerController,
    SettingsStore,
    count_players,
    dns_running,
    format_uptime,
    http_responds,
    load_player_admin_state,
    manifest_matches,
    normalize_profile_key,
    resolved_manifest_host,
    run_diagnostics,
    request_save_session_release,
    server_profile,
    server_state,
    tail_text,
)
from .diagnostic_report import build_diagnostic_report, read_milestones
from .i18n import LANGUAGE_NAMES, detect_system_language, translate
from .release_notes import (
    ReleaseSection,
    load_release_section,
    sanitize_remote_notes,
)
from .startup_coordinator import StartupCoordinator, StartupIssue, StartupState
from .theme import (
    COLORS,
    MOTION,
    SIZES,
    SPACING,
    IconLibrary,
    font as _font,
)
from .widgets import (
    Animator,
    Card,
    GradientBanner,
    MetricTile,
    ProgressTrack,
    SectionHeading,
    SlidingIndicator,
    Spinner,
    StatusPill,
)
from .update_manager import (
    DownloadedUpdate,
    LATEST_RELEASE_PAGE,
    ReleaseInfo,
    UpdateError,
    check_for_update,
    download_update,
)
from .update_launch import launch_update_helper
from .version import DISPLAY_VERSION, VERSION

ctk.set_appearance_mode("dark")


def _asset_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    return root / relative


ICONS = IconLibrary(_asset_path(""))


def _cleanup_legacy_update_bridge() -> None:
    """Remove the temporary 1.0.8 one-file updater delay payload."""
    bridge = BASE_DIR / "assets" / "icons" / "update_bridge_108"
    if bridge.is_dir():
        shutil.rmtree(bridge, ignore_errors=True)


def _make_setup_qr_image(url: str) -> Image.Image:
    if not url.startswith("http://") or not url.endswith("/setup/"):
        raise ValueError("setup QR URL must be a local HTTP setup page")
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=9,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(
        fill_color="#102C1F",
        back_color="#FFFFFF",
    ).convert("RGB")


class ModernButton(ctk.CTkButton):
    def __init__(self, master, text: str, command=None, kind: str = "secondary",
                 icon_name: str | None = None, icon_size: int = 17, **kwargs):
        self.kind = kind
        self.icon_name = icon_name
        self.icon_size = icon_size
        self.selected = False
        kwargs.pop("padx", None)
        kwargs.pop("pady", None)
        width = kwargs.pop("width", max(82, len(text) * 8 + (48 if icon_name else 26)))
        background, foreground, hover = self._palette()
        super().__init__(
            master,
            text=text,
            command=command,
            width=width,
            height=kwargs.pop("height", SIZES["button"]),
            fg_color=background,
            text_color=foreground,
            hover_color=hover,
            text_color_disabled=COLORS["disabled_text"],
            font=ctk.CTkFont("Segoe UI", 13, "bold" if kind in ("primary", "accent") else "normal"),
            corner_radius=SIZES["radius_small"],
            border_width=1 if kind in ("secondary", "tab", "ghost") else 0,
            border_color=self._border(),
            image=ICONS.get(icon_name, icon_size, foreground) if icon_name else None,
            compound="left",
            **kwargs,
        )

    def _palette(self):
        if self.kind == "primary":
            return COLORS["primary"], COLORS["on_primary"], COLORS["primary_bright"]
        if self.kind == "accent":
            return COLORS["amber_soft"], COLORS["amber_bright"], COLORS["amber_glow"]
        if self.kind == "danger":
            return COLORS["red_soft"], COLORS["red"], COLORS["red_glow"]
        if self.kind == "ghost":
            return "transparent", COLORS["muted"], COLORS["surface_3"]
        if self.kind == "tab":
            if self.selected:
                return COLORS["surface_3"], COLORS["text"], COLORS["surface_hover"]
            return "transparent", COLORS["muted"], COLORS["surface_2"]
        return COLORS["surface_3"], COLORS["text"], COLORS["surface_hover"]

    def _border(self):
        if self.kind == "ghost":
            return COLORS["border_soft"]
        if self.kind == "tab" and self.selected:
            return COLORS["border_strong"]
        return COLORS["border"]

    def _apply_visual_state(self):
        state = self.cget("state")
        if state == "disabled":
            background = COLORS["disabled"]
            foreground = COLORS["disabled_text"]
            hover = background
        else:
            background, foreground, hover = self._palette()
        super().configure(
            fg_color=background,
            text_color=foreground,
            hover_color=hover,
            border_width=1 if self.kind in ("secondary", "tab", "ghost") else 0,
            border_color=COLORS["border_soft"] if state == "disabled" else self._border(),
            image=ICONS.get(self.icon_name, self.icon_size, foreground) if self.icon_name else None,
        )

    def configure(self, cnf=None, **kwargs):
        result = super().configure(cnf, **kwargs)
        if hasattr(self, "kind") and any(key in kwargs for key in ("state", "text")):
            self._apply_visual_state()
        return result

    config = configure

    def set_selected(self, selected: bool):
        self.selected = selected
        self._apply_visual_state()


class Panel(Card):
    """Established name for the standard elevated surface card."""


class ToggleSwitch(ctk.CTkSwitch):
    def __init__(self, master, variable: tk.BooleanVar, command=None):
        super().__init__(master, text="", variable=variable, command=command, width=48,
            switch_width=46, switch_height=24, corner_radius=12, border_width=2,
            fg_color=COLORS["surface_3"], progress_color=COLORS["green"],
            button_color=COLORS["text"], button_hover_color=COLORS["green_bright"])


class DinosaurServerLauncher(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.settings_store = SettingsStore()
        settings = self.settings_store.load()
        configured_language = str(settings.get("launcher_language") or "")
        self.language = configured_language if configured_language in LANGUAGE_NAMES else detect_system_language()
        self.profile_key = normalize_profile_key(settings.get("platform_profile"))
        self._manual_host_value = ""
        self.reduced_motion = bool(settings.get("reduced_motion"))
        self.show_release_notes = bool(settings.get("show_release_notes", True))
        self.animator = Animator(self, enabled=not self.reduced_motion)

        self.title(self.t("app_title"))
        self.configure(fg_color=COLORS["window"])
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        self.minsize(
            min(960, max(760, screen_width - 40)),
            min(620, max(420, screen_height - 64)),
        )
        self.geometry(self._centered_geometry(1180, 680))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_window_icon()
        self.after(20, self._style_windows_titlebar)
        loading_window = self._create_loading_window()

        self.controller = ServerController()
        self.coordinator = StartupCoordinator(self.controller, self._startup_progress)
        self._busy = False
        self._closing = False
        self._runner: subprocess.Popen | None = None
        self._last_server_running: bool | None = None
        self._health_ok = False
        self._health_check_pending = False
        self._last_health_check = 0.0
        self._poll_after_id = None
        self._last_log_text = ""
        self._last_player_refresh = 0.0
        requested_page = os.environ.get("DINOSAUR_LAUNCHER_START_PAGE", "dashboard")
        self._current_page = requested_page if requested_page in {"dashboard", "console", "diagnostics", "players", "guide", "settings", "about"} else "dashboard"
        self._settings_tab = "general"
        self._brand_image = None
        self._guide_images: dict[str, ImageTk.PhotoImage] = {}
        self._profile_value_to_key: dict[str, str] = {}
        self._players_state_signature = ""
        self._last_players_refresh = 0.0
        self._update_release: ReleaseInfo | None = None
        self._update_status = "idle"
        self._update_error = ""
        self._update_checking = False
        self._release_notes_dialog: ctk.CTkToplevel | None = None
        self._release_notes_shown = False
        self._hero_state = "stopped"
        self._hero_detail_key = "stopped_detail"
        self._hero_detail_values: dict = {}

        self._build_interface()
        loading_window.destroy()
        self.after_idle(self._reveal_main_window)
        self._schedule_state_poll(150)
        self.after(450, self._refresh_logs)
        self.after(1400, self._start_update_check)
        # The window must be fully built and stable before a modal appears.
        self.after(900, self._maybe_show_release_notes)

    def t(self, key: str, **values) -> str:
        return translate(self.language, key, **values)

    def _centered_geometry(self, width: int, height: int) -> str:
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        width = min(width, max(760, screen_width - 40))
        height = min(height, max(420, screen_height - 64))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        return f"{width}x{height}+{x}+{y}"

    def _set_window_icon(self):
        icon = _asset_path("icons/app_icon.ico")
        try:
            self.iconbitmap(default=str(icon))
        except tk.TclError:
            pass

    def _create_loading_window(self):
        """Show a complete lightweight shell while the real panels are built."""
        window = ctk.CTkToplevel(self)
        window.overrideredirect(True)
        window.configure(fg_color=COLORS["window"])
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        width, height = 460, 210
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        panel = Card(window, accent=COLORS["green"])
        panel.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(
            panel,
            text="DINO SERVER",
            text_color=COLORS["green"],
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
        ).pack(pady=(34, 8))
        ctk.CTkLabel(
            panel,
            text=self.t("loading_title"),
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 21, "bold"),
        ).pack()
        ctk.CTkLabel(
            panel,
            text=self.t("loading_detail"),
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 11),
        ).pack(pady=(7, 17))
        progress = ctk.CTkProgressBar(
            panel,
            width=320,
            height=5,
            corner_radius=4,
            fg_color=COLORS["surface_3"],
            progress_color=COLORS["green"],
        )
        progress.pack()
        progress.set(0.72)
        window.update_idletasks()
        window.update()
        return window

    def _rebuild_with_loading(self):
        loading_window = self._create_loading_window()
        try:
            self.withdraw()
            self._rebuild_interface()
        finally:
            loading_window.destroy()
        self.after_idle(self._reveal_main_window)

    def _reveal_main_window(self):
        if self._closing:
            return
        try:
            self.deiconify()
            self._stabilize_page_layout()
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _style_windows_titlebar(self):
        """Keep the native title bar visually consistent with the dark green UI."""
        if os.name != "nt":
            return
        try:
            import ctypes

            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            enabled = ctypes.c_int(1)
            # Windows 11 uses attribute 20; older Windows 10 builds use 19.
            for attribute in (20, 19):
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attribute,
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )
                if result == 0:
                    break
        except (AttributeError, OSError):
            pass

    def _build_interface(self):
        self._build_shell()
        self._build_pages()
        self.show_page(self._current_page, trigger_actions=False)

    def _rebuild_interface(self):
        page = self._current_page
        self.animator.cancel_all()
        for child in self.winfo_children():
            child.destroy()
        for name in (
            "update_banner",
            "dashboard_update_title",
            "dashboard_update_detail",
            "dashboard_update_button",
            "dashboard_update_notes_button",
            "about_update_status",
            "about_update_button",
            "about_release_notes_button",
            "hero_pill",
            "global_status",
            "topbar_spinner",
            "nav_indicator",
        ):
            if hasattr(self, name):
                delattr(self, name)
        self.title(self.t("app_title"))
        self._last_log_text = ""
        self._build_shell()
        self._build_pages()
        self.show_page(page, trigger_actions=False)
        # Complete geometry calculation while the loading window is still
        # covering the rebuild.  Waiting until the withdrawn main window is
        # revealed can briefly expose 1-pixel controls after a language switch.
        self._stabilize_page_layout()

    def _stabilize_page_layout(self):
        self.update_idletasks()
        width = max(620, min(SIZES["content_max"], self.page_host.winfo_width()))
        height = max(1, self.page_host.winfo_height())
        self.page_stack.place_configure(width=width, height=height)
        self.update_idletasks()

    def _build_shell(self):
        self.sidebar = ctk.CTkFrame(self, fg_color=COLORS["sidebar"], width=SIZES["sidebar"], corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Frame(self, bg=COLORS["border_soft"], width=1).pack(side="left", fill="y")

        brand = ctk.CTkFrame(
            self.sidebar, fg_color=COLORS["surface_2"], corner_radius=SIZES["radius"],
            border_width=1, border_color=COLORS["border_soft"], height=66,
        )
        brand.pack(fill="x", padx=11, pady=(14, 14))
        brand.pack_propagate(False)
        ctk.CTkFrame(brand, fg_color=COLORS["green"], width=3, corner_radius=2).place(
            x=0, rely=0.5, anchor="w", relheight=0.55,
        )
        icon_path = _asset_path("icons/app_icon.png")
        try:
            source = Image.open(icon_path).convert("RGBA")
            self._brand_image = ctk.CTkImage(light_image=source, dark_image=source, size=(32, 32))
            ctk.CTkLabel(brand, image=self._brand_image, text="", width=32).pack(
                side="left", padx=(13, 0),
            )
        except (OSError, tk.TclError):
            ctk.CTkLabel(brand, text="D", fg_color=COLORS["primary"], text_color=COLORS["on_primary"],
                font=ctk.CTkFont("Segoe UI", 15, "bold"), width=32, height=32,
                corner_radius=9).pack(side="left", padx=(13, 0))
        brand_copy = ctk.CTkFrame(brand, fg_color="transparent")
        brand_copy.pack(side="left", padx=(9, 0))
        ctk.CTkLabel(brand_copy, text=self.t("brand_title"), text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 11, "bold"), height=15).pack(anchor="w")
        ctk.CTkLabel(brand_copy, text=self.t("brand_subtitle").title(), text_color=COLORS["green"],
            font=ctk.CTkFont("Segoe UI", 8, "bold"), height=12).pack(anchor="w", pady=(2, 0))

        nav_groups = [
            ("nav_group_server", [
                ("dashboard", "nav_dashboard", "layout-dashboard"),
                ("console", "nav_console", "terminal"),
                ("diagnostics", "nav_diagnostics", "activity"),
                ("players", "nav_players", "database"),
            ]),
            ("nav_group_system", [
                ("guide", "nav_guide", "book-open"),
                ("settings", "nav_settings", "settings"),
                ("about", "nav_about", "info"),
            ]),
        ]
        self.nav_host = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        self.nav_host.pack(fill="x")
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.nav_rows: dict[str, tk.Frame] = {}
        self.nav_icon_names = {
            key: icon for _group, items in nav_groups for key, _label, icon in items
        }
        for group_key, items in nav_groups:
            tk.Label(
                self.nav_host, text=self.t(group_key), bg=COLORS["sidebar"],
                fg=COLORS["faint"], font=_font(8, "bold"), anchor="w",
            ).pack(fill="x", padx=(20, 10), pady=(10, 5))
            for key, label_key, icon_name in items:
                row = tk.Frame(self.nav_host, bg=COLORS["sidebar"], height=SIZES["nav"])
                row.pack(fill="x", padx=(10, 10), pady=1)
                row.pack_propagate(False)
                button = ctk.CTkButton(row, text=self.t(label_key),
                    command=lambda selected=key: self.show_page(selected), anchor="w",
                    fg_color="transparent", hover_color=COLORS["surface_2"],
                    text_color=COLORS["muted"], font=ctk.CTkFont("Segoe UI", 13),
                    image=ICONS.get(icon_name, 17, COLORS["muted"]), compound="left",
                    corner_radius=SIZES["radius_small"], height=SIZES["nav"])
                button.pack(side="left", fill="both", expand=True, padx=(6, 0))
                button.bind("<Enter>", lambda _event=None, name=key: self._set_nav_hover(name, True), add="+")
                button.bind("<Leave>", lambda _event=None, name=key: self._set_nav_hover(name, False), add="+")
                self.nav_buttons[key] = button
                self.nav_rows[key] = row
        self.nav_indicator = SlidingIndicator(
            self.nav_host, width=3, color=COLORS["green"], animator=self.animator,
        )
        self.nav_indicator.place(x=0, y=0, height=1)

        footer = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=13, pady=14)
        version_chip = ctk.CTkFrame(
            footer, fg_color=COLORS["surface_2"], corner_radius=SIZES["radius_small"],
            border_width=1, border_color=COLORS["border_soft"],
        )
        version_chip.pack(fill="x")
        ctk.CTkLabel(
            version_chip, text=DISPLAY_VERSION, text_color=COLORS["text_soft"],
            font=ctk.CTkFont("Segoe UI", 10, "bold"), anchor="w",
        ).pack(fill="x", padx=11, pady=(7, 0))
        ctk.CTkLabel(
            version_chip, text=self.t("editable_local"), text_color=COLORS["faint"],
            font=ctk.CTkFont("Segoe UI", 8), anchor="w",
        ).pack(fill="x", padx=11, pady=(1, 8))

        self.body = ctk.CTkFrame(self, fg_color=COLORS["window"], corner_radius=0)
        self.body.pack(side="left", fill="both", expand=True)
        topbar = Panel(self.body, height=SIZES["topbar"])
        topbar.pack(fill="x", padx=SPACING["page"], pady=(SPACING["page"], 0))
        topbar.pack_propagate(False)
        title_box = tk.Frame(topbar, bg=COLORS["surface"])
        title_box.pack(side="left", fill="y", padx=20)
        self.page_title = tk.Label(title_box, text="", bg=COLORS["surface"], fg=COLORS["text"], font=_font(18, "bold"))
        self.page_title.pack(anchor="w", pady=(13, 0))
        self.page_subtitle = tk.Label(title_box, text="", bg=COLORS["surface"], fg=COLORS["muted"], font=_font(9))
        self.page_subtitle.pack(anchor="w", pady=(2, 0))

        controls = tk.Frame(topbar, bg=COLORS["surface"])
        controls.pack(side="right", fill="y", padx=14)
        self.platform_badge = ctk.CTkLabel(
            controls, text=self._profile_label(), text_color=COLORS["ios"],
            fg_color=COLORS["ios_soft"], corner_radius=SIZES["radius_small"],
            font=ctk.CTkFont("Segoe UI", 11, "bold"), width=92, height=30,
        )
        self.platform_badge.pack(side="left", pady=21, padx=(0, 9))
        language_values = list(LANGUAGE_NAMES.values())
        self.language_combo = ctk.CTkOptionMenu(controls, values=language_values,
            command=self._language_selected, width=112, height=32, corner_radius=SIZES["radius_small"],
            fg_color=COLORS["surface_3"], button_color=COLORS["surface_hover"],
            button_hover_color=COLORS["border"], dropdown_fg_color=COLORS["surface_2"],
            dropdown_hover_color=COLORS["surface_hover"], text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 12), dropdown_font=ctk.CTkFont("Segoe UI", 12))
        self.language_combo.set(LANGUAGE_NAMES[self.language])
        self.language_combo.pack(side="left", pady=20, padx=(0, 9))
        self.topbar_spinner = Spinner(
            controls, size=18, color=COLORS["amber"],
            background=COLORS["surface"], animator=self.animator,
        )
        self.topbar_spinner.pack(side="left", padx=(0, 8), pady=27)
        self.global_status = StatusPill(
            controls, text=self.t("status_checking").title(), color=COLORS["amber"],
            background=COLORS["amber_soft"], animator=self.animator,
        )
        self.global_status.pack(side="left", pady=20)

        status_strip = ctk.CTkFrame(
            self.body, fg_color=COLORS["surface"], corner_radius=SIZES["radius_small"],
            border_width=1, border_color=COLORS["border_soft"], height=26,
        )
        status_strip.pack(side="bottom", fill="x", padx=SPACING["page"], pady=(0, 10))
        status_strip.pack_propagate(False)
        self.bottom_status = tk.Label(status_strip, text=self.t("ready"), bg=COLORS["surface"],
            fg=COLORS["faint"], anchor="w", font=_font(8))
        self.bottom_status.pack(side="left", fill="x", expand=True, padx=12)
        self.bottom_progress = ProgressTrack(
            status_strip, height=4, color=COLORS["green"], animator=self.animator,
        )
        self.bottom_progress.pack(side="right", padx=12, pady=11)
        self.bottom_progress.configure(width=120)

        self.page_host = ctk.CTkFrame(self.body, fg_color=COLORS["window"], corner_radius=0)
        self.page_host.pack(fill="both", expand=True, padx=SPACING["page"], pady=(12, 10))
        self.page_stack = tk.Frame(self.page_host, bg=COLORS["window"])
        self.page_stack.grid_rowconfigure(0, weight=1)
        self.page_stack.grid_columnconfigure(0, weight=1)
        self.page_stack.place(relx=0.5, y=0, anchor="n")
        self.page_host.bind("<Configure>", self._resize_page_stack, add="+")

    def _resize_page_stack(self, event):
        width = max(620, min(SIZES["content_max"], event.width))
        self.page_stack.place_configure(width=width, height=max(1, event.height))

    def _set_nav_hover(self, key: str, hovered: bool):
        if key == self._current_page:
            return
        color = COLORS["text"] if hovered else COLORS["muted"]
        self.nav_buttons[key].configure(image=ICONS.get(self.nav_icon_names[key], 17, color))

    def _build_pages(self):
        self.pages: dict[str, tk.Frame] = {}
        builders = {
            "dashboard": self._build_dashboard,
            "console": self._build_console,
            "diagnostics": self._build_diagnostics,
            "players": self._build_players,
            "guide": self._build_guide,
            "settings": self._build_settings,
            "about": self._build_about,
        }
        for key, builder in builders.items():
            page = tk.Frame(self.page_stack, bg=COLORS["window"])
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
            builder(page)

    def _build_dashboard(self, page: tk.Frame):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(4, weight=1)

        self.hero = GradientBanner(
            page,
            colors=(COLORS["surface_2"], COLORS["surface"]),
            corner_radius=SIZES["radius"],
            border_color=COLORS["border_soft"],
            background=COLORS["window"],
            animator=self.animator,
            height=128,
        )
        self.hero.grid(row=0, column=0, sticky="ew")
        self._apply_hero_state("stopped")

        control = Panel(page)
        control.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        control.grid_columnconfigure(0, weight=1)

        profile_bar = ctk.CTkFrame(control, fg_color=COLORS["surface_2"],
            corner_radius=SIZES["radius_small"], border_width=1, border_color=COLORS["border_soft"])
        profile_bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(13, 0))
        profile_copy = tk.Frame(profile_bar, bg=COLORS["surface_2"])
        profile_copy.pack(side="left", fill="both", expand=True, padx=(13, 8), pady=7)
        tk.Label(profile_copy, text=self.t("platform_profile").title(), bg=COLORS["surface_2"],
            fg=COLORS["text"], font=_font(9, "bold")).pack(anchor="w")
        self.profile_hint_label = tk.Label(profile_copy, text="", bg=COLORS["surface_2"],
            fg=COLORS["faint"], font=_font(8), anchor="w")
        self.profile_hint_label.pack(anchor="w", pady=(2, 0))
        profile_values = [self.t("profile_ios"), self.t("profile_android")]
        self._profile_value_to_key = {
            self.t("profile_ios"): "ios",
            self.t("profile_android"): "android",
        }
        self.profile_selector = ctk.CTkSegmentedButton(
            profile_bar,
            values=profile_values,
            command=self._profile_selected,
            width=292,
            height=34,
            dynamic_resizing=False,
            corner_radius=SIZES["radius_small"],
            border_width=1,
            fg_color=COLORS["surface_3"],
            selected_color=COLORS["primary"],
            selected_hover_color=COLORS["primary_bright"],
            unselected_color=COLORS["surface_3"],
            unselected_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"],
            text_color_disabled=COLORS["disabled_text"],
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        )
        self.profile_selector.pack(side="right", padx=6, pady=6)
        self._update_profile_ui()

        endpoint = ctk.CTkFrame(control, fg_color=COLORS["surface_2"],
            corner_radius=SIZES["radius_small"], border_width=1, border_color=COLORS["border_soft"])
        endpoint.grid(row=1, column=0, sticky="ew", padx=14, pady=(8, 0))
        ctk.CTkLabel(endpoint, text=self.t("client_address").title(), text_color=COLORS["faint"],
            font=ctk.CTkFont("Segoe UI", 9, "bold"), image=ICONS.get("network", 15, COLORS["faint"]),
            compound="left").pack(side="left", padx=(13, 9), pady=8)
        self.endpoint_value = ctk.CTkLabel(endpoint, text="—", text_color=COLORS["green"],
            font=ctk.CTkFont("Cascadia Mono", 13, "bold"), anchor="w")
        self.endpoint_value.pack(side="left", fill="x", expand=True, pady=8)
        self.endpoint_health = ctk.CTkLabel(endpoint, text=self.t("health_not_checked"),
            image=ICONS.get("server", 13, COLORS["muted"]), compound="left",
            text_color=COLORS["muted"], font=ctk.CTkFont("Segoe UI", 9))
        self.endpoint_health.pack(side="left", padx=8)
        self.copy_address_button = ModernButton(endpoint, self.t("copy"), self._copy_endpoint,
            "ghost", icon_name="copy", width=74, height=30)
        self.copy_address_button.pack(side="right", padx=(0, 5), pady=4)

        button_row = tk.Frame(control, bg=COLORS["surface"])
        button_row.grid(row=2, column=0, sticky="w", padx=14, pady=(10, 13))
        self.start_button = ModernButton(button_row, self.t("start_server"), self.start_server,
            "primary", icon_name="play", width=166)
        self.start_button.pack(side="left")
        self.stop_button = ModernButton(button_row, self.t("stop_server"), self.stop_server,
            "danger", icon_name="square", width=116)
        self.stop_button.pack(side="left", padx=(7, 0))
        self.restart_button = ModernButton(button_row, self.t("restart_server"), self.restart_server,
            "secondary", icon_name="rotate-cw", width=158)
        self.restart_button.pack(side="left", padx=(7, 0))

        metrics = tk.Frame(page, bg=COLORS["window"])
        metrics.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        for column in range(4):
            metrics.grid_columnconfigure(column, weight=1, uniform="metric")
        self.metric_vars: dict[str, tk.StringVar] = {}
        self.metric_tiles: dict[str, MetricTile] = {}
        items = [
            ("lan", "metric_lan", "network", COLORS["green"]),
            ("pid", "metric_process", "server", COLORS["muted"]),
            ("uptime", "metric_uptime", "activity", COLORS["muted"]),
            ("players", "metric_saves", "database", COLORS["muted"]),
        ]
        for column, (key, label_key, icon_name, accent) in enumerate(items):
            variable = tk.StringVar(value="—")
            self.metric_vars[key] = variable
            tile = MetricTile(
                metrics,
                self.t(label_key).title(),
                variable,
                icon=ICONS.get(icon_name, 14, COLORS["faint"]),
                accent=accent,
            )
            tile.grid(
                row=0, column=column, sticky="ew",
                padx=(0 if column == 0 else 5, 0 if column == 3 else 5),
            )
            self.metric_tiles[key] = tile

        self.update_banner = Panel(page, accent=COLORS["amber"])
        self.update_banner.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        self.update_banner.grid_columnconfigure(0, weight=1)
        update_copy = tk.Frame(self.update_banner, bg=COLORS["surface"])
        update_copy.grid(row=0, column=0, sticky="ew", padx=16, pady=(13, 11))
        self.dashboard_update_title = tk.Label(
            update_copy,
            text="",
            bg=COLORS["surface"],
            fg=COLORS["amber_bright"],
            font=_font(11, "bold"),
        )
        self.dashboard_update_title.pack(anchor="w")
        self.dashboard_update_detail = tk.Label(
            update_copy,
            text="",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=_font(9),
            anchor="w",
            justify="left",
        )
        self.dashboard_update_detail.pack(anchor="w", pady=(2, 0))
        update_actions = tk.Frame(self.update_banner, bg=COLORS["surface"])
        update_actions.grid(row=0, column=1, padx=12, pady=10)
        self.dashboard_update_notes_button = ModernButton(
            update_actions,
            self.t("update_view_changes"),
            self._show_remote_release_notes,
            "ghost",
            icon_name="file-text",
            width=148,
        )
        self.dashboard_update_notes_button.pack(side="left", padx=(0, 7))
        self.dashboard_update_button = ModernButton(
            update_actions,
            self.t("update_and_restart"),
            self._update_and_restart,
            "primary",
            icon_name="refresh-cw",
            width=178,
        )
        self.dashboard_update_button.pack(side="left")
        self.update_banner.grid_remove()

        lower = tk.Frame(page, bg=COLORS["window"])
        lower.grid(row=4, column=0, sticky="nsew", pady=(9, 0))
        lower.grid_columnconfigure(0, weight=3)
        lower.grid_columnconfigure(1, weight=2)
        lower.grid_rowconfigure(0, weight=1)
        actions = Panel(lower)
        actions.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        services_head = tk.Frame(actions, bg=COLORS["surface"])
        services_head.pack(fill="x", padx=16, pady=(13, 2))
        tk.Label(services_head, text=self.t("dashboard_services_title"), bg=COLORS["surface"],
            fg=COLORS["text"], font=_font(11, "bold")).pack(anchor="w")
        tk.Label(services_head, text=self.t("dashboard_services_hint"), bg=COLORS["surface"],
            fg=COLORS["faint"], font=_font(8), anchor="w", justify="left").pack(
            fill="x", anchor="w", pady=(2, 0))
        button_area = tk.Frame(actions, bg=COLORS["surface"])
        button_area.pack(fill="x", padx=13, pady=(9, 10))
        button_area.grid_columnconfigure(1, weight=1)
        self.connection_labels: dict[str, ctk.CTkLabel] = {}
        connection_items = [
            ("dns", "connection_dns"),
            ("http", "connection_http"),
            ("game", "connection_game"),
            ("device", "connection_device"),
        ]
        for row, (key, label_key) in enumerate(connection_items):
            ctk.CTkLabel(
                button_area,
                text=self.t(label_key),
                text_color=COLORS["muted"],
                font=ctk.CTkFont("Segoe UI", 10),
                anchor="w",
            ).grid(row=row, column=0, sticky="w", padx=3, pady=2)
            value = ctk.CTkLabel(
                button_area,
                text=self.t("connection_off"),
                image=ICONS.get("circle-x", 14, COLORS["faint"]),
                compound="left",
                text_color=COLORS["faint"],
                font=ctk.CTkFont("Segoe UI", 10, "bold"),
                anchor="e",
            )
            value.grid(row=row, column=1, sticky="e", padx=3, pady=2)
            self.connection_labels[key] = value
        connection_buttons = tk.Frame(actions, bg=COLORS["surface"])
        connection_buttons.pack(fill="x", padx=13, pady=(0, 12))
        ModernButton(
            connection_buttons,
            self.t("open_setup_guide"),
            lambda: self.show_page("guide"),
            "primary",
            icon_name="book-open",
            width=160,
        ).pack(side="left")
        ModernButton(
            connection_buttons,
            self.t("dashboard_setup_qr"),
            self._show_setup_qr,
            "ghost",
            icon_name="network",
            width=182,
        ).pack(side="left", padx=(7, 0))

        activity = Panel(lower)
        activity.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        activity_head = tk.Frame(activity, bg=COLORS["surface"])
        activity_head.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(activity_head, text=self.t("dashboard_activity_title"), bg=COLORS["surface"],
            fg=COLORS["text"], font=_font(11, "bold")).pack(side="left")
        tk.Label(activity_head, text=self.t("last_action"), bg=COLORS["surface"],
            fg=COLORS["faint"], font=_font(8)).pack(side="right")
        self.activity_title = tk.Label(activity, text=self.t("launcher_ready"), bg=COLORS["surface"],
            fg=COLORS["green"], font=_font(10, "bold"), wraplength=330, justify="left")
        self.activity_title.pack(anchor="w", padx=16)
        self.activity_detail = tk.Label(activity, text=self.t("launcher_ready_detail"), bg=COLORS["surface"],
            fg=COLORS["muted"], font=_font(9), wraplength=330, justify="left")
        self.activity_detail.pack(anchor="w", padx=16, pady=(6, 14))
        activity.bind(
            "<Configure>",
            lambda event: [
                self.activity_title.configure(wraplength=max(180, event.width - 34)),
                self.activity_detail.configure(wraplength=max(180, event.width - 34)),
            ],
            add="+",
        )
        self._refresh_update_ui()

    def _apply_hero_state(self, state: str, detail_key: str | None = None, **detail_values):
        """Repaint the hero banner for stopped / starting / running."""
        if detail_key:
            self._hero_detail_key = detail_key
            self._hero_detail_values = detail_values
        if not hasattr(self, "hero"):
            self._hero_state = state
            return
        if state == "running":
            accent = COLORS["green"]
            colors = (COLORS["green_soft"], COLORS["surface"], COLORS["surface_2"])
            title = self.t("server_running")
            badge = self.t("hero_ready_badge")
            pulsing = True
        elif state == "starting":
            accent = COLORS["amber"]
            colors = (COLORS["amber_soft"], COLORS["surface"], COLORS["surface_2"])
            title = self.t("server_starting")
            badge = self.t("hero_busy_badge")
            pulsing = True
        else:
            accent = COLORS["faint"]
            colors = (COLORS["surface_2"], COLORS["surface"], COLORS["surface"])
            title = self.t("server_stopped")
            badge = self.t("hero_offline_badge")
            pulsing = False
        self._hero_state = state
        self.hero.set_state(
            eyebrow=self.t("server_label").upper(),
            title=title,
            detail=self.t(self._hero_detail_key, **self._hero_detail_values),
            badge=badge,
            accent=accent,
            colors=colors,
            pulsing=pulsing and not self.reduced_motion,
        )

    def _build_console(self, page: tk.Frame):
        toolbar = Panel(page)
        toolbar.pack(fill="x", pady=(0, 9))
        copy = tk.Frame(toolbar, bg=COLORS["surface"])
        copy.pack(side="left", fill="both", expand=True, padx=18, pady=12)
        tk.Label(copy, text=self.t("page_console_title"), bg=COLORS["surface"],
            fg=COLORS["text"], font=_font(12, "bold")).pack(anchor="w")
        tk.Label(copy, text=self.t("console_hint"), bg=COLORS["surface"],
            fg=COLORS["faint"], font=_font(8), anchor="w", justify="left").pack(
            fill="x", anchor="w", pady=(3, 0))
        buttons = tk.Frame(toolbar, bg=COLORS["surface"])
        buttons.pack(side="right", padx=13, pady=11)
        ModernButton(buttons, self.t("refresh"), self._refresh_logs_now, "secondary",
            icon_name="refresh-cw").pack(side="left")
        ModernButton(buttons, self.t("copy"), self._copy_logs, "ghost",
            icon_name="copy").pack(side="left", padx=(7, 0))
        ModernButton(buttons, self.t("logs_folder"), lambda: self._open_path(LOG_DIR), "ghost",
            icon_name="folder-open").pack(side="left", padx=(7, 0))

        console = Panel(page, tone="console")
        console.pack(fill="both", expand=True)
        console_head = tk.Frame(console, bg=COLORS["console"])
        console_head.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(console_head, text=self.t("auto_refresh"), bg=COLORS["console"],
            fg=COLORS["faint"], font=_font(8)).pack(side="right")
        self.console_text = ctk.CTkTextbox(console, fg_color=COLORS["console"], text_color=COLORS["text_soft"],
            scrollbar_button_color=COLORS["surface_3"], scrollbar_button_hover_color=COLORS["surface_hover"],
            corner_radius=9, wrap="none", height=10, font=ctk.CTkFont("Cascadia Mono", 13),
            border_width=0, activate_scrollbars=True)
        self.console_text.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.console_text._textbox.tag_configure("error", foreground=COLORS["red"])
        self.console_text._textbox.tag_configure("heading", foreground=COLORS["green"], font=_font(9, "bold", "Cascadia Mono"))
        self.console_text.configure(state="disabled")

    def _build_diagnostics(self, page: tk.Frame):
        header = Panel(page, height=136)
        header.pack(fill="x")
        header.pack_propagate(False)
        copy = tk.Frame(header, bg=COLORS["surface"])
        copy.pack(fill="x", padx=20, pady=(14, 6))
        tk.Label(copy, text=self.t("diagnostics_title"), bg=COLORS["surface"], fg=COLORS["text"], font=_font(12, "bold")).pack(anchor="w")
        tk.Label(
            copy,
            text=self.t("diagnostics_description"),
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=_font(9),
            anchor="w",
            justify="left",
        ).pack(fill="x", anchor="w", pady=(5, 0))
        actions = tk.Frame(header, bg=COLORS["surface"])
        actions.pack(fill="x", padx=18, pady=(0, 12))
        self.cache_check_button = ModernButton(
            actions,
            self.t("check_cache_files"),
            self.run_cache_check,
            "secondary",
            icon_name="folder-open",
            width=162,
        )
        self.cache_check_button.pack(side="left", padx=3)
        self.diagnostic_button = ModernButton(
            actions,
            self.t("run_check"),
            self.run_diagnostics,
            "primary",
            icon_name="activity",
            width=134,
        )
        self.diagnostic_button.pack(side="left", padx=3)
        self.save_report_button = ModernButton(
            actions, self.t("save_diagnostic_report"), self._save_diagnostic_report,
            "ghost", icon_name="save", width=154,
        )
        self.save_report_button.pack(side="right", padx=3)
        self.copy_report_button = ModernButton(
            actions, self.t("copy_diagnostic_report"), self._copy_diagnostic_report,
            "ghost", icon_name="copy", width=154,
        )
        self.copy_report_button.pack(side="right", padx=3)
        self.diagnostic_results = ctk.CTkScrollableFrame(
            page,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["surface_hover"],
        )
        self.diagnostic_results.pack(fill="both", expand=True, pady=(12, 0))
        self._diagnostic_placeholder(self.t("not_checked"))

    def _diagnostic_placeholder(self, text: str):
        for child in self.diagnostic_results.winfo_children():
            child.destroy()
        empty = Panel(self.diagnostic_results)
        empty.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            empty, text="", image=ICONS.get("activity", 26, COLORS["faint"]),
        ).pack(pady=(34, 8))
        tk.Label(empty, text=text, bg=COLORS["surface"], fg=COLORS["text"],
            font=_font(11, "bold")).pack()
        tk.Label(empty, text=self.t("diagnostics_idle_hint"), bg=COLORS["surface"],
            fg=COLORS["faint"], font=_font(9), wraplength=470, justify="center").pack(
            pady=(6, 36))

    def _build_players(self, page: tk.Frame):
        header = Panel(page, height=124)
        header.pack(fill="x", pady=(0, 10))
        header.pack_propagate(False)
        copy = tk.Frame(header, bg=COLORS["surface"])
        tk.Label(
            copy,
            text=self.t("players_title"),
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=_font(13, "bold"),
        ).pack(anchor="w")
        self.players_summary = tk.Label(
            copy,
            text=self.t("players_loading"),
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=_font(9),
        )
        self.players_summary.pack(anchor="w", pady=(5, 0))
        self.players_access_hint = tk.Label(
            copy,
            text=self.t("players_access_hint"),
            bg=COLORS["surface"],
            fg=COLORS["faint"],
            font=_font(8),
            anchor="w",
            justify="left",
        )
        self.players_access_hint.pack(fill="x", anchor="w", pady=(4, 0))
        copy.bind(
            "<Configure>",
            lambda event: self.players_access_hint.configure(
                wraplength=max(160, event.width - 4)
            ),
        )

        actions = tk.Frame(header, bg=COLORS["surface"])
        actions.pack(side="right", padx=14)
        copy.pack(side="left", fill="both", expand=True, padx=20, pady=14)
        ModernButton(
            actions,
            self.t("players_validate"),
            self._validate_players,
            "ghost",
            icon_name="shield-check",
            width=112,
        ).pack(side="left", padx=(0, 6))
        ModernButton(
            actions,
            self.t("refresh"),
            lambda: self._refresh_players(force=True),
            "secondary",
            icon_name="refresh-cw",
            width=105,
        ).pack(side="left", padx=(0, 6))
        ModernButton(
            actions,
            self.t("players_add"),
            self._add_player,
            "primary",
            icon_name="database",
            width=132,
        ).pack(side="left")

        self.players_list = ctk.CTkScrollableFrame(
            page,
            fg_color=COLORS["window"],
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["surface_hover"],
            corner_radius=0,
        )
        self.players_list.pack(fill="both", expand=True)
        self._refresh_players(force=True)

    def _player_state_signature(self, state: dict) -> str:
        stable = json.loads(json.dumps(state))
        for player in stable.get("players", []):
            session = player.get("active_session")
            if isinstance(session, dict):
                session.pop("last_seen", None)
        return json.dumps(stable, ensure_ascii=True, sort_keys=True)

    def _refresh_players(self, force: bool = False):
        if not hasattr(self, "players_list"):
            return
        state = load_player_admin_state()
        signature = self._player_state_signature(state)
        if not force and signature == self._players_state_signature:
            return
        self._players_state_signature = signature
        self._last_players_refresh = time.monotonic()
        for child in self.players_list.winfo_children():
            child.destroy()

        players = state.get("players", [])
        active_count = sum(bool(player.get("active_session")) for player in players)
        whitelist_key = "players_whitelist_on" if state.get("whitelist_enabled") else "players_whitelist_off"
        self.players_summary.configure(
            text=self.t(
                "players_summary",
                count=len(players),
                active=active_count,
                whitelist=self.t(whitelist_key),
            )
        )
        if not players:
            empty = Panel(self.players_list)
            empty.pack(fill="x", pady=(0, 8))
            tk.Label(
                empty,
                text=self.t("players_empty"),
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                font=_font(10),
            ).pack(padx=20, pady=35)
            return

        for player in players:
            self._render_player_card(player, bool(state.get("whitelist_enabled")))

    def _render_player_card(self, player: dict, whitelist_enabled: bool):
        card = Panel(self.players_list)
        card.pack(fill="x", pady=(0, 8))
        top = tk.Frame(card, bg=COLORS["surface"])
        top.pack(fill="x", padx=16, pady=(13, 8))
        title_box = tk.Frame(top, bg=COLORS["surface"])
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_box,
            text=str(player.get("name") or player.get("save_id")),
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=_font(11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text=str(player.get("save_id") or ""),
            bg=COLORS["surface"],
            fg=COLORS["faint"],
            font=_font(8, family="Cascadia Mono"),
        ).pack(anchor="w", pady=(3, 0))

        allowed = bool(player.get("whitelisted"))
        access_text = self.t("players_access_open")
        access_color = COLORS["green"]
        if whitelist_enabled:
            access_text = self.t("players_access_allowed" if allowed else "players_access_blocked")
            access_color = COLORS["green"] if allowed else COLORS["red"]
        ctk.CTkLabel(
            top,
            text=access_text,
            fg_color=COLORS["green_soft"] if access_color == COLORS["green"] else COLORS["red_soft"],
            text_color=access_color,
            corner_radius=7,
            height=26,
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
        ).pack(side="right", padx=(8, 0))

        details = tk.Frame(card, bg=COLORS["surface_2"])
        details.pack(fill="x", padx=12, pady=(0, 8))
        devices = dict(player.get("devices") or {})
        if devices:
            for label, device_id in sorted(devices.items()):
                row = tk.Frame(details, bg=COLORS["surface_2"])
                row.pack(fill="x", padx=10, pady=5)
                tk.Label(
                    row,
                    text=str(label),
                    bg=COLORS["surface_2"],
                    fg=COLORS["muted"],
                    font=_font(8, "bold"),
                    width=11,
                    anchor="w",
                ).pack(side="left")
                tk.Label(
                    row,
                    text=str(device_id),
                    bg=COLORS["surface_2"],
                    fg=COLORS["text"],
                    font=_font(8, family="Cascadia Mono"),
                    anchor="w",
                ).pack(side="left", fill="x", expand=True)
                ModernButton(
                    row,
                    self.t("players_unlink"),
                    lambda value=device_id: self._remove_player_device(value),
                    "ghost",
                    width=92,
                    height=28,
                ).pack(side="right")
        else:
            tk.Label(
                details,
                text=self.t("players_no_devices"),
                bg=COLORS["surface_2"],
                fg=COLORS["faint"],
                font=_font(8),
            ).pack(anchor="w", padx=10, pady=10)

        session = player.get("active_session")
        if isinstance(session, dict):
            session_row = ctk.CTkFrame(
                card,
                fg_color=COLORS["green_soft"],
                corner_radius=8,
                border_width=1,
                border_color=COLORS["green"],
            )
            session_row.pack(fill="x", padx=12, pady=(0, 8))
            session_text = self.t(
                "players_active_session",
                device=session.get("device_label") or session.get("device_id") or self.t("players_unknown_device"),
                ip=session.get("client_ip") or "—",
            )
            ctk.CTkLabel(
                session_row,
                text=session_text,
                text_color=COLORS["green"],
                font=ctk.CTkFont("Segoe UI", 10, "bold"),
                anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=10, pady=7)
            ModernButton(
                session_row,
                self.t("players_release"),
                lambda save_id=player["save_id"]: self._release_player_session(save_id),
                "danger",
                width=122,
                height=28,
            ).pack(side="right", padx=5, pady=4)

        footer = tk.Frame(card, bg=COLORS["surface"])
        footer.pack(fill="x", padx=13, pady=(0, 12))
        ModernButton(
            footer,
            self.t("players_add_device"),
            lambda save_id=player["save_id"]: self._add_player_device(save_id),
            "secondary",
            icon_name="network",
            width=142,
            height=30,
        ).pack(side="left")
        action_key = "players_disable" if allowed and whitelist_enabled else "players_enable"
        ModernButton(
            footer,
            self.t(action_key),
            lambda save_id=player["save_id"], enabled=allowed and whitelist_enabled:
                self._toggle_player_access(save_id, enabled),
            "ghost",
            width=122,
            height=30,
        ).pack(side="left", padx=(6, 0))
        conflicts = int(player.get("conflicts") or 0)
        if conflicts:
            ModernButton(
                footer,
                self.t("players_conflicts", count=conflicts),
                lambda save_id=player["save_id"]: self._open_player_conflicts(save_id),
                "ghost",
                width=132,
                height=30,
            ).pack(side="left", padx=(6, 0))
        ModernButton(
            footer,
            self.t("players_remove"),
            lambda save_id=player["save_id"]: self._remove_player(save_id),
            "danger",
            width=102,
            height=30,
        ).pack(side="right")

    def _ask_player_value(self, title_key: str, prompt_key: str) -> str | None:
        dialog = ctk.CTkInputDialog(
            title=self.t(title_key),
            text=self.t(prompt_key),
            fg_color=COLORS["surface"],
            button_fg_color=COLORS["primary"],
            button_hover_color=COLORS["primary_hover"],
            button_text_color=COLORS["on_primary"],
            entry_fg_color=COLORS["surface_3"],
            entry_border_color=COLORS["border"],
            entry_text_color=COLORS["text"],
        )
        value = dialog.get_input()
        return value.strip() if isinstance(value, str) else None

    def _run_player_command(self, arguments: list[str], activity_key: str, show_result: bool = False):
        def task():
            code, output = self.controller.manage_players(arguments)
            if code != 0:
                raise RuntimeError(output.strip() or self.t("players_command_failed"))
            return output.strip()

        def done(output: str):
            self._players_state_signature = ""
            self._refresh_players(force=True)
            self._record_activity(self.t(activity_key), output or self.t("players_updated"))
            if show_result:
                messagebox.showinfo(self.t(activity_key), output or self.t("players_updated"), parent=self)

        self._run_background(self.t("players_updating"), task, done)

    def _add_player(self):
        name = self._ask_player_value("players_add", "players_name_prompt")
        if not name:
            return
        device_id = self._ask_player_value("players_add", "players_device_prompt")
        if not device_id:
            return
        save_id = self._ask_player_value("players_add", "players_save_prompt")
        arguments = ["add", "--name", name, "--device", device_id]
        if save_id:
            arguments.extend(["--save-id", save_id])
        self._run_player_command(arguments, "players_added")

    def _add_player_device(self, save_id: str):
        device_id = self._ask_player_value("players_add_device", "players_device_prompt")
        if not device_id:
            return
        self._run_player_command(
            ["add-device", "--save-id", save_id, "--device", device_id],
            "players_device_added",
        )

    def _remove_player_device(self, device_id: str):
        if not messagebox.askyesno(
            self.t("players_unlink"),
            self.t("players_unlink_confirm", device=device_id),
            parent=self,
        ):
            return
        self._run_player_command(
            ["remove-device", "--device", device_id],
            "players_device_removed",
        )

    def _toggle_player_access(self, save_id: str, currently_enabled: bool):
        command = "disable" if currently_enabled else "enable"
        activity = "players_disabled" if currently_enabled else "players_enabled"
        self._run_player_command([command, "--save-id", save_id], activity)

    def _remove_player(self, save_id: str):
        if not messagebox.askyesno(
            self.t("players_remove"),
            self.t("players_remove_confirm", save_id=save_id),
            parent=self,
        ):
            return
        self._run_player_command(
            ["remove-player", "--save-id", save_id],
            "players_removed",
        )

    def _validate_players(self):
        self._run_player_command(["validate"], "players_validation", show_result=True)

    def _release_player_session(self, save_id: str):
        if not messagebox.askyesno(
            self.t("players_release"),
            self.t("players_release_confirm", save_id=save_id),
            parent=self,
        ):
            return
        try:
            request_save_session_release(save_id)
        except Exception as exc:
            messagebox.showerror(self.t("error_title"), str(exc), parent=self)
            return
        self._record_activity(
            self.t("players_release_requested"),
            self.t("players_release_requested_detail", save_id=save_id),
        )
        self.after(800, lambda: self._refresh_players(force=True))

    def _open_player_conflicts(self, save_id: str):
        safe_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in save_id
        )
        self._open_path(BASE_DIR / "guest_saves" / "conflicts" / safe_id)

    def _build_guide(self, page: tk.Frame):
        header = Panel(page, height=122)
        header.pack(fill="x", pady=(0, 10))
        header.pack_propagate(False)
        copy = tk.Frame(header, bg=COLORS["surface"])
        copy.pack(side="left", fill="both", expand=True, padx=20, pady=13)
        tk.Label(copy, text=self.t("guide_title"), bg=COLORS["surface"],
            fg=COLORS["text"], font=_font(13, "bold")).pack(anchor="w")
        tk.Label(copy, text=self.t("guide_intro"), bg=COLORS["surface"],
            fg=COLORS["muted"], font=_font(9)).pack(anchor="w", pady=(5, 0))
        badge_row = tk.Frame(copy, bg=COLORS["surface"])
        badge_row.pack(anchor="w", fill="x", pady=(6, 0))
        self.guide_platform_badge = ctk.CTkLabel(
            badge_row,
            text=f"{self.t('guide_platform_badge')} · {self._profile_label()}",
            text_color=COLORS["ios"] if self.profile_key == "ios" else COLORS["android"],
            fg_color=COLORS["ios_soft"] if self.profile_key == "ios" else COLORS["android_soft"],
            corner_radius=SIZES["radius_small"],
            font=ctk.CTkFont("Segoe UI", 9, "bold"),
            height=24,
        )
        self.guide_platform_badge.pack(side="left", padx=(0, 9))
        tk.Label(
            badge_row,
            text=self.t("guide_qr_explainer"),
            bg=COLORS["surface"],
            fg=COLORS["green"],
            font=_font(8, "bold"),
            anchor="w",
            justify="left",
        ).pack(side="left")

        dns_card = ctk.CTkFrame(header, fg_color=COLORS["surface_2"],
            corner_radius=SIZES["radius_small"], border_width=1, border_color=COLORS["border"])
        dns_card.pack(side="right", padx=12, pady=10)
        dns_card.grid_columnconfigure(0, weight=1)
        dns_copy = tk.Frame(dns_card, bg=COLORS["surface_2"])
        dns_copy.grid(row=0, column=0, sticky="w", padx=(12, 10), pady=8)
        tk.Label(dns_copy, text=self.t("guide_dns_address").title(), bg=COLORS["surface_2"],
            fg=COLORS["faint"], font=_font(8)).pack(anchor="w")
        self.guide_dns_value = tk.Label(dns_copy, text="—", bg=COLORS["surface_2"],
            fg=COLORS["text"], font=_font(10, "bold", "Cascadia Mono"))
        self.guide_dns_value.pack(anchor="w", pady=(2, 0))
        self.guide_copy_button = ModernButton(
            dns_card, self.t("copy"), self._copy_dns_ip, "ghost",
            icon_name="copy", width=82, height=34,
        )
        self.guide_copy_button.grid(row=0, column=1, padx=(0, 7), pady=7)
        self.guide_qr_button = ModernButton(
            dns_card, self.t("show_setup_qr"), self._show_setup_qr, "primary",
            icon_name="network", width=170, height=34,
        )
        self.guide_qr_button.grid(row=0, column=2, padx=(0, 7), pady=7)

        tabs = ctk.CTkTabview(
            page,
            fg_color=COLORS["surface"],
            segmented_button_fg_color=COLORS["surface_2"],
            segmented_button_selected_color=COLORS["primary"],
            segmented_button_selected_hover_color=COLORS["primary_hover"],
            segmented_button_unselected_color=COLORS["surface_3"],
            segmented_button_unselected_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"],
            corner_radius=SIZES["radius"],
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        tabs.pack(fill="both", expand=True)
        ios_tab_name = self.t("profile_ios")
        android_tab_name = self.t("profile_android")
        tabs.add(ios_tab_name)
        tabs.add(android_tab_name)
        tabs.set(self._profile_label())
        self.guide_tabs = tabs
        self.guide_texts: dict[str, ctk.CTkTextbox] = {}
        for key, tab_name in (("ios", ios_tab_name), ("android", android_tab_name)):
            tab = tabs.tab(tab_name)
            tab.configure(fg_color=COLORS["surface"])
            text = ctk.CTkTextbox(
                tab,
                fg_color=COLORS["surface"],
                text_color=COLORS["text"],
                scrollbar_button_color=COLORS["surface_3"],
                scrollbar_button_hover_color=COLORS["surface_hover"],
                corner_radius=8,
                wrap="word",
                height=10,
                font=ctk.CTkFont("Segoe UI", 12),
                border_width=0,
                activate_scrollbars=True,
            )
            text.pack(fill="both", expand=True, padx=4, pady=4)
            text._textbox.tag_configure(
                "title", foreground=COLORS["text"], font=_font(16, "bold"), spacing3=8
            )
            text._textbox.tag_configure(
                "section", foreground=COLORS["green"], font=_font(10, "bold"),
                spacing1=13, spacing3=5
            )
            text._textbox.tag_configure("muted", foreground=COLORS["muted"])
            text._textbox.tag_configure(
                "code", foreground=COLORS["amber"], background=COLORS["surface_2"],
                font=_font(9, family="Cascadia Mono"), lmargin1=12, lmargin2=12,
                rmargin=12, spacing1=7, spacing3=7
            )
            self.guide_texts[key] = text
        self._refresh_guide_content()

    def _guide_sections(self, profile_key: str, host: str):
        if profile_key == "ios":
            return [
                (
                    self.t("guide_ios_start_title"),
                    self.t("guide_ios_start_body"),
                    "",
                    "",
                ),
                (
                    self.t("guide_ios_phone_title"),
                    self.t("guide_ios_phone_body"),
                    host,
                    self.t("guide_ios_phone_note"),
                ),
                (
                    self.t("guide_test_title"),
                    self.t("guide_test_body"),
                    "http://jp-4-9-0-pag.ludia.net/status/2.0/",
                    self.t("guide_test_note"),
                ),
                (
                    self.t("guide_ios_install_title"),
                    self.t("guide_ios_install_body"),
                    "",
                    self.t("guide_ios_install_note"),
                ),
            ]
        return [
            (
                self.t("guide_android_start_title"),
                self.t("guide_android_start_body"),
                "",
                "",
            ),
            (
                self.t("guide_android_vpn_title"),
                self.t("guide_android_vpn_body"),
                "",
                self.t("guide_android_vpn_note"),
            ),
            (
                self.t("guide_android_redirect_title"),
                self.t("guide_android_redirect_body", host=host),
                f"{host}  jp-4-9-0-pag.ludia.net\n{host}  jp-4-9-0-pap.ludia.net",
                self.t("guide_android_redirect_note"),
            ),
            (
                self.t("guide_android_enable_title"),
                self.t("guide_android_enable_body"),
                "",
                self.t("guide_android_enable_note"),
            ),
            (
                self.t("guide_test_title"),
                self.t("guide_test_body"),
                "http://jp-4-9-0-pag.ludia.net/status/2.0/",
                self.t("guide_test_note"),
            ),
            (
                self.t("guide_android_install_title"),
                self.t("guide_android_install_body"),
                "",
                self.t("guide_android_install_note"),
            ),
        ]

    def _refresh_guide_content(self):
        if not hasattr(self, "guide_texts"):
            return
        host = resolved_manifest_host(self.settings_store.load())
        if hasattr(self, "guide_dns_value"):
            self.guide_dns_value.configure(text=host)
        if hasattr(self, "guide_platform_badge"):
            is_ios = self.profile_key == "ios"
            self.guide_platform_badge.configure(
                text=f"{self.t('guide_platform_badge')} · {self._profile_label()}",
                text_color=COLORS["ios"] if is_ios else COLORS["android"],
                fg_color=COLORS["ios_soft"] if is_ios else COLORS["android_soft"],
            )
        for profile_key, text in self.guide_texts.items():
            text.configure(state="normal")
            text.delete("1.0", "end")
            title_key = "guide_ios_title" if profile_key == "ios" else "guide_android_title"
            intro_key = "guide_ios_intro" if profile_key == "ios" else "guide_android_intro"
            text.insert("end", self.t(title_key) + "\n", "title")
            text.insert("end", self.t(intro_key) + "\n", "muted")
            for title, body, code, note in self._guide_sections(profile_key, host):
                text.insert("end", title + "\n", "section")
                text.insert("end", body + "\n")
                if code:
                    text.insert("end", code + "\n", "code")
                if note:
                    text.insert("end", note + "\n", "muted")
            if profile_key == "android":
                for filename, caption_key in (
                    ("hosts_manager_lite_main.png", "guide_android_image_main"),
                    ("hosts_manager_lite_add_item.png", "guide_android_image_add"),
                ):
                    path = _asset_path(f"assets/guide/{filename}")
                    if not path.is_file():
                        continue
                    source = Image.open(path).convert("RGB")
                    source.thumbnail((660, 390), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(source)
                    self._guide_images[filename] = photo
                    text._textbox.image_create("end", image=photo)
                    text.insert("end", "\n" + self.t(caption_key) + "\n", "muted")
            text.configure(state="disabled")

    def _build_settings(self, page: tk.Frame):
        tab_bar = Panel(page)
        tab_bar.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(tab_bar, bg=COLORS["surface_2"])
        inner.pack(fill="x", padx=8, pady=8)
        self.settings_tab_buttons = {
            "general": ModernButton(inner, self.t("settings_general"), lambda: self._show_settings_tab("general"), "tab", icon_name="settings"),
            "files": ModernButton(inner, self.t("settings_files"), lambda: self._show_settings_tab("files"), "tab", icon_name="folder-open"),
        }
        for button in self.settings_tab_buttons.values():
            button.pack(side="left", fill="x", expand=True, padx=3)
        self.settings_content = tk.Frame(page, bg=COLORS["window"])
        self.settings_content.pack(fill="both", expand=True)
        self.settings_frames: dict[str, tk.Frame] = {}
        self._build_general_settings()
        self._build_file_settings()
        self._show_settings_tab(self._settings_tab)

    def _section_header(self, master, text: str):
        SectionHeading(master, text, background=COLORS["surface"]).pack(
            fill="x", padx=26, pady=(20, 6),
        )

    def _settings_row(self, master, label: str, hint: str = "") -> tuple[tk.Frame, tk.Frame]:
        row = tk.Frame(master, bg=COLORS["surface"])
        row.pack(fill="x", padx=26, pady=8)
        row.grid_columnconfigure(0, minsize=310)
        row.grid_columnconfigure(1, weight=1)
        copy = tk.Frame(row, bg=COLORS["surface"])
        copy.grid(row=0, column=0, sticky="new", padx=(0, 12))
        tk.Label(copy, text=label, bg=COLORS["surface"], fg=COLORS["text"], font=_font(10)).pack(anchor="w")
        if hint:
            tk.Label(copy, text=hint, bg=COLORS["surface"], fg=COLORS["faint"], font=_font(8),
                wraplength=285, justify="left").pack(anchor="w", pady=(3, 0))
        control = tk.Frame(row, bg=COLORS["surface"])
        control.grid(row=0, column=1, sticky="new")
        return row, control

    def _settings_panel(self, name: str) -> ctk.CTkScrollableFrame:
        """Scrollable settings surface: four languages never clip a hint."""
        frame = ctk.CTkScrollableFrame(
            self.settings_content,
            fg_color=COLORS["surface"],
            corner_radius=SIZES["radius"],
            border_width=1,
            border_color=COLORS["border_soft"],
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["surface_hover"],
        )
        frame.grid(row=0, column=0, sticky="nsew")
        self.settings_content.grid_rowconfigure(0, weight=1)
        self.settings_content.grid_columnconfigure(0, weight=1)
        self.settings_frames[name] = frame
        return frame

    def _build_general_settings(self):
        frame = self._settings_panel("general")
        self._section_header(frame, self.t("section_interface"))
        _, control = self._settings_row(frame, self.t("language"), self.t("language_hint"))
        self.settings_language_combo = ctk.CTkOptionMenu(control, values=list(LANGUAGE_NAMES.values()),
            command=self._language_selected, height=40, corner_radius=8,
            fg_color=COLORS["surface_3"], button_color=COLORS["surface_hover"],
            button_hover_color=COLORS["border"], dropdown_fg_color=COLORS["surface_2"],
            dropdown_hover_color=COLORS["surface_hover"], text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 14), dropdown_font=ctk.CTkFont("Segoe UI", 13))
        self.settings_language_combo.set(LANGUAGE_NAMES[self.language])
        self.settings_language_combo.pack(fill="x")

        self._section_header(frame, self.t("section_appearance"))
        self.reduced_motion_var = tk.BooleanVar(value=self.reduced_motion)
        _, control = self._settings_row(
            frame, self.t("reduced_motion"), self.t("reduced_motion_hint")
        )
        ToggleSwitch(control, self.reduced_motion_var, self._toggle_reduced_motion).pack(anchor="w")
        self.show_release_notes_var = tk.BooleanVar(value=self.show_release_notes)
        _, control = self._settings_row(
            frame, self.t("show_release_notes_label"), self.t("show_release_notes_hint")
        )
        ToggleSwitch(control, self.show_release_notes_var, self._toggle_release_notes).pack(anchor="w")

        self._section_header(frame, self.t("section_network"))
        self.auto_host_var = tk.BooleanVar(value=True)
        _, control = self._settings_row(frame, self.t("automatic_ip"), self.t("automatic_ip_hint"))
        auto = ToggleSwitch(control, self.auto_host_var, self._toggle_host_entry)
        auto.pack(anchor="w")
        _, control = self._settings_row(frame, self.t("ip_address"), self.t("ip_address_hint"))
        self.host_entry = self._entry(control)
        self.host_entry.pack(fill="x")
        _, control = self._settings_row(frame, self.t("manifest_port"))
        self.port_entry = self._entry(control)
        self.port_entry.pack(fill="x")
        footer = tk.Frame(frame, bg=COLORS["surface"])
        footer.pack(fill="x", padx=26, pady=(16, 24))
        ModernButton(footer, self.t("save_settings"), self.save_settings, "primary", icon_name="save", width=230).pack(side="right")
        self._load_settings_form()

    def _build_file_settings(self):
        frame = self._settings_panel("files")
        tk.Label(frame, text=self.t("files_title"), bg=COLORS["surface"], fg=COLORS["text"], font=_font(14, "bold")).pack(anchor="w", padx=26, pady=(24, 5))
        tk.Label(frame, text=self.t("files_description"), bg=COLORS["surface"], fg=COLORS["muted"], font=_font(9), wraplength=720, justify="left").pack(anchor="w", padx=26)
        button_area = tk.Frame(frame, bg=COLORS["surface"])
        button_area.pack(fill="x", padx=26, pady=(20, 0))
        button_area.grid_columnconfigure(0, weight=1)
        button_area.grid_columnconfigure(1, weight=1)
        files = [
            ("offer_rotation", CONFIG_DIR / "offer_rotation.json", "file-braces-corner"),
            ("whitelist", CONFIG_DIR / "whitelist.json", "file-braces-corner"),
            ("linked_devices", CONFIG_DIR / "device_links.json", "network"),
            ("main_server_file", None, "file-text"),
        ]
        for index, (label_key, path, icon_name) in enumerate(files):
            if path is None:
                command = lambda: self._open_path(
                    server_profile(self.profile_key).server_script
                )
            else:
                command = lambda target=path: self._open_path(target)
            ModernButton(button_area, self.t(label_key), command, "secondary", icon_name=icon_name).grid(row=index // 2, column=index % 2, sticky="ew", padx=(0 if index % 2 == 0 else 6, 6 if index % 2 == 0 else 0), pady=6)
        ModernButton(button_area, self.t("open_server_folder"), lambda: self._open_path(BASE_DIR), "ghost", icon_name="folder-open").grid(row=2, column=0, sticky="ew", padx=(0, 6), pady=6)
        ModernButton(button_area, self.t("open_lan_ports"), self.open_firewall, "danger", icon_name="shield-check").grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=6)
        note = ctk.CTkFrame(frame, fg_color=COLORS["surface_2"], corner_radius=10,
            border_width=1, border_color=COLORS["border"])
        note.pack(fill="x", padx=26, pady=(22, 24))
        tk.Label(note, text=self.t("restart_note_title"), bg=COLORS["surface_2"], fg=COLORS["amber"], font=_font(10, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        tk.Label(note, text=self.t("restart_note_body"), bg=COLORS["surface_2"], fg=COLORS["muted"], font=_font(9)).pack(anchor="w", padx=18, pady=(0, 14))

    def _toggle_reduced_motion(self):
        value = bool(self.reduced_motion_var.get())
        self.reduced_motion = value
        self.settings_store.save({"reduced_motion": value})
        self.animator.enabled = not value
        self.animator.cancel_all()
        self._apply_hero_state(self._hero_state)
        self._move_nav_indicator(self._current_page)
        self._record_activity(self.t("settings_saved"), self.t("reduced_motion"))

    def _toggle_release_notes(self):
        value = bool(self.show_release_notes_var.get())
        self.show_release_notes = value
        self.settings_store.save({"show_release_notes": value})
        self._record_activity(self.t("settings_saved"), self.t("show_release_notes_label"))

    def _show_settings_tab(self, name: str):
        self._settings_tab = name
        if not hasattr(self, "settings_frames"):
            return
        if name not in self.settings_frames:
            return
        for key, frame in self.settings_frames.items():
            if key == name:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        for key, button in self.settings_tab_buttons.items():
            button.set_selected(key == name)

    def _entry(self, master) -> ctk.CTkEntry:
        return ctk.CTkEntry(master, fg_color=COLORS["surface_3"], text_color=COLORS["text"],
            border_color=COLORS["border"], border_width=1, corner_radius=8, height=40,
            font=ctk.CTkFont("Segoe UI", 14))

    def _build_about(self, page: tk.Frame):
        panel = Panel(page)
        panel.pack(fill="both", expand=True)
        content = tk.Frame(panel, bg=COLORS["surface"])
        content.pack(fill="both", expand=True, padx=34, pady=28)
        header = tk.Frame(content, bg=COLORS["surface"])
        header.pack(fill="x")
        try:
            source = Image.open(_asset_path("icons/app_icon.png")).convert("RGBA")
            image = ctk.CTkImage(light_image=source, dark_image=source, size=(56, 56))
            header._image = image
            ctk.CTkLabel(header, image=image, text="", width=56).pack(side="left", padx=(0, 14))
        except (OSError, tk.TclError):
            pass
        header_copy = tk.Frame(header, bg=COLORS["surface"])
        header_copy.pack(side="left")
        tk.Label(header_copy, text=self.t("about_name"), bg=COLORS["surface"], fg=COLORS["text"], font=_font(21, "bold")).pack(anchor="w")
        tk.Label(header_copy, text=self.t("about_tagline", version=VERSION), bg=COLORS["surface"], fg=COLORS["green"], font=_font(10, "bold")).pack(anchor="w", pady=(5, 0))
        tk.Label(content, text=self.t("about_body"), bg=COLORS["surface"], fg=COLORS["muted"], font=_font(10), justify="left", wraplength=800).pack(anchor="w", pady=(23, 0))
        update_panel = ctk.CTkFrame(
            content,
            fg_color=COLORS["surface_2"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        update_panel.pack(fill="x", pady=(22, 0))
        update_copy = tk.Frame(update_panel, bg=COLORS["surface_2"])
        update_copy.pack(side="left", fill="both", expand=True, padx=18, pady=13)
        tk.Label(
            update_copy,
            text=self.t("updates_title"),
            bg=COLORS["surface_2"],
            fg=COLORS["text"],
            font=_font(10, "bold"),
        ).pack(anchor="w")
        self.about_update_status = tk.Label(
            update_copy,
            text="",
            bg=COLORS["surface_2"],
            fg=COLORS["muted"],
            font=_font(9),
            justify="left",
            wraplength=650,
        )
        self.about_update_status.pack(anchor="w", pady=(4, 0))
        update_buttons = tk.Frame(update_panel, bg=COLORS["surface_2"])
        update_buttons.pack(side="right", padx=14, pady=12)
        self.about_release_notes_button = ModernButton(
            update_buttons,
            self.t("release_notes_show_again"),
            self.show_release_notes_dialog,
            "ghost",
            icon_name="file-text",
            width=150,
        )
        self.about_release_notes_button.pack(side="left", padx=(0, 7))
        self.about_update_button = ModernButton(
            update_buttons,
            self.t("check_for_updates"),
            lambda: self._start_update_check(manual=True),
            "secondary",
            icon_name="refresh-cw",
            width=176,
        )
        self.about_update_button.pack(side="left")
        details = ctk.CTkFrame(content, fg_color=COLORS["surface_2"], corner_radius=10,
            border_width=1, border_color=COLORS["border"])
        details.pack(fill="x", pady=(24, 0))
        rows = [
            (self.t("about_root"), str(BASE_DIR)),
            (self.t("about_entry"), self.t("about_entry_value")),
            (self.t("about_control"), self.t("about_control_value")),
            (self.t("about_config"), self.t("about_config_value")),
        ]
        for label, value in rows:
            row = tk.Frame(details, bg=COLORS["surface_2"])
            row.pack(fill="x", padx=18, pady=10)
            tk.Label(row, text=label, bg=COLORS["surface_2"], fg=COLORS["faint"], font=_font(9, "bold"), width=20, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=COLORS["surface_2"], fg=COLORS["text"], font=_font(9), anchor="w").pack(side="left", fill="x", expand=True)
        self._refresh_update_ui()

    def _update_status_text(self) -> str:
        if self._update_status == "checking":
            return self.t("update_checking")
        if self._update_status == "available" and self._update_release:
            return self.t(
                "update_available_detail",
                current=VERSION,
                latest=self._update_release.version,
            )
        if self._update_status == "up_to_date":
            return self.t("update_up_to_date", version=VERSION)
        if self._update_status == "downloading":
            return self.t("update_downloading")
        if self._update_status == "applying":
            return self.t("update_applying")
        if self._update_status == "failed":
            return self.t("update_check_failed")
        return self.t("update_not_checked", version=VERSION)

    def _refresh_update_ui(self):
        status_text = self._update_status_text()
        busy = self._update_status in {"checking", "downloading", "applying"}
        available = self._update_status == "available" and self._update_release is not None

        if hasattr(self, "about_update_status"):
            self.about_update_status.configure(
                text=status_text,
                fg=COLORS["green"] if available else (
                    COLORS["red"] if self._update_status == "failed" else COLORS["muted"]
                ),
            )
        if hasattr(self, "about_update_button"):
            self.about_update_button.configure(
                text=self.t("update_and_restart") if available else self.t("check_for_updates"),
                command=self._update_and_restart if available else (
                    lambda: self._start_update_check(manual=True)
                ),
                state="disabled" if busy else "normal",
            )

        if hasattr(self, "update_banner"):
            if available:
                self.dashboard_update_title.configure(
                    text=self.t(
                        "update_available_title",
                        version=self._update_release.version,
                    )
                )
                summary = sanitize_remote_notes(self._update_release.notes)
                detail = summary[0].lstrip("• ") if summary else ""
                self.dashboard_update_detail.configure(
                    text=(
                        f"{detail[:130]} · {self.t('update_banner_safe')}"
                        if detail
                        else self.t("update_available_short")
                    )
                )
                self.dashboard_update_button.configure(
                    text=self.t("update_and_restart"),
                    state="normal",
                )
                if hasattr(self, "dashboard_update_notes_button"):
                    self.dashboard_update_notes_button.configure(state="normal")
                self.update_banner.grid()
            else:
                self.update_banner.grid_remove()

    def _start_update_check(self, manual: bool = False):
        if self._update_checking or self._closing:
            return
        self._update_checking = True
        self._update_status = "checking"
        self._update_error = ""
        self._refresh_update_ui()

        def worker():
            try:
                release = check_for_update(VERSION)
            except Exception as exc:
                try:
                    self.after(
                        0,
                        lambda error=exc: self._update_check_failed(error, manual),
                    )
                except (tk.TclError, RuntimeError):
                    pass
            else:
                try:
                    self.after(0, lambda value=release: self._update_check_finished(value))
                except (tk.TclError, RuntimeError):
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _update_check_finished(self, release: ReleaseInfo | None):
        self._update_checking = False
        self._update_release = release
        self._update_status = "available" if release else "up_to_date"
        self._refresh_update_ui()
        if release:
            self.bottom_status.configure(
                text=self.t("update_available_title", version=release.version),
                fg=COLORS["green"],
            )

    def _update_check_failed(self, error: Exception, manual: bool):
        self._update_checking = False
        self._update_error = str(error)
        self._update_status = "failed" if manual else "idle"
        self._refresh_update_ui()
        if manual:
            self._show_error_dialog(
                self.t("update_error_title"),
                self.t("update_check_failed_detail", error=str(error)),
            )

    def _update_and_restart(self):
        release = self._update_release
        if not release or self._busy:
            return
        if not getattr(sys, "frozen", False):
            self._show_error_dialog(
                self.t("update_error_title"),
                self.t("update_frozen_only"),
            )
            return
        if not messagebox.askyesno(
            self.t("update_confirm_title"),
            self.t("update_confirm_body", version=release.version),
            parent=self,
        ):
            return

        self._update_status = "downloading"
        self._refresh_update_ui()

        def task():
            return download_update(release)

        def done(downloaded: DownloadedUpdate):
            self._begin_apply_update(downloaded)

        def failed(_error):
            self._update_status = "available"
            self._refresh_update_ui()

        self._run_background(self.t("update_downloading"), task, done, failed)

    def _begin_apply_update(self, downloaded: DownloadedUpdate):
        self._update_status = "applying"
        self._refresh_update_ui()
        self._set_busy(True, self.t("update_applying"))

        def worker():
            try:
                self.coordinator.stop()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and server_state().running:
                    time.sleep(0.1)
                if server_state().running:
                    raise UpdateError(self.t("update_stop_failed"))

                helper_source = _asset_path("tools/apply_update.ps1")
                if not helper_source.is_file():
                    raise UpdateError(self.t("update_helper_missing"))
                helper_path = downloaded.package_path.parent / "apply_update.ps1"
                shutil.copy2(helper_source, helper_path)
                powershell = (
                    Path(os.environ.get("SystemRoot", r"C:\Windows"))
                    / "System32"
                    / "WindowsPowerShell"
                    / "v1.0"
                    / "powershell.exe"
                )
                command = [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(helper_path),
                    "-InstallDir",
                    str(BASE_DIR),
                    "-PackagePath",
                    str(downloaded.package_path),
                    "-LauncherPid",
                    str(os.getpid()),
                    "-RestartExecutable",
                    str(Path(sys.executable).resolve()),
                ]
                launch_update_helper(
                    command,
                    downloaded.package_path.parent,
                )
            except Exception as exc:
                try:
                    self.after(0, lambda error=exc: self._update_apply_failed(error))
                except (tk.TclError, RuntimeError):
                    pass
            else:
                try:
                    self.after(0, self.destroy)
                except (tk.TclError, RuntimeError):
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _update_apply_failed(self, error: Exception):
        self._set_busy(False, self.t("operation_error"))
        self._update_status = "available"
        self._refresh_update_ui()
        self._show_error_dialog(self.t("update_error_title"), str(error))

    def _changelog_candidates(self) -> tuple[Path, ...]:
        """Bundled changelog locations, installation folder first."""
        return (BASE_DIR / "CHANGELOG.md", _asset_path("CHANGELOG.md"))

    def _maybe_show_release_notes(self):
        """Open the release notes exactly once after an update."""
        if self._closing or self._release_notes_shown:
            return
        settings = self.settings_store.load()
        if str(settings.get("last_shown_release_notes_version") or "") == VERSION:
            return
        # Record the version even when the dialog is disabled, so enabling the
        # preference later cannot replay notes for an already-running build.
        self.settings_store.save({"last_shown_release_notes_version": VERSION})
        if not bool(settings.get("show_release_notes", True)):
            return
        section = load_release_section(self._changelog_candidates(), VERSION)
        if section is None or section.is_empty:
            return
        self.show_release_notes_dialog(section)

    def show_release_notes_dialog(self, section: ReleaseSection | None = None):
        if section is None:
            section = load_release_section(self._changelog_candidates(), VERSION)
        lines = [f"• {item}" for item in section.items] if section else []
        self._release_notes_shown = True
        return self._show_notes_dialog(
            title=self.t("release_notes_title", version=VERSION),
            subtitle=self.t("release_notes_subtitle"),
            heading=self.t("release_notes_heading"),
            lines=lines or [self.t("release_notes_empty")],
            accent=COLORS["green"],
        )

    def _show_remote_release_notes(self):
        release = self._update_release
        if release is None:
            return
        lines = sanitize_remote_notes(release.notes)
        return self._show_notes_dialog(
            title=self.t("release_notes_remote_title", version=release.version),
            subtitle=self.t("release_notes_remote_subtitle"),
            heading=release.title or release.tag,
            lines=lines or [self.t("release_notes_remote_empty")],
            accent=COLORS["amber"],
            page_url=release.page_url,
        )

    def _show_notes_dialog(self, title: str, subtitle: str, heading: str,
                           lines: list[str], accent: str, page_url: str | None = None):
        """Plain-text notes window. No markup is rendered and no link opens itself."""
        existing = self._release_notes_dialog
        if existing is not None:
            try:
                existing.destroy()
            except tk.TclError:
                pass
        dialog = ctk.CTkToplevel(self)
        self._release_notes_dialog = dialog
        dialog.title(title)
        dialog.configure(fg_color=COLORS["window"])
        dialog.geometry(self._center_child_geometry(700, 560))
        dialog.minsize(560, 420)
        dialog.transient(self)

        header = Card(dialog, accent=accent)
        header.pack(fill="x", padx=18, pady=(18, 10))
        header_copy = tk.Frame(header, bg=COLORS["surface"])
        header_copy.pack(fill="x", padx=20, pady=(16, 15))
        tk.Label(
            header_copy, text=title, bg=COLORS["surface"], fg=COLORS["text"],
            font=_font(17, "bold"), anchor="w", justify="left", wraplength=600,
        ).pack(anchor="w")
        tk.Label(
            header_copy, text=subtitle, bg=COLORS["surface"], fg=COLORS["muted"],
            font=_font(9), anchor="w", justify="left", wraplength=600,
        ).pack(anchor="w", pady=(6, 0))

        body = Panel(dialog)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        SectionHeading(body, heading, background=COLORS["surface"], color=accent, size=10).pack(
            fill="x", padx=16, pady=(14, 6),
        )
        text = ctk.CTkTextbox(
            body,
            fg_color=COLORS["surface"],
            text_color=COLORS["text_soft"],
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["surface_hover"],
            wrap="word",
            font=ctk.CTkFont("Segoe UI", 12),
            border_width=0,
        )
        text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        text._textbox.tag_configure("item", spacing1=5, spacing3=5, lmargin2=16)
        for line in lines:
            text.insert("end", line + "\n", "item")
        text.configure(state="disabled")

        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 16))
        ModernButton(
            actions,
            self.t("release_notes_open_github"),
            lambda: self._open_release_page(page_url),
            "ghost",
            icon_name="external-link",
            width=196,
        ).pack(side="left")

        def close():
            self._release_notes_dialog = None
            dialog.destroy()

        ModernButton(actions, self.t("close"), close, "primary", width=118).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.lift()
        dialog.focus_force()
        return dialog

    def _open_release_page(self, page_url: str | None = None):
        try:
            os.startfile(page_url or LATEST_RELEASE_PAGE)
        except OSError as exc:
            self._show_error_dialog(self.t("open_failed"), str(exc))

    def _open_latest_release(self):
        try:
            os.startfile(
                self._update_release.page_url
                if self._update_release
                else LATEST_RELEASE_PAGE
            )
        except OSError as exc:
            self._show_error_dialog(self.t("open_failed"), str(exc))

    def show_page(self, key: str, trigger_actions: bool = True):
        title_key = f"page_{key}_title"
        subtitle_key = f"page_{key}_subtitle"
        self._current_page = key
        # Raising sibling frames is unreliable when native Tk widgets and
        # CustomTkinter canvases are mixed: child windows from the previous
        # page can remain visible above the selected page.  Remove inactive
        # pages from the geometry manager so two pages can never overlap.
        for name, page in self.pages.items():
            if name == key:
                page.grid(row=0, column=0, sticky="nsew")
                page.tkraise()
            else:
                page.grid_remove()
        self.page_title.configure(text=self.t(title_key))
        self.page_subtitle.configure(text=self.t(subtitle_key))
        for name, button in self.nav_buttons.items():
            selected = name == key
            color = COLORS["green"] if selected else COLORS["muted"]
            button.configure(
                fg_color=COLORS["surface_2"] if selected else "transparent",
                text_color=COLORS["text"] if selected else COLORS["muted"],
                image=ICONS.get(self.nav_icon_names[name], 17, color),
            )
        self._move_nav_indicator(key)
        self._animate_page_entry(self.pages[key])
        if key == "settings":
            self._load_settings_form()
        elif key == "diagnostics" and trigger_actions and not self._busy:
            self.run_diagnostics()
        elif key == "players":
            self._refresh_players(force=True)
        elif key == "console":
            self._refresh_logs_now()
        elif key == "guide":
            self._refresh_guide_content()

    def _move_nav_indicator(self, key: str):
        """Glide the selection marker onto the active navigation row."""
        indicator = getattr(self, "nav_indicator", None)
        row = getattr(self, "nav_rows", {}).get(key)
        if indicator is None or row is None:
            return
        try:
            self.nav_host.update_idletasks()
            top = row.winfo_y()
            height = row.winfo_height()
        except tk.TclError:
            return
        if height <= 1:
            # The sidebar has not been laid out yet; retry once it has.
            self.after(60, lambda: self._move_nav_indicator(key))
            return
        indicator.move_to(top + 9, max(6, height - 18), animate=not self.reduced_motion)

    def _animate_page_entry(self, page: tk.Frame):
        """Slide the selected page up into place."""
        if self.reduced_motion:
            page.grid_configure(pady=0)
            return

        def step(progress: float):
            page.grid_configure(pady=(int(round(14 * (1 - progress))), 0))

        self.animator.animate("page", MOTION["normal"], step)

    def _language_selected(self, event=None):
        if self._busy:
            return
        selected_name = event if isinstance(event, str) else self.language_combo.get()
        selected = next((code for code, name in LANGUAGE_NAMES.items() if name == selected_name), self.language)
        if selected == self.language:
            return
        self.language = selected
        self.settings_store.save({"launcher_language": selected})
        self._rebuild_with_loading()

    def _profile_label(self, profile_key: str | None = None) -> str:
        key = normalize_profile_key(profile_key or self.profile_key)
        return self.t("profile_ios") if key == "ios" else self.t("profile_android")

    def _update_profile_ui(self, profile_key: str | None = None):
        key = normalize_profile_key(profile_key or self.profile_key)
        if hasattr(self, "profile_selector"):
            label = self._profile_label(key)
            if self.profile_selector.get() != label:
                self.profile_selector.set(label)
        if hasattr(self, "profile_hint_label"):
            hint_key = "profile_ios_hint" if key == "ios" else "profile_android_hint"
            hint = self.t(hint_key)
            if self.profile_hint_label.cget("text") != hint:
                self.profile_hint_label.configure(text=hint)
        if hasattr(self, "platform_badge"):
            # iOS and Android stay visually distinct; their data never mixes.
            accent = COLORS["ios"] if key == "ios" else COLORS["android"]
            background = COLORS["ios_soft"] if key == "ios" else COLORS["android_soft"]
            self.platform_badge.configure(
                text=self._profile_label(key),
                text_color=accent,
                fg_color=background,
            )

    def _set_profile_selector_state(self, state: str):
        if not hasattr(self, "profile_selector"):
            return
        if self.profile_selector.cget("state") != state:
            self.profile_selector.configure(state=state)

    def _profile_selected(self, selected_value: str):
        selected = self._profile_value_to_key.get(selected_value, self.profile_key)
        state = server_state()
        if self._busy:
            self._update_profile_ui(state.profile_key or self.profile_key)
            return
        selected = normalize_profile_key(selected)
        if selected == self.profile_key:
            return
        if state.running and not messagebox.askyesno(
            self.t("profile_restart_title"),
            self.t("profile_restart_body", profile=self._profile_label(selected)),
            parent=self,
        ):
            self._update_profile_ui(state.profile_key or self.profile_key)
            return
        self.profile_key = selected
        self.settings_store.save({"platform_profile": selected})
        self._update_profile_ui()
        if hasattr(self, "guide_tabs"):
            self.guide_tabs.set(self._profile_label())
        if hasattr(self, "guide_texts"):
            self._refresh_guide_content()
        self._record_activity(
            self.t("profile_changed"),
            self.t("profile_changed_detail", profile=self._profile_label()),
        )
        if state.running:
            self.restart_server()

    def _set_busy(self, value: bool, status: str = ""):
        self._busy = value
        state = "disabled" if value else "normal"
        for name in (
            "start_button",
            "stop_button",
            "restart_button",
            "diagnostic_button",
            "cache_check_button",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=state)
        profile_state = "disabled" if value else "normal"
        self._set_profile_selector_state(profile_state)
        spinner = getattr(self, "topbar_spinner", None)
        if spinner is not None:
            spinner.start(COLORS["amber"]) if value else spinner.stop()
        progress = getattr(self, "bottom_progress", None)
        if progress is not None:
            progress.set_value(
                0.65 if value else 0.0,
                COLORS["amber"] if value else COLORS["green"],
            )
        if status:
            self.bottom_status.configure(text=status, fg=COLORS["amber"] if value else COLORS["muted"])

    def _run_background(self, busy_text: str, task, on_success, on_error=None):
        if self._busy:
            return
        self._set_busy(True, busy_text)

        def worker():
            try:
                result = task()
            except Exception as exc:
                self.after(0, lambda error=exc: self._background_failed(error, on_error))
            else:
                self.after(0, lambda value=result: self._background_succeeded(value, on_success))

        threading.Thread(target=worker, daemon=True).start()

    def _background_succeeded(self, result, callback):
        self._set_busy(False, self.t("ready"))
        callback(result)

    def _background_failed(self, error: Exception, callback=None):
        self._set_busy(False, self.t("operation_error"))
        detail = self.t("server_already_running", pid=error.pid) if isinstance(error, ServerAlreadyRunningError) else str(error)
        if isinstance(error, StartupIssue):
            if error.code.startswith("CACHE_"):
                profile = server_profile(self.profile_key)
                report = validate_cache_folder(
                    self.profile_key,
                    profile.cache_dirs[0],
                    CONFIG_DIR,
                )
                detail = (
                    self.t("cache_start_blocked")
                    + "\n\n"
                    + self._cache_report_text((report,))
                )
            detail = f"[{error.code}] {detail}"
        activity_detail = next(
            (line.strip() for line in detail.splitlines() if line.strip()),
            self.t("operation_error"),
        )
        self._record_activity(
            self.t("operation_error"),
            activity_detail[:240],
            error=True,
        )
        if callback:
            callback(error)
        self._show_error_dialog(self.t("error_title"), detail)

    def _center_child_geometry(self, width: int, height: int) -> str:
        self.update_idletasks()
        x = max(0, self.winfo_rootx() + (self.winfo_width() - width) // 2)
        y = max(0, self.winfo_rooty() + (self.winfo_height() - height) // 2)
        return f"{width}x{height}+{x}+{y}"

    def _show_error_dialog(self, title: str, detail: str):
        """Use a resizable, scrollable dialog so long failures remain readable."""
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.configure(fg_color=COLORS["window"])
        dialog.geometry(self._center_child_geometry(690, 430))
        dialog.minsize(560, 330)
        dialog.transient(self)
        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 10))
        ctk.CTkLabel(
            header,
            text=title,
            image=ICONS.get("circle-x", 22, COLORS["red"]),
            compound="left",
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text=self.t("error_dialog_hint"),
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 11),
        ).pack(anchor="w", pady=(6, 0))
        text = ctk.CTkTextbox(
            dialog,
            fg_color=COLORS["surface"],
            text_color=COLORS["text"],
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["surface_hover"],
            wrap="word",
            font=ctk.CTkFont("Segoe UI", 12),
            border_width=1,
            border_color=COLORS["border"],
        )
        text.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        text.insert("1.0", detail)
        text.configure(state="disabled")
        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 18))

        def copy_detail():
            self.clipboard_clear()
            self.clipboard_append(detail)

        ModernButton(
            actions,
            self.t("copy_error"),
            copy_detail,
            "secondary",
            icon_name="copy",
            width=142,
        ).pack(side="left", padx=4)
        ModernButton(
            actions,
            self.t("close"),
            dialog.destroy,
            "primary",
            width=110,
        ).pack(side="right", padx=4)
        dialog.focus_force()

    def _manifest_for_current_settings(self) -> tuple[str, int, str, str]:
        settings = self.settings_store.load()
        host = resolved_manifest_host(settings)
        port = int(settings.get("manifest_port") or 9943)
        profile_key = normalize_profile_key(settings.get("platform_profile"))
        target = f"{host}:{port}"
        targets = dict(settings.get("last_manifest_targets") or {})
        output = ""
        if targets.get(profile_key) != target or not manifest_matches(host, port, profile_key):
            code, output = self.controller.rebuild_manifest(host, port, profile_key)
            if code != 0:
                raise RuntimeError(self.t("manifest_failed") + "\n\n" + output[-1200:])
            targets[profile_key] = target
            self.settings_store.save({"last_manifest_targets": targets})
        return host, port, output, profile_key

    def start_server(self):
        def task():
            settings = self.settings_store.load()
            host = resolved_manifest_host(settings)
            port = int(settings.get("manifest_port") or 9943)
            result = self.coordinator.start(self.profile_key, host, port)
            targets = dict(settings.get("last_manifest_targets") or {})
            targets[result.profile_key] = f"{host}:{port}"
            self.settings_store.save({"last_manifest_targets": targets})
            return result

        def done(result):
            self._runner = result.process
            self._watch_runner(result.process)
            self._health_ok = True
            detail = self.t("server_started_manifest")
            self._record_activity(self.t("server_started"), detail)
            self.bottom_status.configure(text=self.t("server_available", host=result.host), fg=COLORS["green"])
            self._poll_state()

        self._run_background(self.t("preparing_start"), task, done)

    def _watch_runner(self, process: subprocess.Popen):
        def watch():
            process.wait()
            close_handles = getattr(process, "close_log_handles", None)
            if callable(close_handles):
                close_handles()
            try:
                self.after(0, self._runner_finished)
            except tk.TclError:
                pass
        threading.Thread(target=watch, daemon=True).start()

    def _runner_finished(self):
        self._runner = None
        self._health_ok = False
        if not self._busy and self._last_server_running:
            self._record_activity(self.t("server_ended"), self.t("server_ended_detail"), error=True)

    def stop_server(self):
        def task():
            self.coordinator.stop()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and server_state().running:
                time.sleep(0.15)
            return None

        def done(_result):
            self._health_ok = False
            self._record_activity(self.t("server_stopped_activity"), self.t("server_stopped_activity_detail"))
            self._poll_state()

        self._run_background(self.t("stopping"), task, done)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.animator.cancel_all()
        self._close_when_idle()

    def _close_when_idle(self):
        if self._busy:
            self.bottom_status.configure(text=self.t("stopping"), fg=COLORS["amber"])
            self.after(100, self._close_when_idle)
            return

        self._set_busy(True, self.t("stopping"))

        def worker():
            try:
                self.coordinator.stop()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and server_state().running:
                    time.sleep(0.1)
            finally:
                runner = self._runner
                close_handles = getattr(runner, "close_log_handles", None)
                if callable(close_handles):
                    close_handles()
                try:
                    self.after(0, self.destroy)
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def restart_server(self):
        def task():
            self.coordinator.stop()
            time.sleep(0.4)
            settings = self.settings_store.load()
            host = resolved_manifest_host(settings)
            port = int(settings.get("manifest_port") or 9943)
            return self.coordinator.start(self.profile_key, host, port)

        def done(result):
            self._runner = result.process
            self._watch_runner(result.process)
            self._health_ok = True
            self._record_activity(self.t("server_restarted"), self.t("server_restarted_detail", host=result.host))
            self._poll_state()

        self._run_background(self.t("restarting"), task, done)

    def rebuild_manifest(self):
        def task():
            settings = self.settings_store.load()
            host = resolved_manifest_host(settings)
            port = int(settings.get("manifest_port") or 9943)
            profile_key = normalize_profile_key(settings.get("platform_profile"))
            code, output = self.controller.rebuild_manifest(host, port, profile_key)
            if code != 0:
                raise RuntimeError(self.t("manifest_failed") + "\n\n" + output[-1600:])
            targets = dict(settings.get("last_manifest_targets") or {})
            targets[profile_key] = f"{host}:{port}"
            self.settings_store.save({"last_manifest_targets": targets})
            return host, port, output, profile_key

        def done(result):
            host, port, output, profile_key = result
            summary = [line for line in output.splitlines() if line.startswith(
                ("entries checked:", "hashes updated:", "manifest entries:", "missing assets:")
            )]
            detail = " · ".join(summary) or self.t("manifest_address", host=host, port=port)
            self._record_activity(
                self.t("manifest_updated"),
                f"{self._profile_label(profile_key)} · {detail}",
            )

        self._run_background(self.t("rebuilding"), task, done)

    def _cache_reports(self) -> tuple[CacheValidationReport, ...]:
        reports = []
        for profile_key in ("ios", "android"):
            profile = server_profile(profile_key)
            reports.append(
                validate_cache_folder(
                    profile_key,
                    profile.cache_dirs[0],
                    CONFIG_DIR,
                )
            )
        return tuple(reports)

    def _cache_report_text(self, reports: tuple[CacheValidationReport, ...]) -> str:
        sections: list[str] = []
        for report in reports:
            profile = self._profile_label(report.profile_key)
            status = self.t("cache_status_ok") if report.okay else self.t("cache_status_problem")
            lines = [
                f"{profile} — {status}",
                self.t(
                    "cache_counts",
                    present=report.required_present_count,
                    expected=report.expected_count,
                    additional=report.additional_count,
                    total=report.present_count,
                ),
                self.t("cache_folder_name", folder=report.cache_dir.name),
            ]
            if not report.folder_exists:
                lines.append(self.t("cache_folder_missing"))
            elif not report.catalog_available:
                lines.append(self.t("cache_catalog_missing"))
            if report.missing:
                lines.append("")
                lines.append(self.t("cache_missing_heading", count=len(report.missing)))
                lines.extend(f"  • {name}" for name in report.missing)
            if report.wrong_size:
                lines.append("")
                lines.append(self.t("cache_damaged_heading", count=len(report.wrong_size)))
                lines.extend(
                    "  • "
                    + self.t(
                        "cache_wrong_size_line",
                        filename=item.filename,
                        actual=item.actual,
                        expected=item.expected,
                    )
                    for item in report.wrong_size
                )
            if report.unreadable:
                lines.append("")
                lines.append(self.t("cache_unreadable_heading", count=len(report.unreadable)))
                lines.extend(f"  • {name}" for name in report.unreadable)
            if report.okay:
                lines.extend(("", self.t("cache_complete_detail")))
            sections.append("\n".join(lines))
        return ("\n\n" + "─" * 64 + "\n\n").join(sections)

    def _show_cache_report(self, reports: tuple[CacheValidationReport, ...]):
        dialog = ctk.CTkToplevel(self)
        dialog.title(self.t("cache_check_title"))
        dialog.configure(fg_color=COLORS["window"])
        dialog.geometry(self._center_child_geometry(780, 590))
        dialog.minsize(620, 430)
        dialog.transient(self)
        all_okay = all(report.okay for report in reports)
        color = COLORS["green"] if all_okay else COLORS["red"]
        ctk.CTkLabel(
            dialog,
            text=self.t("cache_check_title"),
            image=ICONS.get("circle-check" if all_okay else "circle-x", 22, color),
            compound="left",
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 19, "bold"),
        ).pack(anchor="w", padx=24, pady=(20, 4))
        ctk.CTkLabel(
            dialog,
            text=self.t("cache_check_description"),
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 11),
            wraplength=720,
            justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 12))
        report_text = ctk.CTkTextbox(
            dialog,
            fg_color=COLORS["surface"],
            text_color=COLORS["text"],
            scrollbar_button_color=COLORS["surface_3"],
            scrollbar_button_hover_color=COLORS["surface_hover"],
            wrap="none",
            font=ctk.CTkFont("Cascadia Mono", 11),
            border_width=1,
            border_color=COLORS["border"],
        )
        report_text.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        report_text.insert("1.0", self._cache_report_text(reports))
        report_text.configure(state="disabled")
        ModernButton(
            dialog,
            self.t("close"),
            dialog.destroy,
            "primary",
            width=116,
        ).pack(anchor="e", padx=24, pady=(0, 18))
        dialog.focus_force()

    def run_cache_check(self):
        def done(reports):
            self._show_cache_report(reports)
            issues = sum(report.issue_count for report in reports)
            self._record_activity(
                self.t("cache_check_passed") if issues == 0 else self.t("cache_check_failed"),
                self.t("cache_check_result_detail", count=issues),
                error=issues > 0,
            )

        self._run_background(
            self.t("checking_cache_files"),
            self._cache_reports,
            done,
        )

    def _render_diagnostic_results(self, results):
        for child in self.diagnostic_results.winfo_children():
            child.destroy()
        failed = 0
        for index, result in enumerate(results):
            okay = result.okay
            failed += int(not okay and not result.warning)
            row = Panel(self.diagnostic_results)
            row.pack(fill="x", pady=(0 if index == 0 else 5, 0))
            color = COLORS["green"] if okay else (COLORS["amber"] if result.warning else COLORS["red"])
            icon_name = "circle-check" if okay else "circle-x"
            ctk.CTkFrame(row, fg_color=color, width=3, corner_radius=2).place(
                x=0, rely=0.5, anchor="w", relheight=0.6,
            )
            ctk.CTkLabel(row, text="", image=ICONS.get(icon_name, 19, color), width=34).pack(side="left", padx=(9, 0), pady=12)
            copy = tk.Frame(row, bg=COLORS["surface"])
            copy.pack(side="left", fill="both", expand=True, pady=11)
            tk.Label(copy, text=self.t(f"diag_{result.name}"), bg=COLORS["surface"], fg=COLORS["text"], font=_font(10, "bold")).pack(anchor="w")
            detail = result.detail
            if not okay and result.code:
                detail = f"[{result.code}] {detail}"
            detail_label = tk.Label(
                copy,
                text=detail,
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                font=_font(8),
                anchor="w",
                justify="left",
            )
            detail_label.pack(fill="x", anchor="w", pady=(3, 0))
            copy.bind(
                "<Configure>",
                lambda event, label=detail_label: label.configure(
                    wraplength=max(180, event.width - 8)
                ),
                add="+",
            )
            status_key = "diag_done" if okay else ("diag_warning" if result.warning else "diag_error")
            tk.Label(row, text=self.t(status_key), bg=COLORS["surface"], fg=color, font=_font(8, "bold")).pack(side="right", padx=18, pady=12)
        title = self.t("diagnostics_passed") if failed == 0 else self.t("diagnostics_failed", count=failed)
        self._record_activity(title, self.t("diagnostics_result_detail"), error=failed > 0)

    def run_diagnostics(self):
        profile_key = self.profile_key

        self._run_background(
            self.t("checking"),
            lambda: run_diagnostics(profile_key),
            self._render_diagnostic_results,
        )

    def _diagnostic_report(self) -> str:
        settings = self.settings_store.load()
        host = resolved_manifest_host(settings)
        port = int(settings.get("manifest_port") or 9943)
        return build_diagnostic_report(
            BASE_DIR,
            self.profile_key,
            host,
            manifest_matches(host, port, self.profile_key),
            http_responds("http://127.0.0.1/status/2.0/", 1.2),
            http_responds(STATUS_URL, 1.2),
        )

    def _copy_diagnostic_report(self):
        report = self._diagnostic_report()
        self.clipboard_clear()
        self.clipboard_append(report)
        self.bottom_status.configure(text=self.t("diagnostic_report_copied"), fg=COLORS["green"])

    def _save_diagnostic_report(self):
        destination = filedialog.asksaveasfilename(
            parent=self,
            title=self.t("save_diagnostic_report"),
            defaultextension=".txt",
            initialfile="DinoServer-diagnostic.txt",
            filetypes=[("Text", "*.txt")],
        )
        if not destination:
            return
        Path(destination).write_text(self._diagnostic_report(), encoding="utf-8")
        self.bottom_status.configure(text=self.t("diagnostic_report_saved"), fg=COLORS["green"])

    def _load_settings_form(self):
        if not hasattr(self, "host_entry"):
            return
        settings = self.settings_store.load()
        host = str(settings.get("manifest_host") or "")
        self._manual_host_value = host
        self.auto_host_var.set(not bool(host))
        self.host_entry.configure(state="normal")
        self.host_entry.delete(0, "end")
        self.host_entry.insert(
            0,
            resolved_manifest_host({"manifest_host": ""}) if self.auto_host_var.get() else host,
        )
        self.port_entry.delete(0, "end")
        self.port_entry.insert(0, str(settings.get("manifest_port") or 9943))
        self._toggle_host_entry()

    def _toggle_host_entry(self):
        automatic = self.auto_host_var.get()
        current = self.host_entry.get().strip()
        detected = resolved_manifest_host({"manifest_host": ""})
        if automatic:
            if current and current != detected:
                self._manual_host_value = current
            value = detected
        else:
            value = self._manual_host_value or current or detected
        self.host_entry.configure(state="normal")
        self.host_entry.delete(0, "end")
        self.host_entry.insert(0, value)
        self.host_entry.configure(state="disabled" if automatic else "normal")
        if not automatic:
            self.host_entry.focus_set()
            self.host_entry.icursor("end")

    def save_settings(self):
        host = "" if self.auto_host_var.get() else self.host_entry.get().strip()
        try:
            if host and ipaddress.ip_address(host).version != 4:
                raise ValueError
            port = int(self.port_entry.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror(self.t("invalid_settings_title"), self.t("invalid_settings"), parent=self)
            return
        self.settings_store.save({"manifest_host": host, "manifest_port": port})
        resolved = resolved_manifest_host(self.settings_store.load())
        self._record_activity(self.t("settings_saved"), self.t("settings_saved_detail", host=resolved, port=port))
        self._refresh_guide_content()
        self._poll_state()

    def open_firewall(self):
        path = BASE_DIR / "OPEN_LAN_FIREWALL.bat"
        try:
            os.startfile(str(path))
            self._record_activity(self.t("firewall_requested"), self.t("firewall_requested_detail"))
        except OSError as exc:
            messagebox.showerror(self.t("open_failed"), str(exc), parent=self)

    def _open_setup_page(self):
        host = resolved_manifest_host(self.settings_store.load())
        try:
            os.startfile(f"http://{host}:9943/setup/")
        except OSError as exc:
            messagebox.showerror(self.t("open_failed"), str(exc), parent=self)

    def _show_setup_qr(self):
        host = resolved_manifest_host(self.settings_store.load())
        url = f"http://{host}:9943/setup/"
        image = _make_setup_qr_image(url)
        dialog = ctk.CTkToplevel(self)
        dialog.title(self.t("setup_qr_title"))
        dialog.configure(fg_color=COLORS["window"])
        dialog.resizable(False, False)
        dialog.transient(self)
        width, height = 390, 465
        x = max(0, self.winfo_rootx() + (self.winfo_width() - width) // 2)
        y = max(0, self.winfo_rooty() + (self.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        ctk.CTkLabel(
            dialog,
            text=self.t("setup_qr_title"),
            text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI", 20, "bold"),
        ).pack(pady=(20, 5))
        ctk.CTkLabel(
            dialog,
            text=self.t("setup_qr_hint"),
            text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI", 11),
            wraplength=340,
        ).pack(padx=20)
        qr_photo = ctk.CTkImage(light_image=image, dark_image=image, size=(270, 270))
        dialog._dino_qr_image = qr_photo
        ctk.CTkLabel(
            dialog,
            text="",
            image=qr_photo,
            fg_color="#FFFFFF",
            corner_radius=8,
        ).pack(pady=(12, 8))
        ctk.CTkLabel(
            dialog,
            text=url,
            text_color=COLORS["text"],
            font=ctk.CTkFont("Cascadia Mono", 11),
            wraplength=350,
        ).pack(padx=20)
        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.pack(pady=(12, 16))

        def copy_url():
            self.clipboard_clear()
            self.clipboard_append(url)
            self.bottom_status.configure(text=self.t("setup_url_copied"), fg=COLORS["green"])

        ModernButton(
            actions,
            self.t("copy_setup_url"),
            copy_url,
            "secondary",
            icon_name="copy",
            width=135,
        ).pack(side="left", padx=4)
        ModernButton(
            actions,
            self.t("open_setup_page"),
            self._open_setup_page,
            "primary",
            icon_name="external-link",
            width=150,
        ).pack(side="left", padx=4)

    def _startup_progress(self, state: StartupState, detail: str):
        try:
            self.after(0, lambda: self.bottom_status.configure(text=detail, fg=COLORS["amber"]))
        except tk.TclError:
            pass

    def _open_path(self, path: Path):
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror(self.t("open_failed"), str(exc), parent=self)

    def _copy_logs(self):
        value = self.console_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(value)
        self.bottom_status.configure(text=self.t("logs_copied"), fg=COLORS["green"])

    def _copy_endpoint(self):
        value = str(self.endpoint_value.cget("text"))
        if value and value != "—":
            self.clipboard_clear()
            self.clipboard_append(value)
            self.bottom_status.configure(text=self.t("address_copied"), fg=COLORS["green"])

    def _copy_dns_ip(self):
        value = resolved_manifest_host(self.settings_store.load())
        self.clipboard_clear()
        self.clipboard_append(value)
        self.bottom_status.configure(text=self.t("dns_address_copied"), fg=COLORS["green"])

    def _refresh_logs_now(self):
        self._last_log_text = ""
        self._render_logs()

    def _refresh_logs(self):
        self._render_logs()
        try:
            self.after(1200, self._refresh_logs)
        except tk.TclError:
            pass

    def _render_logs(self):
        errors = tail_text(SERVER_ERROR_FILE, 50)
        output = tail_text(SERVER_LOG_FILE, 180)
        combined = f"{errors}\n{output}"
        if combined != self._last_log_text and hasattr(self, "console_text"):
            at_bottom = self.console_text.yview()[1] > 0.96
            self.console_text.configure(state="normal")
            self.console_text.delete("1.0", "end")
            if errors.strip():
                self.console_text.insert("end", self.t("stderr_heading") + "\n", "heading")
                self.console_text.insert("end", errors.strip() + "\n\n", "error")
            self.console_text.insert("end", self.t("stdout_heading") + "\n", "heading")
            self.console_text.insert("end", output.strip() if output.strip() else self.t("empty_log"))
            self.console_text.configure(state="disabled")
            if at_bottom or not self._last_log_text:
                self.console_text.see("end")
            self._last_log_text = combined

    def _record_activity(self, title: str, detail: str, error: bool = False):
        self.activity_title.configure(text=title, fg=COLORS["red"] if error else COLORS["green"])
        self.activity_detail.configure(text=detail)
        self.bottom_status.configure(text=title, fg=COLORS["red"] if error else COLORS["green"])

    def _poll_state(self):
        # Start/stop/restart completion handlers request an immediate refresh.
        # Cancel the previously scheduled refresh first, otherwise every
        # restart creates another permanent polling loop and eventually floods
        # the Tk event queue.
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        try:
            state = server_state()
            settings = self.settings_store.load()
            host = resolved_manifest_host(settings)
            port = int(settings.get("manifest_port") or 9943)
            display_profile = state.profile_key or self.profile_key
            self._update_profile_ui(display_profile)
            self.endpoint_value.configure(text=host)
            self.metric_vars["lan"].set(host)
            self.metric_vars["pid"].set(self.t("pid_value", pid=state.pid) if state.running else self.t("not_running"))
            self.metric_vars["uptime"].set(format_uptime(state.started_at) if state.running else "—")
            now = time.monotonic()
            if (
                self._current_page == "players"
                and now - self._last_players_refresh > 3
            ):
                self._last_players_refresh = now
                self._refresh_players()
            if now - self._last_player_refresh > 4:
                saves, allowed = count_players()
                self.metric_vars["players"].set(self.t("saves_value", saves=saves, allowed=allowed))
                self._last_player_refresh = now
            if state.running:
                self._apply_hero_state(
                    "running", "running_detail", pid=state.pid,
                )
                self.global_status.set_state(
                    self.t("status_running").title(),
                    COLORS["green"],
                    COLORS["green_soft"],
                    border=COLORS["green_glow"],
                    pulsing=not self.reduced_motion,
                )
                self.metric_tiles["pid"].set_accent(COLORS["text"])
                self.metric_tiles["uptime"].set_accent(COLORS["text"])
                self.start_button.configure(state="disabled")
                self._set_profile_selector_state("normal")
                if not self._busy:
                    self.stop_button.configure(state="normal")
                    self.restart_button.configure(state="normal")
                if now - self._last_health_check > 4 and not self._health_check_pending:
                    self._start_health_check()
            else:
                self._health_ok = False
                self._apply_hero_state(
                    "starting" if self._busy else "stopped",
                    "starting_detail" if self._busy else "stopped_detail",
                )
                if self._busy:
                    self.global_status.set_state(
                        self.t("status_starting").title(),
                        COLORS["amber"],
                        COLORS["amber_soft"],
                        border=COLORS["amber_glow"],
                        pulsing=not self.reduced_motion,
                    )
                else:
                    self.global_status.set_state(
                        self.t("status_stopped").title(),
                        COLORS["faint"],
                        COLORS["surface_2"],
                        border=COLORS["border_soft"],
                    )
                self.metric_tiles["pid"].set_accent(COLORS["muted"])
                self.metric_tiles["uptime"].set_accent(COLORS["muted"])
                if not self._busy:
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.restart_button.configure(state="disabled")
                    self._set_profile_selector_state("normal")
                self.endpoint_health.configure(
                    text=self.t("health_offline"), text_color=COLORS["muted"],
                    image=ICONS.get("circle-x", 13, COLORS["muted"]),
                )
            if state.running and self._health_ok:
                self.endpoint_health.configure(
                    text=self.t("health_online"), text_color=COLORS["green"],
                    image=ICONS.get("circle-check", 13, COLORS["green"]),
                )
            elif state.running:
                self.endpoint_health.configure(
                    text=self.t("health_starting"), text_color=COLORS["amber"],
                    image=ICONS.get("server", 13, COLORS["amber"]),
                )
            if hasattr(self, "connection_labels"):
                milestones = read_milestones(BASE_DIR)
                dns_ok = dns_running()
                http_ok = state.running and self._health_ok
                statuses = {
                    "dns": (
                        dns_ok,
                        self.t("connection_running") if dns_ok else self.t("connection_off"),
                    ),
                    "http": (
                        http_ok,
                        self.t("connection_running") if http_ok else self.t("connection_off"),
                    ),
                    "game": (
                        milestones.game_connected or milestones.login_completed,
                        self.t("connection_connected") if milestones.game_connected or milestones.login_completed else self.t("connection_waiting"),
                    ),
                    "device": (
                        milestones.simple_state != "waiting",
                        self.t(f"device_{milestones.simple_state}"),
                    ),
                }
                for key, (okay, text) in statuses.items():
                    color = COLORS["green"] if okay else COLORS["faint"]
                    icon = "circle-check" if okay else "server"
                    self.connection_labels[key].configure(
                        text=text,
                        text_color=color,
                        image=ICONS.get(icon, 14, color),
                    )
            self._last_server_running = state.running
            self._schedule_state_poll(1000)
        except tk.TclError:
            pass
        except Exception as exc:
            try:
                self.bottom_status.configure(text=self.t("state_error", error=exc), fg=COLORS["red"])
                self._schedule_state_poll(1800)
            except tk.TclError:
                pass

    def _schedule_state_poll(self, delay_ms: int):
        if self._closing:
            return
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
        self._poll_after_id = self.after(delay_ms, self._poll_state)

    def _start_health_check(self):
        self._health_check_pending = True
        self._last_health_check = time.monotonic()

        def worker():
            okay = http_responds(STATUS_URL, 1.2)
            try:
                self.after(0, lambda: self._finish_health_check(okay))
            except tk.TclError:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _finish_health_check(self, okay: bool):
        self._health_ok = okay
        self._health_check_pending = False


def _enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main():
    _enable_dpi_awareness()
    _cleanup_legacy_update_bridge()
    try:
        app = DinosaurServerLauncher()
    except Exception as exc:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / "launcher_error.log").write_text(
                traceback.format_exc(),
                encoding="utf-8",
            )
        except OSError:
            pass
        root = tk.Tk()
        root.withdraw()
        language = detect_system_language()
        messagebox.showerror(translate(language, "error_title"), str(exc), parent=root)
        root.destroy()
        return
    app.mainloop()


if __name__ == "__main__":
    main()
