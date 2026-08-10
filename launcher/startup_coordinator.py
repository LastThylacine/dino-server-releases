"""Ordered one-click startup with rollback and stable diagnostic codes."""

from __future__ import annotations

import enum
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .core import (
    BASE_DIR,
    CONFIG_DIR,
    DNS_PID_FILE,
    STATUS_URL,
    ServerController,
    http_responds,
    manifest_matches,
    server_profile,
)
from .cache_validation import validate_cache_folder
from .firewall import configure_firewall, inspect_firewall
from .port_inspection import belongs_to_installation, describe_owner, inspect_required_ports


class StartupState(str, enum.Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    CHECKING_CACHE = "CHECKING_CACHE"
    CHECKING_PORTS = "CHECKING_PORTS"
    CONFIGURING_FIREWALL = "CONFIGURING_FIREWALL"
    BUILDING_MANIFEST = "BUILDING_MANIFEST"
    STARTING_DNS = "STARTING_DNS"
    STARTING_SERVER = "STARTING_SERVER"
    VERIFYING_HTTP = "VERIFYING_HTTP"
    READY = "READY"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class StartupResult:
    process: object
    host: str
    profile_key: str
    manifest_output: str


class StartupIssue(RuntimeError):
    def __init__(self, code: str, message: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(message + (f"\n\n{detail}" if detail else ""))


ProgressCallback = Callable[[StartupState, str], None]


class StartupCoordinator:
    def __init__(self, controller: ServerController, progress: ProgressCallback | None = None):
        self.controller = controller
        self.progress = progress or (lambda _state, _detail: None)
        self.state = StartupState.IDLE

    def _set(self, state: StartupState, detail: str) -> None:
        self.state = state
        self.progress(state, detail)

    def validate_cache(self, profile_key: str) -> tuple[int, int]:
        profile = server_profile(profile_key)
        cache = profile.cache_dirs[0]
        report = validate_cache_folder(profile_key, cache, CONFIG_DIR)
        if not report.folder_exists:
            raise StartupIssue(
                "CACHE_FOLDER_MISSING",
                f"{profile_key.title()} cache folder is missing.",
                str(cache),
            )
        if not report.catalog_available:
            raise StartupIssue(
                "CACHE_CATALOG_MISSING",
                "The built-in cache file list is unavailable.",
                f"Cannot verify {profile_key} cache safely.",
            )
        if report.present_count == 0:
            raise StartupIssue("CACHE_EMPTY", f"{profile_key.title()} cache folder is empty.", str(cache))
        if report.missing or report.wrong_size or report.unreadable:
            details: list[str] = []
            if report.missing:
                details.append(
                    "Missing files:\n"
                    + "\n".join(f"• {name}" for name in report.missing[:30])
                )
                if len(report.missing) > 30:
                    details.append(f"…and {len(report.missing) - 30} more missing files.")
            if report.wrong_size:
                details.append(
                    "Damaged or incomplete files:\n"
                    + "\n".join(
                        f"• {item.filename} ({item.actual} / {item.expected} bytes)"
                        for item in report.wrong_size[:20]
                    )
                )
            if report.unreadable:
                details.append(
                    "Unreadable files:\n"
                    + "\n".join(f"• {name}" for name in report.unreadable[:20])
                )
            raise StartupIssue(
                "CACHE_FILES_INCOMPLETE",
                f"{profile_key.title()} cache files are incomplete.",
                "\n\n".join(details),
            )
        files = [path for path in cache.iterdir() if path.is_file()]
        extensions = {path.suffix.casefold() for path in files}
        if not {".dab", ".dhr", ".dsb"}.issubset(extensions):
            raise StartupIssue(
                "CACHE_PLATFORM_MISMATCH",
                f"{profile_key.title()} cache is incomplete or uses the wrong platform set.",
                "Expected .dab, .dhr and .dsb packages.",
            )
        if profile_key == "ios":
            required = {
                f"imgbannerwide_hd{suffix}" for suffix in (".dab", ".dhr", ".dsb")
            }
            missing = sorted(name for name in required if not (cache / name).is_file())
            if missing:
                raise StartupIssue(
                    "CACHE_PLATFORM_MISMATCH",
                    "The iOS banner package is incomplete.",
                    ", ".join(missing),
                )
        versions: list[int] = []
        for path in files[:120]:
            try:
                with path.open("rb") as handle:
                    header = handle.read(8)
            except OSError:
                continue
            if len(header) == 8 and header[4:8] == b"LPKG":
                versions.append(header[0])
        if versions:
            version4 = versions.count(4)
            version6 = versions.count(6)
            if profile_key == "ios" and version6 > len(versions) * 0.8:
                raise StartupIssue(
                    "CACHE_PLATFORM_MISMATCH",
                    "The selected iOS folder appears to contain the Android cache.",
                )
            if profile_key == "android" and version4 > len(versions) * 0.8:
                raise StartupIssue(
                    "CACHE_PLATFORM_MISMATCH",
                    "The selected Android folder appears to contain the iOS cache.",
                )
        return report.present_count, report.total_bytes

    def _check_ports(self) -> None:
        for owner in inspect_required_ports():
            if not owner.occupied:
                continue
            code = f"PORT_{owner.port}_OCCUPIED"
            if belongs_to_installation(owner, BASE_DIR):
                raise StartupIssue(
                    code,
                    f"{owner.protocol.upper()} port {owner.port} is still in use by an untracked Dino Server process.",
                    f"{describe_owner(owner)}. Close the stale Dino Server process, then try again.",
                )
            raise StartupIssue(
                code,
                f"{owner.protocol.upper()} port {owner.port} is already in use.",
                describe_owner(owner),
            )

    def _ensure_firewall(self) -> None:
        if os.environ.get("DINOSERVER_SKIP_FIREWALL") == "1":
            return
        status = inspect_firewall(Path(os.sys.executable))
        if not status.configured:
            self._set(
                StartupState.CONFIGURING_FIREWALL,
                "Approve the Windows prompt once; Dino Server will configure local-only access.",
            )
            try:
                configure_firewall(Path(os.sys.executable))
            except RuntimeError as exc:
                raise StartupIssue("FIREWALL_RULE_MISSING", str(exc)) from exc

    def start(self, profile_key: str, host: str, port: int = 9943) -> StartupResult:
        started = False
        self._set(StartupState.PREFLIGHT, "Preparing Dino Server")
        try:
            # Stop only tracked, installation-owned stale children.
            self.controller.cleanup_stale()
            self._set(StartupState.CHECKING_CACHE, f"Checking {profile_key} cache")
            self.validate_cache(profile_key)
            self._set(StartupState.CHECKING_PORTS, "Checking DNS, HTTP and game ports")
            self._check_ports()
            self._ensure_firewall()
            self._set(StartupState.BUILDING_MANIFEST, "Building the selected cache manifest")
            code, output = self.controller.rebuild_manifest(host, port, profile_key)
            if code:
                raise StartupIssue("MANIFEST_BUILD_FAILED", "The manifest could not be built.", output[-1600:])
            if not manifest_matches(host, port, profile_key):
                raise StartupIssue("MANIFEST_BUILD_FAILED", "The generated manifest has the wrong profile mapping.")
            self._set(StartupState.STARTING_DNS, "Starting embedded local DNS")
            self.controller.start_dns(host)
            started = True
            self._set(StartupState.STARTING_SERVER, "Starting game and backup services")
            process = self.controller.start(profile_key)
            self._set(StartupState.VERIFYING_HTTP, "Verifying local HTTP endpoints")
            deadline = time.monotonic() + 30.0
            seen_9943 = False
            seen_80 = False
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                seen_9943 = seen_9943 or http_responds(STATUS_URL, 0.7)
                seen_80 = seen_80 or http_responds("http://127.0.0.1/status/2.0/", 0.7)
                if seen_9943 and seen_80:
                    self._set(StartupState.READY, "Dino Server is ready")
                    return StartupResult(process, host, profile_key, output)
                time.sleep(0.25)
            if not seen_9943:
                raise StartupIssue("HTTP_9943_FAILED", "The cache/status service did not respond on port 9943.")
            raise StartupIssue("HTTP_80_FAILED", "The original-domain HTTP service did not respond on port 80.")
        except Exception:
            self._set(StartupState.ERROR, "Startup failed; rolling back")
            if started:
                self.controller.stop()
            raise

    def stop(self) -> None:
        self._set(StartupState.STOPPING, "Stopping all Dino Server services")
        self.controller.stop()
        self._set(StartupState.IDLE, "Dino Server is stopped")
