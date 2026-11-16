"""Readable names for subscription devices reported by the panel (HWID records).

Clients fill the fields differently: Happ on Windows puts the computer name into the
model ("HOME-PC_x86_64") and "10_10.0.19045" into the OS version, Android clients send
"Android 13" or just "13", the user agent may hold anything. These helpers turn that
into "💻 HOME-PC" / "Windows 10 · Happ 4.7.2".
"""
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

# Known VPN clients, matched case-insensitively against the start of the user agent.
_KNOWN_APPS = (
    "Happ", "INCY", "v2raytun", "V2Box", "v2rayNG", "v2rayN", "sing-box", "Shadowrocket",
    "Streisand", "Hiddify", "NekoBox", "FoXray", "Karing", "Clash", "mihomo", "FlClash",
    "Throne", "xrayui", "xray",
)
_ARCH_SUFFIX_RE = re.compile(r"[\s_-]*(?:x86_64|amd64|arm64|aarch64|x64|x86)$", re.IGNORECASE)
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")


def _platform_kind(platform: str) -> str:
    p = platform.lower()
    if "tv" in p:
        return "tv"
    if "openwrt" in p or "asuswrt" in p or "keenetic" in p or "router" in p:
        return "router"
    if p.startswith(("ios", "ipados", "android")):
        return "phone"
    if p.startswith(("windows", "macos", "linux", "ubuntu", "debian")):
        return "computer"
    return "other"


_EMOJI = {"phone": "📱", "computer": "💻", "tv": "📺", "router": "📡", "other": "🔌"}


def device_emoji(device: Dict) -> str:
    return _EMOJI[_platform_kind(device.get("platform") or "")]


def _dedupe_words(text: str) -> str:
    words: List[str] = []
    for word in text.split():
        if not words or words[-1].lower() != word.lower():
            words.append(word)
    return " ".join(words)


def device_name(device: Dict, fallback: str = "Device") -> str:
    """Model without CPU architecture and repeated vendor words, else the platform."""
    model = _ARCH_SUFFIX_RE.sub("", (device.get("deviceModel") or "").strip())
    model = _dedupe_words(model)
    if model:
        return model
    return (device.get("platform") or "").strip() or fallback


def device_os(device: Dict) -> str:
    platform = (device.get("platform") or "").strip()
    version = (device.get("osVersion") or "").strip()
    lowered = platform.lower()
    if lowered.startswith("windows"):
        # Happ: "10_10.0.19045" / "11_10.0.26200"; INCY: platform "Windows 11", version "10.0"
        major = version.split("_", 1)[0] if "_" in version else ""
        if lowered == "windows" and major.isdigit():
            return f"Windows {major}"
        return "Windows" + platform[len("windows"):]
    if lowered.startswith("macos"):
        return f"macOS {version}".strip()
    if lowered == "androidtv":
        platform = "Android TV"
    if version.lower().startswith(platform.lower().split()[0] if platform else "\0"):
        return version
    if platform and version and version not in platform:
        return f"{platform} {version}"
    return platform or version


def device_app(device: Dict) -> str:
    """Client name and version from the user agent, empty for unknown agents."""
    head = (device.get("userAgent") or "").split("/")
    first = head[0].strip()
    name = next((app for app in _KNOWN_APPS if first.lower().startswith(app.lower())), None)
    if not name:
        return ""
    version = first[len(name):].strip()
    if not version and len(head) > 1:
        version = head[1].strip()
    return f"{name} {version}" if _VERSION_RE.match(version) else name


def device_details(device: Dict) -> str:
    return " · ".join(part for part in (device_os(device), device_app(device)) if part)


def hwid_tails(hwids: Iterable[str], min_length: int = 5) -> Dict[str, str]:
    """Shortest HWID endings (at least `min_length`) that tell the given devices apart."""
    hwids = [h for h in hwids if h]
    tails: Dict[str, str] = {}
    for hwid in hwids:
        length = min(min_length, len(hwid))
        others = [h for h in hwids if h != hwid]
        while length < len(hwid) and any(o.endswith(hwid[-length:]) for o in others):
            length += 1
        tails[hwid] = hwid[-length:]
    return tails


def short_hwid(tail: Optional[str]) -> str:
    return f"…{tail}" if tail else "-"


# A device that has not requested the subscription for longer is marked as idle.
IDLE_AFTER_DAYS = 30


def parse_panel_time(value: Optional[str]) -> Optional[datetime]:
    """Panel timestamp ("2026-11-16T11:04:00.000Z") as an aware datetime, None if unusable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def days_since(moment: datetime, now: datetime) -> int:
    """Whole calendar days between the two moments in the local time zone of `now`."""
    return max(0, (now.date() - moment.astimezone(now.tzinfo).date()).days)


def mask_hwid(hwid: Optional[str], keep: int = 5, stars: int = 5) -> str:
    """First and last `keep` characters with stars between, so the line fits on a phone."""
    if not hwid:
        return "-"
    if len(hwid) <= keep * 2 + 1:
        return hwid
    return f"{hwid[:keep]}{'*' * stars}{hwid[-keep:]}"


def plural_form(number: int) -> str:
    """Russian plural category: "one" (1, 21), "few" (2-4, 22-24) or "many" (5-20, 25...)."""
    if number % 10 == 1 and number % 100 != 11:
        return "one"
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return "few"
    return "many"
