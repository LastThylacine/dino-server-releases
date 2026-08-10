"""Cross-session protection for shared guest saves.

The game protocol has no reliable merge operation.  This module therefore
allows one live writer per save id, publishes active sessions for the desktop
launcher, and accepts explicit release requests from that launcher.
"""

from __future__ import annotations

import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
import threading
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class SaveFileUnavailableError(RuntimeError):
    """The persisted save could not be read safely during maintenance."""


@contextlib.contextmanager
def save_file_lock(
    save_path: str | os.PathLike[str],
    timeout_seconds: float = 10.0,
    retry_seconds: float = 0.05,
    lock_dir: str | os.PathLike[str] | None = None,
):
    """Hold an OS-released, cross-process lock for one persisted save.

    The companion ``.lock`` file is deliberately stable: removing it while
    another process is waiting could split the lock across two file objects.
    The byte-range/flock itself is released automatically when a process exits.
    """
    path = Path(save_path)
    if lock_dir is None:
        lock_path = path.with_name(f".{path.name}.lock")
    else:
        normalized_path = os.path.normcase(os.path.abspath(os.fspath(path)))
        lock_name = hashlib.sha256(
            normalized_path.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:24]
        lock_path = Path(lock_dir) / f"save-{lock_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        while True:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(max(0.01, float(retry_seconds)))
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _atomic_json_write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class SaveSessionRegistry:
    def __init__(self, run_dir: str | os.PathLike[str], ttl_seconds: float = 180.0):
        self.run_dir = Path(run_dir)
        self.state_path = self.run_dir / "save_sessions.json"
        self.release_requests_path = self.run_dir / "save_session_release_requests.json"
        self.ttl_seconds = max(30.0, float(ttl_seconds))
        self._records: dict[str, dict[str, object]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(save_id: object) -> str:
        return str(save_id or "").strip().lower()

    def _consume_release_requests_locked(self) -> set[str]:
        try:
            data = json.loads(
                self.release_requests_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError, TypeError):
            return set()
        requests = data.get("requests", []) if isinstance(data, dict) else []
        released = {
            self._key(item.get("save_id") if isinstance(item, dict) else item)
            for item in requests
        }
        released.discard("")
        if released:
            for save_key in released:
                self._records.pop(save_key, None)
        try:
            _atomic_json_write(self.release_requests_path, {"requests": []})
        except OSError:
            pass
        return released

    def _purge_stale_locked(self, now: float) -> bool:
        stale = [
            key
            for key, record in self._records.items()
            if now - float(record.get("last_seen", 0.0)) > self.ttl_seconds
        ]
        for key in stale:
            self._records.pop(key, None)
        return bool(stale)

    def _persist_locked(self) -> None:
        sessions = []
        for record in sorted(
            self._records.values(), key=lambda item: str(item.get("save_id", ""))
        ):
            public = {
                key: copy.deepcopy(value)
                for key, value in record.items()
                if not key.startswith("_")
            }
            sessions.append(public)
        _atomic_json_write(
            self.state_path,
            {
                "updated_at": time.time(),
                "ttl_seconds": self.ttl_seconds,
                "sessions": sessions,
            },
        )

    def acquire(
        self,
        save_id: str,
        session_id: str,
        device_id: str = "",
        device_label: str = "",
        client_ip: str = "",
    ) -> tuple[bool, dict[str, object] | None]:
        now = time.time()
        key = self._key(save_id)
        if not key or not session_id:
            return False, None
        with self._lock:
            changed = bool(self._consume_release_requests_locked())
            changed = self._purge_stale_locked(now) or changed
            existing = self._records.get(key)
            if existing and existing.get("session_id") != session_id:
                if changed:
                    self._persist_locked()
                return False, copy.deepcopy(existing)
            if existing:
                existing["last_seen"] = now
                existing["_last_persist"] = now
            else:
                self._records[key] = {
                    "save_id": str(save_id),
                    "session_id": str(session_id),
                    "device_id": str(device_id or ""),
                    "device_label": str(device_label or "primary"),
                    "client_ip": str(client_ip or ""),
                    "connected_at": now,
                    "last_seen": now,
                    "_last_persist": now,
                }
            self._persist_locked()
            return True, None

    def touch(self, save_id: str, session_id: str) -> bool:
        now = time.time()
        key = self._key(save_id)
        with self._lock:
            changed = bool(self._consume_release_requests_locked())
            changed = self._purge_stale_locked(now) or changed
            record = self._records.get(key)
            if not record or record.get("session_id") != session_id:
                if changed:
                    self._persist_locked()
                return False
            record["last_seen"] = now
            last_persist = float(record.get("_last_persist", 0.0))
            if changed or now - last_persist >= 5.0:
                record["_last_persist"] = now
                self._persist_locked()
            return True

    def owns(self, save_id: str, session_id: str) -> bool:
        return self.touch(save_id, session_id)

    def release(self, save_id: str, session_id: str) -> bool:
        key = self._key(save_id)
        with self._lock:
            record = self._records.get(key)
            if not record or record.get("session_id") != session_id:
                return False
            self._records.pop(key, None)
            self._persist_locked()
            return True

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._persist_locked()
