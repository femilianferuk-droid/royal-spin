# -*- coding: utf-8 -*-
"""
Golden Casino — однофайловый Flask-проект.
Игры с наградой: Слоты, Кости, Монетка, Блэкджек.
Игры без награды: Футбол (мяч бьётся ОТ ворот), Сапёр (выбор поля), Крестики-нолики (бот играет по очереди).
"""
import os
import random
from flask import Flask, render_template_string, request, session, redirect, url_for, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "golden-casino-secret-key")

# ---------------- Стили ----------------
BASE_STYLE = """
<style>
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;font-family:'Segoe UI',Roboto,sans-serif;background:#0b0b14;color:#fff;min-height:100vh;padding-bottom:90px}
  .top-bar{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;background:#0b0b14}
  .top-bar h1{margin:0;font-size:22px;font-weight:700}
  .icon-btn{background:none;border:0;color:#fff;font-size:22px;cursor:pointer;padding:6px}
  .balance-row{display:flex;align-items:center;justify-content:space-between;padding:10px 18px}
  .balance-row .lbl{color:#e8b95a;font-weight:700;font-size:18px}
  .balance-row .lbl::before{content:"♠ ";color:#e8b95a}
  .balance-pill{background:#1a1a26;border:1px solid #2a2a3a;border-radius:20px;padding:6px 14px;color:#e8b95a;font-weight:700}
  .welcome-card{margin:14px 18px;padding:22px;border-radius:18px;background:linear-gradient(135deg,#1c1230 0%,#2a0f3a 60%,#3a0e2a 100%);position:relative;overflow:hidden;min-height:160px}
  .welcome-card h3{margin:0 0 4px;font-size:14px;color:#cdb88a;font-weight:500}
  .welcome-card h2{margin:0 0 14px;font-size:24px;color:#fff}
  .welcome-card .coin-big{font-size:46px;font-weight:800;color:#e8b95a;line-height:1}
  .welcome-card .coin-big::before{content:"⦿ ";color:#e8b95a;font-size:32px;vertical-align:middle;margin-right:4px}
  .welcome-card .coin-sub{font-size:12px;color:#9b8a78;margin-top:4px}
  .welcome-card .slot-illus{position:absolute;right:14px;top:50%;transform:translateY(-50%);font-size:80px;opacity:.85}
  .section-title{padding:18px 18px 8px;font-size:15px;color:#9b9bb0;font-weight:600;letter-spacing:.5px}
  .games-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:0 18px}
  .game-card{position:relative;background:linear-gradient(160deg,#16161f 0%,#1f1f2c 100%);border:1px solid #262635;border-radius:16px;padding:18px 14px 14px;cursor:pointer;text-decoration:none;color:inherit;display:block;transition:transform .15s,border-color .15s}
  .game-card:hover{transform:translateY(-2px);border-color:#3a3a55}
  .game-card .badge{position:absolute;top:10px;right:10px;background:#241a0e;color:#e8b95a;border:1px solid #4a3a1a;border-radius:14px;padding:3px 9px;font-size:11px;font-weight:700}
  .game-card .badge.no-reward{background:#1a1a26;color:#8a8aa0;border-color:#2a2a3a}
  .game-card .icon{font-size:42px;line-height:1;margin-bottom:10px}
  .game-card h4{margin:0 0 4px;font-size:17px;color:#fff;font-weight:700}
  .game-card p{margin:0;font-size:12px;color:#8a8aa0}
  .game-card.football .icon{color:#ff7a59}
  .game-card.mines .icon{color:#d33}
  .game-card.ttt .icon{color:#7a9cff}
  .bottom-nav{position:fixed;left:0;right:0;bottom:0;background:#0b0b14;border-top:1px solid #1a1a26;display:flex;justify-content:space-around;padding:10px 6px 14px;z-index:10}
  .nav-item{display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11px;color:#8a8aa0;text-decoration:none;flex:1}
  .nav-item.active{color:#e8b95a}
  .nav-item .ic{font-size:22px}
  .container{padding:14px 18px;max-width:680px;margin:0 auto}
  .panel{background:#16161f;border:1px solid #262635;border-radius:16px;padding:18px;margin-bottom:14px}
  .panel h2{margin:0 0 12px;font-size:20px;color:#e8b95a}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .row.mb{margin-bottom:12px}
  .btn{background:linear-gradient(180deg,#e8b95a 0%,#b8893a 100%);color:#1a1206;border:0;border-radius:12px;padding:12px 18px;font-weight:800;cursor:pointer;font-size:15px;transition:transform .1s}
  .btn:hover{transform:translateY(-1px)}
  .btn.ghost{background:#1a1a26;color:#fff;border:1px solid #2a2a3a}
  .btn.danger{background:linear-gradient(180deg,#e85a5a 0%,#a03030 100%);color:#fff}
  input[type=number],input[type=text],select{background:#0e0e18;border:1px solid #2a2a3a;color:#fff;border-radius:10px;padding:10px 12px;font-size:15px;width:100%;outline:none}
  input:focus,select:focus{border-color:#e8b95a}
  .reels{display:flex;gap:10px;justify-content:center;margin:14px 0}
  .reel{width:70px;height:70px;background:#0e0e18;border:2px solid #e8b95a;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:42px}
  .dice{display:flex;gap:10px;justify-content:center;margin:14px 0}
  .die{width:78px;height:78px;background:#0e0e18;border:2px solid #e8b95a;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:46px;color:#e8b95a}
  .coin{font-size:120px;text-align:center;margin:14px 0;line-height:1}
  .msg{margin:10px 0;padding:10px 14px;border-radius:10px;font-weight:600;text-align:center}
  .msg.win{background:rgba(232,185,90,.15);color:#e8b95a;border:1px solid #4a3a1a}
  .msg.lose{background:rgba(232,90,90,.12);color:#ff8a8a;border:1px solid #4a1a1a}
  .msg.info{background:rgba(122,156,255,.1);color:#9ab0ff;border:1px solid #1a2a4a}
  .balance-strip{display:flex;align-items:center;justify-content:space-between;background:#16161f;border:1px solid #262635;border-radius:12px;padding:10px 14px;margin-bottom:14px}
  .balance-strip .b{color:#e8b95a;font-weight:800;font-size:18px}
  .balance-strip .b::before{content:"⦿ ";margin-right:4px}
  .back{display:inline-flex;align-items:center;gap:6px;color:#8a8aa0;text-decoration:none;font-size:14px;margin-bottom:8px}
  .field-mines{display:grid;gap:6px;margin:14px 0}
  .cell{aspect-ratio:1;background:#1a1a26;border:1px solid #2a2a3a;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:20px;cursor:pointer;user-select:none;transition:background .1s}
  .cell:hover{background:#22222f}
  .cell.open.safe{background:#1a3a1a;border-color:#2a5a2a}
  .cell.open.mine{background:#3a1a1a;border-color:#5a2a2a}
  .cell.flag::before{content:"🚩"}
  .ttt-board{display:grid;grid-template-columns:repeat(3,90px);gap:8px;justify-content:center;margin:14px auto}
  .ttt-cell{width:90px;height:90px;background:#16161f;border:2px solid #2a2a3a;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:46px;font-weight:800;cursor:pointer;color:#fff}
  .ttt-cell.x{color:#7a9cff}
  .ttt-cell.o{color:#ff7a59}
  .ttt-cell.win{background:#1a3a1a;border-color:#2a5a2a}
  .pitch{position:relative;height:300px;background:linear-gradient(180deg,#0e3018 0%,#0a4a20 100%);border-radius:14px;border:2px solid #fff;margin:14px 0;overflow:hidden}
  .pitch .goal{position:absolute;left:50%;transform:translateX(-50%);width:120px;height:30px;border:3px solid #fff;border-top:0;bottom:0;border-radius:0 0 12px 12px;background:rgba(255,255,255,.05)}
  .pitch .goal::before{content:"GOAL";position:absolute;left:50%;top:-22px;transform:translateX(-50%);color:#fff;font-size:12px;letter-spacing:2px}
  .ball{position:absolute;width:34px;height:34px;background:radial-gradient(circle at 30% 30%,#fff,#aaa);border-radius:50%;box-shadow:0 2px 6px rgba(0,0,0,.4);transition:all .6s cubic-bezier(.4,0,.2,1);display:flex;align-items:center;justify-content:center;color:#222;font-size:18px;font-weight:800}
  .kick-btn{position:absolute;left:50%;transform:translateX(-50%);bottom:8px}
  .info-line{color:#8a8aa0;font-size:13px;margin-top:6px}
  .stat{display:inline-block;background:#1a1a26;border:1px solid #2a2a3a;border-radius:10px;padding:6px 10px;margin-right:8px;color:#cdb88a;font-size:13px}
  .stat b{color:#e8b95a}
  .results-box{background:#0e0e18;border:1px dashed #2a2a3a;border-radius:10px;padding:10px;margin-top:10px;color:#cdb88a;font-size:13px;white-space:pre-wrap}
  .small{font-size:12px;color:#8a8aa0}
  .switches{display:flex;gap:6px;flex-wrap:wrap}
  .switch{background:#1a1a26;border:1px solid #2a2a3a;border-radius:10px;padding:8px 12px;cursor:pointer;color:#fff;font-size:13px}
  .switch.active{background:#e8b95a;color:#1a1206;border-color:#e8b95a;font-weight:700}
</style>
"""

