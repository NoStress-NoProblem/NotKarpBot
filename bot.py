
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

# ThreadPool для блокирующих операций
executor = ThreadPoolExecutor(max_workers=4)

# Состояния пользователя
USER_STATES = {}
start_time = time.time()

# === ОЧЕРЕДЬ СООБЩЕНИЙ (ВАРИАНТ 1) ===
class MessageQueue:
    """Очередь сообщений для обработки по порядку с защитой от флуда"""

    def __init__(self):
        self._queues = defaultdict(queue.Queue)
        self._processing = set()
        self._lock = asyncio.Lock()
        self._last_message_time = defaultdict(float)
        self._rate_limit = 0.5  # Минимальная задержка между сообщениями одного пользователя

    async def add(self, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE, handler_func):
        """Добавить сообщение в очередь и начать обработку если не запущена"""
        async with self._lock:
            self._queues[user_id].put((update, context, handler_func))
            if user_id not in self._processing:
                self._processing.add(user_id)
                asyncio.create_task(self._process_queue(user_id))

    async def _process_queue(self, user_id: int):
        """Обработать все сообщения в очереди пользователя по порядку"""
        while True:
            async with self._lock:
                q = self._queues[user_id]
                if q.empty():
                    self._processing.discard(user_id)
                    break

            try:
                update, context, handler_func = self._queues[user_id].get_nowait()

                # Rate limiting
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
                        await update.effective_message.reply_text(
                            "⚠️ Произошла ошибка при обработке. Попробуйте позже."
                        )
                except:
                    pass

# Глобальная очередь
message_queue = MessageQueue()

# === ФУНКЦИИ СОХРАНЕНИЯ СОСТОЯНИЙ ===
def save_states():
    """Сохранить состояния пользователей в файл"""
    try:
        with open('user_states.json', 'w', encoding='utf-8') as f:
            json.dump(USER_STATES, f, ensure_ascii=False, indent=2)
        logger.debug(f"Сохранено {len(USER_STATES)} состояний")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояний: {e}")

def load_states():
    """Загрузить состояния пользователей из файла"""
    global USER_STATES
    if os.path.exists('user_states.json'):
        try:
            with open('user_states.json', 'r', encoding='utf-8') as f:
                USER_STATES = json.load(f)
            logger.info(f"Загружено {len(USER_STATES)} состояний пользователей")
        except Exception as e:
            logger.error(f"Ошибка загрузки состояний: {e}")
            USER_STATES = {}
    else:
        USER_STATES = {}

# === GOOGLE ТАБЛИЦА (ИСПРАВЛЕНО) ===
def init_google_sheets():
    """Инициализация подключения к Google Sheets с использованием временных файлов"""
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

        # ИСПРАВЛЕНО: Используем tempfile для безопасного создания файла
        if google_creds_json.strip().startswith('{'):
            fd, creds_file = tempfile.mkstemp(suffix='.json', prefix='google_creds_')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(google_creds_json)
            except:
                os.close(fd)
                raise

            # Регистрируем удаление при выходе
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

        # ИСПРАВЛЕНО: Убраны пробелы в конце URL
        SCOPE = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]

        CREDS = ServiceAccountCredentials.from_json_keyfile_name(creds_file, SCOPE)
        CLIENT = gspread.authorize(CREDS)
        SHEET = CLIENT.open("Клиенты фитнес-бота").sheet1

        headers = SHEET.row_values(1)
        expected_headers = ["ID", "Username", "Имя", "Рост", "Вес", "Калораж", "Дата", "Тариф", "Email"]

        if not headers:
            SHEET.append_row(expected_headers)
            logger.info("Созданы заголовки в таблице")

        logger.info("✅ Успешно подключено к Google Таблице!")
        return SHEET

    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Google Таблице: {e}")
        return None

SHEET = init_google_sheets()

# === ФУНКЦИИ ДЛЯ РАБОТЫ С ДАННЫМИ (АСИНХРОННЫЕ) ===
async def save_to_google_sheets(user_data: dict):
    """Асинхронное сохранение данных пользователя в Google Sheets"""
    if not SHEET:
        logger.warning("Google Sheets не подключен, данные не сохранены")
        return False

    try:
        row_data = [
            str(user_data.get('user_id', '')),
            user_data.get('username', ''),
            user_data.get('name', ''),
            user_data.get('height', ''),
            user_data.get('weight', ''),
            user_data.get('calories', ''),
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user_data.get('tariff', ''),
            user_data.get('email', '')
        ]

        # ИСПРАВЛЕНО: Запускаем блокирующий вызов в executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(executor, partial(SHEET.append_row, row_data))

        logger.info(f"Данные сохранены для пользователя {user_data.get('user_id')}")
        return True

    except Exception as e:
        logger.error(f"Ошибка при сохранении в Google Sheets: {e}")
        return False

