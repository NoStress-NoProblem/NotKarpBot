import os
import logging
import json
import asyncio
import gspread
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.filters.text import Text
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from google.oauth2 import service_account

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана!")

PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-secret")
BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL", "https://your-domain.com")  # Укажите ваш домен

# === ФИКТИВНЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# === GOOGLE ТАБЛИЦА (обновлено для google-auth) ===
SHEET = None
try:
    google_creds_json = os.getenv("GOOGLE_CREDS")
    if not google_creds_json:
        raise ValueError("Переменная GOOGLE_CREDS не задана!")

    credentials_dict = json.loads(google_creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    gc = gspread.authorize(credentials)
    SHEET = gc.open("Клиенты фитнес-бота").sheet1
    print("✅ Успешно подключено к Google Таблице!")
except Exception as e:
    print(f"❌ Ошибка подключения к Google Таблице: {e}")

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FSM состояния
class UserStates(StatesGroup):
    waiting_for_email = State()

# Инициализация бота и диспетчера
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# === КНОПКИ ===
START_BUTTON = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Хочу в проект 💪")]], resize_keyboard=True, one_time_keyboard=False)
TARIFF_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="15 дней (1990 ₽)"), KeyboardButton(text="1 месяц (3000 ₽)")],
        [KeyboardButton(text="3 месяца (6990 ₽)")],
        [KeyboardButton(text="⬅️ Назад")]
    ], 
    resize_keyboard=True
)
AFTER_PAYMENT_MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Продолжить ▶️")]], resize_keyboard=True)

MAIN_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Тарифы 💰", callback_data="tariffs")],
    [InlineKeyboardButton(text="Отзывы 🥹", callback_data="reviews")]
])

@dp.message(CommandStart())
async def start(message: Message):
    photo_url = "https://i.ibb.co/pr4Cxkk/1.jpg"
    caption = (
        "«POLINAFIT» — место, где ты обретёшь новую версию себя! 💫\n\n"
        "Проект — это не краткосрочный марафон. Это про индивидуальный подход к каждой участнице!\n\n"
        "Я даю рекомендации по питанию, после того как подробно изучу каждый,индивидуальный случай, "
        "исходя из вашей ситуации, образа жизни, активности, вида деятельности , возможные травмы. "
        "Именно такой подход поможет тебе достричь поставленной цели!"
    )
    await message.reply_photo(photo=photo_url, caption=caption)
    await message.reply("Готова начать? 👇", reply_markup=START_BUTTON)

@dp.message(Text(text="Хочу в проект 💪"))
async def want_project(message: Message):
    desc = (
        "Проект POLINAFIT- это комплексная работа,где важно абсолютно всё! Режим питания,тренировки,"
        "поддержка от участниц проекта и лично меня! Это то, место где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, место где ты не откатишься назад и не потеряешь результат, "
        "если случились непредвиденные обстоятельства (отпуск,стресс,травмы,болезнь итд)"
    )
    await message.reply(desc)

    features = (
        "Что входит в проект:\n\n"
        "🤍 Тренировки для любого уровня подготовки дома или в зале:\n"
           "— легкие , для тех кто только начинает\n"
    "— средней сложности, для тех кто уже занимается\n"
    "— интенсивные, для тех кто тренируется регулярно и хочет прогрессировать и готов к нагрузкам\n\n"
        "🤍 Питание:\n"
        "индивидуальный расчет КБЖУ, исходя из ваших особенностей, активности и образа жизни, "
        "анализ динамики и изменения расчета по необходимости большие сборники завтраков,обедов и ужинов "
        "с указанием КБЖУ каждого блюда , для того чтобы тебе было легче подбирать рацион\n\n"
        "🤍 Индивидуальная работа с отчетами:\n"
        "2 раза в неделю проверяю лично отчеты по питанию, по необходимости вношу корректировки "
        "для более эффективного результата поставленной цели\n"
        "2 раза в месяц проверяю отчеты по форме,фиксируем замеры , на основе которых могу изменить "
        "тренировочный план или норму КБЖУ\n\n"
        "🤍 Абсолютно любая цель:\n"
        "— снижение веса\n"
        "— набор веса\n\n"
        "🤍 Доступ к чату со всеми девочками участницами , там мы обсуждаем результаты,делимся эмоциями, "
        "рецептами, просто болтаем и поддерживаем друг друга на протяжении каждого дня, заряжаемся позитивом, "
        "настраиваемся на продуктивные дни, там ты всегда можешь задать мне интересующий тебя вопрос. "
        "Ведь так важно знать,что ты не один и тебя всегда поддержат!🫂"
    )
    await message.reply(features)

    await message.reply("Выбери, что хочешь узнать:", reply_markup=MAIN_MENU)

