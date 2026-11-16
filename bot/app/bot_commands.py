"""Command menu shown by Telegram next to the input field."""
import logging
from typing import List, Optional

from aiogram import Bot
from aiogram.types import BotCommand

from bot.middlewares.i18n import JsonI18n
from config.settings import Settings

# command -> locale key of its description; commands handled for every user
MENU_COMMANDS = (
    ("start", "bot_command_start"),
    ("connect", "bot_command_connect"),
    ("language", "bot_command_language"),
)


def build_bot_commands(i18n: JsonI18n, lang: str,
                       start_description: Optional[str] = None) -> List[BotCommand]:
    """Menu for one language; `start_description` replaces the /start text if given."""
    commands = []
    for command, key in MENU_COMMANDS:
        description = i18n.gettext(lang, key)
        if command == "start" and start_description:
            description = start_description
        commands.append(BotCommand(command=command, description=description))
    return commands


async def setup_bot_commands(bot: Bot, i18n: JsonI18n, settings: Settings) -> None:
    """Default menu in DEFAULT_LANGUAGE plus one per loaded locale.

    START_COMMAND_DESCRIPTION, when set, is used for /start in the default language only.
    """
    default_lang = settings.DEFAULT_LANGUAGE
    custom_start = settings.START_COMMAND_DESCRIPTION
    menus = [(None, build_bot_commands(i18n, default_lang, custom_start))]
    for lang in sorted(i18n.locales_data):
        menus.append(
            (lang, build_bot_commands(i18n, lang, custom_start if lang == default_lang else None))
        )
    for language_code, commands in menus:
        try:
            await bot.set_my_commands(commands, language_code=language_code)
        except Exception as e:
            logging.error(
                f"STARTUP: Failed to set bot commands for language '{language_code or 'default'}': {e}",
                exc_info=True,
            )
    logging.info(f"STARTUP: bot command menu set for default and {sorted(i18n.locales_data)}.")
