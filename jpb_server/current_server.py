#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Current Jurassic Park Builder private server.

This is the cleaned working server based on v141.
Old login-probe payloads were removed; the stable Android 4.9.0 path remains.
"""

import argparse
import copy
import datetime
import hashlib
import html
import json
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import threading
import time
import zlib
from itertools import count
from urllib.parse import quote, unquote, urlsplit

try:
    from jpb_server.content_overrides import (
        is_indominus_offer_asset,
        patch_indominus_offer,
    )
    from jpb_server.save_concurrency import (
        SaveFileUnavailableError,
        SaveSessionRegistry,
        save_file_lock,
    )
    from jpb_server.setup_guide import setup_resource
except ModuleNotFoundError:
    from content_overrides import (
        is_indominus_offer_asset,
        patch_indominus_offer,
    )
    from save_concurrency import (
        SaveFileUnavailableError,
        SaveSessionRegistry,
        save_file_lock,
    )
    from setup_guide import setup_resource


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE_DIR = (
    os.path.dirname(MODULE_DIR)
    if os.path.basename(MODULE_DIR).lower() == "jpb_server"
    else MODULE_DIR
)
BASE_DIR = os.path.abspath(os.environ.get("JPB_BASE_DIR") or DEFAULT_BASE_DIR)
CONFIG_DIR = os.path.join(BASE_DIR, "config")
# Original iOS donor-cache default. It can still be overridden without editing:
#   JPB_MANIFEST_FILE=config/fixed_manifest.json
#   JPB_CACHE_DIRS=another_cache_folder
MANIFEST_FILE = os.path.join(BASE_DIR, os.environ.get("JPB_MANIFEST_FILE", os.path.join("config", "fixed_manifest2.json")))
_CACHE_DIRS = os.environ.get("JPB_CACHE_DIRS", "cache_ios")
CACHE_DIRS = [
    os.path.join(BASE_DIR, entry.strip())
    for entry in _CACHE_DIRS.split(";")
    if entry.strip()
]
CACHE_DIR = CACHE_DIRS[0] if CACHE_DIRS else os.path.join(BASE_DIR, "cache_ios")
# Serve LPKG cache files byte-for-byte as stored on disk.
LPKG_REWRITE_VERSION = None
ONLINE_OPTIONS_FILE = os.path.join(CONFIG_DIR, "onlineoptions")
OFFER_ROTATION_FILE = os.environ.get("JPB_OFFER_ROTATION_FILE") or os.path.join(
    CONFIG_DIR, "offer_rotation.json"
)
OFFER_ROTATION_STATE_FILE = os.environ.get("JPB_OFFER_ROTATION_STATE_FILE") or os.path.join(
    CONFIG_DIR, "offer_rotation_state.json"
)
LOG_DIR = os.path.join(BASE_DIR, "logs")
PLAYER_LOG_DIR = os.path.join(LOG_DIR, "players")
PLAYER_SESSION_LOG_DIR = os.path.join(PLAYER_LOG_DIR, "sessions")
SAVE_DIR = os.path.join(BASE_DIR, "guest_saves")
SAVE_CONFLICT_DIR = os.path.join(SAVE_DIR, "conflicts")
ROLLING_SAVE_BACKUP_DIR = os.path.join(BASE_DIR, "save_backups", "rolling")
RUN_DIR = os.path.join(BASE_DIR, "run")
WHITELIST_FILE = os.environ.get("JPB_WHITELIST_FILE") or os.path.join(CONFIG_DIR, "whitelist.json")
DEVICE_LINKS_FILE = os.environ.get("JPB_DEVICE_LINKS_FILE") or os.path.join(CONFIG_DIR, "device_links.json")
SAVE_SESSION_TTL_SECONDS = float(os.environ.get("JPB_SAVE_SESSION_TTL_SECONDS", "180"))

DEFAULT_HOST = "0.0.0.0"
DEFAULT_HTTP_PORTS = (80, 9943)
DEFAULT_SFS_PORT = 9933
STATIC_CACHE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
SFS_SERVER_KEEPALIVE_SECONDS = 2.0
PAIRING_ID_TTL_SECONDS = 15 * 60
PAIRING_IDS_BY_IP = {}
PAIRING_IDS_LOCK = threading.Lock()
OFFER_ROTATION_STATE_LOCK = threading.Lock()

T_NULL = 0
T_BOOL = 1
T_BYTE = 2
T_SHORT = 3
T_INT = 4
T_LONG = 5
T_FLOAT = 6
T_DOUBLE = 7
T_UTF = 8
T_BOOL_ARR = 9
T_BYTE_ARR = 10
T_SHORT_ARR = 11
T_INT_ARR = 12
T_LONG_ARR = 13
T_FLOAT_ARR = 14
T_DOUBLE_ARR = 15
T_UTF_ARR = 16
T_SFS_ARRAY = 17
T_SFS_OBJECT = 18

CLIENT_CLOSED = "client closed connection"
DEFAULT_RANDOM_VISIT_OWNER_ID = 8801492
BATTLE_RANDOM_BOT_ID_BASE = 4_900_000
BATTLE_RANDOM_BOT_MAX = 10
BATTLE_RANDOM_BOT_ACCOUNT_PREFIX = "jpb-bot"
BATTLE_CONTEXT_KEYS = ("96", "97", "98", "99", "100", "101", "102", "103", "118")
BATTLE_STARTUP_BATL_STARRAY_PIGGYBACK_ENABLED = True
BATTLE_SYNTHETIC_WARMUP_ENABLED = False
IAP_MARKET_ITEM_ID_KEYS = tuple(f"MARKET_ITEM_ID_{index}" for index in range(1, 10))
IAP_HARDCASH_PACK_KEYS = tuple(f"HARDCASH_PACK_{index}" for index in range(1, 7))
IAP_GPLAY_STORE_NAME = "GPLAY"
IAP_DEFAULT_CATALOG_PRICE = 0
IAP_DEFAULT_CATALOG_PRICE_TEXT = "Unavailable"
BATTLE_TOURNAMENT_DIRECT_NO_REPLY_COMMANDS = {
    "c.bl",
    "c.blc",
    "c.ble",
    "c.bli",
    "c.blj",
    "c.bll",
    "c.bllv",
    "c.blrd",
    "c.blrj",
    "c.blrl",
    "c.bls",
    "c.blsr",
    "c.blu",
    "c.t",
    "c.td",
    "c.tf",
    "c.to",
    "c.tr",
    "c.tw",
}
ONLINE_OPTIONS_VERSION = 20260715
# Force clients to refresh onlineoptions after each server restart. The value
# remains stable for the lifetime of the process, so it does not cause repeated
# downloads while players are connected.
ONLINE_OPTIONS_BOOT_VERSION = int(time.time())

_TOURNAMENT_TIME_SCALE_ENV = os.environ.get("JPB_TOURNAMENT_TIME_SCALE", "1").strip()
try:
    TOURNAMENT_TIME_SCALE = max(1.0, min(float(_TOURNAMENT_TIME_SCALE_ENV), 60.0))
except ValueError:
    TOURNAMENT_TIME_SCALE = 1.0
_NETWORK_TIME_REAL_ANCHOR = time.monotonic()
_NETWORK_TIME_GAME_ANCHOR_MS = int(time.time() * 1000)
PROMO_SCHEDULER_PROBE_ENABLED = os.environ.get("JPB_PROMO_PROBE", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

_TOURNAMENT_COOLDOWN_ENV = os.environ.get("JPB_TOURNAMENT_COOLDOWN_SECONDS", "").strip()
try:
    TOURNAMENT_COOLDOWN_LIMIT_MS = (
        max(1, int(float(_TOURNAMENT_COOLDOWN_ENV))) * 1000
        if _TOURNAMENT_COOLDOWN_ENV
        else 0
    )
except ValueError:
    TOURNAMENT_COOLDOWN_LIMIT_MS = 0


def current_network_time():
    """Return game-server Unix time in milliseconds for the native clock."""
    elapsed = time.monotonic() - _NETWORK_TIME_REAL_ANCHOR
    return int(
        _NETWORK_TIME_GAME_ANCHOR_MS
        + elapsed * 1000.0 * TOURNAMENT_TIME_SCALE
    )


def current_online_options_version():
    """Return a version that changes on source edits and offer rotations."""
    # Resolve the current rotation before reading mtimes. A new weekend choice
    # may create/update the state file, and that write must affect this same
    # login response rather than the following player's response.
    _offers, _duration_days, rotation_index = current_offer_rotation()
    source_paths = (
        ONLINE_OPTIONS_FILE,
        OFFER_ROTATION_FILE,
        OFFER_ROTATION_STATE_FILE,
        os.path.abspath(__file__),
    )
    modified_seconds = []
    for path in source_paths:
        try:
            modified_seconds.append(int(os.path.getmtime(path)))
        except OSError:
            continue
    rotation_version = max(0, int(rotation_index or 0))
    utc_day_version = int(time.time()) // (24 * 60 * 60)
    return (
        max([ONLINE_OPTIONS_VERSION, ONLINE_OPTIONS_BOOT_VERSION, *modified_seconds])
        + rotation_version
        + utc_day_version
    )

# These content rotations are intentionally configurable through the cached
# onlineoptions file. The remaining defaults stay server-controlled.
ONLINE_OPTIONS_FILE_PREFERRED_KEYS = {
    "EVENT_DNA_DINO",
    "EVENT_DNA_NB_STEPS",
    "ADDON_DINO_ID",
    "AQ_ADDON_DINO_ID",
    "AR_ADDON_DINO_ID",
}

# The 4.9.0 client can retain a previously downloaded DNA Rescue selection and
# then omit these keys from later batch reads. Include this small dynamic set in
# every normal c.pb response so all connected clients converge on the current
# server-wide event without clearing app data.
ONLINE_OPTIONS_LIVE_REFRESH_KEYS = (
    "EVENT_DNA_DINO",
    "EVENT_DNA_LIMIT",
    "EVENT_DNA_NB_STEPS",
    "CP_LTD_OFFER_ENABLE",
    "ADDON_DINO_ID",
    "ADDON_DINO_LIMIT",
    "AQ_ADDON_DINO_ID",
    "AQ_ADDON_DINO_LIMIT",
    "AR_ADDON_DINO_ID",
    "AR_ADDON_DINO_LIMIT",
) + tuple(
    key
    for index in range(1, 10)
    for key in (f"MARKET_ITEM_ID_{index}", f"MARKET_LIMIT_{index}")
)

OFFER_ROTATION_OPTION_KEYS = {
    "event_dinos": "EVENT_DNA_DINO",
    "surface_addon_dinos": "ADDON_DINO_ID",
    "aquatic_addon_dinos": "AQ_ADDON_DINO_ID",
    "glacier_addon_dinos": "AR_ADDON_DINO_ID",
}

MARKET_ITEM_ROTATION_KEYS = {
    "market_item_ids": "MARKET_ITEM_ID_{}",
}

BATTLE_TOURNAMENT_SCHEDULE_HOURS = tuple(range(0, 24, 4))
TOURNAMENT_SEASON_DURATION_SECONDS = 1296000
BATTLE_TOURNAMENT_TIME = {
    str(index): hour for index, hour in enumerate(BATTLE_TOURNAMENT_SCHEDULE_HOURS)
}
BATTLE_TOURNAMENT_TYPE = {
    day: {"0": 0, "4": 1, "8": 0, "12": 2, "16": 1, "20": 0}
    for day in (
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    )
}
BATTLE_ONLINE_OPTION_DEFAULTS = {
    "TNT_BANNED_DINO": "",
    "TNT_LEAGUES": "",
    "TNT_DNA": "",
    "EVENT_DNA_DINO": "",
    "TOURNAMENT_TIME": json.dumps(BATTLE_TOURNAMENT_TIME, separators=(",", ":")),
    "TOURNAMENT_TYPE": json.dumps(BATTLE_TOURNAMENT_TYPE, separators=(",", ":")),
    "BACK_WMBATTLE_ADS": json.dumps({"interval": 1, "minStage": 1}, separators=(",", ":")),
    "BACK_TNTBATTLE_ADS": json.dumps({"intervalTnt": 1, "minLeague": 1}, separators=(",", ":")),
    "BATL_KO_COST_MULTI": "2.0",
    "BATL_DMG_MULTI": "1.0",
    "TOURNAMENT_START": "0",
    "TNT_NEWRIVALCOST": "1",
    "TNT_SEASON_DURATION": "0",
    "BATL_POINT_PACK_1": "5",
    "BATL_POINT_PACK_2": "55",
    "BATL_POINT_PACK_3": "300",
    "BATL_POINT_PACK_4": "650",
    "BATL_POINT_PACK_5": "1750",
    "BATL_POINT_PACK_6": "3750",
    "BATL_BLOCK_COST": "10",
    "TNT_SPCLLDINO_TROPH": "5",
    "BATL_CRITICAL_COST": "10",
    "BATL_CRITICAL_BONUS": "50",
    "EVENT_DNA_STRAND_VAL": "30",
    "EVENT_DNA_NB_STEPS": "30",
    "EVENT_DNA_LIMIT": "0",
    "TNT_BESTAVG_COUNT": "5",
    "TNT_NEWRIVALSLIMIT": "10",
    "TNT_SEASONE_WRN": "86400000",
    "TNT_RAND_OPP_FLAG": "0",
    "interval": "1",
    "minStage": "1",
    "intervalTnt": "1",
    "minLeague": "1",
}


def _load_offer_rotation_config():
    try:
        with open(OFFER_ROTATION_FILE, "r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return config if isinstance(config, dict) else {}


def _event_dna_weekend_only(config=None):
    if config is None:
        config = _load_offer_rotation_config()
    value = config.get("event_weekend_only", False) if isinstance(config, dict) else False
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _truthy_config(config, key, default=False):
    value = config.get(key, default) if isinstance(config, dict) else default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _offer_week_key(now_seconds):
    now_utc = datetime.datetime.fromtimestamp(int(now_seconds), datetime.timezone.utc)
    year, week, _weekday = now_utc.isocalendar()
    return f"{year}-W{week:02d}"


def _event_dna_window(now_seconds=None, config=None):
    """Return the configured weekly DNA Rescue window in UTC."""
    if now_seconds is None:
        now_seconds = int(time.time())
    if config is None:
        config = _load_offer_rotation_config()
    now_utc = datetime.datetime.fromtimestamp(int(now_seconds), datetime.timezone.utc)
    try:
        start_weekday = int(config.get("event_start_weekday_utc", 5))
    except (AttributeError, TypeError, ValueError):
        start_weekday = 5
    try:
        duration_days = int(config.get("event_duration_days", 2))
    except (AttributeError, TypeError, ValueError):
        duration_days = 2
    start_weekday = max(0, min(start_weekday, 6))
    duration_days = max(1, min(duration_days, 7))

    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_start = (now_utc.weekday() - start_weekday) % 7
    start_utc = midnight_utc - datetime.timedelta(days=days_since_start)
    end_utc = start_utc + datetime.timedelta(days=duration_days)
    active = start_utc <= now_utc < end_utc
    if not active:
        start_utc += datetime.timedelta(days=7)
        end_utc = start_utc + datetime.timedelta(days=duration_days)
    window_key = start_utc.strftime("%Y-%m-%d")
    return active, start_utc, end_utc, window_key


def _weekend_offer_enabled(config=None):
    if config is None:
        config = _load_offer_rotation_config()
    return _truthy_config(config, "weekend_offer_enabled", False)


def _weekend_offer_window(now_seconds=None, config=None):
    """Return the configured two-day market weekend in UTC."""
    if now_seconds is None:
        now_seconds = int(time.time())
    if config is None:
        config = _load_offer_rotation_config()
    now_utc = datetime.datetime.fromtimestamp(int(now_seconds), datetime.timezone.utc)
    try:
        start_weekday = int(config.get("weekend_start_weekday_utc", 5))
    except (AttributeError, TypeError, ValueError):
        start_weekday = 5
    try:
        duration_days = int(config.get("weekend_duration_days", 2))
    except (AttributeError, TypeError, ValueError):
        duration_days = 2
    start_weekday = max(0, min(start_weekday, 6))
    duration_days = max(1, min(duration_days, 7))

    midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_start = (now_utc.weekday() - start_weekday) % 7
    start_utc = midnight_utc - datetime.timedelta(days=days_since_start)
    end_utc = start_utc + datetime.timedelta(days=duration_days)
    active = start_utc <= now_utc < end_utc
    if not active:
        start_utc += datetime.timedelta(days=7)
        end_utc = start_utc + datetime.timedelta(days=duration_days)
    return active, start_utc, end_utc, start_utc.strftime("%Y-%m-%d")


def _weekend_offer_rotation_index(start_utc, config):
    anchor_text = str(config.get("weekend_cycle_anchor_utc", "1970-01-03")).strip()
    try:
        anchor_date = datetime.date.fromisoformat(anchor_text)
    except ValueError:
        anchor_date = datetime.date(1970, 1, 3)
    return (start_utc.date() - anchor_date).days // 7


def _load_offer_rotation_state():
    try:
        with open(OFFER_ROTATION_STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_offer_rotation_state(state):
    try:
        os.makedirs(os.path.dirname(OFFER_ROTATION_STATE_FILE), exist_ok=True)
        temp_path = f"{OFFER_ROTATION_STATE_FILE}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, OFFER_ROTATION_STATE_FILE)
    except OSError:
        pass


def current_weekend_event_dino(choices, now_seconds=None, config=None):
    """Pick one random DNA Rescue creature per UTC weekend and persist it."""
    if now_seconds is None:
        now_seconds = int(time.time())
    if not choices:
        return ""
    if not _truthy_config(config, "event_random_weekend_dino", False):
        event_rotation_index = now_seconds // (7 * 24 * 60 * 60)
        return choices[event_rotation_index % len(choices)]

    if _event_dna_weekend_only(config):
        _active, _start_utc, _end_utc, week_key = _event_dna_window(now_seconds, config)
    else:
        week_key = _offer_week_key(now_seconds)
    with OFFER_ROTATION_STATE_LOCK:
        state = _load_offer_rotation_state()
        saved_week = str(state.get("event_week_key", ""))
        saved_dino = str(state.get("event_dino", "")).strip()
        if saved_week == week_key and saved_dino in choices:
            return saved_dino

        avoid_repeat = _truthy_config(config, "event_avoid_repeat", True)
        previous_dino = str(state.get("last_event_dino", saved_dino)).strip()
        pool = [
            choice
            for choice in choices
            if not (avoid_repeat and len(choices) > 1 and choice == previous_dino)
        ]
        picked = random.choice(pool or choices)
        state.update({
            "event_week_key": week_key,
            "event_dino": picked,
            "last_event_dino": picked,
            "updated_at": int(time.time()),
        })
        _save_offer_rotation_state(state)
        return picked


def current_market_offer_limit_ms(now_seconds=None, duration_days=3, config=None):
    """Return the absolute end of the current shared offer window in ms."""
    if now_seconds is None:
        now_seconds = int(time.time())
    if config is None:
        config = _load_offer_rotation_config()
    if _weekend_offer_enabled(config):
        _active, _start_utc, end_utc, _window_key = _weekend_offer_window(
            now_seconds,
            config,
        )
        return int(end_utc.timestamp()) * 1000
    try:
        days = int(duration_days)
    except (TypeError, ValueError):
        days = 3
    days = max(1, min(days, 90))
    window_seconds = days * 24 * 60 * 60
    # Native 4.9.0 compares each *_LIMIT value directly with its millisecond
    # network clock. Use a fixed window boundary so repeated option requests
    # do not move the deadline forward and prevent the rotation from ending.
    end_seconds = ((int(now_seconds) // window_seconds) + 1) * window_seconds
    return end_seconds * 1000


def current_event_dna_limit_ms(now_seconds=None, config=None):
    """Return the end of the current or upcoming weekly DNA Rescue window."""
    if now_seconds is None:
        now_seconds = int(time.time())
    _active, _start_utc, end_utc, _window_key = _event_dna_window(now_seconds, config)
    return int(end_utc.timestamp()) * 1000


def current_offer_rotation(now_seconds=None):
    """Return deterministic offer IDs for the current shared rotation window."""
    if now_seconds is None:
        now_seconds = int(time.time())
    config = _load_offer_rotation_config()
    if not config:
        return {}, 3, None
    try:
        duration_days = max(1, min(int(config.get("duration_days", 3)), 30))
    except (TypeError, ValueError):
        duration_days = 3
    weekend_enabled = _weekend_offer_enabled(config)
    if weekend_enabled:
        weekend_active, weekend_start, _weekend_end, _weekend_key = (
            _weekend_offer_window(now_seconds, config)
        )
        try:
            duration_days = max(
                1,
                min(int(config.get("weekend_duration_days", 2)), 7),
            )
        except (TypeError, ValueError):
            duration_days = 2
        rotation_index = _weekend_offer_rotation_index(weekend_start, config)
    else:
        weekend_active = True
        window_seconds = duration_days * 24 * 60 * 60
        rotation_index = now_seconds // window_seconds
    scheduled_event_active, _event_start, _event_end, _event_key = _event_dna_window(
        now_seconds,
        config,
    )
    event_active = not _event_dna_weekend_only(config) or scheduled_event_active
    values = {}
    for config_key, option_key in OFFER_ROTATION_OPTION_KEYS.items():
        if weekend_enabled and config_key in (
            "surface_addon_dinos",
            "aquatic_addon_dinos",
            "glacier_addon_dinos",
        ):
            continue
        choices = config.get(config_key)
        if not isinstance(choices, list):
            continue
        choices = [str(choice).strip() for choice in choices if str(choice).strip()]
        if choices:
            if config_key == "event_dinos":
                values[option_key] = current_weekend_event_dino(choices, now_seconds, config) if event_active else ""
            else:
                values[option_key] = choices[rotation_index % len(choices)]
    for config_key, option_template in MARKET_ITEM_ROTATION_KEYS.items():
        if weekend_enabled:
            continue
        slots = config.get(config_key)
        if not isinstance(slots, dict):
            continue
        for slot, choices in slots.items():
            try:
                slot_index = int(str(slot).strip())
            except (TypeError, ValueError):
                continue
            if slot_index < 1 or slot_index > 9 or not isinstance(choices, list):
                continue
            choices = [str(choice).strip() for choice in choices if str(choice).strip()]
            if choices:
                values[option_template.format(slot_index)] = choices[rotation_index % len(choices)]
    if weekend_enabled:
        values.update({
            "CP_LTD_OFFER_ENABLE": "1" if weekend_active else "0",
            "ADDON_DINO_ID": "",
            "AQ_ADDON_DINO_ID": "",
            "AR_ADDON_DINO_ID": "",
        })
        for slot_index in range(1, 10):
            values[f"MARKET_ITEM_ID_{slot_index}"] = ""

        if weekend_active:
            cycle = config.get("weekend_offer_cycle")
            cycle = [
                item
                for item in cycle
                if isinstance(item, dict)
            ] if isinstance(cycle, list) else []
            if cycle:
                selected = cycle[rotation_index % len(cycle)]
                values["ADDON_DINO_ID"] = str(
                    selected.get("surface_dino", "")
                ).strip()
                building_id = str(selected.get("building", "")).strip()
                try:
                    building_slot = int(selected.get("market_slot", 1))
                except (TypeError, ValueError):
                    building_slot = 1
                if building_id and 1 <= building_slot <= 9:
                    values[f"MARKET_ITEM_ID_{building_slot}"] = building_id

            for config_key, option_key in (
                ("aquatic_addon_dinos", "AQ_ADDON_DINO_ID"),
                ("glacier_addon_dinos", "AR_ADDON_DINO_ID"),
            ):
                choices = config.get(config_key)
                choices = [
                    str(choice).strip()
                    for choice in choices
                    if str(choice).strip()
                ] if isinstance(choices, list) else []
                if choices:
                    values[option_key] = choices[rotation_index % len(choices)]
    else:
        featured_surface_addon = str(
            config.get("surface_addon_featured_dino", "")
        ).strip()
        if featured_surface_addon:
            values["ADDON_DINO_ID"] = featured_surface_addon
    return values, duration_days, rotation_index

def current_tournament_start(now_seconds=None):
    if now_seconds is None:
        now_seconds = int(time.time())
    duration = TOURNAMENT_SEASON_DURATION_SECONDS
    return now_seconds - (now_seconds % duration)

def battle_online_option_defaults():
    values = dict(BATTLE_ONLINE_OPTION_DEFAULTS)
    saved = load_guest_save("Guest", None)
    # Preserve the legacy 4.9.0 onlineoptions contract. The original client
    # expects a zero season epoch here; modern Unix timestamps overflow its
    # old date arithmetic and produce billion-day counters.
    values["TOURNAMENT_START"] = "0"
    rotating_offers, offer_duration_days, _rotation_index = current_offer_rotation()
    market_limit = str(
        current_market_offer_limit_ms(duration_days=offer_duration_days)
    )
    event_dna_limit = (
        str(current_event_dna_limit_ms())
        if _event_dna_weekend_only()
        else market_limit
    )
    for index in range(1, 10):
        if str(values.get(f"MARKET_ITEM_ID_{index}", "")).strip():
            values[f"MARKET_LIMIT_{index}"] = market_limit
            
    # The tournament screen hosts these limited-offer panels. When they are
    # expired or malformed, the client leaves a "%02dd %02dhr" overlay active
    # and tournament buttons stop receiving clicks. Disable them while the
    # tournament flow is under server control.
    values["MARKET_LIMIT_9"] = "1465991940000"
    values["TNT_EVENT_NOTIF_FLAG"] = "0"
    values["EVENT_DNA_NB_STEPS"] = "30"
    values["MEAT_PACKS"] = '{"Surface":{"Amount0":5000,"Amount1":30000,"Amount2":75000,"Amount3":250000},"Aquatic":{"Amount0":5000,"Amount1":30000,"Amount2":75000,"Amount3":250000},"Glacier":{"Amount0":5000,"Amount1":30000,"Amount2":75000,"Amount3":250000}}'
    values["MAX_SC_PACK_2"] = "140500"
    values["MAX_SC_PACK_3"] = "802000"
    values["MAX_SC_PACK_4"] = "1737000"
    values["MAX_SC_PACK_5"] = "5010000"
    values["TNT_SEASON_DURATION"] = str(TOURNAMENT_SEASON_DURATION_SECONDS)
    values["MARKET_ITEM_ID_1"] = "Build_HotAirBalloon_5x5"
    values["CR_FILL_HOURS"] = "9"
    values["EVENT_DNA_STRAND_VAL"] = "60"
    values["MARKET_ITEM_ID_2"] = "AQBuild_ObsDeck000_4x4"
    values["BATL_DMG_MULTI"] = "1.2"
    values["MARKET_ITEM_ID_9"] = "ARBuild_HotSprin000_4X4"
    values["AR_EXPDN_AMBR_LTD_NO"] = "45"
    values["MARKET_ITEM_ID_7"] = "BattlePointPack04"
    values["MARKET_ITEM_ID_8"] = "BattlePointPack05"
    values["MARKET_ITEM_ID_5"] = "ARBuild_PowerGen000_2X2"
    values["MARKET_ITEM_ID_6"] = "BattlePointPack03"
    values["CR_LVL_MULTI"] = "0.05"
    values["BATL_KO_COST_MULTI"] = "1"
    values["MARKET_ITEM_ID_3"] = "AQDeco_Galleon000_5x5"
    values["MARKET_ITEM_ID_4"] = "AQBuild_Garden000_4x4"
    values["AR_EXPDN_AMBR_LTD_HC"] = "359"
    values["FB_FAN_PAGE"] = "https://www.facebook.com/269588813130044"
    values["ADCOLONY_MARKET"] = "1"
    values["TNT_DNA"] = '{"first_box_chance":25,"second_box_chance":75,"third_box_chance":100,"winning_box_chance":100}'
    values["AR_ADDON_DINO_LIMIT"] = "1893456000000"
    values["AUTO_SAVE_MS"] = "60000"
    values["TJ_VIDEO_CACHE_COUNT"] = "5"
    values["CR_FILL_SCORE"] = "650"
    values["BATL_POINT_PACK_5"] = "650"
    values["BATL_POINT_PACK_6"] = "3750"
    values["TOURNAMENT_TIME"] = json.dumps(BATTLE_TOURNAMENT_TIME, separators=(",", ":"))
    values["BATL_POINT_PACK_3"] = "300"
    values["AQ_ADDON_DINO_ID"] = "Dakosaurus"
    values["BATL_POINT_PACK_4"] = "650"
    values["BATL_POINT_PACK_1"] = "5"
    values["TNT_NEWRIVALCOST"] = "1"
    values["BATL_POINT_PACK_2"] = "55"
    values["MTX_ANIM"] = "1"
    values["MAX_SC_PACK_1"] = "13400"
    values["SPEEDUP_VIDEO_ENABLE"] = "1"
    values["CP_DINO_ENSURE_SEQ"] = "4"
    values["AD_DATA_ENABLED"] = "1"
    values["BORROW_DINO_COST"] = "1"
    values["SLOT_2_ERR_PRCNT"] = "15"
    values["ADCOLONY_TIERS"] = '{ "Apple_ADCOLONY":1, "Google_ADCOLONY":1, "Amazon_ADCOLONY":1, "Facebook_ADCOLONY":0 }'
    values["EXPDN_AMBR_LTD_NOT"] = "45"
    values["AR_ADDON_DINO_ID"] = "Procoptodon"
    values["BATL_CRITICAL_COST"] = "5"
    values["RANDOM_PARK_NUM"] = "2"
    values["BACK_WMBATTLE_ADS"] = '{"interval":3,"minStage":5}'
    values["CR_FILL_TIME"] = "7"
    values["FB_SNDGIFT_ENABLE"] = '{"FACEBOOK_SNDGIFT_FREIND_BAR":1, "FACEBOOK_SNDGIFT_MAIL":1}'
    values["ADDON_DINO_LIMIT"] = "1893456000000"
    values["EVENT_DNA_DINO"] = "Giganotosaurus"
    values["CP_RESOURCE_MOD"] = '{"Meat":100,"Crops":100,"SoftCash":100,"HardCash":100,"BattlePoint":100}'
    values["BATL_BLOCK_COST"] = "5"
    values["CHARTBOOST_FB_DATA"] = "{}"
    values["MARKET_LIMIT_7"] = "1499860800000"
    values["MARKET_LIMIT_8"] = "1499860800000"
    values["TNT_BESTAVG_COUNT"] = "9"
    values["SPEEDUP_VIDEO_TIME"] = "1800000"
    values["MARKET_LIMIT_3"] = "1572872400000"
    values["MARKET_LIMIT_4"] = "1562587200000"
    values["MARKET_LIMIT_5"] = "1528113600000"
    values["MARKET_LIMIT_6"] = "1499860800000"
    values["EXPDN_AMBR_CMN_HC"] = "99"
    values["MARKET_LIMIT_1"] = "1573477200000"
    values["CR_FILL_ACCEL"] = "0.03"
    values["MARKET_LIMIT_2"] = "1573477200000"
    values["AUTORECONNECT"] = "1"
    values["CR_TIME_PER_DINO"] = "10"
    values["OFFERWALL_TIERS"] = '{ "Apple":1, "Google":1, "Amazon":1, "Facebook":1 }'
    values["COPPA"] = '{ "COPPA_PushNotif":1, "COPPA_LocalNotif":1, "COPPA_SessionM":1, "COPPA_Tapjoy":1, "COPPA_TapjoyMail":1, "COPPA_ChartboostAds":1, "COPPA_ChartboostMail":1, "COPPA_AdColony":0}'
    values["AR_EXPDN_AMBR_LTD_SL"] = "3"
    values["TOURNAMENT_TYPE"] = json.dumps(BATTLE_TOURNAMENT_TYPE, separators=(",", ":"))
    values["TNT_NEWRIVALSLIMIT"] = "10"
    values["EXPDN_AMBR_LTD_HC"] = "359"
    values["HARDCASH_PACK_5"] = "700"
    values["HARDCASH_PACK_6"] = "1500"
    values["HARDCASH_PACK_3"] = "120"
    values["RANDOM_PARK_ID_2"] = "8801492"
    values["RANDOM_PARK_ID_1"] = "8801038"
    values["HARDCASH_PACK_4"] = "325"
    values["HARDCASH_PACK_1"] = "20"
    values["CP_FREE_FILL_HOURS"] = "8"
    values["HARDCASH_PACK_2"] = "55"
    values["CARNI_MAX_GAIN"] = "2500"
    values["CARD_SUB_PRIZE_HC"] = "125"
    values["TNT_SPCLLDINO_TROPH"] = "5"
    values["RANDOM_PARK_ID_3"] = "2395"
    values["FACEBOOK_MERGE_REWAR"] = "5"
    values["AD_DATA_DELAY"] = "1200000"
    values["ADDON_DINO_ID"] = "Carnotaurus"
    values["SLOT_SUCCESS_INCR"] = "0"
    values["FACEBOOK_LIKE_REWARD"] = "2"
    values["AR_EXPDN_AMBR_CMN_HC"] = "99"
    values["CP_LTD_OFFER_ENABLE"] = "1"
    values["MARKET_REBATE_9"] = "0.4"
    values["MARKET_REBATE_7"] = "0.3"
    values["EVENT_DNA_LIMIT"] = event_dna_limit
    values["ADDON_DINO_LIMIT"] = market_limit
    values["AQ_ADDON_DINO_LIMIT"] = market_limit
    values["AR_ADDON_DINO_LIMIT"] = market_limit
    values["MARKET_STOCK_UNLOCK"] = "3"
    values["MARKET_REBATE_8"] = "0.3"
    values["MARKET_REBATE_5"] = "0.4"
    values["MARKET_REBATE_6"] = "0.3"
    values["MARKET_REBATE_3"] = "0.5"
    values["MARKET_REBATE_4"] = "0.5"
    values["MARKET_REBATE_1"] = "0.3"
    values["MARKET_REBATE_2"] = "0.25"
    values["BATL_CRITICAL_BONUS"] = "50"
    values["MIXPANEL_TIERS"] = '{"inApps":0,"assetSold":0,"spendHardcash":0,"spendSoftcash":0,"researchCompleted":0,"visitPark":0,"dinosaurLevelUp":0,"inviteSent":0,"levelUp":0,"expedition":0,"daily":0,"login":0,"newPlayer":0,"transaction":0,"download":0,"combatOver":0,"spendClaw":0,"codeRedCompleted":0,"mailboxPlayer":0,"mailboxServer":0,"callToAction":0,"tournamentOver":0,"savePanic":0,"mission":0,"ageGate":0,"cardPackOpened":0}'
    values["BACK_TNTBATTLE_ADS"] = '{"intervalTnt":1,"minLeague":2}'
    values["EXPDN_AMBR_LTD_SEL"] = "3"
    values["TNT_SEASONE_WRN"] = "86400000"
    values["CDN_URL"] = "https://d1feakv9w1xgxl.cloudfront.net/jurassic/"
    values["TNT_BANNED_DINO"] = '{"Surface": [],"Aquatic": [],"Arctic": []}'
    values["TNT_LEAGUES"] = '{"Bronze": {"ReqTrophy": 0,"LeagueReset": 1,"Normal": {"Entry": 1,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 2},"Finals": {"PrizeHardCash": 1,"PrizeTrophies": 5},"Winner": {"PrizeHardCash": 4,"PrizeTrophies": 10}},"Big": {"Entry": 1,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 2},"Finals": {"PrizeHardCash": 1,"PrizeTrophies": 5},"Winner": {"PrizeHardCash": 4,"PrizeTrophies": 10}},"High": {"Entry": 1,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 2},"Finals": {"PrizeHardCash": 1,"PrizeTrophies": 5},"Winner": {"PrizeHardCash": 4,"PrizeTrophies": 10}},"Dna": {"Entry": 1,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 2},"Finals": {"PrizeHardCash": 1,"PrizeTrophies": 5},"Winner": {"PrizeHardCash": 4,"PrizeTrophies": 10}},"ParkParameters": {"Surface": {"feroComputeType": 2,"ferocityMin": 1,"ferocityMax": 19,"BestAverageModifier": {"FirstRound": -30,"SemiFinalist": -20,"Finalist": -10},"TeamSizes": {"Small": {"soloOdd": 15,"duoOdd": 10},"Mid": {"soloOdd": 15,"duoOdd": 10},"Big": {"soloOdd": 15,"duoOdd": 10}},"AIInfo": {"FirstRound": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Aquatic": {"feroComputeType": 2,"ferocityMin": 1,"ferocityMax": 19,"BestAverageModifier": {"FirstRound": -30,"SemiFinalist": -20,"Finalist": -10},"TeamSizes": {"Small": {"soloOdd": 15,"duoOdd": 10},"Mid": {"soloOdd": 15,"duoOdd": 10},"Big": {"soloOdd": 15,"duoOdd": 10}},"AIInfo": {"FirstRound": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Glacier": {"feroComputeType": 2,"ferocityMin": 1,"ferocityMax": 19,"BestAverageModifier": {"FirstRound": -30,"SemiFinalist": -20,"Finalist": -10},"TeamSizes": {"Small": {"soloOdd": 15,"duoOdd": 10},"Mid": {"soloOdd": 15,"duoOdd": 10},"Big": {"soloOdd": 15,"duoOdd": 10}},"AIInfo": {"FirstRound": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 10,"BlockUsage": 10,"PowerChance": 0,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0}}}}},"Silver": {"ReqTrophy": 10,"LeagueReset": 5,"Normal": {"Entry": 2,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 3},"Finals": {"PrizeHardCash": 2,"PrizeTrophies": 7},"Winner": {"PrizeHardCash": 8,"PrizeTrophies": 20}},"Big": {"Entry": 3,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 3},"Finals": {"PrizeHardCash": 3,"PrizeTrophies": 7},"Winner": {"PrizeHardCash": 12,"PrizeTrophies": 20}},"High": {"Entry": 4,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 3},"Finals": {"PrizeHardCash": 4,"PrizeTrophies": 7},"Winner": {"PrizeHardCash": 16,"PrizeTrophies": 20}},"Dna": {"Entry": 4,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 3},"Finals": {"PrizeHardCash": 4,"PrizeTrophies": 7},"Winner": {"PrizeHardCash": 16,"PrizeTrophies": 20}},"ParkParameters": {"Surface": {"feroComputeType": 2,"ferocityMin": 20,"ferocityMax": 76,"BestAverageModifier": {"FirstRound": -20,"SemiFinalist": 10,"Finalist": 0},"TeamSizes": {"Small": {"soloOdd": 10,"duoOdd": 5},"Mid": {"soloOdd": 10,"duoOdd": 5},"Big": {"soloOdd": 10,"duoOdd": 5}},"AIInfo": {"FirstRound": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Aquatic": {"feroComputeType": 2,"ferocityMin": 20,"ferocityMax": 76,"BestAverageModifier": {"FirstRound": -20,"SemiFinalist": -10,"Finalist": 0},"TeamSizes": {"Small": {"soloOdd": 40,"duoOdd": 60},"Mid": {"soloOdd": 20,"duoOdd": 60},"Big": {"soloOdd": 0,"duoOdd": 60}},"AIInfo": {"FirstRound": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Glacier": {"feroComputeType": 2,"ferocityMin": 20,"ferocityMax": 76,"BestAverageModifier": {"FirstRound": -20,"SemiFinalist": -10,"Finalist": 0},"TeamSizes": {"Small": {"soloOdd": 40,"duoOdd": 60},"Mid": {"soloOdd": 20,"duoOdd": 60},"Big": {"soloOdd": 0,"duoOdd": 60}},"AIInfo": {"FirstRound": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 5,"BlockSuccess": 5},"Finalist": {"SpecialUsage": 20,"BlockUsage": 20,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 0,"SpecialSuccess": 10,"BlockSuccess": 10}}}}},"Gold": {"ReqTrophy": 50,"LeagueReset": 10,"Normal": {"Entry": 3,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 4},"Finals": {"PrizeHardCash": 3,"PrizeTrophies": 9},"Winner": {"PrizeHardCash": 12,"PrizeTrophies": 30}},"Big": {"Entry": 4,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 4},"Finals": {"PrizeHardCash": 4,"PrizeTrophies": 9},"Winner": {"PrizeHardCash": 16,"PrizeTrophies": 30}},"High": {"Entry": 5,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 4},"Finals": {"PrizeHardCash": 5,"PrizeTrophies": 9},"Winner": {"PrizeHardCash": 20,"PrizeTrophies": 30}},"Dna": {"Entry": 12,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 4},"Finals": {"PrizeHardCash": 5,"PrizeTrophies": 9},"Winner": {"PrizeHardCash": 48,"PrizeTrophies": 30}},"ParkParameters": {"Surface": {"feroComputeType": 2,"ferocityMin": 156,"ferocityMax": 312,"BestAverageModifier": {"FirstRound": -10,"SemiFinalist": 0,"Finalist": 10},"TeamSizes": {"Small": {"soloOdd": 5,"duoOdd": 0},"Mid": {"soloOdd": 5,"duoOdd": 0},"Big": {"soloOdd": 5,"duoOdd": 0}},"AIInfo": {"FirstRound": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Aquatic": {"feroComputeType": 2,"ferocityMin": 77,"ferocityMax": 312,"BestAverageModifier": {"FirstRound": -10,"SemiFinalist": 0,"Finalist": 10},"TeamSizes": {"Small": {"soloOdd": 10,"duoOdd": 60},"Mid": {"soloOdd": 0,"duoOdd": 60},"Big": {"soloOdd": 0,"duoOdd": 40}},"AIInfo": {"FirstRound": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Glacier": {"feroComputeType": 2,"ferocityMin": 77,"ferocityMax": 312,"BestAverageModifier": {"FirstRound": -10,"SemiFinalist": 0,"Finalist": 10},"TeamSizes": {"Small": {"soloOdd": 10,"duoOdd": 60},"Mid": {"soloOdd": 0,"duoOdd": 60},"Big": {"soloOdd": 0,"duoOdd": 40}},"AIInfo": {"FirstRound": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 10,"BlockSuccess": 10},"Finalist": {"SpecialUsage": 30,"BlockUsage": 30,"PowerChance": 50,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 15,"BlockSuccess": 15}}}}},"Platinum": {"ReqTrophy": 140,"LeagueReset": 15,"Normal": {"Entry": 4,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 5},"Finals": {"PrizeHardCash": 4,"PrizeTrophies": 11},"Winner": {"PrizeHardCash": 16,"PrizeTrophies": 40}},"Big": {"Entry": 5,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 5},"Finals": {"PrizeHardCash": 5,"PrizeTrophies": 11},"Winner": {"PrizeHardCash": 20,"PrizeTrophies": 40}},"High": {"Entry": 6,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 5},"Finals": {"PrizeHardCash": 6,"PrizeTrophies": 11},"Winner": {"PrizeHardCash": 24,"PrizeTrophies": 40}},"Dna": {"Entry": 15,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 5},"Finals": {"PrizeHardCash": 6,"PrizeTrophies": 11},"Winner": {"PrizeHardCash": 60,"PrizeTrophies": 40}},"ParkParameters": {"Surface": {"feroComputeType": 2,"ferocityMin": 555,"ferocityMax": 875,"BestAverageModifier": {"FirstRound": 0,"SemiFinalist": 10,"Finalist": 20},"AIInfo": {"FirstRound": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Aquatic": {"feroComputeType": 2,"ferocityMin": 555,"ferocityMax": 875,"BestAverageModifier": {"FirstRound": 0,"SemiFinalist": 10,"Finalist": 20},"AIInfo": {"FirstRound": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Glacier": {"feroComputeType": 2,"ferocityMin": 555,"ferocityMax": 875,"BestAverageModifier": {"FirstRound": 0,"SemiFinalist": 10,"Finalist": 20},"AIInfo": {"FirstRound": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 15,"BlockSuccess": 15},"Finalist": {"SpecialUsage": 40,"BlockUsage": 40,"PowerChance": 75,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 20,"BlockSuccess": 20}}}}},"Allstar": {"ReqTrophy": 300,"LeagueReset": 20,"Normal": {"Entry": 5,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 10},"Finals": {"PrizeHardCash": 5,"PrizeTrophies": 25},"Winner": {"PrizeHardCash": 20,"PrizeTrophies": 100}},"Big": {"Entry": 15,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 10},"Finals": {"PrizeHardCash": 15,"PrizeTrophies": 25},"Winner": {"PrizeHardCash": 60,"PrizeTrophies": 100}},"High": {"Entry": 30,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 10},"Finals": {"PrizeHardCash": 30,"PrizeTrophies": 25},"Winner": {"PrizeHardCash": 120,"PrizeTrophies": 100}},"Dna": {"Entry": 20,"SemiFinals": {"PrizeHardCash": 0,"PrizeTrophies": 10},"Finals": {"PrizeHardCash": 30,"PrizeTrophies": 25},"Winner": {"PrizeHardCash": 80,"PrizeTrophies": 100}},"ParkParameters": {"Surface": {"feroComputeType": 2,"ferocityMin": 876,"ferocityMax": 2000,"BestAverageModifier": {"FirstRound": 10,"SemiFinalist": 20,"Finalist": 30},"AIInfo": {"FirstRound": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Aquatic": {"feroComputeType": 2,"ferocityMin": 876,"ferocityMax": 2000,"BestAverageModifier": {"FirstRound": 10,"SemiFinalist": 20,"Finalist": 30},"AIInfo": {"FirstRound": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"Finalist": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0}}},"Glacier": {"feroComputeType": 2,"ferocityMin": 876,"ferocityMax": 2000,"BestAverageModifier": {"FirstRound": 10,"SemiFinalist": 20,"Finalist": 30},"AIInfo": {"FirstRound": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 0,"BlockSuccess": 0},"SemiFinalist": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 20,"BlockSuccess": 20},"Finalist": {"SpecialUsage": 50,"BlockUsage": 50,"PowerChance": 100,"DisconnectChance": 1,"SwitchChance": 25,"SpamClaw": 1,"SpecialSuccess": 25,"BlockSuccess": 25}}}}}}'
    values["TNT_RAND_OPP_FLAG"] = "0"
    values["HERBI_MAX_GAIN"] = "2500"
    values["FACEBOOK_LIKE_ACTIVE"] = "1"
    values["AQ_ADDON_DINO_LIMIT"] = "1893456000000"
    values["ADCOLONY_MAIL"] = "0"
    values["SLOT_1_ERR_PRCNT"] = "80"
    values["CHARTBOOST_MODE"] = '{ "Apple_ChartBoost":2, "Google_ChartBoost":2, "Amazon_ChartBoost":2, "Facebook_ChartBoost":0 }'
    values["EXPDN_FILL_HOURS"] = "9"
    values["CROPS_PACKS"] = '{"Surface":{"Amount0":5000,"Amount1":30000,"Amount2":75000,"Amount3":250000},"Aquatic":{"Amount0":5000,"Amount1":30000,"Amount2":75000,"Amount3":250000},"Glacier":{"Amount0":5000,"Amount1":30000,"Amount2":75000,"Amount3":250000}}'
    values["PARK_LEVEL_MAX"] = "114"

    # Apply the shared absolute offer deadline last so old donor timestamps do
    # not expire the cards and every offer rotates at the same window boundary.
    for index in range(1, 10):
        if str(values.get(f"MARKET_ITEM_ID_{index}", "")).strip():
            values[f"MARKET_LIMIT_{index}"] = market_limit
    values["EVENT_DNA_LIMIT"] = event_dna_limit
    values["ADDON_DINO_LIMIT"] = market_limit
    values["AQ_ADDON_DINO_LIMIT"] = market_limit
    values["AR_ADDON_DINO_LIMIT"] = market_limit
    values.update(rotating_offers)

    test_cooldown = os.environ.get("JPB_TOURNAMENT_TEST_COOLDOWN_SECONDS", "").strip()
    if test_cooldown:
        try:
            cooldown_seconds = max(1, int(float(test_cooldown)))
        except ValueError:
            cooldown_seconds = 5
        now_seconds = int(time.time())
        values["TNT_SEASON_DURATION"] = str(cooldown_seconds)
        start_seconds = now_seconds - (now_seconds % cooldown_seconds)
        values["TOURNAMENT_START"] = str(start_seconds)
    return values

def log_tournament_timer_state(log, label):
    try:
        now_seconds = int(time.time())
        defaults = battle_online_option_defaults()
        schedule = json.loads(defaults.get("TOURNAMENT_TIME", "{}"))
        schedule_hours = sorted(
            int(value)
            for value in schedule.values()
            if str(value).lstrip("-").isdigit()
        )
        seconds_until_next = None
        if schedule_hours:
            local_now = datetime.datetime.fromtimestamp(now_seconds)
            today_candidates = [
                local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
                for hour in schedule_hours
            ]
            future = [candidate for candidate in today_candidates if candidate.timestamp() > now_seconds]
            next_start = future[0] if future else (
                local_now.replace(hour=schedule_hours[0], minute=0, second=0, microsecond=0)
                + datetime.timedelta(days=1)
            )
            seconds_until_next = int(next_start.timestamp() - now_seconds)
        log(
            f"[TOURNAMENT] {label} now={now_seconds} "
            f"TOURNAMENT_START={defaults.get('TOURNAMENT_START')} "
            f"TNT_SEASON_DURATION={defaults.get('TNT_SEASON_DURATION')} "
            f"schedule_hours={schedule_hours} seconds_until_next={seconds_until_next}"
        )
    except Exception as exc:
        log(f"[TOURNAMENT] {label} debug failed: {exc!r}")


TOURNAMENT_PROFILE_LEAGUE_BASELINES = {
    "74": [1, 1, 1],
    "75": [0, 0, 0],
    "76": [1, 1, 1],
    "95": 1,
    "107": 0,
    "108": 0,
    "109": 0,
}

DEFAULT_LOGCAT_MARKERS = (
    "ludia",
    "jurassic",
    "smartfox",
    "sfs",
    "login",
    "disconnect",
    "connection",
    "socket",
    "agnetwork",
    "unity",
    "androidruntime",
    "fatal",
    "exception",
    "error",
)


def now_text():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


VERSION = "current"
SERVER_AUTHORITY_AFTER_REPLAY_SECONDS = 20.0
POST_WRITE_READ_REPLAY_SECONDS = 30.0
POST_REPLAY_CLIENT_RESET_SECONDS = 8.0
POST_REPLAY_TIMER_RESET_MS = 120 * 1000
POST_REPLAY_TIMER_RESET_SECONDS = 120
HARDCASH_DIRECT_CGI_DELAY_SECONDS = 0.0
HARDCASH_DELIVERY_MODE = os.environ.get("JPB_HARDCASH_DELIVERY", "direct").strip().lower()
if HARDCASH_DELIVERY_MODE not in ("mail", "direct"):
    HARDCASH_DELIVERY_MODE = "direct"
HARDCASH_LATE_SYNC_DELAYS = (
    (0.25, 1.0, 3.0, 8.0)
    if HARDCASH_DELIVERY_MODE == "direct"
    else (4.0, 10.0, 18.0)
)
HARDCASH_MAIL_GIFT_ID = 8800001
HARDCASH_MAIL_GIFT_IMAGE = (
    "https://d1feakv9w1xgxl.cloudfront.net/jurassic//"
    "JurassicParkBuilder_Hammond_Icon.png"
)
HARDCASH_MAIL_CONTENT_TYPE = 7
HARDCASH_MAIL_ACTION = 5
LOG_RETENTION_SECONDS = 7 * 24 * 60 * 60
def log_hour_stamp(now=None):
    if now is None:
        now = datetime.datetime.now()
    return now.strftime("%Y%m%d_%H0000")


def cleanup_old_log_files(retention_seconds=LOG_RETENTION_SECONDS):
    cutoff = time.time() - retention_seconds
    for folder in (LOG_DIR, PLAYER_LOG_DIR, PLAYER_SESSION_LOG_DIR):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.endswith(".log"):
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


class FileCache:
    def __init__(self, path, parser, fallback_data=None, max_size=None):
        self.path = path
        self.parser = parser
        self.fallback_data = fallback_data
        self.max_size = max_size
        self.mtime = None
        self.data = None
        self.lock = threading.Lock()

    def get(self, log_func=None):
        with self.lock:
            try:
                current_mtime = os.path.getmtime(self.path)
            except OSError:
                if self.data is not None:
                    return self.data
                return self.fallback_data

            if self.mtime == current_mtime and self.data is not None:
                return self.data

            try:
                if self.max_size is not None:
                    size = os.path.getsize(self.path)
                    if size > self.max_size:
                        if log_func:
                            log_func(f"[CACHE] File {self.path} too large ({size} bytes), reading directly")
                        with open(self.path, "rb") as handle:
                            return self.parser(handle)

                is_json = (self.parser == json.load or "json" in getattr(self.parser, "__name__", "").lower())
                mode = "r" if is_json else "rb"
                encoding = "utf-8" if is_json else None
                with open(self.path, mode, encoding=encoding) as handle:
                    new_data = self.parser(handle)
                self.data = new_data
                self.mtime = current_mtime
                if log_func:
                    log_func(f"[CACHE] Loaded and cached {self.path} mtime={self.mtime}")
                return self.data
            except Exception as exc:
                if log_func:
                    log_func(f"[CACHE] Error loading {self.path}: {exc!r}. Using fallback.")
                if self.data is not None:
                    return self.data
                return self.fallback_data


class StaticAssetCache:
    def __init__(self, max_size=5 * 1024 * 1024):
        self.max_size = max_size
        self.cache = {}  # path -> (mtime, content)
        self.lock = threading.Lock()

    def get_content(self, path, log_func=None):
        with self.lock:
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
            except OSError:
                return b""

            if size > self.max_size:
                # Bypass cache for large files to save RAM
                return b""

            cached = self.cache.get(path)
            if cached and cached[0] == mtime:
                return cached[1]

            try:
                with open(path, "rb") as f:
                    content = f.read()
                self.cache[path] = (mtime, content)
                if log_func:
                    log_func(f"[CACHE] Loaded and cached static file {os.path.basename(path)} ({size} bytes)")
                return content
            except OSError as exc:
                if log_func:
                    log_func(f"[CACHE] Error reading static file {path}: {exc!r}")
                if cached:
                    return cached[1]
                return b""

    def preload_dirs(self, dirs, log_func=None):
        loaded = 0
        skipped = 0
        total_bytes = 0
        for folder in dirs:
            if not os.path.isdir(folder):
                continue
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        skipped += 1
                        continue
                    if size > self.max_size:
                        skipped += 1
                        continue
                    content = self.get_content(path, None)
                    if content:
                        loaded += 1
                        total_bytes += len(content)
                    else:
                        skipped += 1
        if log_func:
            log_func(
                f"[CACHE] preloaded static assets files={loaded} "
                f"bytes={total_bytes} skipped={skipped}"
            )


STATIC_ASSET_CACHE = StaticAssetCache()
LPKG_METADATA_CACHE = {}
LPKG_METADATA_LOCK = threading.Lock()


def maybe_rewrite_lpkg_header(body):
    if LPKG_REWRITE_VERSION is None:
        return body
    if len(body) >= 8 and body[1:3] == b"\x00\x00" and body[4:8] == b"LPKG":
        current_a = body[0]
        current_b = body[3]
        if current_a == current_b and current_a != LPKG_REWRITE_VERSION:
            return bytes((LPKG_REWRITE_VERSION, 0, 0, LPKG_REWRITE_VERSION)) + body[4:]
    return body


def package_needs_lpkg_rewrite(path):
    if LPKG_REWRITE_VERSION is None:
        return False
    try:
        with open(path, "rb") as handle:
            header = handle.read(8)
    except OSError:
        return False
    if len(header) < 8 or header[1:3] != b"\x00\x00" or header[4:8] != b"LPKG":
        return False
    return header[0] == header[3] and header[0] != LPKG_REWRITE_VERSION


def rewritten_lpkg_checksum(path, size, mtime):
    needs_lpkg_rewrite = package_needs_lpkg_rewrite(path)
    is_indominus_asset = is_indominus_offer_asset(path)
    if not needs_lpkg_rewrite and not is_indominus_asset:
        return None
    key = (path, size, mtime, LPKG_REWRITE_VERSION, "indominus-hardcash-addon-v3")
    with LPKG_METADATA_LOCK:
        cached = LPKG_METADATA_CACHE.get(key)
        if cached:
            return cached

    if is_indominus_asset:
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return None
        body = maybe_rewrite_lpkg_header(body)
        body, indominus_ready = patch_indominus_offer(path, body)
        if not indominus_ready and not needs_lpkg_rewrite:
            return None
        checksum = hashlib.md5(body).hexdigest()
        with LPKG_METADATA_LOCK:
            LPKG_METADATA_CACHE[key] = checksum
        return checksum

    digest = hashlib.md5()
    try:
        with open(path, "rb") as handle:
            first = handle.read(1024 * 1024)
            if not first:
                return None
            digest.update(maybe_rewrite_lpkg_header(first))
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    checksum = digest.hexdigest()
    with LPKG_METADATA_LOCK:
        LPKG_METADATA_CACHE[key] = checksum
    return checksum


class GuestSaveCache:
    def __init__(self):
        self.cache = {}  # username -> (mtime, data)
        self.lock = threading.Lock()

    def get(self, username, path, log):
        with self.lock:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                return None

            cached = self.cache.get(username)
            if cached and cached[0] == mtime:
                return copy.deepcopy(cached[1])
            return None

    def set(self, username, path, data):
        with self.lock:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                return
            self.cache[username] = (mtime, copy.deepcopy(data))


GUEST_SAVE_CACHE = GuestSaveCache()


PLAYER_LOCKS_LOCK = threading.Lock()
PLAYER_LOCKS = {}
SAVE_SESSION_REGISTRY = SaveSessionRegistry(RUN_DIR, SAVE_SESSION_TTL_SECONDS)

def get_player_lock(username):
    with PLAYER_LOCKS_LOCK:
        if username not in PLAYER_LOCKS:
            PLAYER_LOCKS[username] = threading.Lock()
        return PLAYER_LOCKS[username]


LOGGER_INSTANCE = None


class Logger:
    def __init__(self, quiet=False, mode="hourly"):
        global LOGGER_INSTANCE
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(PLAYER_LOG_DIR, exist_ok=True)
        os.makedirs(PLAYER_SESSION_LOG_DIR, exist_ok=True)
        self.mode = "session" if mode == "test" else mode
        if self.mode not in ("hourly", "session"):
            self.mode = "hourly"
        self.hour_stamp = log_hour_stamp()
        session_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.mode == "session":
            self.path = os.path.join(LOG_DIR, f"jpb_session_{session_stamp}.log")
        else:
            self.path = os.path.join(LOG_DIR, f"jpb_current_{self.hour_stamp}.log")
        self.quiet = quiet
        self.file = open(self.path, "a", encoding="utf-8")
        
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.player_handles = {}
        
        cleanup_old_log_files()
        
        self.thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.thread.start()
        
        LOGGER_INSTANCE = self

    def write(self, msg):
        self.queue.put(("main", None, msg))

    def write_player(self, player_id, msg, session_id=""):
        self.queue.put(("player", (player_id, session_id), msg))

    def _rotate_if_needed(self):
        if self.mode != "hourly":
            return
        hour_stamp = log_hour_stamp()
        if hour_stamp == self.hour_stamp:
            return
        try:
            self.file.write(f"[{now_text()}] [LOG] rotating hourly log to {hour_stamp}\n")
            self.file.flush()
            self.file.close()
        except OSError:
            pass
        self.hour_stamp = hour_stamp
        self.path = os.path.join(LOG_DIR, f"jpb_current_{self.hour_stamp}.log")
        self.file = open(self.path, "a", encoding="utf-8")
        self.file.write(f"[{now_text()}] [LOG] started hourly log {self.path}\n")
        self.file.flush()
        cleanup_old_log_files()

    def _writer_loop(self):
        while not self.stop_event.is_set():
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                try:
                    self.file.flush()
                    for handle in list(self.player_handles.values()):
                        handle.flush()
                except OSError:
                    pass
                continue

            log_type, target_id, msg = item
            timestamp = now_text()
            line = f"[{timestamp}] {msg}"

            if log_type == "main":
                self._rotate_if_needed()
                if not self.quiet:
                    print(line, flush=True)
                try:
                    self.file.write(line + "\n")
                except OSError:
                    pass
            elif log_type == "player" and target_id:
                os.makedirs(PLAYER_SESSION_LOG_DIR, exist_ok=True)
                player_id, session_id = target_id if isinstance(target_id, tuple) else (target_id, "")
                safe_id = safe_log_identity(player_id)
                safe_session = safe_log_identity(session_id) if session_id else "unknown_session"
                handle_key = f"{safe_id}_{safe_session}"
                handle = self.player_handles.get(handle_key)
                if not handle:
                    path = os.path.join(PLAYER_SESSION_LOG_DIR, f"{safe_id}_{safe_session}.log")
                    try:
                        handle = open(path, "a", encoding="utf-8")
                        self.player_handles[handle_key] = handle
                    except OSError:
                        pass
                if handle:
                    try:
                        handle.write(line + "\n")
                        handle.flush()
                    except OSError:
                        pass

            self.queue.task_done()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2.0)
        try:
            self.file.flush()
            self.file.close()
        except OSError:
            pass
        for handle in self.player_handles.values():
            try:
                handle.flush()
                handle.close()
            except OSError:
                pass


def safe_log_identity(value):
    text = str(value or "unknown")
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in text) or "unknown"


def write_player_log(player_id, msg, session_id=""):
    global LOGGER_INSTANCE
    if LOGGER_INSTANCE is not None:
        LOGGER_INSTANCE.write_player(player_id, msg, session_id=session_id)
    else:
        os.makedirs(PLAYER_SESSION_LOG_DIR, exist_ok=True)
        safe_session = safe_log_identity(session_id) if session_id else datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(PLAYER_SESSION_LOG_DIR, f"{safe_log_identity(player_id)}_{safe_session}.log")
        line = f"[{now_text()}] {msg}"
        with PLAYER_LOCKS_LOCK:
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass


def start_logcat_capture(args, log):
    if not args.adb_logcat:
        return None

    markers = [item.strip().lower() for item in args.logcat_filter.split(",") if item.strip()]
    if not markers:
        markers = list(DEFAULT_LOGCAT_MARKERS)

    adb_path = args.adb_path
    if args.clear_logcat:
        try:
            subprocess.run([adb_path, "logcat", "-c"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            log("[LOGCAT] cleared old Android log buffer")
        except Exception as exc:
            log(f"[LOGCAT] could not clear old Android log buffer: {exc}")

    try:
        log(f"[LOGCAT] starting capture: {adb_path} logcat -v time")
        proc = subprocess.Popen(
            [adb_path, "logcat", "-v", "time"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        log(f"[LOGCAT] failed to start adb logcat: {exc}")
        return None

    def worker():
        crash_passthrough = 0
        try:
            for line in proc.stdout:
                stripped = line.rstrip()
                lower = stripped.lower()
                starts_crash = (
                    "fatal signal" in lower
                    or " f/debug" in lower
                    or "backtrace:" in lower
                    or "tombstone" in lower
                )
                if starts_crash:
                    crash_passthrough = max(crash_passthrough, args.logcat_crash_lines)
                keep = crash_passthrough > 0 or any(marker in lower for marker in markers)
                if keep:
                    log(f"[LOGCAT] {stripped}")
                if crash_passthrough > 0:
                    crash_passthrough -= 1
        except Exception as exc:
            log(f"[LOGCAT] capture stopped with error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return proc


def e_null():
    return bytes([T_NULL])


def e_bool(value):
    return bytes([T_BOOL, 1 if value else 0])


def e_byte(value):
    return bytes([T_BYTE, value & 0xFF])


def e_short(value):
    return bytes([T_SHORT]) + struct.pack(">h", value)


def e_int(value):
    return bytes([T_INT]) + struct.pack(">i", value)


def e_long(value):
    return bytes([T_LONG]) + struct.pack(">q", value)


def is_signed_long_value(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -(1 << 63) <= value <= (1 << 63) - 1
    )


def is_smartfox_long_array(value, allow_empty=True):
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(is_signed_long_value(item) for item in value)
    )


def e_long_arr(values):
    """Encode a homogeneous SmartFox long array without narrowing zeroes."""
    items = [int(value) for value in values]
    return (
        bytes([T_LONG_ARR])
        + struct.pack(">H", len(items))
        + b"".join(struct.pack(">q", value) for value in items)
    )


def e_float(value):
    return bytes([T_FLOAT]) + struct.pack(">f", value)


def e_double(value):
    return bytes([T_DOUBLE]) + struct.pack(">d", value)


def e_str(value):
    data = str(value).encode("utf-8")
    return bytes([T_UTF]) + struct.pack(">H", len(data)) + data


def e_arr(items):
    return bytes([T_SFS_ARRAY]) + struct.pack(">H", len(items)) + b"".join(items)


def e_obj(fields):
    body = b""
    for key, value in fields:
        key_data = str(key).encode("utf-8")
        body += struct.pack(">H", len(key_data)) + key_data + value
    return bytes([T_SFS_OBJECT]) + struct.pack(">H", len(fields)) + body


def e_value(value):
    if value is None:
        return e_null()
    if isinstance(value, bool):
        return e_bool(value)
    if isinstance(value, int):
        if -2147483648 <= value <= 2147483647:
            return e_int(value)
        return e_long(value)
    if isinstance(value, float):
        return e_float(value)
    if isinstance(value, str):
        return e_str(value)
    if isinstance(value, list):
        return e_arr([e_value(item) for item in value])
    if isinstance(value, dict):
        return e_obj([(key, e_value(item)) for key, item in value.items()])
    return e_str(str(value))


def e_battle_field_value(key, value):
    """Keep batl.104 as the long array used by the native storage schema."""
    if str(key) == "104" and is_smartfox_long_array(value):
        return e_long_arr(value)
    return e_value(value)


def e_battle_content(content):
    content_value = content if isinstance(content, dict) else {}
    return e_obj([
        (key, e_battle_field_value(key, value))
        for key, value in content_value.items()
    ])


def e_storage_content(key, content):
    if str(key) == "batl":
        return e_battle_content(content)
    return e_value(content)


def get_online_option_value(key, cached_values):
    key = str(key)
    defaults = battle_online_option_defaults()
    rotating_offers, _duration_days, _rotation_index = current_offer_rotation()
    if key in rotating_offers:
        return str(rotating_offers[key]), "server-rotation"
    if key in ONLINE_OPTIONS_FILE_PREFERRED_KEYS and key in cached_values:
        return str(cached_values[key]), "cache-preferred"
    if key in defaults:
        return defaults[key], "battle-default"
    if key in cached_values:
        return str(cached_values[key]), "cache"
    return None, "missing"


def online_options_binary_overrides():
    overrides = {
        key: value
        for key, value in battle_online_option_defaults().items()
        if key not in ONLINE_OPTIONS_FILE_PREFERRED_KEYS
    }
    rotating_offers, _duration_days, _rotation_index = current_offer_rotation()
    overrides.update(rotating_offers)
    return overrides


def battle_random_bot_ids(requested_count):
    try:
        count_value = int(requested_count)
    except (TypeError, ValueError):
        count_value = BATTLE_RANDOM_BOT_MAX
    count_value = max(1, min(count_value, BATTLE_RANDOM_BOT_MAX))
    return [BATTLE_RANDOM_BOT_ID_BASE + index for index in range(1, count_value + 1)]


def battle_random_bot_account_tokens(bot_ids):
    return [f"{BATTLE_RANDOM_BOT_ACCOUNT_PREFIX}-{bot_id}" for bot_id in bot_ids]


def empty_notification_fields():
    return [("ne", e_int(0)), ("e", e_arr([]))]


def scheduler_event_count(log=None, reason=""):
    """Opt-in probe for recovering the native scheduler/promo payload shape."""
    count_value = 1 if PROMO_SCHEDULER_PROBE_ENABLED else 0
    if log and PROMO_SCHEDULER_PROBE_ENABLED:
        suffix = f" reason={reason}" if reason else ""
        log(f"[PROMO-PROBE] reporting scheduler event count={count_value}{suffix}")
    return count_value


def _catalog_int(value, default=0):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def iap_store_catalog_products(cached_values):
    # c.rp is the product catalogue used to construct the market item list.
    # It must resolve the same rotating onlineoptions snapshot as c.pb.
    # Reading only the donor file here left c.rp on Build_HotAirBalloon_5x5
    # while c.pb advertised Indominus, so the client could never match the
    # configured ADDON_DINO_ID to an available product.
    effective_values = dict(cached_values if isinstance(cached_values, dict) else {})
    effective_values.update(battle_online_option_defaults())
    products = []
    for index, pack_key in enumerate(IAP_HARDCASH_PACK_KEYS, start=1):
        sku = str(effective_values.get(IAP_MARKET_ITEM_ID_KEYS[index - 1], "")).strip()
        if not sku or sku == "0":
            continue
        products.append({
            "k": sku,
            "pr": IAP_DEFAULT_CATALOG_PRICE,
            "rt": 1,
            "ra": _catalog_int(effective_values.get(pack_key), 0),
            "u": "",
            "t": IAP_DEFAULT_CATALOG_PRICE_TEXT,
            "d": IAP_DEFAULT_CATALOG_PRICE_TEXT,
            "i": "resource_hardcash",
        })
    return products


def iap_product_wire_object(product):
    return e_obj([
        ("k", e_str(product.get("k", ""))),
        ("pr", e_long(_catalog_int(product.get("pr"), IAP_DEFAULT_CATALOG_PRICE))),
        ("rt", e_long(_catalog_int(product.get("rt"), 1))),
        ("ra", e_long(_catalog_int(product.get("ra"), 0))),
        ("u", e_str(product.get("u", ""))),
        ("t", e_str(product.get("t", IAP_DEFAULT_CATALOG_PRICE_TEXT))),
        ("d", e_str(product.get("d", IAP_DEFAULT_CATALOG_PRICE_TEXT))),
        ("i", e_str(product.get("i", "resource_hardcash"))),
    ])


def iap_resource_wire_object(product):
    return e_obj([
        ("t", e_long(_catalog_int(product.get("rt"), 1))),
        ("a", e_long(_catalog_int(product.get("ra"), 0))),
    ])


def make_iap_store_catalog_fields(params, cached_values):
    payload = params if isinstance(params, dict) else {}
    store_name = payload.get("st") if isinstance(payload.get("st"), str) else IAP_GPLAY_STORE_NAME
    products = iap_store_catalog_products(cached_values if isinstance(cached_values, dict) else {})
    product_objects = [iap_product_wire_object(product) for product in products]
    resource_objects = [iap_resource_wire_object(product) for product in products]
    product_ids = [str(product.get("k", "")) for product in products if product.get("k")]

    fields = []
    serial = payload.get("s")
    if isinstance(serial, int):
        fields.append(("s", e_long(serial)))
    hash_value = payload.get("h")
    if isinstance(hash_value, str):
        fields.append(("h", e_str(hash_value)))
    if isinstance(payload.get("st"), str):
        fields.append(("st", e_str(payload.get("st"))))

    fields.extend([
        ("ok", e_bool(True)),
        ("v", e_int(0)),
        ("c", e_int(0)),
        ("n", e_int(0)),
        ("vs", e_arr([])),
        *empty_notification_fields(),
        ("r", e_arr(resource_objects)),
        ("g", e_arr([e_str(product_id) for product_id in product_ids])),
        ("m", e_obj([
            ("ok", e_bool(True)),
            ("st", e_str(store_name)),
            ("p", e_arr(product_objects)),
            ("g", e_arr([e_str(product_id) for product_id in product_ids])),
            ("r", e_arr(resource_objects)),
        ])),
        ("f", e_bool(True)),
        ("d", e_str(store_name)),
        ("p", e_arr(product_objects)),
    ])
    return fields, len(products)


def make_game_random_search_fields(
    request_params,
    log=None,
    current_save_id="",
    session_state=None,
    current_saved=None,
):
    payload = request_params.get("p") if isinstance(request_params, dict) else None
    if not isinstance(payload, dict):
        payload = request_params if isinstance(request_params, dict) else {}

    current_save_key = str(current_save_id or "")
    opponent_saves = battle_opponent_save_targets(
        current_save_key,
        current_saved,
        log,
    )
    bot_ids = (
        battle_random_bot_ids(payload.get("n"))
        if opponent_saves
        else []
    )
    bot_account_tokens = battle_random_bot_account_tokens(bot_ids)
    if isinstance(session_state, dict) and opponent_saves:
        owner_map = session_state.setdefault("visit_owner_map", {})
        if not isinstance(owner_map, dict):
            owner_map = {}
            session_state["visit_owner_map"] = owner_map
        for index, bot_id in enumerate(bot_ids):
            owner_map[bot_id] = opponent_saves[index % len(opponent_saves)]
    id_array = e_arr([e_long(bot_id) for bot_id in bot_ids])
    fa_array = e_arr([e_str(account_token) for account_token in bot_account_tokens])

    fields = []
    serial = payload.get("s")
    if isinstance(serial, int):
        fields.append(("s", e_long(serial)))
    hash_value = payload.get("h")
    if isinstance(hash_value, str):
        fields.append(("h", e_str(hash_value)))

    fields.extend([
        ("r", e_int(0)),
        ("ok", e_bool(True)),
        ("v", e_int(0)),
        ("c", e_int(0)),
        ("n", e_int(0)),
        ("vs", e_arr([])),
    ])
    fields.extend(empty_notification_fields())
    fields.extend([
        ("id", id_array),
        ("fa", fa_array),
        ("p", e_obj([
            ("id", id_array),
            ("fa", fa_array),
            ("v", e_int(0)),
            ("c", e_int(0)),
            ("n", e_int(0)),
            ("vs", e_arr([])),
            *empty_notification_fields(),
        ])),
    ])

    if log:
        log(
            f"[BATTLE] c.grs random opponents sent "
            f"requested={payload.get('n')!r} count={len(bot_ids)} "
            f"eligible_saves={len(opponent_saves)}"
        )
    return fields


def make_battle_online_storage_read_fields(command, sequence, version, content):
    content_value = content if isinstance(content, dict) else {}
    common_fields = [
        ("k", e_str("batl")),
        ("v", e_int(int(version or 0))),
        ("c", e_battle_content(content_value)),
        ("r", e_int(0)),
        ("r2", e_int(0)),
        ("e", e_arr([])),
        ("ok", e_bool(True)),
        ("n", e_int(0)),
        ("vs", e_arr([])),
        ("ne", e_int(0)),
    ]
    fields = [
        ("s", e_long(sequence)),
        ("co", e_str(command)),
    ]
    fields.extend(common_fields)
    existing_names = {name for name, _ in fields}
    for field_key, field_value in content_value.items():
        field_name = str(field_key)
        if field_name in existing_names:
            continue
        fields.append((field_name, e_battle_field_value(field_name, field_value)))
        existing_names.add(field_name)
    fields.append(("p", e_obj([
        ("s", e_long(sequence)),
        ("co", e_str(command)),
        *common_fields,
    ])))
    return fields


def make_battle_online_storage_write_ack_fields(command, sequence, version, content):
    content_value = content if isinstance(content, dict) else {}
    fields = [
        ("s", e_long(sequence)),
        ("co", e_str(command)),
        ("k", e_str("batl")),
        ("ok", e_bool(True)),
        ("v", e_int(int(version or 0))),
        ("c", e_battle_content(content_value)),
        ("r", e_int(0)),
        ("r2", e_int(0)),
        ("e", e_arr([])),
        ("n", e_int(0)),
        ("vs", e_arr([])),
        ("ne", e_int(0)),
    ]
    existing_names = {name for name, _ in fields}
    for field_key, field_value in content_value.items():
        field_name = str(field_key)
        if field_name in existing_names:
            continue
        fields.append((field_name, e_battle_field_value(field_name, field_value)))
        existing_names.add(field_name)
    fields.append(("p", e_obj([
        ("s", e_long(sequence)),
        ("co", e_str(command)),
        ("k", e_str("batl")),
        ("ok", e_bool(True)),
        ("v", e_int(int(version or 0))),
        ("c", e_battle_content(content_value)),
        ("r", e_int(0)),
        ("r2", e_int(0)),
        ("e", e_arr([])),
        ("n", e_int(0)),
        ("vs", e_arr([])),
        ("ne", e_int(0)),
    ])))
    return fields


def make_commonstorage2_starray_read_fields(
    command, sequence, key, version, owner, result, content, extra_rows=None
):
    version_value = int(version or 0)
    owner_value = int(owner or 0)
    result_value = int(result or 0)
    object_key = str(key or "")
    rows = []

    def add_row(row_key, row_version, row_owner, row_result, row_content):
        if row_content is None:
            return
        row_key = str(row_key or "")
        rows.append(e_obj([
            ("k", e_str(row_key)),
            ("c", e_storage_content(row_key, row_content)),
            ("o", e_long(int(row_owner or 0))),
            ("r", e_int(int(row_result or 0))),
            ("r2", e_int(0)),
            ("e", e_arr([])),
            ("v", e_int(int(row_version or 0))),
            ("ok", e_bool(True)),
            ("n", e_int(0)),
            ("vs", e_arr([])),
            ("ne", e_int(0)),
        ]))

    add_row(object_key, version_value, owner_value, result_value, content)
    for row in extra_rows or []:
        add_row(
            row.get("key"),
            row.get("version"),
            row.get("owner"),
            row.get("result"),
            row.get("content"),
        )

    fields = [
        ("s", e_long(sequence)),
        ("co", e_str(command)),
        ("k", e_str(object_key)),
        ("v", e_int(version_value)),
        ("st", e_arr(rows)),
        ("ok", e_bool(True)),
        ("n", e_int(0)),
        ("vs", e_arr([])),
        ("ne", e_int(0)),
        ("e", e_arr([])),
        ("p", e_arr(rows)),
    ]
    return fields


def make_profile_common_storage_write_ack_fields(command, sequence, version, owner, result, content):
    content_value = content if isinstance(content, dict) else {}
    version_value = int(version or 0)
    owner_value = int(owner or 1)
    result_value = int(result or 0)
    common_fields = [
        ("k", e_str("prof")),
        ("c", e_value(content_value)),
        ("o", e_long(owner_value)),
        ("r", e_int(result_value)),
        ("r2", e_int(0)),
        ("e", e_arr([])),
        ("v", e_int(version_value)),
        ("ok", e_bool(True)),
        ("n", e_int(0)),
        ("vs", e_arr([])),
        ("ne", e_int(0)),
    ]
    return [
        ("s", e_long(sequence)),
        ("co", e_str(command)),
        *common_fields,
        ("p", e_obj([
            ("s", e_long(sequence)),
            ("co", e_str(command)),
            *common_fields,
        ])),
    ]


def make_direct_generic_fields(params):
    payload = params if isinstance(params, dict) else {}
    fields = []
    for key in ("s", "h", "k", "t", "as"):
        if key in payload:
            fields.append((key, e_value(payload[key])))
    fields.extend([
        ("r", e_int(0)),
        ("ok", e_bool(True)),
        ("v", e_int(0)),
        ("c", e_int(0)),
        ("n", e_int(0)),
        ("vs", e_arr([])),
    ])
    fields.extend(empty_notification_fields())
    return fields


def e_utf_arr(items):
    body = bytes([T_UTF_ARR]) + struct.pack(">H", len(items))
    for item in items:
        item_data = str(item).encode("utf-8")
        body += struct.pack(">H", len(item_data)) + item_data
    return body


def build_packet(ctrl, action, fields, compressed=False):
    inner = e_obj(fields)
    outer = e_obj([("c", e_byte(ctrl)), ("a", e_short(action)), ("p", inner)])
    if compressed:
        body = zlib.compress(outer, level=6)
        header = 0xA0
    else:
        body = outer
        header = 0x80
    if len(body) > 65535:
        return bytes([header | 0x08]) + struct.pack(">I", len(body)) + body
    return bytes([header]) + struct.pack(">H", len(body)) + body


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, size):
        if self.pos + size > len(self.data):
            raise ValueError(f"short read at {self.pos}, need {size}, have {len(self.data) - self.pos}")
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def u8(self):
        return self.read(1)[0]

    def s8(self):
        return struct.unpack("b", self.read(1))[0]

    def u16(self):
        return struct.unpack(">H", self.read(2))[0]

    def s16(self):
        return struct.unpack(">h", self.read(2))[0]

    def s32(self):
        return struct.unpack(">i", self.read(4))[0]

    def s64(self):
        return struct.unpack(">q", self.read(8))[0]

    def f32(self):
        return struct.unpack(">f", self.read(4))[0]

    def f64(self):
        return struct.unpack(">d", self.read(8))[0]

    def value(self):
        type_id = self.u8()
        if type_id == T_NULL:
            return None
        if type_id == T_BOOL:
            return self.u8() != 0
        if type_id == T_BYTE:
            return self.s8()
        if type_id == T_SHORT:
            return self.s16()
        if type_id == T_INT:
            return self.s32()
        if type_id == T_LONG:
            return self.s64()
        if type_id == T_FLOAT:
            return self.f32()
        if type_id == T_DOUBLE:
            return self.f64()
        if type_id == T_UTF:
            size = self.u16()
            return self.read(size).decode("utf-8", "replace")
        if type_id == T_BOOL_ARR:
            size = self.u16()
            return [self.u8() != 0 for _ in range(size)]
        if type_id == T_BYTE_ARR:
            size = self.s32()
            return {"__bytes__": self.read(size).hex()}
        if type_id == T_SHORT_ARR:
            size = self.u16()
            return [self.s16() for _ in range(size)]
        if type_id == T_INT_ARR:
            size = self.u16()
            return [self.s32() for _ in range(size)]
        if type_id == T_LONG_ARR:
            size = self.u16()
            return [self.s64() for _ in range(size)]
        if type_id == T_FLOAT_ARR:
            size = self.u16()
            return [self.f32() for _ in range(size)]
        if type_id == T_DOUBLE_ARR:
            size = self.u16()
            return [self.f64() for _ in range(size)]
        if type_id == T_UTF_ARR:
            size = self.u16()
            result = []
            for _ in range(size):
                item_size = self.u16()
                result.append(self.read(item_size).decode("utf-8", "replace"))
            return result
        if type_id == T_SFS_ARRAY:
            return self.array()
        if type_id == T_SFS_OBJECT:
            return self.object()
        return f"<unknown_sfs_type_{type_id}>"

    def object(self):
        size = self.u16()
        result = {}
        for _ in range(size):
            key_size = self.u16()
            key = self.read(key_size).decode("utf-8", "replace")
            result[key] = self.value()
        return result

    def array(self):
        size = self.u16()
        return [self.value() for _ in range(size)]

    def sfs_object(self):
        type_id = self.u8()
        if type_id != T_SFS_OBJECT:
            raise ValueError(f"expected SFS_OBJECT type 18, found {type_id}")
        return self.object()


def decode_packet(body):
    outer = Reader(body).sfs_object()
    return outer.get("c", 0), outer.get("a", 0), outer.get("p", {}), outer


def short_hex(data, limit=512):
    text = data[:limit].hex(" ")
    if len(data) > limit:
        text += f" ... (+{len(data) - limit} bytes)"
    return text


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SFSStream:
    def __init__(self, conn):
        self.conn = conn
        self.buffer = b""

    def fill(self, timeout):
        self.conn.settimeout(timeout)
        chunk = self.conn.recv(8192)
        if not chunk:
            raise ConnectionError(CLIENT_CLOSED)
        self.buffer += chunk

    def read_frame(self, timeout=30.0):
        while len(self.buffer) < 3:
            self.fill(timeout)

        header = self.buffer[0]
        is_big = bool(header & 0x08)
        is_compressed = bool(header & 0x20)

        if is_big:
            while len(self.buffer) < 5:
                self.fill(timeout)
            body_size = struct.unpack(">I", self.buffer[1:5])[0]
            header_size = 5
        else:
            body_size = struct.unpack(">H", self.buffer[1:3])[0]
            header_size = 3

        total_size = header_size + body_size
        while len(self.buffer) < total_size:
            self.fill(timeout)

        frame = self.buffer[:total_size]
        body = self.buffer[header_size:total_size]
        self.buffer = self.buffer[total_size:]

        if is_compressed:
            decoded_body = zlib.decompress(body)
        else:
            decoded_body = body

        return {
            "header": header,
            "body_size": body_size,
            "is_big": is_big,
            "is_compressed": is_compressed,
            "frame": frame,
            "body": decoded_body,
        }


def make_login_profile_android_confirmed(username, zone, login_params):
    # Native login processing passes `t` directly to the same millisecond
    # network-clock baseline later refreshed by c.gt. Sending Unix seconds here
    # leaves that clock frozen near zero until the client's delayed c.gt poll.
    now_ms = current_network_time()
    has_save = bool(login_params.get("_server_has_save"))
    saved = login_params.get("_server_save_data") if isinstance(login_params, dict) else {}
    include_hardcash = not has_save or hardcash_balance_is_known(saved)
    hardcash = (
        saved_hardcash_estimate(saved)
        if hardcash_balance_is_known(saved)
        else 3 if not has_save else None
    )
    resource_fields = [
        ("Softcash", e_int(0)),
        ("SoftCash", e_int(0)),
    ]
    if include_hardcash:
        resource_fields[:0] = [
            ("Hardcash", e_int(hardcash)),
            ("HardCash", e_int(hardcash)),
        ]
    resources = e_obj(resource_fields)
    fields = [
        # FUN_003dd558: login success gate and salt storage.
        ("r", e_str("ok")),
        ("s", e_str("jpb-v38-local-salt")),
        # FUN_003dd344 unconditionally reads getString("as") after success.
        # The native age gate uses /network/coppa/age status; "n" means the
        # player has not confirmed an age and reopens the birthday picker.
        # "o" is the client's over-age state and persists across logins.
        ("as", e_str("o")),
        # Recovered PTR_DAT_00592e0c/10/14/18 fields, all read numerically.
        ("t", e_long(now_ms)),
        ("ll", e_long(0)),
        ("a", e_long(1)),
        ("oo", e_long(current_online_options_version())),
    ]
    if include_hardcash:
        # Never impose a guessed balance on a legacy save without wallet
        # metadata. Known balances and new-account defaults remain explicit.
        fields.extend([
            ("hc", e_long(hardcash)),
            ("Hardcash", e_long(hardcash)),
            ("HardCash", e_long(hardcash)),
        ])
    fields.append(("Resources", resources))
    if not has_save and bool(login_params.get("_server_include_new_player_hint")):
        # The Android handler appears to treat the presence of `np` as the
        # new-player branch, regardless of boolean value. Omit it entirely
        # by default, matching the donor server's login-new-account=omit flow.
        fields.append(("np", e_bool(True)))
    return fields

def make_handshake_response():
    return build_packet(0, 0, [
        ("tk", e_str("DummyToken")),
        ("ct", e_int(100000)),
        ("ms", e_int(10000000)),
    ])


def make_login_ok(username, zone, login_params, _profile_name="android_confirmed", shape="normal"):
    p_fields = make_login_profile_android_confirmed(username, zone, login_params)
    # SmartFox login `rs` is reconnection seconds. Earlier builds reused it as
    # a save-present flag, which made existing-save logins advertise rs=1.
    reconnect_seconds = 300
    fields = [
        ("rl", e_arr([])),
        ("id", e_int(1)),
        ("un", e_str(username)),
        ("pi", e_short(1)),
        ("rs", e_short(reconnect_seconds)),
        ("zn", e_str(zone)),
    ]
    if shape == "normal":
        fields.append(("p", e_obj(p_fields)))
    elif shape == "missing_p":
        pass
    elif shape == "null_p":
        fields.append(("p", e_null()))
    elif shape == "string_p":
        fields.append(("p", e_str(compact_json(dict((k, "<value>") for k, _v in p_fields)))))
    else:
        raise ValueError(f"unknown login response shape: {shape}")
    return build_packet(0, 1, fields)


def make_login_reject(username, zone, message):
    return build_packet(0, 1, [
        ("rl", e_arr([])),
        ("id", e_int(-1)),
        ("un", e_str(username)),
        ("pi", e_short(1)),
        ("rs", e_short(300)),
        ("zn", e_str(zone)),
        ("p", e_obj([
            ("r", e_str("ko")),
            ("ec", e_int(403)),
            ("em", e_str(message)),
            ("err", e_str(message)),
            ("message", e_str(message)),
        ])),
    ])


def make_ext_response(cmd, fields):
    return build_packet(1, 13, [
        ("c", e_str(cmd or "unknown")),
        ("p", e_obj(fields)),
    ])


def guest_save_path(username):
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in username)
    return os.path.join(SAVE_DIR, f"{safe_name or 'guest'}.json")


def has_guest_save(username):
    path = guest_save_path(username)
    try:
        return os.path.getsize(path) > 16
    except OSError:
        return False


def save_identity(username, login_params):
    if isinstance(login_params, dict):
        device_uid = login_params.get("du") or login_params.get("DID")
        if device_uid:
            return f"D-{device_uid}"
    return username


def owner_id_for_save_id(save_id):
    # Keep owner ids positive and away from tiny sentinel values such as 0/1.
    return 1000000 + (zlib.crc32(str(save_id).encode("utf-8")) % 900000000)


def save_id_for_owner_id(owner_id):
    for save_id in list_guest_save_ids():
        if owner_id_for_save_id(save_id) == owner_id:
            return save_id
    return ""


def list_guest_save_ids():
    if not os.path.isdir(SAVE_DIR):
        return []
    result = []
    for name in os.listdir(SAVE_DIR):
        if not name.endswith(".json"):
            continue
        save_id = name[:-5]
        path = guest_save_path(save_id)
        try:
            if os.path.getsize(path) > 16:
                result.append(save_id)
        except OSError:
            pass
    return sorted(set(result))


def save_has_visitable_park(save_id):
    try:
        data = load_guest_save(save_id, lambda _m: None)
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("park"), dict) and isinstance(data.get("road"), dict)


def save_has_battle_opponent_state(data):
    if not isinstance(data, dict):
        return False
    profile_entry = data.get("prof")
    profile = profile_entry.get("c") if isinstance(profile_entry, dict) else None
    if (
        not isinstance(profile, dict)
        or _as_int(profile.get("5"), 0) < 1
    ):
        return False
    battle_entry = data.get("batl")
    battle = battle_entry.get("c") if isinstance(battle_entry, dict) else None
    slots = battle.get("104") if isinstance(battle, dict) else None
    return (
        is_smartfox_long_array(slots, allow_empty=False)
    )


def battle_opponent_save_targets(current_save_id, current_saved=None, log=None):
    current_key = str(current_save_id or "")
    if save_has_battle_opponent_state(current_saved):
        return [current_key] if current_key else []
    candidates = []
    for save_id in list_guest_save_ids():
        if save_id == current_key:
            continue
        try:
            candidate = load_guest_save(save_id, lambda _message: None)
        except Exception:
            continue
        if save_has_battle_opponent_state(candidate):
            candidates.append(save_id)
    if log and not candidates:
        log(
            "[BATTLE] c.grs has no persisted opponent with both prof and batl; "
            "returning an empty opponent list"
        )
    return candidates


def list_visitable_parks(current_save_id=""):
    parks = []
    for save_id in list_guest_save_ids():
        if not save_has_visitable_park(save_id):
            continue
        parks.append({
            "save_id": save_id,
            "owner_id": owner_id_for_save_id(save_id),
            "name": save_id[2:] if save_id.startswith("D-") else save_id,
        })
    current_key = str(current_save_id or "")
    parks.sort(key=lambda item: (item["save_id"] == current_key, item["save_id"]))
    return parks


def choose_visit_save_id(current_save_id, requested_owner_id, session_state, log):
    if not isinstance(session_state, dict):
        session_state = {}
    owner_map = session_state.setdefault("visit_owner_map", {})
    try:
        owner_id = int(requested_owner_id)
    except Exception:
        owner_id = 0
    if owner_id <= 1:
        return ""
    if owner_id in owner_map:
        return owner_map[owner_id]

    current_key = str(current_save_id or "")
    direct = save_id_for_owner_id(owner_id)
    if direct and direct != current_key:
        owner_map[owner_id] = direct
        return direct
    if direct == current_key:
        log(f"[VISIT] direct owner_id={owner_id} matched current save; choosing another park")

    parks = [item for item in list_visitable_parks(current_save_id) if item["save_id"] != current_key]
    if not parks:
        parks = list_visitable_parks(current_save_id)
    if not parks:
        return ""
    index = int(session_state.get("visit_rotation_index", 0)) % len(parks)
    session_state["visit_rotation_index"] = index + 1
    target = parks[index]["save_id"]
    owner_map[owner_id] = target
    log(
        f"[VISIT] mapped requested owner_id={owner_id} to save_id={target!r} "
        f"rotation_index={index}/{len(parks)}"
    )
    return target


def visit_identity_value(requested_owner_id, save_id, slot):
    seed = f"{requested_owner_id}:{save_id}:{slot}".encode("utf-8")
    high = 0x10000000 + (zlib.crc32(seed) & 0x0fffffff)
    low = zlib.crc32(seed[::-1]) & 0xffffffff
    return (high << 32) | low


def rewrite_visit_content_identity(key, content, requested_owner_id, visit_save_id):
    if not isinstance(content, dict):
        return content
    if key in ("park", "aqpk", "arpk"):
        # Test saves are often copied from the local player. The Android client
        # appears to cache park payloads by these internal ids, so visiting a
        # clone with unchanged ids can visually snap back to the local park.
        # Apply to all park types: Jurassic (park), Aquatic (aqpk), Glacier (arpk).
        content["13"] = visit_identity_value(requested_owner_id, visit_save_id, f"{key}13")
        content["14"] = visit_identity_value(requested_owner_id, visit_save_id, f"{key}14")
    elif key == "gen":
        content["15"] = visit_identity_value(requested_owner_id, visit_save_id, "gen15")
        content["16"] = visit_identity_value(requested_owner_id, visit_save_id, "gen16")
        content["17"] = visit_identity_value(requested_owner_id, visit_save_id, "gen17")
    elif key == "prof":
        content["10"] = f"G-VISIT-{int(requested_owner_id)}"
        content["91"] = f"Random Park {int(requested_owner_id)}"
    return content


def make_random_park_friend_fields(command, sequence, current_save_id, mode, log):
    parks = list_visitable_parks(current_save_id)
    other_parks = [item for item in parks if item["save_id"] != current_save_id]
    visible = other_parks or parks

    def park_level(item):
        if not item:
            return 1
        try:
            return saved_player_level(
                load_guest_save(item["save_id"], lambda _message: None)
            )
        except Exception:
            return 1

    if mode == "random_user_stub":
        owner_id = visible[0]["owner_id"] if visible else 0
        name = visible[0]["name"] if visible else ""
        level = park_level(visible[0]) if visible else 1
        info = compact_json({
            "id": owner_id,
            "un": name,
            "n": name,
            "sn": name,
            "lvl": level,
            "xp": 0,
        }) if visible else ""
        log(f"[FRIENDS] random park stub owner_id={owner_id} save={visible[0]['save_id']!r}" if visible else "[FRIENDS] random park stub unavailable")
        return [
            ("co", e_str(command)),
            ("s", e_long(sequence)),
            ("id", e_long(owner_id)),
            ("fi", e_str(info)),
        ]

    id_values = [e_long(item["owner_id"]) for item in visible]
    friend_values = []
    for item in visible:
        level = park_level(item)
        friend_values.append(e_obj([
            ("id", e_long(item["owner_id"])),
            ("un", e_str(item["name"])),
            ("n", e_str(item["name"])),
            ("sn", e_str(item["name"])),
            ("lvl", e_long(level)),
            ("xp", e_long(0)),
        ]))
    log(f"[FRIENDS] random parks available={len(visible)} total_saves={len(parks)}")
    return [
        ("co", e_str(command)),
        ("s", e_long(sequence)),
        ("id", e_arr(id_values)),
        ("fa", e_arr(friend_values)),
    ]


def normalize_device_id(value):
    return str(value or "").strip().lower()


def _read_and_process_device_links(handle):
    data = json.load(handle)
    result = {"devices": {}, "error": ""}
    if not isinstance(data, dict):
        result["error"] = "root JSON value must be an object"
        return result

    for player in data.get("players", []):
        if not isinstance(player, dict):
            continue
        save_id = str(player.get("save_id") or "").strip()
        if not save_id:
            continue
        devices = player.get("devices", {})
        if isinstance(devices, dict):
            for label, device_id in devices.items():
                normalized = normalize_device_id(device_id)
                if normalized:
                    result["devices"][normalized] = {
                        "save_id": save_id,
                        "label": str(label or "device").strip() or "device",
                    }
        elif isinstance(devices, list):
            for index, device_id in enumerate(devices, start=1):
                normalized = normalize_device_id(device_id)
                if normalized:
                    result["devices"][normalized] = {
                        "save_id": save_id,
                        "label": f"device{index}",
                    }
    return result


_DEVICE_LINKS_FILE_CACHE = None

def load_device_links():
    global _DEVICE_LINKS_FILE_CACHE
    if not os.path.exists(DEVICE_LINKS_FILE):
        return {"devices": {}, "error": ""}

    if _DEVICE_LINKS_FILE_CACHE is None:
        _DEVICE_LINKS_FILE_CACHE = FileCache(
            DEVICE_LINKS_FILE,
            _read_and_process_device_links,
            {"devices": {}, "error": "Initial load failed"}
        )
    return _DEVICE_LINKS_FILE_CACHE.get()


def linked_save_identity(username, login_params):
    device_id = login_device_id(login_params)
    links = load_device_links()
    if links["error"] or not device_id:
        return save_identity(username, login_params), "", links["error"]
    linked = links["devices"].get(normalize_device_id(device_id))
    if linked:
        return linked["save_id"], linked["label"], ""
    return save_identity(username, login_params), "", ""


def stable_account_identity(
    username, login_params, shared_save_id="", saved=None
):
    shared_save_id = str(shared_save_id or "").strip()
    if shared_save_id:
        saved_profile = (
            saved.get("prof", {}).get("c", {})
            if isinstance(saved, dict)
            else {}
        )
        saved_account = (
            str(saved_profile.get("10") or "").strip()
            if isinstance(saved_profile, dict)
            else ""
        )
        if saved_account:
            return saved_account
        digest = hashlib.md5(
            f"linked-save:{shared_save_id}".encode("utf-8")
        ).hexdigest()
        number = int(digest[:8], 16) % 4000000000
        return f"G-{number}"
    if isinstance(login_params, dict):
        device_uid = login_params.get("du") or login_params.get("DID")
        if device_uid:
            digest = hashlib.md5(str(device_uid).encode("utf-8")).hexdigest()
            number = int(digest[:8], 16) % 4000000000
            return f"G-{number}"
    return username


def login_device_id(login_params):
    if not isinstance(login_params, dict):
        return ""
    device_uid = login_params.get("du") or login_params.get("DID") or ""
    return str(device_uid).strip()


def normalize_whitelist_id(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def whitelist_env_enabled():
    raw = os.environ.get("JPB_WHITELIST_ENABLED")
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on", "enabled"):
        return True
    if value in ("0", "false", "no", "off", "disabled"):
        return False
    return None


def empty_whitelist(enabled=False, error=""):
    return {
        "enabled": enabled,
        "error": error,
        "device_ids": set(),
        "save_ids": set(),
        "denied_save_ids": set(),
        "usernames": set(),
        "auto_whitelist": False,
    }


def add_whitelist_value(target, value):
    normalized = normalize_whitelist_id(value)
    if normalized:
        target.add(normalized)


def whitelist_values(data, key):
    values = data.get(key, [])
    if values is None:
        return []
    if isinstance(values, (str, dict)):
        return [values]
    try:
        return list(values)
    except TypeError:
        return []


def _read_and_process_whitelist(handle):
    data = json.load(handle)
    if not isinstance(data, dict):
        return empty_whitelist(enabled=True, error="root JSON value must be an object")

    override_enabled = whitelist_env_enabled()
    enabled = bool(data.get("enabled", False))
    if override_enabled is True:
        enabled = True

    result = empty_whitelist(enabled=enabled)
    result["auto_whitelist"] = bool(data.get("auto_whitelist", False))

    for value in whitelist_values(data, "allow_device_ids"):
        add_whitelist_value(result["device_ids"], value)
    for value in whitelist_values(data, "allow_save_ids"):
        add_whitelist_value(result["save_ids"], value)
    for value in whitelist_values(data, "allow_usernames"):
        add_whitelist_value(result["usernames"], value)
    for value in whitelist_values(data, "deny_save_ids"):
        add_whitelist_value(result["denied_save_ids"], value)

    for entry in whitelist_values(data, "allow"):
        if isinstance(entry, str):
            normalized = normalize_whitelist_id(entry)
            if normalized.startswith("d-"):
                result["save_ids"].add(normalized)
            else:
                result["device_ids"].add(normalized)
            continue
        if not isinstance(entry, dict):
            continue
        add_whitelist_value(result["device_ids"], entry.get("device_id"))
        add_whitelist_value(result["save_ids"], entry.get("save_id"))
        add_whitelist_value(result["usernames"], entry.get("username") or entry.get("login_username") or entry.get("user"))
    return result


_WHITELIST_FILE_CACHE = None

def load_whitelist():
    global _WHITELIST_FILE_CACHE
    override_enabled = whitelist_env_enabled()
    if override_enabled is False:
        return empty_whitelist(enabled=False)

    if not os.path.exists(WHITELIST_FILE):
        return empty_whitelist(enabled=override_enabled is True)

    if _WHITELIST_FILE_CACHE is None:
        _WHITELIST_FILE_CACHE = FileCache(
            WHITELIST_FILE,
            _read_and_process_whitelist,
            empty_whitelist(enabled=True, error="Initial load failed")
        )
    return _WHITELIST_FILE_CACHE.get()


def whitelist_allows_login(username, device_id, save_username):
    whitelist = load_whitelist()
    if not whitelist["enabled"]:
        return True, "disabled", whitelist
    if whitelist["error"]:
        return False, f"config_error={whitelist['error']}", whitelist

    normalized_device_id = normalize_whitelist_id(device_id)
    normalized_save_id = normalize_whitelist_id(save_username)
    normalized_username = normalize_whitelist_id(username)
    if normalized_save_id and normalized_save_id in whitelist["denied_save_ids"]:
        return False, "denied_save_id", whitelist
    if normalized_device_id and normalized_device_id in whitelist["device_ids"]:
        return True, "device_id", whitelist
    if normalized_save_id and normalized_save_id in whitelist["save_ids"]:
        return True, "save_id", whitelist
    if normalized_username and normalized_username in whitelist["usernames"]:
        return True, "username", whitelist
    return False, "not_listed", whitelist


def auto_whitelist_device(device_id, save_id, username, log_func=None):
    global _WHITELIST_FILE_CACHE
    if not device_id:
        return
    try:
        data = {}
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except Exception:
                    data = {}
        if not isinstance(data, dict):
            data = {}
        
        if "allow" not in data or not isinstance(data["allow"], list):
            data["allow"] = []
        
        # Check if already present in JSON list
        for entry in data["allow"]:
            if isinstance(entry, dict) and entry.get("device_id") == device_id:
                return
                
        # Append new whitelist entry
        new_entry = {
            "device_id": device_id,
            "save_id": save_id or f"D-{device_id}",
            "username": username or "",
            "auto_added": True,
            "date_added": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        data["allow"].append(new_entry)
        
        # Ensure enabled remains
        if "enabled" not in data:
            data["enabled"] = True
            
        # Write atomically
        tmp_path = WHITELIST_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, WHITELIST_FILE)
        
        # Invalidate memory cache so next request sees the update
        _WHITELIST_FILE_CACHE = None
        if log_func:
            log_func(f"[WHITELIST] Automatically whitelisted new device_id={device_id!r} save_id={save_id!r}")
    except Exception as exc:
        if log_func:
            log_func(f"[WHITELIST] Error auto-whitelisting: {exc!r}")


def remember_pairing_identity(client_ip, device_id, save_id):
    client_ip = str(client_ip or "").strip()
    save_id = str(save_id or "").strip()
    if not client_ip or not save_id:
        return
    with PAIRING_IDS_LOCK:
        PAIRING_IDS_BY_IP[client_ip] = {
            "device_id": str(device_id or "").strip(),
            "save_id": save_id,
            "seen_at": time.time(),
        }


def recent_pairing_identity(client_ip):
    client_ip = str(client_ip or "").strip()
    now = time.time()
    with PAIRING_IDS_LOCK:
        expired = [
            ip for ip, entry in PAIRING_IDS_BY_IP.items()
            if now - float(entry.get("seen_at", 0)) > PAIRING_ID_TTL_SECONDS
        ]
        for ip in expired:
            PAIRING_IDS_BY_IP.pop(ip, None)
        entry = PAIRING_IDS_BY_IP.get(client_ip)
        return dict(entry) if isinstance(entry, dict) else None


def pairing_page(client_ip):
    entry = recent_pairing_identity(client_ip)
    save_id = str(entry.get("save_id", "")) if entry else ""
    if save_id:
        safe_save_id = html.escape(save_id)
        javascript_save_id = json.dumps(save_id)
        status = "Your save ID is ready."
        detail = f'<code id="save-id">{safe_save_id}</code>'
        action = '<button id="copy" type="button" onclick="copySaveId()">Copy save ID</button>'
        script = f"""
