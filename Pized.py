import logging
import os
import io
import html
from datetime import datetime
import pandas as pd
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes
from dotenv import load_dotenv
import psycopg2

# Завантажуємо змінні середовища (для локального тестування)
load_dotenv()

# --- 1. КОНСТАНТИ ТА НАЛАШТУВАННЯ ---

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# КОНСТАНТИ РОЗРАХУНКУ
PAY_RATE = 7.0   # Оплата за годину 
CURRENCY_SYMBOL = "€" # Нова константа для символу валюти

# СКОРОЧЕНІ КОМАНДИ (УКРАЇНСЬКІ)
CMD_START_DAY = "po"     # Почати
CMD_SUMMARY = "zvit"     # Звіт
CMD_YEAR_SUMMARY = "rik"     # Рік
CMD_DELETE_DAY = "vid" # Видалити запис
CMD_CANCEL = "vidm"      # Відмінити
CMD_SWITCH_USER = "kor" # Обрати користувача
CMD_USER_LIST = "ulist" # Список користувачів (Адмін)
CMD_USER_DELETE = "udel" # Видалити користувача (Адмін)
CMD_HOLIDAY = "vih" # Вихідний

# СПИСОК КОРИСТУВАЧІВ ДЛЯ ОБЛІКУ
KNOWN_USERS = {
    'user_1': "Іра",
    'user_2': "Андрей",
    'user_3': "Паша"
    # Додайте тут інші імена або коди
}

# СТАНИ ДЛЯ ConversationHandler
(USER_SELECT, GET_DATE, GET_START_TIME, GET_END_TIME, GET_LUNCH, GET_HOLIDAY_DATE) = range(6)

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- 2. ЛОГІКА БАЗИ ДАНИХ (POSTGRESQL) ---

def get_db_connection():
    """
    Створює та повертає підключення до бази даних PostgreSQL. 
    """
    try:
        # 1. Підключення через повний URL (пріоритет для Railway)
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            conn = psycopg2.connect(db_url)
            logger.info("Успішне підключення до PostgreSQL через DATABASE_URL.")
            return conn
        else:
            # 2. Підключення через окремі змінні (резервний варіант)
            conn = psycopg2.connect(
                host=os.getenv("PGHOST"),
                database=os.getenv("PGDATABASE"),
                user=os.getenv("PGUSER"),
                password=os.getenv("PGPASSWORD"),
                port=os.getenv("PGPORT")
            )
            logger.info("Успішне підключення до PostgreSQL через окремі змінні.")
            return conn
    except Exception as e:
        logger.error(f"Помилка підключення до PostgreSQL: {e}")
        return None

def setup_database():
    """Створює таблицю, якщо вона не існує."""
    conn = get_db_connection()
    if conn is None:
        logger.error("Не вдалося ініціалізувати базу даних через відсутність підключення.")
        return

    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS records (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                work_date TEXT, -- Зберігаємо як текст YYYY-MM-DD
                time_start TEXT,
                time_end TEXT,
                lunch_mins INTEGER,
                net_hours REAL,
                daily_pay REAL
            )
        ''')
        conn.commit()
        logger.info("Таблиця 'records' перевірена/створена успішно.")
    except Exception as e:
        logger.error(f"Помилка ініціалізації таблиць PostgreSQL: {e}")
    finally:
        if conn:
            conn.close()

def save_record(user_code: str, work_date, time_start, time_end, lunch_mins, net_hours, daily_pay):
    """Зберігає розраховані дані в базу."""
    conn = get_db_connection()
    if conn is None:
        return

    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO records
            (user_id, work_date, time_start, time_end, lunch_mins, net_hours, daily_pay)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (user_code, work_date, time_start, time_end, lunch_mins, net_hours, daily_pay))
        conn.commit()
    except Exception as e:
        logger.error(f"Помилка збереження запису в PostgreSQL: {e}")
        conn.rollback()
    finally:
        if conn:
            conn.close()

def get_monthly_records(month_year_prefix: str, user_code: str):
    """Витягує всі записи за вказаний місяць для користувача."""
    conn = get_db_connection()
    if conn is None:
        return []

    records = []
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT work_date, time_start, time_end, lunch_mins, net_hours, daily_pay
            FROM records
            WHERE user_id = %s AND work_date LIKE %s
            ORDER BY work_date ASC -- Сортування за датою в БД
        ''', (user_code, month_year_prefix + '%'))

        records = cursor.fetchall()
    except Exception as e:
        logger.error(f"Помилка отримання місячних записів PostgreSQL: {e}")
    finally:
        if conn:
            conn.close()
    return records

