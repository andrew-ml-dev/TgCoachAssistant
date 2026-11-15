"""Подключение Polar Flow и импорт тренировок."""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import db, keyboards as kb
from ..services import nutrition, panel, polar
from ..services.budget import fmt

log = logging.getLogger(__name__)
router = Router()

SYNC_EVERY = 15 * 60
_last_sync: datetime | None = None
_lock = asyncio.Lock()  # кнопка «Проверить сейчас» и планировщик не должны импортировать одно и то же дважды
WEIGHT_STEP = 3.0       # кг по тренду, после которых напомнить обновить вес в профиле Polar


class Polar(StatesGroup):
    code = State()


NOT_CONFIGURED = (
    "<b>⌚ Polar Flow</b>\n"
    "Чтобы тренировки с H10 приходили сами, нужен клиент Polar AccessLink:\n"
    "1. admin.polaraccesslink.com → Create client.\n"
    f"2. Redirect URL: <code>{polar.REDIRECT}</code>\n"
    "3. Client ID и Client secret — в файл <code>.env</code> как POLAR_CLIENT_ID и POLAR_CLIENT_SECRET, "
    "затем деплой."
)


async def screen(message: Message, state: FSMContext, profile) -> None:
    if not polar.configured():
        return await message.answer(NOT_CONFIGURED)
    if profile["polar_token"]:
        return await message.answer(
            "<b>⌚ Polar Flow подключён.</b> Новые тренировки проверяю каждые 15 минут "
            "(после синхронизации H10 с приложением Polar Flow).",
            reply_markup=kb.ikb([("🔄 Проверить сейчас", "pl:sync")], [("Отключить", "pl:off")]))
    nonce = secrets.token_hex(4)
    await state.set_state(Polar.code)
    await state.update_data(polar_state=nonce)
    await message.answer(
        "<b>⌚ Подключение Polar Flow</b>\n"
        f"1. Откройте ссылку и войдите в Polar: <a href=\"{polar.auth_url(nonce)}\">подключить Polar</a>\n"
        "2. После «Accept» браузер перейдёт на страницу localhost, которая <b>не откроется</b> — так и должно быть.\n"
        "3. Скопируйте адрес этой страницы целиком (он начинается с http://localhost) и пришлите мне.\n\n"
        "Код действует 10 минут. Отмена: /cancel", disable_web_page_preview=True)


@router.message(Command("polar"))
async def polar_cmd(message: Message, state: FSMContext, profile) -> None:
    await screen(message, state, profile)


@router.callback_query(F.data == "s:polar")
async def polar_cb(cb: CallbackQuery, state: FSMContext, profile) -> None:
    await cb.answer()
    await screen(cb.message, state, profile)


@router.message(Polar.code, F.text)
async def got_code(message: Message, state: FSMContext, profile) -> None:
    data = await state.get_data()
    if "state=" in message.text and f"state={data.get('polar_state')}" not in message.text:
        return await message.answer("Это адрес от старой ссылки. Откройте последнюю ссылку подключения. Отмена: /cancel")
    code = polar.extract_code(message.text)
    await state.clear()
    if not code:  # не держим человека в режиме ввода кода: следующее сообщение должно работать как обычно
        return await message.answer("Не нашёл код Polar в сообщении, подключение отменил. Чтобы попробовать снова — "
                                    "/polar и пришлите адрес страницы целиком (http://localhost:5000/oauth2_callback?code=…).")
    try:
        token, uid = await polar.connect(code)
    except polar.PolarError as e:
        return await message.answer(str(e))
    except Exception:
        log.exception("Polar connect")
        return await message.answer("Не удалось связаться с Polar. Попробуйте ещё раз через пару минут: /polar")
    await db.update_profile(polar_token=token, polar_user=uid)
    last = await db.last_weight()
    if last:
        await db.mark_flag("polar_weight_ref", f"{last['trend']:.1f}")
    await message.answer(
        "✅ <b>Polar Flow подключён.</b>\n"
        "Новые тренировки буду забирать сами каждые 15 минут после того, как H10 синхронизируется "
        "с приложением Polar Flow. Polar отдаёт только тренировки, загруженные <b>после</b> подключения, "
        "поэтому сегодняшнюю, если она уже была, допишите вручную: <code>трен 650</code>.")
    await sync(message.bot, await db.get_profile(), force=True)


@router.callback_query(F.data == "pl:sync")
async def sync_now(cb: CallbackQuery, profile) -> None:
    n = await sync(cb.bot, profile, force=True)
    await cb.answer("Новых тренировок нет" if n == 0 else (f"Добавлено: {n}" if n else "Polar не ответил"))


