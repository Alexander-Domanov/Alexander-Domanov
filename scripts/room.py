#!/usr/bin/env python3
"""Комната профиля: рисует анимированный SVG и подставляет его в README.

Зачем это здесь. README профиля раньше открывал четыре статичные картинки, а
значит на главной странице не было видно комнаты — только список. Этот скрипт
рисует один SVG: Мидзу, Моти, лампа, экран, гитара, барабаны. Внутри файла идёт
анимация (CSS и SMIL), поэтому картинка живёт в README без скриптов — GitHub
вставляет SVG именно как <img>.

Вид зависит от времени суток (Минск, UTC+3): утро, день, вечер, ночь.
Имя файла содержит версию — хеш содержимого, поэтому кэш картинок не держит
старую комнату: новое состояние — новое имя, и README получает новое имя.

Пульт. Состояние комнаты задаётся классами на корне <svg>:
m-lamp1, m-music1, m-drums1, m-guitar1, m-dog1, m-decor1, m-secret1.
Гость жмёт кнопку на странице комнаты — класс меняется, картинка меняется сразу.
Тот же набор классов уезжает в адрес страницы (?r=…), и тот же набор рисует
окно в README: состояние на странице и в профиле не расходятся, потому что
правила одни и те же — они лежат здесь, в <style> внутри SVG.

Запуск:
    python3 scripts/room.py                     # по часам, пишет assets/room и README
    python3 scripts/room.py --moment night      # конкретное состояние суток
    python3 scripts/room.py --state m-music1    # комната с включённой музыкой
    python3 scripts/room.py --out-dir /tmp/x --no-readme   # только файл
"""

import argparse
import datetime
import hashlib
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets", "room")
README = os.path.join(ROOT, "README.md")
STATE_FILE = os.path.join(ASSETS, "state.json")
LATEST = "latest.json"
BASE = os.environ.get("ROOM_BASE", "https://alexander-domanov.github.io/Alexander-Domanov").rstrip("/")
MINSK = datetime.timezone(datetime.timedelta(hours=3))

# Гость жмёт кнопку на странице комнаты — нажатие уходит сюда, а воркер читает
# отсюда и рисует окно в README. Тема ntfy.sh публичная: пишет кто угодно, читает
# воркер. Поэтому в состояние пускаем только наши классы и только 0/1 — чужой
# текст в комнату не попадёт физически, даже если его кто-то пришлёт.
REMOTE_TOPIC = "domanov-room-4f2c9ab71e05"
REMOTE_URL = f"https://ntfy.sh/{REMOTE_TOPIC}"
REMOTE_KEEP = 12 * 3600  # столько ntfy держит сообщение; старше — не состояние гостя
START = "<!-- room:start -->"
END = "<!-- room:end -->"

ALT = "the room of Mizu and Mochi: the view changes with the hour - morning, day, evening, night"

# Честная подпись: рядом с комнатой в README строка, что это живой код на странице,
# а не картинка, и ссылка на исходники. Иначе комната смахивает на статичную картинку.
# Живёт в самом генераторе, поэтому воркер перерисовывает README вместе с подписью —
# подпись не отстаёт от комнаты.
CAPTION = (
    "<sub>the room above is not a picture — it is a live page: "
    "press the switches and the same state rides into the URL. "
    "<a href=\"https://alexander-domanov.github.io/console/room.html\">step inside</a> · "
    "source <a href=\"https://github.com/Alexander-Domanov/console\">Alexander-Domanov/console</a></sub>"
)

STATES = ("morning", "day", "evening", "night")

# часы Минска, в которые начинается состояние
HOURS = ((5, "morning"), (11, "day"), (18, "evening"), (22, "night"))

# кнопки пульта: класс → что он включает
SWITCHES = ("lamp", "music", "drums", "guitar", "dog", "decor", "secret")

# что включено, если гость ничего не трогал
DEFAULT_STATE = {"lamp": 1, "music": 0, "drums": 0, "guitar": 0, "dog": 0, "decor": 0, "secret": 0}

