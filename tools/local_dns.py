#!/usr/bin/env python3
"""Minimal LAN-only DNS forwarder with two exact Dino Server overrides."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path


TARGET_NAMES = {
    "jp-4-9-0-pag.ludia.net",
    "jp-4-9-0-pap.ludia.net",
}
FALLBACK_UPSTREAMS = ("1.1.1.1", "8.8.8.8")
MAX_UDP_PACKET = 4096
MAX_TCP_PACKET = 65535
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IPV4_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


class DnsFormatError(ValueError):
    pass


def parse_question(packet: bytes) -> tuple[int, int, bytes, str, int, int]:
    if len(packet) < 12:
        raise DnsFormatError("short DNS header")
    txid, flags, qdcount, _ancount, _nscount, _arcount = struct.unpack("!6H", packet[:12])
    if qdcount != 1:
        raise DnsFormatError("exactly one question is required")
    labels: list[str] = []
    offset = 12
    while True:
        if offset >= len(packet):
            raise DnsFormatError("truncated qname")
        length = packet[offset]
        offset += 1
        if length == 0:
            break
        if length & 0xC0 or length > 63 or offset + length > len(packet):
            raise DnsFormatError("invalid qname label")
        try:
            labels.append(packet[offset : offset + length].decode("ascii"))
        except UnicodeDecodeError as exc:
            raise DnsFormatError("non-ASCII qname") from exc
        offset += length
    if offset + 4 > len(packet):
        raise DnsFormatError("truncated question")
    qtype, qclass = struct.unpack("!HH", packet[offset : offset + 4])
    question = packet[12 : offset + 4]
    name = ".".join(labels).rstrip(".").casefold()
    return txid, flags, question, name, qtype, qclass


def error_response(packet: bytes, rcode: int = 1) -> bytes:
    txid = packet[:2] if len(packet) >= 2 else b"\0\0"
    request_flags = struct.unpack("!H", packet[2:4])[0] if len(packet) >= 4 else 0
    flags = 0x8000 | (request_flags & 0x0100) | 0x0080 | (rcode & 0xF)
    return txid + struct.pack("!5H", flags, 0, 0, 0, 0)


def override_response(packet: bytes, address: str) -> tuple[bytes | None, str, int]:
    txid, request_flags, question, name, qtype, qclass = parse_question(packet)
    if name not in TARGET_NAMES:
        return None, name, qtype
    flags = 0x8000 | 0x0400 | (request_flags & 0x0100) | 0x0080
    answer = b""
    answer_count = 0
    if qclass == 1 and qtype == 1:
        answer = (
            b"\xC0\x0C"
            + struct.pack("!HHIH", 1, 1, 60, 4)
            + socket.inet_aton(address)
        )
        answer_count = 1
    header = struct.pack("!6H", txid, flags, 1, answer_count, 0, 0)
    return header + question + answer, name, qtype


def _windows_upstreams() -> list[str]:
    if os.name != "nt":
        return []
    script = (
        "Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue|"
        "Where-Object {$_.InterfaceOperationalStatus -eq 'Up'}|"
        "ForEach-Object {$_.ServerAddresses}|"
        "Where-Object {$_}|Select-Object -Unique|ConvertTo-Json -Compress"
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
            timeout=5,
        )
        data = json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else []
        if isinstance(data, str):
            data = [data]
        return [str(item) for item in data if item]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def usable_upstreams(lan_ip: str, explicit: list[str] | None = None) -> list[str]:
    candidates = list(explicit or ()) + _windows_upstreams() + list(FALLBACK_UPSTREAMS)
    blocked = {lan_ip, "127.0.0.1", "0.0.0.0"}
    result: list[str] = []
    for value in candidates:
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            continue
        if parsed.version != 4 or str(parsed) in blocked:
            continue
        if str(parsed) not in result:
            result.append(str(parsed))
    return result


def client_allowed(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or (
            isinstance(parsed, ipaddress.IPv4Address)
            and parsed in IPV4_SHARED_ADDRESS_SPACE
        )
    )


class Runtime:
    def __init__(self, lan_ip: str, upstreams: list[str], log_path: Path, milestone_path: Path):
        self.lan_ip = lan_ip
        self.upstreams = upstreams
        self.log_path = log_path
        self.milestone_path = milestone_path
        self.lock = threading.Lock()

    def log(self, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n"
        with self.lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def milestone(self, key: str) -> None:
        self.milestone_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            try:
                data = json.loads(self.milestone_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                data = {}
            data[key] = True
            data[f"{key}_at"] = int(time.time())
            temporary = self.milestone_path.with_name(
                f".{self.milestone_path.name}.{os.getpid()}.tmp"
            )
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.milestone_path)

    def resolve(self, packet: bytes) -> bytes:
        try:
            response, name, qtype = override_response(packet, self.lan_ip)
        except DnsFormatError as exc:
            self.log(f"malformed query rejected: {exc}")
            return error_response(packet)
        if response is not None:
            self.milestone("dns_query_received")
            self.log(f"override {name} type={qtype} -> {self.lan_ip if qtype == 1 else 'NODATA'}")
            return response
        # Do not write unrelated names to disk: this is intentionally not a
        # browsing-history logger.
        for upstream in self.upstreams:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(1.2)
                    sock.sendto(packet, (upstream, 53))
                    response = sock.recv(MAX_TCP_PACKET)
                    if response:
                        return response
            except OSError:
                continue
        self.log(f"upstream resolution failed type={qtype}")
        return error_response(packet, rcode=2)


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        if len(data) > MAX_UDP_PACKET or not client_allowed(self.client_address[0]):
            return
        response = self.server.runtime.resolve(data)
        if response:
            sock.sendto(response[:MAX_UDP_PACKET], self.client_address)


class _TCPHandler(socketserver.BaseRequestHandler):
    def _read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.request.recv(length - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def handle(self):
        if not client_allowed(self.client_address[0]):
            return
        self.request.settimeout(4.0)
        while True:
            header = self._read_exact(2)
            if len(header) != 2:
                return
            length = struct.unpack("!H", header)[0]
            if length < 12 or length > MAX_TCP_PACKET:
                return
            packet = self._read_exact(length)
            if len(packet) != length:
                return
            response = self.server.runtime.resolve(packet)
            self.request.sendall(struct.pack("!H", len(response)) + response)


class _UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = False
    daemon_threads = True


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


def run_server(bind: str, port: int, runtime: Runtime, ready_file: Path | None = None) -> int:
    udp = _UDPServer((bind, port), _UDPHandler)
    tcp = _TCPServer((bind, port), _TCPHandler)
    udp.runtime = runtime
    tcp.runtime = runtime
    threads = [
        threading.Thread(target=udp.serve_forever, name="dns-udp", daemon=True),
        threading.Thread(target=tcp.serve_forever, name="dns-tcp", daemon=True),
    ]
    stopping = threading.Event()

    def stop(_signum=None, _frame=None):
        stopping.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    for thread in threads:
        thread.start()
    runtime.log(
        f"started UDP/TCP {bind}:{port}; overrides={','.join(sorted(TARGET_NAMES))}; "
        f"upstreams={','.join(runtime.upstreams)}"
    )
    runtime.milestone("dns_started")
    if ready_file:
        ready_file.parent.mkdir(parents=True, exist_ok=True)
        ready_file.write_text(str(os.getpid()), encoding="ascii")
    try:
        while not stopping.wait(0.25):
            if not all(thread.is_alive() for thread in threads):
                return 2
    finally:
        udp.shutdown()
        tcp.shutdown()
        udp.server_close()
        tcp.server_close()
        if ready_file:
            try:
                ready_file.unlink()
            except OSError:
                # The launcher may remove the disposable ready/PID file at
                # the same time while shutting down the process tree.
                pass
        runtime.log("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    parser.add_argument("--lan-ip", required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--upstream", action="append", default=[])
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args(argv)
    try:
        lan_ip = str(ipaddress.IPv4Address(args.lan_ip))
    except ValueError:
        parser.error("--lan-ip must be a valid IPv4 address")
    base_dir = args.base_dir.resolve()
    runtime = Runtime(
        lan_ip,
        usable_upstreams(lan_ip, args.upstream),
        base_dir / "logs" / "dns.log",
        base_dir / "run" / "connection_milestones.json",
    )
    try:
        return run_server(args.bind, args.port, runtime, args.ready_file)
    except OSError as exc:
        runtime.log(f"startup failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
