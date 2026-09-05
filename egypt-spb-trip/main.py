"""
Telegram-бот + Mini App «Египет + Петербург, октябрь 2026».

Один процесс делает две вещи:
  1. Telegram-бот (long polling) — план по дням, билеты, виза, транспорт,
     рестораны, день рождения, бюджет, чек-лист, ежедневные напоминания.
  2. HTTP-сервер на PORT (по умолчанию 80) — раздаёт Mini App (webapp/index.html)
     с уже встроенными данными и JSON-API /api/trip.

Все данные лежат в data/trip.json — бот и Mini App читают один и тот же файл.

Переменные окружения:
  TELEGRAM_BOT_TOKEN  — токен бота (обязательно)
  WEBAPP_URL          — https-адрес Mini App (например https://<проект>-<логин>.amvera.io/)
  ALLOWED_USERS       — id пользователей через запятую; пусто = доступ всем
  ADMIN_USERS         — id админов (команда /reload); по умолчанию = ALLOWED_USERS
  PORT                — порт HTTP-сервера (80)
  STATE_DIR           — каталог для состояния (чек-лист, подписки); /data на Amvera
"""
import os, json, logging, threading, html, re
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      WebAppInfo, MenuButtonWebApp, BotCommand)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          ContextTypes)
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('trip-bot')

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / 'data' / 'trip.json'
WEBAPP_DIR = BASE / 'webapp'

_default_state = '/data' if os.path.isdir('/data') and os.access('/data', os.W_OK) else str(BASE / 'state')
STATE_DIR = Path(os.environ.get('STATE_DIR', _default_state))
STATE_FILE = STATE_DIR / 'trip_state.json'

WEBAPP_URL = os.environ.get('WEBAPP_URL', '').strip()
PORT = int(os.environ.get('PORT', '80'))

TZ_EGYPT = ZoneInfo('Africa/Cairo')
TZ_SPB = ZoneInfo('Europe/Moscow')

# ── Доступ ────────────────────────────────────────────────────────────────────

def _parse_ids(env_var, fallback=''):
    val = os.environ.get(env_var, fallback)
    return {int(x.strip()) for x in val.split(',') if x.strip().lstrip('-').isdigit()}

ALLOWED_USERS = _parse_ids('ALLOWED_USERS')
ADMIN_USERS = _parse_ids('ADMIN_USERS') or ALLOWED_USERS

def _allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return update.effective_user is not None and update.effective_user.id in ALLOWED_USERS

def guarded(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _allowed(update):
            if update.callback_query:
                await update.callback_query.answer('Нет доступа', show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text('Нет доступа.')
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

# ── Данные поездки ────────────────────────────────────────────────────────────

TRIP: dict = {}

def load_trip():
    global TRIP
    with open(DATA_FILE, encoding='utf-8') as f:
        TRIP = json.load(f)
    logger.info('trip.json loaded: %d days', len(TRIP.get('days', [])))
    return TRIP

def trip_start() -> date:
    return date.fromisoformat(TRIP['meta']['start'])

def trip_end() -> date:
    return date.fromisoformat(TRIP['meta']['end'])

def day_by_date(d: date):
    for day in TRIP['days']:
        if day['date'] == d.isoformat():
            return day
    return None

def day_by_n(n: int):
    for day in TRIP['days']:
        if day['n'] == n:
            return day
    return None

def now_local() -> datetime:
    """Текущее время «где мы сейчас»: до вылета — Петербург, в Египте — Каир."""
    today_spb = datetime.now(TZ_SPB).date()
    day = day_by_date(today_spb)
    tz = TZ_EGYPT if (day and day.get('city') == 'egypt') else TZ_SPB
    return datetime.now(tz)

# ── Состояние (чек-лист, подписки) ────────────────────────────────────────────

_state_lock = threading.Lock()

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'done': {}, 'remind': []}

def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)

def toggle_done(chat_id: int, item_id: str) -> bool:
    with _state_lock:
        st = load_state()
        done = set(st['done'].get(str(chat_id), []))
        if item_id in done:
            done.discard(item_id)
            val = False
        else:
            done.add(item_id)
            val = True
        st['done'][str(chat_id)] = sorted(done)
        save_state(st)
        return val

def done_set(chat_id: int) -> set:
    return set(load_state()['done'].get(str(chat_id), []))