<script>
const saveId = {javascript_save_id};
async function copySaveId() {{
  let copied = false;
  try {{
    await navigator.clipboard.writeText(saveId);
    copied = true;
  }} catch (error) {{
    const field = document.createElement('textarea');
    field.value = saveId;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    copied = document.execCommand('copy');
    field.remove();
  }}
  const button = document.getElementById('copy');
  button.textContent = copied ? 'Copied' : 'Press and hold the ID';
}}
</script>"""
    else:
        status = "No recent game login was found."
        detail = "<p>Open Jurassic Park Builder once, then return here and refresh this page.</p>"
        action = '<button type="button" onclick="location.reload()">Refresh</button>'
        script = ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>JPB Save ID</title>
<style>
body{{margin:0;background:#101418;color:#f5f7f8;font:17px system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}}
main{{box-sizing:border-box;width:min(92vw,520px);padding:28px;background:#1d242a;border:1px solid #39434b;border-radius:8px}}
h1{{font-size:25px;margin:0 0 12px}}p{{line-height:1.5;color:#cbd3d8}}
code{{display:block;overflow-wrap:anywhere;padding:16px;margin:20px 0;background:#090c0e;border:1px solid #505b63;font-size:18px;user-select:all}}
button{{width:100%;min-height:50px;border:0;border-radius:6px;background:#f5b82e;color:#17120a;font:700 17px system-ui,sans-serif}}
</style>
</head>
<body><main><h1>JPB Save ID</h1><p>{html.escape(status)}</p>{detail}{action}</main>{script}</body>
</html>"""


