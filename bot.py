import os
import logging
import gspread
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан! Добавьте в переменные окружения Render.")

PORT = int(os.environ.get("PORT", 10000))

# === ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECK ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body><h1>Bot is running!</h1></body></html>')
        elif self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Отключаем логирование запросов

def run_health_server():
    """Запуск простого веб-сервера для поддержания активности"""
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
        print(f"✅ Health сервер запущен на порту {PORT}")
        server.serve_forever()
    except Exception as e:
        print(f"❌ Ошибка health сервера: {e}")

# Запускаем health сервер в отдельном потоке
threading.Thread(target=run_health_server, daemon=True).start()

# === GOOGLE SHEETS ИНИЦИАЛИЗАЦИЯ ===
SHEET = None
try:
    # Получаем credentials из переменной окружения
    google_creds = os.getenv("GOOGLE_CREDS_JSON")
    
    if google_creds:
        # Для Render: credentials хранятся как JSON строка
        import json
        
        # Парсим JSON
        creds_dict = json.loads(google_creds)
        
        # Записываем во временный файл
        with open("service_account.json", "w") as f:
            json.dump(creds_dict, f)
        
        # Настраиваем доступ
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
        
        credentials = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
        client = gspread.authorize(credentials)
        
        # Открываем таблицу
        SHEET = client.open("Клиенты фитнес-бота").sheet1
        
        print("✅ Google Sheets подключен успешно!")
    else:
        print("⚠️ GOOGLE_CREDS_JSON не задан, работа без Google Sheets")
        
except Exception as e:
    print(f"❌ Ошибка подключения Google Sheets: {e}")
    SHEET = None

# === НАСТРОЙКА ЛОГГИРОВАНИЯ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === КНОПКИ ===
START_BUTTONS = [["Хочу в проект 💪"]]
TARIFF_MENU = [
    ["15 дней (1990 ₽)", "1 месяц (3000 ₽)"],
    ["3 месяца (6990 ₽)"],
    ["⬅️ Назад"]
]
AFTER_PAYMENT_MENU = [["Продолжить ▶️"]]

# === СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ ===
USER_STATES = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    logger.info(f"Новый пользователь: {user.id} - @{user.username}")
    
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
        reply_markup=ReplyKeyboardMarkup(START_BUTTONS, resize_keyboard=True)
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик основного меню"""
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "Хочу в проект 💪":
        desc = (
            "Проект POLINAFIT - это комплексная работа, где важно абсолютно всё! Режим питания, тренировки, "
            "поддержка от участниц проекта и лично меня! Это то место, где я помогу тебе дойти до результата, "
            "доведу тебя за ручку до твоей цели, место где ты не откатишься назад и не потеряешь результат, "
            "если случились непредвиденные обстоятельства (отпуск, стресс, травмы, болезнь и т.д.)"
        )
        await update.message.reply_text(desc)
        
        features = (
            "Что входит в проект:\n\n"
            "🤍 Тренировки для любого уровня подготовки дома или в зале\n"
            "🤍 Индивидуальный расчет КБЖУ\n"
            "🤍 Работа с отчетами 2 раза в неделю\n"
            "🤍 Поддержка в общем чате\n"
            "🤍 Любая цель: снижение или набор веса"
        )
        await update.message.reply_text(features)
        
        await update.message.reply_text(
            "Выбери, что хочешь узнать:",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰", "Отзывы 🥹"]], resize_keyboard=True)
        )
        
    elif text == "Тарифы 💰":
        photo_url = "https://i.ibb.co/F9mRf4f/Tarif.jpg"
        caption = (
            "В проекте действует подписка, которая открывает тебе доступ к преимуществам:\n\n"
            "🤍 Индивидуальный расчет КБЖУ и план тренировок\n"
            "🤍 Тренировки на любую цель\n"
            "🤍 Контроль питания и формы\n"
            "🤍 Общий чат с участницами\n"
            "🤍 Сборник рецептов и гайдов"
        )
        await update.message.reply_photo(photo=photo_url, caption=caption)
        await update.message.reply_text(
            "Выбери тариф:",
            reply_markup=ReplyKeyboardMarkup(TARIFF_MENU, resize_keyboard=True)
        )
        
    elif text in ["15 дней (1990 ₽)", "1 месяц (3000 ₽)", "3 месяца (6990 ₽)"]:
        context.user_data['tariff'] = text
        USER_STATES[user_id] = 'waiting_email'
        await update.message.reply_text("Пожалуйста, укажи свой email для отправки чека:")
        
    elif text == "Отзывы 🥹":
        await update.message.reply_photo(photo="https://i.ibb.co/N6yx0vQ7/Otziv-foto.jpg")
        await update.message.reply_text(
            "Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!\n\n"
            "Хочешь тоже так?",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰"]], resize_keyboard=True)
        )
        
    elif text == "⬅️ Назад":
        await update.message.reply_text(
            "Выбери, что хочешь узнать:",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰", "Отзывы 🥹"]], resize_keyboard=True)
        )
        
    elif USER_STATES.get(user_id) == 'waiting_email' and '@' in text and '.' in text:
        # Сохраняем email
        context.user_data['email'] = text
        USER_STATES.pop(user_id, None)
        
        # Записываем в Google Sheets если подключен
        if SHEET:
            try:
                user = update.effective_user
                row = [
                    str(user.id),
                    user.username or "",
                    user.first_name or "",
                    "",
                    "",
                    "",
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    context.user_data.get('tariff', ''),
                    text
                ]
                SHEET.append_row(row)
                logger.info(f"Данные сохранены для {user.id}")
            except Exception as e:
                logger.error(f"Ошибка записи в таблицу: {e}")
        
        tariff = context.user_data.get('tariff', '')
        duration = "15 дней" if "15" in tariff else ("1 месяц" if "1" in tariff else "3 месяца")
        
        await update.message.reply_text(
            f"Отлично! Подписка на {duration} оформлена! 🎉\n\n"
            "Следующие шаги:\n"
            "1. Вступи в чат: https://t.me/plans_channel\n"
            "2. Напиши мне: @your_trainer\n\n"
            "Нажми 'Продолжить' для инструкций:",
            reply_markup=ReplyKeyboardMarkup(AFTER_PAYMENT_MENU, resize_keyboard=True)
        )
        
    elif text == "Продолжить ▶️":
        await update.message.reply_text(
            "Отлично! Теперь вступай в закрытую группу:\n"
            "👉 https://t.me/recipes_group\n\n"
            "Там найди раздел 'НАВИГАЦИЯ' и заполни анкету для вступления."
        )
        
    else:
        await update.message.reply_text(
            "Пожалуйста, используй кнопки меню 👇",
            reply_markup=ReplyKeyboardMarkup(START_BUTTONS, resize_keyboard=True)
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла ошибка. Попробуйте снова или используйте /start"
            )
        except:
            pass

def main():
    """Основная функция запуска бота"""
    # Создаем приложение
    application = Application.builder().token(TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    
    # Добавляем обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    print("🤖 Бот запускается...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()