def set_remind(chat_id: int, on: bool):
    with _state_lock:
        st = load_state()
        chats = set(st.get('remind', []))
        (chats.add if on else chats.discard)(chat_id)
        st['remind'] = sorted(chats)
        save_state(st)

def remind_chats() -> list:
    return list(load_state().get('remind', []))

# ── Форматирование ────────────────────────────────────────────────────────────

RU_WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
RU_MON = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

def ru_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f'{RU_WD[d.weekday()]}, {d.day} {RU_MON[d.month - 1]}'

def esc(s) -> str:
    return html.escape(str(s or ''), quote=False)

def a(url: str, text: str = 'ссылка') -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'

BLOCK_ICON = {
    'fly': '✈️', 'move': '🚕', 'see': '📍', 'meal': '🍽', 'book': '📝',
    'rest': '🏖', 'dive': '🤿', 'party': '🎂', 'hotel': '🏨', 'shop': '🛍',
    'tip': '💡', 'bus': '🚌', 'boat': '⛵', 'walk': '🚶', 'car': '🚗',
}

def fmt_block(b: dict) -> str:
    lines = []
    t = f"<b>{esc(b['time'])}</b> · " if b.get('time') else ''
    lines.append(f"{t}{BLOCK_ICON.get(b.get('type', ''), '•')} <b>{esc(b['title'])}</b>")
    if b.get('text'):
        lines.append(esc(b['text']))
    if b.get('how'):
        lines.append(f"🚕 {esc(b['how'])}")
    if b.get('price'):
        lines.append(f"💵 {esc(b['price'])}")
    if b.get('link'):
        lines.append('🔗 ' + a(b['link'], b.get('link_text', 'ссылка')))
    return '\n'.join(lines)

def fmt_day(d: dict) -> str:
    head = f"<b>День {d['n']} · {ru_date(d['date'])} · {esc(d['place'])}</b>"
    parts = [head]
    if d.get('highlight'):
        parts.append('🎂 <b>ДЕНЬ РОЖДЕНИЯ</b>')
    parts.append(f"<i>{esc(d['title'])}</i>")
    if d.get('summary'):
        parts.append(esc(d['summary']))
    meta = []
    if d.get('weather'):
        meta.append(f"🌤 {esc(d['weather'])}")
    if d.get('stay'):
        meta.append(f"🏨 {esc(d['stay'])}")
    if meta:
        parts.append('\n'.join(meta))
    for b in d.get('blocks', []):
        parts.append(fmt_block(b))
    if d.get('tips'):
        parts.append('\n'.join(f'💡 {esc(t)}' for t in d['tips']))
    return '\n\n'.join(parts)

def fmt_days_list() -> str:
    lines = [f"<b>{esc(TRIP['meta']['title'])}</b>", esc(TRIP['meta'].get('subtitle', '')), '']
    for d in TRIP['days']:
        mark = '🎂 ' if d.get('highlight') else ''
        flag = '🇪🇬' if d.get('city') == 'egypt' else '🇷🇺'
        lines.append(f"{flag} <b>{d['n']}</b> · {ru_date(d['date'])} — {mark}{esc(d['title'])}")
    return '\n'.join(lines)

def fmt_flights() -> str:
    f = TRIP['flights']
    parts = ['<b>✈️ Билеты</b>', esc(f.get('intro', ''))]
    for o in f.get('options', []):
        rec = ' ⭐' if o.get('recommended') else ''
        block = [f"<b>{esc(o['name'])}{rec}</b>"]
        for k, label in (('route', '🛫'), ('airline', '🏷'), ('days', '📅'), ('duration', '⏱'), ('price', '💵')):
            if o.get(k):
                block.append(f"{label} {esc(o[k])}")
        if o.get('pros'):
            block.append(f"➕ {esc(o['pros'])}")
        if o.get('cons'):
            block.append(f"➖ {esc(o['cons'])}")
        if o.get('link'):
            block.append('🔗 ' + a(o['link'], o.get('link_text', 'смотреть цены')))
        parts.append('\n'.join(block))
    if f.get('recommendation'):
        parts.append(f"<b>Рекомендация:</b> {esc(f['recommendation'])}")
    if f.get('tips'):
        parts.append('\n'.join(f'💡 {esc(t)}' for t in f['tips']))
    return '\n\n'.join(p for p in parts if p)

