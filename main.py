import os, json, csv, io, base64, logging
from datetime import datetime, timedelta
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters)
from telegram.constants import ParseMode
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALLOWED_USERS = {257170336}

def restricted(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ALLOWED_USERS:
            await update.effective_message.reply_text("Нет доступа.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

class S(Enum):
    MENU=1; SELECT_PROJECT=2; OP_TYPE=3; CATEGORY=4; AMOUNT=5
    DATE=6; PAY_STATUS=7; CONTRACTOR=8; COMMENT=9
    CP_NAME=20; CP_REVENUE=21; CP_EXPENSE=22
    EDIT_LIST=30

# ── Categories ────────────────────────────────────────────────────────────────
EXPENSE_CATS = ['Растения', 'Озеленение', 'Химия и уход', 'Оплата подряду',
                'Логистика', 'Командировка', 'Расходы на ЗСД', 'Прочее']
INCOME_CATS  = ['Поступление от клиента', 'Возврат', 'Прочие доходы']

# ── Sheets ────────────────────────────────────────────────────────────────────
_sheets = None

def get_sheets():
    global _sheets
    if _sheets is None:
        b64 = os.environ['GOOGLE_SERVICE_ACCOUNT_B64']
        sa  = json.loads(base64.b64decode(b64).decode())
        creds = Credentials.from_service_account_info(
            sa, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        _sheets = gspread.authorize(creds).open_by_key(os.environ['GOOGLE_SHEETS_ID'])
        _ensure_base_sheets(_sheets)
        _ensure_object_sheets(_sheets)
        _refresh_dashboard(_sheets)
    return _sheets

def _ws_titles(spreadsheet):
    return [ws.title for ws in spreadsheet.worksheets()]

def _ensure_base_sheets(sp):
    titles = _ws_titles(sp)
    if 'Объекты' not in titles:
        ws = sp.add_worksheet('Объекты', 100, 10)
        ws.update('A1:J1', [['Название','Статус','Дата начала','Адрес',
            'Сумма договора','План расход','План прибыль','План маржа %',
            'Факт доход','Факт расход']])
    if 'Операции' not in titles:
        ws = sp.add_worksheet('Операции', 1000, 9)
        ws.update('A1:I1', [['ID','Дата','Объект','Тип','Категория',
            'Сумма','Контрагент','Статус оплаты','Комментарий']])
    if 'Дашборд' not in titles:
        sp.add_worksheet('Дашборд', 200, 12)
    if 'Cash Flow' not in titles:
        sp.add_worksheet('Cash Flow', 20, 30)

def _ensure_object_sheets(sp):
    projects = _get_projects_raw(sp)
    titles = _ws_titles(sp)
    for p in projects:
        if p and p not in titles:
            _create_object_sheet(sp, p)

def _create_object_sheet(sp, name):
    try:
        ws = sp.add_worksheet(name[:50], 200, 10)
        _update_object_sheet(sp, name, ws)
        logger.info(f"Created sheet for {name}")
    except Exception as e:
        logger.error(f"Error creating sheet for {name}: {e}")

def _update_object_sheet(sp, name, ws=None):
    try:
        if ws is None:
            try:
                ws = sp.worksheet(name[:50])
            except:
                ws = sp.add_worksheet(name[:50], 200, 10)

        ops = _get_operations_raw(sp)
        p_ops = [o for o in ops if o['project'] == name]
        _, row = _get_project_row_raw(sp, name)

        plan_revenue = _num(row[4]) if row and len(row) > 4 else 0
        plan_expense = _num(row[5]) if row and len(row) > 5 else 0
        plan_profit  = plan_revenue - plan_expense
        plan_margin  = plan_profit / plan_revenue if plan_revenue > 0 else 0

        fact_income  = sum(o['amount'] for o in p_ops if o['type'] == 'Приход')
        fact_expense = sum(o['amount'] for o in p_ops if o['type'] == 'Расход')
        fact_profit  = fact_income - fact_expense
        fact_margin  = fact_profit / fact_income if fact_income > 0 else 0

        ws.clear()
        # Header block
        ws.update('A1:B1', [['Объект:', name]])
        ws.update('A2:B6', [
            ['Статус', row[1] if row and len(row)>1 else ''],
            ['Сумма договора', plan_revenue],
            ['План расход', plan_expense],
            ['План прибыль', plan_profit],
            ['План маржа', round(plan_margin * 100, 1)],
        ])
        ws.update('D2:E6', [
            ['Факт доход', fact_income],
            ['Факт расход', fact_expense],
            ['Факт прибыль', fact_profit],
            ['Факт маржа', round(fact_margin * 100, 1)],
            ['Откл. прибыль', fact_profit - plan_profit],
        ])
        # Operations table
        ws.update('A8:I8', [['ID','Дата','Тип','Категория','Сумма',
                              'Контрагент','Статус оплаты','Комментарий','']])
        if p_ops:
            rows_data = [[o['id'], o['date'], o['type'], o['category'],
                          o['amount'], o['contractor'], o['pay_status'], o['comment'], '']
                         for o in sorted(p_ops, key=lambda x: x['date'], reverse=True)]
            ws.update(f'A9:I{8+len(rows_data)}', rows_data)
    except Exception as e:
        logger.error(f"Error updating sheet {name}: {e}")

def _refresh_dashboard(sp):
    try:
        ws = sp.worksheet('Дашборд')
        ws.clear()
        projects = _get_projects_raw(sp)
        ops = _get_operations_raw(sp)

        all_income  = sum(o['amount'] for o in ops if o['type'] == 'Приход')
        all_expense = sum(o['amount'] for o in ops if o['type'] == 'Расход')
        all_profit  = all_income - all_expense
        all_margin  = all_profit / all_income if all_income > 0 else 0

        ws.update('A1', [['ZenScape — Финансовый дашборд']])
        ws.update('A2:B6', [
            ['Всего объектов', len(projects)],
            ['Факт доход', all_income],
            ['Факт расход', all_expense],
            ['Факт прибыль', all_profit],
            ['Факт маржа %', round(all_margin * 100, 1)],
        ])

        # Per-project table
        ws.update('A8:K8', [['Объект','Статус','Договор','План расход',
            'План прибыль','План маржа %','Факт доход','Факт расход',
            'Факт прибыль','Факт маржа %','Откл. прибыль']])

        obj_ws = sp.worksheet('Объекты')
        obj_rows = obj_ws.get_all_values()[1:]
        table = []
        for r in obj_rows:
            if not r or not r[0]:
                continue
            pname = r[0]
            p_ops = [o for o in ops if o['project'] == pname]
            plan_rev = _num(r[4]) if len(r) > 4 else 0
            plan_exp = _num(r[5]) if len(r) > 5 else 0
            plan_prf = plan_rev - plan_exp
            plan_mrg = plan_prf / plan_rev if plan_rev > 0 else 0
            f_inc = sum(o['amount'] for o in p_ops if o['type'] == 'Приход')
            f_exp = sum(o['amount'] for o in p_ops if o['type'] == 'Расход')
            f_prf = f_inc - f_exp
            f_mrg = f_prf / f_inc if f_inc > 0 else 0
            table.append([pname, r[1] if len(r)>1 else '',
                plan_rev, plan_exp, plan_prf, round(plan_mrg*100,1),
                f_inc, f_exp, f_prf, round(f_mrg*100,1), f_prf - plan_prf])
        if table:
            ws.update(f'A9:K{8+len(table)}', table)
    except Exception as e:
        logger.error(f"Dashboard refresh error: {e}")

def _refresh_cashflow(sp):
    try:
        ws = sp.worksheet('Cash Flow')
        ws.clear()
        ops = _get_operations_raw(sp)

        months = []
        now = datetime.now()
        for i in range(-3, 9):
            m = now.month + i
            y = now.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            months.append((y, m))

        header = ['Показатель'] + [f"{m:02d}.{y}" for y, m in months]
        ws.update('A1', [header])

        def month_ops(y, m, t):
            return sum(o['amount'] for o in ops
                if o['type'] == t and _parse_date(o['date']) and
                _parse_date(o['date']).year == y and _parse_date(o['date']).month == m)

        income_row  = ['Приход']
        expense_row = ['Расход']
        saldo_row   = ['Сальдо']
        cumul_row   = ['Накопительно']
        cumul = 0
        for y, m in months:
            inc = month_ops(y, m, 'Приход')
            exp = month_ops(y, m, 'Расход')
            sal = inc - exp
            cumul += sal
            income_row.append(inc)
            expense_row.append(exp)
            saldo_row.append(sal)
            cumul_row.append(cumul)

        ws.update('A2:A5', [['Приход'], ['Расход'], ['Сальдо'], ['Накопительно']])
        for i, row in enumerate([income_row, expense_row, saldo_row, cumul_row], 2):
            ws.update_cell(i, 1, row[0])
            for j, val in enumerate(row[1:], 2):
                ws.update_cell(i, j, val)
    except Exception as e:
        logger.error(f"Cash flow error: {e}")

def _parse_date(s):
    for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except:
            pass
    return None

# ── Raw data helpers ──────────────────────────────────────────────────────────
def _num(val):
    try:
        return float(str(val).replace(' ','').replace(',','.').replace('\xa0','').replace('₽','').strip())
    except:
        return 0.0

def _fmt(v):
    try: return f"{float(v):,.0f}".replace(',', ' ')
    except: return str(v) if v else "0"

def _pct(v):
    try: return f"{float(v)*100:.1f}%"
    except: return "—"

def _get_projects_raw(sp):
    try:
        rows = sp.worksheet('Объекты').get_all_values()[1:]
        return [r[0].strip() for r in rows if r and r[0].strip()]
    except:
        return []

def _get_project_row_raw(sp, project):
    try:
        rows = sp.worksheet('Объекты').get_all_values()[1:]
        for i, r in enumerate(rows):
            if r and r[0].strip() == project:
                return i + 2, r
        return None, None
    except:
        return None, None

def _get_operations_raw(sp):
    try:
        rows = sp.worksheet('Операции').get_all_values()[1:]
        ops = []
        for r in rows:
            if r and r[0]:
                ops.append({'id':r[0],'date':r[1],'project':r[2],'type':r[3],
                    'category':r[4],'amount':_num(r[5]),
                    'contractor':r[6] if len(r)>6 else '',
                    'pay_status':r[7] if len(r)>7 else '',
                    'comment':r[8] if len(r)>8 else ''})
        return ops
    except:
        return []

# ── Public data functions ─────────────────────────────────────────────────────
def get_projects():
    return _get_projects_raw(get_sheets())

def get_project_summary(project):
    sp = get_sheets()
    ops = _get_operations_raw(sp)
    p_ops = [o for o in ops if o['project'] == project]
    _, row = _get_project_row_raw(sp, project)
    if row is None:
        return None
    plan_revenue = _num(row[4]) if len(row)>4 else 0
    plan_expense = _num(row[5]) if len(row)>5 else 0
    plan_profit  = plan_revenue - plan_expense
    plan_margin  = plan_profit / plan_revenue if plan_revenue > 0 else 0
    fact_income  = sum(o['amount'] for o in p_ops if o['type'] == 'Приход')
    fact_expense = sum(o['amount'] for o in p_ops if o['type'] == 'Расход')
    fact_profit  = fact_income - fact_expense
    fact_margin  = fact_profit / fact_income if fact_income > 0 else 0
    return {
        'name': project, 'status': row[1] if len(row)>1 else '',
        'plan_revenue': plan_revenue, 'plan_expense': plan_expense,
        'plan_profit': plan_profit, 'plan_margin': plan_margin,
        'fact_income': fact_income, 'fact_expense': fact_expense,
        'fact_profit': fact_profit, 'fact_margin': fact_margin,
        'dev_profit': fact_profit - plan_profit,
        'recent_ops': sorted(p_ops, key=lambda x: x['date'], reverse=True)[:5],
    }

def get_all_summary():
    sp = get_sheets()
    ops = _get_operations_raw(sp)
    obj_rows = sp.worksheet('Объекты').get_all_values()[1:]
    fact_income  = sum(o['amount'] for o in ops if o['type'] == 'Приход')
    fact_expense = sum(o['amount'] for o in ops if o['type'] == 'Расход')
    fact_profit  = fact_income - fact_expense
    fact_margin  = fact_profit / fact_income if fact_income > 0 else 0
    plan_revenue = sum(_num(r[4]) for r in obj_rows if r and r[0] and len(r)>4)
    plan_expense = sum(_num(r[5]) for r in obj_rows if r and r[0] and len(r)>5)
    plan_profit  = plan_revenue - plan_expense
    plan_margin  = plan_profit / plan_revenue if plan_revenue > 0 else 0
    return {'fact_income':fact_income,'fact_expense':fact_expense,
            'fact_profit':fact_profit,'fact_margin':fact_margin,
            'plan_revenue':plan_revenue,'plan_expense':plan_expense,
            'plan_profit':plan_profit,'plan_margin':plan_margin,
            'projects_count':len([r for r in obj_rows if r and r[0]])}

def add_operation(project, op_type, category, amount, date, pay_status, contractor='', comment=''):
    sp = get_sheets()
    ws = sp.worksheet('Операции')
    rows = ws.get_all_values()[1:]
    next_id = len([r for r in rows if r and r[0]]) + 1
    ws.append_row([next_id, date, project, op_type, category,
                   amount, contractor, pay_status, comment])
    # Update object sheet + dashboard + cashflow
    _update_object_sheet(sp, project)
    _refresh_dashboard(sp)
    _refresh_cashflow(sp)
    # Budget warning
    warning = None
    _, proj_row = _get_project_row_raw(sp, project)
    if proj_row and op_type == 'Расход':
        plan_exp = _num(proj_row[5]) if len(proj_row)>5 else 0
        if plan_exp > 0:
            ops = _get_operations_raw(sp)
            total_exp = sum(o['amount'] for o in ops if o['project']==project and o['type']=='Расход')
            pct = total_exp / plan_exp
            if pct >= 0.8:
                warning = f"⚠️ Расходы {project}: {round(pct*100,0):.0f}% от плана ({_fmt(total_exp)} / {_fmt(plan_exp)} ₽)"
    return next_id, warning

def create_project(name, revenue, plan_expense):
    sp = get_sheets()
    plan_profit = revenue - plan_expense
    plan_margin = plan_profit / revenue if revenue > 0 else 0
    sp.worksheet('Объекты').append_row([
        name, 'Активный', datetime.now().strftime('%d.%m.%Y'), '',
        revenue, plan_expense, plan_profit, round(plan_margin*100, 1), 0, 0])
    _create_object_sheet(sp, name)
    _refresh_dashboard(sp)

def delete_operation(op_id):
    sp = get_sheets()
    ws = sp.worksheet('Операции')
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], 2):
        if row and str(row[0]) == str(op_id):
            project = row[2] if len(row) > 2 else None
            ws.delete_rows(i)
            if project:
                _update_object_sheet(sp, project)
                _refresh_dashboard(sp)
                _refresh_cashflow(sp)
            return True
    return False

def get_recent_ops(project=None, limit=10):
    ops = _get_operations_raw(get_sheets())
    if project:
        ops = [o for o in ops if o['project'] == project]
    return sorted(ops, key=lambda x: x['date'], reverse=True)[:limit]

# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_kb():
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

# ── Handlers ──────────────────────────────────────────────────────────────────
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🌱 ZenScape — финансовый учёт\n\nЧто хочешь сделать?", reply_markup=main_kb())
    return S.MENU

@restricted
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == 'back_to_menu':
        context.user_data.clear()
        await q.edit_message_text("Что хочешь сделать?", reply_markup=main_kb())
        return S.MENU

    # ── New operation ──
    if d == 'op_new':
        projects = get_projects()
        if not projects:
            await q.edit_message_text("Нет объектов. Создай объект сначала.", reply_markup=back_kb())
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
            ]))
        return S.OP_TYPE

    if d.startswith('type_'):
        context.user_data['op_type'] = d[5:]
        cats = INCOME_CATS if d[5:] == 'Приход' else EXPENSE_CATS
        kb = [[InlineKeyboardButton(c, callback_data=f'cat_{c}')] for c in cats]
        await q.edit_message_text(f"Категория ({d[5:]}):", reply_markup=InlineKeyboardMarkup(kb))
        return S.CATEGORY

    if d.startswith('cat_'):
        context.user_data['category'] = d[4:]
        await q.edit_message_text("Сумма (₽):")
        return S.AMOUNT

    if d in ('date_today','date_yesterday','date_custom'):
        if d == 'date_today':
            context.user_data['date'] = datetime.now().strftime('%d.%m.%Y')
        elif d == 'date_yesterday':
            context.user_data['date'] = (datetime.now()-timedelta(days=1)).strftime('%d.%m.%Y')
        else:
            await q.edit_message_text("Напиши дату в формате ДД.ММ.ГГГГ:")
            return S.DATE
        return await _show_pay_status(q, context)

    if d.startswith('pay_'):
        context.user_data['pay_status'] = d[4:]
        await q.edit_message_text("Контрагент:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_contractor')]]))
        return S.CONTRACTOR

    if d == 'skip_contractor':
        context.user_data['contractor'] = ''
        await q.edit_message_text("Комментарий:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_comment')]]))
        return S.COMMENT

    if d == 'skip_comment':
        context.user_data['comment'] = ''
        return await _save_op_q(q, context)

    # ── Reports ──
    if d == 'report_all':
        s = get_all_summary()
        text = (f"📊 <b>Общий отчёт</b>\n\nОбъектов: {s['projects_count']}\n\n"
                f"<b>ФАКТ:</b>\n"
                f"💰 Доход: {_fmt(s['fact_income'])} ₽\n"
                f"💸 Расход: {_fmt(s['fact_expense'])} ₽\n"
                f"📈 Прибыль: {_fmt(s['fact_profit'])} ₽\n"
                f"📊 Маржа: {_pct(s['fact_margin'])}\n\n"
                f"<b>ПЛАН:</b>\n"
                f"💰 Выручка: {_fmt(s['plan_revenue'])} ₽\n"
                f"💸 Расход: {_fmt(s['plan_expense'])} ₽\n"
                f"📈 Прибыль: {_fmt(s['plan_profit'])} ₽\n"
                f"📊 Маржа: {_pct(s['plan_margin'])}")
        await q.edit_message_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        return S.MENU

    if d == 'report_object':
        projects = get_projects()
        kb = [[InlineKeyboardButton(p, callback_data=f'rpt_{p}')] for p in projects]
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Выбери объект:", reply_markup=InlineKeyboardMarkup(kb))
        return S.MENU

    if d.startswith('rpt_'):
        s = get_project_summary(d[4:])
        if not s:
            await q.edit_message_text("Объект не найден.", reply_markup=back_kb())
            return S.MENU
        text = (f"📊 <b>{s['name']}</b> ({s['status']})\n\n"
                f"<b>ПЛАН:</b>\n"
                f"💰 Договор: {_fmt(s['plan_revenue'])} ₽\n"
                f"💸 Расход: {_fmt(s['plan_expense'])} ₽\n"
                f"📈 Прибыль: {_fmt(s['plan_profit'])} ₽\n"
                f"📊 Маржа: {_pct(s['plan_margin'])}\n\n"
                f"<b>ФАКТ:</b>\n"
                f"💰 Доход: {_fmt(s['fact_income'])} ₽\n"
                f"💸 Расход: {_fmt(s['fact_expense'])} ₽\n"
                f"📈 Прибыль: {_fmt(s['fact_profit'])} ₽\n"
                f"📊 Маржа: {_pct(s['fact_margin'])}\n\n"
                f"📉 Откл.: {_fmt(s['dev_profit'])} ₽\n")
        if s['recent_ops']:
            text += "\n<b>Последние операции:</b>\n"
            for o in s['recent_ops']:
                sign = "+" if o['type'] == 'Приход' else "-"
                text += f"  {o['date']}  {sign}{_fmt(o['amount'])} ₽  {o['category']}\n"
        await q.edit_message_text(text, reply_markup=back_kb(), parse_mode=ParseMode.HTML)
        return S.MENU

    # ── Create project ──
    if d == 'create_project':
        await q.edit_message_text("Название нового объекта:")
        return S.CP_NAME

    # ── Edit operations ──
    if d == 'edit_ops':
        ops = get_recent_ops(limit=10)
        if not ops:
            await q.edit_message_text("Операций нет.", reply_markup=back_kb())
            return S.MENU
        kb = []
        for o in ops:
            sign = "+" if o['type'] == 'Приход' else "-"
            label = f"{o['date']} {o['project'][:10]} {sign}{_fmt(o['amount'])}₽"
            kb.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f'del_{o["id"]}')])
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Нажми 🗑 для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return S.EDIT_LIST

    if d.startswith('del_'):
        if delete_operation(d[4:]):
            await q.answer("✅ Удалено", show_alert=True)
        else:
            await q.answer("❌ Ошибка", show_alert=True)
        ops = get_recent_ops(limit=10)
        if not ops:
            await q.edit_message_text("Операций больше нет.", reply_markup=back_kb())
            return S.MENU
        kb = []
        for o in ops:
            sign = "+" if o['type'] == 'Приход' else "-"
            label = f"{o['date']} {o['project'][:10]} {sign}{_fmt(o['amount'])}₽"
            kb.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f'del_{o["id"]}')])
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Нажми 🗑 для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return S.EDIT_LIST

    # ── Export ──
    if d == 'export':
        projects = get_projects()
        kb = [[InlineKeyboardButton("📦 Все объекты", callback_data='exp_all')]]
        kb += [[InlineKeyboardButton(p, callback_data=f'expp_{p}')] for p in projects]
        kb.append([InlineKeyboardButton("◀ Назад", callback_data='back_to_menu')])
        await q.edit_message_text("Выгрузить:", reply_markup=InlineKeyboardMarkup(kb))
        return S.MENU

    if d in ('exp_all',) or d.startswith('expp_'):
        project = None if d == 'exp_all' else d[5:]
        ops = _get_operations_raw(get_sheets())
        if project:
            ops = [o for o in ops if o['project'] == project]
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(['ID','Дата','Объект','Тип','Категория','Сумма','Контрагент','Статус оплаты','Комментарий'])
        for o in ops:
            w.writerow([o['id'],o['date'],o['project'],o['type'],o['category'],
                        o['amount'],o['contractor'],o['pay_status'],o['comment']])
        bio = io.BytesIO(out.getvalue().encode('utf-8-sig'))
        fname = f"zenscape_{project or 'all'}.csv"
        await q.message.reply_document(document=bio, filename=fname, caption="✅ Готово")
        await q.edit_message_text("✅ Файл отправлен.", reply_markup=back_kb())
        return S.MENU

    return S.MENU

