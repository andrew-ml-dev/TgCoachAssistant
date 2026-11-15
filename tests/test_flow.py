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
from bot.handlers import diet, food, menu, misc, onboarding
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
        rs = [onboarding.router, menu.router, diet.router, onboarding.settings_router, food.router, misc.router]
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

    async def fake_menu(**kw):
        calls["menu"] = kw
        return llm.DayMenu(tip="пейте воду", meals=[
            llm.MenuMeal(title="Обед, 12:00", items=[F(name="омлет", grams=200, kcal=300, protein=22, fat=20, carbs=4),
                                                     F(name="протеиновый коктейль", grams=30, kcal=120, protein=24, fat=2, carbs=3)]),
            llm.MenuMeal(title="Ужин, 19:00", items=[F(name="курица с гречкой", grams=350, kcal=500, protein=50, fat=10, carbs=50)])])

    async def fake_swap(title, kcal, protein, old, dislikes, prefs):
        calls["swap"] = (title, kcal, protein, list(dislikes))
        return llm.MenuMeal(title=title, items=[F(name="индейка с рисом", grams=350, kcal=490, protein=48, fat=9, carbs=52)])

    async def fake_product(text):
        return llm.Product(ok="ккал" in text, name="Hi Tec Protein", kcal=380, protein=75, fat=6, carbs=8)

    for name, fn in (("parse_food", fake_parse), ("correct_food", fake_correct), ("day_menu", fake_menu),
                     ("replace_meal", fake_swap), ("parse_product", fake_product)):
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
        assert "1 500 ккал/день" in out and "≈0.99 кг/нед" in out and "ни на что не влияет" in out
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
        assert "🕘 — записать ещё раз как было" in out
    run(h, go)


def test_over_budget_edit_can_yesterday(h):
    async def go():
        await onboard(h)
        assert "🔴" not in await h.say("1100 ккал")
        assert "🔴" not in await h.say("150 ккал")              # 83% — промежуточных предупреждений нет
        out = await h.say("400 ккал")                           # 110%
        assert "🔴 Бюджет превышен на 150 ккал" in out and "Перебор: <b>+150 ккал</b>" in out
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
        assert "8 000 шагов ≈ 368 ккал" in await h.say("8000 шагов")
        out = await h.say("📊 Сегодня")
        assert "Вес утром: 92.0" in out and "• Борщ" in out
        assert "Баланс сейчас: расход 2 585 − съедено" in out and "Шаги: ≈368" in out
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
        assert "📦 — выбрать продукт и ввести вес" in out and "🕘 — записать ещё раз" in out
        # выбор продукта кнопкой и ввод веса
        pid = (await db.products())[0]["id"]
        await h.tap(f"pr:use:{pid}")
        out = await h.say("30")
        assert "hi tec protein 30 г — 114 ккал" in out and "по этикетке" in out
        out = await h.tap(f"pr:use:{pid}")
        assert "прошлую порцию" in out                            # есть быстрая кнопка
        assert await db.recent_grams("hi tec protein") == [30.0, 110.0]
        assert "114 ккал" in await h.tap(f"pr:g:{pid}:30")
        assert "Отправьте число" not in await h.say("📊 Сегодня")   # кнопка меню сбрасывает ввод веса
        await h.tap("pr:manage")
        await h.tap(f"pr:del:{pid}")
        assert await db.products() == []
    run(h, go)


def test_daily_menu_and_shake(h):
    async def go():
        await onboard(h)
        p = await db.get_profile()
        await h.tap("d:prefs")
        assert "Запомнил" in await h.say("2 приёма пищи: 12:00 и 19:00")
        await h.tap("d:sh:2")
        p = await db.get_profile()
        now = db.now().replace(hour=8, minute=0)

        # меню приходит только после взвешивания (или после отсечки), один раз в день
        await db.delete_weight(db.today())
        h.session.calls.clear()
        await reminders.morning_menu(h.bot, p, now)
        assert h.out() == ""
        await reminders.morning_menu(h.bot, p, now.replace(hour=12, minute=5))
        out = h.out()
        assert "Меню на сегодня" in out and "Итого: <b>920 ккал</b>" in out
        kw = h.llm_calls["menu"]
        assert (kw["kcal"], kw["protein"], kw["shakes"]) == (1500, 160, 2)
        h.session.calls.clear()
        await reminders.morning_menu(h.bot, p, now.replace(hour=12, minute=6))
        assert h.out() == ""

        # замена блюда: один вопрос, нелюбимое запоминается, коктейль остаётся
        day = db.today()
        v = (await db.get_menu(day))["v"]
        assert "Что не так в «Ужин, 19:00»" in await h.tap(f"mn:sw:{day}:{v}:1")
        out = await h.tap(f"mn:dl:{day}:{v}:1:0")
        assert "индейка с рисом" in out and "курица с гречкой" not in out
        assert h.llm_calls["swap"] == ("Ужин, 19:00", 500, 50, ["курица с гречкой"])
        assert (await db.get_profile())["diet_dislikes"] == "курица с гречкой"

        # «съел по плану» — без Claude и только один раз
        out = await h.tap(f"mn:eat:{day}:{v}:0")
        assert "Обед, 12:00 — 420 ккал" in out and "Осталось на сегодня: <b>1 080 ккал</b>" in out
        await h.tap(f"mn:eat:{day}:{v}:0")
        assert len(await db.meals_on(day)) == 1
        assert "✅ <b>Обед, 12:00</b>" in await h.say("🥗 Меню")

        await h.tap("d:toggle")
        assert (await db.get_profile())["menu_enabled"] == 0
    run(h, go)