def fmt_visa() -> str:
    v = TRIP['visa']
    parts = ['<b>🛂 Виза и въезд</b>', esc(v.get('intro', ''))]
    for key in ('ru', 'by'):
        sec = v.get(key)
        if not sec:
            continue
        lines = [f"<b>{esc(sec['title'])}</b>"]
        for i, s in enumerate(sec.get('steps', []), 1):
            lines.append(f"{i}. {esc(s)}")
        parts.append('\n'.join(lines))
    if v.get('notes'):
        parts.append('\n'.join(f'⚠️ {esc(t)}' for t in v['notes']))
    if v.get('links'):
        parts.append('\n'.join('🔗 ' + a(l['url'], l['title']) for l in v['links']))
    return '\n\n'.join(p for p in parts if p)

def fmt_transport() -> str:
    t = TRIP['transport']
    parts = ['<b>🚗 Транспорт и аренда авто</b>', esc(t.get('intro', ''))]
    car = t.get('car', {})
    if car:
        lines = [f"<b>Аренда машины: {esc(car.get('verdict', ''))}</b>"]
        if car.get('text'):
            lines.append(esc(car['text']))
        for r in car.get('requirements', []):
            lines.append(f'• {esc(r)}')
        parts.append('\n'.join(lines))
        for c in car.get('companies', []):
            block = [f"<b>{esc(c['name'])}</b> — {esc(c.get('where', ''))}"]
            if c.get('price'):
                block.append(f"💵 {esc(c['price'])}")
            if c.get('notes'):
                block.append(esc(c['notes']))
            if c.get('link'):
                block.append('🔗 ' + a(c['link'], c.get('link_text', 'сайт')))
            parts.append('\n'.join(block))
    if t.get('alternatives'):
        lines = ['<b>Как ещё передвигаться</b>']
        for alt in t['alternatives']:
            line = f"• <b>{esc(alt['name'])}</b>"
            if alt.get('price'):
                line += f" — {esc(alt['price'])}"
            if alt.get('notes'):
                line += f". {esc(alt['notes'])}"
            if alt.get('link'):
                line += ' ' + a(alt['link'], '↗')
            lines.append(line)
        parts.append('\n'.join(lines))
    if t.get('spb'):
        parts.append('<b>Петербург</b>\n' + '\n'.join(f'• {esc(x)}' for x in t['spb']))
    return '\n\n'.join(p for p in parts if p)

def fmt_hotels() -> str:
    parts = ['<b>🏨 Отели</b>', esc(TRIP.get('hotels_intro', ''))]
    seg = None
    for h in TRIP['hotels']:
        if h.get('segment') != seg:
            seg = h.get('segment')
            parts.append(f"<b>— {esc(seg)} —</b>")
        rec = ' ⭐' if h.get('recommended') else ''
        block = [f"<b>{esc(h['name'])}</b>{rec} · {esc(h.get('area', ''))}"]
        if h.get('price'):
            block.append(f"💵 {esc(h['price'])}")
        if h.get('why'):
            block.append(esc(h['why']))
        if h.get('link'):
            block.append('🔗 ' + a(h['link'], h.get('link_text', 'бронировать')))
        parts.append('\n'.join(block))
    return '\n\n'.join(p for p in parts if p)

def fmt_food(city: str) -> str:
    title = {'egypt': '🇪🇬 Рестораны в Египте', 'spb': '🇷🇺 Рестораны в Петербурге'}.get(city, 'Рестораны')
    parts = [f'<b>🍽 {title}</b>']
    area = None
    for r in TRIP['restaurants']:
        if r.get('city') != city:
            continue
        if r.get('area') != area:
            area = r.get('area')
            parts.append(f"<b>— {esc(area)} —</b>")
        rec = ' ⭐' if r.get('recommended') else ''
        block = [f"<b>{esc(r['name'])}</b>{rec} · {esc(r.get('cuisine', ''))}"]
        if r.get('when'):
            block.append(f"🕒 {esc(r['when'])}")
        if r.get('price'):
            block.append(f"💵 {esc(r['price'])}")
        if r.get('must'):
            block.append(f"👉 {esc(r['must'])}")
        if r.get('notes'):
            block.append(esc(r['notes']))
        if r.get('link'):
            block.append('🔗 ' + a(r['link'], r.get('link_text', 'ссылка')))
        parts.append('\n'.join(block))
    return '\n\n'.join(parts)

