#!/usr/bin/env python3
"""Create rolling, change-aware snapshots of JPB guest save files."""

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HARDCASH_RECOVERY_MARKER_FILE = "wallet_latest.json"


def process_is_running(pid):
    if not pid:
        return True
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return (
                bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                and exit_code.value == 259
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def valid_save_bytes(data):
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict)


def snapshot_changed_saves(save_dir, backup_dir, hashes):
    for source in sorted(save_dir.glob("*.json")):
        try:
            data = source.read_bytes()
        except OSError as exc:
            print(f"read failed {source.name}: {exc}", flush=True)
            continue
        if not valid_save_bytes(data):
            print(f"skip invalid JSON {source.name}", flush=True)
            continue

        digest = hashlib.sha256(data).hexdigest()
        if hashes.get(source.name) == digest:
            continue

        destination_dir = backup_dir / source.stem
        destination_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        destination = destination_dir / f"{timestamp}.json"
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        hashes[source.name] = digest
        print(
            f"snapshot {source.name} -> {destination} bytes={len(data)}",
            flush=True,
        )


def prune_expired_snapshots(backup_dir, retention_seconds):
    cutoff = time.time() - retention_seconds
    for save_backup_dir in backup_dir.iterdir() if backup_dir.exists() else ():
        if not save_backup_dir.is_dir():
            continue
        snapshots = []
        for snapshot in save_backup_dir.glob("*.json"):
            if snapshot.name.casefold() == HARDCASH_RECOVERY_MARKER_FILE.casefold():
                continue
            try:
                snapshots.append((snapshot.stat().st_mtime_ns, snapshot.name, snapshot))
            except OSError as exc:
                print(f"prune stat failed {snapshot}: {exc}", flush=True)
        snapshots.sort()
        newest_snapshot = snapshots[-1][2] if snapshots else None

        # The trusted wallet marker has its own lifecycle. Keep it separate from
        # rolling retention, and always retain the newest ordinary snapshot.
        for _modified_ns, _name, snapshot in snapshots:
            try:
                if snapshot != newest_snapshot and snapshot.stat().st_mtime < cutoff:
                    snapshot.unlink()
            except OSError as exc:
                print(f"prune failed {snapshot}: {exc}", flush=True)
        try:
            if not any(save_backup_dir.iterdir()):
                save_backup_dir.rmdir()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=BASE_DIR / "guest_saves",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=BASE_DIR / "save_backups" / "rolling",
    )
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--retention", type=float, default=7200.0)
    parser.add_argument("--server-pid", type=int)
    args = parser.parse_args()

    if args.interval < 1:
        raise SystemExit("--interval must be at least 1 second")
    if args.retention < args.interval:
        raise SystemExit("--retention must be at least one interval")

    args.save_dir.mkdir(parents=True, exist_ok=True)
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    print(
        f"watching {args.save_dir} every {args.interval:g}s; "
        f"retention={args.retention:g}s backup_dir={args.backup_dir}",
        flush=True,
    )

    while process_is_running(args.server_pid):
        snapshot_changed_saves(args.save_dir, args.backup_dir, hashes)
        prune_expired_snapshots(args.backup_dir, args.retention)
        time.sleep(args.interval)
    print(f"server pid {args.server_pid} exited; backup watcher stopping", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("backup watcher stopped", flush=True)