def test_reminders_and_weekly(h):
    async def go():
        await onboard(h)
        await h.tap("d:toggle")                                 # меню выключено, чтобы не мешало
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
        assert "ни на что не влияет" in out
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

        # вечерний итог спрашивает, всё ли записано
        h.session.calls.clear()
        await reminders.evening(h.bot, await db.get_profile())
        assert "Всё за день записано?" in h.out()
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
        assert "1 400 ккал" in out and "✅" in out and "⚠️ неполный" in out and "сегодня" in out
        assert "Записано дней: 3 из 3, полных (без сегодня): 1" in out
        assert "В среднем: <b>1 400 ккал</b>" in out and "Дефицит за полные дни: 1 185 ккал ≈ -0.2 кг" in out
        assert "Вес по тренду: 92.0 →" in out
        from bot import keyboards as kb
        names = lambda: [type(c).__name__ for c in h.session.calls]
        # из текстового отчёта на «Всё»: текст нельзя превратить в фото — удалить и прислать фото
        out = await h.tap("r:0", markup=kb.report_menu("r:3"))
        assert "<b>Всё наблюдение</b>" in out and names()[1:] == ["DeleteMessage", "SendPhoto"]
        # фото → фото: правится на месте
        out = await h.tap("r:7", markup=kb.report_menu("r:0"), photo=True)
        assert "<b>Неделя</b>" in out and names()[1:] == ["EditMessageMedia"]
        # фото → сегодня (текст): пересоздаётся
        out = await h.tap("r:1", markup=kb.report_menu("r:7"), photo=True)
        assert "Осталось" in out and names()[1:] == ["DeleteMessage", "SendMessage"]
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
        assert "Быстрая запись" in out and "🕘 — записать ещё раз" in out
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

        # 5. кнопки старого меню после «Всё меню заново» не срабатывают
        await h.say("/menu")
        old_v = (await db.get_menu(db.today()))["v"]
        await h.tap("mn:new")
        n = len(await db.meals_on(db.today()))
        out = await h.tap(f"mn:eat:{db.today()}:{old_v}:0")
        assert len(await db.meals_on(db.today())) == n

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



def test_evening_retries_after_failure(h):
    async def go():
        await onboard(h)
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import SendMessage
        p = await db.get_profile()
        real = h.session.make_request
        async def failing(bot, method, timeout=None):
            if isinstance(method, SendMessage) and "Итог дня" in method.text:
                raise TelegramNetworkError(method=method, message="network down")
            return await real(bot, method, timeout)
        h.session.make_request = failing
        with pytest.raises(TelegramNetworkError):
            await reminders.evening(h.bot, p)
        assert not await db.was_sent("evening")                   # отметки нет — будет повтор
        h.session.make_request = real
        h.session.calls.clear()
        await reminders.evening(h.bot, p)
        assert "Итог дня" in h.out() and await db.was_sent("evening")
        h.session.calls.clear()
        await reminders.evening(h.bot, p)
        assert h.out() == ""                                       # второй раз не шлём

        # меню, открытое вручную через /menu, утром повторно не приходит
        await h.say("/menu")
        h.session.calls.clear()
        await reminders.morning_menu(h.bot, p, db.now().replace(hour=12, minute=30))
        assert h.out() == ""
    run(h, go)


def test_workouts(h):
    async def go():
        await onboard(h)                                            # расход 2585, бюджет 1500
        await h.say("1000 ккал")
        out = await h.say("трен 700")
        assert "в расход <b>595 ккал</b>" in out and "2 585 + 595 = <b>3 180 ккал</b>" in out
        assert "дефицит 2 180 ккал" in out and h.llm_calls["n"] == 0
        assert "тренировка +595 ккал к дефициту" in (await db.get_profile())["panel_text"]
        # без цифры — спросить калории, а не отправлять в разбор еды
        assert "Сколько калорий показали часы" in await h.say("зал")
        out = await h.say("650")
        assert "в расход <b>552 ккал</b>" in out and h.llm_calls["n"] == 0
        out = await h.tap("r:1")
        assert "расход 2 585 + тренировка 1 147 − съедено 1 000" in out and "дефицит 2 732" in out
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
        assert "🏋️ 340" in out and "дефицит 1 325" in out          # 2585 + 340 − 1600
        assert "Дефицит за полные дни: 1 325 ккал" in out
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