def fmt_birthday() -> str:
    b = TRIP['birthday']
    parts = [f"<b>🎂 День рождения · {ru_date(TRIP['meta']['birthday'])}</b>", esc(b.get('intro', ''))]
    for o in b.get('options', []):
        rank = f"#{o['rank']} " if o.get('rank') else ''
        block = [f"<b>{rank}{esc(o['name'])}</b> · {esc(o.get('type_label', ''))}"]
        if o.get('text'):
            block.append(esc(o['text']))
        if o.get('price'):
            block.append(f"💵 {esc(o['price'])}")
        if o.get('includes'):
            block.append(f"✅ {esc(o['includes'])}")
        if o.get('pros'):
            block.append(f"➕ {esc(o['pros'])}")
        if o.get('cons'):
            block.append(f"➖ {esc(o['cons'])}")
        if o.get('how_to_book'):
            block.append(f"📝 {esc(o['how_to_book'])}")
        if o.get('link'):
            block.append('🔗 ' + a(o['link'], o.get('link_text', 'сайт')))
        parts.append('\n'.join(block))
    if b.get('recommendation'):
        parts.append(f"<b>Что выбрать:</b> {esc(b['recommendation'])}")
    if b.get('plan'):
        parts.append('<b>План на 25–26 октября</b>\n' + '\n'.join(f'• {esc(p)}' for p in b['plan']))
    return '\n\n'.join(p for p in parts if p)

def fmt_budget() -> str:
    bd = TRIP['budget']
    lines = ['<b>💰 Бюджет на двоих</b>', esc(bd.get('intro', '')), '']
    for it in bd.get('items', []):
        lines.append(f"• {esc(it['name'])}: <b>{esc(it['amount'])}</b>" + (f" — {esc(it['note'])}" if it.get('note') else ''))
    if bd.get('total'):
        lines.append('')
        lines.append(f"<b>Итого: {esc(bd['total'])}</b>")
    if bd.get('notes'):
        lines.append('')
        lines.extend(f'💡 {esc(n)}' for n in bd['notes'])
    return '\n'.join(lines)

def fmt_practical(idx: int = None) -> str:
    secs = TRIP['practical']['sections']
    if idx is None:
        lines = ['<b>ℹ️ Полезное</b>', 'Выберите раздел кнопкой ниже.']
        return '\n'.join(lines)
    s = secs[idx]
    lines = [f"<b>{esc(s['title'])}</b>", '']
    for it in s['items']:
        lines.append(f'• {esc(it)}')
    return '\n'.join(lines)

def split_message(text: str, limit: int = 3900) -> list:
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ''
    for para in text.split('\n\n'):
        cand = (cur + '\n\n' + para) if cur else para
        if len(cand) > limit and cur:
            chunks.append(cur)
            cur = para
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks

# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _webapp_button():
    if WEBAPP_URL.startswith('https://'):
        return InlineKeyboardButton('📱 Открыть приложение', web_app=WebAppInfo(url=WEBAPP_URL))
    return None

def main_kb() -> InlineKeyboardMarkup:
    rows = []
    wb = _webapp_button()
    if wb:
        rows.append([wb])
    rows += [
        [InlineKeyboardButton('📅 Сегодня', callback_data='today'),
         InlineKeyboardButton('🗓 Все дни', callback_data='days')],
        [InlineKeyboardButton('✈️ Билеты', callback_data='sec:flights'),
         InlineKeyboardButton('🛂 Виза', callback_data='sec:visa')],
        [InlineKeyboardButton('🚗 Транспорт', callback_data='sec:transport'),
         InlineKeyboardButton('🏨 Отели', callback_data='sec:hotels')],
        [InlineKeyboardButton('🍽 Еда Египет', callback_data='food:egypt'),
         InlineKeyboardButton('🍽 Еда СПб', callback_data='food:spb')],
        [InlineKeyboardButton('🎂 День рождения', callback_data='sec:birthday'),
         InlineKeyboardButton('💰 Бюджет', callback_data='sec:budget')],
        [InlineKeyboardButton('✅ Чек-лист', callback_data='chk'),
         InlineKeyboardButton('ℹ️ Полезное', callback_data='info')],
    ]
    return InlineKeyboardMarkup(rows)

