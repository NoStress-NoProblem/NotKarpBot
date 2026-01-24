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

# === РАБОТА С GOOGLE ТАБЛИЦЕЙ ===
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

# Состояния пользователя (для сбора данных)
USER_STATE = {}  # {user_id: "waiting_for_name" / "waiting_for_height" / "waiting_for_weight"}

# Главное меню
MAIN_MENU = [
    [KeyboardButton("📸 Фото мне"), KeyboardButton("⭐ Отзывы")],
    [KeyboardButton("📝 Записаться на курс"), KeyboardButton("📦 Что входит в курс")],
    [KeyboardButton("📞 Связаться со мной")]
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сразу показываем меню"""
    await update.message.reply_text(
        "Привет! 👋 Я помогу тебе начать путь к стройности.\nВыбери действие:",
        reply_markup=ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    user_id = user.id

    if text == "📝 Записаться на курс":
        payment_text = (
            "Курс стоит всего **50 ₽**!\n\n"
            "👉 Переведите 50 ₽ на номер: **+7 (914) 195-03-33** (СБП)\n"
            "Или по ссылке: https://example.com/pay\n\n"
            "После оплаты нажмите кнопку ниже:"
        )
        await update.message.reply_text(
            payment_text,
            reply_markup=ReplyKeyboardMarkup([["✅ Оплатил"]], resize_keyboard=True)
        )

    elif text == "✅ Оплатил":
        # Считаем, что оплата прошла
        links_text = (
            "🎉 Спасибо за оплату! Вот ваши материалы:\n\n"
            "📚 Группа с рецептами: https://t.me/recipes_group\n"
            "📊 Планы похудения: https://t.me/plans_channel\n"
            "💬 Обратная связь: @your_trainer\n"
            "❓ Вопросы: https://t.me/questions_chat\n\n"
            "Теперь ответьте на 3 вопроса для персонального расчёта:"
        )
        await update.message.reply_text(links_text)
        await update.message.reply_text("1. Как вас зовут?")
        USER_STATE[user_id] = "waiting_for_name"

    elif text == "📸 Фото мне":
        await update.message.reply_text("Вдохновляйся! Ты тоже сможешь так 💯")

    elif text == "⭐ Отзывы":
        reviews = (
            "💬 Анна, -12 кг за 2 месяца!\n"
            "💬 Максим, -18 кг и больше не возвращается к старым привычкам!"
        )
        await update.message.reply_text(f"Наши реальные отзывы:\n\n{reviews}")

    elif text == "📦 Что входит в курс":
        info = (
            "✅ Индивидуальный план питания\n"
            "✅ Еженедельные чек-апы\n"
            "✅ Поддержка 24/7\n"
            "✅ Группа мотивации\n"
            "✅ Рецепты и тренировки"
        )
        await update.message.reply_text(f"Вот что вы получите:\n\n{info}")

    elif text == "📞 Связаться со мной":
        await update.message.reply_text("Напишите мне: @your_trainer_username")

    else:
        # Обработка ответов на вопросы (имя, рост, вес)
        if user_id in USER_STATE:
            state = USER_STATE[user_id]
            if state == "waiting_for_name":
                context.user_data['name'] = text
                await update.message.reply_text("2. Ваш рост (в см)?")
                USER_STATE[user_id] = "waiting_for_height"
            elif state == "waiting_for_height":
                try:
                    height = int(text)
                    context.user_data['height'] = height
                    await update.message.reply_text("3. Ваш вес (в кг)?")
                    USER_STATE[user_id] = "waiting_for_weight"
                except ValueError:
                    await update.message.reply_text("Пожалуйста, введите число (например: 170)")
            elif state == "waiting_for_weight":
                try:
                    weight = int(text)
                    context.user_data['weight'] = weight

                    # Расчёт калоража
                    calories = weight + (context.user_data['height'] / 2)
                    calories = round(calories)

                    # Сохранение в Google Таблицу
                    try:
                        SHEET.append_row([
                            str(user_id),
                            user.username or "",
                            context.user_data['name'],
                            context.user_data['height'],
                            weight,
                            calories,
                            datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                        ])
                    except Exception as e:
                        logger.error(f"Ошибка записи в таблицу: {e}")

                    # Ответ пользователю
                    await update.message.reply_text(
                        f"Готово! 🎯\nВаш ежедневный калораж: **{calories} ккал**\n\n"
                        "Следуйте плану, и результат не заставит себя ждать! 💪"
                    )

                    # Очистка состояния
                    del USER_STATE[user_id]

                except ValueError:
                    await update.message.reply_text("Пожалуйста, введите число (например: 65)")
        else:
            await update.message.reply_text("Пожалуйста, используйте кнопки меню.")

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    application.run_polling()

if __name__ == "__main__":
    main()