@dp.callback_query(F.data == "tariffs")
async def tariffs_callback(callback: CallbackQuery):
    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = (
        "В проекте действует подписка, которая открывает тебе доступ к следующим преимуществам:\n\n"
        "🤍 Анализ состояния для подбора питания и тренировок\n"
        "🤍 Индивидуальный расчет КБЖУ и план тренировок, составленный лично\n"
        "🤍 Тренировки на любую цель ( жиросжигание,силовые итп)\n"
        "🤍 Возможность тренироваться где удобно, дома или в зале\n"
        "🤍 Подробно расписанная техника каждого упражнения и возможность задавать вопросы по технике в общий чат\n"
        "🤍 Контроль питания и формы каждую неделю\n"
        "🤍 Общий чат с участницами проекта\n"
        "🤍 Возможность задавать любые вопросы по теме питания\n"
        "🤍 Огромный сборник простых,бюджетных рецептов\n"
        "🤍 Гайд по продуктам\n"
        "🤍 Путеводитель по питанию\n"
        "🤍 Подробное видео с часто задаваемыми вопросами, связанные с питанию и тренировками\n"
    )
    await callback.message.reply_photo(photo=photo_url, caption=caption)
    await callback.message.reply("Выбери тариф:", reply_markup=TARIFF_MENU)
    await callback.answer()

@dp.message(Text(text=["15 дней (1990 ₽)", "1 месяц (3000 ₽)", "3 месяца (6990 ₽)"]))
async def select_tariff(message: Message, state: FSMContext):
    await state.update_data(tariff=message.text)
    await message.reply("Пожалуйста, укажи свой email — я отправлю тебе чек после оплаты:")
    await state.set_state(UserStates.waiting_for_email)

