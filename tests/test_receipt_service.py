"""«Мой налог» receipts: queued, resent with backoff while LKNPD is down, alerts, refunds."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.handlers.admin.receipts_admin import build_receipts_text
from bot.handlers.user import payment as payment_handlers
from bot.middlewares.i18n import JsonI18n
from bot.services import receipt_service as rs
from bot.services.lknpd_client import LknpdApiError, LknpdValidationError, stable_device_id
from bot.services.lknpd_service import LknpdService
from bot.services.receipt_service import ReceiptService, retry_delay
from db.dal import payment_dal

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)  # 15:00 MSK


@pytest.fixture(scope="module")
def i18n():
    return JsonI18n(str(ROOT / "locales"), default="ru")


def payment(pid, paid_ago=timedelta(minutes=1), status="succeeded", receipt_status="pending", **extra):
    fields = dict(
        payment_id=pid, status=status, amount=199.0, subscription_duration_months=1,
        receipt_status=receipt_status, receipt_uuid=None, receipt_item_name="subscription 1 months",
        receipt_paid_at=NOW - paid_ago, created_at=NOW - paid_ago, receipt_attempts=0,
        receipt_last_error=None, receipt_next_attempt_at=None, receipt_alerted_at=None,
        yookassa_payment_id=f"yk-{pid}",
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


class FakeLknpd:
    configured = True

    def __init__(self, errors=None):
        self.errors = errors or {}
        self.calls = []

    async def create_income_receipt(self, *, item_name, amount, operation_time, quantity=1.0):
        self.calls.append((item_name, amount, operation_time))
        error = self.errors.get(len(self.calls))
        if error:
            raise error
        return f"receipt-{len(self.calls)}"


@pytest.fixture
def make_service(i18n, settings, monkeypatch):
    def _make(payments, lknpd=None):
        store = {"payments": payments, "commits": 0}

        class Session:
            async def commit(self):
                store["commits"] += 1

        @asynccontextmanager
        async def session_factory():
            yield Session()

        async def due(session, now, limit=20):
            return [p for p in store["payments"] if p.receipt_status == "pending"
                    and (p.receipt_next_attempt_at is None or p.receipt_next_attempt_at <= now)][:limit]

        async def pending(session):
            return [p for p in store["payments"] if p.receipt_status == "pending"]

        async def by_yk(session, yk_id):
            return next((p for p in store["payments"] if p.yookassa_payment_id == yk_id), None)

        monkeypatch.setattr(payment_dal, "get_due_receipts", due)
        monkeypatch.setattr(payment_dal, "get_pending_receipts", pending)
        monkeypatch.setattr(payment_dal, "get_payment_by_yookassa_id", by_yk)
        sent = []

        async def notify(text):
            sent.append(text)

        service = ReceiptService(settings, lknpd or FakeLknpd(), session_factory, notify,
                                 lambda key, **kw: i18n.gettext("ru", key, **kw))
        service.sent_notices = sent
        service.store = store
        return service
    return _make


def test_retry_delays():
    assert [retry_delay(n).total_seconds() / 60 for n in (1, 2, 3, 4, 5, 9)] == [5, 10, 20, 40, 60, 60]


async def test_sends_with_payment_time(make_service):
    p1, p2 = payment(1), payment(2)
    service = make_service([p1, p2])
    assert await service.process(NOW) == {"sent": 2, "failed": 0, "recovered": 0}
    assert (p1.receipt_status, p1.receipt_uuid, p2.receipt_uuid) == ("sent", "receipt-1", "receipt-2")
    assert service.lknpd.calls[0] == ("subscription 1 months", 199.0, p1.receipt_paid_at)
    assert service.sent_notices == []


async def test_login_failure_postpones_the_whole_batch(make_service):
    p1, p2 = payment(1), payment(2)
    lknpd = FakeLknpd({1: LknpdApiError('Authentication failed: {"code":"entity.not.found"}', 422)})
    service = make_service([p1, p2], lknpd)
    assert await service.process(NOW) == {"sent": 0, "failed": 2, "recovered": 0}
    assert len(lknpd.calls) == 1  # no second login attempt in the same pass
    for p in (p1, p2):
        assert p.receipt_status == "pending" and p.receipt_attempts == 1
        assert p.receipt_next_attempt_at == NOW + timedelta(minutes=5)
        assert "entity.not.found" in p.receipt_last_error
    assert await service.process(NOW + timedelta(minutes=1)) == {"sent": 0, "failed": 0, "recovered": 0}


async def test_validation_error_skips_only_that_payment(make_service):
    p1, p2 = payment(1), payment(2)
    service = make_service([p1, p2], FakeLknpd({1: LknpdValidationError("bad amount", 400)}))
    await service.process(NOW)
    assert (p1.receipt_status, p1.receipt_attempts, p2.receipt_status) == ("pending", 1, "sent")


async def test_refunded_payment_is_not_sent(make_service):
    p1 = payment(1, status="refunded")
    service = make_service([p1])
    await service.process(NOW)
    assert p1.receipt_status == "not_needed" and service.lknpd.calls == []


async def test_alert_after_six_hours_then_daily(make_service):
    p1 = payment(1, paid_ago=timedelta(hours=7))
    service = make_service([p1], FakeLknpd({n: LknpdApiError("Network error") for n in range(1, 10)}))
    await service.process(NOW)
    assert len(service.sent_notices) == 1
    assert "Не отправлено: <b>1</b> на <b>199 ₽</b>" in service.sent_notices[0]
    assert "Network error" in service.sent_notices[0]
    assert p1.receipt_alerted_at == NOW
    await service.process(NOW + timedelta(hours=2))
    assert len(service.sent_notices) == 1
    await service.process(NOW + timedelta(hours=25))
    assert len(service.sent_notices) == 2


async def test_no_alert_for_fresh_failures(make_service):
    p1 = payment(1, paid_ago=timedelta(hours=5))
    service = make_service([p1], FakeLknpd({1: LknpdApiError("down")}))
    await service.process(NOW)
    assert service.sent_notices == []


async def test_recovery_notice(make_service):
    p1 = payment(1, paid_ago=timedelta(hours=8), receipt_alerted_at=NOW - timedelta(hours=1))
    service = make_service([p1])
    assert (await service.process(NOW))["recovered"] == 1
    assert service.sent_notices == ["✅ <b>Чеки ушли в «Мой налог»</b>\n\nОтправлено после сбоя: <b>1</b>"]


@pytest.mark.parametrize("today, key_text", [
    (datetime(2026, 12, 7, 9, 0, tzinfo=timezone.utc), "до 9-го"),
    (datetime(2026, 12, 10, 9, 0, tzinfo=timezone.utc), "прошёл"),
])
def test_deadline_lines(make_service, today, key_text):
    p1 = payment(1, receipt_paid_at=datetime(2026, 11, 30, 18, 0, tzinfo=timezone.utc))
    service = make_service([p1])
    assert key_text in service.alert_text([p1], today)


def test_no_deadline_line_for_this_month(make_service):
    p1 = payment(1)
    assert "9-" not in make_service([p1]).alert_text([p1], NOW)


async def test_refund_drops_unsent_receipt(make_service):
    p1 = payment(1)
    service = make_service([p1])
    assert await service.handle_refund(SimpleNamespace(commit=_noop), "yk-1") == "not_needed"
    assert p1.receipt_status == "not_needed"
    assert "отправлять его не будем" in service.sent_notices[0]


async def test_refund_of_sent_receipt_asks_to_cancel(make_service):
    p1 = payment(1, receipt_status="sent", receipt_uuid="20abc")
    service = make_service([p1])
    assert await service.handle_refund(SimpleNamespace(commit=_noop), "yk-1") == "sent"
    assert "<code>20abc</code>" in service.sent_notices[0]


async def test_refund_without_receipt_is_ignored(make_service):
    service = make_service([payment(1, receipt_status=None)])
    assert await service.handle_refund(SimpleNamespace(commit=_noop), "yk-1") is None
    assert service.sent_notices == []


async def _noop():
    return None


class FakeYooKassa:
    def __init__(self, refund):
        self.refund = refund

    async def get_refund_info(self, refund_id):
        return self.refund


class FakeReceipts:
    def __init__(self):
        self.refunds = []

    async def handle_refund(self, session, yk_payment_id):
        self.refunds.append(yk_payment_id)


@asynccontextmanager
async def _session_factory():
    yield None


async def test_refund_notification_is_checked_with_the_api():
    receipts = FakeReceipts()
    resp = await payment_handlers._handle_refund_notification(
        SimpleNamespace(id="rf-1"), FakeYooKassa(None), receipts, _session_factory)
    assert resp.text == "refund_unconfirmed" and receipts.refunds == []
    resp = await payment_handlers._handle_refund_notification(
        SimpleNamespace(id="rf-1"), FakeYooKassa({"id": "rf-1", "status": "succeeded", "payment_id": "yk-7"}),
        receipts, _session_factory)
    assert resp.status == 200 and receipts.refunds == ["yk-7"]


async def test_lknpd_service_raises_on_login_failure():
    service = LknpdService("123456789012", "secret")

    async def fail(inn, password):
        raise LknpdApiError("Authentication failed: entity.not.found", 422)

    service._client.authenticate = fail
    with pytest.raises(LknpdApiError, match="entity.not.found"):
        await service.create_income_receipt(item_name="x", amount=1)


def test_stable_device_id():
    assert stable_device_id("123") == stable_device_id("123")
    assert stable_device_id("123") != stable_device_id("456")
    assert len(stable_device_id("123")) == 21
    assert LknpdService("123", "p")._client.device_id == stable_device_id("123")


def test_paid_at_from_capture_time():
    assert payment_handlers._paid_at({"captured_at": "2026-11-17T09:30:00.000Z"}) == datetime(
        2026, 11, 17, 9, 30, tzinfo=timezone.utc)
    assert payment_handlers._paid_at({}).tzinfo is not None


def test_receipts_list(i18n):
    from zoneinfo import ZoneInfo
    get_text = lambda key, **kw: i18n.gettext("ru", key, **kw)
    assert build_receipts_text([], get_text, ZoneInfo("Europe/Moscow")) == "✅ Все чеки отправлены в «Мой налог»."
    p1 = payment(216, receipt_attempts=3, receipt_last_error="Authentication failed",
                 receipt_next_attempt_at=NOW + timedelta(minutes=20))
    text = build_receipts_text([p1], get_text, ZoneInfo("Europe/Moscow"))
    assert "🧾 <b>Не отправлено чеков: 1</b>" in text
    assert "#216 · 199 ₽ · оплата 17.11 14:59" in text
    assert "попыток: 3, следующая: 17.11 15:20" in text
    assert "<code>Authentication failed</code>" in text


def test_module_constants():
    assert rs.ALERT_AFTER == timedelta(hours=6) and rs.REMIND_EVERY == timedelta(hours=24)
