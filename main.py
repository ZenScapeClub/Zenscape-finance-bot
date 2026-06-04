import os
import json
import csv
import io
import base64
import logging
from datetime import datetime, timedelta
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── AUTH ───────────────────────────────────────────────────────────────────
ALLOWED_USERS = {257170336}

def restricted(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USERS:
            await update.effective_message.reply_text("Нет доступа.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

# ─── STATES ─────────────────────────────────────────────────────────────────
class S(Enum):
    MENU = 1
    SELECT_PROJECT = 2
    OP_TYPE = 3
    CATEGORY = 4
    AMOUNT = 5
    DATE = 6
    PAY_STATUS = 7
    CONTRACTOR = 8
    COMMENT = 9
    # Create project
    CP_NAME = 20
    CP_REVENUE = 21
    CP_EXPENSE = 22
    # Edit
    EDIT_LIST = 30

# ─── SHEETS ─────────────────────────────────────────────────────────────────
_sheets = None

def get_sheets():
    global _sheets
    if _sheets is None:
        b64 = os.environ.get('GOOGLE_SERVICE_ACCOUNT_B64')
        if not b64:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_B64 not set")
        sa = json.loads(base64.b64decode(b64).decode())
        creds = Credentials.from_service_account_info(
            sa, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        client = gspread.authorize(creds)
        sheet_id = os.environ.get('GOOGLE_SHEETS_ID')
        if not sheet_id:
            raise ValueError("GOOGLE_SHEETS_ID not set")
        _sheets = client.open_by_key(sheet_id)
        _ensure_sheets(_sheets)
    return _sheets

def _ensure_sheets(spreadsheet):
    existing = [ws.title for ws in spreadsheet.worksheets()]
    if 'Объекты' not in existing:
        ws = spreadsheet.add_worksheet('Объекты', rows=100, cols=10)
        ws.update('A1:J1', [['Название', 'Статус', 'Дата начала', 'Адрес',
                              'Сумма договора', 'План расход', 'План прибыль', 'План маржа',
                              'Факт доход', 'Факт расход']])
    if 'Операции' not in existing:
        ws = spreadsheet.add_worksheet('Операции', rows=1000, cols=9)
        ws.update('A1:I1', [['ID', 'Дата', 'Объект', 'Тип', 'Категория',
                              'Сумма', 'Контрагент', 'Статус оплаты', 'Комментарий']])
    for name in ['Sheet1']:
        if name in existing and len(existing) > 2:
            try:
                spreadsheet.del_worksheet(spreadsheet.worksheet(name))
            except:
                pass

# ─── DATA HELPERS ────────────────────────────────────────────────────────────
def _num(val):
    try:
        return float(str(val).replace(' ', '').replace(',', '.').replace('\xa0', '').replace('₽','').strip())
    except:
        return 0.0

def _fmt(v):
    try:
        return f"{float(v):,.0f}".replace(',', ' ')
    except:
        return str(v) if v else "0"

def _pct(v):
    try:
        return f"{float(v)*100:.1f}%"
    except:
        return "—"

def get_projects():
    ws = get_sheets().worksheet('Объекты')
    rows = ws.get_all_values()[1:]
    return [r[0].strip() for r in rows if r and r[0].strip()]

def get_project_row(project):
    ws = get_sheets().worksheet('Объекты')
    rows = ws.get_all_values()[1:]
    for i, r in enumerate(rows):
        if r and r[0].strip() == project:
            return i + 2, r  # 1-indexed + header
    return None, None

def get_operations():
    ws = get_sheets().worksheet('Операции')
    rows = ws.get_all_values()[1:]
    ops = []
    for r in rows:
        if r and r[0]:
            ops.append({
                'id': r[0], 'date': r[1], 'project': r[2],
                'type': r[3], 'category': r[4], 'amount': _num(r[5]),
                'contractor': r[6] if len(r) > 6 else '',
                'pay_status': r[7] if len(r) > 7 else '',
                'comment': r[8] if len(r) > 8 else '',
                '_row': None
            })
    return ops

def get_project_summary(project):
    ops = get_operations()
    p_ops = [o for o in ops if o['project'] == project]
    _, row = get_project_row(project)
    if row is None:
        return None
    plan_revenue = _num(row[4]) if len(row) > 4 else 0
    plan_expense = _num(row[5]) if len(row) > 5 else 0
    plan_profit = plan_revenue - plan_expense
    plan_margin = plan_profit / plan_revenue if plan_revenue > 0 else 0.0

    fact_income = sum(o['amount'] for o in p_ops if o['type'] == 'Приход')
    fact_expense = sum(o['amount'] for o in p_ops if o['type'] == 'Расход')
    fact_profit = fact_income - fact_expense
    fact_margin = fact_profit / fact_income if fact_income > 0 else 0.0

    return {
        'name': project,
        'status': row[1] if len(row) > 1 else '',
        'plan_revenue': plan_revenue,
        'plan_expense': plan_expense,
        'plan_profit': plan_profit,
        'plan_margin': plan_margin,
        'fact_income': fact_income,
        'fact_expense': fact_expense,
        'fact_profit': fact_profit,
        'fact_margin': fact_margin,
        'dev_profit': fact_profit - plan_profit,
        'recent_ops': sorted(p_ops, key=lambda x: x['date'], reverse=True)[:5],
    }

def get_all_summary():
    ops = get_operations()
    ws = get_sheets().worksheet('Объекты')
    rows = ws.get_all_values()[1:]

    fact_income = sum(o['amount'] for o in ops if o['type'] == 'Приход')
    fact_expense = sum(o['amount'] for o in ops if o['type'] == 'Расход')
    fact_profit = fact_income - fact_expense
    fact_margin = fact_profit / fact_income if fact_income > 0 else 0.0

    plan_revenue = sum(_num(r[4]) for r in rows if r and r[0] and len(r) > 4)
    plan_expense = sum(_num(r[5]) for r in rows if r and r[0] and len(r) > 5)
    plan_profit = plan_revenue - plan_expense
    plan_margin = plan_profit / plan_revenue if plan_revenue > 0 else 0.0

    return {
        'fact_income': fact_income, 'fact_expense': fact_expense,
        'fact_profit': fact_profit, 'fact_margin': fact_margin,
        'plan_revenue': plan_revenue, 'plan_expense': plan_expense,
        'plan_profit': plan_profit, 'plan_margin': plan_margin,
        'projects_count': len([r for r in rows if r and r[0]]),
    }

def add_operation(project, op_type, category, amount, date, pay_status, contractor='', comment=''):
    ws = get_sheets().worksheet('Операции')
    rows = ws.get_all_values()[1:]
    next_id = len([r for r in rows if r and r[0]]) + 1
    ws.append_row([next_id, date, project, op_type, category,
                   amount, contractor, pay_status, comment])
    # Check budget warning
    warning = None
    _, proj_row = get_project_row(project)
    if proj_row and op_type == 'Расход':
        plan_expense = _num(proj_row[5]) if len(proj_row) > 5 else 0
        if plan_expense > 0:
            ops = get_operations()
            total_expense = sum(o['amount'] for o in ops if o['project'] == project and o['type'] == 'Расход')
            pct = total_expense / plan_expense
            if pct >= 0.8:
                warning = f"⚠️ Расходы по объекту {project}: {_pct(pct)} от планового бюджета ({_fmt(total_expense)} / {_fmt(plan_expense)} ₽)"
    return next_id, warning

def create_project(name, revenue, plan_expense):
    ws = get_sheets().worksheet('Объекты')
    plan_profit = revenue - plan_expense
    plan_margin = plan_profit / revenue if revenue > 0 else 0.0
    ws.append_row([name, 'Активный', datetime.now().strftime('%d.%m.%Y'), '',
                   revenue, plan_expense, plan_profit,
                   round(plan_margin * 100, 1),
                   0, 0])

def delete_operation(op_id):
    ws = get_sheets().worksheet('Операции')
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], 2):
        if row and str(row[0]) == str(op_id):
            ws.delete_rows(i)
            return True
    return False

def get_recent_operations(project=None, limit=10):
    ops = get_operations()
    if project:
        ops = [o for o in ops if o['project'] == project]
    return sorted(ops, key=lambda x: x['date'], reverse=True)[:limit]

# ─── KEYBOARDS ───────────────────────────────────────────────────────────────
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Новая операция", callback_data='op_new')],
        [InlineKeyboardButton("📊 Общий отчёт", callback_data='report_all'),
         InlineKeyboardButton("🏢 По объекту", callback_data='report_object')],
        [InlineKeyboardButton("📁 Создать объект", callback_data='create_project'),
         InlineKeyboardButton("✏️ Операции", callback_data='edit_ops')],
        [InlineKeyboardButton("📥 Выгрузить CSV", callback_data='export')],
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')]])

# ─── HANDLERS ────────────────────────────────────────────────────────────────
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🌱 ZenScape — финансовый учёт\n\nЧто хочешь сделать?",
        reply_markup=main_menu_kb()
    )
    return S.MENU