PALETTE = {
    "morning": dict(room="#f2ece2", back="#e6dccd", window="#cfe3f0", sun="#f7d774",
                    lamp="#c9b48c", glow=0.18, screen="#dfe9d6", ink="#3a3733",
                    dust="#b9a98c", decor="#e3d7ef",
                    line="I opened the window. The kettle is on, sit down."),
    "day": dict(room="#eef1f5", back="#e0e6ee", window="#c9dced", sun="#fdf3c8",
                lamp="#b9bcc4", glow=0.10, screen="#d8e6ef", ink="#2f3540",
                dust="#9fb0c2", decor="#dfe7f2",
                line="Working. Ask me, I answer to the point."),
    "evening": dict(room="#3b3140", back="#332a38", window="#4a3b55", sun="#e9a86a",
                    lamp="#f0b96b", glow=0.85, screen="#c8a2d6", ink="#efe6ea",
                    dust="#c9a0b8", decor="#4a3550",
                    line="The lamp is warm. I am reading, but listening."),
    "night": dict(room="#1b1d24", back="#171920", window="#20232e", sun="#cfd6e6",
                  lamp="#e8c27a", glow=0.72, screen="#6f8fb5", ink="#cfd3dd",
                  dust="#8fa2c0", decor="#242033",
                  line="Quiet. The screen is glowing, I am dozing."),
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


# ---------------------------------------------------------------- пульт

def parse_state(text):
    """Разбирает состояние из строки вида `m-music1,m-dog1` или `music=1,dog=1`."""
    state = dict(DEFAULT_STATE)
    if not text:
        return state
    for chunk in re.split(r"[\s,;&]+", text.strip()):
        if not chunk:
            continue
        chunk = chunk[2:] if chunk.startswith("m-") else chunk
        if "=" in chunk:
            key, _, val = chunk.partition("=")
        else:
            key, val = chunk[:-1], chunk[-1:]
        key = key.strip().lower()
        if key in state:
            state[key] = 1 if str(val).strip() in ("1", "on", "true", "yes") else 0
    return state


def state_text(state):
    return " ".join(f"m-{k}{1 if state.get(k) else 0}" for k in SWITCHES)


def load_state():
    """Состояние, которое оставил последний, кто жал кнопки: файл рядом с комнатой."""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return parse_state(json.load(fh).get("state", ""))
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)