def get_annual_records_by_month(user_code: str, year: str):
    """Витягує всі робочі дати (РРРР-ММ-ДД) за вказаний рік для користувача."""
    conn = get_db_connection()
    if conn is None:
        return []

    dates = []
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT work_date
            FROM records
            WHERE user_id = %s AND work_date LIKE %s
            ORDER BY work_date ASC
        ''', (user_code, year + '-%'))
        dates = [row[0] for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Помилка отримання річних записів PostgreSQL: {e}")
    finally:
        if conn:
            conn.close()
    return dates

def delete_record(user_code: str, date_str: str):
    """Видаляє запис за конкретною датою для користувача."""
    conn = get_db_connection()
    if conn is None:
        return 0

    changes = 0
    try:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM records
            WHERE user_id = %s AND work_date = %s
        ''', (user_code, date_str))
        changes = cursor.rowcount
        conn.commit()
    except Exception as e:
        logger.error(f"Помилка видалення запису PostgreSQL: {e}")
        conn.rollback()
    finally:
        if conn:
            conn.close()
    return changes

def check_record_exists(user_code: str, date_str: str) -> bool:
    """Перевіряє, чи існує запис для даного користувача і дати."""
    conn = get_db_connection()
    if conn is None:
        return False

    record_exists = False
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 1
            FROM records
            WHERE user_id = %s AND work_date = %s
        ''', (user_code, date_str))
        record_exists = cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Помилка перевірки запису PostgreSQL: {e}")
    finally:
        if conn:
            conn.close()
    return record_exists

def delete_user_records(user_code: str):
    """Видаляє всі записи для конкретного коду користувача."""
    conn = get_db_connection()
    if conn is None:
        return 0

    changes = 0
    try:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM records
            WHERE user_id = %s
        ''', (user_code,))

        changes = cursor.rowcount
        conn.commit()
    except Exception as e:
        logger.error(f"Помилка видалення всіх записів користувача PostgreSQL: {e}")
        conn.rollback()
    finally:
        if conn:
            conn.close()
    return changes


# --- 3. ЛОГІКА РОЗРАХУНКУ ЧАСУ ---

def calculate_work_data(date_str, start_time_str, end_time_str, lunch_minutes):
    """
    Розраховує чистий робочий час та оплату за день.
    """
    try:
        start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")

        total_duration_minutes = (end_dt - start_dt).total_seconds() / 60
        
        # Віднімаємо лише загальний час, вказаний користувачем
        total_deduction_minutes = lunch_minutes 
        net_minutes = total_duration_minutes - total_deduction_minutes

        if net_minutes < 0:
            return None, None, "Помилка: Загальний час перерви перевищує тривалість зміни. Перевірте дані."

        net_hours = round(net_minutes / 60, 2)
        daily_pay = round(net_hours * PAY_RATE, 2)

        return net_hours, daily_pay, None

    except ValueError:
        return None, None, "Помилка формату: Переконайтеся, що час введено як ГГ:ХХ (наприклад, 09:00), а дата як РРРР-ММ-ДД."
    except Exception as e:
        logger.error(f"Непередбачена помилка: {e}")
        return None, None, f"Непередбачена помилка: {e}"


# --- 4. ОБРОБНИКИ TELEGRAM-БОТА ---

