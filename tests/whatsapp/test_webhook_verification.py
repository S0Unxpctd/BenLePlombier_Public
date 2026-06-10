"""QA contract item 1 — webhook signature verification + GET challenge."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from whatsapp.webhook import create_app, verify_signature
from tests.whatsapp.conftest import TEST_SECRET, TEST_VERIFY, sign


class TestSignatureHelper:
    def test_valid_signature_returns_true(self):
        body = b'{"hello":"world"}'
        assert verify_signature(body, sign(body), TEST_SECRET) is True

    def test_invalid_signature_returns_false(self):
        body = b'{"hello":"world"}'
        assert verify_signature(body, "sha256=deadbeef", TEST_SECRET) is False

    def test_missing_prefix_returns_false(self):
        body = b"x"
        assert verify_signature(body, "not-a-sig", TEST_SECRET) is False

    def test_empty_header_returns_false(self):
        assert verify_signature(b"x", "", TEST_SECRET) is False

    def test_empty_secret_returns_false(self):
        body = b"x"
        assert verify_signature(body, sign(body), "") is False

    def test_constant_time_compare_no_leaks_on_prefix_match(self):
        body = b"{}"
        correct = sign(body)
        # Mutate the last char — should still fail.
        mutated = correct[:-1] + ("0" if correct[-1] != "0" else "1")
        assert verify_signature(body, mutated, TEST_SECRET) is False


class TestGetVerification:
    def test_correct_token_echoes_challenge(self, test_config):
        app = create_app(cfg=test_config, dispatch=lambda _p: None)
        client = TestClient(app)
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": TEST_VERIFY,
                "hub.challenge": "abc123",
            },
        )
        assert r.status_code == 200
        assert r.text == "abc123"

    def test_wrong_token_returns_403(self, test_config):
        app = create_app(cfg=test_config, dispatch=lambda _p: None)
        client = TestClient(app)
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "abc123",
            },
        )
        assert r.status_code == 403

    def test_non_subscribe_mode_returns_403(self, test_config):
        app = create_app(cfg=test_config, dispatch=lambda _p: None)
        client = TestClient(app)
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": TEST_VERIFY,
                "hub.challenge": "abc123",
            },
        )
        assert r.status_code == 403


class TestPostIngest:
    def test_valid_signature_acked_and_dispatched(self, test_config):
        received: list[dict] = []
        app = create_app(cfg=test_config, dispatch=lambda p: received.append(p))
        client = TestClient(app)
        body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
        r = client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": sign(body)},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "received"}
        assert received == [{"object": "whatsapp_business_account", "entry": []}]

    def test_invalid_signature_rejected_403(self, test_config):
        received: list[dict] = []
        app = create_app(cfg=test_config, dispatch=lambda p: received.append(p))
        client = TestClient(app)
        body = json.dumps({"object": "whatsapp_business_account"}).encode()
        r = client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=0000"},
        )
        assert r.status_code == 403
        assert received == []

    def test_missing_signature_header_rejected_403(self, test_config):
        app = create_app(cfg=test_config, dispatch=lambda _p: None)
        client = TestClient(app)
        r = client.post("/webhook", content=b"{}")
        assert r.status_code == 403

    def test_malformed_json_still_acks_200(self, test_config):
        """Meta retries on non-200; don't let broken payloads cause retries."""
        app = create_app(cfg=test_config, dispatch=lambda _p: None)
        client = TestClient(app)
        body = b"not-json"
        r = client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": sign(body)},
        )
        assert r.status_code == 200

    def test_dispatch_exception_does_not_break_ack(self, test_config):
        def boom(_p):
            raise RuntimeError("dispatch exploded")

        app = create_app(cfg=test_config, dispatch=boom)
        client = TestClient(app)
        body = b"{}"
        r = client.post(
            "/webhook",
            content=body,
            headers={"X-Hub-Signature-256": sign(body)},
        )
        # Meta must get 200 regardless of our internal bugs, so it stops retrying
        # — we instead rely on Sentry to track the exception.
        assert r.status_code == 200

    def test_health_endpoint_alive(self, test_config):
        app = create_app(cfg=test_config, dispatch=lambda _p: None)
        client = TestClient(app)
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