BASE_HTML = """
<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{{ title }}</title>
""" + BASE_STYLE + """
</head><body>
<div class="top-bar">
  <button class="icon-btn" onclick="history.back()">✕</button>
  <h1>{{ title }}</h1>
  <button class="icon-btn" onclick="location.href='/profile'">⋮</button>
</div>
{{ body|safe }}
<div class="bottom-nav">
  <a class="nav-item {{ 'active' if active=='games' }}" href="/"><span class="ic">🎮</span>ИГРЫ</a>
  <a class="nav-item {{ 'active' if active=='spin' }}" href="/spin"><span class="ic">🎡</span>СПИН</a>
  <a class="nav-item {{ 'active' if active=='top' }}" href="/top"><span class="ic">🏆</span>ТОП</a>
  <a class="nav-item {{ 'active' if active=='profile' }}" href="/profile"><span class="ic">👤</span>ПРОФИЛЬ</a>
</div>
</body></html>
"""

INDEX_BODY = """
<a href="/" style="text-decoration:none">
<div class="balance-row">
  <div class="lbl">Golden</div>
  <div class="balance-pill">⦿ {{ balance }}</div>
</div>
</a>
<div class="welcome-card">
  <h3>Добро пожаловать,</h3>
  <h2>взятник #физы</h2>
  <div class="coin-big">{{ balance }}</div>
  <div class="coin-sub">⦿ монет казино</div>
  <div class="slot-illus">🎰</div>
</div>
<div class="section-title">ВЫБЕРИ ИГРУ</div>
<div class="games-grid">
  <a class="game-card" href="/slots">
    <div class="badge">до x50</div>
    <div class="icon">🎰</div>
    <h4>Слоты</h4>
    <p>Три символа в ряд</p>
  </a>
  <a class="game-card" href="/dice">
    <div class="badge">x5</div>
    <div class="icon">🎲</div>
    <h4>Кости</h4>
    <p>Угадай число 1–6</p>
  </a>
  <a class="game-card" href="/coin">
    <div class="badge">x2</div>
    <div class="icon">🪙</div>
    <h4>Монетка</h4>
    <p>Орёл или решка</p>
  </a>
  <a class="game-card" href="/blackjack">
    <div class="badge">x2.5</div>
    <div class="icon">♠️</div>
    <h4>Блэкджек</h4>
    <p>21 против дилера</p>
  </a>
  <a class="game-card football" href="/football">
    <div class="badge no-reward">без награды</div>
    <div class="icon">⚽</div>
    <h4>Футбол</h4>
    <p>Пенальти — удар от ворот</p>
  </a>
  <a class="game-card mines" href="/minesweeper">
    <div class="badge no-reward">без награды</div>
    <div class="icon">💣</div>
    <h4>Сапёр</h4>
    <p>Выбери размер поля</p>
  </a>
  <a class="game-card ttt" href="/tictactoe">
    <div class="badge no-reward">без награды</div>
    <div class="icon">❌⭕</div>
    <h4>Крестики-нолики</h4>
    <p>По очереди с ботом</p>
  </a>
</div>
"""

