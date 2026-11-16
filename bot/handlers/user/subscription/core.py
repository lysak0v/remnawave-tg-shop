import hashlib
import html
import logging
import math
from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from typing import Optional, Union
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from config.settings import Settings
from bot.keyboards.inline.user_keyboards import (
    get_subscription_options_keyboard,
    get_back_to_main_menu_markup,
    get_autorenew_confirm_keyboard,
)
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
from bot.middlewares.i18n import JsonI18n
from bot.utils.device_display import (
    device_details,
    device_emoji,
    IDLE_AFTER_DAYS,
    days_since,
    device_name,
    hwid_tails,
    mask_hwid,
    parse_panel_time,
    plural_form,
    short_hwid,
)
from db.dal import subscription_dal, user_billing_dal
from db.models import Subscription

router = Router(name="user_subscription_core_router")


def _hwid_callback_token(hwid: Optional[str]) -> str:
    """Stable short token for callback_data; avoids 64b limit with raw HWID."""
    hwid_str = str(hwid or "")
    return hashlib.sha256(hwid_str.encode()).hexdigest()[:32]


_BUTTON_NAME_LIMIT = 24
_BUTTONS_PER_ROW = 2
_KEYCAPS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")


def index_mark(index: int) -> str:
    """Prominent number for the device list: keycap emoji up to 10, then "№11"."""
    return _KEYCAPS[index - 1] if 1 <= index <= len(_KEYCAPS) else f"№{index}"


def _last_seen_ago(days: int, get_text) -> str:
    if days == 0:
        return get_text("device_seen_today")
    if days == 1:
        return get_text("device_seen_yesterday")
    # one/few/many keys: Russian needs three forms ("1 день", "2 дня", "5 дней")
    return get_text(f"device_seen_days_ago_{plural_form(days)}", days=days)


def _device_fields(device: dict, get_text, hwid_tail: Optional[str] = None,
                   now: Optional[datetime] = None) -> dict:
    """Placeholders for device texts; the old ones are kept for operator overrides.

    The panel refreshes `updatedAt` of a HWID record on every subscription request from
    that device, so it is shown as the last request. Times are in the server's local zone.
    """
    now = now or datetime.now(timezone.utc).astimezone()  # server's local zone
    name = device_name(device, fallback=get_text("device_unknown_name"))
    created_at = parse_panel_time(device.get("createdAt"))
    created_at_str = created_at.astimezone(now.tzinfo).strftime("%d.%m.%Y") if created_at else "-"
    last_seen = parse_panel_time(device.get("updatedAt")) or created_at
    if last_seen:
        idle_days = days_since(last_seen, now)
        last_seen_str = last_seen.astimezone(now.tzinfo).strftime("%d.%m.%Y %H:%M")
        last_seen_ago = _last_seen_ago(idle_days, get_text)
        idle_mark = " 💤" if idle_days > IDLE_AFTER_DAYS else ""
    else:
        last_seen_str, last_seen_ago, idle_mark = "-", get_text("device_seen_unknown"), ""
    button_name = name if len(name) <= _BUTTON_NAME_LIMIT else name[:_BUTTON_NAME_LIMIT - 1] + "…"
    return {
        "emoji": device_emoji(device),
        "name": html.escape(name),
        "button_name": button_name,
        "details": html.escape(device_details(device)),
        "hwid": html.escape(mask_hwid(device.get("hwid"))),
        "hwid_full": html.escape(device.get("hwid") or "-"),
        "hwid_short": short_hwid(hwid_tail),
        "created_at_str": created_at_str,
        "last_seen_str": last_seen_str,
        "last_seen_ago": last_seen_ago,
        "idle_mark": idle_mark,
        "device_model": html.escape(device.get("deviceModel") or "-"),
        "platform": html.escape(device.get("platform") or "-"),
        "os_version": html.escape(device.get("osVersion") or "-"),
        "user_agent": html.escape(device.get("userAgent") or "-"),
    }


