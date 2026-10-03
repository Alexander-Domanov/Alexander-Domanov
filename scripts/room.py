#!/usr/bin/env python3
"""Комната профиля: рисует анимированный SVG и подставляет его в README.

Зачем это здесь. README профиля раньше открывал четыре статичные картинки, а
значит на главной странице не было видно комнаты — только список. Этот скрипт
рисует один SVG: Мидзу, Моти, лампа, экран. Внутри файла идёт анимация (CSS и
SMIL), поэтому картинка живёт в README без скриптов — GitHub вставляет SVG
именно как <img>.

Вид зависит от времени суток (Минск, UTC+3): утро, день, вечер, ночь.
Имя файла содержит версию — хеш содержимого, поэтому кэш картинок не держит
старую комнату: новое состояние — новое имя, и README получает новое имя.

Запуск:
    python3 scripts/room.py                 # по часам, пишет assets/room и README
    python3 scripts/room.py --moment night  # конкретное состояние
    python3 scripts/room.py --out-dir /tmp/x --no-readme   # только файл
"""

import argparse
import datetime
import hashlib
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets", "room")
README = os.path.join(ROOT, "README.md")
BASE = os.environ.get("ROOM_BASE", "https://alexander-domanov.github.io/Alexander-Domanov").rstrip("/")
MINSK = datetime.timezone(datetime.timedelta(hours=3))
START = "<!-- room:start -->"
END = "<!-- room:end -->"

ALT = "комната Мидзу и Моти: вид меняется по часам Минска — утро, день, вечер, ночь"

STATES = ("morning", "day", "evening", "night")

# часы Минска, в которые начинается состояние
HOURS = ((5, "morning"), (11, "day"), (18, "evening"), (22, "night"))

PALETTE = {
    "morning": dict(room="#f2ece2", back="#e6dccd", window="#cfe3f0", sun="#f7d774",
                    lamp="#c9b48c", glow=0.18, screen="#dfe9d6", ink="#3a3733",
                    dust="#b9a98c",
                    line="Окно открыла. Чайник греется, садись."),
    "day": dict(room="#eef1f5", back="#e0e6ee", window="#c9dced", sun="#fdf3c8",
                lamp="#b9bcc4", glow=0.10, screen="#d8e6ef", ink="#2f3540",
                dust="#9fb0c2",
                line="Работаю. Спрашивай, отвечу по делу."),
    "evening": dict(room="#3b3140", back="#332a38", window="#4a3b55", sun="#e9a86a",
                    lamp="#f0b96b", glow=0.85, screen="#c8a2d6", ink="#efe6ea",
                    dust="#c9a0b8",
                    line="Лампа тёплая. Читаю, но слушаю."),
    "night": dict(room="#1b1d24", back="#171920", window="#20232e", sun="#cfd6e6",
                  lamp="#e8c27a", glow=0.72, screen="#6f8fb5", ink="#cfd3dd",
                  dust="#8fa2c0",
                  line="Тихо. Экран светит, я дремлю."),
}

AWAKE = {"morning": 1, "day": 1, "evening": 1, "night": 0}      # Моти спит ночью
STANDING = {"morning": 1, "day": 0, "evening": 0, "night": 0}   # утром Мидзу у окна
WARM = {"morning": 0.15, "day": 0.08, "evening": 0.5, "night": 0.35}


def moment_for(now=None):
    """Состояние комнаты по часам Минска."""
    now = now or datetime.datetime.now(MINSK)
    hour = now.astimezone(MINSK).hour
    name = "night"
    for start, value in HOURS:
        if hour >= start:
            name = value
    return name


