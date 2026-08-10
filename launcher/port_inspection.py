"""Read-only TCP/UDP listener and process-owner inspection."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class PortOwner:
    port: int
    protocol: str
    pid: int | None
    process_name: str = ""
    executable: str = ""
    command_line: str = ""

    @property
    def occupied(self) -> bool:
        return self.pid is not None

    @property
    def display_name(self) -> str:
        return self.process_name or (f"PID {self.pid}" if self.pid else "unknown process")


def _powershell_json(script: str, timeout: float = 5.0):
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
        if result.returncode or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _process_image_path(pid: int | None) -> str:
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


def _same_path(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    try:
        return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
            os.path.abspath(os.fspath(right))
        )
    except (OSError, TypeError, ValueError):
        return False


def command_line_references_installation(
    command_line: str,
    base_dir: Path,
    tokens: tuple[str, ...],
) -> bool:
    """Match a known program argument under this exact installation root."""
    if not command_line:
        return False
    try:
        arguments = shlex.split(command_line, posix=False)
    except ValueError:
        return False
    expected_relative_paths = {
        "local_dns.py": (Path("tools") / "local_dns.py",),
        "current_server.py": (Path("jpb_server") / "current_server.py",),
        "current_server_android.py": (
            Path("jpb_server") / "current_server_android.py",
        ),
        "server_current.py": (Path("server_current.py"),),
        "backup_guest_saves.py": (Path("backup_guest_saves.py"),),
        "dinoserver.exe": (Path("DinoServer.exe"),),
    }
    wanted_names = {Path(token).name.casefold() for token in tokens}
    for raw_argument in arguments:
        argument = raw_argument
        if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in "\"'":
            argument = argument[1:-1]
        candidate = Path(argument)
        if candidate.name.casefold() not in wanted_names:
            continue
        if not candidate.is_absolute():
            # A relative script name cannot prove the process working
            # directory, so treating it as relative to our installation could
            # claim and terminate an unrelated reused PID.
            continue
        relative_paths = expected_relative_paths.get(
            candidate.name.casefold(),
            (Path(candidate.name),),
        )
        if any(_same_path(candidate, base_dir / relative) for relative in relative_paths):
            return True
    return False


def _owner_details(pid: int | None) -> tuple[str, str, str]:
    if os.name != "nt" or not pid:
        return "", "", ""
    data = _powershell_json(
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
        + str(int(pid))
        + "\" -ErrorAction SilentlyContinue;"
        "if($p){[pscustomobject]@{Name=$p.Name;Executable=$p.ExecutablePath;"
        "CommandLine=$p.CommandLine}|ConvertTo-Json -Compress}"
    )
    if not isinstance(data, dict):
        executable = _process_image_path(pid)
        return Path(executable).name if executable else "", executable, ""
    executable = str(data.get("Executable") or "") or _process_image_path(pid)
    return (
        str(data.get("Name") or "") or (Path(executable).name if executable else ""),
        executable,
        str(data.get("CommandLine") or ""),
    )


def inspect_port(port: int, protocol: str) -> PortOwner:
    """Return a listener owner. A pid of -1 means occupied but unattributed."""
    protocol = protocol.lower()
    if protocol not in {"tcp", "udp"}:
        raise ValueError("protocol must be tcp or udp")
    pid = None
    if os.name == "nt":
        command = (
            f"$x=Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
            "-ErrorAction SilentlyContinue|Select-Object -First 1;"
            if protocol == "tcp"
            else f"$x=Get-NetUDPEndpoint -LocalPort {int(port)} "
            "-ErrorAction SilentlyContinue|Select-Object -First 1;"
        )
        data = _powershell_json(
            command
            + "if($x){[pscustomobject]@{Pid=$x.OwningProcess}|ConvertTo-Json -Compress}"
        )
        if isinstance(data, dict):
            try:
                pid = int(data.get("Pid"))
            except (TypeError, ValueError):
                pid = -1
    if pid is None and not _can_bind(port, protocol):
        pid = -1
    name, executable, command_line = _owner_details(pid if pid and pid > 0 else None)
    return PortOwner(port, protocol, pid, name, executable, command_line)


def _can_bind(port: int, protocol: str, host: str = "0.0.0.0") -> bool:
    sock_type = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
    sock = socket.socket(socket.AF_INET, sock_type)
    try:
        if protocol == "tcp":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def inspect_required_ports() -> list[PortOwner]:
    return [
        inspect_port(53, "udp"),
        inspect_port(53, "tcp"),
        inspect_port(80, "tcp"),
        inspect_port(9933, "tcp"),
        inspect_port(9943, "tcp"),
    ]


def belongs_to_installation(owner: PortOwner, base_dir: Path) -> bool:
    if not owner.occupied:
        return False
    if command_line_references_installation(
        owner.command_line,
        base_dir,
        (
            "local_dns.py",
            "current_server.py",
            "current_server_android.py",
            "dinoserver.exe",
        ),
    ):
        return True
    expected_executable = (
        Path(sys.executable)
        if getattr(sys, "frozen", False)
        else base_dir / "DinoServer.exe"
    )
    return bool(owner.executable and _same_path(owner.executable, expected_executable))


def describe_owner(owner: PortOwner) -> str:
    if not owner.occupied:
        return "free"
    name = owner.display_name
    suffix = f" (PID {owner.pid})" if owner.pid and owner.pid > 0 and "PID" not in name else ""
    if "adguard" in (name + owner.executable + owner.command_line).casefold():
        name = "AdGuard Home"
    return f"{name}{suffix}"