# ---------------- Состояние ----------------
def get_balance():
    if "balance" not in session:
        session["balance"] = 100
    return session["balance"]

def set_balance(v):
    session["balance"] = max(0, int(v))

def take_bet(default=10, label="ставка", min_v=1):
    """Парсит ставку из POST. Возвращает (amount, error_msg)."""
    raw = request.form.get("bet", "").strip()
    if not raw:
        return default, None
    try:
        amount = int(float(raw))
    except ValueError:
        return None, f"Введите число в поле «{label}»"
    if amount < min_v:
        return None, f"Минимальная {label} — {min_v}"
    bal = get_balance()
    if amount > bal:
        return None, "Недостаточно монет"
    return amount, None

# ---------------- Главная ----------------
@app.route("/")
def index():
    return render_template_string(BASE_HTML,
        title="Golden Casino!",
        body=INDEX_BODY.replace("{{ balance }}", str(get_balance())),
        active="games")

# ---------------- Слоты ----------------
SLOTS_BODY = """
<a class="back" href="/">← назад</a>
<div class="balance-strip"><span>Баланс</span><span class="b">{{ balance }}</span></div>
<div class="panel">
  <h2>🎰 Слоты</h2>
  <form method="post">
    <div class="row mb">
      <input type="number" name="bet" placeholder="Ставка" value="{{ default_bet }}" min="1" max="{{ balance }}" required>
      <button class="btn">Крутить</button>
    </div>
  </form>
  <div class="reels">
    <div class="reel">{{ r0 }}</div>
    <div class="reel">{{ r1 }}</div>
    <div class="reel">{{ r2 }}</div>
  </div>
  {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  <div class="small">Три одинаковых — x10. Два одинаковых — x2. Разные — проигрыш. Джекпот (💎💎💎) — x50.</div>
</div>
"""
@app.route("/slots", methods=["GET","POST"])
def slots():
    bal = get_balance()
    state = session.get("slots_state") or {"r":["🍒","🍋","🔔"]}
    msg, msg_cls, default_bet = "", "", 10
    if request.method == "POST":
        bet, err = take_bet(default=10, label="ставка")
        if err:
            msg, msg_cls = err, "lose"
            default_bet = int(request.form.get("bet") or 10)
        else:
            syms = ["🍒","🍋","🔔","⭐","7️⃣","💎"]
            r = [random.choice(syms) for _ in range(3)]
            state = {"r": r}
            if r[0]==r[1]==r[2]:
                if r[0]=="💎":
                    win = bet*50
                    msg = f"ДЖЕКПОТ! +{win} монет"
                else:
                    win = bet*10
                    msg = f"Три в ряд! +{win} монет"
                bal += win
                msg_cls = "win"
            elif r[0]==r[1] or r[1]==r[2] or r[0]==r[2]:
                win = bet*2
                bal += win
                msg = f"Два совпали! +{win}"
                msg_cls = "win"
            else:
                bal -= bet
                msg = f"−{bet} монет"
                msg_cls = "lose"
            set_balance(bal)
            default_bet = bet
            session["slots_state"] = state
    set_balance(bal)
    body = ("<div class='container'>" +
        (SLOTS_BODY
            .replace("{{ balance }}", str(bal))
            .replace("{{ default_bet }}", str(default_bet))
            .replace("{{ r0 }}", state["r"][0])
            .replace("{{ r1 }}", state["r"][1])
            .replace("{{ r2 }}", state["r"][2])
        ).replace("{% if msg %}", "").replace("{% endif %}", "")
        .replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
        + "</div>")
    return render_template_string(BASE_HTML, title="Слоты", body=body, active="games")

# ---------------- Кости ----------------
DICE_BODY = """
<a class="back" href="/">← назад</a>
<div class="balance-strip"><span>Баланс</span><span class="b">{{ balance }}</span></div>
<div class="panel">
  <h2>🎲 Кости</h2>
  <form method="post">
    <div class="row mb">
      <input type="number" name="bet" placeholder="Ставка" value="{{ default_bet }}" min="1" max="{{ balance }}" required>
      <select name="guess">
        {% for n in range(1,7) %}<option value="{{n}}">{{n}}</option>{% endfor %}
      </select>
      <button class="btn">Бросить</button>
    </div>
  </form>
  <div class="dice"><div class="die">{{ face }}</div></div>
  {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  <div class="small">Угадал число — x5.</div>
</div>
"""
@app.route("/dice", methods=["GET","POST"])
def dice():
    bal = get_balance()
    state = session.get("dice_state") or {"face":"🎲"}
    msg, msg_cls, default_bet = "", "", 10
    if request.method == "POST":
        bet, err = take_bet(default=10, label="ставка")
        guess = request.form.get("guess","1")
        try: guess = int(guess)
        except: guess = 1
        if err:
            msg, msg_cls = err, "lose"
            default_bet = int(request.form.get("bet") or 10)
        else:
            face = random.randint(1,6)
            state = {"face": face}
            if face == guess:
                win = bet*5
                bal += win
                msg = f"Угадал! Выпало {face}. +{win}"
                msg_cls = "win"
            else:
                bal -= bet
                msg = f"Выпало {face}. −{bet}"
                msg_cls = "lose"
            set_balance(bal)
            default_bet = bet
            session["dice_state"] = state
    set_balance(bal)
    face_show = state["face"] if isinstance(state["face"], int) else "🎲"
    body = ("<div class='container'>" +
        (DICE_BODY
            .replace("{{ balance }}", str(bal))
            .replace("{{ default_bet }}", str(default_bet))
            .replace("{{ face }}", str(face_show))
        ).replace("{% if msg %}","").replace("{% endif %}","")
        .replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
        .replace("{% for n in range(1,7) %}","").replace("{% endfor %}","")
        + "</div>")
    return render_template_string(BASE_HTML, title="Кости", body=body, active="games")

