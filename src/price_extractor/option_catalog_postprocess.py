"""Heuristic post-processing for bootstrap option_catalog rows.

Some configurators (notably Onlineprinters' radio-card UI for paper/material) render
each option as its own DOM block whose only visible label is the option text itself
("115 g/m² Bilderdruckpapier"). The in-browser extractor then can't find a parent
group label and produces one singleton-group-per-option. Pre-validation later finds
a "material" group whose label happens to contain the user's value, picks it, and
ends up with a catalog row whose form-field name/backendHints/href are all empty.

This module detects clusters of such singleton groups by pattern, merges them
under synthetic parent labels (the canonical option-key vocabulary), and produces
a catalog the synthesis adapter and pre-validation can work with.

The patterns are deliberately printing-industry-centric (paper, color, binding,
finishing, orientation) because that is the domain the framework targets. Adding
new clusters is one new entry in CLUSTER_PATTERNS.
"""

from __future__ import annotations

import re
from typing import Any

# Each entry: (canonical_label, compiled_regex, papier_context_from_match)
# papier_context lets us distinguish cover vs inner paper while still grouping them.
CLUSTER_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # Cover paper: "Umschlag 130 g/m² Bilderdruck..."
    (
        "material",
        re.compile(r"^\s*umschlag\s+\d+\s*g\s*/\s*m[²2]?", re.IGNORECASE),
        "umschlag",
    ),
    # Inner / generic paper: "130 g/m² Bilderdruckpapier", "100 g/m² Offsetpapier", etc.
    (
        "material",
        re.compile(r"^\s*\d+\s*g\s*/\s*m[²2]?\s*\S", re.IGNORECASE),
        "innen",
    ),
    # Color: "4/4-farbig", "1/0-farbig", "4/0 Euroskala"
    (
        "color",
        re.compile(r"^\s*\d+\s*/\s*\d+\s*(?:-?\s*farbig|\s+euroskala|\s+pantone|\s+hks)", re.IGNORECASE),
        "",
    ),
    # Binding: "Klammerheftung", "Klebebindung", "Spiralbindung", "Wire-O"
    (
        "binding",
        re.compile(r"\b(klammerheftung|klebebindung|spiralbindung|wire[-\s]?o|fadenheftung|ringbindung)\b", re.IGNORECASE),
        "",
    ),
    # Finishing: cellophanierung, UV-Lack, Heißfolienprägung, etc.
    (
        "finishing",
        re.compile(r"\b(cellophan|matt\s*folien|gl(?:a|ä)nz\s*folien|uv[-\s]*lack|hei(?:ß|ss)folien|pr(?:ä|a)gung)\b", re.IGNORECASE),
        "",
    ),
]


def _build_option_row(group_row: dict[str, Any]) -> dict[str, Any]:
    """Turn an option_groups summary row into an option row for catalog use.

    Carries forward the limited information available (visible label + control type)
    and explicitly leaves form-metadata fields empty so the synthesis adapter's
    `[adapter-injection-skipped]` diagnostic surfaces the gap loudly.
    """
    label = str(group_row.get("group") or "").strip()
    sample_values = list(group_row.get("sampleValues") or [])
    visible_value = str(sample_values[0]).strip() if sample_values else label
    control_types = list(group_row.get("controlTypes") or [])
    return {
        "visibleLabel": label,
        "visibleValue": visible_value,
        "selected": False,
        "controlTag": control_types[0] if control_types else "unknown",
        "controlType": control_types[0] if control_types else "unknown",
        "name": "",
        "backendHints": {},
        "sourceGroupLabel": label,
    }


def regroup_singleton_clusters(
    summary_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge singleton summary-groups whose labels match a known cluster pattern.

    Returns a list of synthetic catalog rows (one per detected cluster) with the
    canonical option-key as `groupLabel`. The catalog rows still have empty
    form-field metadata per option — this function fixes the *grouping* problem,
    not the *form-field-name* problem (which requires bootstrap-side improvements
    or runtime inference from captured network traces).
    """
    clusters: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unmatched: list[dict[str, Any]] = []

    for group_row in summary_groups:
        if not isinstance(group_row, dict):
            continue
        label = str(group_row.get("group") or "").strip()
        option_count = int(group_row.get("optionCount") or 0)
        # Only collapse singletons. Multi-option summary groups are already
        # structured correctly and don't fit the radio-card-per-option pattern.
        if option_count != 1 or not label:
            unmatched.append(group_row)
            continue

        matched = False
        for canonical, pattern, context in CLUSTER_PATTERNS:
            if pattern.search(label):
                clusters.setdefault((canonical, context), []).append(group_row)
                matched = True
                break
        if not matched:
            unmatched.append(group_row)

    synthesized: list[dict[str, Any]] = []
    for (canonical, context), rows in clusters.items():
        # Don't synthesize a parent for a single-member cluster — that's
        # indistinguishable from the original singleton and adds no value.
        if len(rows) < 2:
            unmatched.extend(rows)
            continue
        options = [_build_option_row(row) for row in rows]
        for option, source_row in zip(options, rows):
            if context:
                option["papierContext"] = context
            # Carry through anything else the summary recorded — controlTypes etc.
            attrs = dict(source_row)
            attrs.pop("group", None)
            attrs.pop("optionCount", None)
            attrs.pop("sampleValues", None)
            attrs.pop("controlTypes", None)
            for k, v in attrs.items():
                option.setdefault(k, v)

        group_label = canonical if not context else f"{canonical} ({context})"
        synthesized.append(
            {
                "groupLabel": group_label,
                "normalizedGroupLabel": group_label.lower(),
                "visibleGroupLabel": group_label,
                "controlTypes": sorted({opt["controlType"] for opt in options}),
                "options": options,
                "truncated": False,
                "backendHints": {},
                "sourceHints": {"synthesized": True, "cluster": canonical, "context": context or None},
                "_synthesizedFromSingletonCluster": True,
            }
        )

    # Carry through unmatched singletons in the original fallback shape, so the
    # rest of the pre-validation pipeline still sees them.
    carried: list[dict[str, Any]] = []
    for row in unmatched:
        label = str(row.get("group") or "").strip() or "unknown"
        sample_values = [str(v).strip() for v in list(row.get("sampleValues") or []) if str(v).strip()]
        carried.append(
            {
                "groupLabel": label,
                "normalizedGroupLabel": label.lower(),
                "visibleGroupLabel": label,
                "controlTypes": list(row.get("controlTypes") or []),
                "options": [
                    {
                        "visibleLabel": value,
                        "visibleValue": value,
                        "selected": False,
                        "controlTag": "unknown",
                        "controlType": "unknown",
                        "name": "",
                        "backendHints": {},
                    }
                    for value in sample_values
                ],
                "truncated": False,
                "backendHints": {},
                "sourceHints": {},
            }
        )

    return synthesized + carried
