import logging
import os
import asyncio
import csv
import io
import tempfile
import json
import hashlib
import datetime
from functools import wraps
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, ContextTypes, filters
from telegram.error import BadRequest, NetworkError
from dotenv import load_dotenv

# Загрузка переменных из .env файла
load_dotenv()

# Настройки
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)
# httpx logs Telegram request URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Состояния диалога
SELECTING_ACTION, ADDING_MEMBERS, ADDING_PRODUCT_NAME, PROCESSING_JSON = range(4)

# Глобальное хранилище данных пользователей
user_data = {}
# Общее хранилище чеков для всех пользователей
ACTIVE_RECEIPTS = {}

# Telegram API can occasionally take longer than its default timeout to return
# metadata or contents of an uploaded document.
TELEGRAM_TIMEOUT = 30
FILE_DOWNLOAD_ATTEMPTS = 3

# Список разрешенных ID из .env
ALLOWED_USERS = [int(uid.strip()) for uid in os.getenv("ALLOWED_USERS", "").split(",") if uid.strip()]

def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            message = update.message or update.callback_query.message
            if message:
                await message.reply_text(f"Доступ запрещен. Ваш ID: {user_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_data:
        user_data[user_id] = {}
    
    keyboard = [["Загрузить JSON", "Список чеков"]]
    await update.message.reply_text(
        "Привет! Я бот для совместного расчета покупок.\n\n"
        "Один человек загружает JSON-файл чека, а затем каждый заходит и отмечает свои позиции.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return SELECTING_ACTION

@restricted
async def select_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Загрузить JSON":
        keyboard = [["Загрузить JSON", "Список чеков"]]
        await update.message.reply_text(
            "Отправьте JSON файл с данными чека.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return PROCESSING_JSON
    elif text == "Список чеков":
        return await list_active_receipts(update, context)
    return SELECTING_ACTION

async def list_active_receipts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ACTIVE_RECEIPTS:
        message = update.message or (update.callback_query.message if update.callback_query else None)
        if message:
            await message.reply_text("Нет активных чеков. Загрузите новый!")
        return SELECTING_ACTION
    
    buttons = []
    for rid, r in ACTIVE_RECEIPTS.items():
        date_str = r['created_at'].split('T')[0]
        buttons.append([InlineKeyboardButton(f"{r['name']} ({date_str})", callback_data=f"select_receipt_{rid}")])
    
    markup = InlineKeyboardMarkup(buttons)
    # Команда "Список чеков" всегда отправляет новое сообщение
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Выберите чек для распределения позиций:",
        reply_markup=markup
    )
    return SELECTING_ACTION

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.callback_query
    
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        await query.answer("Доступ запрещен.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "back_to_list":
        # Возврат к списку - это "команда" на смену контекста, отправим новое сообщение
        return await list_active_receipts(update, context)

    if data.startswith("select_receipt_"):
        rid = data.replace("select_receipt_", "")
        if user_id not in user_data: user_data[user_id] = {}
        user_data[user_id]["current_rid"] = rid
        user_data[user_id]["item_page"] = 0
        # Выбор чека - новое сообщение
        return await show_interactive_items(update, context, edit=False)

    if data.startswith("toggle_item_"):
        parts = data.split("_")
        rid, idx = parts[2], int(parts[3])
        if rid in ACTIVE_RECEIPTS:
            item = ACTIVE_RECEIPTS[rid]["items"][idx]
            if user_id in item["selected_by"]:
                item["selected_by"].remove(user_id)
            else:
                item["selected_by"].add(user_id)
            # Действие (клик по товару) - редактируем сообщение
            return await show_interactive_items(update, context, edit=True)

    if data.startswith("page_"):
        page = int(data.split("_")[1])
        if user_id not in user_data: user_data[user_id] = {}
        user_data[user_id]["item_page"] = page
        # Действие (смена страницы) - редактируем сообщение
        return await show_interactive_items(update, context, edit=True)

    if data == "show_my_total":
        rid = user_data[user_id].get("current_rid")
        if rid and rid in ACTIVE_RECEIPTS:
            total = 0
            selected_items = []
            for item in ACTIVE_RECEIPTS[rid]["items"]:
                if user_id in item["selected_by"]:
                    share = item["total_sum"] / len(item["selected_by"])
                    total += share
                    qty_label = f" ({item['quantity']} шт/кг)" if item['quantity'] != 1 else ""
                    selected_items.append(f"▫️ {item['name']}{qty_label}\n   └ {share:.2f}₽ (из {item['total_sum']:.2f}₽)")
            
            summary = "\n".join(selected_items)
            # Итог - это "команда", новое сообщение
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🧾 Ваш список:\n\n{summary}\n\n💰 ВАШ ИТОГО: {total:.2f}₽"
            )
        return

async def show_interactive_items(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    user_id = update.effective_user.id
    query = update.callback_query
    rid = user_data[user_id].get("current_rid")
    page = user_data[user_id].get("item_page", 0)
    per_page = 8

    if not rid or rid not in ACTIVE_RECEIPTS:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Чек не найден.")
        return

    items = ACTIVE_RECEIPTS[rid]["items"]
    start_idx = page * per_page
    end_idx = start_idx + per_page
    
    buttons = []
    for i in range(start_idx, min(end_idx, len(items))):
        item = items[i]
        users_count = len(item["selected_by"])
        mark = "✅ " if user_id in item["selected_by"] else ""
        count_label = f"[{users_count}] " if users_count > 0 else "[0] "
        
        qty_val = item['quantity']
        qty_part = ""
        if qty_val != 1:
            qty_part = f"({int(qty_val)}шт) " if qty_val % 1 == 0 else f"({qty_val}кг) "

        btn_text = f"{mark}{count_label}{item['total_sum']:.2f}₽ {qty_part}{item['name']}"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_item_{rid}_{i}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page-1}"))
    if end_idx < len(items):
        nav.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{page+1}"))
    
    if nav:
        buttons.append(nav)
    
    buttons.append([InlineKeyboardButton("💰 Мой итог", callback_data="show_my_total")])
    buttons.append([InlineKeyboardButton("🏠 К списку чеков", callback_data="back_to_list")])

    header = f"🧾 {ACTIVE_RECEIPTS[rid]['name']}\n💰 Итого по чеку: {ACTIVE_RECEIPTS[rid]['total_sum']:.2f}₽"
    text = f"{header}\n\nСтр. {page+1} из {(len(items)-1)//per_page + 1}"
    
    if edit and query:
        try:
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except BadRequest:
            pass
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    return

async def download_document(document, destination):
    """Download a Telegram document, retrying temporary network failures."""
    for attempt in range(1, FILE_DOWNLOAD_ATTEMPTS + 1):
        try:
            telegram_file = await document.get_file(
                read_timeout=TELEGRAM_TIMEOUT,
                write_timeout=TELEGRAM_TIMEOUT,
                connect_timeout=TELEGRAM_TIMEOUT,
                pool_timeout=TELEGRAM_TIMEOUT,
            )
            await telegram_file.download_to_drive(
                destination,
                read_timeout=TELEGRAM_TIMEOUT,
                write_timeout=TELEGRAM_TIMEOUT,
                connect_timeout=TELEGRAM_TIMEOUT,
                pool_timeout=TELEGRAM_TIMEOUT,
            )
            return
        except NetworkError:
            if attempt == FILE_DOWNLOAD_ATTEMPTS:
                raise
            delay = 2 ** (attempt - 1)
            logger.warning(
                "Telegram file download failed (attempt %s/%s); retrying in %s s",
                attempt,
                FILE_DOWNLOAD_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

@restricted
async def process_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not update.message.document:
        await update.message.reply_text("Пожалуйста, отправьте JSON файл.")
        return PROCESSING_JSON
    
    document = update.message.document
    if not document.file_name or not document.file_name.lower().endswith('.json'):
        await update.message.reply_text("Файл должен быть в формате JSON.")
        return PROCESSING_JSON
    
    await update.message.reply_text("Обрабатываю JSON...")
    tmp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp_file:
            tmp_file_path = tmp_file.name
        await download_document(document, tmp_file_path)

        with open(tmp_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                items_data = []
                total_receipt_sum = 0
                receipt_date = ""
                receipts = data if isinstance(data, list) else [data]
                for r in receipts:
                    receipt_obj = r.get('ticket', {}).get('document', {}).get('receipt', {})
                    if not receipt_obj:
                        receipt_obj = r.get('document', {}).get('receipt', {}) or r.get('receipt', {})
                    
                    if receipt_obj:
                        items_data.extend(receipt_obj.get('items', []))
                        # Извлекаем дату и время из чека
                        dt_str = receipt_obj.get('dateTime', '')
                        if dt_str:
                            try:
                                # Преобразуем из 2026-05-09T20:30:00 в более читаемый вид
                                dt_obj = datetime.datetime.fromisoformat(dt_str.replace('Z', ''))
                                receipt_date = dt_obj.strftime('%d.%m %H:%M')
                            except Exception:
                                receipt_date = dt_str
                        
                        receipt_total = float(receipt_obj.get('totalSum', 0)) / 100.0
                        if receipt_total > 0:
                            total_receipt_sum += receipt_total
                
                if not items_data:
                    await update.message.reply_text("Товары не найдены.")
                    return SELECTING_ACTION
                
                receipt_items = []
                calculated_total = 0
                for item in items_data:
                    try:
                        name = item.get('name', 'Неизвестный товар').strip()
                        total_sum = float(item.get('sum', 0)) / 100.0
                        quantity = float(item.get('quantity', 1))
                        
                        if total_sum > 0:
                            receipt_items.append({
                                "name": name,
                                "total_sum": total_sum,
                                "quantity": quantity,
                                "selected_by": set()
                            })
                            calculated_total += total_sum
                    except (ValueError, TypeError):
                        continue
                
                if total_receipt_sum == 0:
                    total_receipt_sum = calculated_total
                
                rid = hashlib.md5(f"{datetime.datetime.now()}_{user_id}".encode()).hexdigest()[:8]
                ACTIVE_RECEIPTS[rid] = {
                    "items": receipt_items,
                    "name": f"Чек {receipt_date}" if receipt_date else f"Чек {datetime.datetime.now().strftime('%H:%M')}",
                    "created_at": datetime.datetime.now().isoformat(),
                    "total_sum": total_receipt_sum
                }
                
                await update.message.reply_text(f"Чек загружен! ID: {rid}")
                return await list_active_receipts(update, context)
    except NetworkError:
        logger.exception("Could not download JSON file from Telegram")
        await update.message.reply_text(
            "Не удалось скачать файл из Telegram из-за проблем с сетью. "
            "Попробуйте отправить JSON ещё раз."
        )
        return PROCESSING_JSON
    except Exception:
        logger.exception("Error while processing JSON file")
        await update.message.reply_text("Ошибка при обработке файла.")
        return SELECTING_ACTION
    finally:
        if tmp_file_path and os.path.exists(tmp_file_path):
            os.unlink(tmp_file_path)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Загрузить JSON", "Список чеков"]]
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return ConversationHandler.END

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN not found!")
        return
        
    application = (
        Application.builder()
        .token(token)
        .connect_timeout(TELEGRAM_TIMEOUT)
        .read_timeout(TELEGRAM_TIMEOUT)
        .write_timeout(TELEGRAM_TIMEOUT)
        .pool_timeout(TELEGRAM_TIMEOUT)
        .build()
    )
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECTING_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_action)],
            PROCESSING_JSON: [MessageHandler(filters.Document.ALL, process_json)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(select_receipt_|toggle_item_|page_|show_my_total|back_to_list)"))
    
    logger.info("Bot started...")
    # Keep retrying when Telegram is temporarily unreachable during startup.
    application.run_polling(bootstrap_retries=-1)

if __name__ == '__main__':
    main()
