import os
import time
import unittest
from unittest.mock import Mock, patch

from paper_service.security import (
    ACCESS_JWT_HEADER,
    ACTION_HEADER,
    AccessTokenRejected,
    AccessVerificationUnavailable,
    SecurityConfig,
    SecurityConfigurationError,
    browser_mutation_is_valid,
    is_machine_request,
    machine_identity_is_allowed,
    is_public_request,
    security_config_from_env,
    validate_access_token,
)


TEAM_DOMAIN = "https://rapid-king-ed86.cloudflareaccess.com"
AUDIENCE = "666a2a49a6e448cf80a9041826b83c7dead160141e24ee8c3f00db30f2f3ef92"
PUBLIC_ORIGIN = "https://papers.minjae-jo.com"


def enabled_environment() -> dict[str, str]:
    return {
        "PAPER_ACCESS_ENFORCE": "true",
        "PAPER_ACCESS_TEAM_DOMAIN": TEAM_DOMAIN,
        "PAPER_ACCESS_AUDIENCE": AUDIENCE,
        "PAPER_PUBLIC_ORIGIN": PUBLIC_ORIGIN,
        "PAPER_ACCESS_CLOCK_SKEW_SECONDS": "0",
    }


class SecurityConfigurationTests(unittest.TestCase):
    def test_defaults_are_disabled_for_local_and_test_environments(self):
        with patch.dict(os.environ, {}, clear=True):
            config = security_config_from_env()
        self.assertFalse(config.access_enforced)
        self.assertFalse(config.mutation_protection)
        self.assertIsNone(config.issuer)

    def test_enabled_configuration_is_normalized(self):
        environment = enabled_environment()
        environment["PAPER_ACCESS_TEAM_DOMAIN"] += "/"
        environment["PAPER_PUBLIC_ORIGIN"] += "/"
        with patch.dict(os.environ, environment, clear=True):
            config = security_config_from_env()
        self.assertTrue(config.access_enforced)
        self.assertTrue(config.mutation_protection)
        self.assertEqual(config.issuer, TEAM_DOMAIN)
        self.assertEqual(config.audience, AUDIENCE)
        self.assertEqual(config.public_origin, PUBLIC_ORIGIN)
        self.assertIsNone(config.vault_sync_service_id)
        self.assertEqual(
            config.jwks_url, f"{TEAM_DOMAIN}/cdn-cgi/access/certs"
        )

    def test_enforcement_fails_closed_without_identity_settings(self):
        with patch.dict(os.environ, {"PAPER_ACCESS_ENFORCE": "true"}, clear=True):
            with self.assertRaises(SecurityConfigurationError):
                security_config_from_env()

    def test_mutation_protection_requires_an_exact_origin(self):
        environment = enabled_environment()
        environment.pop("PAPER_PUBLIC_ORIGIN")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(SecurityConfigurationError):
                security_config_from_env()

    def test_invalid_security_boolean_is_rejected(self):
        with patch.dict(
            os.environ, {"PAPER_ACCESS_ENFORCE": "sometimes"}, clear=True
        ):
            with self.assertRaises(SecurityConfigurationError):
                security_config_from_env()

    def test_non_cloudflare_team_domain_is_rejected(self):
        environment = enabled_environment()
        environment["PAPER_ACCESS_TEAM_DOMAIN"] = "https://attacker.example"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(SecurityConfigurationError):
                security_config_from_env()

    def test_service_client_id_is_optional_but_strict_when_configured(self):
        environment = enabled_environment()
        service_id = "a" * 32 + ".access"
        environment["PAPER_VAULT_SYNC_SERVICE_ID"] = service_id
        with patch.dict(os.environ, environment, clear=True):
            config = security_config_from_env()
        self.assertEqual(config.vault_sync_service_id, service_id)

        environment["PAPER_VAULT_SYNC_SERVICE_ID"] = "not-a-service-id"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(SecurityConfigurationError):
                security_config_from_env()


