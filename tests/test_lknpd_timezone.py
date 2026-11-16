"""Receipt times go to «Мой налог» in NALOGO_TIMEZONE, so the receipt shows local time."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from bot.services.lknpd_client import LknpdClient, PaymentType, _format_datetime
from bot.services.lknpd_service import LknpdService
from config.settings import Settings

MOSCOW = ZoneInfo("Europe/Moscow")
PAID_AT = datetime(2026, 11, 16, 16, 40, 10, tzinfo=UTC)  # 19:40:10 MSK


def test_format_datetime_moscow_offset():
    assert _format_datetime(PAID_AT, MOSCOW) == "2026-11-16T19:40:10+03:00"


def test_format_datetime_utc_keeps_z_suffix():
    assert _format_datetime(PAID_AT) == "2026-11-16T16:40:10Z"


def test_format_datetime_naive_is_utc():
    assert _format_datetime(datetime(2026, 11, 16, 16, 40, 10), MOSCOW) == "2026-11-16T19:40:10+03:00"


def test_format_datetime_month_boundary_stays_in_local_month():
    # 00:30 MSK on December 1st is still November in UTC
    assert _format_datetime(datetime(2026, 11, 30, 21, 30, tzinfo=UTC), MOSCOW) == "2026-12-01T00:30:00+03:00"


def test_create_income_sends_times_in_client_timezone():
    client = LknpdClient(timezone=MOSCOW)
    client._token_data = {"token": "t", "profile": {"inn": "1"}}
    sent = {}

    async def fake_request(method, path, json_data=None, **kwargs):
        sent.update(json_data)
        return SimpleNamespace(status_code=200, text="", json=lambda: {"approvedReceiptUuid": "r1"})

    client._request = fake_request
    receipt = asyncio.run(client.create_income(
        name="subscription 1 months", amount=199, payment_type=PaymentType.WIRE, operation_time=PAID_AT,
    ))

    assert receipt == "r1"
    assert sent["operationTime"] == "2026-11-16T19:40:10+03:00"
    assert sent["requestTime"].endswith("+03:00")


def test_service_passes_timezone_to_client():
    service = LknpdService("123", "secret", timezone_name="Asia/Yekaterinburg")
    assert service._client.timezone == ZoneInfo("Asia/Yekaterinburg")


def test_setting_defaults_to_moscow(monkeypatch):
    monkeypatch.delenv("NALOGO_TIMEZONE", raising=False)
    assert Settings().LKNPD_TIMEZONE == "Europe/Moscow"


def test_setting_reads_env(monkeypatch):
    monkeypatch.setenv("NALOGO_TIMEZONE", "UTC")
    assert Settings().LKNPD_TIMEZONE == "UTC"


def test_setting_empty_falls_back_to_moscow(monkeypatch):
    monkeypatch.setenv("NALOGO_TIMEZONE", " ")
    assert Settings().LKNPD_TIMEZONE == "Europe/Moscow"


def test_setting_rejects_unknown_zone(monkeypatch):
    monkeypatch.setenv("NALOGO_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValidationError, match="NALOGO_TIMEZONE"):
        Settings()