# ---------------- Монетка ----------------
COIN_BODY = """
<a class="back" href="/">← назад</a>
<div class="balance-strip"><span>Баланс</span><span class="b">{{ balance }}</span></div>
<div class="panel">
  <h2>🪙 Монетка</h2>
  <form method="post">
    <div class="row mb">
      <input type="number" name="bet" placeholder="Ставка" value="{{ default_bet }}" min="1" max="{{ balance }}" required>
      <select name="side">
        <option value="heads">Орёл 👑</option>
        <option value="tails">Решка 🦅</option>
      </select>
      <button class="btn">Бросить</button>
    </div>
  </form>
  <div class="coin">{{ coin }}</div>
  {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  <div class="small">Угадал — x2.</div>
</div>
"""
@app.route("/coin", methods=["GET","POST"])
def coin():
    bal = get_balance()
    state = session.get("coin_state") or {"side":"heads"}
    msg, msg_cls, default_bet = "", "", 10
    if request.method == "POST":
        bet, err = take_bet(default=10, label="ставка")
        side = request.form.get("side","heads")
        if err:
            msg, msg_cls = err, "lose"
            default_bet = int(request.form.get("bet") or 10)
        else:
            result = random.choice(["heads","tails"])
            state = {"side": result}
            if side == result:
                win = bet*2
                bal += win
                msg = f"Угадал! +{win}"
                msg_cls = "win"
            else:
                bal -= bet
                msg = f"Не угадал. −{bet}"
                msg_cls = "lose"
            set_balance(bal)
            default_bet = bet
            session["coin_state"] = state
    set_balance(bal)
    coin_show = "👑" if state["side"]=="heads" else "🦅"
    body = ("<div class='container'>" +
        (COIN_BODY
            .replace("{{ balance }}", str(bal))
            .replace("{{ default_bet }}", str(default_bet))
            .replace("{{ coin }}", coin_show)
        ).replace("{% if msg %}","").replace("{% endif %}","")
        .replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
        .replace("<option value=\"heads\">Орёл 👑</option>","<option value=\"heads\" selected>Орёл 👑</option>")
        .replace("<option value=\"tails\">Решка 🦅</option>","<option value=\"tails\" selected>Решка 🦅</option>")
        + "</div>")
    return render_template_string(BASE_HTML, title="Монетка", body=body, active="games")

# ---------------- Блэкджек ----------------
BJ_BODY = """
<a class="back" href="/">← назад</a>
<div class="balance-strip"><span>Баланс</span><span class="b">{{ balance }}</span></div>
<div class="panel">
  <h2>♠️ Блэкджек</h2>
  <form method="post">
    <div class="row mb">
      <input type="number" name="bet" placeholder="Ставка" value="{{ default_bet }}" min="1" max="{{ balance }}" required>
      <button class="btn" name="action" value="deal">Сдать</button>
    </div>
  </form>
  <div class="results-box">
Игрок: {{ player }}
Дилер: {{ dealer }}
  </div>
  {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  {% if state == 'play' %}
  <form method="post">
    <input type="hidden" name="bet" value="{{ default_bet }}">
    <div class="row mb">
      <button class="btn" name="action" value="hit">Ещё</button>
      <button class="btn ghost" name="action" value="stand">Хватит</button>
    </div>
  </form>
  {% endif %}
  <div class="small">Победа — x2.5. Блэкджек (21 с двух карт) — x3.</div>
</div>
"""
def bj_new_deck():
    vals = [2,3,4,5,6,7,8,9,10,10,10,10,11]
    deck = vals*4
    random.shuffle(deck)
    return deck

def bj_hand_str(hand):
    return " ".join(str(c) for c in hand) + f"  ({sum_bj(hand)})"

def sum_bj(hand):
    s = sum(hand)
    aces = hand.count(11)
    while s>21 and aces:
        s -= 10
        aces -= 1
    return s

@app.route("/blackjack", methods=["GET","POST"])
def blackjack():
    bal = get_balance()
    state = session.get("bj") or {"deck":[], "player":[], "dealer":[], "phase":"idle", "bet":0}
    msg, msg_cls, default_bet = "", "", state.get("bet") or 10
    action = request.form.get("action","deal")
    bet_raw = request.form.get("bet")
    if bet_raw:
        try: default_bet = int(float(bet_raw))
        except: pass

    if action == "deal":
        bet, err = take_bet(default=10, label="ставка")
        if err:
            msg, msg_cls = err, "lose"
        else:
            deck = bj_new_deck()
            p = [deck.pop(), deck.pop()]
            d = [deck.pop(), deck.pop()]
            state = {"deck":deck, "player":p, "dealer":d, "phase":"play", "bet":bet}
            if sum_bj(p) == 21:
                # мгновенный блэкджек
                win = bet*3
                bal += win
                msg = f"БЛЭКДЖЕК! +{win}"
                msg_cls = "win"
                state["phase"] = "done"
                set_balance(bal)
            else:
                bal -= bet
                set_balance(bal)
                msg = "Берите ещё или хватит"
                msg_cls = "info"
            session["bj"] = state
    elif action == "hit" and state.get("phase") == "play":
        state["player"].append(state["deck"].pop())
        if sum_bj(state["player"]) > 21:
            msg = f"Перебор! −{state['bet']}"
            msg_cls = "lose"
            state["phase"] = "done"
        else:
            msg = "Берите ещё или хватит"
            msg_cls = "info"
        session["bj"] = state
    elif action == "stand" and state.get("phase") == "play":
        while sum_bj(state["dealer"]) < 17:
            state["dealer"].append(state["deck"].pop())
        ps, ds = sum_bj(state["player"]), sum_bj(state["dealer"])
        if ds > 21 or ps > ds:
            win = int(state["bet"]*2.5)
            bal += win
            set_balance(bal)
            msg = f"Победа! +{win}"
            msg_cls = "win"
        elif ps == ds:
            bal += state["bet"]
            set_balance(bal)
            msg = "Ничья — ставка возвращена"
            msg_cls = "info"
        else:
            msg = f"Проигрыш. −{state['bet']}"
            msg_cls = "lose"
        state["phase"] = "done"
        session["bj"] = state

    set_balance(bal)
    body = ("<div class='container'>" +
        (BJ_BODY
            .replace("{{ balance }}", str(bal))
            .replace("{{ default_bet }}", str(default_bet))
            .replace("{{ player }}", bj_hand_str(state["player"]))
            .replace("{{ dealer }}", bj_hand_str(state["dealer"]))
        ).replace("{% if msg %}","").replace("{% endif %}","")
        .replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
        .replace("{% if state == 'play' %}",
                 "" if state.get("phase")=="play" else "<!--off")
        .replace("{% endif %}","-->")
        + "</div>")
    return render_template_string(BASE_HTML, title="Блэкджек", body=body, active="games")