def back_kb(extra_rows=None) -> InlineKeyboardMarkup:
    rows = list(extra_rows or [])
    rows.append([InlineKeyboardButton('◀️ Меню', callback_data='menu')])
    return InlineKeyboardMarkup(rows)

def days_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for d in TRIP['days']:
        label = f"{'🎂' if d.get('highlight') else ''}{d['n']}"
        row.append(InlineKeyboardButton(label, callback_data=f"day:{d['n']}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton('◀️ Меню', callback_data='menu')])
    return InlineKeyboardMarkup(rows)

def day_kb(n: int) -> InlineKeyboardMarkup:
    total = len(TRIP['days'])
    nav = []
    if n > 1:
        nav.append(InlineKeyboardButton('◀️ Пред.', callback_data=f'day:{n - 1}'))
    nav.append(InlineKeyboardButton('🗓 Дни', callback_data='days'))
    if n < total:
        nav.append(InlineKeyboardButton('След. ▶️', callback_data=f'day:{n + 1}'))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton('◀️ Меню', callback_data='menu')]])

def checklist_sections_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(s['title'], callback_data=f'chk:sec:{i}')]
            for i, s in enumerate(TRIP['checklist']['sections'])]
    rows.append([InlineKeyboardButton('◀️ Меню', callback_data='menu')])
    return InlineKeyboardMarkup(rows)

def checklist_kb(chat_id: int, idx: int) -> InlineKeyboardMarkup:
    sec = TRIP['checklist']['sections'][idx]
    done = done_set(chat_id)
    rows = []
    for it in sec['items']:
        mark = '✅' if it['id'] in done else '⬜️'
        text = f"{mark} {it['text']}"
        if it.get('due'):
            text += f" · до {ru_date(it['due'])}"
        rows.append([InlineKeyboardButton(text[:60], callback_data=f"chk:t:{idx}:{it['id']}")])
    rows.append([InlineKeyboardButton('◀️ Разделы', callback_data='chk'),
                 InlineKeyboardButton('Меню', callback_data='menu')])
    return InlineKeyboardMarkup(rows)

def practical_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(s['title'], callback_data=f'info:{i}')]
            for i, s in enumerate(TRIP['practical']['sections'])]
    rows.append([InlineKeyboardButton('◀️ Меню', callback_data='menu')])
    return InlineKeyboardMarkup(rows)

# ── Отправка ──────────────────────────────────────────────────────────────────

async def send_long(message, text: str, kb=None):
    chunks = split_message(text)
    for i, ch in enumerate(chunks):
        await message.reply_text(ch, parse_mode=ParseMode.HTML,
                                 reply_markup=kb if i == len(chunks) - 1 else None,
                                 disable_web_page_preview=True)

async def edit_or_send(q, text: str, kb=None):
    """Для callback: если текст влезает — редактируем, иначе шлём новыми сообщениями."""
    chunks = split_message(text)
    if len(chunks) == 1:
        try:
            await q.edit_message_text(chunks[0], parse_mode=ParseMode.HTML,
                                      reply_markup=kb, disable_web_page_preview=True)
            return
        except Exception as e:  # message is not modified / too old
            logger.debug('edit failed: %s', e)
    await send_long(q.message, text, kb)

def countdown_text() -> str:
    today = now_local().date()
    start, end = trip_start(), trip_end()
    if today < start:
        n = (start - today).days
        return f'До поездки {n} дн.'
    if today <= end:
        return f'Идёт день {(today - start).days + 1} из {(end - start).days + 1}'
    return 'Поездка завершена — пора планировать следующую 😉'

def welcome_text() -> str:
    m = TRIP['meta']
    lines = [f"<b>{esc(m['title'])}</b>", esc(m.get('subtitle', '')), '',
             f"📆 {ru_date(m['start'])} — {ru_date(m['end'])} · {countdown_text()}",
             f"🎂 День рождения: {ru_date(m['birthday'])}", '']
    if not WEBAPP_URL.startswith('https://'):
        lines.append('<i>Mini App не подключён: задайте WEBAPP_URL в настройках сервера.</i>')
        lines.append('')
    lines.append('Выберите раздел:')
    return '\n'.join(lines)

# ── Хендлеры ──────────────────────────────────────────────────────────────────

