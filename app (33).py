"""
Telegram Mini App — казино-бот (единый файл на Flask)
=====================================================

Игры:
  • Кубик      — угадать число 1-6
  • Футбол     — пенальти (гол / мимо)
  • Баскетбол  — бросок в кольцо (3 попытки)
  • Слоты      — три барабана с коэффициентами

Бот-партнёр: https://github.com/femilianferuk-droid/zakaz-test.git
Баланс общий — читаем/пишем напрямую в ту же PostgreSQL БД, что и бот.
В .env нужно указать:
  BOT_TOKEN=<токен бота>           — для валидации initData из Telegram Mini App
  DATABASE_URL=postgresql://...   — та же строка, что использует бот

Запуск:
  pip install -r requirements.txt
  python app.py
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
from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
# NB: дефолты совпадают с тем, что захардкожено в bot.py репозитория
# https://github.com/femilianferuk-droid/zakaz-test.git
# Если хочешь переопределить — просто экспортируй переменные окружения.
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "7567265819:AAE21Bruo7hAtftkWlFODEYsV_lNuMFxIQg",
).strip()
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
).strip()
MIN_BET = Decimal("1")
MAX_BET = Decimal("100000")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# БД (общая с ботом)
# ---------------------------------------------------------------------------
def ensure_schema() -> None:
    """Создаёт таблицу users с теми же полями, что и бот, если её нет."""
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
            # Доп. колонки, если их нет (на случай если бот ещё не стартовал)
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


@contextmanager
def get_conn():
    """Контекстный менеджер подключения к PostgreSQL."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def get_or_create_user(user_id: int, username: str = "") -> Decimal:
    """Возвращает текущий баланс. Если записи нет — создаёт со стартовым 0."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO users (user_id, username, balance) VALUES (%s, %s, 0)",
                    (user_id, username or None),
                )
                conn.commit()
                return Decimal("0")
            # Обновим username, если поменялся
            cur.execute(
                "UPDATE users SET username = COALESCE(%s, username) WHERE user_id = %s",
                (username or None, user_id),
            )
            conn.commit()
            return Decimal(row["balance"])


def change_balance(user_id: int, delta: Decimal) -> Decimal:
    """Атомарно меняет баланс на delta. Возвращает новый баланс.

    delta > 0 — начисление, delta < 0 — списание.
    Если итоговый баланс уходит в минус — откатываем (недостаточно средств).
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT balance FROM users WHERE user_id = %s FOR UPDATE",
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO users (user_id, balance) VALUES (%s, 0)", (user_id,)
                )
                current = Decimal("0")
            else:
                current = Decimal(row["balance"])

            new_balance = current + delta
            if new_balance < 0:
                conn.rollback()
                raise ValueError("Недостаточно средств")

            cur.execute(
                "UPDATE users SET balance = %s, games_played = games_played + %s "
                "WHERE user_id = %s",
                (new_balance, 1 if delta < 0 else 0, user_id),
            )
            conn.commit()
            return new_balance