def remote_state(timeout=8):
    """Что нажал последний гость. Возвращает (состояние, кто) или (None, None).

    Читает тему ntfy.sh: страница комнаты пишет туда классы состояния. Проверяем
    две вещи, прежде чем верить: сообщение состоит только из наших m-классов и
    оно свежее. Иначе возвращаем None — комната нарисуется по умолчанию.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(REMOTE_URL + "/json?poll=1", timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception:
        return None, None
    for line in reversed(body.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = str(event.get("message", "")).strip()
        if not re.fullmatch(r"(m-[a-z]+[01][\s,]*)*", message) or "m-" not in message:
            continue  # чужой текст или мусор: не наше состояние, не берём
        when = event.get("time")
        if isinstance(when, int) and datetime.datetime.now(datetime.timezone.utc).timestamp() - when > REMOTE_KEEP:
            continue  # гость был больше двенадцати часов назад — окно этого не помнит
        return parse_state(message), "guest"
    return None, None


def line_for(moment, state):
    """Реплика Мидзу: короткая, до двенадцати слов, без восклицаний."""
    if state.get("secret"):
        return "Found it. Tea at four in the morning. Just between us."
    if state.get("drums"):
        return "Drums. Mochi will go sleep in the hallway now."
    if state.get("music"):
        return "Playing. Listen, if you are not in a hurry."
    if state.get("dog"):
        return "Mochi is awake. He can see you."
    if not state.get("lamp"):
        return "Lamp off. The screen is enough."
    if state.get("decor"):
        return "Different walls. I breathe easier like this."
    return PALETTE[moment]["line"]


# ---------------------------------------------------------------- рисование

def figure(p, x, y, awake):
    """Мидзу: нарисована линиями, лицо почти не меняется."""
    pose = "M0 0 v-6" if awake else "M0 0 v-3"
    return f'''
  <g id="mizu" transform="translate({x},{y})" stroke="{p['ink']}" stroke-width="2" fill="none" stroke-linecap="round">
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


def dog(p, x, y, awake, gid):
    """Моти: спит у тёплого места, на команду просыпается."""
    tilt = "" if awake else " rotate(-4)"
    ear = "M-6 -6 l-3 -6 l6 1 z" if awake else "M-6 -6 l-2 -4 l5 0 z"
    eye = "M24 -6 h1" if awake else "M23 -6 h2 M23 -6 v1"
    return f'''
  <g id="{gid}" transform="translate({x},{y}){tilt}" stroke="{p['ink']}" stroke-width="2" fill="none" stroke-linecap="round">
    <ellipse cx="0" cy="0" rx="20" ry="10" fill="{p['back']}"/>
    <circle cx="20" cy="-6" r="9" fill="{p['back']}"/>
    <path d="{ear}" fill="{p['ink']}"/>
    <path d="{eye}" stroke-width="2.6"/>
    <path d="M-14 8 q-6 4 -2 8" />
    <path d="M-2 -10 q8 -8 14 -2" opacity="0.5"/>
  </g>'''


def notes(p):
    """Ноты: видно только когда гость включил барабаны.

    Музыка и барабаны — два разных переключателя, и вид у них обязан быть
    разным: если оба рисовали одно и то же, нажатие на второй ничего не меняло
    бы на экране. Барабанам — ноты у установки, музыке — полоски на столе.
    """
    out = []
    for i, (x, y) in enumerate(((160, 196), (196, 176), (232, 156))):
        out.append(f'''
    <g transform="translate({x},{y})" opacity="0.9">
      <ellipse cx="0" cy="0" rx="5" ry="4" fill="{p['ink']}"/>
      <path d="M5 0 v-22" stroke="{p['ink']}" stroke-width="2"/>
      <animateTransform attributeName="transform" type="translate" additive="sum"
        values="0 0; 6 -14; 0 -28" dur="{3 + i}s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0.9;0.4;0" dur="{3 + i}s" repeatCount="indefinite"/>
    </g>''')
    return "".join(out)


def music(p):
    """Музыка: полоски-эквалайзер на столе, рядом с экраном."""
    out = []
    for i, (x, h) in enumerate(((600, 12), (610, 22), (620, 16), (630, 8))):
        y0 = 230 - h
        dur = 1.2 + i * 0.25
        out.append(f'''
    <rect x="{x}" y="{y0}" width="5" height="{h}" rx="2" fill="{p['ink']}" opacity="0.9">
      <animate attributeName="height" values="{h};{h + 7};{h}" dur="{dur:.2f}s" repeatCount="indefinite"/>
      <animate attributeName="y" values="{y0};{y0 - 7};{y0}" dur="{dur:.2f}s" repeatCount="indefinite"/>
    </rect>''')
    return "".join(out)


def guitar(p):
    """Гитара на стене: включённая подсвечивается."""
    return f'''
  <g id="guitar" stroke="{p['ink']}" stroke-width="2" fill="none" stroke-linecap="round">
    <ellipse cx="812" cy="168" rx="26" ry="32" fill="{p['back']}"/>
    <circle cx="812" cy="168" r="8"/>
    <path d="M812 136 v-58" stroke-width="6"/>
    <path d="M804 78 h16 v-8 h-16 z" fill="{p['ink']}"/>
    <path d="M806 150 h12 M806 176 h12" opacity="0.5"/>
  </g>'''


def drums(p):
    """Барабаны в углу: её способ позвать."""
    return f'''
  <g id="drums" stroke="{p['ink']}" stroke-width="2" fill="none" stroke-linecap="round">
    <g class="drum" opacity="0.6">
      <ellipse cx="140" cy="252" rx="34" ry="12" fill="{p['back']}"/>
      <path d="M106 252 v30 M174 252 v30 M106 282 q34 12 68 0"/>
      <path d="M118 240 l-14 -26 M162 240 l14 -26"/>
    </g>
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


def room(moment, state=None):
    p = PALETTE[moment]
    state = state or dict(DEFAULT_STATE)
    standing = STANDING[moment]
    x_miz = 300 if standing else 520
    y_miz = 150 if standing else 176
    warm = WARM[moment]
    glow = p["glow"] if state.get("lamp") else 0.02
    motes = "".join(dust(p, i, x, y) for i, x, y in DUST)
    line = line_for(moment, state)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 360" width="900" height="360" class="moment-{moment} {state_text(state)}" role="img" aria-label="Комната Мидзу и Моти, {moment}">
  <style>
    @keyframes flicker {{ 0%,100% {{ opacity:{glow:.2f} }} 43% {{ opacity:{min(1.0, glow + 0.14):.2f} }} 58% {{ opacity:{max(0.05, glow - 0.05):.2f} }} }}
    #lamp {{ animation: flicker 4.5s ease-in-out infinite; }}
    @keyframes breathe {{ 0%,100% {{ transform: translateY(0px) }} 50% {{ transform: translateY(-2.5px) }} }}
    #roommove {{ animation: breathe 11s ease-in-out infinite; }}
    @keyframes tip {{ 0%,60% {{ transform: translateX(0px) }} 75% {{ transform: translateX(-4px) }} 100% {{ transform: translateX(0px) }} }}
    #drums .drum {{ animation: tip 6s ease-in-out infinite; }}
    /* ---- пульт: состояние комнаты ---- */
    #dark {{ display:none }}
    #notes {{ display:none }}
    #music {{ display:none }}
    #mochi-sleep {{ display:none }}
    #bubble {{ display:none }}
    #secret {{ display:none }}
    .moment-night #mochi-awake {{ display:none }}
    .moment-night #mochi-sleep {{ display:inline }}
    .m-lamp0 #lamp {{ animation:none; opacity:0.04 }}
    .m-lamp0 #dark {{ display:inline; opacity:0.34 }}
    .m-lamp0 #screen {{ opacity:0.95 }}
    .m-drums1 #notes {{ display:inline }}
    .m-music1 #music {{ display:inline }}
    .m-drums1 #drums .drum {{ opacity:1; stroke-width:3 }}
    .m-guitar1 #guitar {{ stroke-width:3; opacity:1 }}
    #guitar {{ opacity:0.55 }}
    .m-dog1 #mochi-awake {{ display:inline }}
    .m-dog1 #mochi-sleep {{ display:none }}
    .m-dog1 #bubble {{ display:inline }}
    /* Моти: m-dog0 — спит, m-dog1 — не спит. Правило нужно на все виды суток:
       без него вне ночи оба состояния выглядят одинаково (разнится только
       пузырь), и кнопка «Моти» у гостя ничего не меняет. */
    .m-dog0 #mochi-awake {{ display:none }}
    .m-dog0 #mochi-sleep {{ display:inline }}
    .m-decor1 #wall {{ fill:{p['decor']} }}
    .m-decor1 #back {{ fill:{p['decor']} }}
    .m-secret1 #secret {{ display:inline }}
  </style>
  <rect id="wall" width="900" height="360" fill="{p['room']}"/>
  <rect id="back" y="280" width="900" height="80" fill="{p['back']}"/>
  <g id="roommove">
    <!-- свет из окна лежит на полу и медленно едет по комнате -->
    <rect x="80" y="284" width="230" height="66" fill="{p['window']}" opacity="{warm}">
      <animateTransform attributeName="transform" type="translate"
        values="0 0; 26 0; 0 0" dur="14s" repeatCount="indefinite"/>
    </rect>
    <!-- окно и свет за ним -->
    <rect x="70" y="60" width="220" height="150" rx="6" fill="{p['window']}"/>
    <circle id="sun" cx="215" cy="105" r="20" fill="{p['sun']}" opacity="0.85">
      <animate attributeName="opacity" values="0.7;1;0.7" dur="7s" repeatCount="indefinite"/>
    </circle>
    <path d="M70 60 h220 M70 135 h220 M180 60 v150" stroke="{p['ink']}" stroke-width="2" opacity="0.35"/>
    {guitar(p)}
    {drums(p)}
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
    {figure(p, x_miz, y_miz, AWAKE[moment])}
    {dog(p, 700, 268, True, 'mochi-awake')}
    {dog(p, 700, 268, False, 'mochi-sleep')}
    <g id="notes">{notes(p)}</g>
    <g id="music">{music(p)}</g>
    <g id="bubble">
      <rect x="716" y="212" width="62" height="26" rx="13" fill="{p['back']}" opacity="0.95"/>
      <text x="747" y="231" font-family="Georgia, serif" font-size="15" fill="{p['ink']}" text-anchor="middle">woof</text>
    </g>
    <rect id="dark" x="0" y="0" width="900" height="360" fill="#0a0c14"/>
    <g id="secret">
      <text x="40" y="344" font-family="Georgia, serif" font-size="14" fill="{p['ink']}" opacity="0.8">★ tea at four in the morning</text>
    </g>
  </g>
  {motes}
  <!-- реплика -->
  <g>
    <rect x="40" y="16" width="620" height="34" rx="17" fill="{p['back']}" opacity="0.95"/>
    <text id="line" x="62" y="39" font-family="Georgia, serif" font-size="17" fill="{p['ink']}">{line}</text>
  </g>
  <text x="862" y="348" font-family="Georgia, serif" font-size="13" fill="{p['ink']}" opacity="0.6" text-anchor="end">{moment}</text>
</svg>
'''


def tag_for(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]


def file_name(moment, body):
    return f"room-{moment}-{tag_for(body)}.svg"


def render(moment, state=None):
    body = room(moment, state)
    return file_name(moment, body), body


def write_assets(moment, out_dir, state=None, origin="default", all_moments=False):
    """Кладёт свежую комнату, убирает старые версии, пишет указатель latest.json.

    Указатель нужен странице комнаты: имя файла меняется каждый час, а адрес у
    страницы постоянный, поэтому имя она берёт отсюда.

    Видов четыре, и гостю нужен свой: у него вечер — значит вечер, а не время
    сервера. Поэтому вместе с текущим видом кладём в указатель карту «время
    суток → имя файла» (all_moments). Без неё странице нечего показать гостю с
    другим часом.
    """
    wanted = STATES if all_moments else (moment,)
    made = {}
    for name_of_moment in wanted:
        made[name_of_moment] = render(name_of_moment, state)
    os.makedirs(out_dir, exist_ok=True)
    keep = {built[0] for built in made.values()}
    for old in os.listdir(out_dir):
        if old.startswith("room-") and old.endswith(".svg") and old not in keep:
            os.remove(os.path.join(out_dir, old))
    for built_name, body in made.values():
        with open(os.path.join(out_dir, built_name), "w", encoding="utf-8") as fh:
            fh.write(body)
    name, body = made[moment]
    latest = {"name": name, "moment": moment, "state": state_text(state or DEFAULT_STATE),
              "stateFrom": origin, "url": f"{BASE}/assets/room/{name}",
              "moments": {m: made[m][0] for m in made}}
    with open(os.path.join(out_dir, LATEST), "w", encoding="utf-8") as fh:
        json.dump(latest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return name, body, os.path.join(out_dir, name)


def update_readme(name):
    with open(README, encoding="utf-8") as fh:
        text = fh.read()
    url = f"{BASE}/assets/room/{name}"
    img = f'<img src="{url}" width="880" alt="{ALT}">'
    block = f"{START}\n{img}\n{CAPTION}\n{END}"
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
    parser.add_argument("--state", default=None,
                        help="состояние пульта: m-music1,m-dog1 (по умолчанию — из state.json)")
    parser.add_argument("--remote", action="store_true",
                        help="взять состояние у последнего гостя (ntfy), иначе — из state.json")
    parser.add_argument("--out-dir", default=ASSETS)
    parser.add_argument("--all-moments", action="store_true",
                        help="нарисовать все четыре вида суток, а не только текущий: "
                             "страница комнаты берёт вид по часам гостя")
    parser.add_argument("--no-readme", action="store_true")
    parser.add_argument("--print-name", action="store_true")
    args = parser.parse_args()

    moment = args.moment or moment_for()
    origin = "owner"
    if args.state:
        state = parse_state(args.state)
    elif args.remote:
        guest, who = remote_state()
        if guest is None:
            state, origin = load_state(), "owner"
        else:
            state, origin = guest, who or "guest"
    else:
        state = load_state()
    name, _body, path = write_assets(moment, args.out_dir, state, origin,
                                     all_moments=args.all_moments)
    if args.print_name:
        print(name)
        return 0
    print(f"{moment} {state_text(state)} ({origin}): {path}")
    if not args.no_readme:
        print(f"README → {update_readme(name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