def build_devices_view(devices: list, get_text, max_devices_display: str,
                       now: Optional[datetime] = None):
    """Device list text and a grid of delete buttons, two per row.

    Identical devices (same model, OS and app) differ by number, connection date and HWID
    in the list; the buttons carry the device type and the same number.
    """
    if not devices:
        return get_text("no_devices_details_found_message", max_devices=max_devices_display), []
    tails = hwid_tails(d.get("hwid") for d in devices)
    blocks, buttons, any_idle = [], [], False
    for index, device in enumerate(devices, start=1):
        fields = _device_fields(device, get_text, tails.get(device.get("hwid")), now)
        any_idle = any_idle or bool(fields["idle_mark"])
        blocks.append(get_text("device_details", index=index, index_mark=index_mark(index), **fields))
        if device.get("hwid"):
            buttons.append(InlineKeyboardButton(
                text=get_text("disconnect_device_button", index=index, **fields),
                callback_data=f"disconnect_device:{_hwid_callback_token(device['hwid'])}",
            ))
    # two per row; with an odd count the last one takes the whole row
    rows = [buttons[i:i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]
    idle_note = get_text("devices_idle_note", days=IDLE_AFTER_DAYS) if any_idle else ""
    text = get_text("my_devices_details", devices="\n\n".join(blocks), idle_note=idle_note,
                    current_devices=len(devices), max_devices=max_devices_display)
    return text, rows


def build_disconnect_confirmation(device: dict, get_text, now: Optional[datetime] = None):
    fields = _device_fields(device, get_text, now=now)
    token = _hwid_callback_token(device.get("hwid"))
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_text("disconnect_device_confirm_button"),
                              callback_data=f"disconnect_device_do:{token}")],
        [InlineKeyboardButton(text=get_text("disconnect_device_cancel_button"),
                              callback_data="main_action:my_devices")],
    ])
    return get_text("disconnect_device_confirm", **fields), markup


