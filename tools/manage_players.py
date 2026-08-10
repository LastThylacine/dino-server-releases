#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manage JPB private server whitelist and linked devices."""

import argparse
import datetime
import json
import os
import sys
import tempfile
import threading
from copy import deepcopy


TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(TOOLS_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jpb_server.save_concurrency import save_file_lock


CONFIG_DIR = os.path.join(BASE_DIR, "config")
DEFAULT_WHITELIST_FILE = os.path.join(CONFIG_DIR, "whitelist.json")
DEFAULT_DEVICE_LINKS_FILE = os.path.join(CONFIG_DIR, "device_links.json")
DEFAULT_GUEST_SAVE_DIR = os.path.join(BASE_DIR, "guest_saves")
DEFAULT_SAVE_BACKUP_DIR = os.path.join(BASE_DIR, "save_backups", "rolling")
DEFAULT_RUN_DIR = os.path.join(BASE_DIR, "run")
HARDCASH_SCHEMA_VERSION = 2
HARDCASH_MAX_BALANCE = 2_147_483_647
HARDCASH_RECOVERY_MARKER_FILE = "wallet_latest.json"
HARDCASH_SIDECAR_KEYS = (
    "hardcash_state",
    "hardcash_schema_version",
    "hardcash_source",
    "hardcash_baseline",
    "hardcash_delivery_base",
    "hardcash_balance_estimate",
    "hardcash_last_good_estimate",
    "hardcash_delta_total",
    "hardcash_mail_delivered_balance",
    "hardcash_mail_last_delivered_balance",
    "hardcash_mail_last_delivered_by_device",
    "reward_transactions",
    "reward_transaction_ids",
)


def load_json(path, default):
    if not os.path.exists(path):
        return deepcopy(default)
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root JSON value must be an object")
    return data


def save_json(path, data):
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    temporary = f"{absolute}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, absolute)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_bytes_atomic(path, payload):
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    temporary = f"{absolute}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, absolute)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def normalize_id(value):
    return str(value or "").strip()


def normalize_key(value):
    return normalize_id(value).lower()


def save_id_for_device(device_id):
    device_id = normalize_id(device_id)
    if not device_id:
        raise ValueError("device id is required")
    return f"D-{device_id}"


def guest_save_path(save_id, save_dir=DEFAULT_GUEST_SAVE_DIR):
    safe_name = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in normalize_id(save_id)
    )
    return os.path.join(os.path.abspath(save_dir), f"{safe_name or 'guest'}.json")


def copy_device_save_for_new_identity(device_id, save_id, save_dir=DEFAULT_GUEST_SAVE_DIR):
    """Seed a new custom save identity without overwriting either save."""
    source_save_id = save_id_for_device(device_id)
    if normalize_key(source_save_id) == normalize_key(save_id):
        return "same_identity"

    source_path = guest_save_path(source_save_id, save_dir)
    target_path = guest_save_path(save_id, save_dir)
    if os.path.exists(target_path):
        return "target_exists"
    if not os.path.isfile(source_path):
        return "source_missing"

    with open(source_path, "rb") as source:
        payload = source.read()
    try:
        parsed = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"source save is not valid JSON: {source_path}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"source save root must be an object: {source_path}")

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    temporary_path = ""
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".save-migration-",
            suffix=".tmp",
            dir=os.path.dirname(target_path),
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        # A hard-link publish is atomic and never replaces an existing target.
        os.link(temporary_path, target_path)
    except FileExistsError:
        return "target_exists"
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    return "copied"


def initialize_guest_save_hardcash(
    save_id,
    balance,
    save_dir=DEFAULT_GUEST_SAVE_DIR,
    backup_dir=DEFAULT_SAVE_BACKUP_DIR,
    run_dir=DEFAULT_RUN_DIR,
):
    """Initialize a legacy wallet from a player-observed balance while offline."""
    save_id = normalize_id(save_id)
    if not save_id:
        raise ValueError("save id is required")
    try:
        balance = int(balance)
    except (TypeError, ValueError) as exc:
        raise ValueError("balance must be a non-negative integer") from exc
    if not 0 <= balance <= HARDCASH_MAX_BALANCE:
        raise ValueError(
            f"balance must be an integer from 0 to {HARDCASH_MAX_BALANCE}"
        )

    server_pid_file = os.path.join(os.path.abspath(run_dir), "jpb-server.pid")
    if os.path.exists(server_pid_file):
        raise ValueError(
            "Dino Server must be stopped before initializing a wallet; "
            "use the launcher Stop action first"
        )

    save_path = guest_save_path(save_id, save_dir)
    if not os.path.isfile(save_path):
        raise ValueError(f"save does not exist: {save_path}")
    with save_file_lock(
        save_path,
        timeout_seconds=0.0,
        lock_dir=run_dir,
    ) as file_lock_acquired:
        if not file_lock_acquired:
            raise ValueError(
                "the save is currently in use; stop Dino Server and try again"
            )
        return _initialize_guest_save_hardcash_locked(
            save_path,
            balance,
            backup_dir,
            server_pid_file,
        )