async def _show_pay_status(q, context):
    await q.edit_message_text(
        f"📅 {context.user_data['date']}\n\nСтатус оплаты:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Оплачено", callback_data='pay_Оплачено')],
            [InlineKeyboardButton("⏳ Ожидает", callback_data='pay_Ожидает')],
            [InlineKeyboardButton("🔸 Частично", callback_data='pay_Частично')],
        ]))
    return S.PAY_STATUS

async def _save_op_q(q, context):
    op_id, warning = add_operation(
        context.user_data['project'], context.user_data['op_type'],
        context.user_data['category'], context.user_data['amount'],
        context.user_data.get('date', datetime.now().strftime('%d.%m.%Y')),
        context.user_data.get('pay_status', 'Оплачено'),
        context.user_data.get('contractor', ''), context.user_data.get('comment', ''))
    text = (f"✅ Сохранено!\n\n"
            f"📍 {context.user_data['project']}\n"
            f"{'💰' if context.user_data['op_type']=='Приход' else '💸'} "
            f"{context.user_data['op_type']} — {_fmt(context.user_data['amount'])} ₽\n"
            f"🏷 {context.user_data['category']}\n"
            f"📅 {context.user_data.get('date','')}\n"
            f"💳 {context.user_data.get('pay_status','')}")
    if warning:
        text += f"\n\n{warning}"
    context.user_data.clear()
    await q.edit_message_text(text, reply_markup=main_kb())
    return S.MENU

