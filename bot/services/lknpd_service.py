import asyncio
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .lknpd_client import LknpdApiError, LknpdAuthError, LknpdClient, PaymentType, stable_device_id


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
            LknpdClient(base_url=api_url, timezone=ZoneInfo(timezone_name), device_id=stable_device_id(self.inn))
            if self.configured else None
        )
        self._auth_lock = asyncio.Lock()

        if not self.configured:
            logging.warning("LKNPD credentials are missing. Receipt sending disabled.")

    async def _ensure_authenticated(self) -> None:
        async with self._auth_lock:
            if not self._client.is_authenticated:
                await self._client.authenticate(self.inn, self.password)

    async def create_income_receipt(
        self,
        *,
        item_name: str,
        amount: float,
        quantity: float = 1.0,
        operation_time: Optional[datetime] = None,
    ) -> Optional[str]:
        """Register the income in «Мой налог» and return the receipt UUID (None if the API gave none).

        Raises LknpdApiError (with the API answer) on any failure, so the caller can retry later.
        """
        if not self.configured:
            raise LknpdApiError("LKNPD credentials are not configured")
        await self._ensure_authenticated()
        try:
            receipt_uuid = await self._client.create_income(
                name=item_name,
                amount=amount,
                quantity=quantity,
                payment_type=PaymentType.WIRE,
                operation_time=operation_time,
            )
        except LknpdAuthError:
            self._client._token_data = None  # log in again on the next attempt
            raise
        if not receipt_uuid:
            logging.info("LKNPD receipt created without a UUID in response.")
        return receipt_uuid

    async def close(self) -> None:
        return None
