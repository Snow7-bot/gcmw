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
    monkeypatch.setenv("GCMW_REDIS_URL", "redis://redis.internal:6379/0")
    monkeypatch.setenv(
        "GCMW_DATABASE_URL", "postgresql://gcmw:secret@db.internal:5432/gcmw"
    )
    monkeypatch.delenv("GCMW_CLOUD_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        Settings.from_env()
    assert "GCMW_CLOUD_API_KEY" in str(exc.value)


def test_production_with_cloud_secret_passes(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "cloud")
    monkeypatch.setenv("GCMW_CLOUD_API_KEY", "test-secret")
    monkeypatch.setenv("GCMW_REDIS_URL", "redis://redis.internal:6379/0")
    monkeypatch.setenv(
        "GCMW_DATABASE_URL", "postgresql://gcmw:secret@db.internal:5432/gcmw"
    )
    settings = Settings.from_env()
    assert settings.cloud.model == "qwen3.5-omni-plus-realtime"


def test_production_invalid_integer_raises(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "development")
    monkeypatch.setenv("GCMW_RUN_TIMEOUT_MS", "abc")
    with pytest.raises(ValueError):
        Settings.from_env()