# ---------------------------------------------------------------------------
# Telegram Mini App: валидация initData
# ---------------------------------------------------------------------------
def validate_init_data(init_data: str) -> dict | None:
    """Проверяет подпись initData, как требует Telegram WebApp.
    Возвращает dict c данными пользователя или None при ошибке.
    Алгоритм: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    # Пустая строка — открыли не через Telegram (прямой URL в браузере, не из бота).
    if not init_data:
        return None

    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None

    if not BOT_TOKEN:
        return None

    data_check_string = "\n".join(
        f"{k}={parsed[k]}" for k in sorted(parsed.keys())
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
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


# ---------------------------------------------------------------------------
# HTML-фронт (Telegram Mini App)
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <title>Казино — Mini App</title>
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
    .user-info .balance {
      margin-top: 4px; color: var(--accent);
      font-weight: 700; font-size: 19px;
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
        <div class="balance" id="balance">0 ⭐</div>
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

    // Достаём initData (в Telegram) или делаем фолбэк для отладки в браузере
    const INIT_DATA = (tg && tg.initData) ? tg.initData : "";

    const state = {
      user: null,
      balance: 0,
      activeGame: null,
      diceChoice: null,
      footChoice: "goal",
    };

    function setBet(inputId, val) {
      const el = document.getElementById(inputId);
      if (val === "all") {
        el.value = Math.floor(state.balance);
      } else {
        el.value = val;
      }
    }

    function showErr(msg) {
      const div = document.createElement("div");
      div.className = "err-msg";
      div.textContent = msg;
      document.querySelector(".app").appendChild(div);
      setTimeout(() => div.remove(), 4000);
    }

    // ---------------------------------------------------------------------
    // Загрузка пользователя
    // ---------------------------------------------------------------------
    function showLoadingError(msg, hint) {
      document.getElementById("loading").innerHTML =
        `<div style="color:#fca5a5; padding:24px; text-align:center; max-width: 320px;">
          <div style="font-size:18px; margin-bottom:8px;">⚠️ Не удалось загрузиться</div>
          <div style="font-size:14px; opacity:.95; margin-bottom:6px;">${msg}</div>
          ${hint ? `<div style="font-size:12px; opacity:.6; margin-bottom:14px;">${hint}</div>` : ""}
          <button onclick="location.reload()" style="margin-top:8px; background:#facc15; color:#000; border:none; padding:10px 20px; border-radius:10px; font-weight:600; cursor:pointer;">
            ↻ Попробовать снова
          </button>
        </div>`;
    }

    async function loadUser() {
      // Быстрая проверка health до запроса /api/me — если БД лежит, не будем заставлять
      // пользователя видеть сообщение про подпись.
      try {
        const h = await fetch("/health", { cache: "no-store" });
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
        // Не блокируем — пойдём дальше, /api/me вернёт нормальную ошибку
      }

      try {
        const r = await fetch("/api/me", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ init_data: INIT_DATA }),
        });
        const data = await r.json();
        if (!data.ok) {
          console.error("auth failed:", data);
          let hint = "";
          if (data.reason === "auth" && !INIT_DATA) {
            hint = "Запусти Mini App кнопкой из бота, а не через прямую ссылку.";
          } else if (data.reason === "db") {
            hint = "Сервер мини-аппа не подключается к БД. Запусти PostgreSQL.";
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

        // Ник
        const name =
          (data.user.username ? "@" + data.user.username : null) ||
          [data.user.first_name, data.user.last_name].filter(Boolean).join(" ") ||
          "Гость";
        document.getElementById("username").textContent = name;
        updateBalance();
        document.getElementById("loading").classList.add("hidden");
        document.getElementById("app").classList.remove("hidden");
      } catch (e) {
        console.error("loadUser error:", e);
        showLoadingError(
          "Сервер мини-аппа недоступен.",
          e.message + " — Проверь, запущен ли app.py и открыт ли порт."
        );
      }
    }

    function updateBalance() {
      document.getElementById("balance").textContent =
        `${Number(state.balance).toLocaleString("ru-RU")} ⭐`;
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
      };
      diceWrap.appendChild(b);
    }

    // ---------------------------------------------------------------------
    // Футбол: переключатель гол/мимо
    // ---------------------------------------------------------------------
    document.querySelectorAll("#panel-football .choice-btn").forEach(b => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#panel-football .choice-btn").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        state.footChoice = b.dataset.choose;
      });
    });

    // ---------------------------------------------------------------------
    // Игра: отправка на бэк
    // ---------------------------------------------------------------------
    async function playGame(game, payload) {
      const r = await fetch("/api/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          init_data: INIT_DATA,
          game, ...payload,
        }),
      });
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || "Ошибка");
      state.balance = data.balance;
      updateBalance();
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
        // Анимация бросков
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
        // Анимация останавливается по очереди
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

    // Стартуем
    loadUser();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Маршруты Flask
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string_safe(INDEX_HTML)


def render_template_string_safe(html: str):
    """Возвращает HTML как есть. Flask render_template_string с {% %} бы съел фигурные
    скобки из JS-кода, поэтому используем прямой Response-подход."""
    from flask import Response

    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/me", methods=["POST"])
def api_me():
    """Авторизация: валидируем initData и возвращаем профиль + баланс."""
    body = request.get_json(silent=True) or {}
    init_data = body.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data or "user" not in user_data:
        # Разделяем причины для диагностики
        if not init_data:
            msg = "Открой приложение через Telegram-бота (прямой URL в браузере не сработает)"
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
        balance = get_or_create_user(user_id, username)
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


@app.route("/api/play", methods=["POST"])
def api_play():
    """Главный эндпоинт для ставок. Один и тот же сервер для всех игр."""
    body = request.get_json(silent=True) or {}
    init_data = body.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data or "user" not in user_data:
        if not init_data:
            msg = "Открой приложение через Telegram-бота"
        elif not BOT_TOKEN:
            msg = "На сервере не задан BOT_TOKEN"
        else:
            msg = "Не авторизован (подпись initData невалидна)"
        return jsonify({"ok": False, "error": msg}), 401

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
        new_balance = change_balance(user_id, bet)
        return jsonify({"ok": False, "error": f"Ошибка: {e}", "balance": float(new_balance)}), 500

    result["ok"] = True
    return jsonify(result)


# ---------------------------------------------------------------------------
# Игровая логика (на сервере — чтобы клиент не мог накрутить)
# ---------------------------------------------------------------------------
def play_dice(body, bet, user_id):
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
        new_balance = change_balance(user_id, win)
    else:
        new_balance = change_balance(user_id, Decimal("0"))  # фиксируем баланс

    return {
        "rolled": rolled,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
        "balance": float(new_balance),
    }


def play_football(body, bet, user_id):
    """Пенальти: гол или мимо (50/50). Угадал — x2."""
    choice = body.get("choice", "goal")
    if choice not in ("goal", "miss"):
        raise ValueError("Неверный выбор")

    outcome = random.choice(("goal", "miss"))
    if outcome == choice:
        coef = Decimal("2")
        win = (bet * coef).quantize(Decimal("0.01"))
        new_balance = change_balance(user_id, win)
    else:
        coef = Decimal("0")
        win = Decimal("0")
        new_balance = change_balance(user_id, Decimal("0"))

    return {
        "outcome": outcome,
        "userChoice": choice,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
        "balance": float(new_balance),
    }


def play_basket(body, bet, user_id):
    """Баскетбол: 3 броска. Попадание — 60% шанс.
    3/3 → x4, 2/3 → x2, 1/3 → x1, иначе 0.
    """
    hits = sum(1 for _ in range(3) if random.random() < 0.6)
    table = {3: Decimal("4"), 2: Decimal("2"), 1: Decimal("1"), 0: Decimal("0")}
    coef = table[hits]
    win = (bet * coef).quantize(Decimal("0.01"))
    if win > 0:
        new_balance = change_balance(user_id, win)
    else:
        new_balance = change_balance(user_id, Decimal("0"))

    return {
        "hits": hits,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
        "balance": float(new_balance),
    }


def play_slots(body, bet, user_id):
    """Слоты: три барабана. Три одинаковых — x10, два одинаковых — x3, иначе 0."""
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
        new_balance = change_balance(user_id, win)
    else:
        new_balance = change_balance(user_id, Decimal("0"))

    return {
        "reels": reels,
        "win": float(win),
        "coef": float(coef),
        "bet": float(bet),
        "balance": float(new_balance),
    }


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ensure_schema()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)