#!/usr/bin/env python3
"""Apply a YASB points release diff to this data repo.

Designed for Francespo/xwing-data2 (the XWA data source consumed by
xwing-unified-data). It does **not** mirror all current YASB values: the data
repo intentionally differs from YASB for some variants/loadouts. It fetches the
YASB file at the latest points-release commit and at its parent, diffs only the
points/loadout fields, and applies that release diff.

The workflow commits with:

    data: apply YASB 50P 2.2 points (2026-12-01)

The unified detector parses the version and release date from that subject.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
YASB_RAW = (
    "https://raw.githubusercontent.com/raithos/xwing/{sha}/"
    "coffeescripts/content/cards-common.coffee"
)
YASB_COMMITS = (
    "https://api.github.com/repos/raithos/xwing/commits"
    "?path=coffeescripts/content/cards-common.coffee&per_page=100"
)
TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


def fetch(url: str) -> str:
    headers = {"User-Agent": "xwing-data2-yasb-sync/1.0"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def parse_yasb(text: str) -> dict[str, dict]:
    """Parse pilots/upgrades keyed by a canonical identity."""
    entries: dict[str, dict] = {}

    for block in re.split(r'\n\s*name:\s*"', text)[1:]:
        name_match = re.match(r'^([^"]+)"', block)
        if not name_match:
            continue
        name = name_match.group(1)

        points_match = re.search(r"^\s*points:\s*(\d+)", block, re.MULTILINE)
        if not points_match:
            continue
        loadout_match = re.search(r"^\s*loadout:\s*(\d+)", block, re.MULTILINE)
        ship_match = re.search(r'^\s*ship:\s*"([^"]+)"', block, re.MULTILINE)
        faction_match = re.search(r'^\s*faction:\s*"([^"]+)"', block, re.MULTILINE)
        slot_match = re.search(r'^\s*slot:\s*"([^"]+)"', block, re.MULTILINE)

        kind = (
            "upgrade"
            if slot_match and not re.search(r"^\s*skill:\s*\d+", block, re.MULTILINE)
            else "pilot"
        )
        entry = {
            "name": name,
            "ship": ship_match.group(1) if ship_match else "",
            "faction": faction_match.group(1) if faction_match else "",
            "slot": slot_match.group(1) if slot_match else "",
            "kind": kind,
            "points": int(points_match.group(1)),
            "loadout": int(loadout_match.group(1)) if loadout_match else None,
        }
        identity = "|".join(
            [
                kind,
                normalize(name),
                normalize(entry["ship"]),
                normalize(entry["faction"]),
                normalize(entry["slot"]),
            ]
        )
        entries[identity] = entry

    return entries


def entry_keys(entry: dict) -> list[str]:
    base = normalize(entry["name"])
    keys = [base]
    ship = normalize(entry["ship"])
    faction = normalize(entry["faction"])
    slot = normalize(entry["slot"])

    if ship:
        keys.insert(0, f"{base}|{ship}")
        if faction:
            keys.insert(0, f"{base}|{ship}|{faction}")
    if entry["kind"] == "upgrade" and slot:
        keys.insert(0, f"{base}|{slot}|{ship}|{faction}")
        keys.insert(0, f"{base}|{slot}")
    return keys


def build_maps(entries: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    pilots: dict[str, dict] = {}
    upgrades: dict[str, dict] = {}
    for entry in entries:
        target = upgrades if entry["kind"] == "upgrade" else pilots
        for key in entry_keys(entry):
            target[key] = entry
    return pilots, upgrades


def lookup_pilot(pilots: dict[str, dict], pilot: dict) -> dict | None:
    name = normalize(pilot.get("name") or pilot.get("xws"))
    ship = normalize(pilot.get("ship"))
    faction = normalize(pilot.get("faction"))
    for key in (f"{name}|{ship}|{faction}", f"{name}|{ship}", name):
        if key in pilots:
            return pilots[key]
    return None


def lookup_upgrade(upgrades: dict[str, dict], upgrade: dict) -> dict | None:
    name = normalize(upgrade.get("name") or upgrade.get("xws"))
    sides = upgrade.get("sides") or []
    slot = ""
    if sides and isinstance(sides[0], dict):
        slot = normalize(sides[0].get("type"))
    for key in (f"{name}|{slot}", name):
        if key in upgrades:
            return upgrades[key]
    return None


def latest_release() -> tuple[str | None, str | None, str | None, str | None]:
    """Latest YASB points version, its date, commit SHA and parent SHA."""
    try:
        commits = json.loads(fetch(YASB_COMMITS))
    except Exception as exc:  # noqa: BLE001 - surfaced in the workflow log
        print(f"warning: cannot read YASB commit history: {exc}", file=sys.stderr)
        return None, None, None, None

    for commit in commits:
        subject = commit.get("commit", {}).get("message", "").splitlines()[0].strip()
        match = re.fullmatch(r"(\d+\.\d+(?:\.\d+)?)", subject)
        if not match:
            continue
        parents = commit.get("parents") or []
        parent_sha = parents[0]["sha"] if parents else None
        return (
            match.group(1),
            commit["commit"]["author"]["date"][:10],
            commit["sha"],
            parent_sha,
        )
    return None, None, None, None


def release_diff(old_text: str, new_text: str) -> list[dict]:
    old_entries = parse_yasb(old_text)
    new_entries = parse_yasb(new_text)
    changes: list[dict] = []
    for identity, entry in new_entries.items():
        previous = old_entries.get(identity)
        if not previous:
            continue
        if previous["points"] != entry["points"]:
            changes.append(entry)
        elif entry["loadout"] is not None and previous["loadout"] != entry["loadout"]:
            changes.append(entry)
    return changes


def write_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def apply_pilots(pilots: dict[str, dict], dry_run: bool) -> list[str]:
    changes: list[str] = []
    for path in sorted((DATA / "pilots").rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        dirty = False
        for pilot in data.get("pilots", []):
            match = lookup_pilot(pilots, pilot)
            if not match:
                continue
            if pilot.get("cost") != match["points"]:
                changes.append(
                    f"{pilot.get('xws') or pilot.get('name')}: cost "
                    f"{pilot.get('cost')} -> {match['points']}"
                )
                pilot["cost"] = match["points"]
                dirty = True
            if match["loadout"] is not None and pilot.get("loadout") != match["loadout"]:
                changes.append(
                    f"{pilot.get('xws') or pilot.get('name')}: loadout "
                    f"{pilot.get('loadout')} -> {match['loadout']}"
                )
                pilot["loadout"] = match["loadout"]
                dirty = True
        if dirty and not dry_run:
            write_json(path, data)
    return changes


def apply_upgrades(upgrades: dict[str, dict], dry_run: bool) -> list[str]:
    changes: list[str] = []
    for path in sorted((DATA / "upgrades").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and isinstance(data.get("upgrades"), list):
            items = data["upgrades"]
        else:
            continue

        dirty = False
        for upgrade in items:
            match = lookup_upgrade(upgrades, upgrade)
            if not match:
                continue
            new_cost = match["points"]
            cost = upgrade.get("cost")
            if isinstance(cost, dict):
                if cost.get("value") != new_cost:
                    changes.append(
                        f"{upgrade.get('xws') or upgrade.get('name')}: cost "
                        f"{cost.get('value')} -> {new_cost}"
                    )
                    cost["value"] = new_cost
                    dirty = True
            elif isinstance(cost, (int, float)):
                if cost != new_cost:
                    changes.append(
                        f"{upgrade.get('xws') or upgrade.get('name')}: cost "
                        f"{cost} -> {new_cost}"
                    )
                    upgrade["cost"] = new_cost
                    dirty = True
            elif cost is None:
                changes.append(
                    f"{upgrade.get('xws') or upgrade.get('name')}: cost "
                    f"missing -> {new_cost}"
                )
                upgrade["cost"] = {"value": new_cost}
                dirty = True
        if dirty and not dry_run:
            write_json(path, data)
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="YASB points version override, e.g. 2.2")
    parser.add_argument("--date", help="release date override (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--meta", type=Path, help="write JSON metadata for the workflow")
    args = parser.parse_args()

    release_version, release_date, sha, parent_sha = latest_release()
    version = args.version or release_version
    release_date = args.date or release_date

    if not sha or not parent_sha:
        print(
            "error: cannot resolve the latest YASB points release commit/parent",
            file=sys.stderr,
        )
        return 1

    old_text = fetch(YASB_RAW.format(sha=parent_sha))
    new_text = fetch(YASB_RAW.format(sha=sha))
    release_entries = release_diff(old_text, new_text)

    # A release diff should be small (the XWA 2.1.1 hotfix touched ~60 cards).
    # A larger diff means the parent is not the previous points release; refuse
    # to silently rewrite the whole data set.
    if len(release_entries) > 100:
        print(
            f"error: release diff has {len(release_entries)} entries; "
            "refusing to apply a non-release diff",
            file=sys.stderr,
        )
        return 1

    pilots, upgrades = build_maps(release_entries)
    pilot_changes = apply_pilots(pilots, args.dry_run)
    upgrade_changes = apply_upgrades(upgrades, args.dry_run)
    changes = pilot_changes + upgrade_changes

    if changes and (not version or not release_date):
        print(
            "error: points changed but no YASB release version/date was found; "
            "pass --version and --date explicitly",
            file=sys.stderr,
        )
        return 1

    message = (
        f"data: apply YASB 50P {version} points ({release_date})" if changes else ""
    )
    meta = {
        "changed": bool(changes),
        "version": version,
        "date": release_date,
        "release_sha": sha,
        "parent_sha": parent_sha,
        "release_entries": len(release_entries),
        "pilot_changes": len(pilot_changes),
        "upgrade_changes": len(upgrade_changes),
        "changes": changes[:50],
        "message": message,
        "dry_run": args.dry_run,
    }

    print(json.dumps(meta, indent=2))
    if args.meta:
        args.meta.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
