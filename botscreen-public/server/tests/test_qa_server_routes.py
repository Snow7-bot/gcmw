"""Regression smoke test for the existing qa_server.py entrypoint."""


def test_qa_server_has_expected_routes():
    import qa_server

    paths = {route.path for route in qa_server.app.routes}
    expected = {
        "/chat",
        "/suggestions",
        "/mic/wakeup",
        "/mic/hw_wakeup",
        "/mic/stop",
        "/mic/status",
        "/mic/notify_asr",
        "/sse",
        "/health",
    }
    assert expected.issubset(paths)