# ---------------- Футбол (без награды) ----------------
FOOTBALL_BODY = """
<a class="back" href="/">← назад</a>
<div class="balance-strip"><span>Баланс</span><span class="b">{{ balance }}</span></div>
<div class="panel">
  <h2>⚽ Пенальти — удар от ворот</h2>
  <p class="small">Мяч стоит у ворот. Ты — вратарь (или бьющий?). Жми «Удар» — мяч полетит ОТ ворот вратарю соперника. Цель — попасть в створ (3 зоны).</p>
  <div class="pitch" id="pitch">
    <div class="goal"></div>
    <div class="ball" id="ball" style="left:50%;bottom:6px;transform:translateX(-50%)">⚽</div>
  </div>
  <form id="kickForm" method="post" onsubmit="return kick(event)">
    <div class="row mb">
      <select name="dir" id="dir">
        <option value="left">Левая зона</option>
        <option value="center">Центр</option>
        <option value="right">Правая зона</option>
      </select>
      <input type="hidden" name="res" id="res">
      <button class="btn">Удар от ворот</button>
    </div>
  </form>
  {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  <div class="row">
    <div class="stat">Ударов: <b>{{ kicks }}</b></div>
    <div class="stat">Голов: <b>{{ goals }}</b></div>
    <div class="stat">Сейвов соперника: <b>{{ saves }}</b></div>
  </div>
  <div class="small" style="margin-top:10px">В этом режиме награда не начисляется — просто тренировка.</div>
</div>
<script>
function kick(e){
  e.preventDefault();
  var dir = document.getElementById('dir').value;
  var ball = document.getElementById('ball');
  var pitch = document.getElementById('pitch');
  var pw = pitch.clientWidth;
  var bh = ball.clientWidth;
  var x;
  if(dir==='left') x = pw*0.18;
  else if(dir==='right') x = pw*0.82;
  else x = pw*0.5;
  ball.style.left = (x - bh/2) + 'px';
  ball.style.bottom = (pitch.clientHeight - 60) + 'px';
  document.getElementById('res').value = dir;
  setTimeout(function(){ document.getElementById('kickForm').submit(); }, 650);
  return false;
}
</script>
"""
@app.route("/football", methods=["GET","POST"])
def football():
    bal = get_balance()
    s = session.get("fb") or {"kicks":0,"goals":0,"saves":0,"msg":"","msg_cls":""}
    msg, msg_cls = "", ""
    if request.method == "POST":
        dir = request.form.get("res","center")
        s["kicks"] += 1
        # шанс попадания 50%, шанс сейва 35% от попаданий
        r = random.random()
        if r < 0.15:
            s["saves"] += 1
            msg = "Мимо ворот! Соперник разводит руками."
            msg_cls = "lose"
        elif r < 0.55:
            s["saves"] += 1
            msg = f"Сейв! Вратарь в зоне «{('лево','центр','право')[(['left','center','right'].index(dir))]}» вытащил."
            msg_cls = "lose"
        else:
            s["goals"] += 1
            msg = "ГОООЛ! Точно в сетку ⚽🔥"
            msg_cls = "win"
        s["msg"] = msg; s["msg_cls"] = msg_cls
        session["fb"] = s
    else:
        msg, msg_cls = s.get("msg",""), s.get("msg_cls","")
    body = ("<div class='container'>" +
        (FOOTBALL_BODY
            .replace("{{ balance }}", str(bal))
            .replace("{{ kicks }}", str(s["kicks"]))
            .replace("{{ goals }}", str(s["goals"]))
            .replace("{{ saves }}", str(s["saves"]))
        ).replace("{% if msg %}","").replace("{% endif %}","")
        .replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
        + "</div>")
    return render_template_string(BASE_HTML, title="Футбол", body=body, active="games")