class BrowserRequestProtectionTests(unittest.TestCase):
    def setUp(self):
        self.config = SecurityConfig(
            access_enforced=True,
            mutation_protection=True,
            issuer=TEAM_DOMAIN,
            audience=AUDIENCE,
            public_origin=PUBLIC_ORIGIN,
            clock_skew_seconds=0,
        )

    def test_only_read_only_health_requests_are_public(self):
        self.assertTrue(is_public_request("GET", "/healthz"))
        self.assertTrue(is_public_request("HEAD", "/readyz"))
        self.assertFalse(is_public_request("POST", "/healthz"))
        self.assertFalse(is_public_request("GET", "/library"))

    def test_read_request_does_not_need_mutation_headers(self):
        self.assertTrue(browser_mutation_is_valid("GET", {}, self.config))

    def test_mutation_requires_exact_origin_and_action_header(self):
        valid_headers = {"origin": PUBLIC_ORIGIN, ACTION_HEADER.lower(): "1"}
        self.assertTrue(browser_mutation_is_valid("POST", valid_headers, self.config))
        self.assertFalse(browser_mutation_is_valid("POST", {}, self.config))
        self.assertFalse(
            browser_mutation_is_valid(
                "POST",
                {"origin": f"{PUBLIC_ORIGIN}.attacker.example", ACTION_HEADER.lower(): "1"},
                self.config,
            )
        )
        self.assertFalse(
            browser_mutation_is_valid(
                "POST",
                {"origin": PUBLIC_ORIGIN, ACTION_HEADER.lower(): "true"},
                self.config,
            )
        )

    def test_machine_route_matching_has_a_component_boundary(self):
        self.assertTrue(is_machine_request("/api/knowledge/outbox/lease"))
        self.assertFalse(is_machine_request("/api/knowledge/outbox-attacker"))

    def test_machine_identity_is_bound_to_the_configured_client_id(self):
        service_id = "a" * 32 + ".access"
        config = SecurityConfig(
            **{**self.config.__dict__, "vault_sync_service_id": service_id}
        )
        self.assertTrue(
            machine_identity_is_allowed(
                {"kind": "service", "service_id": service_id}, config
            )
        )
        self.assertFalse(
            machine_identity_is_allowed(
                {"kind": "service", "service_id": "b" * 32 + ".access"},
                config,
            )
        )


class AccessTokenValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        cls.jwt = jwt
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.config = SecurityConfig(
            access_enforced=True,
            mutation_protection=True,
            issuer=TEAM_DOMAIN,
            audience=AUDIENCE,
            public_origin=PUBLIC_ORIGIN,
            clock_skew_seconds=0,
        )

    def token(self, **overrides):
        now = int(time.time())
        payload = {
            "type": "app",
            "iss": TEAM_DOMAIN,
            "aud": [AUDIENCE],
            "sub": "user-123",
            "email": "student@example.com",
            "iat": now - 1,
            "exp": now + 300,
        }
        payload.update(overrides)
        return self.jwt.encode(
            payload,
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test"},
        )

    def client(self):
        signing_key = Mock()
        signing_key.key = self.public_key
        client = Mock()
        client.get_signing_key_from_jwt.return_value = signing_key
        return client

    def test_valid_signed_token_returns_only_identity_claims(self):
        claims = validate_access_token(
            self.token(extra="not exposed"), self.config, jwks_client=self.client()
        )
        self.assertEqual(
            claims,
            {
                "kind": "human",
                "email": "student@example.com",
                "sub": "user-123",
            },
        )

    def test_valid_service_token_returns_a_distinct_machine_identity(self):
        service_id = "a" * 32 + ".access"
        claims = validate_access_token(
            self.token(sub="", email=None, common_name=service_id),
            self.config,
            jwks_client=self.client(),
        )
        self.assertEqual(
            claims, {"kind": "service", "service_id": service_id}
        )

    def test_service_token_cannot_spoof_a_nonempty_user_subject(self):
        with self.assertRaises(AccessTokenRejected):
            validate_access_token(
                self.token(common_name="a" * 32 + ".access"),
                self.config,
                jwks_client=self.client(),
            )

    def test_wrong_audience_is_rejected(self):
        with self.assertRaises(AccessTokenRejected):
            validate_access_token(
                self.token(aud=["another-app"]), self.config, jwks_client=self.client()
            )

    def test_wrong_issuer_is_rejected(self):
        with self.assertRaises(AccessTokenRejected):
            validate_access_token(
                self.token(iss="https://attacker.example"),
                self.config,
                jwks_client=self.client(),
            )

    def test_non_rs256_algorithm_is_rejected(self):
        now = int(time.time())
        token = self.jwt.encode(
            {
                "iss": TEAM_DOMAIN,
                "aud": [AUDIENCE],
                "type": "app",
                "sub": "user-123",
                "email": "student@example.com",
                "iat": now - 1,
                "exp": now + 300,
            },
            "untrusted-secret-that-is-long-enough",
            algorithm="HS256",
            headers={"kid": "test"},
        )
        with self.assertRaises(AccessTokenRejected):
            validate_access_token(token, self.config, jwks_client=self.client())

    def test_expired_token_is_rejected(self):
        with self.assertRaises(AccessTokenRejected):
            validate_access_token(
                self.token(exp=int(time.time()) - 10),
                self.config,
                jwks_client=self.client(),
            )

    def test_token_without_user_email_is_rejected(self):
        with self.assertRaises(AccessTokenRejected):
            validate_access_token(
                self.token(email=""), self.config, jwks_client=self.client()
            )

    def test_jwks_network_failure_is_fail_closed_and_distinguishable(self):
        from jwt.exceptions import PyJWKClientConnectionError

        client = Mock()
        client.get_signing_key_from_jwt.side_effect = PyJWKClientConnectionError(
            "offline"
        )
        with self.assertRaises(AccessVerificationUnavailable):
            validate_access_token(self.token(), self.config, jwks_client=client)

    def test_oversized_token_is_rejected_before_key_lookup(self):
        client = self.client()
        with self.assertRaises(AccessTokenRejected):
            validate_access_token("x" * 20_000, self.config, jwks_client=client)
        client.get_signing_key_from_jwt.assert_not_called()

    def test_malformed_token_is_rejected_before_jwks_network_access(self):
        with self.assertRaises(AccessTokenRejected):
            validate_access_token("not-a-jwt", self.config)


class ApiSecurityMiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from paper_service.api import app

        cls.client = TestClient(app)

    def request(self, method: str, path: str, **kwargs):
        environment = enabled_environment()
        with patch.dict(os.environ, environment, clear=True), patch(
            "paper_service.api.validate_access_token",
            return_value={
                "kind": "human",
                "email": "student@example.com",
                "sub": "user-123",
            },
        ):
            return self.client.request(
                method, path, follow_redirects=False, **kwargs
            )

    def test_health_endpoint_remains_public(self):
        response = self.request("GET", "/healthz")
        self.assertEqual(response.status_code, 200)

    def test_private_request_without_token_is_rejected_before_route(self):
        with patch.dict(os.environ, enabled_environment(), clear=True):
            response = self.client.get("/library", follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_valid_token_allows_private_read(self):
        response = self.request(
            "GET", "/", headers={ACCESS_JWT_HEADER: "signed-token"}
        )
        self.assertEqual(response.status_code, 307)

    def test_human_identity_cannot_use_machine_outbox_namespace(self):
        response = self.request(
            "GET",
            "/api/knowledge/outbox/not-a-route",
            headers={ACCESS_JWT_HEADER: "signed-token"},
        )
        self.assertEqual(response.status_code, 403)

    def test_exact_service_identity_is_limited_to_machine_namespace(self):
        service_id = "a" * 32 + ".access"
        environment = enabled_environment()
        environment["PAPER_VAULT_SYNC_SERVICE_ID"] = service_id
        with patch.dict(os.environ, environment, clear=True), patch(
            "paper_service.api.validate_access_token",
            return_value={"kind": "service", "service_id": service_id},
        ):
            allowed = self.client.get(
                "/api/knowledge/outbox/not-a-route",
                headers={ACCESS_JWT_HEADER: "service-token"},
            )
            blocked = self.client.get(
                "/library", headers={ACCESS_JWT_HEADER: "service-token"}
            )

        self.assertEqual(allowed.status_code, 404)
        self.assertEqual(blocked.status_code, 403)

    def test_mutation_requires_browser_intent_headers(self):
        headers = {ACCESS_JWT_HEADER: "signed-token"}
        response = self.request("POST", "/jobs/sync", headers=headers)
        self.assertEqual(response.status_code, 403)
        headers.update({"Origin": PUBLIC_ORIGIN, ACTION_HEADER: "1"})
        with patch("paper_service.api.queue_sync", return_value={"id": 42}):
            response = self.request("POST", "/jobs/sync", headers=headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"job": {"id": 42}})

    def test_invalid_token_is_rejected(self):
        with patch.dict(os.environ, enabled_environment(), clear=True), patch(
            "paper_service.api.validate_access_token",
            side_effect=AccessTokenRejected("invalid"),
        ):
            response = self.client.get(
                "/", headers={ACCESS_JWT_HEADER: "invalid"}, follow_redirects=False
            )
        self.assertEqual(response.status_code, 401)

    def test_key_service_failure_is_fail_closed(self):
        with patch.dict(os.environ, enabled_environment(), clear=True), patch(
            "paper_service.api.validate_access_token",
            side_effect=AccessVerificationUnavailable("offline"),
        ):
            response = self.client.get(
                "/", headers={ACCESS_JWT_HEADER: "token"}, follow_redirects=False
            )
        self.assertEqual(response.status_code, 503)

    def test_incomplete_enforced_configuration_is_fail_closed(self):
        with patch.dict(
            os.environ,
            {"PAPER_ACCESS_ENFORCE": "true"},
            clear=True,
        ):
            response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
