"""The panel is the source of truth: the bot adds to what the admin set there and never wipes it."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bot.handlers.user.subscription import core as core_handlers
from bot.services import subscription_service as svc_module
from bot.services.subscription_service import (
    SubscriptionService,
    merge_squad_uuids,
    panel_user_is_active,
)
from bot.utils.panel_description import merge_panel_description, profile_text
from config.settings import Settings

NOW = datetime(2026, 11, 16, 7, 21, tzinfo=timezone.utc)  # 10:21 MSK


# --- description -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "username, first, last, expected",
    [
        ("john", "John", "Doe", "john | John Doe"),
        ("@john", "John", None, "john | John"),
        (None, "John", "Doe", "- | John Doe"),
        ("john", None, None, "john"),
        (None, None, " ", ""),
    ],
)
def test_profile_text(username, first, last, expected):
    assert profile_text(username, first, last) == expected


def test_description_for_new_user():
    assert merge_panel_description(None, "john", "John", None, "Europe/Moscow", NOW) == \
        "[2026-11-16 10:21 MSK] john | John"


def test_description_keeps_admin_notes_and_appends_change():
    current = "мама, оплатила наличными\n[2026-10-01 12:00 MSK] john | John"
    assert merge_panel_description(current, "johnny", "John", None, "Europe/Moscow", NOW) == (
        "мама, оплатила наличными\n[2026-10-01 12:00 MSK] john | John\n"
        "[2026-11-16 10:21 MSK] johnny | John"
    )


def test_description_unchanged_profile_is_not_rewritten():
    current = "заметка\n[2026-10-01 12:00 MSK] john | John"
    assert merge_panel_description(current, "john", "John", None, "Europe/Moscow", NOW) is None


def test_description_in_legacy_format_is_not_rewritten():
    # written by earlier versions: username / first / last name on separate lines
    assert merge_panel_description("john\nJohn\nDoe", "john", "John", "Doe", "UTC", NOW) is None


def test_description_legacy_text_is_kept_when_profile_changed():
    assert merge_panel_description("john\nJohn", "john", "Johnny", None, "UTC", NOW) == \
        "john\nJohn\n[2026-11-16 07:21 UTC] john | Johnny"


def test_description_empty_profile_changes_nothing():
    assert merge_panel_description("notes", None, None, None, "UTC", NOW) is None


def test_settings_description_timezone(monkeypatch):
    monkeypatch.setenv("PANEL_DESCRIPTION_TIMEZONE", "Europe/Moscow")
    assert Settings().PANEL_DESCRIPTION_TIMEZONE == "Europe/Moscow"
    monkeypatch.setenv("PANEL_DESCRIPTION_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValidationError):
        Settings()


# --- squads and status -------------------------------------------------------------------

SQ_BOT = "11111111-1111-1111-1111-111111111111"
SQ_CDN = "22222222-2222-2222-2222-222222222222"
SQ_EXT = "33333333-3333-3333-3333-333333333333"


@pytest.mark.parametrize(
    "current, configured, expected",
    [
        ([{"uuid": SQ_CDN, "name": "cdn"}], [SQ_BOT], [SQ_CDN, SQ_BOT]),
        ([{"uuid": SQ_CDN}, {"uuid": SQ_BOT}], [SQ_BOT], None),
        ([], [SQ_BOT], [SQ_BOT]),
        ([SQ_CDN], [SQ_BOT], [SQ_CDN, SQ_BOT]),
        ([{"uuid": SQ_CDN}], None, None),
    ],
)
def test_merge_squad_uuids(current, configured, expected):
    assert merge_squad_uuids({"activeInternalSquads": current}, configured) == expected


def _settings(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_payload_adds_squads_and_keeps_disabled(monkeypatch):
    service = SubscriptionService(
        _settings(monkeypatch, USER_SQUAD_UUIDS=SQ_BOT, USER_EXTERNAL_SQUAD_UUID=SQ_EXT), None)
    panel_user = {"id": 1, "status": "DISABLED", "activeInternalSquads": [{"uuid": SQ_CDN}],
                  "externalSquadUuid": None}
    payload = service._build_panel_update_payload(panel_user=panel_user, expire_at=NOW, status="ACTIVE")
    assert "status" not in payload
    assert payload["activeInternalSquads"] == [SQ_CDN, SQ_BOT]
    assert payload["externalSquadUuid"] == SQ_EXT
    assert payload["expireAt"] == "2026-11-16T07:21:00.000Z"


def test_payload_keeps_external_squad_set_by_admin(monkeypatch):
    service = SubscriptionService(_settings(monkeypatch, USER_EXTERNAL_SQUAD_UUID=SQ_EXT), None)
    payload = service._build_panel_update_payload(
        panel_user={"status": "ACTIVE", "externalSquadUuid": SQ_CDN}, status="ACTIVE")
    assert payload["status"] == "ACTIVE"
    assert "externalSquadUuid" not in payload


def test_payload_without_panel_record_sends_no_squads(monkeypatch):
    service = SubscriptionService(_settings(monkeypatch, USER_SQUAD_UUIDS=SQ_BOT), None)
    payload = service._build_panel_update_payload(panel_user=None, status="ACTIVE")
    assert payload == {"status": "ACTIVE"}


@pytest.mark.parametrize(
    "panel_user, expected",
    [
        ({"status": "ACTIVE", "expireAt": "2026-12-01T00:00:00Z"}, True),
        ({"status": "ACTIVE", "expireAt": "2026-11-01T00:00:00Z"}, False),
        ({"status": "EXPIRED", "expireAt": "2026-12-01T00:00:00Z"}, False),
        ({"status": "DISABLED", "expireAt": "2026-12-01T00:00:00Z"}, False),
        (None, False),
    ],
)
def test_panel_user_is_active(panel_user, expected):
    assert panel_user_is_active(panel_user, NOW) is expected


# --- panel-aware checks ------------------------------------------------------------------


class LookupPanel:
    """`by_tg`: list of panel users, or None for an API failure."""

    def __init__(self, by_tg, by_id=None):
        self.by_tg = by_tg
        self.by_id = by_id
        self.updates = []

    async def get_users_by_filter(self, telegram_id=None, username=None, **_):
        return self.by_tg

    async def get_user_by_panel_id(self, panel_user_id, **_):
        return self.by_id

    async def update_user_details_on_panel(self, panel_user_id, payload):
        self.updates.append(payload)
        return {"id": panel_user_id, "subscriptionUrl": "https://sub/x", "shortUuid": "s1"}


@pytest.fixture
def local_db(monkeypatch):
    state = {"has_sub": False, "active_sub": None, "db_user": SimpleNamespace(
        user_id=77, panel_user_id=None, username="john", first_name="John", last_name=None)}

    async def has_any_subscription_for_user(session, user_id):
        return state["has_sub"]

    async def get_user_by_id(session, user_id):
        return state["db_user"]

    async def get_active_subscription_by_user_id(session, user_id, panel_user_id=None):
        return state["active_sub"]

    monkeypatch.setattr(svc_module.subscription_dal, "has_any_subscription_for_user",
                        has_any_subscription_for_user)
    monkeypatch.setattr(svc_module.subscription_dal, "get_active_subscription_by_user_id",
                        get_active_subscription_by_user_id)
    monkeypatch.setattr(svc_module.user_dal, "get_user_by_id", get_user_by_id)
    return state


FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()


@pytest.mark.parametrize(
    "has_local, by_tg, expected",
    [
        (True, [], True),
        # added by the admin in the panel, unknown to the bot
        (False, [{"id": 5, "status": "EXPIRED"}], True),
        (False, [], False),
        # panel unreachable: do not offer a trial on top of an unknown subscription
        (False, None, True),
    ],
)
async def test_has_had_any_subscription(settings, local_db, has_local, by_tg, expected):
    local_db["has_sub"] = has_local
    service = SubscriptionService(settings, LookupPanel(by_tg))
    assert await service.has_had_any_subscription(None, 77) is expected


@pytest.mark.parametrize(
    "by_tg, expected",
    [
        ([{"id": 5, "status": "ACTIVE", "expireAt": FUTURE}], True),
        ([{"id": 5, "status": "ACTIVE", "expireAt": PAST}], False),
        ([], False),
        (None, False),
    ],
)
async def test_has_active_subscription_consults_panel(settings, local_db, by_tg, expected):
    service = SubscriptionService(settings, LookupPanel(by_tg))
    assert await service.has_active_subscription(None, 77) is expected


async def test_has_active_subscription_local_is_enough(settings, local_db):
    local_db["db_user"].panel_user_id = 5
    local_db["active_sub"] = SimpleNamespace(
        is_active=True, end_date=datetime.now(timezone.utc) + timedelta(days=3))
    service = SubscriptionService(settings, LookupPanel(None))
    assert await service.has_active_subscription(None, 77) is True


async def test_find_panel_user_falls_back_to_local_panel_id(settings, local_db):
    local_db["db_user"].panel_user_id = 5
    service = SubscriptionService(settings, LookupPanel([], by_id={"id": 5, "status": "DISABLED"}))
    assert await service.find_panel_user(None, 77) == (True, {"id": 5, "status": "DISABLED"})
    assert await service.is_disabled_in_panel(None, 77) is True


@pytest.mark.parametrize("by_tg, expected", [
    ([{"id": 5, "status": "DISABLED"}], True),
    ([{"id": 5, "status": "ACTIVE"}], False),
    ([], False),
    (None, False),  # unreachable panel does not block payments
])
async def test_is_disabled_in_panel(settings, local_db, by_tg, expected):
    service = SubscriptionService(settings, LookupPanel(by_tg))
    assert await service.is_disabled_in_panel(None, 77) is expected


# --- payment entry -----------------------------------------------------------------------


class FakeCallback:
    def __init__(self):
        self.from_user = SimpleNamespace(id=77)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def test_payment_refused_for_disabled_user(settings, local_db, monkeypatch):
    monkeypatch.setattr(core_handlers.types, "CallbackQuery", FakeCallback)
    service = SubscriptionService(settings, LookupPanel([{"id": 5, "status": "DISABLED"}]))
    callback = FakeCallback()
    refused = await core_handlers.refuse_if_disabled_in_panel(callback, service, None, lambda k: k)
    assert refused is True
    assert callback.answers == [("account_disabled_in_panel", True)]


async def test_payment_allowed_for_active_user(settings, local_db, monkeypatch):
    monkeypatch.setattr(core_handlers.types, "CallbackQuery", FakeCallback)
    service = SubscriptionService(settings, LookupPanel([{"id": 5, "status": "ACTIVE"}]))
    callback = FakeCallback()
    assert await core_handlers.refuse_if_disabled_in_panel(callback, service, None, lambda k: k) is False
    assert callback.answers == []


# --- paid activation ---------------------------------------------------------------------


@pytest.fixture
def activation_dal(monkeypatch, local_db):
    local_db["db_user"].panel_user_id = 5

    async def get_user_by_panel_id(session, panel_user_id):
        return None

    async def update_user(session, user_id, data):
        return None

    async def deactivate_other_active_subscriptions(session, panel_user_id, link_id):
        return None

    async def upsert_subscription(session, data):
        return SimpleNamespace(subscription_id=1, **data)

    monkeypatch.setattr(svc_module.user_dal, "get_user_by_panel_id", get_user_by_panel_id)
    monkeypatch.setattr(svc_module.user_dal, "update_user", update_user)
    monkeypatch.setattr(svc_module.subscription_dal, "deactivate_other_active_subscriptions",
                        deactivate_other_active_subscriptions)
    monkeypatch.setattr(svc_module.subscription_dal, "upsert_subscription", upsert_subscription)


async def test_paid_activation_respects_panel_state(monkeypatch, activation_dal):
    settings = _settings(monkeypatch, USER_SQUAD_UUIDS=SQ_BOT,
                         PANEL_DESCRIPTION_TIMEZONE="Europe/Moscow")
    panel_user = {
        "id": 5, "telegramId": 77, "shortUuid": "s5", "status": "DISABLED",
        "expireAt": FUTURE, "activeInternalSquads": [{"uuid": SQ_CDN}],
        "description": "друг, CDN-тест",
    }
    panel = LookupPanel([panel_user], by_id=panel_user)
    result = await SubscriptionService(settings, panel).activate_subscription(
        None, 77, months=1, payment_amount=100.0, payment_db_id=1, provider="stars")
    assert result and result["is_active"] is True
    sent = panel.updates[-1]
    assert "status" not in sent  # stays DISABLED
    assert sent["activeInternalSquads"] == [SQ_CDN, SQ_BOT]
    assert sent["description"].startswith("друг, CDN-тест\n[")
    assert sent["description"].endswith("] john | John")
    # extended from the panel expiry
    assert sent["expireAt"] > FUTURE[:10]
