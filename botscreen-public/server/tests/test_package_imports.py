"""Ensure the new backend module skeleton is importable."""


def test_app_package_imports():
    import app  # noqa: F401
    import app.agents  # noqa: F401
    import app.api  # noqa: F401
    import app.api.v1  # noqa: F401
    import app.contracts  # noqa: F401
    import app.memory  # noqa: F401
    import app.observability  # noqa: F401
    import app.orchestration  # noqa: F401
    import app.providers  # noqa: F401
    import app.rag  # noqa: F401
    import app.safety  # noqa: F401
    import app.storage  # noqa: F401
    import app.tools  # noqa: F401


def test_api_router_exists():
    from app.api.v1.router import router

    paths = {route.path for route in router.routes}
    assert "/api/v1/health/live" in paths
    assert "/api/v1/health/ready" in paths
