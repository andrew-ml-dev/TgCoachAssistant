from datetime import date, timedelta

from bot.services import nutrition as n
from bot.services.parser import classify, normalize, parse_time


def test_bmr_and_tdee():
    assert round(n.bmr("m", 92, 180, 34)) == 1880
    assert round(n.bmr("f", 70, 165, 30)) == 1420
    assert n.initial_tdee("m", 92, 180, 34, 1.375) == 2585


def test_budget_floor_at_2kg_pace():
    b = n.make_budget(2585, 2.0, 1500)
    assert b.wanted_deficit == 2200
    assert b.floored and b.budget == 1500
    assert b.achievable_pace == round((2585 - 1500) * 7 / 7700, 2)
    assert n.extra_activity_needed(2585, 1500, 2.0) == 2200 - 1085


def test_budget_without_floor():
    b = n.make_budget(2600, 0.5, 1500)
    assert not b.floored and b.budget == 2050 and b.achievable_pace == 0.5


def test_trend_smooths_and_handles_gaps():
    d0 = date(2026, 11, 1)
    pts = [(d0, 90.0), (d0 + timedelta(days=1), 92.0), (d0 + timedelta(days=4), 90.0)]
    t = n.compute_trends(pts)
    assert t[0] == 90.0
    assert t[1] == 90.5
    assert t[2] == round(90.5 + (1 - 0.75 ** 3) * (90.0 - 90.5), 2)


def test_adaptive_tdee():
    # ел 1800, вес падает на 0.1 кг в день → 1800 + 0.1*7700 = 2570
    d0 = date(2026, 11, 1)
    pts = [(d0 + timedelta(days=i), 92 - 0.1 * i + (0.4 if i % 2 else -0.4)) for i in range(14)]
    assert abs(n.slope_per_day(pts) + 0.1) < 0.02   # шум взвешиваний почти не влияет
    assert n.adaptive_tdee(1800, -0.1) == 2570
    assert n.adaptive_tdee(1800, 0.9) == 1200        # ограничение снизу
    assert n.smooth_tdee(2500, 2700) == 2640


def test_rate_and_forecast():
    d0 = date(2026, 11, 1)
    pts = [(d0 + timedelta(days=i), 92 - 0.1 * i) for i in range(15)]
    assert n.weekly_rate(pts) == -0.7
    assert n.forecast_date(90.6, 87.1, -0.7, d0) == d0 + timedelta(days=35)
    assert n.forecast_date(90.6, 87.1, 0.1, d0) is None


def test_products():
    from bot.services import products as pr
    prods = [{"name": "hi tec protein", "kcal": 380, "protein": 75, "fat": 6, "carbs": 8}]
    it = pr.match_exact("Hi Tec Protein 110 г", prods)
    assert (it["kcal"], it["protein"]) == (418.0, 82.5)
    assert pr.match_exact("110 грамм hi tec protein", prods)["grams"] == 110
    assert pr.match_exact("пельмени и hi tec protein 110 г", prods) is None
    items = [{"name": "Hi Tec Protein", "grams": 30, "kcal": 999, "protein": 1, "fat": 0, "carbs": 0},
             {"name": "пельмени", "grams": 400, "kcal": 1040, "protein": 44, "fat": 0, "carbs": 0}]
    out = pr.apply(items, prods)
    assert (out[0]["kcal"], out[0]["protein"]) == (114.0, 22.5) and out[1]["kcal"] == 1040


def test_classify():
    assert classify("82.4").kind == "weight"
    assert classify("82,4 кг").value == 82.4
    assert classify("вес 82").kind == "weight"
    assert classify("450").kind == "kcal"
    c = classify("450 ккал обед")
    assert (c.kind, c.value, c.label) == ("kcal", 450, "обед")
    c = classify("шаурма 600 ккал")
    assert (c.kind, c.value, c.label) == ("kcal", 600, "шаурма")
    assert classify("8000 шагов").value == 8000
    assert classify("шаги 12 500").value == 12500
    assert classify("90", last_weight=91.5).kind == "ambiguous"
    assert classify("90", last_weight=None).kind == "ambiguous"
    assert classify("300", last_weight=91.5).kind == "kcal"
    assert classify("тарелка борща и 2 куска хлеба").kind == "food"
    assert classify("2 яйца").kind == "food"


def test_helpers():
    assert normalize("  Борщ   И Хлеб. ") == "борщ и хлеб"
    assert parse_time("7:30") == "07:30" and parse_time("8") == "08:00" and parse_time("25:00") is None


def test_classify_workout():
    for text, val, label in (("трен 700", 700, "трен"), ("зал + дорожка 650 ккал", 650, "зал + дорожка"),
                             ("бег 30 мин 400", 400, "бег 30 мин"), ("700 тренировка", 700, "тренировка"),
                             ("силовая 1 час 520", 520, "силовая 1 час")):
        c = classify(text)
        assert (c.kind, c.value, c.label) == ("workout", val, label)
    assert (classify("зал").kind, classify("зал").value) == ("workout", 0)
    assert classify("обед 700 ккал").kind == "kcal"
    assert classify("гречка 200 г").kind == "food"
