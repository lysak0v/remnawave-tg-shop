"""Prices read as "199 ₽": no ".0", the ruble sign instead of RUB, the yearly option marked with 🔥."""
from decimal import Decimal
from pathlib import Path

import pytest

from bot.keyboards.inline.user_keyboards import get_subscription_options_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.utils.money import RUB_SIGN, currency_sign, format_amount, format_money

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = {1: 199.0, 3: 549.0, 6: 999.0, 12: 1490.0}


@pytest.fixture
def i18n():
    return JsonI18n(str(ROOT / "locales"), default="ru")


@pytest.mark.parametrize(
    "value, expected",
    [
        (199.0, "199"),
        (199, "199"),
        ("1490.00", "1490"),
        (Decimal("549"), "549"),
        (179.1, "179.10"),
        (0.005, "0.01"),
        (None, "0"),
    ],
)
def test_format_amount(value, expected):
    assert format_amount(value) == expected


@pytest.mark.parametrize("code, sign", [("RUB", "₽"), ("rub", "₽"), ("XTR", "⭐"), ("USDT", "USDT"), (None, "₽")])
def test_currency_sign(code, sign):
    assert currency_sign(code) == sign


def test_format_money():
    assert format_money(199.0) == "199 ₽"
    assert format_money(50, "XTR") == "50 ⭐"


def _button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def test_period_buttons_ru(i18n):
    texts = _button_texts(get_subscription_options_keyboard(OPTIONS, RUB_SIGN, "ru", i18n))
    assert texts[:4] == ["1 мес. - 199 ₽", "3 мес. - 549 ₽", "6 мес. - 999 ₽", "🔥 12 мес. - 1490 ₽"]


def test_period_buttons_en(i18n):
    texts = _button_texts(get_subscription_options_keyboard(OPTIONS, RUB_SIGN, "en", i18n))
    assert texts[0] == "1 mo. - 199 ₽"
    assert texts[3] == "🔥 12 mo. - 1490 ₽"


def test_traffic_buttons_are_not_highlighted(i18n):
    texts = _button_texts(get_subscription_options_keyboard({12: 300.0}, RUB_SIGN, "ru", i18n, traffic_mode=True))
    assert texts[0] == "12 ГБ - 300 ₽"


def test_no_rub_code_left_in_locales():
    for name in ("ru.json", "en.json"):
        assert "RUB" not in (ROOT / "locales" / name).read_text(encoding="utf-8")
