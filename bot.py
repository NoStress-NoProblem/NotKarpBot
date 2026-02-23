import os
import logging
import gspread
import datetime
import asyncio
import json
import time
import re
import tempfile
import atexit
import queue
import base64
from functools import partial
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# === НАСТРОЙКИ ЛОГИРОВАНИЯ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# === КОНСТАНТЫ ===
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "polinanekarpovaa")  # ИЗМЕНЕНО на @polinanekarpovaa

if not TOKEN:
    logger.error("Переменная BOT_TOKEN не задана!")
    raise ValueError("Переменная BOT_TOKEN не задана!")

if ADMIN_ID == 0:
    logger.warning("ADMIN_ID не настроен! Админ-команды будут недоступны.")

PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# === PAYKEEPER НАСТРОЙКИ ===
PAYKEEPER_SERVER = os.getenv("PAYKEEPER_SERVER", "")
PAYKEEPER_USER = os.getenv("PAYKEEPER_USER", "")
PAYKEEPER_PASSWORD = os.getenv("PAYKEEPER_PASSWORD", "")
PAYKEEPER_SECRET = os.getenv("PAYKEEPER_SECRET", "")

PAYMENTS = {}
PAID_USERS = {}
executor = ThreadPoolExecutor(max_workers=4)
USER_STATES = {}
start_time = time.time()

# СТИКЕРЫ
STICKER_HELLO = "CAACAgIAAxkBAAENWKZnO-3P7h2j3Zz_8dXlKz7Y8F1a9QACPgADr8ZRGrCDrWfHn9g2NgQ"
STICKER_HEART = "CAACAgIAAxkBAAENWKpnO-3QnJ7rX9vK7mHh8W2j4L5r9AACQAADr8ZRGmVJ_w7G1t9zNgQ"
STICKER_WHITE_HEART = "CAACAgIAAxkBAAENWKxnO-3R7h2j3Zz_8dXlKz7Y8F1a9QACPgADr8ZRGrCDrWfHn9g2NgQ"

# === ОЧЕРЕДЬ СООБЩЕНИЙ С ЗАДЕРЖКОЙ 0.5 СЕК ===
class MessageQueue:
    def __init__(self):
        self._queues = defaultdict(queue.Queue)
        self._processing = set()
        self._lock = asyncio.Lock()
        self._last_message_time = defaultdict(float)
        self._rate_limit = 0.5

    async def add(self, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, handler_func):
        async with self._lock:
            self._queues[user_id].put((update, context, handler_func))
            if user_id not in self._processing:
                self._processing.add(user_id)
                asyncio.create_task(self._process_queue(user_id))

    async def _process_queue(self, user_id: int):
        while True:
            async with self._lock:
                q = self._queues[user_id]
                if q.empty():
                    self._processing.discard(user_id)
                    break

            try:
                update, context, handler_func = self._queues[user_id].get_nowait()
                current_time = time.time()
                time_since_last = current_time - self._last_message_time[user_id]
                if time_since_last < self._rate_limit:
                    await asyncio.sleep(self._rate_limit - time_since_last)

                await handler_func(update, context)
                self._last_message_time[user_id] = time.time()
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"Ошибка обработки сообщения для {user_id}: {e}")
                try:
                    if update and update.effective_message:
                        await update.effective_message.reply_text("⚠️ Произошла ошибка при обработке.")
                except:
                    pass

message_queue = MessageQueue()