def clear_profile_error_marker(content, log, reason):
    if not isinstance(content, dict):
        return content
    changed = []
    if "Error Loading Savefile" in str(content.get("93", "")):
        content["93"] = ""
        changed.append("93")
    if changed:
        log(f"[SAVE] cleared profile fallback marker fields={changed} reason={reason}")
    return content


def clear_profile_tournament_volatile_markers(content, log, reason):
    # Tournament season/cup fields are persisted player progress.
    return content


def normalize_profile_tournament_league_state(content, log, reason):
    if not isinstance(content, dict):
        return content
    changed = []
    for key, baseline in TOURNAMENT_PROFILE_LEAGUE_BASELINES.items():
        value = content.get(key)
        if not isinstance(baseline, list):
            if key in content and content.get(key) != baseline:
                content[key] = baseline
                changed.append((key, value, baseline))
            continue
        if not isinstance(value, list):
            continue
        normalized = list(value[:3])
        while len(normalized) < 3:
            normalized.append(baseline[len(normalized)])
        for index, base_value in enumerate(baseline):
            if _as_int(normalized[index], base_value) > base_value:
                normalized[index] = base_value
        if normalized != value:
            content[key] = normalized
            changed.append((key, value, normalized))
    if changed:
        log(f"[TOURNAMENT] normalized profile league fields={changed!r} reason={reason}")
    return content


DEFAULT_DINO_IDS = [
    7553477379137895,
    7562140328168910,
]


def ensure_park_dino_ids(content, log, reason):
    return content


def ensure_park_shape_fields(content, log, reason, key="park"):
    if not isinstance(content, dict):
        return content
    defaults = {
        "45": 0,
        "46": [],
        "47": [],
        "48": 0,
        "49": [],
        "50": [],
        "51": [],
        "55": [0, 0],
        "56": [0, 0],
        "57": 0,
        "58": [],
        "59": [],
        "60": 0,
    }
    changed = []
    for field, default in defaults.items():
        if field in content:
            continue
        content[field] = json.loads(json.dumps(default))
        changed.append(field)
    if changed:
        log(f"[SAVE] initialized {key} missing park fields={changed} reason={reason}")
    return content


def ensure_road_shape_fields(content, log, reason, key="road"):
    if not isinstance(content, dict):
        return content
    defaults = {
        "52": 0,
        "53": [],
        "54": [],
    }
    changed = []
    for field, default in defaults.items():
        if field in content:
            continue
        content[field] = json.loads(json.dumps(default))
        changed.append(field)
    if changed:
        log(f"[SAVE] initialized {key} missing road fields={changed} reason={reason}")
    return content


def trim_overlong_dino_save_array(content, log, reason):
    if not isinstance(content, dict):
        return 0
    count = _as_int(content.get("57"), None)
    values = content.get("58")
    paired_values = content.get("59")
    if (
        count is None
        or count < 0
        or not isinstance(values, list)
        or len(values) <= count
    ):
        return 0
    if isinstance(paired_values, list) and len(paired_values) > count:
        return 0
    old_len = len(values)
    del values[count:]
    log(f"[SAVE] trimmed overlong park.58 dino array {old_len}->{len(values)} reason={reason}")
    return 1



def visit_snapshot_session_state(session_state):
    snapshot_state = dict(session_state or {})
    snapshot_state["server_offline_elapsed_seconds"] = 0
    return snapshot_state


def offline_elapsed_should_apply(key, session_state):
    if not isinstance(session_state, dict):
        return True
    applied = session_state.setdefault("server_offline_elapsed_applied_keys", set())
    if key in applied:
        return False
    applied.add(key)
    return True



def normalize_saved_content_for_replay(key, content, session_state, log, for_replay=True, replay_elapsed_seconds=None):
    if not isinstance(content, dict):
        return content
    normalized = json.loads(json.dumps(content))
    account_username = (
        session_state.get("account_username")
        if isinstance(session_state, dict)
        else ""
    )
    if key == "prof" and account_username:
        old_account = normalized.get("10")
        if old_account != account_username:
            normalized["10"] = account_username
            log(f"[SAVE] normalized prof account id {old_account!r} -> {account_username!r}")
    if key == "prof":
        clear_profile_error_marker(
            normalized,
            log,
            "replay" if for_replay else "persist",
        )
        # Tournament state is real player progress. Earlier builds cleared the
        # season/cup fields and clamped league counters during replay, which
        # made cups and season timers reset after restart or tournament rejoin.
        cap_profile_tournament_cooldowns(
            normalized,
            log,
            "replay" if for_replay else "persist",
        )
    if key == "batl":
        normalize_battle_cooldown_slots(
            normalized,
            log,
            "replay" if for_replay else "persist",
        )
    if key in ("park", "aqpk", "arpk"):
        ensure_park_shape_fields(
            normalized,
            log,
            "replay" if for_replay else "persist",
            key,
        )
        ensure_park_dino_ids(
            normalized,
            log,
            "replay" if for_replay else "persist",
        )
        trim_overlong_dino_save_array(
            normalized,
            log,
            "replay" if for_replay else "persist",
        )
    if key in ("road", "aqrd", "arrd"):
        ensure_road_shape_fields(
            normalized,
            log,
            "replay" if for_replay else "persist",
            key,
        )
    if for_replay and key == "prof":
        if replay_elapsed_seconds is not None:
            replay_state = dict(session_state or {})
            replay_state["server_offline_elapsed_seconds"] = replay_elapsed_seconds
            apply_profile_offline_timers_for_replay(normalized, replay_state, log)
        elif offline_elapsed_should_apply("prof", session_state):
            apply_profile_offline_timers_for_replay(normalized, session_state, log)
    if for_replay and key in ("park", "aqpk", "arpk"):
        if replay_elapsed_seconds is not None:
            replay_state = dict(session_state or {})
            replay_state["server_offline_elapsed_seconds"] = replay_elapsed_seconds
            apply_active_offline_timers_for_replay(normalized, replay_state, log)
        elif offline_elapsed_should_apply(key, session_state):
            apply_active_offline_timers_for_replay(normalized, session_state, log)
    if for_replay and key == "batl":
        if replay_elapsed_seconds is not None:
            replay_state = dict(session_state or {})
            replay_state["server_offline_elapsed_seconds"] = replay_elapsed_seconds
            apply_battle_offline_timers_for_replay(normalized, replay_state, log)
        elif offline_elapsed_should_apply("batl", session_state):
            apply_battle_offline_timers_for_replay(normalized, session_state, log)
    return normalized


def _pack_signed64_from_u64(value):
    value &= 0xffffffffffffffff
    if value >= 0x8000000000000000:
        value -= 0x10000000000000000
    return value


def _add_to_high_u32(value, delta):
    packed = int(value) & 0xffffffffffffffff
    low = packed & 0xffffffff
    high = (packed >> 32) & 0xffffffff
    high = (high + int(delta)) & 0xffffffff
    return _pack_signed64_from_u64((high << 32) | low)


def _low_u32(value):
    return int(value) & 0xffffffff


def _high_u32(value):
    return (int(value) & 0xffffffffffffffff) >> 32


def _replace_high_u32(value, high):
    packed = int(value) & 0xffffffffffffffff
    low = packed & 0xffffffff
    return _pack_signed64_from_u64(((int(high) & 0xffffffff) << 32) | low)


def _advance_remaining_elapsed_u32_pair(value, delta, reset_elapsed_on_signed_overflow=False):
    """Mirror client resume(): low32 is remaining ms, high32 is elapsed ms."""
    remaining = _low_u32(value)
    elapsed = _high_u32(value)
    if remaining == 0:
        return value
    step = min(max(0, int(delta)), remaining)
    remaining = (remaining - step) & 0xffffffff
    elapsed = (elapsed + step) & 0xffffffff
    if reset_elapsed_on_signed_overflow and elapsed > 0x7fffffff:
        elapsed = 0
    return _pack_signed64_from_u64((elapsed << 32) | remaining)


def _advance_port_timer(value, delta, reset_elapsed_on_signed_overflow=False):
    """Mirror client updateItemTimer(): high32 is remaining ms, low32 is elapsed ms."""
    remaining = _high_u32(value)
    elapsed = _low_u32(value)
    if remaining == 0:
        return value
    step = min(max(0, int(delta)), remaining)
    remaining = (remaining - step) & 0xffffffff
    elapsed = (elapsed + step) & 0xffffffff
    if reset_elapsed_on_signed_overflow and elapsed > 0x7fffffff:
        elapsed = 0
    return _pack_signed64_from_u64((remaining << 32) | elapsed)



def _add_to_low32_high_u16(value, delta):
    packed = int(value) & 0xffffffffffffffff
    low16 = packed & 0xffff
    high16 = (packed >> 16) & 0xffff
    rest = packed & 0xffffffff00000000
    high16 = min(0xffff, high16 + max(0, int(delta)))
    return _pack_signed64_from_u64(rest | (high16 << 16) | low16)


def _low32_high_u16(value):
    return (int(value) & 0xffffffff) >> 16


def _add_to_p59_hatch_elapsed(value, delta):
    packed = int(value) & 0xffffffffffffffff
    low32 = packed & 0xffffffff
    elapsed = (packed >> 32) & 0xffffffff
    elapsed = (elapsed + max(0, int(delta))) & 0xffffffff
    return _pack_signed64_from_u64((elapsed << 32) | low32)


def _battle_cooldown_parts(value):
    packed = int(value) & 0xffffffffffffffff
    high = (packed >> 32) & 0xffffffff
    low = packed & 0xffffffff
    return high, low


def _battle_cooldown_remaining_ms(value):
    high, low = _battle_cooldown_parts(value)
    values = []
    for part in (high, low):
        # Battle cooldowns are millisecond timers. Ignore tiny flag-like values.
        if part >= 60 * 1000:
            values.append(part)
    return values


def _advance_battle_cooldown_value(value, elapsed_ms):
    packed = int(value) & 0xffffffffffffffff
    high, low = _battle_cooldown_parts(value)
    changed = False
    if high >= 60 * 1000:
        high = max(0, high - elapsed_ms)
        changed = True
    if low >= 60 * 1000:
        low = max(0, low - elapsed_ms)
        changed = True
    if not changed:
        return value
    return _pack_signed64_from_u64((high << 32) | low)


def battle_cooldown_remaining_values(content, elapsed_ms=0):
    if not isinstance(content, dict):
        return []
    values = content.get("104")
    if not isinstance(values, list):
        return []
    remaining = []
    elapsed_ms = max(0, int(elapsed_ms or 0))
    for value in values:
        for part in _battle_cooldown_remaining_ms(value):
            part = max(0, part - elapsed_ms)
            if part >= 60 * 1000:
                remaining.append(part)
    remaining.sort(reverse=True)
    return remaining


def _profile_battle_cooldown_remaining_values(content):
    if not isinstance(content, dict):
        return []
    elapsed_values = content.get("111")
    duration_values = content.get("112")
    if not isinstance(elapsed_values, list) or not isinstance(duration_values, list):
        return []
    remaining = []
    for index in range(min(len(elapsed_values), len(duration_values))):
        duration = _as_int(duration_values[index], 0)
        current = _as_int(elapsed_values[index], 0)
        value = current if current > 0 else duration
        if value >= 60 * 1000:
            remaining.append(value)
    remaining.sort(reverse=True)
    return remaining[:3]


def _cooldown_value_matches(existing, candidate):
    candidate = _as_int(candidate, 0)
    for value in existing:
        value = _as_int(value, 0)
        if value <= 0 or candidate <= 0:
            continue
        tolerance = max(15 * 60 * 1000, int(max(value, candidate) * 0.15))
        if abs(value - candidate) <= tolerance:
            return True
    return False


def synthesize_battle_cooldowns_from_profile(saved, log, label):
    # prof.111/112 are tournament progress fields, not a source for the
    # opaque packed creature state in batl.104.
    return 0


