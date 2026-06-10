"""whatsapp.users — phone normalisation + deterministic user_id + mapping."""

from __future__ import annotations

import pytest

from whatsapp import users


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("33600000000", "+33600000000"),
            ("+33600000000", "+33600000000"),
            ("+33 7 67 96 46 62", "+33600000000"),
            ("  +33-7-67-96-46-62  ", "+33600000000"),
        ],
    )
    def test_normalises_known_shapes(self, raw, expected):
        assert users.normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "not-a-phone", "+++"])
    def test_rejects_garbage(self, raw):
        with pytest.raises(ValueError):
            users.normalize_phone(raw)

    def test_rejects_none(self):
        with pytest.raises(ValueError):
            users.normalize_phone(None)  # type: ignore[arg-type]


class TestPhoneToUserId:
    def test_deterministic(self):
        a = users.phone_to_user_id("+33600000000")
        b = users.phone_to_user_id("+33600000000")
        assert a == b

    def test_not_colliding_with_telegram_range(self):
        uid = users.phone_to_user_id("+33600000000")
        # Telegram user ids are ≤ ~10^10 in practice; our allocator starts
        # at 10^12 to keep them fully separated.
        assert uid >= 10**12
        assert uid < 2 * 10**12

    def test_different_phones_yield_different_ids(self):
        a = users.phone_to_user_id("+33600000000")
        b = users.phone_to_user_id("+33767964663")
        assert a != b

    def test_idempotent_under_formatting(self):
        a = users.phone_to_user_id("+33 7 67 96 46 62")
        b = users.phone_to_user_id("+33600000000")
        assert a == b


class TestMappingPostgres:
    def test_get_or_create_is_idempotent(self, fake_cursor_factory, fake_store):
        uid1 = users.get_or_create_user(
            "+33600000000", cursor_factory=fake_cursor_factory
        )
        uid2 = users.get_or_create_user(
            "+33600000000", cursor_factory=fake_cursor_factory
        )
        assert uid1 == uid2
        assert len(fake_store["whatsapp_users"]) == 1

    def test_roundtrip_phone_user(self, fake_cursor_factory):
        uid = users.get_or_create_user(
            "+33600000000", cursor_factory=fake_cursor_factory
        )
        assert users.get_phone_from_user_id(uid, cursor_factory=fake_cursor_factory) == "+33600000000"
        assert users.get_user_id_from_phone("+33600000000", cursor_factory=fake_cursor_factory) == uid

    def test_get_user_id_absent_returns_none(self, fake_cursor_factory):
        assert users.get_user_id_from_phone("+33111111111", cursor_factory=fake_cursor_factory) is None

    def test_get_phone_absent_returns_none(self, fake_cursor_factory):
        assert users.get_phone_from_user_id(999, cursor_factory=fake_cursor_factory) is None
