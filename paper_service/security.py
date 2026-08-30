"""Request security for the private paper web application.

Cloudflare Access authenticates the browser at the edge and forwards a signed
JWT to the origin.  The origin must still validate that signature and the
token's issuer/audience; trusting the header's presence alone would allow a
request that bypassed the edge to impersonate an authenticated user.
"""

from __future__ import annotations

import os
import hmac
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit


ACCESS_JWT_HEADER = "Cf-Access-Jwt-Assertion"
ACTION_HEADER = "X-Paper-Action"
ACTION_HEADER_VALUE = "1"
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_REQUESTS = frozenset(
    {
        ("GET", "/healthz"),
        ("HEAD", "/healthz"),
        ("GET", "/readyz"),
        ("HEAD", "/readyz"),
    }
)
MAX_ACCESS_TOKEN_LENGTH = 16_384
MACHINE_API_PREFIX = "/api/knowledge/outbox"
_SERVICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\.access")
_SAFE_IDENTITY_RE = re.compile(r"[^\x00-\x1f\x7f]{1,512}")


class SecurityConfigurationError(RuntimeError):
    """The service was told to enforce security but is not safely configured."""


class AccessTokenRejected(Exception):
    """The presented Access token is missing required or valid claims."""


class AccessVerificationUnavailable(Exception):
    """The Access signing keys could not currently be obtained."""


class SigningKey(Protocol):
    key: Any


class SigningKeyClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> SigningKey: ...


@dataclass(frozen=True)
class SecurityConfig:
    """Validated runtime settings for request security."""

    access_enforced: bool
    mutation_protection: bool
    issuer: str | None = None
    audience: str | None = None
    public_origin: str | None = None
    vault_sync_service_id: str | None = None
    clock_skew_seconds: int = 30

    @property
    def jwks_url(self) -> str:
        if not self.issuer:
            raise SecurityConfigurationError("Access issuer is not configured")
        return f"{self.issuer}/cdn-cgi/access/certs"


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SecurityConfigurationError(f"{name} must be true or false")


def _https_origin(name: str, raw: str, *, cloudflare_team: bool = False) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SecurityConfigurationError(f"{name} must be an HTTPS origin")
    if cloudflare_team and not parsed.hostname.endswith(".cloudflareaccess.com"):
        raise SecurityConfigurationError(
            f"{name} must be a Cloudflare Access team domain"
        )
    return f"https://{parsed.hostname}"


def security_config_from_env() -> SecurityConfig:
    """Read and validate security settings without caching environment state."""

    access_enforced = _boolean("PAPER_ACCESS_ENFORCE", False)
    mutation_protection = _boolean(
        "PAPER_BROWSER_MUTATION_PROTECTION", access_enforced
    )

    raw_skew = os.environ.get("PAPER_ACCESS_CLOCK_SKEW_SECONDS", "30")
    try:
        clock_skew_seconds = int(raw_skew)
    except ValueError as exc:
        raise SecurityConfigurationError(
            "PAPER_ACCESS_CLOCK_SKEW_SECONDS must be an integer"
        ) from exc
    if not 0 <= clock_skew_seconds <= 120:
        raise SecurityConfigurationError(
            "PAPER_ACCESS_CLOCK_SKEW_SECONDS must be between 0 and 120"
        )

    issuer: str | None = None
    audience: str | None = None
    public_origin: str | None = None
    vault_sync_service_id: str | None = None

    if access_enforced:
        raw_team_domain = os.environ.get("PAPER_ACCESS_TEAM_DOMAIN", "")
        audience = os.environ.get("PAPER_ACCESS_AUDIENCE", "").strip()
        if not raw_team_domain.strip() or not audience:
            raise SecurityConfigurationError(
                "Access enforcement requires team domain and audience"
            )
        issuer = _https_origin(
            "PAPER_ACCESS_TEAM_DOMAIN", raw_team_domain, cloudflare_team=True
        )

    if mutation_protection:
        raw_origin = os.environ.get("PAPER_PUBLIC_ORIGIN", "")
        if not raw_origin.strip():
            raise SecurityConfigurationError(
                "Mutation protection requires PAPER_PUBLIC_ORIGIN"
            )
        public_origin = _https_origin("PAPER_PUBLIC_ORIGIN", raw_origin)

    raw_service_id = os.environ.get("PAPER_VAULT_SYNC_SERVICE_ID", "").strip()
    if raw_service_id:
        if not _SERVICE_ID_RE.fullmatch(raw_service_id):
            raise SecurityConfigurationError(
                "PAPER_VAULT_SYNC_SERVICE_ID must be a Cloudflare service Client ID"
            )
        vault_sync_service_id = raw_service_id

    return SecurityConfig(
        access_enforced=access_enforced,
        mutation_protection=mutation_protection,
        issuer=issuer,
        audience=audience,
        public_origin=public_origin,
        vault_sync_service_id=vault_sync_service_id,
        clock_skew_seconds=clock_skew_seconds,
    )


