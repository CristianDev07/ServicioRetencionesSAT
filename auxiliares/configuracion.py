"""Configuración del proyecto refactorizado.

Paso 1: carga las variables del archivo ``.env`` ubicado en la raíz.
Paso 2: valida la conexión, las rutas y los parámetros del SAT.
Paso 3: entrega objetos de configuración inmutables al resto del proceso.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = BASE_DIR / ".env"


class ConfigurationError(RuntimeError):
    """Indica que la configuración requerida es inválida o está incompleta."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    driver: str
    server: str
    database: str
    trusted_connection: bool
    username: str | None
    password: str | None = field(repr=False)
    encrypt: bool
    trust_server_certificate: bool

    @property
    def connection_string(self) -> str:
        driver = self.driver.strip("{}")
        if self.trusted_connection:
            autenticacion = "Trusted_Connection=yes;"
        else:
            if not self.username or self.password is None:
                raise ConfigurationError(
                    "La autenticación SQL requiere DB_USER y DB_PASSWORD"
                )
            autenticacion = (
                f"UID={_odbc_value(self.username)};"
                f"PWD={_odbc_value(self.password)};"
                "Trusted_Connection=no;"
            )

        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={_odbc_value(self.server)};"
            f"DATABASE={_odbc_value(self.database)};"
            f"{autenticacion}"
            f"Encrypt={_to_odbc_bool(self.encrypt)};"
            "TrustServerCertificate="
            f"{_to_odbc_bool(self.trust_server_certificate)};"
        )