# === ФУНКЦИИ СОХРАНЕНИЯ ===
def save_states():
    try:
        data = {
            'user_states': USER_STATES, 
            'payments': PAYMENTS, 
            'paid_users': PAID_USERS,
            'reminders_sent': REMINDERS_SENT,
            'expired_notifications_sent': EXPIRED_NOTIFICATIONS_SENT,
            'admin_expiry_notifications_sent': ADMIN_EXPIRY_NOTIFICATIONS_SENT
        }
        with open('user_states.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logger.debug(f"Сохранено {len(USER_STATES)} состояний, {len(PAYMENTS)} платежей")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояний: {e}")

def load_states():
    global USER_STATES, PAYMENTS, PAID_USERS, REMINDERS_SENT, EXPIRED_NOTIFICATIONS_SENT, ADMIN_EXPIRY_NOTIFICATIONS_SENT
    if os.path.exists('user_states.json'):
        try:
            with open('user_states.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            USER_STATES = data.get('user_states', {})
            PAYMENTS = data.get('payments', {})
            PAID_USERS = data.get('paid_users', {})
            REMINDERS_SENT = data.get('reminders_sent', {})
            EXPIRED_NOTIFICATIONS_SENT = data.get('expired_notifications_sent', {})
            ADMIN_EXPIRY_NOTIFICATIONS_SENT = data.get('admin_expiry_notifications_sent', {})
            logger.info(f"Загружено {len(USER_STATES)} состояний, {len(PAYMENTS)} платежей")
        except Exception as e:
            logger.error(f"Ошибка загрузки состояний: {e}")
            USER_STATES, PAYMENTS, PAID_USERS, REMINDERS_SENT, EXPIRED_NOTIFICATIONS_SENT, ADMIN_EXPIRY_NOTIFICATIONS_SENT = {}, {}, {}, {}, {}, {}
    else:
        USER_STATES, PAYMENTS, PAID_USERS, REMINDERS_SENT, EXPIRED_NOTIFICATIONS_SENT, ADMIN_EXPIRY_NOTIFICATIONS_SENT = {}, {}, {}, {}, {}, {}

# === GOOGLE SHEETS ===
def init_google_sheets():
    try:
        google_creds_json = os.getenv("GOOGLE_CREDS_JSON")
        creds_file = None

        if not google_creds_json:
            try:
                with open("credentials.json", "r", encoding="utf-8") as f:
                    google_creds_json = f.read()
            except FileNotFoundError:
                logger.warning("Файл credentials.json не найден, Google Sheets отключен")
                return None

        if google_creds_json.strip().startswith('{'):
            fd, creds_file = tempfile.mkstemp(suffix='.json', prefix='google_creds_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(google_creds_json)

            def cleanup():
                try:
                    if os.path.exists(creds_file):
                        os.unlink(creds_file)
                        logger.debug(f"Временный файл {creds_file} удален")
                except Exception as e:
                    logger.error(f"Ошибка удаления временного файла: {e}")

            atexit.register(cleanup)
        else:
            creds_file = google_creds_json

        SCOPE = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]

        CREDS = ServiceAccountCredentials.from_json_keyfile_name(creds_file, SCOPE)
        CLIENT = gspread.authorize(CREDS)
        SHEET = CLIENT.open("Клиенты фитнес-бота").sheet1

        headers = SHEET.row_values(1)
        expected_headers = ["ID", "Username", "Имя", "Рост", "Вес", "Калораж", "Дата", "Тариф", "Email", "Фамилия Имя", "Номер телефона", "Подписка до", "Статус", "ID платежа"]

        if not headers:
            SHEET.append_row(expected_headers)
            logger.info("Созданы заголовки в таблице")

        logger.info("✅ Успешно подключено к Google Таблице!")
        return SHEET

    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Google Таблице: {e}")
        return None

SHEET = init_google_sheets()

# === PAYKEEPER API ===
class PayKeeperAPI:
    def __init__(self, server: str, user: str, password: str):
        self.server = server
        self.user = user
        self.password = password
        self.base_url = f"https://{server}"

    def get_auth_headers(self):
        credentials = base64.b64encode(f'{self.user}:{self.password}'.encode()).decode()
        return {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': f'Basic {credentials}'
        }

    async def get_token(self):
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f'{self.base_url}/info/settings/token/'
                async with session.get(url, headers=self.get_auth_headers()) as response:
                    data = await response.json()
                    return data.get('token')
        except Exception as e:
            logger.error(f"Ошибка получения токена PayKeeper: {e}")
            return None

    async def create_invoice(self, order_id: str, amount: float, client_email: str, client_id: str = "", service_name: str = ""):
        try:
            token = await self.get_token()
            if not token:
                return {'error': 'Не удалось получить токен'}

            import aiohttp
            payment_data = {
                'pay_amount': amount,
                'orderid': order_id,
                'service_name': service_name,
                'client_email': client_email,
                'clientid': client_id,
                'token': token,
            }

            async with aiohttp.ClientSession() as session:
                url = f'{self.base_url}/change/invoice/preview/'
                async with session.post(url, headers=self.get_auth_headers(), data=payment_data) as response:
                    result = await response.json()
                    if 'invoice_id' in result:
                        result['payment_link'] = f'{self.base_url}/bill/{result["invoice_id"]}/'
                    return result

        except Exception as e:
            logger.error(f"Ошибка создания счета PayKeeper: {e}")
            return {'error': str(e)}

    async def check_payment_status(self, invoice_id: str):
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f'{self.base_url}/info/invoice/byid/?id={invoice_id}'
                async with session.get(url, headers=self.get_auth_headers()) as response:
                    return await response.json()
        except Exception as e:
            logger.error(f"Ошибка проверки статуса: {e}")
            return {'error': str(e)}

paykeeper = None
if PAYKEEPER_SERVER and PAYKEEPER_USER and PAYKEEPER_PASSWORD:
    paykeeper = PayKeeperAPI(PAYKEEPER_SERVER, PAYKEEPER_USER, PAYKEEPER_PASSWORD)
    logger.info("✅ PayKeeper API инициализирован")
else:
    logger.warning("⚠️ PayKeeper не настроен")

# === ВАЛИДАЦИЯ ===
def is_valid_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def is_valid_phone(phone: str) -> bool:
    cleaned = re.sub(r'\D', '', phone)
    return len(cleaned) >= 10 and len(cleaned) <= 15

# === КЛАВИАТУРЫ ===
def get_start_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Хочу в проект 💪", callback_data='want_project')]])

def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Тарифы 💰", callback_data='tariffs')],
        [InlineKeyboardButton("Отзывы 🥹", callback_data='reviews')],
        [InlineKeyboardButton("Подписка 📅", callback_data='my_subscription')],
        [InlineKeyboardButton("Связь со мной 💬", callback_data='contact_me')]
    ])

def get_tariffs_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("3 дня (10 ₽)", callback_data='tariff_3')],
        [InlineKeyboardButton("15 дней (1990 ₽)", callback_data='tariff_15')],
        [InlineKeyboardButton("1 месяц (3000 ₽)", callback_data='tariff_30')],
        [InlineKeyboardButton("3 месяца (6990 ₽)", callback_data='tariff_90')]
    ])

def get_payment_keyboard(payment_url: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Оплатить", url=payment_url)],
        [InlineKeyboardButton("🔄 Проверить оплату", callback_data='check_payment')],
        [InlineKeyboardButton("❌ Отмена", callback_data='cancel')]
    ])

def get_reviews_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Тарифы 💰", callback_data='tariffs')]])

def get_continue_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Продолжить ▶️", callback_data='continue')]])

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data='cancel')]])

def get_renew_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Продлить подписку 💰", callback_data='tariffs')]])