@restricted
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    # ── BACK ──
    if d == 'back_to_menu':
        context.user_data.clear()
        await q.edit_message_text("Что хочешь сделать?", reply_markup=main_menu_kb())
        return S.MENU

    # ── NEW OPERATION ──
    if d == 'op_new':
        projects = get_projects()
        if not projects:
            await q.edit_message_text("❌ Нет объектов. Создай объект сначала.", reply_markup=back_kb())
            return S.MENU
        kb = [[InlineKeyboardButton(p, callback_data=f'proj_{p}')] for p in projects]
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Выбери объект:", reply_markup=InlineKeyboardMarkup(kb))
        return S.SELECT_PROJECT

    if d.startswith('proj_'):
        context.user_data['project'] = d[5:]
        await q.edit_message_text(
            f"📍 {context.user_data['project']}\n\nТип операции:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 Приход", callback_data='type_Приход')],
                [InlineKeyboardButton("💸 Расход", callback_data='type_Расход')],
                [InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')],
            ])
        )
        return S.OP_TYPE

    if d.startswith('type_'):
        context.user_data['op_type'] = d[5:]
        cats = (['Оплата от клиента', 'Аванс', 'Доплата', 'Прочее']
                if context.user_data['op_type'] == 'Приход'
                else ['Растения', 'Материалы', 'Субподряд', 'Рабочие', 'Доставка', 'Накладные', 'Прочее'])
        kb = [[InlineKeyboardButton(c, callback_data=f'cat_{c}')] for c in cats]
        await q.edit_message_text(f"Категория ({context.user_data['op_type']}):", reply_markup=InlineKeyboardMarkup(kb))
        return S.CATEGORY

    if d.startswith('cat_'):
        context.user_data['category'] = d[4:]
        await q.edit_message_text("Сумма (₽):")
        return S.AMOUNT

    # ── DATE ──
    if d in ('date_today', 'date_yesterday', 'date_custom'):
        if d == 'date_today':
            context.user_data['date'] = datetime.now().strftime('%d.%m.%Y')
        elif d == 'date_yesterday':
            context.user_data['date'] = (datetime.now() - timedelta(days=1)).strftime('%d.%m.%Y')
        elif d == 'date_custom':
            await q.edit_message_text("Напиши дату в формате ДД.ММ.ГГГГ:")
            return S.DATE
        await q.edit_message_text(
            f"📅 {context.user_data['date']}\n\nСтатус оплаты:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Оплачено", callback_data='pay_Оплачено')],
                [InlineKeyboardButton("⏳ Ожидает", callback_data='pay_Ожидает')],
                [InlineKeyboardButton("🔸 Частично", callback_data='pay_Частично')],
            ])
        )
        return S.PAY_STATUS

    # ── PAY STATUS ──
    if d.startswith('pay_'):
        context.user_data['pay_status'] = d[4:]
        kb = [[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_contractor')]]
        await q.edit_message_text("Контрагент:", reply_markup=InlineKeyboardMarkup(kb))
        return S.CONTRACTOR

    if d == 'skip_contractor':
        context.user_data['contractor'] = ''
        kb = [[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_comment')]]
        await q.edit_message_text("Комментарий:", reply_markup=InlineKeyboardMarkup(kb))
        return S.COMMENT

    if d == 'skip_comment':
        context.user_data['comment'] = ''
        return await _save_op(q, context)

    # ── REPORT ALL ──
    if d == 'report_all':
        s = get_all_summary()
        text = (
            "📊 <b>Общий отчёт</b>\n\n"
            f"Объектов: {s['projects_count']}\n\n"
            "<b>ФАКТ:</b>\n"
            f"💰 Доход: {_fmt(s['fact_income'])} ₽\n"
            f"💸 Расход: {_fmt(s['fact_expense'])} ₽\n"
            f"📈 Прибыль: {_fmt(s['fact_profit'])} ₽\n"
            f"📊 Маржа: {_pct(s['fact_margin'])}\n\n"
            "<b>ПЛАН:</b>\n"
            f"💰 Выручка: {_fmt(s['plan_revenue'])} ₽\n"
            f"💸 Расход: {_fmt(s['plan_expense'])} ₽\n"
            f"📈 Прибыль: {_fmt(s['plan_profit'])} ₽\n"
            f"📊 Маржа: {_pct(s['plan_margin'])}"
        )
        await q.edit_message_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        return S.MENU

    # ── REPORT OBJECT ──
    if d == 'report_object':
        projects = get_projects()
        kb = [[InlineKeyboardButton(p, callback_data=f'rpt_{p}')] for p in projects]
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Выбери объект:", reply_markup=InlineKeyboardMarkup(kb))
        return S.MENU

    if d.startswith('rpt_'):
        project = d[4:]
        s = get_project_summary(project)
        if not s:
            await q.edit_message_text("Объект не найден.", reply_markup=back_kb())
            return S.MENU
        text = (
            f"📊 <b>{s['name']}</b> ({s['status']})\n\n"
            "<b>ПЛАН:</b>\n"
            f"💰 Выручка (договор): {_fmt(s['plan_revenue'])} ₽\n"
            f"💸 Расход: {_fmt(s['plan_expense'])} ₽\n"
            f"📈 Прибыль: {_fmt(s['plan_profit'])} ₽\n"
            f"📊 Маржа: {_pct(s['plan_margin'])}\n\n"
            "<b>ФАКТ:</b>\n"
            f"💰 Доход: {_fmt(s['fact_income'])} ₽\n"
            f"💸 Расход: {_fmt(s['fact_expense'])} ₽\n"
            f"📈 Прибыль: {_fmt(s['fact_profit'])} ₽\n"
            f"📊 Маржа: {_pct(s['fact_margin'])}\n\n"
            f"📉 Откл. прибыль: {_fmt(s['dev_profit'])} ₽\n"
        )
        if s['recent_ops']:
            text += "\n<b>Последние операции:</b>\n"
            for o in s['recent_ops']:
                sign = "+" if o['type'] == 'Приход' else "-"
                text += f"  {o['date']} {sign}{_fmt(o['amount'])} ₽ {o['category']}\n"
        await q.edit_message_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        return S.MENU

    # ── CREATE PROJECT ──
    if d == 'create_project':
        await q.edit_message_text("Название нового объекта:")
        return S.CP_NAME

    # ── EDIT OPERATIONS ──
    if d == 'edit_ops':
        ops = get_recent_operations(limit=10)
        if not ops:
            await q.edit_message_text("Операций пока нет.", reply_markup=back_kb())
            return S.MENU
        kb = []
        for o in ops:
            sign = "+" if o['type'] == 'Приход' else "-"
            label = f"{o['date']} {o['project'][:12]} {sign}{_fmt(o['amount'])}₽"
            kb.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f'del_{o["id"]}')])
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Последние операции\nНажми 🗑 чтобы удалить:", reply_markup=InlineKeyboardMarkup(kb))
        return S.EDIT_LIST

    if d.startswith('del_'):
        op_id = d[4:]
        if delete_operation(op_id):
            await q.answer("✅ Удалено", show_alert=True)
        else:
            await q.answer("❌ Ошибка", show_alert=True)
        # Refresh list
        ops = get_recent_operations(limit=10)
        if not ops:
            await q.edit_message_text("Операций больше нет.", reply_markup=back_kb())
            return S.MENU
        kb = []
        for o in ops:
            sign = "+" if o['type'] == 'Приход' else "-"
            label = f"{o['date']} {o['project'][:12]} {sign}{_fmt(o['amount'])}₽"
            kb.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f'del_{o["id"]}')])
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Последние операции\nНажми 🗑 чтобы удалить:", reply_markup=InlineKeyboardMarkup(kb))
        return S.EDIT_LIST

    # ── EXPORT ──
    if d == 'export':
        projects = get_projects()
        kb = [[InlineKeyboardButton("📦 Все объекты", callback_data='export_all')]]
        kb += [[InlineKeyboardButton(p, callback_data=f'exportp_{p}')] for p in projects]
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Выгрузить:", reply_markup=InlineKeyboardMarkup(kb))
        return S.MENU

    if d == 'export_all' or d.startswith('exportp_'):
        project = None if d == 'export_all' else d[8:]
        ops = get_operations()
        if project:
            ops = [o for o in ops if o['project'] == project]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['ID', 'Дата', 'Объект', 'Тип', 'Категория', 'Сумма', 'Контрагент', 'Статус оплаты', 'Комментарий'])
        for o in ops:
            writer.writerow([o['id'], o['date'], o['project'], o['type'],
                             o['category'], o['amount'], o['contractor'],
                             o['pay_status'], o['comment']])
        bio = io.BytesIO(output.getvalue().encode('utf-8-sig'))
        filename = f"zenscape_{project or 'all'}.csv"
        await q.message.reply_document(document=bio, filename=filename, caption="✅ Выгрузка готова")
        await q.edit_message_text("✅ Файл отправлен.", reply_markup=back_kb())
        return S.MENU

    return S.MENU

