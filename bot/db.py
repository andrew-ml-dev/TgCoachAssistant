"""SQLite: схема и запросы. Бот однопользовательский, профиль — одна строка."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import aiosqlite

from . import config
from .services import nutrition

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    chat_id INTEGER, sex TEXT, age INTEGER, height_cm REAL,
    start_weight REAL, target_weight REAL, activity REAL,
    pace REAL, floor_kcal INTEGER, tdee INTEGER, budget INTEGER, protein_goal INTEGER,
    wake_time TEXT DEFAULT '07:30', cutoff_time TEXT DEFAULT '12:00', created TEXT
);
CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK (id = 1), user_id INTEGER);
CREATE TABLE IF NOT EXISTS weights (date TEXT PRIMARY KEY, weight REAL, trend REAL);
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ts TEXT, title TEXT,
    kcal REAL, protein REAL, fat REAL, carbs REAL, source TEXT, raw_text TEXT
);
CREATE INDEX IF NOT EXISTS meals_date ON meals(date);
CREATE TABLE IF NOT EXISTS meal_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, meal_id INTEGER REFERENCES meals(id) ON DELETE CASCADE,
    name TEXT, grams REAL, kcal REAL, protein REAL, fat REAL, carbs REAL
);
CREATE TABLE IF NOT EXISTS food_cache (key TEXT PRIMARY KEY, items TEXT);
CREATE TABLE IF NOT EXISTS templates (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, items TEXT);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ts TEXT, descr TEXT, kcal REAL
);
CREATE TABLE IF NOT EXISTS day_state (
    date TEXT PRIMARY KEY, weight_skipped INTEGER DEFAULT 0, snooze_until TEXT,
    last_weight_remind TEXT, warned INTEGER DEFAULT 0, sent TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS budget_history (date TEXT, tdee INTEGER, budget INTEGER);
CREATE TABLE IF NOT EXISTS menus (date TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS products (name TEXT PRIMARY KEY, kcal REAL, protein REAL, fat REAL, carbs REAL);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost REAL
);
"""

# Колонки профиля, добавленные после первой версии (миграция при старте)
PROFILE_EXTRA = {
    "diet_prefs": "TEXT DEFAULT ''",
    "shake_servings": "INTEGER DEFAULT 1",
    "shake_protein": "REAL DEFAULT 24",
    "menu_enabled": "INTEGER DEFAULT 1",
    "diet_dislikes": "TEXT DEFAULT ''",
    "shake_product": "TEXT DEFAULT ''",
    "panel_msg": "INTEGER",
    "panel_text": "TEXT DEFAULT ''",
}

_db: aiosqlite.Connection | None = None


async def init() -> None:
    global _db
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA foreign_keys = ON")
    await _db.executescript(SCHEMA)
    cols = {r["name"] for r in await _all("PRAGMA table_info(profile)")}
    for name, ddl in PROFILE_EXTRA.items():
        if name not in cols:
            await _db.execute(f"ALTER TABLE profile ADD COLUMN {name} {ddl}")
    cols = {r["name"] for r in await _all("PRAGMA table_info(day_state)")}
    if "remind_msg" not in cols:
        await _db.execute("ALTER TABLE day_state ADD COLUMN remind_msg INTEGER")
    if "complete" not in cols:  # 1 — всё записано, -1 — не всё, 0 — не отмечено
        await _db.execute("ALTER TABLE day_state ADD COLUMN complete INTEGER DEFAULT 0")
    cols = {r["name"] for r in await _all("PRAGMA table_info(activity)")}
    if "kind" not in cols:  # steps — справочно, workout — входит в расход дня
        await _db.execute("ALTER TABLE activity ADD COLUMN kind TEXT DEFAULT 'steps'")
    if "raw_kcal" not in cols:  # цифра с часов до поправки
        await _db.execute("ALTER TABLE activity ADD COLUMN raw_kcal REAL")
    await _db.commit()


async def close() -> None:
    if _db:
        await _db.close()


