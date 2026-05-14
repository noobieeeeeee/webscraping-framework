from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.option_catalog_postprocess import regroup_singleton_clusters


def _summary(label: str, control_type: str = "checkbox") -> dict:
    return {
        "group": label,
        "controlTypes": [control_type],
        "optionCount": 1,
        "sampleValues": [label.rstrip("PEFCFSCBio").strip()],
    }


class TestPaperClusterMerging(unittest.TestCase):
    """The Onlineprinters bug: 158 singleton groups, each paper variant its own
    group with the paper label as the group label. Post-processing must merge
    them under one 'material' parent group so pre-validation picks an option
    inside the group instead of picking a singleton group by its label."""

    def test_inner_paper_singletons_merge_into_material_group(self) -> None:
        summary = [
            _summary("90 g/m² Bilderdruckpapier"),
            _summary("100 g/m² BilderdruckpapierPEFC"),
            _summary("115 g/m² BilderdruckpapierPEFC"),
            _summary("130 g/m² BilderdruckpapierPEFC"),
            _summary("150 g/m² BilderdruckpapierPEFC"),
            _summary("80 g/m² Offsetpapier"),
        ]
        result = regroup_singleton_clusters(summary)
        material = next(g for g in result if g["groupLabel"] == "material (innen)")
        labels = {opt["visibleLabel"] for opt in material["options"]}
        self.assertIn("115 g/m² BilderdruckpapierPEFC", labels)
        self.assertIn("80 g/m² Offsetpapier", labels)
        self.assertEqual(len(material["options"]), 6)
        self.assertTrue(material["sourceHints"]["synthesized"])
        self.assertEqual(material["sourceHints"]["cluster"], "material")

    def test_cover_paper_singletons_split_into_separate_material_cover_group(self) -> None:
        summary = [
            _summary("Umschlag 130 g/m² Bilderdruckpapier"),
            _summary("Umschlag 150 g/m² Bilderdruckpapier"),
            _summary("Umschlag 170 g/m² Bilderdruckpapier"),
            _summary("130 g/m² Bilderdruckpapier"),
            _summary("150 g/m² Bilderdruckpapier"),
        ]
        result = regroup_singleton_clusters(summary)
        groups_by_label = {g["groupLabel"]: g for g in result}
        self.assertIn("material (umschlag)", groups_by_label)
        self.assertIn("material (innen)", groups_by_label)
        cover = groups_by_label["material (umschlag)"]
        inner = groups_by_label["material (innen)"]
        self.assertEqual(len(cover["options"]), 3)
        self.assertEqual(len(inner["options"]), 2)
        for opt in cover["options"]:
            self.assertEqual(opt["papierContext"], "umschlag")
        for opt in inner["options"]:
            self.assertEqual(opt["papierContext"], "innen")

    def test_color_singletons_merge_into_color_group(self) -> None:
        summary = [
            _summary("1/0-farbig (schwarz)"),
            _summary("4/4-farbig Euroskala"),
            _summary("4/0-farbig Euroskala"),
        ]
        result = regroup_singleton_clusters(summary)
        color = next(g for g in result if g["groupLabel"] == "color")
        self.assertEqual(len(color["options"]), 3)

    def test_singleton_below_cluster_threshold_passes_through(self) -> None:
        """A single paper-shaped label is not enough to declare a cluster — leave
        it alone so multi-group sites aren't misgrouped from one stray entry."""
        summary = [_summary("130 g/m² Bilderdruckpapier")]
        result = regroup_singleton_clusters(summary)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["groupLabel"], "130 g/m² Bilderdruckpapier")
        self.assertNotIn("_synthesizedFromSingletonCluster", result[0])

    def test_non_singleton_summary_passes_through_unchanged_shape(self) -> None:
        summary = [
            {"group": "Auflage", "controlTypes": ["text"], "optionCount": 5, "sampleValues": ["1.000"]},
        ]
        result = regroup_singleton_clusters(summary)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["groupLabel"], "Auflage")

    def test_unrecognized_singleton_labels_pass_through(self) -> None:
        summary = [
            _summary("Standardlieferung"),
            _summary("Express"),
            _summary("Premium-Lieferung"),
        ]
        result = regroup_singleton_clusters(summary)
        self.assertEqual(len(result), 3)
        for g in result:
            self.assertNotIn("_synthesizedFromSingletonCluster", g)

    def test_mixed_summary_combines_synthesized_and_passthrough_groups(self) -> None:
        summary = [
            _summary("90 g/m² Bilderdruckpapier"),
            _summary("100 g/m² Bilderdruckpapier"),
            _summary("Standardlieferung"),
            {"group": "Auflage", "controlTypes": ["text"], "optionCount": 5, "sampleValues": ["1.000"]},
        ]
        result = regroup_singleton_clusters(summary)
        labels = {g["groupLabel"] for g in result}
        self.assertIn("material (innen)", labels)
        self.assertIn("Standardlieferung", labels)
        self.assertIn("Auflage", labels)


class TestEndToEndPrevalidationWithSyntheticCluster(unittest.TestCase):
    """Verify the synthetic group is picked correctly by the existing pre-validation
    chain in cli._prevalidate_requested_options."""

    def test_prevalidation_matches_paper_string_inside_synthetic_material_group(self) -> None:
        from price_extractor.cli import _prevalidate_requested_options

        bootstrap = {
            "option_catalog": [],
            "option_groups": [
                _summary("90 g/m² Bilderdruckpapier"),
                _summary("115 g/m² BilderdruckpapierPEFC"),
                _summary("130 g/m² BilderdruckpapierPEFC"),
                _summary("150 g/m² BilderdruckpapierPEFC"),
            ],
        }
        result = _prevalidate_requested_options(
            {"material": "115 g/m² Bilderdruckpapier"},
            bootstrap,
        )
        self.assertTrue(result["valid"], msg=str(result))
        matched = result["matched"]
        self.assertEqual(len(matched), 1)
        row = matched[0]
        self.assertEqual(row["key"], "material")
        self.assertIn("115", row["matchedLabel"])
        # The matched group label should be the synthetic parent, not the singleton's own label
        self.assertEqual(row["groupLabel"], "material (innen)")


if __name__ == "__main__":
    unittest.main()
