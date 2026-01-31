import os
import logging
import gspread
import datetime
import asyncio
import json
import time
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# === НАСТРОЙКИ ЛОГИРОВАНИЯ ===
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# === КОНСТАНТЫ ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("Переменная BOT_TOKEN не задана!")
    raise ValueError("Переменная BOT_TOKEN не задана!")

PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Состояния пользователя
USER_STATES = {}
start_time = time.time()

# === ФУНКЦИИ СОХРАНЕНИЯ СОСТОЯНИЙ ===
def save_states():
    try:
        with open('user_states.json', 'w', encoding='utf-8') as f:
            json.dump(USER_STATES, f)
        logger.debug(f"Сохранено {len(USER_STATES)} состояний")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояний: {e}")

def load_states():
    global USER_STATES
    if os.path.exists('user_states.json'):
        try:
            with open('user_states.json', 'r', encoding='utf-8') as f:
                USER_STATES = json.load(f)
            logger.info(f"Загружено {len(USER_STATES)} состояний пользователей")
        except Exception as e:
            logger.error(f"Ошибка загрузки состояний: {e}")
            USER_STATES = {}

# === GOOGLE ТАБЛИЦА (ИСПРАВЛЕНО) ===
def init_google_sheets():
    try:
        google_creds_json = os.getenv("GOOGLE_CREDS_JSON")
        
        if not google_creds_json:
            try:
                with open("credentials.json", "r", encoding="utf-8") as f:
                    google_creds_json = f.read()
            except FileNotFoundError:
                logger.warning("Файл credentials.json не найден, Google Sheets отключен")
                return None
        
        if google_creds_json.startswith('{'):
            with open("temp_credentials.json", "w", encoding="utf-8") as f:
                f.write(google_creds_json)
            creds_file = "temp_credentials.json"
        else:
            creds_file = google_creds_json
        
        # ИСПРАВЛЕНО: Убраны пробелы в конце строк
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

# === ФУНКЦИИ ДЛЯ РАБОТЫ С ДАННЫМИ ===
# ИСПРАВЛЕНО: Правильное определение параметра (было "user_ dict")
def save_to_google_sheets(user_data: dict):
    """Сохранение данных пользователя в Google Sheets"""
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
        
        SHEET.append_row(row_data)
        logger.info(f"Данные сохранены для пользователя {user_data.get('user_id')}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при сохранении в Google Sheets: {e}")
        return False

# === КОМАНДЫ МЕНЮ БОТА ===
async def set_bot_commands(application: Application):
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

# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Пользователь {user.id} ({user.username}) начал диалог")
    
    if 'user_data' in context.user_data:
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
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! Режим питания, тренировки, "
        "поддержка от участниц проекта и лично меня! Это то место, где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, и ты не откатишься назад даже при непредвиденных обстоятельствах "
        "(отпуск, стресс, травмы, болезнь и т.д.)"
    )
    
    await update.message.reply_text(desc)
    
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
    await update.message.reply_text(
        "Выбери, что хочешь узнать:",
        reply_markup=get_main_menu_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    query = update.callback_query
    await query.answer()
    
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! Режим питания, тренировки, "
        "поддержка от участниц проекта и лично меня! Это то место, где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, и ты не откатишься назад даже при непредвиденных обстоятельствах "
        "(отпуск, стресс, травмы, болезнь и т.д.)"
    )
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=desc
    )
    
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
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=features
    )
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Выбери, что хочешь узнать:",
        reply_markup=get_main_menu_keyboard()
    )

async def send_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    query = update.callback_query
    await query.answer()
    
    # ИСПРАВЛЕНО: Убраны пробелы в конце всех URL
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
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=url
            )
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
    # ИСПРАВЛЕНО: Убраны пробелы в конце всех URL
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

    await update.message.reply_text(
        "Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!\n"
        "Хочешь тоже так? Жми 👇",
        reply_markup=get_reviews_keyboard()
    )

# ИСПРАВЛЕНО: Правильное определение параметра (было "tariff_ str")
async def handle_tariff_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, tariff_data: str):
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
    query = update.callback_query
    await query.answer()
    
    await send_final_instructions(update, context)

