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

if not TOKEN:
    logger.error("Переменная BOT_TOKEN не задана!")
    raise ValueError("Переменная BOT_TOKEN не задана!")

if ADMIN_ID == 0:
    logger.warning("ADMIN_ID не настроен!")

PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# === PAYKEEPER ===
PAYKEEPER_SERVER = os.getenv("PAYKEEPER_SERVER", "")
PAYKEEPER_USER = os.getenv("PAYKEEPER_USER", "")
PAYKEEPER_PASSWORD = os.getenv("PAYKEEPER_PASSWORD", "")
PAYKEEPER_SECRET = os.getenv("PAYKEEPER_SECRET", "")

PAYMENTS = {}
PAID_USERS = {}
executor = ThreadPoolExecutor(max_workers=4)
USER_STATES = {}
start_time = time.time()

# === ОЧЕРЕДЬ СООБЩЕНИЙ ===
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
                logger.error(f"Ошибка: {e}")

message_queue = MessageQueue()

# === ФУНКЦИИ СОХРАНЕНИЯ ===
def save_states():
    try:
        data = {'user_states': USER_STATES, 'payments': PAYMENTS, 'paid_users': PAID_USERS}
        with open('user_states.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
        except Exception as e:
            logger.error(f"Ошибка загрузки: {e}")
    else:
        USER_STATES, PAYMENTS, PAID_USERS = {}, {}, {}

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
                return None

        if google_creds_json.strip().startswith('{'):
            fd, creds_file = tempfile.mkstemp(suffix='.json', prefix='google_creds_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(google_creds_json)

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
        if not headers:
            SHEET.append_row(["ID", "Username", "Имя", "Рост", "Вес", "Калораж", "Дата", "Тариф", "Email", "Статус", "ID платежа"])

        return SHEET
    except Exception as e:
        logger.error(f"Ошибка Google Sheets: {e}")
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
            logger.error(f"Ошибка токена: {e}")
            return None

    async def create_invoice(self, order_id: str, amount: float, client_email: str, client_id: str = "", service_name: str = ""):
        try:
            token = await self.get_token()
            if not token:
                return {'error': 'Нет токена'}

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
            return {'error': str(e)}

    async def check_payment_status(self, invoice_id: str):
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f'{self.base_url}/info/invoice/byid/?id={invoice_id}'
                async with session.get(url, headers=self.get_auth_headers()) as response:
                    return await response.json()
        except Exception as e:
            return {'error': str(e)}

paykeeper = None
if PAYKEEPER_SERVER and PAYKEEPER_USER and PAYKEEPER_PASSWORD:
    paykeeper = PayKeeperAPI(PAYKEEPER_SERVER, PAYKEEPER_USER, PAYKEEPER_PASSWORD)
    logger.info("✅ PayKeeper инициализирован")

# === ВАЛИДАЦИЯ ===
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
        [InlineKeyboardButton("🔄 Проверить", callback_data='check_payment')],
        [InlineKeyboardButton("❌ Отмена", callback_data='cancel')]
    ])

def get_continue_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Продолжить ▶️", callback_data='continue')]])

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data='cancel')]])

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()

    if str(user.id) in PAID_USERS:
        paid_info = PAID_USERS[str(user.id)]
        await update.message.reply_text(
            f"👋 С возвращением! Подписка: {paid_info.get('tariff')}\n"
            f"До: {paid_info.get('paid_until', 'неизвестно')}",
            reply_markup=get_main_menu_keyboard()
        )
        return

    photo_url = "https://i.ibb.co/pr4CxkkM/1.jpg"
    caption = "«POLINAFIT» — место, где ты обретёшь новую версию себя! 💫"

    try:
        await update.message.reply_photo(photo=photo_url, caption=caption, reply_markup=get_start_keyboard())
    except:
        await update.message.reply_text(caption, reply_markup=get_start_keyboard())

async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = "Проект POLINAFIT — комплексная работа над собой! 💪\n\n🤍 Индивидуальный расчет КБЖУ\n🤍 Тренировки\n🤍 Поддержка"
    await update.message.reply_text(desc, reply_markup=get_main_menu_keyboard())

async def send_project_info(context, chat_id):
    desc = "Проект POLINAFIT — комплексная работа над собой! 💪\n\n🤍 Индивидуальный расчет КБЖУ\n🤍 Тренировки\n🤍 Поддержка"
    await context.bot.send_message(chat_id=chat_id, text=desc, reply_markup=get_main_menu_keyboard())

