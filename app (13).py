"""
Royal Spin — Telegram Mini App (Flask, single file)
====================================================

Mini App с играми (кубик / футбол / баскетбол / слоты), который работает
поверх той же PostgreSQL БД, что и бот `zakaz`
(https://github.com/femilianferuk-droid/zakaz.git). Баланс у них общий,
потому что таблица `users` одна.

Ключевые отличия от старой версии
---------------------------------
1. Структура таблицы `users` идентична `zakaz/bot.py` — гарантируем
   один и тот же ключ `user_id BIGINT PRIMARY KEY` и те же поля
   (`username`, `balance`, `bonus_claimed`, `games_played`,
   `created_at`).
2. Жёсткие таймауты на все сетевые вызовы — никакого вечного
   «Загружаем...», при ошибке сразу понятное сообщение с кнопкой
   «Попробовать снова».
3. Сохранение состояния UI (выбранная игра, ставка) в localStorage —
   не теряется при перезагрузке/сворачивании.
4. Визуальная «пульсация» баланса при изменении после ставки.

Как обновляется баланс
----------------------
- При открытии mini-app — один запрос `/api/me` (получает баланс).
- После каждой ставки — баланс обновляется из ответа `/api/play`.
- Если бот `zakaz` поменял баланс вне mini-app (бонус, пополнение,
  вывод, игра в самом боте) — нужно перезагрузить страницу mini-app,
  чтобы он подтянулся.

ENV-переменные (BOT_TOKEN и DATABASE_URL — обязательны)
-------------------------------------------------------
  BOT_TOKEN              — токен бота, в котором зарегистрирован Mini App.
                           Должен совпадать с токеном бота zakaz, если
                           mini-app открывается из бота zakaz.
  DATABASE_URL           — строка подключения к PostgreSQL.
                           Должна СОВПАДАТЬ с той, что использует bot.py
                           zakaz, иначе баланс не будет общим.
  PORT                   — порт Flask (по умолчанию 8080).
  DB_CONNECT_TIMEOUT     — таймаут подключения к БД в секундах (5).

Структура таблицы users (должна совпадать с zakaz/bot.py):
  user_id        BIGINT PRIMARY KEY,
  username       TEXT,
  balance        DECIMAL DEFAULT 0,
  bonus_claimed  BOOLEAN DEFAULT FALSE,
  games_played   INT DEFAULT 0,
  created_at     TIMESTAMP DEFAULT NOW()
"""

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

# ============================================================================
# Конфигурация
# ============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))
# Интервал опроса баланса с сервера (мс). Запрос идёт на /api/balance.
# 3000 = 3 секунды — баланс обновляется почти мгновенно после действий в боте,
# но при этом нет лишней нагрузки.
# POLL_INTERVAL_MS больше не используется — бесконечный polling убран.
# Баланс обновляется один раз при старте (через /api/me) и после каждой
# ставки (из ответа /api/play). Если бот zakaz поменяет баланс вне
# mini-app — нужно перезагрузить страницу.

# Лимиты ставок (синхронизированы с zakaz/bot.py по смыслу)
MIN_BET = Decimal("1")
MAX_BET = Decimal("100000")

if not BOT_TOKEN:
    print(
        "[startup] WARN: BOT_TOKEN не задан — /api/* будет возвращать auth error",
        flush=True,
    )
if not DATABASE_URL:
    print(
        "[startup] WARN: DATABASE_URL не задан — приложение не сможет работать с БД",
        flush=True,
    )

app = Flask(__name__)


# ============================================================================
# БД
# ============================================================================

@contextmanager
def get_conn():
    """Контекстный менеджер подключения к PostgreSQL.

    connect_timeout важен: без него psycopg2 может зависнуть на десятки
    секунд (минуты) при недоступном хосте, и фронт будет крутить
    «Загружаем...» бесконечно.
    """
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
    """Создаёт таблицу users (совместимую с zakaz/bot.py), если её нет."""
    if not DATABASE_URL:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance DECIMAL DEFAULT 0,
                    bonus_claimed BOOLEAN DEFAULT FALSE,
                    games_played INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            )
            # Доп. колонки — на случай если бот ещё не стартовал и таблица
            # была создана в старом формате.
            try:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_claimed BOOLEAN DEFAULT FALSE"
                )
            except Exception:
                pass
            try:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS games_played INT DEFAULT 0"
                )
            except Exception:
                pass
        conn.commit()


