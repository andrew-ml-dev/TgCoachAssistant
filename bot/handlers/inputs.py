"""Другие способы ввода: голосовые, фото штрихкода или этикетки, запись еды по шагам."""
import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, Update

from .. import db, keyboards as kb
from ..services import barcode, base_foods, foodtable, llm, voice
from ..services.parser import normalize
from . import common, diet

router = Router()


class Table(StatesGroup):
    confirm = State()


class Scan(StatesGroup):
    label = State()  # штрихкод не нашёлся — ждём фото таблицы пищевой ценности


class BaseFood(StatesGroup):
    grams = State()


class Steps(StatesGroup):
    what = State()
    amount = State()
    cook = State()


async def _download(message: Message) -> bytes:
    if message.photo:
        fid = message.photo[-1].file_id
    else:
        fid = (message.voice or message.audio or message.document).file_id
    return (await message.bot.download(fid)).read()


async def _refeed(message: Message, dispatcher, text: str) -> None:
    """Пропускает текст через обычные обработчики, как будто его написали в чат."""
    msg = message.model_copy(update={"text": text, "voice": None, "audio": None, "caption": None})
    await dispatcher.feed_update(message.bot, Update(update_id=0, message=msg))


# ---------- голосовые ----------

def command_text(c: llm.VoiceCommand) -> str | None:
    """Команда из голосового → текст в том виде, который бот понимает без Claude."""
    t, v = c.text.strip(), c.value
    out = None
    if c.action == "food" and t:
        out = t
    elif c.action == "kcal" and 0 < v <= 5000:
        out = f"{v:g} ккал {t}".strip()
    elif c.action == "workout":
        # калории убираем из подписи, «5 км» и «30 мин» оставляем
        label = re.sub(r"(?<![\d.,])\d+(?:[.,]\d+)?(?![\d.,]|\s*(?:мин|км|ч\b|час))(?:\s*(?:ккал|кал|kcal))?",
                       "", t)
        label = re.sub(r"\s+", " ", label).strip(" ,.-") or "тренировка"
        out = f"тренировка {label} {int(v)} ккал" if 30 <= v <= 3000 else "тренировка"
    elif c.action == "weight" and 30 <= v <= 300:
        out = f"вес {v:g}"
    elif c.action == "can_eat" and t:
        out = f"/can {t}"
    elif c.action == "product" and t:
        out = f"продукт: {t}"
    elif c.action in ("report", "menu", "undo"):
        out = {"report": "/today", "menu": "/eat", "undo": "/undo"}[c.action]
    if out and c.yesterday and c.action in ("food", "kcal", "workout", "weight"):
        out = "вчера: " + out
    return out


@router.message(F.voice | F.audio)
async def voice_msg(message: Message, state: FSMContext, dispatcher) -> None:
    if (message.voice or message.audio).duration > 120:
        return await message.answer("Голосовое длиннее 2 минут — разбейте на части.")
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        text = await voice.transcribe(await _download(message))
    except voice.VoiceError as e:
        return await message.answer(str(e))
    if not text:
        return await message.answer("Не расслышал, повторите или напишите текстом.")
    await message.answer(f"🎙 <i>{escape(text)}</i>")
    if await state.get_state():  # бот задал вопрос — голос это ответ на него
        return await _refeed(message, dispatcher, text)
    try:
        known = [p["name"] for p in await db.products()] + await db.top_foods()
        parsed = await llm.route_voice(text, known)
    except llm.LLMError as e:
        return await message.answer(str(e))
    done = False
    for c in parsed.commands:
        cmd = command_text(c)
        if cmd:
            done = True
            await _refeed(message, dispatcher, cmd)
        elif c.action == "unknown" and c.text:
            done = True
            await message.answer(escape(c.text))
    if not done:
        await message.answer("Не понял, что записать. Скажите, например: «съел гречку с курицей», "
                             "«тренировка 650 калорий», «вес 91 и 4».")


# ---------- фото: штрихкод или этикетка ----------