async def tariffs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = "Выберите тариф:"
    try:
        await update.message.reply_photo(photo=photo_url, caption=caption, reply_markup=get_tariffs_keyboard())
    except:
        await update.message.reply_text(caption, reply_markup=get_tariffs_keyboard())

async def send_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = "В проекте подписка с доступом к тренировкам, питанию и чату."

    try:
        await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo_url, caption=caption, reply_markup=get_tariffs_keyboard())
    except:
        await context.bot.send_message(chat_id=query.message.chat_id, text=caption, reply_markup=get_tariffs_keyboard())

async def reviews_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query if update.callback_query else None
    if query:
        await query.answer()
        chat_id = query.message.chat_id
    else:
        chat_id = update.message.chat_id

    photos = [
        "https://i.ibb.co/N6yx0vQ7/Otziv-foto.jpg",
        "https://i.ibb.co/qLgkfHqk/Otziv-foto-2.jpg",
        "https://i.ibb.co/zWxK49Xb/Otziv-foto-1.jpg",
        "https://i.ibb.co/HD66d5vd/Otziv-1.jpg",
        "https://i.ibb.co/mVrGJPWs/Otziv-2.jpg"
    ]

    for i, url in enumerate(photos):
        try:
            if query:
                await context.bot.send_photo(chat_id=chat_id, photo=url)
            else:
                await update.message.reply_photo(photo=url)
            if i < len(photos) - 1:
                await asyncio.sleep(0.5)
        except:
            continue

    text = "Хочешь так же? Выбирай тариф! 👇"
    if query:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=get_main_menu_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=get_main_menu_keyboard())

# === PAYKEEPER ===
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
        return

    context.user_data['tariff'] = tariff_info
    context.user_data['payment_step'] = 'waiting_email'

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Вы выбрали: {tariff_info['name']}\n\nУкажите email для чека:",
        reply_markup=get_cancel_keyboard()
    )

async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    email = update.message.text.strip()

    if not is_valid_email(email):
        await update.message.reply_text("❌ Некорректный email. Попробуйте:", reply_markup=get_cancel_keyboard())
        return

    tariff_info = context.user_data.get('tariff')
    if not tariff_info:
        await update.message.reply_text("Ошибка: начните с /start")
        return

    if not paykeeper:
        await update.message.reply_text("⚠️ Оплата недоступна. Напишите @polinakaulkina")
        return

    order_id = f"POLI_{user.id}_{int(time.time())}"

    result = await paykeeper.create_invoice(
        order_id=order_id,
        amount=tariff_info['price'],
        client_email=email,
        client_id=str(user.id),
        service_name=f"POLINAFIT - {tariff_info['name']}"
    )

    if 'error' in result or 'invoice_id' not in result:
        await update.message.reply_text("❌ Ошибка создания счета. Попробуйте позже.")
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
        'status': 'created'
    }

    PAYMENTS[order_id] = payment_info
    context.user_data['current_order_id'] = order_id
    save_states()

    text = f"💳 **Счет создан**\n\nТариф: {tariff_info['name']}\nСумма: {tariff_info['price']} ₽\n\nНажмите кнопку для оплаты:"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_payment_keyboard(result['payment_link']))

async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Проверяем...")

    order_id = context.user_data.get('current_order_id')
    if not order_id or order_id not in PAYMENTS:
        await query.edit_message_text("❌ Платеж не найден. Начните с /start")
        return

    payment_info = PAYMENTS[order_id]
    result = await paykeeper.check_payment_status(payment_info['invoice_id'])

    if result.get('status') == 'paid':
        await process_successful_payment(order_id, payment_info, query, context)
    else:
        await query.edit_message_text(
            "⏳ Ожидание оплаты...\nЕсли вы оплатили, подождите 1-2 минуты.",
            reply_markup=get_payment_keyboard(payment_info['payment_link'])
        )

async def process_successful_payment(order_id, payment_info, query, context):
    user_id = payment_info['user_id']
    PAYMENTS[order_id]['status'] = 'paid'

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
            row = [str(user_id), payment_info.get('username', ''), '', '', '', '', datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payment_info['tariff'], payment_info['email'], 'ОПЛАЧЕНО', order_id]
            await asyncio.get_event_loop().run_in_executor(executor, partial(SHEET.append_row, row))
        except:
            pass

    text = f"🎉 **ОПЛАТА УСПЕШНА!**\n\nТариф: {payment_info['tariff']}\nДо: {paid_until}\n\nДобро пожаловать!"
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_continue_keyboard())

    if ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"💰 Оплата: {payment_info['tariff']} - {payment_info['amount']} ₽")
        except:
            pass

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await context.bot.send_message(chat_id=query.message.chat_id, text="Отменено.", reply_markup=get_main_menu_keyboard())

