"""Referral texts: the bonus table of the referral screen and the message for a friend."""
from typing import Callable, Dict, Optional

from bot.utils.subscription_view import days_text, devices_upto


def bonus_table(periods, inviter: Dict, referee: Dict, get_text: Callable[..., str]) -> str:
    """One line per subscription period: "3 мес. → +7 дней каждому" or "+7 тебе, +3 другу"."""
    lines = []
    for months in sorted(periods):
        inv = inviter.get(months) or 0
        ref = referee.get(months) or 0
        if not inv and not ref:
            continue
        if inv == ref:
            lines.append(get_text("referral_bonus_line_same", months=months, days=days_text(inv, get_text)))
        else:
            lines.append(get_text("referral_bonus_line_split", months=months,
                                  inviter_days=days_text(inv, get_text), referee_days=days_text(ref, get_text)))
    return "\n".join(lines)


def max_referee_bonus(referee: Dict) -> int:
    return max((days or 0 for days in referee.values()), default=0)


def friend_message(get_text: Callable[..., str], referral_link: str, referee: Dict,
                   max_devices: Optional[int]) -> str:
    return get_text(
        "referral_friend_message",
        referral_link=referral_link,
        max_bonus_days=max_referee_bonus(referee),
        devices=devices_upto(max_devices, get_text),
    )
