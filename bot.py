import os
import logging
import gspread
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана!")

PORT = int(os.environ.get("PORT", 10000))

# === WEB APP URL (ОБЯЗАТЕЛЬНО ЗАМЕНИТЕ!) ===
WEB_APP_URL = "https://your-project.vercel.app/menu.html"

# === HEALTH CHECK ДЛЯ RENDER ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# === GOOGLE ТАБЛИЦА ===
SHEET = None
try:
    google_creds_json = os.getenv("GOOGLE_CREDS")
    if not google_creds_json:
        raise ValueError("Переменная GOOGLE_CREDS не задана!")

    with open("credentials.json", "w", encoding="utf-8") as f:
        f.write(google_creds_json)

    SCOPE = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    CREDS = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)
    CLIENT = gspread.authorize(CREDS)
    SHEET = CLIENT.open("Клиенты фитнес-бота").sheet1
    print("✅ Успешно подключено к Google Таблице!")
except Exception as e:
    print(f"❌ Ошибка подключения к Google Таблице: {e}")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

USER_STATE = {}

# === INLINE-КНОПКИ ===
def main_menu_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💪 Хочу в проект", callback_data="join_project")]
    ])

def tariff_or_reviews_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Тарифы", callback_data="tariffs")],
        [InlineKeyboardButton("💬 Отзывы", callback_data="reviews")]
    ])

def tariff_options_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 15 дней (1990 ₽)", callback_data="tariff_15")],
        [InlineKeyboardButton("📆 1 месяц (3000 ₽)", callback_data="tariff_30")],
        [InlineKeyboardButton("🗓️ 3 месяца (6990 ₽)", callback_data="tariff_90")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_menu")]
    ])

def after_payment_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Продолжить", callback_data="continue_after_payment")]
    ])

# === КОМАНДА /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_url = "https://i.ibb.co/pr4CxkkM/1.jpg"
    caption = (
        "«POLINAFIT» — место, где ты обретёшь новую версию себя! 💫\n\n"
        "Проект — это не краткосрочный марафон. Это про индивидуальный подход к каждой участнице!\n\n"
        "Я даю рекомендации по питанию, после того как подробно изучу каждый,индивидуальный случай, "
        "исходя из вашей ситуации, образа жизни, активности, вида деятельности , возможные травмы. "
        "Именно такой подход поможет тебе достричь поставленной цели!"
    )
    await update.message.reply_photo(photo=photo_url, caption=caption)
    await update.message.reply_text(
        "Готова начать? 👇",
        reply_markup=main_menu_inline()
    )

# === КОМАНДА /menu ===
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Главное меню:",
        reply_markup=main_menu_inline()
    )

# === INLINE-ОБРАБОТЧИК ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "join_project":
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
        await query.message.reply_text(features)
        await query.message.reply_text(
            "Выбери, что хочешь узнать:",
            reply_markup=tariff_or_reviews_inline()
        )

    elif query.data == "tariffs":
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
        await query.message.reply_photo(photo=photo_url, caption=caption)
        await query.message.reply_text(
            "Выбери тариф:",
            reply_markup=tariff_options_inline()
        )

    elif query.data in ["tariff_15", "tariff_30", "tariff_90"]:
        tariff_map = {
            "tariff_15": "15 дней (1990 ₽)",
            "tariff_30": "1 месяц (3000 ₽)",
            "tariff_90": "3 месяца (6990 ₽)"
        }
        context.user_data['tariff'] = tariff_map[query.data]
        await query.message.reply_text("Пожалуйста, укажи свой email — я отправлю тебе чек после оплаты:")
        USER_STATE[user_id] = "waiting_for_email"

    elif query.data == "reviews":
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

        for url in review_photos:
            await query.message.reply_photo(photo=url)

        await query.message.reply_text(
            "Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!"
        )
        await query.message.reply_text(
            "Хочешь тоже так? Жми 👇",
            reply_markup=tariff_or_reviews_inline()
        )

    elif query.data == "back_to_menu":
        await query.message.reply_text(
            "Выбери, что хочешь узнать:",
            reply_markup=tariff_or_reviews_inline()
        )

    elif query.data == "continue_after_payment":
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
        await query.message.reply_text(instruction)
        await query.message.reply_text(
            "Вступай в закрытую группу со всей информацией 🫶🏻\n"
            "👉 https://t.me/recipes_group"
        )

# === ОБРАБОТКА ТЕКСТА И WEB APP ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    # === ОБРАБОТКА WEB APP ===
    if hasattr(update.message, 'web_app_data') and update.message.web_app_data is not None:
        data = update.message.web_app_data.data
        if data == "/menu":
            await update.message.reply_text(
                "Главное меню:",
                reply_markup=main_menu_inline()
            )
        return

    # === ОБРАБОТКА EMAIL ===
    if user_id in USER_STATE and USER_STATE[user_id] == "waiting_for_email":
        if "@" in text and "." in text:
            context.user_data['email'] = text
            del USER_STATE[user_id]

            tariff = context.user_data['tariff']
            duration = "15 дней" if "15" in tariff else ("1 месяц" if "1" in tariff else "3 месяца")

            payment_msg = (
                f"Поздравляю! Подписка успешно оформлена на **{duration}** 🥳\n\n"
                "Ура! Ты в проекте! Прежде чем начать, давай ообсудим пару организационных моментов⤵️\n\n"
                "1️⃣ Вступи в чат ,где мы общаемся: https://t.me/plans_channel\n"
                "2️⃣ Активируй чат с Полиной: @your_trainer\n\n"
                "После этого нажми кнопку ниже:"
            )
            await update.message.reply_text(payment_msg, parse_mode="Markdown")
            await update.message.reply_text(
                "Продолжить ▶️",
                reply_markup=after_payment_inline()
            )

            if SHEET:
                try:
                    user = update.effective_user
                    SHEET.append_row([
                        str(user_id),
                        user.username or "",
                        "",  # имя
                        "",  # рост
                        "",  # вес
                        "",  # калораж
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        context.user_data['tariff'],
                        context.user_data['email']
                    ])
                except Exception as e:
                    logger.error(f"Ошибка записи в таблицу: {e}")
        else:
            await update.message.reply_text("Пожалуйста, введите корректный email (например: polina@mail.ru)")
    else:
        await update.message.reply_text(
            "Пожалуйста, используй кнопки.",
            reply_markup=main_menu_inline()
        )

# === ЗАПУСК ===
def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling()

if __name__ == "__main__":
    main()