@lru_cache(maxsize=8)
def _get_jwks_client(jwks_url: str) -> SigningKeyClient:
    """Create a bounded, rotating-key-aware JWKS client for one team domain."""

    import jwt

    return jwt.PyJWKClient(
        jwks_url,
        cache_keys=True,
        cache_jwk_set=True,
        lifespan=300,
        timeout=5,
    )


def validate_access_token(
    token: str,
    config: SecurityConfig,
    *,
    jwks_client: SigningKeyClient | None = None,
) -> dict[str, Any]:
    """Validate a Cloudflare Access application token and return safe claims."""

    import jwt
    from jwt.exceptions import (
        InvalidTokenError,
        PyJWKClientConnectionError,
        PyJWKClientError,
    )

    if not config.access_enforced or not config.issuer or not config.audience:
        raise SecurityConfigurationError("Access validation is not configured")
    if not token or len(token) > MAX_ACCESS_TOKEN_LENGTH:
        raise AccessTokenRejected("invalid Access token")

    client = jwks_client or _get_jwks_client(config.jwks_url)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
    except PyJWKClientConnectionError as exc:
        raise AccessVerificationUnavailable("Access keys are unavailable") from exc
    except PyJWKClientError as exc:
        raise AccessTokenRejected("invalid Access signing key") from exc
    except InvalidTokenError as exc:
        raise AccessTokenRejected("invalid Access token") from exc

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=config.audience,
            issuer=config.issuer,
            leeway=config.clock_skew_seconds,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "type"]},
        )
    except InvalidTokenError as exc:
        raise AccessTokenRejected("invalid Access token") from exc

    if claims.get("type") != "app":
        raise AccessTokenRejected("Access token is not an application token")
    subject = claims.get("sub")
    common_name = claims.get("common_name")
    if common_name is not None:
        if (
            not isinstance(common_name, str)
            or not _SERVICE_ID_RE.fullmatch(common_name)
            or subject != ""
        ):
            raise AccessTokenRejected("Access service identity is invalid")
        return {"kind": "service", "service_id": common_name}

    email = claims.get("email")
    if (
        not isinstance(email, str)
        or not _SAFE_IDENTITY_RE.fullmatch(email.strip())
        or not isinstance(subject, str)
        or not _SAFE_IDENTITY_RE.fullmatch(subject.strip())
    ):
        raise AccessTokenRejected("Access user identity is invalid")

    return {
        "kind": "human",
        "email": email.strip(),
        "sub": subject.strip(),
    }


def is_public_request(method: str, path: str) -> bool:
    """Only the read-only health endpoints bypass Access validation."""

    return (method.upper(), path) in PUBLIC_REQUESTS


def is_machine_request(path: str) -> bool:
    """Return whether a route belongs to the Mac reviewed-knowledge bridge."""

    return path == MACHINE_API_PREFIX or path.startswith(f"{MACHINE_API_PREFIX}/")


def machine_identity_is_allowed(
    identity: Mapping[str, Any], config: SecurityConfig
) -> bool:
    """Bind the outbox bridge to one explicitly configured Access token."""

    expected = config.vault_sync_service_id
    actual = identity.get("service_id")
    return (
        identity.get("kind") == "service"
        and isinstance(expected, str)
        and isinstance(actual, str)
        and hmac.compare_digest(actual, expected)
    )


def browser_mutation_is_valid(
    method: str,
    headers: Mapping[str, str],
    config: SecurityConfig,
) -> bool:
    """Require same-origin browser intent for state-changing HTTP methods."""

    if method.upper() not in MUTATION_METHODS or not config.mutation_protection:
        return True
    if not config.public_origin:
        raise SecurityConfigurationError("Mutation origin is not configured")
    return (
        headers.get("origin") == config.public_origin
        and headers.get(ACTION_HEADER.lower()) == ACTION_HEADER_VALUE
    )