def now() -> datetime:
    return datetime.now(config.TZ)


def today() -> str:
    return now().date().isoformat()


async def _all(sql: str, *args) -> list[aiosqlite.Row]:
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def _one(sql: str, *args) -> aiosqlite.Row | None:
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


async def _exec(sql: str, *args) -> int:
    cur = await _db.execute(sql, args)
    await _db.commit()
    return cur.lastrowid


# ---------- владелец ----------

async def get_owner() -> int | None:
    r = await _one("SELECT user_id FROM owner WHERE id = 1")
    return r["user_id"] if r else None


async def claim_owner(user_id: int) -> None:
    await _exec("INSERT OR IGNORE INTO owner (id, user_id) VALUES (1, ?)", user_id)


# ---------- профиль ----------

async def get_profile() -> aiosqlite.Row | None:
    return await _one("SELECT * FROM profile WHERE id = 1")


async def save_profile(**f) -> None:
    f["created"] = today()
    old = await get_profile()
    if old:  # настройки диеты переживают повторный онбординг
        for k in PROFILE_EXTRA:
            f.setdefault(k, old[k])
    cols = ", ".join(f)
    marks = ", ".join("?" for _ in f)
    await _exec(f"INSERT OR REPLACE INTO profile (id, {cols}) VALUES (1, {marks})", *f.values())


async def update_profile(**f) -> None:
    sets = ", ".join(f"{k} = ?" for k in f)
    await _exec(f"UPDATE profile SET {sets} WHERE id = 1", *f.values())


async def set_budget(tdee: int, budget: int) -> None:
    await update_profile(tdee=tdee, budget=budget)
    await _exec("INSERT INTO budget_history VALUES (?, ?, ?)", today(), tdee, budget)



# ---------- вес ----------

async def add_weight(weight: float, day: str | None = None) -> None:
    await _exec("INSERT OR REPLACE INTO weights (date, weight, trend) VALUES (?, ?, 0)", day or today(), weight)
    await _recalc_trends()


async def delete_weight(day: str) -> None:
    await _exec("DELETE FROM weights WHERE date = ?", day)
    await _recalc_trends()


async def _recalc_trends() -> None:
    rows = await _all("SELECT date, weight FROM weights ORDER BY date")
    pts = [(date.fromisoformat(r["date"]), r["weight"]) for r in rows]
    for r, t in zip(rows, nutrition.compute_trends(pts)):
        await _db.execute("UPDATE weights SET trend = ? WHERE date = ?", (t, r["date"]))
    await _db.commit()


async def weights(since: str | None = None) -> list[aiosqlite.Row]:
    return await _all("SELECT * FROM weights WHERE date >= ? ORDER BY date", since or "0000")


async def last_weight() -> aiosqlite.Row | None:
    return await _one("SELECT * FROM weights ORDER BY date DESC LIMIT 1")


async def weight_on(day: str) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM weights WHERE date = ?", day)


async def weight_before(day: str) -> aiosqlite.Row | None:
    """Последняя запись не позже указанной даты."""
    return await _one("SELECT * FROM weights WHERE date <= ? ORDER BY date DESC LIMIT 1", day)


# ---------- еда ----------

def make_title(items: list[dict]) -> str:
    """Название приёма пищи по содержимому: «Пельмени домашние, блины…»."""
    t = ", ".join(i["name"] for i in items)
    return t[:1].upper() + (t[1:38] + "…" if len(t) > 38 else t[1:])


async def add_meal(items: list[dict], source: str, raw_text: str = "", day: str | None = None,
                   title: str | None = None) -> int:
    tot = {k: sum(i.get(k) or 0 for i in items) for k in ("kcal", "protein", "fat", "carbs")}
    mid = await _exec(
        "INSERT INTO meals (date, ts, title, kcal, protein, fat, carbs, source, raw_text) VALUES (?,?,?,?,?,?,?,?,?)",
        day or today(), now().isoformat(timespec="seconds"), title or make_title(items),
        tot["kcal"], tot["protein"], tot["fat"], tot["carbs"], source, raw_text,
    )
    for i in items:
        await _db.execute(
            "INSERT INTO meal_items (meal_id, name, grams, kcal, protein, fat, carbs) VALUES (?,?,?,?,?,?,?)",
            (mid, i["name"], i.get("grams") or 0, i.get("kcal") or 0,
             i.get("protein") or 0, i.get("fat") or 0, i.get("carbs") or 0),
        )
    await _db.commit()
    return mid


