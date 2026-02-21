
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

# Хранилище платежей
PAYMENTS = {}
PAID_USERS = {}

# ThreadPool для блокирующих операций
executor = ThreadPoolExecutor(max_workers=4)

# Состояния пользователя
USER_STATES = {}
start_time = time.time()

# === ОЧЕРЕДЬ СООБЩЕНИЙ С ЗАДЕРЖКОЙ 0.5 СЕК ===
class MessageQueue:
    """Очередь сообщений с фиксированной задержкой 0.5 сек между сообщениями"""

    def __init__(self):
        self._queues = defaultdict(queue.Queue)
        self._processing = set()
        self._lock = asyncio.Lock()
        self._last_message_time = defaultdict(float)
        self._rate_limit = 0.5  # ЗАДЕРЖКА 0.5 СЕКУНДЫ

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

                # ЗАДЕРЖКА 0.5 СЕКУНДЫ между сообщениями
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
            'paid_users': PAID_USERS
        }
        with open('user_states.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"Сохранено {len(USER_STATES)} состояний, {len(PAYMENTS)} платежей")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def load_states():
    global USER_STATES, PAYMENTS, PAID_USERS
    if os.path.exists('user_states.json'):
        try:
            with open('user_states.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            USER_STATES = data.get('user_states', {})
            PAYMENTS = data.get('payments', {})
            PAID_USERS = data.get('paid_users', {})
            logger.info(f"Загружено {len(USER_STATES)} состояний, {len(PAYMENTS)} платежей")
        except Exception as e:
            logger.error(f"Ошибка загрузки: {e}")
            USER_STATES, PAYMENTS, PAID_USERS = {}, {}, {}
    else:
        USER_STATES, PAYMENTS, PAID_USERS = {}, {}, {}

# === GOOGLE ТАБЛИЦА ===
def init_google_sheets():
    try:
        google_creds_json = os.getenv("GOOGLE_CREDS_JSON")
        creds_file = None

        if not google_creds_json:
            try:
                with open("credentials.json", "r", encoding="utf-8") as f:
                    google_creds_json = f.read()
            except FileNotFoundError:
                logger.warning("Google Sheets отключен")
                return None

        if google_creds_json.strip().startswith('{'):
            fd, creds_file = tempfile.mkstemp(suffix='.json', prefix='google_creds_')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(google_creds_json)
            except:
                os.close(fd)
                raise

            def cleanup():
                try:
                    if os.path.exists(creds_file):
                        os.unlink(creds_file)
                except:
                    pass

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
        expected_headers = ["ID", "Username", "Имя", "Рост", "Вес", "Калораж", "Дата", "Тариф", "Email", "Статус оплаты", "ID платежа"]

        if not headers:
            SHEET.append_row(expected_headers)
            logger.info("Созданы заголовки в таблице")

        logger.info("✅ Google Таблица подключена!")
        return SHEET

    except Exception as e:
        logger.error(f"❌ Ошибка Google Таблицы: {e}")
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
            logger.error(f"Ошибка токена PayKeeper: {e}")
            return None

    async def create_invoice(self, order_id: str, amount: float, client_email: str, 
                           client_phone: str = "", service_name: str = "", 
                           client_id: str = "") -> dict:
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
                'client_phone': client_phone,
                'clientid': client_id,
                'token': token,
            }

            async with aiohttp.ClientSession() as session:
                url = f'{self.base_url}/change/invoice/preview/'
                async with session.post(url, headers=self.get_auth_headers(), 
                                       data=payment_data) as response:
                    result = await response.json()

                    if 'invoice_id' in result:
                        result['payment_link'] = f'{self.base_url}/bill/{result["invoice_id"]}/'

                    return result

        except Exception as e:
            logger.error(f"Ошибка создания счета: {e}")
            return {'error': str(e)}

    async def check_payment_status(self, invoice_id: str) -> dict:
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

# === ВАЛИДАЦИЯ EMAIL ===
def is_valid_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

# === КЛАВИАТУРЫ ===
def get_start_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Хочу в проект 💪", callback_data='want_project')]])

def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Тарифы 💰", callback_data='tariffs')],
        [InlineKeyboardButton("Отзывы 🥹", callback_data='reviews')]
    ])

