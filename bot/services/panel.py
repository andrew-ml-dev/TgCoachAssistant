"""Закреплённая панель: одно сообщение вверху чата со сводкой дня.

Обновляется после каждой записи и раз в минуту планировщиком (новый день, смена бюджета).
Сообщение одно на всё время: правится на месте, поэтому «закрепил сообщение» появляется только раз.
"""
from __future__ import annotations

import logging
from datetime import date

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from .. import db
from . import nutrition
from .budget import WD, fmt

log = logging.getLogger(__name__)


async def panel_text(profile) -> str:
    day = db.today()
    tot = await db.day_totals(day)
    left = profile["budget"] - tot["kcal"]
    pct = tot["kcal"] / profile["budget"] * 100
    d = date.fromisoformat(day)
    head = (f"осталось <b>{fmt(left)}</b> ккал" if left >= 0 else f"перебор <b>+{fmt(-left)}</b> ккал")
    lines = [
        f"📌 {WD[d.weekday()]} {d.strftime('%d.%m')} · {head}",
        f"{nutrition.bar(pct)} {fmt(tot['kcal'])} / {fmt(profile['budget'])} · белок {fmt(tot['protein'])}/{profile['protein_goal']} г",
    ]
    w = await db.weight_on(day)
    if w:
        week = await db.weight_before(db.days_ago(7))
        delta = f" · за 7 дн {w['trend'] - week['trend']:+.1f}" if week and week["date"] != day else ""
        lines.append(f"⚖️ {w['weight']:.1f} (тренд {w['trend']:.1f}){delta}")
    else:
        lines.append("⚖️ сегодня не взвешен — отправьте число, например 92.4")
    return "\n".join(lines)


async def refresh(bot: Bot, profile=None) -> None:
    profile = profile or await db.get_profile()
    if not profile or not profile["chat_id"]:
        return
    text = await panel_text(profile)
    if text == profile["panel_text"] and profile["panel_msg"]:
        return
    try:
        if profile["panel_msg"]:
            try:
                await bot.edit_message_text(text, chat_id=profile["chat_id"], message_id=profile["panel_msg"])
                await db.update_profile(panel_text=text)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    await db.update_profile(panel_text=text)
                    return
                # сообщение удалено или его нельзя править — создаём заново
        msg = await bot.send_message(profile["chat_id"], text, disable_notification=True)
        try:
            await bot.pin_chat_message(profile["chat_id"], msg.message_id, disable_notification=True)
        except TelegramBadRequest:
            log.warning("Не удалось закрепить панель")
        await db.update_profile(panel_msg=msg.message_id, panel_text=text)
    except Exception:
        log.exception("Ошибка обновления панели")
