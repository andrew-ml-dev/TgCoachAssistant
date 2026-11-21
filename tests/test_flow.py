"""Сквозной тест: апдейты Telegram проходят через настоящие обработчики.

Сеть заменена заглушкой (запросы к Telegram записываются), Claude — фиктивным ответом.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import DeleteMessage, EditMessageMedia, EditMessageText, SendDocument, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from bot import config, db
from bot.handlers import body, diet, food, inputs, menu, misc, onboarding, polar
from bot.main import Access
from bot.services import llm, reminders

UID = 111


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto, SendDocument, EditMessageText, EditMessageMedia)):
            return Message(message_id=1000 + len(self.calls), date=datetime.now(),
                           chat=Chat(id=UID, type="private"), text=getattr(method, "text", None) or "x")
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""


class Harness:
    def __init__(self):
        self.session = FakeSession()
        self.bot = Bot("1:TEST", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher()
        self.dp.message.middleware(Access())
        self.dp.callback_query.middleware(Access())
        self.dp.include_routers(*self._fresh_routers())
        self.n = 0
        self.user = User(id=UID, is_bot=False, first_name="T")
        self.chat = Chat(id=UID, type="private")

    @staticmethod
    def _fresh_routers():
        rs = [onboarding.router, menu.router, body.router, diet.router, onboarding.settings_router, food.router, polar.router,
              inputs.router, misc.router]
        for r in rs:
            r._parent_router = None
        return rs

    async def say(self, text, user_id=UID):
        self.n += 1
        self.session.calls.clear()
        msg = Message(message_id=self.n, date=datetime.now(), chat=self.chat,
                      from_user=User(id=user_id, is_bot=False, first_name="T"), text=text)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, message=msg))
        return self.out()

    async def tap(self, data, markup=None, photo=False):
        self.n += 1
        self.session.calls.clear()
        extra = {"photo": [PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)]} if photo else {"text": "card"}
        msg = Message(message_id=self.n, date=datetime.now(), chat=self.chat, from_user=self.user,
                      reply_markup=markup, **extra)
        cb = CallbackQuery(id=str(self.n), from_user=self.user, chat_instance="c", data=data, message=msg)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, callback_query=cb))
        return self.out()

    def out(self):
        return "\n---\n".join(
            (getattr(c, "text", None) or getattr(c, "caption", None)
             or getattr(getattr(c, "media", None), "caption", None) or type(c).__name__)
            for c in self.session.calls
            if isinstance(c, (SendMessage, SendPhoto, SendDocument, EditMessageText, EditMessageMedia)))


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr(config, "ALLOWED_USER_ID", UID)
    calls = {"n": 0}
    F = llm.FoodItem

    async def fake_parse(text, allow_question=True, products=()):
        calls["n"] += 1
        calls["products"] = list(products)
        if "ресторан" in text and allow_question:
            return llm.FoodParse(items=[], question="Что именно вы ели?")
        items = [F(name="борщ", grams=300, kcal=180, protein=8, fat=7, carbs=20),
                 F(name="хлеб", grams=60, kcal=140, protein=5, fat=1, carbs=28)]
        if "protein" in text.lower():
            items.append(F(name="hi tec protein", grams=30, kcal=999, protein=1, fat=0, carbs=0))
        return llm.FoodParse(question="", items=items)

    async def fake_correct(items, correction):
        return llm.FoodParse(question="", items=[F(name="борщ", grams=500, kcal=300, protein=13, fat=12, carbs=33)])

    async def fake_review(facts):
        calls["review"] = facts
        R = llm
        return R.DietReview(summary="Ест мало овощей.", fixes=[
            R.ReviewFix(problem="мало овощей", change="огурец 150 г к обеду", why="сытость", short="+ огурец к обеду")],
            dishes=[R.ReviewDish(title="Пюре с котлетами и огурцом", slot="любое", from_history=True, items=[
                R.ReviewItem(name="котлеты свиные", role="p", grams=150, lo=100, hi=200),
                R.ReviewItem(name="картофельное пюре", role="c", grams=180, lo=0, hi=250),
                R.ReviewItem(name="огурец", role="v", grams=150, lo=150, hi=150),
                R.ReviewItem(name="выдуманный продукт", role="c", grams=100, lo=0, hi=200)]),
                R.ReviewDish(title="Только пюре", slot="день", from_history=False, items=[
                    R.ReviewItem(name="картофельное пюре", role="c", grams=200, lo=100, hi=300)])])

    async def fake_product(text):
        return llm.Product(ok="ккал" in text, name="Hi Tec Protein", kcal=380, protein=75, fat=6, carbs=8)

    for name, fn in (("parse_food", fake_parse), ("correct_food", fake_correct), ("diet_review", fake_review),
                     ("parse_product", fake_product)):
        monkeypatch.setattr(llm, name, fn)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(db.init())
    hh = Harness()
    hh.loop, hh.llm_calls = loop, calls
    yield hh
    loop.run_until_complete(db.close())
    loop.close()


def run(h, coro):
    asyncio.set_event_loop(h.loop)
    h.loop.run_until_complete(coro())


async def onboard(h):
    assert "Сначала" in await h.say("привет")
    assert "пол" in await h.say("/start")
    await h.tap("onb:sex:m")
    await h.say("34")
    await h.say("180")
    await h.say("92")
    assert "меньше текущего" in await h.say("95")
    await h.say("80")
    await h.tap("onb:act:1.375")
    await h.tap("pace:2")
    return await h.say("7:30")


def test_logging_food(h):
    async def go():
        out = await onboard(h)
        # темп 2 кг/нед недостижим: бюджет упирается в пол 1500, бот это говорит
        assert "1 500 ккал/день" in out and "≈0.99 кг/нед" in out and "Без тренировок темп" in out
        p = await db.get_profile()
        assert (p["tdee"], p["budget"], p["protein_goal"]) == (2585, 1500, 160)
        assert await h.say("91.5", user_id=999) == ""          # чужой пользователь игнорируется

        out = await h.say("91.5")
        assert "Записал: <b>91.5 кг</b>" in out and "Тренд" in out

        out = await h.say("450 ккал завтрак")                   # число — без Claude
        assert "450 ккал" in out and "Осталось на сегодня: <b>1 050 ккал</b>" in out
        assert h.llm_calls["n"] == 0

        out = await h.say("тарелка борща и 2 куска хлеба")      # текст → Claude
        assert "320 ккал" in out and "борщ" in out and "Осталось на сегодня: <b>730 ккал</b>" in out
        assert (await db.last_meal())["title"] == "Борщ, хлеб"

        out = await h.say("Тарелка борща и 2 куска хлеба")      # повтор — из кеша, без подтверждений
        assert "без запроса к Claude" in out and h.llm_calls["n"] == 1
        assert "Осталось на сегодня: <b>410 ккал</b>" in out

        # удаление неудачного разбора убирает его и из кеша
        await h.tap(f"m:del:{(await db.last_meal())['id']}")
        await h.say("тарелка борща и 2 куска хлеба")
        assert h.llm_calls["n"] == 2

        # кнопка «Повторить»
        out = await h.tap("f:recent")
        assert "ввести граммы" in out and "🕘" not in out
    run(h, go)


def test_over_budget_edit_can_yesterday(h):
    async def go():
        await onboard(h)
        assert "🔴" not in await h.say("1100 ккал")
        assert "🔴" not in await h.say("150 ккал")              # 83% — промежуточных предупреждений нет
        out = await h.say("400 ккал")                           # 110%
        assert "🔴 Бюджет дня (1 500) превышен на 150 ккал" in out and "Перебор: <b>+150 ккал</b>" in out
        out = await h.say("10 ккал")
        assert "🔴" not in out and "Перебор" in out             # отдельное сообщение — один раз в день
        await h.say("/undo")
        out = await h.say("/undo")
        assert "Удалил" in out and "Осталось на сегодня: <b>250 ккал</b>" in out

        await h.say("борщ")
        meal = await db.last_meal()
        await h.tap(f"m:edit:{meal['id']}")
        out = await h.say("борща было 500 г")
        assert "300 ккал" in out and "исправлено" in out
        await h.tap(f"m:edit:{meal['id']}")
        assert "150 ккал" in await h.say("150")
        assert (await db.get_meal(meal["id"]))["kcal"] == pytest.approx(150, abs=1)

        n = len(await db.meals_on(db.today()))
        assert "Не влезает" in await h.say("/can шаурма")       # ничего не записывает до кнопки
        assert len(await db.meals_on(db.today())) == n
        await h.tap("can:add")
        assert len(await db.meals_on(db.today())) == n + 1

        assert "Что именно вы ели?" in await h.say("поел в ресторане")
        assert "320 ккал" in await h.say("борщ и хлеб")

        # запись за вчера: префиксом и кнопкой
        yday = db.days_ago(1)
        out = await h.say("вчера: 700 ккал ужин")
        assert "Записано на вчера" in out and "700 / 1 500" in out
        await h.say("200 ккал")
        mid = (await db.last_meal())["id"]
        out = await h.tap(f"m:yday:{mid}")
        assert "Итог за вчера: 900 / 1 500" in out
        assert [m["kcal"] for m in await db.meals_on(yday)] == [700, 200]
        assert "Вопрос" not in await h.say("вчера: поел в ресторане") and h.llm_calls["n"] >= 1
        await h.say("борщ")
        assert len(await db.meals_on(yday)) == 3               # уточнение тоже ушло на вчера

        assert "вес или калории" in await h.say("92")
        assert "Записал: <b>92.0 кг</b>" in await h.tap("amb:w:92")
        assert "Шаги не записываю" in await h.say("8000 шагов")
        out = await h.say("📊 Сегодня")
        assert "<b>Вес</b> 92.0 кг" in out and "Борщ" in out
        assert "Расход за день" in out and "2 585" in out and "шаги" not in out
        # вчера записано 700 + 200 + 320 = 1220 (≥60% бюджета) — неотмеченный день считается
        out = await h.say("📈 Прогресс")                            # старая кнопка → отчёт «Всё»
        assert "переехал" in out and "полных (без сегодня): 1" in out and "SendPhoto" not in out
        await h.tap(f"dc:-1:{yday}")                               # «не всё» — исключается
        assert "полных (без сегодня): 0" in await h.say("/chart")
        await h.tap(f"dc:1:{yday}")
        assert "полных (без сегодня): 1" in await h.say("/chart")
        out = await h.say("📊 Сегодня")
        assert "SendDocument" in await h.say("/export")
        assert "$0.0000" in await h.say("/cost")

        await h.tap(f"m:edit:{meal['id']}")                     # кнопка меню отменяет диалог
        assert "Отправьте вес" in await h.say("⚖️ Вес")
        out = await h.say("/chart")
        assert "Старт 92" in out and "после 5 дней" in out
    run(h, go)


def test_products(h):
    async def go():
        await onboard(h)
        assert "Не нашёл калорийности" in await h.say("продукт: hi tec protein")
        out = await h.say("продукт: Hi Tec Protein, на 100 г 380 ккал, белок 75")
        assert "Запомнил: hi tec protein — на 100 г: 380 ккал" in out
        out = await h.say("Hi Tec Protein 110 г")              # точное совпадение — без Claude
        assert "418 ккал" in out and "по этикетке" in out and h.llm_calls["n"] == 0
        assert (await db.day_totals(db.today()))["protein"] == 82.5
        out = await h.say("борщ, хлеб и hi tec protein 30 г")   # в составе — Claude, но цифры с этикетки
        assert h.llm_calls["products"][0]["name"] == "hi tec protein"
        assert "hi tec protein 30 г — 114 ккал" in out and "434 ккал" in out
        out = await h.tap("s:products")
        assert "ввести граммы" in out
        assert any("hi tec protein" in b.text for row in h.session.calls[-1].reply_markup.inline_keyboard for b in row)
        # выбор продукта кнопкой и ввод веса
        pid = (await db.products())[0]["id"]
        await h.tap(f"pr:use:{pid}")
        out = await h.say("30")
        assert "hi tec protein 30 г — 114 ккал" in out and "по этикетке" in out
        out = await h.tap(f"pr:use:{pid}")
        assert "Или выберите" in out                               # есть быстрая кнопка
        assert await db.recent_grams("hi tec protein") == [30.0, 110.0]
        assert "114 ккал" in await h.tap(f"pr:g:{pid}:30")
        assert "Отправьте число" not in await h.say("📊 Сегодня")   # кнопка меню сбрасывает ввод веса
        await h.tap("pr:manage")
        await h.tap(f"pr:del:{pid}")
        assert await db.products() == []
    run(h, go)


def test_reminders_and_weekly(h):
    async def go():
        await onboard(h)
        p = await db.get_profile()
        await db.delete_weight(db.today())
        now = db.now().replace(hour=8, minute=0)

        h.session.calls.clear()
        await reminders.weight_reminder(h.bot, p, now)
        assert "Время взвеситься" in h.out()
        first = (await db.day_state())["remind_msg"]
        h.session.calls.clear()
        await reminders.weight_reminder(h.bot, p, now + timedelta(minutes=10))
        assert h.out() == ""                                   # рано для повтора
        await reminders.weight_reminder(h.bot, p, now + timedelta(minutes=31))
        assert "всё ещё не внесён" in h.out()
        deleted = [c.message_id for c in h.session.calls if isinstance(c, DeleteMessage)]
        assert deleted == [first]                              # в чате остаётся одно напоминание
        h.session.calls.clear()
        await reminders.weight_reminder(h.bot, p, now + timedelta(hours=6))
        assert h.out() == ""                                   # после отсечки молчит
        await h.say("91.0")                                    # ввод веса убирает напоминание
        assert (await db.day_state())["remind_msg"] is None
        await reminders.weight_reminder(h.bot, p, now + timedelta(minutes=65))
        assert "взвес" not in h.out().lower().replace("взвешиванию", "")

        # три недели данных: ел 2100, вес падает на 0.1 кг в день → расход ≈ 2870
        for i in range(21, 0, -1):
            day = db.days_ago(i)
            await db.add_weight(92 - 0.1 * (21 - i), day)
            await db.add_meal([{"name": "еда", "grams": 0, "kcal": 2100, "protein": 120, "fat": 0, "carbs": 0}],
                              "manual", "еда", day)
        out = await h.say("/week")
        assert "Средние калории: 2 100" in out and "реальный расход" in out
        from bot.services import weekly
        rep = await weekly.build(await db.get_profile())
        tdee, bud = rep.proposal
        assert tdee in (2784, 2785) and bud == 1500
        await h.tap(f"bud:{tdee}:{bud}")
        assert (await db.get_profile())["tdee"] == tdee
        out = await h.say("📈 Прогресс")                        # один экран: график с подписью
        assert "Темп за 3 недели" in out and "цель ≈" in out and "по вашим данным" in out
        assert (await db.backup()).endswith(".db")
    run(h, go)


def test_menu_shake_uses_label(h):
    async def go():
        await onboard(h)
        from bot.handlers import diet
        await db.add_product("hi tec protein", 378, 79, 3.7, 7.1)
        sh = await diet.shake_item(await db.get_profile())
        assert (sh["grams"], round(sh["kcal"])) == (30, 113)
        assert "≈113 ккал" in await h.tap("s:diet")
    run(h, go)



def test_goal_service_export_shake_pick(h):
    async def go():
        await onboard(h)
        out = await h.tap("s:goal")
        assert "Без тренировок темп" in out
        await h.tap("g:target")
        out = await h.say("85")
        assert "Цель: 85 кг" in out and (await db.get_profile())["protein_goal"] == 170
        out = await h.tap("gp:0.5")                                # 2585 − 550 = 2035 > пола
        assert "Темп: <b>0.5 кг/нед</b>" in out and (await db.get_profile())["budget"] == 2035
        assert "Сервис" in await h.tap("s:service")

        # выбор протеина для меню
        await db.add_product("hi tec protein", 378, 79, 3.7, 7.1)
        await db.add_product("kfd pure wpc", 391, 79, 6.5, 4)
        assert "Протеин: <b>hi tec protein</b>" in await h.tap("s:diet")    # без выбора — первый похожий
        wpc = next(x["id"] for x in await db.products() if x["name"] == "kfd pure wpc")
        out = await h.tap(f"d:spn:{wpc}")
        assert "Протеин: <b>kfd pure wpc</b>" in out
        out = await h.tap("d:spn:x")
        assert "по оценке, без этикетки" in out

        # выгрузка для анализа: файл с заданием и данными, без запроса к Claude
        await h.say("91.0")
        await h.say("300 ккал творог")
        n = h.llm_calls["n"]
        await h.tap("ex:7")
        doc = next(c for c in h.session.calls if type(c).__name__ == "SendDocument")
        text = doc.document.data.decode()
        assert "## Задание" in text and "## По дням" in text and "творог" in text
        assert "| " + db.today() + " | 91.0 |" in text and "kfd pure wpc" in text
        assert h.llm_calls["n"] == n
    run(h, go)



def test_report_periods(h):
    async def go():
        await onboard(h)
        for i, kcal in ((2, 1400), (1, 600)):                      # позавчера полный, вчера неполный
            await db.add_meal([{"name": "еда", "grams": 0, "kcal": kcal, "protein": 100, "fat": 40, "carbs": 120}],
                              "manual", "еда", db.days_ago(i))
        await db.add_weight(92.0, db.days_ago(2))
        await db.add_weight(91.5)
        await h.say("500 ккал")
        assert "Белок" in await h.say("📊 Отчёт")
        assert "стала «📊 Отчёт»" in await h.say("📊 Сегодня")     # старая кнопка работает и обновляет меню

        out = await h.tap("r:3")
        assert "<b>Отчёт: 3 дн.</b>" in out
        assert "1 400" in out and "✅" in out and "⚠️" in out and "⏳ сегодня" in out
        assert "Записано дней: 3 из 3, полных (без сегодня): 1" in out
        assert "1 400 / 1 500" in out and "1 185 ккал ≈ −0.2 кг" in out
        assert "мин 128 · цель 160" in out and "Белок ≥ мин" in out
        assert "Вес по тренду:</b> 92.0 →" in out
        from bot import keyboards as kb
        names = lambda: [type(c).__name__ for c in h.session.calls]
        # из текстового отчёта на «Всё»: текст нельзя превратить в фото — удалить и прислать фото
        out = await h.tap("r:0", markup=kb.report_menu("r:3"))
        assert "<b>Всё наблюдение</b>" in out and names()[1:] == ["DeleteMessage", "SendPhoto"]
        # фото → фото: правится на месте
        out = await h.tap("r:7", markup=kb.report_menu("r:0"), photo=True)
        assert "неделя</b>" in out and names()[1:] == ["EditMessageMedia"]
        # фото → сегодня (текст): пересоздаётся
        out = await h.tap("r:1", markup=kb.report_menu("r:7"), photo=True)
        assert "осталось" in out and names()[1:] == ["DeleteMessage", "SendMessage"]
        # текст → текст: правится на месте
        await h.tap("r:3", markup=kb.report_menu("r:1"))
        assert names()[1:] == ["EditMessageText"]
        # кнопка «Прогресс» под весом — новое сообщение, карточка веса остаётся
        await h.tap("r:0", markup=kb.weight_menu)
        assert names()[1:] == ["SendPhoto"]
    run(h, go)



def test_panel_and_bottom_buttons(h):
    async def go():
        await onboard(h)
        from aiogram.methods import EditMessageText, PinChatMessage
        p = await db.get_profile()
        assert p["panel_msg"] and "сегодня не взвешен" not in p["panel_text"]   # вес из онбординга
        assert "осталось <b>1 500</b>" in p["panel_text"]

        await h.say("450 ккал")                                   # запись еды правит панель на месте
        edits = [c for c in h.session.calls if isinstance(c, EditMessageText) and c.message_id == p["panel_msg"]]
        assert edits and "осталось <b>1 050</b>" in edits[0].text
        assert not [c for c in h.session.calls if isinstance(c, PinChatMessage)]   # закреплено один раз

        h.session.calls.clear()
        await reminders.tick(h.bot)                                # без изменений — не трогаем Telegram
        assert not [c for c in h.session.calls if isinstance(c, EditMessageText)]

        # кнопки постоянной панели
        out = await h.say("⚡ Быстрая запись")
        assert "Быстрая запись" in out and "ввести граммы" in out
        assert "объединены" in await h.say("🕘 Повторить")         # старые кнопки работают
        assert "объединены" in await h.say("📦 Мои продукты")
        assert "влезет ли в остаток (1 050 ккал)" in await h.say("❓ Можно ли?")
        assert "Влезает: после этого останется 730" in await h.say("шаурма")  # ответ на «Можно ли?»
        assert "Панель внизу обновлена" in await h.say("🍽 Еда")   # старая кнопка работает
    run(h, go)



def test_review_regressions(h):
    """Ошибки из финального ревью."""
    async def go():
        await onboard(h)
        from bot import keyboards as kb

        # 1. график с весом, но без еды не падает
        assert "Всё наблюдение" in await h.say("/chart")
        assert any(type(c).__name__ == "SendPhoto" for c in h.session.calls)

        # 2. кнопка панели во время вопроса отменяет его, а не сохраняется как ответ
        await h.tap("d:prefs")
        assert "влезет ли в остаток" in await h.say("❓ Можно ли?")
        assert (await db.get_profile())["diet_prefs"] == ""
        await h.say("/cancel")
        await h.say("450 ккал")
        mid = (await db.last_meal())["id"]
        await h.tap(f"m:edit:{mid}")
        await h.say("⚡ Быстрая запись")
        assert (await db.get_meal(mid))["kcal"] == 450

        # 3. спецсимволы в названии не ломают HTML отчёта
        out = await h.say("M&M's <3 250 ккал")
        out = await h.say("📊 Отчёт")
        assert "M&amp;M&#x27;s &lt;3" in out or "M&amp;M's &lt;3" in out

        # 4. после «Не всё» вечером снова можно отметить «Всё записал»
        await h.tap(f"dc:-1:{db.today()}")
        from bot.handlers import common
        import bot.db as dbm
        real_now = dbm.now
        dbm.now = lambda: real_now().replace(hour=20)
        try:
            markup = await common.today_markup()
        finally:
            dbm.now = real_now
        assert any(b.callback_data == f"dc:1:{db.today()}" for row in markup.inline_keyboard for b in row)

        # 6. кнопки продуктов не сдвигаются при добавлении нового
        tid = await db.add_product("творог", 98, 17, 2, 3)
        await db.add_product("банан", 89, 1, 0, 23)                 # встаёт раньше по алфавиту
        out = await h.tap(f"pr:use:{tid}")
        assert "«творог»" in out
        # 8. «200 гр» принимается
        out = await h.say("200 гр")
        assert "творог 200 г — 196 ккал" in out

        # 10. /undo удаляет сегодняшнюю запись, даже если последней была запись за вчера
        await h.say("вчера: 300 ккал")
        out = await h.say("/undo")
        assert "Удалил" in out and len(await db.meals_on(db.days_ago(1))) == 1
    run(h, go)


def test_yesterday_reminder_removed(h):
    async def go():
        await onboard(h)
        from aiogram.methods import DeleteMessage
        await db.set_day_state(db.days_ago(1), remind_msg=555)
        await db.delete_weight(db.today())
        p = await db.get_profile()
        h.session.calls.clear()
        await reminders.weight_reminder(h.bot, p, db.now().replace(hour=8, minute=0))
        assert 555 in [c.message_id for c in h.session.calls if isinstance(c, DeleteMessage)]
        assert (await db.day_state(db.days_ago(1)))["remind_msg"] is None
    run(h, go)


def test_all_chart_after_profile_redo(h):
    async def go():
        await onboard(h)
        from bot.services import report_view
        for i in range(20, 0, -1):
            await db.add_weight(92 - 0.2 * (20 - i), db.days_ago(i))
        await db.update_profile(start_weight=88.0)                 # как после «Профиль заново»
        text, png = await report_view.build(await db.get_profile(), 0)
        assert "Старт 92" in text and png
    run(h, go)



def test_yesterday_asked_with_weight(h):
    async def go():
        await onboard(h)
        yday = db.days_ago(1)
        await db.delete_weight(db.today())
        await db.add_meal([{"name": "еда", "grams": 100, "kcal": 1300, "protein": 90, "fat": 0, "carbs": 0}],
                          "manual", "еда", yday)
        out = await h.say("91.5")
        assert "Вчера</b>: 1 300 из 1 500 ккал, белок 90 г — ниже минимума 128" in out and "Всё записали?" in out
        out = await h.tap(f"dc:-1:{yday}")
        assert "вчера: пельмени 300 г" in out and (await db.day_state(yday))["complete"] == -1
        assert "Вчера</b>" not in await h.say("91.4")                 # уже отмечено — не спрашиваем
        await h.tap(f"dc:1:{yday}")
        assert (await db.day_state(yday))["complete"] == 1
    run(h, go)


def test_workouts(h):
    async def go():
        await onboard(h)                                            # расход 2585, бюджет 1500
        await h.say("1000 ккал")
        out = await h.say("трен 700")
        assert "в расход <b>595 ккал</b>" in out and "2 585 + 595 = <b>3 180 ккал</b>" in out
        assert "дефицит 2 180 ккал" in out and h.llm_calls["n"] == 0
        assert "тренировка 595: +298 к еде, +297 к дефициту" in (await db.get_profile())["panel_text"]
        # без цифры — спросить калории, а не отправлять в разбор еды
        assert "Сколько калорий показали часы" in await h.say("зал")
        out = await h.say("650")
        assert "в расход <b>552 ккал</b>" in out and h.llm_calls["n"] == 0
        out = await h.tap("r:1")
        assert "Тренировки" in out and "+1 147" in out and "−1 000" in out and "2 732" in out
        # бюджет еды не меняется
        assert (await db.get_profile())["budget"] == 1500
        wid = (await db.workouts_on(db.today()))[-1]["id"]
        assert "Тренировка удалена" in await h.tap(f"wo:del:{wid}")
        assert await db.workout_kcal(db.today()) == 595
        assert "Уже удалено" not in await h.tap(f"wo:del:{wid}")   # повторное нажатие не падает

        # за вчера + отчёты
        await db.add_meal([{"name": "еда", "grams": 0, "kcal": 1600, "protein": 100, "fat": 40, "carbs": 120}],
                          "manual", "еда", db.days_ago(1))
        assert "за вчера" in await h.say("вчера: бег 30 мин 400")
        assert await db.workout_kcal(db.days_ago(1)) == 340
        out = await h.tap("r:3")
        assert "1 600 100↓  1 325" in out                       # 2585 + 340 − 1600
        assert "Дефицит за полные дни:</b> 1 325 ккал" in out
        out = await h.tap("r:7")
        assert "Тренировки: 2 дн." in out
        out = await h.tap("ex:7")
        assert any(type(c).__name__ == "SendDocument" for c in h.session.calls)
        from bot.services import report
        md = await report.build(await db.get_profile(), 7)
        assert "| Тренировка |" in md and f"| {db.days_ago(1)} |" in md and "| 340 | +1325 |" in md
    run(h, go)


def test_real_tdee_subtracts_workouts(h):
    async def go():
        from bot.services import weekly
        await onboard(h)
        for i in range(14, 0, -1):
            await db.add_meal([{"name": "еда", "grams": 0, "kcal": 2000, "protein": 100, "fat": 40, "carbs": 120}],
                              "manual", "еда", db.days_ago(i))
            await db.add_weight(92.0, db.days_ago(i))
            await db.add_workout("зал", 500, 500, db.days_ago(i))
        calc, why = await weekly.real_tdee(await db.get_profile())
        assert calc == 1500 and "без тренировок" in why           # вес стоит: 2000 съедено = 1500 + 500
    run(h, go)


def test_polar_import(h, monkeypatch):
    from bot.services import polar as ps
    monkeypatch.setattr(ps, "configured", lambda: True)
    monkeypatch.setattr(polar, "_last_sync", None)
    feed = {"items": [], "auth_fail": False}

    async def fake_connect(code):
        assert code == "abc123"
        return "TOKEN", 42

    async def fake_exercises(token):
        if feed["auth_fail"]:
            raise ps.PolarAuthError("x")
        return [ps.parse(e) for e in feed["items"]]

    monkeypatch.setattr(ps, "connect", fake_connect)
    monkeypatch.setattr(ps, "exercises", fake_exercises)

    async def go():
        await onboard(h)
        out = await h.say("/polar")
        assert "подключить Polar" in out and "localhost" in out
        nonce = (await h.dp.fsm.get_context(h.bot, UID, UID).get_data())["polar_state"]
        assert "старой ссылки" in await h.say("http://localhost:5000/oauth2_callback?code=abc123&state=zzz")
        out = await h.say(f"http://localhost:5000/oauth2_callback?code=abc123&state={nonce}")
        assert "Polar Flow подключён" in out
        assert (await db.get_profile())["polar_token"] == "TOKEN"

        # ручная запись, затем та же тренировка приходит из Polar
        await h.say("трен 650")
        feed["items"] = [{"id": "E1", "start_time": f"{db.today()}T18:00:00", "duration": "PT58M12S",
                          "calories": 640, "sport": "STRENGTH_TRAINING", "heart_rate": {"average": 128, "maximum": 165}}]
        p = await db.get_profile()
        assert await polar.sync(h.bot, p) is None                     # 15 минут с прошлого опроса не прошло
        h.session.calls.clear()
        assert await polar.sync(h.bot, p, force=True) == 1
        out = h.out()
        assert "Из Polar: силовая 58 мин" in out and "пульс 128/165" in out and "в расход <b>544 ккал</b>" in out
        assert "Это та же тренировка?" in out
        ws = await db.workouts_on(db.today())
        manual, imported = ws[0]["id"], ws[1]["id"]
        await h.tap(f"pl:dup:{manual}:{imported}")
        assert await db.workout_kcal(db.today()) == 544

        # повторный опрос не дублирует; удалённая импортированная не возвращается
        assert await polar.sync(h.bot, p, force=True) == 0
        await h.tap(f"wo:del:{imported}")
        assert await polar.sync(h.bot, p, force=True) == 0
        assert await db.workout_kcal(db.today()) == 0

        # тренировки до начала наблюдения не импортируются
        feed["items"].append({"id": "OLD", "start_time": "2020-01-01T10:00:00", "duration": "PT30M", "calories": 300})
        assert await polar.sync(h.bot, p, force=True) == 0

        from bot.services import report
        assert "## Тренировки" not in await report.build(p, 7)    # удалённые в экспорт не попадают
        await h.say("бег 30 мин 400")
        assert "вручную" in await report.build(p, 7)

        # доступ отозван — токен сброшен, пользователь предупреждён
        feed["auth_fail"] = True
        h.session.calls.clear()
        assert await polar.sync(h.bot, p, force=True) is None
        assert "отозвал доступ" in h.out() and (await db.get_profile())["polar_token"] is None
    run(h, go)


def test_polar_extras(h, monkeypatch):
    from bot.services import polar as ps, weekly
    monkeypatch.setattr(ps, "configured", lambda: True)
    feed = []

    async def fake_exercises(token):
        return [ps.parse(e) for e in feed]

    monkeypatch.setattr(ps, "exercises", fake_exercises)

    async def go():
        await onboard(h)                                         # расход 2585, бюджет 1500, темп 2
        # неверный ввод в режиме кода — режим снимается, следующее сообщение работает как обычно
        await h.say("/polar")
        assert "подключение отменил" in await h.say("трен 650")
        assert "в расход <b>552 ккал</b>" in await h.say("трен 650")
        await db.delete_workout((await db.workouts_on(db.today()))[0]["id"])

        await db.add_weight(92.0)
        await db.update_profile(polar_token="T", polar_user=1)
        await db.mark_flag("polar_weight_ref", "92.0")
        feed.append({"id": "E9", "start_time": f"{db.today()}T07:00:00", "duration": "PT45M", "calories": 500,
                     "sport": "OTHER", "detailed_sport_info": "AQUA_FITNESS", "heart_rate": {"average": 120, "maximum": 150}})
        p = await db.get_profile()
        assert await polar.sync(h.bot, p, force=True) == 1
        # ручная запись поверх импортированной — предупреждение о двойном счёте
        out = await h.say("зал 480")
        assert "уже пришла тренировка из Polar: aqua fitness 45 мин" in out and "посчитаются дважды" in out
        manual = (await db.workouts_on(db.today()))[-1]["id"]
        await h.tap(f"wo:del:{manual}")
        assert await db.workout_kcal(db.today()) == 425

        out = await h.tap("r:1")
        assert "aqua fitness 45 мин · ♥ 120 — <b>425</b>" in out

        # недельная сводка: тренировки против нужного для темпа
        lines = await weekly.workouts_summary(p, db.days_ago(6), db.today())
        assert "Тренировки: 1, 425 ккал в расход, 0 ч 45 мин" in lines[0]
        assert "Для темпа 2 кг/нед нужно" in lines[1] and "сделано 425" in lines[1]

        # похудел на 3 кг с подключения — одно напоминание обновить вес в Polar
        for v in (90.0, 89.5, 89.0, 88.5, 88.0, 87.5, 87.0, 86.0, 85.5, 85.0, 84.5):
            await db.add_weight(v)
        out = await h.say("84.5")
        assert "Обновите вес в Polar Flow" in out
        assert "Обновите вес в Polar Flow" not in await h.say("84.4")
    run(h, go)


async def send_media(h, **media):
    from aiogram.types import Voice
    h.n += 1
    h.session.calls.clear()
    if media.pop("voice", False):
        media["voice"] = Voice(file_id="v", file_unique_id="v", duration=5)
    if media.pop("photo", False):
        media["photo"] = [PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)]
    msg = Message(message_id=h.n, date=datetime.now(), chat=h.chat, from_user=h.user, **media)
    await h.dp.feed_update(h.bot, Update(update_id=h.n, message=msg))
    return h.out()


def test_voice(h, monkeypatch):
    from bot.handlers import inputs
    from bot.services import voice
    V = llm.VoiceCommand
    said = {}

    async def fake_transcribe(data):
        return said["text"]

    async def fake_route(text, known=()):
        return llm.VoiceParse(commands=said["cmds"])

    async def fake_dl(message):
        return b"x"

    monkeypatch.setattr(voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(llm, "route_voice", fake_route)
    monkeypatch.setattr(inputs, "_download", fake_dl)

    async def go():
        await onboard(h)
        said.update(text="вес 91 и 4, съел тарелку борща", cmds=[
            V(action="weight", text="", value=91.4, yesterday=False),
            V(action="food", text="тарелка борща и 2 куска хлеба", value=0, yesterday=False)])
        out = await send_media(h, voice=True)
        assert "🎙 <i>вес 91 и 4" in out and "91.4 кг" in out and "борщ" in out
        assert (await db.last_weight())["weight"] == 91.4

        said.update(text="тренировка", cmds=[V(action="workout", text="бег", value=0, yesterday=False)])
        assert "Сколько калорий" in await send_media(h, voice=True)
        said["text"] = "650"                                     # ответ голосом на вопрос бота
        out = await send_media(h, voice=True)
        assert "650" in out and "Тренировка" in out or "тренировк" in out

        said.update(text="бла", cmds=[V(action="unknown", text="Я записываю еду и тренировки.", value=0,
                                         yesterday=False)])
        assert "Я записываю еду" in await send_media(h, voice=True)
    run(h, go)


def test_voice_command_text():
    from bot.handlers.inputs import command_text
    V = llm.VoiceCommand
    c = lambda a, t="", v=0, y=False: command_text(V(action=a, text=t, value=v, yesterday=y))
    assert c("weight", v=91.4) == "вес 91.4" and c("weight", v=914) is None
    assert c("workout", "бег 5 км 400 ккал", 400) == "тренировка бег 5 км 400 ккал"
    assert c("workout", "зал") == "тренировка"
    assert c("workout", "бег 30 минут", 380) == "тренировка бег 30 минут 380 ккал"
    assert c("workout", "бег 380 ккал 1.5 час", 380) == "тренировка бег 1.5 час 380 ккал"
    assert c("food", "пельмени 400 г", y=True) == "вчера: пельмени 400 г"
    assert c("kcal", "обед", 700) == "700 ккал обед" 
    assert c("can_eat", "шаурма") == "/can шаурма" and c("undo") == "/undo" and c("food") is None
    from bot.services.parser import classify
    assert classify(c("workout", "бег 5 км 400 ккал", 400)).kind == "workout"
    assert classify(c("workout", "бег 5 км 400 ккал", 400)).value == 400


def test_parse_off():
    from bot.services.barcode import parse_off
    p = parse_off({"product_name": "Twaróg półtłusty", "brands": "Piątnica,Other", "product_quantity": "250",
                   "nutriments": {"energy-kcal_100g": 121, "proteins_100g": 18, "fat_100g": 4, "carbohydrates_100g": 3.5}})
    assert p == {"name": "piątnica twaróg półtłusty", "kcal": 121, "protein": 18, "fat": 4, "carbs": 3.5, "pack": 250}
    assert parse_off({"product_name": "x", "nutriments": {"energy_100g": 418.4}})["kcal"] == 100
    assert parse_off({"product_name": "x", "nutriments": {}})["kcal"] is None


def test_barcode_decode():
    zxingcpp = pytest.importorskip("zxingcpp")
    from io import BytesIO
    from bot.services import barcode
    img = zxingcpp.create_barcode("5901234123457", zxingcpp.BarcodeFormat.EAN13).to_image(scale=4)
    from PIL import Image
    buf = BytesIO()
    Image.fromarray(__import__("numpy").asarray(img)).save(buf, "PNG") if not hasattr(img, "save") else img.save(buf, "PNG")
    assert barcode._decode(buf.getvalue()) == "5901234123457"


def test_scan(h, monkeypatch):
    from bot.handlers import inputs
    from bot.services import barcode
    state = {"code": "5900000000001", "off": None, "n_lookup": 0}

    async def fake_decode(data):
        return state["code"]

    async def fake_lookup(code):
        state["n_lookup"] += 1
        return state["off"]

    async def fake_label(image, hint=""):
        state["hint"] = hint
        return llm.Label(ok=True, name="", kcal=120, protein=18, fat=4, carbs=3, pack=0)

    async def fake_dl(message):
        return b"x"

    monkeypatch.setattr(barcode, "decode", fake_decode)
    monkeypatch.setattr(barcode, "lookup", fake_lookup)
    monkeypatch.setattr(llm, "parse_label", fake_label)
    monkeypatch.setattr(inputs, "_download", fake_dl)

    async def go():
        await onboard(h)
        state["off"] = {"name": "piątnica twaróg", "kcal": 121, "protein": 18, "fat": 4, "carbs": 3.5, "pack": 250}
        out = await send_media(h, photo=True)
        assert "Нашёл по штрихкоду" in out and "piątnica twaróg — на 100 г: 121 ккал" in out
        assert "Вся упаковка · 250 г" in str(h.session.calls[-1].reply_markup)
        out = await h.say("200")
        assert "242 ккал" in out
        out = await send_media(h, photo=True)                    # второй раз — из своих продуктов, без сети
        assert "Уже есть" in out and state["n_lookup"] == 1
        await h.say("/cancel")

        # нет в базе → фото таблицы → запомнен со штрихкодом, имя из подсказки
        state.update(code="4000000000002", off={"name": "skyr", "kcal": None, "protein": 0, "fat": 0, "carbs": 0,
                                                "pack": None})
        assert "без калорий" in await send_media(h, photo=True)
        out = await send_media(h, photo=True)
        assert state["hint"] == "skyr" and "Прочитал этикетку" in out
        assert (await db.product_by_barcode("4000000000002"))["kcal"] == 120

        # ни штрихкода, ни таблицы
        state["code"] = None

        async def no_label(image, hint=""):
            return llm.Label(ok=False, name="", kcal=0, protein=0, fat=0, carbs=0, pack=0)
        monkeypatch.setattr(llm, "parse_label", no_label)
        await h.say("/cancel")
        assert "ни штрихкода, ни таблицы" in await send_media(h, photo=True)
    run(h, go)


def test_steps(h):
    async def go():
        await onboard(h)
        assert "Шаг 1/3" in await h.tap("st:go")
        assert "Шаг 2/3" in await h.say("гречка")
        assert "Шаг 3/3" in await h.tap("st:a:2")
        out = await h.tap("st:c:0")
        assert "гречка, большая порция, варёное или на пару" in out
        await h.tap("st:more")
        await h.say("котлета")
        await h.say("2 штуки")
        out = await h.tap("st:c:5")
        assert "• котлета, 2 штуки" in out and "Пропустить" not in out.split("котлета")[1]
        out = await h.tap("st:save")
        assert "320 ккал" in out                                  # фиктивный разбор в фикстуре
        assert "устарел" in await h.tap("st:save")
    run(h, go)


def test_workout_bonus_formula():
    from bot.services.nutrition import workout_bonus
    # темп 2 кг/нед, бюджет на полу: до темпа не хватает 1115 → к еде только половина
    assert workout_bonus(2585, 1500, 2, 552) == 276
    # реальный случай: расход 2296, темп 1, пол 1500 → не хватает 304, остальное к еде
    assert workout_bonus(2296, 1500, 1, 992) == 688
    # бюджет не на полу: дефицит темпа уже в бюджете → к еде вся тренировка
    assert workout_bonus(2800, 1700, 1, 400) == 400
    assert workout_bonus(2296, 1500, 1, 0) == 0


def test_workout_raises_food_budget(h):
    async def go():
        await onboard(h)                                          # расход 2585, бюджет 1500, темп 2
        await h.say("1000 ккал")
        out = await h.say("трен 650")                             # 650 → 552 в расход, к еде +276
        assert "1 500 + 276 за тренировку = 1 776" in out and "осталось <b>776</b>" in out
        out = await h.say("700 ккал")                             # 1700 < 1776 — не перебор
        assert "Осталось на сегодня: <b>76 ккал</b> (в бюджете +276 за тренировку)" in out and "🔴" not in out
        out = await h.say("/today")
        assert "съедено 1 700 из 1 776" in out and "к еде +276, остальные 276 — в дефицит" in out
        out = await h.say("200 ккал")
        assert "🔴 Бюджет дня (1 776 с тренировкой) превышен на 124 ккал" in out and "Дефицит дня всё равно есть" in out
    run(h, go)


def test_protein_tips(h):
    from bot.services import budget
    async def go():
        await onboard(h)
        await db.add_product("скир", 63, 12, 0, 3.8, pack=150)
        await db.add_product("шоколад", 540, 6, 31, 57)            # белка мало — не предлагается
        F = lambda n, g, k, p: {"name": n, "grams": g, "kcal": k, "protein": p, "fat": 0, "carbs": 0}
        for g in (180, 200, 220):                                   # творог обычно ~200 г
            await db.add_meal([F("творог", g, g * 1.2, g * 0.17)], "llm", "творог", db.days_ago(2))
        await db.add_meal([F("макароны", 250, 400, 12)], "llm", "макароны", db.days_ago(2))
        tip = await budget.protein_tips(40, 800)
        assert tip.startswith("💡 добрать 40 г белка — на выбор: ")
        assert "скир 225 г (142 ккал, +27 г)" in tip and "творог 235 г (282 ккал)" in tip
        assert "шоколад" not in tip and "макароны" not in tip
        await h.say("500 ккал")                                     # белка 0 → подсказка в отчёте
        assert "💡 добрать" in await h.say("/today")
    run(h, go)


def test_reports_use_budget_history(h):
    from bot.services import budget
    async def go():
        await onboard(h)                                            # план от сегодня: 2585 / 1500
        await db._exec("UPDATE budget_history SET date = ?", db.days_ago(10))
        await db._exec("INSERT INTO budget_history VALUES (?, ?, ?)", db.days_ago(3), 2400, 1500)
        pl = await budget.plans(await db.get_profile(), db.days_ago(5), db.today())
        assert pl[db.days_ago(5)] == (2585, 1500) and pl[db.days_ago(2)] == (2400, 1500)
        await db.add_meal([{"name": "еда", "grams": 0, "kcal": 1400, "protein": 0, "fat": 0, "carbs": 0}],
                          "manual", "еда", db.days_ago(2))
        await db.set_day_state(db.days_ago(2), complete=1)
        out = await h.tap("r:3")
        assert "1 000 ккал ≈" in out                                # 2400 − 1400, а не 2585 − 1400
    run(h, go)


def test_base_foods_match():
    from bot.services.base_foods import match
    assert match("творог 200 г")["kcal"] == 242 and match("200 г творога")["grams"] == 200
    assert match("2 яйца")["grams"] == 110 and match("яйца 3 шт")["grams"] == 165 and match("яйцо")["grams"] == 55
    assert match("хлеб 2 куска")["grams"] == 60 and match("огурец")["grams"] == 120
    assert match("гречка 250")["grams"] == 250 and match("Хлеб ржаной 30 г")["name"] == "хлеб ржаной"
    assert match("сырники 200 г") is None and match("борщ") is None and match("творог") is None
    assert match("творог с бананом") is None                       # сложное — к Claude


def test_base_foods_flow(h):
    async def go():
        await onboard(h)
        out = await h.say("2 яйца")
        assert "по справочнику, без запроса к Claude" in out and "яйцо 110 г — 173 ккал" in out
        assert h.llm_calls["n"] == 0
        await h.tap("s:products")
        assert "🥚 Базовые продукты" in str(h.session.calls[-1].reply_markup)
        out = await h.tap("bf:1")                                    # творог 5%
        assert "творог 5%" in out and "на 100 г: 121 ккал" in out
        assert "242 ккал" in await h.say("200")
        await h.tap("bf:0")
        assert "яйцо 165 г" in await h.say("3 шт")
        assert "яйцо 55 г" in await h.tap("bf:g:0:55")
        await db.add_product("творог", 98, 17, 2, 3)                  # свой продукт важнее справочника
        assert "по этикетке" in await h.say("творог 200 г")
    run(h, go)


def test_week_runs_cumulative():
    from datetime import date, timedelta
    from bot.services import charts
    mon = date(2026, 11, 16)
    days = [mon + timedelta(i) for i in range(7)]
    d = charts.ReportData(title="", subtitle="", days=days, weights={}, expected=[],
                          kcal={days[0]: (1500, True), days[1]: (800, False), days[2]: (1400, True),
                                days[3]: (1200, True)},
                          budget=1500, tdee=2300, target=82, today=days[3], target_deficit=1100,
                          workouts={days[2]: 500})
    r = charts.week_runs(d)[-1]
    assert r["daily"] == {days[0]: 800, days[2]: 1400}
    assert r["skipped"] == [days[1]]          # неполный день не идёт ни в дефицит, ни в цель
    assert (r["cum"], r["tgt"], r["days"], r["left"]) == (2200, 2200, 2, 4)
    assert r["proj"][-1][1] == 2200 + 4 * 1100


def test_what_to_eat(h):
    from bot.services import suggest

    async def go():
        await onboard(h)
        await h.say("2 яйца")
        out = await h.say("🍽 Что съесть?")
        assert "Что съесть сейчас" in out and "Не хватает за день: белок" in out and "<b>1. " in out
        assert h.llm_calls["n"] == 0
        mk = h.session.calls[-1].reply_markup
        picks = [b.callback_data for row in mk.inline_keyboard for b in row if b.callback_data.startswith("ws:")
                 and not b.callback_data.startswith("ws:more")]
        assert len(picks) == 3 and all(len(p.encode()) <= 64 for p in picks)
        title, items = await suggest.decode(picks[0])
        out = await h.tap(picks[0])
        assert title in out and "по справочнику" in out
        assert len(await db.meals_on(db.today())) == 2
        assert "Другие" in str(mk)
        assert "<b>4. " not in await h.tap("ws:more:3")
        # белок и жиры добраны — есть не предлагаем, остаток идёт в дефицит
        await db.add_meal([{"name": "x", "grams": 0, "kcal": 600, "protein": 140, "fat": 60, "carbs": 0}], source="text")
        out = await h.say("🍽 Что съесть?")
        assert "уже добраны" in out and "уйдут в дефицит" in out and "<b>1. " not in out

    run(h, go)


def test_suggest_portions():
    from bot.services import suggest
    prof = {"target_weight": 82, "protein_goal": 164}
    # вечер, запас большой: не съедаем остаток, а добираем белок до минимума наименьшими калориями
    nd = suggest.need(prof, {"kcal": 900, "protein": 95, "fat": 30}, 2188, 19)
    assert nd.meals_left == 1 and nd.protein == 131 - 95
    top = suggest.options(nd, 19)[:3]
    for _, dish, items in top:
        assert sum(x["protein"] for x in items) >= 30 and sum(x["kcal"] for x in items) <= 400
        assert not any(x["name"] in ("гречка варёная", "рис варёный", "картофель варёный") for x in items)
    # утром белок дня делится на 3 приёма
    assert suggest.need(prof, {"kcal": 0, "protein": 0, "fat": 0}, 1500, 8).protein == 131 / 3
    # названия собираются из того, что вошло
    d = suggest.DISHES[7]
    assert suggest.title(d, [{"name": "творог обезжиренный"}, {"name": "сметана 15%"}]) == "Обезжиренный творог со сметаной"


async def _seed_meal(day, hhmm, items):
    mid = await db.add_meal(items, "llm", "x", day)
    await db._exec("UPDATE meals SET ts = ? WHERE id = ?", f"{day}T{hhmm}:00+01:00", mid)
    return mid


def _it(name, grams, kcal, p, f=0, c=0):
    return {"name": name, "grams": grams, "kcal": kcal, "protein": p, "fat": f, "carbs": c}


async def _seed_week(days=6):
    for i in range(days, 0, -1):
        d = db.days_ago(i)
        await _seed_meal(d, "09:30", [_it("hi tec protein", 100, 378, 79, 4, 7)])
        await _seed_meal(d, "13:20", [_it("картофельное пюре", 270, 300 if i % 2 else 470, 5, 10, 45)])
        await _seed_meal(d, "13:22", [_it("котлеты свиные", 150, 420, 23, 33, 6)])
        await _seed_meal(d, "19:00", [_it("куриная грудка", 150, 250, 40, 8, 0)])


def test_diet_profile(h):
    from bot.services import diet_profile

    async def go():
        await onboard(h)
        await _seed_week()
        data = await diet_profile.collect(await db.get_profile())
        day = [s for s in data["sittings"] if s["date"] == db.days_ago(1)]
        assert len(day) == 3                                  # пюре и котлеты за 2 минуты — один обед
        assert {i["name"] for i in day[1]["items"]} == {"картофельное пюре", "котлеты свиные"}
        foods = {f["name"]: f for f in data["foods"]}
        assert foods["hi tec protein"]["powder"] and foods["картофельное пюре"]["grams"] == 270
        assert data["meals"][0]["items"] == {"картофельное пюре": 270, "котлеты свиные": 150}
        st = data["stats"]
        assert st["full_days"] == 6 and st["sittings_per_day"] == 3 and st["veg_g"] == 0
        flags = " ".join(data["flags"])
        assert "порошок даёт" in flags and "овощей в среднем 0 г" in flags
        assert "«картофельное пюре» записан с разной калорийностью" in flags
    run(h, go)


def test_diet_review_and_eat(h):
    from bot.services import diet_review, suggest

    async def go():
        await onboard(h)
        assert "нужно ещё 5 полн. дн." in await h.tap("s:review")       # данных мало — Claude не зовём
        assert "review" not in h.llm_calls
        await _seed_week()
        out = await h.tap("s:review")
        assert "Разбор рациона" in out and "огурец 150 г к обеду" in out and "⭐ Пюре с котлетами и огурцом" in out
        facts = h.llm_calls["review"]
        assert "ПЕРЕКОСЫ" in facts and "котлеты свиные: 6 раз" in facts and "картофельное пюре 270 г + котлеты свиные 150 г" in facts
        plan = await db.last_plan()
        assert [d["title"] for d in plan["dishes"]] == ["Пюре с котлетами и огурцом"]   # без белка — отброшено
        names = [i["name"] for i in plan["dishes"][0]["items"]]
        assert "выдуманный продукт" not in names
        kot = next(i for i in plan["dishes"][0]["items"] if i["name"] == "котлеты свиные")
        assert kot["kcal"] == 280                                       # цифры из записей, не от модели

        # «Что съесть?» предлагает своё блюдо первым и записывает его одной кнопкой
        out = await h.say("🍽 Что съесть?")
        assert "📌 На этой неделе: + огурец к обеду" in out and "⭐ Пюре с котлетами и огурцом" in out
        mk = h.session.calls[-1].reply_markup
        wp = next(b.callback_data for row in mk.inline_keyboard for b in row if b.callback_data.startswith("wp:"))
        assert len(wp.encode()) <= 64
        out = await h.tap(wp)
        assert "Пюре с котлетами и огурцом" in out and "котлеты свиные" in out
        n = len(await db.meals_on(db.today()))
        await h.tap("wp:999:0:0-100")                                   # кнопка от удалённого разбора
        assert len(await db.meals_on(db.today())) == n
        assert "Варианты устарели" in h.session.calls[-1].text

        # воскресные итоги приходят вместе с разбором рациона
        from bot.handlers import common
        h.session.calls.clear()
        await common.send_weekly(h.bot, UID, await db.get_profile(), with_review=True)
        assert "Разбор рациона" in h.out()
        assert "Меню на сегодня" not in await h.say("/menu")            # старое меню удалено
    run(h, go)


def test_foodbase_learns_and_reuses(h):
    from bot.services import foodbase

    async def go():
        await onboard(h)
        await h.say("борщ и 2 куска хлеба")                           # Claude: борщ 300 г / 180, хлеб 60 г / 140
        rows = {r["name"]: r for r in await db.foods()}
        assert rows["борщ"]["kcal"] == 60 and rows["борщ"]["pinned"] == 0 and rows["борщ"]["source"] == "claude"
        assert await db.products() == []                               # в быструю запись не попадает
        n = h.llm_calls["n"]
        out = await h.say("борщ 200 г")
        assert "по базе продуктов (оценка Claude), без запроса к Claude" in out and "120 ккал" in out
        assert h.llm_calls["n"] == n
        await db.update_product(rows["борщ"]["id"], kcal=50)          # ручная правка цифр
        out = await h.say("суп борщ с хлебом")                         # новый текст → Claude, но цифры из базы
        assert "по базе продуктов: борщ, хлеб" in out and "борщ 300 г — 150 ккал" in out

        # правка приёма пищи меняет калорийность продукта в базе, если она заметно другая
        idx = foodbase.index(await db.foods())
        changed = await foodbase.correct([{"name": "борщ", "grams": 300, "kcal": 240, "protein": 9, "fat": 9, "carbs": 30}], idx)
        assert changed == ["борщ"] and (await db.get_product(rows["борщ"]["id"]))["kcal"] == 80

        # две упаковки с одним названием и разными штрихкодами: по тексту — последняя использованная
        a = await db.add_product("куриное филе", 110, 23, 2, 0, "5901234123457", source="barcode")
        b = await db.add_product("куриное филе", 98, 22, 1, 0, "5900000000001", source="barcode")
        assert a != b
        await db.touch_products([b])
        assert "98 ккал" in await h.say("куриное филе 100 г")
        await db.update_product(a, aliases="филе курицы")
        await db.touch_products([a])
        assert "165 ккал" in await h.say("филе курицы 150 г")
    run(h, go)


def test_products_migration(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE products (name TEXT PRIMARY KEY, kcal REAL, protein REAL, fat REAL, carbs REAL);
        INSERT INTO products VALUES ('hi tec protein', 378, 79, 3.7, 7.1);
        CREATE TABLE meals (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ts TEXT, title TEXT,
            kcal REAL, protein REAL, fat REAL, carbs REAL, source TEXT, raw_text TEXT);
        CREATE TABLE meal_items (id INTEGER PRIMARY KEY AUTOINCREMENT, meal_id INTEGER, name TEXT, grams REAL,
            kcal REAL, protein REAL, fat REAL, carbs REAL);
        INSERT INTO meals (id, date, ts) VALUES (1, '2026-11-15', '2026-11-15T13:00:00'), (2, '2026-11-17', '2026-11-17T13:00:00');
        INSERT INTO meal_items (meal_id, name, grams, kcal, protein, fat, carbs) VALUES
            (1, 'Картофельное пюре', 270, 300, 5, 10, 45), (2, 'картофельное пюре', 270, 470, 5, 10, 45),
            (2, 'hi tec protein', 100, 378, 79, 4, 7);
    """)
    c.commit(); c.close()
    monkeypatch.setattr(config, "DB_PATH", path)

    async def go():
        await db.init()
        p = await db.products()
        assert [(x["id"], x["name"], x["pinned"], x["source"]) for x in p] == [(1, "hi tec protein", 1, "label")]
        rows = {r["name"]: r for r in await db.foods()}
        pure = rows["картофельное пюре"]
        assert pure["pinned"] == 0 and pure["source"] == "history" and pure["uses"] == 2
        assert pure["kcal"] == round((300 / 270 * 100 + 470 / 270 * 100) / 2, 1)
        await db.close()
        await db.init()                                             # повторный запуск ничего не дублирует
        assert len(await db.foods()) == 2
        await db.close()
    asyncio.run(go())


