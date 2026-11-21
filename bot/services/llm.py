"""Обращения к Claude: разбор еды, оценка активности, недельный разбор, идеи."""
from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Literal

import anthropic
from pydantic import BaseModel

from .. import config, db

log = logging.getLogger(__name__)
_client: anthropic.AsyncAnthropic | None = None


class LLMError(Exception):
    """Сообщение пригодно для показа пользователю."""


class FoodItem(BaseModel):
    name: str
    grams: float
    kcal: float
    protein: float
    fat: float
    carbs: float


class FoodParse(BaseModel):
    items: list[FoodItem]
    question: str  # пустая строка, если уточнение не нужно


class Product(BaseModel):
    ok: bool        # false, если в тексте нет калорийности
    name: str
    kcal: float     # всё на 100 г
    protein: float
    fat: float
    carbs: float


class Label(Product):
    pack: float     # вес упаковки в граммах, 0 — если не виден


class ReviewItem(BaseModel):
    name: str        # точно как в списке продуктов пользователя или справочника
    role: Literal["p", "c", "f", "v"]
    grams: float     # обычная порция в этом блюде
    lo: float        # меньше — не предлагать (0 — можно убрать из блюда)
    hi: float


class ReviewDish(BaseModel):
    title: str
    slot: Literal["утро", "день", "вечер", "любое"]
    from_history: bool   # пользователь уже ест это сочетание
    items: list[ReviewItem]


class ReviewFix(BaseModel):
    problem: str
    change: str
    why: str
    short: str           # до 40 символов, для напоминания


class DietReview(BaseModel):
    summary: str
    fixes: list[ReviewFix]
    dishes: list[ReviewDish]


class VoiceCommand(BaseModel):
    action: Literal["food", "kcal", "workout", "weight", "can_eat", "report", "menu", "undo",
                    "product", "unknown"]
    text: str
    value: float
    yesterday: bool


class VoiceParse(BaseModel):
    commands: list[VoiceCommand]


VOICE_SYSTEM = """Ты разбираешь голосовое сообщение пользователя Telegram-бота для похудения. Текст получен \
распознаванием речи и часто искажает слова: восстанавливай их по звучанию (например «селом лет» — «омлет», \
«гречу» — «гречку»), особенно названия еды; если искажённое слово похоже на продукт из списка привычной еды \
пользователя, бери его. \
Определи, что хочет пользователь, и верни список команд по порядку. Команды:
- food — записать съеденное. text: что и сколько съел, коротко, с количествами, как сказано \
(например «гречка 200 г, котлета, огурец»), без слов «я съел», «запиши».
- kcal — записать только число калорий без описания еды. value: калории; text: к чему относится, если сказано.
- workout — тренировка. value: калории с часов (0, если не названы); text: вид тренировки, коротко.
- weight — вес тела. value: килограммы.
- can_eat — спрашивает, можно ли съесть / влезет ли блюдо в остаток, ничего не записывая. text: блюдо.
- report — хочет отчёт: сколько съел, сколько осталось, итоги дня.
- menu — спрашивает, что ему сейчас съесть, или хочет идею/меню.
- undo — удалить или отменить последнюю запись еды.
- product — запомнить продукт с этикетки. text: название и пищевая ценность, как сказано.
- unknown — ничего из перечисленного. text: одна короткая фраза по-русски о том, что бот умеет \
(записать еду, тренировку, вес; спросить, влезет ли блюдо; показать отчёт или подсказать, что съесть).
Числа словами переводи в цифры. yesterday=true, если речь о вчерашнем дне. \
Для команд без числа value=0, без текста text — пустая строка. \
Несколько действий в одном сообщении — несколько команд."""

LABEL_SYSTEM = """На фото упаковка продукта или таблица пищевой ценности. Извлеки пищевую ценность на 100 г: \
ккал, белки, жиры, углеводы (если дано только на порцию — пересчитай на 100 г). \
name: подсказка пользователя, если она есть; иначе бренд и название с упаковки строчными буквами; иначе пустая строка. \
pack: масса нетто упаковки в граммах (мл считай граммами), 0 — если не видна. \
Если таблицы с калорийностью на фото нет — ok=false."""