async def _save_op_msg(update, context):
    op_id, warning = add_operation(
        context.user_data['project'], context.user_data['op_type'],
        context.user_data['category'], context.user_data['amount'],
        context.user_data.get('date', datetime.now().strftime('%d.%m.%Y')),
        context.user_data.get('pay_status', 'Оплачено'),
        context.user_data.get('contractor', ''), context.user_data.get('comment', ''))
    text = (f"✅ Сохранено!\n\n"
            f"📍 {context.user_data['project']}\n"
            f"{'💰' if context.user_data['op_type']=='Приход' else '💸'} "
            f"{context.user_data['op_type']} — {_fmt(context.user_data['amount'])} ₽\n"
            f"🏷 {context.user_data['category']}\n"
            f"📅 {context.user_data.get('date','')}\n"
            f"💳 {context.user_data.get('pay_status','')}")
    if warning:
        text += f"\n\n{warning}"
    context.user_data.clear()
    await update.message.reply_text(text, reply_markup=main_kb())
    return S.MENU

# ── Text input handlers ───────────────────────────────────────────────────────
@restricted
async def amount_handler(update, context):
    try:
        context.user_data['amount'] = float(update.message.text.strip().replace(',','.').replace(' ',''))
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        await update.message.reply_text("Дата операции:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📅 Сегодня ({today.strftime('%d.%m')})", callback_data='date_today')],
            [InlineKeyboardButton(f"📅 Вчера ({yesterday.strftime('%d.%m')})", callback_data='date_yesterday')],
            [InlineKeyboardButton("✏️ Своя дата", callback_data='date_custom')],
        ]))
        return S.DATE
    except ValueError:
        await update.message.reply_text("❌ Введи число, например: 15000")
        return S.AMOUNT

