import asyncio
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .lknpd_client import LknpdClient, PaymentType, LknpdApiError


class LknpdService:
    def __init__(
        self,
        inn: Optional[str],
        password: Optional[str],
        api_url: str = "https://lknpd.nalog.ru/api",
        timezone_name: str = "Europe/Moscow",
    ) -> None:
        self.inn = inn.strip() if inn else None
        self.password = password
        self.configured = bool(self.inn and self.password)
        self._client = (
            LknpdClient(base_url=api_url, timezone=ZoneInfo(timezone_name)) if self.configured else None
        )
        self._auth_lock = asyncio.Lock()

        if not self.configured:
            logging.warning("LKNPD credentials are missing. Receipt sending disabled.")

    async def _ensure_authenticated(self) -> bool:
        if not self._client:
            return False

        async with self._auth_lock:
            if self._client.is_authenticated:
                return True

            try:
                await self._client.authenticate(self.inn, self.password)
                return True
            except LknpdApiError:
                logging.exception("LKNPD authentication failed.")
                return False

    async def create_income_receipt(
        self,
        *,
        item_name: str,
        amount: float,
        quantity: float = 1.0,
        operation_time: Optional[datetime] = None,
    ) -> Optional[str]:
        if not self.configured:
            return None
        if not await self._ensure_authenticated():
            return None

        try:
            receipt_uuid = await self._client.create_income(
                name=item_name,
                amount=amount,
                quantity=quantity,
                payment_type=PaymentType.WIRE,
                operation_time=operation_time,
            )
            if not receipt_uuid:
                logging.info("LKNPD receipt created without a UUID in response.")
            return receipt_uuid
        except LknpdApiError:
            logging.exception("Failed to create LKNPD receipt.")
            return None

    async def close(self) -> None:
        return None