PRODUCT_SYSTEM = """Пользователь диктует данные с этикетки продукта. Извлеки название (как написал пользователь, \
строчными буквами, без слов «продукт», «запомни») и пищевую ценность на 100 г: ккал, белки, жиры, углеводы. \
Если цифры даны на порцию другого размера — пересчитай на 100 г. Если жиры или углеводы не названы — \
оцени их по типу продукта так, чтобы сходилась калорийность. Если в тексте нет ни калорий, ни белка — ok=false."""

FOOD_SYSTEM = """Ты диетолог-нутрициолог. Пользователь описывает съеденное по-русски. \
Разбей описание на продукты и оцени для каждого массу в граммах (в готовом виде), калории, белки, жиры, углеводы.

Правила:
- Если порция не указана, бери типовую домашнюю порцию (тарелка супа 300 г, кусок хлеба 30 г, \
котлета 100 г, стакан 250 мл, столовая ложка масла 15 г) и не переспрашивай.
- Учитывай способ приготовления и скрытые калории: масло при жарке, заправки, сахар в напитках.
- При сомнении оценивай калорийность ближе к верхней границе: люди чаще недооценивают съеденное.
- Названия продуктов короткие, по-русски, со строчной буквы.
- Поле question заполняй только если без ответа оценка может ошибиться более чем вдвое \
(например «поел в ресторане» без состава). В этом случае items оставь пустым. Иначе question — пустая строка.
- Если текст вообще не про еду, верни пустой items и question с просьбой описать еду."""

DIET_REVIEW_SYSTEM = """Ты спортивный диетолог. Человек худеет с сохранением мышц и записывает всю еду в бота. \
Тебе даны факты, посчитанные кодом по его записям: средние за полные дни, его продукты с обычными порциями, \
повторяющиеся приёмы пищи, найденные перекосы и цели. Цифры уже посчитаны — не пересчитывай их и не спорь с ними.

Задача — подстроить питание под его реальные привычки, а не придумать новую диету.
1. summary: 2–3 коротких предложения — как он ест сейчас и главное, что стоит поправить. Без похвалы и воды.
2. fixes: 1–3 самые важные поправки на эту неделю, по убыванию пользы для похудения с сохранением мышц. \
Каждая — конкретная замена или изменение порции в его же продуктах, которое легко выполнить \
(«пюре 270 → 180 г и огурец 150 г рядом», а не «ешьте больше овощей»). Не трогай то, что работает. \
Протеиновый порошок — нормальный источник белка; поправляй его только если из-за него не хватает обычной еды. \
short — суть поправки до 40 символов. Если есть прошлый разбор, учти, что из него уже выполнено, и не повторяй \
выполненное.
3. dishes: 4–8 блюд, из которых он будет выбирать, что съесть. Сначала его собственные частые сочетания \
с поправками из fixes (from_history=true), потом 1–3 новых варианта из его продуктов и справочника, \
закрывающих перекосы (from_history=false). Названия продуктов в items — строго дословно из списков \
«Продукты пользователя» или «Справочник», ничего другого. role: p — главный источник белка, c — углеводы, \
f — жиры, v — овощи. grams — порция с поправкой; lo/hi — разумные пределы, в которых бот будет подгонять \
порцию под остаток калорий (lo=0, если часть можно убрать). В каждом блюде должна быть часть с role=p. \
Протеиновый порошок отдельным блюдом не включай. slot — когда это обычно едят.
Учитывай предпочтения пользователя. Пиши по-русски, коротко."""


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise LLMError("Не задан ANTHROPIC_API_KEY. Можно вносить калории числом: «450 ккал».")
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def _check_budget() -> None:
    used = await db.usage_month()
    if used["cost"] >= config.MONTHLY_LLM_BUDGET:
        raise LLMError(
            f"Месячный лимит API (${config.MONTHLY_LLM_BUDGET:.2f}) исчерпан. "
            "Вносите калории числом: «450 ккал обед»."
        )