@restricted
async def date_text_handler(update, context):
    try:
        datetime.strptime(update.message.text.strip(), '%d.%m.%Y')
        context.user_data['date'] = update.message.text.strip()
        await update.message.reply_text(
            f"📅 {context.user_data['date']}\n\nСтатус оплаты:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Оплачено", callback_data='pay_Оплачено')],
                [InlineKeyboardButton("⏳ Ожидает", callback_data='pay_Ожидает')],
                [InlineKeyboardButton("🔸 Частично", callback_data='pay_Частично')],
            ]))
        return S.PAY_STATUS
    except ValueError:
        await update.message.reply_text("❌ Формат: ДД.ММ.ГГГГ")
        return S.DATE

@restricted
async def contractor_handler(update, context):
    context.user_data['contractor'] = update.message.text.strip()
    await update.message.reply_text("Комментарий:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_comment')]]))
    return S.COMMENT

@restricted
async def comment_handler(update, context):
    context.user_data['comment'] = update.message.text.strip()
    return await _save_op_msg(update, context)

@restricted
async def cp_name_handler(update, context):
    context.user_data['cp_name'] = update.message.text.strip()
    await update.message.reply_text(
        f"Объект: <b>{context.user_data['cp_name']}</b>\n\nСумма договора (₽):",
        parse_mode=ParseMode.HTML)
    return S.CP_REVENUE

@restricted
async def cp_revenue_handler(update, context):
    try:
        context.user_data['cp_revenue'] = float(update.message.text.strip().replace(',','.').replace(' ',''))
        await update.message.reply_text(
            f"Выручка: {_fmt(context.user_data['cp_revenue'])} ₽\n\nПлановые расходы (₽):\n<i>0 если неизвестно</i>",
            parse_mode=ParseMode.HTML)
        return S.CP_EXPENSE
    except ValueError:
        await update.message.reply_text("❌ Введи число")
        return S.CP_REVENUE

@restricted
async def cp_expense_handler(update, context):
    try:
        expense = float(update.message.text.strip().replace(',','.').replace(' ',''))
        name = context.user_data['cp_name']
        revenue = context.user_data['cp_revenue']
        profit = revenue - expense
        margin = profit / revenue if revenue > 0 else 0
        create_project(name, revenue, expense)
        await update.message.reply_text(
            f"✅ Объект создан!\n\n"
            f"📍 {name}\n"
            f"💰 Договор: {_fmt(revenue)} ₽\n"
            f"💸 Расход: {_fmt(expense)} ₽\n"
            f"📈 Прибыль: {_fmt(profit)} ₽\n"
            f"📊 Маржа: {_pct(margin)}\n\n"
            f"Вкладка в таблице создана автоматически.",
            reply_markup=main_kb())
        context.user_data.clear()
        return S.MENU
    except ValueError:
        await update.message.reply_text("❌ Введи число")
        return S.CP_EXPENSE

@restricted
async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=main_kb())
    return S.MENU

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(os.environ['TELEGRAM_BOT_TOKEN']).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            S.MENU:           [CallbackQueryHandler(menu_cb)],
            S.SELECT_PROJECT: [CallbackQueryHandler(menu_cb)],
            S.OP_TYPE:        [CallbackQueryHandler(menu_cb)],
            S.CATEGORY:       [CallbackQueryHandler(menu_cb)],
            S.AMOUNT:         [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_handler)],
            S.DATE:           [CallbackQueryHandler(menu_cb, pattern='^date_'),
                               MessageHandler(filters.TEXT & ~filters.COMMAND, date_text_handler)],
            S.PAY_STATUS:     [CallbackQueryHandler(menu_cb, pattern='^pay_')],
            S.CONTRACTOR:     [CallbackQueryHandler(menu_cb, pattern='^skip_contractor$'),
                               MessageHandler(filters.TEXT & ~filters.COMMAND, contractor_handler)],
            S.COMMENT:        [CallbackQueryHandler(menu_cb, pattern='^skip_comment$'),
                               MessageHandler(filters.TEXT & ~filters.COMMAND, comment_handler)],
            S.CP_NAME:        [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_name_handler)],
            S.CP_REVENUE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_revenue_handler)],
            S.CP_EXPENSE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_expense_handler)],
            S.EDIT_LIST:      [CallbackQueryHandler(menu_cb)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start)],
    )
    app.add_handler(conv)
    app.run_polling()

if __name__ == '__main__':
    main()