def test_food_table_roundtrip(h):
    from io import BytesIO
    from openpyxl import load_workbook
    from bot.services import foodtable

    async def go():
        await onboard(h)
        await h.say("борщ и 2 куска хлеба")
        await db.add_product("hi tec protein", 378, 79, 3.7, 7.1, "5901234123457", source="barcode")
        out = await h.tap("s:table")
        doc = next(c for c in h.session.calls if type(c).__name__ == "SendDocument")
        assert "База продуктов: 3, из них в быстрой записи 1" in doc.caption
        wb = load_workbook(BytesIO(doc.document.data))
        ws = wb["Продукты"]
        names = {ws.cell(r, 2).value: r for r in range(2, ws.max_row + 1)}
        assert ws.cell(names["hi tec protein"], 8).value == "5901234123457"
        r = names["борщ"]
        ws.cell(r, 4).value = 55
        ws.cell(r, 3).value = "борщ домашний, Борщик"
        ws.cell(r, 10).value = "да"
        ws.cell(names["хлеб"], 14).value = "да"
        ws.append([None, "Куриное филе Kraina", "куриное филе", 98, 22, 1, 0, 5900000000001, 500, "да"])
        ws.append([None, "плохой", "", "abc", 1, 1, 1])
        ws.append([None, "странный", "", 500, 10, 1, 1])
        buf = BytesIO(); wb.save(buf)
        current = await db.foods()
        changes, errors, warns = foodtable.parse(buf.getvalue(), current)
        assert {c["op"] for c in changes} == {"upd", "new", "del"} and len(changes) == 4
        assert any("«ккал» = «abc» — не число" in e for e in errors)
        assert any("странный" in w for w in warns)
        text = foodtable.summary(changes, current, errors, warns)
        assert "ккал 60 → 55" in text and "штрихкод 5900000000001" in text and "Удалить: 1" in text
        done, errs = await foodtable.apply(changes)
        assert done == 4 and not errs
        rows = {r["name"]: r for r in await db.foods()}
        assert "хлеб" not in rows and rows["борщ"]["kcal"] == 55 and rows["борщ"]["source"] == "manual"
        assert rows["борщ"]["pinned"] == 1 and rows["борщ"]["aliases"] == "борщ домашний, борщик"
        assert (await db.product_by_barcode("5900000000001"))["name"] == "куриное филе kraina"
        assert "по базе продуктов" in await h.say("куриное филе 200 г") and "196 ккал" in h.out()
        # файл не от бота
        assert foodtable.parse(b"not xlsx", [])[1]
    run(h, go)


