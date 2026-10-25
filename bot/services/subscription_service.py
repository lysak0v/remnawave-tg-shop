import logging
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, NamedTuple, Tuple
from aiogram import Bot
from bot.middlewares.i18n import JsonI18n

from db.dal import user_dal, subscription_dal, promo_code_dal, user_billing_dal, payment_dal
from bot.utils.date_utils import add_months
from bot.utils.config_link import prepare_config_links
from db.models import User, Subscription

from config.settings import Settings
from .panel_api_service import PanelApiService


class PanelUserLink(NamedTuple):
    panel_user_id: Optional[int]
    panel_sub_link_id: Optional[str]
    panel_short_uuid: Optional[str]
    created_or_linked_now: bool
    panel_user: Optional[Dict[str, Any]] = None
    created_now: bool = False


def _parse_panel_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def resolve_extension_base(
    now: datetime,
    panel_expire_at: Optional[datetime],
    local_end_date: Optional[datetime],
    panel_user_created_now: bool,
) -> datetime:
    """Date a paid period or bonus days are added to.

    The panel is the source of truth for the expiry (admins may change it there).
    A panel user created in this very call carries only a placeholder expiry, so the
    period starts now. The local DB end date is used only when the panel gave no date.
    """
    if panel_user_created_now:
        return now
    reference = panel_expire_at if panel_expire_at is not None else local_end_date
    if reference is not None and reference > now:
        return reference
    return now