# ─── TEXT HANDLERS ───────────────────────────────────────────────────────────
@restricted
async def amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(',', '.').replace(' ', ''))
        context.user_data['amount'] = amount
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        kb = [
            [InlineKeyboardButton(f"📅 Сегодня ({today.strftime('%d.%m')})", callback_data='date_today')],
            [InlineKeyboardButton(f"📅 Вчера ({yesterday.strftime('%d.%m')})", callback_data='date_yesterday')],
            [InlineKeyboardButton("✏️ Своя дата", callback_data='date_custom')],
        ]
        await update.message.reply_text("Дата операции:", reply_markup=InlineKeyboardMarkup(kb))
        return S.DATE
    except ValueError:
        await update.message.reply_text("❌ Введи число, например: 15000")
        return S.AMOUNT

@restricted
async def date_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        datetime.strptime(text, '%d.%m.%Y')
        context.user_data['date'] = text
        await update.message.reply_text(
            f"📅 {text}\n\nСтатус оплаты:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Оплачено", callback_data='pay_Оплачено')],
                [InlineKeyboardButton("⏳ Ожидает", callback_data='pay_Ожидает')],
                [InlineKeyboardButton("🔸 Частично", callback_data='pay_Частично')],
            ])
        )
        return S.PAY_STATUS
    except ValueError:
        await update.message.reply_text("❌ Формат: ДД.ММ.ГГГГ (например, 15.05.2026)")
        return S.DATE

