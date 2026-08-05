from pathlib import Path

from sagasmith_dnd_mcp.tool_profiles import PROFILE_COMBAT, PROFILE_PLAY, profiles_for_tool


def test_mcp_runtime_has_no_creature_specific_execution_contracts() -> None:
    source_root = Path(__file__).parents[1] / "src" / "sagasmith_dnd_mcp"
    source = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in source_root.rglob("*.py")
    )
    forbidden = {
        "dnd5e.core.monster",
        "gazer_eye_ray",
        "intellect_devourer",
        "frightful_presence",
        "body_thief",
        "death_burst",
        "pack_tactics",
        "sunlight_sensitivity",
        "wing_attack_2014",
        "heated_body",
        "corrosive_form",
        "detach_attachment",
        "source_traits",
        "critical_followup",
        "anatomical_loss",
        "inside_host",
        "on_hit_condition",
        "on_hit_save_condition",
        "source_ongoing_damage",
        "reviewed_on_hit_escape_checks",
        "release_unavailable_source_grapples",
    }

    assert not {token for token in forbidden if token in source}


def test_content_solutions_can_be_persisted_at_first_use() -> None:
    profiles = profiles_for_tool("content_solution")

    assert PROFILE_PLAY in profiles
    assert PROFILE_COMBAT in profiles


def test_encounter_driver_has_no_creature_specific_facade_paths() -> None:
    driver = (
        Path(__file__).parents[1] / "scripts" / "regression_encounter.py"
    ).read_text(encoding="utf-8").casefold()
    forbidden = {
        "source_trait_json",
        "source_on_hit_ruling_json",
        "source_extra_damage_ruling_json",
        "source_random_activity_json",
        "source_save_activity_json",
        "source_contest_activity_json",
        "source_attack_environment_json",
        "source_casualty_pool_json",
        "agent_death_trigger_ruling_json",
        "body_thief",
        "death_burst",
        "detach_attachment",
        "inside_host",
        "source_zero_hp_finisher",
        "source_zero_hp_stabilization",
        "douse the troll",
        "yawning portal step forward to stabilize",
        "end_source_ongoing_damage",
        "on_hit_condition",
        "on_hit_save_condition",
        "source_ongoing_damage",
        "_postcombat_unavailable_grapple_effect_ids",
    }

    assert not {token for token in forbidden if token in driver}
