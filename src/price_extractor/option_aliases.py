from __future__ import annotations


def build_option_value_aliases(site_name: str) -> dict[str, list[str]]:
    # Keys are normalized compact user inputs (lowercase, alnum only).
    aliases: dict[str, list[str]] = {
        "a5": ["a5", "a 5", "din a5", "din-a5", "format a5", "endformat din a5"],
        "a4": ["a4", "a 4", "din a4", "din-a4", "format a4", "endformat din a4"],
        "130gsm": [
            "130gsm",
            "130 g/m2",
            "130 g/m²",
            "130 g",
            "130 g bilderdruckpapier",
            "130 g/m² bilderdruckpapier",
            "bilderdruckpapier 130 g",
        ],
        "135gsm": ["135gsm", "135 g/m2", "135 g/m²", "135 g", "135 g bilderdruckpapier"],
        "170gsm": [
            "170gsm",
            "170 g/m2",
            "170 g/m²",
            "170 g",
            "170 g bilderdruckpapier",
        ],
    }

    # Site-specific expansions can be extended incrementally as evidence accumulates.
    if site_name in {"onlineprinters.de", "wir-machen-druck.de", "print24.com", "saxoprint.de"}:
        aliases.setdefault("a5", []).extend(["din a5 hoch", "din a5 quer", "endformat a5"])
        aliases.setdefault("130gsm", []).extend(["bilderdruck 130 g", "130 g gloss", "130 g matt"])

    return aliases