def get_back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Вернуться в меню", callback_data='main_menu')]])

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Пользователь {user.id} ({user.username}) начал диалог")

    context.user_data.clear()

    if str(user.id) in PAID_USERS:
        paid_info = PAID_USERS[str(user.id)]
        await update.message.reply_text(
            f"👋 С возвращением! У вас активная подписка: {paid_info.get('tariff')}\n"
            f"Действует до: {paid_info.get('paid_until', 'неизвестно')}\n\n"
            f"Используйте /project для продолжения работы",
            reply_markup=get_main_menu_keyboard()
        )
        return

    photo_url = "https://i.ibb.co/pr4CxkkM/1.jpg"
    caption = (
        "«POLINAFIT» — место, где ты обретёшь новую версию себя! 💫\n\n"
        "Проект — это не краткосрочный марафон. Это про индивидуальный подход к каждой участнице!\n\n"
        "Я даю рекомендации по питанию, после того как подробно изучу каждый индивидуальный случай, "
        "исходя из вашей ситуации, образа жизни, активности, вида деятельности, возможные травмы. "
        "Именно такой подход поможет тебе достичь поставленной цели!"
    )

    try:
        await update.message.reply_photo(photo=photo_url, caption=caption, reply_markup=get_start_keyboard())
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        await update.message.reply_text(caption, reply_markup=get_start_keyboard())

async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! Режим питания, тренировки, "
        "поддержка от участниц проекта и лично меня! Это то место, где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, и ты не откатишься назад даже при непредвиденных обстоятельствах "
        "(отпуск, стресс, травмы, болезнь и т.д.)"
    )

    await update.message.reply_text(desc)
    await asyncio.sleep(0.5)

    features = (
        "Что входит в проект:\n\n"
        "🤍 Тренировки для любого уровня подготовки дома или в зале:\n"
        "— лёгкие, для тех кто только начинает\n"
        "— средней сложности, для тех кто уже занимается\n"
        "— интенсивные, для тех кто тренируется регулярно и хочет прогрессировать\n\n"
        "🤍 Питание:\n"
        "Индивидуальный расчёт КБЖУ, исходя из ваших особенностей, активности и образа жизни. "
        "Анализ динамики и изменения расчёта по необходимости. Большие сборники завтраков, обедов и ужинов "
        "с указанием КБЖУ каждого блюда.\n\n"
        "🤍 Индивидуальная работа с отчётами:\n"
        "— 2 раза в неделю проверяю отчёты по питанию, вношу корректировки для эффективного результата\n"
        "— 2 раза в месяц проверяю отчёты по форме, фиксируем замеры для коррекции плана тренировок или КБЖУ\n\n"
        "🤍 Абсолютно любая цель:\n"
        "— снижение веса\n"
        "— набор мышечной массы\n\n"
        "🤍 Доступ к закрытому чату со всеми участницами проекта:\n"
        "Обсуждаем результаты, делимся эмоциями и рецептами, поддерживаем друг друга каждый день! "
        "Ты всегда можешь задать мне любой вопрос. Важно знать, что ты не одна — тебя всегда поддержат! 🫂"
    )

    await update.message.reply_text(features)
    await asyncio.sleep(0.5)
    await update.message.reply_text("Выбери, что хочешь узнать:", reply_markup=get_main_menu_keyboard())

async def send_project_info(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! Режим питания, тренировки, "
        "поддержка от участниц проекта и лично меня! Это то место, где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, и ты не откатишься назад даже при непредвиденных обстоятельствах "
        "(отпуск, стресс, травмы, болезнь и т.д.)"
    )

    await context.bot.send_message(chat_id=chat_id, text=desc)
    await asyncio.sleep(0.5)

    features = (
        "Что входит в проект:\n\n"
        "🤍 Тренировки для любого уровня подготовки дома или в зале:\n"
        "— лёгкие, для тех кто только начинает\n"
        "— средней сложности, для тех кто уже занимается\n"
        "— интенсивные, для тех кто тренируется регулярно и хочет прогрессировать\n\n"
        "🤍 Питание:\n"
        "Индивидуальный расчёт КБЖУ, исходя из ваших особенностей, активности и образа жизни. "
        "Анализ динамики и изменения расчёта по необходимости. Большие сборники завтраков, обедов и ужинов "
        "с указанием КБЖУ каждого блюда.\n\n"
        "🤍 Индивидуальная работа с отчётами:\n"
        "— 2 раза в неделю проверяю отчёты по питанию, вношу корректировки для эффективного результата\n"
        "— 2 раза в месяц проверяю отчёты по форме, фиксируем замеры для коррекции плана тренировок или КБЖУ\n\n"
        "🤍 Абсолютно любая цель:\n"
        "— снижение веса\n"
        "— набор мышечной массы\n\n"
        "🤍 Доступ к закрытому чату со всеми участницами проекта:\n"
        "Обсуждаем результаты, делимся эмоциями и рецептами, поддерживаем друг друга каждый день! "
        "Ты всегда можешь задать мне любой вопрос. Важно знать, что ты не одна — тебя всегда поддержат! 🫂"
    )

    await context.bot.send_message(chat_id=chat_id, text=features)
    await asyncio.sleep(0.5)
    await context.bot.send_message(chat_id=chat_id, text="Выбери, что хочешь узнать:", reply_markup=get_main_menu_keyboard())

async def send_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = (
        "В проекте действует подписка, которая открывает тебе доступ к следующим преимуществам:\n\n"
        "🤍 Анализ состояния для подбора питания и тренировок\n"
        "🤍 Индивидуальный расчет КБЖУ и план тренировок, составленный лично мной\n"
        "🤍 Тренировки на любую цель (жиросжигание, силовые и т.п.)\n"
        "🤍 Возможность тренироваться где удобно — дома или в зале\n"
        "🤍 Подробно расписанная техника каждого упражнения\n"
        "🤍 Контроль питания и формы каждую неделю\n"
        "🤍 Общий чат с участницами проекта для поддержки и обмена опытом\n"
        "🤍 Возможность задавать любые вопросы по теме питания и тренировок\n"
        "🤍 Огромный сборник простых и бюджетных рецептов с КБЖУ\n"
        "🤍 Гайд по продуктам и путеводитель по питанию\n"
        "🤍 Видео с ответами на частые вопросы"
    )

    try:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=photo_url,
            caption=caption,
            reply_markup=get_tariffs_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки фото тарифов: {e}")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=caption,
            reply_markup=get_tariffs_keyboard()
        )