def figure(p, x, y, awake):
    """Мидзу: нарисована линиями, лицо почти не меняется."""
    pose = "M0 0 v-6" if awake else "M0 0 v-3"
    return f'''
  <g transform="translate({x},{y})" stroke="{p['ink']}" stroke-width="2" fill="none" stroke-linecap="round">
    <path d="M-16 34 h32" stroke-width="3"/>
    <path d="M-9 34 {pose} l0 -18" />
    <path d="M9 34 {pose} l0 -18" />
    <path d="M-11 -8 q0 -14 11 -14 q11 0 11 14 z" fill="{p['ink']}" opacity="0.9" stroke="none"/>
    <circle cx="0" cy="2" r="9" fill="{p['room']}"/>
    <path d="M-9 -3 q9 -7 18 0" />
    <path d="M-4 3 h1 M3 3 h1" stroke-width="2.4"/>
    <path d="M-6 16 l6 5 l6 -5" />
    <path d="M-12 16 l12 5 l12 -5" />
  </g>'''


def dog(p, x, y, awake):
    tilt = "" if awake else " rotate(-4)"
    ear = "M-6 -6 l-3 -6 l6 1 z" if awake else "M-6 -6 l-2 -4 l5 0 z"
    return f'''
  <g transform="translate({x},{y}){tilt}" stroke="{p['ink']}" stroke-width="2" fill="none" stroke-linecap="round">
    <ellipse cx="0" cy="0" rx="20" ry="10" fill="{p['back']}"/>
    <circle cx="20" cy="-6" r="9" fill="{p['back']}"/>
    <path d="{ear}" fill="{p['ink']}"/>
    <path d="M24 -6 h1" stroke-width="2.6"/>
    <path d="M-14 8 q-6 4 -2 8" />
    <path d="M-2 -10 q8 -8 14 -2" opacity="0.5"/>
  </g>'''


def dust(p, i, x, y):
    """Пылинка в луче света: медленно плывёт — комната дышит."""
    dur = 9 + (i % 4) * 2
    dy = 10 + (i % 3) * 5
    dx = 16 + (i % 5) * 6
    return f'''
  <circle cx="{x}" cy="{y}" r="{1 + (i % 3) * 0.6}" fill="{p['dust']}" opacity="{0.25 + (i % 4) * 0.12}">
    <animateTransform attributeName="transform" type="translate"
      values="0 0; {dx} -{dy}; 0 0" dur="{dur}s" repeatCount="indefinite"/>
    <animate attributeName="opacity" values="{0.15 + (i % 3) * 0.1};{0.5};{0.2}" dur="{5 + i}s" repeatCount="indefinite"/>
  </circle>'''


DUST = ((1, 150, 90), (2, 200, 130), (3, 260, 200), (4, 495, 120), (5, 540, 160),
        (6, 620, 150), (7, 275, 250), (8, 655, 105))


