import os
import json
import csv
import io
import logging
from datetime import datetime
from enum import Enum
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# States for ConversationHandler
class State(Enum):
    MENU = 1
    SELECT_PROJECT = 2
    OP_TYPE = 3
    CATEGORY = 4
    AMOUNT = 5
    CONTRACTOR = 6
    COMMENT = 7
    CREATE_PROJECT = 8

# Initialize Sheets API
def init_sheets():
    service_account_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not service_account_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    
    service_account = json.loads(service_account_json)
    creds = Credentials.from_service_account_info(
        service_account,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    client = gspread.authorize(creds)
    
    sheet_id = os.environ.get('GOOGLE_SHEETS_ID')
    if not sheet_id:
        raise ValueError("GOOGLE_SHEETS_ID env var not set")
    
    return client.open_by_key(sheet_id)

sheets = None
def get_sheets():
    global sheets
    if sheets is None:
        sheets = init_sheets()
    return sheets

# Helper functions
def get_projects():
    """Get list of active projects from Объекты sheet"""
    try:
        ws = get_sheets().worksheet('Объекты')
        projects = ws.col_values(1)[2:]  # Skip header and title
        return [p for p in projects if p and p.strip()]
    except Exception as e:
        logger.error(f"Error getting projects: {e}")
        return []

def add_operation(project: str, op_type: str, category: str, amount: float, contractor: str = "", comment: str = ""):
    """Add operation to Операции sheet"""
    try:
        ws = get_sheets().worksheet('Операции')
        next_row = len(ws.col_values(1)) + 1
        
        # Find next ID
        op_ids = ws.col_values(1)[2:]
        next_id = len([x for x in op_ids if x and x.strip()]) + 1
        
        ws.append_row([
            next_id,
            datetime.now().strftime('%d.%m.%Y'),
            project,
            op_type,
            category,
            amount,
            contractor,
            'Оплачено',
            comment
        ])
        return True
    except Exception as e:
        logger.error(f"Error adding operation: {e}")
        return False

def create_project(name: str, plan_income: float = 0, plan_expense: float = 0):
    """Create new project in Объекты sheet"""
    try:
        ws = get_sheets().worksheet('Объекты')
        ws.append_row([
            name,
            'Активный',
            datetime.now().strftime('%d.%m.%Y'),
            '',
            plan_income,
            plan_expense,
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            ''
        ])
        return True
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        return False

def get_project_summary(project: str):
    """Get summary for a project from Дашборд"""
    try:
        ws_obj = get_sheets().worksheet('Объекты')
        projects = ws_obj.col_values(1)[2:]
        
        idx = None
        for i, p in enumerate(projects):
            if p and p.strip() == project:
                idx = i + 3  # +3 because col_values is 0-indexed and starts at row 1, but we start at row 3
                break
        
        if idx is None:
            return None
        
        row = ws_obj.row_values(idx)
        if len(row) < 12:
            return None
        
        return {
            'name': row[0],
            'status': row[1],
            'plan_income': row[4],
            'plan_expense': row[5],
            'plan_margin': row[7],
            'fact_income': row[8],
            'fact_expense': row[9],
            'fact_margin': row[11],
            'dev_profit': row[14] if len(row) > 14 else 0
        }
    except Exception as e:
        logger.error(f"Error getting project summary: {e}")
        return None

def get_all_summary():
    """Get summary from Дашборд sheet"""
    try:
        ws = get_sheets().worksheet('Дашборд')
        return {
            'fact_income': ws.cell(3, 2).value,
            'fact_expense': ws.cell(4, 2).value,
            'fact_profit': ws.cell(5, 2).value,
            'fact_margin': ws.cell(6, 2).value,
            'plan_income': ws.cell(8, 2).value,
            'plan_expense': ws.cell(9, 2).value,
            'plan_profit': ws.cell(10, 2).value,
            'plan_margin': ws.cell(11, 2).value,
        }
    except Exception as e:
        logger.error(f"Error getting summary: {e}")
        return None

def export_to_csv(project: str = None):
    """Export operations to CSV"""
    try:
        ws = get_sheets().worksheet('Операции')
        rows = ws.get_all_values()
        
        output = io.StringIO()
        if project:
            rows = [rows[0]] + [r for r in rows[1:] if len(r) > 2 and r[2] == project]
        
        writer = csv.writer(output)
        writer.writerows(rows)
        return output.getvalue()
    except Exception as e:
        logger.error(f"Error exporting: {e}")
        return None

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    keyboard = [
        [InlineKeyboardButton("➕ Новая операция", callback_data='op_new')],
        [InlineKeyboardButton("📊 Отчет по всем", callback_data='report_all')],
        [InlineKeyboardButton("🏢 По объекту", callback_data='report_object')],
        [InlineKeyboardButton("📁 Создать объект", callback_data='create_project')],
        [InlineKeyboardButton("📥 Выгрузить CSV", callback_data='export')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! 🌱 Это бот финансового учёта ZenScape.\n\nЧто хочешь сделать?",
        reply_markup=reply_markup
    )
    return State.MENU

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu callbacks"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'op_new':
        projects = get_projects()
        if not projects:
            await query.edit_message_text("❌ Объектов не найдено. Сначала создай объект.")
            return State.MENU
        
        keyboard = [[InlineKeyboardButton(p, callback_data=f'proj_{p}')] for p in projects]
        keyboard.append([InlineKeyboardButton("➕ Новый объект", callback_data='create_project')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выбери объект:", reply_markup=reply_markup)
        return State.SELECT_PROJECT
    
    elif query.data == 'report_all':
        summary = get_all_summary()
        if summary:
            fact_margin_pct = f"{float(summary['fact_margin'])*100:.1f}%" if summary['fact_margin'] else "—"
            plan_margin_pct = f"{float(summary['plan_margin'])*100:.1f}%" if summary['plan_margin'] else "—"
            
            text = f"""
📊 <b>Общий отчёт</b>

<b>ФАКТ:</b>
💰 Доход: {summary['fact_income']} ₽
💸 Расход: {summary['fact_expense']} ₽
📈 Прибыль: {summary['fact_profit']} ₽
📊 Маржа: {fact_margin_pct}

<b>ПЛАН:</b>
💰 Доход: {summary['plan_income']} ₽
💸 Расход: {summary['plan_expense']} ₽
📈 Прибыль: {summary['plan_profit']} ₽
📊 Маржа: {plan_margin_pct}
"""
            keyboard = [[InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return State.MENU
    
    elif query.data == 'report_object':
        projects = get_projects()
        keyboard = [[InlineKeyboardButton(p, callback_data=f'report_{p}')] for p in projects]
        keyboard.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выбери объект:", reply_markup=reply_markup)
        return State.MENU
    
    elif query.data.startswith('report_'):
        project = query.data[7:]
        summary = get_project_summary(project)
        if summary:
            fact_margin_pct = f"{float(summary['fact_margin'])*100:.1f}%" if summary['fact_margin'] else "—"
            plan_margin_pct = f"{float(summary['plan_margin'])*100:.1f}%" if summary['plan_margin'] else "—"
            
            text = f"""
📊 <b>Отчёт: {summary['name']}</b>

<b>ФАКТ:</b>
💰 Доход: {summary['fact_income']} ₽
💸 Расход: {summary['fact_expense']} ₽
📊 Маржа: {fact_margin_pct}

<b>ПЛАН:</b>
💰 Доход: {summary['plan_income']} ₽
💸 Расход: {summary['plan_expense']} ₽
📊 Маржа: {plan_margin_pct}
"""
            keyboard = [[InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return State.MENU
    
    elif query.data == 'create_project':
        await query.edit_message_text("Напиши название нового объекта:")
        return State.CREATE_PROJECT
    
    elif query.data == 'export':
        projects = get_projects()
        keyboard = [[InlineKeyboardButton("Все объекты", callback_data='export_all')]]
        keyboard += [[InlineKeyboardButton(p, callback_data=f'export_{p}')] for p in projects]
        keyboard.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выбери объект для выгрузки:", reply_markup=reply_markup)
        return State.MENU
    
    elif query.data.startswith('export_'):
        project = query.data[7:] if query.data != 'export_all' else None
        csv_data = export_to_csv(project)
        if csv_data:
            filename = f"export_{project}.csv" if project else "export_all.csv"
            await query.edit_message_text(f"✅ Готово. Файл: {filename}\n\n```{csv_data[:500]}...```", parse_mode=ParseMode.MARKDOWN)
        return State.MENU
    
    elif query.data == 'back_to_menu':
        keyboard = [
            [InlineKeyboardButton("➕ Новая операция", callback_data='op_new')],
            [InlineKeyboardButton("📊 Отчет по всем", callback_data='report_all')],
            [InlineKeyboardButton("🏢 По объекту", callback_data='report_object')],
            [InlineKeyboardButton("📁 Создать объект", callback_data='create_project')],
            [InlineKeyboardButton("📥 Выгрузить CSV", callback_data='export')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Что хочешь сделать?", reply_markup=reply_markup)
        return State.MENU
    
    elif query.data.startswith('proj_'):
        context.user_data['project'] = query.data[5:]
        keyboard = [
            [InlineKeyboardButton("💰 Приход", callback_data='type_income')],
            [InlineKeyboardButton("💸 Расход", callback_data='type_expense')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Объект: {context.user_data['project']}\n\nТип операции:", reply_markup=reply_markup)
        return State.OP_TYPE
    
    elif query.data.startswith('type_'):
        context.user_data['op_type'] = 'Приход' if query.data == 'type_income' else 'Расход'
        
        categories = {
            'Приход': ['Оплата от клиента', 'Аванс', 'Доплата', 'Прочее'],
            'Расход': ['Растения', 'Материалы', 'Субподряд', 'Рабочие', 'Доставка', 'Накладные', 'Прочее']
        }
        cats = categories[context.user_data['op_type']]
        keyboard = [[InlineKeyboardButton(c, callback_data=f'cat_{c}')] for c in cats]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Категория ({context.user_data['op_type']}):", reply_markup=reply_markup)
        return State.CATEGORY
    
    elif query.data.startswith('cat_'):
        context.user_data['category'] = query.data[4:]
        await query.edit_message_text(f"Сумма (₽):")
        return State.AMOUNT
    
    return State.MENU

async def create_project_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle project creation"""
    project_name = update.message.text.strip()
    if create_project(project_name):
        await update.message.reply_text(f"✅ Объект '{project_name}' создан!")
    else:
        await update.message.reply_text("❌ Ошибка при создании объекта.")
    
    keyboard = [
        [InlineKeyboardButton("➕ Новая операция", callback_data='op_new')],
        [InlineKeyboardButton("📊 Отчет по всем", callback_data='report_all')],
        [InlineKeyboardButton("🏢 По объекту", callback_data='report_object')],
        [InlineKeyboardButton("📁 Создать объект", callback_data='create_project')],
        [InlineKeyboardButton("📥 Выгрузить CSV", callback_data='export')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Что дальше?", reply_markup=reply_markup)
    return State.MENU

async def amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle amount input"""
    try:
        amount = float(update.message.text.strip())
        context.user_data['amount'] = amount
        await update.message.reply_text("Контрагент (опционально, напиши или нажми /skip):")
        return State.CONTRACTOR
    except ValueError:
        await update.message.reply_text("❌ Напиши число (например, 15000)")
        return State.AMOUNT

async def contractor_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle contractor input"""
    text = update.message.text.strip()
    if text.lower() == '/skip':
        context.user_data['contractor'] = ''
    else:
        context.user_data['contractor'] = text
    
    await update.message.reply_text("Комментарий (опционально, напиши или нажми /skip):")
    return State.COMMENT

async def comment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle comment and save operation"""
    text = update.message.text.strip()
    if text.lower() == '/skip':
        context.user_data['comment'] = ''
    else:
        context.user_data['comment'] = text
    
    if add_operation(
        context.user_data['project'],
        context.user_data['op_type'],
        context.user_data['category'],
        context.user_data['amount'],
        context.user_data.get('contractor', ''),
        context.user_data.get('comment', '')
    ):
        await update.message.reply_text(
            f"""✅ Операция сохранена!
            
📍 Объект: {context.user_data['project']}
📌 Тип: {context.user_data['op_type']}
🏷 Категория: {context.user_data['category']}
💰 Сумма: {context.user_data['amount']} ₽"""
        )
    else:
        await update.message.reply_text("❌ Ошибка при сохранении операции.")
    
    keyboard = [
        [InlineKeyboardButton("➕ Новая операция", callback_data='op_new')],
        [InlineKeyboardButton("📊 Отчет по всем", callback_data='report_all')],
        [InlineKeyboardButton("🏢 По объекту", callback_data='report_object')],
        [InlineKeyboardButton("📁 Создать объект", callback_data='create_project')],
        [InlineKeyboardButton("📥 Выгрузить CSV", callback_data='export')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Что дальше?", reply_markup=reply_markup)
    return State.MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    await update.message.reply_text("Отменено.")
    return State.MENU

def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN env var not set")
    
    app = Application.builder().token(token).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            State.MENU: [CallbackQueryHandler(menu_callback)],
            State.SELECT_PROJECT: [CallbackQueryHandler(menu_callback)],
            State.OP_TYPE: [CallbackQueryHandler(menu_callback)],
            State.CATEGORY: [CallbackQueryHandler(menu_callback)],
            State.AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_handler)],
            State.CONTRACTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, contractor_handler)],
            State.COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_handler)],
            State.CREATE_PROJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_project_handler)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)],
    )
    
    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == '__main__':
    main()