async def tariffs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = (
        "В проекте действует подписка, которая открывает тебе доступ к следующим преимуществам:\n\n"
        "🤍 Анализ состояния для подбора питания и тренировок\n"
        "🤍 Индивидуальный расчет КБЖУ и план тренировок, составленный лично мной\n"
        "🤍 Тренировки на любую цель (жиросжигание, силовые и т.п.)\n"
        "🤍 Возможность тренироваться где удобно — дома или в зале\n"
        "🤍 Подробно расписанная техника каждого упражнения\n"
        "🤍 Контроль питания и формы каждую неделю\n"
        "🤍 Общий чат с участницами проекта для поддержки и обмена опытом\n"
        "🤍 Возможность задавать любые вопросы по теме питания и тренировок\n"
        "🤍 Огромный сборник простых и бюджетных рецептов с КБЖУ\n"
        "🤍 Гайд по продуктам и путеводитель по питанию\n"
        "🤍 Видео с ответами на частые вопросы"
    )

    try:
        await update.message.reply_photo(photo=photo_url, caption=caption, reply_markup=get_tariffs_keyboard())
    except Exception as e:
        logger.error(f"Ошибка отправки фото тарифов: {e}")
        await update.message.reply_text(caption, reply_markup=get_tariffs_keyboard())

async def send_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    review_photos = [
        "https://i.ibb.co/N6yx0vQ7/Otziv-foto.jpg",
        "https://i.ibb.co/qLgkfHqk/Otziv-foto-2.jpg",
        "https://i.ibb.co/zWxK49Xb/Otziv-foto-1.jpg",
        "https://i.ibb.co/HD66d5vd/Otziv-1.jpg",
        "https://i.ibb.co/mVrGJPWs/Otziv-2.jpg",
        "https://i.ibb.co/G3B9Fpt3/Otziv-3.jpg",
        "https://i.ibb.co/xSDjZs9F/Otziv-4.jpg",
        "https://i.ibb.co/394skJ6t/Otziv-5.jpg",
        "https://i.ibb.co/ccRXCJ6p/Otziv.jpg"
    ]

    for i, url in enumerate(review_photos[:5]):
        try:
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=url)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка отправки отзыва {i+1}: {e}")
            continue

    await asyncio.sleep(0.5)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!\n"
             "Хочешь тоже так? Жми 👇",
        reply_markup=get_reviews_keyboard()
    )

async def reviews_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    review_photos = [
        "https://i.ibb.co/N6yx0vQ7/Otziv-foto.jpg",
        "https://i.ibb.co/qLgkfHqk/Otziv-foto-2.jpg",
        "https://i.ibb.co/zWxK49Xb/Otziv-foto-1.jpg",
        "https://i.ibb.co/HD66d5vd/Otziv-1.jpg",
        "https://i.ibb.co/mVrGJPWs/Otziv-2.jpg",
        "https://i.ibb.co/G3B9Fpt3/Otziv-3.jpg",
        "https://i.ibb.co/xSDjZs9F/Otziv-4.jpg",
        "https://i.ibb.co/394skJ6t/Otziv-5.jpg",
        "https://i.ibb.co/ccRXCJ6p/Otziv.jpg"
    ]

    for i, url in enumerate(review_photos[:5]):
        try:
            await update.message.reply_photo(photo=url)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка отправки отзыва {i+1}: {e}")
            continue

    await asyncio.sleep(0.5)
    await update.message.reply_text(
        "Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!\n"
        "Хочешь тоже так? Жми 👇",
        reply_markup=get_reviews_keyboard()
    )

# === НОВЫЕ ФУНКЦИИ: ПОДПИСКА И СВЯЗЬ ===
async def send_my_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает информацию о текущей подписке пользователя"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    
    if user_id in PAID_USERS:
        user_data = PAID_USERS[user_id]
        tariff = user_data.get('tariff', 'Неизвестен')
        paid_until = user_data.get('paid_until', 'Неизвестно')
        
        # Вычисляем сколько дней осталось
        try:
            expiry_date = datetime.datetime.strptime(paid_until, '%Y-%m-%d')
            today = datetime.datetime.now()
            days_left = (expiry_date - today).days
            
            if days_left < 0:
                days_text = "❌ Подписка истекла"
            elif days_left == 0:
                days_text = "⏳ Истекает сегодня"
            else:
                days_text = f"⏳ Осталось дней: {days_left}"
            
            message = (
                f"📅 Информация о вашей подписке:\n\n"
                f"Тариф: {tariff}\n"
                f"Действует до: {paid_until}\n"
                f"{days_text}\n\n"
                f"Хочешь продлить подписку? 👇"
            )
            
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=message,
                reply_markup=get_renew_keyboard()
            )
        except Exception as e:
            logger.error(f"Ошибка вычисления дней подписки: {e}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"📅 Ваша подписка:\n\nТариф: {tariff}\nДействует до: {paid_until}",
                reply_markup=get_renew_keyboard()
            )
    else:
        message = (
            f"📅 У вас нет активной подписки.\n\n"
            f"Хотите оформить подписку и присоединиться к проекту POLINAFIT? 💪"
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message,
            reply_markup=get_tariffs_keyboard()
        )

async def send_contact_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет информацию для связи с админом"""
    query = update.callback_query
    await query.answer()
    
    message = (
        f"Это ссылка на мой ТГ @{ADMIN_USERNAME} 💬\n\n"
        f"Можешь задать мне любой вопрос, с радостью на него отвечу! 🤍"
    )
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=message,
        reply_markup=get_back_to_menu_keyboard()
    )

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает в главное меню"""
    query = update.callback_query
    await query.answer()
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Главное меню:",
        reply_markup=get_main_menu_keyboard()
    )

