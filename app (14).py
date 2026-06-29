"""Royal Spin — Telegram Mini App (Flask, single-file)."""
import hashlib
import hmac
import json
import os
import random
import time
import urllib.parse
from contextlib import contextmanager
from decimal import Decimal

import psycopg2
import psycopg2.extras
from flask import Flask, Response, jsonify, request

# --- config -----------------------------------------------------------------

BOT_TOKEN = "8361709660:AAHFOfvt1G_YsS79A66yP9DPGVlhMbMjMUQ".strip()
DATABASE_URL = "postgresql://bothost_db_fb587bd5cfc7:nxrYSSziFvKTp24e5vuivV9NEapvBJDWdCaK3cwX5Rw@node1.pghost.ru:15742/bothost_db_fb587bd5cfc7".strip()
PORT = int(os.getenv("PORT", "8080"))
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))

# Лимиты ставок
MIN_BET = Decimal("1")
MAX_BET = Decimal("100000")

if not BOT_TOKEN:
    print("[startup] WARN: BOT_TOKEN не задан — /api/* будет возвращать auth error", flush=True)
if not DATABASE_URL:
    print("[startup] WARN: DATABASE_URL не задан — приложение не сможет работать с БД", flush=True)

app = Flask(__name__)

# --- DB ---------------------------------------------------------------------