# ---------------- Сапёр (без награды) ----------------
MS_BODY = """
<a class="back" href="/">← назад</a>
<div class="balance-strip"><span>Баланс</span><span class="b">{{ balance }}</span></div>
<div class="panel">
  <h2>💣 Сапёр</h2>
  <form method="get">
    <div class="row mb">
      <div class="small">Размер:</div>
      <div class="switches">
        <div class="switch {{ 'active' if size==6 }}" data-sz="6">6×6 (5 мин)</div>
        <div class="switch {{ 'active' if size==10 }}" data-sz="10">10×10 (15 мин)</div>
        <div class="switch {{ 'active' if size==16 }}" data-sz="16">16×16 (40 мин)</div>
      </div>
      <input type="hidden" name="size" id="sizeInput" value="{{ size }}">
      <button class="btn ghost">Новая игра</button>
    </div>
  </form>
  <div class="row">
    <div class="stat">Размер: <b>{{ size }}×{{ size }}</b></div>
    <div class="stat">Мин: <b>{{ mines }}</b></div>
    <div class="stat">Флажков: <b>{{ flags }}</b></div>
  </div>
  <form method="post" id="msForm">
    <input type="hidden" name="size" value="{{ size }}">
    <input type="hidden" name="action" value="reveal">
    <input type="hidden" name="x" id="x">
    <input type="hidden" name="y" id="y">
    <div class="field-mines" style="grid-template-columns:repeat({{ size }},1fr);max-width:min(95vw, {{ size*36 }}px);margin:14px auto">
      {% for y in range(size) %}
        {% for x in range(size) %}
          <div class="cell {{ cell_class(x,y) }}" onclick="msClick({{x}},{{y}})" oncontextmenu="msFlag({{x}},{{y}});return false;">{{ cell_text(x,y) }}</div>
        {% endfor %}
      {% endfor %}
    </div>
  </form>
  <form method="post" style="margin-top:8px">
    <input type="hidden" name="size" value="{{ size }}">
    <button class="btn ghost" name="action" value="reset">Сбросить</button>
  </form>
  {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  <div class="small">ЛКМ — открыть, ПКМ — поставить флаг. Награды нет — тренировка.</div>
</div>
<script>
function msClick(x,y){ document.getElementById('x').value=x; document.getElementById('y').value=y; document.getElementById('msForm').submit(); }
function msFlag(x,y){
  var inp = document.createElement('input');
  inp.type='hidden'; inp.name='flag'; inp.value=x+'_'+y;
  var f = document.getElementById('msForm');
  f.appendChild(inp); f.action.value='flag'; f.submit();
}
document.querySelectorAll('.switch').forEach(function(el){
  el.addEventListener('click',function(){
    document.getElementById('sizeInput').value = el.dataset.sz;
    el.parentElement.parentElement.parentElement.submit();
  });
});
</script>
"""
def ms_new(size):
    mines_count = {6:5, 10:15, 16:40}[size]
    cells = [[0]*size for _ in range(size)]
    mines = set()
    while len(mines) < mines_count:
        x = random.randint(0,size-1); y = random.randint(0,size-1)
        mines.add((x,y))
    for (x,y) in mines:
        cells[y][x] = -1
    for y in range(size):
        for x in range(size):
            if cells[y][x] == -1: continue
            c = 0
            for dy in (-1,0,1):
                for dx in (-1,0,1):
                    if dx==0 and dy==0: continue
                    nx,ny = x+dx, y+dy
                    if 0<=nx<size and 0<=ny<size and cells[ny][nx]==-1: c+=1
            cells[y][x] = c
    return cells, mines_count

def ms_open(state, x, y):
    if state["over"]: return
    size = state["size"]
    cells = state["cells"]
    if state["revealed"][y][x] or state["flagged"][y][x]: return
    state["revealed"][y][x] = True
    if cells[y][x] == -1:
        state["over"] = True
        state["win"] = False
        return
    if cells[y][x] == 0:
        for dy in (-1,0,1):
            for dx in (-1,0,1):
                nx,ny = x+dx, y+dy
                if 0<=nx<size and 0<=ny<size and not state["revealed"][ny][nx]:
                    ms_open(state, nx, ny)
    # проверка победы
    safe = size*size - state["mines"]
    opened = sum(sum(1 for v in row if v) for row in state["revealed"])
    if opened >= safe:
        state["over"] = True
        state["win"] = True

@app.route("/minesweeper", methods=["GET","POST"])
def minesweeper():
    bal = get_balance()
    size = 6
    try: size = int(request.values.get("size", 6))
    except: size = 6
    if size not in (6,10,16): size = 6
    msg, msg_cls = "", ""

    if request.method == "POST" and request.form.get("action") == "reset":
        session.pop("ms", None)

    state = session.get("ms")
    if not state or state.get("size") != size:
        cells, mc = ms_new(size)
        state = {"size":size, "cells":cells, "mines":mc, "revealed":[[False]*size for _ in range(size)], "flagged":[[False]*size for _ in range(size)], "over":False, "win":False}
        session["ms"] = state

    if request.method == "POST":
        if request.form.get("action") == "flag":
            try:
                x,y = map(int, request.form.get("flag","0_0").split("_"))
                if 0<=x<size and 0<=y<size and not state["revealed"][y][x]:
                    state["flagged"][y][x] = not state["flagged"][y][x]
            except: pass
        elif request.form.get("action") == "reveal":
            try:
                x,y = int(request.form.get("x",-1)), int(request.form.get("y",-1))
                if 0<=x<size and 0<=y<size:
                    ms_open(state, x, y)
            except: pass
        session["ms"] = state
        if state["over"]:
            if state["win"]:
                msg, msg_cls = "Поле разминировано! 🏁", "win"
            else:
                msg, msg_cls = "Бум! Ты подорвался 💥", "lose"

    flags = sum(sum(1 for v in row if v) for row in state["flagged"])

    def cell_class(x,y):
        if state["over"] and state["cells"][y][x]==-1: return "open mine"
        if state["revealed"][y][x]: return "open safe"
        if state["flagged"][y][x]: return "flag"
        return ""
    def cell_text(x,y):
        if not state["revealed"][y][x] and not (state["over"] and state["cells"][y][x]==-1):
            return ""
        v = state["cells"][y][x]
        if v == -1: return "💣"
        if v == 0: return ""
        return str(v)

    # Ручной рендер через str.replace (избегаем Jinja в строках)
    html = MS_BODY
    # Построим поле вручную
    field_html = f'<div class="field-mines" style="grid-template-columns:repeat({size},1fr);max-width:min(95vw, {size*36}px);margin:14px auto">'
    for y in range(size):
        for x in range(size):
            cls = cell_class(x,y)
            txt = cell_text(x,y)
            field_html += f'<div class="cell {cls}" onclick="msClick({x},{y})" oncontextmenu="msFlag({x},{y});return false;">{txt}</div>'
    field_html += "</div>"

    body = ("<div class='container'>" +
        html
        .replace("{{ balance }}", str(bal))
        .replace("{{ size }}", str(size))
        .replace("{{ mines }}", str(state["mines"]))
        .replace("{{ flags }}", str(flags))
        .replace("{% for y in range(size) %}","").replace("{% endfor %}","")
        .replace("{% for x in range(size) %}","").replace("{% endfor %","")
        + "</div>")
    # Заменим плейсхолдер поля на наш html
    body = body.replace(
        '<div class="field-mines" style="grid-template-columns:repeat(6,1fr);max-width:min(95vw, 216px);margin:14px auto"></div>',
        field_html
    )
    # Если size не 6 — заменим style по факту:
    body = body.replace(
        '<div class="field-mines" style="grid-template-columns:repeat(10,1fr);max-width:min(95vw, 360px);margin:14px auto"></div>',
        field_html
    ).replace(
        '<div class="field-mines" style="grid-template-columns:repeat(16,1fr);max-width:min(95vw, 576px);margin:14px auto"></div>',
        field_html
    )
    # Финальная замена для любого варианта placeholder
    import re
    body = re.sub(r'<div class="field-mines" style="grid-template-columns:repeat\(\d+,1fr\);[^"]*"></div>', field_html, body, count=1)
    body = body.replace("{% if msg %}","").replace("{% endif %}","")
    body = body.replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
    # active class для switch
    body = body.replace(f'data-sz="{size}">{{ \'active\' if size==6 }}', f'data-sz="6">')
    body = body.replace(f'data-sz="{size}">{{ \'active\' if size==10 }}', f'data-sz="10">')
    body = body.replace(f'data-sz="{size}">{{ \'active\' if size==16 }}', f'data-sz="16">')
    return render_template_string(BASE_HTML, title="Сапёр", body=body, active="games")

