"""«Моя подписка»: what the user sees about the subscription, built from the panel record.

Lines are kept short (about 32 characters) so they do not wrap on a phone screen.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from bot.utils.device_display import days_since, parse_panel_time, plural_form

# The user counts as online while the panel saw traffic within this window.
ONLINE_WINDOW = timedelta(minutes=5)

ACTIVE, TRIAL, EXPIRED, TRIAL_EXPIRED, LIMITED, DISABLED, NONE = (
    "active", "trial", "expired", "trial_expired", "limited", "disabled", "none",
)

_EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def subscription_state(details: Optional[Dict[str, Any]], now: datetime,
                       trial_tag: Optional[str]) -> str:
    """One of the states above for the details returned by get_active_subscription_details."""
    if not details:
        return NONE
    status = (details.get("status_from_panel") or "").upper()
    is_trial = bool(trial_tag) and details.get("tag") == trial_tag
    if status == "DISABLED":
        return DISABLED
    if status == "LIMITED":
        return LIMITED
    end_date = details.get("end_date")
    if status == "ACTIVE" and end_date and end_date > now:
        return TRIAL if is_trial else ACTIVE
    return TRIAL_EXPIRED if is_trial else EXPIRED


def is_renewal(state: str) -> bool:
    """A paid subscription that still runs is renewed; everything else is bought anew."""
    return state == ACTIVE


def format_date(moment: datetime, lang: str, tz) -> str:
    local = moment.astimezone(tz)
    if (lang or "").startswith("en"):
        return f"{_EN_MONTHS[local.month - 1]} {local.day}, {local.year}"
    return local.strftime("%d.%m.%Y")


def days_text(days: int, get_text: Callable[..., str]) -> str:
    """ "1 день" / "3 дня" / "30 дней"."""
    return get_text(f"days_{plural_form(int(days))}", days=days)


def devices_upto(max_devices: Optional[int], get_text: Callable[..., str]) -> str:
    """ "до 3 устройств" / "до 1 устройства", or the no-limit wording."""
    if not max_devices:
        return get_text("devices_upto_any")
    form = "one" if plural_form(int(max_devices)) == "one" else "many"
    return get_text(f"devices_upto_{form}", n=max_devices)


def last_seen_ago(days: int, get_text: Callable[..., str]) -> str:
    if days == 0:
        return get_text("device_seen_today")
    if days == 1:
        return get_text("device_seen_yesterday")
    # one/few/many keys: Russian needs three forms ("1 день", "2 дня", "5 дней")
    return get_text(f"device_seen_days_ago_{plural_form(days)}", days=days)


def format_gb(value_bytes: Optional[float], get_text: Callable[..., str]) -> str:
    gb = float(value_bytes or 0) / 2**30
    value = f"{gb:.2f}" if gb < 10 else f"{gb:.1f}" if gb < 100 else f"{gb:.0f}"
    return get_text("traffic_gb_value", value=value)


def _days_line(state: str, days_left: int, soon_days: int, get_text) -> str:
    if days_left <= 0:
        return get_text("my_sub_ends_today")
    form = plural_form(days_left)
    if days_left <= soon_days:
        key = f"my_sub_days_left_soon_{form}" if state == ACTIVE else f"my_sub_trial_days_left_soon_{form}"
        return get_text(key, days=days_left)
    return get_text(f"my_sub_days_left_{form}", days=days_left)


def _online_line(online_at: Optional[str], now: datetime, get_text) -> str:
    seen = parse_panel_time(online_at)
    if not seen:
        return get_text("my_sub_never_connected")
    if now - seen <= ONLINE_WINDOW:
        return get_text("my_sub_online_now")
    return get_text("my_sub_online_ago", ago=last_seen_ago(days_since(seen, now), get_text))


def _devices_line(devices_count: Optional[int], max_devices: Optional[int], get_text) -> str:
    current = "?" if devices_count is None else str(devices_count)
    if max_devices:
        return get_text("my_sub_devices", current=current, max=max_devices)
    return get_text("my_sub_devices_unlimited", current=current)


def _traffic_line(details: Dict[str, Any], get_text) -> str:
    used = format_gb(details.get("traffic_used_bytes"), get_text)
    limit = details.get("traffic_limit_bytes")
    if limit:
        return get_text("my_sub_traffic_limited", used=used, limit=format_gb(limit, get_text))
    return get_text("my_sub_traffic_unlimited", used=used)


def build_subscription_text(details: Optional[Dict[str, Any]], state: str, *, get_text,
                            lang: str, now: datetime, devices_count: Optional[int] = None,
                            show_devices: bool = True, soon_days: int = 3) -> str:
    """Message text for «Моя подписка». `now` must be aware; dates use its time zone."""
    lines: List[str] = [get_text("my_sub_title"), ""]
    end_date = (details or {}).get("end_date")
    date_str = format_date(end_date, lang, now.tzinfo) if end_date else "-"

    if state in (ACTIVE, TRIAL):
        days_left = (end_date.astimezone(now.tzinfo).date() - now.date()).days
        lines.append(get_text("my_sub_active_until" if state == ACTIVE else "my_sub_trial_until", date=date_str))
        lines.append(_days_line(state, days_left, soon_days, get_text))
        lines.append("")
        if show_devices:
            lines.append(_devices_line(devices_count, details.get("max_devices"), get_text))
        lines.append(_online_line(details.get("online_at"), now, get_text))
        lines.append(_traffic_line(details, get_text))
        link = details.get("config_link")
        lines += ["", get_text("my_sub_link_label")]
        if link:
            lines += [get_text("my_sub_link_value", link=link), get_text("my_sub_link_copy_hint")]
        else:
            lines.append(get_text("config_link_not_available"))
    elif state in (EXPIRED, TRIAL_EXPIRED):
        lines.append(get_text("my_sub_expired" if state == EXPIRED else "my_sub_trial_expired", date=date_str))
        lines += ["", get_text("my_sub_kept_hint")]
    elif state == LIMITED:
        lines += [get_text("my_sub_limited"), "", get_text("my_sub_kept_hint")]
    elif state == DISABLED:
        lines.append(get_text("my_sub_disabled"))
    else:
        lines.append(get_text("my_sub_none"))
    return "\n".join(lines)


def renew_first(details: Optional[Dict[str, Any]], state: str, now: datetime, soon_days: int = 3) -> bool:
    """Put the buy/renew button on top when the subscription is about to end."""
    if state not in (ACTIVE, TRIAL):
        return False
    end_date = details.get("end_date")
    return (end_date.astimezone(now.tzinfo).date() - now.date()).days <= soon_days


def utc_now_local() -> datetime:
    """Current time in the server's local zone (MSK on prod), as the device list uses."""
    return datetime.now(timezone.utc).astimezone()