@contextmanager
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан на сервере")
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=DB_CONNECT_TIMEOUT)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ensure_schema() -> None:
    """Создаёт таблицу users, если её нет."""
    if not DATABASE_URL:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id        BIGINT PRIMARY KEY,
                    username       TEXT,
                    balance        DECIMAL DEFAULT 0,
                    bonus_claimed  BOOLEAN DEFAULT FALSE,
                    games_played   INT DEFAULT 0,
                    created_at     TIMESTAMP DEFAULT NOW()
                )
            """)
            # Миграции со старого формата
            try:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_claimed BOOLEAN DEFAULT FALSE")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS games_played INT DEFAULT 0")
            except Exception:
                pass
        conn.commit()


def ping_db() -> tuple:
    """Проверяет доступность БД. Возвращает (ok, error_message)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def get_balance(user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(balance, 0) FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return None if row is None else Decimal(row[0])


def get_or_create_user(user_id: int, username: str = ""):
    """Возвращает (row, balance). Создаёт юзера с балансом 0 при отсутствии."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO users (user_id, username, balance) VALUES (%s, %s, 0) RETURNING *",
                    (user_id, username or None),
                )
                row = cur.fetchone()
                conn.commit()
            else:
                cur_username = row.get("username") or ""
                if username and username != cur_username:
                    cur.execute(
                        "UPDATE users SET username = %s WHERE user_id = %s",
                        (username, user_id),
                    )
                    row["username"] = username
                    conn.commit()
            return row, Decimal(row.get("balance") or 0)


def change_balance(user_id: int, delta: Decimal) -> Decimal:
    """Атомарно меняет баланс. delta>0 — начисление, delta<0 — списание."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()
            if row is None:
                if delta < 0:
                    conn.rollback()
                    raise ValueError("Недостаточно средств")
                cur.execute(
                    "INSERT INTO users (user_id, balance) VALUES (%s, %s)",
                    (user_id, delta),
                )
                new_balance = Decimal(delta)
            else:
                current = Decimal(row[0] or 0)
                new_balance = current + delta
                if new_balance < 0:
                    conn.rollback()
                    raise ValueError("Недостаточно средств")
                cur.execute(
                    "UPDATE users SET balance = %s WHERE user_id = %s",
                    (new_balance, user_id),
                )
            conn.commit()
            return new_balance


# --- Telegram initData validation ------------------------------------------

def validate_init_data(init_data: str):
    """Возвращает dict c данными пользователя или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None
    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, recv_hash):
        return None
    if "user" in parsed:
        try:
            parsed["user"] = json.loads(parsed["user"])
        except Exception:
            pass
    return parsed


# --- routes -----------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    db_ok, db_err = ping_db()
    payload = {
        "ok": db_ok and bool(DATABASE_URL) and bool(BOT_TOKEN),
        "db_ok": db_ok,
        "bot_token_set": bool(BOT_TOKEN),
        "database_url_set": bool(DATABASE_URL),
    }
    if not db_ok:
        payload["db_error"] = db_err
    return jsonify(payload), (200 if payload["ok"] else 503)


@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/api/me", methods=["POST"])
def api_me():
    body = request.get_json(silent=True) or {}
    init_data = body.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data or "user" not in user_data:
        if not init_data:
            msg = "Открой приложение через Telegram-бота"
        elif not BOT_TOKEN:
            msg = "На сервере не задан BOT_TOKEN"
        else:
            msg = "Подпись initData не прошла проверку"
        return jsonify({"ok": False, "error": msg, "reason": "auth"}), 401
    user = user_data["user"]
    user_id = int(user.get("id", 0))
    if not user_id:
        return jsonify({"ok": False, "error": "Нет user_id в данных Telegram"}), 400
    username = user.get("username") or user.get("first_name") or ""
    try:
        row, balance = get_or_create_user(user_id, username)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ошибка БД: {e}", "reason": "db"}), 500
    return jsonify({
        "ok": True,
        "user": {
            "id": user_id,
            "username": user.get("username"),
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "photo_url": user.get("photo_url"),
        },
        "balance": float(balance),
    })


@app.route("/api/balance", methods=["GET", "POST"])
def api_balance():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        init_data = body.get("init_data", "")
    else:
        init_data = request.args.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data or "user" not in user_data:
        return jsonify({"ok": False, "error": "Auth failed", "reason": "auth"}), 401
    user_id = int(user_data["user"].get("id", 0))
    if not user_id:
        return jsonify({"ok": False, "error": "Нет user_id"}), 400
    try:
        balance = get_balance(user_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ошибка БД: {e}", "reason": "db"}), 500
    if balance is None:
        return jsonify({"ok": False, "error": "User not found", "reason": "not_found"}), 404
    return jsonify({
        "ok": True,
        "user_id": user_id,
        "balance": float(balance),
        "ts": int(time.time()),
    })


@app.route("/api/play", methods=["POST"])
def api_play():
    body = request.get_json(silent=True) or {}
    init_data = body.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data or "user" not in user_data:
        return jsonify({"ok": False, "error": "Не авторизован"}), 401
    user_id = int(user_data["user"].get("id", 0))
    if not user_id:
        return jsonify({"ok": False, "error": "Нет user_id"}), 400
    game = body.get("game")
    try:
        bet = Decimal(str(body.get("bet", "0")))
    except Exception:
        return jsonify({"ok": False, "error": "Ставка указана неверно"}), 400
    if bet < MIN_BET:
        return jsonify({"ok": False, "error": f"Мин. ставка {MIN_BET}"}), 400
    if bet > MAX_BET:
        return jsonify({"ok": False, "error": f"Макс. ставка {MAX_BET}"}), 400

    try:
        get_or_create_user(user_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ошибка БД: {e}", "reason": "db"}), 500

    try:
        change_balance(user_id, -bet)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ошибка БД при списании: {e}"}), 500

    result = {"bet": float(bet), "win": 0, "coef": 1, "balance": 0}
    try:
        if game == "dice":
            result = play_dice(body, bet, user_id)
        elif game == "football":
            result = play_football(body, bet, user_id)
        elif game == "basket":
            result = play_basket(body, bet, user_id)
        elif game == "slots":
            result = play_slots(body, bet, user_id)
        else:
            new_balance = change_balance(user_id, bet)
            return jsonify({"ok": False, "error": "Неизвестная игра", "balance": float(new_balance)}), 400
    except Exception as e:
        try:
            new_balance = change_balance(user_id, bet)
        except Exception:
            new_balance = Decimal("0")
        return jsonify({"ok": False, "error": f"Ошибка: {e}", "balance": float(new_balance)}), 500

    final_balance = get_balance(user_id) or Decimal("0")
    result["balance"] = float(final_balance)
    result["ok"] = True
    return jsonify(result)


# --- games ------------------------------------------------------------------

def play_dice(body, bet: Decimal, user_id: int) -> dict:
    """Кубик 1-6: точное попадание x6, ≥4 — x2, иначе 0."""
    choice = int(body.get("choice", 0))
    if not 1 <= choice <= 6:
        raise ValueError("Неверный выбор числа")
    rolled = random.randint(1, 6)
    if rolled == choice:
        coef = Decimal("6")
    elif rolled >= 4:
        coef = Decimal("2")
    else:
        coef = Decimal("0")
    win = (bet * coef).quantize(Decimal("0.01"))
    if win > 0:
        change_balance(user_id, win)
    return {"rolled": rolled, "win": float(win), "coef": float(coef), "bet": float(bet)}


def play_football(body, bet: Decimal, user_id: int) -> dict:
    """Пенальти: 50/50. Угадал — x2."""
    choice = body.get("choice", "goal")
    if choice not in ("goal", "miss"):
        raise ValueError("Неверный выбор")
    outcome = random.choice(("goal", "miss"))
    if outcome == choice:
        coef = Decimal("2")
        win = (bet * coef).quantize(Decimal("0.01"))
        change_balance(user_id, win)
    else:
        coef = Decimal("0")
        win = Decimal("0")
    return {"outcome": outcome, "userChoice": choice, "win": float(win), "coef": float(coef), "bet": float(bet)}


def play_basket(body, bet: Decimal, user_id: int) -> dict:
    """Баскетбол: 3 броска, шанс попадания 60%. 3/3 — x4, 2/3 — x2, 1/3 — x1."""
    hits = sum(1 for _ in range(3) if random.random() < 0.6)
    table = {3: Decimal("4"), 2: Decimal("2"), 1: Decimal("1"), 0: Decimal("0")}
    coef = table[hits]
    win = (bet * coef).quantize(Decimal("0.01"))
    if win > 0:
        change_balance(user_id, win)
    return {"hits": hits, "win": float(win), "coef": float(coef), "bet": float(bet)}


def play_slots(body, bet: Decimal, user_id: int) -> dict:
    """Слоты: 3 барабана. Три одинаковых — x10, два — x3."""
    symbols = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎"]
    reels = [random.choice(symbols) for _ in range(3)]
    if reels[0] == reels[1] == reels[2]:
        coef = Decimal("10")
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        coef = Decimal("3")
    else:
        coef = Decimal("0")
    win = (bet * coef).quantize(Decimal("0.01"))
    if win > 0:
        change_balance(user_id, win)
    return {"reels": reels, "win": float(win), "coef": float(coef), "bet": float(bet)}


# --- HTML / JS --------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <title>Royal Spin — Mini App</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #0f172a; --bg2: #1e293b; --card: #1e293b;
      --accent: #facc15; --accent2: #22c55e; --danger: #ef4444;
      --text: #f1f5f9; --muted: #94a3b8;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0; padding: 0; min-height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: linear-gradient(180deg, #0f172a 0%, #1e1b4b 100%);
      color: var(--text);
      -webkit-tap-highlight-color: transparent;
    }
    .app { padding: 16px; padding-bottom: 100px; max-width: 600px; margin: 0 auto; }

    .header {
      display: flex; align-items: center; gap: 12px;
      background: var(--card); border-radius: 18px; padding: 14px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.3);
    }
    .avatar {
      width: 56px; height: 56px; border-radius: 50%;
      background: linear-gradient(135deg, #f59e0b, #ef4444);
      display: flex; align-items: center; justify-content: center;
      font-weight: bold; font-size: 22px;
      overflow: hidden; flex-shrink: 0;
    }
    .avatar img { width: 100%; height: 100%; object-fit: cover; }

    .user-info { flex: 1; min-width: 0; }
    .user-info .name { font-size: 17px; font-weight: 600; }
    .balance-row { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
    .balance-row .balance {
      color: var(--accent); font-weight: 700; font-size: 19px;
      transition: transform .25s ease, color .25s ease;
    }
    .balance-row .balance.bump { animation: bump .55s ease; }
    @keyframes bump {
      0%   { transform: scale(1);   color: var(--accent); }
      30%  { transform: scale(1.18); color: var(--accent2); }
      100% { transform: scale(1);   color: var(--accent); }
    }

    .grid { margin-top: 18px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .game-card {
      background: var(--card); border-radius: 18px;
      padding: 22px 12px; text-align: center;
      font-size: 16px; font-weight: 600;
      cursor: pointer; user-select: none;
      transition: transform .12s ease, background .12s ease;
      border: 2px solid transparent;
    }
    .game-card:active { transform: scale(0.96); }
    .game-card .emoji { font-size: 38px; display: block; margin-bottom: 6px; }
    .game-card.active { border-color: var(--accent); background: #2a2540; }

    .panel {
      margin-top: 18px; background: var(--card);
      border-radius: 18px; padding: 18px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.3);
    }
    .panel h2 {
      margin: 0 0 12px; font-size: 20px;
      display: flex; align-items: center; gap: 8px;
    }
    .panel h2 .emoji { font-size: 28px; }

    .row { display: flex; gap: 10px; flex-wrap: wrap; }
    .num-btn, .choice-btn {
      flex: 1 1 calc(33% - 10px);
      background: #334155; border: none; color: var(--text);
      padding: 14px 8px; border-radius: 12px;
      font-size: 17px; font-weight: 600; cursor: pointer;
      min-width: 60px;
    }
    .num-btn.active, .choice-btn.active { background: var(--accent); color: #000; }
    .num-btn:disabled, .choice-btn:disabled { opacity: .45; cursor: not-allowed; }

    .input-row {
      display: flex; gap: 10px; align-items: center;
      margin: 14px 0 10px;
    }
    .input-row label { color: var(--muted); font-size: 14px; }
    .input-row input {
      flex: 1;
      background: #0f172a; border: 1px solid #334155;
      color: var(--text); padding: 12px 14px;
      border-radius: 12px; font-size: 16px; outline: none;
    }
    .input-row input:focus { border-color: var(--accent); }

    .play-btn {
      width: 100%;
      background: linear-gradient(135deg, #f59e0b, #ef4444);
      color: #fff; border: none; padding: 16px;
      border-radius: 14px; font-size: 18px; font-weight: 700;
      cursor: pointer; margin-top: 8px;
      box-shadow: 0 6px 16px rgba(239, 68, 68, 0.3);
    }
    .play-btn:disabled { opacity: .5; cursor: not-allowed; }

    .quick-bets { display: flex; gap: 8px; margin-top: 6px; }
    .quick-bets button {
      flex: 1; background: #334155; color: var(--text);
      border: none; padding: 8px; border-radius: 10px;
      font-size: 13px; cursor: pointer;
    }

    .result {
      margin-top: 14px; padding: 14px; border-radius: 12px;
      text-align: center; font-size: 16px; font-weight: 600;
      display: none;
    }
    .result.win  { background: rgba(34, 197, 94, 0.18);  color: #4ade80; display: block; }
    .result.lose { background: rgba(239, 68, 68, 0.18);  color: #fca5a5; display: block; }
    .result.draw { background: rgba(148, 163, 184, 0.18); color: #cbd5e1; display: block; }

    .slots { display: flex; justify-content: center; gap: 10px; margin: 14px 0; }
    .reel {
      width: 70px; height: 70px;
      background: #0f172a; border: 2px solid #334155;
      border-radius: 12px;
      display: flex; align-items: center; justify-content: center;
      font-size: 40px;
    }
    .reel.spin { animation: shake .3s infinite; }
    @keyframes shake {
      0%, 100% { transform: translateY(0); }
      25%      { transform: translateY(-6px); }
      75%      { transform: translateY(6px); }
    }

    .basket-ball {
      width: 80px; height: 80px; margin: 0 auto; border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #fb923c, #c2410c);
      display: flex; align-items: center; justify-content: center;
      font-size: 40px; transition: transform .4s ease;
    }
    .basket-ball.shoot { transform: translateY(-60px) scale(0.8); }
    .basket-result { text-align: center; font-size: 18px; font-weight: 700; margin: 10px 0; }

    .hidden { display: none; }

    /* Экран загрузки + экран ошибки в одном месте */
    .loading {
      position: fixed; inset: 0;
      background: var(--bg);
      display: flex; align-items: center; justify-content: center;
      z-index: 100; flex-direction: column; gap: 12px;
    }
    .loading.error { background: var(--bg); }
    .spinner {
      width: 40px; height: 40px;
      border: 4px solid #334155;
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .err-box {
      color: #fca5a5; padding: 24px;
      text-align: center; max-width: 340px;
    }
    .err-box .ttl { font-size: 18px; margin-bottom: 8px; }
    .err-box .msg { font-size: 14px; opacity: .95; margin-bottom: 6px; }
    .err-box .hint { font-size: 12px; opacity: .6; margin-bottom: 14px; }
    .err-box button {
      margin-top: 8px;
      background: #facc15; color: #000;
      border: none; padding: 10px 20px;
      border-radius: 10px;
      font-weight: 600; cursor: pointer;
    }
  </style>
</head>
<body>
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div>Загружаем...</div>
  </div>

  <div class="app hidden" id="app">
    <div class="header">
      <div class="avatar" id="avatar">?</div>
      <div class="user-info">
        <div class="name" id="username">@user</div>
        <div class="balance-row">
          <span class="balance" id="balance">0 ⭐</span>
        </div>
      </div>
    </div>

    <div class="grid">
      <div class="game-card" data-game="dice">     <span class="emoji">🎲</span>Кубик</div>
      <div class="game-card" data-game="football"> <span class="emoji">⚽</span>Футбол</div>
      <div class="game-card" data-game="basket">   <span class="emoji">🏀</span>Баскетбол</div>
      <div class="game-card" data-game="slots">    <span class="emoji">🎰</span>Слоты</div>
    </div>

    <div class="panel hidden" id="panel-dice">
      <h2><span class="emoji">🎲</span>Кубик — угадай число 1–6</h2>
      <div class="row" id="dice-numbers"></div>
      <div class="input-row">
        <label>Ставка:</label>
        <input type="number" id="dice-bet" min="1" placeholder="Введите сумму" />
      </div>
      <div class="quick-bets">
        <button onclick="setBet('dice-bet', 10)">10</button>
        <button onclick="setBet('dice-bet', 50)">50</button>
        <button onclick="setBet('dice-bet', 100)">100</button>
        <button onclick="setBet('dice-bet', 'all')">MAX</button>
      </div>
      <button class="play-btn" id="dice-play">🎲 Бросить кубик</button>
      <div class="result" id="dice-result"></div>
    </div>

    <div class="panel hidden" id="panel-football">
      <h2><span class="emoji">⚽</span>Футбол — пенальти</h2>
      <div class="row">
        <button class="choice-btn active" data-choose="goal">⚽ Гол</button>
        <button class="choice-btn" data-choose="miss">❌ Мимо</button>
      </div>
      <div class="input-row">
        <label>Ставка:</label>
        <input type="number" id="foot-bet" min="1" placeholder="Введите сумму" />
      </div>
      <div class="quick-bets">
        <button onclick="setBet('foot-bet', 10)">10</button>
        <button onclick="setBet('foot-bet', 50)">50</button>
        <button onclick="setBet('foot-bet', 100)">100</button>
        <button onclick="setBet('foot-bet', 'all')">MAX</button>
      </div>
      <button class="play-btn" id="foot-play">🥅 Пенальти!</button>
      <div class="result" id="foot-result"></div>
    </div>

    <div class="panel hidden" id="panel-basket">
      <h2><span class="emoji">🏀</span>Баскетбол — 3 броска</h2>
      <div style="text-align:center">
        <div class="basket-ball" id="ball">🏀</div>
        <div class="basket-result" id="basket-shots">Попаданий: 0 / 3</div>
      </div>
      <div class="input-row">
        <label>Ставка:</label>
        <input type="number" id="basket-bet" min="1" placeholder="Введите сумму" />
      </div>
      <div class="quick-bets">
        <button onclick="setBet('basket-bet', 10)">10</button>
        <button onclick="setBet('basket-bet', 50)">50</button>
        <button onclick="setBet('basket-bet', 100)">100</button>
        <button onclick="setBet('basket-bet', 'all')">MAX</button>
      </div>
      <button class="play-btn" id="basket-play">🏀 Бросить 3 раза</button>
      <div class="result" id="basket-result"></div>
    </div>

    <div class="panel hidden" id="panel-slots">
      <h2><span class="emoji">🎰</span>Слоты — три барабана</h2>
      <div class="slots">
        <div class="reel" id="reel1">🍒</div>
        <div class="reel" id="reel2">🍋</div>
        <div class="reel" id="reel3">🔔</div>
      </div>
      <div class="input-row">
        <label>Ставка:</label>
        <input type="number" id="slots-bet" min="1" placeholder="Введите сумму" />
      </div>
      <div class="quick-bets">
        <button onclick="setBet('slots-bet', 10)">10</button>
        <button onclick="setBet('slots-bet', 50)">50</button>
        <button onclick="setBet('slots-bet', 100)">100</button>
        <button onclick="setBet('slots-bet', 'all')">MAX</button>
      </div>
      <button class="play-btn" id="slots-play">🎰 Крутить!</button>
      <div class="result" id="slots-result"></div>
    </div>
  </div>

<script>
(function () {
  'use strict';

  // --- Telegram init -------------------------------------------------------
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    try { tg.ready(); tg.expand(); } catch (e) {}
  }
  var INIT_DATA = (tg && tg.initData) ? tg.initData : '';

  // --- state ---------------------------------------------------------------
  var state = {
    user: null,
    balance: 0,
    activeGame: null,
    diceChoice: null,
    footChoice: 'goal',
    done: false  // флаг «загрузка завершена» для страховочного таймера
  };

  // --- localStorage --------------------------------------------------------
  var LS_KEY = 'royal_spin_state_v1';
  function loadLs() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (!raw) return {};
      return JSON.parse(raw) || {};
    } catch (e) { return {}; }
  }
  function saveLs() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        activeGame: state.activeGame,
        diceChoice: state.diceChoice,
        footChoice: state.footChoice,
        bets: {
          'dice-bet':   getVal('dice-bet'),
          'foot-bet':   getVal('foot-bet'),
          'basket-bet': getVal('basket-bet'),
          'slots-bet':  getVal('slots-bet')
        }
      }));
    } catch (e) {}
  }
  function getVal(id) {
    var el = document.getElementById(id);
    return el ? (el.value || '') : '';
  }

  // --- helpers -------------------------------------------------------------
  window.setBet = function (inputId, val) {
    var el = document.getElementById(inputId);
    if (!el) return;
    if (val === 'all') el.value = Math.floor(state.balance);
    else el.value = val;
    saveLs();
  };

  function showErr(msg) {
    var app = document.querySelector('.app');
    if (!app) return;
    var div = document.createElement('div');
    div.className = 'err-msg';
    div.style.cssText = 'background:rgba(239,68,68,.15);color:#fca5a5;padding:12px;' +
      'border-radius:10px;margin-top:10px;font-size:14px;text-align:center;';
    div.textContent = msg;
    app.appendChild(div);
    setTimeout(function () { try { div.remove(); } catch (e) {} }, 4000);
  }

  function fetchWithTimeout(url, opts, timeoutMs) {
    opts = opts || {};
    timeoutMs = timeoutMs || 10000;
    var ctrl = new AbortController();
    var timer = setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, timeoutMs);
    return fetch(url, Object.assign({}, opts, { signal: ctrl.signal }))
      .finally(function () { clearTimeout(timer); });
  }

  // Показать ошибку вместо спиннера. ОДИН раз — чтобы таймер-failsafe
  // не затирал реальное сообщение об ошибке.
  function showLoadingError(msg, hint) {
    var loading = document.getElementById('loading');
    if (!loading) return;
    loading.classList.add('error');
    loading.innerHTML =
      '<div class="err-box">' +
        '<div class="ttl">⚠️ Не удалось загрузиться</div>' +
        '<div class="msg"></div>' +
        '<div class="hint"></div>' +
        '<button onclick="location.reload()">↻ Попробовать снова</button>' +
      '</div>';
    loading.querySelector('.msg').textContent  = msg || '';
    if (hint) {
      loading.querySelector('.hint').textContent = hint;
    } else {
      loading.querySelector('.hint').remove();
    }
  }

  function updateBalanceUi(prevBalance) {
    var el = document.getElementById('balance');
    if (!el) return;
    el.textContent = Number(state.balance).toLocaleString('ru-RU') + ' ⭐';
    if (prevBalance !== undefined && prevBalance !== state.balance) {
      el.classList.remove('bump');
      void el.offsetWidth;
      el.classList.add('bump');
    }
  }

  function showApp() {
    var loading = document.getElementById('loading');
    var app = document.getElementById('app');
    if (loading) loading.classList.add('hidden');
    if (app) app.classList.remove('hidden');
  }

  // Страховка: если за 18 секунд экран загрузки всё ещё виден —
  // показываем ошибку. Защита от вечной крутилки при любом сбое.
  setTimeout(function () {
    var loading = document.getElementById('loading');
    if (!loading || loading.classList.contains('hidden')) return;
    if (state.done) return;
    showLoadingError(
      'Сервер не отвечает слишком долго (таймаут 18 c).',
      'Проверь, запущен ли app.py и доступен ли PostgreSQL.'
    );
  }, 18000);

  // --- main load -----------------------------------------------------------
  async function loadUser() {
    if (!INIT_DATA) {
      showLoadingError(
        'Mini App нужно открывать из Telegram-бота.',
        'Зайди в бота → нажми кнопку с игрой. Прямая ссылка в браузере не сработает.'
      );
      return;
    }

    try {
      var h = await fetchWithTimeout('/health', { cache: 'no-store' }, 8000);
      var hj = await h.json();
      if (hj && !hj.db_ok) {
        showLoadingError(
          'Сервер не может подключиться к базе данных.',
          (hj && hj.db_error) || 'Проверь, запущен ли PostgreSQL и правильный ли DATABASE_URL'
        );
        return;
      }
    } catch (e) {
      // /health не критичен — пробуем /api/me
    }

    try {
      var r = await fetchWithTimeout('/api/me', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ init_data: INIT_DATA })
      }, 12000);
      var data = await r.json();

      if (!data || !data.ok) {
        var reason = data && data.reason;
        var hint = '';
        if (reason === 'auth' && !INIT_DATA) {
          hint = 'Запусти Mini App кнопкой из бота, а не через прямую ссылку.';
        } else if (reason === 'auth') {
          hint = 'Возможно, бот Mini App и тот, из которого открываешь — разные. BOT_TOKEN должен совпадать.';
        } else if (reason === 'db') {
          hint = 'Сервер Mini App не подключается к БД. Проверь DATABASE_URL.';
        }
        showLoadingError((data && data.error) || 'Ошибка авторизации', hint);
        return;
      }

      state.user = data.user;
      state.balance = Number(data.balance);

      var av = document.getElementById('avatar');
      if (data.user.photo_url) {
        av.innerHTML = '<img src="' + data.user.photo_url + '" alt="" ' +
          'onerror="this.parentElement.textContent=\'' +
          ((data.user.first_name || '?').trim()[0] || '?') + '\'" />';
      } else {
        av.textContent = (data.user.first_name || '?').trim()[0] || '?';
      }

      var u = data.user;
      var name = (u.username ? '@' + u.username : null) ||
                 [u.first_name, u.last_name].filter(Boolean).join(' ') ||
                 'Гость';
      var nameEl = document.getElementById('username');
      if (nameEl) nameEl.textContent = name;

      var ls = loadLs();
      if (ls.bets) {
        Object.keys(ls.bets).forEach(function (k) {
          var el = document.getElementById(k);
          if (el && ls.bets[k]) el.value = ls.bets[k];
        });
      }

      updateBalanceUi();
      showApp();

      // Восстановим выбранную игру / кубик / пенальти
      if (ls.activeGame) {
        var card = document.querySelector('.game-card[data-game="' + ls.activeGame + '"]');
        if (card) card.click();
      }
      if (ls.diceChoice) {
        var btn = document.querySelector('#dice-numbers .num-btn:nth-child(' + ls.diceChoice + ')');
        if (btn) btn.click();
      }
      if (ls.footChoice) {
        var fbtn = document.querySelector(
          '#panel-football .choice-btn[data-choose="' + ls.footChoice + '"]'
        );
        if (fbtn) fbtn.click();
      }
    } catch (e) {
      var isAbort = e && (e.name === 'AbortError' || /aborted/i.test(e.message || ''));
      showLoadingError(
        isAbort ? 'Сервер мини-аппа не отвечает (таймаут).' : 'Сервер мини-аппа недоступен.',
        isAbort
          ? 'Похоже, бэк зависает на подключении к БД или просто упал. Проверь, запущен ли app.py.'
          : ((e && e.message) || '') + ' — Проверь, запущен ли app.py и открыт ли порт.'
      );
    }
  }

  // --- game selection ------------------------------------------------------
  var cards = document.querySelectorAll('.game-card');
  for (var i = 0; i < cards.length; i++) {
    cards[i].addEventListener('click', function () {
      var g = this.dataset.game;
      state.activeGame = g;
      var all = document.querySelectorAll('.game-card');
      for (var j = 0; j < all.length; j++) all[j].classList.remove('active');
      this.classList.add('active');
      var panels = document.querySelectorAll('.panel');
      for (var k = 0; k < panels.length; k++) panels[k].classList.add('hidden');
      var target = document.getElementById('panel-' + g);
      if (target) target.classList.remove('hidden');
      var results = document.querySelectorAll('.result');
      for (var r2 = 0; r2 < results.length; r2++) {
        results[r2].classList.remove('win', 'lose', 'draw');
        results[r2].style.display = 'none';
      }
      saveLs();
    });
  }

  // --- dice buttons --------------------------------------------------------
  var diceWrap = document.getElementById('dice-numbers');
  for (var d = 1; d <= 6; d++) {
    (function (n) {
      var b = document.createElement('button');
      b.className = 'num-btn';
      b.textContent = n;
      b.addEventListener('click', function () {
        var xs = document.querySelectorAll('#dice-numbers .num-btn');
        for (var i2 = 0; i2 < xs.length; i2++) xs[i2].classList.remove('active');
        b.classList.add('active');
        state.diceChoice = n;
        saveLs();
      });
      diceWrap.appendChild(b);
    })(d);
  }

  // --- save bet on input ---------------------------------------------------
  ['dice-bet', 'foot-bet', 'basket-bet', 'slots-bet'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('input', saveLs);
  });

  // --- football toggle -----------------------------------------------------
  var fbtns = document.querySelectorAll('#panel-football .choice-btn');
  for (var fi = 0; fi < fbtns.length; fi++) {
    fbtns[fi].addEventListener('click', function () {
      var xs = document.querySelectorAll('#panel-football .choice-btn');
      for (var fi2 = 0; fi2 < xs.length; fi2++) xs[fi2].classList.remove('active');
      this.classList.add('active');
      state.footChoice = this.dataset.choose;
      saveLs();
    });
  }

  // --- play helper ---------------------------------------------------------
  async function playGame(game, payload) {
    var r = await fetchWithTimeout('/api/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ init_data: INIT_DATA, game: game }, payload))
    }, 15000);
    var data = await r.json();
    if (!data || !data.ok) throw new Error((data && data.error) || 'Ошибка');
    var prev = state.balance;
    state.balance = Number(data.balance);
    updateBalanceUi(prev);
    return data;
  }

  function showResult(id, type, text) {
    var el = document.getElementById(id);
    if (!el) return;
    el.className = 'result ' + type;
    el.style.display = 'block';
    el.textContent = text;
  }

  function haptic(win) {
    if (tg && tg.HapticFeedback) {
      try { tg.HapticFeedback.notificationOccurred(win ? 'success' : 'error'); } catch (e) {}
    }
  }

  // dice
  document.getElementById('dice-play').addEventListener('click', async function () {
    if (state.diceChoice === null) return showErr('Выберите число');
    var bet = parseFloat(document.getElementById('dice-bet').value);
    if (!bet || bet <= 0) return showErr('Введите ставку');
    var btn = document.getElementById('dice-play');
    btn.disabled = true;
    try {
      var r = await playGame('dice', { choice: state.diceChoice, bet: bet });
      var text = 'Выпало ' + r.rolled + '. Ваш выбор ' + state.diceChoice + '. ' +
        (r.win > 0 ? '+' + r.win + ' ⭐ (x' + r.coef + ')' : '-' + r.bet + ' ⭐');
      showResult('dice-result', r.win > 0 ? 'win' : 'lose', text);
      haptic(r.win > 0);
    } catch (e) { showErr(e.message); }
    finally { btn.disabled = false; }
  });

  // football
  document.getElementById('foot-play').addEventListener('click', async function () {
    var bet = parseFloat(document.getElementById('foot-bet').value);
    if (!bet || bet <= 0) return showErr('Введите ставку');
    var btn = document.getElementById('foot-play');
    btn.disabled = true;
    try {
      var r = await playGame('football', { choice: state.footChoice, bet: bet });
      var text;
      if (r.outcome === 'goal') {
        text = r.win > 0
          ? '⚽ ГООЛ! +' + r.win + ' ⭐ (x' + r.coef + ')'
          : '⚽ Гол, но вы ставили на «' + r.userChoice + '» — -' + r.bet + ' ⭐';
      } else {
        text = r.win > 0
          ? '❌ Мимо, но вы угадали! +' + r.win + ' ⭐'
          : '⚽ Гол, вы ставили «мимо» — -' + r.bet + ' ⭐';
      }
      showResult('foot-result', r.win > 0 ? 'win' : 'lose', text);
      haptic(r.win > 0);
    } catch (e) { showErr(e.message); }
    finally { btn.disabled = false; }
  });

  // basket
  document.getElementById('basket-play').addEventListener('click', async function () {
    var bet = parseFloat(document.getElementById('basket-bet').value);
    if (!bet || bet <= 0) return showErr('Введите ставку');
    var btn = document.getElementById('basket-play');
    btn.disabled = true;
    var ball = document.getElementById('ball');
    try {
      ball.classList.add('shoot');
      var r = await playGame('basket', { bet: bet });
      setTimeout(function () { ball.classList.remove('shoot'); }, 400);
      document.getElementById('basket-shots').textContent = 'Попаданий: ' + r.hits + ' / 3';
      var text = r.win > 0
        ? 'Попаданий: ' + r.hits + '/3 → +' + r.win + ' ⭐ (x' + r.coef + ')'
        : 'Попаданий: ' + r.hits + '/3 → -' + r.bet + ' ⭐';
      showResult('basket-result', r.win > 0 ? 'win' : 'lose', text);
      haptic(r.win > 0);
    } catch (e) {
      showErr(e.message);
      ball.classList.remove('shoot');
    } finally { btn.disabled = false; }
  });

  // slots
  document.getElementById('slots-play').addEventListener('click', async function () {
    var bet = parseFloat(document.getElementById('slots-bet').value);
    if (!bet || bet <= 0) return showErr('Введите ставку');
    var btn = document.getElementById('slots-play');
    btn.disabled = true;
    var reels = [
      document.getElementById('reel1'),
      document.getElementById('reel2'),
      document.getElementById('reel3')
    ];
    reels.forEach(function (rr) { rr.classList.add('spin'); });
    try {
      var data = await playGame('slots', { bet: bet });
      reels.forEach(function (rr, idx) {
        setTimeout(function () {
          rr.classList.remove('spin');
          rr.textContent = data.reels[idx];
        }, 300 + idx * 400);
      });
      setTimeout(function () {
        var text = data.win > 0
          ? '🎰 ' + data.reels.join(' ') + ' → +' + data.win + ' ⭐ (x' + data.coef + ')'
          : '🎰 ' + data.reels.join(' ') + ' → -' + data.bet + ' ⭐';
        showResult('slots-result', data.win > 0 ? 'win' : 'lose', text);
        haptic(data.win > 0);
      }, 300 + reels.length * 400 + 100);
    } catch (e) {
      reels.forEach(function (rr) { rr.classList.remove('spin'); });
      showErr(e.message);
    } finally {
      setTimeout(function () { btn.disabled = false; }, 1500);
    }
  });

  // --- start ---------------------------------------------------------------
  loadUser().finally(function () { state.done = true; });
})();
</script>
</body>
</html>
"""


# --- entrypoint -------------------------------------------------------------

try:
    ensure_schema()
    print("[startup] schema ensured", flush=True)
except Exception as e:
    print(f"[startup] WARN: ensure_schema failed: {e!r}", flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
