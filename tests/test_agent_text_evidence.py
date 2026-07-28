from sagasmith_dnd_mcp.server import (
    _agent_evidence_supports_fact,
    _compact_agent_evidence,
)


def test_agent_evidence_allows_bounded_ocr_repairs() -> None:
    evidence = _compact_agent_evidence(
        "Ice Walk. Difficult terrain composed of ice or snow doesn't cost it "
        "extra moment. Legendary Resistance (3JDay)."
    )

    assert _agent_evidence_supports_fact(
        _compact_agent_evidence(
            "Ice Walk. Difficult terrain composed of ice or snow doesn't cost it "
            "extra movement."
        ),
        evidence,
    )
    assert _agent_evidence_supports_fact(
        _compact_agent_evidence("Legendary Resistance (3/Day)."),
        evidence,
    )


def test_agent_evidence_rejects_changed_numbers_and_rule_terms() -> None:
    evidence = _compact_agent_evidence(
        "Club. Hit: 2 (1d4) bludgeoning damage. Cold Breath."
    )

    assert not _agent_evidence_supports_fact(
        _compact_agent_evidence("Club. Hit: 99 (1d4) bludgeoning damage."),
        evidence,
    )
    assert not _agent_evidence_supports_fact(
        _compact_agent_evidence("Club. Hit: 2 (1d4) fire damage."),
        evidence,
    )
