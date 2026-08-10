"""File and process integration used by the desktop launcher.

The launcher deliberately treats the existing server, PowerShell and utility
scripts as external programs.  Keeping this module free of server imports makes
it possible to replace those scripts without rebuilding the desktop UI.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
CONFIG_DIR = BASE_DIR / "config"
RUN_DIR = BASE_DIR / "run"
LOG_DIR = BASE_DIR / "logs"
SETTINGS_FILE = CONFIG_DIR / "local_settings.json"
SERVER_PID_FILE = RUN_DIR / "jpb-server.pid"
BACKUP_PID_FILE = RUN_DIR / "jpb-backup.pid"
DNS_PID_FILE = RUN_DIR / "dino-dns.pid"
SERVER_PROFILE_FILE = RUN_DIR / "jpb-profile.txt"
SERVER_LOG_FILE = LOG_DIR / "server.log"
SERVER_ERROR_FILE = LOG_DIR / "server_error.log"
DNS_LOG_FILE = LOG_DIR / "dns.log"
PLAYER_SESSIONS_FILE = RUN_DIR / "save_sessions.json"
PLAYER_SESSION_RELEASE_FILE = RUN_DIR / "save_session_release_requests.json"
STATUS_URL = "http://127.0.0.1:9943/status/2.0/"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class ServerState:
    running: bool
    pid: int | None
    started_at: dt.datetime | None = None
    profile_key: str | None = None


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    okay: bool
    detail: str
    warning: bool = False
    code: str = ""


@dataclass(frozen=True)
class ServerProfile:
    key: str
    manifest_name: str
    cache_dir_names: tuple[str, ...]
    server_script: Path
    manifest_builder: str

    @property
    def manifest_path(self) -> Path:
        return CONFIG_DIR / self.manifest_name

    @property
    def cache_dirs(self) -> tuple[Path, ...]:
        return tuple(BASE_DIR / name for name in self.cache_dir_names)


SERVER_PROFILES = {
    "ios": ServerProfile(
        key="ios",
        manifest_name="fixed_manifest_ios.json",
        cache_dir_names=("cache_ios",),
        server_script=BASE_DIR / "server_current.py",
        manifest_builder="ios_banner",
    ),
    "android": ServerProfile(
        key="android",
        manifest_name="fixed_manifest_android.json",
        cache_dir_names=("cache_android",),
        server_script=BASE_DIR / "jpb_server" / "current_server_android.py",
        manifest_builder="standard",
    ),
}
DEFAULT_PROFILE_KEY = "ios"


def normalize_profile_key(value: object) -> str:
    key = str(value or "").strip().lower()
    return key if key in SERVER_PROFILES else DEFAULT_PROFILE_KEY


def server_profile(value: object) -> ServerProfile:
    return SERVER_PROFILES[normalize_profile_key(value)]


class ServerAlreadyRunningError(RuntimeError):
    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"Server component already running (PID {pid}).")


class SettingsStore:
    defaults = {
        "manifest_host": "",
        "manifest_port": 9943,
        "language": "en",
        "launcher_language": "",
        "last_manifest_target": "",
        "last_manifest_targets": {},
        "platform_profile": DEFAULT_PROFILE_KEY,
        "settings_machine": "",
        # Interface preferences. These stay out of the update payload, so the
        # "what changed" dialog is shown exactly once per installed version.
        "last_shown_release_notes_version": "",
        "show_release_notes": True,
        "reduced_motion": False,
    }

    @staticmethod
    def machine_token() -> str:
        identity = f"{socket.gethostname().lower()}|{uuid.getnode():012x}"
        import hashlib

        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def load(self) -> dict:
        data = dict(self.defaults)
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError, TypeError):
            pass
        # A manually selected LAN address belongs to one computer. Older
        # launchers did not record ownership, so copied folders could expose
        # the previous owner's IP. Treat missing/mismatched ownership as auto.
        current_machine = self.machine_token()
        if data.get("settings_machine") != current_machine:
            data["manifest_host"] = ""
            data["last_manifest_target"] = ""
            data["last_manifest_targets"] = {}
            data["launcher_language"] = ""
        data["platform_profile"] = normalize_profile_key(data.get("platform_profile"))
        if not isinstance(data.get("last_manifest_targets"), dict):
            data["last_manifest_targets"] = {}
        data["settings_machine"] = current_machine
        return data

    def save(self, values: dict) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        merged = self.load()
        merged.update(values)
        merged["settings_machine"] = self.machine_token()
        temporary = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.launcher-{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, SETTINGS_FILE)


def _kill_process_tree(pid: int | None) -> None:
    if not process_exists(pid):
        return
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def process_command_line(pid: int | None) -> str:
    if os.name != "nt" or not pid:
        return ""
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\" "
        "-ErrorAction SilentlyContinue;if($p){$p.CommandLine}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=4,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def process_executable_path(pid: int | None) -> str:
    """Read a process image path without WMI/CIM or administrator access."""
    if os.name != "nt" or not pid:
        return ""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _same_windows_path(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    try:
        left_value = os.path.normcase(os.path.abspath(os.fspath(left)))
        right_value = os.path.normcase(os.path.abspath(os.fspath(right)))
    except (OSError, TypeError, ValueError):
        return False
    return left_value == right_value


def process_belongs_to_installation(pid: int | None, tokens: Iterable[str]) -> bool:
    if not process_exists(pid):
        return False
    if os.name != "nt":
        return True
    command = process_command_line(pid)
    from .port_inspection import command_line_references_installation

    if command_line_references_installation(command, BASE_DIR, tuple(tokens)):
        return True
    # Frozen worker processes (server, DNS and backup) all use the current
    # DinoServer.exe image. QueryFullProcessImageNameW remains available on
    # systems where Win32_Process.CommandLine is hidden by policy.
    image = process_executable_path(pid)
    return bool(
        getattr(sys, "frozen", False)
        and image
        and _same_windows_path(image, sys.executable)
    )


def _installation_owner_id() -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(BASE_DIR)))
    return uuid.uuid5(uuid.NAMESPACE_URL, f"dino-server:{normalized}").hex


def _pid_file_matches_process_start(
    path: Path,
    pid: int | None,
    tolerance: float = 0.5,
) -> bool:
    """Prove a tracked PID is the exact process recorded by this installation."""
    if not process_exists(pid):
        return False
    started = process_started_at(pid)
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        recorded_pid = int(payload["pid"])
        recorded_started = dt.datetime.fromtimestamp(float(payload["started_at"]))
        owner_id = str(payload["owner"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False
    if (
        started is None
        or recorded_pid != pid
        or owner_id != _installation_owner_id()
    ):
        return False
    return abs((started - recorded_started).total_seconds()) <= tolerance


def tracked_process_belongs(path: Path, tokens: Iterable[str]) -> bool:
    pid = _read_pid(path)
    return process_belongs_to_installation(pid, tokens) or _pid_file_matches_process_start(
        path,
        pid,
    )


def _stop_owned_pid_file(path: Path, tokens: Iterable[str]) -> None:
    pid = _read_pid(path)
    owned = tracked_process_belongs(path, tokens)
    if owned:
        _kill_process_tree(pid)
        deadline = time.monotonic() + 1.2
        while process_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.04)
    if not process_exists(pid) or owned:
        _unlink_runtime_file(path)


def _unlink_runtime_file(path: Path, attempts: int = 10) -> bool:
    """Best-effort removal for disposable PID/state files on Windows."""
    for attempt in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt + 1 < attempts:
                time.sleep(0.05)
    # A stale PID file is harmless and cleanup_stale() will retry next start.
    return False


class ServerProcessGroup:
    """Popen-like handle for the editable server and backup watcher scripts."""

    def __init__(
        self,
        server: subprocess.Popen,
        backup: subprocess.Popen,
        log_handles: tuple,
        profile_key: str,
    ):
        self.server = server
        self.backup = backup
        self.pid = server.pid
        self.profile_key = profile_key
        self._jpb_log_handles = log_handles

    def close_log_handles(self) -> None:
        handles = self._jpb_log_handles
        self._jpb_log_handles = ()
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass

    def poll(self):
        return self.server.poll()

    def wait(self):
        code = self.server.wait()
        _kill_process_tree(self.backup.pid)
        try:
            self.backup.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.close_log_handles()
        for path in (SERVER_PID_FILE, BACKUP_PID_FILE, SERVER_PROFILE_FILE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        _stop_owned_pid_file(DNS_PID_FILE, ("local_dns.py",))
        return code


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
        try:
            value = int(raw)
        except ValueError:
            value = int(json.loads(raw)["pid"])
        return value if value > 0 else None
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _write_owned_pid(path: Path, pid: int) -> None:
    started = process_started_at(pid)
    if started is None:
        path.write_text(str(pid), encoding="ascii")
        return
    payload = {
        "schema": 1,
        "pid": int(pid),
        "started_at": started.timestamp(),
        "owner": _installation_owner_id(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        encoding="ascii",
    )


def process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def process_started_at(pid: int | None) -> dt.datetime | None:
    if os.name != "nt" or not pid:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        okay = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not okay:
            return None
        creation_ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        unix_seconds = creation_ticks / 10_000_000 - 11_644_473_600
        return dt.datetime.fromtimestamp(unix_seconds)
    finally:
        kernel32.CloseHandle(handle)


def running_profile_key() -> str | None:
    try:
        value = SERVER_PROFILE_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return normalize_profile_key(value) if value in SERVER_PROFILES else None


def server_state() -> ServerState:
    pid = _read_pid(SERVER_PID_FILE)
    running = tracked_process_belongs(
        SERVER_PID_FILE,
        ("current_server.py", "current_server_android.py", "server_current.py"),
    )
    return ServerState(
        running,
        pid if running else None,
        process_started_at(pid) if running else None,
        running_profile_key() if running else None,
    )


def dns_running() -> bool:
    return tracked_process_belongs(DNS_PID_FILE, ("local_dns.py",))


def discover_python() -> str:
    """Return a real Python executable, avoiding the Microsoft Store alias."""
    current = Path(sys.executable)
    if current.name.lower() == "pythonw.exe":
        console = current.with_name("python.exe")
        if console.is_file():
            current = console
    if current.is_file() and current.name.lower() not in {"py.exe"}:
        return str(current)
    raise RuntimeError("A working Python 3 runtime was not found.")


def lan_addresses() -> list[str]:
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if address not in found and not address.startswith(("127.", "169.254.")):
                found.append(address)
    except OSError:
        pass
    return found


def resolved_manifest_host(settings: dict | None = None) -> str:
    settings = settings or SettingsStore().load()
    configured = str(settings.get("manifest_host") or "").strip()
    if configured:
        return configured
    addresses = lan_addresses()
    return addresses[0] if addresses else "127.0.0.1"


def http_responds(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def manifest_matches(host: str, port: int, profile_key: str = DEFAULT_PROFILE_KEY) -> bool:
    path = server_profile(profile_key).manifest_path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return False
        prefix = f"http://{host}:{port}/jp/local_path/"
        return str(data[0].get("url") or "").startswith(prefix)
    except (OSError, ValueError, TypeError):
        return False


def tail_text(path: Path, max_lines: int = 160, max_bytes: int = 180_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
        text = raw.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-max_lines:])
    except OSError:
        return ""


def count_players() -> tuple[int, int]:
    saves = len(list((BASE_DIR / "guest_saves").glob("*.json")))
    allowed = 0
    try:
        data = json.loads((CONFIG_DIR / "whitelist.json").read_text(encoding="utf-8-sig"))
        entries = data.get("allow", []) if isinstance(data, dict) else []
        allowed = len(entries) if isinstance(entries, list) else 0
    except (OSError, ValueError, TypeError):
        pass
    return saves, allowed


def _read_json_object(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def load_player_admin_state() -> dict:
    whitelist = _read_json_object(
        CONFIG_DIR / "whitelist.json",
        {"enabled": False, "allow": []},
    )
    links = _read_json_object(CONFIG_DIR / "device_links.json", {"players": []})
    whitelist_enabled = bool(whitelist.get("enabled", False))

    allowed_save_ids: set[str] = set()
    for value in whitelist.get("allow_save_ids", []):
        if str(value or "").strip():
            allowed_save_ids.add(str(value).strip().lower())
    for entry in whitelist.get("allow", []):
        value = entry.get("save_id") if isinstance(entry, dict) else entry
        if str(value or "").strip():
            allowed_save_ids.add(str(value).strip().lower())
    denied_save_ids = {
        str(value).strip().lower()
        for value in whitelist.get("deny_save_ids", [])
        if str(value or "").strip()
    }

    sessions_by_save: dict[str, dict] = {}
    if server_state().running:
        session_data = _read_json_object(PLAYER_SESSIONS_FILE, {"sessions": []})
        ttl = float(session_data.get("ttl_seconds") or 180)
        now = dt.datetime.now().timestamp()
        for session in session_data.get("sessions", []):
            if not isinstance(session, dict):
                continue
            save_id = str(session.get("save_id") or "").strip()
            last_seen = float(session.get("last_seen") or 0)
            if save_id and now - last_seen <= max(30.0, ttl):
                sessions_by_save[save_id.lower()] = session

    conflict_counts: dict[str, int] = {}
    conflict_root = BASE_DIR / "guest_saves" / "conflicts"
    if conflict_root.is_dir():
        for directory in conflict_root.iterdir():
            if directory.is_dir():
                conflict_counts[directory.name.lower()] = len(list(directory.glob("*.json")))

    records: dict[str, dict] = {}
    raw_players = links.get("players", [])
    if not isinstance(raw_players, list):
        raw_players = []
    for raw in raw_players:
        if not isinstance(raw, dict):
            continue
        save_id = str(raw.get("save_id") or "").strip()
        if not save_id:
            continue
        devices_value = raw.get("devices", {})
        devices: dict[str, str] = {}
        if isinstance(devices_value, dict):
            devices = {
                str(label or "device"): str(device_id or "").strip()
                for label, device_id in devices_value.items()
                if str(device_id or "").strip()
            }
        elif isinstance(devices_value, list):
            devices = {
                f"device{index}": str(device_id).strip()
                for index, device_id in enumerate(devices_value, start=1)
                if str(device_id or "").strip()
            }
        key = save_id.lower()
        records[key] = {
            "name": str(raw.get("name") or save_id),
            "save_id": save_id,
            "devices": devices,
        }

    save_dir = BASE_DIR / "guest_saves"
    if save_dir.is_dir():
        for path in save_dir.glob("*.json"):
            save_id = path.stem
            records.setdefault(
                save_id.lower(),
                {"name": save_id, "save_id": save_id, "devices": {}},
            )

    for entry in whitelist.get("allow", []):
        if not isinstance(entry, dict):
            continue
        save_id = str(entry.get("save_id") or "").strip()
        if not save_id:
            continue
        records.setdefault(
            save_id.lower(),
            {
                "name": str(entry.get("name") or save_id),
                "save_id": save_id,
                "devices": {},
            },
        )

    players = []
    for key, record in records.items():
        save_id = record["save_id"]
        safe_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in save_id
        ).lower()
        record["whitelisted"] = (
            key not in denied_save_ids
            and (not whitelist_enabled or key in allowed_save_ids)
        )
        record["active_session"] = sessions_by_save.get(key)
        record["conflicts"] = conflict_counts.get(safe_id, 0)
        record["save_exists"] = (save_dir / f"{safe_id or 'guest'}.json").is_file()
        players.append(record)
    players.sort(key=lambda item: (str(item["name"]).lower(), item["save_id"].lower()))
    return {
        "whitelist_enabled": whitelist_enabled,
        "players": players,
        "active_sessions": list(sessions_by_save.values()),
    }


def request_save_session_release(save_id: str) -> None:
    save_id = str(save_id or "").strip()
    if not save_id:
        raise ValueError("save id is required")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_json_object(PLAYER_SESSION_RELEASE_FILE, {"requests": []})
    requests = data.get("requests", [])
    if not isinstance(requests, list):
        requests = []
    requests = [
        item
        for item in requests
        if not (
            isinstance(item, dict)
            and str(item.get("save_id") or "").strip().lower() == save_id.lower()
        )
    ]
    requests.append({"save_id": save_id, "requested_at": dt.datetime.now().timestamp()})
    temporary = PLAYER_SESSION_RELEASE_FILE.with_name(
        f".{PLAYER_SESSION_RELEASE_FILE.name}.launcher-{os.getpid()}.tmp"
    )
    temporary.write_text(
        json.dumps({"requests": requests}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, PLAYER_SESSION_RELEASE_FILE)


def format_uptime(started_at: dt.datetime | None) -> str:
    if not started_at:
        return "—"
    seconds = max(0, int((dt.datetime.now() - started_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days} д {hours} ч"
    return f"{hours:02d}:{minutes:02d}"


def firewall_rule_exists() -> bool:
    try:
        from .firewall import inspect_firewall

        return inspect_firewall(Path(sys.executable)).configured
    except Exception:
        return False


def run_diagnostics(profile_key: str = DEFAULT_PROFILE_KEY) -> list[DiagnosticResult]:
    settings = SettingsStore().load()
    host = resolved_manifest_host(settings)
    port = int(settings.get("manifest_port") or 9943)
    python = discover_python()
    profile = server_profile(profile_key)
    from .cache_validation import validate_cache_folder
    from .diagnostic_report import read_milestones
    from .firewall import inspect_firewall
    from .port_inspection import (
        belongs_to_installation,
        describe_owner,
        inspect_required_ports,
    )

    firewall = inspect_firewall(Path(sys.executable))
    cache_report = validate_cache_folder(
        profile.key,
        profile.cache_dirs[0],
        CONFIG_DIR,
    )
    running = server_state().running
    milestones = read_milestones(BASE_DIR)
    manifest_ok = manifest_matches(host, port, profile.key)
    cache_detail = (
        f"{profile.cache_dirs[0].name} · "
        f"{cache_report.required_present_count}/{cache_report.expected_count} required present"
    )
    if cache_report.additional_count:
        cache_detail += f" · {cache_report.additional_count} additional"
    cache_detail += f" · {cache_report.present_count} total"
    if cache_report.missing:
        cache_detail += f" · {len(cache_report.missing)} missing"
    if cache_report.wrong_size:
        cache_detail += f" · {len(cache_report.wrong_size)} damaged/incomplete"
    cache_code = ""
    if not cache_report.folder_exists:
        cache_code = "CACHE_FOLDER_MISSING"
    elif not cache_report.catalog_available:
        cache_code = "CACHE_CATALOG_MISSING"
    elif not cache_report.okay:
        cache_code = "CACHE_FILES_INCOMPLETE"
    results = [
        DiagnosticResult(
            "python",
            Path(python).is_file(),
            "Embedded Dino Server runtime" if getattr(sys, "frozen", False) else Path(python).name,
        ),
        DiagnosticResult(
            "manifest",
            manifest_ok,
            (
                f"{profile.key} · {host}:{port}"
                if manifest_ok or running
                else f"{profile.key} · will rebuild automatically on Start · {host}:{port}"
            ),
            warning=not running and not manifest_ok,
            code="MANIFEST_BUILD_FAILED" if running else "MANIFEST_WILL_REBUILD",
        ),
        DiagnosticResult(
            "cache",
            cache_report.okay,
            cache_detail,
            code=cache_code,
        ),
        DiagnosticResult(
            "http_9943",
            http_responds(STATUS_URL, 1.5),
            STATUS_URL if running else "Server is stopped; this endpoint will be checked after Start.",
            warning=not running,
            code="HTTP_9943_FAILED",
        ),
        DiagnosticResult(
            "http_80",
            http_responds("http://127.0.0.1/status/2.0/", 1.5),
            "http://127.0.0.1/status/2.0/" if running else "Server is stopped; this endpoint will be checked after Start.",
            warning=not running,
            code="HTTP_80_FAILED",
        ),
        DiagnosticResult(
            "dns",
            dns_running(),
            "UDP/TCP 53",
            warning=not running,
            code="DNS_START_FAILED",
        ),
        DiagnosticResult(
            "network",
            firewall.network_category != "Unknown",
            f"{firewall.network_category} · configured automatically; no Windows Settings change required",
            warning=firewall.network_category == "Unknown",
            code="NETWORK_PROFILE_UNKNOWN",
        ),
        DiagnosticResult(
            "firewall",
            firewall.configured,
            "Dino Server · Private/Public · LocalSubnet only",
            warning=not firewall.configured,
            code="FIREWALL_RULE_MISSING",
        ),
    ]
    if running:
        connection_code = milestones.diagnostic_code
        if connection_code == "HTTP_OK_NO_GAME":
            connection_detail = (
                "The browser reached Dino Server, but the game has not connected to TCP 9933. "
                + (
                    "On iPhone/iPad, enable Settings > Privacy & Security > Local Network "
                    "for the game, then fully close and reopen it."
                    if profile.key == "ios"
                    else "Check that Hosts Manager Lite is enabled, then fully close and reopen the game."
                )
            )
        elif connection_code == "DNS_OK_NO_HTTP":
            connection_detail = (
                "The device reached embedded DNS but not the status page. "
                "Check the device browser test and local-network access."
            )
        elif connection_code == "NO_DNS_QUERY":
            connection_detail = (
                "No device DNS query has reached Dino Server yet. "
                "Follow the selected device guide."
            )
        else:
            connection_detail = "The game has connected to Dino Server."
        results.append(
            DiagnosticResult(
                "connection_path",
                not connection_code,
                connection_detail,
                warning=bool(connection_code),
                code=connection_code,
            )
        )
    for owner in inspect_required_ports():
        okay = not owner.occupied or belongs_to_installation(owner, BASE_DIR)
        results.append(
            DiagnosticResult(
                f"port_{owner.port}_{owner.protocol}",
                okay,
                describe_owner(owner),
                code=f"PORT_{owner.port}_OCCUPIED",
            )
        )
    return results


class ServerController:
    def __init__(self) -> None:
        self.python = discover_python()

    def manifest_command(self, host: str, port: int, profile_key: str) -> list[str]:
        profile = server_profile(profile_key)
        if profile.manifest_builder == "ios_banner":
            return [
                self.python,
                str(BASE_DIR / "tools" / "build_ios_banner_experiment.py"),
                "--host",
                host,
                "--port",
                str(port),
                "--source-manifest",
                str(CONFIG_DIR / "fixed_manifest.json"),
                "--output-manifest",
                str(profile.manifest_path),
                "--manifest-only",
            ]
        return [
            self.python,
            str(BASE_DIR / "tools" / "fix_manifest.py"),
            "--host",
            host,
            "--port",
            str(port),
            "--source",
            str(CONFIG_DIR / "fixed_manifest.json"),
            "--cache-dir",
            str(profile.cache_dirs[0]),
            "--build-from-cache",
            "--output",
            str(profile.manifest_path),
        ]

    def rebuild_manifest(self, host: str, port: int, profile_key: str) -> tuple[int, str]:
        process = subprocess.run(
            self.manifest_command(host, port, profile_key),
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return process.returncode, process.stdout

    def manage_players(self, arguments: list[str]) -> tuple[int, str]:
        process = subprocess.run(
            [
                self.python,
                str(BASE_DIR / "tools" / "manage_players.py"),
                *arguments,
            ],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return process.returncode, process.stdout

    def cleanup_stale(self) -> None:
        """Remove dead state and stop only children proven to belong here."""
        tracked = (
            (DNS_PID_FILE, ("local_dns.py",)),
            (BACKUP_PID_FILE, ("backup_guest_saves.py",)),
            (
                SERVER_PID_FILE,
                ("current_server.py", "current_server_android.py", "server_current.py"),
            ),
        )
        for path, tokens in tracked:
            pid = _read_pid(path)
            if not process_exists(pid):
                _unlink_runtime_file(path)
            elif tracked_process_belongs(path, tokens):
                _stop_owned_pid_file(path, tokens)
            else:
                # The PID may have been reused by an unrelated process. The
                # PID file is local disposable state; the process is not ours.
                _unlink_runtime_file(path)
        if not process_exists(_read_pid(SERVER_PID_FILE)):
            try:
                SERVER_PROFILE_FILE.unlink()
            except FileNotFoundError:
                pass

    def start_dns(self, lan_ip: str, port: int = 53) -> subprocess.Popen:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        pid = _read_pid(DNS_PID_FILE)
        if process_exists(pid):
            raise ServerAlreadyRunningError(pid or 0)
        try:
            DNS_PID_FILE.unlink()
        except FileNotFoundError:
            pass
        environment = os.environ.copy()
        environment["JPB_BASE_DIR"] = str(BASE_DIR)
        process = subprocess.Popen(
            [
                self.python,
                str(BASE_DIR / "tools" / "local_dns.py"),
                "--base-dir",
                str(BASE_DIR),
                "--lan-ip",
                lan_ip,
                "--port",
                str(port),
                "--ready-file",
                str(DNS_PID_FILE),
            ],
            cwd=BASE_DIR,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        deadline = dt.datetime.now().timestamp() + 8.0
        while dt.datetime.now().timestamp() < deadline:
            if process.poll() is not None:
                break
            if _read_pid(DNS_PID_FILE) == process.pid:
                _write_owned_pid(DNS_PID_FILE, process.pid)
                return process
            import time

            time.sleep(0.05)
        _kill_process_tree(process.pid)
        detail = tail_text(DNS_LOG_FILE, 20)
        raise RuntimeError(detail or "Embedded DNS did not become ready.")

    def start(self, profile_key: str) -> ServerProcessGroup:
        profile = server_profile(profile_key)
        current = server_state()
        if current.running:
            raise ServerAlreadyRunningError(current.pid or 0)
        for path in (SERVER_PID_FILE, BACKUP_PID_FILE):
            pid = _read_pid(path)
            if process_exists(pid):
                raise ServerAlreadyRunningError(pid or 0)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            SERVER_PROFILE_FILE.unlink()
        except FileNotFoundError:
            pass
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "guest_saves").mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "save_backups" / "rolling").mkdir(parents=True, exist_ok=True)
        for path in (SERVER_LOG_FILE, SERVER_ERROR_FILE):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        stdout_handle = SERVER_LOG_FILE.open("a", encoding="utf-8")
        stderr_handle = SERVER_ERROR_FILE.open("a", encoding="utf-8")
        environment = os.environ.copy()
        environment["JPB_PYTHON_EXE"] = self.python
        environment["JPB_BASE_DIR"] = str(BASE_DIR)
        environment["JPB_MANIFEST_FILE"] = os.path.join("config", profile.manifest_name)
        environment["JPB_CACHE_DIRS"] = ";".join(profile.cache_dir_names)
        environment["JPB_TOURNAMENT_DIFF_LOG"] = "1"
        # The original Android client applies restored premium currency through
        # the mailbox gift flow. Direct c.gi delivery is retained in the server
        # as an opt-in diagnostic mode, but is not the launcher default.
        environment["JPB_HARDCASH_DELIVERY"] = "mail"
        for name in ("JPB_PROMO_PROBE", "JPB_TOURNAMENT_TIME_SCALE", "JPB_TOURNAMENT_COOLDOWN_SECONDS"):
            environment.pop(name, None)
        server = None
        backup = None
        try:
            server = subprocess.Popen(
                [self.python, str(profile.server_script)],
                cwd=BASE_DIR,
                env=environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=CREATE_NO_WINDOW,
            )
            _write_owned_pid(SERVER_PID_FILE, server.pid)
            SERVER_PROFILE_FILE.write_text(profile.key, encoding="ascii")
            backup = subprocess.Popen(
                [
                    self.python,
                    str(BASE_DIR / "backup_guest_saves.py"),
                    "--save-dir", str(BASE_DIR / "guest_saves"),
                    "--backup-dir", str(BASE_DIR / "save_backups" / "rolling"),
                    "--server-pid", str(server.pid),
                    "--interval", "15",
                    "--retention", "7200",
                ],
                cwd=BASE_DIR,
                env=environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=CREATE_NO_WINDOW,
            )
            _write_owned_pid(BACKUP_PID_FILE, backup.pid)
        except Exception:
            if backup is not None:
                _kill_process_tree(backup.pid)
            if server is not None:
                _kill_process_tree(server.pid)
            for path in (SERVER_PID_FILE, BACKUP_PID_FILE, SERVER_PROFILE_FILE):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            stdout_handle.close()
            stderr_handle.close()
            raise
        return ServerProcessGroup(
            server,
            backup,
            (stdout_handle, stderr_handle),
            profile.key,
        )

    def stop(self) -> None:
        _stop_owned_pid_file(BACKUP_PID_FILE, ("backup_guest_saves.py",))
        _stop_owned_pid_file(
            SERVER_PID_FILE,
            ("current_server.py", "current_server_android.py", "server_current.py"),
        )
        _stop_owned_pid_file(DNS_PID_FILE, ("local_dns.py",))
        for path in (SERVER_PROFILE_FILE,):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