async def select_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код select_user_start) ...
    user_options = "\n".join([f"• <b>{html.escape(key)}</b> - {html.escape(name)}"
                             for key, name in KNOWN_USERS.items()])

    await update.message.reply_text(
        "👤 <b>Оберіть, для кого буде вестися облік:</b>\n"
        "Введіть один із кодів зі списку (наприклад, user_1):\n\n"
        f"{user_options}",
        parse_mode='HTML'
    )
    return USER_SELECT

async def select_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код select_user) ...
    user_code = update.message.text.strip().lower()

    if user_code not in KNOWN_USERS:
        await update.message.reply_text(
            f"⛔️ Код `{user_code}` не знайдено. Введіть коректний код зі списку:",
            parse_mode='Markdown'
        )
        return USER_SELECT

    user_name = KNOWN_USERS[user_code]
    context.user_data['current_user'] = user_code

    await update.message.reply_text(
        f"✅ Облік встановлено для **{user_name}** (`{user_code}`).\n"
        f"Тепер ви можете почати облік: **/{CMD_START_DAY}**",
        parse_mode='Markdown'
    )
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код start) ...
    current_user_code = context.user_data.get('current_user')

    if not current_user_code:
        await update.message.reply_text(
            f"❌ **Помилка:** Спочатку оберіть користувача для обліку: **/{CMD_SWITCH_USER}**",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    user_name = KNOWN_USERS[current_user_code]

    await update.message.reply_text(
        f"👋 Привіт! Облік для **{user_name}**.\n"
        "Введіть **дату** (формат: РРРР-ММ-ДД, наприклад: 2025-10-15):"
    )
    return GET_DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отримання дати, перевірка, стандартизація і запит часу початку."""
    date_str_input = update.message.text.strip()

    try:
        # Стандартизуємо дату: перетворюємо на об'єкт datetime, а потім назад у формат YYYY-MM-DD
        # Це виправить '2025-10-1' на '2025-10-01'
        date_obj = datetime.strptime(date_str_input, "%Y-%m-%d")
        date_str_standard = date_obj.strftime("%Y-%m-%d") # Це гарантує формат з нулями спереду
    except ValueError:
        await update.message.reply_text("⛔️ Невірний формат дати. Спробуйте ще раз (РРРР-ММ-ДД):")
        return GET_DATE

    current_user_code = context.user_data.get('current_user')
    if not current_user_code:
        await update.message.reply_text(f"❌ Помилка: Користувач не обраний. Будь ласка, почніть з `/{CMD_SWITCH_USER}`.")
        return ConversationHandler.END

    if check_record_exists(current_user_code, date_str_standard):
        await update.message.reply_text(
            f"❌ **Помилка:** Запис за дату **{date_str_standard}** для користувача **{KNOWN_USERS[current_user_code]}** вже існує!\n\n"
            f"Щоб додати новий запис, спочатку видаліть існуючий командою: `/{CMD_DELETE_DAY} {date_str_standard}` або скасуйте введення: `/{CMD_CANCEL}`.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    context.user_data['work_date'] = date_str_standard # Зберігаємо стандартизовану дату
    await update.message.reply_text(
        f"✅ Дату **{date_str_standard}** прийнято.\n"
        "Введіть **час початку** роботи (формат: ГГ:ХХ, наприклад: 09:00):"
    )
    return GET_START_TIME

async def get_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код get_start_time) ...
    start_time_str = update.message.text.strip()
    context.user_data['time_start'] = start_time_str

    await update.message.reply_text(
        f"✅ Початок **{start_time_str}** прийнято.\n"
        "Введіть **час завершення** роботи (формат: ГГ:ХХ, наприклад: 18:30):"
    )
    return GET_END_TIME

async def get_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код get_end_time) ...
    end_time_str = update.message.text.strip()
    context.user_data['time_end'] = end_time_str

    # ТЕКСТ: Просимо ввести загальний час перерв
    await update.message.reply_text(
        f"✅ Закінчення **{end_time_str}** прийнято.\n"
        "Введіть **загальну тривалість усіх перерв/обіду у хвилинах** (наприклад: 60, 90, або **0**, якщо перерви не було):"
    )
    return GET_LUNCH

async def get_lunch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код get_lunch) ...
    try:
        lunch_mins = int(update.message.text.strip())
        if lunch_mins < 0:
             await update.message.reply_text("⛔️ Час перерви не може бути від'ємним. Введіть 0 або позитивне число:")
             return GET_LUNCH
    except ValueError:
        await update.message.reply_text("⛔️ Введіть числове значення у хвилинах (наприклад, 60 або 0):")
        return GET_LUNCH

    # Збір усіх даних
    current_user_code = context.user_data.get('current_user')
    if not current_user_code:
        await update.message.reply_text(f"❌ Помилка: Користувач не обраний. Будь ласка, почніть з `/{CMD_SWITCH_USER}`.")
        return ConversationHandler.END

    data = context.user_data

    # Виконання розрахунку
    net_hours, daily_pay, error_msg = calculate_work_data(
        data['work_date'], data['time_start'], data['time_end'], lunch_mins
    )

    if error_msg:
        await update.message.reply_text(f"❌ **Помилка!** {error_msg}\nСпробуйте почати знову: /{CMD_START_DAY}")
        return ConversationHandler.END

    # Збереження даних у базу (data['work_date'] вже стандартизовано в get_date)
    save_record(current_user_code, data['work_date'], data['time_start'], data['time_end'], lunch_mins, net_hours, daily_pay)

    # Надсилання результату
    summary = (
        f"--- ✅ **ДАНІ ЗБЕРЕЖЕНО** ✅ ---\n"
        f"👤 **Користувач:** {KNOWN_USERS[current_user_code]}\n"
        f"📅 **Дата:** {data['work_date']}\n"
        f"🕒 **Зміна:** {data['time_start']} - {data['time_end']}\n"
        f"🍕 **Вирахування (Обід/Перерви):** {lunch_mins} хв\n"
        f"-----------------------------------\n"
        f"⏱️ **Чистий час:** **{net_hours} годин**\n"
        f"💰 **Оплата за день ({CURRENCY_SYMBOL}{PAY_RATE}/год):** **{daily_pay} {CURRENCY_SYMBOL}**"
    )
    await update.message.reply_text(summary, parse_mode='Markdown')

    # Очищуємо дані форми
    data.pop('work_date', None)
    data.pop('time_start', None)
    data.pop('time_end', None)
    return ConversationHandler.END


# -----------------------------------------------------------------
# ОБРОБНИКИ ДЛЯ КОМАНДИ ВИХІДНИЙ
# -----------------------------------------------------------------

async def start_holiday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код start_holiday) ...
    current_user_code = context.user_data.get('current_user')

    if not current_user_code:
        await update.message.reply_text(
            f"❌ **Помилка:** Спочатку оберіть користувача для обліку: **/{CMD_SWITCH_USER}**",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    user_name = KNOWN_USERS[current_user_code]

    await update.message.reply_text(
        f"🏖️ Облік для **{user_name}**.\n"
        "Введіть **дату вихідного** (формат: РРРР-ММ-ДД, наприклад: 2025-10-15):"
    )
    return GET_HOLIDAY_DATE

async def get_holiday_date_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отримує дату, перевіряє її, стандартизує та зберігає запис з нульовими годинами."""
    date_str_input = update.message.text.strip()

    try:
        # Стандартизуємо дату
        date_obj = datetime.strptime(date_str_input, "%Y-%m-%d")
        date_str_standard = date_obj.strftime("%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("⛔️ Невірний формат дати. Спробуйте ще раз (РРРР-ММ-ДД):")
        return GET_HOLIDAY_DATE

    current_user_code = context.user_data.get('current_user')
    if not current_user_code:
        await update.message.reply_text(f"❌ Помилка: Користувач не обраний. Будь ласка, почніть з `/{CMD_SWITCH_USER}`.")
        return ConversationHandler.END

    if check_record_exists(current_user_code, date_str_standard):
        await update.message.reply_text(
            f"❌ **Помилка:** Запис за дату **{date_str_standard}** вже існує!\n"
            f"Щоб додати вихідний, спочатку видаліть існуючий запис: `/{CMD_DELETE_DAY} {date_str_standard}`"
        )
        return ConversationHandler.END

    # Збереження запису з нульовими значеннями для Вихідного
    save_record(
        user_code=current_user_code, 
        work_date=date_str_standard, 
        time_start="-", 
        time_end="-",   
        lunch_mins=0, 
        net_hours=0.0, 
        daily_pay=0.0
    )

    await update.message.reply_text(
        f"✅ **Вихідний** для **{KNOWN_USERS[current_user_code]}** за дату **{date_str_standard}** успішно додано до бази даних.\n"
        f"Ця дата буде відображена у звіті Excel як неробочий день (0 годин/0 {CURRENCY_SYMBOL}).",
        parse_mode='Markdown'
    )
    return ConversationHandler.END


# -----------------------------------------------------------------
# ОБРОБНИКИ ЗВІТІВ ТА ІНШИХ КОМАНД
# -----------------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # ... (код cancel) ...
    await update.message.reply_text("🚫 Введення скасовано.")

    # Зберігаємо обраного користувача, але очищуємо дані форми
    if 'current_user' in context.user_data:
        temp_user = context.user_data['current_user']
        context.user_data.clear()
        context.user_data['current_user'] = temp_user
    else:
        context.user_data.clear()

    return ConversationHandler.END

async def get_current_user_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    # ... (код get_current_user_code) ...
    user_code = context.user_data.get('current_user')
    if not user_code:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ **Помилка:** Спочатку оберіть користувача для звіту: `/{CMD_SWITCH_USER}`",
            parse_mode='Markdown'
        )
    return user_code

async def monthly_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник команди /zvit РРРР-ММ. Генерує та надсилає Excel-файл з коректним сортуванням 
    і додає стовпець з днем тижня."""
    user_code = await get_current_user_code(update, context)
    if not user_code:
        return

    try:
        month_year_prefix = context.args[0]
        if len(month_year_prefix) != 7 or month_year_prefix[4] != '-':
            await update.message.reply_text(f"⛔️ Невірний формат. Вкажіть місяць у форматі `/{CMD_SUMMARY} РРРР-ММ` (наприклад: `/{CMD_SUMMARY} 2025-10`)")
            return
    except IndexError:
        await update.message.reply_text(f"Будь ласка, вкажіть місяць у форматі `/{CMD_SUMMARY} РРРР-ММ` (наприклад: `/{CMD_SUMMARY} 2025-10`)")
        return

    records = get_monthly_records(month_year_prefix, user_code)

    if not records:
        await update.message.reply_text(f"Немає записів за **{month_year_prefix}** для **{KNOWN_USERS[user_code]}**.")
        return

    # 1. Створення DataFrame
    currency_column_name = f'Оплата ({CURRENCY_SYMBOL})'
    df = pd.DataFrame(
        records,
        columns=['Дата', 'Початок', 'Кінець', 'Перерва (хв)', 'Чистий час (год)', currency_column_name]
    )
    
    # 2. **КОРЕКТНЕ СОРТУВАННЯ ЗА ДАТОЮ** та **РОЗРАХУНОК ДНЯ ТИЖНЯ**
    
    # Словник для перекладу англійських скорочень на українські
    day_names = {
        'Mon': 'Пн', 'Tue': 'Вт', 'Wed': 'Ср', 'Thu': 'Чт', 
        'Fri': 'Пт', 'Sat': 'Сб', 'Sun': 'Нд'
    }
    
    # Тимчасовий стовпець для коректного перетворення в datetime (для сортування і дня тижня)
    # Зберігаємо його як новий стовпець
    df['Temp_Date_Sort'] = pd.to_datetime(df['Дата'], errors='coerce') 

    # Створення стовпця "День тижня"
    # Якщо дата валідна, отримуємо день тижня і перекладаємо його. Якщо NaT (вихідний), залишаємо порожнім.
    df.insert(1, 'День тижня', df['Temp_Date_Sort'].dt.strftime('%a').map(day_names).fillna(''))
    
    # *** ВИПРАВЛЕННЯ: Сортуємо за назвою тимчасового стовпця! ***
    df = df.sort_values(by='Temp_Date_Sort', ascending=True, na_position='last')
    
    # Видаляємо допоміжний стовпець, оскільки він більше не потрібен
    df = df.drop(columns=['Temp_Date_Sort'])
    
    # Повертаємо формат дати назад у рядок (YYYY-MM-DD), щоб "-" для вихідних також коректно відображався
    df['Дата'] = df['Дата'].apply(lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) else x) # x - це '-' для NaT

    # 3. Розрахунок підсумків
    total_hours = df['Чистий час (год)'].sum()
    total_pay = df[currency_column_name].sum()
    
    # Створення підсумкового рядка
    summary_row = {
        'Дата': f'РАЗОМ ({KNOWN_USERS[user_code]}):',
        'День тижня': '', # Додаємо пусте поле для нового стовпця
        'Початок': '', 
        'Кінець': '', 
        'Перерва (хв)': '', 
        'Чистий час (год)': round(total_hours, 2),
        currency_column_name: round(total_pay, 2)
    }

    # Додавання підсумкового рядка до DataFrame
    df.loc[len(df)] = summary_row
    

    # 4. Експорт безпосередньо в Excel
    output = io.BytesIO()
    excel_filename = f"Zvit_{month_year_prefix}_{user_code}.xlsx"

    # Використовуємо df.to_excel
    df.to_excel(output, index=False, sheet_name='Work Log')
    output.seek(0)

    caption_text = (
        f"✅ Звіт по робочих змінах для **{KNOWN_USERS[user_code]}** за **{month_year_prefix}**.\n"
        f"Сумарна оплата: **{round(total_pay, 2)} {CURRENCY_SYMBOL}**"
    )

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=output,
        filename=excel_filename,
        caption=caption_text,
        parse_mode='Markdown'
    )

    await update.message.reply_text("Звіт успішно сформовано та надіслано!")

async def annual_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (код annual_summary_command) ...
    user_code = await get_current_user_code(update, context)
    if not user_code:
        return

    try:
        year = context.args[0]
        if len(year) != 4 or not year.isdigit():
            await update.message.reply_text(f"⛔️ Невірний формат. Вкажіть рік у форматі `/{CMD_YEAR_SUMMARY} РРРР` (наприклад: `/{CMD_YEAR_SUMMARY} 2025`)")
            return
    except (IndexError, TypeError):
        await update.message.reply_text(f"Будь ласка, вкажіть рік у форматі `/{CMD_YEAR_SUMMARY} РРРР` (наприклад: `/{CMD_YEAR_SUMMARY} 2025`)")
        return

    all_dates = get_annual_records_by_month(user_code, year)

    if not all_dates:
        await update.message.reply_text(f"Немає записів за **{year}** для **{KNOWN_USERS[user_code]}**.")
        return

    monthly_data = {}
    for date_str in all_dates:
        month_prefix = date_str[:7]
        day = date_str[8:]

        if month_prefix not in monthly_data:
            monthly_data[month_prefix] = []

        monthly_data[month_prefix].append(day)

    response_parts = [
        f"📅 **Активні робочі дні для {KNOWN_USERS[user_code]} за {year} рік:**",
        "--------------------------------------"
    ]

    for month, days in monthly_data.items():
        days_str = ", ".join(days)
        response_parts.append(f"\n🗓️ **{month}** ({len(days)} дн.):")
        response_parts.append(days_str)

    response_parts.append("\n--------------------------------------")
    response_parts.append(f"Для детального звіту по місяцю використовуйте: `/{CMD_SUMMARY} РРРР-ММ`")

    final_response = "\n".join(response_parts)

    await update.message.reply_text(final_response, parse_mode='Markdown')

async def delete_day_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (код delete_day_command) ...
    user_code = await get_current_user_code(update, context)
    if not user_code:
        return

    try:
        date_str_input = context.args[0]
        # Стандартизуємо дату для видалення, щоб вона точно збігалася з БД
        date_obj = datetime.strptime(date_str_input, "%Y-%m-%d")
        date_str_to_delete = date_obj.strftime("%Y-%m-%d")

    except (IndexError, ValueError):
        await update.message.reply_text(f"⛔️ Невірний формат. Вкажіть дату у форматі `/{CMD_DELETE_DAY} РРРР-ММ-ДД` (наприклад: `/{CMD_DELETE_DAY} 2025-10-15`)")
        return

    changes = delete_record(user_code, date_str_to_delete)

    if changes > 0:
        await update.message.reply_text(f"🗑️ Запис за **{date_str_to_delete}** для **{KNOWN_USERS[user_code]}** успішно видалено.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Запис за **{date_str_to_delete}** для **{KNOWN_USERS[user_code]}** не знайдено або не було видалено.", parse_mode='Markdown')

async def user_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (код user_list_command) ...
    if not KNOWN_USERS:
        await update.message.reply_text("Список користувачів для обліку (KNOWN_USERS) порожній.", parse_mode='Markdown')
        return

    user_options = "\n".join([f"• <b>{html.escape(key)}</b> - {html.escape(name)}"
                             for key, name in KNOWN_USERS.items()])

    response_text = (
        "👤 <b>Поточний список облікових записів:</b>\n"
        "-------------------------------------\n"
        "<b>Код</b> - Ім'я (для обміну):\n\n"
        f"{user_options}"
    )

    await update.message.reply_text(response_text, parse_mode='HTML')


async def user_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (код user_delete_command) ...
    try:
        user_code_to_delete = context.args[0].strip().lower()
    except IndexError:
        await update.message.reply_text(
            f"⛔️ Будь ласка, вкажіть код користувача для видалення: `/{CMD_USER_DELETE} <код>` (наприклад: `/{CMD_USER_DELETE} user_3`)",
            parse_mode='Markdown'
        )
        return

    if user_code_to_delete not in KNOWN_USERS:
        await update.message.reply_text(
            f"❌ Код користувача **`{user_code_to_delete}`** не знайдено у списку `KNOWN_USERS`. Видалення скасовано.",
            parse_mode='Markdown'
        )
        return

    user_name = KNOWN_USERS[user_code_to_delete]

    # Видалення записів з бази даних
    deleted_count = delete_user_records(user_code_to_delete)

    await update.message.reply_text(
        f"🗑️ Усі записи для **{user_name}** (`{user_code_to_delete}`) успішно видалено з бази даних.\n"
        f"Видалено записів: **{deleted_count}**.",
        parse_mode='Markdown'
    )

    # Скидаємо поточного користувача, якщо він був видалений
    if context.user_data.get('current_user') == user_code_to_delete:
        context.user_data.pop('current_user')
        await update.message.reply_text(
            f"Тепер облік для вас не встановлено. Оберіть нового користувача: `/{CMD_SWITCH_USER}`",
            parse_mode='Markdown'
        )

async def log_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (код log_user_messages) ...
    if update.message and update.message.text:
        chat_id = update.message.chat_id
        text = update.message.text

        # Ідентифікуємо користувача, якщо він обраний
        user_code = context.user_data.get('current_user', 'N/A')
        user_name = KNOWN_USERS.get(user_code, 'Невідомий')

        # Виводимо в консоль
        logger.info(f"[USER_INPUT] ChatID: {chat_id} | User: {user_name} ({user_code}) | Message: '{text}'")


# --- 5. ГОЛОВНА ФУНКЦІЯ ---

async def set_bot_commands(application: Application):
    # ... (код set_bot_commands) ...
    commands = [
        BotCommand(CMD_SWITCH_USER, f"Змінити: Обрати поточного користувача ({' / '.join(KNOWN_USERS.values())})"),
        BotCommand(CMD_HOLIDAY, f"Вихідний: Додати неробочий день (/{CMD_HOLIDAY} РРРР-ММ-ДД)"),
        BotCommand(CMD_START_DAY, "Почати облік нового робочого дня"),
        BotCommand(CMD_SUMMARY, f"Звіт: Отримати Excel-звіт за місяць (напр.: /{CMD_SUMMARY} 2024-12)"),
        BotCommand(CMD_YEAR_SUMMARY, f"Рік: Переглянути робочі дні за рік (напр.: /{CMD_YEAR_SUMMARY} 2025)"),
        BotCommand(CMD_DELETE_DAY, f"Видалити: Стерти запис за день (напр.: /{CMD_DELETE_DAY} 2025-01-01)"),
        BotCommand(CMD_USER_LIST, "Адмін: Показати список користувачів"),
        BotCommand(CMD_USER_DELETE, "Адмін: Видалити всі записи користувача"),
        BotCommand(CMD_CANCEL, "Скасувати поточне введення даних")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Список команд успішно встановлено.")

def main() -> None:
    """Запуск бота."""
    
    # Спроба ініціалізації БД
    setup_database()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.post_init = set_bot_commands

    # ConversationHandler для вибору користувача
    switch_handler = ConversationHandler(
        entry_points=[CommandHandler(CMD_SWITCH_USER, select_user_start)],
        states={
            USER_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_user)],
        },
        fallbacks=[CommandHandler(CMD_CANCEL, cancel)],
    )

    # ConversationHandler для вводу робочих даних
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler(CMD_START_DAY, start)],
        states={
            GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            GET_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_start_time)],
            GET_END_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_end_time)],
            GET_LUNCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_lunch)],
        },
        fallbacks=[CommandHandler(CMD_CANCEL, cancel)],
    )

    # ConversationHandler для додавання вихідного
    holiday_handler = ConversationHandler(
        entry_points=[CommandHandler(CMD_HOLIDAY, start_holiday)],
        states={
            GET_HOLIDAY_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_holiday_date_and_save)],
        },
        fallbacks=[CommandHandler(CMD_CANCEL, cancel)],
    )


    # Додавання обробників: 1. Діалоги, 2. Окремі команди, 3. Catch-all (log_user_messages)
    application.add_handler(switch_handler)
    application.add_handler(conv_handler)
    application.add_handler(holiday_handler) 

    # Обробники звітів та видалення
    application.add_handler(CommandHandler(CMD_SUMMARY, monthly_summary_command))
    application.add_handler(CommandHandler(CMD_YEAR_SUMMARY, annual_summary_command))
    application.add_handler(CommandHandler(CMD_DELETE_DAY, delete_day_command))

    # Обробники керування користувачами
    application.add_handler(CommandHandler(CMD_USER_LIST, user_list_command))
    application.add_handler(CommandHandler(CMD_USER_DELETE, user_delete_command))

    # Обробник для логування всіх не-командних повідомлень (ПОВИНЕН БУТИ ОСТАННІМ!)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_user_messages))

    # --- ЗАПУСК У РЕЖИМІ WEBHOOKS (ОБОВ'ЯЗКОВО ДЛЯ RAILWAY) ---

    PORT = int(os.environ.get("PORT", 8080)) 
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL") 
    
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL не встановлено. Запуск у режимі Long Polling (Тільки для локального тестування!)")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    else:
        # Успішний запуск у режимі Webhook
        logger.info(f"Запуск у режимі Webhook на порту {PORT} за адресою {WEBHOOK_URL}")
        
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=WEBHOOK_URL + TELEGRAM_TOKEN,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

if __name__ == '__main__':
    main()