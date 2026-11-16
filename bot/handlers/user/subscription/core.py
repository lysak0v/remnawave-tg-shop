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
    short_hwid,
)
from bot.utils.money import RUB_SIGN
from bot.utils.subscription_view import (
    ACTIVE,
    DISABLED,
    TRIAL,
    build_subscription_text,
    devices_upto,
    is_renewal,
    last_seen_ago as _last_seen_ago,
    renew_first,
    subscription_state,
    utc_now_local,
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
        limit = int(max_devices_display) if str(max_devices_display).isdigit() else None
        limit_line = (get_text("devices_empty_limit", devices=devices_upto(limit, get_text)) if limit
                      else get_text("devices_empty_unlimited"))
        return get_text("no_devices_details_found_message", max_devices=max_devices_display,
                        limit_line=limit_line), []
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

    currency_symbol_val = RUB_SIGN
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

    details = await subscription_service.get_active_subscription_details(session, event.from_user.id)
    now = utc_now_local()
    state = subscription_state(details, now, settings.TRIAL_USER_TAG)
    traffic_mode = bool(getattr(settings, "traffic_sale_mode", False))
    soon_days = settings.SUBSCRIPTION_NOTIFY_DAYS_BEFORE
    running = state in (ACTIVE, TRIAL)

    devices_count: Optional[int] = None
    if running and settings.MY_DEVICES_SECTION_ENABLED and details.get("user_id"):
        try:
            devices_count = _count_devices(await panel_service.get_user_devices(details["user_id"]))
        except Exception:
            logging.exception("Failed to load devices for user %s", details.get("user_id"))

    if traffic_mode and details:
        text = _traffic_details_text(details, get_text)
    else:
        text = build_subscription_text(
            details, state, get_text=get_text, lang=current_lang, now=now,
            devices_count=devices_count, show_devices=settings.MY_DEVICES_SECTION_ENABLED,
            soon_days=soon_days,
        )

    buy_row = [InlineKeyboardButton(
        text=get_text("menu_renew_inline" if is_renewal(state) else "menu_subscribe_inline"),
        callback_data="main_action:subscribe",
    )]
    rows = []
    if running:
        rows += _connect_rows(details, settings, get_text)
        if settings.MY_DEVICES_SECTION_ENABLED:
            max_devices = details.get("max_devices")
            rows.append([InlineKeyboardButton(
                text=get_text(
                    "devices_button",
                    current_devices="?" if devices_count is None else devices_count,
                    max_devices=max_devices or get_text("devices_unlimited_label"),
                ),
                callback_data="main_action:my_devices",
            )])
        if renew_first(details, state, now, soon_days):
            rows.insert(0, buy_row)
        else:
            rows.append(buy_row)
        rows += await _autorenew_rows(session, event.from_user.id, settings, traffic_mode, get_text)
    elif state == DISABLED:
        if settings.SUPPORT_LINK:
            rows.append([InlineKeyboardButton(text=get_text("menu_support_button"), url=settings.SUPPORT_LINK)])
    else:
        rows.append(buy_row)
    markup = InlineKeyboardMarkup(
        inline_keyboard=rows + get_back_to_main_menu_markup(current_lang, i18n).inline_keyboard
    )

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


def _connect_rows(details: dict, settings: Settings, get_text) -> list:
    """«🔗 Подключиться»: the mini app when configured, else the subscription page link."""
    if settings.SUBSCRIPTION_MINI_APP_URL:
        return [[InlineKeyboardButton(
            text=get_text("connect_button"), web_app=WebAppInfo(url=settings.SUBSCRIPTION_MINI_APP_URL),
        )]]
    connect_url = details.get("connect_button_url") or details.get("config_link")
    if connect_url:
        return [[InlineKeyboardButton(text=get_text("connect_button"), url=connect_url)]]
    return []


def _count_devices(devices_response) -> Optional[int]:
    """Number of HWID devices from the panel answer (a list, or a dict with devices/total)."""
    if isinstance(devices_response, list):
        return len(devices_response)
    if not isinstance(devices_response, dict):
        return None
    devices = devices_response.get("devices")
    if isinstance(devices, list):
        return len(devices)
    if isinstance(devices, int):
        return devices
    total = devices_response.get("total")
    return total if isinstance(total, int) else None


def _traffic_details_text(details: dict, get_text) -> str:
    """Traffic sale mode keeps the older layout with the remaining amount."""
    def _fmt_gb(val: Optional[float]) -> str:
        if isinstance(val, (int, float)):
            return f"{float(val) / 2**30:.2f} GB"
        return get_text("traffic_na")

    limit_val = details.get("traffic_limit_bytes") or 0
    used_val = details.get("traffic_used_bytes") or 0
    end_date = details.get("end_date")
    return get_text(
        "my_traffic_details",
        status=details.get("status_from_panel", get_text("status_active")).capitalize(),
        end_date=end_date.strftime("%Y-%m-%d") if end_date else get_text("traffic_no_expiry"),
        traffic_limit=_fmt_gb(details.get("traffic_limit_bytes")),
        traffic_used=_fmt_gb(details.get("traffic_used_bytes")),
        traffic_left=_fmt_gb(max(0, float(limit_val) - float(used_val))),
        config_link=details.get("config_link") or get_text("config_link_not_available"),
    )


async def _autorenew_rows(session: AsyncSession, user_id: int, settings: Settings,
                          traffic_mode: bool, get_text) -> list:
    """Auto-renew toggle and card management (YooKassa autopayments only)."""
    if traffic_mode or not settings.yookassa_autopayments_active:
        return []
    rows = []
    try:
        local_sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id)
    except Exception:
        logging.exception("Failed to load local subscription for auto-renew buttons")
        local_sub = None
    if local_sub and local_sub.provider == "yookassa":
        rows.append([InlineKeyboardButton(
            text=get_text("autorenew_disable_button" if local_sub.auto_renew_enabled else "autorenew_enable_button"),
            callback_data=f"toggle_autorenew:{local_sub.subscription_id}:{1 if not local_sub.auto_renew_enabled else 0}",
        )])
    rows.append([InlineKeyboardButton(text=get_text("payment_methods_manage_button"), callback_data="pm:manage")])
    return rows


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
    if not devices_list_raw:
        devices_kb = _connect_rows(active, settings, get_text)

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