# === PAYKEEPER ОБРАБОТЧИКИ ===
async def handle_tariff_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, tariff_data: str):
    query = update.callback_query
    await query.answer()

    tariff_map = {
        'tariff_3': {'name': '3 дня (10 ₽)', 'price': 10, 'days': 3},
        'tariff_15': {'name': '15 дней (1990 ₽)', 'price': 1990, 'days': 15},
        'tariff_30': {'name': '1 месяц (3000 ₽)', 'price': 3000, 'days': 30},
        'tariff_90': {'name': '3 месяца (6990 ₽)', 'price': 6990, 'days': 90}
    }

    tariff_info = tariff_map.get(tariff_data)
    if not tariff_info:
        await query.edit_message_text("Ошибка: тариф не найден")
        return

    context.user_data['tariff'] = tariff_info
    context.user_data['payment_step'] = 'waiting_fullname'

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Вы выбрали: {tariff_info['name']}\n\nПожалуйста, укажи свою Фамилию и Имя (пример: Иванова Светлана):",
        reply_markup=get_cancel_keyboard()
    )

async def handle_fullname_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fullname = update.message.text.strip()
    
    if len(fullname.split()) < 2:
        await update.message.reply_text(
            "⚠️ Пожалуйста, введи Фамилию и Имя полностью (пример: Иванова Светлана):",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    context.user_data['fullname'] = fullname
    context.user_data['payment_step'] = 'waiting_phone'
    
    await update.message.reply_text(
        "Отлично! Теперь укажи номер телефона (пример: 89105441100):",
        reply_markup=get_cancel_keyboard()
    )

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    
    if not is_valid_phone(phone):
        await update.message.reply_text(
            "📱 Некорректный номер телефона. Пожалуйста, введи номер в формате: 89105441100\n\n"
            "Попробуй ещё раз:",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    context.user_data['phone'] = phone
    context.user_data['payment_step'] = 'waiting_email'
    
    await update.message.reply_text(
        "Отлично! Теперь укажи свой email — я отправлю тебе чек после оплаты:",
        reply_markup=get_cancel_keyboard()
    )

async def create_paykeeper_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    email = update.message.text.strip()

    if not is_valid_email(email):
        await update.message.reply_text(
            "📧 Некорректный email. Пример правильного формата:\n"
            "`example@mail.ru` или `name@gmail.com`\n\n"
            "Попробуй ещё раз:",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard()
        )
        return

    tariff_info = context.user_data.get('tariff')
    if not tariff_info:
        await update.message.reply_text("Ошибка: тариф не выбран. Начните с /start")
        return

    if not paykeeper:
        await update.message.reply_text(
            "⚠️ Система оплаты временно недоступна.\nПожалуйста, свяжитесь с @polinanekarpovaa",
            reply_markup=get_start_keyboard()
        )
        return

    fullname = context.user_data.get('fullname', '')
    phone = context.user_data.get('phone', '')

    order_id = f"POLI_{user.id}_{int(time.time())}"

    result = await paykeeper.create_invoice(
        order_id=order_id,
        amount=tariff_info['price'],
        client_email=email,
        client_id=fullname,
        service_name=f"Подписка POLINAFIT - {tariff_info['name']}"
    )

    if 'error' in result or 'invoice_id' not in result:
        logger.error(f"Ошибка создания платежа: {result.get('error', 'Нет invoice_id')}")
        await update.message.reply_text(
            "❌ Не удалось создать счет. Попробуйте позже или свяжитесь с поддержкой.",
            reply_markup=get_start_keyboard()
        )
        return

    payment_info = {
        'user_id': user.id,
        'username': user.username,
        'fullname': fullname,
        'phone': phone,
        'email': email,
        'tariff': tariff_info['name'],
        'amount': tariff_info['price'],
        'days': tariff_info['days'],
        'order_id': order_id,
        'invoice_id': result['invoice_id'],
        'payment_link': result['payment_link'],
        'status': 'created',
        'created_at': datetime.datetime.now().isoformat()
    }

    PAYMENTS[order_id] = payment_info
    context.user_data['current_order_id'] = order_id
    context.user_data['payment_step'] = 'waiting_payment'
    save_states()

    payment_text = (
        f"💳 **Счет на оплату создан**\n\n"
        f"Тариф: {tariff_info['name']}\n"
        f"Сумма: {tariff_info['price']} ₽\n"
        f"Фамилия Имя: {fullname}\n"
        f"Телефон: {phone}\n"
        f"Email: {email}\n\n"
        f"Нажмите кнопку ниже для оплаты. После оплаты нажмите \"Проверить оплату\":"
    )

    await update.message.reply_text(
        payment_text,
        parse_mode="Markdown",
        reply_markup=get_payment_keyboard(result['payment_link'])
    )

async def check_payment_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Проверяем статус...")

    order_id = context.user_data.get('current_order_id')
    if not order_id or order_id not in PAYMENTS:
        await query.edit_message_text(
            "❌ Информация о платеже не найдена. Начните сначала с /start",
            reply_markup=get_start_keyboard()
        )
        return

    payment_info = PAYMENTS[order_id]

    if not paykeeper:
        await query.edit_message_text("⚠️ Система проверки недоступна")
        return

    status_result = await paykeeper.check_payment_status(payment_info['invoice_id'])

    if 'error' in status_result:
        await query.edit_message_text(
            "❌ Ошибка проверки. Попробуйте позже.",
            reply_markup=get_payment_keyboard(payment_info['payment_link'])
        )
        return

    status = status_result.get('status', 'unknown')

    if status == 'paid':
        await process_successful_payment(order_id, payment_info, query, context)
    else:
        await query.edit_message_text(
            f"⏳ **Статус: Ожидание оплаты**\n\n"
            f"Платеж еще не поступил.\n"
            f"Если вы уже оплатили, подождите 1-2 минуты и проверьте снова.\n\n"
            f"Если возникли проблемы, напишите @polinanekarpovaa",
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(payment_info['payment_link'])
        )

async def process_successful_payment(order_id: str, payment_info: dict, query, context: ContextTypes.DEFAULT_TYPE):
    user_id = payment_info['user_id']

    PAYMENTS[order_id]['status'] = 'paid'
    PAYMENTS[order_id]['paid_at'] = datetime.datetime.now().isoformat()

    days = payment_info.get('days', 30)
    paid_until = (datetime.datetime.now() + datetime.timedelta(days=days)).strftime('%Y-%m-%d')

    PAID_USERS[str(user_id)] = {
        'tariff': payment_info['tariff'],
        'paid_until': paid_until,
        'payment_id': order_id,
        'email': payment_info['email'],
        'fullname': payment_info.get('fullname', ''),
        'phone': payment_info.get('phone', ''),
        'username': payment_info.get('username', '')
    }

    save_states()

    if SHEET:
        try:
            row_data = [
                str(user_id),
                payment_info.get('username', ''),
                '',
                '',
                '',
                '',
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                payment_info['tariff'],
                payment_info['email'],
                payment_info.get('fullname', ''),
                payment_info.get('phone', ''),
                paid_until,
                'ОПЛАЧЕНО',
                order_id
            ]
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(executor, partial(SHEET.append_row, row_data))
        except Exception as e:
            logger.error(f"Ошибка сохранения в Google Sheets: {e}")

    success_text = (
        f"🎉 **ОПЛАТА ПРОШЛА УСПЕШНО!**\n\n"
        f"Тариф: {payment_info['tariff']}\n"
        f"Сумма: {payment_info['amount']} ₽\n"
        f"Подписка активна до: {paid_until}\n\n"
        f"Добро пожаловать в POLINAFIT! 💪\n"
        f"Нажмите \"Продолжить\" для инструкций:"
    )

    await query.edit_message_text(
        success_text,
        parse_mode="Markdown",
        reply_markup=get_continue_keyboard()
    )

    await asyncio.sleep(0.5)
    
    receipt_text = (
        f"Если тебе не пришел чек, напиши мне @{ADMIN_USERNAME} и я тебе его отправлю в лс 🤍"
    )
    
    await context.bot.send_message(
        chat_id=user_id,
        text=receipt_text
    )
    
    await asyncio.sleep(0.5)
    try:
        await context.bot.send_sticker(chat_id=user_id, sticker=STICKER_WHITE_HEART)
    except Exception as e:
        logger.error(f"Ошибка отправки стикера белого сердца: {e}")

    if ADMIN_ID:
        try:
            admin_text = (
                f"💰 Новая оплата!\n\n"
                f"Тариф: {payment_info['tariff']}\n"
                f"Сумма: {payment_info['amount']} ₽\n"
                f"Логин: @{payment_info.get('username', 'N/A')}\n"
                f"Фамилия Имя: {payment_info.get('fullname', 'N/A')}\n"
                f"Телефон: {payment_info.get('phone', 'N/A')}\n"
                f"Email: {payment_info.get('email', 'N/A')}"
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text)
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления админу: {e}")

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data.clear()

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Действие отменено. Что хочешь сделать?",
        reply_markup=get_main_menu_keyboard()
    )

async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    instruction = (
        "Дорогая, я рада тебя приветствовать в проекте POLINAFIT 🥳\n"
        "Поздравляю, ты на шаг к своему идеальному телу! ✨\n\n"
        "Для того, чтобы нам структурировано продолжить работать, вот что нужно сделать:\n\n"
        "1️⃣ Зайди в закрытый канал с материалами проекта: https://t.me/+UZosO3IIMoI4MDYy\n"
        "2️⃣ Нажми на закреплённое сообщение «НАВИГАЦИЯ»\n"
        "3️⃣ Перейди по кнопке «АНКЕТА ДЛЯ ВСТУПЛЕНИЯ В ПРОЕКТ»\n"
        "4️⃣ Скопируй анкету и вставь её в ЛИЧНЫЙ ЧАТ со мной (@polinanekarpovaa)\n"
        "5️⃣ Заполни анкету подробно и отправь мне\n"
        "6️⃣ Вернись в закрытый канал и изучай материалы последовательно (сверху вниз)\n\n"
        "❗️В навигации также есть кнопки для отчётов по питанию и форме — они тебе понадобятся регулярно.\n\n"
        "ЕСЛИ ТЫ ВСЁ ПОНЯЛА, НАЖМИ «ПРОДОЛЖИТЬ» 👇"
    )

    await context.bot.send_message(chat_id=query.message.chat_id, text=instruction)
    await asyncio.sleep(0.5)

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Вступай в закрытую группу со всей информацией 🫶🏻\n👉 https://t.me/+Jbb_WAbbePM2Mzky",
        reply_markup=get_continue_keyboard()
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    handlers = {
        'want_project': lambda u, c: send_project_info(c, query.message.chat_id),
        'tariffs': send_tariffs,
        'reviews': send_reviews,
        'my_subscription': send_my_subscription,
        'contact_me': send_contact_info,
        'main_menu': send_main_menu,
        'tariff_3': lambda u, c: handle_tariff_selection(u, c, 'tariff_3'),
        'tariff_15': lambda u, c: handle_tariff_selection(u, c, 'tariff_15'),
        'tariff_30': lambda u, c: handle_tariff_selection(u, c, 'tariff_30'),
        'tariff_90': lambda u, c: handle_tariff_selection(u, c, 'tariff_90'),
        'check_payment': check_payment_status_handler,
        'cancel': handle_cancel,
        'continue': handle_continue
    }

    handler = handlers.get(data)
    if handler:
        await handler(update, context)
    else:
        await query.answer(f"Неизвестная команда: {data}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment_step = context.user_data.get('payment_step')

    if payment_step == 'waiting_fullname':
        await handle_fullname_input(update, context)
        return
    elif payment_step == 'waiting_phone':
        await handle_phone_input(update, context)
        return
    elif payment_step == 'waiting_email':
        await create_paykeeper_payment(update, context)
        return

    text = update.message.text.lower()

    command_map = {
        '/start': start,
        '/project': project_command,
        '/tariffs': tariffs_command,
        '/reviews': reviews_command
    }

    if text in command_map:
        await command_map[text](update, context)
    else:
        await update.message.reply_text(
            "Я не понял ваше сообщение. Используйте команды меню или кнопки ниже:",
            reply_markup=get_start_keyboard()
        )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID == 0 or update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Эта команда только для администратора.")
        return

    total_payments = len(PAYMENTS)
    paid_payments = len([p for p in PAYMENTS.values() if p['status'] == 'paid'])
    total_amount = sum([p['amount'] for p in PAYMENTS.values() if p['status'] == 'paid'])

    stats_text = (
        f"📊 **Статистика бота:**\n\n"
        f"💰 Всего платежей: {total_payments}\n"
        f"✅ Оплачено: {paid_payments}\n"
        f"💵 Общая сумма: {total_amount} ₽\n"
        f"👥 Активных подписок: {len(PAID_USERS)}"
    )

    await update.message.reply_text(stats_text, parse_mode="Markdown")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка при обработке обновления: {context.error}", exc_info=True)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла временная ошибка. Пожалуйста, попробуйте снова через несколько секунд.",
                reply_markup=get_start_keyboard()
            )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

# === СИСТЕМА НАПОМИНАНИЙ О ПОДПИСКЕ ===
REMINDERS_SENT = {}
EXPIRED_NOTIFICATIONS_SENT = {}
ADMIN_EXPIRY_NOTIFICATIONS_SENT = {}
EXPIRED_USERS_DATA = {}

async def check_subscriptions_reminders(bot):
    """Проверка и отправка напоминаний о подписке"""
    while True:
        try:
            now = datetime.datetime.now()
            
            for user_id_str, user_data in list(PAID_USERS.items()):
                try:
                    paid_until = datetime.datetime.strptime(user_data['paid_until'], '%Y-%m-%d')
                    paid_until_end = paid_until.replace(hour=23, minute=59, second=59)
                    
                    reminder_time = paid_until_end - datetime.timedelta(days=1)
                    time_diff = (now - reminder_time).total_seconds()
                    if 0 <= time_diff <= 3600:
                        if user_id_str not in REMINDERS_SENT:
                            await send_reminder(bot, int(user_id_str), user_data, paid_until)
                            REMINDERS_SENT[user_id_str] = now.isoformat()
                            save_states()
                    
                    expiry_notification_time = paid_until_end + datetime.timedelta(minutes=5)
                    expiry_diff = (now - expiry_notification_time).total_seconds()
                    
                    if expiry_diff >= 0 and expiry_diff <= 3600:
                        if user_id_str not in EXPIRED_NOTIFICATIONS_SENT:
                            EXPIRED_USERS_DATA[user_id_str] = user_data.copy()
                            
                            if user_id_str not in PAID_USERS or PAID_USERS[user_id_str]['paid_until'] == user_data['paid_until']:
                                await send_expiry_notification(bot, int(user_id_str), user_data)
                                EXPIRED_NOTIFICATIONS_SENT[user_id_str] = now.isoformat()
                                save_states()
                                if user_id_str in PAID_USERS:
                                    del PAID_USERS[user_id_str]
                                    save_states()
                    
                    admin_notification_time = paid_until_end + datetime.timedelta(minutes=7)
                    admin_diff = (now - admin_notification_time).total_seconds()
                    
                    if admin_diff >= 0 and admin_diff <= 3600:
                        if user_id_str not in ADMIN_EXPIRY_NOTIFICATIONS_SENT:
                            current_data = PAID_USERS.get(user_id_str)
                            is_expired = True
                            
                            if current_data and current_data['paid_until'] != user_data['paid_until']:
                                is_expired = False
                            
                            if is_expired:
                                notify_data = EXPIRED_USERS_DATA.get(user_id_str, user_data)
                                await send_admin_expiry_notification(bot, int(user_id_str), notify_data)
                                ADMIN_EXPIRY_NOTIFICATIONS_SENT[user_id_str] = now.isoformat()
                                save_states()
                                if user_id_str in EXPIRED_USERS_DATA:
                                    del EXPIRED_USERS_DATA[user_id_str]
                    
                except Exception as e:
                    logger.error(f"Ошибка проверки подписки для {user_id_str}: {e}")
                    continue
            
            await asyncio.sleep(300)
            
        except Exception as e:
            logger.error(f"Ошибка в цикле напоминаний: {e}")
            await asyncio.sleep(300)

async def send_reminder(bot, user_id: int, user_data: dict, paid_until: datetime.datetime):
    """Отправка напоминания за 1 день до окончания подписки"""
    try:
        await bot.send_sticker(chat_id=user_id, sticker=STICKER_HELLO)
        await asyncio.sleep(0.5)
        
        expiry_datetime = paid_until.strftime('%d.%m.%Y в 23:59')
        
        message = (
            f"Привет, подружка! 👋\n\n"
            f"Завтра ({expiry_datetime}) у тебя заканчивается подписка в проекте POLINAFIT, "
            f"надеюсь у тебя все хорошо и ты остаешься 💪"
        )
        
        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=get_renew_keyboard()
        )
        
        logger.info(f"Отправлено напоминание о подписке пользователю {user_id}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания пользователю {user_id}: {e}")