@dp.message(UserStates.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    if "@" in message.text and "." in message.text:
        await state.update_data(email=message.text)
        data = await state.get_data()
        tariff = data['tariff']
        duration = "15 дней" if "15" in tariff else ("1 месяц" if "1" in tariff else "3 месяца")

        payment_msg = (
            f"Поздравляю! Подписка успешно оформлена на **{duration}** 🥳\n\n"
            "Ура! Ты в проекте! Прежде чем начать, давай ообсудим пару организационных моментов⤵️\n\n"
            "1️⃣ Вступи в чат ,где мы общаемся: https://t.me/plans_channel   \n"
            "2️⃣ Активируй чат с Полиной: @your_trainer\n\n"
            "После этого нажми кнопку ниже:"
        )
        await message.reply(payment_msg)
        await message.reply("Продолжить ▶️", reply_markup=AFTER_PAYMENT_MENU)

        # Запись в Google Таблицу
        if SHEET:
            try:
                user = message.from_user
                SHEET.append_row([
                    str(user.id),
                    user.username or "",
                    "",  # имя
                    "",  # рост
                    "",  # вес
                    "",  # калораж
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    data['tariff'],
                    data['email']
                ])
            except Exception as e:
                logger.error(f"Ошибка записи в таблицу: {e}")

        await state.clear()
    else:
        await message.reply("Пожалуйста, введите корректный email (например: polina@mail.ru)")

@dp.message(Text(text="⬅️ Назад"))
@dp.callback_query(F.data == "back")
async def back_menu(item):
    if isinstance(item, Message):
        await item.reply("Выбери, что хочешь узнать:", reply_markup=MAIN_MENU)
    else:
        await item.message.reply("Выбери, что хочешь узнать:", reply_markup=MAIN_MENU)
        await item.answer()

@dp.callback_query(F.data == "reviews")
async def reviews_callback(callback: CallbackQuery):
    review_photos = [
        "https://i.ibb.co/N6yx0vQ/Otziv-foto.jpg",
        "https://i.ibb.co/qLgkfHqk/Otziv-foto-2.jpg",
        "https://i.ibb.co/zWxK49Xb/Otziv-foto-1.jpg",
        "https://i.ibb.co/HD66d5vd/Otziv-1.jpg",
        "https://i.ibb.co/mVrGJPWs/Otziv-2.jpg",
        "https://i.ibb.co/G3B9Fpt3/Otziv-3.jpg",
        "https://i.ibb.co/xSDjZs9F/Otziv-4.jpg",
        "https://i.ibb.co/394skJ6t/Otziv-5.jpg",
        "https://i.ibb.co/ccRXCJ6p/Otziv.jpg"
    ]

    for url in review_photos:
        await callback.message.reply_photo(photo=url)

    await callback.message.reply("Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!")
    await callback.message.reply(
        "Хочешь тоже так? Жми 👇", 
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Тарифы 💰")]], 
            resize_keyboard=True
        )
    )
    await callback.answer()

@dp.message(Text(text="Продолжить ▶️"))
async def continue_after_payment(message: Message):
    instruction = (
        "Дорогая, я рада тебя приветствовать в проекте POLINAFIT, поздравляю,ты на шаг к своему идеальному телу! "
        "Для того,чтобы нам структурировано продолжить работать,давай я расскажу что ты должна сделать:\n"
        "Для начала ты должна мне отправить анкету со всеми твоими данными, она находится в закрытом телеграмм канале, "
        "где собрана вся информация по питанию, важным вопросам, меню, анкеты для отчетов по питанию и форме\n"
        "В этом канале есть вверху закрепленное сообщение под названием «НАВИГАЦИЯ», как только  ты зайдешь в канал, "
        "жми на «НАВИГАЦИЮ» затем на кликабельную кнопку «АНКЕТА ДЛЯ ВСТУПЛЕНИЕ В ПРОЕКТ» тебя перебросит сразу на анкету, "
        "скопируй анкету и вставь её в сообщения в ЛИЧНОМ ЧАТЕ СО МНОЙ, заполни анкету подробно, отправляй её мне и "
        "ВОЗВРАЩАЙСЯ В ЗАКРЫТЫЙ КАНАЛ для изучения всей информации. БОЛЬШАЯ ПРОСЬБА, ИЗУЧАТЬ МАТЕРИАЛ ПОСЛЕДОВАТЕЛЬНО, "
        "просматривать и читать сообщения с верху вниз, так ты не запутаешься и в твоей голове все разложится по полочкам\n"
        "Так же в навигации ты найдешь кликабельные кнопки на анкеты для отчета по питанию и отчета по форме, "
        "которые тебе часто будут нужны"
    )
    await message.reply(instruction)
    await message.reply("Вступай в закрытую группу со всей информацией 🫶🏻\n👉 https://t.me/recipes_group")

@dp.message()
async def unknown(message: Message):
    await message.reply("Пожалуйста, используй кнопки меню.", reply_markup=START_BUTTON)

# === WEBHOOK НАСТРОЙКА ===
async def on_startup(app):
    webhook_url = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )
    logger.info(f"Webhook установлен: {webhook_url}")

async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.session.close()
    logger.info("Webhook удален")

async def main():
    app = web.Application()
    
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
