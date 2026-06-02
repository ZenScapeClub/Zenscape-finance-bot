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

import base64
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
    DATE = 6
    CONTRACTOR = 7
    COMMENT = 8
    CREATE_PROJECT = 9

# Initialize Sheets API
def init_sheets():
    service_account_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_B64')
    if not service_account_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_B64 env var not set")
    
    service_account = json.loads(base64.b64decode(service_account_json).decode())
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
        setup_sheets(sheets)
    return sheets

def setup_sheets(spreadsheet):
    """Create required worksheets and headers if they don't exist"""
    existing = [ws.title for ws in spreadsheet.worksheets()]
    
    if 'Объекты' not in existing:
        ws = spreadsheet.add_worksheet('Объекты', rows=100, cols=15)
        ws.update('A1', [['ZenScape — Объекты']])
        ws.update('A2:O2', [['Название объекта', 'Статус', 'Дата начала', 'Адрес / Описание',
                             'План доход', 'План расход', 'План прибыль', 'План маржа',
                             'Факт доход', 'Факт расход', 'Факт прибыль', 'Факт маржа',
                             'Откл. доход', 'Откл. расход', 'Откл. прибыль']])
        logger.info("Created sheet: Объекты")
    
    if 'Операции' not in existing:
        ws = spreadsheet.add_worksheet('Операции', rows=1000, cols=9)
        ws.update('A1', [['ZenScape — Операции']])
        ws.update('A2:I2', [['ID', 'Дата', 'Объект', 'Тип', 'Категория',
                             'Сумма', 'Контрагент', 'Статус оплаты', 'Комментарий']])
        logger.info("Created sheet: Операции")
    
    if 'Дашборд' not in existing:
        ws = spreadsheet.add_worksheet('Дашборд', rows=100, cols=12)
        ws.update('A1', [['ZenScape — Финансовый дашборд']])
        logger.info("Created sheet: Дашборд")
    
    if 'Cash Flow' not in existing:
        ws = spreadsheet.add_worksheet('Cash Flow', rows=10, cols=26)
        logger.info("Created sheet: Cash Flow")
    
    if 'Категории' not in existing:
        ws = spreadsheet.add_worksheet('Категории', rows=10, cols=2)
        ws.update('A1:B1', [['Категория расхода', 'Категория дохода']])
        logger.info("Created sheet: Категории")
    
    # Remove default Sheet1 if it exists and our sheets are there
    if 'Sheet1' in existing and 'Объекты' in [ws.title for ws in spreadsheet.worksheets()]:
        try:
            spreadsheet.del_worksheet(spreadsheet.worksheet('Sheet1'))
        except Exception:
            pass

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

