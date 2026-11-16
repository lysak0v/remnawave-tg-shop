"""Money as users read it: "199 ₽" instead of "199.0 RUB"."""
from decimal import ROUND_HALF_UP, Decimal
from typing import Union

RUB_SIGN = "₽"
_SIGNS = {"RUB": RUB_SIGN, "XTR": "⭐"}


def format_amount(value: Union[int, float, Decimal, str, None]) -> str:
    """Whole amounts without a fraction ("199"), others with kopecks ("179.10")."""
    if value is None:
        return "0"
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount:.2f}"


def currency_sign(code: Union[str, None]) -> str:
    """Sign for a currency code ("RUB" -> "₽", Telegram Stars "XTR" -> "⭐"), other codes as is."""
    if not code:
        return RUB_SIGN
    return _SIGNS.get(str(code).upper(), str(code))


def format_money(value: Union[int, float, Decimal, str, None], currency: Union[str, None] = "RUB") -> str:
    return f"{format_amount(value)} {currency_sign(currency)}"