# ---------------- Крестики-нолики (без награды) ----------------
TTT_BODY = """
<a class="back" href="/">← назад</a>
<div class="balance-strip"><span>Баланс</span><span class="b">{{ balance }}</span></div>
<div class="panel">
  <h2>❌⭕ Крестики-нолики</h2>
  <p class="small">Ход по очереди. Кто начинает — рандом. Бот старается выиграть. Награды нет.</p>
  <div class="row mb">
    <div class="stat">Ты играешь за: <b>{{ player_sym }}</b></div>
    <div class="stat">Начинает: <b>{{ starter }}</b></div>
    <div class="stat">Ход: <b>{{ turn }}</b></div>
  </div>
  <form method="post" id="tttForm">
    <input type="hidden" name="move" id="move">
    <div class="ttt-board">
      {% for i in range(9) %}
        <div class="ttt-cell {{ 'x' if board[i]=='X' else '' }} {{ 'o' if board[i]=='O' else '' }} {{ 'win' if i in win_line else '' }}" onclick="tttClick({{i}})">{{ board[i] }}</div>
      {% endfor %}
    </div>
  </form>
  <form method="post">
    <div class="row mb">
      <button class="btn ghost" name="action" value="reset">Новая партия</button>
    </div>
  </form>
  {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  <div class="small">Счёт партии: победы {{ score['p'] }} · ничьи {{ score['d'] }} · победы бота {{ score['b'] }}</div>
</div>
<script>
function tttClick(i){
  var board = {{ board|tojson }};
  var turn = "{{ turn }}";
  var over = {{ 'true' if over else 'false' }};
  if (over || board[i] !== '') return;
  if (turn === "{{ starter }}" || (turn === 'X' && "{{ player_sym }}" === 'X') || (turn === 'O' && "{{ player_sym }}" === 'O')) {
    document.getElementById('move').value = i;
    document.getElementById('tttForm').submit();
  }
}
</script>
"""

WIN_LINES = [
    (0,1,2),(3,4,5),(6,7,8),
    (0,3,6),(1,4,7),(2,5,8),
    (0,4,8),(2,4,6)
]

def ttt_bot_move(board, bot_sym, human_sym):
    """Бот: 1) выиграть, если есть ход; 2) блокировать; 3) центр; 4) угол; 5) край."""
    # выиграть
    for line in WIN_LINES:
        vals = [board[i] for i in line]
        if vals.count(bot_sym)==2 and vals.count("")==1:
            return line[vals.index("")]
    # блок
    for line in WIN_LINES:
        vals = [board[i] for i in line]
        if vals.count(human_sym)==2 and vals.count("")==1:
            return line[vals.index("")]
    if board[4] == "": return 4
    for i in (0,2,6,8):
        if board[i] == "": return i
    for i in (1,3,5,7):
        if board[i] == "": return i
    return -1

def ttt_winner(board):
    for a,b,c in WIN_LINES:
        if board[a] and board[a]==board[b]==board[c]:
            return board[a], (a,b,c)
    if "" not in board: return "draw", ()
    return None, ()

