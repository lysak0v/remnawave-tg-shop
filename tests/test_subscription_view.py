"""«Моя подписка»: states from the panel record, short lines, renew vs purchase buttons."""
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.keyboards.inline.user_keyboards import get_main_menu_inline_keyboard, get_subscribe_only_markup
from bot.middlewares.i18n import JsonI18n
from bot.services import panel_webhook_service as pws
from bot.services.panel_webhook_service import PanelWebhookService
from bot.utils import subscription_view as sv

ROOT = Path(__file__).resolve().parents[1]
MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 16, 21, 0, tzinfo=MSK)
LINK = "https://sub.074740.xyz/AbCdEfGhIjKlMnOp"
# Fits a phone-wide message bubble without wrapping.
MAX_LINE = 34


@pytest.fixture(scope="module")
def i18n():
    return JsonI18n(str(ROOT / "locales"), default="ru")


def texts(i18n, lang="ru"):
    return lambda key, **kw: i18n.gettext(lang, key, **kw)


def details(days=364, status="ACTIVE", tag="PAID", online=None, used=343646169, limit=0, link=LINK, hours=0):
    return {
        "user_id": 3,
        "status_from_panel": status,
        "end_date": (NOW + timedelta(days=days, hours=hours)).astimezone(timezone.utc),
        "tag": tag,
        "online_at": online,
        "traffic_used_bytes": used,
        "traffic_limit_bytes": limit,
        "config_link": link,
        "max_devices": 3,
    }


def render(i18n, d, lang="ru", devices=2):
    state = sv.subscription_state(d, NOW, "TRIAL")
    return state, sv.build_subscription_text(d, state, get_text=texts(i18n, lang), lang=lang, now=NOW,
                                             devices_count=devices)


@pytest.mark.parametrize(
    "d, state",
    [
        (details(), sv.ACTIVE),
        (details(tag="TRIAL", days=3), sv.TRIAL),
        (details(days=-1), sv.EXPIRED),
        (details(days=-1, status="EXPIRED", tag="TRIAL"), sv.TRIAL_EXPIRED),
        (details(status="LIMITED", tag="TRIAL"), sv.LIMITED),
        (details(status="DISABLED"), sv.DISABLED),
        (details(tag="FREE"), sv.ACTIVE),
        (None, sv.NONE),
    ],
)
def test_state(d, state):
    assert sv.subscription_state(d, NOW, "TRIAL") == state


def test_only_running_paid_subscription_is_renewed():
    assert sv.is_renewal(sv.ACTIVE)
    assert not any(sv.is_renewal(s) for s in (sv.TRIAL, sv.EXPIRED, sv.TRIAL_EXPIRED, sv.LIMITED, sv.NONE))


def test_active_text_ru(i18n):
    online = (NOW - timedelta(minutes=2)).astimezone(timezone.utc).isoformat()
    _, text = render(i18n, details(online=online))
    assert text == (
        "🔐 <b>Моя подписка</b>\n\n"
        "✅ Активна до <b>15.11.2027</b>\n"
        "⏳ Осталось 364 дня\n\n"
        "📱 Устройства: 2 из 3\n"
        "🟢 Онлайн: сейчас\n"
        "📊 Трафик: 0.32 ГБ (безлимит)\n\n"
        "🔗 Ссылка на подписку:\n"
        f"<pre>{LINK}</pre>\n"
        "👆 Нажмите, чтобы скопировать"
    )


def test_active_text_en(i18n):
    _, text = render(i18n, details(days=1), lang="en")
    assert "✅ Active until <b>Nov 17, 2026</b>" in text
    assert "⚠️ 1 day left — time to renew" in text
    assert "⚪ Not connected yet" in text


@pytest.mark.parametrize("days, hours, line", [
    (21, 0, "⏳ Остался 21 день"), (4, 0, "⏳ Осталось 4 дня"), (3, 0, "⚠️ Осталось 3 дня — пора продлить"),
    (1, 0, "⚠️ Остался 1 день — пора продлить"), (0, 2, "⚠️ Заканчивается сегодня"),
])
def test_days_left_forms(i18n, days, hours, line):
    assert line in render(i18n, details(days=days, hours=hours))[1].split("\n")


def test_trial_text_has_no_renew_call(i18n):
    state, text = render(i18n, details(tag="TRIAL", days=2, limit=6 * 2**30, used=2**30))
    assert state == sv.TRIAL
    assert "🆓 Пробный период до <b>18.11.2026</b>" in text
    assert "⚠️ Осталось 2 дня" in text.split("\n")
    assert "📊 Трафик: 1.00 ГБ из 6.00 ГБ" in text


def test_online_days_ago(i18n):
    online = (NOW - timedelta(days=5)).astimezone(timezone.utc).isoformat()
    assert "⚪ Онлайн: 5 дней назад" in render(i18n, details(online=online))[1]


