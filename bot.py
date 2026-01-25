import os
import logging
import gspread
import datetime
import asyncio
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# === НАСТРОЙКИ ЛОГИРОВАНИЯ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === КОНСТАНТЫ ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("Переменная BOT_TOKEN не задана!")
    raise ValueError("Переменная BOT_TOKEN не задана!")

PORT = int(os.environ.get("PORT", 10000))

# Состояния пользователя
USER_STATES = {}

# === ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECK ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK - Bot is running')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP Request: {self.path}")

def run_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        logger.info(f"Веб-сервер запущен на порту {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Ошибка веб-сервера: {e}")

# Запускаем веб-сервер в фоновом потоке
health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

# === GOOGLE ТАБЛИЦА ===
def init_google_sheets():
    """Инициализация подключения к Google Sheets"""
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

# === INLINE КЛАВИАТУРЫ ===
def get_start_keyboard():
    """Клавиатура для команды /start"""
    keyboard = [
        [InlineKeyboardButton("Хочу в проект 💪", callback_data='want_project')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_main_menu_keyboard():
    """Основное меню после описания проекта"""
    keyboard = [
        [InlineKeyboardButton("Тарифы 💰", callback_data='tariffs')],
        [InlineKeyboardButton("Отзывы 🥹", callback_data='reviews')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_tariffs_keyboard():
    """Клавиатура с тарифами"""
    keyboard = [
        [InlineKeyboardButton("15 дней (1990 ₽)", callback_data='tariff_15')],
        [InlineKeyboardButton("1 месяц (3000 ₽)", callback_data='tariff_30')],
        [InlineKeyboardButton("3 месяца (6990 ₽)", callback_data='tariff_90')],
        [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_main')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_reviews_keyboard():
    """Клавиатура после отзывов"""
    keyboard = [
        [InlineKeyboardButton("Тарифы 💰", callback_data='tariffs')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_continue_keyboard():
    """Клавиатура после оплаты"""
    keyboard = [
        [InlineKeyboardButton("Продолжить ▶️", callback_data='continue')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    """Клавиатура для отмены ввода email"""
    keyboard = [
        [InlineKeyboardButton("Отмена", callback_data='cancel')]
    ]
    return InlineKeyboardMarkup(keyboard)

# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    logger.info(f"Пользователь {user.id} ({user.username}) начал диалог")
    
    if 'user_data' in context.user_data:
        context.user_data.clear()
    
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

async def send_project_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка описания проекта"""
    query = update.callback_query
    await query.answer()
    
    desc = (
        "Проект POLINAFIT- это комплексная работа,где важно абсолютно всё! Режим питания,тренировки,"
        "поддержка от участниц проекта и лично меня! Это то, место где я помогу тебе дойти до результата, "
        "доведу тебя за ручку до твоей цели, место где ты не откатишься назад и не потеряешь результат, "
        "если случились непредвиденные обстоятельства (отпуск,стресс,травмы,болезнь итд)"
    )
    
    await query.edit_message_text(desc)
    
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
    
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=features,
        reply_markup=get_main_menu_keyboard()
    )

async def send_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка информации о тарифах"""
    query = update.callback_query
    await query.answer()
    
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
        "🤍 Подробное видео с часто задаваемыми вопросами, связанные с питанием и тренировками\n"
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
        await query.edit_message_text(
            caption,
            reply_markup=get_tariffs_keyboard()
        )

async def send_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка отзывов"""
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
        text="Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!",
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
        USER_STATES[query.from_user.id] = "waiting_for_email"
        
        await query.edit_message_text(
            f"Вы выбрали: {tariff}\n\n"
            "Пожалуйста, укажи свой email — я отправлю тебе чек после оплаты:",
            reply_markup=get_cancel_keyboard()
        )

async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки назад"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Выбери, что хочешь узнать:",
        reply_markup=get_main_menu_keyboard()
    )

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка отмены"""
    query = update.callback_query
    await query.answer()
    
    USER_STATES.pop(query.from_user.id, None)
    
    await query.edit_message_text(
        "Действие отменено. Что хочешь сделать?",
        reply_markup=get_main_menu_keyboard()
    )

async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки продолжить"""
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
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=instruction
    )
    
    await context.bot.send_message(
        chat_id=chat_id,
        text="Вступай в закрытую группу со всей информацией 🫶🏻\n👉 https://t.me/recipes_group"
    )

async def handle_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода email"""
    user_id = update.effective_user.id
    email = update.message.text
    
    if user_id in USER_STATES and USER_STATES[user_id] == "waiting_for_email":
        if "@" in email and "." in email:
            context.user_data['email'] = email
            context.user_data['user_id'] = user_id
            context.user_data['username'] = update.effective_user.username or ""
            
            USER_STATES.pop(user_id, None)
            
            tariff = context.user_data.get('tariff', '')
            duration = "15 дней" if "15" in tariff else ("1 месяц" if "1" in tariff else "3 месяца")
            
            payment_msg = (
                f"Поздравляю! Подписка успешно оформлена на **{duration}** 🥳\n\n"
                "Ура! Ты в проекте! Прежде чем начать, давай ообсудим пару организационных моментов⤵️\n\n"
                "1️⃣ Вступи в чат ,где мы общаемся: https://t.me/plans_channel  \n"
                "2️⃣ Активируй чат с Полиной: @your_trainer\n\n"
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
                "Пожалуйста, введите корректный email (например: polina@mail.ru)\n"
                "Или нажмите кнопку 'Отмена':",
                reply_markup=get_cancel_keyboard()
            )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех callback запросов"""
    query = update.callback_query
    data = query.data
    
    handlers = {
        'want_project': send_project_description,
        'tariffs': send_tariffs,
        'reviews': send_reviews,
        'tariff_15': lambda u, c: handle_tariff_selection(u, c, 'tariff_15'),
        'tariff_30': lambda u, c: handle_tariff_selection(u, c, 'tariff_30'),
        'tariff_90': lambda u, c: handle_tariff_selection(u, c, 'tariff_90'),
        'back_to_main': handle_back,
        'cancel': handle_cancel,
        'continue': handle_continue
    }
    
    handler = handlers.get(data)
    if handler:
        await handler(update, context)
    else:
        await query.answer(f"Неизвестная команда: {data}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    
    if user_id in USER_STATES and USER_STATES[user_id] == "waiting_for_email":
        await handle_email_input(update, context)
    else:
        await update.message.reply_text(
            "Пожалуйста, используй кнопки меню или начни с команды /start",
            reply_markup=get_start_keyboard()
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка при обработке обновления: {context.error}", exc_info=True)
    
    if update and hasattr(update, 'callback_query') and update.callback_query:
        try:
            await update.callback_query.answer("Произошла ошибка. Попробуйте снова.")
        except:
            pass
    elif update and update.message:
        try:
            await update.message.reply_text(
                "Произошла ошибка. Пожалуйста, попробуйте снова или начните заново с /start",
                reply_markup=get_start_keyboard()
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

# === АДМИН КОМАНДЫ ===
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика для администратора"""
    ADMIN_ID = 123456789  # Замените на ваш ID Telegram
    
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
            f"✅ Бот работает\n"
            f"👥 Всего пользователей в базе: {records}\n"
            f"🤖 Состояний пользователей в памяти: {len(USER_STATES)}\n"
            f"🕒 Время сервера: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🌐 Health check: http://0.0.0.0:{PORT}/health"
        )
        
        await update.message.reply_text(stats_text, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await update.message.reply_text(f"Ошибка получения статистики: {e}")

# === ОСНОВНАЯ ФУНКЦИЯ ===
def main():
    """Основная функция запуска бота"""
    try:
        logger.info("=" * 50)
        logger.info("🤖 ЗАПУСК ТЕЛЕГРАМ БОТА")
        logger.info(f"Токен: {TOKEN[:10]}...")
        logger.info(f"Порт: {PORT}")
        logger.info(f"Google Sheets: {'Подключен' if SHEET else 'Не подключен'}")
        logger.info("=" * 50)
        
        application = Application.builder().token(TOKEN).build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("stats", admin_stats))
        application.add_handler(CallbackQueryHandler(handle_callback_query))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        application.add_error_handler(error_handler)
        
        logger.info("✅ Бот запущен и готов к работе!")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False
        )
        
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}")
        raise

if __name__ == "__main__":
    main()