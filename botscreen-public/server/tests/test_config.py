import os

import pytest

from app.config import Settings


def test_from_env_defaults():
    settings = Settings.from_env()
    assert settings.active_provider == "mock"
    assert "video" in settings.cloud.modalities
    assert settings.run_timeout_ms > 0


def test_production_missing_cloud_secret_fails(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "cloud")
    monkeypatch.delenv("GCMW_CLOUD_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_production_with_cloud_secret_passes(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "cloud")
    monkeypatch.setenv("GCMW_CLOUD_API_KEY", "test-secret")
    settings = Settings.from_env()
    assert settings.cloud.model == "qwen3.5-omni-plus-realtime"
