"""Privacy-safe diagnostics and simple connection milestone extraction."""

from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

from .firewall import inspect_firewall
from .port_inspection import describe_owner, inspect_required_ports
from .version import DISPLAY_VERSION


_UUID_RE = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
_DEVICE_RE = re.compile(r"\bD-[A-Za-z0-9_-]{8,}\b")
_USER_PATH_RE = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s]+")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass(frozen=True)
class Milestones:
    dns_query: bool = False
    http_status: bool = False
    manifest: bool = False
    cache_asset: bool = False
    game_connected: bool = False
    login_completed: bool = False

    @property
    def simple_state(self) -> str:
        if self.login_completed or self.game_connected:
            return "game_connected"
        if self.cache_asset or self.manifest:
            return "downloading_cache"
        if self.http_status or self.dns_query:
            return "device_found"
        return "waiting"

    @property
    def diagnostic_code(self) -> str:
        if self.game_connected or self.login_completed:
            return ""
        if self.http_status:
            return "HTTP_OK_NO_GAME"
        if self.dns_query:
            return "DNS_OK_NO_HTTP"
        return "NO_DNS_QUERY"


def _read_tail(path: Path, limit: int = 240_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_milestones(base_dir: Path) -> Milestones:
    dns_query = False
    milestone_path = base_dir / "run" / "connection_milestones.json"
    try:
        data = json.loads(milestone_path.read_text(encoding="utf-8"))
        dns_query = bool(data.get("dns_query_received"))
    except (OSError, ValueError, TypeError):
        pass
    log = _read_tail(base_dir / "logs" / "server.log")
    lower = log.casefold()
    return Milestones(
        dns_query=dns_query,
        http_status="status ok source=device" in lower,
        manifest="manifest bytes=" in lower,
        cache_asset="streaming " in lower or "/jp/local_path/" in lower,
        game_connected=(
            "new connection" in lower
            or "game client" in lower
            or " sfs] connected from " in lower
        ),
        login_completed=(
            "login completed" in lower
            or "login success" in lower
            or "login accepted" in lower
            or "out login ok" in lower
        ),
    )


def _sanitize_event(line: str, lan_ip: str) -> str:
    value = _UUID_RE.sub("<redacted-id>", line)
    value = _DEVICE_RE.sub("<redacted-device>", value)
    value = _USER_PATH_RE.sub(r"<user-profile>", value)
    value = re.sub(r"(?i)(device|save|username|player)[_ =:'\"]+[^\s,;]+", r"\1=<redacted>", value)
    value = _IP_RE.sub(lambda match: match.group(0) if match.group(0) in {lan_ip, "127.0.0.1", "0.0.0.0"} else "<redacted-ip>", value)
    return value[:400]


def _safe_event_summary(line: str) -> str | None:
    lower = line.casefold()
    if "status ok" in lower:
        return "[HTTP] status request completed"
    if "manifest bytes=" in lower:
        return "[HTTP] cache manifest requested"
    if "streaming " in lower or "/jp/local_path/" in lower:
        return "[HTTP] cache asset requested"
    if " sfs] connected from " in lower or "new connection" in lower:
        return "[GAME] client connected"
    if "login accepted" in lower or "out login ok" in lower:
        return "[GAME] login completed"
    if "client closed" in lower:
        return "[GAME] client closed the connection"
    if "error" in lower or "traceback" in lower:
        return "[SERVER] an error was recorded; inspect local logs"
    return None


def build_diagnostic_report(
    base_dir: Path,
    profile_key: str,
    lan_ip: str,
    manifest_matches: bool,
    http_80: bool,
    http_9943: bool,
) -> str:
    cache_dir = base_dir / ("cache_ios" if profile_key == "ios" else "cache_android")
    files = [path for path in cache_dir.iterdir() if path.is_file()] if cache_dir.is_dir() else []
    total_bytes = sum(path.stat().st_size for path in files)
    firewall = inspect_firewall(Path(os.sys.executable))
    milestones = read_milestones(base_dir)
    ports = inspect_required_ports()
    lines = [
        DISPLAY_VERSION,
        f"Windows: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Profile: {profile_key}",
        f"Cache: files={len(files)}, bytes={total_bytes}",
        f"LAN IPv4: {lan_ip}",
        f"Manifest mapping: {'OK' if manifest_matches else 'FAILED'}",
        f"Firewall: configured={firewall.configured}, network={firewall.network_category}",
        "Listeners:",
    ]
    for owner in ports:
        lines.append(
            f"  {owner.protocol.upper()} {owner.port}: "
            f"{'LISTENING' if owner.occupied else 'FREE'} ({describe_owner(owner)})"
        )
    lines.extend(
        [
            f"DNS process: {'RUNNING' if any(p.port == 53 and p.occupied for p in ports) else 'STOPPED'}",
            f"DNS target query received: {milestones.dns_query}",
            f"HTTP 80 status: {http_80}",
            f"HTTP 9943 status: {http_9943}",
            f"Manifest requested: {milestones.manifest}",
            f"Cache asset requested: {milestones.cache_asset}",
            f"Game connected: {milestones.game_connected}",
            f"Login completed: {milestones.login_completed}",
            f"Connection diagnosis: {milestones.diagnostic_code or 'READY'}",
            "Recent sanitized events:",
        ]
    )
    raw = _read_tail(base_dir / "logs" / "server.log", 120_000)
    selected = [summary for line in raw.splitlines() if (summary := _safe_event_summary(line))][-20:]
    lines.extend(f"  {line}" for line in selected)
    if not selected:
        lines.append("  No relevant events recorded.")
    return "\n".join(lines) + "\n"