@guarded
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_remind(update.effective_chat.id, True)
    await update.effective_message.reply_text(welcome_text(), parse_mode=ParseMode.HTML,
                                              reply_markup=main_kb(), disable_web_page_preview=True)

@guarded
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ('<b>Команды</b>\n'
            '/start — меню\n/app — открыть Mini App\n/today — план на сегодня\n'
            '/tomorrow — план на завтра\n/day N — день N\n/plan — все дни\n'
            '/flights — билеты\n/visa — виза\n/car — транспорт и аренда\n'
            '/hotels — отели\n/food — рестораны\n/birthday — день рождения\n'
            '/budget — бюджет\n/checklist — чек-лист\n/info — полезное\n'
            '/remind on|off — утренние напоминания')
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

@guarded
async def app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wb = _webapp_button()
    if not wb:
        await update.effective_message.reply_text('WEBAPP_URL не задан — Mini App пока недоступен.')
        return
    await update.effective_message.reply_text('Открыть план поездки:',
                                              reply_markup=InlineKeyboardMarkup([[wb]]))

async def _send_day_for(update: Update, d: date, label: str):
    day = day_by_date(d)
    if not day:
        await update.effective_message.reply_text(
            f'{label} ({ru_date(d.isoformat())}) — не день поездки. {countdown_text()}',
            reply_markup=days_kb())
        return
    await send_long(update.effective_message, fmt_day(day), day_kb(day['n']))

@guarded
async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_day_for(update, now_local().date(), 'Сегодня')

@guarded
async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_day_for(update, now_local().date() + timedelta(days=1), 'Завтра')

@guarded
async def day_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args or [''])[0]
    if not arg.isdigit() or not day_by_n(int(arg)):
        await update.effective_message.reply_text('Укажите номер дня: /day 5',
                                                  reply_markup=days_kb())
        return
    day = day_by_n(int(arg))
    await send_long(update.effective_message, fmt_day(day), day_kb(day['n']))

@guarded
async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_long(update.effective_message, fmt_days_list(), days_kb())

def _simple_cmd(fmt_func):
    @guarded
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send_long(update.effective_message, fmt_func(), back_kb())
    return handler

@guarded
async def food_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('🇪🇬 Египет', callback_data='food:egypt'),
                                InlineKeyboardButton('🇷🇺 Петербург', callback_data='food:spb')]])
    await update.effective_message.reply_text('Где ищем, где поесть?', reply_markup=kb)

@guarded
async def checklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text('<b>✅ Чек-лист</b>\nВыберите раздел:',
                                              parse_mode=ParseMode.HTML,
                                              reply_markup=checklist_sections_kb())

@guarded
async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(fmt_practical(), parse_mode=ParseMode.HTML,
                                              reply_markup=practical_kb())

@guarded
async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args or ['on'])[0].lower()
    on = arg not in ('off', 'выкл', '0')
    set_remind(update.effective_chat.id, on)
    await update.effective_message.reply_text(
        'Напоминания включены: план дня в 08:00 и превью завтрашнего дня в 21:00.' if on
        else 'Напоминания выключены.')

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USERS and update.effective_user.id not in ADMIN_USERS:
        return
    try:
        load_trip()
        await update.effective_message.reply_text(f"Данные перечитаны: {len(TRIP['days'])} дней.")
    except Exception as e:
        await update.effective_message.reply_text(f'Ошибка загрузки: {e}')

