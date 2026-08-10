"""Safe GitHub Releases update discovery and package validation."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable


GITHUB_OWNER = "LastThylacine"
GITHUB_REPOSITORY = "dino-server-releases"
LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
LATEST_RELEASE_PAGE = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases/latest"
)
USER_AGENT = "DinoServer-Updater/1"
MAX_API_BYTES = 1_000_000
MAX_CHECKSUM_BYTES = 8_192
MAX_ARCHIVE_BYTES = 250_000_000
MAX_EXTRACTED_BYTES = 400_000_000
UPDATE_ROOT = "DinoServerUpdate"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_ROOT_FILES = {
    "DinoServer.exe",
    "server_current.py",
    "backup_guest_saves.py",
    "README_FIRST.txt",
    "LOCAL_SERVER_README.txt",
    "CACHE_SETUP.txt",
    "THIRD_PARTY_NOTICES.md",
    "CHANGELOG.md",
    "OPEN_LAN_FIREWALL.bat",
}
_CONFIG_FILES = {
    "config/cache_index_ios.json",
    "config/cache_index_android.json",
    "config/onlineoptions",
    "config/offer_rotation.json",
}
_PREFIX_EXTENSIONS = {
    "launcher/": {".py"},
    "jpb_server/": {".py"},
    "tools/": {".py", ".ps1"},
    "assets/guide/": {".png", ".jpg", ".jpeg", ".webp"},
    "assets/icons/": {".png", ".svg", ".txt"},
    "icons/": {".png", ".ico"},
}
_FORBIDDEN_PARTS = {
    "cache_ios",
    "cache_android",
    "guest_saves",
    "save_backups",
    "logs",
    "run",
    "__pycache__",
}
_FORBIDDEN_NAMES = {
    "local_settings.json",
    "device_links.json",
    "whitelist.json",
    "fixed_manifest.json",
    "fixed_manifest_ios.json",
    "fixed_manifest_android.json",
    "save_sessions.json",
    "save_session_release_requests.json",
    "jpb-server.pid",
    "jpb-backup.pid",
    "dino-dns.pid",
}


class UpdateError(RuntimeError):
    """A safe, user-displayable updater failure."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    digest: str = ""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    title: str
    notes: str
    page_url: str
    package: ReleaseAsset
    checksum: ReleaseAsset


@dataclass(frozen=True)
class DownloadedUpdate:
    release: ReleaseInfo
    package_path: Path
    sha256: str
    manifest: dict


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise UpdateError(f"Unsupported release version: {value!r}")
    return tuple(int(part) for part in match.groups())


