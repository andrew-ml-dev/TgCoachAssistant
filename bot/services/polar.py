"""Polar AccessLink: импорт тренировок из Polar Flow (H10 → приложение Polar Flow → облако → бот).

Подключение разовое: ссылка авторизации → Polar перенаправляет на localhost (страница не откроется,
это нормально) → пользователь присылает боту адрес из браузера → бот меняет код на токен.
Дальше планировщик раз в 15 минут спрашивает новые тренировки. Белый IP не нужен.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from .. import config

log = logging.getLogger(__name__)

AUTH_URL = "https://flow.polar.com/oauth2/authorization"
TOKEN_URL = "https://polarremote.com/v2/oauth2/token"
API = "https://www.polaraccesslink.com/v3"
REDIRECT = "http://localhost:5000/oauth2_callback"   # тот же адрес нужно указать при создании клиента
TIMEOUT = aiohttp.ClientTimeout(total=30)

SPORTS = {
    "RUNNING": "бег", "TREADMILL_RUNNING": "бег на дорожке", "STRENGTH_TRAINING": "силовая",
    "WALKING": "ходьба", "CYCLING": "велосипед", "INDOOR_CYCLING": "велотренажёр", "SWIMMING": "плавание",
    "CIRCUIT_TRAINING": "круговая", "CROSSFIT": "кроссфит", "OTHER_INDOOR": "в зале", "FITNESS_CLASS": "фитнес",
    "CROSS_TRAINER": "эллипс", "ROWING": "гребля", "INDOOR_ROWING": "гребной тренажёр", "HIIT": "ВИИТ",
}


class PolarError(Exception):
    """Текст исключения показывается пользователю."""


class PolarAuthError(PolarError):
    """Токен отозван или недействителен — нужно подключиться заново."""


@dataclass
class Exercise:
    id: str
    date: str            # локальная дата начала, YYYY-MM-DD
    sport: str
    minutes: int
    kcal: int
    hr_avg: int | None
    hr_max: int | None

    @property
    def title(self) -> str:
        return f"{self.sport} {self.minutes} мин" if self.minutes else self.sport


def configured() -> bool:
    return bool(config.POLAR_CLIENT_ID and config.POLAR_CLIENT_SECRET)


def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": config.POLAR_CLIENT_ID,
                                       "redirect_uri": REDIRECT, "state": state})


def extract_code(text: str) -> str | None:
    """Код из присланного адреса (…?code=XXX&state=…) или сам код."""
    m = re.search(r"[?&]code=([^&\s#]+)", text)
    if m:
        return m.group(1)
    t = text.strip()
    return t if re.fullmatch(r"[A-Za-z0-9_\-.~]{6,200}", t) else None


def _basic() -> str:
    raw = f"{config.POLAR_CLIENT_ID}:{config.POLAR_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def connect(code: str) -> tuple[str, int]:
    """Код → (access_token, polar user id); регистрирует пользователя у клиента."""
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
                          headers={"Authorization": _basic(), "Accept": "application/json;charset=UTF-8"}) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                log.warning("Polar token %s: %s", r.status, body.get("error") if isinstance(body, dict) else "")
                raise PolarError("Polar не принял код. Он одноразовый и живёт 10 минут — "
                                 "нажмите ссылку подключения ещё раз.")
        token, uid = body["access_token"], int(body["x_user_id"])
        async with s.post(f"{API}/users", json={"member-id": f"weightbot-{uid}"},
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}) as r:
            if r.status == 403:
                raise PolarError("Polar требует принять согласия на передачу данных. Откройте flow.polar.com, "
                                 "примите их и подключитесь заново.")
            if r.status not in (200, 201, 409):  # 201 — зарегистрирован, 409 — уже был, оба нормально
                log.warning("Polar register %s", r.status)  # тело не пишем: там имя и дата рождения
                raise PolarError(f"Polar ответил ошибкой {r.status} при регистрации. Попробуйте позже.")
    return token, uid


def _minutes(iso: str | None) -> int:
    """ISO 8601 «PT1H2M3.5S» → минуты."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, sec = (float(x or 0) for x in m.groups())
    return round(d * 1440 + h * 60 + mi + sec / 60)


def parse(e: dict) -> Exercise | None:
    if not e.get("calories"):
        return None
    sport = (e.get("detailed_sport_info") or e.get("sport") or "").upper()
    name = SPORTS.get(sport) or SPORTS.get((e.get("sport") or "").upper()) or "тренировка"
    hr = e.get("heart_rate") or {}
    return Exercise(id=str(e["id"]), date=(e.get("start_time") or "")[:10], sport=name,
                    minutes=_minutes(e.get("duration")), kcal=int(e["calories"]),
                    hr_avg=hr.get("average"), hr_max=hr.get("maximum"))


async def exercises(token: str) -> list[Exercise]:
    """Тренировки, загруженные в Polar Flow за последние 30 дней (и после подключения)."""
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.get(f"{API}/exercises", headers={"Authorization": f"Bearer {token}",
                                                      "Accept": "application/json"}) as r:
            if r.status in (401, 403):
                raise PolarAuthError("Доступ к Polar отозван или устарел.")
            if r.status == 204:
                return []
            if r.status != 200:
                raise PolarError(f"Polar {r.status}")
            data = await r.json(content_type=None)
    items = data if isinstance(data, list) else data.get("exercises", [])
    return [x for x in (parse(e) for e in items) if x and x.date]