# === ВАЛИДАЦИЯ EMAIL ===
def is_valid_email(email: str) -> bool:
    """Проверка email регулярным выражением RFC 5322 (упрощенно)"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

# === КОМАНДЫ МЕНЮ БОТА ===
async def set_bot_commands(application: Application):
    """Установка команд меню бота"""
    commands = [
        BotCommand("start", "Начать работу"),
        BotCommand("project", "Описание проекта"),
        BotCommand("tariffs", "Тарифы"),
        BotCommand("reviews", "Отзывы"),
        BotCommand("help", "Помощь")
    ]

    await application.bot.set_my_commands(commands)
    logger.info("✅ Команды меню установлены")

# === ОБРАБОТЧИК HEALTH CHECK ===
async def health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка работоспособности бота"""
    uptime = int(time.time() - start_time)
    status_text = (
        f"✅ POLINAFIT Bot ONLINE\n"
        f"🕐 Uptime: {uptime} секунд\n"
        f"👥 Активных диалогов: {len(USER_STATES)}\n"
        f"⏰ Последняя проверка: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    if update.message:
        await update.message.reply_text(status_text)
    elif update.callback_query:
        await update.callback_query.answer("✅ Online", show_alert=False)

# === INLINE КЛАВИАТУРЫ ===
def get_start_keyboard():
    keyboard = [
        [InlineKeyboardButton("Хочу в проект 💪", callback_data='want_project')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("Тарифы 💰", callback_data='tariffs')],
        [InlineKeyboardButton("Отзывы 🥹", callback_data='reviews')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_tariffs_keyboard():
    keyboard = [
        [InlineKeyboardButton("15 дней (1990 ₽)", callback_data='tariff_15')],
        [InlineKeyboardButton("1 месяц (3000 ₽)", callback_data='tariff_30')],
        [InlineKeyboardButton("3 месяца (6990 ₽)", callback_data='tariff_90')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_reviews_keyboard():
    keyboard = [
        [InlineKeyboardButton("Тарифы 💰", callback_data='tariffs')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_continue_keyboard():
    keyboard = [
        [InlineKeyboardButton("Продолжить ▶️", callback_data='continue')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    keyboard = [
        [InlineKeyboardButton("Отмена", callback_data='cancel')]
    ]
    return InlineKeyboardMarkup(keyboard)

# === ОБЩИЕ ФУНКЦИИ ОТПРАВКИ (DRY - Don't Repeat Yourself) ===
async def send_project_info(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Отправка информации о проекте (единая функция для всех вызовов)"""
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! Режим питания, тренировки, "
        "поддержка от участниц проекта и лично меня! Это то место, где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, и ты не откатишься назад даже при непредвиденных обстоятельствах "
        "(отпуск, стресс, травмы, болезнь и т.д.)"
    )

    await context.bot.send_message(chat_id=chat_id, text=desc)

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
    await context.bot.send_message(
        chat_id=chat_id,
        text="Выбери, что хочешь узнать:",
        reply_markup=get_main_menu_keyboard()
    )

# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start"""
    user = update.effective_user
    logger.info(f"Пользователь {user.id} ({user.username}) начал диалог")

    # Очищаем старые данные
    context.user_data.clear()

    # ИСПРАВЛЕНО: Убраны пробелы в конце URL
    photo_url = "https://i.ibb.co/pr4CxkkM/1.jpg"
    caption = (
        "«POLINAFIT» — место, где ты обретёшь новую версию себя! 💫\n\n"
        "Проект — это не краткосрочный марафон. Это про индивидуальный подход к каждой участнице!\n\n"
        "Я даю рекомендации по питанию, после того как подробно изучу каждый индивидуальный случай, "
        "исходя из вашей ситуации, образа жизни, активности, вида деятельности, возможные травмы. "
        "Именно такой подход поможет тебе достичь поставленной цели!"
    )

    try:
        await update.message.reply_photo(
            photo=photo_url,
            caption=caption,
            reply_markup=get_start_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        await update.message.reply_text(
            caption,
            reply_markup=get_start_keyboard()
        )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /menu"""
    menu_text = (
        "📋 **Главное меню POLINAFIT**\n\n"
        "Доступные команды:\n"
        "🚀 /start - Начать работу с ботом\n"
        "💪 /project - Описание проекта\n"
        "💰 /tariffs - Тарифы и цены\n"
        "🥹 /reviews - Отзывы участниц\n"
        "❓ /help - Помощь и контакты"
    )

    await update.message.reply_text(
        menu_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )

async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /project"""
    await send_project_info(context, update.message.chat_id)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /help"""
    help_text = (
        "🆘 **Помощь и поддержка**\n\n"
        "Если у вас возникли вопросы:\n\n"
        "👤 **Тренер:** @polinakaulkina\n"
        "💬 **Общий чат проекта:** @plans_channel\n"
        "📚 **Закрытая группа с материалами:** @recipes_group\n\n"
        "**Команды бота:**\n"
        "/start - Начать диалог\n"
        "/project - Описание проекта\n"
        "/tariffs - Тарифы и цены\n"
        "/reviews - Отзывы участниц\n"
        "/help - Эта справка"
    )

    await update.message.reply_text(
        help_text,
        parse_mode="Markdown"
    )

async def send_project_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка описания проекта по кнопке"""
    query = update.callback_query
    await query.answer()
    await send_project_info(context, query.message.chat_id)

async def send_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка информации о тарифах по кнопке"""
    query = update.callback_query
    await query.answer()

    # ИСПРАВЛЕНО: Убраны пробелы в конце URL
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

async def send_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка отзывов по кнопке с задержкой между фото"""
    query = update.callback_query
    await query.answer()

    # ИСПРАВЛЕНО: Убраны пробелы в конце URL
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

    # ИСПРАВЛЕНО: Отправляем с задержкой между фото (anti-flood)
    for i, url in enumerate(review_photos[:5]):
        try:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=url
            )
            # Задержка между фото для избежания rate limit
            if i < 4:  # Не ждем после последнего фото
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка отправки отзыва {i+1}: {e}")
            continue

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!\n"
             "Хочешь тоже так? Жми 👇",
        reply_markup=get_reviews_keyboard()
    )

async def tariffs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /tariffs"""
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
        await update.message.reply_photo(
            photo=photo_url,
            caption=caption,
            reply_markup=get_tariffs_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки фото тарифов: {e}")
        await update.message.reply_text(
            caption,
            reply_markup=get_tariffs_keyboard()
        )

async def reviews_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /reviews с задержкой между фото"""
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
            # Задержка между фото
            if i < 4:
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка отправки отзыва {i+1}: {e}")
            continue

    await update.message.reply_text(
        "Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!\n"
        "Хочешь тоже так? Жми 👇",
        reply_markup=get_reviews_keyboard()
    )

async def handle_tariff_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, tariff_data: str):
    """Обработка выбора тарифа"""
    query = update.callback_query
    await query.answer()

    tariff_map = {
        'tariff_15': '15 дней (1990 ₽)',
        'tariff_30': '1 месяц (3000 ₽)',
        'tariff_90': '3 месяца (6990 ₽)'
    }

    tariff = tariff_map.get(tariff_data)
    if tariff:
        context.user_data['tariff'] = tariff
        USER_STATES[str(query.from_user.id)] = "waiting_for_email"
        save_states()

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Вы выбрали: {tariff}\n\n"
                 "Пожалуйста, укажи свой email — я отправлю тебе чек после оплаты:",
            reply_markup=get_cancel_keyboard()
        )

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка отмены действия"""
    query = update.callback_query
    await query.answer()

    USER_STATES.pop(str(query.from_user.id), None)
    save_states()

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Действие отменено. Что хочешь сделать?",
        reply_markup=get_main_menu_keyboard()
    )

async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка продолжения"""
    query = update.callback_query
    await query.answer()

    await send_final_instructions(update, context)

async def send_final_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка финальных инструкций"""
    if hasattr(update, 'callback_query'):
        query = update.callback_query
        chat_id = query.message.chat_id
    else:
        chat_id = update.message.chat_id

    instruction = (
        "Дорогая, я рада тебя приветствовать в проекте POLINAFIT 🥳\n"
        "Поздравляю, ты на шаг к своему идеальному телу! ✨\n\n"
        "Для того, чтобы нам структурировано продолжить работать, вот что нужно сделать:\n\n"
        "1️⃣ Зайди в закрытый канал с материалами проекта: @recipes_group\n"
        "2️⃣ Нажми на закреплённое сообщение «НАВИГАЦИЯ»\n"
        "3️⃣ Перейди по кнопке «АНКЕТА ДЛЯ ВСТУПЛЕНИЯ В ПРОЕКТ»\n"
        "4️⃣ Скопируй анкету и вставь её в ЛИЧНЫЙ ЧАТ со мной (@polinakaulkina)\n"
        "5️⃣ Заполни анкету подробно и отправь мне\n"
        "6️⃣ Вернись в закрытый канал и изучай материалы последовательно (сверху вниз)\n\n"
        "❗️В навигации также есть кнопки для отчётов по питанию и форме — они тебе понадобятся регулярно.\n\n"
        "ЕСЛИ ТЫ ВСЁ ПОНЯЛА, НАЖМИ «ПРОДОЛЖИТЬ» 👇"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=instruction
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text="Вступай в закрытую группу со всей информацией 🫶🏻\n👉 @recipes_group",
        reply_markup=get_continue_keyboard()
    )

async def handle_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода email"""
    user_id = update.effective_user.id
    email = update.message.text.strip()

    if str(user_id) in USER_STATES and USER_STATES[str(user_id)] == "waiting_for_email":
        # ИСПРАВЛЕНО: Используем валидацию regex вместо простой проверки
        if is_valid_email(email):
            context.user_data['email'] = email
            context.user_data['user_id'] = user_id
            context.user_data['username'] = update.effective_user.username or ""

            USER_STATES.pop(str(user_id), None)
            save_states()

            tariff = context.user_data.get('tariff', '')
            duration = "15 дней" if "15" in tariff else ("1 месяц" if "1" in tariff else "3 месяца")

            payment_msg = (
                f"Поздравляю! Подписка успешно оформлена на **{duration}** 🥳\n\n"
                "Ура! Ты в проекте! Прежде чем начать, давай обсудим пару организационных моментов:\n\n"
                "1️⃣ Вступи в чат проекта: https://t.me/+BzRGEXhUe2VjNzNi \n"
                "2️⃣ Напиши мне в личные сообщения: @polinakaulkina\n\n"
                "После этого нажми кнопку ниже:"
            )

            await update.message.reply_text(
                payment_msg,
                parse_mode="Markdown",
                reply_markup=get_continue_keyboard()
            )

            user_data_to_save = {
                'user_id': user_id,
                'username': update.effective_user.username or '',
                'name': update.effective_user.first_name or '',
                'tariff': tariff,
                'email': email
            }
            await save_to_google_sheets(user_data_to_save)

        else:
            await update.message.reply_text(
                "📧 Некорректный email. Пример правильного формата:\n"
                "`example@mail.ru` или `name@gmail.com`\n\n"
                "Попробуй ещё раз:",
                parse_mode="Markdown",
                reply_markup=get_cancel_keyboard()
            )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Маршрутизатор callback запросов"""
    query = update.callback_query
    data = query.data

    handlers = {
        'want_project': send_project_description,
        'tariffs': send_tariffs,
        'reviews': send_reviews,
        'tariff_15': lambda u, c: handle_tariff_selection(u, c, 'tariff_15'),
        'tariff_30': lambda u, c: handle_tariff_selection(u, c, 'tariff_30'),
        'tariff_90': lambda u, c: handle_tariff_selection(u, c, 'tariff_90'),
        'cancel': handle_cancel,
        'continue': handle_continue
    }

    handler = handlers.get(data)
    if handler:
        await handler(update, context)
    else:
        await query.answer(f"Неизвестная команда: {data}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Маршрутизатор текстовых сообщений"""
    user_id = update.effective_user.id

    if str(user_id) in USER_STATES and USER_STATES[str(user_id)] == "waiting_for_email":
        await handle_email_input(update, context)
    else:
        text = update.message.text.lower()

        # ИСПРАВЛЕНО: Используем словарь для маршрутизации вместо if-elif
        command_map = {
            '/start': start,
            '/menu': menu_command,
            '/help': help_command,
            '/project': project_command,
            '/tariffs': tariffs_command,
            '/reviews': reviews_command
        }

        if text in command_map:
            await command_map[text](update, context)
        elif any(keyword in text for keyword in ["проект", "хочу", "fit"]):
            await send_project_info(context, update.message.chat_id)
        else:
            await update.message.reply_text(
                "Я не понял ваше сообщение. Используйте команды меню слева от поля ввода.",
                reply_markup=get_start_keyboard()
            )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    logger.error(f"Ошибка при обработке обновления: {context.error}", exc_info=True)

    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла временная ошибка. Пожалуйста, попробуйте снова через несколько секунд.",
                reply_markup=get_start_keyboard()
            )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

# === АДМИН КОМАНДЫ ===
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика бота (только для админа)"""
    if ADMIN_ID == 0:
        await update.message.reply_text("Команда недоступна: ADMIN_ID не настроен.")
        return

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Эта команда только для администратора.")
        return

    try:
        if SHEET:
            records = len(SHEET.get_all_values()) - 1
        else:
            records = 0

        stats_text = (
            "📊 **Статистика бота:**\n\n"
            f"✅ Работает с: {datetime.datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"⏰ Uptime: {int(time.time() - start_time)} сек (~{int((time.time() - start_time)/60)} мин)\n"
            f"👥 Пользователей в базе: {records}\n"
            f"💬 Активных диалогов: {len(USER_STATES)}\n"
            f"🌐 Webhook: {'Настроен' if WEBHOOK_URL else '⚠️ Не настроен'}"
        )

        await update.message.reply_text(stats_text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await update.message.reply_text(f"Ошибка получения статистики: {e}")

# === WRAPPER ДЛЯ ОЧЕРЕДИ (ВАРИАНТ 1) ===
async def queued_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обёртка для /start с очередью"""
    await message_queue.add(update.effective_user.id, update, context, start)

async def queued_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, menu_command)

async def queued_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, help_command)

async def queued_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, project_command)

async def queued_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, tariffs_command)

async def queued_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, reviews_command)

async def queued_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, health_check)