@restricted
async def contractor_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['contractor'] = update.message.text.strip()
    kb = [[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_comment')]]
    await update.message.reply_text("Комментарий:", reply_markup=InlineKeyboardMarkup(kb))
    return S.COMMENT

@restricted
async def comment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['comment'] = update.message.text.strip()
    return await _save_op_msg(update, context)

async def _save_op(q, context):
    op_id, warning = add_operation(
        context.user_data['project'],
        context.user_data['op_type'],
        context.user_data['category'],
        context.user_data['amount'],
        context.user_data.get('date', datetime.now().strftime('%d.%m.%Y')),
        context.user_data.get('pay_status', 'Оплачено'),
        context.user_data.get('contractor', ''),
        context.user_data.get('comment', ''),
    )
    text = (
        "✅ Сохранено!\n\n"
        f"📍 {context.user_data['project']}\n"
        f"{'💰' if context.user_data['op_type'] == 'Приход' else '💸'} "
        f"{context.user_data['op_type']} — {_fmt(context.user_data['amount'])} ₽\n"
        f"🏷 {context.user_data['category']}\n"
        f"📅 {context.user_data.get('date', '')}\n"
        f"💳 {context.user_data.get('pay_status', '')}"
    )
    if warning:
        text += f"\n\n{warning}"
    context.user_data.clear()
    await q.edit_message_text(text, reply_markup=main_menu_kb())
    return S.MENU

async def _save_op_msg(update, context):
    op_id, warning = add_operation(
        context.user_data['project'],
        context.user_data['op_type'],
        context.user_data['category'],
        context.user_data['amount'],
        context.user_data.get('date', datetime.now().strftime('%d.%m.%Y')),
        context.user_data.get('pay_status', 'Оплачено'),
        context.user_data.get('contractor', ''),
        context.user_data.get('comment', ''),
    )
    text = (
        "✅ Сохранено!\n\n"
        f"📍 {context.user_data['project']}\n"
        f"{'💰' if context.user_data['op_type'] == 'Приход' else '💸'} "
        f"{context.user_data['op_type']} — {_fmt(context.user_data['amount'])} ₽\n"
        f"🏷 {context.user_data['category']}\n"
        f"📅 {context.user_data.get('date', '')}\n"
        f"💳 {context.user_data.get('pay_status', '')}"
    )
    if warning:
        text += f"\n\n{warning}"
    context.user_data.clear()
    await update.message.reply_text(text, reply_markup=main_menu_kb())
    return S.MENU

# ─── CREATE PROJECT FLOW ─────────────────────────────────────────────────────
@restricted
async def cp_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_name'] = update.message.text.strip()
    await update.message.reply_text(
        f"Объект: <b>{context.user_data['cp_name']}</b>\n\n"
        "Сумма договора (плановая выручка, ₽):",
        parse_mode=ParseMode.HTML
    )
    return S.CP_REVENUE

@restricted
async def cp_revenue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['cp_revenue'] = float(
            update.message.text.strip().replace(',', '.').replace(' ', ''))
        plan_margin_hint = ""
        await update.message.reply_text(
            f"Сумма договора: {_fmt(context.user_data['cp_revenue'])} ₽\n\n"
            "Плановые расходы (₽):\n"
            "<i>Введи 0 если пока неизвестно</i>",
            parse_mode=ParseMode.HTML
        )
        return S.CP_EXPENSE
    except ValueError:
        await update.message.reply_text("❌ Введи число, например: 500000")
        return S.CP_REVENUE

@restricted
async def cp_expense_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        expense = float(update.message.text.strip().replace(',', '.').replace(' ', ''))
        name = context.user_data['cp_name']
        revenue = context.user_data['cp_revenue']
        profit = revenue - expense
        margin = profit / revenue if revenue > 0 else 0.0
        create_project(name, revenue, expense)
        await update.message.reply_text(
            f"✅ Объект создан!\n\n"
            f"📍 {name}\n"
            f"💰 Выручка: {_fmt(revenue)} ₽\n"
            f"💸 Расход: {_fmt(expense)} ₽\n"
            f"📈 Прибыль: {_fmt(profit)} ₽\n"
            f"📊 Плановая маржа: {_pct(margin)}",
            reply_markup=main_menu_kb()
        )
        context.user_data.clear()
        return S.MENU
    except ValueError:
        await update.message.reply_text("❌ Введи число, например: 300000")
        return S.CP_EXPENSE

@restricted
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=main_menu_kb())
    return S.MENU

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            S.MENU: [CallbackQueryHandler(menu_cb)],
            S.SELECT_PROJECT: [CallbackQueryHandler(menu_cb)],
            S.OP_TYPE: [CallbackQueryHandler(menu_cb)],
            S.CATEGORY: [CallbackQueryHandler(menu_cb)],
            S.AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_handler)],
            S.DATE: [
                CallbackQueryHandler(menu_cb, pattern='^date_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, date_text_handler),
            ],
            S.PAY_STATUS: [CallbackQueryHandler(menu_cb, pattern='^pay_')],
            S.CONTRACTOR: [
                CallbackQueryHandler(menu_cb, pattern='^skip_contractor$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, contractor_handler),
            ],
            S.COMMENT: [
                CallbackQueryHandler(menu_cb, pattern='^skip_comment$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, comment_handler),
            ],
            S.CP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_name_handler)],
            S.CP_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_revenue_handler)],
            S.CP_EXPENSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_expense_handler)],
            S.EDIT_LIST: [CallbackQueryHandler(menu_cb)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)],
    )

    app.add_handler(conv)
    app.run_polling()

if __name__ == '__main__':
    main()
