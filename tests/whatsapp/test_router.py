"""whatsapp.router — payload normalisation + dispatch error isolation."""

from __future__ import annotations

import pytest

from whatsapp import router
from whatsapp.router import InboundMessage, build_inbound, iter_messages


SAMPLE_WEBHOOK = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "waba-id",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": "PNID"},
                        "contacts": [{"profile": {"name": "Jean"}, "wa_id": "33600000000"}],
                        "messages": [
                            {
                                "from": "33600000000",
                                "id": "wamid.abc",
                                "timestamp": "1728000000",
                                "type": "text",
                                "text": {"body": "Bonjour"},
                            }
                        ],
                    },
                }
            ],
        }
    ],
}


class TestIterMessages:
    def test_walks_payload(self):
        pairs = list(iter_messages(SAMPLE_WEBHOOK))
        assert len(pairs) == 1
        contact, message = pairs[0]
        assert contact["profile"]["name"] == "Jean"
        assert message["type"] == "text"

    def test_ignores_non_whatsapp_object(self):
        assert list(iter_messages({"object": "something_else"})) == []

    def test_handles_empty_payload(self):
        assert list(iter_messages({})) == []

    def test_handles_statuses_updates(self):
        # Meta sends these too — no 'messages' key; should yield nothing.
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}
            ],
        }
        assert list(iter_messages(payload)) == []


class TestBuildInbound:
    def test_normalises_text_message(self, fake_cursor_factory):
        contact, message = next(iter(iter_messages(SAMPLE_WEBHOOK)))
        inbound = build_inbound(contact, message, cursor_factory=fake_cursor_factory)
        assert inbound is not None
        assert inbound.phone_e164 == "+33600000000"
        assert inbound.wamid == "wamid.abc"
        assert inbound.msg_type == "text"
        assert inbound.payload == {"body": "Bonjour"}
        assert inbound.profile_name == "Jean"

    def test_returns_none_without_from_field(self, fake_cursor_factory):
        assert build_inbound({}, {"id": "x"}, cursor_factory=fake_cursor_factory) is None

    def test_unknown_type_has_empty_payload(self, fake_cursor_factory):
        msg = {"from": "33600000000", "id": "w", "type": "sticker"}
        inbound = build_inbound({}, msg, cursor_factory=fake_cursor_factory)
        assert inbound is not None
        assert inbound.msg_type == "sticker"
        assert inbound.payload == {}


class TestDispatchErrorIsolation:
    def test_one_bad_message_does_not_kill_the_batch(self, monkeypatch, fake_cursor_factory):
        """If one message raises, subsequent messages should still run."""
        seen: list[str] = []

        # Stash the cursor_factory in an instance attribute so Python doesn't
        # bind it as a method.
        class StubDeps:
            def __init__(self, cursor_factory):
                self.cursor_factory = cursor_factory

        stub = StubDeps(fake_cursor_factory)

        # Patch the flow handler to raise on the first message.
        from whatsapp import flows as _flows

        calls = {"i": 0}

        def handler(inbound, *, deps):
            calls["i"] += 1
            if calls["i"] == 1:
                raise RuntimeError("simulated flow error")
            seen.append(inbound.wamid)

        monkeypatch.setattr(_flows, "handle_inbound", handler)
        monkeypatch.setattr(
            _flows.FlowDeps, "default", classmethod(lambda cls: stub)
        )

        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"from": "33111", "id": "w1", "type": "text", "text": {"body": "hi"}},
                                    {"from": "33222", "id": "w2", "type": "text", "text": {"body": "yo"}},
                                ]
                            }
                        }
                    ]
                }
            ],
        }
        results = router.dispatch_webhook_event(payload)
        assert any(r["status"] == "error" for r in results)
        assert any(r["status"] == "ok" for r in results)
        assert seen == ["w2"]
