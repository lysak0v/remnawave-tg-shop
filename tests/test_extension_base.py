"""Paid periods and bonus days are added to the panel expiry, not to the local DB copy."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services import subscription_service as svc_module
from bot.services.subscription_service import (
    PanelUserLink,
    SubscriptionService,
    _parse_panel_datetime,
    resolve_extension_base,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "panel, local, created, expected",
    [
        # panel is the source of truth, even when the local copy is later (admin shortened it)
        (NOW + timedelta(days=1), NOW + timedelta(days=30), False, NOW + timedelta(days=1)),
        # admin prolonged in the panel, local copy is stale
        (NOW + timedelta(days=40), NOW + timedelta(days=5), False, NOW + timedelta(days=40)),
        # expired in the panel -> period starts now, regardless of a stale local date
        (NOW - timedelta(days=3), NOW + timedelta(days=10), False, NOW),
        # brand-new panel user carries only a placeholder expiry
        (NOW + timedelta(days=1), None, True, NOW),
        # panel gave no date -> fall back to the local end date
        (None, NOW + timedelta(days=7), False, NOW + timedelta(days=7)),
        (None, NOW - timedelta(days=7), False, NOW),
        (None, None, False, NOW),
    ],
)
def test_resolve_extension_base(panel, local, created, expected):
    assert resolve_extension_base(NOW, panel, local, created) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-11-26T16:15:00.000Z", datetime(2026, 11, 26, 16, 15, tzinfo=timezone.utc)),
        ("2026-11-26T16:15:00+03:00", datetime(2026, 11, 26, 13, 15, tzinfo=timezone.utc)),
        ("2026-11-26T16:15:00", datetime(2026, 11, 26, 16, 15, tzinfo=timezone.utc)),
        (None, None),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_parse_panel_datetime(raw, expected):
    assert _parse_panel_datetime(raw) == expected


def _service(settings, panel):
    return SubscriptionService(settings, panel)


def _link(expire_at, created=False):
    return PanelUserLink(443, "short", "short", False,
                         panel_user={"id": 443, "expireAt": expire_at}, created_now=created)


def test_extension_base_for_prefers_panel_over_local(settings):
    service = _service(settings, panel=None)
    panel_expiry = datetime.now(timezone.utc) + timedelta(days=2)
    local = datetime.now(timezone.utc) + timedelta(days=20)
    base = service._extension_base_for(1, _link(panel_expiry.isoformat().replace("+00:00", "Z")), local)
    assert abs((base - panel_expiry).total_seconds()) < 1


def test_extension_base_for_new_panel_user_starts_now(settings):
    service = _service(settings, panel=None)
    placeholder = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    before = datetime.now(timezone.utc)
    base = service._extension_base_for(1, _link(placeholder, created=True), None)
    assert before <= base <= datetime.now(timezone.utc)


class FakePanel:
    def __init__(self, found=None, created=None):
        self.found = found
        self.created = created
        self.create_calls = 0

    async def get_users_by_filter(self, telegram_id=None, username=None, **_):
        return [self.found] if self.found else []

    async def get_user_by_panel_id(self, panel_user_id, **_):
        return self.found

    async def create_panel_user(self, **_):
        self.create_calls += 1
        return {"response": self.created}

    async def update_user_details_on_panel(self, *_, **__):
        return {}


@pytest.fixture
def patched_user_dal(monkeypatch):
    async def get_user_by_panel_id(session, panel_user_id):
        return None

    async def update_user(session, user_id, data):
        return None

    monkeypatch.setattr(svc_module.user_dal, "get_user_by_panel_id", get_user_by_panel_id)
    monkeypatch.setattr(svc_module.user_dal, "update_user", update_user)


async def test_ensure_panel_user_marks_created(settings, patched_user_dal):
    created = {"id": 900, "telegramId": 77, "shortUuid": "s900", "expireAt": "2026-10-26T12:00:00Z"}
    panel = FakePanel(found=None, created=created)
    db_user = SimpleNamespace(user_id=77, panel_user_id=None, username=None, first_name=None, last_name=None)
    link = await _service(settings, panel)._ensure_panel_user(None, 77, db_user)
    assert panel.create_calls == 1
    assert link.panel_user_id == 900 and link.created_now is True
    assert link.panel_user["expireAt"] == created["expireAt"]


async def test_ensure_panel_user_existing_is_not_created(settings, patched_user_dal):
    found = {"id": 443, "telegramId": 77, "shortUuid": "s443", "expireAt": "2026-11-26T16:15:00Z"}
    panel = FakePanel(found=found)
    db_user = SimpleNamespace(user_id=77, panel_user_id=443, username=None, first_name=None, last_name=None)
    link = await _service(settings, panel)._ensure_panel_user(None, 77, db_user)
    assert panel.create_calls == 0
    assert link.panel_user_id == 443 and link.created_now is False
    assert link.panel_user["expireAt"] == found["expireAt"]


async def test_legacy_wrapper_keeps_four_tuple(settings, patched_user_dal):
    found = {"id": 443, "telegramId": 77, "shortUuid": "s443", "expireAt": "2026-11-26T16:15:00Z"}
    db_user = SimpleNamespace(user_id=77, panel_user_id=443, username=None, first_name=None, last_name=None)
    result = await _service(settings, FakePanel(found=found))._get_or_create_panel_user_link_details(
        None, 77, db_user)
    assert result == (443, "s443", "s443", False)