def get_tariffs_keyboard():
    return InlineKeyboardMarkup([
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

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Пользователь {user.id} начал диалог")

    context.user_data.clear()

    if str(user.id) in PAID_USERS:
        paid_info = PAID_USERS[str(user.id)]
        await update.message.reply_text(
            f"👋 С возвращением! У вас активная подписка: {paid_info.get('tariff')}\n"
            f"Действует до: {paid_info.get('paid_until', 'неизвестно')}\n\n"
            f"Используйте /project для продолжения",
            reply_markup=get_main_menu_keyboard()
        )
        return

    photo_url = "https://i.ibb.co/pr4CxkkM/1.jpg"
    caption = (
        "«POLINAFIT» — место, где ты обретёшь новую версию себя! 💫\n\n"
        "Проект — это не краткосрочный марафон. Это про индивидуальный подход к каждой участнице!"
    )

    try:
        await update.message.reply_photo(photo=photo_url, caption=caption, reply_markup=get_start_keyboard())
    except:
        await update.message.reply_text(caption, reply_markup=get_start_keyboard())

async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Описание проекта"""
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! "
        "Режим питания, тренировки, поддержка от участниц проекта и лично меня! 💪\n\n"
        "Что входит:\n"
        "🤍 Индивидуальный расчет КБЖУ\n"
        "🤍 Тренировки для дома и зала\n"
        "🤍 Проверка отчетов 2 раза в неделю\n"
        "🤍 Закрытый чат участниц\n\n"
        "Выбери действие:"
    )
    await update.message.reply_text(desc, reply_markup=get_main_menu_keyboard())

async def send_project_info(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! "
        "Режим питания, тренировки, поддержка от участниц проекта и лично меня! 💪\n\n"
        "Что входит:\n"
        "🤍 Индивидуальный расчет КБЖУ\n"
        "🤍 Тренировки для дома и зала\n"
        "🤍 Проверка отчетов 2 раза в неделю\n"
        "🤍 Закрытый чат участниц\n\n"
        "Выбери действие:"
    )
    await context.bot.send_message(chat_id=chat_id, text=desc, reply_markup=get_main_menu_keyboard())

async def send_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = (
        "В проекте действует подписка:\n\n"
        "🤍 Индивидуальный расчет КБЖУ\n"
        "🤍 Тренировки для дома и зала\n"
        "🤍 Поддержка 24/7\n"
        "🤍 Закрытый чат участниц"
    )

    try:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=photo_url,
            caption=caption,
            reply_markup=get_tariffs_keyboard()
        )
    except:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=caption,
            reply_markup=get_tariffs_keyboard()
        )

async def tariffs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = "Выберите подходящий тариф:"
    try:
        await update.message.reply_photo(photo=photo_url, caption=caption, reply_markup=get_tariffs_keyboard())
    except:
        await update.message.reply_text(caption, reply_markup=get_tariffs_keyboard())

async def send_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    review_photos = [
        "https://i.ibb.co/N6yx0vQ7/Otziv-foto.jpg",
        "https://i.ibb.co/qLgkfHqk/Otziv-foto-2.jpg",
        "https://i.ibb.co/zWxK49Xb/Otziv-foto-1.jpg",
        "https://i.ibb.co/HD66d5vd/Otziv-1.jpg",
        "https://i.ibb.co/mVrGJPWs/Otziv-2.jpg"
    ]

    for i, url in enumerate(review_photos):
        try:
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=url)
            if i < len(review_photos) - 1:
                await asyncio.sleep(0.5)
        except:
            continue

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Хочешь так же? Выбирай тариф! 👇",
        reply_markup=get_reviews_keyboard()
    )

async def reviews_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_reviews(update, context)

# === PAYKEEPER ОБРАБОТЧИКИ ===
async def handle_tariff_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, tariff_data: str):
    query = update.callback_query
    await query.answer()

    tariff_map = {
        'tariff_15': {'name': '15 дней (1990 ₽)', 'price': 1990, 'days': 15},
        'tariff_30': {'name': '1 месяц (3000 ₽)', 'price': 3000, 'days': 30},
        'tariff_90': {'name': '3 месяца (6990 ₽)', 'price': 6990, 'days': 90}
    }

    tariff_info = tariff_map.get(tariff_data)
    if not tariff_info:
        await query.edit_message_text("Ошибка: тариф не найден")
        return

    context.user_data['tariff'] = tariff_info
    context.user_data['payment_step'] = 'waiting_email'

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Вы выбрали: {tariff_info['name']}\n\nДля создания счета укажите ваш email:",
        reply_markup=get_cancel_keyboard()
    )

async def create_paykeeper_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    email = update.message.text.strip()

    if not is_valid_email(email):
        await update.message.reply_text(
            "❌ Некорректный email. Попробуйте еще раз:",
            reply_markup=get_cancel_keyboard()
        )
        return

    tariff_info = context.user_data.get('tariff')
    if not tariff_info:
        await update.message.reply_text("Ошибка: тариф не выбран. Начните с /start")
        return

    if not paykeeper:
        await update.message.reply_text(
            "⚠️ Система оплаты временно недоступна. Свяжитесь с @polinakaulkina",
            reply_markup=get_start_keyboard()
        )
        return

    order_id = f"POLI_{user.id}_{int(time.time())}"

    result = await paykeeper.create_invoice(
        order_id=order_id,
        amount=tariff_info['price'],
        client_email=email,
        client_id=str(user.id),
        service_name=f"Подписка POLINAFIT - {tariff_info['name']}"
    )

    if 'error' in result or 'invoice_id' not in result:
        await update.message.reply_text(
            "❌ Не удалось создать счет. Попробуйте позже.",
            reply_markup=get_start_keyboard()
        )
        return

    payment_info = {
        'user_id': user.id,
        'username': user.username,
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
        f"Email: {email}\n\n"
        f"Нажмите кнопку ниже для оплаты:"
    )

    await update.message.reply_text(
        payment_text,
        parse_mode="Markdown",
        reply_markup=get_payment_keyboard(result['payment_link'])
    )

async def check_payment_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Проверяем...")

    order_id = context.user_data.get('current_order_id')
    if not order_id or order_id not in PAYMENTS:
        await query.edit_message_text(
            "❌ Информация о платеже не найдена. Начните с /start",
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
            f"⏳ **Ожидание оплаты**\n\n"
            f"Платеж еще не поступил.\n"
            f"Если вы оплатили, подождите 1-2 минуты.",
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
        'email': payment_info['email']
    }

    save_states()

    if SHEET:
        try:
            row_data = [
                str(user_id),
                payment_info.get('username', ''),
                '', '', '', '',
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                payment_info['tariff'],
                payment_info['email'],
                'ОПЛАЧЕНО',
                order_id
            ]
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(executor, partial(SHEET.append_row, row_data))
        except Exception as e:
            logger.error(f"Ошибка сохранения: {e}")

    success_text = (
        f"🎉 **ОПЛАТА ПРОШЛА УСПЕШНО!**\n\n"
        f"Тариф: {payment_info['tariff']}\n"
        f"Сумма: {payment_info['amount']} ₽\n"
        f"Активно до: {paid_until}\n\n"
        f"Добро пожаловать в POLINAFIT! 💪"
    )

    await query.edit_message_text(
        success_text,
        parse_mode="Markdown",
        reply_markup=get_continue_keyboard()
    )

    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"💰 Новая оплата!\n{payment_info['tariff']}\n{payment_info['amount']} ₽"
            )
        except:
            pass

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Действие отменено.",
        reply_markup=get_main_menu_keyboard()
    )

async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    instruction = (
        "Добро пожаловать в POLINAFIT! 🥳\n\n"
        "1️⃣ Вступи в канал: @recipes_group\n"
        "2️⃣ Напиши мне: @polinakaulkina\n"
        "3️⃣ Заполни анкету в закрепе"
    )

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=instruction
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    handlers = {
        'want_project': lambda u, c: send_project_info(c, query.message.chat_id),
        'tariffs': send_tariffs,
        'reviews': send_reviews,
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

    if payment_step == 'waiting_email':
        await create_paykeeper_payment(update, context)
        return

    text = update.message.text.lower()

    # УДАЛЕНЫ /menu и /help
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
            "Используйте кнопки ниже:",
            reply_markup=get_start_keyboard()
        )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID == 0 or update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Только для администратора")
        return

    total_payments = len(PAYMENTS)
    paid_payments = len([p for p in PAYMENTS.values() if p['status'] == 'paid'])
    total_amount = sum([p['amount'] for p in PAYMENTS.values() if p['status'] == 'paid'])

    stats = (
        f"📊 **Статистика**\n\n"
        f"💰 Всего платежей: {total_payments}\n"
        f"✅ Оплачено: {paid_payments}\n"
        f"💵 Сумма: {total_amount} ₽"
    )

    await update.message.reply_text(stats, parse_mode="Markdown")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}", exc_info=True)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_start_keyboard()
            )
    except:
        pass

# === WRAPPERS С ЗАДЕРЖКОЙ 0.5 СЕК ===
async def queued_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, start)

async def queued_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, project_command)

async def queued_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, tariffs_command)

async def queued_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, reviews_command)

async def queued_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, admin_stats)

async def queued_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, handle_callback_query)

async def queued_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, handle_message)

# === ЗАПУСК ===
async def post_init(application: Application):
    load_states()

    # УДАЛЕНЫ /menu и /help из списка команд
    commands = [
        BotCommand("start", "Начать работу"),
        BotCommand("project", "О проекте"),
        BotCommand("tariffs", "Тарифы"),
        BotCommand("reviews", "Отзывы"),
        BotCommand("stats", "Статистика (админ)")
    ]
    await application.bot.set_my_commands(commands)
    logger.info(f"✅ Бот инициализирован. Задержка: 0.5 сек")

def main():
    load_states()

    logger.info("=" * 60)
    logger.info("🤖 POLINAFIT Bot (без /menu и /help, задержка 0.5с)")
    logger.info("=" * 60)

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # УДАЛЕНЫ ОБРАБОТЧИКИ /menu и /help
    application.add_handler(CommandHandler("start", queued_start))
    application.add_handler(CommandHandler("project", queued_project))
    application.add_handler(CommandHandler("tariffs", queued_tariffs))
    application.add_handler(CommandHandler("reviews", queued_reviews))
    application.add_handler(CommandHandler("stats", queued_stats))
    application.add_handler(CallbackQueryHandler(queued_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queued_message))
    application.add_error_handler(error_handler)

    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
            drop_pending_updates=False
        )
    else:
        application.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()