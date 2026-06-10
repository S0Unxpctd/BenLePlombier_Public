"""QA contract item 2 — every send_* produces a correctly-shaped payload.

We build an ``httpx.MockTransport`` that captures the outbound request and
returns a Meta-like success response. No real network traffic. No env vars.
"""

from __future__ import annotations

import json

import httpx
import pytest

from whatsapp import send
from whatsapp.send import SendError


# ─────────────────────────────────────────────────────────────────────────────
# Transport fixture
# ─────────────────────────────────────────────────────────────────────────────


def _make_transport(
    status: int = 200,
    body: dict | None = None,
    captured: list | None = None,
    fail_first_n: int = 0,
):
    captured_list = captured if captured is not None else []
    retries = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_list.append(request)
        if retries["count"] < fail_first_n:
            retries["count"] += 1
            return httpx.Response(status_code=503, text="try again")
        return httpx.Response(
            status_code=status, json=body or {"messages": [{"id": "wamid.x"}]}
        )

    return httpx.MockTransport(handler), captured_list


@pytest.fixture
def mock_client():
    def make(**kw):
        transport, captured = _make_transport(**kw)
        return httpx.Client(transport=transport), captured

    return make


# ─────────────────────────────────────────────────────────────────────────────
# send_text
# ─────────────────────────────────────────────────────────────────────────────


class TestSendText:
    def test_basic_payload_shape(self, mock_client, test_config):
        client, captured = mock_client()
        resp = send.send_text("+33600000000", "Bonjour !", client=client, cfg=test_config)
        assert resp == {"messages": [{"id": "wamid.x"}]}
        assert len(captured) == 1
        payload = json.loads(captured[0].content)
        assert payload["messaging_product"] == "whatsapp"
        assert payload["to"] == "33600000000"  # no leading +
        assert payload["type"] == "text"
        assert payload["text"]["body"] == "Bonjour !"
        assert payload["text"]["preview_url"] is False

    def test_preview_url_opt_in(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_text("+33600000000", "https://example.fr", preview_url=True, client=client, cfg=test_config)
        payload = json.loads(captured[0].content)
        assert payload["text"]["preview_url"] is True

    def test_retries_on_503(self, mock_client, test_config):
        client, captured = mock_client(fail_first_n=2)
        resp = send.send_text("+33600000000", "hi", client=client, cfg=test_config)
        assert resp["messages"][0]["id"] == "wamid.x"
        assert len(captured) == 3  # two 503s + one success

    def test_raises_on_persistent_4xx(self, mock_client, test_config):
        client, captured = mock_client(status=400, body={"error": "bad"})
        with pytest.raises(SendError) as exc:
            send.send_text("+33600000000", "hi", client=client, cfg=test_config)
        assert exc.value.status == 400
        assert len(captured) == 1  # no retry on 4xx

    def test_authorization_header_carries_token(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_text("+33600000000", "hi", client=client, cfg=test_config)
        assert captured[0].headers["authorization"] == f"Bearer {test_config.token}"


# ─────────────────────────────────────────────────────────────────────────────
# reply_buttons
# ─────────────────────────────────────────────────────────────────────────────


class TestReplyButtons:
    def test_shape(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_reply_buttons(
            "+33600000000",
            "Choose:",
            [
                {"id": "a", "title": "A"},
                {"id": "b", "title": "B"},
            ],
            client=client,
            cfg=test_config,
        )
        payload = json.loads(captured[0].content)
        assert payload["type"] == "interactive"
        assert payload["interactive"]["type"] == "button"
        btns = payload["interactive"]["action"]["buttons"]
        assert len(btns) == 2
        assert btns[0] == {"type": "reply", "reply": {"id": "a", "title": "A"}}

    def test_rejects_zero_buttons(self, test_config):
        with pytest.raises(ValueError):
            send.send_reply_buttons("+33600000000", "body", [], cfg=test_config)

    def test_rejects_more_than_three_buttons(self, test_config):
        btns = [{"id": str(i), "title": f"B{i}"} for i in range(4)]
        with pytest.raises(ValueError):
            send.send_reply_buttons("+33600000000", "body", btns, cfg=test_config)

    def test_rejects_button_missing_id_or_title(self, test_config):
        with pytest.raises(ValueError):
            send.send_reply_buttons(
                "+33600000000", "body", [{"id": "", "title": "x"}], cfg=test_config
            )

    def test_truncates_long_title(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_reply_buttons(
            "+33600000000",
            "body",
            [{"id": "a", "title": "X" * 50}],
            client=client,
            cfg=test_config,
        )
        payload = json.loads(captured[0].content)
        assert len(payload["interactive"]["action"]["buttons"][0]["reply"]["title"]) == 20


# ─────────────────────────────────────────────────────────────────────────────
# list_message
# ─────────────────────────────────────────────────────────────────────────────


class TestListMessage:
    def test_shape(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_list_message(
            "+33600000000",
            "Menu",
            "Choose",
            [
                {
                    "title": "Actions",
                    "rows": [
                        {"id": "r1", "title": "Row 1", "description": "First"},
                        {"id": "r2", "title": "Row 2"},
                    ],
                }
            ],
            client=client,
            cfg=test_config,
        )
        payload = json.loads(captured[0].content)
        assert payload["type"] == "interactive"
        assert payload["interactive"]["type"] == "list"
        assert payload["interactive"]["action"]["button"] == "Choose"
        sections = payload["interactive"]["action"]["sections"]
        assert sections[0]["rows"][0]["description"] == "First"
        assert "description" not in sections[0]["rows"][1]

    def test_rejects_zero_rows(self, test_config):
        with pytest.raises(ValueError):
            send.send_list_message(
                "+33600000000", "Menu", "Choose",
                [{"title": "Empty", "rows": []}], cfg=test_config
            )

    def test_rejects_more_than_ten_rows(self, test_config):
        rows = [{"id": str(i), "title": str(i)} for i in range(11)]
        with pytest.raises(ValueError):
            send.send_list_message(
                "+33600000000", "Menu", "Choose",
                [{"title": "A", "rows": rows}], cfg=test_config
            )


# ─────────────────────────────────────────────────────────────────────────────
# send_document / audio / image
# ─────────────────────────────────────────────────────────────────────────────


class TestSendDocument:
    def test_shape(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_document(
            "+33600000000", "media-id-123", "devis.pdf", caption="Votre devis",
            client=client, cfg=test_config,
        )
        payload = json.loads(captured[0].content)
        assert payload["type"] == "document"
        assert payload["document"] == {
            "id": "media-id-123",
            "filename": "devis.pdf",
            "caption": "Votre devis",
        }

    def test_no_caption(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_document(
            "+33600000000", "mid", "x.pdf", client=client, cfg=test_config
        )
        payload = json.loads(captured[0].content)
        assert "caption" not in payload["document"]


class TestSendAudioImage:
    def test_audio_shape(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_audio("+33600000000", "mid", client=client, cfg=test_config)
        payload = json.loads(captured[0].content)
        assert payload["type"] == "audio"
        assert payload["audio"] == {"id": "mid"}

    def test_image_shape_with_caption(self, mock_client, test_config):
        client, captured = mock_client()
        send.send_image("+33600000000", "mid", caption="Photo", client=client, cfg=test_config)
        payload = json.loads(captured[0].content)
        assert payload["type"] == "image"
        assert payload["image"] == {"id": "mid", "caption": "Photo"}


class TestMarkTyping:
    def test_mark_typing_is_noop(self, test_config):
        # Must not blow up; must not raise; must not hit the network.
        out = send.mark_typing("+33600000000", cfg=test_config)
        assert out == {"skipped": True}