async def send_final_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    user_id = update.effective_user.id
    email = update.message.text.strip()
    
    if str(user_id) in USER_STATES and USER_STATES[str(user_id)] == "waiting_for_email":
        if "@" in email and "." in email and len(email) > 5 and " " not in email:
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
                "1️⃣ Вступи в чат проекта: https://t.me/+BzRGEXhUe2VjNzNi\n"
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
            save_to_google_sheets(user_data_to_save)
            
        else:
            await update.message.reply_text(
                "📧 Некорректный email. Пример правильного формата:\n"
                "`example@mail.ru` или `name@gmail.com`\n\n"
                "Попробуй ещё раз:",
                parse_mode="Markdown",
                reply_markup=get_cancel_keyboard()
            )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    user_id = update.effective_user.id
    
    if str(user_id) in USER_STATES and USER_STATES[str(user_id)] == "waiting_for_email":
        await handle_email_input(update, context)
    else:
        text = update.message.text.lower()
        
        if text == "/start":
            await start(update, context)
        elif text == "/menu":
            await menu_command(update, context)
        elif text == "/help":
            await help_command(update, context)
        elif text == "/project":
            await project_command(update, context)
        elif text == "/tariffs":
            await tariffs_command(update, context)
        elif text == "/reviews":
            await reviews_command(update, context)
        elif "проект" in text or "хочу" in text or "fit" in text:
            await send_project_description_from_message(update, context)
        else:
            await update.message.reply_text(
                "Я не понял ваше сообщение. Используйте команды меню слева от поля ввода.",
                reply_markup=get_start_keyboard()
            )

async def send_project_description_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = (
        "Проект POLINAFIT — это комплексная работа, где важно абсолютно всё! Режим питания, тренировки, "
        "поддержка от участниц проекта и лично меня! Это то место, где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, и ты не откатишься назад даже при непредвиденных обстоятельствах "
        "(отпуск, стресс, травмы, болезнь и т.д.)"
    )
    
    await update.message.reply_text(desc)
    
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
    await update.message.reply_text(
        "Выбери, что хочешь узнать:",
        reply_markup=get_main_menu_keyboard()
    )

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

# === АДМИН КОМАНДЫ ===
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ADMIN_ID = 123456789  # ЗАМЕНИТЕ НА ВАШ РЕАЛЬНЫЙ ID
    
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

# === ОСНОВНАЯ ФУНКЦИЯ ===
async def post_init(application: Application):
    load_states()
    await set_bot_commands(application)
    logger.info(f"✅ Загружено {len(USER_STATES)} состояний пользователей")

def main():
    load_states()
    
    logger.info("=" * 60)
    logger.info("🤖 ЗАПУСК POLINAFIT БОТА")
    logger.info(f"Токен: {TOKEN[:10]}...")
    logger.info(f"Порт: {PORT}")
    logger.info(f"Webhook URL: {WEBHOOK_URL if WEBHOOK_URL else '⚠️ Не настроен'}")
    logger.info(f"Google Sheets: {'Подключен' if SHEET else 'Не подключен'}")
    logger.info(f"Загружено состояний: {len(USER_STATES)}")
    logger.info("=" * 60)
    
    try:
        application = Application.builder() \
            .token(TOKEN) \
            .post_init(post_init) \
            .build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("project", project_command))
        application.add_handler(CommandHandler("tariffs", tariffs_command))
        application.add_handler(CommandHandler("reviews", reviews_command))
        application.add_handler(CommandHandler("health", health_check))
        application.add_handler(CommandHandler("stats", admin_stats))
        application.add_handler(CallbackQueryHandler(handle_callback_query))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("✅ Бот инициализирован")
        
        if WEBHOOK_URL:
            logger.info(f"🔌 Запуск в режиме WEBHOOK (порт {PORT})")
            logger.info(f"   Webhook URL: {WEBHOOK_URL}/{TOKEN}")
            
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TOKEN,
                webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES
            )
        else:
            logger.warning("⚠️ WEBHOOK_URL не настроен! Используется polling (только для разработки)")
            application.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES
            )
            
    except Exception as e:
        logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()