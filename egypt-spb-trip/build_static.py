"""
Собирает автономную версию Mini App: dist/index.html с встроенными данными.
Подходит для любого статического хостинга (GitHub Pages, Vercel, Netlify),
если не хочется раздавать Mini App с того же сервера, что и бот.

    python build_static.py            # -> dist/index.html
    python build_static.py out.html   # -> произвольный путь
"""
import json, re, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
MARK = re.compile(r'/\*__TRIP_JSON__\*/.*?/\*__END__\*/', re.S)

def build(out: Path):
    trip = json.loads((BASE / 'data' / 'trip.json').read_text(encoding='utf-8'))
    tpl = (BASE / 'webapp' / 'index.html').read_text(encoding='utf-8')
    payload = json.dumps(trip, ensure_ascii=False).replace('</', '<\\/')
    html = MARK.sub(lambda _m: payload, tpl, count=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding='utf-8')
    print(f'{out} — {len(html) // 1024} KB, {len(trip["days"])} дней')

if __name__ == '__main__':
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / 'dist' / 'index.html')
