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
from ..handlers import common, diet
from . import budget, llm, panel

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


async def morning_menu(bot: Bot, profile, now: datetime) -> None:
    """Меню приходит после взвешивания; если вес пропущен или не внесён до отсечки — после неё."""
    if not profile["menu_enabled"] or now < _at(now, profile["wake_time"]) or now.hour >= 15:
        return
    if await db.get_menu(db.today()):
        return
    st = await db.day_state()
    weighed = await db.weight_on(db.today())
    if not (weighed or st["weight_skipped"] or now > _cutoff(profile, now)):
        return
    if not await db.mark_sent("menu"):
        return
    try:
        await diet.send_menu(bot, profile["chat_id"], profile)
    except llm.LLMError as e:
        log.warning("Утреннее меню не составлено: %s", e)


async def evening(bot: Bot, profile) -> None:
    if not await db.mark_sent("evening"):
        return
    tot = await db.day_totals(db.today())
    text = "🌙 <b>Итог дня</b>\n" + await budget.today_text(profile)
    if tot["n"] and tot["protein"] < profile["protein_goal"] * 0.7:
        text += "\n\n🥩 Белка сегодня мало. При дефиците он нужен, чтобы сохранять мышцы."
    text += ("\n\n<b>Всё за день записано?</b> Если что-то забыли — допишите и нажмите «✅ Всё записал». "
             "Неполные дни я не учитываю в расчёте расхода и прогнозе веса.")
    await bot.send_message(profile["chat_id"], text, reply_markup=kb.day_done(db.today()))


async def tick(bot: Bot) -> None:
    profile = await db.get_profile()
    if not profile:
        return
    now = db.now()
    hm = now.strftime("%H:%M")
    await panel.refresh(bot, profile)  # удаления, правки, новый день, новый бюджет
    await weight_reminder(bot, profile, now)
    await morning_menu(bot, profile, now)
    if "21:00" <= hm < "22:30":
        await evening(bot, profile)
    if now.weekday() == 6 and "10:00" <= hm < "12:00" and await db.mark_sent("weekly"):
        await common.send_weekly(bot, profile["chat_id"], profile, with_review=True)
    if "03:00" <= hm < "04:00" and await db.mark_sent("backup"):
        await db.backup()


async def run(bot: Bot) -> None:
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("Ошибка в планировщике")
        await asyncio.sleep(60)