def details_from_panel_user(panel_user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The fields subscription_state needs, taken from a raw panel user record."""
    if not panel_user:
        return None
    return {
        "status_from_panel": panel_user.get("status"),
        "end_date": parse_panel_time(panel_user.get("expireAt")),
        "tag": panel_user.get("tag"),
    }


def menu_status_line(state: str, details: Optional[Dict[str, Any]], *, get_text, lang: str,
                     now: datetime, soon_days: int = 3, trial_days: Optional[int] = None) -> str:
    """One line about the subscription under the main menu greeting ("" when nothing to say)."""
    end_date = (details or {}).get("end_date")
    date_str = format_date(end_date, lang, now.tzinfo) if end_date else ""
    if state in (ACTIVE, TRIAL):
        days_left = (end_date.astimezone(now.tzinfo).date() - now.date()).days
        if days_left <= 0:
            return get_text("menu_status_ends_today")
        if state == ACTIVE and days_left <= soon_days:
            return get_text(f"menu_status_soon_{plural_form(days_left)}", days=days_left)
        return get_text("menu_status_active" if state == ACTIVE else "menu_status_trial", date=date_str)
    if state == EXPIRED:
        return get_text("menu_status_expired", date=date_str)
    if state == TRIAL_EXPIRED:
        return get_text("menu_status_trial_expired", date=date_str)
    if state == LIMITED:
        return get_text("my_sub_limited")
    if state == DISABLED:
        return get_text("menu_status_disabled")
    if trial_days:
        return get_text(f"menu_status_try_free_{plural_form(trial_days)}", days=trial_days)
    return get_text("menu_status_none")


def main_menu_text(first_name: str, status_line: str, get_text) -> str:
    parts = [get_text("main_menu_greeting", user_name=first_name)]
    if status_line:
        parts.append(status_line)
    parts.append(get_text("main_menu_prompt"))
    return "\n\n".join(parts)