def _initialize_guest_save_hardcash_locked(
    save_path,
    balance,
    backup_dir,
    server_pid_file,
):
    try:
        with open(save_path, "rb") as handle:
            original_bytes = handle.read()
    except FileNotFoundError as exc:
        raise ValueError(f"save does not exist: {save_path}") from exc
    try:
        data = json.loads(original_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"save is not valid JSON: {save_path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"save root must be an object: {save_path}")
    meta = data.setdefault("__meta", {})
    if not isinstance(meta, dict):
        raise ValueError(f"save __meta must be an object: {save_path}")

    backup_save_dir = os.path.join(
        os.path.abspath(backup_dir),
        os.path.splitext(os.path.basename(save_path))[0],
    )
    os.makedirs(backup_save_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    backup_path = os.path.join(
        backup_save_dir,
        f"manual_wallet_preinit_{stamp}.json",
    )
    descriptor = os.open(
        backup_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(backup_path)
        except FileNotFoundError:
            pass
        raise

    for key in HARDCASH_SIDECAR_KEYS:
        meta.pop(key, None)
    meta.update({
        "hardcash_state": "known",
        "hardcash_schema_version": HARDCASH_SCHEMA_VERSION,
        "hardcash_source": "manual_observation",
        "hardcash_baseline": balance,
        "hardcash_delivery_base": 3,
        "hardcash_balance_estimate": balance,
        "hardcash_last_good_estimate": balance,
        "hardcash_delta_total": 0,
        "reward_transactions": [],
        "reward_transaction_ids": [],
    })
    # Re-check immediately before the atomic publish. A running server could
    # otherwise race this offline migration and overwrite newer progress.
    if os.path.exists(server_pid_file):
        raise ValueError(
            "Dino Server started during wallet initialization; "
            "the save was not changed"
        )
    try:
        with open(save_path, "rb") as handle:
            current_bytes = handle.read()
    except FileNotFoundError as exc:
        raise ValueError(
            "the save disappeared during wallet initialization"
        ) from exc
    if current_bytes != original_bytes:
        raise ValueError(
            "the save changed during wallet initialization; "
            "the save was not changed by this command"
        )
    save_json(save_path, data)
    marker_meta = {
        key: deepcopy(meta[key])
        for key in HARDCASH_SIDECAR_KEYS
        if key in meta
    }
    marker_path = os.path.join(
        backup_save_dir,
        HARDCASH_RECOVERY_MARKER_FILE,
    )
    try:
        save_json(marker_path, {"__meta": marker_meta})
    except OSError as exc:
        try:
            save_bytes_atomic(save_path, original_bytes)
        except OSError as rollback_exc:
            raise RuntimeError(
                "trusted wallet marker failed and the save rollback also "
                f"failed; recover from {backup_path}"
            ) from rollback_exc
        raise RuntimeError(
            "trusted wallet marker failed; the original save was restored "
            f"from memory and remains backed up at {backup_path}"
        ) from exc
    return save_path, backup_path


def ensure_list_field(data, key):
    value = data.get(key)
    if isinstance(value, list):
        return value
    if value is None:
        data[key] = []
        return data[key]
    data[key] = [value]
    return data[key]


def ensure_whitelist(path):
    data = load_json(path, {"enabled": True, "allow": []})
    data["enabled"] = bool(data.get("enabled", True))
    ensure_list_field(data, "allow")
    return data


def ensure_device_links(path):
    data = load_json(path, {"players": []})
    ensure_list_field(data, "players")
    data["players"] = [player for player in data["players"] if isinstance(player, dict)]
    return data


def player_name(player):
    return normalize_id(player.get("name")) or "(no name)"


def player_save_id(player):
    return normalize_id(player.get("save_id"))


def player_devices(player):
    devices = player.get("devices")
    if isinstance(devices, dict):
        return devices
    if isinstance(devices, list):
        converted = {}
        for index, device_id in enumerate(devices, start=1):
            if normalize_id(device_id):
                converted[f"device{index}"] = normalize_id(device_id)
        player["devices"] = converted
        return converted
    player["devices"] = {}
    return player["devices"]


def next_device_label(devices):
    index = 1
    while f"device{index}" in devices:
        index += 1
    return f"device{index}"


def find_player_by_save_id(device_links, save_id):
    wanted = normalize_key(save_id)
    for player in device_links.get("players", []):
        if normalize_key(player_save_id(player)) == wanted:
            return player
    return None


def find_player_by_device(device_links, device_id):
    wanted = normalize_key(device_id)
    for player in device_links.get("players", []):
        for label, current in player_devices(player).items():
            if normalize_key(current) == wanted:
                return player, label
    return None, ""


def whitelist_allow_entries(whitelist):
    return ensure_list_field(whitelist, "allow")


def upsert_whitelist_entry(whitelist, name, device_id, save_id, discord="", notes="", enabled=True):
    whitelist["enabled"] = bool(enabled)
    allow = whitelist_allow_entries(whitelist)
    wanted_save_id = normalize_key(save_id)
    wanted_device_id = normalize_key(device_id)
    for entry in allow:
        if not isinstance(entry, dict):
            continue
        if normalize_key(entry.get("save_id")) == wanted_save_id or normalize_key(entry.get("device_id")) == wanted_device_id:
            if name:
                entry["name"] = name
            if device_id:
                entry["device_id"] = device_id
            entry["save_id"] = save_id
            if discord:
                entry["discord"] = discord
            if notes:
                entry["notes"] = notes
            return entry
    entry = {
        "name": name or save_id,
        "device_id": device_id,
        "save_id": save_id,
    }
    if discord:
        entry["discord"] = discord
    if notes:
        entry["notes"] = notes
    allow.append(entry)
    return entry


def whitelist_has_save_id(whitelist, save_id):
    wanted = normalize_key(save_id)
    for value in ensure_list_field(whitelist, "allow_save_ids"):
        if normalize_key(value) == wanted:
            return True
    for entry in whitelist_allow_entries(whitelist):
        if isinstance(entry, str) and normalize_key(entry) == wanted:
            return True
        if isinstance(entry, dict) and normalize_key(entry.get("save_id")) == wanted:
            return True
    return False


def remove_whitelist_save_id(whitelist, save_id):
    wanted = normalize_key(save_id)
    whitelist["allow"] = [
        entry for entry in whitelist_allow_entries(whitelist)
        if not (
            (isinstance(entry, str) and normalize_key(entry) == wanted)
            or (isinstance(entry, dict) and normalize_key(entry.get("save_id")) == wanted)
        )
    ]
    whitelist["allow_save_ids"] = [
        value for value in ensure_list_field(whitelist, "allow_save_ids")
        if normalize_key(value) != wanted
    ]


def set_save_denied(whitelist, save_id, denied):
    wanted = normalize_key(save_id)
    values = ensure_list_field(whitelist, "deny_save_ids")
    values = [
        value for value in values
        if normalize_key(value) != wanted
    ]
    if denied and wanted:
        values.append(normalize_id(save_id))
    whitelist["deny_save_ids"] = values


def cmd_list(args, whitelist, device_links):
    print(f"whitelist enabled: {bool(whitelist.get('enabled', False))}")
    players = device_links.get("players", [])
    if not players:
        print("device_links players: none")
    for player in players:
        devices = player_devices(player)
        parts = [f"{label}={device_id}" for label, device_id in sorted(devices.items())]
        marker = "whitelisted" if whitelist_has_save_id(whitelist, player_save_id(player)) else "not-in-whitelist"
        print(f"{player_name(player)} | {player_save_id(player)} | {marker} | " + " | ".join(parts))

    linked_save_ids = {normalize_key(player_save_id(player)) for player in players}
    extra = []
    for entry in whitelist_allow_entries(whitelist):
        if isinstance(entry, dict):
            save_id = normalize_id(entry.get("save_id"))
            if save_id and normalize_key(save_id) not in linked_save_ids:
                extra.append((entry.get("name") or save_id, save_id, entry.get("device_id") or ""))
    if extra:
        print("")
        print("whitelist-only entries:")
        for name, save_id, device_id in extra:
            print(f"{name} | {save_id} | device_id={device_id}")


def cmd_add(args, whitelist, device_links):
    device_id = normalize_id(args.device)
    save_id = normalize_id(args.save_id) or save_id_for_device(device_id)
    existing, label = find_player_by_device(device_links, device_id)
    if existing and normalize_key(player_save_id(existing)) != normalize_key(save_id):
        raise ValueError(f"device already belongs to {player_save_id(existing)} as {label}")

    player = find_player_by_save_id(device_links, save_id)
    if player is None:
        migration = copy_device_save_for_new_identity(device_id, save_id)
        if migration == "copied":
            print(
                f"copied existing device save {save_id_for_device(device_id)} "
                f"to new linked save {save_id}; source preserved"
            )
        elif migration == "target_exists":
            print(
                f"using existing linked save {save_id}; "
                f"device save {save_id_for_device(device_id)} was not overwritten or removed"
            )
    if player is None:
        player = {"name": args.name or save_id, "save_id": save_id, "devices": {}}
        device_links["players"].append(player)
    elif args.name:
        player["name"] = args.name

    devices = player_devices(player)
    if not any(normalize_key(value) == normalize_key(device_id) for value in devices.values()):
        devices[next_device_label(devices)] = device_id

    set_save_denied(whitelist, save_id, False)
    upsert_whitelist_entry(
        whitelist,
        args.name or player_name(player),
        device_id,
        save_id,
        discord=args.discord,
        notes=args.notes,
        enabled=not args.no_enable_whitelist,
    )
    print(f"added/updated {args.name or player_name(player)} | {save_id} | device={device_id}")


def cmd_add_device(args, whitelist, device_links):
    save_id = normalize_id(args.save_id)
    device_id = normalize_id(args.device)
    if not save_id:
        raise ValueError("--save-id is required")
    if not device_id:
        raise ValueError("--device is required")
    existing, label = find_player_by_device(device_links, device_id)
    if existing and normalize_key(player_save_id(existing)) != normalize_key(save_id):
        raise ValueError(f"device already belongs to {player_save_id(existing)} as {label}")

    player = find_player_by_save_id(device_links, save_id)
    if player is None:
        player = {"name": args.name or save_id, "save_id": save_id, "devices": {}}
        device_links["players"].append(player)
    elif args.name:
        player["name"] = args.name

    devices = player_devices(player)
    if any(normalize_key(value) == normalize_key(device_id) for value in devices.values()):
        print(f"device already linked to {save_id}")
        return
    label = next_device_label(devices)
    devices[label] = device_id
    if not whitelist_has_save_id(whitelist, save_id):
        upsert_whitelist_entry(whitelist, args.name or player_name(player), "", save_id, enabled=True)
    print(f"linked {label} to {save_id}")


def cmd_remove_device(args, whitelist, device_links):
    player, label = find_player_by_device(device_links, args.device)
    if player is None:
        raise ValueError("device not found")
    devices = player_devices(player)
    removed = devices.pop(label)
    print(f"removed {label}={removed} from {player_save_id(player)}")


def cmd_remove_player(args, whitelist, device_links):
    save_id = normalize_id(args.save_id)
    if not save_id:
        raise ValueError("--save-id is required")
    before = len(device_links.get("players", []))
    device_links["players"] = [
        player for player in device_links.get("players", [])
        if normalize_key(player_save_id(player)) != normalize_key(save_id)
    ]
    remove_whitelist_save_id(whitelist, save_id)
    if len(device_links["players"]) == before:
        print(f"player {save_id} was not linked; whitelist entry removed if it existed")
    else:
        print(f"removed player {save_id}")


def cmd_enable(args, whitelist, device_links):
    save_id = normalize_id(args.save_id)
    player = find_player_by_save_id(device_links, save_id)
    device_id = ""
    name = save_id
    if player:
        name = player_name(player)
        devices = player_devices(player)
        device_id = next(iter(devices.values()), "")
    set_save_denied(whitelist, save_id, False)
    upsert_whitelist_entry(whitelist, name, device_id, save_id, enabled=True)
    print(f"enabled {save_id}")


def cmd_disable(args, whitelist, device_links):
    remove_whitelist_save_id(whitelist, args.save_id)
    set_save_denied(whitelist, args.save_id, True)
    print(f"disabled {args.save_id}")


def validate_data(whitelist, device_links):
    errors = []
    warnings = []
    seen_devices = {}
    for player in device_links.get("players", []):
        save_id = player_save_id(player)
        if not save_id:
            errors.append(f"player {player_name(player)} has empty save_id")
            continue
        for label, device_id in player_devices(player).items():
            if not normalize_id(device_id):
                errors.append(f"{save_id} has empty {label}")
                continue
            key = normalize_key(device_id)
            if key in seen_devices:
                errors.append(f"device {device_id} is duplicated in {seen_devices[key]} and {save_id}/{label}")
            else:
                seen_devices[key] = f"{save_id}/{label}"
        if whitelist.get("enabled") and not whitelist_has_save_id(whitelist, save_id):
            warnings.append(f"{save_id} is linked but not whitelisted")
    return errors, warnings


def cmd_validate(args, whitelist, device_links):
    errors, warnings = validate_data(whitelist, device_links)
    if errors:
        print("validation failed:")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    if warnings:
        print("validation ok, with warnings:")
        for warning in warnings:
            print(f"- {warning}")
        return
    print("validation ok")


def cmd_init_hardcash(args):
    save_path, backup_path = initialize_guest_save_hardcash(
        args.save_id,
        args.balance,
        args.save_dir,
        args.backup_dir,
        args.run_dir,
    )
    print(f"initialized trusted Bucks balance for {args.save_id}: {args.balance}")
    print(f"save: {save_path}")
    print(f"byte-exact backup: {backup_path}")


def build_parser():
    parser = argparse.ArgumentParser(description="Manage JPB whitelist.json and device_links.json")
    parser.add_argument("--whitelist", default=DEFAULT_WHITELIST_FILE)
    parser.add_argument("--device-links", default=DEFAULT_DEVICE_LINKS_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="show players and whitelist status")
    subparsers.add_parser("validate", help="check JSON consistency")

    add = subparsers.add_parser("add", help="add or update a player with the first device")
    add.add_argument("--name", required=True)
    add.add_argument("--device", required=True)
    add.add_argument("--save-id", default="")
    add.add_argument("--discord", default="")
    add.add_argument("--notes", default="")
    add.add_argument("--no-enable-whitelist", action="store_true")

    add_device = subparsers.add_parser("add-device", help="link another device to an existing save_id")
    add_device.add_argument("--save-id", required=True)
    add_device.add_argument("--device", required=True)
    add_device.add_argument("--name", default="")

    remove_device = subparsers.add_parser("remove-device", help="remove a linked device id")
    remove_device.add_argument("--device", required=True)

    remove_player = subparsers.add_parser("remove-player", help="remove a player from device links and whitelist")
    remove_player.add_argument("--save-id", required=True)

    enable = subparsers.add_parser("enable", help="add save_id to whitelist")
    enable.add_argument("--save-id", required=True)

    disable = subparsers.add_parser("disable", help="remove save_id from whitelist")
    disable.add_argument("--save-id", required=True)

    init_hardcash = subparsers.add_parser(
        "init-hardcash",
        help="initialize a legacy Bucks balance from an observed client value",
    )
    init_hardcash.add_argument("--save-id", required=True)
    init_hardcash.add_argument("--balance", required=True, type=int)
    init_hardcash.add_argument("--save-dir", default=DEFAULT_GUEST_SAVE_DIR)
    init_hardcash.add_argument("--backup-dir", default=DEFAULT_SAVE_BACKUP_DIR)
    init_hardcash.add_argument("--run-dir", default=DEFAULT_RUN_DIR)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "init-hardcash":
        cmd_init_hardcash(args)
        return
    whitelist = ensure_whitelist(args.whitelist)
    device_links = ensure_device_links(args.device_links)

    commands = {
        "list": cmd_list,
        "add": cmd_add,
        "add-device": cmd_add_device,
        "remove-device": cmd_remove_device,
        "remove-player": cmd_remove_player,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "validate": cmd_validate,
    }
    commands[args.command](args, whitelist, device_links)

    if args.command not in ("list", "validate"):
        save_json(args.whitelist, whitelist)
        save_json(args.device_links, device_links)
        errors, warnings = validate_data(whitelist, device_links)
        if errors:
            print("")
            print("saved, but validation has errors:")
            for error in errors:
                print(f"- {error}")
        elif warnings:
            print("")
            print("saved, with validation warnings:")
            for warning in warnings:
                print(f"- {warning}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