@guarded
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ''
    chat_id = update.effective_chat.id
    await q.answer()

    if data == 'menu':
        await edit_or_send(q, welcome_text(), main_kb())
    elif data == 'today':
        day = day_by_date(now_local().date())
        if day:
            await edit_or_send(q, fmt_day(day), day_kb(day['n']))
        else:
            await edit_or_send(q, f'Сегодня не день поездки. {countdown_text()}\nВыберите день:', days_kb())
    elif data == 'days':
        await edit_or_send(q, fmt_days_list(), days_kb())
    elif data.startswith('day:'):
        day = day_by_n(int(data[4:]))
        if day:
            await edit_or_send(q, fmt_day(day), day_kb(day['n']))
    elif data == 'sec:flights':
        await edit_or_send(q, fmt_flights(), back_kb())
    elif data == 'sec:visa':
        await edit_or_send(q, fmt_visa(), back_kb())
    elif data == 'sec:transport':
        await edit_or_send(q, fmt_transport(), back_kb())
    elif data == 'sec:hotels':
        await edit_or_send(q, fmt_hotels(), back_kb())
    elif data == 'sec:birthday':
        await edit_or_send(q, fmt_birthday(), back_kb())
    elif data == 'sec:budget':
        await edit_or_send(q, fmt_budget(), back_kb())
    elif data.startswith('food:'):
        await edit_or_send(q, fmt_food(data[5:]), back_kb())
    elif data == 'chk':
        await edit_or_send(q, '<b>✅ Чек-лист</b>\nВыберите раздел:', checklist_sections_kb())
    elif data.startswith('chk:sec:'):
        idx = int(data.split(':')[2])
        sec = TRIP['checklist']['sections'][idx]
        await edit_or_send(q, f"<b>✅ {esc(sec['title'])}</b>\nНажмите на пункт, чтобы отметить.",
                           checklist_kb(chat_id, idx))
    elif data.startswith('chk:t:'):
        _, _, idx, item_id = data.split(':', 3)
        idx = int(idx)
        toggle_done(chat_id, item_id)
        try:
            await q.edit_message_reply_markup(reply_markup=checklist_kb(chat_id, idx))
        except Exception as e:
            logger.debug('edit markup failed: %s', e)
    elif data == 'info':
        await edit_or_send(q, fmt_practical(), practical_kb())
    elif data.startswith('info:'):
        idx = int(data[5:])
        await edit_or_send(q, fmt_practical(idx),
                           back_kb([[InlineKeyboardButton('◀️ Разделы', callback_data='info')]]))

# ── Напоминания ───────────────────────────────────────────────────────────────

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    today = now_local().date()
    day = day_by_date(today)
    if not day:
        return
    text = '☀️ <b>Доброе утро! План на сегодня</b>\n\n' + fmt_day(day)
    for chat_id in remind_chats():
        try:
            for ch in split_message(text):
                await context.bot.send_message(chat_id, ch, parse_mode=ParseMode.HTML,
                                               disable_web_page_preview=True)
        except Exception as e:
            logger.warning('morning_job %s: %s', chat_id, e)

async def evening_job(context: ContextTypes.DEFAULT_TYPE):
    tomorrow = now_local().date() + timedelta(days=1)
    day = day_by_date(tomorrow)
    if not day:
        return
    head = f"🌙 <b>Завтра — день {day['n']}: {esc(day['title'])}</b>"
    first = [b for b in day.get('blocks', []) if b.get('time')][:3]
    lines = [head, '']
    for b in first:
        lines.append(f"<b>{esc(b['time'])}</b> {esc(b['title'])}")
    if day.get('tips'):
        lines.append('')
        lines.append(f"💡 {esc(day['tips'][0])}")
    text = '\n'.join(lines)
    for chat_id in remind_chats():
        try:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                           reply_markup=InlineKeyboardMarkup(
                                               [[InlineKeyboardButton('Полный план дня', callback_data=f"day:{day['n']}")]]))
        except Exception as e:
            logger.warning('evening_job %s: %s', chat_id, e)

async def checklist_due_job(context: ContextTypes.DEFAULT_TYPE):
    """До поездки: напоминаем о пунктах чек-листа с дедлайном в ближайшие 3 дня."""
    today = datetime.now(TZ_SPB).date()
    soon = []
    for sec in TRIP['checklist']['sections']:
        for it in sec['items']:
            if it.get('due'):
                due = date.fromisoformat(it['due'])
                if 0 <= (due - today).days <= 3:
                    soon.append((due, it))
    if not soon:
        return
    for chat_id in remind_chats():
        done = done_set(chat_id)
        pending = [(d, it) for d, it in soon if it['id'] not in done]
        if not pending:
            continue
        lines = ['⏰ <b>Скоро дедлайны по чек-листу</b>', '']
        for d, it in sorted(pending, key=lambda x: x[0]):
            lines.append(f"• {esc(it['text'])} — до {ru_date(d.isoformat())}")
        try:
            await context.bot.send_message(chat_id, '\n'.join(lines), parse_mode=ParseMode.HTML,
                                           reply_markup=InlineKeyboardMarkup(
                                               [[InlineKeyboardButton('Открыть чек-лист', callback_data='chk')]]))
        except Exception as e:
            logger.warning('checklist_due_job %s: %s', chat_id, e)