@dataclass(frozen=True, slots=True)
class PathSettings:
    root_dir: Path
    process_dir: Path
    sat_pending_dir: Path
    sat_processed_dir: Path
    sat_error_dir: Path
    manual_pending_dir: Path
    manual_processed_dir: Path
    manual_error_dir: Path
    work_dir: Path
    quarantine_dir: Path
    export_temp_dir: Path
    log_dir: Path

    @property
    def inbox_dir(self) -> Path:
        """Alias temporal para el importador manual existente."""
        return self.manual_pending_dir

    @property
    def storage_dir(self) -> Path:
        """Raíz desde la que se resuelven las rutas relativas de los XML."""
        return self.root_dir

    @property
    def processed_dir(self) -> Path:
        """Alias temporal para los ZIP manuales procesados."""
        return self.manual_processed_dir

    def all_directories(self) -> tuple[Path, ...]:
        return (
            self.root_dir,
            self.process_dir,
            self.sat_pending_dir,
            self.sat_processed_dir,
            self.sat_error_dir,
            self.manual_pending_dir,
            self.manual_processed_dir,
            self.manual_error_dir,
            self.work_dir,
            self.quarantine_dir,
            self.export_temp_dir,
            self.log_dir,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    database: DatabaseSettings
    paths: PathSettings
    zip_pattern: str
    log_level: str
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 10


@dataclass(frozen=True, slots=True)
class FielSettings:
    storage_dir: Path
    fernet_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SatSettings:
    """Configuración exclusiva de la descarga masiva de retenciones."""

    base: Settings
    fiel: FielSettings
    poll_seconds: int
    max_wait_minutes: int
    request_timeout_seconds: int
    token_reuse_seconds: int
    verify_tls: bool
    download_lease_minutes: int = 15
    followup_minutes: int = 15
    historical_companies: int = 1
    daily_time: time = time(2, 0)
    timezone_name: str = "America/Mexico_City"
    telegram_enabled: bool = False
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    telegram_timeout_seconds: int = 15


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    if env_file is not None:
        load_dotenv(dotenv_path=env_file, override=False)

    required_keys = (
        "DB_DRIVER",
        "DB_SERVER",
        "DB_NAME",
        "DB_TRUSTED_CONNECTION",
        "DB_ENCRYPT",
        "DB_TRUST_SERVER_CERTIFICATE",
        "RETENCIONES_ROOT_DIR",
    )
    missing_keys = [key for key in required_keys if not _get_env(key)]
    if missing_keys:
        missing = ", ".join(missing_keys)
        raise ConfigurationError(f"Faltan variables de entorno: {missing}")

    trusted_connection = _parse_bool(
        "DB_TRUSTED_CONNECTION", _required_env("DB_TRUSTED_CONNECTION")
    )
    username: str | None = None
    password: str | None = None
    if not trusted_connection:
        username = _required_env("DB_USER")
        password = _required_secret_env("DB_PASSWORD")

    database = DatabaseSettings(
        driver=_required_env("DB_DRIVER"),
        server=_required_env("DB_SERVER"),
        database=_required_env("DB_NAME"),
        trusted_connection=trusted_connection,
        username=username,
        password=password,
        encrypt=_parse_bool("DB_ENCRYPT", _required_env("DB_ENCRYPT")),
        trust_server_certificate=_parse_bool(
            "DB_TRUST_SERVER_CERTIFICATE",
            _required_env("DB_TRUST_SERVER_CERTIFICATE"),
        ),
    )

    root_dir = _absolute_path(
        "RETENCIONES_ROOT_DIR",
        _required_env("RETENCIONES_ROOT_DIR"),
    )
    process_dir = root_dir / "_procesos" / "Retenciones"
    packages_dir = process_dir / "paquetes"
    sat_packages_dir = packages_dir / "sat"
    manual_packages_dir = packages_dir / "manuales"
    log_dir = _absolute_path(
        "RETENCIONES_LOG_DIR",
        _get_env("RETENCIONES_LOG_DIR", str(process_dir / "logs")),
    )
    _validate_inside_root("RETENCIONES_LOG_DIR", log_dir, root_dir)

    paths = PathSettings(
        root_dir=root_dir,
        process_dir=process_dir,
        sat_pending_dir=sat_packages_dir / "pendientes",
        sat_processed_dir=sat_packages_dir / "procesados",
        sat_error_dir=sat_packages_dir / "errores",
        manual_pending_dir=manual_packages_dir / "pendientes",
        manual_processed_dir=manual_packages_dir / "procesados",
        manual_error_dir=manual_packages_dir / "errores",
        work_dir=process_dir / "trabajo",
        quarantine_dir=process_dir / "cuarentena",
        export_temp_dir=process_dir / "exportaciones_temporales",
        log_dir=log_dir,
    )

    zip_pattern = _get_env("ZIP_PATTERN", "*.zip")
    if not zip_pattern:
        raise ConfigurationError("ZIP_PATTERN no puede estar vacío")

    log_level = _get_env("LOG_LEVEL", "INFO").upper()
    valid_log_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
    if log_level not in valid_log_levels:
        valid_values = ", ".join(sorted(valid_log_levels))
        raise ConfigurationError(
            f"LOG_LEVEL debe ser uno de estos valores: {valid_values}"
        )

    return Settings(
        database=database,
        paths=paths,
        zip_pattern=zip_pattern,
        log_level=log_level,
        log_max_bytes=_parse_int_in_range(
            "LOG_MAX_BYTES",
            _get_env("LOG_MAX_BYTES", str(20 * 1024 * 1024)),
            minimum=1024 * 1024,
        ),
        log_backup_count=_parse_int_in_range(
            "LOG_BACKUP_COUNT",
            _get_env("LOG_BACKUP_COUNT", "10"),
            minimum=1,
            maximum=100,
        ),
    )


def load_sat_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> SatSettings:
    """Carga la configuración del SAT sin afectar al importador manual.

    La carpeta de e.firmas puede quedar fuera de RETENCIONES_ROOT_DIR porque
    es administrada por Django. SQL conserva el nombre exacto de cada archivo
    y el repositorio resolverá ese nombre dentro de esta carpeta controlada.
    """

    base = load_settings(env_file)
    fiel = FielSettings(
        storage_dir=_absolute_path(
            "FIEL_STORAGE_DIR",
            _required_env("FIEL_STORAGE_DIR"),
        ),
        fernet_key=_required_secret_env("FERNET_KEY"),
    )

    settings = SatSettings(
        base=base,
        fiel=fiel,
        poll_seconds=_parse_int_in_range(
            "SAT_POLL_SECONDS",
            _get_env("SAT_POLL_SECONDS", "60"),
            minimum=30,
        ),
        max_wait_minutes=_parse_int_in_range(
            "SAT_MAX_WAIT_MINUTES",
            _get_env("SAT_MAX_WAIT_MINUTES", "30"),
            minimum=1,
        ),
        request_timeout_seconds=_parse_int_in_range(
            "SAT_REQUEST_TIMEOUT_SECONDS",
            _get_env("SAT_REQUEST_TIMEOUT_SECONDS", "60"),
            minimum=15,
        ),
        token_reuse_seconds=_parse_int_in_range(
            "SAT_TOKEN_REUSE_SECONDS",
            _get_env("SAT_TOKEN_REUSE_SECONDS", "240"),
            minimum=1,
            maximum=299,
        ),
        verify_tls=_parse_bool(
            "SAT_VERIFY_TLS",
            _get_env("SAT_VERIFY_TLS", "yes"),
        ),
        download_lease_minutes=_parse_int_in_range(
            "SAT_DOWNLOAD_LEASE_MINUTES",
            _get_env("SAT_DOWNLOAD_LEASE_MINUTES", "15"),
            minimum=1,
        ),
        followup_minutes=_parse_int_in_range(
            "SAT_FOLLOWUP_MINUTES",
            _get_env("SAT_FOLLOWUP_MINUTES", "15"),
            minimum=1,
            maximum=1440,
        ),
        historical_companies=_parse_int_in_range(
            "SAT_HISTORICAL_COMPANIES",
            _get_env("SAT_HISTORICAL_COMPANIES", "1"),
            minimum=1,
            maximum=100,
        ),
        daily_time=_parse_time(
            "SAT_DAILY_TIME",
            _get_env(
                "SAT_DAILY_TIME",
                _get_env("SAT_WEEKLY_TIME", "02:00"),
            ),
        ),
        timezone_name=_parse_timezone(
            "SAT_TIMEZONE",
            _get_env("SAT_TIMEZONE", "America/Mexico_City"),
        ),
        telegram_enabled=_parse_bool(
            "TELEGRAM_ENABLED",
            _get_env("TELEGRAM_ENABLED", "no"),
        ),
        telegram_bot_token=_get_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get_env("TELEGRAM_CHAT_ID"),
        telegram_timeout_seconds=_parse_int_in_range(
            "TELEGRAM_TIMEOUT_SECONDS",
            _get_env("TELEGRAM_TIMEOUT_SECONDS", "15"),
            minimum=1,
            maximum=60,
        ),
    )
    if settings.telegram_enabled and (
        not settings.telegram_bot_token or not settings.telegram_chat_id
    ):
        raise ConfigurationError(
            "TELEGRAM_ENABLED=yes requiere TELEGRAM_BOT_TOKEN y "
            "TELEGRAM_CHAT_ID."
        )
    return settings


def _get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _required_env(key: str) -> str:
    value = _get_env(key)
    if not value:
        raise ConfigurationError(f"La variable {key} es obligatoria")
    return value


def _required_secret_env(key: str) -> str:
    value = os.getenv(key)
    if value is None or value == "":
        raise ConfigurationError(f"La variable {key} es obligatoria")
    return value


def _parse_bool(key: str, value: str) -> bool:
    normalized = value.strip().lower()
    true_values = {"1", "true", "yes", "y", "si", "sí", "on"}
    false_values = {"0", "false", "no", "n", "off"}
    if normalized in true_values:
        return True
    if normalized in false_values:
        return False
    raise ConfigurationError(
        f"{key} debe ser un valor booleano como yes/no o true/false"
    )


def _parse_int_in_range(
    key: str,
    value: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} debe ser un número entero") from exc

    if parsed < minimum:
        raise ConfigurationError(
            f"{key} debe ser mayor o igual a {minimum}"
        )
    if maximum is not None and parsed > maximum:
        raise ConfigurationError(
            f"{key} debe ser menor o igual a {maximum}"
        )
    return parsed


def _parse_choice(key: str, value: str, allowed: set[str]) -> str:
    parsed = str(value or "").strip().upper()
    if parsed not in allowed:
        values = ", ".join(sorted(allowed))
        raise ConfigurationError(f"{key} debe ser uno de estos valores: {values}")
    return parsed


def _parse_weekday(key: str, value: str) -> int:
    weekdays = {
        "MONDAY": 0,
        "TUESDAY": 1,
        "WEDNESDAY": 2,
        "THURSDAY": 3,
        "FRIDAY": 4,
        "SATURDAY": 5,
        "SUNDAY": 6,
    }
    parsed = str(value or "").strip().upper()
    if parsed not in weekdays:
        values = ", ".join(weekdays)
        raise ConfigurationError(f"{key} debe ser uno de estos valores: {values}")
    return weekdays[parsed]


def _parse_time(key: str, value: str) -> time:
    try:
        return time.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise ConfigurationError(
            f"{key} debe usar el formato HH:MM o HH:MM:SS"
        ) from exc


def _parse_timezone(key: str, value: str) -> str:
    parsed = str(value or "").strip()
    try:
        ZoneInfo(parsed)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"{key} no contiene una zona horaria válida: {parsed}"
        ) from exc
    return parsed


def _absolute_path(key: str, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{key} debe contener una ruta absoluta: {value}")
    return path.resolve()


def _validate_inside_root(key: str, path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError(
            f"{key} debe permanecer dentro de RETENCIONES_ROOT_DIR: {path}"
        ) from exc


def _to_odbc_bool(value: bool) -> str:
    return "yes" if value else "no"


def _odbc_value(value: str) -> str:
    """Escapa un valor para usarlo dentro de una cadena de conexión ODBC."""
    return "{" + str(value).replace("}", "}}") + "}"