async def send_expiry_notification(bot, user_id: int, user_data: dict):
    """Отправка уведомления об окончании подписки"""
    try:
        await bot.send_sticker(chat_id=user_id, sticker=STICKER_HELLO)
        await asyncio.sleep(0.5)
        
        message = (
            f"Привет, подружка! 👋\n\n"
            f"Жаль, что ты не продлила подписку, доступ к каналу и информации будет приостановлен. "
            f"Рада была с тобой работать ❤️"
        )
        
        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=get_renew_keyboard()
        )
        await asyncio.sleep(0.5)
        
        await bot.send_sticker(chat_id=user_id, sticker=STICKER_HEART)
        
        logger.info(f"Отправлено уведомление об окончании подписки пользователю {user_id}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления об окончании пользователю {user_id}: {e}")

async def send_admin_expiry_notification(bot, user_id: int, user_data: dict):
    """Отправка уведомления админу о непродленной подписке"""
    try:
        if not ADMIN_ID:
            logger.warning("ADMIN_ID не настроен, уведомление админу не отправлено")
            return
        
        fullname = user_data.get('fullname', 'Не указано')
        username = user_data.get('username', 'Не указан')
        phone = user_data.get('phone', 'Не указан')
        tariff = user_data.get('tariff', 'Неизвестен')
        
        admin_text = (
            f"⚠️ Пользователь не продлил подписку!\n\n"
            f"Фамилия Имя: {fullname}\n"
            f"Telegram ID: {user_id}\n"
            f"Никнейм: @{username}\n"
            f"Номер телефона: {phone}\n"
            f"Тариф который оформлял (истек): {tariff}"
        )
        
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text)
        logger.info(f"Отправлено уведомление админу о непродленной подписке пользователя {user_id}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу о непродленной подписке: {e}")

