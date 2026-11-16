"""Telegram command menu: localized, limited to commands the bot actually handles."""
import re
from pathlib import Path

import pytest

from bot.app.bot_commands import MENU_COMMANDS, build_bot_commands, setup_bot_commands
from bot.middlewares.i18n import JsonI18n

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def i18n():
    return JsonI18n(str(ROOT / "locales"), default="ru")


def test_menu_texts_per_language(i18n):
    ru = {c.command: c.description for c in build_bot_commands(i18n, "ru")}
    en = {c.command: c.description for c in build_bot_commands(i18n, "en")}
    assert list(ru) == ["start", "connect", "language"]
    assert ru["connect"] == "Моя подписка и подключение"
    assert en["connect"] == "My subscription and connection"


def test_custom_start_description(i18n):
    commands = build_bot_commands(i18n, "ru", start_description="Открыть магазин")
    assert commands[0].description == "Открыть магазин"
    assert commands[1].description == "Моя подписка и подключение"


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_menu_fits_telegram_limits(i18n, lang):
    for command in build_bot_commands(i18n, lang):
        assert re.fullmatch(r"[a-z0-9_]{1,32}", command.command)
        assert 1 <= len(command.description) <= 256
        assert command.description != dict(MENU_COMMANDS)[command.command]  # not a raw key


def test_every_menu_command_has_a_handler():
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "bot").rglob("*.py"))
    for command, _ in MENU_COMMANDS:
        if command == "start":
            assert "CommandStart()" in sources
        else:
            assert f'Command("{command}")' in sources, command


_NO_FAILURE = object()


class RecordingBot:
    def __init__(self, fail_for=_NO_FAILURE):
        self.calls = []
        self.fail_for = fail_for

    async def set_my_commands(self, commands, language_code=None):
        if self.fail_for is not _NO_FAILURE and language_code == self.fail_for:
            raise RuntimeError("telegram down")
        self.calls.append((language_code, [(c.command, c.description) for c in commands]))


async def test_setup_registers_default_and_each_language(i18n, settings, monkeypatch):
    monkeypatch.setattr(settings, "START_COMMAND_DESCRIPTION", "Открыть магазин")
    bot = RecordingBot()
    await setup_bot_commands(bot, i18n, settings)
    menus = dict(bot.calls)
    assert list(menus) == [None, "en", "ru"]
    # the custom /start text is for the default language (ru) only
    assert menus[None][0] == ("start", "Открыть магазин")
    assert menus["ru"][0] == ("start", "Открыть магазин")
    assert menus["en"][0] == ("start", "Open the menu")
    assert menus["en"][2] == ("language", "Change language / Язык")


async def test_setup_survives_a_failing_language(i18n, settings):
    bot = RecordingBot(fail_for="en")
    await setup_bot_commands(bot, i18n, settings)
    assert [lang for lang, _ in bot.calls] == [None, "ru"]
