"""Main menu status line, reminders, empty device list and referral texts: content and phone-width lines."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.middlewares.i18n import JsonI18n
from bot.services.panel_webhook_service import _end_date
from bot.utils import subscription_view as sv
from bot.utils.referral_view import bonus_table, friend_message

ROOT = Path(__file__).resolve().parents[1]
MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 16, 21, 0, tzinfo=MSK)
MAX_LINE = 34
PERIODS = (1, 3, 6, 12)
BONUS = {1: 3, 3: 7, 6: 15, 12: 30}
LINK = "https://t.me/nitro_vpn_sales_bot?start=ref_uO5C6N92VM"


@pytest.fixture(scope="module")
def i18n():
    return JsonI18n(str(ROOT / "locales"), default="ru")


def texts(i18n, lang="ru"):
    return lambda key, **kw: i18n.gettext(lang, key, **kw)


def visible(line):
    return re.sub(r"<[^>]+>", "", line).replace("️", "")


def assert_fits(text):
    for line in text.split("\n"):
        if "https://" in line:
            continue  # links are tapped or copied, not read
        assert len(visible(line)) <= MAX_LINE, line


def panel_details(days, status="ACTIVE", tag="PAID"):
    return {"status_from_panel": status, "end_date": NOW + timedelta(days=days), "tag": tag}


@pytest.mark.parametrize("details, trial_days, line", [
    (panel_details(364), None, "✅ Подписка активна до <b>15.11.2027</b>"),
    (panel_details(2), None, "⚠️ До конца подписки: 2 дня"),
    (panel_details(1, tag="TRIAL"), None, "🆓 Пробный период до <b>17.11.2026</b>"),
    (panel_details(-3), None, "⛔ Подписка закончилась <b>13.11.2026</b>"),
    (panel_details(-3, status="EXPIRED", tag="TRIAL"), None, "⛔ Пробный период истёк <b>13.11.2026</b>"),
    (panel_details(30, status="DISABLED"), None, "⏸ Подписка приостановлена"),
    (None, 2, "🆓 Попробуйте бесплатно — 2 дня"),
    (None, None, "💳 Оформите подписку, чтобы начать"),
])
def test_menu_status_line(i18n, details, trial_days, line):
    state = sv.subscription_state(details, NOW, "TRIAL")
    got = sv.menu_status_line(state, details, get_text=texts(i18n), lang="ru", now=NOW, trial_days=trial_days)
    assert got == line
    assert_fits(got)


def test_main_menu_text(i18n):
    status = sv.menu_status_line(sv.ACTIVE, panel_details(364), get_text=texts(i18n), lang="ru", now=NOW)
    assert sv.main_menu_text("Александр", status, texts(i18n)) == (
        "👋 Привет, Александр!\n\n✅ Подписка активна до <b>15.11.2027</b>\n\nВыберите действие 👇"
    )
    assert sv.main_menu_text("Alex", "", texts(i18n, "en")) == "👋 Hi, Alex!\n\nChoose an action 👇"


@pytest.mark.parametrize("n, ru", [(1, "до 1 устройства"), (3, "до 3 устройств"), (5, "до 5 устройств"),
                                   (None, "любое число устройств")])
def test_devices_upto(i18n, n, ru):
    assert sv.devices_upto(n, texts(i18n)) == ru


def test_reminder_date_format():
    payload = {"expireAt": "2026-11-19T20:35:00.000Z"}
    assert _end_date(payload, "ru") in ("19.11.2026", "20.11.2026")  # server zone decides the day
    assert _end_date({}, "ru") == ""


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("key", [
    "subscription_72h_notification", "subscription_48h_notification", "subscription_24h_notification",
    "subscription_expired_notification", "subscription_expired_yesterday_notification",
])
def test_reminders_fit(i18n, lang, key):
    date = "16.11.2026" if lang == "ru" else "Nov 16, 2026"
    assert_fits(i18n.gettext(lang, key, user_name="Александр", end_date=date))


def test_reminder_text_ru(i18n):
    assert i18n.gettext("ru", "subscription_72h_notification", user_name="Александр", end_date="19.11.2026") == (
        "👋 Привет, Александр!\n\n⏳ Подписка закончится через\n3 дня — 19.11.2026\n\n"
        "Продлите заранее, чтобы VPN\nне отключился в нужный момент.\nОставшиеся дни не сгорят ✅"
    )


def test_bonus_table(i18n):
    assert bonus_table(PERIODS, BONUS, BONUS, texts(i18n)) == (
        "1 мес. → +3 дня каждому\n3 мес. → +7 дней каждому\n"
        "6 мес. → +15 дней каждому\n12 мес. → +30 дней каждому"
    )
    assert bonus_table((1,), {1: 7}, {1: 3}, texts(i18n)) == "1 мес. → +7 дней тебе, +3 дня другу"
    assert bonus_table((1, 3), {1: 0}, {}, texts(i18n)) == ""


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_referral_screen_fits(i18n, lang):
    get_text = texts(i18n, lang)
    text = get_text(
        "referral_program_info_new", referral_link=LINK, invited_count=12, purchased_count=3,
        bonus_details=bonus_table(PERIODS, BONUS, BONUS, get_text), bonus_rule=get_text("referral_bonus_rule_every"),
    )
    assert_fits(text)
    assert f"<pre>{LINK}</pre>" in text


def test_friend_message(i18n):
    text = friend_message(texts(i18n), LINK, BONUS, 3)
    assert "до 3 устройств на подписку" in text
    assert "до +30 дней в подарок 👇" in text
    assert text.endswith(LINK)
    assert_fits(text)


SUB_LINK = "https://sub.074740.xyz/byxA4enWg9Ms9Ufp"


@pytest.mark.parametrize("days, ru", [(1, "1 день"), (3, "3 дня"), (7, "7 дней"), (21, "21 день"), (30, "30 дней")])
def test_days_text(i18n, days, ru):
    assert sv.days_text(days, texts(i18n)) == ru


def test_payment_success_text_ru(i18n):
    assert i18n.gettext("ru", "payment_successful_full", months=1, end_date="18.03.2027", config_link=SUB_LINK) == (
        "✅ Оплата прошла — спасибо!\n\n📅 Подписка на 1 мес. активна\nдо <b>18.03.2027</b>\n\n"
        f"🔗 Ссылка на подписку:\n<pre>{SUB_LINK}</pre>\n\n"
        "Уже подключены? Ничего делать\nне нужно — VPN уже работает.\nНовое устройство — нажмите\n«🔗 Подключиться» 👇"
    )


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_payment_success_variants_fit(i18n, lang):
    get_text = texts(i18n, lang)
    bonus = sv.days_text(30, get_text)
    common = dict(months=12, config_link=SUB_LINK)
    variants = [
        get_text("payment_successful_full", end_date="18.03.2027", **common),
        get_text("payment_successful_with_referral_bonus_full", base_end_date="18.03.2027",
                 final_end_date="17.04.2027", bonus_days=bonus, inviter_name="Александр", **common),
        get_text("payment_successful_with_promo_full", end_date="17.04.2027", bonus_days=bonus, **common),
    ]
    for text in variants:
        assert "{" not in text
        assert_fits(text)
    assert f"🎁 +{bonus}" in variants[1] and f"🎁 +{bonus}" in variants[2]


def test_payment_date_format():
    from bot.handlers.user.payment import _user_date
    moment = datetime(2027, 3, 18, 12, 0, tzinfo=timezone.utc)
    assert _user_date(moment, "ru") == "18.03.2027"
    assert _user_date(moment, "en") == "Mar 18, 2027"
