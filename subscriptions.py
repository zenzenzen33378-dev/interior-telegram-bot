"""Подписка: ЮKassa (разовая оплата на месяц) + SQLite."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from yookassa import Configuration, Payment
from yookassa.domain.notification import WebhookNotificationEventType, WebhookNotificationFactory

logger = logging.getLogger(__name__)

DB_PATH = Path(
    os.getenv("SUBSCRIPTIONS_DB_PATH", str(Path(__file__).parent / "subscriptions.db"))
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _format_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class SubscriptionStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    paid_until TEXT,
                    generations_left INTEGER NOT NULL DEFAULT 0,
                    trial_used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS processed_payments (
                    payment_id TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_payments (
                    payment_id TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "trial_used" not in columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN trial_used INTEGER NOT NULL DEFAULT 0"
                )

    def get_user(self, telegram_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT telegram_id, paid_until, generations_left, trial_used
                FROM users
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            ).fetchone()
        if not row:
            return {
                "telegram_id": telegram_id,
                "paid_until": None,
                "generations_left": 0,
                "trial_used": False,
            }
        return {
            "telegram_id": row["telegram_id"],
            "paid_until": _parse_dt(row["paid_until"]),
            "generations_left": row["generations_left"],
            "trial_used": bool(row["trial_used"]),
        }

    def status_text(self, telegram_id: int) -> str:
        user = self.get_user(telegram_id)
        paid_until = user["paid_until"]
        left = user["generations_left"]
        trial_used = user["trial_used"]
        now = _utcnow()
        if paid_until and paid_until > now and left > 0:
            local_until = paid_until.astimezone().strftime("%d.%m.%Y %H:%M")
            return (
                f"✅ Подписка активна до {local_until}\n"
                f"Осталось генераций: {left}"
            )
        if paid_until and paid_until > now and left <= 0:
            return (
                "⚠️ Срок подписки ещё не истёк, но лимит генераций исчерпан.\n"
                "Оплатите снова: /subscribe"
            )
        if not trial_used:
            return "🎁 Доступна 1 бесплатная пробная генерация.\nЗапустите /design"
        return "❌ Подписка не активна.\nОформите: /subscribe"

    def can_generate(self, telegram_id: int) -> bool:
        user = self.get_user(telegram_id)
        paid_until = user["paid_until"]
        if paid_until and paid_until > _utcnow() and user["generations_left"] > 0:
            return True
        return not user["trial_used"]

    def consume_generation(self, telegram_id: int) -> bool:
        if not self.can_generate(telegram_id):
            return False
        user = self.get_user(telegram_id)
        paid_until = user["paid_until"]
        if paid_until and paid_until > _utcnow() and user["generations_left"] > 0:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE users
                    SET generations_left = generations_left - 1
                    WHERE telegram_id = ? AND generations_left > 0
                    """,
                    (telegram_id,),
                )
                return conn.total_changes == 1

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, paid_until, generations_left, trial_used)
                VALUES (?, NULL, 0, 1)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    trial_used = 1
                """,
                (telegram_id,),
            )
            return conn.total_changes == 1

    def is_payment_processed(self, payment_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
        return row is not None

    def activate_subscription(self, telegram_id: int, payment_id: str, days: int, generations: int) -> None:
        now = _utcnow()
        user = self.get_user(telegram_id)
        base = user["paid_until"] if user["paid_until"] and user["paid_until"] > now else now
        paid_until = base + timedelta(days=days)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, paid_until, generations_left)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    paid_until = excluded.paid_until,
                    generations_left = excluded.generations_left
                """,
                (telegram_id, _format_dt(paid_until), generations),
            )
            conn.execute(
                """
                INSERT INTO processed_payments (payment_id, telegram_id, processed_at)
                VALUES (?, ?, ?)
                """,
                (payment_id, telegram_id, _format_dt(now)),
            )
            conn.execute("DELETE FROM pending_payments WHERE payment_id = ?", (payment_id,))

    def save_pending_payment(self, telegram_id: int, payment_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_payments (payment_id, telegram_id, created_at)
                VALUES (?, ?, ?)
                """,
                (payment_id, telegram_id, _format_dt(_utcnow())),
            )

    def latest_pending_payment(self, telegram_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payment_id FROM pending_payments
                WHERE telegram_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (telegram_id,),
            ).fetchone()
        return row["payment_id"] if row else None

    def grant_subscription_skip(self, telegram_id: int, days: int, generations: int) -> None:
        """Временная активация без оплаты (тестовый режим)."""
        payment_id = f"skip-{telegram_id}-{uuid.uuid4()}"
        self.activate_subscription(telegram_id, payment_id, days, generations)
        logger.info("Skip-payment subscription user=%s payment=%s", telegram_id, payment_id)


class YooKassaBilling:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        store: SubscriptionStore,
        *,
        price_rub: float,
        subscription_days: int,
        subscription_generations: int,
        return_url: str,
    ) -> None:
        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key
        self.store = store
        self.price_rub = price_rub
        self.subscription_days = subscription_days
        self.subscription_generations = subscription_generations
        self.return_url = return_url

    def create_payment(self, telegram_id: int) -> tuple[str, str]:
        amount = f"{self.price_rub:.2f}"
        idempotence_key = str(uuid.uuid4())
        payment = Payment.create(
            {
                "amount": {"value": amount, "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": self.return_url,
                },
                "capture": True,
                "description": (
                    f"Подписка MakeRoomBot: {self.subscription_days} дн., "
                    f"{self.subscription_generations} генераций"
                ),
                "metadata": {"telegram_user_id": str(telegram_id)},
            },
            idempotence_key,
        )
        payment_id = payment.id
        confirmation_url = payment.confirmation.confirmation_url
        self.store.save_pending_payment(telegram_id, payment_id)
        return payment_id, confirmation_url

    def try_activate_payment(self, payment_id: str) -> tuple[bool, str, Optional[int]]:
        if self.store.is_payment_processed(payment_id):
            return True, "Эта оплата уже была учтена ранее.", None

        payment = Payment.find_one(payment_id)
        status = payment.status
        metadata = payment.metadata or {}
        telegram_id_raw = metadata.get("telegram_user_id")
        if not telegram_id_raw:
            return False, "В платеже нет данных пользователя.", None

        telegram_id = int(telegram_id_raw)

        if status == "succeeded":
            self.store.activate_subscription(
                telegram_id,
                payment_id,
                self.subscription_days,
                self.subscription_generations,
            )
            logger.info("Subscription activated user=%s payment=%s", telegram_id, payment_id)
            return True, "Подписка активирована!", telegram_id

        if status == "pending" or status == "waiting_for_capture":
            return False, "Оплата ещё не завершена. Завершите платёж и нажмите «Проверить оплату».", None

        if status == "canceled":
            return False, "Платёж отменён. Нажмите /subscribe для новой ссылки.", None

        return False, f"Статус платежа: {status}", None

    def process_webhook_body(self, body: bytes) -> Optional[int]:
        payload = json.loads(body.decode("utf-8"))
        notification = WebhookNotificationFactory().create(payload)
        if notification.event != WebhookNotificationEventType.PAYMENT_SUCCEEDED:
            return None

        payment = notification.object
        payment_id = payment.id
        if self.store.is_payment_processed(payment_id):
            return None

        metadata = payment.metadata or {}
        telegram_id_raw = metadata.get("telegram_user_id")
        if not telegram_id_raw:
            logger.warning("Webhook payment without telegram_user_id: %s", payment_id)
            return None

        telegram_id = int(telegram_id_raw)
        self.store.activate_subscription(
            telegram_id,
            payment_id,
            self.subscription_days,
            self.subscription_generations,
        )
        logger.info("Webhook activated user=%s payment=%s", telegram_id, payment_id)
        return telegram_id