@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def photo(message: Message, state: FSMContext) -> None:
    await message.bot.send_chat_action(message.chat.id, "typing")
    data = await _download(message)
    sdata = await state.get_data() if await state.get_state() == Scan.label.state else {}
    code = sdata.get("scan_code") or await barcode.decode(data)

    if code and not sdata:
        p = await db.product_by_barcode(code)
        if p:
            if not p["pinned"]:   # был в базе (например, из таблицы) — раз сканируете, он нужен под рукой
                await db.update_product(p["id"], pinned=1)
            await state.clear()
            return await diet.ask_grams(message, state, p, "📦 Уже есть в ваших продуктах.\n")
        off = await barcode.lookup(code)
        if off and off["kcal"]:
            pid = await db.add_product(off["name"], round(off["kcal"], 1), round(off["protein"], 1),
                                       round(off["fat"], 1), round(off["carbs"], 1), code, off["pack"],
                                       source="barcode")
            p = await db.get_product(pid)
            await state.clear()
            return await diet.ask_grams(message, state, p, "📦 Нашёл по штрихкоду и запомнил:\n"
                                        + diet._product_line(p) + "\n\n")
        await state.set_state(Scan.label)
        await state.update_data(scan_code=code, scan_name=off["name"] if off else "")
        why = "есть в базе Open Food Facts, но без калорий" if off else "не нашёлся в базе Open Food Facts"
        return await message.answer(f"Штрихкод {code} {why}. Сфотографируйте таблицу пищевой ценности "
                                    "на упаковке — прочитаю её и запомню продукт. Отмена: /cancel")

    hint = (message.caption or "").strip() or sdata.get("scan_name", "")
    try:
        label = await llm.parse_label(data, hint)
    except llm.LLMError as e:
        return await message.answer(str(e))
    if not label.ok or label.kcal <= 0:
        if sdata:
            return await message.answer("Не вижу таблицы с калориями. Сфотографируйте её ближе и ровнее. "
                                        "Отмена: /cancel")
        return await message.answer("Не нашёл на фото ни штрихкода, ни таблицы пищевой ценности. "
                                    "Сфотографируйте штрихкод ближе и ровнее или таблицу на упаковке.")
    name = normalize(label.name)[:40] or f"продукт {db.now():%d.%m %H:%M}"
    pid = await db.add_product(name, round(label.kcal, 1), round(label.protein, 1), round(label.fat, 1),
                               round(label.carbs, 1), sdata.get("scan_code"), label.pack or None)
    p = await db.get_product(pid)
    await state.clear()
    note = "" if label.name else "Название не прочитал — в следующий раз добавьте его подписью к фото.\n"
    await diet.ask_grams(message, state, p, "📦 Прочитал этикетку и запомнил:\n" + diet._product_line(p)
                         + "\n" + note + "\n")


# ---------- по шагам: что → сколько → как приготовлено ----------

AMOUNTS = ["маленькая порция", "обычная порция", "большая порция", "100 г", "200 г", "300 г"]
COOK = ["варёное или на пару", "жареное на масле", "запечённое или гриль", "сырое, свежее",
        "готовое из магазина", ""]
COOK_LABELS = ["Варёное / на пару", "Жареное на масле", "Запечённое / гриль", "Сырое / свежее",
               "Готовое из магазина", "Пропустить"]


@router.callback_query(F.data.in_({"st:go", "st:more"}))
async def steps_start(cb: CallbackQuery, state: FSMContext) -> None:
    parts = (await state.get_data()).get("st_parts", []) if cb.data == "st:more" else []
    await state.clear()
    await state.set_state(Steps.what)
    await state.update_data(st_parts=parts)
    await cb.answer()
    await cb.message.answer("Шаг 1/3. Что съели? Одно блюдо или продукт — текстом или голосом. Отмена: /cancel")


@router.message(Steps.what, F.text)
async def steps_what(message: Message, state: FSMContext) -> None:
    await state.update_data(st_cur=message.text.strip())
    await state.set_state(Steps.amount)
    await message.answer("Шаг 2/3. Сколько? Выберите или напишите: «2 куска», «250 г», «стакан».",
                         reply_markup=kb.ikb(*[[(AMOUNTS[i].capitalize(), f"st:a:{i}") for i in row]
                                               for row in ((0, 1, 2), (3, 4, 5))]))


async def _ask_cook(message: Message, state: FSMContext, amount: str) -> None:
    cur = (await state.get_data()).get("st_cur", "")
    await state.update_data(st_cur=f"{cur}, {amount}")
    await state.set_state(Steps.cook)
    await message.answer("Шаг 3/3. Как приготовлено? Или напишите своё: «с майонезом», «в кляре».",
                         reply_markup=kb.ikb(*[[(COOK_LABELS[i], f"st:c:{i}") for i in row]
                                               for row in ((0, 1), (2, 3), (4, 5))]))


@router.callback_query(Steps.amount, F.data.startswith("st:a:"))
async def steps_amount_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await _ask_cook(cb.message, state, AMOUNTS[int(cb.data.split(":")[2])])


@router.message(Steps.amount, F.text)
async def steps_amount(message: Message, state: FSMContext) -> None:
    await _ask_cook(message, state, message.text.strip())


async def _summary(message: Message, state: FSMContext, cook: str) -> None:
    data = await state.get_data()
    cur = data.get("st_cur", "") + (f", {cook}" if cook else "")
    parts = data.get("st_parts", []) + [cur]
    await state.set_state(None)  # шаги закончены, собранное остаётся в данных до «Записать»
    await state.update_data(st_parts=parts, st_cur="")
    await message.answer("Записываю:\n" + "\n".join(f"• {escape(p)}" for p in parts),
                         reply_markup=kb.ikb([("➕ Ещё продукт", "st:more"), ("✅ Записать", "st:save")]))


@router.callback_query(Steps.cook, F.data.startswith("st:c:"))
async def steps_cook_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await _summary(cb.message, state, COOK[int(cb.data.split(":")[2])])


@router.message(Steps.cook, F.text)
async def steps_cook(message: Message, state: FSMContext) -> None:
    await _summary(message, state, message.text.strip())