def ping_db() -> tuple[bool, str | None]:
    """Проверяет доступность БД. Возвращает (ok, error_message)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def get_balance(user_id: int) -> Decimal | None:
    """Возвращает текущий баланс юзера или None, если юзер не найден."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(balance, 0) FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return None if row is None else Decimal(row[0])


def get_or_create_user(user_id: int, username: str = "") -> tuple[dict, Decimal]:
    """Возвращает (row, balance). Создаёт юзера со стартовым балансом 0,
    если записи нет. Обновляет username, если он изменился.
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO users (user_id, username, balance) "
                    "VALUES (%s, %s, 0) RETURNING *",
                    (user_id, username or None),
                )
                row = cur.fetchone()
                conn.commit()
            else:
                # Обновим username, если он реально поменялся
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
    """Атомарно меняет баланс на delta. Возвращает новый баланс.

    delta > 0 — начисление, delta < 0 — списание.
    Если итоговый баланс уходит в минус — откатываем (недостаточно средств).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT balance FROM users WHERE user_id = %s FOR UPDATE",
                (user_id,),
            )
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


# ============================================================================
# Telegram Mini App: валидация initData
# ============================================================================

def validate_init_data(init_data: str) -> dict | None:
    """Проверяет подпись initData, как требует Telegram WebApp.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Возвращает dict c данными пользователя или None при ошибке.
    """
    if not init_data:
        return None
    if not BOT_TOKEN:
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
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, recv_hash):
        return None

    if "user" in parsed:
        try:
            parsed["user"] = json.loads(parsed["user"])
        except Exception:
            pass

    return parsed


# ============================================================================
# Маршруты
# ============================================================================

@app.route("/health", methods=["GET"])
def health():
    """Эндпоинт для предварительной проверки (фронт стучится сюда при загрузке).
    Возвращает JSON с ok/db_ok, чтобы клиент мог показать осмысленную ошибку,
    если БД лежит, а не крутить «Загружаем...» бесконечно.
    """
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
    """Главная страница Mini App."""
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/api/me", methods=["POST"])
def api_me():
    """Авторизация: валидируем initData и возвращаем профиль + баланс.
    Создаёт юзера в БД (если его ещё нет) с балансом 0.
    """
    body = request.get_json(silent=True) or {}
    init_data = body.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data or "user" not in user_data:
        if not init_data:
            msg = "Открой приложение через Telegram-бота"
        elif not BOT_TOKEN:
            msg = "На сервере не задан BOT_TOKEN"
        else:
            msg = "Подпись initData не прошла проверку (неверный токен или устаревшие данные)"
        return jsonify({"ok": False, "error": msg, "reason": "auth"}), 401

    user = user_data["user"]
    user_id = int(user.get("id", 0))
    if not user_id:
        return jsonify({"ok": False, "error": "Нет user_id в данных Telegram"}), 400

    username = user.get("username") or user.get("first_name") or ""

    try:
        row, balance = get_or_create_user(user_id, username)
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"Ошибка БД: {e}",
            "reason": "db",
        }), 500

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
    """Лёгкий endpoint для polling-а баланса. Не создаёт юзера и не пишет в БД.

    Поддерживает и GET, и POST — для GET init_data передаётся в query (?init_data=...),
    для POST — в JSON-теле. GET удобнее для простого опроса.

    Возвращает 404, если юзер ещё не существует в БД (например, бот zakaz ещё
    не успел его создать). В этом случае polling вернёт not_found — это нормально,
    фронт просто покажет «обновлено только что» и попробует снова.
    """
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
        # Юзер ещё не создан. Возвращаем 404, чтобы фронт различал
        # «юзера нет» от «баланс = 0».
        return jsonify({"ok": False, "error": "User not found", "reason": "not_found"}), 404

    return jsonify({
        "ok": True,
        "user_id": user_id,
        "balance": float(balance),
        "ts": int(time.time()),
    })


