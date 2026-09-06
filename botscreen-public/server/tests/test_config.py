import pytest
from pydantic import ValidationError

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


def test_production_cloud_rejected_even_with_secret(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "cloud")
    monkeypatch.setenv("GCMW_CLOUD_API_KEY", "test-secret")
    monkeypatch.setenv("GCMW_REDIS_URL", "redis://redis.internal:6379/0")
    monkeypatch.setenv(
        "GCMW_DATABASE_URL", "postgresql://gcmw:secret@db.internal:5432/gcmw"
    )
    with pytest.raises(RuntimeError) as exc:
        Settings.from_env()
    assert "GCMW_ACTIVE_PROVIDER" in str(exc.value)


def test_production_local_valid_passes(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "local")
    monkeypatch.setenv("GCMW_REDIS_URL", "rediss://redis.internal:6379/0")
    monkeypatch.setenv(
        "GCMW_DATABASE_URL", "postgresql://gcmw:secret@db.internal:5432/gcmw"
    )
    settings = Settings.from_env()
    assert settings.active_provider == "local"


def test_production_invalid_integer_raises(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "development")
    monkeypatch.setenv("GCMW_RUN_TIMEOUT_MS", "abc")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_production_localhost_variants_rejected(monkeypatch):
    from app.config import _is_loopback_url

    assert _is_loopback_url("redis://LOCALHOST:6379/0")
    assert _is_loopback_url("postgresql://user@127.0.0.1:5432/db")
    assert _is_loopback_url("postgresql://user@[::1]:5432/db")
    assert not _is_loopback_url("redis://redis.internal:6379/0")


def test_invalid_modalities_rejected(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "development")
    monkeypatch.setenv("GCMW_CLOUD_MODALITIES", "text,image,not-a-modal")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_nonpositive_timeout_rejected(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "development")
    monkeypatch.setenv("GCMW_MODEL_TIMEOUT_MS", "0")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_environment_typo_rejected(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "prod")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize(
    "redis,db",
    [
        ("redis://LOCALHOST:6379/0", "postgresql://u:p@db.internal:5432/db"),
        ("redis://127.0.0.1:6379/0", "postgresql://u:p@db.internal:5432/db"),
        ("redis://127.1.2.3:6379/0", "postgresql://u:p@db.internal:5432/db"),
        ("redis://[::1]:6379/0", "postgresql://u:p@db.internal:5432/db"),
        ("not-a-url", "postgresql://u:p@db.internal:5432/db"),
        ("redis:///tmp/redis.sock", "postgresql://u:p@db.internal:5432/db"),
    ],
)
def test_production_storage_url_rejected_via_settings(monkeypatch, redis, db):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "local")
    monkeypatch.setenv("GCMW_REDIS_URL", redis)
    monkeypatch.setenv("GCMW_DATABASE_URL", db)
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_production_valid_remote_storage_passes(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "local")
    monkeypatch.setenv("GCMW_REDIS_URL", "rediss://redis.internal:6379/0")
    monkeypatch.setenv(
        "GCMW_DATABASE_URL", "postgresql://gcmw:secret@db.internal:5432/gcmw"
    )
    settings = Settings.from_env()
    assert settings.environment == "production"


def test_production_rejects_wrong_scheme_and_trailing_dot_localhost(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "local")
    monkeypatch.setenv("GCMW_REDIS_URL", "http://localhost.:6379/0")
    monkeypatch.setenv("GCMW_DATABASE_URL", "mysql://gcmw:secret@db.internal:5432/gcmw")
    with pytest.raises(RuntimeError) as exc:
        Settings.from_env()
    message = str(exc.value)
    assert "GCMW_REDIS_URL" in message
    assert "GCMW_DATABASE_URL" in message


def test_direct_settings_production_validation_enforced():
    with pytest.raises(RuntimeError):
        Settings(
            environment="production",
            active_provider="mock",
            redis_url="redis://127.0.0.1:6379/0",
            database_url="postgresql://gcmw:gcmw@127.0.0.1:5432/gcmw",
        )


def test_invalid_boolean_raises(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "development")
    monkeypatch.setenv("GCMW_DEBUG", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "redis://::ffff:127.0.0.1:6379/0",
        "redis://2130706433:6379/0",
        "redis://localhost.:6379/0",
        "redis://127.0.0.2:6379/0",
    ],
)
def test_production_rejects_ipv4_mapped_decimal_and_trailing_dot(monkeypatch, url):
    monkeypatch.setenv("GCMW_ENV", "production")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "local")
    monkeypatch.setenv("GCMW_REDIS_URL", url)
    monkeypatch.setenv(
        "GCMW_DATABASE_URL", "postgresql://gcmw:secret@db.internal:5432/gcmw"
    )
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_development_cloud_missing_key_fails(monkeypatch):
    monkeypatch.setenv("GCMW_ENV", "development")
    monkeypatch.setenv("GCMW_ACTIVE_PROVIDER", "cloud")
    monkeypatch.delenv("GCMW_CLOUD_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        Settings.from_env()
    assert "GCMW_CLOUD_API_KEY" in str(exc.value)


def test_settings_is_frozen():
    with pytest.raises(ValidationError):
        settings = Settings()
        settings.environment = "production"