@router.callback_query(F.data == "st:save")
async def steps_save(cb: CallbackQuery, state: FSMContext, profile) -> None:
    parts = (await state.get_data()).get("st_parts")
    await state.clear()
    await cb.answer()
    if not parts:
        return await cb.message.answer("Этот список уже записан или устарел. Начните заново: ⚡ Быстрая запись.")
    await cb.message.edit_reply_markup(reply_markup=None)
    try:
        await common.record_food_text(cb.message, profile, "; ".join(parts), allow_question=False)
    except llm.LLMError as e:
        await cb.message.answer(str(e))


# ---------- базовые продукты: выбрать → порция кнопкой или граммы ----------

@router.callback_query(F.data == "bf:list")
async def base_list(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    names = [(b.name, f"bf:{i}") for i, b in enumerate(base_foods.FOODS)]
    await cb.message.answer("🥚 <b>Базовые продукты</b> — цифры по справочнику, без Claude.\n"
                            "Можно и текстом: «творог 200 г», «2 яйца», «хлеб 2 куска».",
                            reply_markup=kb.ikb(*[names[i:i + 2] for i in range(0, len(names), 2)]))


@router.callback_query(F.data.regexp(r"^bf:\d+$"))
async def base_pick(cb: CallbackQuery, state: FSMContext) -> None:
    i = int(cb.data.split(":")[1])
    b = base_foods.FOODS[i]
    await state.set_state(BaseFood.grams)
    await state.update_data(bf=i)
    await cb.answer()
    await cb.message.answer(
        f"<b>{escape(b.name)}</b> — на 100 г: {b.kcal:g} ккал, Б {b.protein:g} · Ж {b.fat:g} · У {b.carbs:g}\n"
        f"Сколько? Выберите или отправьте граммы{' или штуки («3 шт»)' if b.piece else ''}.",
        reply_markup=kb.ikb([(label, f"bf:g:{i}:{g:g}") for label, g in b.portions]))


async def _record_base(message: Message, state: FSMContext, profile, i: int, grams: float) -> None:
    await state.clear()
    b = base_foods.FOODS[i]
    mid = await db.add_meal([base_foods.item(b, grams)], source="base", raw_text=f"{b.name} {grams:g} г")
    await common.send_meal(message, profile, mid, "по справочнику")


@router.callback_query(F.data.startswith("bf:g:"))
async def base_portion(cb: CallbackQuery, state: FSMContext, profile) -> None:
    _, _, i, g = cb.data.split(":")
    await cb.answer()
    await _record_base(cb.message, state, profile, int(i), float(g))


@router.message(BaseFood.grams, F.text)
async def base_grams(message: Message, state: FSMContext, profile) -> None:
    i = (await state.get_data())["bf"]
    b = base_foods.FOODS[i]
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*(шт|кус)?", message.text.lower())
    n = float(m.group(1).replace(",", ".")) if m else 0
    grams = n * b.piece if m and (m.group(2) or (b.piece and n <= 10)) and b.piece else n
    if not 1 <= grams <= 3000:
        return await message.answer("Отправьте граммы числом, например 150. Отмена: /cancel")
    await _record_base(message, state, profile, i, grams)


# ---------- база продуктов ↔ Excel ----------

@router.callback_query(F.data == "s:table")
async def table_export(cb: CallbackQuery) -> None:
    await cb.answer()
    rows = await db.foods()
    data = foodtable.export(rows)
    pinned = sum(1 for r in rows if r["pinned"])
    await cb.message.answer_document(
        BufferedInputFile(data, filename=f"продукты_{db.today()}.xlsx"),
        caption=f"📗 База продуктов: {len(rows)}, из них в быстрой записи {pinned}. Цифры — на 100 г.\n"
                "Поправьте в Excel или Google Таблицах (названия, синонимы, КБЖУ, штрихкод) и пришлите файл "
                "сюда — покажу изменения перед сохранением. Подробнее — на листе «Как пользоваться».")


@router.message(F.document & F.document.file_name.lower().endswith(".xlsx"))
async def table_import(message: Message, state: FSMContext) -> None:
    data = await _download(message)
    current = await db.foods()
    changes, errors, warns = foodtable.parse(data, current)
    text = foodtable.summary(changes, current, errors, warns)
    if not changes:
        await state.clear()
        return await message.answer(text)
    await state.set_state(Table.confirm)
    await state.update_data(table=changes)
    await message.answer(text, reply_markup=kb.ikb([("✅ Сохранить", "ft:ok"), ("Отмена", "ft:no")]))


@router.callback_query(Table.confirm, F.data == "ft:ok")
async def table_apply(cb: CallbackQuery, state: FSMContext) -> None:
    changes = (await state.get_data())["table"]
    await state.clear()
    done, errors = await foodtable.apply(changes)
    await cb.answer("Сохранено")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"📗 Сохранил изменений: {done}." + "".join(f"\n⚠️ {escape(e)}" for e in errors))


@router.callback_query(F.data.in_({"ft:ok", "ft:no"}))
async def table_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() == Table.confirm.state:
        await state.clear()
    await cb.answer("Отменено" if cb.data == "ft:no" else "Файл устарел — пришлите его ещё раз", show_alert=cb.data != "ft:no")
    await cb.message.edit_reply_markup(reply_markup=None)