def _merge_battle_cooldown_value(previous_value, incoming_value):
    previous_high, previous_low = _battle_cooldown_parts(previous_value)
    incoming_high, incoming_low = _battle_cooldown_parts(incoming_value)
    if incoming_high >= 60 * 1000 or incoming_low >= 60 * 1000:
        return incoming_value, False
    changed = False

    if previous_high >= 60 * 1000 and incoming_high < 60 * 1000:
        incoming_high = previous_high
        changed = True
    if previous_low >= 60 * 1000 and incoming_low < 60 * 1000:
        incoming_low = previous_low
        changed = True

    if not changed:
        return incoming_value, False
    return _pack_signed64_from_u64((incoming_high << 32) | incoming_low), True


def normalize_battle_cooldown_slots(content, log, label):
    # The two packed halves have no proven remaining/elapsed orientation.
    # Preserve the native value byte-for-byte until that schema is recovered.
    return 0


def cap_profile_tournament_cooldowns(content, log, label):
    # prof.111/112 are tournament progress, not millisecond cooldown values.
    return 0


def _merge_battle_cooldown_floor(previous_batl, merged_batl, log, label):
    # Never merge individual packed halves: doing so can manufacture a state
    # that neither the previous nor the incoming client ever sent.
    return 0


def apply_battle_offline_timers_for_replay(content, session_state, log):
    # The client owns advancement of this opaque packed state. Server-side
    # subtraction previously changed both halves and corrupted some creatures.
    return 0


def apply_profile_offline_timers_for_replay(content, session_state, log):
    elapsed = _as_int(session_state.get("server_offline_elapsed_seconds"), 0)
    if elapsed <= 0:
        return
    elapsed_ms = min(elapsed, 7 * 24 * 60 * 60) * 1000

    touched = session_state.setdefault("server_offline_elapsed_logged_keys", set())
    should_log = "prof" not in touched

    if "144" in content:
        old_val = _as_int(content.get("144"), 0)
        limit = _as_int(content.get("145"), 8 * 60 * 60 * 1000)
        new_val = min(max(old_val, 0) + elapsed_ms, max(limit, 0))
        content["144"] = new_val
        if should_log:
            log(f"[TIME]   prof.144 card-pack advanced: {old_val} -> {new_val} (+{elapsed_ms}ms limit={limit})")

    # prof.1 and prof.146 are absolute clock anchors. Moving them forward by
    # the offline duration cancels real elapsed time and freezes tournament
    # countdowns across reconnects. Preserve them and let wall time advance.
    if should_log:
        anchors = {
            key: content.get(key)
            for key in ("1", "146")
            if key in content
        }
        if anchors:
            log(f"[TIME]   preserved absolute profile anchors offline elapsed={elapsed}s {anchors!r}")

    if should_log:
        touched.add("prof")


def apply_active_offline_timers_for_replay(content, session_state, log):
    elapsed = _as_int(session_state.get("server_offline_elapsed_seconds"), 0)
    if elapsed <= 0:
        return
    elapsed = min(elapsed, 7 * 24 * 60 * 60)
    elapsed_ms = elapsed * 1000

    touched = session_state.setdefault("server_offline_elapsed_logged_keys", set())
    should_log = "park" not in touched

    changed_objects = 0
    object_count = _as_int(content.get("45"), 0)
    object_timers = content.get("47")
    if isinstance(object_timers, list):
        for index in range(min(max(object_count, 0), len(object_timers))):
            try:
                old_val = object_timers[index]
                new_val = _add_to_high_u32(old_val, elapsed_ms)
                object_timers[index] = new_val
                changed_objects += 1
                if should_log:
                    log(f"[TIME]   object.47[{index}] advanced: {old_val} -> {new_val} (+{elapsed_ms}ms)")
            except Exception as exc:
                if should_log:
                    log(f"[TIME]   object.47[{index}] error: {exc!r}")
                continue

    changed_expansions = 0
    expansion_timers = content.get("50")
    expansion_states = content.get("51")
    if isinstance(expansion_timers, list) and isinstance(expansion_states, list):
        for index in range(min(len(expansion_timers), len(expansion_states))):
            if _as_int(expansion_states[index], 0) == 0:
                continue
            try:
                old_val = expansion_timers[index]
                new_val = _add_to_high_u32(old_val, elapsed_ms)
                expansion_timers[index] = new_val
                changed_expansions += 1
                if should_log:
                    log(f"[TIME]   expansion.50[{index}] advanced: {old_val} -> {new_val} (+{elapsed_ms}ms)")
            except Exception as exc:
                if should_log:
                    log(f"[TIME]   expansion.50[{index}] error: {exc!r}")
                continue

    changed_dinos = 0
    dino_count = _as_int(content.get("57"), 0)
    dino_timers = content.get("58")
    if isinstance(dino_timers, list):
        for index in range(min(max(dino_count, 0), len(dino_timers))):
            try:
                old_val = dino_timers[index]
                old_remaining = _low_u32(old_val)
                old_elapsed = _high_u32(old_val)
                new_val = _advance_remaining_elapsed_u32_pair(old_val, elapsed_ms)
                dino_timers[index] = new_val
                changed_dinos += 1
                if should_log:
                    log(
                        f"[TIME]   dino.58[{index}] resumed: {old_val} -> {new_val} "
                        f"remaining {old_remaining}->{_low_u32(new_val)} "
                        f"elapsed {old_elapsed}->{_high_u32(new_val)} (+{elapsed_ms}ms max-aware)"
                    )
            except Exception as exc:
                if should_log:
                    log(f"[TIME]   dino.58[{index}] error: {exc!r}")
                continue

    changed_hatching = 0
    hatch_values = content.get("59")
    if isinstance(hatch_values, list) and isinstance(dino_timers, list):
        for index in range(min(len(hatch_values), len(dino_timers))):
            if _as_int(dino_timers[index], 0) != 0 or _as_int(hatch_values[index], 0) == 0:
                continue
            try:
                old_val = hatch_values[index]
                new_val = _add_to_p59_hatch_elapsed(old_val, elapsed)
                hatch_values[index] = new_val
                changed_hatching += 1
                if should_log:
                    log(f"[TIME]   hatch dino.59[{index}] advanced: {old_val} -> {new_val} (+{elapsed}s)")
            except Exception as exc:
                if should_log:
                    log(f"[TIME]   hatch dino.59[{index}] error: {exc!r}")
                continue

    changed_ports = 0
    port_timers = content.get("55")
    if isinstance(port_timers, list):
        for index in range(min(2, len(port_timers))):
            try:
                old_val = port_timers[index]
                old_remaining = _high_u32(old_val)
                old_elapsed = _low_u32(old_val)
                new_val = _advance_port_timer(
                    old_val,
                    elapsed_ms,
                    reset_elapsed_on_signed_overflow=True,
                )
                port_timers[index] = new_val
                changed_ports += 1
                if should_log:
                    log(
                        f"[TIME]   port.55[{index}] resumed: {old_val} -> {new_val} "
                        f"remaining {old_remaining}->{_high_u32(new_val)} "
                        f"elapsed {old_elapsed}->{_low_u32(new_val)} (+{elapsed_ms}ms max-aware)"
                    )
            except Exception as exc:
                if should_log:
                    log(f"[TIME]   port.55[{index}] error: {exc!r}")
                continue

    changed_buildings = 0
    building_count = _as_int(content.get("60"), 0)
    building_timers = content.get("61")
    if isinstance(building_timers, list):
        for index in range(min(max(building_count, 0), len(building_timers))):
            try:
                old_val = building_timers[index]
                new_val = _add_to_high_u32(old_val, elapsed)
                building_timers[index] = new_val
                changed_buildings += 1
                if should_log:
                    log(f"[TIME]   building.61[{index}] advanced: {old_val} -> {new_val} (+{elapsed}s)")
            except Exception as exc:
                if should_log:
                    log(f"[TIME]   building.61[{index}] error: {exc!r}")
                continue
    elif building_count > 0 and building_timers is not None:
        try:
            old_val = building_timers
            new_val = _add_to_high_u32(old_val, elapsed)
            content["61"] = new_val
            changed_buildings = 1
            if should_log:
                log(f"[TIME]   building.61 scalar advanced: {old_val} -> {new_val} (+{elapsed}s)")
        except Exception as exc:
            if should_log:
                log(f"[TIME]   building.61 scalar error: {exc!r}")

    if should_log:
        touched.add("park")
        log(
            f"[TIME] replay applied active offline elapsed={elapsed}s "
            f"objects={changed_objects} expansions={changed_expansions} "
            f"dinos={changed_dinos} hatching={changed_hatching} "
            f"buildings={changed_buildings} ports={changed_ports}"
        )


def load_guest_save(username, log):
    path = guest_save_path(username)
    if not os.path.isfile(path):
        return {}
    lock = get_player_lock(username)
    with lock, save_file_lock(
        path,
        timeout_seconds=10.0,
        lock_dir=RUN_DIR,
    ) as file_lock_acquired:
        if not file_lock_acquired:
            log(f"[SAVE] timed out waiting for maintenance lock on {path!r}")
            raise SaveFileUnavailableError(
                f"save is temporarily unavailable during maintenance: {path}"
            )
        cached_data = GUEST_SAVE_CACHE.get(username, path, log)
        if cached_data is not None:
            hardcash_migrated = migrate_legacy_hardcash_sidecar(
                cached_data,
                log,
            )
            if (
                recover_hardcash_sidecar_from_backups(username, cached_data, log)
                or hardcash_migrated
            ):
                GUEST_SAVE_CACHE.set(username, path, cached_data)
            sync_authoritative_tournament_anchor(cached_data, log, "cached load")
            return cached_data

        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            migrate_legacy_hardcash_sidecar(data, log)
            recover_hardcash_sidecar_from_backups(username, data, log)
            sync_authoritative_tournament_anchor(data, log, "disk load")
            log(f"[SAVE] loaded {path!r} keys={sorted(data.keys())!r}")
            GUEST_SAVE_CACHE.set(username, path, data)
            return copy.deepcopy(data)
        except FileNotFoundError:
            raise SaveFileUnavailableError(
                f"save disappeared while being loaded: {path}"
            )
        except Exception as exc:
            log(f"[SAVE] could not load {path!r}: {type(exc).__name__}: {exc}")
            raise SaveFileUnavailableError(
                f"existing save could not be loaded safely: {path}"
            ) from exc


def _parse_online_options_file(handle):
    data = handle.read()
    return _parse_online_options_bytes(data)


def _parse_online_options_bytes(data):
    key_marker = b"\x01\x00\x00\x00k\x04\x00\x00\x00"
    value_marker = b"\x01\x00\x00\x00v\x04\x00\x00\x00"
    values = {}
    offset = 0
    while True:
        offset = data.find(key_marker, offset)
        if offset < 0:
            break
        key_length_at = offset + len(key_marker)
        if key_length_at + 4 > len(data):
            break
        key_length = struct.unpack_from("<I", data, key_length_at)[0]
        key_at = key_length_at + 4
        value_at = key_at + key_length
        if (
            value_at + len(value_marker) + 4 <= len(data)
            and data[value_at:value_at + len(value_marker)] == value_marker
        ):
            value_length_at = value_at + len(value_marker)
            value_length = struct.unpack_from("<I", data, value_length_at)[0]
            text_at = value_length_at + 4
            text_end = text_at + value_length
            if text_end <= len(data):
                key = data[key_at:value_at].decode("utf-8", errors="replace")
                value = data[text_at:text_end].decode("utf-8", errors="replace")
                values[key] = value
        offset += 1
    return values


def patch_online_options_bytes(data, overrides):
    """Patch the cached onlineoptions blob without changing unrelated entries."""
    if not isinstance(data, (bytes, bytearray)) or not isinstance(overrides, dict):
        return data, []
    key_marker = b"\x01\x00\x00\x00k\x04\x00\x00\x00"
    value_marker = b"\x01\x00\x00\x00v\x04\x00\x00\x00"
    patched = bytes(data)
    changed = []
    offset = 0
    while True:
        offset = patched.find(key_marker, offset)
        if offset < 0:
            break
        key_length_at = offset + len(key_marker)
        if key_length_at + 4 > len(patched):
            break
        key_length = struct.unpack_from("<I", patched, key_length_at)[0]
        key_at = key_length_at + 4
        value_at = key_at + key_length
        if value_at + len(value_marker) + 4 > len(patched):
            offset += 1
            continue
        key = patched[key_at:value_at].decode("utf-8", errors="replace")
        if patched[value_at:value_at + len(value_marker)] != value_marker:
            offset += 1
            continue
        value_length_at = value_at + len(value_marker)
        value_length = struct.unpack_from("<I", patched, value_length_at)[0]
        text_at = value_length_at + 4
        text_end = text_at + value_length
        if text_end > len(patched):
            offset += 1
            continue
        if key in overrides:
            new_value = str(overrides[key]).encode("utf-8")
            old_value = patched[text_at:text_end].decode("utf-8", errors="replace")
            if old_value != str(overrides[key]):
                patched = (
                    patched[:value_length_at]
                    + struct.pack("<I", len(new_value))
                    + new_value
                    + patched[text_end:]
                )
                changed.append(key)
                offset = text_at + len(new_value)
                continue
        offset = text_end
    return patched, changed


_ONLINE_OPTIONS_FILE_CACHE = None
_GUEST_SAVE_SNAPSHOT_HASHES = {}
_HARDCASH_RECOVERY_NEGATIVE_CACHE = {}

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
HARDCASH_SCHEMA_VERSION = 2
HARDCASH_MAX_BALANCE = 2_147_483_647
HARDCASH_RECOVERY_MAX_CANDIDATES = 32
HARDCASH_RECOVERY_MARKER_FILE = "wallet_latest.json"
HARDCASH_TRUSTED_SOURCES = frozenset({
    "new_account_default",
    "legacy_server_ledger",
    "manual_observation",
})