# === AIOHTTP WEB SERVER С HEALTH CHECK ===
async def health_handler(request):
    return web.Response(text="OK", status=200)

async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        return web.Response(status=500)

async def paykeeper_webhook_handler(request):
    try:
        data = await request.post()

        if PAYKEEPER_SECRET:
            if data.get('secret') != PAYKEEPER_SECRET:
                logger.warning("Неверное секретное слово в webhook!")
                return web.Response(status=403)

        order_id = data.get('orderid', '')
        status = data.get('status', '')

        if order_id in PAYMENTS and status == 'paid':
            PAYMENTS[order_id]['status'] = 'paid'
            payment_info = PAYMENTS[order_id]
            user_id = payment_info['user_id']
            days = payment_info.get('days', 30)
            paid_until = (datetime.datetime.now() + datetime.timedelta(days=days)).strftime('%Y-%m-%d')

            PAID_USERS[str(user_id)] = {
                'tariff': payment_info['tariff'],
                'paid_until': paid_until,
                'payment_id': order_id,
                'email': payment_info['email'],
                'fullname': payment_info.get('fullname', ''),
                'phone': payment_info.get('phone', ''),
                'username': payment_info.get('username', '')
            }
            save_states()
            logger.info(f"✅ Webhook активация подписки для user_id={user_id}")

        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Ошибка PayKeeper webhook: {e}")
        return web.Response(status=500)

