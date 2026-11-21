import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, CallbackQuery, Message

from . import config, db
from . import keyboards as kb
from .handlers import body, diet, food, inputs, menu, misc, onboarding, polar
from .services import reminders, usage

log = logging.getLogger(__name__)


class Access(BaseMiddleware):
    """Пускает только владельца; без профиля — только онбординг."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user:
            return None
        owner = config.ALLOWED_USER_ID or await db.get_owner()
        if not owner:
            # ALLOWED_USER_ID не задан: владельцем становится первый, кто отправит /start
            if not (isinstance(event, Message) and (event.text or "").startswith("/start")):
                return None
            await db.claim_owner(user.id)
            log.warning("Владелец бота закреплён: user_id=%s", user.id)
            owner = user.id
        if user.id != owner:
            return None
        profile = await db.get_profile()
        data["profile"] = profile
        if profile is None:
            state = await data["state"].get_state()
            is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
            if not is_start and not (state or "").startswith("Onb"):
                if isinstance(event, Message):
                    await event.answer("Сначала настроим профиль: нажмите /start")
                elif isinstance(event, CallbackQuery):
                    await event.answer("Сначала /start", show_alert=True)
                return None
        if profile is not None and (k := usage.key(event)):
            try:
                await db.count_use(k)
            except Exception:  # счётчик не должен мешать работе бота
                log.exception("Счётчик использования")
        return await handler(event, data)


KEYBOARD_VERSION = "5"  # поднять, когда меняется постоянная панель


async def _keyboard_outdated() -> bool:
    """True один раз после смены версии панели."""
    return await db.mark_flag("keyboard", KEYBOARD_VERSION)


async def main() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(config.DB_PATH.parent / "bot.log", maxBytes=1_000_000, backupCount=2,
                                      encoding="utf-8")])
    if not config.BOT_TOKEN:
        raise SystemExit("Заполните BOT_TOKEN в файле .env (см. .env.example)")

    await db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.message.middleware(Access())
    dp.callback_query.middleware(Access())
    # Порядок важен: онбординг → меню и команды → настройки → состояния еды → свободный ввод
    dp.include_routers(onboarding.router, menu.router, body.router, diet.router, onboarding.settings_router, food.router, polar.router, inputs.router, misc.router)

    # Меню «/» — по частоте использования
    await bot.set_my_commands([
        BotCommand(command="today", description="Отчёт: сегодня, 3 дня, неделя, всё"),
        BotCommand(command="eat", description="Что съесть сейчас"),
        BotCommand(command="can", description="Влезет ли блюдо в остаток"),
        BotCommand(command="undo", description="Удалить последнюю запись еды"),
        BotCommand(command="chart", description="Прогресс за всё время"),
        BotCommand(command="week", description="Итоги недели и пересчёт бюджета"),
        BotCommand(command="help", description="Как пользоваться"),
        BotCommand(command="cancel", description="Отменить текущий вопрос"),
        BotCommand(command="polar", description="Тренировки из Polar Flow"),
        BotCommand(command="cost", description="Расход API"),
        BotCommand(command="export", description="Выгрузка в CSV"),
    ])

    # при каждом запуске отправляем свежую клавиатуру: панель внизу обновится и после деплоя
    profile = await db.get_profile()
    if profile and profile["chat_id"] and await _keyboard_outdated():
        try:
            await bot.send_message(profile["chat_id"], "🔄 Панель обновлена: «❓ Можно ли?» теперь кнопка внутри «🍽 Что съесть?».",
                                   reply_markup=kb.main_menu, disable_notification=True)
        except Exception:
            log.warning("Не удалось отправить клавиатуру")
    task = asyncio.create_task(reminders.run(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
