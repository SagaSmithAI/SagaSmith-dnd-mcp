from sagasmith_dnd_mcp.server import SessionExposureFastMCP


def test_campaign_query_nested_payload_routes_host_context_binding() -> None:
    campaign_id = SessionExposureFastMCP._result_campaign_id(
        "campaign_query",
        ([], {"status": "ok", "result": {"id": "campaign-1"}}),
        {"view": "get", "payload": {"campaign_id": "campaign-1"}},
    )

    assert campaign_id == "campaign-1"


def test_campaign_query_get_result_routes_host_context_binding() -> None:
    campaign_id = SessionExposureFastMCP._result_campaign_id(
        "campaign_query",
        ([], {"status": "ok", "result": {"id": "campaign-2"}}),
        {"view": "get"},
    )

    assert campaign_id == "campaign-2"
