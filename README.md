# Бот для похудения (Telegram)

Личный бот: вес, калории, дневной бюджет с предупреждениями, настойчивые напоминания взвеситься,
разбор еды по тексту через Claude, адаптивный расчёт реального расхода. Полное описание — в `PLAN.md`.

## Настройка

1. Создайте бота у @BotFather, скопируйте токен.
2. Узнайте свой Telegram ID (например, у @userinfobot).
3. Получите ключ на console.anthropic.com.
4. `cp .env.example .env` и заполните `BOT_TOKEN`, `ALLOWED_USER_ID`, `ANTHROPIC_API_KEY`.

## Запуск локально

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m bot.main
```

## Деплой на Raspberry Pi

```bash
./deploy/deploy.sh пользователь@192.168.8.112
```

Скрипт копирует код и `.env`, ставит зависимости и запускает сервис `weightbot` (systemd, автозапуск).
База `data/bot.db` на Pi при повторном деплое не затрагивается. Логи: `journalctl --user -u weightbot -f`.

Не запускайте бота одновременно на Mac и на Pi с одним токеном: Telegram отдаёт сообщения только одному.

## Тесты

```bash
.venv/bin/python -m pytest -q
```
