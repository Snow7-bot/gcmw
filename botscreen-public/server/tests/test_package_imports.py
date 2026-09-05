"""Ensure the new backend module skeleton is importable."""


def test_app_package_imports():
    import app
    import app.agents
    import app.api
    import app.api.v1
    import app.contracts
    import app.memory
    import app.observability
    import app.orchestration
    import app.providers
    import app.rag
    import app.safety
    import app.storage
    import app.tools  # noqa: F401


def test_api_router_exists():
    from app.api.v1.router import router

    paths = {route.path for route in router.routes}
    assert "/api/v1/health/live" in paths
    assert "/api/v1/health/ready" in paths