async def queued_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, admin_stats)

async def queued_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, handle_callback_query)

async def queued_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await message_queue.add(update.effective_user.id, update, context, handle_message)

# === ОСНОВНАЯ ФУНКЦИЯ ===
async def post_init(application: Application):
    """Инициализация после запуска бота"""
    load_states()
    await set_bot_commands(application)
    logger.info(f"✅ Загружено {len(USER_STATES)} состояний пользователей")
    logger.info(f"✅ Очередь сообщений активирована (rate limit: 0.5s)")

def main():
    """Точка входа"""
    load_states()

    logger.info("=" * 60)
    logger.info("🤖 ЗАПУСК POLINAFIT БОТА (v2.0 - с очередью сообщений)")
    logger.info(f"Токен: {TOKEN[:10]}...")
    logger.info(f"Порт: {PORT}")
    logger.info(f"Webhook URL: {WEBHOOK_URL if WEBHOOK_URL else '⚠️ Не настроен'}")
    logger.info(f"Google Sheets: {'Подключен' if SHEET else 'Не подключен'}")
    logger.info(f"Admin ID: {ADMIN_ID if ADMIN_ID else '⚠️ Не настроен'}")
    logger.info(f"Загружено состояний: {len(USER_STATES)}")
    logger.info("=" * 60)

    try:
        application = Application.builder() \
            .token(TOKEN) \
            .post_init(post_init) \
            .build()

        # ИСПРАВЛЕНО: Все обработчики через очередь для гарантированной обработки
        application.add_handler(CommandHandler("start", queued_start))
        application.add_handler(CommandHandler("menu", queued_menu))
        application.add_handler(CommandHandler("help", queued_help))
        application.add_handler(CommandHandler("project", queued_project))
        application.add_handler(CommandHandler("tariffs", queued_tariffs))
        application.add_handler(CommandHandler("reviews", queued_reviews))
        application.add_handler(CommandHandler("health", queued_health))
        application.add_handler(CommandHandler("stats", queued_stats))
        application.add_handler(CallbackQueryHandler(queued_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queued_message))
        application.add_error_handler(error_handler)

        logger.info("✅ Бот инициализирован с очередью сообщений")

        if WEBHOOK_URL:
            logger.info(f"🔌 Запуск в режиме WEBHOOK (порт {PORT})")
            logger.info(f"   Webhook URL: {WEBHOOK_URL}/{TOKEN}")

            # ИСПРАВЛЕНО: drop_pending_updates=False для получения накопившихся сообщений
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN,
                webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
                drop_pending_updates=False,  # ВАЖНО: False для обработки накопившихся сообщений
                allowed_updates=Update.ALL_TYPES
            )
        else:
            logger.warning("⚠️ WEBHOOK_URL не настроен! Используется polling (только для разработки)")
            application.run_polling(
                drop_pending_updates=False,  # ВАЖНО: False здесь тоже
                allowed_updates=Update.ALL_TYPES
            )

    except Exception as e:
        logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()