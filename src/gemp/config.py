"""Runtime configuration, from environment with .env fallback.

The signing key is deliberately NOT given a default. A hard failure at startup is
correct: a system whose integrity guarantee silently depends on a well-known
development key is worse than one that refuses to start.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

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

    # A CEILING on that clock, and the reason the acceleration above is safe to
    # leave switched on.
    #
    # `sim_speed` with nothing above it is a one-way ratchet: the simulator
    # resumes from the newest stored reading, so a restart continues the climb
    # rather than resetting it. At 720x one wall day is two data years, and a
    # deployment left running for ten days had readings dated 2047 - twenty years
    # ahead of the wall clock, on top of a database that had grown to 35.7 million
    # rows because of it.
    #
    # With this on, the acceleration only ever spends itself catching UP: the
    # clock runs fast through backfill until it reaches the present and then
    # advances at real time, so data time converges on wall time instead of
    # diverging from it. Turn it off only for a deliberate long-horizon replay,
    # and expect to re-seed afterwards.
    sim_clamp_to_wall_clock: bool = True

    # --- ingestion ---
    ingest_batch_rows: int = Field(default=500, gt=0)
    ingest_batch_seconds: float = Field(default=2.0, gt=0)

    # --- sessions ---
    # `Secure` keeps the session cookie off plaintext connections. It defaults to
    # FALSE because the offline demonstration is served over http on localhost and a
    # Secure cookie is simply never sent there - the login would appear to succeed and
    # then every request would come back anonymous, which is a miserable thing to
    # debug ten minutes before a presentation.
    #
    # Any deployment reachable over a network must set this true and terminate TLS.
    # `/health` reports the current value so the mistake is visible rather than
    # silent.
    cookie_secure: bool = False

    # The interactive API documentation and the OpenAPI schema behind it. Mounted
    # they hand any caller who can reach them the complete endpoint inventory, every
    # parameter shape and the role each route demands - a reconnaissance map, worth
    # exactly nothing to a demonstration audience.
    #
    # Off by default, so a deployment is closed unless somebody opens it. Set
    # GEMP_API_DOCS=true while developing against the API; note that even mounted
    # they are NOT anonymous - the middleware requires a session for them, because
    # anything worth building against needs an account anyway.
    api_docs: bool = False

    # --- alerting (F11) ---
    # The review's alerting fix is a row in the anomaly table, a marker on the map,
    # and an optional outbound webhook. Unset means the path is inert, which is the
    # demonstration default: nobody reads email or watches a chat channel during a
    # presentation. Environment only, never settable through the API - a notification
    # target that any caller could change is an exfiltration primitive.
    webhook_url: str = ""
    # "high" rather than "critical", so that a stuck meter is forwarded. A flatline is
    # a CERTAIN fault - the meter is broken or the plant is jammed on - but its score
    # is a sentinel graded to "high", not a measured excursion, so a critical-only
    # filter would silently drop the entire fault class. Forwarded: extreme residual
    # excursions and certain hardware faults. Suppressed: marginal residual blips.
    webhook_min_severity: str = "high"

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
        # quote_plus, not raw interpolation: a password containing `@`, `/` or `:`
        # otherwise re-parses as host/port/user separators, silently aiming the
        # connection somewhere else or failing it. Generated passwords are hex, but
        # nothing stops an operator choosing one that isn't.
        password = self.db_password.get_secret_value()
        return (
            f"postgresql+psycopg://{quote_plus(self.db_user)}:{quote_plus(password)}"
            f"@{self.resolved_db_host}:{self.db_port}/{quote_plus(self.db_name)}"
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