@router.callback_query(F.data == "pl:off")
async def polar_off(cb: CallbackQuery) -> None:
    await db.update_profile(polar_token=None, polar_user=None)
    await cb.answer("Отключено")
    await cb.message.edit_text("Polar Flow отключён. Записанные тренировки остались. Подключить снова: /polar")


@router.callback_query(F.data.startswith("pl:dup:"))
async def same_workout(cb: CallbackQuery, profile) -> None:
    """Импортированная тренировка совпала с ручной записью — ручную убираем."""
    manual = int(cb.data.split(":")[2])
    w = await db.get_workout(manual)
    if w and not w["ext_id"]:
        await db.delete_workout(manual)
    await cb.answer("Ручная запись убрана")
    await cb.message.edit_reply_markup(reply_markup=kb.ikb([("❌ Удалить", f"wo:del:{cb.data.split(':')[3]}")]))
    await panel.refresh(cb.bot, profile)


@router.callback_query(F.data.startswith("pl:both:"))
async def different_workouts(cb: CallbackQuery) -> None:
    await cb.answer("Оставил обе")
    await cb.message.edit_reply_markup(reply_markup=kb.ikb([("❌ Удалить", f"wo:del:{cb.data.split(':')[2]}")]))


def card(e: polar.Exercise, net: int) -> str:
    hr = f" · пульс {e.hr_avg}/{e.hr_max}" if e.hr_avg else ""
    when = "" if e.date == db.today() else f" ({e.date[8:10]}.{e.date[5:7]})"
    return (f"⌚ <b>Из Polar{when}: {e.title}</b>{hr}\n"
            f"{fmt(e.kcal)} ккал → в расход <b>{fmt(net)} ккал</b> (−15%: обычный расход за это время уже учтён)")


async def sync(bot: Bot, profile, force: bool = False) -> int | None:
    """Забирает новые тренировки. Возвращает число добавленных или None при ошибке."""
    if not profile or not profile["polar_token"] or not polar.configured():
        return None
    async with _lock:
        return await _sync(bot, profile, force)


async def weight_hint(bot: Bot, profile) -> None:
    """Polar считает калории от веса в своём профиле: при похудении цифры завышаются, если вес там старый."""
    if not profile["polar_token"]:
        return
    last = await db.last_weight()
    ref = await db.get_flag("polar_weight_ref")
    if not last:
        return
    if ref is None:
        await db.mark_flag("polar_weight_ref", f"{last['trend']:.1f}")
        return
    if float(ref) - last["trend"] >= WEIGHT_STEP:
        await bot.send_message(
            profile["chat_id"],
            f"⌚ С подключения Polar вы похудели на {float(ref) - last['trend']:.1f} кг. Обновите вес в Polar Flow "
            f"(Настройки → Физические данные → {last['trend']:.0f} кг): от него Polar считает калории тренировок, "
            "и со старым весом цифры будут завышены.")
        await db.mark_flag("polar_weight_ref", f"{last['trend']:.1f}")


async def _sync(bot: Bot, profile, force: bool) -> int | None:
    global _last_sync
    now = db.now()
    if not force and _last_sync and (now - _last_sync).total_seconds() < SYNC_EVERY:
        return None
    _last_sync = now
    try:
        items = await polar.exercises(profile["polar_token"])
    except polar.PolarAuthError:
        await db.update_profile(polar_token=None)
        await bot.send_message(profile["chat_id"], "⌚ Polar отозвал доступ к тренировкам. Подключите заново: /polar")
        return None
    except Exception:
        log.exception("Polar sync")
        return None
    known = await db.known_ext_ids()
    start = (profile["created"] or "")[:10]
    added = 0
    for e in sorted(items, key=lambda x: x.date):
        if e.id in known or e.date < start:
            continue
        net = nutrition.workout_net(e.kcal)
        manual = [w for w in await db.workouts_on(e.date) if not w["ext_id"]]
        wid = await db.add_workout(e.title, e.kcal, net, e.date, ext_id=e.id, minutes=e.minutes,
                                   hr_avg=e.hr_avg, hr_max=e.hr_max)
        added += 1
        text = card(e, net)
        if manual:
            m = manual[-1]
            text += (f"\n\nЗа этот день уже есть ручная запись: {m['descr']}, {fmt(m['raw_kcal'] or m['kcal'])} ккал. "
                     "Это та же тренировка?")
            markup = kb.ikb([("🔁 Та же — убрать ручную", f"pl:dup:{m['id']}:{wid}")],
                            [("Разные тренировки", f"pl:both:{wid}")])
        else:
            markup = kb.ikb([("❌ Удалить", f"wo:del:{wid}")])
        await bot.send_message(profile["chat_id"], text, reply_markup=markup)
    if added:
        await panel.refresh(bot, profile)
    return added
