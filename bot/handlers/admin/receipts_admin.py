"""/receipts — «Мой налог» receipts that have not been accepted yet, with «retry now»."""
import html
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from bot.utils.money import format_money
from config.settings import Settings
from db.dal import payment_dal

router = Router(name="admin_receipts_router")

_LIST_LIMIT = 10


def build_receipts_text(pending: list, get_text, tz) -> str:
    if not pending:
        return get_text("receipts_none")
    parts = [get_text("receipts_list_header", count=len(pending))]
    for payment in pending[:_LIST_LIMIT]:
        paid_at = payment.receipt_paid_at or payment.created_at
        next_at = payment.receipt_next_attempt_at
        parts.append(get_text(
            "receipts_list_item",
            payment_id=payment.payment_id,
            amount=format_money(payment.amount),
            paid_at=paid_at.astimezone(tz).strftime("%d.%m %H:%M") if paid_at else "-",
            attempts=payment.receipt_attempts or 0,
            next_at=next_at.astimezone(tz).strftime("%d.%m %H:%M") if next_at else get_text("receipts_next_now"),
        ))
    if len(pending) > _LIST_LIMIT:
        parts.append(get_text("receipts_list_more", count=len(pending) - _LIST_LIMIT))
    last_error = next((p.receipt_last_error for p in reversed(pending) if p.receipt_last_error), None)
    if last_error:
        parts.append(get_text("receipts_list_error", error=html.escape(last_error)))
    return "\n\n".join(parts)


async def _show(target: types.Message, settings: Settings, i18n_data: dict, session: AsyncSession, edit: bool):
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(lang, key, **kw)
    pending = await payment_dal.get_pending_receipts(session)
    text = build_receipts_text(pending, get_text, ZoneInfo(settings.LKNPD_TIMEZONE))
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=get_text("receipts_retry_button"), callback_data="receipts:retry",
    )]]) if pending else None
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(Command("receipts"))
async def receipts_command_handler(message: types.Message, settings: Settings, i18n_data: dict,
                                   session: AsyncSession):
    await _show(message, settings, i18n_data, session, edit=False)


@router.callback_query(F.data == "receipts:retry")
async def receipts_retry_handler(callback: types.CallbackQuery, settings: Settings, i18n_data: dict,
                                 session: AsyncSession, receipt_service=None):
    lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    await payment_dal.retry_pending_receipts_now(session)
    await session.commit()
    if receipt_service:
        receipt_service.kick()
    await callback.answer(i18n.gettext(lang, "receipts_retry_started"))
    try:
        await _show(callback.message, settings, i18n_data, session, edit=True)
    except Exception as exc:
        logging.debug("Receipts list was not refreshed: %s", exc)