async def refuse_if_disabled_in_panel(
    event: Union[types.Message, types.CallbackQuery],
    subscription_service: Optional[SubscriptionService],
    session: AsyncSession,
    get_text,
) -> bool:
    """Tell the user and return True if the admin disabled them in the panel."""
    if not subscription_service or not event.from_user:
        return False
    try:
        disabled = await subscription_service.is_disabled_in_panel(session, event.from_user.id)
    except Exception:
        logging.exception("Panel status check before payment failed")
        return False
    if not disabled:
        return False
    text = get_text("account_disabled_in_panel")
    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer(text, show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    else:
        await event.answer(text)
    return True


async def display_subscription_options(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
    promo_code_service=None,
    subscription_service: Optional[SubscriptionService] = None,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n:
        err_msg = "Language service error."
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(err_msg, show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        elif isinstance(event, types.Message):
            await event.answer(err_msg)
        return

    if await refuse_if_disabled_in_panel(event, subscription_service, session, get_text):
        return

    currency_symbol_val = "RUB"
    traffic_packages = getattr(settings, "traffic_packages", {}) or {}
    stars_traffic_packages = getattr(settings, "stars_traffic_packages", {}) or {}
    traffic_mode = bool(getattr(settings, "traffic_sale_mode", False) or stars_traffic_packages)

    options_are_stars = False
    if traffic_mode:
        if traffic_packages:
            options = traffic_packages
        elif stars_traffic_packages:
            options = stars_traffic_packages
            currency_symbol_val = "⭐"
            options_are_stars = True
        else:
            options = {}
    else:
        options = settings.subscription_options

    display_options = options
    if options and promo_code_service:
        try:
            active_discount_info = await promo_code_service.get_user_active_discount(
                session, event.from_user.id
            )
        except Exception:
            active_discount_info = None
        if active_discount_info:
            discount_pct, _promo_code = active_discount_info
            discounted_options = {}
            for period, price in options.items():
                if price is None:
                    discounted_options[period] = price
                else:
                    discounted_price, _ = promo_code_service.calculate_discounted_price(
                        price, discount_pct
                    )
                    if options_are_stars:
                        discounted_price = math.ceil(discounted_price)
                    discounted_options[period] = discounted_price
            display_options = discounted_options

    if display_options:
        text_content = get_text("select_traffic_package") if traffic_mode else get_text("select_subscription_period")
        reply_markup = get_subscription_options_keyboard(
            display_options, currency_symbol_val, current_lang, i18n, traffic_mode=traffic_mode
        )
    else:
        text_content = get_text("no_subscription_options_available")
        reply_markup = get_back_to_main_menu_markup(current_lang, i18n)

    target_message_obj = event.message if isinstance(event, types.CallbackQuery) else event
    if not target_message_obj:
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(get_text("error_occurred_try_again"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    if isinstance(event, types.CallbackQuery):
        try:
            await target_message_obj.edit_text(text_content, reply_markup=reply_markup)
        except Exception:
            await target_message_obj.answer(text_content, reply_markup=reply_markup)
        try:
            await event.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    else:
        await target_message_obj.answer(text_content, reply_markup=reply_markup)


@router.callback_query(F.data == "main_action:subscribe")
async def reshow_subscription_options_callback(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
    promo_code_service=None,
    subscription_service: Optional[SubscriptionService] = None,
):
    await display_subscription_options(
        callback, i18n_data, settings, session, promo_code_service=promo_code_service,
        subscription_service=subscription_service,
    )


async def my_subscription_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot,
):
    target = event.message if isinstance(event, types.CallbackQuery) else event
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    if not i18n or not target:
        if isinstance(event, types.Message):
            await event.answer(get_text("error_occurred_try_again"))
        return

    if not panel_service or not subscription_service:
        await target.answer(get_text("error_service_unavailable"))
        return

    active = await subscription_service.get_active_subscription_details(session, event.from_user.id)

    if not active:
        text = get_text("subscription_not_active")

        buy_button = InlineKeyboardButton(
            text=get_text("menu_subscribe_inline"), callback_data="main_action:subscribe"
        )
        back_markup = get_back_to_main_menu_markup(current_lang, i18n)

        kb = InlineKeyboardMarkup(inline_keyboard=[[buy_button], *back_markup.inline_keyboard])

        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer()
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            try:
                await event.message.edit_text(text, reply_markup=kb)
            except Exception:
                await event.message.answer(text, reply_markup=kb)
        else:
            await event.answer(text, reply_markup=kb)
        return

    end_date = active.get("end_date")
    days_left = (end_date.date() - datetime.now().date()).days if end_date else 0
    traffic_mode = bool(getattr(settings, "traffic_sale_mode", False))
    config_link_display = active.get("config_link")
    connect_button_url = active.get("connect_button_url")
    config_link_value = config_link_display or get_text("config_link_not_available")
    def _fmt_gb(val: Optional[float]) -> str:
        if val is None:
            return get_text("traffic_na")
        try:
            if isinstance(val, (int, float)):
                val_gb = float(val) / (2**30)
                return f"{val_gb:.2f} GB"
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return str(val)

    if traffic_mode:
        limit_display = _fmt_gb(active.get("traffic_limit_bytes"))
        used_display = _fmt_gb(active.get("traffic_used_bytes"))
        remaining_display = get_text("traffic_na")
        try:
            limit_val = active.get("traffic_limit_bytes") or 0
            used_val = active.get("traffic_used_bytes") or 0
            remaining_val = max(0, float(limit_val) - float(used_val))
            remaining_display = _fmt_gb(remaining_val)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        text = get_text(
            "my_traffic_details",
            status=active.get("status_from_panel", get_text("status_active")).capitalize(),
            end_date=end_date.strftime("%Y-%m-%d") if end_date else get_text("traffic_no_expiry"),
            traffic_limit=limit_display,
            traffic_used=used_display,
            traffic_left=remaining_display,
            config_link=config_link_value,
        )
    else:
        text = get_text(
            "my_subscription_details",
            end_date=end_date.strftime("%Y-%m-%d") if end_date else "N/A",
            days_left=max(0, days_left),
            status=active.get("status_from_panel", get_text("status_active")).capitalize(),
            config_link=config_link_value,
            traffic_limit=(f"{active['traffic_limit_bytes'] / 2**30:.2f} GB" if active.get("traffic_limit_bytes") else get_text("traffic_unlimited")),
            traffic_used=(
                f"{active['traffic_used_bytes'] / 2**30:.2f} GB" if active.get("traffic_used_bytes") is not None else get_text("traffic_na")
            ),
        )

    base_markup = get_back_to_main_menu_markup(current_lang, i18n)
    kb = base_markup.inline_keyboard
    try:
        local_sub = await subscription_dal.get_active_subscription_by_user_id(session, event.from_user.id)
        # Build rows to prepend above the base "back" markup
        prepend_rows = []

        # 1) Mini-app connect button on top if enabled, otherwise fall back to config link URL
        if settings.SUBSCRIPTION_MINI_APP_URL:
            prepend_rows.append([
                InlineKeyboardButton(
                    text=get_text("connect_button"),
                    web_app=WebAppInfo(url=settings.SUBSCRIPTION_MINI_APP_URL),
                )
            ])
        else:
            cfg_link_val = connect_button_url or config_link_display
            if cfg_link_val:
                prepend_rows.append([
                    InlineKeyboardButton(
                        text=get_text("connect_button"),
                        url=cfg_link_val,
                    )
                ])

        if settings.MY_DEVICES_SECTION_ENABLED:
            max_devices_value = active.get("max_devices")
            max_devices_display = get_text("devices_unlimited_label")
            if max_devices_value not in (None, 0):
                try:
                    max_devices_int = int(max_devices_value)
                    if max_devices_int >= 0:
                        max_devices_display = str(max_devices_int)
                except (TypeError, ValueError):
                    max_devices_display = str(max_devices_value)
            current_devices_display = "?"
            panel_user_id = active.get("user_id")
            devices_response = None
            if panel_user_id:
                try:
                    devices_response = await panel_service.get_user_devices(panel_user_id)
                except Exception:
                    logging.exception("Failed to load devices for user %s", panel_user_id)
            if devices_response:
                devices_count: Optional[int] = None
                if isinstance(devices_response, dict):
                    devices_list = devices_response.get("devices")
                    if isinstance(devices_list, list):
                        devices_count = len(devices_list)
                    elif isinstance(devices_list, int):
                        devices_count = devices_list
                    else:
                        try:
                            devices_count = len(devices_list)  # type: ignore[arg-type]
                        except Exception:
                            devices_count = None
                    if devices_count is None:
                        total_value = devices_response.get("total")
                        if isinstance(total_value, int):
                            devices_count = total_value
                elif isinstance(devices_response, list):
                    devices_count = len(devices_response)
                if devices_count is not None:
                    current_devices_display = str(devices_count)
            devices_button_text = get_text(
                "devices_button",
                current_devices=current_devices_display,
                max_devices=max_devices_display,
            )
            prepend_rows.append([
                InlineKeyboardButton(
                    text=devices_button_text,
                    callback_data="main_action:my_devices",
                )
            ])

        # 2) Auto-renew toggle (YooKassa only)
        if not traffic_mode and local_sub and local_sub.provider == "yookassa" and settings.yookassa_autopayments_active:
            toggle_text = (
                get_text("autorenew_disable_button") if local_sub.auto_renew_enabled else get_text("autorenew_enable_button")
            )
            prepend_rows.append([
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"toggle_autorenew:{local_sub.subscription_id}:{1 if not local_sub.auto_renew_enabled else 0}",
                )
            ])

        # 3) Payment methods management (when autopayments enabled)
        if not traffic_mode and settings.yookassa_autopayments_active:
            prepend_rows.append([
                InlineKeyboardButton(text=get_text("payment_methods_manage_button"), callback_data="pm:manage")
            ])

        if prepend_rows:
            kb = prepend_rows + kb
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        try:
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await bot.send_message(
                chat_id=target.chat.id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "main_action:my_devices")
async def my_devices_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot,
):
    target = event.message if isinstance(event, types.CallbackQuery) else event
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    if not i18n or not target:
        if isinstance(event, types.Message):
            await event.answer(get_text("error_occurred_try_again"))
        return

    if not settings.MY_DEVICES_SECTION_ENABLED:
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(get_text("my_devices_feature_disabled"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        else:
            await target.answer(get_text("my_devices_feature_disabled"))
        return

    # TODO: context?
    active = await subscription_service.get_active_subscription_details(session, event.from_user.id)
    if not active or not active.get("user_id"):
        message = get_text("subscription_not_active")
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(message, show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        else:
            await target.answer(message)
        return

    devices = await panel_service.get_user_devices(active.get("user_id")) if active else None
    if not devices:
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(get_text("no_devices_found"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        else:
            await target.answer(get_text("no_devices_found"))
        return

    devices_list_raw = []
    if isinstance(devices, dict):
        devices_list_raw = devices.get("devices") or []
    elif isinstance(devices, list):
        devices_list_raw = devices

    max_devices_value = active.get("max_devices")
    max_devices_display = get_text("devices_unlimited_label")
    if max_devices_value not in (None, 0):
        try:
            max_devices_int = int(max_devices_value)
            if max_devices_int >= 0:
                max_devices_display = str(max_devices_int)
        except (TypeError, ValueError):
            max_devices_display = str(max_devices_value)

    text, devices_kb = build_devices_view(devices_list_raw, get_text, max_devices_display)

    base_markup = get_back_to_main_menu_markup(current_lang, i18n, callback_data="main_action:my_subscription")
    markup = InlineKeyboardMarkup(inline_keyboard=devices_kb + base_markup.inline_keyboard)

    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except Exception:
            await event.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("disconnect_device:"))
@router.callback_query(F.data.startswith("disconnect_device_do:"))
async def disconnect_device_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    """`disconnect_device:` asks for confirmation, `disconnect_device_do:` disconnects."""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not settings.MY_DEVICES_SECTION_ENABLED:
        try:
            await callback.answer(get_text("my_devices_feature_disabled"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    try:
        action, hwid_token = callback.data.split(":", 1)
    except Exception:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    active = await subscription_service.get_active_subscription_details(session, callback.from_user.id)
    if not active or not active.get("user_id"):
        await callback.answer(get_text("subscription_not_active"), show_alert=True)
        return

    devices = await panel_service.get_user_devices(active.get("user_id"))
    if not devices:
        await callback.answer(get_text("no_devices_found"), show_alert=True)
        return

    devices_list_raw = []
    if isinstance(devices, dict):
        devices_list_raw = devices.get("devices") or []
    elif isinstance(devices, list):
        devices_list_raw = devices

    target_device = next(
        (d for d in devices_list_raw
         if d.get("hwid") and _hwid_callback_token(d["hwid"]) == hwid_token),
        None,
    )
    if not target_device:
        # already disconnected (e.g. a stale message): show the current list
        try:
            await callback.answer(get_text("device_already_disconnected"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        await my_devices_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)
        return

    if action == "disconnect_device":
        text, markup = build_disconnect_confirmation(target_device, get_text)
        try:
            await callback.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:
            await callback.message.answer(text, reply_markup=markup)
        return

    hwid = target_device["hwid"]
    success = await panel_service.disconnect_device(active.get("user_id"), hwid)
    if not success:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    await session.commit()
    try:
        await callback.answer(get_text("device_disconnected"))
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    await my_devices_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)


@router.callback_query(F.data.startswith("toggle_autorenew:"))
async def toggle_autorenew_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    try:
        _, payload = callback.data.split(":", 1)
        sub_id_str, enable_str = payload.split(":")
        sub_id = int(sub_id_str)
        enable = bool(int(enable_str))
    except Exception:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    sub = await session.get(Subscription, sub_id)
    if not sub or sub.user_id != callback.from_user.id:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if sub.provider != "yookassa":
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if enable:
        has_saved_card = await user_billing_dal.user_has_saved_payment_method(session, callback.from_user.id)
        if not has_saved_card:
            try:
                await callback.answer(get_text("autorenew_enable_requires_card"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            return

    # Show confirmation popup and inline buttons
    confirm_text = get_text("autorenew_confirm_enable") if enable else get_text("autorenew_confirm_disable")
    kb = get_autorenew_confirm_keyboard(enable, sub.subscription_id, current_lang, i18n)
    try:
        await callback.message.edit_text(confirm_text, reply_markup=kb)
    except Exception:
        try:
            await callback.message.answer(confirm_text, reply_markup=kb)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    try:
        await callback.answer()
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    return


@router.callback_query(F.data.startswith("autorenew:confirm:"))
async def confirm_autorenew_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    try:
        _, _, sub_id_str, enable_str = callback.data.split(":", 3)
        sub_id = int(sub_id_str)
        enable = bool(int(enable_str))
    except Exception:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    sub = await session.get(Subscription, sub_id)
    if not sub or sub.user_id != callback.from_user.id:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if sub.provider != "yookassa":
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if enable:
        has_saved_card = await user_billing_dal.user_has_saved_payment_method(session, callback.from_user.id)
        if not has_saved_card:
            try:
                await callback.answer(get_text("autorenew_enable_requires_card"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            try:
                await my_subscription_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            return

    await subscription_dal.update_subscription(session, sub.subscription_id, {"auto_renew_enabled": enable})
    await session.commit()
    try:
        await callback.answer(get_text("subscription_autorenew_updated"))
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    await my_subscription_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)


@router.callback_query(F.data == "autorenew:cancel")
async def autorenew_cancel_from_webhook_button(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    # Disable auto-renew on the active subscription
    from db.dal import subscription_dal
    sub = await subscription_dal.get_active_subscription_by_user_id(session, callback.from_user.id)
    if not sub:
        try:
            await callback.answer(get_text("subscription_not_active"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return
    if sub.provider != "yookassa":
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return
    await subscription_dal.update_subscription(session, sub.subscription_id, {"auto_renew_enabled": False})
    await session.commit()
    try:
        await callback.answer(get_text("subscription_autorenew_updated"))
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    await my_subscription_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)


@router.message(Command("subscription"))
@router.message(Command("connect"))  # older name, kept for users who learned it
async def connect_command_handler(
    message: types.Message,
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot,
):
    logging.info(f"User {message.from_user.id} used /connect command.")
    await my_subscription_command_handler(message, i18n_data, settings, panel_service, subscription_service, session, bot)
