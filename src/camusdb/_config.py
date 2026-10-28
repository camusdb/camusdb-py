"""Connection settings: the keyword arguments of ``connect`` and the CamusDB connection string.

The connection string is the one the other CamusDB drivers use, so one string serves every
language::

    Endpoint=http://localhost:5095;Database=test;User=app;Password='pa;ss''word'

Keys are matched without regard to case. A value ends at the next ``;`` unless it is quoted. A quote
character is written twice to include it in a quoted value. A repeated key is an error.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar
from urllib.parse import urlsplit

from . import codes
from .errors import InterfaceError, NotSupportedError, ProgrammingError
from .types import IsolationLevel, Locking

_T = TypeVar("_T")

DEFAULT_TIMEOUT = 10.0
DEFAULT_TOKEN_LIFETIME = 600.0


@dataclass(frozen=True)
class Config:
    endpoints: tuple[str, ...]
    database: str
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    timeout: float = DEFAULT_TIMEOUT
    token_lifetime: float = DEFAULT_TOKEN_LIFETIME
    isolation_level: IsolationLevel | None = None
    read_only: bool | None = None
    locking: Locking | None = None
    autocommit: bool = False
    http2: bool = False
    allow_insecure_credentials: bool = False

    def with_database(self, database: str) -> Config:
        return replace(self, database=database)


# Connection-string keys, lower-cased, to the keyword argument they set.
_KEYS: dict[str, str] = {
    "endpoint": "endpoint",
    "database": "database",
    "timeout": "timeout",
    "user": "user",
    "userid": "user",
    "uid": "user",
    "username": "user",
    "password": "password",
    "pwd": "password",
    "accesstoken": "access_token",
    "tokenlifetime": "token_lifetime",
    "isolationlevel": "isolation_level",
    "transactionmode": "transaction_mode",
    "readonly": "read_only",
    "locking": "locking",
    "autocommit": "autocommit",
    "httpversion": "http_version",
    "http2": "http2",
    "allowinsecurecredentials": "allow_insecure_credentials",
    "protocol": "protocol",
}

# Keys of the .NET driver that tune features this driver does not have. They are accepted and have
# no effect, so one connection string can serve both drivers.
_IGNORED_KEYS = frozenset(
    {
        "maxautoprepare",
        "autoprepareminusages",
        "channelpoolsize",
        "coalescingthreshold",
        "coalescingdelay",
        "requestframes",
        "tracequerytext",
        "backupendpoint",
        "backuptimeout",
        "routingmode",
        "routingnodes",
        "routingmaxhintage",
    }
)

_SECRET_KEYS = frozenset({"password", "pwd", "accesstoken"})


def parse_connection_string(text: str) -> dict[str, str]:
    """Parse a connection string into its keys (lower-cased) and values."""
    pairs: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and (text[i].isspace() or text[i] == ";"):
            i += 1
        if i >= n:
            break
        eq = text.find("=", i)
        if eq < 0:
            raise ProgrammingError(
                f"connection string: {text[i:].strip()!r} has no '='", codes.INVALID_INPUT
            )
        key = text[i:eq].strip().lower()
        if not key:
            raise ProgrammingError("connection string: a value has no key", codes.INVALID_INPUT)
        i = eq + 1
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] in "'\"":
            quote = text[i]
            i += 1
            chunks: list[str] = []
            while True:
                end = text.find(quote, i)
                if end < 0:
                    raise ProgrammingError(
                        f"connection string: the value of {key!r} has no closing quote",
                        codes.INVALID_INPUT,
                    )
                chunks.append(text[i:end])
                if end + 1 < n and text[end + 1] == quote:
                    chunks.append(quote)
                    i = end + 2
                    continue
                i = end + 1
                break
            value = "".join(chunks)
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] != ";":
                raise ProgrammingError(
                    f"connection string: unexpected text after the value of {key!r}",
                    codes.INVALID_INPUT,
                )
        else:
            end = text.find(";", i)
            end = n if end < 0 else end
            value = text[i:end].strip()
            i = end
        if key in pairs:
            raise ProgrammingError(
                f"connection string: the key {key!r} is repeated", codes.INVALID_INPUT
            )
        pairs[key] = value
    return pairs


def redact(text: str) -> str:
    """Return the connection string with the password and the access token masked."""
    try:
        pairs = parse_connection_string(text)
    except ProgrammingError:
        # A string that does not parse cannot be masked reliably.
        return "<unparseable connection string>"
    return ";".join(
        f"{key}={'***' if key in _SECRET_KEYS else value}" for key, value in pairs.items()
    )


def build_config(dsn: str | None, overrides: Mapping[str, Any]) -> Config:
    """Merge a connection string and keyword arguments. A keyword argument wins over the string."""
    settings: dict[str, Any] = {}
    if dsn:
        for key, value in parse_connection_string(dsn).items():
            if key in _IGNORED_KEYS:
                continue
            if key not in _KEYS:
                raise ProgrammingError(
                    f"connection string: unknown key {key!r}", codes.INVALID_INPUT
                )
            settings[_KEYS[key]] = value
    unknown = set(overrides) - set(_KEYS.values())
    if unknown:
        raise TypeError(f"connect() got unexpected keyword arguments: {', '.join(sorted(unknown))}")
    settings.update({key: value for key, value in overrides.items() if value is not None})

    protocol = str(settings.pop("protocol", "rest")).strip().lower()
    if protocol != "rest":
        raise NotSupportedError(
            f"protocol {protocol!r} is not supported: this driver speaks the REST protocol only"
        )

    endpoints = _endpoints(settings.pop("endpoint", None))
    database = settings.pop("database", None)
    if not database:
        raise ProgrammingError("a database is required", codes.INVALID_INPUT)

    http_version = str(settings.pop("http_version", "1.1")).strip()
    if http_version not in {"1.1", "2", "2.0", "3", "3.0"}:
        http_version = "1.1"
    # HTTP/3 needs a QUIC client, which httpx does not have. The version is the highest that the
    # client asks for, not a demand, so the client uses HTTP/2, as the .NET driver does without QUIC.
    http2 = settings.pop("http2", None)
    http2 = http_version != "1.1" if http2 is None else _bool("http2", http2)

    transaction_mode = settings.pop("transaction_mode", None)
    read_only = settings.pop("read_only", None)
    if transaction_mode is not None and read_only is None:
        read_only = _enum_value(
            "TransactionMode", transaction_mode, {"readwrite": False, "readonly": True}
        )

    config = Config(
        endpoints=endpoints,
        database=str(database),
        user=_optional_str(settings.pop("user", None)),
        password=_optional_str(settings.pop("password", None), strip=False),
        access_token=_optional_str(settings.pop("access_token", None)),
        timeout=_positive_float("timeout", settings.pop("timeout", DEFAULT_TIMEOUT)),
        token_lifetime=_positive_float(
            "token_lifetime", settings.pop("token_lifetime", DEFAULT_TOKEN_LIFETIME)
        ),
        isolation_level=_isolation(settings.pop("isolation_level", None)),
        read_only=None if read_only is None else _bool("read_only", read_only),
        locking=_locking(settings.pop("locking", None)),
        autocommit=_bool("autocommit", settings.pop("autocommit", False)),
        http2=http2,
        allow_insecure_credentials=_bool(
            "allow_insecure_credentials", settings.pop("allow_insecure_credentials", False)
        ),
    )
    if config.password is not None and not config.user:
        raise ProgrammingError("a password needs a user", codes.INVALID_INPUT)
    check_credentials_transport(config)
    return config


def check_credentials_transport(config: Config) -> None:
    """Refuse to send credentials in clear text to a host that is not this machine.

    The server refuses them too (``CADB0519``), but only after the password crossed the network.
    """
    if config.allow_insecure_credentials or (
        config.password is None and config.access_token is None
    ):
        return
    for endpoint in config.endpoints:
        parts = urlsplit(endpoint)
        if parts.scheme == "https" or _is_loopback(parts.hostname):
            continue
        raise InterfaceError(
            f"credentials are configured for the endpoint {endpoint!r}, which is neither https nor "
            "loopback, so the password and the token would cross the network in clear text. Use "
            "https, or set allow_insecure_credentials=True when TLS ends at a proxy inside the "
            "trust boundary",
            codes.INSECURE_TRANSPORT,
        )


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_ENDPOINT = re.compile(r"https?://[^\s/?#]+(/[^\s?#]*)?", re.IGNORECASE)


def _endpoints(raw: Any) -> tuple[str, ...]:
    if not raw:
        raise ProgrammingError(
            "an endpoint is required, such as 'http://localhost:5095'", codes.INVALID_INPUT
        )
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    endpoints: list[str] = []
    for item in items:
        endpoint = str(item).strip().rstrip("/")
        if not endpoint:
            continue
        if not _ENDPOINT.fullmatch(endpoint):
            raise ProgrammingError(
                f"{endpoint!r} is not an endpoint: expected an http:// or https:// URL",
                codes.INVALID_INPUT,
            )
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    if not endpoints:
        raise ProgrammingError("an endpoint is required", codes.INVALID_INPUT)
    return tuple(endpoints)


def _optional_str(value: Any, *, strip: bool = True) -> str | None:
    if value is None:
        return None
    text = str(value)
    if strip:
        text = text.strip()
    return text or None


def _positive_float(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ProgrammingError(
            f"{name} must be a number of seconds, not {value!r}", codes.INVALID_INPUT
        ) from None
    if number <= 0:
        raise ProgrammingError(f"{name} must be greater than zero", codes.INVALID_INPUT)
    return number


def _bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    raise ProgrammingError(f"{name} must be true or false, not {value!r}", codes.INVALID_INPUT)


def _enum_value(name: str, value: Any, choices: Mapping[str, _T]) -> _T:
    key = re.sub(r"[\s_-]", "", str(value)).lower()
    if key not in choices:
        raise ProgrammingError(
            f"{name} {value!r} is not one of: {', '.join(choices)}", codes.INVALID_INPUT
        )
    return choices[key]


def _isolation(value: Any) -> IsolationLevel | None:
    if value is None or isinstance(value, IsolationLevel):
        return value
    return _enum_value(
        "isolation_level",
        value,
        {
            "serializable": IsolationLevel.SERIALIZABLE,
            "readcommitted": IsolationLevel.READ_COMMITTED,
        },
    )


def _locking(value: Any) -> Locking | None:
    if value is None or isinstance(value, Locking):
        return value
    return _enum_value(
        "locking", value, {"pessimistic": Locking.PESSIMISTIC, "optimistic": Locking.OPTIMISTIC}
    )


def coerce_isolation(value: Any) -> IsolationLevel | None:
    return _isolation(value)


def coerce_locking(value: Any) -> Locking | None:
    return _locking(value)
