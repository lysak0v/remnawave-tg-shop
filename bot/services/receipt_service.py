"""«Мой налог» receipts: queued after payment and resent until LKNPD accepts them.

A receipt carries the payment time, so a late one is still dated correctly; for card and
SBP payments self-employed people may issue it up to the 9th of the next month.
"""
import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import sessionmaker

from bot.services.lknpd_client import LknpdValidationError
from bot.services.lknpd_service import LknpdService
from bot.utils.money import format_money
from config.settings import Settings
from db.dal import payment_dal
from db.dal.payment_dal import RECEIPT_NOT_NEEDED, RECEIPT_PENDING, RECEIPT_SENT
from db.models import Payment

# Pause before the next attempt by the number of failed attempts, in minutes.
RETRY_DELAYS_MIN = (5, 10, 20, 40, 60)
TICK = timedelta(minutes=5)
ALERT_AFTER = timedelta(hours=6)
REMIND_EVERY = timedelta(hours=24)
DEADLINE_DAY = 9
DEADLINE_WARN_FROM_DAY = 6
BATCH = 20
_ERROR_LIMIT = 500


def retry_delay(attempts: int) -> timedelta:
    index = min(max(attempts, 1), len(RETRY_DELAYS_MIN)) - 1
    return timedelta(minutes=RETRY_DELAYS_MIN[index])


def _paid_at(payment: Payment) -> Optional[datetime]:
    return payment.receipt_paid_at or payment.created_at


class ReceiptService:
    def __init__(self, settings: Settings, lknpd_service: Optional[LknpdService],
                 async_session_factory: sessionmaker, notify: Callable, get_text: Callable[..., str]):
        """`notify(text)` posts to the log channel; `get_text(key, **kw)` uses the default language."""
        self.settings = settings
        self.lknpd = lknpd_service
        self.session_factory = async_session_factory
        self.notify = notify
        self.get_text = get_text
        self.tz = ZoneInfo(settings.LKNPD_TIMEZONE)
        self._wake = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.lknpd and self.lknpd.configured)

    def kick(self) -> None:
        """Send queued receipts now instead of at the next tick (e.g. right after a payment)."""
        self._wake.set()

    async def run(self) -> None:
        logging.info("Receipt worker started")
        while True:
            try:
                await self.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Receipt worker iteration failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK.total_seconds())
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def process(self, now: Optional[datetime] = None) -> dict:
        """One pass: send due receipts, then alert about stuck ones or report the recovery."""
        now = now or datetime.now(timezone.utc)
        sent, failed = 0, 0
        recovered: List[Payment] = []
        async with self.session_factory() as session:
            due = await payment_dal.get_due_receipts(session, now, limit=BATCH)
            for index, payment in enumerate(due):
                if payment.status != "succeeded":
                    payment.receipt_status = RECEIPT_NOT_NEEDED
                    payment.receipt_next_attempt_at = None
                    continue
                try:
                    receipt_uuid = await self.lknpd.create_income_receipt(
                        item_name=payment.receipt_item_name or self._default_item_name(payment),
                        amount=payment.amount,
                        operation_time=_paid_at(payment),
                    )
                except LknpdValidationError as exc:
                    # This payment's data is refused; the others may still go through.
                    self._postpone([payment], exc, now)
                    failed += 1
                    continue
                except Exception as exc:
                    # Login or the service itself is down: do not hammer it with the rest.
                    rest = [p for p in due[index:] if p.status == "succeeded"]
                    self._postpone(rest, exc, now)
                    failed += len(rest)
                    logging.warning("Receipt sending paused: %s", exc)
                    break
                payment.receipt_status = RECEIPT_SENT
                payment.receipt_uuid = receipt_uuid
                payment.receipt_last_error = None
                payment.receipt_next_attempt_at = None
                sent += 1
                if payment.receipt_alerted_at:
                    recovered.append(payment)
            await session.commit()

            pending = await payment_dal.get_pending_receipts(session)
            if recovered:
                await self._send(self.get_text("receipt_recovered", count=len(recovered))
                                 + (self.get_text("receipt_recovered_left", count=len(pending)) if pending else ""))
            stuck = [p for p in pending if _paid_at(p) and _paid_at(p) <= now - ALERT_AFTER]
            if stuck and any(p.receipt_alerted_at is None or p.receipt_alerted_at <= now - REMIND_EVERY
                             for p in stuck):
                await self._send(self.alert_text(pending, now))
                for payment in stuck:
                    payment.receipt_alerted_at = now
                await session.commit()
        return {"sent": sent, "failed": failed, "recovered": len(recovered)}

    def _postpone(self, payments: List[Payment], exc: Exception, now: datetime) -> None:
        error = str(exc)[:_ERROR_LIMIT] or exc.__class__.__name__
        for payment in payments:
            payment.receipt_attempts = (payment.receipt_attempts or 0) + 1
            payment.receipt_last_error = error
            payment.receipt_next_attempt_at = now + retry_delay(payment.receipt_attempts)

    def _default_item_name(self, payment: Payment) -> str:
        return self.settings.LKNPD_RECEIPT_NAME_SUBSCRIPTION.format(
            months=payment.subscription_duration_months or 1)

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.tz)

    def alert_text(self, pending: List[Payment], now: datetime) -> str:
        _ = self.get_text
        oldest = min((_paid_at(p) for p in pending if _paid_at(p)), default=now)
        last_error = next((p.receipt_last_error for p in reversed(pending) if p.receipt_last_error), "-")
        text = _(
            "receipt_alert",
            count=len(pending),
            amount=format_money(sum(p.amount or 0 for p in pending)),
            oldest=self._local(oldest).strftime("%d.%m.%Y %H:%M"),
            error=html.escape(last_error),
        )
        today = self._local(now)
        previous_month = any(
            (self._local(_paid_at(p)).year, self._local(_paid_at(p)).month) < (today.year, today.month)
            for p in pending if _paid_at(p)
        )
        if previous_month and today.day >= DEADLINE_WARN_FROM_DAY:
            key = "receipt_alert_deadline" if today.day <= DEADLINE_DAY else "receipt_alert_overdue"
            text += "\n\n" + _(key, day=DEADLINE_DAY)
        return text

    async def handle_refund(self, session, yookassa_payment_id: Optional[str]) -> Optional[str]:
        """After a confirmed refund: drop an unsent receipt, or remind to cancel a sent one."""
        if not yookassa_payment_id:
            return None
        payment = await payment_dal.get_payment_by_yookassa_id(session, yookassa_payment_id)
        if not payment or not payment.receipt_status:
            return None
        amount = format_money(payment.amount)
        if payment.receipt_status == RECEIPT_PENDING:
            payment.receipt_status = RECEIPT_NOT_NEEDED
            payment.receipt_next_attempt_at = None
            await session.commit()
            await self._send(self.get_text("receipt_refund_unsent", payment_id=payment.payment_id, amount=amount))
            return RECEIPT_NOT_NEEDED
        if payment.receipt_status == RECEIPT_SENT:
            await self._send(self.get_text(
                "receipt_refund_sent", payment_id=payment.payment_id, amount=amount,
                receipt_uuid=html.escape(payment.receipt_uuid or "-"),
            ))
            return RECEIPT_SENT
        return None

    async def _send(self, text: str) -> None:
        try:
            await self.notify(text)
        except Exception:
            logging.exception("Failed to post receipt notice to the log channel")
