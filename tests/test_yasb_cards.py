import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import yasb_cards as yasb  # noqa: E402


YASB_TEXT = '''
        {
            name: "Anakin Skywalker (Delta-7b)"
            ship: "Delta-7b Aethersprite"
            faction: "Galactic Republic"
            skill: 6
            points: 17
            loadout: 14
        }
        {
            name: "Anakin Skywalker"
            ship: "Delta-7 Aethersprite"
            faction: "Galactic Republic"
            skill: 6
            points: 13
            loadout: 8
        }
        {
            name: '"Klick"'
            ship: "Nimbus-class V-wing"
            faction: "Galactic Republic"
            skill: 4
            points: 10
            loadout: 17
        }
        {
            name: "Poe Dameron (HoH)"
            xwsaddon: 'swz68'
            ship: "T-70 X-wing"
            faction: "Resistance"
            skill: 6
            points: 15
            loadout: 13
        }
        {
            name: "Han Solo (Scum)"
            ship: "Customized YT-1300 Light Freighter"
            faction: "Scum and Villainy"
            skill: 6
            points: 14
            loadout: 16
        }
        {
            name: "Zorii Bliss (Wartime)"
            xwsaddon: "wartime"
            ship: "BTA-NR2-W Y-wing"
            faction: "Resistance"
            skill: 5
            points: 11
            loadout: 20
        }
        {
            name: "Wedge Antilles (A-Wing)"
            ship: "RZ-1 A-wing"
            faction: "Rebel Alliance"
            skill: 6
            points: 9
            loadout: 6
        }
'''


class YasbCardsTest(unittest.TestCase):
    def setUp(self):
        self.entries = yasb.parse_yasb(YASB_TEXT)
        self.index = yasb.build_pilot_index(self.entries)

    def lookup(self, name, xws, ship, faction):
        return yasb.lookup_pilot(
            self.index, {"name": name, "xws": xws}, ship, faction
        )

    def test_parses_single_quoted_name_and_xwsaddon(self):
        klick = next(e for e in self.entries if "Klick" in e["name"])
        self.assertEqual(klick["points"], 10)
        poe = next(e for e in self.entries if e["name"] == "Poe Dameron (HoH)")
        self.assertEqual(poe["xwsaddon"], "swz68")

    def test_matches_delta7b_variant_by_ship(self):
        match = self.lookup(
            "Anakin Skywalker",
            "anakinskywalker-delta7baethersprite",
            "Delta-7B Aethersprite",
            "galacticrepublic",
        )
        self.assertIsNotNone(match)
        self.assertEqual((match["points"], match["loadout"]), (17, 14))
        self.assertEqual(match["suffix"], "Delta-7b")

    def test_does_not_match_delta7b_entry_to_plain_delta7(self):
        match = self.lookup(
            "Anakin Skywalker",
            "anakinskywalker",
            "Delta-7 Aethersprite",
            "galacticrepublic",
        )
        self.assertIsNotNone(match)
        self.assertEqual((match["points"], match["loadout"]), (13, 8))

    def test_matches_nickname_with_single_quotes(self):
        match = self.lookup(
            "“Klick”", "klick", "Nimbus-class V-wing", "galacticrepublic"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["points"], 10)

    def test_xwsaddon_disambiguates_poe_variants(self):
        self.assertIsNone(
            self.lookup("Poe Dameron", "poedameron", "T-70 X-wing", "resistance")
        )
        match = self.lookup(
            "Poe Dameron", "poedameron-swz68", "T-70 X-wing", "resistance"
        )
        self.assertIsNotNone(match)
        self.assertEqual((match["points"], match["loadout"]), (15, 13))

    def test_faction_suffix_maps_to_plain_card(self):
        match = self.lookup(
            "Han Solo",
            "hansolo",
            "Customized YT-1300 Light Freighter",
            "scumandvillainy",
        )
        self.assertIsNotNone(match)
        self.assertEqual((match["points"], match["loadout"]), (14, 16))

    def test_xwsaddon_bridges_spelling_difference_in_ship(self):
        match = self.lookup(
            "Zorii Bliss",
            "zoriibliss-wartime",
            "BTA-NR2 Y-Wing",
            "resistance",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["points"], 11)
        self.assertEqual(match["suffix"], "Wartime")

    def test_ship_mismatch_does_not_match_other_chassis(self):
        self.assertIsNone(
            self.lookup("Wedge Antilles", "wedgeantilles", "T-65 X-wing", "rebelalliance")
        )

    def test_release_diff_detects_delta7b_change(self):
        old = YASB_TEXT.replace("points: 17", "points: 16").replace(
            "loadout: 14", "loadout: 12"
        )
        changes = yasb.release_diff(old, YASB_TEXT)
        vader = [e for e in changes if e["name"] == "Anakin Skywalker (Delta-7b)"]
        self.assertEqual(len(vader), 1)
        self.assertEqual(vader[0]["points"], 17)


if __name__ == "__main__":
    unittest.main()