def _transaction_signature(transaction):
    if not isinstance(transaction, dict):
        return None
    return json.dumps(transaction, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _transaction_is_new(transaction, previous_ids, previous_signatures):
    if not isinstance(transaction, dict):
        return False
    transaction_id = str(transaction.get("id") or "").strip()
    if transaction_id:
        return transaction_id not in previous_ids
    signature = _transaction_signature(transaction)
    return signature is not None and signature not in previous_signatures


def _hardcash_transaction_delta(transaction):
    if not isinstance(transaction, dict):
        return 0
    if transaction.get("applied") is False:
        return 0
    tx_type = str(transaction.get("t", ""))
    if tx_type == "MAILGIFT":
        return 0
    item_type = _as_int(transaction.get("it"), 0) or _as_int(transaction.get("dt"), 0)
    if item_type != 1:
        return 0
    amount = _as_int(transaction.get("ia"), 0)
    return amount if amount else -_as_int(transaction.get("da"), 0)


def _has_new_hardcash_spend(previous_meta, incoming_meta):
    previous_transactions = previous_meta.get("reward_transactions", [])
    incoming_transactions = incoming_meta.get("reward_transactions", [])
    if not isinstance(previous_transactions, list) or not isinstance(incoming_transactions, list):
        return False
    previous_signatures = {
        signature
        for signature in map(_transaction_signature, previous_transactions)
        if signature is not None
    }
    previous_ids = {
        str(transaction_id).strip()
        for transaction_id in previous_meta.get("reward_transaction_ids", [])
        if str(transaction_id).strip()
    } if isinstance(previous_meta.get("reward_transaction_ids"), list) else set()
    previous_ids.update(
        str(transaction.get("id") or "").strip()
        for transaction in previous_transactions
        if isinstance(transaction, dict)
        and str(transaction.get("id") or "").strip()
    )
    for transaction in incoming_transactions:
        if isinstance(transaction, dict) and transaction.get("applied") is False:
            continue
        if not _transaction_is_new(
            transaction,
            previous_ids,
            previous_signatures,
        ):
            continue
        tx_type = str(transaction.get("t", ""))
        item_type = _as_int(transaction.get("it"), 0) or _as_int(transaction.get("dt"), 0)
        amount = _as_int(transaction.get("ia"), 0)
        if not amount:
            amount = -_as_int(transaction.get("da"), 0)
        if tx_type != "MAILGIFT" and item_type == 1 and amount < 0:
            return True
    return False


def preserve_persisted_hardcash_sidecar(path, data, log):
    """Reject stale battle/tournament writes that silently erase hardcash."""
    if not os.path.exists(path) or not isinstance(data, dict):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            previous = json.load(handle)
    except Exception as exc:
        log(f"[HARDCASH] sidecar guard skipped unreadable save: {type(exc).__name__}: {exc}")
        return

    previous_meta = previous.get("__meta", {}) if isinstance(previous, dict) else {}
    incoming_meta = data.setdefault("__meta", {})
    if not isinstance(previous_meta, dict) or not isinstance(incoming_meta, dict):
        return
    if not hardcash_balance_is_known(previous):
        return
    if not hardcash_balance_is_known(data):
        for key in HARDCASH_SIDECAR_KEYS:
            if key in previous_meta:
                incoming_meta[key] = copy.deepcopy(previous_meta[key])
        log("[HARDCASH] restored persisted wallet sidecar missing from incoming save")
        return
    previous_balance = saved_hardcash_estimate(previous)
    incoming_balance = saved_hardcash_estimate(data)
    if incoming_balance >= previous_balance:
        return
    previous_transactions = previous_meta.get("reward_transactions", [])
    incoming_transactions = incoming_meta.get("reward_transactions", [])
    previous_signatures = {
        signature
        for signature in map(_transaction_signature, previous_transactions)
        if signature is not None
    } if isinstance(previous_transactions, list) else set()
    previous_ids = {
        str(transaction_id).strip()
        for transaction_id in previous_meta.get("reward_transaction_ids", [])
        if str(transaction_id).strip()
    } if isinstance(previous_meta.get("reward_transaction_ids"), list) else set()
    if isinstance(previous_transactions, list):
        previous_ids.update(
            str(transaction.get("id") or "").strip()
            for transaction in previous_transactions
            if isinstance(transaction, dict)
            and str(transaction.get("id") or "").strip()
        )
    new_transactions = [
        transaction
        for transaction in incoming_transactions
        if _transaction_is_new(
            transaction,
            previous_ids,
            previous_signatures,
        )
    ] if isinstance(incoming_transactions, list) else []
    new_delta = sum(_hardcash_transaction_delta(transaction) for transaction in new_transactions)

    if new_delta < 0 and _has_new_hardcash_spend(previous_meta, incoming_meta):
        adjusted_balance = max(0, previous_balance + new_delta)
        merged_transactions = copy.deepcopy(previous_transactions) if isinstance(previous_transactions, list) else []
        merged_signatures = set(previous_signatures)
        for transaction in new_transactions:
            signature = _transaction_signature(transaction)
            if signature is None or signature in merged_signatures:
                continue
            merged_transactions.append(copy.deepcopy(transaction))
            merged_signatures.add(signature)
        if len(merged_transactions) > 50:
            merged_transactions = merged_transactions[-50:]

        previous_ids = previous_meta.get("reward_transaction_ids", [])
        incoming_ids = incoming_meta.get("reward_transaction_ids", [])
        merged_ids = []
        for transaction_id in [
            *(previous_ids if isinstance(previous_ids, list) else []),
            *(incoming_ids if isinstance(incoming_ids, list) else []),
        ]:
            if transaction_id not in merged_ids:
                merged_ids.append(transaction_id)
        incoming_meta["reward_transactions"] = merged_transactions
        incoming_meta["reward_transaction_ids"] = merged_ids[-100:]
        incoming_meta["hardcash_baseline"] = _as_int(previous_meta.get("hardcash_baseline"), 3)
        incoming_meta["hardcash_balance_estimate"] = adjusted_balance
        incoming_meta["hardcash_last_good_estimate"] = adjusted_balance
        incoming_meta["hardcash_delta_total"] = (
            adjusted_balance - _as_int(incoming_meta.get("hardcash_baseline"), 3)
        )
        for key in (
            "hardcash_mail_delivered_balance",
            "hardcash_mail_last_delivered_balance",
            "hardcash_mail_last_delivered_by_device",
        ):
            if key in previous_meta:
                incoming_meta[key] = copy.deepcopy(previous_meta[key])
        log(
            f"[HARDCASH] merged explicit spend into latest balance "
            f"persisted={previous_balance} incoming={incoming_balance} "
            f"delta={new_delta} result={adjusted_balance}"
        )
        return

    for key in HARDCASH_SIDECAR_KEYS:
        if key in previous_meta:
            incoming_meta[key] = copy.deepcopy(previous_meta[key])
    log(
        f"[HARDCASH] rejected stale balance drop "
        f"{previous_balance} -> {incoming_balance}; restored persisted sidecar"
    )


# Fields that establish that a tournament result was committed. Keep timer and
# creature-cooldown fields out of this set: the client rewrites those during
# startup/replay, which must not authorize replacing the persisted clock anchor.
TOURNAMENT_RESULT_PROGRESS_KEYS = (
    "74", "75", "76", "95", "107", "108", "109",
    "113", "114", "122", "123", "124",
)

# The legacy client changes these profile fields when it acknowledges an ended
# season and opens the replacement season. That write is allowed to lower cup
# totals; treating it as stale progress makes the reward popup repeat forever.
TOURNAMENT_SEASON_ACK_KEYS = ("89", "90", "143")

TOURNAMENT_ANCHOR_META_KEY = "tournament_anchor_unix"


def _tournament_result_progress_changed(previous_prof, incoming_prof):
    """Return whether the incoming profile commits tournament result progress."""
    if not isinstance(previous_prof, dict) or not isinstance(incoming_prof, dict):
        return False
    return any(
        incoming_prof.get(key) != previous_prof.get(key)
        for key in TOURNAMENT_RESULT_PROGRESS_KEYS
    )


def _tournament_season_ack_changed(previous_prof, incoming_prof):
    """Return whether the client acknowledged a season rollover."""
    if not isinstance(previous_prof, dict) or not isinstance(incoming_prof, dict):
        return False

    # Observed clean-season saves clear either acknowledgement boolean after
    # claiming the season reward.
    for key in ("89", "143"):
        if previous_prof.get(key) is True and incoming_prof.get(key) is False:
            return True

    # prof.90 is the client's last acknowledged season timestamp in seconds.
    previous_timestamp = _as_int(previous_prof.get("90"), 0)
    incoming_timestamp = _as_int(incoming_prof.get("90"), 0)
    return (
        946684800 <= previous_timestamp <= 4102444800
        and 946684800 <= incoming_timestamp <= 4102444800
        and incoming_timestamp > previous_timestamp
    )


def _valid_profile_clock_anchor(value):
    """Return whether a profile clock anchor is a plausible Unix timestamp."""
    timestamp = _as_int(value, 0)
    return 946684800 <= timestamp <= 4102444800


def authoritative_tournament_anchor(save_data):
    """Return the server-owned absolute tournament timestamp, if available."""
    if not isinstance(save_data, dict):
        return None
    meta = save_data.get("__meta", {})
    if isinstance(meta, dict):
        value = meta.get(TOURNAMENT_ANCHOR_META_KEY)
        if _valid_profile_clock_anchor(value):
            return _as_int(value, 0)
    profile = save_data.get("prof", {})
    profile = profile.get("c", {}) if isinstance(profile, dict) else {}
    value = profile.get("146") if isinstance(profile, dict) else None
    if _valid_profile_clock_anchor(value):
        return _as_int(value, 0)
    return None


def sync_authoritative_tournament_anchor(save_data, log, label, profile_content=None):
    """Seed and apply the server-owned absolute tournament timestamp."""
    if not isinstance(save_data, dict):
        return False
    meta = save_data.setdefault("__meta", {})
    if not isinstance(meta, dict):
        return False
    if profile_content is None:
        profile = save_data.get("prof", {})
        profile_content = profile.get("c", {}) if isinstance(profile, dict) else {}
    if not isinstance(profile_content, dict):
        return False

    anchor = authoritative_tournament_anchor(save_data)
    if anchor is None:
        return False

    changed = False
    if meta.get(TOURNAMENT_ANCHOR_META_KEY) != anchor:
        meta[TOURNAMENT_ANCHOR_META_KEY] = anchor
        changed = True
        log(f"[TOURNAMENT] {label} seeded authoritative anchor={anchor}")
    if profile_content.get("146") != anchor:
        old_value = profile_content.get("146")
        profile_content["146"] = anchor
        changed = True
        log(
            f"[TOURNAMENT] {label} restored prof.146 from "
            f"{old_value!r} to authoritative anchor={anchor}"
        )
    return changed


def preserve_persisted_tournament_anchor(path, data, log):
    """Reject client startup writes that restart the tournament cooldown."""
    if not os.path.exists(path) or not isinstance(data, dict):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            previous = json.load(handle)
    except Exception as exc:
        log(f"[TOURNAMENT] anchor guard skipped unreadable save: {type(exc).__name__}: {exc}")
        return

    sync_authoritative_tournament_anchor(previous, log, "persisted save")
    previous_prof = previous.get("prof", {}).get("c", {}) if isinstance(previous, dict) else {}
    incoming_prof = data.get("prof", {}).get("c", {})
    if not isinstance(previous_prof, dict) or not isinstance(incoming_prof, dict):
        return
    previous_anchor = authoritative_tournament_anchor(previous)
    if previous_anchor is None:
        sync_authoritative_tournament_anchor(data, log, "first persisted write")
        return
    incoming_anchor = incoming_prof.get("146")
    if incoming_anchor == previous_anchor:
        data.setdefault("__meta", {})[TOURNAMENT_ANCHOR_META_KEY] = previous_anchor
        return

    # A malformed incoming timestamp can produce the enormous positive or
    # negative day counters seen in the legacy statistics screen. Never let it
    # replace a valid persisted Unix anchor.
    if _valid_profile_clock_anchor(previous_anchor) and not _valid_profile_clock_anchor(incoming_anchor):
        incoming_prof["146"] = copy.deepcopy(previous_anchor)
        log(
            f"[TOURNAMENT] rejected malformed prof.146 anchor "
            f"{incoming_anchor!r} -> {previous_anchor!r}"
        )
        return

    result_changed = _tournament_result_progress_changed(previous_prof, incoming_prof)
    if result_changed and _valid_profile_clock_anchor(incoming_anchor):
        incoming_anchor = _as_int(incoming_anchor, 0)
        data.setdefault("__meta", {})[TOURNAMENT_ANCHOR_META_KEY] = incoming_anchor
        log(
            f"[TOURNAMENT] accepted prof.146 anchor change with result progress "
            f"{previous_anchor!r} -> {incoming_anchor!r}"
        )
        return

    incoming_prof["146"] = copy.deepcopy(previous_anchor)
    data.setdefault("__meta", {})[TOURNAMENT_ANCHOR_META_KEY] = previous_anchor
    log(
        f"[TOURNAMENT] rejected cooldown-only prof.146 reset "
        f"{previous_anchor!r} -> {incoming_anchor!r}"
    )


def snapshot_guest_save(username, path, data, log):
    """Snapshot a successfully written save when meaningful content changed."""
    comparable = copy.deepcopy(data)
    meta = comparable.get("__meta")
    if isinstance(meta, dict):
        meta.pop("last_server_save_time", None)
        meta.pop("last_server_save_time_ms", None)
    digest = hashlib.sha256(
        json.dumps(
            comparable,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if _GUEST_SAVE_SNAPSHOT_HASHES.get(username) == digest:
        return

    destination_dir = os.path.join(
        ROLLING_SAVE_BACKUP_DIR,
        os.path.splitext(os.path.basename(path))[0],
    )
    os.makedirs(destination_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    destination = os.path.join(destination_dir, f"{stamp}.json")
    temporary = f"{destination}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        shutil.copyfile(path, temporary)
        os.replace(temporary, destination)
        _GUEST_SAVE_SNAPSHOT_HASHES[username] = digest
        log(f"[SAVE-BACKUP] snapshot {username!r} -> {destination!r}")
    except Exception as exc:
        log(
            f"[SAVE-BACKUP] ERROR snapshotting {username!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        try:
            os.remove(temporary)
        except OSError:
            pass


def snapshot_trusted_hardcash_sidecar(username, data, log):
    if not hardcash_balance_is_known(data):
        return False
    meta = data.get("__meta", {})
    marker_meta = {
        key: copy.deepcopy(meta[key])
        for key in HARDCASH_SIDECAR_KEYS
        if key in meta
    }
    destination_dir = os.path.join(
        ROLLING_SAVE_BACKUP_DIR,
        os.path.splitext(os.path.basename(guest_save_path(username)))[0],
    )
    os.makedirs(destination_dir, exist_ok=True)
    destination = os.path.join(
        destination_dir,
        HARDCASH_RECOVERY_MARKER_FILE,
    )
    temporary = f"{destination}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                {"__meta": marker_meta},
                handle,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return True
    except Exception as exc:
        log(
            f"[HARDCASH] could not update trusted wallet marker: "
            f"{type(exc).__name__}: {exc}"
        )
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False


def save_file_revision(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    return digest.hexdigest()


def backup_guest_save_before_write(username, path, log):
    if not os.path.isfile(path):
        return
    destination_dir = os.path.join(
        ROLLING_SAVE_BACKUP_DIR,
        os.path.splitext(os.path.basename(path))[0],
    )
    os.makedirs(destination_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    destination = os.path.join(destination_dir, f"prewrite_{stamp}.json")
    temporary = f"{destination}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        shutil.copyfile(path, temporary)
        os.replace(temporary, destination)
        log(f"[SAVE-BACKUP] prewrite {username!r} -> {destination!r}")
    except Exception as exc:
        log(
            f"[SAVE-BACKUP] ERROR prewrite snapshot for {username!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        try:
            os.remove(temporary)
        except OSError:
            pass


def write_save_conflict(username, data, session_state, current_revision, log, reason):
    safe_save = safe_log_identity(username)
    device_id = safe_log_identity((session_state or {}).get("device_id") or "unknown_device")
    session_id = safe_log_identity((session_state or {}).get("session_id") or "unknown_session")
    destination_dir = os.path.join(SAVE_CONFLICT_DIR, safe_save)
    os.makedirs(destination_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    destination = os.path.join(destination_dir, f"{stamp}_{device_id}_{session_id}.json")
    temporary = f"{destination}.tmp-{os.getpid()}-{threading.get_ident()}"
    conflict_data = copy.deepcopy(data)
    conflict_data["__conflict"] = {
        "reason": reason,
        "save_id": username,
        "device_id": (session_state or {}).get("device_id", ""),
        "session_id": (session_state or {}).get("session_id", ""),
        "expected_revision": (session_state or {}).get("save_revision"),
        "current_revision": current_revision,
        "created_at": time.time(),
    }
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(conflict_data, handle, ensure_ascii=True, indent=2, sort_keys=True)
        os.replace(temporary, destination)
        log(
            f"[SAVE-CONFLICT] blocked overwrite for {username!r}; "
            f"reason={reason} copy={destination!r}"
        )
        return destination
    except Exception as exc:
        log(
            f"[SAVE-CONFLICT] ERROR saving conflict for {username!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        try:
            os.remove(temporary)
        except OSError:
            pass
        return ""


def load_cached_online_options(log):
    """Extract the original c.pb `k`/`v` entries from cached onlineoptions."""
    global _ONLINE_OPTIONS_FILE_CACHE
    if _ONLINE_OPTIONS_FILE_CACHE is None:
        _ONLINE_OPTIONS_FILE_CACHE = FileCache(ONLINE_OPTIONS_FILE, _parse_online_options_file, {})
    return _ONLINE_OPTIONS_FILE_CACHE.get(log)


def save_guest_data(username, data, log, session_state=None):
    os.makedirs(SAVE_DIR, exist_ok=True)
    path = guest_save_path(username)
    meta = data.setdefault("__meta", {})
    if isinstance(meta, dict):
        meta["last_server_save_time"] = int(time.time())
        meta["last_server_save_time_ms"] = int(time.time() * 1000)
    
    lock = get_player_lock(username)
    with lock, save_file_lock(
        path,
        timeout_seconds=10.0,
        lock_dir=RUN_DIR,
    ) as file_lock_acquired:
        if not file_lock_acquired:
            log(f"[SAVE] timed out waiting for maintenance lock on {path!r}")
            raise SaveFileUnavailableError(
                f"save is temporarily unavailable during maintenance: {path}"
            )
        current_revision = save_file_revision(path)
        if isinstance(session_state, dict):
            session_id = str(session_state.get("session_id") or "")
            if not SAVE_SESSION_REGISTRY.owns(username, session_id):
                write_save_conflict(
                    username,
                    data,
                    session_state,
                    current_revision,
                    log,
                    "session lock is no longer owned",
                )
                session_state["save_conflicted"] = True
                raise SaveFileUnavailableError(
                    f"save session no longer owns persisted state: {path}"
                )
            expected_revision = session_state.get("save_revision")
            if expected_revision is None:
                session_state["save_revision"] = current_revision
                expected_revision = current_revision
            if current_revision is None or current_revision != expected_revision:
                write_save_conflict(
                    username,
                    data,
                    session_state,
                    current_revision,
                    log,
                    "persisted save changed after session login",
                )
                session_state["save_conflicted"] = True
                raise SaveFileUnavailableError(
                    f"persisted save revision changed during session: {path}"
                )

        temp_path = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            preserve_persisted_hardcash_sidecar(path, data, log)
            preserve_persisted_tournament_anchor(path, data, log)
            backup_guest_save_before_write(username, path, log)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=True, indent=2, sort_keys=True)
            os.replace(temp_path, path)
            snapshot_trusted_hardcash_sidecar(username, data, log)
            new_revision = save_file_revision(path)
            if isinstance(session_state, dict):
                session_state["save_revision"] = new_revision
                session_state["save_conflicted"] = False
            GUEST_SAVE_CACHE.set(username, path, data)
            log(f"[SAVE] wrote {path!r} atomically keys={sorted(data.keys())!r}")
            snapshot_guest_save(username, path, data, log)
            return True
        except Exception as exc:
            log(f"[SAVE] ERROR writing {path!r} atomically: {type(exc).__name__}: {exc}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise SaveFileUnavailableError(
                f"failed to persist save atomically: {path}"
            ) from exc


def saved_offline_elapsed_seconds(data, now_seconds=None):
    if now_seconds is None:
        now_seconds = int(time.time())
    if not isinstance(data, dict):
        return 0
    meta = data.get("__meta", {})
    if isinstance(meta, dict):
        last_saved_at = _as_int(meta.get("last_server_save_time"), None)
        if isinstance(last_saved_at, int) and last_saved_at > 0:
            elapsed = now_seconds - last_saved_at
            return max(0, min(elapsed, 7 * 24 * 60 * 60))
    park = data.get("park", {})
    content = park.get("c", {}) if isinstance(park, dict) else {}
    last_saved_at = _as_int(content.get("1"), None) if isinstance(content, dict) else None
    if not isinstance(last_saved_at, int) or last_saved_at <= 0:
        return 0
    elapsed = now_seconds - last_saved_at
    return max(0, min(elapsed, 7 * 24 * 60 * 60))


def save_payload_size(data):
    try:
        return len(json.dumps(data or {}, ensure_ascii=True, sort_keys=True))
    except Exception:
        return -1


def log_tournament_save_diff(previous, current, log):
    """Log compact prof/batl changes so unknown tournament timer fields surface."""
    if os.environ.get("JPB_TOURNAMENT_DIFF_LOG", "").strip().lower() not in (
        "1", "true", "yes", "on"
    ):
        return
    for section in ("prof", "batl"):
        before = previous.get(section, {}) if isinstance(previous, dict) else {}
        after = current.get(section, {}) if isinstance(current, dict) else {}
        before = before.get("c", {}) if isinstance(before, dict) else {}
        after = after.get("c", {}) if isinstance(after, dict) else {}
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        changed = []
        for key in sorted(set(before) | set(after), key=lambda value: str(value)):
            old_value = before.get(key)
            new_value = after.get(key)
            if old_value == new_value:
                continue
            old_text = repr(old_value)
            new_text = repr(new_value)
            changed.append(
                f"{key}:{old_text[:160]}->{new_text[:160]}"
            )
        if changed:
            log(f"[TOURNAMENT-DIFF] {section} " + " | ".join(changed))


def save_has_main_park(data):
    if not isinstance(data, dict):
        return False
    park = data.get("park", {})
    if not isinstance(park, dict):
        return False
    content = park.get("c", {})
    if not isinstance(content, dict):
        return False
    return _as_int(content.get("45"), 0) > 0 or _as_int(content.get("57"), 0) > 0


def merge_server_resource_floor(previous, current, log, label):
    merged = json.loads(json.dumps(current or {}))
    previous_res = (
        previous.get("res", {}).get("c", {})
        if isinstance(previous, dict)
        else {}
    )
    merged_res = (
        merged.get("res", {}).get("c", {})
        if isinstance(merged, dict)
        else {}
    )
    if isinstance(previous_res, dict) and isinstance(merged_res, dict):
        for resource_key in ("2",):
            old_value = _as_int(previous_res.get(resource_key), None)
            new_value = _as_int(merged_res.get(resource_key), None)
            if old_value is not None and new_value is not None and old_value > new_value:
                merged_res[resource_key] = old_value
                log(
                    f"[SAVE] {label} preserved res.{resource_key} "
                    f"{new_value} -> {old_value}"
                )
    return merged


def _park_content(save_data, key="park"):
    if not isinstance(save_data, dict):
        return {}
    park = save_data.get(key, {})
    if not isinstance(park, dict):
        return {}
    content = park.get("c", {})
    return content if isinstance(content, dict) else {}


def _profile_content(save_data):
    if not isinstance(save_data, dict):
        return {}
    profile = save_data.get("prof", {})
    if not isinstance(profile, dict):
        return {}
    content = profile.get("c", {})
    return content if isinstance(content, dict) else {}


def saved_player_level(saved, default=1):
    level = _as_int(_profile_content(saved).get("5"), default)
    return max(1, level if level is not None else default)


def _preserve_profile_level_floor(previous_entry, incoming_entry, log, label):
    previous_content = (
        previous_entry.get("c", {})
        if isinstance(previous_entry, dict)
        else {}
    )
    incoming_content = (
        incoming_entry.get("c", {})
        if isinstance(incoming_entry, dict)
        else {}
    )
    if not isinstance(previous_content, dict) or not isinstance(incoming_content, dict):
        return 0
    previous_level = _as_int(previous_content.get("5"), None)
    incoming_level = _as_int(incoming_content.get("5"), None)
    if previous_level is None:
        return 0
    if incoming_level is not None and incoming_level >= previous_level:
        return 0
    incoming_content["5"] = previous_content.get("5")
    log(
        f"[SAVE] {label} preserved player level "
        f"{incoming_level} -> {previous_level}"
    )
    return 1


def _preserve_profile_account_identity(previous_entry, incoming_entry, log, label):
    previous_content = (
        previous_entry.get("c", {})
        if isinstance(previous_entry, dict)
        else {}
    )
    incoming_content = (
        incoming_entry.get("c", {})
        if isinstance(incoming_entry, dict)
        else {}
    )
    if not isinstance(previous_content, dict) or not isinstance(incoming_content, dict):
        return 0
    previous_identity = str(previous_content.get("10") or "").strip()
    incoming_identity = str(incoming_content.get("10") or "").strip()
    if not previous_identity or incoming_identity == previous_identity:
        return 0
    incoming_content["10"] = previous_content.get("10")
    log(
        f"[SAVE] {label} preserved profile account identity "
        f"{incoming_identity or '<missing>'!r} -> {previous_identity!r}"
    )
    return 1


def _resource_content(save_data):
    if not isinstance(save_data, dict):
        return {}
    resources = save_data.get("res", {})
    if not isinstance(resources, dict):
        return {}
    content = resources.get("c", {})
    return content if isinstance(content, dict) else {}


def _gen_content(save_data):
    if not isinstance(save_data, dict):
        return {}
    gen = save_data.get("gen", {})
    if not isinstance(gen, dict):
        return {}
    content = gen.get("c", {})
    return content if isinstance(content, dict) else {}


def _batl_content(save_data):
    if not isinstance(save_data, dict):
        return {}
    battle = save_data.get("batl", {})
    if not isinstance(battle, dict):
        return {}
    content = battle.get("c", {})
    return content if isinstance(content, dict) else {}


def _battle_progress_value_score(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value not in (0, -1) else 0
    if isinstance(value, float):
        return 1 if value not in (0.0, -1.0) else 0
    if isinstance(value, list):
        return sum(_battle_progress_value_score(item) for item in value)
    if isinstance(value, dict):
        return sum(_battle_progress_value_score(item) for item in value.values())
    return 0


def _battle_progress_score(content):
    if not isinstance(content, dict):
        return 0
    # These fields are the observed battle state fields. Ignore timestamp-ish
    # constants such as 1, 63, 64, 8 and currency-like 96 so a fresh battle
    # packet cannot look richer just because time or teeth changed.
    progress_keys = (
        "97", "98", "99", "100", "101", "102", "103", "104", "105", "106",
        "107", "108", "109", "118", "119", "120", "121", "122", "123",
        "124", "125", "126", "127", "128", "129", "130", "131", "132",
        "133", "134", "135", "137", "138", "139",
    )
    return sum(_battle_progress_value_score(content.get(key)) for key in progress_keys)


PROFILE_BATTLE_PROGRESS_KEYS = ()


def _merge_profile_battle_progress_floor(previous_entry, incoming_entry, log, label):
    if not isinstance(previous_entry, dict) or not isinstance(incoming_entry, dict):
        return 0
    previous_content = previous_entry.get("c")
    incoming_content = incoming_entry.get("c")
    if not isinstance(previous_content, dict) or not isinstance(incoming_content, dict):
        return 0

    if (
        _tournament_result_progress_changed(previous_content, incoming_content)
        or _tournament_season_ack_changed(previous_content, incoming_content)
    ):
        log(
            f"[TOURNAMENT] {label} accepted result/season profile write "
            f"without old prof.110-114 floor"
        )
        return 0

    changed = 0
    changed_keys = []
    for key in PROFILE_BATTLE_PROGRESS_KEYS:
        if key not in previous_content or key not in incoming_content:
            continue
        previous_score = _battle_progress_value_score(previous_content.get(key))
        incoming_score = _battle_progress_value_score(incoming_content.get(key))
        if previous_score <= 0 or incoming_score >= previous_score:
            continue
        incoming_content[key] = json.loads(json.dumps(previous_content[key]))
        changed += 1
        changed_keys.append(key)

    if changed:
        log(
            f"[BATTLE] {label} preserved profile battle fields "
            f"keys={changed_keys}"
        )
    return changed


def _profile_has_active_battle_cooldowns(content):
    return False


def ensure_profile_battle_cooldown_mode(content, log, label):
    if not isinstance(content, dict) or not _profile_has_active_battle_cooldowns(content):
        return 0
    changed = []
    if content.get("94") is True:
        content["94"] = False
        changed.append("94")
    if changed:
        log(f"[BATTLE] {label} restored profile cooldown mode fields={changed}")
    return len(changed)


def _profile_battle_team_ids(content):
    ids = [0, 0, 0]
    if not isinstance(content, dict):
        return ids
    for index, key in enumerate(("74", "75", "76")):
        value = content.get(key)
        if isinstance(value, list) and value:
            ids[index] = _as_int(value[0], 0)
        else:
            ids[index] = _as_int(value, 0)
    return ids


def _list3_ints(value):
    result = [0, 0, 0]
    if isinstance(value, list):
        for index in range(min(3, len(value))):
            result[index] = _as_int(value[index], 0)
    return result


def _battle_context_value_score(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value not in (0, -1) else 0
    if isinstance(value, list):
        return sum(_battle_context_value_score(item) for item in value)
    return 0


def _battle_context_score(content):
    if not isinstance(content, dict):
        return 0
    return sum(_battle_context_value_score(content.get(key)) for key in BATTLE_CONTEXT_KEYS)


def _battle_context_strong_score(content):
    if not isinstance(content, dict):
        return 0
    return sum(_battle_context_value_score(content.get(key)) for key in ("97", "98", "101", "118"))


def _battle_context_snapshot(content):
    if not isinstance(content, dict) or _battle_context_strong_score(content) <= 0:
        return {}
    snapshot = {}
    for key in BATTLE_CONTEXT_KEYS:
        if key in content:
            snapshot[key] = json.loads(json.dumps(content[key]))
    return snapshot


def _saved_meta(saved):
    if not isinstance(saved, dict):
        return {}
    meta = saved.setdefault("__meta", {})
    if not isinstance(meta, dict):
        meta = {}
        saved["__meta"] = meta
    return meta


def remember_battle_context(saved, content, log, label):
    snapshot = _battle_context_snapshot(content)
    if not snapshot:
        return 0
    meta = _saved_meta(saved)
    previous = meta.get("battle_context")
    previous_score = _battle_context_score(previous)
    snapshot_score = _battle_context_score(snapshot)
    if previous == snapshot or previous_score > snapshot_score:
        return 0
    meta["battle_context"] = snapshot
    log(
        f"[BATTLE] {label} remembered battle context "
        f"score={snapshot_score} keys={sorted(snapshot.keys())}"
    )
    return 1


def apply_battle_context_snapshot(content, snapshot, log, label):
    # Battle fields form one versioned client state. Recombining selected keys
    # from an older sidecar can create an object that the client never wrote.
    return 0


def hydrate_battle_context_from_saved(saved, content, log, label):
    if not isinstance(saved, dict):
        return 0
    meta = saved.get("__meta", {})
    snapshot = meta.get("battle_context") if isinstance(meta, dict) else None
    return apply_battle_context_snapshot(content, snapshot, log, label)


def synthesize_profile_battle_cooldowns_from_batl(saved, profile_content, session_state, log, label):
    # Tournament UI reads profile fields 111/112 as cup totals. Keeping battle
    # cooldowns in those fields makes the tournament start at huge cup counts.
    return 0


def _guard_direct_battle_write(saved, key, incoming_entry, log):
    if key == "batl":
        guarded = {"batl": json.loads(json.dumps(incoming_entry))}
        _preserve_battle_progress(saved, guarded, log, "direct write")
        remember_battle_context(saved, guarded.get("batl", {}).get("c"), log, "direct write")
        return guarded["batl"]
    elif key == "prof":
        incoming_entry = json.loads(json.dumps(incoming_entry))
        _preserve_profile_level_floor(
            saved.get("prof"),
            incoming_entry,
            log,
            "direct write",
        )
        _preserve_profile_account_identity(
            saved.get("prof"),
            incoming_entry,
            log,
            "direct write",
        )
        _merge_profile_battle_progress_floor(
            saved.get("prof"),
            incoming_entry,
            log,
            "direct write",
        )
        synthesize_profile_battle_cooldowns_from_batl(
            saved,
            incoming_entry.get("c"),
            {},
            log,
            "direct write",
        )
        return incoming_entry
    return incoming_entry


def _preserve_battle_progress(previous, merged, log, label):
    merged_entry = merged.get("batl")
    previous_entry = previous.get("batl")
    if not isinstance(merged_entry, dict):
        return 0
    merged_batl = _batl_content(merged)
    incoming_slots = merged_batl.get("104")
    if (
        "104" in merged_batl
        and not is_smartfox_long_array(incoming_slots)
    ):
        previous_batl = _batl_content(previous)
        if isinstance(previous_entry, dict) and previous_batl:
            merged["batl"] = json.loads(json.dumps(previous_entry))
            log(f"[BATTLE] {label} rejected invalid batl.104 wire values")
            return 1
        raise ValueError("invalid first batl.104 write")
    if not isinstance(previous_entry, dict):
        return 0
    previous_batl = _batl_content(previous)
    if not previous_batl:
        return 0

    previous_version = _as_int(previous_entry.get("v"), 0)
    merged_version = _as_int(merged_entry.get("v"), 0)
    if not merged_batl:
        merged["batl"] = json.loads(json.dumps(previous_entry))
        log(
            f"[BATTLE] {label} rejected empty batl state "
            f"version {merged_version}->{previous_version}"
        )
        return 1
    previous_slots = previous_batl.get("104")
    if isinstance(previous_slots, list) and (
        not is_smartfox_long_array(incoming_slots)
        or (
            len(previous_slots) > 0
            and len(incoming_slots) < len(previous_slots)
        )
    ):
        merged["batl"] = json.loads(json.dumps(previous_entry))
        log(
            f"[BATTLE] {label} rejected incomplete batl.104 state "
            f"version {merged_version}->{previous_version}"
        )
        return 1
    explicit_equal_state = (
        merged_version == previous_version
        and (
            "104" in merged_batl
            or ("104" not in previous_batl and bool(merged_batl))
        )
    )
    if merged_version > previous_version or explicit_equal_state:
        log(
            f"[BATTLE] {label} accepted incoming batl version "
            f"{previous_version}->{merged_version}"
        )
        return 0

    merged["batl"] = json.loads(json.dumps(previous_entry))
    log(
        f"[BATTLE] {label} rejected older batl state "
        f"version {merged_version}->{previous_version}"
    )
    return 1


def _preserve_harbor_level_guard(previous, merged, log, label):
    previous_gen = _gen_content(previous)
    merged_gen = _gen_content(merged)
    merged_prof = _profile_content(merged)
    if not previous_gen or not merged_gen:
        return 0
    if "65" not in previous_gen or "65" not in merged_gen:
        return 0

    previous_harbor = _as_int(previous_gen.get("65"), None)
    merged_harbor = _as_int(merged_gen.get("65"), None)
    player_level = _as_int(merged_prof.get("5"), None)
    if previous_harbor is None or merged_harbor is None:
        return 0
    if merged_harbor <= previous_harbor:
        return 0

    jumped_multiple_levels = merged_harbor > previous_harbor + 1
    exceeds_player_level = (
        isinstance(player_level, int)
        and player_level >= 0
        and merged_harbor > player_level
    )
    if not jumped_multiple_levels and not exceeds_player_level:
        return 0

    old_value = merged_gen.get("65")
    merged_gen["65"] = previous_gen.get("65")
    reasons = []
    if jumped_multiple_levels:
        reasons.append(f"jump {previous_harbor}->{merged_harbor}")
    if exceeds_player_level:
        reasons.append(f"player_level={player_level}")
    log(
        f"[SAVE] {label} preserved gen.65 harbor level "
        f"{old_value!r} -> {previous_gen.get('65')!r} "
        f"reason={', '.join(reasons)}"
    )
    return 1


def _card_pack_claim_has_reward_progress(previous, merged):
    """Recognize the reward writes paired with a legitimate free-pack claim."""
    previous_res = _resource_content(previous)
    merged_res = _resource_content(merged)
    if isinstance(previous_res, dict) and isinstance(merged_res, dict):
        for key, merged_value in merged_res.items():
            if key in ("1", "8"):
                continue
            previous_value = previous_res.get(key)
            if (
                isinstance(previous_value, (int, float))
                and not isinstance(previous_value, bool)
                and isinstance(merged_value, (int, float))
                and not isinstance(merged_value, bool)
                and merged_value > previous_value
            ):
                return True

    previous_gen = _gen_content(previous)
    merged_gen = _gen_content(merged)
    if not isinstance(previous_gen, dict) or not isinstance(merged_gen, dict):
        return False
    passive_gen_keys = {"1", "8", "63", "64"}
    return any(
        previous_gen.get(key) != merged_gen.get(key)
        for key in set(previous_gen) | set(merged_gen)
        if key not in passive_gen_keys
    )


def _preserve_profile_timer_progress(previous, merged, log, label, allow_client_timer_resets=False):
    previous_prof = _profile_content(previous)
    merged_prof = _profile_content(merged)
    if not previous_prof or not merged_prof:
        return 0
    changed = 0
    tournament_result_changed = _tournament_result_progress_changed(
        previous_prof,
        merged_prof,
    )
    if "144" in previous_prof and "144" in merged_prof:
        previous_value = _as_int(previous_prof.get("144"), 0)
        merged_value = _as_int(merged_prof.get("144"), 0)
        previous_limit = max(0, _as_int(previous_prof.get("145"), 8 * 60 * 60 * 1000))
        claim_with_rewards = (
            previous_limit > 0
            and previous_value >= previous_limit
            and 0 <= merged_value <= POST_REPLAY_TIMER_RESET_MS
            and _card_pack_claim_has_reward_progress(previous, merged)
        )
        if previous_value > merged_value:
            if (
                0 <= merged_value <= POST_REPLAY_TIMER_RESET_MS
                and (allow_client_timer_resets or claim_with_rewards)
            ):
                reason = "claim rewards" if claim_with_rewards else "client timer reset window"
                log(
                    f"[TIME] {label} accepted client reset for prof.144 card-pack "
                    f"{previous_value} -> {merged_value} reason={reason}"
                )
            else:
                old_value = merged_prof.get("144")
                merged_prof["144"] = previous_prof.get("144")
                changed += 1
                log(
                    f"[TIME] {label} preserved prof.144 card-pack "
                    f"{old_value!r} -> {previous_prof.get('144')!r}"
                )
    for key in ("1", "146"):
        if key not in previous_prof or key not in merged_prof:
            continue
        previous_value = _as_int(previous_prof.get(key), 0)
        merged_value = _as_int(merged_prof.get(key), 0)
        if key == "146":
            if previous_value == merged_value:
                continue
            if (
                tournament_result_changed
                and _valid_profile_clock_anchor(merged_value)
            ):
                log(
                    f"[TOURNAMENT] {label} accepted prof.146 result anchor "
                    f"{previous_value!r} -> {merged_value!r}"
                )
                continue
            old_value = merged_prof.get(key)
            merged_prof[key] = previous_prof.get(key)
            changed += 1
            log(
                f"[TOURNAMENT] {label} rejected startup prof.146 reset "
                f"{old_value!r} -> {previous_prof.get(key)!r}"
            )
            continue
        if previous_value <= merged_value:
            continue
        old_value = merged_prof.get(key)
        merged_prof[key] = previous_prof.get(key)
        changed += 1
        log(
            f"[TIME] {label} preserved prof.{key} timestamp "
            f"{old_value!r} -> {previous_prof.get(key)!r}"
        )
    return changed


PROFILE_TOURNAMENT_PROGRESS_KEYS = (
    "74", "75", "76", "95", "107", "108", "109",
    "111", "112", "113", "114", "122", "123", "124",
    "132", "133", "134", "135", "137", "138",
)


def _profile_tournament_value_score(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value not in (0, -1) else 0
    if isinstance(value, float):
        return 1 if value not in (0.0, -1.0) else 0
    if isinstance(value, list):
        return sum(_profile_tournament_value_score(item) for item in value)
    if isinstance(value, dict):
        return sum(_profile_tournament_value_score(item) for item in value.values())
    if isinstance(value, str):
        return 1 if value else 0
    return 0


def _merge_profile_tournament_value(previous_value, merged_value):
    previous_score = _profile_tournament_value_score(previous_value)
    merged_score = _profile_tournament_value_score(merged_value)
    if previous_score <= 0:
        return merged_value, False
    if merged_score <= 0:
        return json.loads(json.dumps(previous_value)), True
    if isinstance(previous_value, (int, float)) and isinstance(merged_value, (int, float)):
        if previous_value > merged_value:
            return previous_value, True
        return merged_value, False
    if isinstance(previous_value, list) and isinstance(merged_value, list):
        changed = False
        merged_items = json.loads(json.dumps(merged_value))
        limit = min(len(previous_value), len(merged_items))
        for index in range(limit):
            item, item_changed = _merge_profile_tournament_value(
                previous_value[index],
                merged_items[index],
            )
            if item_changed:
                merged_items[index] = item
                changed = True
        if len(previous_value) > len(merged_items):
            merged_items.extend(json.loads(json.dumps(previous_value[len(merged_items):])))
            changed = True
        return merged_items, changed
    if isinstance(previous_value, dict) and isinstance(merged_value, dict):
        changed = False
        merged_dict = json.loads(json.dumps(merged_value))
        for key, previous_item in previous_value.items():
            item, item_changed = _merge_profile_tournament_value(
                previous_item,
                merged_dict.get(key),
            )
            if item_changed:
                merged_dict[key] = item
                changed = True
        return merged_dict, changed
    return merged_value, False


def _preserve_profile_tournament_progress(previous, merged, log, label):
    previous_prof = _profile_content(previous)
    merged_prof = _profile_content(merged)
    if not previous_prof or not merged_prof:
        return 0
    if _tournament_result_progress_changed(previous_prof, merged_prof):
        log(f"[TOURNAMENT] {label} accepted tournament result profile values")
        return 0
    if _tournament_season_ack_changed(previous_prof, merged_prof):
        log(f"[TOURNAMENT] {label} accepted season rollover profile values")
        return 0
    changed = 0
    changed_keys = []
    for key in PROFILE_TOURNAMENT_PROGRESS_KEYS:
        if key not in previous_prof:
            continue
        new_value, did_change = _merge_profile_tournament_value(
            previous_prof.get(key),
            merged_prof.get(key),
        )
        if did_change:
            merged_prof[key] = new_value
            changed += 1
            changed_keys.append(key)
    if changed:
        log(
            f"[TOURNAMENT] {label} preserved profile tournament fields "
            f"keys={changed_keys}"
        )
    return changed


def _preserve_high_u32_array(
    previous_park,
    merged_park,
    key,
    log,
    label,
    item_label,
    allow_client_timer_resets=False,
    reset_threshold=POST_REPLAY_TIMER_RESET_MS,
):
    previous_values = previous_park.get(key)
    merged_values = merged_park.get(key)
    if not isinstance(previous_values, list) or not isinstance(merged_values, list):
        return 0
    changed = 0
    for index in range(min(len(previous_values), len(merged_values))):
        try:
            previous_high = _high_u32(previous_values[index])
            merged_high = _high_u32(merged_values[index])
        except Exception:
            continue
        if (
            allow_client_timer_resets
            and previous_high > merged_high
            and 0 <= merged_high <= reset_threshold
        ):
            log(
                f"[TIME] {label} accepted client reset for {item_label}[{index}] "
                f"high32 {previous_high} -> {merged_high}"
            )
            continue
        if previous_high > merged_high:
            old_value = merged_values[index]
            merged_values[index] = previous_values[index]
            changed += 1
            log(
                f"[TIME] {label} preserved {item_label}[{index}] "
                f"high32 {merged_high} -> {previous_high} "
                f"value {old_value!r} -> {previous_values[index]!r}"
            )
    return changed


def _preserve_port_resume_progress(previous_park, merged_park, log, label, allow_client_timer_resets=False):
    previous_values = previous_park.get("55")
    merged_values = merged_park.get("55")
    if not isinstance(previous_values, list) or not isinstance(merged_values, list):
        return 0
    changed = 0
    for index in range(min(2, len(previous_values), len(merged_values))):
        try:
            previous_remaining = _high_u32(previous_values[index])
            merged_remaining = _high_u32(merged_values[index])
            previous_elapsed = _low_u32(previous_values[index])
            merged_elapsed = _low_u32(merged_values[index])
        except Exception:
            continue

        progressed = (
            previous_remaining < merged_remaining
            and previous_elapsed >= merged_elapsed
        )
        completed = (
            previous_remaining == 0
            and merged_remaining != 0
            and previous_elapsed >= merged_elapsed
        )
        incomplete_zero = previous_remaining == 0 and 0 < previous_elapsed < merged_elapsed + merged_remaining
        # A newly started shipment usually has near-zero elapsed and a large
        # remaining timer; do not overwrite that legitimate reset.
        looks_like_new_order = merged_elapsed < 5000 and merged_remaining > 60000
        if (
            allow_client_timer_resets
            and previous_remaining == 0
            and merged_remaining > 0
            and merged_elapsed <= POST_REPLAY_TIMER_RESET_MS
        ):
            log(
                f"[TIME] {label} accepted client reset for port.55[{index}] "
                f"remaining {previous_remaining}->{merged_remaining} "
                f"elapsed {previous_elapsed}->{merged_elapsed}"
            )
            continue
        if incomplete_zero or not (progressed or completed) or looks_like_new_order:
            continue

        old_value = merged_values[index]
        merged_values[index] = previous_values[index]
        changed += 1
        log(
            f"[TIME] {label} preserved port.55[{index}] "
            f"remaining {merged_remaining}->{previous_remaining} "
            f"elapsed {merged_elapsed}->{previous_elapsed} "
            f"value {old_value!r} -> {previous_values[index]!r}"
        )
    return changed


def _preserve_object_timer_progress(previous_park, merged_park, log, label, allow_client_timer_resets=False):
    previous_values = previous_park.get("47")
    merged_values = merged_park.get("47")
    previous_objects = previous_park.get("46")
    merged_objects = merged_park.get("46")
    previous_count = _as_int(previous_park.get("45"), 0)
    merged_count = _as_int(merged_park.get("45"), 0)
    if not isinstance(previous_values, list) or not isinstance(merged_values, list):
        return 0
    changed = 0
    limit = min(max(min(previous_count, merged_count), 0), len(previous_values), len(merged_values))
    for index in range(limit):
        if (
            isinstance(previous_objects, list)
            and isinstance(merged_objects, list)
            and index < len(previous_objects)
            and index < len(merged_objects)
            and previous_objects[index] != merged_objects[index]
        ):
            continue
        try:
            previous_high = _high_u32(previous_values[index])
            merged_high = _high_u32(merged_values[index])
        except Exception:
            continue
        if (
            allow_client_timer_resets
            and previous_high > merged_high
            and 0 <= merged_high <= POST_REPLAY_TIMER_RESET_MS
        ):
            log(
                f"[TIME] {label} accepted client reset for object.47[{index}] "
                f"elapsed_ms {previous_high} -> {merged_high}"
            )
            continue
        if previous_high > merged_high:
            old_value = merged_values[index]
            merged_values[index] = _replace_high_u32(merged_values[index], previous_high)
            changed += 1
            log(
                f"[TIME] {label} preserved object.47[{index}] "
                f"elapsed_ms {merged_high} -> {previous_high} "
                f"kept placement low32={_low_u32(old_value)} "
                f"value {old_value!r} -> {merged_values[index]!r}"
            )
    return changed


def _preserve_active_expansion_timers(previous_park, merged_park, log, label, allow_client_timer_resets=False):
    previous_values = previous_park.get("50")
    merged_values = merged_park.get("50")
    previous_states = previous_park.get("51")
    merged_states = merged_park.get("51")
    if not isinstance(previous_values, list) or not isinstance(merged_values, list):
        return 0
    changed = 0
    for index in range(min(len(previous_values), len(merged_values))):
        previous_active = (
            isinstance(previous_states, list)
            and index < len(previous_states)
            and _as_int(previous_states[index], 0) != 0
        )
        merged_active = (
            isinstance(merged_states, list)
            and index < len(merged_states)
            and _as_int(merged_states[index], 0) != 0
        )
        if not previous_active and not merged_active:
            continue
        try:
            previous_high = _high_u32(previous_values[index])
            merged_high = _high_u32(merged_values[index])
        except Exception:
            continue
        if (
            allow_client_timer_resets
            and previous_high > merged_high
            and 0 <= merged_high <= POST_REPLAY_TIMER_RESET_MS
        ):
            log(
                f"[TIME] {label} accepted client reset for active expansion.50[{index}] "
                f"high32 {previous_high} -> {merged_high}"
            )
            continue
        if previous_high > merged_high:
            old_value = merged_values[index]
            merged_values[index] = previous_values[index]
            changed += 1
            log(
                f"[TIME] {label} preserved active expansion.50[{index}] "
                f"high32 {merged_high} -> {previous_high} "
                f"value {old_value!r} -> {previous_values[index]!r}"
            )
    return changed


def _preserve_counted_array_group(
    previous_park,
    merged_park,
    count_key,
    array_keys,
    log,
    label,
    item_label,
    allow_client_count_decrease=False,
):
    previous_count = _as_int(previous_park.get(count_key), 0)
    merged_count = _as_int(merged_park.get(count_key), 0)
    if previous_count <= merged_count:
        return 0
    if allow_client_count_decrease:
        log(
            f"[SAVE] {label} accepted client count decrease for {item_label} "
            f"{previous_count} -> {merged_count}"
        )
        return 0

    changed = 0
    old_count = merged_park.get(count_key)
    merged_park[count_key] = previous_park.get(count_key)
    changed += 1
    copied = [count_key]

    for key in array_keys:
        previous_value = previous_park.get(key)
        merged_value = merged_park.get(key)
        if not isinstance(previous_value, list):
            continue
        if not isinstance(merged_value, list) or len(previous_value) > len(merged_value):
            merged_park[key] = json.loads(json.dumps(previous_value))
            changed += 1
            copied.append(key)

    log(
        f"[SAVE] {label} preserved {item_label} count "
        f"{old_count!r} -> {previous_park.get(count_key)!r} copied={','.join(copied)}"
    )
    return changed


def _preserve_active_expansion_state(previous_park, merged_park, log, label, allow_client_timer_resets=False):
    """Keep active expansion slots from being erased by stale local startup writes."""
    previous_states = previous_park.get("51")
    merged_states = merged_park.get("51")
    if not isinstance(previous_states, list) or not isinstance(merged_states, list):
        return 0

    previous_start = previous_park.get("49")
    merged_start = merged_park.get("49")
    previous_timers = previous_park.get("50")
    merged_timers = merged_park.get("50")

    changed = 0
    for index in range(min(len(previous_states), len(merged_states))):
        previous_active = _as_int(previous_states[index], 0) != 0
        merged_active = _as_int(merged_states[index], 0) != 0
        if not previous_active or merged_active:
            continue
        if allow_client_timer_resets:
            log(
                f"[TIME] {label} accepted client-cleared expansion slot[{index}] "
                f"state {previous_states[index]!r} -> {merged_states[index]!r}"
            )
            continue

        old_state = merged_states[index]
        merged_states[index] = previous_states[index]
        copied = ["51"]

        if (
            isinstance(previous_start, list)
            and isinstance(merged_start, list)
            and index < len(previous_start)
            and index < len(merged_start)
        ):
            merged_start[index] = previous_start[index]
            copied.append("49")

        if (
            isinstance(previous_timers, list)
            and isinstance(merged_timers, list)
            and index < len(previous_timers)
            and index < len(merged_timers)
        ):
            merged_timers[index] = previous_timers[index]
            copied.append("50")

        changed += 1
        log(
            f"[TIME] {label} preserved active expansion slot[{index}] "
            f"state {old_state!r} -> {previous_states[index]!r} copied={','.join(copied)}"
        )
    return changed


def _preserve_low32_high_u16_array(previous_park, merged_park, key, log, label, item_label):
    previous_values = previous_park.get(key)
    merged_values = merged_park.get(key)
    if not isinstance(previous_values, list) or not isinstance(merged_values, list):
        return 0
    changed = 0
    for index in range(min(len(previous_values), len(merged_values))):
        try:
            previous_high = _low32_high_u16(previous_values[index])
            merged_high = _low32_high_u16(merged_values[index])
        except Exception:
            continue
        if previous_high > merged_high:
            old_value = merged_values[index]
            merged_values[index] = previous_values[index]
            changed += 1
            log(
                f"[TIME] {label} preserved {item_label}[{index}] "
                f"low32.high16 {merged_high} -> {previous_high} "
                f"value {old_value!r} -> {previous_values[index]!r}"
            )
    return changed


def _preserve_p59_hatch_elapsed(previous_park, merged_park, log, label):
    previous_values = previous_park.get("59")
    merged_values = merged_park.get("59")
    previous_timers = previous_park.get("58")
    merged_timers = merged_park.get("58")
    previous_count = _as_int(previous_park.get("57"), 0)
    merged_count = _as_int(merged_park.get("57"), 0)
    if not isinstance(previous_values, list) or not isinstance(merged_values, list):
        return 0
    changed = 0
    limit = min(max(min(previous_count, merged_count), 0), len(previous_values), len(merged_values))
    for index in range(limit):
        previous_hatch = (
            isinstance(previous_timers, list)
            and index < len(previous_timers)
            and _as_int(previous_timers[index], 0) == 0
            and _as_int(previous_values[index], 0) != 0
        )
        merged_hatch = (
            isinstance(merged_timers, list)
            and index < len(merged_timers)
            and _as_int(merged_timers[index], 0) == 0
            and _as_int(merged_values[index], 0) != 0
        )
        if not previous_hatch and not merged_hatch:
            continue
        try:
            previous_elapsed = _high_u32(previous_values[index])
            merged_elapsed = _high_u32(merged_values[index])
        except Exception:
            continue
        if previous_elapsed > merged_elapsed:
            old_value = merged_values[index]
            merged_values[index] = _replace_high_u32(merged_values[index], previous_elapsed)
            changed += 1
            log(
                f"[TIME] {label} preserved hatch dino.59[{index}] "
                f"high32 {merged_elapsed} -> {previous_elapsed} "
                f"value {old_value!r} -> {merged_values[index]!r}"
            )
    return changed


def _merge_single_park_timer_floor(
    previous_park,
    merged_park,
    log,
    label,
    allow_client_timer_resets=False,
):
    changed = 0
    changed += _preserve_counted_array_group(
        previous_park,
        merged_park,
        "45",
        ("46", "47"),
        log,
        label,
        "park objects",
        allow_client_count_decrease=True,
    )
    changed += _preserve_counted_array_group(
        previous_park,
        merged_park,
        "57",
        ("58", "59"),
        log,
        label,
        "dinos",
        allow_client_count_decrease=True,
    )
    changed += _preserve_counted_array_group(
        previous_park,
        merged_park,
        "60",
        ("61", "62"),
        log,
        label,
        "buildings",
    )
    changed += _preserve_object_timer_progress(
        previous_park,
        merged_park,
        log,
        label,
        allow_client_timer_resets=allow_client_timer_resets,
    )
    changed += _preserve_active_expansion_state(
        previous_park,
        merged_park,
        log,
        label,
        allow_client_timer_resets=allow_client_timer_resets,
    )
    changed += _preserve_active_expansion_timers(
        previous_park,
        merged_park,
        log,
        label,
        allow_client_timer_resets=allow_client_timer_resets,
    )
    changed += _preserve_port_resume_progress(
        previous_park,
        merged_park,
        log,
        label,
        allow_client_timer_resets=allow_client_timer_resets,
    )
    changed += _preserve_p59_hatch_elapsed(previous_park, merged_park, log, label)
    changed += _preserve_high_u32_array(
        previous_park,
        merged_park,
        "61",
        log,
        label,
        "building.61",
        allow_client_timer_resets=allow_client_timer_resets,
        reset_threshold=POST_REPLAY_TIMER_RESET_SECONDS,
    )
    if changed:
        log(f"[TIME] {label} preserved timer slots={changed}")
    return changed


def merge_server_timer_floor(previous, current, log, label, allow_client_timer_resets=False):
    merged = json.loads(json.dumps(current or {}))
    for park_key in ("park", "aqpk", "arpk"):
        previous_park = _park_content(previous, park_key)
        merged_park = _park_content(merged, park_key)
        if not previous_park or not merged_park:
            continue
        park_label = label if park_key == "park" else f"{label} {park_key}"
        _merge_single_park_timer_floor(
            previous_park,
            merged_park,
            log,
            park_label,
            allow_client_timer_resets=allow_client_timer_resets,
        )
    return merged


def merge_postreplay_client_progress(previous, current, log, allow_client_timer_resets=False):
    """Keep client progress after replay, without letting stale local floors win."""
    merged = merge_server_resource_floor(previous, current, log, "post-replay merge")
    _preserve_profile_level_floor(
        previous.get("prof") if isinstance(previous, dict) else None,
        merged.get("prof") if isinstance(merged, dict) else None,
        log,
        "post-replay merge",
    )
    _preserve_profile_account_identity(
        previous.get("prof") if isinstance(previous, dict) else None,
        merged.get("prof") if isinstance(merged, dict) else None,
        log,
        "post-replay merge",
    )
    battle_changed = _preserve_battle_progress(
        previous,
        merged,
        log,
        "post-replay merge",
    )
    battle_changed += remember_battle_context(
        merged,
        _batl_content(merged),
        log,
        "post-replay merge",
    )
    harbor_changed = _preserve_harbor_level_guard(
        previous,
        merged,
        log,
        "post-replay merge",
    )
    profile_changed = _preserve_profile_timer_progress(
        previous,
        merged,
        log,
        "post-replay merge",
        allow_client_timer_resets=allow_client_timer_resets,
    )
    tournament_profile_changed = _preserve_profile_tournament_progress(
        previous,
        merged,
        log,
        "post-replay merge",
    )
    merged = merge_server_timer_floor(
        previous,
        merged,
        log,
        "post-replay merge",
        allow_client_timer_resets=allow_client_timer_resets,
    )
    if profile_changed:
        log(f"[TIME] post-replay merge preserved profile timer slots={profile_changed}")
    if tournament_profile_changed:
        log(f"[TOURNAMENT] post-replay merge preserved profile slots={tournament_profile_changed}")
    if harbor_changed:
        log(f"[SAVE] post-replay merge preserved harbor level slots={harbor_changed}")
    if battle_changed:
        log(f"[BATTLE] post-replay merge preserved battle state slots={battle_changed}")
    return merged


def import_guest_save(username, source_path, log):
    if not source_path:
        return
    source_path = os.path.abspath(source_path)
    if not os.path.exists(source_path):
        log(f"[SAVE] import skipped; source does not exist: {source_path!r}")
        return
    os.makedirs(SAVE_DIR, exist_ok=True)
    target_path = guest_save_path(username)
    if os.path.abspath(target_path) == source_path:
        log(f"[SAVE] import skipped; source is already active save {target_path!r}")
        return
    if os.path.exists(target_path):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{target_path}.bak_import_{stamp}"
        shutil.copy2(target_path, backup_path)
        log(f"[SAVE] backed up active save to {backup_path!r}")
    shutil.copy2(source_path, target_path)
    log(f"[SAVE] imported {source_path!r} into active save {target_path!r}")


def should_preserve_existing_save(previous, current, threshold_bytes=2048):
    previous_size = save_payload_size(previous)
    current_size = save_payload_size(current)
    if previous_size < 0 or current_size < 0:
        return False, previous_size, current_size
    return previous_size > current_size + threshold_bytes, previous_size, current_size


EXTRA_SAVE_KEYS = {"batl"}
# Experimental addon/battle save keys. Disabled for park-only public testing
# because iOS battle cache appears incompatible with Android runtime.
# EXTRA_SAVE_KEYS = {"aqpk", "aqrd", "aqge", "arpk", "arrd", "arge", "batl"}
VISIT_READ_KEYS = {"prof", "res", "park", "road", "gen", "_cb_refer", "aqpk", "aqrd", "aqge", "arpk", "arrd", "arge", "batl"}
SAVE_REPLAY_GROUPS = {
    "park": "jurassic",
    "road": "jurassic",
    "gen": "jurassic",
    "aqpk": "aquatic",
    "aqrd": "aquatic",
    "aqge": "aquatic",
    "arpk": "arctic",
    "arrd": "arctic",
    "arge": "arctic",
}


def save_replay_group(key):
    return SAVE_REPLAY_GROUPS.get(str(key or ""))


def served_save_groups(session_state):
    """Return the per-session park groups already restored to the client."""
    if not isinstance(session_state, dict):
        return set()
    groups = session_state.get("served_save_groups")
    if isinstance(groups, set):
        return groups
    if isinstance(groups, (list, tuple)):
        groups = set(str(value) for value in groups)
    else:
        groups = set()
    session_state["served_save_groups"] = groups
    return groups


def mission_sidecar_progress_score(content):
    """Score persisted mission markers without treating mission IDs as progress."""
    if not isinstance(content, dict):
        return 0
    score = sum(
        1
        for field in ("16", "17", "39", "82")
        if _as_int(content.get(field), 0) != 0
    )
    if content.get("38") is True:
        score += 1

    active = content.get("30")
    if isinstance(active, dict):
        score += sum(value is True for value in active.get("35", []))
        score += sum(_as_int(value, 0) > 0 for value in active.get("36", []))
        score += sum(_as_int(value, 0) > 0 for value in active.get("37", []))

    missions = content.get("40")
    if isinstance(missions, list):
        for mission in missions:
            if not isinstance(mission, dict):
                continue
            score += sum(value is True for value in mission.get("42", []))
            score += sum(_as_int(value, 0) > 0 for value in mission.get("43", []))
            score += sum(_as_int(value, 0) > 0 for value in mission.get("44", []))
    return score


def should_preserve_park_group_write(previous_saved, key, incoming_entry, session_state):
    """Reject bootstrap/regressive park writes that would erase richer data.

    On park entry the client first writes a small bootstrap object and only
    then asks the server for that park.  A session-wide replay flag allowed
    this bootstrap to overwrite Aquatic/Arctic progress after reconnect.
    """
    group = save_replay_group(key)
    if not group:
        return False
    previous_entry = previous_saved.get(key) if isinstance(previous_saved, dict) else None
    if not (
        isinstance(previous_entry, dict)
        and isinstance(previous_entry.get("c"), dict)
        and isinstance(incoming_entry, dict)
        and isinstance(incoming_entry.get("c"), dict)
    ):
        return False
    try:
        previous_size = len(json.dumps(previous_entry["c"], ensure_ascii=True, sort_keys=True))
        incoming_size = len(json.dumps(incoming_entry["c"], ensure_ascii=True, sort_keys=True))
        previous_version = int(previous_entry.get("v", 0) or 0)
        incoming_version = int(incoming_entry.get("v", 0) or 0)
    except (TypeError, ValueError):
        return False
    if incoming_version > previous_version:
        return False
    if incoming_version < previous_version:
        return True
    if key in ("aqge", "arge"):
        previous_score = mission_sidecar_progress_score(previous_entry["c"])
        incoming_score = mission_sidecar_progress_score(incoming_entry["c"])
        if previous_score > 0 and incoming_score == 0:
            return True
    return (
        previous_size >= 128
        and incoming_size * 3 <= previous_size
    )


def _as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _has_nonempty_list(content, fields):
    return any(isinstance(content.get(field), list) and content.get(field) for field in fields)


def is_meaningful_save_content(key, content):
    if not isinstance(content, dict):
        return False
    if key in EXTRA_SAVE_KEYS:
        return True
    if key == "res":
        return (
            _as_int(content.get("1"), 3) != 3
            or _as_int(content.get("2"), 1000) != 1000
            or _as_int(content.get("4"), 0) != 0
            or _as_int(content.get("6"), 100) != 100
            or _as_int(content.get("7"), 100) != 100
        )
    if key == "gen":
        if _as_int(content.get("28"), 0) != 0:
            return True
        if _as_int(content.get("39"), 0) != 0:
            return True
        if _as_int(content.get("65"), 0) != 0:
            return True
        if isinstance(content.get("29"), list) and content.get("29"):
            return True
        if isinstance(content.get("30"), list) and content.get("30"):
            return True
        return False
    if key == "park":
        return _as_int(content.get("45"), 0) not in (0, 18)
    if key in ("aqpk", "arpk"):
        # Aquatic/glacier park sidecars can be meaningful even before they
        # contain dinos; the shape fields carry unlocked terrain/debris state.
        if _as_int(content.get("45"), 0) > 0:
            return True
        if _as_int(content.get("57"), 0) > 0 or _as_int(content.get("60"), 0) > 0:
            return True
        return _has_nonempty_list(content, ("46", "47", "49", "50", "51", "55", "56", "58", "59"))
    if key in ("aqrd", "arrd"):
        if _as_int(content.get("52"), 0) > 0:
            return True
        if _has_nonempty_list(content, ("53", "54")):
            return True
        return all(field in content for field in ("52", "53", "54"))
    if key in ("aqge", "arge"):
        for field in ("28", "33", "39", "65"):
            if _as_int(content.get(field), 0) != 0:
                return True
        return _has_nonempty_list(
            content,
            ("19", "21", "29", "30", "34", "40", "67", "70", "77", "78", "79", "80", "81", "82", "83"),
        )
    if key == "prof":
        return _as_int(content.get("5"), 0) != 0 or _as_int(content.get("9"), 1) != 1
    return False


def hardcash_balance_is_known(saved):
    if not isinstance(saved, dict):
        return False
    meta = saved.get("__meta", {})
    if not isinstance(meta, dict):
        return False
    if (
        meta.get("hardcash_state") != "known"
        or _as_int(meta.get("hardcash_schema_version"), None)
        != HARDCASH_SCHEMA_VERSION
        or str(meta.get("hardcash_source", "")) not in HARDCASH_TRUSTED_SOURCES
    ):
        return False
    baseline = _as_int(meta.get("hardcash_baseline"), None)
    estimate = _as_int(meta.get("hardcash_balance_estimate"), None)
    return (
        baseline is not None
        and 0 <= baseline <= HARDCASH_MAX_BALANCE
        and estimate is not None
        and 0 <= estimate <= HARDCASH_MAX_BALANCE
    )


def migrate_legacy_hardcash_sidecar(saved, log=None):
    """Trust only the pre-schema wallet ledger written by older Dino Server."""
    if not isinstance(saved, dict) or hardcash_balance_is_known(saved):
        return False
    meta = saved.get("__meta", {})
    if not isinstance(meta, dict):
        return False
    if meta.get("hardcash_state") not in (None, ""):
        return False
    baseline = _as_int(meta.get("hardcash_baseline"), None)
    estimate = _as_int(meta.get("hardcash_balance_estimate"), None)
    last_good = _as_int(meta.get("hardcash_last_good_estimate"), None)
    if (
        baseline is None
        or estimate is None
        or last_good is None
        or not 0 <= baseline <= HARDCASH_MAX_BALANCE
        or not 0 <= estimate <= HARDCASH_MAX_BALANCE
        or last_good != estimate
    ):
        return False
    transactions = meta.get("reward_transactions", [])
    has_mailgift = any(
        isinstance(transaction, dict)
        and str(transaction.get("t", "")) == "MAILGIFT"
        and (
            _as_int(transaction.get("it"), 0)
            or _as_int(transaction.get("dt"), 0)
        ) == 1
        and _as_int(transaction.get("ia"), 0) > 0
        for transaction in (
            transactions if isinstance(transactions, list) else []
        )
    )
    delivered_by_device = meta.get("hardcash_mail_last_delivered_by_device")
    has_delivery_record = (
        _as_int(meta.get("hardcash_mail_delivered_balance"), None) is not None
        or _as_int(meta.get("hardcash_mail_last_delivered_balance"), None)
        is not None
        or (
            isinstance(delivered_by_device, dict)
            and any(
                _as_int(value, None) is not None
                for value in delivered_by_device.values()
            )
        )
    )
    if not has_mailgift or not has_delivery_record:
        return False
    meta["hardcash_state"] = "known"
    meta["hardcash_schema_version"] = HARDCASH_SCHEMA_VERSION
    meta["hardcash_source"] = "legacy_server_ledger"
    meta.setdefault("hardcash_delivery_base", 3)
    if callable(log):
        log(
            f"[HARDCASH] migrated pre-schema server wallet ledger "
            f"balance={estimate}"
        )
    return True


def initialize_trusted_hardcash(saved, balance, source):
    if not isinstance(saved, dict) or source not in HARDCASH_TRUSTED_SOURCES:
        return False
    balance = _as_int(balance, None)
    if balance is None or not 0 <= balance <= HARDCASH_MAX_BALANCE:
        return False
    meta = saved.setdefault("__meta", {})
    if not isinstance(meta, dict):
        return False
    meta["hardcash_state"] = "known"
    meta["hardcash_schema_version"] = HARDCASH_SCHEMA_VERSION
    meta["hardcash_source"] = source
    meta["hardcash_baseline"] = balance
    meta["hardcash_delivery_base"] = 3
    meta["hardcash_balance_estimate"] = balance
    meta["hardcash_last_good_estimate"] = balance
    meta["hardcash_delta_total"] = 0
    return True


def _unapplied_hardcash_transaction_delta(transaction):
    if not isinstance(transaction, dict):
        return 0
    if str(transaction.get("t", "")) == "MAILGIFT":
        return 0
    item_type = _as_int(transaction.get("it"), 0) or _as_int(
        transaction.get("dt"), 0
    )
    if item_type != 1:
        return 0
    income = _as_int(transaction.get("ia"), 0)
    return income if income else -_as_int(transaction.get("da"), 0)


def _hardcash_transaction_signature(transaction):
    if not isinstance(transaction, dict):
        return None
    return (
        str(transaction.get("t", "")),
        _as_int(transaction.get("it"), 0),
        _as_int(transaction.get("dt"), 0),
        _as_int(transaction.get("ia"), 0),
        _as_int(transaction.get("da"), 0),
        str(transaction.get("o", "")),
        str(transaction.get("s", "")),
        str(transaction.get("sid", "")),
        _as_int(transaction.get("time"), 0),
    )


def recover_hardcash_sidecar_from_backups(username, saved, log):
    """Restore only a previously known wallet; never invent a legacy balance."""
    if not isinstance(saved, dict) or hardcash_balance_is_known(saved):
        return False
    save_stem = os.path.splitext(os.path.basename(guest_save_path(username)))[0]
    backup_dir = os.path.join(ROLLING_SAVE_BACKUP_DIR, save_stem)
    cache_key = os.path.normcase(os.path.abspath(backup_dir))
    current_meta = saved.get("__meta", {})
    current_transactions = (
        current_meta.get("reward_transactions", [])
        if isinstance(current_meta, dict)
        else []
    )
    current_transaction_signature = tuple(
        (
            str(transaction.get("id") or "").strip(),
            transaction.get("applied"),
            _hardcash_transaction_signature(transaction),
        )
        for transaction in (
            current_transactions if isinstance(current_transactions, list) else []
        )
        if isinstance(transaction, dict)
    )
    try:
        backup_dir_signature = os.stat(backup_dir).st_mtime_ns
    except OSError:
        backup_dir_signature = None
    recovery_cache_signature = (
        backup_dir_signature,
        current_transaction_signature,
    )
    if (
        cache_key in _HARDCASH_RECOVERY_NEGATIVE_CACHE
        and _HARDCASH_RECOVERY_NEGATIVE_CACHE[cache_key]
        == recovery_cache_signature
    ):
        return False
    marker_candidates = []
    marker_path = os.path.join(
        backup_dir,
        HARDCASH_RECOVERY_MARKER_FILE,
    )
    try:
        if os.path.isfile(marker_path):
            marker_candidates.append((
                os.stat(marker_path).st_mtime_ns,
                HARDCASH_RECOVERY_MARKER_FILE,
                marker_path,
            ))
    except OSError:
        pass
    ordinary_candidates = []
    try:
        with os.scandir(backup_dir) as entries:
            for entry in entries:
                if (
                    entry.name.casefold()
                    == HARDCASH_RECOVERY_MARKER_FILE.casefold()
                    or not entry.name.casefold().endswith(".json")
                ):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    modified_ns = entry.stat(
                        follow_symlinks=False
                    ).st_mtime_ns
                except OSError:
                    continue
                ordinary_candidates.append((
                    modified_ns,
                    entry.name,
                    entry.path,
                ))
    except OSError:
        pass
    ordinary_candidates.sort(reverse=True)
    candidates = marker_candidates + ordinary_candidates[
        :HARDCASH_RECOVERY_MAX_CANDIDATES - len(marker_candidates)
    ]
    candidates.sort(reverse=True)
    if not candidates:
        _HARDCASH_RECOVERY_NEGATIVE_CACHE[cache_key] = recovery_cache_signature
        return False

    for _modified_ns, candidate_name, candidate_path in candidates:
        try:
            with open(candidate_path, "r", encoding="utf-8-sig") as handle:
                backup = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(backup, dict) or not hardcash_balance_is_known(backup):
            continue

        backup_meta = backup.get("__meta", {})
        recovered_meta = {
            key: copy.deepcopy(backup_meta[key])
            for key in HARDCASH_SIDECAR_KEYS
            if key in backup_meta
        }
        recovered_meta["hardcash_state"] = "known"
        recovered_save = {"__meta": recovered_meta}
        recovered_balance = saved_hardcash_estimate(recovered_save)

        # An unknown-wallet delta has no proof that it happened after this
        # backup's observed balance. Recover only when every pending delta is
        # already represented by the trusted ledger; otherwise keep the client
        # authoritative until another manual observation.
        recovered_transactions = recovered_meta.setdefault(
            "reward_transactions", []
        )
        recovered_ids = recovered_meta.setdefault("reward_transaction_ids", [])
        if not isinstance(recovered_transactions, list):
            recovered_transactions = []
            recovered_meta["reward_transactions"] = recovered_transactions
        if not isinstance(recovered_ids, list):
            recovered_ids = []
            recovered_meta["reward_transaction_ids"] = recovered_ids
        recovered_id_set = {
            str(transaction_id).strip()
            for transaction_id in recovered_ids
            if str(transaction_id).strip()
        }
        recovered_signatures = {
            signature
            for signature in (
                _hardcash_transaction_signature(transaction)
                for transaction in recovered_transactions
            )
            if signature is not None
        }
        for recovered_transaction in recovered_transactions:
            if not isinstance(recovered_transaction, dict):
                continue
            transaction_id = str(recovered_transaction.get("id") or "").strip()
            if transaction_id:
                recovered_id_set.add(transaction_id)
                if transaction_id not in recovered_ids:
                    recovered_ids.append(transaction_id)
        ambiguous_delta = False
        for transaction in (
            current_transactions if isinstance(current_transactions, list) else []
        ):
            if not isinstance(transaction, dict) or transaction.get("applied") is not False:
                continue
            transaction_id = str(transaction.get("id") or "").strip()
            signature = _hardcash_transaction_signature(transaction)
            if (
                transaction_id
                and transaction_id in recovered_id_set
            ) or (
                not transaction_id
                and signature in recovered_signatures
            ):
                continue
            if _unapplied_hardcash_transaction_delta(transaction):
                ambiguous_delta = True
                break
        if ambiguous_delta:
            _HARDCASH_RECOVERY_NEGATIVE_CACHE[cache_key] = (
                recovery_cache_signature
            )
            return False

        if len(recovered_transactions) > 50:
            del recovered_transactions[:-50]
        if len(recovered_ids) > 100:
            del recovered_ids[:-100]
        baseline = _as_int(recovered_meta.get("hardcash_baseline"), 3)
        recovered_meta["hardcash_balance_estimate"] = recovered_balance
        recovered_meta["hardcash_last_good_estimate"] = recovered_balance
        recovered_meta["hardcash_delta_total"] = recovered_balance - baseline

        destination_meta = saved.setdefault("__meta", {})
        if not isinstance(destination_meta, dict):
            return False
        for key in HARDCASH_SIDECAR_KEYS:
            destination_meta.pop(key, None)
        destination_meta.update(recovered_meta)
        log(
            f"[HARDCASH] recovered trusted wallet sidecar from rolling backup "
            f"balance={recovered_balance}"
        )
        _HARDCASH_RECOVERY_NEGATIVE_CACHE.pop(cache_key, None)
        return True
    _HARDCASH_RECOVERY_NEGATIVE_CACHE[cache_key] = recovery_cache_signature
    return False


def is_hardcash_transaction(item):
    if not isinstance(item, dict):
        return False
    return (_as_int(item.get("it"), 0) or _as_int(item.get("dt"), 0)) == 1


def update_reward_transaction_meta(saved, item, session_state=None):
    meta = saved.setdefault("__meta", {})
    transactions = meta.setdefault("reward_transactions", [])
    tx_type = str(item.get("t", ""))
    item_type = _as_int(item.get("it"), 0) or _as_int(item.get("dt"), 0)
    ia = _as_int(item.get("ia"), 0)
    da = _as_int(item.get("da"), 0)
    
    amount = 0
    if ia:
        amount = ia
    elif da:
        amount = -da
        
    session_state = session_state if isinstance(session_state, dict) else {}
    session_id = str(session_state.get("session_id") or "no-session")
    sequence = _as_int(item.get("s"), 0)
    explicit_id = next(
        (
            str(item.get(key)).strip()
            for key in ("txid", "transaction_id", "rid")
            if str(item.get(key) or "").strip()
        ),
        "",
    )
    if explicit_id:
        tx_id = f"explicit:{tx_type}:{explicit_id}"
    elif _as_int(item.get("o"), 0):
        tx_id = (
            f"{tx_type}:{item_type}:{amount}:"
            f"{_as_int(item.get('o'), 0)}"
        )
    else:
        tx_id = (
            f"packet:{session_id}:{sequence}:{tx_type}:{item_type}:"
            f"{amount}:{item.get('o', 0)}"
        )
    seen = meta.setdefault("reward_transaction_ids", [])
    if tx_id in seen:
        estimate = (
            saved_hardcash_estimate(saved)
            if hardcash_balance_is_known(saved)
            else None
        )
        return 0, estimate, True
    seen.append(tx_id)
    if len(seen) > 100:
        del seen[:-100]

    if (
        not hardcash_balance_is_known(saved)
        and session_state.get("save_existed_at_login") is False
    ):
        initialize_trusted_hardcash(saved, 3, "new_account_default")

    balance_known = hardcash_balance_is_known(saved)
    estimate_before = saved_hardcash_estimate(saved) if balance_known else None
    hardcash_delta = 0
    if balance_known and amount and item_type == 1 and tx_type != "MAILGIFT":
        hardcash_delta = amount

    transactions.append({
        "t": tx_type,
        "it": _as_int(item.get("it"), 0),
        "dt": _as_int(item.get("dt"), 0),
        "ia": ia,
        "da": da,
        "o": item.get("o", 0),
        "s": item.get("s", 0),
        "sid": session_id,
        "id": tx_id,
        "applied": bool(balance_known),
        "time": int(time.time()),
    })
    if len(transactions) > 50:
        del transactions[:-50]

    if not balance_known:
        meta["hardcash_state"] = "unknown"
        return 0, None, False

    if tx_type == "MAILGIFT" and item_type == 1 and ia > 0:
        # MAILGIFT acknowledges the native mailbox award. It must not create
        # new money because c.mc already records the session delivery.
        pass
    estimate_after = min(
        HARDCASH_MAX_BALANCE,
        max(0, estimate_before + hardcash_delta),
    )
    meta["hardcash_state"] = "known"
    meta["hardcash_balance_estimate"] = estimate_after
    meta["hardcash_last_good_estimate"] = estimate_after
    meta["hardcash_delta_total"] = meta["hardcash_balance_estimate"] - _as_int(meta.get("hardcash_baseline"), 3)
    return hardcash_delta, _as_int(meta.get("hardcash_balance_estimate"), 3), False


def recompute_hardcash_from_transactions(meta):
    total = _as_int(meta.get("hardcash_baseline"), 3)
    for item in meta.get("reward_transactions", []):
        if not isinstance(item, dict):
            continue
        if item.get("applied") is False:
            continue
        tx_type = str(item.get("t", ""))
        if tx_type == "MAILGIFT":
            continue
        item_type = _as_int(item.get("it"), 0) or _as_int(item.get("dt"), 0)
        ia = _as_int(item.get("ia"), 0)
        da = _as_int(item.get("da"), 0)
        if item_type == 1:
            if ia:
                total += ia
            elif da:
                total -= da
    return min(HARDCASH_MAX_BALANCE, max(0, total))


def saved_hardcash_estimate(saved):
    # Unknown legacy wallets are client-owned until explicitly initialized.
    # Reading them must not manufacture or persist a default balance.
    if not hardcash_balance_is_known(saved):
        return None
    meta = saved.get("__meta", {})
    transactions = meta.get("reward_transactions", [])
    if isinstance(transactions, list) and transactions:
        recomputed = recompute_hardcash_from_transactions(meta)
        stored = meta.get("hardcash_balance_estimate")
        if stored is None:
            estimate = max(
                recomputed,
                _as_int(meta.get("hardcash_last_good_estimate"), 0),
            )
        else:
            # Once we have an explicit server-side balance estimate, it is
            # authoritative. Purchases legitimately make the value go down.
            estimate = min(
                HARDCASH_MAX_BALANCE,
                max(0, _as_int(stored, 3)),
            )
        if meta.get("hardcash_balance_estimate") != estimate:
            meta["hardcash_balance_estimate"] = estimate
            meta["hardcash_delta_total"] = estimate - _as_int(meta.get("hardcash_baseline"), 3)
        meta["hardcash_last_good_estimate"] = estimate
        return estimate
    estimate = meta.get("hardcash_balance_estimate")
    if estimate is not None:
        estimate = min(
            HARDCASH_MAX_BALANCE,
            max(0, _as_int(estimate, 3)),
        )
        meta["hardcash_last_good_estimate"] = estimate
        return estimate
    total = _as_int(meta.get("hardcash_baseline"), 3)
    for item in meta.get("reward_transactions", []):
        if not isinstance(item, dict):
            continue
        item_type = _as_int(item.get("it"), 0) or _as_int(item.get("dt"), 0)
        tx_type = str(item.get("t", ""))
        if tx_type == "MAILGIFT":
            continue
        ia = _as_int(item.get("ia"), 0)
        da = _as_int(item.get("da"), 0)
        if item_type == 1:
            if ia:
                total += ia
            elif da:
                total -= da
    return min(HARDCASH_MAX_BALANCE, max(0, total))


def make_account_resources_fields(sequence, saved):
    # AGNetworkResourceManager::onGetAccountResourcesCallback reads `s`,
    # then optional array `r`, with items containing long `t` and long `a`.
    # ProductManager::onGetResourcesFromServer treats t=1 as hardcash and
    # copies the plain amount into its hardcash balance before refreshing HUD.
    pairs = []
    if hardcash_balance_is_known(saved):
        hardcash = saved_hardcash_estimate(saved)
        pairs.append(e_obj([
            ("t", e_long(1)),
            ("a", e_long(hardcash)),
        ]))
    res_content = saved.get("res", {}).get("c", {}) if isinstance(saved, dict) else {}
    if isinstance(res_content, dict):
        for resource_key in ("2", "4", "6", "7", "71", "72", "84", "85", "136", "141"):
            if resource_key not in res_content:
                continue
            pairs.append(e_obj([
                ("t", e_long(_as_int(resource_key, 0))),
                ("a", e_long(_as_int(res_content.get(resource_key), 0))),
            ]))
    return [
        ("s", e_long(sequence)),
        ("r", e_arr(pairs)),
    ]


def make_cgi_resource_fields(sequence, saved):
    # LoadingManager::loadResources -> ProductManager::refreshResourcesFromServer
    # -> AGNetworkResourceManager::requestGetAccountResources. The recovered
    # callback reads exactly long `s` and optional array `r` of {long t, long a}
    # resource pairs before invoking LoadingManager::onResourcesLoaded.
    return make_account_resources_fields(sequence, saved)


def make_hardcash_mail_gift(saved, session_state=None):
    # MailManager::executeMailActionCollectGift reads content fields `gt` and
    # `gv`; gt=2 calls Program::inGameHardCashModify(gv, 0x15, false, true).
    if not hardcash_balance_is_known(saved):
        return None, None
    hardcash = saved_hardcash_estimate(saved)
    if HARDCASH_DELIVERY_MODE == "direct":
        return None, hardcash
    meta = saved.get("__meta", {}) if isinstance(saved, dict) else {}
    delivery_base = (
        max(0, _as_int(meta.get("hardcash_delivery_base"), 3))
        if isinstance(meta, dict)
        else 3
    )
    session_state = session_state if isinstance(session_state, dict) else {}
    # Hardcash is not restored reliably from the normal save blocks. Each new
    # login session receives a mail gift for the saved balance, then c.mc marks
    # it delivered only for this live session so repeated mailbox opens do not
    # duplicate it. Persistent delivery markers are kept as diagnostics only and
    # must not block a future login or another linked device.
    delivered = _as_int(
        session_state.get("hardcash_mail_delivered_balance"),
        delivery_base,
    )
    delivered = max(delivered, min(hardcash, delivery_base))
    pending_hardcash = max(0, hardcash - delivered)
    if pending_hardcash <= 0:
        return None, hardcash
    return e_obj([
        ("id", e_long(HARDCASH_MAIL_GIFT_ID)),
        ("st", e_str("n")),
        ("f", e_long(1780276007)),
        ("p", e_long(1)),
        ("dp", e_long(259200)),
        ("de", e_long(259200)),
        ("c", e_obj([
            ("a", e_long(HARDCASH_MAIL_ACTION)),
            ("i", e_long(2)),
            ("img", e_str(HARDCASH_MAIL_GIFT_IMAGE)),
            ("t", e_long(HARDCASH_MAIL_CONTENT_TYPE)),
            ("v", e_long(5)),
            ("gt", e_long(2)),
            ("gv", e_long(pending_hardcash)),
        ])),
    ]), pending_hardcash


def make_mailbox_fields(sequence, saved, log, session_state=None):
    mail, hardcash = make_hardcash_mail_gift(saved, session_state)
    if mail is None:
        log("[MAILBOX] c.mg callback sent mails=0")
        return [
            ("s", e_long(sequence)),
            ("nm", e_long(0)),
            ("m", e_arr([])),
        ]
    log(
        f"[MAILBOX] c.mg hardcash gift sent "
        f"hardcash={hardcash} mail_id={HARDCASH_MAIL_GIFT_ID} "
        f"content_t={HARDCASH_MAIL_CONTENT_TYPE} action_a={HARDCASH_MAIL_ACTION}"
    )
    return [
        ("s", e_long(sequence)),
        ("nm", e_long(1)),
        ("m", e_arr([mail])),
    ]


def make_mail_consume_fields(sequence, params, saved, log, session_state=None):
    mail_id = _as_int(
        params.get("id") if isinstance(params, dict) else None,
        HARDCASH_MAIL_GIFT_ID,
    )
    if not hardcash_balance_is_known(saved):
        log(
            f"[MAILBOX] c.mc acknowledged without wallet mutation "
            f"id={mail_id}; balance is client-owned"
        )
        return [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("id", e_long(mail_id)),
        ]
    hardcash = saved_hardcash_estimate(saved)
    meta = saved.setdefault("__meta", {}) if isinstance(saved, dict) else {}
    delivery_base = (
        max(0, _as_int(meta.get("hardcash_delivery_base"), 3))
        if isinstance(meta, dict)
        else 3
    )
    session_state = session_state if isinstance(session_state, dict) else {}
    device_id = normalize_device_id(session_state.get("device_id"))
    delivered = _as_int(
        session_state.get("hardcash_mail_delivered_balance"),
        delivery_base,
    )
    delivered = max(delivered, min(hardcash, delivery_base))
    pending_hardcash = max(0, hardcash - delivered)
    log(
        f"[MAILBOX] c.mc consume hardcash payload acknowledged "
        f"id={mail_id} hardcash={pending_hardcash} content_t={HARDCASH_MAIL_CONTENT_TYPE} "
        f"action_a={HARDCASH_MAIL_ACTION}"
    )
    if pending_hardcash:
        session_state["hardcash_mail_delivered_balance"] = hardcash
        if isinstance(meta, dict):
            if device_id:
                delivered_by_device = meta.get(
                    "hardcash_mail_last_delivered_by_device"
                )
                if not isinstance(delivered_by_device, dict):
                    delivered_by_device = {}
                delivered_by_device[device_id] = hardcash
                meta["hardcash_mail_last_delivered_by_device"] = delivered_by_device
            else:
                meta["hardcash_mail_last_delivered_balance"] = hardcash
            meta["hardcash_mail_delivered_balance"] = max(
                _as_int(
                    meta.get("hardcash_mail_delivered_balance"),
                    delivery_base,
                ),
                hardcash,
            )
            meta["hardcash_balance_estimate"] = hardcash
            meta["hardcash_last_good_estimate"] = hardcash
            meta["hardcash_delta_total"] = hardcash - _as_int(
                meta.get("hardcash_baseline"),
                3,
            )
        session_state["hardcash_meta_changed"] = True
        log(
            f"[MAILBOX] c.mc marked session-delivered hardcash "
            f"pending={pending_hardcash} device_id={device_id or 'unknown'} "
            f"delivered={session_state['hardcash_mail_delivered_balance']} "
            f"estimate={hardcash}"
        )
    gift_content = e_obj([
        ("a", e_long(HARDCASH_MAIL_ACTION)),
        ("i", e_long(2)),
        ("img", e_str(HARDCASH_MAIL_GIFT_IMAGE)),
        ("t", e_long(HARDCASH_MAIL_CONTENT_TYPE)),
        ("v", e_long(5)),
        ("gt", e_long(2)),
        ("gv", e_long(pending_hardcash)),
    ])
    return [
        ("s", e_long(sequence)),
        ("ok", e_bool(True)),
        ("id", e_long(mail_id)),
        ("t", e_long(HARDCASH_MAIL_CONTENT_TYPE)),
        ("gt", e_long(2)),
        ("gv", e_long(pending_hardcash)),
        ("c", gift_content),
        ("m", e_obj([
            ("id", e_long(mail_id)),
            ("f", e_long(1780276007)),
            ("c", gift_content),
        ])),
    ]


def is_account_resources_command(command):
    lower = str(command or "").lower()
    return lower in {
        "c.gr",   # get resources
        "c.ra",   # resource account
        "c.ar",   # account resources
        "c.gar",  # get account resources
        "c.rga",  # resources get account
        "c.rgr",  # resources get resources
        "c.gi",   # resources get (direct c.gi)
    }





def make_composite_response(
    params, profile, username, options_mode, friend_mode, product_mode,
    scheduler_mode, mail_mode, replay_keys, zr_replay_shape, log, session_state=None
):
    """Build the `c.c` response envelope identified in the iOS callbacks."""
    requests = params.get("c", []) if isinstance(params, dict) else []
    results = []
    skipped = []
    persists_writes = profile in ("savegame", "write_ack_empty_read")
    session_state = session_state if isinstance(session_state, dict) else {}
    replays_reads = profile == "savegame"
    suppress_replay = profile == "write_ack_empty_read"
    replay_already_served = bool(session_state.get("served_save_read"))
    served_groups_at_request = set(served_save_groups(session_state))
    force_save_replay = bool(session_state.get("force_next_save_replay"))
    last_save_persist_monotonic = session_state.get("last_save_persist_monotonic")
    post_write_read_replay = (
        profile == "savegame"
        and replay_already_served
        and isinstance(last_save_persist_monotonic, (int, float))
        and time.monotonic() - last_save_persist_monotonic <= POST_WRITE_READ_REPLAY_SECONDS
    )
    allow_repeat_save_replay = post_write_read_replay or force_save_replay
    replay_keys = set(replay_keys or [])
    saved = load_guest_save(username, log) if persists_writes else {}
    if isinstance(saved, dict) and "server_offline_elapsed_seconds" not in session_state:
        session_state["server_offline_elapsed_seconds"] = saved_offline_elapsed_seconds(saved)
    load_battle_changed = 0
    if (
        persists_writes
        and session_state.get("save_existed_at_login") is False
        and not hardcash_balance_is_known(saved)
    ):
        initialize_trusted_hardcash(saved, 3, "new_account_default")
        load_battle_changed += 1
        log("[HARDCASH] initialized new-save balance baseline=3")
    if persists_writes and isinstance(saved.get("batl"), dict) and isinstance(saved["batl"].get("c"), dict):
        load_battle_changed += remember_battle_context(saved, saved["batl"]["c"], log, "load")
        load_battle_changed += normalize_battle_cooldown_slots(saved["batl"]["c"], log, "load")
        elapsed = _as_int(session_state.get("server_offline_elapsed_seconds"), 0)
        if (
            elapsed > 0
            and battle_cooldown_remaining_values(saved["batl"]["c"])
            and offline_elapsed_should_apply("batl", session_state)
        ):
            before = json.loads(json.dumps(saved["batl"]["c"]))
            apply_battle_offline_timers_for_replay(saved["batl"]["c"], session_state, log)
            if saved["batl"]["c"] != before:
                load_battle_changed += 1
                log(f"[TIME] load persisted battle offline elapsed={elapsed}s")
        load_battle_changed += hydrate_battle_context_from_saved(
            saved,
            saved["batl"]["c"],
            log,
            "load",
        )
    if persists_writes:
        load_battle_changed += synthesize_battle_cooldowns_from_profile(saved, log, "load")
    if load_battle_changed:
        save_guest_data(username, saved, log, session_state)
    saved_before_writes = json.loads(json.dumps(saved)) if persists_writes else {}
    request_has_profile_read = any(
        isinstance(item, dict)
        and str(item.get("co", "")) in ("c.zr", "c.or")
        and str(item.get("k", "")) == "prof"
        for item in requests
    )
    request_has_battle_read = any(
        isinstance(item, dict)
        and str(item.get("co", "")) in ("c.zr", "c.or")
        and str(item.get("k", "")) == "batl"
        for item in requests
    )
    wrote_save = False
    replayed_save_changed = False
    reset_after_replay = False
    startup_reset_on_existing_save = False
    repeated_save_replay_used = False
    meaningful_save_write = False

    if profile in ("storage_empty", "savegame", "write_ack_empty_read"):
        for item in requests:
            if not isinstance(item, dict):
                continue
            command = str(item.get("co", ""))
            sequence = int(item.get("s", 0))
            if command == "c.zr":
                # AGNetworkCommonStorage2Manager::onReadCallback:
                # required sequence `s`; optional storage table `st`.
                key = str(item.get("k", ""))
                requested_owner_id = _as_int(item.get("o", 1), 1)
                visit_save_id = (
                    choose_visit_save_id(username, requested_owner_id, session_state, log)
                    if key in VISIT_READ_KEYS and requested_owner_id > 1
                    else ""
                )
                read_saved = load_guest_save(visit_save_id, log) if visit_save_id else saved
                can_replay_key = key in replay_keys or (bool(visit_save_id) and key in VISIT_READ_KEYS)
                can_repeat_battle_read = (
                    key == "batl"
                    and isinstance(read_saved.get("batl"), dict)
                    and isinstance(read_saved.get("batl", {}).get("c"), dict)
                )
                replay_group = save_replay_group(key)
                group_replay_pending = bool(
                    replay_group and replay_group not in served_groups_at_request
                )
                content = (
                    read_saved.get(key, {}).get("c")
                    if (
                        replays_reads
                        and not suppress_replay
                        and (
                            visit_save_id
                            or not replay_already_served
                            or group_replay_pending
                            or allow_repeat_save_replay
                            or can_repeat_battle_read
                        )
                        and can_replay_key
                    )
                    else None
                )
                if content is not None:
                    replay_elapsed_seconds = None
                    if visit_save_id:
                        session_state.setdefault("visit_started_monotonic", time.monotonic())
                    elif key == "park":
                        visit_started = session_state.pop("visit_started_monotonic", None)
                        if isinstance(visit_started, (int, float)):
                            replay_elapsed_seconds = max(0, min(int(time.monotonic() - visit_started), 7 * 24 * 60 * 60))
                            if replay_elapsed_seconds > 0:
                                log(f"[VISIT] applying return-home elapsed={replay_elapsed_seconds}s to own park replay")
                            session_state.pop("visit_owner_map", None)
                    content = normalize_saved_content_for_replay(
                        key,
                        json.loads(json.dumps(content)),
                        visit_snapshot_session_state(session_state) if visit_save_id else session_state,
                        log,
                        replay_elapsed_seconds=replay_elapsed_seconds,
                    )
                    if key == "prof" and not visit_save_id:
                        sync_authoritative_tournament_anchor(
                            read_saved,
                            log,
                            f"{command} replay",
                            profile_content=content,
                        )
                        synthesize_profile_battle_cooldowns_from_batl(
                            read_saved,
                            content,
                            session_state,
                            log,
                            "replay",
                        )
                    if visit_save_id:
                        content = rewrite_visit_content_identity(
                            key,
                            content,
                            requested_owner_id,
                            visit_save_id,
                        )
                    if key == "res":
                        hardcash = (
                            saved_hardcash_estimate(read_saved)
                            if hardcash_balance_is_known(read_saved)
                            else None
                        )
                        if hardcash is not None and hardcash > 0:
                            log(
                                f"[SAVE] not injecting hardcash={hardcash} into res block; "
                                "res.1 is the save timestamp, not spendable hardcash"
                            )

                    if visit_save_id:
                        log(
                            f"[VISIT] serving {command} key={key!r} "
                            f"owner_id={requested_owner_id} source={visit_save_id!r}"
                        )
                    else:
                        session_state["served_save_read"] = True
                        if replay_group:
                            served_save_groups(session_state).add(replay_group)
                        # Only set replay monotonic on initial login replay,
                        # not on post-write re-replays (which would extend the
                        # authority window indefinitely and cause rollbacks).
                        if not replay_already_served:
                            session_state["last_save_replay_monotonic"] = time.monotonic()
                    if not visit_save_id and isinstance(saved.get(key), dict):
                        saved[key]["c"] = json.loads(json.dumps(content))
                        replayed_save_changed = True
                    if replay_already_served and not visit_save_id:
                        repeated_save_replay_used = True
                if profile == "write_ack_empty_read" and key in saved:
                    log(f"[SAVE] replay suppressed for {command} key={key!r}")
                if profile == "savegame" and key in saved and not can_replay_key:
                    log(f"[SAVE] replay gated off for {command} key={key!r}")
                if (
                    profile == "savegame"
                    and key in saved
                    and replay_already_served
                    and not group_replay_pending
                    and not allow_repeat_save_replay
                    and not can_repeat_battle_read
                ):
                    log(f"[SAVE] replay-once suppressed for {command} key={key!r}")
                if profile == "savegame" and key in saved and post_write_read_replay and content is not None:
                    log(f"[SAVE] post-write replaying {command} key={key!r}")
                if profile == "savegame" and key in saved and force_save_replay and content is not None:
                    log(f"[SAVE] reconnect replaying {command} key={key!r}")
                if profile == "savegame" and key in saved and content is not None:
                    log(f"[SAVE] replaying {command} key={key!r} shape={zr_replay_shape!r}")
                saved_version = int(read_saved.get(key, {}).get("v", 0) or 0)
                saved_owner = (
                    requested_owner_id
                    if visit_save_id
                    else int(read_saved.get(key, {}).get("o", 1) or 1)
                )
                saved_result = int(read_saved.get(key, {}).get("r", 0) or 0)
                extra_rows = []
                if (
                    profile == "savegame"
                    and BATTLE_STARTUP_BATL_STARRAY_PIGGYBACK_ENABLED
                    and key == "prof"
                    and content is not None
                    and not visit_save_id
                    and not replay_already_served
                    and isinstance(saved.get("batl"), dict)
                ):
                    batl_content = saved.get("batl", {}).get("c")
                    if isinstance(batl_content, dict):
                        batl_content = normalize_saved_content_for_replay(
                            "batl",
                            json.loads(json.dumps(batl_content)),
                            session_state,
                            log,
                        )
                        hydrate_battle_context_from_saved(
                            saved,
                            batl_content,
                            log,
                            "prof starray piggyback",
                        )
                        saved["batl"]["c"] = json.loads(json.dumps(batl_content))
                        replayed_save_changed = True
                        extra_rows.append({
                            "key": "batl",
                            "version": int(saved.get("batl", {}).get("v", 0) or 0),
                            "owner": int(saved.get("batl", {}).get("o", 1) or 1),
                            "result": int(saved.get("batl", {}).get("r", 0) or 0),
                            "content": batl_content,
                        })
                        log(
                            f"[BATTLE] piggybacked batl row into prof c.zr starray "
                            f"version={int(saved.get('batl', {}).get('v', 0) or 0)} "
                            f"cooldowns={battle_cooldown_remaining_values(batl_content)}"
                        )
                results.append(e_obj(make_commonstorage2_starray_read_fields(
                    command,
                    sequence,
                    key,
                    saved_version,
                    saved_owner,
                    saved_result,
                    content,
                    extra_rows=extra_rows,
                )))
            elif command == "c.or":
                # AGNetworkOnlineStorageManager::onReadCallback:
                # required `v` and sequence `s`; optional content `c`.
                key = str(item.get("k", ""))
                requested_owner_id = _as_int(item.get("o", 1), 1)
                visit_save_id = (
                    choose_visit_save_id(username, requested_owner_id, session_state, log)
                    if key in VISIT_READ_KEYS and requested_owner_id > 1
                    else ""
                )
                read_saved = load_guest_save(visit_save_id, log) if visit_save_id else saved
                can_replay_key = key in replay_keys or (bool(visit_save_id) and key in VISIT_READ_KEYS)
                can_repeat_battle_read = (
                    key == "batl"
                    and isinstance(read_saved.get("batl"), dict)
                    and isinstance(read_saved.get("batl", {}).get("c"), dict)
                )
                replay_group = save_replay_group(key)
                group_replay_pending = bool(
                    replay_group and replay_group not in served_groups_at_request
                )
                content = (
                    read_saved.get(key, {}).get("c")
                    if (
                        replays_reads
                        and not suppress_replay
                        and (
                            visit_save_id
                            or not replay_already_served
                            or group_replay_pending
                            or allow_repeat_save_replay
                            or can_repeat_battle_read
                        )
                        and can_replay_key
                    )
                    else None
                )
                if content is not None:
                    replay_elapsed_seconds = None
                    if visit_save_id:
                        session_state.setdefault("visit_started_monotonic", time.monotonic())
                    elif key == "park":
                        visit_started = session_state.pop("visit_started_monotonic", None)
                        if isinstance(visit_started, (int, float)):
                            replay_elapsed_seconds = max(0, min(int(time.monotonic() - visit_started), 7 * 24 * 60 * 60))
                            if replay_elapsed_seconds > 0:
                                log(f"[VISIT] applying return-home elapsed={replay_elapsed_seconds}s to own park replay")
                            session_state.pop("visit_owner_map", None)
                    content = normalize_saved_content_for_replay(
                        key,
                        json.loads(json.dumps(content)),
                        visit_snapshot_session_state(session_state) if visit_save_id else session_state,
                        log,
                        replay_elapsed_seconds=replay_elapsed_seconds,
                    )
                    if key == "prof" and not visit_save_id:
                        sync_authoritative_tournament_anchor(
                            read_saved,
                            log,
                            f"{command} replay",
                            profile_content=content,
                        )
                        synthesize_profile_battle_cooldowns_from_batl(
                            read_saved,
                            content,
                            session_state,
                            log,
                            "replay",
                        )
                    if visit_save_id:
                        content = rewrite_visit_content_identity(
                            key,
                            content,
                            requested_owner_id,
                            visit_save_id,
                        )
                    if key == "res":
                        hardcash = (
                            saved_hardcash_estimate(read_saved)
                            if hardcash_balance_is_known(read_saved)
                            else None
                        )
                        if hardcash is not None and hardcash > 0:
                            log(
                                f"[SAVE] not injecting hardcash={hardcash} into res block; "
                                "res.1 is the save timestamp, not spendable hardcash"
                            )

                    if visit_save_id:
                        log(
                            f"[VISIT] serving {command} key={key!r} "
                            f"owner_id={requested_owner_id} source={visit_save_id!r}"
                        )
                    else:
                        session_state["served_save_read"] = True
                        if replay_group:
                            served_save_groups(session_state).add(replay_group)
                        if not replay_already_served:
                            session_state["last_save_replay_monotonic"] = time.monotonic()
                    if not visit_save_id and isinstance(saved.get(key), dict):
                        saved[key]["c"] = json.loads(json.dumps(content))
                        replayed_save_changed = True
                    if replay_already_served and not visit_save_id:
                        repeated_save_replay_used = True
                if profile == "write_ack_empty_read" and key in saved:
                    log(f"[SAVE] replay suppressed for {command} key={key!r}")
                if profile == "savegame" and key in saved and not can_replay_key:
                    log(f"[SAVE] replay gated off for {command} key={key!r}")
                if (
                    profile == "savegame"
                    and key in saved
                    and replay_already_served
                    and not group_replay_pending
                    and not allow_repeat_save_replay
                    and not can_repeat_battle_read
                ):
                    log(f"[SAVE] replay-once suppressed for {command} key={key!r}")
                saved_version = int(read_saved.get(key, {}).get("v", 0) or 0)
                if profile == "savegame" and key in saved and post_write_read_replay and content is not None:
                    log(f"[SAVE] post-write replaying {command} key={key!r}")
                if profile == "savegame" and key in saved and force_save_replay and content is not None:
                    log(f"[SAVE] reconnect replaying {command} key={key!r}")
                if profile == "savegame" and key in saved and content is not None:
                    log(f"[SAVE] replaying {command} key={key!r} version={saved_version}")
                if key == "batl":
                    if isinstance(content, dict):
                        if hydrate_battle_context_from_saved(saved, content, log, "c.or replay"):
                            if not visit_save_id and isinstance(saved.get("batl"), dict):
                                saved["batl"]["c"] = json.loads(json.dumps(content))
                                replayed_save_changed = True
                    results.append(e_obj(make_battle_online_storage_read_fields(
                        command,
                        sequence,
                        saved_version,
                        content,
                    )))
                    continue
                online_read_fields = [
                    ("co", e_str(command)),
                    ("v", e_long(saved_version)),
                    ("s", e_long(sequence)),
                ]
                if isinstance(content, dict):
                    online_read_fields.append(("c", e_storage_content(key, content)))
                elif key == "_cb_refer":
                    log("[REFER] c.or _cb_refer missing locally; omitting empty content object")
                else:
                    online_read_fields.append(("c", e_obj([])))
                results.append(e_obj(online_read_fields))
            elif command == "c.rg" and friend_mode != "skip":
                # The iOS symbols expose both single-user and multi-user friend
                # callbacks. The list form accepts empty arrays without fake data.
                if friend_mode == "random_users_saves":
                    friend_fields = make_random_park_friend_fields(command, sequence, username, friend_mode, log)
                elif friend_mode == "random_user_stub":
                    friend_fields = make_random_park_friend_fields(command, sequence, username, friend_mode, log)
                else:
                    friend_fields = [
                        ("co", e_str(command)),
                        ("s", e_long(sequence)),
                        ("id", e_arr([])),
                        ("fa", e_arr([])),
                    ]
                results.append(e_obj(friend_fields))
                log(f"[FRIENDS] c.rg callback sent mode={friend_mode!r}")
            elif command == "c.grs":
                battle_fields = make_game_random_search_fields(
                    item,
                    log,
                    current_save_id=username,
                    session_state=session_state,
                    current_saved=saved,
                )
                battle_fields = [(key, value) for key, value in battle_fields if key != "s"]
                results.append(e_obj(
                    [("co", e_str(command)), ("s", e_long(sequence))]
                    + battle_fields
                ))
            elif command == "c.t":
                log_tournament_timer_state(log, "c.t")
                results.append(e_obj([
                    ("co", e_str(command)),
                    ("s", e_long(sequence)),
                    ("r", e_int(0)),
                    ("ok", e_bool(True)),
                    ("t", e_long(int(time.time() * 1000))),
                    ("v", e_int(0)),
                    ("c", e_int(0)),
                    ("n", e_int(0)),
                    ("vs", e_arr([])),
                    *empty_notification_fields(),
                ]))
                log("[TIME] c.t game-time callback sent")
            elif command == "c.rp" and product_mode != "skip":
                if item.get("st") == IAP_GPLAY_STORE_NAME:
                    fields, product_count = make_iap_store_catalog_fields(
                        item,
                        load_cached_online_options(log),
                    )
                    results.append(e_obj([("co", e_str(command)), *fields]))
                    log(f"[PRODUCTS] c.rp GPLAY catalog callback sent products={product_count}")
                else:
                    # AGProductsDataTransaction::process() first reads long `np`,
                    # then accesses array `p` exactly `np` times. A zero-product
                    # reply therefore completes loading without fabricated IAPs.
                    results.append(e_obj([
                        ("co", e_str(command)),
                        ("s", e_long(sequence)),
                        ("np", e_long(0)),
                        ("p", e_arr([])),
                    ]))
                    log("[PRODUCTS] c.rp callback sent products=0")
            elif command == "c.pb":
                # AGNetworkOnlineOptionsManager::batchReadCallback reads the
                # `vs` array, whose objects contain string fields `k` and `v`.
                requested = item.get("k", [])
                requested = requested if isinstance(requested, list) else []
                requested = [str(key) for key in requested]
                option_items = []
                missing = []
                battle_defaults = []
                live_refresh = []
                resolved_values = {}
                if options_mode == "full":
                    cached_values = load_cached_online_options(log)
                    response_keys = list(requested)
                    for key in ONLINE_OPTIONS_LIVE_REFRESH_KEYS:
                        if key not in response_keys:
                            response_keys.append(key)
                            live_refresh.append(key)
                    for key in response_keys:
                        value, source = get_online_option_value(key, cached_values)
                        if source != "missing":
                            resolved_values[key] = str(value)
                            option_items.append(e_obj([
                                ("k", e_str(key)),
                                ("v", e_str(value)),
                            ]))
                            if source == "battle-default":
                                battle_defaults.append(key)
                        else:
                            missing.append(key)
                log_tournament_timer_state(log, "c.pb")
                results.append(e_obj([
                    ("co", e_str(command)),
                    ("s", e_long(sequence)),
                    ("vs", e_arr(option_items)),
                ]))
                log(
                    f"[OPTIONS] c.pb callback sent mode={options_mode!r} "
                    f"requested={len(requested)} returned={len(option_items)} "
                    f"live_refresh={live_refresh!r} "
                    f"addon_dino={resolved_values.get('ADDON_DINO_ID')!r} "
                    f"addon_limit={resolved_values.get('ADDON_DINO_LIMIT')!r} "
                    f"cp_ltd={resolved_values.get('CP_LTD_OFFER_ENABLE')!r} "
                    f"battle_defaults={battle_defaults!r} missing={missing!r}"
                )
            elif command == "c.sg" and scheduler_mode != "skip":
                # AGNetworkSchedulerManager::getEventsCount reads long `ne`.
                results.append(e_obj([
                    ("co", e_str(command)),
                    ("s", e_long(sequence)),
                    ("ne", e_long(scheduler_event_count(log, "composite c.sg"))),
                ]))
                log("[SCHEDULER] c.sg callback sent")
            elif command == "c.gi":
                if (
                    isinstance(session_state, dict)
                    and hardcash_balance_is_known(saved)
                ):
                    session_state["hardcash_late_sync_requested"] = True
                results.append(e_obj(
                    [("co", e_str(command))] + make_cgi_resource_fields(sequence, saved)
                ))
                log(
                    f"[RESOURCES] c.gi exact account-resource callback sent "
                    f"hardcash={saved_hardcash_estimate(saved) if hardcash_balance_is_known(saved) else 'client-owned'}"
                )
            elif command == "c.se":
                # SCHEDULER_ADD_EVENT is observed with `k` and `t`. No
                # response payload consumer was recovered, so associate and
                # acknowledge the operation without inventing event data.
                results.append(e_obj([
                    ("co", e_str(command)),
                    ("s", e_long(sequence)),
                    ("ok", e_bool(True)),
                ]))
                log("[SCHEDULER] c.se add-event acknowledged")
            elif command == "c.mg" and mail_mode != "skip":
                # onGetMailCallback checks for `m`; AGNetworkMailList then
                # reads `nm` and parses exactly that many items from `m`.
                results.append(e_obj(
                    [("co", e_str(command))] + make_mailbox_fields(sequence, saved, log, session_state)
                ))
            elif command in ("c.mc", "c.md", "c.mr", "c.mf") and mail_mode != "skip":
                if command == "c.mc":
                    if (
                        isinstance(session_state, dict)
                        and hardcash_balance_is_known(saved)
                    ):
                        session_state["hardcash_late_sync_requested"] = True
                    results.append(e_obj(
                        [("co", e_str(command))] + make_mail_consume_fields(sequence, item, saved, log, session_state)
                    ))
                else:
                    results.append(e_obj([
                        ("co", e_str(command)),
                        ("s", e_long(sequence)),
                        ("ok", e_bool(True)),
                    ]))
                    log(f"[MAILBOX] {command} consume/delete/friend acknowledged")
            elif is_account_resources_command(command):
                results.append(e_obj(
                    [("co", e_str(command))] + make_account_resources_fields(sequence, saved)
                ))
                log(
                    f"[RESOURCES] account resources callback sent "
                    f"hardcash={saved_hardcash_estimate(saved) if hardcash_balance_is_known(saved) else 'client-owned'}"
                )
            elif persists_writes and command == "c.rt":
                # Resource/reward transaction. The client can submit this
                # inside the same composite as save writes; acknowledge it and
                # keep a sidecar trace because hard cash lives outside `res`.
                hardcash_delta, hardcash_estimate, duplicate_reward = update_reward_transaction_meta(
                    saved,
                    item,
                    session_state,
                )
                if isinstance(session_state, dict) and hardcash_balance_is_known(saved):
                    session_state["hardcash_late_sync_requested"] = True
                if (
                    not duplicate_reward
                    and (
                        is_hardcash_transaction(item)
                        or item.get("t") == "MAILGIFT"
                    )
                ):
                    meaningful_save_write = True
                    wrote_save = True
                results.append(e_obj([
                    ("co", e_str(command)),
                    ("s", e_long(sequence)),
                    ("ok", e_bool(True)),
                ]))
                log(
                    f"[REWARD] c.rt acknowledged type={item.get('t')!r} "
                    f"item={item.get('it')!r} amount={item.get('ia')!r} "
                    f"hardcash_delta={hardcash_delta} estimate={hardcash_estimate} "
                    f"duplicate={duplicate_reward}"
                )
            elif persists_writes and command == "c.zw":
                # Confirmed by AGNetworkCommonStorage2Manager::onWriteCallback:
                # only `ok` and sequence `s` are read from the reply.
                key = str(item.get("k", ""))
                requested_owner_id = _as_int(item.get("o", 1), 1)
                if requested_owner_id > 1 and key in VISIT_READ_KEYS:
                    log(
                        f"[VISIT] suppressed write {command} key={key!r} "
                        f"owner_id={requested_owner_id}; visiting is read-only"
                    )
                    results.append(e_obj([
                        ("co", e_str(command)),
                        ("ok", e_bool(True)),
                        ("s", e_long(sequence)),
                    ]))
                    continue
                content = normalize_saved_content_for_replay(
                    key,
                    item.get("c", {}),
                    session_state,
                    log,
                    for_replay=False,
                )
                if key == "res":
                    hardcash = (
                        saved_hardcash_estimate(saved)
                        if hardcash_balance_is_known(saved)
                        else "client-owned"
                    )
                    log(
                        f"[SAVE] not injecting hardcash={hardcash} into res write block; "
                        "res.1 is the save timestamp, not spendable hardcash"
                    )
                if is_meaningful_save_content(key, content):
                    meaningful_save_write = True
                if (
                    key == "prof"
                    and isinstance(content, dict)
                    and "Error Loading Savefile" in str(content.get("93", ""))
                ):
                    if session_state.get("served_save_read"):
                        reset_after_replay = True
                    elif saved_before_writes:
                        startup_reset_on_existing_save = True
                incoming_entry = {
                    "kind": command,
                    "c": content,
                    "v": item.get("v", 0),
                    "o": item.get("o", 1),
                    "r": item.get("r", 0),
                }
                guarded_entry = _guard_direct_battle_write(saved, key, incoming_entry, log)
                preserve_group_write = should_preserve_park_group_write(
                    saved_before_writes,
                    key,
                    incoming_entry,
                    session_state,
                )
                if preserve_group_write:
                    previous_entry = saved_before_writes[key]
                    guarded_entry = json.loads(json.dumps(previous_entry))
                    log(
                        f"[SAVE-GROUP] preserved protected {save_replay_group(key)!r} "
                        f"key={key!r} server_version={previous_entry.get('v', 0)!r} "
                        f"client_version={item.get('v', 0)!r}; "
                        "bootstrap/regressive write acked and server replay requested"
                    )
                preserved_profile_battle_write = (
                    key == "prof"
                    and isinstance(guarded_entry, dict)
                    and isinstance(guarded_entry.get("c"), dict)
                    and guarded_entry.get("c") != content
                    and _profile_has_active_battle_cooldowns(guarded_entry.get("c"))
                )
                saved[key] = guarded_entry
                if key == "prof":
                    synthesize_battle_cooldowns_from_profile(saved, log, "direct write")
                if (
                    key in ("prof", "batl")
                    and hardcash_balance_is_known(saved)
                ):
                    session_state["hardcash_late_sync_requested"] = True
                session_state["saw_save_write"] = True
                session_state["force_next_save_replay"] = True
                wrote_save = True
                try:
                    payload_size = len(json.dumps(content, ensure_ascii=True, sort_keys=True))
                except Exception:
                    payload_size = -1
                log(
                    f"[SAVE] ack {command} key={key!r} seq={sequence} "
                    f"version={item.get('v')!r} bytes={payload_size}"
                )
                if preserved_profile_battle_write:
                    saved_version = int(saved.get("prof", {}).get("v", item.get("v", 0)) or 0)
                    saved_owner = int(saved.get("prof", {}).get("o", item.get("o", 1)) or 1)
                    saved_result = int(saved.get("prof", {}).get("r", item.get("r", 0)) or 0)
                    saved_content = json.loads(json.dumps(saved["prof"]["c"]))
                    results.append(e_obj(make_profile_common_storage_write_ack_fields(
                        command,
                        sequence,
                        saved_version,
                        saved_owner,
                        saved_result,
                        saved_content,
                    )))
                    log(
                        f"[BATTLE] corrected preserved profile battle write in same composite "
                        f"seq={sequence} version={saved_version} "
                        f"remaining={_profile_battle_cooldown_remaining_values(saved_content)}"
                    )
                    continue
                results.append(e_obj([
                    ("co", e_str(command)),
                    ("ok", e_bool(True)),
                    ("s", e_long(sequence)),
                ]))
            elif persists_writes and command == "c.ow":
                # Confirmed by AGNetworkOnlineStorageManager::onWriteCallback:
                # only `ok` and sequence `s` are read from the reply.
                key = str(item.get("k", ""))
                requested_owner_id = _as_int(item.get("o", 1), 1)
                if requested_owner_id > 1 and key in VISIT_READ_KEYS:
                    log(
                        f"[VISIT] suppressed write {command} key={key!r} "
                        f"owner_id={requested_owner_id}; visiting is read-only"
                    )
                    results.append(e_obj([
                        ("co", e_str(command)),
                        ("ok", e_bool(True)),
                        ("s", e_long(sequence)),
                    ]))
                    continue
                content = normalize_saved_content_for_replay(
                    key,
                    item.get("c", {}),
                    session_state,
                    log,
                    for_replay=False,
                )
                if is_meaningful_save_content(key, content):
                    meaningful_save_write = True
                incoming_entry = {
                    "kind": command,
                    "c": content,
                    "v": item.get("v", 0),
                    "o": item.get("o", 1),
                    "r": item.get("r", 0),
                }
                previous_batl_version = (
                    int(saved.get("batl", {}).get("v", 0) or 0)
                    if key == "batl" and isinstance(saved.get("batl"), dict)
                    else 0
                )
                guarded_entry = _guard_direct_battle_write(saved, key, incoming_entry, log)
                preserve_group_write = should_preserve_park_group_write(
                    saved_before_writes,
                    key,
                    incoming_entry,
                    session_state,
                )
                if preserve_group_write:
                    previous_entry = saved_before_writes[key]
                    guarded_entry = json.loads(json.dumps(previous_entry))
                    log(
                        f"[SAVE-GROUP] preserved protected {save_replay_group(key)!r} "
                        f"key={key!r} server_version={previous_entry.get('v', 0)!r} "
                        f"client_version={item.get('v', 0)!r}; "
                        "bootstrap/regressive write acked and server replay requested"
                    )
                preserved_batl_write = (
                    key == "batl"
                    and isinstance(guarded_entry, dict)
                    and isinstance(guarded_entry.get("c"), dict)
                    and guarded_entry.get("c") != content
                    and isinstance(guarded_entry.get("c", {}).get("104"), list)
                )
                if preserved_batl_write:
                    guarded_version = int(guarded_entry.get("v", 0) or 0)
                    if previous_batl_version > guarded_version:
                        guarded_entry = json.loads(json.dumps(guarded_entry))
                        guarded_entry["v"] = previous_batl_version
                        log(
                            f"[BATTLE] direct write preserved batl version "
                            f"{guarded_version}->{previous_batl_version}"
                        )
                saved[key] = guarded_entry
                if key == "prof":
                    synthesize_battle_cooldowns_from_profile(saved, log, "direct write")
                if (
                    key in ("prof", "batl")
                    and hardcash_balance_is_known(saved)
                ):
                    session_state["hardcash_late_sync_requested"] = True
                session_state["saw_save_write"] = True
                session_state["force_next_save_replay"] = True
                wrote_save = True
                try:
                    payload_size = len(json.dumps(content, ensure_ascii=True, sort_keys=True))
                except Exception:
                    payload_size = -1
                log(
                    f"[SAVE] ack {command} key={key!r} seq={sequence} "
                    f"version={item.get('v')!r} bytes={payload_size}"
                )
                ack_fields = [
                    ("co", e_str(command)),
                    ("ok", e_bool(True)),
                    ("s", e_long(sequence)),
                ]
                if preserved_batl_write:
                    saved_version = int(saved.get("batl", {}).get("v", 0) or 0)
                    hydrate_battle_context_from_saved(
                        saved,
                        saved["batl"]["c"],
                        log,
                        "preserved batl write",
                    )
                    saved_content = json.loads(json.dumps(saved["batl"]["c"]))
                    results.append(e_obj(make_battle_online_storage_write_ack_fields(
                        command,
                        sequence,
                        saved_version,
                        saved_content,
                    )))
                    log(
                        f"[BATTLE] corrected preserved batl write in same composite "
                        f"seq={sequence} "
                        f"version={saved_version} cooldowns={battle_cooldown_remaining_values(saved_content)}"
                    )
                    continue
                results.append(e_obj(ack_fields))
            elif command.lower() in BATTLE_TOURNAMENT_DIRECT_NO_REPLY_COMMANDS:
                results.append(e_obj([
                    ("co", e_str(command)),
                    ("s", e_long(sequence)),
                    ("ok", e_bool(True)),
                ]))
                log(
                    f"[BATTLE] composite battle/tournament command "
                    f"{command!r} acknowledged seq={sequence}"
                )
            else:
                skipped.append(command)
    else:
        skipped = [
            str(item.get("co", ""))
            for item in requests
            if isinstance(item, dict)
        ]

    if session_state.pop("hardcash_meta_changed", False) and not wrote_save:
        log("[SAVE] persisted hardcash mail delivery metadata")
        save_guest_data(username, saved, log, session_state)
        session_state["last_save_persist_monotonic"] = time.monotonic()

    if replayed_save_changed and not wrote_save:
        log("[SAVE] persisted server-advanced replay content")
        save_guest_data(username, saved, log, session_state)
        session_state["last_save_persist_monotonic"] = time.monotonic()

    if wrote_save:
        log_tournament_save_diff(saved_before_writes, saved, log)
        preserve, previous_size, current_size = should_preserve_existing_save(saved_before_writes, saved)
        server_save_wins_preread = (
            session_state.get("save_existed_at_login")
            and save_has_main_park(saved_before_writes)
            and not session_state.get("served_save_read")
        )
        last_replay_monotonic = session_state.get("last_save_replay_monotonic")
        server_save_wins_postreplay = (
            session_state.get("save_existed_at_login")
            and session_state.get("served_save_read")
            and isinstance(last_replay_monotonic, (int, float))
            and time.monotonic() - last_replay_monotonic <= SERVER_AUTHORITY_AFTER_REPLAY_SECONDS
        )
        if server_save_wins_preread:
            if meaningful_save_write:
                merged = merge_postreplay_client_progress(
                    saved_before_writes,
                    saved,
                    log,
                    allow_client_timer_resets=False,
                )
                log(
                    "[SAVE] existing server save has not been replayed yet; "
                    "pre-read client write merged and persisted"
                )
                save_guest_data(username, merged, log, session_state)
                session_state["last_save_persist_monotonic"] = time.monotonic()
            else:
                log(
                    "[SAVE] existing server save has not been replayed yet; "
                    "pre-read client write acked but not persisted"
                )
        elif server_save_wins_postreplay:
            elapsed = time.monotonic() - last_replay_monotonic
            allow_client_timer_resets = elapsed >= POST_REPLAY_CLIENT_RESET_SECONDS
            merged = merge_postreplay_client_progress(
                saved_before_writes,
                saved,
                log,
                allow_client_timer_resets=allow_client_timer_resets,
            )
            log(
                "[SAVE] existing server save was just replayed; "
                f"post-replay client write merged and persisted "
                f"elapsed={elapsed:.1f}s window={SERVER_AUTHORITY_AFTER_REPLAY_SECONDS:.1f}s "
                f"client_resets={'yes' if allow_client_timer_resets else 'no'}"
            )
            save_guest_data(username, merged, log, session_state)
            session_state["last_save_persist_monotonic"] = time.monotonic()
            # End the authority window after the first successful merge.
            # The client is now in sync; further merges would only cause rollbacks.
            session_state["last_save_replay_monotonic"] = None
        elif startup_reset_on_existing_save and not meaningful_save_write:
            log(
                "[SAVE] client sent startup fallback profile while an existing "
                "save is present; write acked but not persisted"
            )
        elif reset_after_replay and not meaningful_save_write:
            log(
                "[SAVE] client created a fallback profile after replay; "
                "write acked but not persisted"
            )
        elif meaningful_save_write:
            if startup_reset_on_existing_save or reset_after_replay:
                log("[SAVE] fallback-marked write contains progress; persisting")
            save_guest_data(username, saved, log, session_state)
            session_state["last_save_persist_monotonic"] = time.monotonic()
        elif session_state.get("preserve_richer_save", True):
            if preserve:
                log(
                    f"[SAVE] preserving richer existing save; write acked but not persisted "
                    f"old_bytes={previous_size} new_bytes={current_size}"
                )
            else:
                save_guest_data(username, saved, log, session_state)
                session_state["last_save_persist_monotonic"] = time.monotonic()
        else:
            save_guest_data(username, saved, log, session_state)
            session_state["last_save_persist_monotonic"] = time.monotonic()

    if wrote_save:
        login_username = session_state.get("login_username")
        if login_username and login_username != username:
            guest_save = load_guest_save(login_username, lambda m: None)
            pass

    if repeated_save_replay_used and force_save_replay:
        session_state["force_next_save_replay"] = False
        log("[RECONNECT] forced save replay consumed")

    if (
        profile == "savegame"
        and BATTLE_SYNTHETIC_WARMUP_ENABLED
        and not suppress_replay
        and not replay_already_served
        and request_has_profile_read
        and not request_has_battle_read
        and isinstance(saved.get("batl"), dict)
    ):
        batl_content = saved.get("batl", {}).get("c")
        if isinstance(batl_content, dict):
            synthetic_sequence = max(
                [
                    _as_int(item.get("s"), 0)
                    for item in requests
                    if isinstance(item, dict)
                ]
                or [0]
            ) + 1000
            content = normalize_saved_content_for_replay(
                "batl",
                json.loads(json.dumps(batl_content)),
                session_state,
                log,
            )
            hydrate_battle_context_from_saved(
                saved,
                content,
                log,
                "startup batl warm-up",
            )
            saved["batl"]["c"] = json.loads(json.dumps(content))
            replayed_save_changed = True
            saved_version = int(saved.get("batl", {}).get("v", 0) or 0)
            results.append(e_obj(make_battle_online_storage_read_fields(
                "c.or",
                synthetic_sequence,
                saved_version,
                content,
            )))
            log(
                f"[BATTLE] injected startup batl warm-up "
                f"seq={synthetic_sequence} version={saved_version} "
                f"cooldowns={battle_cooldown_remaining_values(content)}"
            )

    log(
        f"[SFS] c.c composite profile={profile!r} "
        f"results={len(results)} skipped={skipped!r}"
    )
    sequence = int(params.get("s", 0)) if isinstance(params, dict) else 0
    return make_ext_response("c.c", [("s", e_long(sequence)), ("c", e_arr(results))])


def make_jw_cmd_response(cmd, fields):
    return build_packet(1, 1, [
        ("c", e_str(cmd or "unknown")),
        ("p", e_obj(fields)),
    ])


def make_sfs_ping_response():
    return build_packet(0, 29, [])


def make_bootstrap_fields(username, zone, sequence=0):
    return [
        ("s", e_long(sequence)),
        ("ok", e_bool(True)),
        ("success", e_bool(True)),
        ("playerId", e_str("1")),
        ("name", e_str(username)),
        ("parkname", e_str("My Park")),
        ("region", e_str("US")),
        ("platform", e_str("android")),
        ("zone", e_str(zone)),
    ]


def make_savegame_stub(username, zone, sequence=0, saved=None):
    saved = saved if isinstance(saved, dict) else {}
    res_content = (
        saved.get("res", {}).get("c", {})
        if isinstance(saved.get("res"), dict)
        else {}
    )
    include_hardcash = not bool(saved) or hardcash_balance_is_known(saved)
    resource_fields = [
        ("Softcash", e_int(_as_int(res_content.get("2"), 1000))),
        ("Crops", e_int(_as_int(res_content.get("4"), 0))),
        ("FoodPortCrops", e_int(0)),
        ("FoodPortMeat", e_int(0)),
        ("BattlePoint", e_int(0)),
    ]
    if include_hardcash:
        hardcash = (
            saved_hardcash_estimate(saved)
            if hardcash_balance_is_known(saved)
            else 3
        )
        resource_fields[1:1] = [
            ("Hardcash", e_int(hardcash)),
            ("HardCash", e_int(hardcash)),
        ]
    resources = e_obj(resource_fields)
    return [
        ("s", e_long(sequence)),
        ("ok", e_bool(True)),
        ("success", e_bool(True)),
        ("Account Id", e_str(username)),
        ("Account ID", e_str(username)),
        ("Login method", e_str("Guest")),
        ("Level", e_int(saved_player_level(saved))),
        ("Game Mode", e_str("MainGame")),
        ("Park Type", e_str("Surface")),
        ("parkname", e_str("My Park")),
        ("zone", e_str(zone)),
        ("Resources", resources),
        ("Buildings", e_arr([])),
        ("Decorations", e_arr([])),
        ("Props", e_arr([])),
        ("Roads", e_arr([])),
        ("Expansions", e_arr([])),
        ("Parcels", e_arr([])),
        ("Gateway", e_arr([])),
        ("Dinosaurs", e_arr([])),
        ("Carnivorous", e_arr([])),
        ("Herbivorous", e_arr([])),
    ]


def handle_extension_cmd(
    cmd, params, username, zone, composite_profile,
    options_mode, friend_mode, product_mode, scheduler_mode, mail_mode,
    game_services_mode, replay_keys, zr_replay_shape, log, session_state=None
):
    lower = (cmd or "").lower()
    sequence = int(params.get("s", 0)) if isinstance(params, dict) else 0
    log(f"[SFS] EXT cmd={cmd!r} params={compact_json(params)}")

    if lower == "c.c":
        return make_composite_response(
            params, composite_profile, username,
            options_mode, friend_mode, product_mode, scheduler_mode, mail_mode,
            replay_keys, zr_replay_shape, log, session_state
        )

    if lower == "c.t":
        log_tournament_timer_state(log, "direct c.t")
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("r", e_int(0)),
            ("ok", e_bool(True)),
            ("t", e_long(int(time.time() * 1000))),
            ("v", e_int(0)),
            ("c", e_int(0)),
            ("n", e_int(0)),
            ("vs", e_arr([])),
            *empty_notification_fields(),
        ])

    if lower in BATTLE_TOURNAMENT_DIRECT_NO_REPLY_COMMANDS:
        log(f"[BATTLE] direct battle/tournament command {cmd!r} ignored with donor-style no-reply")
        return None

    if lower == "c.gs":
        # Symbol table identifies c.gs as NETWORK_KEEP_ALIVE.
        return make_ext_response(cmd, [
            ("s", e_long(int(params.get("s", 0)) if isinstance(params, dict) else 0)),
            ("ok", e_bool(True)),
        ])

    if lower == "c.gt":
        # NETWORK_GET_TIME. Keep this reply deliberately small: the generic
        # fallback adds unrelated fields and the client appears to parse this
        # callback as a strict time object during long sessions.
        log_tournament_timer_state(log, "direct c.gt")
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("t", e_long(current_network_time())),
        ])

    if lower == "c.gi":
        if HARDCASH_DIRECT_CGI_DELAY_SECONDS > 0:
            log(
                f"[RESOURCES] delaying direct c.gi response "
                f"{HARDCASH_DIRECT_CGI_DELAY_SECONDS:.1f}s to avoid pre-HUD overwrite"
            )
            time.sleep(HARDCASH_DIRECT_CGI_DELAY_SECONDS)
        saved = load_guest_save(username, log)
        hardcash = (
            saved_hardcash_estimate(saved)
            if hardcash_balance_is_known(saved)
            else None
        )
        if isinstance(session_state, dict) and hardcash is not None:
            session_state["hardcash_late_sync_requested"] = True
        log(f"[RESOURCES] direct c.gi exact account-resource callback sent hardcash={hardcash}")
        return make_ext_response(
            cmd,
            make_cgi_resource_fields(
                int(params.get("s", 0)) if isinstance(params, dict) else 0,
                saved,
            ),
        )

    if lower == "c.rt":
        saved = load_guest_save(username, log)
        hardcash_delta, hardcash_estimate, duplicate_reward = update_reward_transaction_meta(
            saved,
            params,
            session_state,
        )
        if (
            not duplicate_reward
            and (
                is_hardcash_transaction(params)
                or params.get("t") == "MAILGIFT"
            )
        ):
            save_guest_data(username, saved, log, session_state)
        if isinstance(session_state, dict) and hardcash_balance_is_known(saved):
            session_state["hardcash_late_sync_requested"] = True
        log(
            f"[REWARD] direct c.rt acknowledged type={params.get('t')!r} "
            f"item={params.get('it')!r} amount={params.get('ia')!r} "
            f"hardcash_delta={hardcash_delta} estimate={hardcash_estimate} "
            f"duplicate={duplicate_reward}"
        )
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
        ])

    if lower == "c.lv":
        return make_jw_cmd_response(cmd, [
            ("ok", e_bool(True)),
            ("uu", e_bool(False)),
            ("re", e_str("O")),
            ("nt", e_int(0)),
            ("s", e_str("")),
        ])

    if lower == "c.vg" and game_services_mode == "generic":
        log("[GPLAY] c.vg callback sent mode='generic_ack'")
        return make_ext_response(cmd, make_direct_generic_fields(params))

    if lower == "c.vg" and game_services_mode != "generic":
        sequence = int(params.get("s", 0)) if isinstance(params, dict) else 0
        if game_services_mode == "reject":
            log("[GPLAY] c.vg callback sent mode='reject' providers=0")
            return make_ext_response(cmd, [
                ("s", e_long(sequence)),
                ("ok", e_bool(False)),
                ("as", e_arr([])),
            ])

        if game_services_mode == "linked_echo":
            # The client requested validation of GPLAY. Echo it as accepted
            # to test whether an empty provider list initiates the Android
            # Google Play sign-in/resume sequence seen in v42/v43.
            if isinstance(session_state, dict):
                session_state["force_next_save_replay"] = True
                log("[RECONNECT] c.vg accepted; next save read will replay saved profile")
            log("[GPLAY] c.vg callback sent mode='linked_echo' providers=['GPLAY']")
            return make_ext_response(cmd, [
                ("s", e_long(sequence)),
                ("ok", e_bool(True)),
                ("as", e_arr([e_str("GPLAY")])),
            ])

        # The client reached this command only after the DOB step, with
        # `as=["GPLAY"]`. Avoid claiming a linked Google Play account while
        # testing the old, re-signed APK whose OAuth identity is no longer valid.
        if isinstance(session_state, dict):
            session_state["force_next_save_replay"] = True
            log("[RECONNECT] c.vg accepted; next save read will replay saved profile")
        log("[GPLAY] c.vg callback sent mode='unlinked' providers=0")
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("as", e_arr([])),
        ])

    if lower == "w.oo":
        return make_jw_cmd_response(cmd, [
            ("nv", e_bool(False)),
            ("vs", e_int(0)),
            ("v", e_str('{"version":1,"segments":[{"id":"Segment_Original","name":"A","weight":100,"active":true}]}')),
            ("ok", e_bool(True)),
            ("uu", e_bool(False)),
            ("re", e_str("O")),
            ("nt", e_int(0)),
            ("s", e_str("")),
        ])

    if lower == "c.sg" and scheduler_mode != "skip":
        return make_ext_response(cmd, [
            ("s", e_long(int(params.get("s", 0)) if isinstance(params, dict) else 0)),
            ("ne", e_long(scheduler_event_count(log, "direct c.sg"))),
        ])

    if lower == "c.se":
        log("[SCHEDULER] c.se add-event acknowledged")
        return make_ext_response(cmd, [
            ("s", e_long(int(params.get("s", 0)) if isinstance(params, dict) else 0)),
            ("ok", e_bool(True)),
        ])

    if lower == "w.ab":
        return make_jw_cmd_response(cmd, [
            ("g", e_str("Segment_Original")),
            ("hg", e_str("A")),
            ("ne", e_bool(False)),
            ("ok", e_bool(True)),
            ("uu", e_bool(False)),
            ("re", e_str("O")),
            ("nt", e_int(0)),
            ("s", e_str("")),
        ])

    if lower == "c.rg" and friend_mode != "skip":
        sequence = int(params.get("s", 0)) if isinstance(params, dict) else 0
        if friend_mode in ("random_users_saves", "random_user_stub"):
            fields = make_random_park_friend_fields(cmd, sequence, username, friend_mode, log)
            fields = [(key, value) for key, value in fields if key != "co"]
        else:
            fields = [
                ("s", e_long(sequence)),
                ("id", e_arr([])),
                ("fa", e_arr([])),
            ]
        return make_ext_response(cmd, fields)

    if lower == "c.grs":
        return make_ext_response(
            cmd,
            make_game_random_search_fields(
                params,
                log,
                current_save_id=username,
                session_state=session_state,
                current_saved=load_guest_save(username, log),
            ),
        )

    if lower == "c.rp" and product_mode != "skip":
        if isinstance(params, dict) and params.get("st") == IAP_GPLAY_STORE_NAME:
            fields, product_count = make_iap_store_catalog_fields(
                params,
                load_cached_online_options(log),
            )
            log(f"[PRODUCTS] direct c.rp GPLAY catalog callback sent products={product_count}")
            return make_ext_response(cmd, fields)
        return make_ext_response(cmd, [
            ("s", e_long(int(params.get("s", 0)) if isinstance(params, dict) else 0)),
            ("np", e_long(0)),
            ("p", e_arr([])),
        ])

    if lower == "c.mg" and mail_mode != "skip":
        saved = load_guest_save(username, log)
        return make_ext_response(
            cmd,
            make_mailbox_fields(
                int(params.get("s", 0)) if isinstance(params, dict) else 0,
                saved,
                log,
                session_state,
            ),
        )

    if is_account_resources_command(lower):
        saved = load_guest_save(username, log)
        hardcash = (
            saved_hardcash_estimate(saved)
            if hardcash_balance_is_known(saved)
            else None
        )
        log(f"[RESOURCES] direct account resources callback sent hardcash={hardcash}")
        return make_ext_response(
            cmd,
            make_account_resources_fields(
                int(params.get("s", 0)) if isinstance(params, dict) else 0,
                saved,
            ),
        )

    if lower == "c.ms" and mail_mode != "skip":
        # onSendMailCallback ignores payload fields; sequence keeps the
        # response associated with the outgoing command.
        return make_ext_response(cmd, [
            ("s", e_long(int(params.get("s", 0)) if isinstance(params, dict) else 0)),
            ("ok", e_bool(True)),
        ])

    if lower in ("c.mc", "c.md", "c.mr", "c.mf") and mail_mode != "skip":
        if lower == "c.mc":
            saved = load_guest_save(username, log)
            fields = make_mail_consume_fields(
                int(params.get("s", 0)) if isinstance(params, dict) else 0,
                params,
                saved,
                log,
                session_state,
            )
            if isinstance(session_state, dict):
                if hardcash_balance_is_known(saved):
                    session_state["hardcash_late_sync_requested"] = True
                if session_state.pop("hardcash_meta_changed", False):
                    save_guest_data(username, saved, log, session_state)
                    session_state["last_save_persist_monotonic"] = time.monotonic()
                    log("[SAVE] persisted direct hardcash mail delivery metadata")
            return make_ext_response(cmd, fields)
        log(f"[MAILBOX] {lower} consume/delete/friend acknowledged")
        return make_ext_response(cmd, [
            ("s", e_long(int(params.get("s", 0)) if isinstance(params, dict) else 0)),
            ("ok", e_bool(True)),
        ])

    if lower == "w.pr":
        return make_jw_cmd_response(cmd, [
            ("r", e_arr([])),
            ("m", e_arr([])),
            ("ok", e_bool(True)),
            ("uu", e_bool(False)),
            ("re", e_str("O")),
            ("nt", e_int(0)),
            ("s", e_str("")),
        ])

    if lower in ("bootstrap", "bs", "init", "gb", "getbootstrap", "gs"):
        return make_ext_response(cmd, make_bootstrap_fields(username, zone, sequence))

    if any(word in lower for word in ("save", "storage", "load", "profile", "park")):
        saved = load_guest_save(username, log) if has_guest_save(username) else {}
        return make_ext_response(
            cmd,
            make_savegame_stub(username, zone, sequence, saved),
        )

    if lower in ("ts", "time", "gettime", "servertime"):
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("timestamp", e_int(int(time.time()))),
            ("serverTime", e_int(int(time.time()))),
        ])

    if any(word in lower for word in ("option", "config", "version")):
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("success", e_bool(True)),
            ("version", e_str("4.9.0.pag")),
            ("onlineoptions", e_bool(True)),
        ])

    if any(word in lower for word in ("friend", "leaderboard", "social")):
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("success", e_bool(True)),
            ("entries", e_arr([])),
            ("friends", e_arr([])),
        ])

    if any(word in lower for word in ("mail", "message", "notification", "reward")):
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("success", e_bool(True)),
            ("messages", e_arr([])),
            ("rewards", e_arr([])),
        ])

    if any(word in lower for word in ("store", "catalog", "shop", "product")):
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("success", e_bool(True)),
            ("items", e_arr([])),
            ("products", e_arr([])),
        ])

    if any(word in lower for word in ("event", "scheduler", "achievement")):
        return make_ext_response(cmd, [
            ("s", e_long(sequence)),
            ("ok", e_bool(True)),
            ("success", e_bool(True)),
            ("events", e_arr([])),
            ("achievements", e_arr([])),
        ])

    log(f"[SFS] UNKNOWN EXTENSION COMMAND: {cmd!r}")
    return make_ext_response(cmd, [
        ("s", e_long(sequence)),
        ("ok", e_bool(True)),
        ("success", e_bool(True)),
        ("echo", e_bool(True)),
        ("serverTime", e_int(int(time.time()))),
    ])


