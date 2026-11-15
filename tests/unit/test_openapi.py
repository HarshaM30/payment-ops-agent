from payops.main import create_app


def test_openapi_security_scheme_and_examples() -> None:
    spec = create_app().openapi()
    assert spec["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
    assert "security" not in spec["paths"]["/health"]["get"]
    for path, method in [
        ("/runs", "post"),
        ("/runs/{run_id}", "get"),
        ("/approvals", "get"),
        ("/approvals/{approval_id}/approve", "post"),
        ("/approvals/{approval_id}/reject", "post"),
        ("/audit", "get"),
    ]:
        op = spec["paths"][path][method]
        assert op["security"] == [{"APIKeyHeader": []}], path
        assert "401" in op["responses"], path

    schemas = spec["components"]["schemas"]
    for name in [
        "RunCreate",
        "RunCreated",
        "RunDetail",
        "ApproveRequest",
        "RejectRequest",
        "ApprovalOut",
        "AuditPage",
    ]:
        assert schemas[name].get("examples"), f"{name} has no example"
