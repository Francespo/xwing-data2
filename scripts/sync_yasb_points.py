#!/usr/bin/env python3
"""Apply a YASB points release diff to this data repo.

Designed for Francespo/xwing-data2 (the XWA data source consumed by
xwing-unified-data). It does **not** mirror all current YASB values: the data
repo intentionally differs from YASB for some variants/loadouts. It fetches the
YASB file at a points-release commit and at its parent, diffs only the
points/loadout fields, and applies that release diff.

The workflow commits with:

    data: apply YASB 50P 2.2 points (2026-12-01)

The unified detector parses the version and release date from that subject.

Matching lives in ``scripts/yasb_cards.py``. Pilots are matched using the
enclosing ship file's ship/faction, not just the pilot name, so YASB variants
such as ``Anakin Skywalker (Delta-7b)`` reach the right card. A release entry
that cannot be matched fails the run (``--allow-unmatched`` overrides) instead
of being skipped silently.

Historical releases can be backfilled explicitly::

    python3 scripts/sync_yasb_points.py \
        --sha <release-sha> --parent-sha <parent-sha> \
        --version 2.1 --date 2026-08-16
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from yasb_cards import (
    build_pilot_index,
    build_upgrade_index,
    entry_identity,
    lookup_pilot,
    lookup_upgrade,
    release_diff,
)

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


def write_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def apply_pilots(pilots: dict, dry_run: bool) -> tuple[list[str], set[str]]:
    changes: list[str] = []
    matched: set[str] = set()
    for path in sorted((DATA / "pilots").rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        ship = data.get("name") or data.get("xws") or ""
        faction = data.get("faction") or ""
        dirty = False
        for pilot in data.get("pilots", []):
            match = lookup_pilot(pilots, pilot, ship, faction)
            if not match:
                continue
            matched.add(entry_identity(match))
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
    return changes, matched


def apply_upgrades(upgrades: dict, dry_run: bool) -> tuple[list[str], set[str]]:
    changes: list[str] = []
    matched: set[str] = set()
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
            matched.add(entry_identity(match))
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
    return changes, matched


def _format_entry(entry: dict) -> str:
    suffix = f" ({entry['suffix']})" if entry.get("suffix") else ""
    return (
        f"{entry['kind']}: {entry['name']}{suffix} "
        f"[{entry.get('ship') or 'no ship'} / {entry.get('faction') or 'no faction'}] "
        f"{entry['points']}/{entry.get('loadout')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="YASB points version override, e.g. 2.2")
    parser.add_argument("--date", help="release date override (YYYY-MM-DD)")
    parser.add_argument("--sha", help="YASB release commit SHA (history backfill)")
    parser.add_argument("--parent-sha", help="parent SHA for --sha")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--meta", type=Path, help="write JSON metadata for the workflow")
    parser.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="warn instead of failing when a release entry has no data card",
    )
    args = parser.parse_args()

    if args.sha:
        if not args.parent_sha:
            print("error: --sha requires --parent-sha", file=sys.stderr)
            return 1
        sha, parent_sha = args.sha, args.parent_sha
    else:
        release_version, release_date, sha, parent_sha = latest_release()
        args.version = args.version or release_version
        args.date = args.date or release_date

    if not sha or not parent_sha:
        print(
            "error: cannot resolve the YASB points release commit/parent",
            file=sys.stderr,
        )
        return 1
    if args.sha and (not args.version or not args.date):
        print(
            "error: historical backfill requires --version and --date",
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

    pilot_changes, matched_pilots = apply_pilots(
        build_pilot_index(release_entries), args.dry_run
    )
    upgrade_changes, matched_upgrades = apply_upgrades(
        build_upgrade_index(release_entries), args.dry_run
    )
    changes = pilot_changes + upgrade_changes
    matched = matched_pilots | matched_upgrades
    unmatched = [
        entry for entry in release_entries if entry_identity(entry) not in matched
    ]

    if unmatched:
        print(
            f"warning: {len(unmatched)} YASB release entr"
            f"{'y' if len(unmatched) == 1 else 'ies'} did not match any data card:",
            file=sys.stderr,
        )
        for entry in unmatched:
            print(f"  - {_format_entry(entry)}", file=sys.stderr)
        if not args.allow_unmatched:
            print(
                "error: refusing a silent skip; fix the matcher or rerun with "
                "--allow-unmatched after reviewing the list",
                file=sys.stderr,
            )
            return 1

    if changes and (not args.version or not args.date):
        print(
            "error: points changed but no YASB release version/date was found; "
            "pass --version and --date explicitly",
            file=sys.stderr,
        )
        return 1

    message = (
        f"data: apply YASB 50P {args.version} points ({args.date})" if changes else ""
    )
    meta = {
        "changed": bool(changes),
        "version": args.version,
        "date": args.date,
        "release_sha": sha,
        "parent_sha": parent_sha,
        "release_entries": len(release_entries),
        "unmatched_entries": len(unmatched),
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
