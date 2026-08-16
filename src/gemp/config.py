"""Runtime configuration, from environment with .env fallback.

The signing key is deliberately NOT given a default. A hard failure at startup is
correct: a system whose integrity guarantee silently depends on a well-known
development key is worse than one that refuses to start.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEMP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- integrity (F5) ---
    hmac_key: SecretStr = Field(
        description="Hex-encoded signing key. Injected as an env secret, never written "
                    "to the database volume, never committed."
    )

    # --- database ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "gemp"
    db_user: str = "gemp"
    db_password: SecretStr = SecretStr("")

    db_ro_user: str = "gemp_ro"
    db_ro_password: SecretStr = SecretStr("")

    # --- MQTT ---
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = "gemp"
    mqtt_password: SecretStr = SecretStr("")
    mqtt_topic_prefix: str = "gemp/reading"

    # --- simulation clock ---
    # 1 wall-second = sim_speed data-seconds. Every rolling window in the system is
    # evaluated in DATA time; using wall time would silently empty the anomaly
    # detector's 30-day window under accelerated replay.
    sim_speed: int = Field(default=720, gt=0)
    sim_interval_s: float = Field(default=10.0, gt=0)

    # --- ingestion ---
    ingest_batch_rows: int = Field(default=500, gt=0)
    ingest_batch_seconds: float = Field(default=2.0, gt=0)

    @field_validator("hmac_key")
    @classmethod
    def _key_must_be_real(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < 32:
            raise ValueError(
                "GEMP_HMAC_KEY is too short. Generate one with:\n"
                '  python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return value

    @property
    def key_bytes(self) -> bytes:
        raw = self.hmac_key.get_secret_value()
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return raw.encode("utf-8")

    @property
    def dsn(self) -> str:
        password = self.db_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.db_user}:{password}"
            f"@{self.resolved_db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def resolved_db_host(self) -> str:
        """`localhost` is rewritten to `127.0.0.1`, deliberately.

        Docker publishes the database as `127.0.0.1:5433`, which binds IPv4 only.
        Windows resolves `localhost` to `::1` first, so an IPv6 connection is
        attempted, hangs until the OS timeout, and only then falls back to IPv4.

        Measured on this stack: connecting via `localhost` took 130.3 seconds;
        via `127.0.0.1`, 0.2 seconds. The same query, a 650x difference, entirely
        in name resolution. It presents as "the database is unusably slow" rather
        than as a connection error, which is why it is worth removing here instead
        of leaving it for whoever next runs a script from the host.

        Containers are unaffected - they reach the database by service name on the
        compose network - so this only ever helps host-side tooling.
        """
        return "127.0.0.1" if self.db_host == "localhost" else self.db_host


@lru_cache
def get_settings() -> Settings:
    return Settings()