def add_operation(project: str, op_type: str, category: str, amount: float, contractor: str = "", comment: str = "", date: str = ""):
    """Add operation to Операции sheet"""
    try:
        ws = get_sheets().worksheet('Операции')
        next_row = len(ws.col_values(1)) + 1
        
        # Find next ID
        op_ids = ws.col_values(1)[2:]
        next_id = len([x for x in op_ids if x and x.strip()]) + 1
        
        ws.append_row([
            next_id,
            date if date else datetime.now().strftime('%d.%m.%Y'),
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
            date if date else datetime.now().strftime('%d.%m.%Y'),
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
        logger.error(f"Error creating project: {type(e).__name__}: {e}")
        return False

def _parse_num(val):
    """Safely parse number from cell value"""
    try:
        return float(str(val).replace(' ', '').replace(',', '.').replace(' ', '')) if val else 0.0
    except:
        return 0.0

def _calc_margin(income, expense):
    profit = income - expense
    return profit / income if income > 0 else 0.0

def get_operations_data():
    """Read all operations from Операции sheet"""
    try:
        ws = get_sheets().worksheet('Операции')
        rows = ws.get_all_values()[2:]  # skip title and header
        ops = []
        for row in rows:
            if len(row) >= 6 and row[0]:  # has ID
                ops.append({
                    'id': row[0],
                    'date': row[1],
                    'project': row[2],
                    'type': row[3],
                    'category': row[4],
                    'amount': _parse_num(row[5]),
                    'contractor': row[6] if len(row) > 6 else '',
                    'status': row[7] if len(row) > 7 else '',
                    'comment': row[8] if len(row) > 8 else '',
                })
        return ops
    except Exception as e:
        logger.error(f"Error reading operations: {e}")
        return []

def get_project_plan(project: str):
    """Get plan values from Объекты sheet"""
    try:
        ws = get_sheets().worksheet('Объекты')
        rows = ws.get_all_values()[2:]
        for row in rows:
            if row and row[0].strip() == project:
                return {
                    'status': row[1] if len(row) > 1 else '',
                    'plan_income': _parse_num(row[4]) if len(row) > 4 else 0,
                    'plan_expense': _parse_num(row[5]) if len(row) > 5 else 0,
                }
        return {'status': 'Активный', 'plan_income': 0, 'plan_expense': 0}
    except Exception as e:
        logger.error(f"Error reading plan: {e}")
        return {'status': '', 'plan_income': 0, 'plan_expense': 0}

def get_project_summary(project: str):
    """Calculate project summary from operations"""
    try:
        ops = get_operations_data()
        proj_ops = [o for o in ops if o['project'] == project]
        plan = get_project_plan(project)

        fact_income = sum(o['amount'] for o in proj_ops if o['type'] == 'Приход')
        fact_expense = sum(o['amount'] for o in proj_ops if o['type'] == 'Расход')
        fact_profit = fact_income - fact_expense
        fact_margin = fact_profit / fact_income if fact_income > 0 else 0.0

        plan_income = plan['plan_income']
        plan_expense = plan['plan_expense']
        plan_profit = plan_income - plan_expense
        plan_margin = plan_profit / plan_income if plan_income > 0 else 0.0

        return {
            'name': project,
            'status': plan['status'],
            'plan_income': plan_income,
            'plan_expense': plan_expense,
            'plan_profit': plan_profit,
            'plan_margin': plan_margin,
            'fact_income': fact_income,
            'fact_expense': fact_expense,
            'fact_profit': fact_profit,
            'fact_margin': fact_margin,
            'dev_profit': fact_profit - plan_profit,
        }
    except Exception as e:
        logger.error(f"Error in get_project_summary: {e}")
        return None

def get_all_summary():
    """Calculate total summary from operations"""
    try:
        ops = get_operations_data()
        fact_income = sum(o['amount'] for o in ops if o['type'] == 'Приход')
        fact_expense = sum(o['amount'] for o in ops if o['type'] == 'Расход')
        fact_profit = fact_income - fact_expense
        fact_margin = fact_profit / fact_income if fact_income > 0 else 0.0

        ws_obj = get_sheets().worksheet('Объекты')
        rows = ws_obj.get_all_values()[2:]
        plan_income = sum(_parse_num(r[4]) for r in rows if r and r[0] and len(r) > 4)
        plan_expense = sum(_parse_num(r[5]) for r in rows if r and r[0] and len(r) > 5)
        plan_profit = plan_income - plan_expense
        plan_margin = plan_profit / plan_income if plan_income > 0 else 0.0

        return {
            'fact_income': fact_income,
            'fact_expense': fact_expense,
            'fact_profit': fact_profit,
            'fact_margin': fact_margin,
            'plan_income': plan_income,
            'plan_expense': plan_expense,
            'plan_profit': plan_profit,
            'plan_margin': plan_margin,
        }
    except Exception as e:
        logger.error(f"Error in get_all_summary: {e}")
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
            def fmt(v):
                try: return f"{float(v):,.0f}".replace(',', ' ')
                except: return str(v) if v else "0"
            def pct(v):
                try: return f"{float(v)*100:.1f}%"
                except: return "—"
            
            text = (
                "📊 <b>Общий отчёт</b>\n\n"
                "<b>ФАКТ:</b>\n"
                "💰 Доход: " + fmt(summary['fact_income']) + " ₽\n"
                "💸 Расход: " + fmt(summary['fact_expense']) + " ₽\n"
                "📈 Прибыль: " + fmt(summary['fact_profit']) + " ₽\n"
                "📊 Маржа: " + pct(summary['fact_margin']) + "\n\n"
                "<b>ПЛАН:</b>\n"
                "💰 Доход: " + fmt(summary['plan_income']) + " ₽\n"
                "💸 Расход: " + fmt(summary['plan_expense']) + " ₽\n"
                "📈 Прибыль: " + fmt(summary['plan_profit']) + " ₽\n"
                "📊 Маржа: " + pct(summary['plan_margin'])
            )
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
            def fmt(v):
                try: return f"{float(v):,.0f}".replace(',', ' ')
                except: return str(v) if v else "0"
            def pct(v):
                try: return f"{float(v)*100:.1f}%"
                except: return "—"
            
            text = (
                "📊 <b>Отчёт: " + summary['name'] + "</b>\n\n"
                "<b>ФАКТ:</b>\n"
                "💰 Доход: " + fmt(summary['fact_income']) + " ₽\n"
                "💸 Расход: " + fmt(summary['fact_expense']) + " ₽\n"
                "📈 Прибыль: " + fmt(summary['fact_profit']) + " ₽\n"
                "📊 Маржа: " + pct(summary['fact_margin']) + "\n\n"
                "<b>ПЛАН:</b>\n"
                "💰 Доход: " + fmt(summary['plan_income']) + " ₽\n"
                "💸 Расход: " + fmt(summary['plan_expense']) + " ₽\n"
                "📈 Прибыль: " + fmt(summary['plan_profit']) + " ₽\n"
                "📊 Маржа: " + pct(summary['plan_margin']) + "\n"
                "📉 Откл. прибыль: " + fmt(summary['dev_profit']) + " ₽"
            )
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
            bio = io.BytesIO(csv_data.encode('utf-8-sig'))
            bio.name = filename
            await query.message.reply_document(document=bio, filename=filename, caption="✅ Выгрузка готова")
            await query.edit_message_text("✅ Файл отправлен выше.")
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
        amount = float(update.message.text.strip().replace(",", ".").replace(" ", ""))
        context.user_data['amount'] = amount
        from datetime import timedelta
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        keyboard = [
            [InlineKeyboardButton(f"📅 Сегодня ({today.strftime('%d.%m')})", callback_data="date_today")],
            [InlineKeyboardButton(f"📅 Вчера ({yesterday.strftime('%d.%m')})", callback_data="date_yesterday")],
            [InlineKeyboardButton("✏️ Своя дата", callback_data="date_custom")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Дата операции:", reply_markup=reply_markup)
        return State.DATE
    except ValueError:
        await update.message.reply_text("❌ Напиши число (например, 15000)")
        return State.AMOUNT

async def date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date selection via buttons"""
    query = update.callback_query
    await query.answer()
    from datetime import timedelta
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    if query.data == "date_today":
        context.user_data["date"] = today.strftime("%d.%m.%Y")
    elif query.data == "date_yesterday":
        context.user_data["date"] = yesterday.strftime("%d.%m.%Y")
    elif query.data == "date_custom":
        await query.edit_message_text("Напиши дату в формате ДД.ММ.ГГГГ (например, 15.05.2026):")
        return State.DATE
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_contractor")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("📅 Дата: " + context.user_data["date"] + "\n\nКонтрагент:", reply_markup=reply_markup)
    return State.CONTRACTOR

async def date_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle manual date input"""
    text = update.message.text.strip()
    try:
        datetime.strptime(text, "%d.%m.%Y")
        context.user_data["date"] = text
        keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_contractor")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("📅 Дата: " + text + "\n\nКонтрагент:", reply_markup=reply_markup)
        return State.CONTRACTOR
    except ValueError:
        await update.message.reply_text("❌ Формат неверный. Напиши ДД.ММ.ГГГГ (например, 15.05.2026):")
        return State.DATE

async def contractor_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle contractor input"""
    text = update.message.text.strip()
    if text.lower() == '/skip':
        context.user_data['contractor'] = ''
    else:
        context.user_data['contractor'] = text
    
    await update.message.reply_text("Комментарий (опционально, напиши или нажми /skip):")
    return State.COMMENT

async def contractor_skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle skip contractor button"""
    query = update.callback_query
    await query.answer()
    context.user_data['contractor'] = ''
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_comment")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Комментарий:", reply_markup=reply_markup)
    return State.COMMENT

async def comment_skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle skip comment button"""
    query = update.callback_query
    await query.answer()
    context.user_data['comment'] = ''
    return await save_operation(update, context, via_query=True)

async def comment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle comment and save operation"""
    context.user_data['comment'] = update.message.text.strip()
    
    return await save_operation(update, context, via_query=False)

async def save_operation(update, context, via_query=False):
    """Save operation and show result"""
    keyboard = [
        [InlineKeyboardButton("➕ Новая операция", callback_data='op_new')],
        [InlineKeyboardButton("📊 Отчет по всем", callback_data='report_all')],
        [InlineKeyboardButton("🏢 По объекту", callback_data='report_object')],
        [InlineKeyboardButton("📁 Создать объект", callback_data='create_project')],
        [InlineKeyboardButton("📥 Выгрузить CSV", callback_data='export')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if add_operation(
        context.user_data['project'],
        context.user_data['op_type'],
        context.user_data['category'],
        context.user_data['amount'],
        context.user_data.get('contractor', ''),
        context.user_data.get('comment', ''),
        context.user_data.get('date', '')
    ):
        text = ("✅ Операция сохранена!\n\n📍 Объект: " + context.user_data['project'] + "\n📌 Тип: " + context.user_data['op_type'] + "\n🏷 Категория: " + context.user_data['category'] + "\n💰 Сумма: " + str(context.user_data['amount']) + " ₽")
    else:
        text = "❌ Ошибка при сохранении операции."
    
    if via_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text)
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
            State.DATE: [CallbackQueryHandler(date_handler), MessageHandler(filters.TEXT & ~filters.COMMAND, date_text_handler)],
            State.CONTRACTOR: [CallbackQueryHandler(contractor_skip_handler, pattern='^skip_contractor$'), MessageHandler(filters.TEXT & ~filters.COMMAND, contractor_handler)],
            State.COMMENT: [CallbackQueryHandler(comment_skip_handler, pattern='^skip_comment$'), MessageHandler(filters.TEXT & ~filters.COMMAND, comment_handler)],
            State.CREATE_PROJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_project_handler)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)],
    )
    
    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == '__main__':
    main()