@app.route("/tictactoe", methods=["GET","POST"])
def tictactoe():
    bal = get_balance()
    st = session.get("ttt")
    if not st or request.form.get("action") == "reset":
        player_sym = random.choice(["X","O"])
        starter = random.choice(["X","O"])
        st = {
            "board": [""]*9,
            "player_sym": player_sym,
            "starter": starter,
            "turn": starter,
            "over": False,
            "win_line": [],
            "score": st["score"] if (st and "score" in st) else {"p":0,"b":0,"d":0},
            "msg": "", "msg_cls": ""
        }
        # если первым ходит бот — сразу ход
        if (st["turn"] == "X" and st["player_sym"]=="O") or (st["turn"]=="O" and st["player_sym"]=="X"):
            move = ttt_bot_move(st["board"], "X" if st["player_sym"]=="O" else "O", st["player_sym"])
            if move >= 0:
                st["board"][move] = "X" if st["player_sym"]=="O" else "O"
                st["turn"] = st["player_sym"]
        session["ttt"] = st

    if request.method == "POST" and not st["over"] and request.form.get("move") is not None:
        try:
            i = int(request.form.get("move","-1"))
        except:
            i = -1
        if 0 <= i < 9 and st["board"][i] == "":
            # человек ходит только в свой ход
            if st["turn"] == st["player_sym"]:
                st["board"][i] = st["player_sym"]
                # проверить победу человека
                w, line = ttt_winner(st["board"])
                if w == st["player_sym"]:
                    st["over"] = True; st["win_line"] = list(line)
                    st["score"]["p"] += 1
                    st["msg"] = "Ты победил! 🏆"
                    st["msg_cls"] = "win"
                elif w == "draw":
                    st["over"] = True
                    st["score"]["d"] += 1
                    st["msg"] = "Ничья 🤝"
                    st["msg_cls"] = "info"
                else:
                    # ход бота
                    bot_sym = "O" if st["player_sym"]=="X" else "X"
                    st["turn"] = bot_sym
                    mv = ttt_bot_move(st["board"], bot_sym, st["player_sym"])
                    if mv >= 0:
                        st["board"][mv] = bot_sym
                    w2, line2 = ttt_winner(st["board"])
                    if w2 == bot_sym:
                        st["over"] = True; st["win_line"] = list(line2)
                        st["score"]["b"] += 1
                        st["msg"] = "Бот выиграл 🤖"
                        st["msg_cls"] = "lose"
                    elif w2 == "draw":
                        st["over"] = True
                        st["score"]["d"] += 1
                        st["msg"] = "Ничья 🤝"
                        st["msg_cls"] = "info"
                    else:
                        st["turn"] = st["player_sym"]
        session["ttt"] = st

    msg = st.get("msg","")
    msg_cls = st.get("msg_cls","")
    body = ("<div class='container'>" +
        (TTT_BODY
            .replace("{{ balance }}", str(bal))
            .replace("{{ player_sym }}", st["player_sym"])
            .replace("{{ starter }}", "ты" if st["starter"]==st["player_sym"] else "бот")
            .replace("{{ turn }}", ("ты" if st["turn"]==st["player_sym"] else "бот") if not st["over"] else "—")
            .replace("{{ score }}", str(st["score"]))
            .replace("{{ board|tojson }}", str(st["board"]).replace("'", '"'))
            .replace("{{ over }}", "true" if st["over"] else "false")
        ).replace("{% for i in range(9) %}","").replace("{% endfor %}","")
        .replace("{{ board[i] }}", "")
        .replace("{% if msg %}","").replace("{% endif %}","")
        .replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
        .replace("{{ score['p'] }}", str(st["score"]["p"]))
        .replace("{{ score['b'] }}", str(st["score"]["b"]))
        .replace("{{ score['d'] }}", str(st["score"]["d"]))
        + "</div>")
    # вставим реальную доску
    board_html = '<div class="ttt-board">'
    for i in range(9):
        cls = "ttt-cell"
        if st["board"][i] == "X": cls += " x"
        if st["board"][i] == "O": cls += " o"
        if i in st["win_line"]: cls += " win"
        board_html += f'<div class="{cls}" onclick="tttClick({i})">{st["board"][i]}</div>'
    board_html += "</div>"
    body = re.sub(r'<div class="ttt-board">.*?</div>', board_html, body, count=1, flags=re.S)
    return render_template_string(BASE_HTML, title="Крестики-нолики", body=body, active="games")

import re

# ---------------- Спин / Топ / Профиль ----------------
SPIN_BODY = """
<div class="container">
  <div class="panel">
    <h2>🎡 Спин</h2>
    <p class="small">Ежедневный бонус. Нажми — крути.</p>
    <div class="coin">🎁</div>
    <form method="post"><button class="btn" name="spin" value="1">Крутить (раз в день)</button></form>
    {% if msg %}<div class="msg {{ msg_cls }}">{{ msg }}</div>{% endif %}
  </div>
</div>
"""
@app.route("/spin", methods=["GET","POST"])
def spin():
    bal = get_balance()
    msg, msg_cls = "", ""
    if request.method == "POST":
        last = session.get("last_spin")
        from datetime import date
        today = str(date.today())
        if last == today:
            msg, msg_cls = "Уже крутил сегодня. Завтра ещё раз.", "info"
        else:
            prize = random.choice([5,10,15,25,50,0,0,100])
            bal += prize
            set_balance(bal)
            session["last_spin"] = today
            if prize > 0:
                msg = f"Выпало {prize} монет!"
                msg_cls = "win"
            else:
                msg = "Пусто! Попробуй завтра."
                msg_cls = "lose"
    body = ("<div class='container'>" +
        SPIN_BODY
        .replace("{% if msg %}","").replace("{% endif %}","")
        .replace("{{ msg }}", msg).replace("{{ msg_cls }}", msg_cls)
        + "</div>")
    return render_template_string(BASE_HTML, title="Спин", body=body, active="spin")

TOP_BODY = """
<div class="container">
  <div class="panel">
    <h2>🏆 Топ игроков</h2>
    <div class="results-box">1. взятник #физы — 9999 монет
2. ProSpinner — 4200
3. LuckyStrike — 3100
4. CoinMaster — 1850
5. NewPlayer — 250</div>
    <p class="small">Это демо-таблица. Скоро здесь будет реальный рейтинг.</p>
  </div>
</div>
"""
@app.route("/top")
def top():
    return render_template_string(BASE_HTML, title="Топ", body=TOP_BODY, active="top")

PROFILE_BODY = """
<div class="container">
  <div class="panel">
    <h2>👤 Профиль</h2>
    <div class="results-box">Ник: взятник #физы
Баланс: {{ balance }} монет
Игр сыграно: {{ games }}
Последний выигрыш: {{ last_win }}</div>
    <form method="post" style="margin-top:12px"><button class="btn ghost" name="reset" value="1">Сбросить баланс до 100</button></form>
  </div>
</div>
"""
@app.route("/profile", methods=["GET","POST"])
def profile():
    bal = get_balance()
    if request.method == "POST" and request.form.get("reset"):
        session["balance"] = 100
        bal = 100
    games = session.get("games_played", 0)
    last_win = session.get("last_win", "—")
    body = ("<div class='container'>" +
        PROFILE_BODY
        .replace("{{ balance }}", str(bal))
        .replace("{{ games }}", str(games))
        .replace("{{ last_win }}", last_win)
        + "</div>")
    return render_template_string(BASE_HTML, title="Профиль", body=body, active="profile")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