async def run_web_server():
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_post(f'/{TOKEN}', webhook_handler)
    app.router.add_post('/paykeeper/webhook', paykeeper_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"✅ Web сервер запущен на порту {PORT}")
    logger.info(f"✅ Health check: http://0.0.0.0:{PORT}/health")

# === WRAPPERS ===
async def queued_start(update, context):
    await message_queue.add(update.effective_user.id, update, context, start)

async def queued_project(update, context):
    await message_queue.add(update.effective_user.id, update, context, project_command)

async def queued_tariffs(update, context):
    await message_queue.add(update.effective_user.id, update, context, tariffs_command)

async def queued_reviews(update, context):
    await message_queue.add(update.effective_user.id, update, context, reviews_command)

async def queued_stats(update, context):
    await message_queue.add(update.effective_user.id, update, context, admin_stats)

async def queued_callback(update, context):
    await message_queue.add(update.effective_user.id, update, context, handle_callback_query)

async def queued_message(update, context):
    await message_queue.add(update.effective_user.id, update, context, handle_message)

# === ГЛОБАЛЬНАЯ ПЕРЕМЕННАЯ ===
application = None

# === ЗАПУСК ===
async def main():
    global application

    load_states()

    logger.info("=" * 60)
    logger.info("🤖 POLINAFIT Bot с Health Check и PayKeeper")
    logger.info(f"PayKeeper: {'✅' if paykeeper else '❌'} {PAYKEEPER_SERVER}")
    logger.info("=" * 60)

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", queued_start))
    application.add_handler(CommandHandler("project", queued_project))
    application.add_handler(CommandHandler("tariffs", queued_tariffs))
    application.add_handler(CommandHandler("reviews", queued_reviews))
    application.add_handler(CommandHandler("stats", queued_stats))
    application.add_handler(CallbackQueryHandler(queued_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queued_message))
    application.add_error_handler(error_handler)

    await application.bot.set_my_commands([
        BotCommand("start", "Начать работу"),
        BotCommand("project", "Описание проекта"),
        BotCommand("tariffs", "Тарифы"),
        BotCommand("reviews", "Отзывы"),
        BotCommand("stats", "Статистика (админ)")
    ])

    await run_web_server()
    asyncio.create_task(check_subscriptions_reminders(application.bot))

    async with application:
        await application.start()

        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
            await application.bot.set_webhook(url=webhook_url)
            logger.info(f"✅ Webhook установлен: {webhook_url}")

        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())