def is_newer_version(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _request(url: str) -> urllib.request.Request:
    if not url.lower().startswith("https://"):
        raise UpdateError("Update downloads must use HTTPS.")
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _read_limited(response, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("The update service returned too much data.")
    return data


def _open(opener, request: urllib.request.Request, timeout: float):
    try:
        return opener(request, timeout=timeout)
    except TypeError:
        return opener(request)


def _asset_from_json(value: object) -> ReleaseAsset:
    if not isinstance(value, dict):
        raise UpdateError("The GitHub release contains an invalid asset.")
    name = str(value.get("name") or "")
    url = str(value.get("browser_download_url") or "")
    size = int(value.get("size") or 0)
    digest = str(value.get("digest") or "")
    if not name or not url.lower().startswith("https://") or size <= 0:
        raise UpdateError("The GitHub release asset is incomplete.")
    return ReleaseAsset(name=name, url=url, size=size, digest=digest)


def check_for_update(
    current_version: str,
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float = 6.0,
) -> ReleaseInfo | None:
    """Return a newer stable release, or None when current is already latest."""
    try:
        with _open(opener, _request(LATEST_RELEASE_API), timeout) as response:
            payload = json.loads(_read_limited(response, MAX_API_BYTES).decode("utf-8"))
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not check GitHub Releases: {exc}") from exc

    if not isinstance(payload, dict):
        raise UpdateError("GitHub returned an invalid release response.")
    if payload.get("draft") or payload.get("prerelease"):
        return None

    tag = str(payload.get("tag_name") or "")
    version = tag[1:] if tag.lower().startswith("v") else tag
    parse_version(version)
    if not is_newer_version(version, current_version):
        return None

    expected_package = f"DinoServer-Update-v{version}.zip"
    expected_checksum = f"{expected_package}.sha256"
    assets = {
        str(asset.get("name") or ""): asset
        for asset in payload.get("assets", ())
        if isinstance(asset, dict)
    }
    if expected_package not in assets or expected_checksum not in assets:
        raise UpdateError(
            f"Release v{version} is missing {expected_package} or its SHA-256 file."
        )

    return ReleaseInfo(
        version=version,
        tag=tag,
        title=str(payload.get("name") or tag),
        notes=str(payload.get("body") or "").strip(),
        page_url=str(payload.get("html_url") or LATEST_RELEASE_PAGE),
        package=_asset_from_json(assets[expected_package]),
        checksum=_asset_from_json(assets[expected_checksum]),
    )


def is_allowed_payload_path(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or path.is_absolute()
        or ".." in path.parts
    ):
        return False
    lowered_parts = {part.casefold() for part in path.parts}
    if lowered_parts & _FORBIDDEN_PARTS:
        return False
    if path.name.casefold() in _FORBIDDEN_NAMES:
        return False
    normalized = path.as_posix()
    if normalized in _ROOT_FILES or normalized in _CONFIG_FILES:
        return True
    for prefix, extensions in _PREFIX_EXTENSIONS.items():
        if normalized.startswith(prefix) and path.suffix.casefold() in extensions:
            return True
    return False


def validate_update_archive(package_path: Path, expected_version: str) -> dict:
    """Validate layout, allow-list, declared sizes, and every payload hash."""
    try:
        archive_size = package_path.stat().st_size
    except OSError as exc:
        raise UpdateError(f"Downloaded update is unavailable: {exc}") from exc
    if archive_size <= 0 or archive_size > MAX_ARCHIVE_BYTES:
        raise UpdateError("The downloaded update has an invalid size.")

    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            files: dict[str, zipfile.ZipInfo] = {}
            total_size = 0
            for info in archive.infolist():
                name = info.filename
                if "\\" in name:
                    raise UpdateError("The update archive contains an unsafe path.")
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise UpdateError("The update archive contains an unsafe path.")
                if info.external_attr >> 16 & 0o170000 == 0o120000:
                    raise UpdateError("The update archive contains a symbolic link.")
                if info.is_dir():
                    continue
                total_size += info.file_size
                if total_size > MAX_EXTRACTED_BYTES:
                    raise UpdateError("The update archive expands beyond the safe limit.")
                files[name] = info

            manifest_name = f"{UPDATE_ROOT}/update_manifest.json"
            if manifest_name not in files:
                raise UpdateError("The update manifest is missing.")
            manifest = json.loads(
                _read_limited(archive.open(files[manifest_name]), MAX_API_BYTES).decode("utf-8-sig")
            )
            if not isinstance(manifest, dict) or manifest.get("version") != expected_version:
                raise UpdateError("The update manifest version does not match the release.")
            declared = manifest.get("files")
            if not isinstance(declared, list) or not declared:
                raise UpdateError("The update manifest has no payload.")

            expected_members: set[str] = {manifest_name}
            seen_paths: set[str] = set()
            for item in declared:
                if not isinstance(item, dict):
                    raise UpdateError("The update manifest contains an invalid file entry.")
                relative = str(item.get("path") or "")
                digest = str(item.get("sha256") or "").lower()
                size = int(item.get("size") or -1)
                if relative in seen_paths or not is_allowed_payload_path(relative):
                    raise UpdateError(f"Forbidden update payload path: {relative}")
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise UpdateError(f"Invalid SHA-256 for update payload: {relative}")
                member_name = f"{UPDATE_ROOT}/payload/{relative}"
                info = files.get(member_name)
                if info is None or info.file_size != size:
                    raise UpdateError(f"Update payload size mismatch: {relative}")
                with archive.open(info) as source:
                    actual = hashlib.sha256(source.read()).hexdigest()
                if actual != digest:
                    raise UpdateError(f"Update payload hash mismatch: {relative}")
                expected_members.add(member_name)
                seen_paths.add(relative)

            if set(files) != expected_members:
                unexpected = sorted(set(files) - expected_members)
                raise UpdateError(
                    f"The update archive contains undeclared files: {unexpected[0]}"
                )
            return manifest
    except UpdateError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise UpdateError(f"The downloaded update archive is invalid: {exc}") from exc


def download_update(
    release: ReleaseInfo,
    *,
    destination_dir: Path | None = None,
    opener: Callable = urllib.request.urlopen,
    timeout: float = 30.0,
    progress: Callable[[int, int], None] | None = None,
) -> DownloadedUpdate:
    """Download, SHA-256 verify, and inspect an update package."""
    destination = destination_dir or Path(
        tempfile.mkdtemp(prefix=f"DinoServer-Update-v{release.version}-")
    )
    destination.mkdir(parents=True, exist_ok=True)
    package_path = destination / release.package.name

    try:
        with _open(opener, _request(release.checksum.url), timeout) as response:
            checksum_text = _read_limited(response, MAX_CHECKSUM_BYTES).decode("ascii")
        match = _SHA256_RE.search(checksum_text)
        if not match:
            raise UpdateError("The release SHA-256 file is invalid.")
        expected_hash = match.group(1).lower()

        github_digest = release.package.digest.lower()
        if github_digest:
            if not github_digest.startswith("sha256:"):
                raise UpdateError("GitHub reported an unsupported asset digest.")
            if github_digest.removeprefix("sha256:") != expected_hash:
                raise UpdateError("GitHub and release SHA-256 values do not match.")

        written = 0
        hasher = hashlib.sha256()
        with _open(opener, _request(release.package.url), timeout) as response:
            with package_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise UpdateError("The update download exceeded the safe size limit.")
                    output.write(chunk)
                    hasher.update(chunk)
                    if progress:
                        progress(written, release.package.size)
        if written != release.package.size:
            raise UpdateError(
                f"The update download is incomplete ({written}/{release.package.size} bytes)."
            )
        actual_hash = hasher.hexdigest()
        if actual_hash != expected_hash:
            raise UpdateError("The downloaded update failed SHA-256 verification.")
        manifest = validate_update_archive(package_path, release.version)
        return DownloadedUpdate(release, package_path, actual_hash, manifest)
    except Exception:
        try:
            package_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
