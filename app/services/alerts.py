from __future__ import annotations

import logging

from aiogram import Bot

from app.config import BootstrapConfig
from app.database import get_connection
from app.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, bot: Bot, bootstrap: BootstrapConfig, runtime: RuntimeConfig):
        self.bot = bot
        self.bootstrap = bootstrap
        self.runtime = runtime

    async def notify(self, key: str, text: str, *, cooldown_minutes: int = 60) -> None:
        if not self.runtime.get("operational_alerts"):
            return
        with get_connection() as conn:
            recent = conn.execute(
                """SELECT 1 FROM operational_alerts
                   WHERE alert_key=? AND last_sent_at>datetime('now',?)""",
                (key, f"-{cooldown_minutes} minutes"),
            ).fetchone()
            if recent:
                conn.execute("UPDATE operational_alerts SET count=count+1 WHERE alert_key=?", (key,))
                conn.commit()
                return
            conn.execute(
                """INSERT INTO operational_alerts(alert_key,last_sent_at,count)
                   VALUES (?,CURRENT_TIMESTAMP,1)
                   ON CONFLICT(alert_key) DO UPDATE SET last_sent_at=CURRENT_TIMESTAMP,count=count+1""",
                (key,),
            )
            conn.commit()
        for owner_id in self.bootstrap.owner_ids:
            try:
                await self.bot.send_message(owner_id, f"⚠️ {text[:3500]}")
            except Exception:
                logger.warning("Could not send operational alert to owner_id=%s", owner_id)