class SubscriptionService:

    def __init__(
        self,
        settings: Settings,
        panel_service: PanelApiService,
        bot: Optional[Bot] = None,
        i18n: Optional[JsonI18n] = None,
    ):
        self.settings = settings
        self.panel_service = panel_service
        self.bot = bot
        self.i18n = i18n

    async def get_user_language(self, session: AsyncSession, user_id: int) -> str:
        user_record = await user_dal.get_user_by_id(session, user_id)
        return (
            user_record.language_code
            if user_record and user_record.language_code
            else self.settings.DEFAULT_LANGUAGE
        )

    async def has_had_any_subscription(self, session: AsyncSession, user_id: int) -> bool:
        return await subscription_dal.has_any_subscription_for_user(session, user_id)

    async def has_active_subscription(self, session: AsyncSession, user_id: int) -> bool:
        """Return True if user currently has an active subscription (end_date in future)."""
        try:
            user_record = await user_dal.get_user_by_id(session, user_id)
            if not user_record or not user_record.panel_user_id:
                return False
            active_sub = await subscription_dal.get_active_subscription_by_user_id(
                session, user_id, user_record.panel_user_id
            )
            if not active_sub or not active_sub.end_date:
                return False
            from datetime import datetime, timezone
            return active_sub.is_active and active_sub.end_date > datetime.now(timezone.utc)
        except Exception:
            return False

    async def _notify_admin_panel_user_creation_failed(self, user_id: int):
        if not self.bot or not self.i18n or not self.settings.ADMIN_IDS:
            return
        admin_lang = self.settings.DEFAULT_LANGUAGE
        _adm = lambda k, **kw: self.i18n.gettext(admin_lang, k, **kw)
        msg = _adm("admin_panel_user_creation_failed", user_id=user_id)
        for admin_id in self.settings.ADMIN_IDS:
            try:
                await self.bot.send_message(admin_id, msg)
            except Exception as e:
                logging.error(
                    f"Failed to notify admin {admin_id} about panel user creation failure: {e}"
                )

    async def _get_or_create_panel_user_link_details(
        self, session: AsyncSession, user_id: int, db_user: Optional[User] = None
    ) -> Tuple[Optional[int], Optional[str], Optional[str], bool]:
        link = await self._ensure_panel_user(session, user_id, db_user)
        return (link.panel_user_id, link.panel_sub_link_id, link.panel_short_uuid,
                link.created_or_linked_now)

    async def _ensure_panel_user(
        self, session: AsyncSession, user_id: int, db_user: Optional[User] = None
    ) -> "PanelUserLink":
        if not db_user:
            db_user = await user_dal.get_user_by_id(session, user_id)

        if not db_user:
            logging.error(
                f"_get_or_create_panel_user_link_details: User {user_id} not found in local DB. Cannot proceed."
            )
            return PanelUserLink(None, None, None, False)

        current_local_panel_id = db_user.panel_user_id
        panel_username_on_panel_standard = f"tg_{user_id}"

        panel_user_obj_from_api = None
        panel_user_created_or_linked_now = False
        panel_user_created_now = False

        panel_users_by_tg_id_list = await self.panel_service.get_users_by_filter(
            telegram_id=user_id
        )
        if panel_users_by_tg_id_list and len(panel_users_by_tg_id_list) == 1:
            panel_user_obj_from_api = panel_users_by_tg_id_list[0]
            logging.info(
                f"Found panel user by telegramId {user_id}: id {panel_user_obj_from_api.get('id')}, Username: {panel_user_obj_from_api.get('username')}"
            )
        elif panel_users_by_tg_id_list and len(panel_users_by_tg_id_list) > 1:
            logging.error(
                f"CRITICAL: Multiple panel users found for telegramId {user_id}. Manual intervention needed."
            )
            return PanelUserLink(None, None, None, False)

        if not panel_user_obj_from_api:
            if current_local_panel_id:

                logging.info(
                    f"User {user_id} (local panel_id: {current_local_panel_id}) not found on panel by TG ID. Fetching by panel_id."
                )
                panel_user_obj_from_api = await self.panel_service.get_user_by_panel_id(
                    current_local_panel_id
                )
                if not panel_user_obj_from_api:
                    logging.warning(
                        f"Local panel_id {current_local_panel_id} for TG user {user_id} also not found on panel. User might be deleted from panel or UUID desynced."
                    )
                    logging.info(
                        f"Creating new panel user '{panel_username_on_panel_standard}' for TG user {user_id}."
                    )
                    creation_response = await self.panel_service.create_panel_user(
                        username_on_panel=panel_username_on_panel_standard,
                        telegram_id=user_id,
                        description="\n".join([
                            (db_user.username or "") if db_user else "",
                            (db_user.first_name or "") if db_user else "",
                            (db_user.last_name or "") if db_user else "",
                        ]),
                        specific_squad_uuids=self.settings.parsed_user_squad_uuids,
                        external_squad_uuid=self.settings.parsed_user_external_squad_uuid,
                        default_traffic_limit_bytes=self.settings.user_traffic_limit_bytes,
                        default_traffic_limit_strategy=self.settings.USER_TRAFFIC_STRATEGY,
                    )
                    if (
                        creation_response
                        and not creation_response.get("error")
                        and creation_response.get("response")
                    ):
                        panel_user_obj_from_api = creation_response.get("response")
                        panel_user_created_or_linked_now = True
                        panel_user_created_now = True
                    else:
                        await self._notify_admin_panel_user_creation_failed(user_id)
                        return PanelUserLink(None, None, None, False)

            else:

                logging.info(
                    f"No panel user by TG ID & no local panel_id for TG user {user_id}. Creating new panel user '{panel_username_on_panel_standard}'."
                )
                creation_response = await self.panel_service.create_panel_user(
                    username_on_panel=panel_username_on_panel_standard,
                    telegram_id=user_id,
                    description="\n".join([
                        (db_user.username or "") if db_user else "",
                        (db_user.first_name or "") if db_user else "",
                        (db_user.last_name or "") if db_user else "",
                    ]),
                    specific_squad_uuids=self.settings.parsed_user_squad_uuids,
                    external_squad_uuid=self.settings.parsed_user_external_squad_uuid,
                    default_traffic_limit_bytes=self.settings.user_traffic_limit_bytes,
                    default_traffic_limit_strategy=self.settings.USER_TRAFFIC_STRATEGY,
                )
                if (
                    creation_response
                    and not creation_response.get("error")
                    and creation_response.get("response")
                ):
                    panel_user_obj_from_api = creation_response.get("response")
                    panel_user_created_or_linked_now = True
                    panel_user_created_now = True

                elif creation_response and creation_response.get("errorCode") == "A019":
                    logging.warning(
                        f"Panel user '{panel_username_on_panel_standard}' already exists (errorCode A019). Fetching by username."
                    )
                    fetched_by_username_list = (
                        await self.panel_service.get_users_by_filter(
                            username=panel_username_on_panel_standard
                        )
                    )
                    if fetched_by_username_list and len(fetched_by_username_list) == 1:
                        panel_user_obj_from_api = fetched_by_username_list[0]

                if not panel_user_obj_from_api:
                    logging.error(
                        f"Failed to create or link panel user for TG_ID {user_id} with panel username '{panel_username_on_panel_standard}'. Response: {creation_response if 'creation_response' in locals() else 'N/A'}"
                    )
                    await self._notify_admin_panel_user_creation_failed(user_id)
                    return PanelUserLink(None, None, None, False)

        if not panel_user_obj_from_api:
            logging.error(
                f"Could not obtain panel user object for TG user {user_id} after all checks."
            )

            return PanelUserLink(
                current_local_panel_id if current_local_panel_id else None,
                None,
                None,
                panel_user_created_or_linked_now,
                panel_user=panel_user_obj_from_api,
                created_now=panel_user_created_now,
            )

        actual_panel_id_from_api = panel_user_obj_from_api.get("id")
        actual_panel_username_from_api = panel_user_obj_from_api.get("username")
        panel_telegram_id_from_api = panel_user_obj_from_api.get("telegramId")

        if not actual_panel_id_from_api:
            logging.error(
                f"Panel user object for TG user {user_id} does not contain 'id'. Data: {panel_user_obj_from_api}"
            )
            return PanelUserLink(
                current_local_panel_id,
                None,
                None,
                panel_user_created_or_linked_now,
                panel_user=panel_user_obj_from_api,
                created_now=panel_user_created_now,
            )

        needs_local_panel_id_update = False
        if current_local_panel_id is None and actual_panel_id_from_api:
            needs_local_panel_id_update = True
        elif (
            current_local_panel_id is not None
            and current_local_panel_id != actual_panel_id_from_api
        ):
            logging.warning(
                f"Local panel_id for user {user_id} ('{current_local_panel_id}') "
                f"differs from panel's UUID ('{actual_panel_id_from_api}') for their telegramId. "
                f"Will attempt to update local to panel's version."
            )
            needs_local_panel_id_update = True

        if needs_local_panel_id_update:

            conflicting_user_record = await user_dal.get_user_by_panel_id(
                session, actual_panel_id_from_api
            )
            if conflicting_user_record and conflicting_user_record.user_id != user_id:
                logging.error(
                    f"CRITICAL CONFLICT: Panel UUID {actual_panel_id_from_api} (from panel for TG ID {user_id}) "
                    f"is ALREADY LINKED in local DB to a different TG User {conflicting_user_record.user_id}. "
                    f"Cannot update panel_user_id for user {user_id}. Manual data correction needed."
                )

                return PanelUserLink(None, None, None, False)
            else:

                update_data_for_local_user = {
                    "panel_user_id": actual_panel_id_from_api
                }

                # Do not overwrite Telegram username with panel username.
                # Only update the local linkage to panel UUID here.
                await user_dal.update_user(session, user_id, update_data_for_local_user)
                db_user.panel_user_id = actual_panel_id_from_api
                panel_user_created_or_linked_now = True
                current_local_panel_id = actual_panel_id_from_api
        else:

            pass

        panel_telegram_id_int = None
        if panel_telegram_id_from_api is not None:
            try:
                panel_telegram_id_int = int(panel_telegram_id_from_api)
            except ValueError:
                pass

        if (
            panel_user_obj_from_api
            and current_local_panel_id
            and panel_telegram_id_int != user_id
        ):
            logging.info(
                f"Panel user {current_local_panel_id} has telegramId '{panel_telegram_id_from_api}'. Updating on panel to '{user_id}'."
            )
            # Also set readable description with Telegram fields
            await self.panel_service.update_user_details_on_panel(
                current_local_panel_id,
                {
                    "telegramId": user_id,
                    "description": "\n".join(
                        [
                            (db_user.username or "") if db_user else "",
                            (db_user.first_name or "") if db_user else "",
                            (db_user.last_name or "") if db_user else "",
                        ]
                    ),
                },
            )

        panel_sub_link_id = panel_user_obj_from_api.get(
            "subscriptionUuid"
        ) or panel_user_obj_from_api.get("shortUuid")
        panel_short_uuid = panel_user_obj_from_api.get("shortUuid")

        if not panel_sub_link_id and current_local_panel_id:
            logging.warning(
                f"No subscriptionUuid or shortUuid found on panel for panel_user_id {current_local_panel_id} (TG ID: {user_id})."
            )

        return PanelUserLink(
            current_local_panel_id,
            panel_sub_link_id,
            panel_short_uuid,
            panel_user_created_or_linked_now,
            panel_user=panel_user_obj_from_api,
            created_now=panel_user_created_now,
        )

    async def activate_trial_subscription(
        self, session: AsyncSession, user_id: int
    ) -> Optional[Dict[str, Any]]:
        if not self.settings.TRIAL_ENABLED or self.settings.TRIAL_DURATION_DAYS <= 0:
            return {
                "eligible": False,
                "activated": False,
                "message_key": "trial_feature_disabled",
            }

        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            logging.error(f"User {user_id} not found in DB, cannot activate trial.")
            return {
                "eligible": False,
                "activated": False,
                "message_key": "user_not_found_for_trial",
            }

        if await self.has_had_any_subscription(session, user_id):
            return {
                "eligible": False,
                "activated": False,
                "message_key": "trial_already_had_subscription_or_trial",
            }

        panel_user_id, panel_sub_link_id, panel_short_uuid, panel_user_created_now = (
            await self._get_or_create_panel_user_link_details(session, user_id, db_user)
        )

        if not panel_user_id or not panel_sub_link_id:
            logging.error(f"Failed to get panel link details for trial user {user_id}.")
            return {
                "eligible": True,
                "activated": False,
                "message_key": "trial_activation_failed_panel_link",
            }

        start_date = datetime.now(timezone.utc)
        end_date = start_date + timedelta(days=self.settings.TRIAL_DURATION_DAYS)

        await subscription_dal.deactivate_other_active_subscriptions(
            session, panel_user_id, panel_sub_link_id
        )

        trial_sub_data = {
            "user_id": user_id,
            "panel_user_id": panel_user_id,
            "panel_subscription_uuid": panel_sub_link_id,
            "start_date": start_date,
            "end_date": end_date,
            "duration_months": 0,
            "is_active": True,
            "status_from_panel": "TRIAL",
            "traffic_limit_bytes": self.settings.trial_traffic_limit_bytes,
            "auto_renew_enabled": False,
        }
        try:
            await subscription_dal.upsert_subscription(session, trial_sub_data)
        except Exception as e_upsert:
            logging.error(
                f"Failed to upsert trial subscription for user {user_id}: {e_upsert}",
                exc_info=True,
            )
            await session.rollback()
            return {
                "eligible": True,
                "activated": False,
                "message_key": "trial_activation_failed_db",
            }

        panel_update_payload = self._build_panel_update_payload(
            expire_at=end_date,
            status="ACTIVE",
            traffic_limit_bytes=self.settings.trial_traffic_limit_bytes,
        )

        # Add user description based on Telegram profile
        panel_update_payload["description"] = "\n".join(
            [
                (db_user.username or "") if db_user else "",
                (db_user.first_name or "") if db_user else "",
                (db_user.last_name or "") if db_user else "",
            ]
        )

        updated_panel_user = await self.panel_service.update_user_details_on_panel(
            panel_user_id, panel_update_payload
        )
        if not updated_panel_user or updated_panel_user.get("error"):
            logging.warning(
                f"Panel user details update FAILED for trial user {panel_user_id}. Response: {updated_panel_user}"
            )
            await session.rollback()
            return {
                "eligible": True,
                "activated": False,
                "message_key": "trial_activation_failed_panel_update",
            }

        await session.commit()

        final_subscription_url = updated_panel_user.get("subscriptionUrl")
        final_panel_short_uuid = updated_panel_user.get("shortUuid", panel_short_uuid)

        return {
            "eligible": True,
            "activated": True,
            "end_date": end_date,
            "days": self.settings.TRIAL_DURATION_DAYS,
            "traffic_gb": self.settings.TRIAL_TRAFFIC_LIMIT_GB,
            "panel_user_id": panel_user_id,
            "panel_short_uuid": final_panel_short_uuid,
            "subscription_url": final_subscription_url,
        }

    async def _activate_traffic_package(
        self,
        session: AsyncSession,
        user_id: int,
        traffic_gb: float,
        payment_amount: float,
        payment_db_id: int,
        provider: str = "yookassa",
    ) -> Optional[Dict[str, Any]]:
        """Activate or extend a traffic-based package instead of a time-based subscription."""
        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            logging.error("User %s not found for traffic package activation", user_id)
            return None

        panel_user_id, panel_sub_link_id, panel_short_uuid, _ = (
            await self._get_or_create_panel_user_link_details(session, user_id, db_user)
        )

        if not panel_user_id or not panel_sub_link_id:
            logging.error("Failed to ensure panel linkage for user %s during traffic activation", user_id)
            return None

        panel_user_data = await self.panel_service.get_user_by_panel_id(panel_user_id) or {}
        traffic_info = panel_user_data.get("userTraffic") or {}
        current_limit = panel_user_data.get("trafficLimitBytes")
        current_used = traffic_info.get("usedTrafficBytes")

        active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session, user_id, panel_user_id
        )
        if current_limit is None and active_sub:
            current_limit = active_sub.traffic_limit_bytes
        if current_used is None and active_sub:
            current_used = active_sub.traffic_used_bytes

        purchase_bytes = int(float(traffic_gb) * (1024**3))
        new_limit = (current_limit or 0) + purchase_bytes

        start_date = datetime.now(timezone.utc)
        # Set a far-future expiry to satisfy panel requirements; keep the latest known expiry if it's further.
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        final_end_date = far_future
        if active_sub and active_sub.end_date and active_sub.end_date > final_end_date:
            final_end_date = active_sub.end_date

        await subscription_dal.deactivate_other_active_subscriptions(
            session, panel_user_id, panel_sub_link_id
        )

        sub_payload = {
            "user_id": user_id,
            "panel_user_id": panel_user_id,
            "panel_subscription_uuid": panel_sub_link_id,
            "start_date": start_date,
            "end_date": final_end_date,
            "duration_months": 0,
            "is_active": True,
            "status_from_panel": "ACTIVE",
            "traffic_limit_bytes": new_limit,
            "traffic_used_bytes": current_used,
            "provider": provider,
            "skip_notifications": True,
            "auto_renew_enabled": False,
        }

        try:
            new_or_updated_sub = await subscription_dal.upsert_subscription(session, sub_payload)
        except Exception as exc:
            logging.error("Failed to upsert traffic subscription for user %s: %s", user_id, exc, exc_info=True)
            return None

        panel_update_payload = self._build_panel_update_payload(
            expire_at=final_end_date,
            status="ACTIVE",
            traffic_limit_bytes=new_limit,
            traffic_limit_strategy="NO_RESET",
        )

        panel_update_payload["description"] = "\n".join(
            [
                (db_user.username or "") if db_user else "",
                (db_user.first_name or "") if db_user else "",
                (db_user.last_name or "") if db_user else "",
            ]
        )

        updated_panel_user = await self.panel_service.update_user_details_on_panel(
            panel_user_id, panel_update_payload
        )
        if not updated_panel_user or updated_panel_user.get("error"):
            logging.warning(
                "Panel user details update FAILED for traffic package user %s. Response: %s",
                panel_user_id,
                updated_panel_user,
            )
            return None

        final_subscription_url = updated_panel_user.get("subscriptionUrl")
        final_panel_short_uuid = updated_panel_user.get("shortUuid", panel_short_uuid)

        return {
            "subscription_id": new_or_updated_sub.subscription_id,
            "end_date": final_end_date,
            "is_active": True,
            "panel_user_id": panel_user_id,
            "panel_short_uuid": final_panel_short_uuid,
            "subscription_url": final_subscription_url,
            "applied_promo_bonus_days": 0,
            "traffic_limit_bytes": new_limit,
        }

    def _extension_base_for(
        self,
        user_id: int,
        panel_link: "PanelUserLink",
        local_end_date: Optional[datetime],
    ) -> datetime:
        panel_expire_at = _parse_panel_datetime(
            (panel_link.panel_user or {}).get("expireAt")
        )
        if panel_expire_at is None and not panel_link.created_now:
            logging.warning(
                "Panel expireAt unavailable for user %s (panel id %s); extending from local end date %s.",
                user_id,
                panel_link.panel_user_id,
                local_end_date,
            )
        elif (
            local_end_date is not None
            and panel_expire_at is not None
            and abs((panel_expire_at - local_end_date).total_seconds()) > 60
        ):
            logging.info(
                "Expiry for user %s differs: panel %s vs local %s; using panel.",
                user_id,
                panel_expire_at,
                local_end_date,
            )
        return resolve_extension_base(
            datetime.now(timezone.utc),
            panel_expire_at,
            local_end_date,
            panel_link.created_now,
        )

    async def activate_subscription(
        self,
        session: AsyncSession,
        user_id: int,
        months: int,
        payment_amount: float,
        payment_db_id: int,
        promo_code_id_from_payment: Optional[int] = None,
        provider: str = "yookassa",
        sale_mode: str = "subscription",
        traffic_gb: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:

        if sale_mode == "traffic" or getattr(self.settings, "traffic_sale_mode", False):
            target_gb = traffic_gb if traffic_gb is not None else float(months)
            return await self._activate_traffic_package(
                session=session,
                user_id=user_id,
                traffic_gb=target_gb,
                payment_amount=payment_amount,
                payment_db_id=payment_db_id,
                provider=provider,
            )

        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            logging.error(
                f"User {user_id} not found in DB for paid subscription activation."
            )
            return None

        panel_link = await self._ensure_panel_user(session, user_id, db_user)
        panel_user_id, panel_sub_link_id, panel_short_uuid, panel_user_created_now = panel_link[:4]

        if not panel_user_id or not panel_sub_link_id:
            logging.error(
                f"Failed to ensure panel user for TG {user_id} during paid subscription."
            )
            return None

        try:
            months_int = int(months)
        except Exception:
            months_int = 1

        current_active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session, user_id, panel_user_id
        )
        start_date = self._extension_base_for(
            user_id,
            panel_link,
            current_active_sub.end_date if current_active_sub else None,
        )

        # base duration by months
        end_after_months = add_months(start_date, months_int)
        duration_days_total = (end_after_months - start_date).days
        applied_promo_bonus_days = 0

        if promo_code_id_from_payment:
            promo_model = await promo_code_dal.get_promo_code_by_id(
                session, promo_code_id_from_payment
            )
            if (
                promo_model
                and promo_model.is_active
                and promo_model.current_activations < promo_model.max_activations
            ):
                applied_promo_bonus_days = promo_model.bonus_days or 0
                duration_days_total += applied_promo_bonus_days

                activation = await promo_code_dal.record_promo_activation(
                    session,
                    promo_code_id_from_payment,
                    user_id,
                    payment_id=payment_db_id,
                )
                if activation:
                    await promo_code_dal.increment_promo_code_usage(
                        session, promo_code_id_from_payment, allow_overflow=True
                    )
                else:
                    logging.warning(
                        f"Promo code {promo_code_id_from_payment} was already activated by user {user_id}, but bonus applied via payment {payment_db_id}."
                    )
            else:
                logging.warning(
                    f"Promo code ID {promo_code_id_from_payment} (from payment) not found or invalid."
                )
                promo_code_id_from_payment = None

        final_end_date = start_date + timedelta(days=duration_days_total)
        await subscription_dal.deactivate_other_active_subscriptions(
            session, panel_user_id, panel_sub_link_id
        )

        auto_renew_should_enable = False
        if provider == "yookassa" and self.settings.yookassa_autopayments_active:
            auto_renew_should_enable = await user_billing_dal.user_has_saved_payment_method(
                session, user_id
            )

        sub_payload = {
            "user_id": user_id,
            "panel_user_id": panel_user_id,
            "panel_subscription_uuid": panel_sub_link_id,
            "start_date": start_date,
            "end_date": final_end_date,
            "duration_months": months_int,
            "is_active": True,
            "status_from_panel": "ACTIVE",
            "traffic_limit_bytes": self.settings.user_traffic_limit_bytes,
            "provider": provider,
            "skip_notifications": False,
            "auto_renew_enabled": auto_renew_should_enable,
        }
        try:
            new_or_updated_sub = await subscription_dal.upsert_subscription(
                session, sub_payload
            )
        except Exception as e_upsert_sub:
            logging.error(
                f"Failed to upsert paid subscription for user {user_id}: {e_upsert_sub}",
                exc_info=True,
            )
            return None

        panel_update_payload = self._build_panel_update_payload(
            expire_at=final_end_date,
            status="ACTIVE",
            traffic_limit_bytes=self.settings.user_traffic_limit_bytes,
        )

        # Add user description based on Telegram profile
        panel_update_payload["description"] = "\n".join(
            [
                (db_user.username or "") if db_user else "",
                (db_user.first_name or "") if db_user else "",
                (db_user.last_name or "") if db_user else "",
            ]
        )

        updated_panel_user = await self.panel_service.update_user_details_on_panel(
            panel_user_id, panel_update_payload
        )
        if not updated_panel_user or updated_panel_user.get("error"):
            logging.warning(
                f"Panel user details update FAILED for paid sub user {panel_user_id}. Response: {updated_panel_user}"
            )
            return None

        final_subscription_url = updated_panel_user.get("subscriptionUrl")
        final_panel_short_uuid = updated_panel_user.get("shortUuid", panel_short_uuid)

        # Consume discount promo code if payment had one
        try:
            promo_code_service = getattr(self, "promo_code_service", None)
            if not promo_code_service:
                from .promo_code_service import PromoCodeService

                promo_code_service = PromoCodeService(
                    self.settings, self, self.bot, self.i18n
                )
            await promo_code_service.consume_discount(session, user_id, payment_db_id)
        except Exception as e:
            logging.error(
                f"Failed to consume discount for user {user_id}, payment {payment_db_id}: {e}"
            )
            # Don't fail the subscription activation if discount consumption fails

        return {
            "subscription_id": new_or_updated_sub.subscription_id,
            "end_date": final_end_date,
            "is_active": True,
            "panel_user_id": panel_user_id,
            "panel_short_uuid": final_panel_short_uuid,
            "subscription_url": final_subscription_url,
            "applied_promo_bonus_days": applied_promo_bonus_days,
        }

    async def extend_active_subscription_days(
        self,
        session: AsyncSession,
        user_id: int,
        bonus_days: int,
        reason: str = "bonus",
    ) -> Optional[datetime]:
        reason_lower = (reason or "").lower()
        apply_main_traffic_limit = any(
            keyword in reason_lower for keyword in ("admin", "promo code", "referral", "bonus")
        )

        user = await user_dal.get_user_by_id(session, user_id)
        if not user:
            logging.warning(
                f"Cannot extend subscription for user {user_id}: user not found."
            )
            return None

        panel_link = await self._ensure_panel_user(session, user_id, user)
        panel_id, panel_sub_uuid = panel_link.panel_user_id, panel_link.panel_sub_link_id
        if not panel_id or not panel_sub_uuid:
            logging.error(
                f"Failed to ensure panel user for subscription extension of user {user_id}."
            )
            return None

        active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session, user_id, panel_id
        )
        if not active_sub or not active_sub.end_date:
            logging.info(
                f"No active subscription found for user {user_id}. Creating new one for {bonus_days} days."
            )
            start_date = datetime.now(timezone.utc)
            new_end_date_obj = (
                self._extension_base_for(user_id, panel_link, None)
                + timedelta(days=bonus_days)
            )

            # Apply main traffic limit for admin/referral/promo bonuses, fallback to trial limit otherwise
            traffic_limit = (
                self.settings.user_traffic_limit_bytes
                if apply_main_traffic_limit
                else self.settings.trial_traffic_limit_bytes
            )

            bonus_sub_payload = {
                "user_id": user_id,
                "panel_user_id": panel_id,
                "panel_subscription_uuid": panel_sub_uuid,
                "start_date": start_date,
                "end_date": new_end_date_obj,
                "duration_months": 0,
                "is_active": True,
                "status_from_panel": "ACTIVE_BONUS",
                "traffic_limit_bytes": traffic_limit,
                "auto_renew_enabled": False,
            }
            await subscription_dal.deactivate_other_active_subscriptions(
                session, panel_id, panel_sub_uuid
            )
            updated_sub_model = await subscription_dal.upsert_subscription(
                session, bonus_sub_payload
            )
        else:
            start_point_for_bonus = self._extension_base_for(
                user_id, panel_link, active_sub.end_date
            )
            new_end_date_obj = start_point_for_bonus + timedelta(days=bonus_days)

            updated_sub_model = await subscription_dal.update_subscription_end_date(
                session, active_sub.subscription_id, new_end_date_obj
            )

            if (
                apply_main_traffic_limit
                and updated_sub_model
                and updated_sub_model.traffic_limit_bytes != self.settings.user_traffic_limit_bytes
            ):
                updated_sub_model = await subscription_dal.update_subscription(
                    session,
                    updated_sub_model.subscription_id,
                    {"traffic_limit_bytes": self.settings.user_traffic_limit_bytes},
                )

        if updated_sub_model:
            # Prepare panel update payload
            panel_update_payload = self._build_panel_update_payload(
                expire_at=new_end_date_obj,
                traffic_limit_bytes=(
                    self.settings.user_traffic_limit_bytes if apply_main_traffic_limit else None
                ),
            )

            panel_update_success = (
                await self.panel_service.update_user_details_on_panel(
                    panel_id,
                    panel_update_payload,
                )
            )
            if not panel_update_success:
                logging.warning(
                    f"Panel expiry update failed for {panel_id} after {reason} bonus. Local DB was updated to {new_end_date_obj}."
                )

            logging.info(
                f"Subscription for user {user_id} extended by {bonus_days} days ({reason}). New end date: {new_end_date_obj}."
            )
            return new_end_date_obj
        else:
            logging.error(
                f"Failed to update subscription end date locally for user {user_id}."
            )
            return None

    async def get_active_subscription_details(
        self, session: AsyncSession, user_id: int
    ) -> Optional[Dict[str, Any]]:
        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user or not db_user.panel_user_id:
            logging.info(
                f"User {user_id} not found in DB or no panel_user_id for 'my_subscription'."
            )
            return None

        panel_user_id = db_user.panel_user_id
        local_active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session, user_id, panel_user_id
        )
        panel_user_data = await self.panel_service.get_user_by_panel_id(panel_user_id)

        if not panel_user_data:
            logging.warning(
                f"Panel user {panel_user_id} not found on panel for user {user_id}. Clearing local linkage."
            )
            await subscription_dal.deactivate_all_user_subscriptions(session, user_id)
            await user_dal.update_user(session, user_id, {"panel_user_id": None})
            return None

        if local_active_sub:
            update_payload_local = {}
            panel_status = panel_user_data.get("status", "UNKNOWN").upper()
            panel_expire_at_str = panel_user_data.get("expireAt")
            traffic_stats = panel_user_data.get("userTraffic") or {}
            panel_traffic_used = traffic_stats.get("usedTrafficBytes")
            panel_traffic_limit = panel_user_data.get("trafficLimitBytes")
            panel_sub_uuid_from_panel = panel_user_data.get(
                "subscriptionUuid"
            ) or panel_user_data.get("shortUuid")

            if local_active_sub.status_from_panel != panel_status:
                update_payload_local["status_from_panel"] = panel_status
            if panel_expire_at_str:
                panel_expire_dt = datetime.fromisoformat(
                    panel_expire_at_str.replace("Z", "+00:00")
                )
                if local_active_sub.end_date.replace(
                    microsecond=0
                ) != panel_expire_dt.replace(microsecond=0):
                    update_payload_local["end_date"] = panel_expire_dt
                    update_payload_local["last_notification_sent"] = None
            if (
                panel_traffic_used is not None
                and local_active_sub.traffic_used_bytes != panel_traffic_used
            ):
                update_payload_local["traffic_used_bytes"] = panel_traffic_used
            if (
                panel_traffic_limit is not None
                and local_active_sub.traffic_limit_bytes != panel_traffic_limit
            ):
                update_payload_local["traffic_limit_bytes"] = panel_traffic_limit
            if (
                panel_sub_uuid_from_panel
                and local_active_sub.panel_subscription_uuid
                != panel_sub_uuid_from_panel
            ):
                update_payload_local["panel_subscription_uuid"] = (
                    panel_sub_uuid_from_panel
                )

            is_active_based_on_panel = panel_status == "ACTIVE" and (
                panel_expire_dt > datetime.now(timezone.utc)
                if panel_expire_dt
                else False
            )
            if local_active_sub.is_active != is_active_based_on_panel:
                update_payload_local["is_active"] = is_active_based_on_panel

            if update_payload_local:
                await subscription_dal.update_subscription(
                    session, local_active_sub.subscription_id, update_payload_local
                )

        panel_end_date = (
            datetime.fromisoformat(panel_user_data["expireAt"].replace("Z", "+00:00"))
            if panel_user_data.get("expireAt")
            else None
        )
        config_link_raw = panel_user_data.get("subscriptionUrl")
        display_link, connect_button_url = await prepare_config_links(self.settings, config_link_raw)
        hwid_limit = panel_user_data.get("hwidDeviceLimit")
        if hwid_limit is None:
            hwid_limit = self.settings.USER_HWID_DEVICE_LIMIT

        return {
            "user_id": panel_user_data.get("id"),
            "end_date": panel_end_date,
            "status_from_panel": panel_user_data.get("status", "UNKNOWN").upper(),
            "config_link": display_link,
            "connect_button_url": connect_button_url,
            "traffic_limit_bytes": panel_user_data.get("trafficLimitBytes"),
            "traffic_used_bytes": (panel_user_data.get("userTraffic") or {}).get("usedTrafficBytes"),
            "user_bot_username": db_user.username,
            "is_panel_data": True,
            "max_devices": hwid_limit,
        }

    async def get_subscriptions_ending_soon(
        self, session: AsyncSession, days_threshold: int
    ) -> List[Dict[str, Any]]:
        subs_models_with_users = (
            await subscription_dal.get_subscriptions_near_expiration(
                session, days_threshold
            )
        )
        results = []
        for sub_model in subs_models_with_users:
            if (
                sub_model.user
                and sub_model.end_date
                and not sub_model.skip_notifications
            ):
                days_left = (
                    sub_model.end_date - datetime.now(timezone.utc)
                ).total_seconds() / (24 * 3600)
                results.append(
                    {
                        "user_id": sub_model.user_id,
                        "first_name": sub_model.user.first_name
                        or f"User {sub_model.user_id}",
                        "language_code": sub_model.user.language_code
                        or self.settings.DEFAULT_LANGUAGE,
                        "end_date_str": sub_model.end_date.strftime("%Y-%m-%d"),
                        "days_left": max(0, int(round(days_left))),
                        "subscription_end_date_iso_for_update": sub_model.end_date,
                    }
                )
        return results

    async def charge_subscription_renewal(
        self,
        session: AsyncSession,
        sub: Subscription,
    ) -> bool:
        """Attempt to charge user using saved payment method. Return True on initiated/handled, False on failure."""
        if getattr(self.settings, "traffic_sale_mode", False):
            logging.info("Auto-renew skipped: traffic sale mode enabled")
            return True
        if not sub.auto_renew_enabled:
            return True
        # If autopayments are disabled globally, skip charging attempts
        if not self.settings.yookassa_autopayments_active:
            return True
        if sub.provider != "yookassa":
            logging.info("Auto-renew skipped: provider %s does not support auto-renew", sub.provider)
            return True

        from db.dal.user_billing_dal import get_user_default_payment_method
        default_pm = await get_user_default_payment_method(session, sub.user_id)
        if not default_pm:
            logging.info(f"Auto-renew skipped: no saved payment method for user {sub.user_id}")
            return False

        try:
            from .yookassa_service import YooKassaService  # local import to avoid cycles
            yk: YooKassaService = self.yookassa_service  # type: ignore[attr-defined]
        except Exception:
            yk = None  # type: ignore
        if not yk or not getattr(yk, 'configured', False):
            logging.warning("YooKassa unavailable for auto-renew")
            return False

        months = sub.duration_months or 1
        amount = self.settings.subscription_options.get(months)
        if not amount:
            logging.error(f"Auto-renew price missing for {months} months")
            return False

        payment_description = f"Auto-renewal for {months} months"
        payment_record = await payment_dal.create_payment_record(
            session,
            {
                "user_id": sub.user_id,
                "amount": float(amount),
                "currency": "RUB",
                "status": "pending_yookassa",
                "description": payment_description,
                "subscription_duration_months": int(months),
                "provider": "yookassa",
            },
        )

        metadata = {
            "user_id": str(sub.user_id),
            "auto_renew_for_subscription_id": str(sub.subscription_id),
            "subscription_months": str(months),
            "payment_db_id": str(payment_record.payment_id),
        }
        resp = await yk.create_payment(
            amount=float(amount),
            currency="RUB",
            description=payment_description,
            metadata=metadata,
            payment_method_id=default_pm.provider_payment_method_id,
            save_payment_method=False,
            capture=True,
        )
        if not resp or resp.get("status") not in {"pending", "waiting_for_capture", "succeeded"}:
            logging.error(f"Auto-renew create_payment failed: {resp}")
            return False
        provider_payment_id = resp.get("id")
        if provider_payment_id:
            await payment_dal.update_provider_payment_and_status(
                session,
                payment_db_id=payment_record.payment_id,
                provider_payment_id=provider_payment_id,
                new_status="pending_yookassa",
            )
        logging.info(f"Auto-renew initiated for user {sub.user_id} payment_id={resp.get('id')}")
        return True

    async def update_last_notification_sent(
        self, session: AsyncSession, user_id: int, subscription_end_date: datetime
    ):
        sub_to_update = (
            await subscription_dal.find_subscription_for_notification_update(
                session, user_id, subscription_end_date
            )
        )
        if sub_to_update:
            await subscription_dal.update_subscription_notification_time(
                session, sub_to_update.subscription_id, datetime.now(timezone.utc)
            )
            logging.info(
                f"Updated last_notification_sent for user {user_id}, sub_id {sub_to_update.subscription_id}"
            )
        else:
            logging.warning(
                f"Could not find subscription for user {user_id} ending at {subscription_end_date.isoformat()} to update notification time."
            )

    # Helpers
    def _build_panel_update_payload(
        self,
        *,
        expire_at: Optional[datetime] = None,
        status: Optional[str] = None,
        traffic_limit_bytes: Optional[int] = None,
        traffic_limit_strategy: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if expire_at is not None:
            payload["expireAt"] = expire_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if status is not None:
            payload["status"] = status
        if traffic_limit_bytes is not None:
            payload["trafficLimitBytes"] = traffic_limit_bytes
            payload["trafficLimitStrategy"] = traffic_limit_strategy or self.settings.USER_TRAFFIC_STRATEGY
        if self.settings.parsed_user_squad_uuids:
            payload["activeInternalSquads"] = self.settings.parsed_user_squad_uuids
        if self.settings.parsed_user_external_squad_uuid:
            payload["externalSquadUuid"] = self.settings.parsed_user_external_squad_uuid
        return payload
