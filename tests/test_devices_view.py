"""Device list: readable names, identical devices told apart, confirmation before disconnect."""
from pathlib import Path

import pytest

from bot.handlers.user.subscription.core import (
    _hwid_callback_token,
    build_devices_view,
    build_disconnect_confirmation,
)
from bot.middlewares.i18n import JsonI18n
from bot.utils.device_display import (
    device_app,
    device_details,
    device_emoji,
    device_name,
    device_os,
    hwid_tails,
)

ROOT = Path(__file__).resolve().parents[1]


def _dev(platform, model, os_version, user_agent, hwid="a1b2c3d4e5f60708", created="2026-10-12T09:15:00.000Z"):
    return {"platform": platform, "deviceModel": model, "osVersion": os_version,
            "userAgent": user_agent, "hwid": hwid, "createdAt": created}


# real combinations seen in the panel
@pytest.mark.parametrize(
    "device, emoji, name, details",
    [
        (_dev("iOS", "iPhone 15 Pro", "18.5", "Happ/6.1.0/ios/2610091516618"),
         "📱", "iPhone 15 Pro", "iOS 18.5 · Happ 6.1.0"),
        (_dev("Android", "TECNO TECNO BG7n", "Android 13", "v2raytun/android"),
         "📱", "TECNO BG7n", "Android 13 · v2raytun"),
        (_dev("Android", "samsung SM-S928B", "17", "INCY/3.9.9/android Dalvik/2.1.0"),
         "📱", "samsung SM-S928B", "Android 17 · INCY 3.9.9"),
        (_dev("Windows", "HOME-PC_x86_64", "10_10.0.19045", "Happ/4.7.2/Windows/2610301"),
         "💻", "HOME-PC", "Windows 10 · Happ 4.7.2"),
        (_dev("Windows 11", "Windows 11 amd64", "10.0", "INCY/3.9.1/windows 11 Dalvik/2.1.0"),
         "💻", "Windows 11", "Windows 11 · INCY 3.9.1"),
        (_dev("macOS", "MacBook", "14.4.1", "Happ/4.8.2/macos catalyst/26"),
         "💻", "MacBook", "macOS 14.4.1 · Happ 4.8.2"),
        (_dev("AndroidTV", "MiTV-MOOQ1", "11", "Happ/4.8.0/AndroidTV/1792139"),
         "📺", "MiTV-MOOQ1", "Android TV 11 · Happ 4.8.0"),
        (_dev("iOS", "iPhone17,2", "26.4.2", "V2Box 10.1.9"),
         "📱", "iPhone17,2", "iOS 26.4.2 · V2Box 10.1.9"),
        (_dev("OpenWrt Linux", "Xiaomi AX6000", "6.12.94", "sing-box"),
         "📡", "Xiaomi AX6000", "OpenWrt Linux 6.12.94 · sing-box"),
        # unknown user agent is not shown
        (_dev("iOS", "iPhone 13 Pro Max", "27.0.1", "x4e5j9sh2e2serhf"),
         "📱", "iPhone 13 Pro Max", "iOS 27.0.1"),
        # no model: the platform is the name
        (_dev("iOS", None, "18.0", "Happ/6.0.0/ios"), "📱", "iOS", "iOS 18.0 · Happ 6.0.0"),
    ],
)
def test_device_display(device, emoji, name, details):
    assert (device_emoji(device), device_name(device), device_details(device)) == (emoji, name, details)


def test_device_without_any_name_uses_fallback():
    assert device_name({"platform": "", "deviceModel": ""}, fallback="Устройство") == "Устройство"
    assert device_os({}) == "" and device_app({}) == ""


def test_hwid_tails_are_unique_and_grow_on_clash():
    tails = hwid_tails(["aaaa1111zzzz9999", "bbbb2222yyyy9999", "cccc3333xxxx1234"])
    assert tails == {"aaaa1111zzzz9999": "z9999", "bbbb2222yyyy9999": "y9999", "cccc3333xxxx1234": "1234"}


@pytest.fixture
def get_text():
    i18n = JsonI18n(str(ROOT / "locales"), default="ru")
    return lambda key, **kw: i18n.gettext("ru", key, **kw)


def test_identical_devices_are_told_apart(get_text):
    twins = [
        _dev("iOS", "iPhone 15 Pro", "18.5", "Happ/6.1.0/ios", hwid="0f9e8d7c6b5a7f3a", created="2026-10-12T09:15:00Z"),
        _dev("iOS", "iPhone 15 Pro", "18.5", "Happ/6.1.0/ios", hwid="1a2b3c4d5e6f9c21", created="2026-11-03T18:40:00Z"),
    ]
    text, rows = build_devices_view(twins, get_text, "3")
    assert "Мои устройства (2/3)" in text
    assert "<b>1. 📱 iPhone 15 Pro</b>\niOS 18.5 · Happ 6.1.0\nПодключено: 12.10.2026" in text
    assert "Подключено: 03.11.2026" in text
    # full HWID stays in the list, none of the raw technical fields
    assert "<code>0f9e8d7c6b5a7f3a</code>" in text and "<code>1a2b3c4d5e6f9c21</code>" in text
    assert "User Agent" not in text
    buttons = [row[0] for row in rows]
    assert [b.text for b in buttons] == ["✖️ 1. iPhone 15 Pro · …7f3a", "✖️ 2. iPhone 15 Pro · …9c21"]
    assert buttons[0].callback_data == f"disconnect_device:{_hwid_callback_token('0f9e8d7c6b5a7f3a')}"
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


def test_long_name_is_shortened_in_button_only(get_text):
    device = _dev("OpenWrt Linux", "Xiaomi Redmi Router AX6000 (OpenWrt U-Boot layout)", "6.12", "sing-box")
    text, rows = build_devices_view([device], get_text, "3")
    assert "Xiaomi Redmi Router AX6000 (OpenWrt U-Boot layout)" in text
    assert rows[0][0].text == "✖️ 1. Xiaomi Redmi Router AX6… · …0708"  # 24 chars incl. "…"


def test_names_are_html_escaped(get_text):
    text, _ = build_devices_view([_dev("iOS", "<b>evil</b>", "18", "Happ/1.0/ios")], get_text, "3")
    assert "&lt;b&gt;evil&lt;/b&gt;" in text


def test_empty_list(get_text):
    text, rows = build_devices_view([], get_text, "3")
    assert rows == [] and "нет устройств" in text


def test_confirmation_screen(get_text):
    device = _dev("iOS", "iPhone 15 Pro", "18.5", "Happ/6.1.0/ios", hwid="0f9e8d7c6b5a7f3a")
    text, markup = build_disconnect_confirmation(device, get_text)
    assert text.startswith("Отключить это устройство?")
    assert "<code>0f9e8d7c6b5a7f3a</code>" in text
    yes, no = markup.inline_keyboard[0][0], markup.inline_keyboard[1][0]
    assert yes.callback_data == f"disconnect_device_do:{_hwid_callback_token('0f9e8d7c6b5a7f3a')}"
    assert no.callback_data == "main_action:my_devices"