async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "Добро пожаловать! 🥳\n\n1️⃣ @recipes_group\n2️⃣ @polinakaulkina"
    await context.bot.send_message(chat_id=query.message.chat_id, text=text)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    handlers = {
        'want_project': lambda u, c: send_project_info(c, query.message.chat_id),
        'tariffs': send_tariffs,
        'reviews': reviews_command,
        'tariff_15': lambda u, c: handle_tariff_selection(u, c, 'tariff_15'),
        'tariff_30': lambda u, c: handle_tariff_selection(u, c, 'tariff_30'),
        'tariff_90': lambda u, c: handle_tariff_selection(u, c, 'tariff_90'),
        'check_payment': check_payment,
        'cancel': handle_cancel,
        'continue': handle_continue
    }

    handler = handlers.get(data)
    if handler:
        await handler(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('payment_step') == 'waiting_email':
        await create_payment(update, context)
        return

    text = update.message.text.lower()
    commands = {'/start': start, '/project': project_command, '/tariffs': tariffs_command, '/reviews': reviews_command}

    if text in commands:
        await commands[text](update, context)
    else:
        await update.message.reply_text("Используйте кнопки:", reply_markup=get_start_keyboard())

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    paid = len([p for p in PAYMENTS.values() if p['status'] == 'paid'])
    total = sum([p['amount'] for p in PAYMENTS.values() if p['status'] == 'paid'])

    await update.message.reply_text(f"📊 Статистика\n\n💰 Оплачено: {paid}\n💵 Сумма: {total} ₽")

# === AIOHTTP WEB SERVER С HEALTH CHECK ===
async def health_handler(request):
    """Health check endpoint для Render"""
    return web.Response(text="OK", status=200)

async def webhook_handler(request):
    """Обработчик webhook от Telegram"""
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        return web.Response(status=500)

async def paykeeper_webhook_handler(request):
    """Обработчик webhook от PayKeeper"""
    try:
        data = await request.post()

        if PAYKEEPER_SECRET:
            if data.get('secret') != PAYKEEPER_SECRET:
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
                'email': payment_info['email']
            }
            save_states()
            logger.info(f"✅ Webhook активация для user_id={user_id}")

        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Ошибка PayKeeper webhook: {e}")
        return web.Response(status=500)

async def run_web_server():
    """Запуск aiohttp сервера с health check"""
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
    logger.info(f"✅ Webhook: http://0.0.0.0:{PORT}/{TOKEN[:10]}...")

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
    await message_queue.add(update.effective_user.id, update, context, handle_callback)

async def queued_message(update, context):
    await message_queue.add(update.effective_user.id, update, context, handle_message)

# === ГЛОБАЛЬНАЯ ПЕРЕМЕННАЯ ДЛЯ APPLICATION ===
application = None

# === ЗАПУСК ===
async def main():
    global application

    load_states()

    logger.info("=" * 60)
    logger.info("🤖 POLINAFIT Bot с Health Check")
    logger.info("=" * 60)

    # Создаем приложение
    application = Application.builder().token(TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", queued_start))
    application.add_handler(CommandHandler("project", queued_project))
    application.add_handler(CommandHandler("tariffs", queued_tariffs))
    application.add_handler(CommandHandler("reviews", queued_reviews))
    application.add_handler(CommandHandler("stats", queued_stats))
    application.add_handler(CallbackQueryHandler(queued_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queued_message))

    # Устанавливаем команды
    await application.bot.set_my_commands([
        BotCommand("start", "Начать"),
        BotCommand("project", "О проекте"),
        BotCommand("tariffs", "Тарифы"),
        BotCommand("reviews", "Отзывы"),
        BotCommand("stats", "Статистика (админ)")
    ])

    # Запускаем web сервер (для health check)
    await run_web_server()

    # Запускаем бота
    async with application:
        await application.start()

        # Устанавливаем webhook если есть URL
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
            await application.bot.set_webhook(url=webhook_url)
            logger.info(f"✅ Webhook установлен: {webhook_url}")

        # Держим бота запущенным
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())