def log_sfs_packet(log, cid, direction, frame_info, ctrl, action, params):
    raw = frame_info["body"]
    meta = {
        "ctrl": ctrl,
        "action": action,
        "compressed": frame_info["is_compressed"],
        "big": frame_info["is_big"],
        "body_size": frame_info["body_size"],
    }
    log(f"[C{cid} SFS] {direction} meta={compact_json(meta)} params={compact_json(params)}")
    log(f"[C{cid} SFS] {direction} body_hex={short_hex(raw)}")


def read_post_login_frame(
    stream,
    timeout,
    save_username,
    player_session_id,
    session_registry,
    keepalive_stop,
    send_failed,
    log,
):
    """Read one frame without expiring a live session during a slow load."""
    try:
        return stream.read_frame(timeout=timeout)
    except socket.timeout as exc:
        if keepalive_stop.is_set() or send_failed.is_set():
            raise ConnectionError("post-login connection is no longer active") from exc
        if not session_registry.touch(save_username, player_session_id):
            raise ConnectionError("post-login save session is no longer active") from exc
        log("[SFS] post-login idle timeout; active session kept open")
        return None


def handle_sfs(conn, addr, cid, args, log):
    stream = SFSStream(conn)
    username = "Guest"
    account_username = "Guest"
    save_username = "Guest"
    device_label = ""
    device_links_error = ""
    zone = "Jurassic"
    login_params = {}
    send_lock = threading.Lock()
    keepalive_stop = threading.Event()
    hardcash_sync_started = False
    player_log_id = None
    player_session_id = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_C{cid}"
    send_failed = threading.Event()
    device_id = ""
    session_acquired = False

    def send_response(response):
        with send_lock:
            if send_failed.is_set() or keepalive_stop.is_set():
                raise ConnectionError(CLIENT_CLOSED)
            try:
                conn.sendall(response)
            except OSError as exc:
                send_failed.set()
                keepalive_stop.set()
                raise ConnectionError(CLIENT_CLOSED) from exc

    def player_log(msg):
        log(msg)
        if player_log_id:
            write_player_log(player_log_id, msg, player_session_id)

    def start_late_hardcash_sync(reason):
        nonlocal hardcash_sync_started
        if hardcash_sync_started:
            return
        hardcash_sync_started = True

        def worker():
            nonlocal hardcash_sync_started
            player_log(
                f"[C{cid} [RESOURCES] starting late hardcash sync "
                f"reason={reason} delays={HARDCASH_LATE_SYNC_DELAYS!r}"
            )
            try:
                for index, delay in enumerate(HARDCASH_LATE_SYNC_DELAYS, start=1):
                    if keepalive_stop.wait(delay):
                        return
                    try:
                        saved = load_guest_save(save_username, lambda m: player_log(f"[C{cid} {m}"))
                        if not hardcash_balance_is_known(saved):
                            player_log(
                                f"[C{cid} [RESOURCES] late hardcash sync skipped: "
                                "balance is client-owned"
                            )
                            return
                        hardcash = saved_hardcash_estimate(saved)
                        sequence = 900000 + index
                        response = make_ext_response(
                            "c.gi",
                            make_cgi_resource_fields(sequence, saved),
                        )
                        send_response(response)
                        player_log(
                            f"[C{cid} [RESOURCES] OUT late c.gi hardcash sync "
                            f"#{index} seq={sequence} hardcash={hardcash} bytes={len(response)}"
                        )
                    except Exception as exc:
                        if str(exc) == CLIENT_CLOSED:
                            player_log(f"[C{cid} [RESOURCES] late hardcash sync stopped: client closed")
                        else:
                            player_log(f"[C{cid} [RESOURCES] late hardcash sync stopped: {exc}")
                        return
            finally:
                hardcash_sync_started = False

        threading.Thread(target=worker, daemon=True).start()

    def start_sfs_keepalive():
        def worker():
            while not keepalive_stop.wait(SFS_SERVER_KEEPALIVE_SECONDS):
                try:
                    response = make_sfs_ping_response()
                    send_response(response)
                    player_log(f"[C{cid} SFS] OUT server keepalive bytes={len(response)}")
                except Exception as exc:
                    if str(exc) != CLIENT_CLOSED:
                        player_log(f"[C{cid} SFS] server keepalive stopped: {exc}")
                    keepalive_stop.set()
                    return

        threading.Thread(target=worker, daemon=True).start()

    try:
        log(f"[C{cid} SFS] connected from {addr[0]}:{addr[1]}")

        frame = stream.read_frame(timeout=10)
        ctrl, action, params, _outer = decode_packet(frame["body"])
        log_sfs_packet(log, cid, "IN handshake", frame, ctrl, action, params)
        if ctrl != 0 or action != 0:
            log(f"[C{cid} SFS] expected handshake ctrl=0 action=0, got ctrl={ctrl} action={action}")
            return

        response = make_handshake_response()
        send_response(response)
        log(f"[C{cid} SFS] OUT handshake ok hex={short_hex(response)}")

        frame = stream.read_frame(timeout=20)
        ctrl, action, params, _outer = decode_packet(frame["body"])
        log_sfs_packet(log, cid, "IN login", frame, ctrl, action, params)
        if ctrl != 0 or action != 1:
            log(f"[C{cid} SFS] expected login ctrl=0 action=1, got ctrl={ctrl} action={action}")
            return

        if isinstance(params, dict):
            username = params.get("un") or username
            zone = params.get("zn") or zone
            login_params = params.get("p") if isinstance(params.get("p"), dict) else {}
            login_params.update({k: v for k, v in params.items() if k != "p"})
            save_username, device_label, device_links_error = linked_save_identity(username, login_params)
            account_username = (
                stable_account_identity(
                    username,
                    login_params,
                    shared_save_id=save_username if device_label else "",
                )
                if args.stable_login_user
                else username
            )

        device_id = login_device_id(login_params)
        remember_pairing_identity(addr[0], device_id, save_username)
        if device_links_error:
            log(f"[C{cid} [DEVICES] link config error: {device_links_error}; using default save identity")
        whitelist_ok, whitelist_reason, whitelist = whitelist_allows_login(username, device_id, save_username)
        if (
            not whitelist_ok
            and whitelist_reason != "denied_save_id"
            and whitelist.get("auto_whitelist")
        ):
            auto_whitelist_device(device_id, save_username, username, log)
            whitelist_ok = True
            whitelist_reason = "auto_whitelisted"

        if whitelist["enabled"]:
            whitelist_line = (
                f"[C{cid} [WHITELIST] "
                f"{'accepted' if whitelist_ok else 'rejected'} "
                f"device_id={device_id!r} save_id={save_username!r} user={username!r} "
                f"device_label={device_label or 'primary'!r} "
                f"reason={whitelist_reason}"
            )
            log(whitelist_line)
        if not whitelist_ok:
            message = "This private server is currently closed for testing."
            response = make_login_reject(username, zone, message)
            send_response(response)
            write_player_log(save_username, whitelist_line, player_session_id)
            log(
                f"[C{cid} SFS] OUT login rejected user={username!r} "
                f"save_id={save_username!r} bytes={len(response)} hex={short_hex(response)}"
            )
            write_player_log(
                save_username,
                f"[C{cid} SFS] OUT login rejected user={username!r} "
                f"save_id={save_username!r} reason={whitelist_reason}",
                player_session_id,
            )
            return

        session_ok, active_session = SAVE_SESSION_REGISTRY.acquire(
            save_username,
            player_session_id,
            device_id=device_id,
            device_label=device_label or "primary",
            client_ip=addr[0],
        )
        if not session_ok:
            active_device = (
                str((active_session or {}).get("device_label") or "")
                or str((active_session or {}).get("device_id") or "")
                or "another device"
            )
            message = (
                "This save is already active on another linked device. "
                "Close the game there and try again."
            )
            response = make_login_reject(username, zone, message)
            send_response(response)
            log(
                f"[C{cid} [SAVE-LOCK] login rejected save_id={save_username!r} "
                f"device_id={device_id!r} active_device={active_device!r}"
            )
            write_player_log(
                save_username,
                f"[C{cid} [SAVE-LOCK] rejected concurrent login "
                f"device_id={device_id!r} active_device={active_device!r}",
                player_session_id,
            )
            return
        session_acquired = True

        player_log_id = save_username
        player_log(
            f"[C{cid} [PLAYER] login accepted "
            f"device_id={device_id!r} save_id={save_username!r} "
            f"device_label={device_label or 'primary'!r} "
            f"account_user={account_username!r} login_user={username!r} zone={zone!r}"
        )
        if whitelist["enabled"]:
            write_player_log(save_username, whitelist_line, player_session_id)

        if isinstance(params, dict):
            login_params["_server_has_save"] = has_guest_save(save_username)
            login_params["_server_save_data"] = (
                load_guest_save(save_username, lambda m: player_log(f"[C{cid} {m}"))
                if login_params["_server_has_save"]
                else {}
            )
            if args.stable_login_user:
                account_username = stable_account_identity(
                    username,
                    login_params,
                    shared_save_id=save_username if device_label else "",
                    saved=login_params["_server_save_data"],
                )
            if save_username != username:
                if device_label:
                    player_log(
                        f"[C{cid} [SAVE] using linked save identity {save_username!r} "
                        f"for {device_label} login user {username!r}"
                    )
                else:
                    player_log(f"[C{cid} [SAVE] using stable save identity {save_username!r} for login user {username!r}")
            if account_username != username:
                player_log(f"[C{cid} [SAVE] using stable account identity {account_username!r} for login user {username!r}")
            player_log(f"[C{cid} [SAVE] login save-present={login_params['_server_has_save']} path={guest_save_path(save_username)!r}")
            if args.import_save_path:
                import_guest_save(save_username, args.import_save_path, lambda m: player_log(f"[C{cid} {m}"))

        response = make_login_ok(account_username, zone, login_params, "android_confirmed", "normal")
        send_response(response)
        player_log(
            f"[C{cid} SFS] OUT login ok profile=android_confirmed "
            f"user={account_username!r} original_user={username!r} zone={zone!r} "
            f"bytes={len(response)} hex={short_hex(response)}"
        )
        start_sfs_keepalive()

        session_state = {
            "saw_save_write": False,
            "preserve_richer_save": args.preserve_richer_save,
            "account_username": account_username,
            "login_username": username,
            "save_username": save_username,
            "session_id": player_session_id,
            "device_id": device_id,
            "device_label": device_label or "primary",
            "save_revision": save_file_revision(guest_save_path(save_username)),
            "save_conflicted": False,
            "server_offline_elapsed_seconds": saved_offline_elapsed_seconds(
                login_params.get("_server_save_data", {})
                if isinstance(login_params, dict)
                else {}
            ),
            "save_existed_at_login": bool(
                login_params.get("_server_has_save")
                if isinstance(login_params, dict)
                else False
            ),
            "served_save_read": False,
            "last_save_replay_monotonic": None,
            "last_save_persist_monotonic": None,
        }
        if session_state["server_offline_elapsed_seconds"] > 0:
            player_log(
                f"[C{cid} [TIME] login offline elapsed frozen at "
                f"{session_state['server_offline_elapsed_seconds']}s before client startup writes"
            )
        else:
            player_log(f"[C{cid} [TIME] login offline elapsed is 0s")
        while True:
            try:
                frame = read_post_login_frame(
                    stream,
                    90,
                    save_username,
                    player_session_id,
                    SAVE_SESSION_REGISTRY,
                    keepalive_stop,
                    send_failed,
                    lambda message: player_log(f"[C{cid} {message}"),
                )
            except ConnectionError as exc:
                if str(exc) == CLIENT_CLOSED:
                    player_log(f"[C{cid} SFS] client closed after login without another complete SFS packet")
                else:
                    player_log(f"[C{cid} SFS] connection error after login: {exc}")
                return
            if frame is None:
                continue
            if not SAVE_SESSION_REGISTRY.touch(save_username, player_session_id):
                player_log(
                    f"[C{cid} [SAVE-LOCK] session was released or replaced; "
                    "closing connection before processing another packet"
                )
                return

            ctrl, action, params, _outer = decode_packet(frame["body"])
            log_sfs_packet(log, cid, "IN", frame, ctrl, action, params)

            if ctrl == 0 and action == 29:
                response = make_sfs_ping_response()
                send_response(response)
                log(f"[C{cid} SFS] OUT ping-pong bytes={len(response)}")
                continue

            if ctrl == 0 and action == 26:
                player_log(f"[C{cid} SFS] client requested manual disconnect")
                return

            if ctrl == 1 or action == 13:
                cmd = params.get("c", "") if isinstance(params, dict) else ""
                ext_params = params.get("p", {}) if isinstance(params, dict) else {}
                response = handle_extension_cmd(
                    cmd,
                    ext_params,
                    save_username,
                    zone,
                    args.composite_profile,
                    args.online_options_mode,
                    args.friend_mode,
                    args.product_mode,
                    args.scheduler_mode,
                    args.mail_mode,
                    args.game_services_mode,
                    args.replay_keys,
                    args.zr_replay_shape,
                    lambda m: player_log(f"[C{cid} {m}"),
                    session_state,
                )
                if response is not None:
                    send_response(response)
                    player_log(f"[C{cid} SFS] OUT ext cmd={cmd!r} bytes={len(response)} hex={short_hex(response)}")
                    if session_state.pop("hardcash_late_sync_requested", False):
                        start_late_hardcash_sync(cmd)
                continue

            log(f"[C{cid} SFS] no handler for ctrl={ctrl} action={action}; sending empty ok")
            response = build_packet(ctrl, action, [("ok", e_bool(True)), ("success", e_bool(True))])
            send_response(response)

    except socket.timeout:
        player_log(f"[C{cid} SFS] timeout waiting for data")
    except ConnectionError as exc:
        player_log(f"[C{cid} SFS] {exc}")
    except Exception as exc:
        player_log(f"[C{cid} SFS] ERROR {type(exc).__name__}: {exc}")
    finally:
        keepalive_stop.set()
        if session_acquired:
            SAVE_SESSION_REGISTRY.release(save_username, player_session_id)