def test_usage_counter(h):
    async def go():
        await onboard(h)
        await h.say("🍽 Что съесть?")
        await h.say("🍽 Что съесть?")
        await h.say("450 ккал")
        await h.tap("r:7")
        await h.tap("pr:g:5:30")
        out = await h.tap("s:usage")
        assert "2 — 🍽 Что съесть?" in out and "1 — еда/вес/тренировка текстом" in out and "1 — отчёт: неделя" in out
        assert "Ни разу:</b>" in out and "🎙 голосовые" in out
    run(h, go)


def test_quick_record_recent_products(h):
    """Быстрая запись: последние продукты без повторов → выбрать → граммы."""
    async def go():
        await onboard(h)
        chick = await db.add_product("куриное филе", 110, 23, 1.5, 0, source="claude", pinned=0)
        await db.add_product("пюре", 90, 2, 3, 14, source="claude", pinned=0)
        prot = await db.add_product("hi tec protein", 378, 79, 3, 6, source="label", pinned=1)
        mk = lambda n, g, k: {"name": n, "grams": g, "kcal": k, "protein": 0, "fat": 0, "carbs": 0}
        await db.add_meal([mk("пюре", 250, 225)], source="text")
        await db.add_meal([mk("Куриное филе", 150, 165)], source="text")
        await db.add_meal([mk("куриное филе", 200, 220)], source="text")
        out = await h.say("⚡ Быстрая запись")
        assert "🕘" not in out
        btns = [b for row in h.session.calls[-1].reply_markup.inline_keyboard for b in row]
        uses = [b.callback_data for b in btns if b.callback_data.startswith("pr:use:")]
        assert uses[0] == f"pr:use:{chick}" and len(uses) == 3 and uses[-1] == f"pr:use:{prot}"
        out = await h.tap(f"pr:use:{chick}")
        assert "Сколько граммов «куриное филе»" in out and "110 ккал" in out
        await h.say("180")
        m = await db.last_meal()
        assert round(m["kcal"]) == 198 and round(m["protein"]) == 41
    run(h, go)


