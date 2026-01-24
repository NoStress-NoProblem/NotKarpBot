import os
import logging
import gspread
import datetime
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана!")

# === GOOGLE ТАБЛИЦА ===
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
    SHEET = None

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния пользователя
USER_STATE = {}  # user_id -> состояние

# === КНОПКИ ===
START_BUTTON = [["Хочу в проект 💪"]]
TARIFF_MENU = [["15 дней (1990 ₽)", "1 месяц (3000 ₽)"], ["3 месяца (6990 ₽)"], ["⬅️ Назад"]]
AFTER_PAYMENT_MENU = [["Продолжить ▶️"]]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_url = "https://i.ibb.co/pr4CxkkM/1.jpg"
    caption = (
        "«POLINAFIT» — место, где ты обретёшь новую версию себя! 💫\n\n"
        "Проект — это не краткосрочный марафон. Это про индивидуальный подход к каждой участнице!\n\n"
        "Я даю рекомендации по питанию после того, как подробно изучу твой случай: "
        "образ жизни, активность, травмы, цели.\n\n"
        "Именно такой подход поможет тебе достичь результата — без стресса и откатов."
    )
    await update.message.reply_photo(photo=photo_url, caption=caption)
    await update.message.reply_text(
        "Готова начать? 👇",
        reply_markup=ReplyKeyboardMarkup(START_BUTTON, resize_keyboard=True)
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "Хочу в проект 💪":
        desc = (
            "Проект POLINAFIT — это комплексная работа, где важно всё:\n\n"
            "• Режим питания\n"
            "• Тренировки\n"
            "• Поддержка от участниц и лично от меня\n\n"
            "Это место, где я доведу тебя за ручку до цели и не дам откатиться назад — "
            "даже при отпуске, стрессе, болезни или травме.\n\n"
            "Что входит в проект:"
        )
        await update.message.reply_text(desc)

        features = (
            "🤍 **Тренировки** для любого уровня: дома или в зале\n"
            "— лёгкие (для новичков)\n"
            "— средние (для продолжающих)\n"
            "— интенсивные (для продвинутых)\n\n"
            "🤍 **Питание**: индивидуальный расчёт КБЖУ + сборники завтраков/обедов/ужинов\n\n"
            "🤍 **Индивидуальная работа**: проверка отчётов 2 раза в неделю по питанию, "
            "2 раза в месяц — по форме\n\n"
            "🤍 **Любая цель**: снижение, набор веса\n\n"
            "🤍 **Закрытый чат** с участницами: поддержка, рецепты, эмоции, вопросы"
        )
        await update.message.reply_text(features, parse_mode="Markdown")

        await update.message.reply_text(
            "Выбери, что хочешь узнать:",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰", "Отзывы 🥹"]], resize_keyboard=True)
        )

    elif text == "Тарифы 💰":
        tariff_info = (
            "В проекте действует подписка, которая открывает тебе доступ к:\n\n"
            "• Анализу состояния\n"
            "• Индивидуальному расчёту КБЖУ и плану тренировок\n"
            "• Тренировкам на любую цель\n"
            "• Видео с техникой упражнений\n"
            "• Еженедельному контролю\n"
            "• Закрытому чату\n"
            "• Сборнику бюджетных рецептов\n"
            "• Гайду по продуктам и путеводителю по питанию\n"
            "• FAQ-видео по питанию и тренировкам"
        )
        await update.message.reply_text(tariff_info)
        await update.message.reply_photo(photo="https://i.ibb.co/F9mRf4f/Tarif.jpg")
        await update.message.reply_text(
            "Выбери тариф:",
            reply_markup=ReplyKeyboardMarkup(TARIFF_MENU, resize_keyboard=True)
        )

    elif text in ["15 дней (1990 ₽)", "1 месяц (3000 ₽)", "3 месяца (6990 ₽)"]:
        context.user_data['tariff'] = text
        await update.message.reply_text("Пожалуйста, укажи свой email — я отправлю тебе чек после оплаты:")
        USER_STATE[user_id] = "waiting_for_email"

    elif text == "Отзывы 🥹":
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
            await update.message.reply_photo(photo=url)

        await update.message.reply_text(
            "Ты только посмотри на отзывы моих девочек 🥹 А это всего один месяц работы! ВАУ!!!"
        )
        await update.message.reply_text(
            "Хочешь тоже так? Жми 👇",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰"]], resize_keyboard=True)
        )

    elif text == "⬅️ Назад":
        await update.message.reply_text(
            "Выбери, что хочешь узнать:",
            reply_markup=ReplyKeyboardMarkup([["Тарифы 💰", "Отзывы 🥹"]], resize_keyboard=True)
        )

    elif user_id in USER_STATE and USER_STATE[user_id] == "waiting_for_email":
        if "@" in text and "." in text:
            context.user_data['email'] = text
            del USER_STATE[user_id]

            tariff = context.user_data['tariff']
            duration = "15 дней" if "15" in tariff else ("1 месяц" if "1" in tariff else "3 месяца")

            payment_msg = (
                f"Поздравляю! Подписка успешно оформлена на **{duration}** 🥳\n\n"
                "Ура! Ты в проекте! Прежде чем начать, давай обсудим организационные моменты:\n\n"
                "1️⃣ Вступи в закрытый чат: https://t.me/plans_channel\n"
                "2️⃣ Активируй чат со мной: @your_trainer\n\n"
                "После этого нажми кнопку ниже:"
            )
            await update.message.reply_text(payment_msg, parse_mode="Markdown")
            await update.message.reply_text(
                "Продолжить ▶️",
                reply_markup=ReplyKeyboardMarkup(AFTER_PAYMENT_MENU, resize_keyboard=True)
            )

            # Запись в Google Таблицу
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

    elif text == "Продолжить ▶️":
        instruction = (
            "Дорогая, я рада приветствовать тебя в проекте POLINAFIT! 🎉\n\n"
            "Для начала тебе нужно:\n"
            "1. Перейти в закрытый канал: https://t.me/recipes_group\n"
            "2. Нажать на закреплённое сообщение «НАВИГАЦИЯ»\n"
            "3. Выбрать «АНКЕТА ДЛЯ ВСТУПЛЕНИЯ В ПРОЕКТ»\n"
            "4. Скопировать анкету, вставить сюда и заполнить\n\n"
            "❗ Изучай материал **последовательно — сверху вниз**, чтобы ничего не пропустить.\n\n"
            "Когда всё прочитаешь и поймёшь — жми «Продолжить»."
        )
        await update.message.reply_text(instruction)
        await update.message.reply_text(
            "Вступай в закрытую группу со всей информацией 🫶🏻\n"
            "👉 https://t.me/recipes_group"
        )

    else:
        await update.message.reply_text(
            "Пожалуйста, используй кнопки меню.",
            reply_markup=ReplyKeyboardMarkup(START_BUTTON, resize_keyboard=True)
        )

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    application.run_polling()

if __name__ == "__main__":
    main()