def http_response(conn, status, body, content_type="application/json", extra_headers=None, method="GET", keep_alive=False):
    if isinstance(body, str):
        body = body.encode("utf-8")
    reason = {
        200: "OK",
        206: "Partial Content",
        404: "Not Found",
        416: "Range Not Satisfiable",
        500: "Internal Server Error",
    }.get(status, "OK")
    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Access-Control-Allow-Origin: *",
        "Connection: keep-alive" if keep_alive else "Connection: close",
    ]
    if extra_headers:
        headers.extend(extra_headers)
    header_blob = "\r\n".join(headers).encode("ascii") + b"\r\n\r\n"
    conn.sendall(header_blob)
    if method.upper() != "HEAD":
        conn.sendall(body)


def parse_http_byte_range(range_header, size):
    if not range_header:
        return None
    text = str(range_header).strip()
    if not text.lower().startswith("bytes=") or "," in text or size <= 0:
        return False
    spec = text[6:].strip()
    if "-" not in spec:
        return False
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return False
            start = max(0, size - suffix_length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or start >= size or end < start:
                return False
            end = min(end, size - 1)
    except (TypeError, ValueError):
        return False
    return start, end


def stream_file_response(conn, file_path, extra_headers=None, method="GET", chunk_size=256 * 1024, log_func=None, cid=None, keep_alive=False, range_header=""):
    try:
        size = os.path.getsize(file_path)
    except OSError:
        http_response(conn, 404, b"", "application/octet-stream", method=method, keep_alive=keep_alive)
        return

    byte_range = parse_http_byte_range(range_header, size)
    if byte_range is False:
        http_response(
            conn,
            416,
            b"",
            "application/octet-stream",
            extra_headers=[f"Content-Range: bytes */{size}", "Accept-Ranges: bytes"],
            method=method,
            keep_alive=False,
        )
        return
    start, end = byte_range if byte_range is not None else (0, size - 1)
    response_size = end - start + 1
    status_line = "HTTP/1.1 206 Partial Content" if byte_range is not None else "HTTP/1.1 200 OK"
    headers = [
        status_line,
        "Content-Type: application/octet-stream",
        f"Content-Length: {response_size}",
        "Accept-Ranges: bytes",
        "Access-Control-Allow-Origin: *",
        "Connection: keep-alive" if keep_alive else "Connection: close",
    ]
    if byte_range is not None:
        headers.append(f"Content-Range: bytes {start}-{end}/{size}")
    if extra_headers:
        headers.extend(extra_headers)
    header_blob = "\r\n".join(headers).encode("ascii") + b"\r\n\r\n"
    conn.sendall(header_blob)

    if method.upper() == "HEAD":
        return

    # Set a generous timeout to allow slow connections to download without timing out
    conn.settimeout(60.0)

    try:
        bytes_sent = 0
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = response_size
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                conn.sendall(chunk)
                bytes_sent += len(chunk)
                remaining -= len(chunk)
                if size > 5 * 1024 * 1024 and log_func and cid:
                    # Log progress every 5MB
                    if (bytes_sent - len(chunk)) // (5 * 1024 * 1024) != bytes_sent // (5 * 1024 * 1024):
                        log_func(f"[C{cid} HTTP] streaming {os.path.basename(file_path)} progress={bytes_sent}/{response_size} bytes range={start}-{end}")
    except Exception as exc:
        if log_func and cid:
            log_func(f"[C{cid} HTTP] streaming ERROR on {os.path.basename(file_path)}: {type(exc).__name__}: {exc}")
        raise exc


def stream_rewritten_lpkg_response(conn, file_path, extra_headers=None, method="GET", chunk_size=256 * 1024, log_func=None, cid=None, keep_alive=False, range_header=""):
    try:
        size = os.path.getsize(file_path)
    except OSError:
        http_response(conn, 404, b"", "application/octet-stream", method=method, keep_alive=keep_alive)
        return

    byte_range = parse_http_byte_range(range_header, size)
    if byte_range is False:
        http_response(
            conn,
            416,
            b"",
            "application/octet-stream",
            extra_headers=[f"Content-Range: bytes */{size}", "Accept-Ranges: bytes"],
            method=method,
            keep_alive=False,
        )
        return
    start, end = byte_range if byte_range is not None else (0, size - 1)
    response_size = end - start + 1
    status_line = "HTTP/1.1 206 Partial Content" if byte_range is not None else "HTTP/1.1 200 OK"
    headers = [
        status_line,
        "Content-Type: application/octet-stream",
        f"Content-Length: {response_size}",
        "Accept-Ranges: bytes",
        "Access-Control-Allow-Origin: *",
        "Connection: keep-alive" if keep_alive else "Connection: close",
    ]
    if byte_range is not None:
        headers.append(f"Content-Range: bytes {start}-{end}/{size}")
    if extra_headers:
        headers.extend(extra_headers)
    header_blob = "\r\n".join(headers).encode("ascii") + b"\r\n\r\n"
    conn.sendall(header_blob)

    if method.upper() == "HEAD":
        return

    conn.settimeout(60.0)

    try:
        bytes_sent = 0
        with open(file_path, "rb") as handle:
            handle.seek(start)
            remaining = response_size
            absolute_offset = start
            while remaining > 0:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    break
                if absolute_offset < 4 and absolute_offset + len(chunk) > 0:
                    rewritten = bytearray(chunk)
                    replacement = bytes((LPKG_REWRITE_VERSION, 0, 0, LPKG_REWRITE_VERSION))
                    for file_offset in range(max(0, absolute_offset), min(4, absolute_offset + len(chunk))):
                        rewritten[file_offset - absolute_offset] = replacement[file_offset]
                    chunk = bytes(rewritten)
                conn.sendall(chunk)
                bytes_sent += len(chunk)
                remaining -= len(chunk)
                absolute_offset += len(chunk)
                if size > 5 * 1024 * 1024 and log_func and cid:
                    if (bytes_sent - len(chunk)) // (5 * 1024 * 1024) != bytes_sent // (5 * 1024 * 1024):
                        log_func(f"[C{cid} HTTP] streaming {os.path.basename(file_path)} progress={bytes_sent}/{response_size} bytes range={start}-{end}")
    except Exception as exc:
        if log_func and cid:
            log_func(f"[C{cid} HTTP] streaming ERROR on {os.path.basename(file_path)}: {type(exc).__name__}: {exc}")
        raise exc


def read_file_or_empty(path):
    if not os.path.exists(path):
        return b""
    with open(path, "rb") as file:
        return file.read()


def current_patched_online_options(log=None):
    """Return the exact onlineoptions payload advertised to the client."""
    raw_body = STATIC_ASSET_CACHE.get_content(ONLINE_OPTIONS_FILE, log) or b""
    option_overrides = online_options_binary_overrides()
    body, patched_keys = patch_online_options_bytes(raw_body, option_overrides)
    return body, option_overrides, patched_keys


def handle_http(conn, addr, cid, _args, log):
    try:
        conn.settimeout(8.0)
        buffer = b""
        
        while True:
            # Read until HTTP headers end (\r\n\r\n)
            while b"\r\n\r\n" not in buffer:
                chunk = conn.recv(8192)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > 32768:
                    return
            
            # Split off the request headers
            req_header_bytes, buffer = buffer.split(b"\r\n\r\n", 1)
            request_text = req_header_bytes.decode("iso-8859-1", "replace")
            
            lines = request_text.split("\r\n")
            request_line = lines[0]
            parts = request_line.split(" ")
            if len(parts) < 2:
                log(f"[C{cid} HTTP] bad request line={request_line!r}")
                return
            
            method, raw_target = parts[0], parts[1]
            parsed = urlsplit(raw_target)
            path = unquote(parsed.path)
            request_headers = {}
            for line in lines[1:]:
                if ":" not in line:
                    continue
                header_name, header_value = line.split(":", 1)
                request_headers[header_name.strip().lower()] = header_value.strip()
            range_header = request_headers.get("range", "")
            range_suffix = f" range={range_header!r}" if range_header else ""
            log(f"[C{cid} HTTP] {method} {path}{range_suffix}")
            
            # Detect keep-alive
            keep_alive = True
            if "close" in request_headers.get("connection", "").lower():
                keep_alive = False
            
            setup = setup_resource(BASE_DIR, path, request_headers.get("host", ""))
            if setup is not None:
                body, content_type = setup
                http_response(
                    conn,
                    200,
                    body,
                    content_type=content_type,
                    extra_headers=["Cache-Control: no-store"],
                    method=method,
                    keep_alive=keep_alive,
                )
                log(f"[C{cid} HTTP] setup resource {path}")

            # Show the most recent game save ID seen from this phone/network.
            elif path.rstrip("/") in ("/pair", "/pair.txt"):
                pairing = recent_pairing_identity(addr[0])
                if path.rstrip("/") == "/pair.txt":
                    body = (str(pairing.get("save_id", "")) + "\n") if pairing else ""
                    http_response(
                        conn,
                        200,
                        body,
                        content_type="text/plain; charset=utf-8",
                        extra_headers=["Cache-Control: no-store"],
                        method=method,
                        keep_alive=keep_alive,
                    )
                else:
                    http_response(
                        conn,
                        200,
                        pairing_page(addr[0]),
                        content_type="text/html; charset=utf-8",
                        extra_headers=["Cache-Control: no-store"],
                        method=method,
                        keep_alive=keep_alive,
                    )
                log(f"[C{cid} HTTP] pairing page save_id={'ready' if pairing else 'not_found'}")

            # Handle status request
            elif "/status/2.0/" in path:
                body = {
                    "status": True,
                    "message": "",
                    "country": "US",
                    "disabled_services": [
                        "MIXPANEL",
                        "FLINTENABLEDMETRICS",
                    ],
                }
                http_response(conn, 200, json.dumps(body), method=method, keep_alive=keep_alive)
                source = "local" if addr[0] in ("127.0.0.1", "::1") else "device"
                log(f"[C{cid} HTTP] status ok source={source}")
            
            # Handle manifest request
            elif "/assets/1.0/" in path:
                body = STATIC_ASSET_CACHE.get_content(MANIFEST_FILE, log) or b"{}"
                try:
                    body_str = body.decode("utf-8")
                    host_header = ""
                    host_header = request_headers.get("host", "")
                    if host_header:
                        manifest = json.loads(body_str)
                        if isinstance(manifest, list):
                            for entry in manifest:
                                if not isinstance(entry, dict):
                                    continue
                                filename = os.path.basename(str(entry.get("filename", "")))
                                if filename:
                                    for candidate_dir in CACHE_DIRS:
                                        candidate_path = os.path.join(candidate_dir, filename)
                                        if not os.path.exists(candidate_path):
                                            continue
                                        try:
                                            size = os.path.getsize(candidate_path)
                                            mtime = os.path.getmtime(candidate_path)
                                        except OSError:
                                            break
                                        checksum = rewritten_lpkg_checksum(candidate_path, size, mtime)
                                        if checksum:
                                            entry["checksum"] = checksum
                                            entry["size"] = str(size)
                                        break
                                    entry["filename"] = filename
                                    entry["url"] = (
                                        f"http://{host_header}/jp/local_path/"
                                        f"{quote(filename)}"
                                    )
                            body = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
                except Exception as exc:
                    log(f"[HTTP] Error replacing host in manifest: {exc!r}")

                http_response(
                    conn,
                    200,
                    body,
                    "text/plain",
                    extra_headers=[f"Cache-Control: public, max-age={STATIC_CACHE_MAX_AGE_SECONDS}"],
                    method=method,
                    keep_alive=keep_alive
                )
                log(f"[C{cid} HTTP] manifest bytes={len(body)}")
            
            # Handle onlineoptions
            elif "onlineoptions" in path.lower():
                body, option_overrides, patched_keys = current_patched_online_options(log)
                http_response(
                    conn,
                    200,
                    body,
                    "application/octet-stream",
                    extra_headers=[
                        "Cache-Control: no-store, no-cache, max-age=0, must-revalidate",
                        "Pragma: no-cache",
                    ],
                    method=method,
                    keep_alive=keep_alive
                )
                log(
                    f"[C{cid} HTTP] onlineoptions bytes={len(body)} "
                    f"version={current_online_options_version()} "
                    f"addon_dino={option_overrides.get('ADDON_DINO_ID')!r} "
                    f"addon_limit={option_overrides.get('ADDON_DINO_LIMIT')!r} "
                    f"event_dino={option_overrides.get('EVENT_DNA_DINO')!r} "
                    f"event_limit={option_overrides.get('EVENT_DNA_LIMIT')!r} "
                    f"patched_keys={patched_keys!r}"
                )
            
            # Handle cache files
            elif "/jp/" in path or "/local_path/" in path:
                requested_filename = os.path.basename(path)
                filename = requested_filename
                cache_path = None
                for candidate_dir in CACHE_DIRS:
                    candidate_path = os.path.join(candidate_dir, filename)
                    if os.path.exists(candidate_path):
                        cache_path = candidate_path
                        break
                if cache_path is None:
                    cache_path = os.path.join(CACHE_DIR, filename)

                try:
                    size = os.path.getsize(cache_path) if os.path.exists(cache_path) else 0
                except OSError:
                    size = 0

                # Large files (> 5MB) are streamed directly from disk to keep RAM usage low
                if size > 5 * 1024 * 1024:
                    keep_alive = False
                    if package_needs_lpkg_rewrite(cache_path):
                        stream_rewritten_lpkg_response(
                            conn,
                            cache_path,
                            extra_headers=[
                                f"Cache-Control: public, max-age={STATIC_CACHE_MAX_AGE_SECONDS}, immutable",
                            ],
                            method=method,
                            log_func=log,
                            cid=cid,
                            keep_alive=keep_alive,
                            range_header=range_header,
                        )
                    else:
                        stream_file_response(
                            conn,
                            cache_path,
                            extra_headers=[
                                f"Cache-Control: public, max-age={STATIC_CACHE_MAX_AGE_SECONDS}, immutable",
                            ],
                            method=method,
                            log_func=log,
                            cid=cid,
                            keep_alive=keep_alive,
                            range_header=range_header,
                        )
                    log(f"[C{cid} HTTP] streamed large asset {filename} bytes={size}")
                else:
                    body = STATIC_ASSET_CACHE.get_content(cache_path, log)
                    if body:
                        body = maybe_rewrite_lpkg_header(body)
                        body, indominus_ready = patch_indominus_offer(requested_filename, body)
                        http_response(
                            conn,
                            200,
                            body,
                            "application/octet-stream",
                            extra_headers=[
                                f"Cache-Control: public, max-age={STATIC_CACHE_MAX_AGE_SECONDS}, immutable",
                            ],
                            method=method,
                            keep_alive=keep_alive
                        )
                        if is_indominus_offer_asset(requested_filename):
                            log(
                                f"[C{cid} HTTP] Indominus 499-hardcash offer "
                                f"{'active' if indominus_ready else 'skipped: unsupported gamedata'}"
                            )
                        log(f"[C{cid} HTTP] cache hit {filename} bytes={len(body)} source={os.path.basename(os.path.dirname(cache_path))}")
                    else:
                        http_response(conn, 404, b"", "application/octet-stream", method=method, keep_alive=keep_alive)
                        log(f"[C{cid} HTTP] cache MISS {filename}")
            
            # Handle generic fallback request
            else:
                body = json.dumps({"status": "ok", "path": path})
                http_response(conn, 200, body, method=method, keep_alive=keep_alive)
                log(f"[C{cid} HTTP] generic ok path={path}")

            # If client requested Connection: close or does not support Keep-Alive, break out
            if not keep_alive:
                break
                
            # Otherwise, set a short Keep-Alive timeout for the next request in the loop
            conn.settimeout(4.0)

    except socket.timeout:
        # Expected after keep-alive timeout expires
        pass
    except Exception as exc:
        log(f"[C{cid} HTTP] ERROR {type(exc).__name__}: {exc}")
        try:
            http_response(conn, 500, json.dumps({"error": str(exc)}), keep_alive=False)
        except Exception:
            pass


def open_listener(port, args, log):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((args.host, port))
        server.listen(256)
    except Exception as exc:
        log(f"[BOOT] failed to listen on {args.host}:{port}: {type(exc).__name__}: {exc}")
        try:
            server.close()
        except Exception:
            pass
        return None

    log(f"[BOOT] listening on {args.host}:{port}")
    return server


def serve_socket(server, port, args, log, cid_counter):
    while True:
        conn, addr = server.accept()
        cid = next(cid_counter)

        # Keep this worker tied to the connection accepted in this iteration.
        def worker(conn=conn, addr=addr, cid=cid, port=port):
            try:
                if port == args.sfs_port:
                    try:
                        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except OSError:
                        pass
                    handle_sfs(conn, addr, cid, args, log)
                else:
                    try:
                        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)
                    except OSError:
                        pass
                    handle_http(conn, addr, cid, args, log)
            finally:
                try:
                    if port != args.sfs_port:
                        # Graceful TCP shutdown: half-close write channel
                        conn.shutdown(socket.SHUT_WR)
                        # Set a short timeout to avoid blocking this worker thread forever
                        conn.settimeout(0.5)
                        # Read and discard any trailing data from client until clean EOF (FIN received)
                        while True:
                            buf = conn.recv(1024)
                            if not buf:
                                break
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                log(f"[C{cid}] closed")

        threading.Thread(target=worker, daemon=True).start()