def room(moment):
    p = PALETTE[moment]
    awake = AWAKE[moment]
    standing = STANDING[moment]
    x_miz = 300 if standing else 520
    y_miz = 150 if standing else 176
    warm = WARM[moment]
    glow = p["glow"]
    motes = "".join(dust(p, i, x, y) for i, x, y in DUST)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 360" width="900" height="360" role="img" aria-label="Комната Мидзу и Моти, {moment}">
  <style>
    @keyframes flicker {{ 0%,100% {{ opacity:{glow:.2f} }} 43% {{ opacity:{min(1.0, glow + 0.14):.2f} }} 58% {{ opacity:{max(0.05, glow - 0.05):.2f} }} }}
    #lamp {{ animation: flicker 4.5s ease-in-out infinite; }}
    @keyframes breathe {{ 0%,100% {{ transform: translateY(0px) }} 50% {{ transform: translateY(-2.5px) }} }}
    #roommove {{ animation: breathe 11s ease-in-out infinite; }}
  </style>
  <rect width="900" height="360" fill="{p['room']}"/>
  <rect y="280" width="900" height="80" fill="{p['back']}"/>
  <g id="roommove">
    <!-- свет из окна лежит на полу и медленно едет по комнате -->
    <rect x="80" y="284" width="230" height="66" fill="{p['window']}" opacity="{warm}">
      <animateTransform attributeName="transform" type="translate"
        values="0 0; 26 0; 0 0" dur="14s" repeatCount="indefinite"/>
    </rect>
    <!-- окно и свет за ним -->
    <rect x="70" y="60" width="220" height="150" rx="6" fill="{p['window']}"/>
    <circle cx="215" cy="105" r="20" fill="{p['sun']}" opacity="0.85">
      <animate attributeName="opacity" values="0.7;1;0.7" dur="7s" repeatCount="indefinite"/>
    </circle>
    <path d="M70 60 h220 M70 135 h220 M180 60 v150" stroke="{p['ink']}" stroke-width="2" opacity="0.35"/>
    <!-- настольная лампа -->
    <g stroke="{p['ink']}" stroke-width="2" fill="none">
      <path d="M600 280 v-70 h40 l-20 -26"/>
      <path d="M640 184 l-46 14" />
    </g>
    <circle id="lamp" cx="612" cy="200" r="54" fill="{p['lamp']}" opacity="{glow}"/>
    <!-- стол и экран -->
    <rect x="380" y="232" width="300" height="10" rx="3" fill="{p['ink']}" opacity="0.55"/>
    <path d="M400 280 v-38 M660 280 v-38" stroke="{p['ink']}" stroke-width="3" opacity="0.35"/>
    <rect x="430" y="196" width="150" height="36" rx="4" fill="{p['screen']}" opacity="0.9">
      <animate attributeName="opacity" values="0.9;0.72;0.92" dur="2.6s" repeatCount="indefinite"/>
    </rect>
    <!-- курсор на экране: мигает -->
    <rect x="588" y="200" width="3" height="14" fill="{p['ink']}">
      <animate attributeName="opacity" values="1;0;1" dur="1.1s" repeatCount="indefinite"/>
    </rect>
    {figure(p, x_miz, y_miz, awake)}
    {dog(p, 700, 268, awake)}
  </g>
  {motes}
  <!-- реплика -->
  <g>
    <rect x="40" y="16" width="560" height="34" rx="17" fill="{p['back']}" opacity="0.95"/>
    <text x="62" y="39" font-family="Georgia, serif" font-size="17" fill="{p['ink']}">{p['line']}</text>
  </g>
  <text x="862" y="348" font-family="Georgia, serif" font-size="13" fill="{p['ink']}" opacity="0.6" text-anchor="end">{moment}</text>
</svg>
'''


def tag_for(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]


def file_name(moment, body):
    return f"room-{moment}-{tag_for(body)}.svg"


def render(moment):
    body = room(moment)
    return file_name(moment, body), body


def write_assets(moment, out_dir):
    """Кладёт свежую комнату, убирает старые версии."""
    name, body = render(moment)
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.startswith("room-") and old.endswith(".svg") and old != name:
            os.remove(os.path.join(out_dir, old))
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return name, body, path


def update_readme(name):
    with open(README, encoding="utf-8") as fh:
        text = fh.read()
    url = f"{BASE}/assets/room/{name}"
    img = f'<img src="{url}" width="880" alt="{ALT}">'
    block = f"{START}\n{img}\n{END}"
    if START in text and END in text:
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), block, text, flags=re.S)
    else:
        text = f"<p align=\"center\">\n{block}\n</p>\n\n" + text
    with open(README, "w", encoding="utf-8") as fh:
        fh.write(text)
    return url


def main():
    parser = argparse.ArgumentParser(description="Комната профиля")
    parser.add_argument("--moment", choices=STATES, default=None)
    parser.add_argument("--out-dir", default=ASSETS)
    parser.add_argument("--no-readme", action="store_true")
    parser.add_argument("--print-name", action="store_true")
    args = parser.parse_args()

    moment = args.moment or moment_for()
    name, _body, path = write_assets(moment, args.out_dir)
    if args.print_name:
        print(name)
        return 0
    print(f"{moment}: {path}")
    if not args.no_readme:
        print(f"README → {update_readme(name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
