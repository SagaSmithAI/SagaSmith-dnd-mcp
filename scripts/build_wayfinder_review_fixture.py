"""Normalize the Wayfinder addon review against the public import layout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "books_catalog_review_v1.json"
BOOK = "D&D 5E - Wayfinders Guide to Eberron.pdf"


def _page(page_number: int) -> list[dict[str, Any]]:
    """Bind a reviewed card to every nonempty chunk on its exact source page."""

    return [{"page_start": page_number, "match_all": True}]


def main() -> None:
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    review = manifest["documents"][BOOK]
    # Printed typos remain literal source evidence. Structured cards use the
    # stronger display headings and page identity instead of rewriting the PDF
    # transcript as though those typos were OCR damage.
    review.pop("text_reviews", None)

    decisions = review["decisions"]
    obsolete_decisions = {
        ("feature", "Source fragment: FEAT: REVENANT BLADE (p. 74)"),
        ("feature", "CHAPTER 4 | THE MARK OF WARDING"),
    }
    decisions[:] = [
        item for item in decisions if (item["kind"], item["name"]) not in obsolete_decisions
    ]
    decision_updates = {
        ("background", "BACKGROUND"): "HOUs E AGENT",
        ("species", "CHANGELING NAMES"): "CHANg ELINg",
    }
    for item in decisions:
        item["name"] = decision_updates.get((item["kind"], item["name"]), item["name"])
    if not any(
        item["kind"] == "species" and item["name"] == "KALASHTAR QUIRKS"
        for item in decisions
    ):
        decisions.append(
            {
                "kind": "species",
                "name": "KALASHTAR QUIRKS",
                "status": "rejected",
                "note": (
                    "This OCR-attached names-and-quirks fragment is not a species card; "
                    "the exact page-reviewed Kalashtar addition replaces it."
                ),
            }
        )
    review["expected_counts"]["item"] = 16

    additions = {
        (item["kind"], item["name"]): item for item in review["additions"]
    }
    house_agents = [
        item
        for (kind, name), item in additions.items()
        if kind == "background" and name.startswith("House Agent (")
    ]
    if len(house_agents) != 13:
        raise RuntimeError("Wayfinder must define exactly thirteen House Agent cards")
    for item in house_agents:
        item["source_selectors"][0]["heading_exact"] = "HOUSE AGENT"

    additions[("species", "Kalashtar")]["source_selectors"] = _page(63)
    for name in (
        "Shifter (Beasthide)",
        "Shifter (Longtooth)",
        "Shifter (Swiftstride)",
        "Shifter (Wildhunt)",
    ):
        item = additions[("species", name)]
        item["source_selectors"] = [_page(65)[0], item["source_selectors"][1]]
    for name in (
        "Warforged (Envoy)",
        "Warforged (Juggernaut)",
        "Warforged (Skirmisher)",
    ):
        item = additions[("species", name)]
        item["source_selectors"][0] = _page(68)[0]

    mark_pages = {
        "Mark of Detection": 96,
        "Mark of Finding (Human)": 97,
        "Mark of Finding (Half-Orc)": 97,
        "Mark of Handling": 98,
        "Mark of Healing": 99,
        "Mark of Hospitality": 100,
        "Mark of Making": 101,
        "Mark of Passage": 102,
        "Mark of Scribing": 103,
        "Mark of Sentinel": 104,
        "Mark of Shadow": 105,
        "Mark of Storm": 106,
        "Mark of Warding": 108,
    }
    for name, page_number in mark_pages.items():
        additions[("species", name)]["source_selectors"] = _page(page_number)
    additions[("species", "Mark of Handling")]["note"] = (
        "The printed body says 'Mark of Handing' once; the display and trait headings "
        "establish the structured Mark of Handling identity without rewriting the source."
    )
    additions[("species", "Mark of Storm")]["note"] = (
        "The printed body retains a copied 'Mark of Detection' sentence; the display, "
        "trait, and footer headings establish the structured Mark of Storm identity."
    )

    additions[("feat", "Aberrant Dragonmark")]["source_selectors"] = [
        {
            "heading_exact": "FEAT: ABERRANT DRAGONMARK",
            "page_start": 112,
        }
    ]

    FIXTURE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