def parse_ports(text):
    ports = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        ports.append(int(part))
    return tuple(ports)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Current Jurassic Park Builder private server. "
            "This entry point keeps the proven v141 behavior and hides old probe toggles."
        )
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--http-ports", default=",".join(str(p) for p in DEFAULT_HTTP_PORTS))
    parser.add_argument("--sfs-port", type=int, default=DEFAULT_SFS_PORT)
    parser.add_argument(
        "--import-save-path",
        default="",
        help=(
            "Optional one-shot test import: copy this JSON save into the stable "
            "device save slot after login, backing up the previous active file."
        ),
    )
    parser.add_argument(
        "--no-preserve-richer-save",
        dest="preserve_richer_save",
        action="store_false",
        help="disable the v55 guard that refuses to persist noticeably smaller save batches",
    )
    parser.add_argument("--quiet", action="store_true", help="write only to log file, not console")
    parser.add_argument(
        "--log-mode",
        choices=("hourly", "session", "test"),
        default="hourly",
        help=(
            "server log mode: hourly rotates jpb_current_YYYYMMDD_HH0000.log; "
            "session/test writes one full jpb_session_YYYYMMDD_HHMMSS.log until shutdown"
        ),
    )
    parser.add_argument(
        "--test-log",
        dest="log_mode",
        action="store_const",
        const="session",
        help="shortcut for --log-mode session",
    )
    parser.add_argument(
        "--no-preload-static-cache",
        dest="preload_static_cache",
        action="store_false",
        help="do not preload cache_files assets into RAM during startup",
    )
    parser.add_argument(
        "--adb-logcat",
        action="store_true",
        help="capture filtered Android logcat lines into the same server log",
    )
    parser.add_argument(
        "--adb-path",
        default="adb",
        help="adb executable path. Default uses adb from PATH.",
    )
    parser.add_argument(
        "--logcat-filter",
        default=",".join(DEFAULT_LOGCAT_MARKERS),
        help="comma-separated lowercase markers used to keep relevant logcat lines",
    )
    parser.add_argument(
        "--logcat-crash-lines",
        type=int,
        default=220,
        help="after a native crash marker, keep this many raw logcat lines to preserve the backtrace",
    )
    parser.add_argument(
        "--clear-logcat",
        dest="clear_logcat",
        action="store_true",
        help="clear the Android logcat buffer before capturing (default)",
    )
    parser.add_argument(
        "--no-clear-logcat",
        dest="clear_logcat",
        action="store_false",
        help="do not clear the Android logcat buffer before capturing",
    )
    parser.set_defaults(clear_logcat=True, preserve_richer_save=True, preload_static_cache=True)
    return parser.parse_args()


def apply_runtime_defaults(args):
    """Keep the known-good v141 settings without exposing old probe switches."""
    args.composite_profile = "savegame"
    args.online_options_mode = "full"
    args.friend_mode = "random_user_stub"
    args.product_mode = "empty"
    args.scheduler_mode = "events_empty"
    args.mail_mode = "empty"
    args.hardcash_mail_type = HARDCASH_MAIL_CONTENT_TYPE
    args.hardcash_mail_action = HARDCASH_MAIL_ACTION
    args.game_services_mode = os.environ.get("JPB_GAME_SERVICES_MODE", "generic").strip() or "generic"
    args.replay_keyset = "all"
    args.zr_replay_shape = "full"
    args.stable_login_user = True
    return args


def main():
    global HARDCASH_MAIL_CONTENT_TYPE, HARDCASH_MAIL_ACTION
    args = apply_runtime_defaults(parse_args())
    HARDCASH_MAIL_CONTENT_TYPE = max(0, min(8, int(args.hardcash_mail_type)))
    HARDCASH_MAIL_ACTION = max(0, int(args.hardcash_mail_action))
    args.http_ports = parse_ports(args.http_ports)
    replay_keysets = {
        "none": [],
        "res_only": ["res"],
        "prof_only": ["prof"],
        "prof_res": ["prof", "res"],
        "prof_res_park": ["prof", "res", "park"],
        "prof_res_park_road": ["prof", "res", "park", "road"],
        "all": [
            "prof",
            "res",
            "park",
            "road",
            "gen",
            "batl",
            "aqpk",
            "aqrd",
            "aqge",
            "arpk",
            "arrd",
            "arge",
        ],
    }
    args.replay_keys = replay_keysets[args.replay_keyset]
    SAVE_SESSION_REGISTRY.clear()
    logger = Logger(quiet=args.quiet, mode=args.log_mode)
    cid_counter = count(1)

    logger.write("=" * 68)
    logger.write("JPB CURRENT SERVER")
    logger.write("timer note: offline elapsed applies to object, expansion, dino, hatch, building, and food-port timer slots during replay")
    logger.write("save note: pre-read main-park writes are persisted before replay so the latest local progress wins")
    logger.write(
        "connection note: keeps the fixed reconnect flow; hardcash note: "
        "v141 uses a=5 mail delivery and treats MAILGIFT as delivery ack, not new income"
    )
    logger.write(f"base dir: {BASE_DIR}")
    logger.write(f"log file: {logger.path}")
    logger.write(f"manifest: {os.path.exists(MANIFEST_FILE)} {MANIFEST_FILE}")
    logger.write(
        f"onlineoptions: {os.path.exists(ONLINE_OPTIONS_FILE)} {ONLINE_OPTIONS_FILE} "
        f"version={current_online_options_version()}"
    )
    logger.write(f"cache dirs: {[(os.path.isdir(path), path) for path in CACHE_DIRS]}")
    logger.write(f"guest saves: {SAVE_DIR}")
    logger.write(f"player session logs: {PLAYER_SESSION_LOG_DIR}")
    logger.write(f"whitelist: {os.path.exists(WHITELIST_FILE)} {WHITELIST_FILE}")
    logger.write(f"device links: {os.path.exists(DEVICE_LINKS_FILE)} {DEVICE_LINKS_FILE}")
    logger.write(
        f"save sessions: one writer per save, ttl={SAVE_SESSION_TTL_SECONDS:.0f}s "
        f"state={SAVE_SESSION_REGISTRY.state_path}"
    )
    logger.write(f"log mode: {logger.mode}; log retention: 7 days")
    logger.write(
        "fixed modes: login=android_confirmed, composite=savegame, "
        f"replay=prof/res/park/road/gen/batl/aq/ar, game_services={args.game_services_mode}, battle_cache=hybrid-test"
    )
    logger.write(
        f"hardcash mail: content_t={HARDCASH_MAIL_CONTENT_TYPE} "
        f"action_a={HARDCASH_MAIL_ACTION}"
    )
    logger.write(f"save guard: preserve_richer_save={args.preserve_richer_save}")
    logger.write(f"adb logcat: {args.adb_logcat} ({args.adb_path}), clear={args.clear_logcat}")
    logger.write("=" * 68)

    if args.preload_static_cache:
        STATIC_ASSET_CACHE.preload_dirs(CACHE_DIRS, logger.write)

    logcat_proc = start_logcat_capture(args, logger.write)

    ports = list(args.http_ports)
    if args.sfs_port not in ports:
        ports.append(args.sfs_port)

    listeners = []
    for port in ports:
        server = open_listener(port, args, logger.write)
        if server is not None:
            listeners.append((port, server))

    if not any(port == args.sfs_port for port, _server in listeners):
        logger.write(f"[BOOT] FATAL: SFS port {args.sfs_port} is not listening. Close old servers or change --sfs-port.")
        if logcat_proc is not None and logcat_proc.poll() is None:
            logcat_proc.terminate()
        logger.close()
        return

    if not any(port in args.http_ports for port, _server in listeners):
        logger.write("[BOOT] FATAL: no HTTP/cache port is listening.")
        if logcat_proc is not None and logcat_proc.poll() is None:
            logcat_proc.terminate()
        logger.close()
        return

    for port, server in listeners:
        threading.Thread(target=serve_socket, args=(server, port, args, logger.write, cid_counter), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.write("[BOOT] shutdown requested")
    finally:
        SAVE_SESSION_REGISTRY.clear()
        if logcat_proc is not None and logcat_proc.poll() is None:
            logcat_proc.terminate()
        logger.close()


if __name__ == "__main__":
    main()
