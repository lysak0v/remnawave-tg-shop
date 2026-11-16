"""Trial and paid activations tag the panel user; admin-set tags (e.g. FREE) are kept."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bot.services import subscription_service as svc_module
from bot.services.subscription_service import SubscriptionService, resolve_panel_tag
from config.settings import Settings


@pytest.mark.parametrize(
    "desired, current, protected, expected",
    [
        ("TRIAL", None, ["FREE"], "TRIAL"),
        ("PAID", "TRIAL", ["FREE"], "PAID"),
        ("PAID", "OTHER", ["FREE"], "PAID"),
        # admin-set tag is never replaced
        ("PAID", "FREE", ["FREE"], None),
        ("TRIAL", "FREE", ["FREE", "VIP"], None),
        # nothing to change
        ("PAID", "PAID", ["FREE"], None),
        # tagging disabled
        (None, None, ["FREE"], None),
        (None, "TRIAL", [], None),
    ],
)
def test_resolve_panel_tag(desired, current, protected, expected):
    assert resolve_panel_tag(desired, current, protected) == expected


def test_settings_normalize_tags(monkeypatch):
    monkeypatch.setenv("TRIAL_USER_TAG", " trial ")
    monkeypatch.setenv("PAID_USER_TAG", "paid")
    monkeypatch.setenv("PROTECTED_USER_TAGS", "free, vip")
    s = Settings()
    assert s.TRIAL_USER_TAG == "TRIAL"
    assert s.PAID_USER_TAG == "PAID"
    assert s.parsed_protected_user_tags == ["FREE", "VIP"]


def test_settings_tags_disabled_by_default(settings):
    assert settings.TRIAL_USER_TAG is None
    assert settings.PAID_USER_TAG is None
    assert settings.parsed_protected_user_tags == []


@pytest.mark.parametrize("bad", ["bad-tag", "TOO_LONG_TAG_NAME_17", "тест"])
def test_settings_reject_invalid_tag(monkeypatch, bad):
    monkeypatch.setenv("PAID_USER_TAG", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_reject_invalid_protected_tag(monkeypatch):
    monkeypatch.setenv("PROTECTED_USER_TAGS", "FREE,not ok")
    with pytest.raises(ValidationError):
        Settings()


class RecordingPanel:
    """Panel fake: `exists=False` means the user is created by the bot in this call."""

    def __init__(self, panel_user, exists=True):
        self.panel_user = panel_user
        self.exists = exists
        self.updates = []

    async def get_users_by_filter(self, telegram_id=None, username=None, **_):
        return [self.panel_user] if self.exists else []

    async def get_user_by_panel_id(self, panel_user_id, **_):
        return self.panel_user if self.exists else None

    async def create_panel_user(self, **_):
        self.exists = True
        return {"response": self.panel_user}

    async def update_user_details_on_panel(self, panel_user_id, payload):
        self.updates.append(payload)
        return {"id": panel_user_id, "subscriptionUrl": "https://sub/x", "shortUuid": "s443"}


class FakeSession:
    async def commit(self):
        pass

    async def rollback(self):
        pass


DB_USER = SimpleNamespace(user_id=77, panel_user_id=None, username="u", first_name="f", last_name="l")


@pytest.fixture
def patched_dal(monkeypatch):
    async def get_user_by_id(session, user_id):
        return DB_USER

    async def get_user_by_panel_id(session, panel_user_id):
        return None

    async def update_user(session, user_id, data):
        return None

    async def deactivate_other_active_subscriptions(session, panel_user_id, link_id):
        return None

    async def upsert_subscription(session, data):
        return SimpleNamespace(subscription_id=1, **data)

    async def get_active_subscription_by_user_id(session, user_id, panel_user_id):
        return None

    monkeypatch.setattr(svc_module.user_dal, "get_user_by_id", get_user_by_id)
    monkeypatch.setattr(svc_module.user_dal, "get_user_by_panel_id", get_user_by_panel_id)
    monkeypatch.setattr(svc_module.user_dal, "update_user", update_user)
    monkeypatch.setattr(svc_module.subscription_dal, "deactivate_other_active_subscriptions",
                        deactivate_other_active_subscriptions)
    monkeypatch.setattr(svc_module.subscription_dal, "upsert_subscription", upsert_subscription)
    monkeypatch.setattr(svc_module.subscription_dal, "get_active_subscription_by_user_id",
                        get_active_subscription_by_user_id)


def _tagging_settings(monkeypatch, **env):
    defaults = {"TRIAL_USER_TAG": "TRIAL", "PAID_USER_TAG": "PAID", "PROTECTED_USER_TAGS": "FREE"}
    for key, value in {**defaults, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings()


def _panel_user(tag):
    return {"id": 443, "telegramId": 77, "shortUuid": "s443", "tag": tag,
            "expireAt": "2026-11-26T16:15:00Z"}


async def _activate_trial(settings, panel):
    service = SubscriptionService(settings, panel)

    async def never_had_subscription(session, user_id):
        return False

    service.has_had_any_subscription = never_had_subscription
    return await service.activate_trial_subscription(FakeSession(), 77)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, "TRIAL"),
        ({"TRIAL_USER_TAG": ""}, None),
    ],
)
async def test_trial_activation_tag(monkeypatch, patched_dal, env, expected):
    panel = RecordingPanel(_panel_user(None), exists=False)
    result = await _activate_trial(_tagging_settings(monkeypatch, **env), panel)
    assert result["activated"] is True
    assert panel.updates[-1].get("tag") == expected


@pytest.mark.parametrize(
    "panel_user",
    [
        # added by the admin with a long subscription, never seen by the bot
        {**_panel_user("FREE"), "status": "ACTIVE", "expireAt": "2027-05-01T00:00:00Z"},
        {**_panel_user(None), "status": "ACTIVE", "expireAt": "2027-05-01T00:00:00Z"},
        {**_panel_user(None), "status": "EXPIRED", "expireAt": "2026-01-01T00:00:00Z"},
    ],
)
async def test_trial_refused_for_existing_panel_user(monkeypatch, patched_dal, panel_user):
    panel = RecordingPanel(panel_user, exists=True)
    result = await _activate_trial(_tagging_settings(monkeypatch), panel)
    assert result == {
        "eligible": False,
        "activated": False,
        "message_key": "trial_already_had_subscription_or_trial",
    }
    # expiry, traffic limit and squads must stay untouched
    assert not any("expireAt" in update for update in panel.updates)


async def test_trial_refused_for_protected_tag(monkeypatch, patched_dal):
    panel = RecordingPanel(_panel_user("FREE"), exists=False)
    result = await _activate_trial(_tagging_settings(monkeypatch), panel)
    assert result["eligible"] is False and result["activated"] is False
    assert panel.updates == []


@pytest.mark.parametrize(
    "current, env, expected",
    [
        ("TRIAL", {}, "PAID"),
        (None, {}, "PAID"),
        ("FREE", {}, None),
        ("PAID", {}, None),
        ("TRIAL", {"PAID_USER_TAG": ""}, None),
    ],
)
async def test_paid_activation_tag(monkeypatch, patched_dal, current, env, expected):
    panel = RecordingPanel(_panel_user(current))
    service = SubscriptionService(_tagging_settings(monkeypatch, **env), panel)
    result = await service.activate_subscription(
        FakeSession(), 77, months=1, payment_amount=100.0, payment_db_id=1, provider="stars")
    assert result and result["is_active"] is True
    assert panel.updates[-1].get("tag") == expected
