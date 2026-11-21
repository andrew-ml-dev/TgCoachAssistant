"""Планировщик: раз в минуту проверяет, что пора отправить.

Всё состояние (отложено, пропущено, что уже отправлено) лежит в БД,
поэтому перезапуск бота или Raspberry Pi ничего не сбивает.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot

from .. import db, keyboards as kb
from ..handlers import common
from . import panel

log = logging.getLogger(__name__)
REPEAT_MIN = 30


def _at(now: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _cutoff(profile, now: datetime) -> datetime:
    return max(_at(now, profile["cutoff_time"]), _at(now, profile["wake_time"]) + timedelta(hours=3))


async def weight_reminder(bot: Bot, profile, now: datetime) -> None:
    """Напоминание повторяется, но в чате всегда висит только одно: старое удаляется."""
    wake = _at(now, profile["wake_time"])
    if not wake <= now <= _cutoff(profile, now):
        return
    if await db.weight_on(db.today()):
        return
    st = await db.day_state()
    if st["weight_skipped"]:
        return
    if st["snooze_until"] and now < datetime.fromisoformat(st["snooze_until"]):
        return
    if st["last_weight_remind"]:
        last = datetime.fromisoformat(st["last_weight_remind"])
        if now - last < timedelta(minutes=REPEAT_MIN):
            return
        text = "⚖️ Вес всё ещё не внесён. Встаньте на весы и отправьте число."
    else:
        text = "⚖️ Доброе утро! Время взвеситься: после туалета, до еды и воды. Отправьте число."
    await common.drop_weight_reminder(bot, profile["chat_id"], db.days_ago(1))  # вчерашнее, если осталось
    await common.drop_weight_reminder(bot, profile["chat_id"])
    msg = await bot.send_message(profile["chat_id"], text, reply_markup=kb.weight_remind)
    await db.set_day_state(last_weight_remind=now.isoformat(timespec="minutes"), remind_msg=msg.message_id)


async def tick(bot: Bot) -> None:
    profile = await db.get_profile()
    if not profile:
        return
    now = db.now()
    hm = now.strftime("%H:%M")
    await panel.refresh(bot, profile)  # удаления, правки, новый день, новый бюджет
    from ..handlers import polar
    await polar.sync(bot, profile)     # сам решает, пора ли (раз в 15 минут)
    await weight_reminder(bot, profile, now)
    if now.weekday() == 6 and "10:00" <= hm < "12:00" and not await db.was_sent("weekly"):
        await common.send_weekly(bot, profile["chat_id"], profile, with_review=True)
        await db.mark_sent("weekly")
    if "03:00" <= hm < "04:00" and not await db.was_sent("backup"):
        await db.backup()
        await db.mark_sent("backup")


async def run(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(60)