async def _call(kind: str, system: str, user: str | list, schema=None, max_tokens: int = 4000,
                model: str | None = None, effort: str = "low"):
    await _check_budget()
    client = _get_client()
    model = model or config.MODEL
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort},
    )
    api = client.messages
    if model == "claude-sonnet-5-5":
        # при отказе классификатора API сам повторит запрос на запасной модели в том же вызове
        api = client.beta.messages
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        if schema is not None:
            resp = await api.parse(output_format=schema, **kwargs)
        else:
            resp = await api.create(**kwargs)
    except anthropic.AuthenticationError:
        raise LLMError("Ключ Anthropic API не принят. Проверьте ANTHROPIC_API_KEY в .env.")
    except anthropic.RateLimitError:
        raise LLMError("Claude сейчас перегружен, попробуйте через минуту.")
    except anthropic.APIStatusError as e:
        log.error("Claude API %s: %s", e.status_code, e.message)
        raise LLMError(f"Ошибка Claude API ({e.status_code}). Попробуйте позже или внесите калории числом.")
    except anthropic.APIConnectionError:
        raise LLMError("Нет связи с Claude API. Попробуйте позже или внесите калории числом.")

    await db.add_usage(kind, resp.usage.input_tokens, resp.usage.output_tokens, resp.model or model)
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise LLMError("Не получилось разобрать сообщение. Попробуйте описать иначе.")
    if schema is not None:
        if resp.parsed_output is None:
            raise LLMError("Не получилось разобрать сообщение. Попробуйте описать иначе.")
        return resp.parsed_output
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def parse_food(text: str, allow_question: bool = True, products: list[dict] = ()) -> FoodParse:
    user = text if allow_question else text + "\n\n(Уточнений не задавай, дай лучшую оценку.)"
    if products:
        user += ("\n\nДанные с этикеток пользователя (на 100 г). Если продукт из этого списка есть в описании, "
                 "назови его точно так же и считай по этим цифрам:\n" + "\n".join(
                     f"- {p['name']}: {p['kcal']:g} ккал, Б {p['protein']:g}, Ж {p['fat']:g}, У {p['carbs']:g}"
                     for p in products))
    return await _call("food", FOOD_SYSTEM, user, FoodParse)


async def parse_product(text: str) -> Product:
    return await _call("product", PRODUCT_SYSTEM, text, Product, 2000)


async def route_voice(text: str, known_foods: list[str] = ()) -> VoiceParse:
    user = f"Распознанный текст: {text}"
    if known_foods:
        user += "\n\nПривычная еда пользователя: " + ", ".join(dict.fromkeys(known_foods))
    return await _call("voice", VOICE_SYSTEM, user, VoiceParse, 2000)


def _jpeg_b64(data: bytes) -> str:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
    img.thumbnail((1280, 1280))  # мелкий текст этикетки читается, токенов меньше
    buf = BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def parse_label(image: bytes, hint: str = "") -> Label:
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(image)}},
               {"type": "text", "text": f"Подсказка пользователя: {hint}" if hint else "Подсказки нет."}]
    return await _call("label", LABEL_SYSTEM, content, Label, 2000)


async def correct_food(items: list[dict], correction: str) -> FoodParse:
    lines = "\n".join(f"- {i['name']}, {i['grams']:.0f} г, {i['kcal']:.0f} ккал" for i in items)
    user = (f"Текущий разбор приёма пищи:\n{lines}\n\nПоправка пользователя: {correction}\n\n"
            "Верни исправленный полный список. Уточнений не задавай.")
    return await _call("food_fix", FOOD_SYSTEM, user, FoodParse)




async def diet_review(facts: str) -> DietReview:
    """Недельный разбор рациона — на модели сильнее (config.REVIEW_MODEL)."""
    return await _call("diet_review", DIET_REVIEW_SYSTEM, facts, DietReview, max_tokens=16000,
                       model=config.REVIEW_MODEL, effort="medium")