@app.route("/api/play", methods=["POST"])
def api_play():
    """Главный эндпоинт для ставок. Один и тот же сервер для всех игр."""
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

    # Сначала убедимся, что юзер существует (бот zakaz мог его создать, но
    # мог и не успеть). get_or_create_user здесь безопасен.
    try:
        get_or_create_user(user_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ошибка БД: {e}", "reason": "db"}), 500

    # Списываем ставку
    try:
        change_balance(user_id, -bet)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ошибка БД при списании: {e}"}), 500

    # Запускаем нужную игру
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
        # Возвращаем ставку при ошибке игры
        try:
            new_balance = change_balance(user_id, bet)
        except Exception:
            new_balance = Decimal("0")
        return jsonify({"ok": False, "error": f"Ошибка: {e}", "balance": float(new_balance)}), 500

    # Баланс после игры перечитываем (для надёжности — на случай гонок)
    final_balance = get_balance(user_id) or Decimal("0")
    result["balance"] = float(final_balance)
    result["ok"] = True
    return jsonify(result)


# ============================================================================
# Игровая логика (на сервере — чтобы клиент не мог накрутить)
# ============================================================================

def play_dice(body, bet: Decimal, user_id: int) -> dict:
    """Кубик 1-6. Точное попадание — x6, ≥4 — x2."""
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
    return {
        "rolled": rolled,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
    }


def play_football(body, bet: Decimal, user_id: int) -> dict:
    """Пенальти: гол или мимо (50/50). Угадал — x2."""
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

    return {
        "outcome": outcome,
        "userChoice": choice,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
    }


def play_basket(body, bet: Decimal, user_id: int) -> dict:
    """Баскетбол: 3 броска, попадание — 60% шанс.
    3/3 → x4, 2/3 → x2, 1/3 → x1, иначе 0.
    """
    hits = sum(1 for _ in range(3) if random.random() < 0.6)
    table = {3: Decimal("4"), 2: Decimal("2"), 1: Decimal("1"), 0: Decimal("0")}
    coef = table[hits]
    win = (bet * coef).quantize(Decimal("0.01"))
    if win > 0:
        change_balance(user_id, win)
    return {
        "hits": hits,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
    }


def play_slots(body, bet: Decimal, user_id: int) -> dict:
    """Слоты: три барабана.
    Три одинаковых — x10, два одинаковых — x3, иначе 0.
    """
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
    return {
        "reels": reels,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
    }


# ============================================================================
# HTML-фронт (Telegram Mini App) — single-file, без шаблонов
# ============================================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <title>Royal Spin — Mini App</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #0f172a;
      --bg2: #1e293b;
      --card: #1e293b;
      --accent: #facc15;
      --accent2: #22c55e;
      --danger: #ef4444;
      --text: #f1f5f9;
      --muted: #94a3b8;
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
      background: var(--card);
      border-radius: 18px; padding: 14px;
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
    .balance-row {
      display: flex; align-items: center; gap: 6px;
      margin-top: 4px;
    }
    .balance-row .balance {
      color: var(--accent);
      font-weight: 700; font-size: 19px;
      transition: transform .25s ease, color .25s ease;
    }
    .balance-row .balance.bump {
      animation: bump .55s ease;
    }
    @keyframes bump {
      0%   { transform: scale(1);   color: var(--accent); }
      30%  { transform: scale(1.18); color: var(--accent2); }
      100% { transform: scale(1);   color: var(--accent); }
    }

    .grid {
      margin-top: 18px;
      display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
    }
    .game-card {
      background: var(--card);
      border-radius: 18px;
      padding: 22px 12px;
      text-align: center;
      font-size: 16px; font-weight: 600;
      cursor: pointer; user-select: none;
      transition: transform .12s ease, background .12s ease;
      border: 2px solid transparent;
    }
    .game-card:active { transform: scale(0.96); }
    .game-card .emoji { font-size: 38px; display: block; margin-bottom: 6px; }
    .game-card.active { border-color: var(--accent); background: #2a2540; }

    .panel {
      margin-top: 18px;
      background: var(--card);
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
      border-radius: 12px; font-size: 16px;
      outline: none;
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
      margin-top: 14px;
      padding: 14px; border-radius: 12px;
      text-align: center; font-size: 16px; font-weight: 600;
      display: none;
    }
    .result.win { background: rgba(34, 197, 94, 0.18); color: #4ade80; display: block; }
    .result.lose { background: rgba(239, 68, 68, 0.18); color: #fca5a5; display: block; }
    .result.draw { background: rgba(148, 163, 184, 0.18); color: #cbd5e1; display: block; }

    .slots {
      display: flex; justify-content: center; gap: 10px;
      margin: 14px 0;
    }
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
      25% { transform: translateY(-6px); }
      75% { transform: translateY(6px); }
    }

    .basket-ball {
      width: 80px; height: 80px; margin: 0 auto;
      border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #fb923c, #c2410c);
      display: flex; align-items: center; justify-content: center;
      font-size: 40px;
      transition: transform .4s ease;
    }
    .basket-ball.shoot { transform: translateY(-60px) scale(0.8); }
    .basket-result {
      text-align: center; font-size: 18px; font-weight: 700;
      margin: 10px 0;
    }

    .hidden { display: none; }
    .loading {
      position: fixed; inset: 0;
      background: var(--bg);
      display: flex; align-items: center; justify-content: center;
      z-index: 100; flex-direction: column; gap: 12px;
    }
    .spinner {
      width: 40px; height: 40px;
      border: 4px solid #334155;
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .err-msg {
      background: rgba(239,68,68,.15); color: #fca5a5;
      padding: 12px; border-radius: 10px; margin-top: 10px;
      font-size: 14px;
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

    <!-- Кубик -->
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

    <!-- Футбол -->
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

    <!-- Баскетбол -->
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

    <!-- Слоты -->
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
    // ---------------------------------------------------------------------
    // Telegram Mini App init
    // ---------------------------------------------------------------------
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (tg) {
      try { tg.ready(); tg.expand(); } catch (e) {}
    }

    // initData из Telegram. Если открыли не через бота (прямой URL в браузере)
    // — строка пустая, дальше покажем понятную ошибку.
    const INIT_DATA = (tg && tg.initData) ? tg.initData : "";

    const state = {
      user: null,
      balance: 0,
      activeGame: null,
      diceChoice: null,
      footChoice: "goal",
    };

    // ---------------------------------------------------------------------
    // localStorage — восстанавливаем выбор игры и ставку между загрузками
    // ---------------------------------------------------------------------
    const LS_KEY = "royal_spin_state_v1";
    function loadLs() {
      try {
        const raw = localStorage.getItem(LS_KEY);
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
            "dice-bet": document.getElementById("dice-bet")?.value || "",
            "foot-bet": document.getElementById("foot-bet")?.value || "",
            "basket-bet": document.getElementById("basket-bet")?.value || "",
            "slots-bet": document.getElementById("slots-bet")?.value || "",
          },
        }));
      } catch (e) {}
    }

    // ---------------------------------------------------------------------
    // Утилиты
    // ---------------------------------------------------------------------
    function setBet(inputId, val) {
      const el = document.getElementById(inputId);
      if (!el) return;
      if (val === "all") {
        el.value = Math.floor(state.balance);
      } else {
        el.value = val;
      }
      saveLs();
    }

    function showErr(msg) {
      const div = document.createElement("div");
      div.className = "err-msg";
      div.textContent = msg;
      document.querySelector(".app").appendChild(div);
      setTimeout(() => div.remove(), 4000);
    }

    // fetch с таймаутом — иначе при зависшем бэке будем бесконечно
    // крутить «Загружаем...».
    function fetchWithTimeout(url, opts = {}, timeoutMs = 10000) {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), timeoutMs);
      return fetch(url, { ...opts, signal: ctrl.signal })
        .finally(() => clearTimeout(t));
    }

    function showLoadingError(msg, hint) {
      document.getElementById("loading").innerHTML =
        `<div style="color:#fca5a5; padding:24px; text-align:center; max-width: 340px;">
          <div style="font-size:18px; margin-bottom:8px;">⚠️ Не удалось загрузиться</div>
          <div style="font-size:14px; opacity:.95; margin-bottom:6px;">${msg}</div>
          ${hint ? `<div style="font-size:12px; opacity:.6; margin-bottom:14px;">${hint}</div>` : ""}
          <button onclick="location.reload()" style="margin-top:8px; background:#facc15; color:#000; border:none; padding:10px 20px; border-radius:10px; font-weight:600; cursor:pointer;">
            ↻ Попробовать снова
          </button>
        </div>`;
    }

    function updateBalanceUi(prevBalance) {
      const el = document.getElementById("balance");
      el.textContent = `${Number(state.balance).toLocaleString("ru-RU")} ⭐`;
      // Анимация "пульсации" при изменении
      if (prevBalance !== undefined && prevBalance !== state.balance) {
        el.classList.remove("bump");
        // Форсируем reflow, чтобы анимация перезапустилась
        void el.offsetWidth;
        el.classList.add("bump");
      }
    }

    // ---------------------------------------------------------------------
    // Загрузка пользователя (старт)
    // ---------------------------------------------------------------------
    async function loadUser() {
      if (!INIT_DATA) {
        showLoadingError(
          "Mini App нужно открывать из Telegram-бота.",
          "Зайди в бота → нажми кнопку с игрой. Прямая ссылка в браузере не сработает."
        );
        return;
      }

      // Сначала /health — если БД лежит, сразу понятная ошибка
      try {
        const h = await fetchWithTimeout("/health", { cache: "no-store" }, 8000);
        const hj = await h.json();
        console.log("health:", hj);
        if (!hj.db_ok) {
          showLoadingError(
            "Сервер не может подключиться к базе данных.",
            hj.db_error || "Проверь, запущен ли PostgreSQL и правильный ли DATABASE_URL"
          );
          return;
        }
      } catch (e) {
        console.warn("health failed:", e);
      }

      try {
        const r = await fetchWithTimeout("/api/me", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ init_data: INIT_DATA }),
        }, 12000);
        const data = await r.json();
        if (!data.ok) {
          let hint = "";
          if (data.reason === "auth" && !INIT_DATA) {
            hint = "Запусти Mini App кнопкой из бота, а не через прямую ссылку.";
          } else if (data.reason === "auth") {
            hint = "Возможно, бот Mini App и тот, из которого открываешь — разные. BOT_TOKEN должен совпадать.";
          } else if (data.reason === "db") {
            hint = "Сервер Mini App не подключается к БД. Проверь DATABASE_URL.";
          }
          showLoadingError(data.error || "Ошибка авторизации", hint);
          return;
        }

        state.user = data.user;
        state.balance = data.balance;

        // Аватарка
        if (data.user.photo_url) {
          document.getElementById("avatar").innerHTML =
            `<img src="${data.user.photo_url}" alt="" onerror="this.parentElement.textContent='${(data.user.first_name||"?").trim()[0]||"?"}'" />`;
        } else {
          const ch = (data.user.first_name || "?").trim()[0] || "?";
          document.getElementById("avatar").textContent = ch;
        }

        const name =
          (data.user.username ? "@" + data.user.username : null) ||
          [data.user.first_name, data.user.last_name].filter(Boolean).join(" ") ||
          "Гость";
        document.getElementById("username").textContent = name;

        // Восстанавливаем сохранённое состояние UI
        const ls = loadLs();
        if (ls.bets) {
          for (const [k, v] of Object.entries(ls.bets)) {
            const el = document.getElementById(k);
            if (el && v) el.value = v;
          }
        }

        updateBalanceUi();
        document.getElementById("loading").classList.add("hidden");
        document.getElementById("app").classList.remove("hidden");

        // Если в LS была активная игра — выбираем её
        if (ls.activeGame) {
          const card = document.querySelector(`.game-card[data-game="${ls.activeGame}"]`);
          if (card) card.click();
        }
        if (ls.diceChoice) {
          const btn = document.querySelector(`#dice-numbers .num-btn:nth-child(${ls.diceChoice})`);
          if (btn) btn.click();
        }
        if (ls.footChoice) {
          const btn = document.querySelector(`#panel-football .choice-btn[data-choose="${ls.footChoice}"]`);
          if (btn) btn.click();
        }
      } catch (e) {
        console.error("loadUser error:", e);
        const isAbort = e && (e.name === "AbortError" || /aborted/i.test(e.message || ""));
        showLoadingError(
          isAbort ? "Сервер мини-аппа не отвечает (таймаут)." : "Сервер мини-аппа недоступен.",
          isAbort
            ? "Похоже, бэк зависает на подключении к БД или просто упал. Проверь, запущен ли app.py."
            : (e.message + " — Проверь, запущен ли app.py и открыт ли порт.")
        );
      }
    }

    // ---------------------------------------------------------------------
    // Выбор игры
    // ---------------------------------------------------------------------
    document.querySelectorAll(".game-card").forEach(card => {
      card.addEventListener("click", () => {
        const g = card.dataset.game;
        state.activeGame = g;
        document.querySelectorAll(".game-card").forEach(c => c.classList.remove("active"));
        card.classList.add("active");
        document.querySelectorAll(".panel").forEach(p => p.classList.add("hidden"));
        document.getElementById("panel-" + g).classList.remove("hidden");
        document.querySelectorAll(".result").forEach(r => {
          r.classList.remove("win", "lose", "draw");
          r.style.display = "none";
        });
        saveLs();
      });
    });

    // ---------------------------------------------------------------------
    // Кубик: генерим кнопки 1-6
    // ---------------------------------------------------------------------
    const diceWrap = document.getElementById("dice-numbers");
    for (let i = 1; i <= 6; i++) {
      const b = document.createElement("button");
      b.className = "num-btn";
      b.textContent = i;
      b.onclick = () => {
        document.querySelectorAll("#dice-numbers .num-btn").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        state.diceChoice = i;
        saveLs();
      };
      diceWrap.appendChild(b);
    }

    // Сохраняем ставки в LS при изменении
    ["dice-bet", "foot-bet", "basket-bet", "slots-bet"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", saveLs);
    });

    // ---------------------------------------------------------------------
    // Футбол: переключатель гол/мимо
    // ---------------------------------------------------------------------
    document.querySelectorAll("#panel-football .choice-btn").forEach(b => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#panel-football .choice-btn").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        state.footChoice = b.dataset.choose;
        saveLs();
      });
    });

    // ---------------------------------------------------------------------
    // Игра: отправка на бэк
    // ---------------------------------------------------------------------
    async function playGame(game, payload) {
      const r = await fetchWithTimeout("/api/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          init_data: INIT_DATA,
          game, ...payload,
        }),
      }, 15000);
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || "Ошибка");
      // Сразу обновим баланс из ответа сервера
      const prev = state.balance;
      state.balance = Number(data.balance);
      updateBalanceUi(prev);
      return data;
    }

    function showResult(id, type, text) {
      const el = document.getElementById(id);
      el.className = "result " + type;
      el.style.display = "block";
      el.textContent = text;
    }

    // Кубик
    document.getElementById("dice-play").addEventListener("click", async () => {
      if (state.diceChoice === null) return showErr("Выберите число");
      const bet = parseFloat(document.getElementById("dice-bet").value);
      if (!bet || bet <= 0) return showErr("Введите ставку");
      const btn = document.getElementById("dice-play");
      btn.disabled = true;
      try {
        const r = await playGame("dice", { choice: state.diceChoice, bet });
        const text = `Выпало ${r.rolled}. Ваш выбор ${state.diceChoice}. ` +
          (r.win > 0
            ? `+${r.win} ⭐ (x${r.coef})`
            : `-${r.bet} ⭐`);
        showResult("dice-result", r.win > 0 ? "win" : "lose", text);
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.win > 0 ? "success" : "error");
      } catch (e) {
        showErr(e.message);
      } finally {
        btn.disabled = false;
      }
    });

    // Футбол
    document.getElementById("foot-play").addEventListener("click", async () => {
      const bet = parseFloat(document.getElementById("foot-bet").value);
      if (!bet || bet <= 0) return showErr("Введите ставку");
      const btn = document.getElementById("foot-play");
      btn.disabled = true;
      try {
        const r = await playGame("football", { choice: state.footChoice, bet });
        const text = r.outcome === "goal"
          ? (r.win > 0 ? `⚽ ГООЛ! +${r.win} ⭐ (x${r.coef})` : `⚽ Гол, но вы ставили на «${r.userChoice}» — -${r.bet} ⭐`)
          : (r.win > 0 ? `❌ Мимо, но вы угадали! +${r.win} ⭐` : `⚽ Гол, вы ставили «мимо» — -${r.bet} ⭐`);
        showResult("foot-result", r.win > 0 ? "win" : "lose", text);
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.win > 0 ? "success" : "error");
      } catch (e) {
        showErr(e.message);
      } finally {
        btn.disabled = false;
      }
    });

    // Баскетбол
    document.getElementById("basket-play").addEventListener("click", async () => {
      const bet = parseFloat(document.getElementById("basket-bet").value);
      if (!bet || bet <= 0) return showErr("Введите ставку");
      const btn = document.getElementById("basket-play");
      btn.disabled = true;
      const ball = document.getElementById("ball");
      try {
        ball.classList.add("shoot");
        const r = await playGame("basket", { bet });
        setTimeout(() => ball.classList.remove("shoot"), 400);

        document.getElementById("basket-shots").textContent =
          `Попаданий: ${r.hits} / 3`;
        const text = r.win > 0
          ? `Попаданий: ${r.hits}/3 → +${r.win} ⭐ (x${r.coef})`
          : `Попаданий: ${r.hits}/3 → -${r.bet} ⭐`;
        showResult("basket-result", r.win > 0 ? "win" : "lose", text);
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(r.win > 0 ? "success" : "error");
      } catch (e) {
        showErr(e.message);
        ball.classList.remove("shoot");
      } finally {
        btn.disabled = false;
      }
    });

    // Слоты
    document.getElementById("slots-play").addEventListener("click", async () => {
      const bet = parseFloat(document.getElementById("slots-bet").value);
      if (!bet || bet <= 0) return showErr("Введите ставку");
      const btn = document.getElementById("slots-play");
      btn.disabled = true;
      const reels = [
        document.getElementById("reel1"),
        document.getElementById("reel2"),
        document.getElementById("reel3"),
      ];
      reels.forEach(r => r.classList.add("spin"));
      try {
        const data = await playGame("slots", { bet });
        reels.forEach((r, i) => {
          setTimeout(() => {
            r.classList.remove("spin");
            r.textContent = data.reels[i];
          }, 300 + i * 400);
        });
        setTimeout(() => {
          const text = data.win > 0
            ? `🎰 ${data.reels.join(" ")} → +${data.win} ⭐ (x${data.coef})`
            : `🎰 ${data.reels.join(" ")} → -${data.bet} ⭐`;
          showResult("slots-result", data.win > 0 ? "win" : "lose", text);
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(data.win > 0 ? "success" : "error");
        }, 300 + reels.length * 400 + 100);
      } catch (e) {
        reels.forEach(r => { r.classList.remove("spin"); });
        showErr(e.message);
      } finally {
        setTimeout(() => { btn.disabled = false; }, 1500);
      }
    });

    // ---------------------------------------------------------------------
    // Старт
    // ---------------------------------------------------------------------
    loadUser();
  </script>
</body>
</html>
"""


# ============================================================================
# Запуск
# ============================================================================

# Инициализация схемы БД при импорте модуля. В try/except — если БД пока
# недоступна, поднимемся и попробуем ещё раз на первом запросе.
try:
    ensure_schema()
    print("[startup] schema ensured", flush=True)
except Exception as e:
    print(f"[startup] WARN: ensure_schema failed: {e!r}", flush=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)