import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from perf_agent.adapters import ApprovalRequired, Guardrail, GuardrailViolation
from perf_agent.config import ConfigError, load_config, parse_config
from perf_agent.governance import (
    ApprovalManager,
    AuditLogger,
    SecretManager,
    User,
    UserRole,
    UserStore,
    redact_sensitive,
)


class GovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("examples/perf_agent_config.json")
        self.tester = User(
            username="tester",
            password_hash=UserStore.hash_password("StrongPassword1"),
            role=UserRole.TESTER,
        )
        self.approver = User(
            username="approver",
            password_hash=UserStore.hash_password("StrongPassword2"),
            role=UserRole.APPROVER,
        )

    def test_password_hash_is_salted_and_verifiable(self) -> None:
        first = UserStore.hash_password("StrongPassword1")
        second = UserStore.hash_password("StrongPassword1")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("pbkdf2_sha256$"))
        self.assertTrue(UserStore.verify_password("StrongPassword1", first))
        self.assertFalse(UserStore.verify_password("wrong", first))

    def test_approval_is_config_bound_and_one_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ApprovalManager(Path(tmp) / "approvals.json")
            request = manager.request(self.config, self.tester, "Launch rehearsal")
            approved = manager.approve(request.approval_id, self.approver)

            self.assertEqual(approved.status, "approved")
            self.assertEqual(
                manager.validate(approved.approval_id, self.config, "tester").approval_id,
                approved.approval_id,
            )

            changed = replace(self.config, release_id="changed-after-approval")
            with self.assertRaisesRegex(ValueError, "changed after approval"):
                manager.validate(approved.approval_id, changed, "tester")
            with self.assertRaisesRegex(ValueError, "only be used by its requester"):
                manager.validate(approved.approval_id, self.config, "another-user")

            manager.consume(approved.approval_id, "run-123")
            with self.assertRaisesRegex(ValueError, "not approved|already been used"):
                manager.validate(approved.approval_id, self.config, "tester")

    def test_requester_cannot_self_approve_unless_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ApprovalManager(Path(tmp) / "approvals.json")
            request = manager.request(self.config, self.approver)

            with self.assertRaisesRegex(ValueError, "cannot approve their own"):
                manager.approve(request.approval_id, self.approver)

    def test_audit_log_and_recursive_redaction_hide_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = AuditLogger(Path(tmp) / "audit.log")
            logger.log(
                "connector_test",
                self.tester,
                {
                    "api_key": "plain-key",
                    "nested": {"Authorization": "Bearer secret", "safe": "visible"},
                    "reference": "$secret:env:API_KEY",
                },
            )
            raw = (Path(tmp) / "audit.log").read_text(encoding="utf-8")

            self.assertNotIn("plain-key", raw)
            self.assertNotIn("Bearer secret", raw)
            self.assertNotIn("$secret:env:API_KEY", raw)
            self.assertIn("visible", raw)

    @patch.dict(os.environ, {"TEST_CONNECTOR_SECRET": "resolved-value"}, clear=False)
    def test_secret_reference_environment_provider(self) -> None:
        self.assertEqual(
            SecretManager.get_secret("$secret:env:TEST_CONNECTOR_SECRET"),
            "resolved-value",
        )

    def test_plaintext_connector_credentials_are_rejected(self) -> None:
        raw = {
            "application_name": "Secure App",
            "release_id": "1",
            "environment": {"name": "qa", "base_url": "https://example.com"},
            "monitoring_connectors": [
                {"name": "Prometheus", "type": "prometheus", "api_key": "plaintext"}
            ],
            "scenarios": [],
        }

        with self.assertRaisesRegex(ConfigError, "must use a secret reference"):
            parse_config(raw)

    def test_plaintext_nested_connector_token_is_rejected(self) -> None:
        raw = {
            "application_name": "Secure App",
            "release_id": "1",
            "environment": {"name": "qa", "base_url": "https://example.com"},
            "monitoring_connectors": [
                {
                    "name": "Prometheus",
                    "type": "prometheus",
                    "options": {"authorization_token": "plaintext"},
                }
            ],
            "scenarios": [],
        }

        with self.assertRaisesRegex(ConfigError, "must use a .secret: reference"):
            parse_config(raw)

    def test_guardrails_enforce_caps_allowlist_and_risky_approval(self) -> None:
        scenario = self.config.scenarios[1]
        restricted_environment = replace(
            self.config.environment,
            max_concurrent_users=100,
            allowed_hosts=["approved.example.com"],
            allow_risky_tests=True,
        )
        config = replace(self.config, environment=restricted_environment)

        with self.assertRaises(GuardrailViolation):
            Guardrail().validate(config, scenario, approve_risky=False)

        within_cap = replace(
            scenario,
            workload=replace(scenario.workload, concurrent_users=50),
        )
        with self.assertRaises(GuardrailViolation):
            Guardrail().validate(config, within_cap, approve_risky=False)

        allowed_environment = replace(
            restricted_environment,
            allowed_hosts=["preprod.example.com"],
        )
        with self.assertRaises(ApprovalRequired):
            Guardrail().validate(
                replace(config, environment=allowed_environment),
                within_cap,
                approve_risky=False,
            )

    def test_redact_sensitive_handles_secret_references(self) -> None:
        redacted = redact_sensitive(
            {
                "safe": "value",
                "connection_string": "postgres://user:password@host/db",
                "nested": {"token": "abc", "ref": "$secret:vault:path"},
            }
        )
        serialized = json.dumps(redacted)
        self.assertIn("value", serialized)
        self.assertNotIn("postgres://", serialized)
        self.assertNotIn("abc", serialized)
        self.assertNotIn("$secret:vault:path", serialized)


if __name__ == "__main__":
    unittest.main()