def test_body_measurements(h):
    """Замеры: предложение после веса раз в неделю, шаги с пропуском, % жира по калиперу, итоги недели."""
    from bot.services import body, weekly

    async def go():
        await onboard(h)
        await db.save_measure(db.days_ago(7), "waist", 97)
        for k, v in (("sf_chest", 14), ("sf_abdomen", 24), ("sf_thigh", 16)):
            await db.save_measure(db.days_ago(7), k, v)
        out = await h.say("91.5")
        assert "замерим?" in out
        assert "s:measure" in str(h.session.calls[0].reply_markup)          # кнопка под каждым весом
        assert "замерим?" not in await h.say("91.4")              # второй раз за день не спрашиваем
        out = await h.tap("bm:go")
        assert "Талия" in out and "пупка" in out
        assert "от 15 до 200" in await h.say("950")
        await h.say("95,5")
        await h.tap("bm:skip")                                     # грудь
        for _ in range(3):
            await h.tap("bm:skip")                                 # бёдра, бедро, рука
        await h.say("12")
        await h.say("20")
        out = await h.say("15")
        assert "Талия: <b>95.5 см</b> (-1.5 к прошлому)" in out
        assert "Жир по калиперу: <b>14.7%</b>" in out and "без жира" in out and "→ -2.0%" in out
        assert body.body_fat("m", 34, {"sf_chest": 12, "sf_abdomen": 20, "sf_thigh": 15}) == 14.7
        assert "жир 16.7 → 14.7%" in (await weekly.build(await db.get_profile())).text
        assert "замерим?" not in await h.say("91.3")               # замеры свежие
        await h.tap("s:measure")
        assert "Закончить" in str(h.session.calls[-1].reply_markup)
        await h.tap("bm:done")
        assert "Замеры" in h.out()
    run(h, go)
