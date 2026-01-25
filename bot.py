import os
import logging
import gspread
import datetime
import asyncio
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

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

# Состояния для ConversationHandler
WAITING_FOR_EMAIL, WAITING_FOR_NAME, WAITING_FOR_HEIGHT, WAITING_FOR_WEIGHT = range(4)

# Состояния пользователя (в памяти, для простоты)
USER_DATA = {}

# === ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECK ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")

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
        google_creds_json = os.getenv("GOOGLE_CREDS")
        
        if not google_creds_json:
            # Попробуем прочитать из файла для локальной разработки
            try:
                with open("credentials.json", "r", encoding="utf-8") as f:
                    google_creds_json = f.read()
            except FileNotFoundError:
                logger.warning("Файл credentials.json не найден, Google Sheets отключен")
                return None
        
        # Записываем в файл если это строка JSON
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
        
        # Проверяем заголовки
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

# === КНОПКИ ===
START_BUTTONS = [["Хочу в проект 💪"]]
TARIFF_MENU = [
    ["15 дней (1990 ₽)", "1 месяц (3000 ₽)"], 
    ["3 месяца (6990 ₽)"], 
    ["⬅️ Назад"]
]
AFTER_PAYMENT_MENU = [["Продолжить ▶️"]]

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

# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    logger.info(f"Пользователь {user.id} ({user.username}) начал диалог")
    
    # Очищаем предыдущие данные пользователя
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
    
    await update.message.reply_photo(photo=photo_url, caption=caption)
    await update.message.reply_text(
        "Готова начать? 👇",
        reply_markup=ReplyKeyboardMarkup(START_BUTTONS, resize_keyboard=True, one_time_keyboard=True)
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основной обработчик меню"""
    text = update.message.text
    user_id = update.effective_user.id
    logger.debug(f"Пользователь {user_id} выбрал: {text}")
    
    if text == "Хочу в проект 💪":
        await send_project_description(update)
        
    elif text == "Тарифы 💰":
        await send_tariffs(update)
        
    elif text in ["15 дней (1990 ₽)", "1 месяц (3000 ₽)", "3 месяца (6990 ₽)"]:
        context.user_data['tariff'] = text
        await update.message.reply_text(
            "Пожалуйста, укажи свой email — я отправлю тебе чек после оплаты:",
            reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)
        )
        USER_DATA[user_id] = "waiting_for_email"
        
    elif text == "Отзывы 🥹":
        await send_reviews(update)
        
    elif text == "⬅️ Назад":
        await update.message.reply_text(
            "Выбери, что хочешь узнать:",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰", "Отзывы 🥹"]], resize_keyboard=True)
        )
        
    elif text == "Отмена":
        USER_DATA.pop(user_id, None)
        await update.message.reply_text(
            "Действие отменено. Что хочешь сделать?",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰", "Отзывы 🥹"]], resize_keyboard=True)
        )
        
    elif user_id in USER_DATA and USER_DATA[user_id] == "waiting_for_email":
        await handle_email_input(update, context, text)
        
    elif text == "Продолжить ▶️":
        await send_final_instructions(update)
        
    else:
        await update.message.reply_text(
            "Пожалуйста, используй кнопки меню.",
            reply_markup=ReplyKeyboardMarkup(START_BUTTONS, resize_keyboard=True)
        )

async def send_project_description(update: Update):
    """Отправка описания проекта"""
    desc = (
        "Проект POLINAFIT - это комплексная работа, где важно абсолютно всё! "
        "Режим питания, тренировки, поддержка от участниц проекта и лично меня! "
        "Это то место, где я помогу тебе дойти до результата, доведу тебя за ручку до твоей цели, "
        "место где ты не откатишься назад и не потеряешь результат, если случились непредвиденные "
        "обстоятельства (отпуск, стресс, травмы, болезнь и т.д.)"
    )
    await update.message.reply_text(desc)

    features = (
        "Что входит в проект:\n\n"
        "🤍 Тренировки для любого уровня подготовки дома или в зале:\n"
        "— легкие, для тех кто только начинает\n"
        "— средней сложности, для тех кто уже занимается\n"
        "— интенсивные, для тех кто тренируется регулярно и хочет прогрессировать\n\n"
        "🤍 Питание:\n"
        "Индивидуальный расчет КБЖУ, исходя из ваших особенностей, активности и образа жизни, "
        "анализ динамики и изменения расчета по необходимости.\n\n"
        "🤍 Индивидуальная работа с отчетами:\n"
        "2 раза в неделю проверяю лично отчеты по питанию, по необходимости вношу корректировки.\n\n"
        "🤍 Абсолютно любая цель:\n"
        "— снижение веса\n"
        "— набор веса\n\n"
        "🤍 Доступ к чату со всеми участницами, поддержка и общение!"
    )
    await update.message.reply_text(features)

    await update.message.reply_text(
        "Выбери, что хочешь узнать:",
        reply_markup=ReplyKeyboardMarkup([["Тарифы 💰", "Отзывы 🥹"]], resize_keyboard=True)
    )

async def send_tariffs(update: Update):
    """Отправка информации о тарифах"""
    photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
    caption = (
        "В проекте действует подписка, которая открывает тебе доступ к следующим преимуществам:\n\n"
        "🤍 Анализ состояния для подбора питания и тренировок\n"
        "🤍 Индивидуальный расчет КБЖУ и план тренировок\n"
        "🤍 Тренировки на любую цель\n"
        "🤍 Возможность тренироваться где удобно\n"
        "🤍 Подробная техника каждого упражнения\n"
        "🤍 Контроль питания и формы каждую неделю\n"
        "🤍 Общий чат с участницами проекта\n"
        "🤍 Огромный сборник простых, бюджетных рецептов\n"
    )
    await update.message.reply_photo(photo=photo_url, caption=caption)
    await update.message.reply_text(
        "Выбери тариф:",
        reply_markup=ReplyKeyboardMarkup(TARIFF_MENU, resize_keyboard=True)
    )

async def send_reviews(update: Update):
    """Отправка отзывов"""
    review_photos = [
        "https://i.ibb.co/N6yx0vQ7/Otziv-foto.jpg",
        "https://i.ibb.co/qLgkfHqk/Otziv-foto-2.jpg",
        "https://i.ibb.co/zWxK49Xb/Otziv-foto-1.jpg",
    ]
    
    for url in review_photos[:3]:  # Отправляем только 3 чтобы не перегружать
        try:
            await update.message.reply_photo(photo=url)
        except Exception as e:
            logger.error(f"Ошибка при отправке фото: {e}")
    
    await update.message.reply_text(
        "Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!"
    )
    await update.message.reply_text(
        "Хочешь тоже так? Жми 👇",
        reply_markup=ReplyKeyboardMarkup([["Тарифы 💰"]], resize_keyboard=True)
    )

async def handle_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE, email: str):
    """Обработка ввода email"""
    user_id = update.effective_user.id
    
    if "@" in email and "." in email:
        context.user_data['email'] = email
        context.user_data['user_id'] = user_id
        context.user_data['username'] = update.effective_user.username or ""
        
        USER_DATA.pop(user_id, None)
        
        tariff = context.user_data['tariff']
        duration = "15 дней" if "15" in tariff else ("1 месяц" if "1" in tariff else "3 месяца")
        
        payment_msg = (
            f"Поздравляю! Подписка успешно оформлена на **{duration}** 🥳\n\n"
            "Ура! Ты в проекте! Прежде чем начать, давай обсудим организационные моменты:\n\n"
            "1️⃣ Вступи в чат: https://t.me/plans_channel\n"
            "2️⃣ Активируй чат с Полиной: @your_trainer\n\n"
            "После этого нажми кнопку ниже:"
        )
        await update.message.reply_text(payment_msg, parse_mode="Markdown")
        await update.message.reply_text(
            "Продолжить ▶️",
            reply_markup=ReplyKeyboardMarkup(AFTER_PAYMENT_MENU, resize_keyboard=True)
        )
        
        # Сохраняем в Google Sheets
        save_to_google_sheets(context.user_data)
        
    else:
        await update.message.reply_text(
            "Пожалуйста, введите корректный email (например: example@mail.ru)\n"
            "Или нажмите 'Отмена' для возврата в меню:",
            reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True)
        )

async def send_final_instructions(update: Update):
    """Отправка финальных инструкций"""
    instruction = (
        "Дорогая, я рада тебя приветствовать в проекте POLINAFIT!\n\n"
        "Для продолжения работы:\n"
        "1. Зайди в закрытый канал\n"
        "2. Найди закрепленное сообщение «НАВИГАЦИЯ»\n"
        "3. Нажми на «АНКЕТА ДЛЯ ВСТУПЛЕНИЯ В ПРОЕКТ»\n"
        "4. Заполни анкету и отправь её мне в личном чате\n\n"
        "Изучай материалы последовательно, сверху вниз."
    )
    await update.message.reply_text(instruction)
    await update.message.reply_text(
        "Вступай в закрытую группу со всей информацией 🫶🏻\n"
        "👉 https://t.me/recipes_group"
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка при обработке обновления: {context.error}")
    
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла ошибка. Пожалуйста, попробуйте снова или начните заново с /start"
            )
        except:
            pass

# === ОСНОВНАЯ ФУНКЦИЯ ===
def main():
    """Основная функция запуска бота"""
    try:
        logger.info("Запуск бота...")
        
        # Создаем Application
        application = Application.builder().token(TOKEN).build()
        
        # Добавляем обработчики
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
        
        # Обработчик ошибок
        application.add_error_handler(error_handler)
        
        # Запускаем бота
        logger.info("Бот запущен и готов к работе!")
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