# ── HTTP: Mini App + API ──────────────────────────────────────────────────────

_TEMPLATE_MARK = re.compile(r'/\*__TRIP_JSON__\*/.*?/\*__END__\*/', re.S)

def render_index() -> str:
    tpl = (WEBAPP_DIR / 'index.html').read_text(encoding='utf-8')
    payload = json.dumps(TRIP, ensure_ascii=False).replace('</', '<\\/')
    return _TEMPLATE_MARK.sub(lambda _m: payload, tpl, count=1)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEBAPP_DIR), **kwargs)

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        try:
            if path in ('/', '/index.html'):
                self._send(200, 'text/html; charset=utf-8', render_index().encode('utf-8'))
            elif path == '/api/trip':
                self._send(200, 'application/json; charset=utf-8',
                           json.dumps(TRIP, ensure_ascii=False).encode('utf-8'))
            elif path == '/health':
                self._send(200, 'text/plain', b'ok')
            else:
                super().do_GET()
        except Exception as e:
            logger.exception('http error')
            self._send(500, 'text/plain', str(e).encode())

    def log_message(self, fmt, *args):
        logger.debug('http ' + fmt, *args)

def start_http_server():
    try:
        srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    except OSError as e:
        logger.error('HTTP server not started on port %s: %s', PORT, e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True, name='http').start()
    logger.info('HTTP server on :%s (Mini App + /api/trip)', PORT)

# ── Запуск ────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand('start', 'Меню'), BotCommand('app', 'Открыть Mini App'),
        BotCommand('today', 'План на сегодня'), BotCommand('tomorrow', 'План на завтра'),
        BotCommand('plan', 'Все дни'), BotCommand('day', 'День N'),
        BotCommand('flights', 'Билеты'), BotCommand('visa', 'Виза'),
        BotCommand('car', 'Транспорт и аренда авто'), BotCommand('hotels', 'Отели'),
        BotCommand('food', 'Рестораны'), BotCommand('birthday', 'День рождения'),
        BotCommand('budget', 'Бюджет'), BotCommand('checklist', 'Чек-лист'),
        BotCommand('info', 'Полезное'), BotCommand('remind', 'Напоминания on/off'),
    ])
    if WEBAPP_URL.startswith('https://'):
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text='План', web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception as e:
            logger.warning('set_chat_menu_button: %s', e)

def main():
    load_trip()
    start_http_server()
    app = Application.builder().token(os.environ['TELEGRAM_BOT_TOKEN']).post_init(post_init).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('app', app_cmd))
    app.add_handler(CommandHandler('today', today_cmd))
    app.add_handler(CommandHandler('tomorrow', tomorrow_cmd))
    app.add_handler(CommandHandler('day', day_cmd))
    app.add_handler(CommandHandler('plan', plan_cmd))
    app.add_handler(CommandHandler('flights', _simple_cmd(fmt_flights)))
    app.add_handler(CommandHandler('visa', _simple_cmd(fmt_visa)))
    app.add_handler(CommandHandler('car', _simple_cmd(fmt_transport)))
    app.add_handler(CommandHandler('hotels', _simple_cmd(fmt_hotels)))
    app.add_handler(CommandHandler('food', food_cmd))
    app.add_handler(CommandHandler('birthday', _simple_cmd(fmt_birthday)))
    app.add_handler(CommandHandler('budget', _simple_cmd(fmt_budget)))
    app.add_handler(CommandHandler('checklist', checklist_cmd))
    app.add_handler(CommandHandler('info', info_cmd))
    app.add_handler(CommandHandler('remind', remind_cmd))
    app.add_handler(CommandHandler('reload', reload_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))

    jq = app.job_queue
    if jq:
        jq.run_daily(morning_job, time=dtime(8, 0, tzinfo=TZ_EGYPT), name='morning')
        jq.run_daily(evening_job, time=dtime(21, 0, tzinfo=TZ_EGYPT), name='evening')
        jq.run_daily(checklist_due_job, time=dtime(10, 0, tzinfo=TZ_SPB), name='checklist')
    else:
        logger.warning('JobQueue недоступен — установите python-telegram-bot[job-queue]')

    logger.info('Bot started')
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
