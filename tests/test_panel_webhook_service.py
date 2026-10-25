"""Panel webhook: Remnawave >= 2.8 user.expiration mapping and X-Remnawave-Signature check."""
import hashlib
import hmac
import json

import pytest

from bot.services import panel_webhook_service as pws
from bot.services.panel_webhook_service import PanelWebhookService

SECRET = "s3cretS3cretS3cretS3cretS3cretS3cret"


def test_expiration_offsets_mapping():
    assert {k: v[0] for k, v in pws.EXPIRATION_REMINDERS.items()} == {-72: 3, -48: 2, -24: 1}
    assert pws.EXPIRATION_REMINDERS[-72][1] == "subscription_72h_notification"
    assert pws.EXPIRATION_REMINDERS[-48][1] == "subscription_48h_notification"
    assert pws.EXPIRATION_REMINDERS[-24][1] == "subscription_24h_notification"
    assert pws.EXPIRED_YESTERDAY_OFFSET == 24


@pytest.fixture
def make_service(settings):
    def _make(secret=SECRET):
        settings.PANEL_WEBHOOK_SECRET = secret
        service = PanelWebhookService(bot=None, settings=settings, i18n=None,
                                      async_session_factory=None, panel_service=None)
        service.events = []

        async def fake_handle_event(event_name, user_payload, meta=None):
            service.events.append((event_name, user_payload, meta))

        service.handle_event = fake_handle_event
        return service
    return _make


def body(event="user.expiration", meta=None):
    payload = {"scope": "user", "event": event, "timestamp": "2026-10-25T00:00:00.000Z",
               "data": {"id": 7, "telegramId": 42, "expireAt": "2026-10-28T00:00:00.000Z"},
               "meta": meta if meta is not None else {"expiration": -72}}
    return json.dumps(payload).encode()


def sign(raw, secret=SECRET):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def test_valid_signature_dispatches_event_with_meta(make_service):
    service = make_service()
    raw = body()
    resp = await service.handle_webhook(raw, sign(raw))
    assert resp.status == 200 and resp.text == "ok"
    assert service.events == [("user.expiration",
                               {"id": 7, "telegramId": 42, "expireAt": "2026-10-28T00:00:00.000Z"},
                               {"expiration": -72})]


async def test_bad_signature_rejected(make_service):
    service = make_service()
    raw = body()
    resp = await service.handle_webhook(raw, sign(raw, "wrong-secret"))
    assert resp.status == 403
    assert service.events == []


async def test_missing_signature_rejected(make_service):
    service = make_service()
    resp = await service.handle_webhook(body(), None)
    assert resp.status == 403


async def test_missing_secret_returns_503(make_service):
    service = make_service(secret=None)
    raw = body()
    resp = await service.handle_webhook(raw, sign(raw))
    assert resp.status == 503