def test_unknown_device_count(i18n):
    assert "📱 Устройства: ? из 3" in render(i18n, details(), devices=None)[1]


@pytest.mark.parametrize("d, expected", [
    (details(days=-1), ["⛔ Подписка истекла <b>15.11.2026</b>", "Ссылка и устройства сохранятся —"]),
    (details(days=-1, status="EXPIRED", tag="TRIAL"), ["⛔ Пробный период истёк <b>15.11.2026</b>"]),
    (details(status="LIMITED", tag="TRIAL"), ["🚫 Лимит трафика исчерпан"]),
    (details(status="DISABLED"), ["⏸ Подписка приостановлена", "Напишите в поддержку"]),
    (None, ["У вас пока нет подписки"]),
])
def test_inactive_texts(i18n, d, expected):
    lines = render(i18n, d)[1].split("\n")
    for line in expected:
        assert line in lines
    assert not any("<pre>" in line for line in lines)


def _visible(line):
    return re.sub(r"<[^>]+>", "", line).replace("️", "")


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("d", [
    details(), details(days=2), details(days=0, hours=2), details(tag="TRIAL", days=2, limit=6 * 2**30),
    details(days=-1), details(days=-1, status="EXPIRED", tag="TRIAL"), details(status="LIMITED"),
    details(status="DISABLED"), details(used=3 * 2**40), None,
])
def test_lines_fit_phone_width(i18n, lang, d):
    online = (NOW - timedelta(days=12)).astimezone(timezone.utc).isoformat()
    if d:
        d = {**d, "online_at": online}
    for line in render(i18n, d, lang=lang)[1].split("\n"):
        if line.startswith("<pre>"):
            continue  # the link is copied, not read
        assert len(_visible(line)) <= MAX_LINE, line


def test_renew_button_goes_first_when_ending_soon():
    assert sv.renew_first(details(days=3), sv.ACTIVE, NOW)
    assert not sv.renew_first(details(days=30), sv.ACTIVE, NOW)
    assert not sv.renew_first(details(days=-1), sv.EXPIRED, NOW)


def _first_texts(markup):
    return [row[0].text for row in markup.inline_keyboard]


def test_main_menu_purchase_and_renew(i18n, settings):
    assert "💳 Оформить подписку" in _first_texts(get_main_menu_inline_keyboard("ru", i18n, settings))
    renew = _first_texts(get_main_menu_inline_keyboard("ru", i18n, settings, renew=True))
    assert "🔄 Продлить подписку" in renew and "💳 Оформить подписку" not in renew
    assert "🔄 Renew subscription" in _first_texts(get_main_menu_inline_keyboard("en", i18n, settings, renew=True))


def test_subscribe_only_markup(i18n):
    assert _first_texts(get_subscribe_only_markup("ru", i18n)) == ["💳 Оформить подписку"]
    assert _first_texts(get_subscribe_only_markup("ru", i18n, renew=True)) == ["🔄 Продлить подписку"]


@pytest.fixture
def reminder_service(i18n, settings, monkeypatch):
    settings.SUBSCRIPTION_NOTIFICATIONS_ENABLED = True
    settings.SUBSCRIPTION_NOTIFY_DAYS_BEFORE = 3
    settings.SUBSCRIPTION_NOTIFY_ON_EXPIRE = True
    settings.TRIAL_USER_TAG = "TRIAL"

    @asynccontextmanager
    async def session_factory():
        yield None

    async def get_user_by_id(session, user_id):
        return SimpleNamespace(language_code="ru", first_name="Alex")

    monkeypatch.setattr(pws.user_dal, "get_user_by_id", get_user_by_id)
    service = PanelWebhookService(bot=None, settings=settings, i18n=i18n,
                                  async_session_factory=session_factory, panel_service=None)
    service.sent = []

    async def fake_send(user_id, lang, key, reply_markup=None, **kwargs):
        service.sent.append((key, _first_texts(reply_markup)))

    service._send_message = fake_send
    return service


@pytest.mark.parametrize("event, meta, tag, button", [
    ("user.expiration", {"expiration": -72}, "PAID", "🔄 Продлить подписку"),
    ("user.expiration", {"expiration": -72}, None, "🔄 Продлить подписку"),
    ("user.expiration", {"expiration": -72}, "TRIAL", "💳 Оформить подписку"),
    ("user.expired", None, "PAID", "💳 Оформить подписку"),
])
async def test_reminder_buttons(reminder_service, event, meta, tag, button):
    payload = {"telegramId": 42, "tag": tag, "expireAt": "2026-11-19T20:35:00.000Z"}
    await reminder_service.handle_event(event, payload, meta)
    assert reminder_service.sent and reminder_service.sent[0][1] == [button]
