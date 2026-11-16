"""Telegram profile entries in the panel user description.

The description is shared with the admin, who may keep notes there. The bot never
replaces it: when the Telegram profile changes, a dated entry is appended, e.g.

    [2026-11-16 10:21 MSK] john_doe | John Doe
"""
import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

_ENTRY_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}[^\]]*\] (?P<profile>.*)$")


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def profile_text(username: Optional[str], first_name: Optional[str],
                 last_name: Optional[str]) -> str:
    """One-line Telegram profile, empty when there is nothing to show."""
    name = " ".join(part for part in (_clean(first_name), _clean(last_name)) if part)
    login = _clean(username).lstrip("@")
    if not name:
        return login
    return f"{login or '-'} | {name}"


def _legacy_text(username: Optional[str], first_name: Optional[str],
                 last_name: Optional[str]) -> str:
    # Format written by earlier versions: username, first and last name on separate lines.
    return "\n".join(part for part in (_clean(username).lstrip("@"), _clean(first_name),
                                       _clean(last_name)) if part)


def _normalized(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def last_profile_entry(description: Optional[str]) -> Optional[str]:
    for line in reversed((description or "").splitlines()):
        match = _ENTRY_RE.match(line.strip())
        if match:
            return match.group("profile")
    return None


def merge_panel_description(
    current: Optional[str],
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    tz_name: str = "UTC",
    now: Optional[datetime] = None,
) -> Optional[str]:
    """New description with the profile appended, or None when nothing changed."""
    profile = profile_text(username, first_name, last_name)
    if not profile:
        return None

    current_text = (current or "").rstrip()
    last_entry = last_profile_entry(current_text)
    if last_entry is not None:
        if last_entry == profile:
            return None
    elif current_text and _normalized(current_text) == _normalized(
            _legacy_text(username, first_name, last_name)):
        return None

    moment = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))
    entry = f"[{moment.strftime('%Y-%m-%d %H:%M %Z')}] {profile}"
    return f"{current_text}\n{entry}" if current_text else entry
