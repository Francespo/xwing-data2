#!/usr/bin/env python3
"""Parse YASB card data and match it to xwing-data2 pilots/upgrades.

YASB disambiguates repeated pilot names with a parenthetical suffix and/or an
``xwsaddon`` field, e.g.::

    name: "Anakin Skywalker (Delta-7b)"
    ship: "Delta-7b Aethersprite"
    faction: "Galactic Republic"

xwing-data2 stores the same pilot as ``name: "Anakin Skywalker"`` inside
``data/pilots/galactic-republic/delta-7b-aethersprite.json``: the ship and
faction live on the file, not on the pilot object. Matching only by pilot
name therefore silently skips every variant (Anakin and Adi Gallia on the
Delta-7B are the cases that exposed this).

The helpers below match on the pilot name **plus the file-level ship/faction**
and use ``xwsaddon`` / the YASB name suffix to disambiguate entries that share
a name and chassis. Ambiguous matches return ``None`` instead of guessing.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

# ``name: "..."`` or ``name: '...'``; nicknames are written as '"Klick"' in
# cards-common.coffee, so a split on ``name: "`` alone drops those cards.
_NAME_LINE = re.compile(r"\n\s*name:\s*(?:\"([^\"]*)\"|'([^']*)')")


def normalize(value: str | None) -> str:
    text = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    # YASB writes some cards with a digit zero where the data repo uses the
    # letter O (Aut-0 / Aut-O, G4R-G0R / G4R-GOR).
    text = text.replace("0", "o")
    return re.sub(r"[^a-z0-9]", "", text)


def split_variant(name: str) -> tuple[str, str]:
    """Return ``(base_name, last_parenthetical)`` for a YASB card name."""
    match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", name or "")
    if match:
        return match.group(1), match.group(2)
    return name or "", ""


def strip_parentheticals(name: str) -> str:
    """Return the name with every trailing ``(...)`` group removed."""
    base, suffix = split_variant(name)
    while suffix:
        base, suffix = split_variant(base)
    return base


def _iter_name_blocks(text: str):
    matches = list(_NAME_LINE.finditer(text))
    for index, match in enumerate(matches):
        name = match.group(1) if match.group(1) is not None else match.group(2)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield name, text[match.end():end]


def parse_yasb(text: str) -> list[dict]:
    """Parse every pilot/upgrade block from ``cards-common.coffee``."""
    entries: list[dict] = []
    for name, block in _iter_name_blocks(text):
        points_match = re.search(r"^\s*points:\s*(\d+)", block, re.MULTILINE)
        if not points_match:
            continue

        loadout_match = re.search(r"^\s*loadout:\s*(\d+)", block, re.MULTILINE)
        ship_match = re.search(r'^\s*ship:\s*"([^"]+)"', block, re.MULTILINE)
        faction_match = re.search(r'^\s*faction:\s*"([^"]+)"', block, re.MULTILINE)
        slot_match = re.search(r'^\s*slot:\s*"([^"]+)"', block, re.MULTILINE)
        addon_match = re.search(
            r"^\s*xwsaddon:\s*(?:\"([^\"]+)\"|'([^']+)')",
            block,
            re.MULTILINE,
        )
        skill_match = re.search(r"^\s*skill:\s*\d+", block, re.MULTILINE)

        kind = "upgrade" if slot_match and not skill_match else "pilot"
        base_name, suffix = split_variant(name)
        entries.append(
            {
                "name": name,
                "base_name": base_name,
                "root_name": strip_parentheticals(name),
                "suffix": suffix,
                "ship": ship_match.group(1) if ship_match else "",
                "faction": faction_match.group(1) if faction_match else "",
                "slot": slot_match.group(1) if slot_match else "",
                "xwsaddon": (
                    addon_match.group(1) or addon_match.group(2)
                    if addon_match
                    else ""
                ),
                "kind": kind,
                "points": int(points_match.group(1)),
                "loadout": int(loadout_match.group(1)) if loadout_match else None,
            }
        )
    return entries


def entry_identity(entry: dict) -> str:
    return "|".join(
        [
            entry["kind"],
            normalize(entry["name"]),
            normalize(entry["ship"]),
            normalize(entry["faction"]),
            normalize(entry["slot"]),
        ]
    )


def release_diff(old_text: str, new_text: str) -> list[dict]:
    """Card entries whose points/loadout changed between two YASB commits."""
    old_entries = {entry_identity(entry): entry for entry in parse_yasb(old_text)}
    new_entries = parse_yasb(new_text)
    changes: list[dict] = []
    for entry in new_entries:
        previous = old_entries.get(entry_identity(entry))
        if not previous:
            continue
        if previous["points"] != entry["points"]:
            changes.append(entry)
        elif (
            entry["loadout"] is not None
            and previous["loadout"] != entry["loadout"]
        ):
            changes.append(entry)
    return changes


def _dedupe(entries: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        identity = entry_identity(entry)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(entry)
    return unique


def _xws_suffix(xws: str) -> str:
    return normalize(xws.split("-", 1)[1]) if "-" in (xws or "") else ""


def _is_faction_label(suffix: str, faction: str) -> bool:
    """True when a parenthetical is just a faction tag, not a variant tag."""
    suffix_key = normalize(suffix)
    faction_key = normalize(faction)
    if not suffix_key or not faction_key:
        return False
    return suffix_key in faction_key or faction_key in suffix_key


def _suffix_matches_ship(suffix: str, ship: str) -> bool:
    """True when the parenthetical only describes the chassis.

    ``TIE Defender`` vs ``TIE/d Defender`` differs by the slash, so compare
    word tokens instead of raw substrings.
    """
    suffix_tokens = set(re.findall(r"[a-z0-9]+", suffix.lower()))
    ship_tokens = set(re.findall(r"[a-z0-9]+", ship.lower()))
    return bool(suffix_tokens) and suffix_tokens <= ship_tokens


def _ships_match(card_ship: str, entry_ship: str) -> bool:
    """Loose chassis equality for the no-exact-ship fallback.

    ``BTA-NR2 Y-Wing`` and YASB's ``BTA-NR2-W Y-wing`` are the same chassis;
    ``T-65 X-wing`` and ``RZ-1 A-wing`` are not. Compare word tokens instead
    of accepting any entry with the same pilot name.
    """
    card_key = normalize(card_ship)
    entry_key = normalize(entry_ship)
    if not card_key or not entry_key:
        return True
    if card_key == entry_key:
        return True
    card_tokens = set(re.findall(r"[a-z0-9]+", str(card_ship).lower()))
    entry_tokens = set(re.findall(r"[a-z0-9]+", str(entry_ship).lower()))
    return card_tokens <= entry_tokens or entry_tokens <= card_tokens


def _compatible(entry: dict, xws: str, faction: str) -> bool:
    """Can this YASB entry describe the card with this XWS id?"""
    xws_norm = normalize(xws)
    xws_suffix = _xws_suffix(xws)
    addon = normalize(entry["xwsaddon"])
    suffix = normalize(entry["suffix"])

    if addon:
        return xws_norm.endswith(addon)
    if suffix:
        if suffix in xws_suffix:
            return True
        if _suffix_matches_ship(entry["suffix"], entry["ship"]):
            return True
        # "Han Solo (Scum)" or "Bo-Katan Kryze (Republic)" just repeat the
        # faction; they are the plain version of a card that also has a
        # source-specific variant (AaD, SoC, ...).
        return _is_faction_label(entry["suffix"], faction)
    return not xws_suffix


def _choose(candidates: list[dict], xws: str, faction: str = "") -> dict | None:
    unique = _dedupe(candidates)
    compatible = [entry for entry in unique if _compatible(entry, xws, faction)]
    if not compatible:
        return None
    if len(compatible) == 1:
        return compatible[0]

    xws_norm = normalize(xws)
    xws_suffix = _xws_suffix(xws)

    # Strongest signal: YASB's xwsaddon is the suffix of our XWS id.
    addon_matches = [
        entry
        for entry in compatible
        if normalize(entry["xwsaddon"])
        and xws_norm.endswith(normalize(entry["xwsaddon"]))
    ]
    if len(addon_matches) == 1:
        return addon_matches[0]

    # Next: the YASB parenthetical variant is contained in our XWS suffix
    # (Delta-7b in anakinskywalker-delta7baethersprite, A-Wing in ...-rz1awing).
    variant_matches = [
        entry
        for entry in compatible
        if normalize(entry["suffix"]) and normalize(entry["suffix"]) in xws_suffix
    ]
    if len(variant_matches) == 1:
        return variant_matches[0]

    if not xws_suffix:
        plain = [entry for entry in compatible if not entry["suffix"]]
        if len(plain) == 1:
            return plain[0]

    return None


def build_pilot_index(entries: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    index: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for entry in entries:
        if entry["kind"] != "pilot":
            continue
        ship = normalize(entry["ship"])
        faction = normalize(entry["faction"])
        names = {
            normalize(entry["name"]),
            normalize(entry["base_name"]),
            normalize(entry["root_name"]),
        }
        for name in names:
            if not name:
                continue
            index[(name, ship, faction)].append(entry)
            if faction:
                index[(name, ship, "")].append(entry)
            # Fallback used only when the exact chassis does not match: YASB
            # sometimes spells the same chassis differently (BTA-NR2-W vs
            # BTA-NR2). ``_choose`` still requires an xwsaddon/variant hit, so
            # this cannot turn into a blind name match.
            index[(name, "", faction)].append(entry)
            index[(name, "", "")].append(entry)
    return index


def lookup_pilot(
    index: dict[tuple[str, str, str], list[dict]],
    pilot: dict,
    ship: str,
    faction: str,
) -> dict | None:
    """Match a pilot object using the enclosing file's ship and faction."""
    pilot_name = pilot.get("name") or ""
    names = [
        normalize(pilot_name),
        normalize(split_variant(pilot_name)[0]),
        normalize(strip_parentheticals(pilot_name)),
    ]
    ship_key = normalize(ship)
    faction_key = normalize(faction)

    for name in names:
        if not name:
            continue
        exact_keys = [(name, ship_key, faction_key)]
        if faction_key:
            exact_keys.append((name, ship_key, ""))
        broad_keys = [(name, "", faction_key)]
        if faction_key:
            broad_keys.append((name, "", ""))
        for keys in (exact_keys, broad_keys):
            for key in keys:
                candidates = index.get(key, [])
                if keys is broad_keys:
                    candidates = [
                        entry
                        for entry in candidates
                        if _ships_match(ship, entry["ship"])
                    ]
                if not candidates:
                    continue
                match = _choose(candidates, pilot.get("xws") or "", faction)
                if match:
                    return match
    return None


def build_upgrade_index(entries: list[dict]) -> dict[tuple[str, str], list[dict]]:
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for entry in entries:
        if entry["kind"] != "upgrade":
            continue
        slot = normalize(entry["slot"])
        names = {
            normalize(entry["name"]),
            normalize(entry["base_name"]),
            normalize(entry["root_name"]),
        }
        for name in names:
            if not name:
                continue
            index[(name, slot)].append(entry)
            index[(name, "")].append(entry)
    return index


def lookup_upgrade(
    index: dict[tuple[str, str], list[dict]], upgrade: dict
) -> dict | None:
    name = upgrade.get("name") or upgrade.get("xws") or ""
    sides = upgrade.get("sides") or []
    slot = ""
    if sides and isinstance(sides[0], dict):
        slot = normalize(sides[0].get("type"))

    names = [
        normalize(name),
        normalize(split_variant(name)[0]),
        normalize(strip_parentheticals(name)),
    ]
    for candidate_name in names:
        if not candidate_name:
            continue
        candidates = index.get((candidate_name, slot), [])
        if not candidates:
            candidates = index.get((candidate_name, ""), [])
        if not candidates:
            continue
        match = _choose(candidates, upgrade.get("xws") or "")
        if match:
            return match
    return None