async def replace_meal_items(meal_id: int, items: list[dict]) -> None:
    await _db.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
    tot = {k: sum(i.get(k) or 0 for i in items) for k in ("kcal", "protein", "fat", "carbs")}
    await _db.execute(
        "UPDATE meals SET kcal=?, protein=?, fat=?, carbs=?, "
        "title = CASE WHEN source = 'menu' THEN title ELSE ? END WHERE id=?",
        (tot["kcal"], tot["protein"], tot["fat"], tot["carbs"], make_title(items), meal_id))
    for i in items:
        await _db.execute(
            "INSERT INTO meal_items (meal_id, name, grams, kcal, protein, fat, carbs) VALUES (?,?,?,?,?,?,?)",
            (meal_id, i["name"], i.get("grams") or 0, i.get("kcal") or 0,
             i.get("protein") or 0, i.get("fat") or 0, i.get("carbs") or 0),
        )
    await _db.commit()


async def get_meal(meal_id: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM meals WHERE id = ?", meal_id)


async def meal_items(meal_id: int) -> list[dict]:
    rows = await _all("SELECT name, grams, kcal, protein, fat, carbs FROM meal_items WHERE meal_id = ?", meal_id)
    return [dict(r) for r in rows]


async def delete_meal(meal_id: int) -> None:
    await _exec("DELETE FROM meals WHERE id = ?", meal_id)


async def meals_on(day: str) -> list[aiosqlite.Row]:
    return await _all("SELECT * FROM meals WHERE date = ? ORDER BY ts", day)


async def last_meal() -> aiosqlite.Row | None:
    return await _one("SELECT * FROM meals ORDER BY id DESC LIMIT 1")


async def move_meal(meal_id: int, day: str) -> None:
    await _exec("UPDATE meals SET date = ? WHERE id = ?", day, meal_id)


async def recent_meals(limit: int = 10) -> list[aiosqlite.Row]:
    return await _all(
        "SELECT MAX(id) AS id, raw_text, kcal FROM meals WHERE raw_text != '' "
        "GROUP BY lower(raw_text) ORDER BY id DESC LIMIT ?", limit)


async def day_totals(day: str) -> dict:
    r = await _one(
        "SELECT COALESCE(SUM(kcal),0) kcal, COALESCE(SUM(protein),0) protein, "
        "COALESCE(SUM(fat),0) fat, COALESCE(SUM(carbs),0) carbs, COUNT(*) n FROM meals WHERE date = ?", day)
    return dict(r)


async def counted_days(since: str, until: str, budget: float) -> list[dict]:
    """Дни, которым можно верить в расчётах расхода и дефицита.

    Отмеченные «всё записал» — да; «не всё» — нет; неотмеченные — если записано ≥60% бюджета
    (иначе день с забытым ужином выглядел бы как голодание).
    """
    rows = await _all(
        "SELECT m.date, SUM(m.kcal) kcal, SUM(m.protein) protein, COALESCE(d.complete, 0) complete "
        "FROM meals m LEFT JOIN day_state d ON d.date = m.date "
        "WHERE m.date >= ? AND m.date <= ? GROUP BY m.date ORDER BY m.date", since, until)
    return [dict(r) for r in rows
            if r["complete"] == 1 or (r["complete"] == 0 and r["kcal"] >= budget * 0.6)]


async def first_date() -> str | None:
    r = await _one("SELECT MIN(d) d FROM (SELECT MIN(date) d FROM meals UNION SELECT MIN(date) FROM weights)")
    return r["d"] if r else None


async def period_totals(dates: list[str]) -> dict:
    marks = ",".join("?" for _ in dates)
    r = await _one(f"SELECT COALESCE(SUM(kcal),0) kcal, COALESCE(SUM(protein),0) protein, COALESCE(SUM(fat),0) fat, "
                   f"COALESCE(SUM(carbs),0) carbs FROM meals WHERE date IN ({marks})", *dates)
    return dict(r)


async def daily_kcal(since: str, until: str) -> list[aiosqlite.Row]:
    return await _all(
        "SELECT date, SUM(kcal) kcal, SUM(protein) protein FROM meals "
        "WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date", since, until)


# ---------- кеш и шаблоны ----------

async def cache_get(key: str) -> list[dict] | None:
    r = await _one("SELECT items FROM food_cache WHERE key = ?", key)
    return json.loads(r["items"]) if r else None


async def cache_put(key: str, items: list[dict]) -> None:
    await _exec("INSERT OR REPLACE INTO food_cache VALUES (?, ?)", key, json.dumps(items, ensure_ascii=False))


async def cache_del(key: str) -> None:
    await _exec("DELETE FROM food_cache WHERE key = ?", key)


async def products() -> list[dict]:
    return [dict(r) for r in await _all("SELECT rowid AS id, * FROM products ORDER BY name")]


async def get_product(pid: int) -> dict | None:
    r = await _one("SELECT rowid AS id, * FROM products WHERE rowid = ?", pid)
    return dict(r) if r else None


async def add_product(name: str, kcal: float, protein: float, fat: float, carbs: float) -> int:
    return await _exec("INSERT OR REPLACE INTO products (name, kcal, protein, fat, carbs) VALUES (?,?,?,?,?)",
                       name, kcal, protein, fat, carbs)


async def recent_grams(name: str, limit: int = 3) -> list[float]:
    """Последние разные порции продукта — для кнопок быстрого выбора."""
    rows = await _all("SELECT grams FROM meal_items WHERE name = ? AND grams > 0 ORDER BY id DESC LIMIT 20", name)
    out: list[float] = []
    for r in rows:
        if r["grams"] not in out:
            out.append(r["grams"])
    return out[:limit]


async def delete_product(pid: int) -> None:
    await _exec("DELETE FROM products WHERE rowid = ?", pid)


# ---------- меню на день ----------

async def top_foods(limit: int = 15) -> list[str]:
    """Что пользователь сам чаще всего записывал за 30 дней (еда из меню не считается)."""
    rows = await _all(
        "SELECT mi.name, COUNT(*) c FROM meal_items mi JOIN meals m ON m.id = mi.meal_id "
        "WHERE m.date >= ? AND mi.grams > 0 AND m.source IN ('llm', 'cache', 'repeat', 'template') "
        "GROUP BY mi.name HAVING c >= 2 ORDER BY c DESC LIMIT ?", days_ago(30), limit)
    return [r["name"] for r in rows]



async def get_menu(day: str) -> dict | None:
    r = await _one("SELECT data FROM menus WHERE date = ?", day)
    return json.loads(r["data"]) if r else None


async def save_menu(day: str, data: dict) -> None:
    await _exec("INSERT OR REPLACE INTO menus VALUES (?, ?)", day, json.dumps(data, ensure_ascii=False))


# ---------- активность ----------

async def add_activity(descr: str, kcal: float) -> int:
    """Шаги: только справочно, в расход не входят (они уже в коэффициенте активности)."""
    return await _exec("INSERT INTO activity (date, ts, descr, kcal, kind) VALUES (?,?,?,?, 'steps')",
                       today(), now().isoformat(timespec="seconds"), descr, kcal)


async def activity_kcal(day: str) -> float:
    r = await _one("SELECT COALESCE(SUM(kcal),0) k FROM activity WHERE date = ? AND kind = 'steps'", day)
    return r["k"]


async def add_workout(descr: str, raw_kcal: float, kcal: float, day: str | None = None) -> int:
    return await _exec("INSERT INTO activity (date, ts, descr, kcal, kind, raw_kcal) VALUES (?,?,?,?, 'workout', ?)",
                       day or today(), now().isoformat(timespec="seconds"), descr, kcal, raw_kcal)


async def get_workout(wid: int) -> aiosqlite.Row | None:
    return await _one("SELECT * FROM activity WHERE id = ? AND kind = 'workout'", wid)


async def delete_workout(wid: int) -> None:
    await _exec("DELETE FROM activity WHERE id = ? AND kind = 'workout'", wid)


async def workouts_on(day: str) -> list[aiosqlite.Row]:
    return await _all("SELECT * FROM activity WHERE date = ? AND kind = 'workout' ORDER BY id", day)


async def workout_kcal(day: str) -> float:
    r = await _one("SELECT COALESCE(SUM(kcal),0) k FROM activity WHERE date = ? AND kind = 'workout'", day)
    return r["k"]


async def workouts_by_day(since: str, until: str) -> dict[str, float]:
    """Учтённые калории тренировок по дням: дата -> ккал (дни без тренировок отсутствуют)."""
    rows = await _all("SELECT date, SUM(kcal) k FROM activity WHERE kind = 'workout' AND date >= ? AND date <= ? "
                      "GROUP BY date", since, until)
    return {r["date"]: r["k"] for r in rows}


# ---------- состояние дня ----------

async def day_state(day: str | None = None) -> aiosqlite.Row:
    day = day or today()
    await _exec("INSERT OR IGNORE INTO day_state (date) VALUES (?)", day)
    return await _one("SELECT * FROM day_state WHERE date = ?", day)


async def set_day_state(day: str | None = None, **f) -> None:
    day = day or today()
    await day_state(day)
    sets = ", ".join(f"{k} = ?" for k in f)
    await _exec(f"UPDATE day_state SET {sets} WHERE date = ?", *f.values(), day)


async def was_sent(flag: str, day: str | None = None) -> bool:
    st = await day_state(day)
    return flag in st["sent"].split(",")


async def mark_sent(flag: str, day: str | None = None) -> bool:
    """Ставит флаг разовой рассылки; False, если уже стоял."""
    st = await day_state(day)
    flags = set(filter(None, st["sent"].split(",")))
    if flag in flags:
        return False
    flags.add(flag)
    await set_day_state(day, sent=",".join(sorted(flags)))
    return True


async def mark_flag(name: str, value: str) -> bool:
    """Сохраняет значение флага; True, если оно изменилось."""
    await _db.execute("CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT)")
    r = await _one("SELECT value FROM flags WHERE name = ?", name)
    if r and r["value"] == value:
        return False
    await _exec("INSERT OR REPLACE INTO flags VALUES (?, ?)", name, value)
    return True


# ---------- расходы API ----------

async def add_usage(kind: str, inp: int, out: int) -> None:
    cost = inp * config.PRICE_IN / 1e6 + out * config.PRICE_OUT / 1e6
    await _exec("INSERT INTO llm_usage (ts, kind, input_tokens, output_tokens, cost) VALUES (?,?,?,?,?)",
                now().isoformat(timespec="seconds"), kind, inp, out, cost)


async def usage_month() -> dict:
    month = today()[:7]
    r = await _one(
        "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) inp, COALESCE(SUM(output_tokens),0) out, "
        "COALESCE(SUM(cost),0) cost FROM llm_usage WHERE ts LIKE ?", month + "%")
    return dict(r)


# ---------- сервис ----------

async def backup() -> str:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = config.BACKUP_DIR / f"bot-{today()}.db"
    if not path.exists():
        await _db.execute("VACUUM INTO ?", (str(path),))
    for old in sorted(config.BACKUP_DIR.glob("bot-*.db"))[:-14]:
        old.unlink()
    return str(path)


def days_ago(n: int) -> str:
    return (now().date() - timedelta(days=n)).isoformat()
