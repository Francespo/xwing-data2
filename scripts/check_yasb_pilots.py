#!/usr/bin/env python3
"""Reconcile every xwing-data2 pilot with the YASB cards-common snapshot.

This is the safety net for the release-diff sync: it walks *all* pilots, matches
them through ``scripts/yasb_cards.py`` and fails when a pilot is missing from
YASB or still has a stale cost/loadout. A silent skip in the release bot shows
up here immediately.

Usage::

    python3 scripts/check_yasb_pilots.py                 # latest YASB release
    python3 scripts/check_yasb_pilots.py --sha <sha>     # historical snapshot
    python3 scripts/check_yasb_pilots.py --fix           # apply the differences

Standard-loadout cards have no loadout value in YASB; xwing-data2 stores 0 for
them, so a non-zero loadout on a ``standard`` pilot is reported as well.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from sync_yasb_points import apply_pilots, fetch, latest_release
from yasb_cards import build_pilot_index, lookup_pilot, parse_yasb

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
YASB_RAW = (
    "https://raw.githubusercontent.com/raithos/xwing/{sha}/"
    "coffeescripts/content/cards-common.coffee"
)


def iter_pilot_files():
    for path in sorted((DATA / "pilots").rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        yield path, data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", help="YASB commit SHA (default: latest release)")
    parser.add_argument("--version", help="YASB points version label")
    parser.add_argument("--date", help="YASB release date (YYYY-MM-DD)")
    parser.add_argument("--fix", action="store_true", help="write the corrections")
    args = parser.parse_args()

    if args.sha:
        sha = args.sha
    else:
        version, date, sha, _ = latest_release()
        args.version = args.version or version
        args.date = args.date or date
    if not sha:
        print("error: cannot resolve a YASB commit", file=sys.stderr)
        return 1

    text = fetch(YASB_RAW.format(sha=sha))
    entries = [entry for entry in parse_yasb(text) if entry["kind"] == "pilot"]
    index = build_pilot_index(entries)

    problems: list[tuple[Path, dict, dict | None, str]] = []
    for path, data in iter_pilot_files():
        ship = data.get("name") or data.get("xws") or ""
        faction = data.get("faction") or ""
        for pilot in data.get("pilots", []):
            match = lookup_pilot(index, pilot, ship, faction)
            if not match:
                problems.append((path, pilot, None, "no YASB match"))
                continue
            if pilot.get("cost") != match["points"]:
                problems.append(
                    (
                        path,
                        pilot,
                        match,
                        f"cost {pilot.get('cost')} != {match['points']}",
                    )
                )
            if match["loadout"] is not None and pilot.get("loadout") != match["loadout"]:
                problems.append(
                    (
                        path,
                        pilot,
                        match,
                        f"loadout {pilot.get('loadout')} != {match['loadout']}",
                    )
                )
            if (
                match["loadout"] is None
                and pilot.get("standard")
                and (pilot.get("loadout") or 0) != 0
            ):
                problems.append(
                    (
                        path,
                        pilot,
                        match,
                        f"standard-loadout card has loadout {pilot.get('loadout')} != 0",
                    )
                )

    label = " ".join(part for part in (args.version, args.date) if part) or sha[:8]
    if not problems:
        print(f"OK: every xwing-data2 pilot matches YASB {label}")
        return 0

    print(f"{len(problems)} pilot mismatch(es) against YASB {label}:", file=sys.stderr)
    for path, pilot, match, reason in problems:
        target = f" -> {match['name']}" if match else ""
        print(
            f"  {pilot.get('xws') or pilot.get('name')}{target}: {reason}",
            file=sys.stderr,
        )

    if not args.fix:
        return 1

    changes, _ = apply_pilots(index, dry_run=False)
    # YASB omits loadout on standard-loadout cards; the data repo stores 0.
    for path, pilot, match, reason in problems:
        if reason.startswith("standard-loadout card"):
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("pilots", []):
                if item.get("xws") == pilot.get("xws"):
                    item["loadout"] = 0
                    path.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    changes.append(f"{pilot.get('xws')}: loadout -> 0")
                    break

    print(f"Fixed {len(changes)} pilot field(s):")
    for change in changes:
        print(f"  {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
