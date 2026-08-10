"""Small, verified cache transformations used by both server profiles.

The shipped cache folders stay byte-for-byte unchanged.  Transformations are
applied only to the HTTP response and the matching manifest checksum.
"""

from __future__ import annotations

import hashlib
import os
import struct


INDOMINUS_OFFER_FILENAME = "gamedata.dsb"
INDOMINUS_CASH_TYPE_OFFSET = 0x8DA1C
INDOMINUS_ORIGINAL_CASH_TYPE = 3
INDOMINUS_HARDCASH_CASH_TYPE = 2
INDOMINUS_AMBER_RARITY_OFFSET = 0x8DA4C
INDOMINUS_ORIGINAL_AMBER_RARITY = 6
INDOMINUS_ADDON_AMBER_RARITY = 2
INDOMINUS_HARDCASH_PRICE_OFFSET = 0x7E048
INDOMINUS_ORIGINAL_HARDCASH_PRICE = 0
INDOMINUS_TARGET_HARDCASH_PRICE = 499
INDOMINUS_GAMEDATA_SIZE = 873_616

# The Android and iOS donor files contain identical game data and differ only
# in the two LPKG version bytes.  Restrict the patch to these reviewed inputs.
INDOMINUS_SOURCE_SHA256 = frozenset(
    {
        "768bdded640446fc0aaeb09fc3a97f4915a1b055e6f687e685165a54d5797570",
        "2a80983791b3a99a78bf602fd82d87903da23221d2bc1c67ab771e45560d568e",
    }
)
INDOMINUS_PATCHED_SHA256 = frozenset(
    {
        "a1b920e341d0cd475f501ef9c840fa5bd06f617f8bce791902c6eb79ecce7d7a",
        "dded721a7540ef828cae5f77953b22889b580d831578530051547800be347e61",
    }
)

# CRC32("CashType"), scalar type 0, original value 3,
# CRC32("LunaResourceType"), scalar type 0.
_INDOMINUS_CONTEXT_BEFORE = bytes.fromhex("de39adc600000000")
_INDOMINUS_CONTEXT_AFTER = bytes.fromhex("01a125e600000000")

# CRC32("AmberRarity"), scalar type 0, original value 6,
# followed by the next reviewed field header.
_INDOMINUS_AMBER_CONTEXT_BEFORE = bytes.fromhex("2f249fc800000000")
_INDOMINUS_AMBER_CONTEXT_AFTER = bytes.fromhex("3a09196501000000")

# CRC32("HardCashPrice"), scalar type 0, original value 0,
# followed by the next reviewed field header.
_INDOMINUS_HARDCASH_PRICE_CONTEXT_BEFORE = bytes.fromhex("bd491d2a00000000")
_INDOMINUS_HARDCASH_PRICE_CONTEXT_AFTER = bytes.fromhex("d1d5c15602000000")


def is_indominus_offer_asset(path_or_name: str) -> bool:
    return os.path.basename(str(path_or_name)).lower() == INDOMINUS_OFFER_FILENAME


def indominus_offer_patch_state(body: bytes) -> str:
    """Return original, patched, or unsupported for a gamedata payload."""
    if not isinstance(body, (bytes, bytearray)):
        return "unsupported"
    if len(body) != INDOMINUS_GAMEDATA_SIZE:
        return "unsupported"
    if body[INDOMINUS_CASH_TYPE_OFFSET - 8 : INDOMINUS_CASH_TYPE_OFFSET] != (
        _INDOMINUS_CONTEXT_BEFORE
    ):
        return "unsupported"
    if body[INDOMINUS_CASH_TYPE_OFFSET + 4 : INDOMINUS_CASH_TYPE_OFFSET + 12] != (
        _INDOMINUS_CONTEXT_AFTER
    ):
        return "unsupported"
    if body[
        INDOMINUS_AMBER_RARITY_OFFSET - 8 : INDOMINUS_AMBER_RARITY_OFFSET
    ] != _INDOMINUS_AMBER_CONTEXT_BEFORE:
        return "unsupported"
    if body[
        INDOMINUS_AMBER_RARITY_OFFSET + 4 : INDOMINUS_AMBER_RARITY_OFFSET + 12
    ] != _INDOMINUS_AMBER_CONTEXT_AFTER:
        return "unsupported"
    if body[
        INDOMINUS_HARDCASH_PRICE_OFFSET - 8 : INDOMINUS_HARDCASH_PRICE_OFFSET
    ] != _INDOMINUS_HARDCASH_PRICE_CONTEXT_BEFORE:
        return "unsupported"
    if body[
        INDOMINUS_HARDCASH_PRICE_OFFSET + 4 : INDOMINUS_HARDCASH_PRICE_OFFSET + 12
    ] != _INDOMINUS_HARDCASH_PRICE_CONTEXT_AFTER:
        return "unsupported"

    cash_type = struct.unpack_from("<I", body, INDOMINUS_CASH_TYPE_OFFSET)[0]
    amber_rarity = struct.unpack_from(
        "<I",
        body,
        INDOMINUS_AMBER_RARITY_OFFSET,
    )[0]
    hardcash_price = struct.unpack_from(
        "<I",
        body,
        INDOMINUS_HARDCASH_PRICE_OFFSET,
    )[0]
    digest = hashlib.sha256(body).hexdigest()
    if (
        cash_type == INDOMINUS_ORIGINAL_CASH_TYPE
        and amber_rarity == INDOMINUS_ORIGINAL_AMBER_RARITY
        and hardcash_price == INDOMINUS_ORIGINAL_HARDCASH_PRICE
        and digest in INDOMINUS_SOURCE_SHA256
    ):
        return "original"
    if (
        cash_type == INDOMINUS_HARDCASH_CASH_TYPE
        and amber_rarity == INDOMINUS_ADDON_AMBER_RARITY
        and hardcash_price == INDOMINUS_TARGET_HARDCASH_PRICE
        and digest in INDOMINUS_PATCHED_SHA256
    ):
        return "patched"
    return "unsupported"


def patch_indominus_offer(path_or_name: str, body: bytes) -> tuple[bytes, bool]:
    """Expose Indominus as a 499-hardcash surface add-on.

    Returns ``(body, applied_or_already_patched)``.  Unknown cache revisions are
    deliberately left untouched instead of applying an offset-only patch.
    """
    if not is_indominus_offer_asset(path_or_name):
        return body, False
    state = indominus_offer_patch_state(body)
    if state == "patched":
        return bytes(body), True
    if state != "original":
        return body, False

    patched = bytearray(body)
    struct.pack_into(
        "<I",
        patched,
        INDOMINUS_CASH_TYPE_OFFSET,
        INDOMINUS_HARDCASH_CASH_TYPE,
    )
    struct.pack_into(
        "<I",
        patched,
        INDOMINUS_AMBER_RARITY_OFFSET,
        INDOMINUS_ADDON_AMBER_RARITY,
    )
    struct.pack_into(
        "<I",
        patched,
        INDOMINUS_HARDCASH_PRICE_OFFSET,
        INDOMINUS_TARGET_HARDCASH_PRICE,
    )
    patched_bytes = bytes(patched)
    if indominus_offer_patch_state(patched_bytes) != "patched":
        return body, False
    return patched_bytes, True
