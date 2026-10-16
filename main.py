import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse, parse_qs

import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo

# ====================== НАСТРОЙКИ ======================
# Токен берётся из переменной окружения BOT_TOKEN.
# НЕ вписывай его в код: файл лежит на GitHub.
TOKEN = os.environ.get("BOT_TOKEN", "")
BASE_WEB_APP_URL = (os.environ.get("BASE_WEB_APP_URL") or "").rstrip("/")
ALLOWED_ORIGIN = (os.environ.get("ALLOWED_ORIGIN") or "*").rstrip("/")
OWNER_ID = 778573280
PORT = int(os.environ.get("PORT", 10000))
DB_PATH = os.environ.get("DB_PATH", "leaderboard.db")

MAX_BODY = 300 * 1024         # максимум размера запроса
MAX_INV_ITEMS = 200           # сколько скинов хранить в профиле
MAX_BALANCE = 10 ** 12        # защита от мусорных чисел
AUTH_MAX_AGE = 3 * 24 * 3600  # сколько живёт подпись Telegram
ONLINE_WINDOW = 5 * 60        # «онлайн» = был активен за 5 минут
TOP_N = 50
MAX_SSE = 300                 # максимум одновременных live-подключений
# =======================================================

if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)

# ---------------------- БАЗА ДАННЫХ ----------------------
db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS players (
        uid INTEGER PRIMARY KEY,
        name TEXT, username TEXT, photo TEXT,
        balance INTEGER DEFAULT 0,
        cases INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1,
        seen INTEGER DEFAULT 0,
        profile TEXT,
        sync_client TEXT DEFAULT '',
        sync_seq INTEGER DEFAULT 0
    )"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, kind TEXT,
        data TEXT, created INTEGER, done INTEGER DEFAULT 0)"""
)
db.execute("CREATE TABLE IF NOT EXISTS skinimg (k TEXT PRIMARY KEY, data BLOB, mime TEXT, h TEXT)")
db.commit()
# ====================== ПАТЧ v6: ТУРНИРЫ / КЛАН-ВОЙНА / БАН ======================
db.execute("CREATE TABLE IF NOT EXISTS tour (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b TEXT, odds_a REAL, odds_b REAL, starts INTEGER, status TEXT DEFAULT 'open', winner INTEGER DEFAULT -1, seed TEXT, ts INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS tour_bets (uid INTEGER, match_id INTEGER, side INTEGER, amount INTEGER, odds REAL, settled INTEGER DEFAULT 0, PRIMARY KEY(uid, match_id))")
db.execute("CREATE TABLE IF NOT EXISTS war (club_id INTEGER PRIMARY KEY, cases INTEGER DEFAULT 0, week TEXT)")
db.commit()
try:
    db.execute("ALTER TABLE players ADD COLUMN banned INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass
for _column, _definition in (("ban_until", "INTEGER DEFAULT 0"), ("ban_reason", "TEXT DEFAULT ''"), ("ban_starts_at", "INTEGER DEFAULT 0")):
    try:
        db.execute("ALTER TABLE players ADD COLUMN %s %s" % (_column, _definition))
        db.commit()
    except sqlite3.OperationalError:
        pass

TOUR_INTERVAL = 300      # новый матч каждые 5 минут
TOUR_ODDS = 1.9          # выплата за угадавшего победителя
TOUR_MAX_BET = 100000
WAR_PRIZE_1 = 10000      # каждому участнику клуба-победителя
WAR_PRIZE_2 = 4000       # каждому участнику клуба со 2-го места


def _week_key():
    import datetime
    d = datetime.date.today()
    return "%d-W%02d" % (d.isocalendar().year, d.isocalendar().week)


def ensure_tour():
    """Закрывает завершившиеся матчи, выплачивает выигрыши через grants, создаёт новые матчи."""
    now = int(time.time())
    with db_lock:
        rows = db.execute(
            "SELECT id, seed, starts FROM tour WHERE status='open' AND starts <= ?",
            (now - 90,),
        ).fetchall()
        for r in rows:
            # победитель определяется из секретного seed (50/50) только на сервере
            win = int(hashlib.sha256((r[1] + "w").encode()).hexdigest(), 16) % 2
            db.execute(
                "UPDATE tour SET status='done', winner=? WHERE id=?", (win, r[0])
            )
            bets = db.execute(
                "SELECT uid, amount, odds FROM tour_bets WHERE match_id=? AND side=? AND settled=0",
                (r[0], win),
            ).fetchall()
            db.executemany(
                "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                [(b[0], "bal", json.dumps({"t": "bal", "v": int(b[1] * b[2]), "note": "Турнир"}, ensure_ascii=False), now) for b in bets],
            )
            db.execute("UPDATE tour_bets SET settled=1 WHERE match_id=?", (r[0],))
        cnt = db.execute(
            "SELECT COUNT(*) FROM tour WHERE status='open' AND starts > ?", (now,)
        ).fetchone()[0]
        last = db.execute("SELECT MAX(starts) FROM tour").fetchone()[0]
        if not last:
            last = now - now % TOUR_INTERVAL
        names = ["Vortex", "Blaze", "Ghost", "Pixel", "Storm", "Rex", "Nova", "Drift", "Ace", "Fang", "Zero", "Onyx"]
        import random as _rnd
        while cnt < 3:
            last += TOUR_INTERVAL
            a, b = _rnd.sample(names, 2)
            db.execute(
                "INSERT INTO tour (a,b,odds_a,odds_b,starts,status,seed,ts) VALUES (?,?,?,?,?,'open',?,?)",
                (a, b, TOUR_ODDS, TOUR_ODDS, last, secrets.token_hex(8), now),
            )
            cnt += 1
        db.commit()


def get_tour():
    ensure_tour()
    now = int(time.time())
    with db_lock:
        rows = db.execute(
            "SELECT id,a,b,odds_a,odds_b,starts,status,winner FROM tour ORDER BY starts DESC LIMIT 12"
        ).fetchall()
    return {
        "now": now,
        "matches": [
            {"id": r[0], "a": r[1], "b": r[2], "oa": r[3], "ob": r[4],
             "starts": r[5], "status": r[6], "winner": r[7]}
            for r in rows
        ],
    }


def _club_of(uid):
    with club_lock:
        rows = db.execute("SELECT id, members FROM clubs").fetchall()
    for r in rows:
        if uid in json.loads(r[1] or "[]"):
            return r[0]
    return None


def add_war_cases(uid, delta):
    if delta <= 0:
        return
    cid = _club_of(uid)
    if not cid:
        return
    with club_lock:
        db.execute(
            "INSERT INTO war (club_id, cases, week) VALUES (?,?,?) "
            "ON CONFLICT(club_id) DO UPDATE SET cases=cases+excluded.cases",
            (cid, delta, _week_key()),
        )
        db.commit()


def get_war():
    import datetime
    wk = _week_key()
    d = datetime.date.today()
    end = int(time.mktime((d + datetime.timedelta(days=7 - d.isocalendar().weekday)).timetuple()))
    now = int(time.time())
    with club_lock:
        # награды за прошлую неделю (лениво, при смене недели)
        aw = db.execute("SELECT v FROM meta WHERE k='war_awarded'").fetchone()
        if aw and aw[0] != wk:
            old = db.execute(
                "SELECT club_id, cases FROM war WHERE week=? ORDER BY cases DESC LIMIT 2",
                (aw[0],),
            ).fetchall()
            for place, (cid, sc) in enumerate(old):
                if sc <= 0:
                    continue
                r = db.execute("SELECT members FROM clubs WHERE id=?", (cid,)).fetchone()
                if not r:
                    continue
                prize = WAR_PRIZE_1 if place == 0 else WAR_PRIZE_2
                members = json.loads(r[0] or "[]")[:CLUB_MAX]
                db.executemany(
                    "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                    [(m, "bal", json.dumps({"t": "bal", "v": prize, "note": "Клан-война"}, ensure_ascii=False), now) for m in members],
                )
            db.execute(
                "INSERT INTO meta (k,v) VALUES ('war_awarded',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (wk,),
            )
            db.execute("DELETE FROM war WHERE week != ?", (wk,))
            db.commit()
        rows = db.execute(
            "SELECT club_id, cases FROM war WHERE week=? ORDER BY cases DESC LIMIT 2", (wk,)
        ).fetchall()
    out = []
    for r in rows:
        c = db.execute("SELECT name,tag,emblem,members FROM clubs WHERE id=?", (r[0],)).fetchone()
        if c:
            out.append({"id": r[0], "name": c[0], "tag": c[1], "emblem": c[2],
                        "cases": r[3], "members": len(json.loads(c[4] or "[]"))})
    return {"week": wk, "end": end, "prize1": WAR_PRIZE_1, "prize2": WAR_PRIZE_2, "clubs": out}
# ====================== КОНЕЦ ПАТЧА v6 ======================

try:
    db.execute("ALTER TABLE players ADD COLUMN susp INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass
# ---------- ОДНОРАЗОВЫЙ ПОЛНЫЙ СБРОС ВСЕХ ИГРОКОВ ----------
# Чтобы сделать новый сброс в будущем, поменяй RESET_VERSION (например, "v6").
RESET_VERSION = "v5"
db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
if not db.execute("SELECT 1 FROM meta WHERE k=?", ("reset_" + RESET_VERSION,)).fetchone():
    db.execute(
        "UPDATE players SET balance=10000, cases=0, level=1, profile=NULL, susp=0"
    )
    db.execute("DELETE FROM grants")
    db.execute("INSERT INTO meta (k,v) VALUES (?,?)", ("reset_" + RESET_VERSION, str(int(time.time()))))
    db.commit()
    print("Сброс всех игроков выполнен:", RESET_VERSION)
for _col in ("strikes", "bonus", "nwbase", "nwts", "state_rev", "last_balance"):
    try:
        db.execute(f"ALTER TABLE players ADD COLUMN {_col} INTEGER DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass
for _col, _type in (("sync_client", "TEXT DEFAULT ''"), ("sync_seq", "INTEGER DEFAULT 0")):
    try:
        db.execute(f"ALTER TABLE players ADD COLUMN {_col} {_type}")
        db.commit()
    except sqlite3.OperationalError:
        pass
db.execute("UPDATE players SET last_balance=balance WHERE last_balance=0 AND balance>0")
db.commit()
START_BAL = 10_000
ADMIN_CMDS = {"bal", "set", "item", "case", "clear", "warn", "maintenance", "ban", "unban"}

# ---------------------- БАТТЛЫ (комнаты живут только в памяти) ----------------------
battle_lock = threading.Lock()
battle_wait = {}
battle_rooms = {}

# ---------------------- ЧАТ / ТРЕЙДЫ / КЛУБЫ ----------------------
db.execute("CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT, text TEXT, ts INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS player_activity (uid INTEGER PRIMARY KEY, name TEXT, game TEXT DEFAULT '', updated INTEGER DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS player_wins (id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT, game TEXT, amount INTEGER, ts INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, from_uid INTEGER, to_uid INTEGER, give TEXT, want TEXT, status TEXT DEFAULT 'pending', ts INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS clubs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT COLLATE NOCASE UNIQUE, tag TEXT, emblem TEXT, owner INTEGER, members TEXT, ts INTEGER)")
db.commit()
chat_lock = threading.Lock()
trade_lock = threading.Lock()
club_lock = threading.Lock()
battle_invites = {}   # uid -> [ {room, from{id,name}, case, rounds, fp, t} ]
last_chat = {}        # uid -> ts (антиспам)
CHAT_KEEP = 200
CHAT_THROТTLE = 1.5
CHAT_THROTTLE = 1.5
CLUB_COST = 10_000
CLUB_MAX = 20
CLUB_EMBLEMS = ["🛡","⚔","🐉","🦅","🔥","💀","👑","🌊","⚡","🐺","🎯","🚀","🍀","💎","🐍","🦂","🌹","🪐","🎩","🐻","🦈","🌵","⭐","🎭"]
chat_version = 0
last_chat_msg = None

# ---------------------- LIVE (SSE) -----------------------
clients = set()
clients_lock = threading.Lock()
board_version = 0
version_cond = threading.Condition()


def bump_version():
    global board_version
    with version_cond:
        board_version += 1
        version_cond.notify_all()


def get_board():
    """Топ по деньгам и по кейсам + число игроков/онлайн."""
    now = int(time.time())
    with db_lock:
        total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        online = db.execute(
            "SELECT COUNT(*) FROM players WHERE seen > ?", (now - ONLINE_WINDOW,)
        ).fetchone()[0]
        online_rows = db.execute(
            "SELECT name,username,photo,seen FROM players WHERE seen > ? ORDER BY seen DESC LIMIT 40",
            (now - ONLINE_WINDOW,),
        ).fetchall()

        def top(col):
            if col == "balance":
                rows = db.execute(
                    "SELECT uid,name,username,photo,balance,cases,level,seen,profile,last_balance "
                    "FROM players WHERE (balance>0 OR last_balance>0) AND susp=0 "
                    "ORDER BY CASE WHEN seen>? THEN balance ELSE COALESCE(NULLIF(last_balance,0),balance) END DESC, uid ASC LIMIT ?",
                    (now - ONLINE_WINDOW, TOP_N),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT uid,name,username,photo,balance,cases,level,seen,profile,0 "
                    f"FROM players WHERE {col} > 0 AND susp = 0 ORDER BY {col} DESC, uid ASC LIMIT ?",
                    (TOP_N,),
                ).fetchall()
            out = []
            for r in rows:
                try:
                    p = json.loads(r[8]) if r[8] else {}
                except Exception:
                    p = {}
                out.append(
                    {
                        "id": r[0],
                        "name": r[1],
                        "username": r[2],
                        "photo": r[3],
                        "balance": r[4],
                        "last_balance": r[9] or r[4],
                        "cases": r[5],
                        "level": r[6],
                        "online": r[7] > now - ONLINE_WINDOW,
                        "title": p.get("title", ""),
                        "frame": p.get("frame", ""),
                    }
                )
            return out

        data = {
            "money": top("balance"),
            "cases": top("cases"),
            "players_online": [
                {"name": r[0] or "Игрок", "username": r[1] or "", "photo": r[2] or "", "seen": r[3]}
                for r in online_rows
            ],
        }
    data["total"] = total
    data["online"] = online
    data["ts"] = now
    return data


def get_maintenance():
    now = int(time.time())
    with db_lock:
        row = db.execute("SELECT v FROM meta WHERE k='maintenance'").fetchone()
    try:
        starts_at = int(json.loads(row[0]).get("startsAt", 0)) if row else 0
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        starts_at = 0
    return {"startsAt": starts_at, "scheduled": starts_at > now, "active": starts_at > 0 and starts_at <= now, "serverTime": now}


def get_rank(uid):
    with db_lock:
        row = db.execute(
            "SELECT balance,cases FROM players WHERE uid=?", (uid,)
        ).fetchone()
        if not row:
            return None
        money_rank = (
            db.execute(
                "SELECT COUNT(*) FROM players WHERE balance > ? AND susp = 0", (row[0],)
            ).fetchone()[0]
            + 1
        )
        cases_rank = (
            db.execute(
                "SELECT COUNT(*) FROM players WHERE cases > ?", (row[1],)
            ).fetchone()[0]
            + 1
        )
    return {"money": money_rank, "cases": cases_rank}


# --------------- ПРОВЕРКА ПОДПИСИ TELEGRAM ---------------
def verify_init_data(init_data):
    """Возвращает dict пользователя Telegram или None, если подпись неверна."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = pairs.pop("hash", None)
        if not got_hash:
            return None
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > AUTH_MAX_AGE:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


# --------------- ОЧИСТКА ДАННЫХ ОТ КЛИЕНТА ---------------
def to_int(v, lo=0, hi=MAX_BALANCE):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return lo


def to_str(v, n):
    return str(v or "")[:n]


def clean_profile(p):
    inv = []
    for it in (p.get("inv") or [])[:MAX_INV_ITEMS]:
        try:
            inv.append(
                [to_str(it[0], 40), to_str(it[1], 60), to_str(it[2], 12), to_int(it[3], 0, 10 ** 9)]
            )
        except Exception:
            continue
    favorite_cases = []
    raw_favorite_cases = p.get("favoriteCases")
    if not isinstance(raw_favorite_cases, list):
        raw_favorite_cases = []
    for case_id in raw_favorite_cases[:10]:
        case_id = to_str(case_id, 48)
        if case_id and case_id not in favorite_cases:
            favorite_cases.append(case_id)
    return {
        "title": to_str(p.get("title"), 24),
        "frame": to_str(p.get("frame"), 4),
        "elo": to_int(p.get("elo"), 0, 10 ** 9),
        "xp": to_int(p.get("xp"), 0, 10 ** 9),
        "games": to_int(p.get("games"), 0, 10 ** 9),
        "upW": to_int(p.get("upW"), 0, 10 ** 9),
        "upL": to_int(p.get("upL"), 0, 10 ** 9),
        "maxBal": to_int(p.get("maxBal")),
        "maxItem": to_int(p.get("maxItem"), 0, 10 ** 9),
        "ach": [bool(x) for x in (p.get("ach") or [])[:30]],
        "streak": to_int(p.get("streak"), 0, 10 ** 5),
        "invCount": to_int(p.get("invCount"), 0, 10 ** 7),
        "invValue": to_int(p.get("invValue")),
        "inv": inv,
        "favoriteCases": favorite_cases,
        "clickPower": to_int(p.get("clickPower"), 1, 100),
    }


def clean_items(items):
    out = []
    for it in (items or [])[:10]:
        try:
            out.append([to_str(it[0], 40), to_str(it[1], 60), to_str(it[2], 12), to_int(it[3], 0, 10 ** 9)])
        except Exception:
            continue
    return out


# ---------------------- HTTP-СЕРВЕР ----------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Vary", "Origin")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            try:
                with open(page, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"error": "casino page is missing"}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/api/stream":
            return self._stream()
        if url.path == "/api/skinimgs":
            with db_lock:
                rows = db.execute("SELECT k,h FROM skinimg").fetchall()
            return self._json({"map": {k: h for k, h in rows}})
        if url.path == "/api/skinimg":
            q = parse_qs(url.query)
            with db_lock:
                r = db.execute(
                    "SELECT data,mime FROM skinimg WHERE k=?", (q.get("k", [""])[0],)
                ).fetchone()
            if not r:
                return self._json({"error": "not found"}, 404)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", r[1])
            self.send_header("Content-Length", str(len(r[0])))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(r[0])
            return
        if url.path == "/api/chat/history":
            with chat_lock:
                rows = db.execute("SELECT id,uid,name,text,ts FROM chat ORDER BY id DESC LIMIT 40").fetchall()
            rows.reverse()
            return self._json({"messages": [{"id": r[0], "uid": r[1], "name": r[2], "text": r[3], "ts": r[4]} for r in rows]})
        if url.path == "/api/activity":
            now = int(time.time())
            with db_lock:
                active = db.execute("SELECT uid,name,game,updated FROM player_activity WHERE updated>? AND game<>'' ORDER BY updated DESC LIMIT 30", (now - ONLINE_WINDOW,)).fetchall()
                wins = db.execute("SELECT uid,name,game,amount,ts FROM player_wins WHERE ts>? ORDER BY ts DESC,id DESC LIMIT 25", (now - 24 * 3600,)).fetchall()
            return self._json({"players": [{"id": r[0], "name": r[1] or "Игрок", "game": r[2], "updated": r[3]} for r in active],
                               "wins": [{"id": r[0], "name": r[1] or "Игрок", "game": r[2], "amount": r[3], "ts": r[4]} for r in wins]})
        if url.path == "/api/board":
            return self._json(get_board())
        if url.path == "/api/maintenance":
            return self._json({"ok": True, **get_maintenance()})
        if url.path == "/api/tour":
            return self._json(get_tour())
        if url.path == "/api/war":
            return self._json(get_war())
        if url.path == "/api/player":
            q = parse_qs(url.query)
            try:
                uid = int(q.get("id", ["0"])[0])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            return self._player(uid)
        if url.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        self.send_error(404, "Not found")

    def _player(self, uid):
        now = int(time.time())
        with db_lock:
            r = db.execute(
                "SELECT uid,name,username,photo,balance,cases,level,seen,profile,last_balance "
                "FROM players WHERE uid=?",
                (uid,),
            ).fetchone()
        if not r:
            return self._json({"error": "not found"}, 404)
        try:
            p = json.loads(r[8]) if r[8] else {}
        except Exception:
            p = {}
        rank = get_rank(uid) or {}
        self._json(
            {
                "id": r[0],
                "name": r[1],
                "username": r[2],
                "photo": r[3],
                "balance": r[4],
                "last_balance": r[9] or r[4],
                "cases": r[5],
                "level": r[6],
                "online": r[7] > now - ONLINE_WINDOW,
                "seen": r[7],
                "rank": rank,
                "profile": p,
            }
        )

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/sync", "/api/admin", "/api/grants", "/api/grants/ack", "/api/skinimg", "/api/ban/status",
                        "/api/battle/join", "/api/battle/poll", "/api/battle/cancel",
                        "/api/battle/online", "/api/battle/invite", "/api/battle/invites",
                        "/api/battle/accept", "/api/battle/status", "/api/battle/decline",
                        "/api/chat/send", "/api/activity",
                        "/api/tour/bet",
                        "/api/trades/create", "/api/trades/list", "/api/trades/accept", "/api/trades/decline",
                        "/api/clubs/create", "/api/clubs/join", "/api/clubs/leave", "/api/clubs/list", "/api/clubs/mine"):
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._json({"error": "bad size"}, 413)
        try:
            data = json.loads(self.rfile.read(n))
        except Exception:
            return self._json({"error": "bad json"}, 400)

        user = verify_init_data(str(data.get("initData", "")))
        if not user:
            return self._json({"error": "auth"}, 401)
        uid = int(user["id"])
        if path == "/api/ban/status":
            if uid == OWNER_ID:
                return self._json({"ok": True, "banned": False})
            now = int(time.time())
            with db_lock:
                row = db.execute("SELECT banned,ban_until,ban_reason,ban_starts_at FROM players WHERE uid=?", (uid,)).fetchone()
                if row and row[0] and row[1] and int(row[1]) <= now:
                    db.execute("UPDATE players SET banned=0,ban_until=0,ban_reason='',ban_starts_at=0 WHERE uid=?", (uid,))
                    db.commit()
                    row = (0, 0, "", 0)
            active = bool(row and row[0] and int(row[3] or 0) <= now)
            return self._json({"ok": True, "banned": active,
                               "pending": bool(row and row[0] and int(row[3] or 0) > now),
                               "startsAt": int(row[3] or 0) if row and row[0] else 0,
                               "reason": (row[2] or "") if active else "",
                               "until": int(row[1] or 0) if active else 0})
        with db_lock:
            _bn = db.execute("SELECT banned,ban_until,ban_reason,ban_starts_at FROM players WHERE uid=?", (uid,)).fetchone()
            if uid != OWNER_ID and _bn and _bn[0] and _bn[1] and int(_bn[1]) <= int(time.time()):
                db.execute("UPDATE players SET banned=0,ban_until=0,ban_reason='',ban_starts_at=0 WHERE uid=?", (uid,))
                db.commit()
                _bn = (0, 0, "", 0)
        if uid != OWNER_ID and _bn and _bn[0] and int(_bn[3] or 0) <= int(time.time()):
            return self._json({"error": "banned", "reason": _bn[2] or "", "until": int(_bn[1] or 0)}, 403)
        if path == "/api/skinimg":
            if int(user["id"]) != OWNER_ID:
                return self._json({"error": "forbidden"}, 403)
            return self._skinimg(data)
        if path.startswith("/api/battle/"):
            return self._battle(path, user, data)
        if path == "/api/admin":
            if int(user["id"]) != OWNER_ID:
                return self._json({"error": "forbidden"}, 403)
            return self._admin(data)
        if path in ("/api/grants", "/api/grants/ack"):
            return self._grants(int(user["id"]), path, data)
        if path == "/api/activity":
            return self._activity(user, data)
        if path == "/api/chat/send":
            return self._chat_send(user, data)
        if path.startswith("/api/trades/"):
            return self._trades(int(user["id"]), path, data)
        if path.startswith("/api/clubs/"):
            return self._clubs(int(user["id"]), path, data)

        if path == "/api/tour/bet":
            mid = to_int(data.get("match_id"), 0, 10 ** 9)
            side = 1 if data.get("side") == 1 else 0
            amount = to_int(data.get("amount"), 0, TOUR_MAX_BET)
            if amount < 10:
                return self._json({"error": "минимальная ставка 10"}, 400)
            now = int(time.time())
            with db_lock:
                m = db.execute(
                    "SELECT id, starts, status, odds_a, odds_b FROM tour WHERE id=?", (mid,)
                ).fetchone()
                if not m or m[2] != "open" or now >= m[1]:
                    return self._json({"error": "ставки на этот матч закрыты"}, 400)
                try:
                    db.execute(
                        "INSERT INTO tour_bets (uid,match_id,side,amount,odds) VALUES (?,?,?,?,?)",
                        (int(user["id"]), mid, side, amount, m[3] if side == 0 else m[4]),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    return self._json({"error": "у тебя уже есть ставка на этот матч"}, 400)
            return self._json({"ok": True})

        uid = int(user["id"])
        # При открытии мини-приложения сначала читаем авторитетное состояние.
        # Не принимаем локальный снимок браузера до завершения этой загрузки:
        # localStorage может быть пустым или устаревшим на новом устройстве.
        if path == "/api/sync" and data.get("mode") == "load":
            with db_lock:
                saved = db.execute(
                    "SELECT balance,cases,level,profile,state_rev FROM players WHERE uid=?", (uid,)
                ).fetchone()
            if saved:
                try:
                    saved_profile = json.loads(saved[3]) if saved[3] else {}
                except Exception:
                    saved_profile = {}
                return self._json({
                    "ok": True, "exists": True, "balance": saved[0], "cases": saved[1],
                    "level": saved[2], "profile": saved_profile, "state_rev": saved[4] or 0,
                })
            return self._json({
                "ok": True, "exists": False, "balance": START_BAL, "cases": 0,
                "level": 1, "profile": {}, "state_rev": 0,
            })
        name = to_str(user.get("first_name") or user.get("username") or "Игрок", 40)
        username = to_str(user.get("username"), 40)
        photo = to_str(user.get("photo_url"), 300)
        if not photo.startswith("https://"):
            photo = ""
        balance = to_int(data.get("balance"), -MAX_BALANCE, MAX_BALANCE)
        cases = to_int(data.get("cases"), 0, 10 ** 9)
        level = to_int(data.get("level"), 1, 10 ** 4)
        pdict = clean_profile(data.get("profile") or {})
        prof = json.dumps(pdict, ensure_ascii=False)
        now = int(time.time())

        with db_lock:
            row = db.execute(
                "SELECT balance,profile,bonus,state_rev,sync_client,sync_seq FROM players WHERE uid=?",
                (uid,),
            ).fetchone()
            client_id = to_str(data.get("clientId"), 80)
            client_seq = to_int(data.get("clientSeq"), 0, 2 ** 63 - 1)
            if row:
                # Защита от двух открытых WebView: устаревший снимок не должен
                # затирать состояние другой сессии. Более новый снимок того же
                # клиента разрешён: например, финальный sendBeacon при закрытии
                # страницы мог обогнать запрос, начатый до последней ставки.
                base_rev = to_int(data.get("baseRev"), 0, 2 ** 63 - 1)
                same_client_newer = bool(client_id and client_id == (row[4] or "") and client_seq > (row[5] or 0))
                if "baseRev" in data and base_rev != (row[3] or 0) and not same_client_newer:
                    try:
                        saved_profile = json.loads(row[1]) if row[1] else {}
                    except Exception:
                        saved_profile = {}
                    return self._json({
                        "ok": False, "revert": True, "balance": row[0],
                        "inv": saved_profile.get("inv", []),
                        "favoriteCases": saved_profile.get("favoriteCases", []),
                        "state_rev": row[3] or 0,
                    })
                bonus = row[2] or 0
                if not client_id:
                    client_id, client_seq = row[4] or "", row[5] or 0
            else:
                bonus = 0
            # очки клан-войны: сколько кейсов открыл игрок с прошлой синхронизации
            try:
                _oc = db.execute("SELECT cases FROM players WHERE uid=?", (uid,)).fetchone()
                add_war_cases(uid, cases - (_oc[0] if _oc else cases))
            except Exception:
                pass
            db.execute(
                """INSERT INTO players (uid,name,username,photo,balance,cases,level,seen,profile,sync_client,sync_seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                     name=excluded.name, username=excluded.username, photo=excluded.photo,
                     balance=excluded.balance, cases=excluded.cases, level=excluded.level,
                     seen=excluded.seen, profile=excluded.profile,
                     sync_client=excluded.sync_client, sync_seq=excluded.sync_seq,
                     state_rev=players.state_rev+1""",
                (uid, name, username, photo, balance, cases, level, now, prof, client_id, client_seq),
            )
            db.execute(
                "UPDATE players SET bonus=?, strikes=0, susp=0, "
                "last_balance=CASE WHEN balance>0 THEN balance ELSE last_balance END WHERE uid=?",
                (bonus, uid)
            )
            db.commit()
        bump_version()
        self._json({"ok": True, "rank": get_rank(uid), "state_rev": (row[3] or 0) + 1 if row else 0})

    def _skinimg(self, data):
        k = to_str(data.get("k"), 120)
        if not k:
            return self._json({"error": "bad key"}, 400)
        if data.get("del"):
            with db_lock:
                db.execute("DELETE FROM skinimg WHERE k=?", (k,))
                db.commit()
            return self._json({"ok": True, "h": ""})
        m = re.match(r"^data:(image/(?:webp|png|jpeg));base64,([A-Za-z0-9+/=]+)$", str(data.get("data", "")))
        if not m:
            return self._json({"error": "bad image"}, 400)
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return self._json({"error": "bad image"}, 400)
        if len(raw) > 200_000:
            return self._json({"error": "too big"}, 413)
        h = hashlib.md5(raw).hexdigest()[:10]
        with db_lock:
            db.execute(
                "INSERT INTO skinimg (k,data,mime,h) VALUES (?,?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET data=excluded.data, mime=excluded.mime, h=excluded.h",
                (k, raw, m.group(1), h),
            )
            db.commit()
        return self._json({"ok": True, "h": h})

    def _admin(self, data):
        cmd = data.get("cmd") or {}
        t = cmd.get("t")
        if t not in ADMIN_CMDS:
            return self._json({"error": "bad cmd"}, 400)
        if t in ("ban", "unban"):
            target = to_int(cmd.get("to"), 0, 10 ** 15)
            if not target:
                return self._json({"error": "укажи Telegram ID игрока"}, 400)
            if target == OWNER_ID:
                return self._json({"error": "нельзя заблокировать владельца"}, 400)
            now = int(time.time())
            with db_lock:
                if t == "ban":
                    seconds = to_int(cmd.get("seconds"), 10, 86400)
                    reason = to_str(cmd.get("reason"), 500).strip()
                    if not reason:
                        return self._json({"error": "укажи причину бана"}, 400)
                    starts_at = now + 3
                    until = starts_at + seconds
                    db.execute(
                        "INSERT INTO players (uid,banned,ban_until,ban_reason,ban_starts_at) VALUES (?,1,?,?,?) "
                        "ON CONFLICT(uid) DO UPDATE SET banned=1,ban_until=excluded.ban_until,ban_reason=excluded.ban_reason,ban_starts_at=excluded.ban_starts_at",
                        (target, until, reason, starts_at),
                    )
                    notice = f"🔨 Бан для игрока {target} начнётся через 3 секунды на срок {seconds} сек. Причина: {reason}"
                    db.execute(
                        "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                        (OWNER_ID, "notice", json.dumps({"t": "notice", "message": notice}, ensure_ascii=False), now),
                    )
                else:
                    db.execute("UPDATE players SET banned=0,ban_until=0,ban_reason='',ban_starts_at=0 WHERE uid=?", (target,))
                    until = 0
                db.commit()
            bump_version()
            return self._json({"ok": True, "target": target, "until": until})
        if t == "maintenance":
            mode = to_str(cmd.get("mode"), 10)
            if mode not in ("start", "stop"):
                return self._json({"error": "bad maintenance mode"}, 400)
            starts_at = int(time.time()) + 10 if mode == "start" else 0
            with db_lock:
                db.execute(
                    "INSERT INTO meta (k,v) VALUES ('maintenance',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (json.dumps({"startsAt": starts_at}),),
                )
                db.commit()
            bump_version()
            return self._json({"ok": True, **get_maintenance()})
        to = cmd.pop("to", None)
        clean = {"t": t}
        if t == "warn":
            target = to_int(to, 0, 10 ** 15)
            message = to_str(cmd.get("message"), 500).strip()
            if not target or to == "all":
                return self._json({"error": "укажи Telegram ID одного игрока"}, 400)
            if not message:
                return self._json({"error": "впиши текст предупреждения"}, 400)
            clean["message"] = message
            now = int(time.time())
            with db_lock:
                db.execute(
                    "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                    (target, t, json.dumps(clean, ensure_ascii=False), now),
                )
                db.commit()
            bump_version()
            return self._json({"ok": True, "count": 1, "target": target})
        if t in ("bal", "set"):
            clean["v"] = to_int(cmd.get("v"), -10 ** 12, 10 ** 12)
        target_uid = to_int(to, 0, 10 ** 15)
        if target_uid > 0 and target_uid != OWNER_ID and (
            (t == "bal" and clean.get("v", 0) > 0) or t in ("item", "case")
        ):
            clean["notify_owner"] = True
        if t == "item":
            clean["item"] = to_str(cmd.get("item"), 100)
        if t == "case":
            clean["case"] = to_str(cmd.get("case"), 30)
        if t in ("item", "case"):
            clean["n"] = to_int(cmd.get("n"), 1, 500)
        now = int(time.time())
        with db_lock:
            if to == "all":
                ids = [r[0] for r in db.execute("SELECT uid FROM players LIMIT 20000")]
            else:
                ids = [to_int(to, 0, 10 ** 15)]
            ids = [u for u in ids if u]
            db.executemany(
                "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                [(u, t, json.dumps(clean, ensure_ascii=False), now) for u in ids],
            )
            add = 0
            if t in ("bal", "set"):
                add = max(0, clean["v"])
            elif t in ("item", "case"):
                add = 2_000_000 * clean["n"]
            if add:
                db.executemany(
                    "UPDATE players SET bonus=bonus+? WHERE uid=?", [(add, u) for u in ids]
                )
            if t in ("set", "clear"):  # админ сбросил игрока — снять пометку «подозрительный»
                db.executemany("UPDATE players SET susp=0 WHERE uid=?", [(u,) for u in ids])
            db.commit()
        bump_version()
        self._json({"ok": True, "count": len(ids)})

    def _battle(self, path, user, data):
        uid = int(user["id"])
        now = time.time()
        me = {"id": uid, "name": to_str(user.get("first_name") or user.get("username") or "Игрок", 30)}
        rid = to_str(data.get("room"), 20)
        with battle_lock:
            for k in [k for k, r in battle_rooms.items() if now - r["t"] > 600]:
                battle_rooms.pop(k, None)
            for k in [k for k, w in battle_wait.items() if now - w["t"] > 45]:
                battle_wait.pop(k, None)
            for u, lst in list(battle_invites.items()):
                lst[:] = [i for i in lst if now - i["t"] < 60]
                if not lst:
                    battle_invites.pop(u, None)
            if path == "/api/battle/online":
                with db_lock:
                    rows = db.execute("SELECT uid,name FROM players WHERE seen > ? AND uid != ? ORDER BY seen DESC LIMIT 30", (int(now) - 600, uid)).fetchall()
                return self._json({"players": [{"id": r[0], "name": r[1]} for r in rows]})
            if path == "/api/battle/invite":
                case = to_str(data.get("case"), 40)
                rounds = to_int(data.get("rounds"), 1, 5)
                fp = to_str(data.get("fp"), 20)
                to = to_int(data.get("to"), 0, 10 ** 15)
                if rounds not in (1, 3, 5) or not case or to <= 0 or to == uid:
                    return self._json({"error": "bad"}, 400)
                k = secrets.token_hex(6)
                battle_rooms[k] = {"id": k, "seed": None, "case": case, "rounds": rounds,
                                   "t": now, "players": [{"id": uid, "name": me["name"]}]}
                battle_invites.setdefault(to, []).append(
                    {"room": k, "from": {"id": uid, "name": me["name"]},
                     "case": case, "rounds": rounds, "fp": fp, "t": now})
                return self._json({"ok": True, "room": k})
            if path == "/api/battle/invites":
                return self._json({"invites": battle_invites.get(uid, [])})
            if path == "/api/battle/accept":
                room = to_str(data.get("room"), 20)
                lst = battle_invites.get(uid, [])
                inv = next((i for i in lst if i["room"] == room), None)
                if not inv:
                    return self._json({"error": "gone"}, 404)
                b = battle_rooms.get(room)
                if not b or b.get("seed"):
                    return self._json({"error": "gone"}, 404)
                b["players"].append({"id": uid, "name": me["name"]})
                b["seed"] = secrets.token_hex(8)
                lst.remove(inv)
                return self._json({"status": "matched", "battle": b, "me": 1})
            if path == "/api/battle/status":
                room = to_str(data.get("room"), 20)
                b = battle_rooms.get(room)
                if not b:
                    return self._json({"status": "gone"})
                if b.get("seed"):
                    return self._json({"status": "matched", "battle": b, "me": 0})
                b["t"] = now
                return self._json({"status": "waiting"})
            if path == "/api/battle/decline":
                room = to_str(data.get("room"), 20)
                lst = battle_invites.get(uid, [])
                inv = next((i for i in lst if i["room"] == room), None)
                if inv:
                    lst.remove(inv)
                    battle_rooms.pop(room, None)
                return self._json({"ok": True})
            if path == "/api/battle/cancel":
                battle_wait.pop(rid, None)
                return self._json({"ok": True})
            if path == "/api/battle/poll":
                b = battle_rooms.get(rid)
                if b:
                    for i, p in enumerate(b["players"]):
                        if p["id"] == uid:
                            return self._json({"status": "matched", "battle": b, "me": i})
                if rid in battle_wait and battle_wait[rid]["uid"] == uid:
                    battle_wait[rid]["t"] = now
                    return self._json({"status": "waiting"})
                return self._json({"status": "gone"})
            case = to_str(data.get("case"), 40)
            rounds = to_int(data.get("rounds"), 1, 5)
            fp = to_str(data.get("fp"), 20)
            if rounds not in (1, 3, 5) or not case:
                return self._json({"error": "bad"}, 400)
            for k in [k for k, w in battle_wait.items() if w["uid"] == uid]:
                battle_wait.pop(k, None)
            for k, w in list(battle_wait.items()):
                if w["case"] == case and w["rounds"] == rounds and w["fp"] == fp:
                    battle_wait.pop(k)
                    b = {"id": k, "seed": secrets.token_hex(8), "case": case,
                         "rounds": rounds, "t": now, "players": [w["p"], me]}
                    battle_rooms[k] = b
                    return self._json({"status": "matched", "battle": b, "me": 1})
            k = secrets.token_hex(6)
            battle_wait[k] = {"uid": uid, "case": case, "rounds": rounds, "fp": fp, "t": now, "p": me}
            return self._json({"status": "waiting", "room": k})

    def _grants(self, uid, path, data):
        with db_lock:
            if path == "/api/grants/ack":
                if data.get("delivered") is True:
                    ids = list(dict.fromkeys(to_int(x, 0, 10 ** 9) for x in (data.get("ids") or [])[:100]))
                    ids = [grant_id for grant_id in ids if grant_id]
                    if not ids:
                        return self._json({"ok": True, "delivered": 0})
                    rows = db.execute(
                        "SELECT id FROM grants WHERE uid=? AND done=0 AND id IN (%s) AND kind='warn'"
                        % ",".join("?" * len(ids)),
                        [uid] + ids,
                    ).fetchall()
                    confirmed = [row[0] for row in rows]
                    if confirmed:
                        db.execute(
                            "UPDATE grants SET done=1 WHERE uid=? AND kind='warn' AND id IN (%s)"
                            % ",".join("?" * len(confirmed)),
                            [uid] + confirmed,
                        )
                        if uid != OWNER_ID:
                            now = int(time.time())
                            message = f"⚠️ Первое предупреждение доставлено игроку {uid}."
                            db.execute(
                                "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                                (OWNER_ID, "notice", json.dumps({"t": "notice", "message": message}, ensure_ascii=False), now),
                            )
                        db.commit()
                    return self._json({"ok": True, "delivered": len(confirmed)})
                if data.get("synced") is not True:
                    return self._json({"ok": False, "error": "sync confirmation required"}, 409)
                ids = [to_int(x, 0, 10 ** 9) for x in (data.get("ids") or [])[:100]]
                if ids:
                    rows = db.execute(
                        "SELECT id,data FROM grants WHERE uid=? AND done=0 AND id IN (%s)" % ",".join("?" * len(ids)),
                        [uid] + ids,
                    ).fetchall()
                    confirmed = []
                    for row_id, raw in rows:
                        try:
                            grant = json.loads(raw)
                            if not grant.get("notify_owner") or uid == OWNER_ID:
                                continue
                            kind = grant.get("t")
                            if kind == "bal" and int(grant.get("v", 0)) > 0:
                                message = f"✅ Игрок {uid} получил {int(grant['v']):,} монет на баланс.".replace(",", " ")
                            elif kind == "case":
                                message = f"✅ Игрок {uid} получил кейсы: {to_str(grant.get('case'), 40)} ×{to_int(grant.get('n'), 1, 500)}."
                            elif kind == "item":
                                message = f"✅ Игрок {uid} получил скин: {to_str(grant.get('item'), 100)} ×{to_int(grant.get('n'), 1, 500)}."
                            else:
                                continue
                            confirmed.append((row_id, message))
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
                    db.execute(
                        "UPDATE grants SET done=1 WHERE uid=? AND id IN (%s)" % ",".join("?" * len(ids)),
                        [uid] + ids,
                    )
                    if uid != OWNER_ID:
                        now = int(time.time())
                        for row_id, message in confirmed:
                            db.execute(
                                "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                                (OWNER_ID, "notice", json.dumps({"t": "notice", "message": message}, ensure_ascii=False), now),
                            )
                    db.commit()
                return self._json({"ok": True, "confirmed": len(ids)})
            rows = db.execute(
                "SELECT id,data FROM grants WHERE uid=? AND done=0 ORDER BY id LIMIT 50", (uid,)
            ).fetchall()
        out = []
        for r in rows:
            try:
                g = json.loads(r[1])
                g["id"] = r[0]
                out.append(g)
            except Exception:
                pass
        self._json({"grants": out})

    def _activity(self, user, data):
        uid = int(user["id"])
        name = to_str(user.get("first_name") or user.get("username") or "Игрок", 30)
        game = to_str(data.get("game"), 48).strip()
        now = int(time.time())
        win = data.get("win") if isinstance(data.get("win"), dict) else None
        amount = to_int(win.get("amount"), 0, MAX_BALANCE) if win else 0
        win_game = to_str((win or {}).get("game") or game or "Игра", 48).strip()
        with db_lock:
            db.execute(
                "INSERT INTO player_activity(uid,name,game,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET name=excluded.name,game=excluded.game,updated=excluded.updated",
                (uid, name, game, now),
            )
            if amount > 0:
                db.execute("INSERT INTO player_wins(uid,name,game,amount,ts) VALUES(?,?,?,?,?)",
                           (uid, name, win_game, amount, now))
                db.execute("DELETE FROM player_wins WHERE id NOT IN (SELECT id FROM player_wins ORDER BY id DESC LIMIT 200)")
            db.commit()
        return self._json({"ok": True})

    def _chat_send(self, user, data):
        global chat_version, last_chat_msg
        uid = int(user["id"])
        name = to_str(user.get("first_name") or user.get("username") or "Игрок", 30)
        text = to_str(data.get("text"), 140).strip()
        if not text:
            return self._json({"error": "empty"}, 400)
        now = time.time()
        with chat_lock:
            if now - last_chat.get(uid, 0) < 1.5:
                return self._json({"error": "slow"}, 429)
            last_chat[uid] = now
            db.execute("INSERT INTO chat (uid,name,text,ts) VALUES (?,?,?,?)", (uid, name, text, int(now)))
            db.execute("DELETE FROM chat WHERE id NOT IN (SELECT id FROM chat ORDER BY id DESC LIMIT ?)", (CHAT_KEEP,))
            db.commit()
            mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        last_chat_msg = {"id": mid, "uid": uid, "name": name, "text": text, "ts": int(now)}
        with version_cond:
            chat_version += 1
            version_cond.notify_all()
        self._json({"ok": True})

    def _trades(self, uid, path, data):
        if path == "/api/trades/create":
            to = to_int(data.get("to"), 0, 10 ** 15)
            if to <= 0 or to == uid:
                return self._json({"error": "bad player"}, 400)
            give = data.get("give") or {}
            want = data.get("want") or {}
            gbal = to_int(give.get("bal"), 0, 10 ** 12)
            wbal = to_int(want.get("bal"), 0, 10 ** 12)
            gitems = clean_items(give.get("items"))
            witems = clean_items(want.get("items"))
            if not (gbal or gitems or wbal or witems):
                return self._json({"error": "пустой обмен"}, 400)
            now = int(time.time())
            with trade_lock:
                c = db.execute(
                    "SELECT COUNT(*) FROM trades WHERE from_uid=? AND to_uid=? AND status='pending'",
                    (uid, to),
                ).fetchone()[0]
                if c >= 3:
                    return self._json({"error": "максимум 3 активных предложения этому игроку"}, 400)
                db.execute(
                    "INSERT INTO trades (from_uid,to_uid,give,want,ts) VALUES (?,?,?,?,?)",
                    (uid, to,
                     json.dumps({"bal": gbal, "items": gitems}, ensure_ascii=False),
                     json.dumps({"bal": wbal, "items": witems}, ensure_ascii=False), now),
                )
                db.commit()
            return self._json({"ok": True})
        if path == "/api/trades/list":
            with trade_lock:
                rows = db.execute(
                    "SELECT id,from_uid,to_uid,give,want,ts FROM trades WHERE (to_uid=? OR from_uid=?) AND status='pending' ORDER BY id DESC LIMIT 50",
                    (uid, uid),
                ).fetchall()
            incoming, outgoing = [], []
            with db_lock:
                for r in rows:
                    fn = db.execute("SELECT name FROM players WHERE uid=?", (r[1],)).fetchone()
                    tn = db.execute("SELECT name FROM players WHERE uid=?", (r[2],)).fetchone()
                    item = {"id": r[0], "from_name": fn[0] if fn else str(r[1]),
                            "to_name": tn[0] if tn else str(r[2]),
                            "give": json.loads(r[3]), "want": json.loads(r[4]), "ts": r[5]}
                    (incoming if r[2] == uid else outgoing).append(item)
            return self._json({"incoming": incoming, "outgoing": outgoing})
        if path == "/api/trades/accept":
            tid = to_int(data.get("id"), 0, 10 ** 9)
            with trade_lock:
                r = db.execute(
                    "SELECT id,from_uid,to_uid,give,want FROM trades WHERE id=? AND status='pending'",
                    (tid,),
                ).fetchone()
                if not r or r[2] != uid:
                    return self._json({"error": "не найдено"}, 404)
                db.execute("UPDATE trades SET status='accepted' WHERE id=?", (tid,))
                db.commit()
            give = json.loads(r[3])
            want = json.loads(r[4])
            now = int(time.time())
            with db_lock:
                db.executemany(
                    "INSERT INTO grants (uid,kind,data,created) VALUES (?,?,?,?)",
                    [(uid, "trade",
                      json.dumps({"t": "trade", "receive": give, "lose": want, "with": r[1]}, ensure_ascii=False), now),
                     (r[1], "trade",
                      json.dumps({"t": "trade", "receive": want, "lose": give, "with": uid}, ensure_ascii=False), now)],
                )
                db.commit()
            return self._json({"ok": True})
        if path == "/api/trades/decline":
            tid = to_int(data.get("id"), 0, 10 ** 9)
            with trade_lock:
                db.execute(
                    "UPDATE trades SET status='declined' WHERE id=? AND to_uid=? AND status='pending'",
                    (tid, uid),
                )
                db.commit()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def _clubs(self, uid, path, data):
        if path == "/api/clubs/create":
            name = to_str(data.get("name"), 20).strip()
            tag = to_str(data.get("tag"), 5).strip().upper()
            emblem = to_str(data.get("emblem"), 8)
            if not re.match(r"^[\wА-Яа-яЁё \-\.]{3,20}$", name):
                return self._json({"error": "название: 3–20 символов (буквы, цифры, пробел)"}, 400)
            if not re.match(r"^[A-ZА-Я0-9]{2,5}$", tag):
                return self._json({"error": "тег: 2–5 символов"}, 400)
            if emblem not in CLUB_EMBLEMS:
                return self._json({"error": "выбери значок"}, 400)
            with club_lock:
                if db.execute("SELECT 1 FROM clubs WHERE name=?", (name,)).fetchone():
                    return self._json({"error": "такой клуб уже есть"}, 400)
                for row in db.execute("SELECT members FROM clubs").fetchall():
                    if uid in json.loads(row[0] or "[]"):
                        return self._json({"error": "сначала выйди из своего клуба"}, 400)
                db.execute(
                    "INSERT INTO clubs (name,tag,emblem,owner,members,ts) VALUES (?,?,?,?,?,?)",
                    (name, tag, emblem, uid, json.dumps([uid]), int(time.time())),
                )
                db.commit()
            with db_lock:
                db.execute("UPDATE players SET balance=MAX(0,balance-?) WHERE uid=?", (CLUB_COST, uid))
                db.commit()
            bump_version()
            return self._json({"ok": True})
        if path == "/api/clubs/join":
            cid = to_int(data.get("id"), 0, 10 ** 9)
            with club_lock:
                r = db.execute("SELECT id,members FROM clubs WHERE id=?", (cid,)).fetchone()
                if not r:
                    return self._json({"error": "клуб не найден"}, 404)
                members = json.loads(r[1] or "[]")
                if uid in members:
                    return self._json({"error": "ты уже в этом клубе"}, 400)
                for row in db.execute("SELECT members FROM clubs").fetchall():
                    if uid in json.loads(row[0] or "[]"):
                        return self._json({"error": "сначала выйди из своего клуба"}, 400)
                if len(members) >= CLUB_MAX:
                    return self._json({"error": "клуб полон"}, 400)
                members.append(uid)
                db.execute("UPDATE clubs SET members=? WHERE id=?", (json.dumps(members), cid))
                db.commit()
            return self._json({"ok": True})
        if path == "/api/clubs/leave":
            with club_lock:
                rows = db.execute("SELECT id,owner,members FROM clubs").fetchall()
                for r in rows:
                    members = json.loads(r[2] or "[]")
                    if uid in members:
                        members.remove(uid)
                        if not members:
                            db.execute("DELETE FROM clubs WHERE id=?", (r[0],))
                        else:
                            owner = members[0] if r[1] == uid else r[1]
                            db.execute("UPDATE clubs SET members=?, owner=? WHERE id=?",
                                       (json.dumps(members), owner, r[0]))
                        db.commit()
                        return self._json({"ok": True})
            return self._json({"error": "ты не в клубе"}, 400)
        if path == "/api/clubs/list":
            with club_lock:
                rows = db.execute("SELECT id,name,tag,emblem,owner,members,ts FROM clubs").fetchall()
            out = []
            with db_lock:
                for r in rows:
                    members = json.loads(r[5] or "[]")
                    score = 0
                    for m in members:
                        b = db.execute("SELECT balance FROM players WHERE uid=?", (m,)).fetchone()
                        score += b[0] if b else 0
                    out.append({"id": r[0], "name": r[1], "tag": r[2], "emblem": r[3],
                                "owner": r[4], "count": len(members), "score": score})
            out.sort(key=lambda c: -c["score"])
            return self._json({"clubs": out[:50]})
        if path == "/api/clubs/mine":
            with club_lock:
                rows = db.execute("SELECT id,name,tag,emblem,owner,members,ts FROM clubs").fetchall()
            for r in rows:
                members = json.loads(r[5] or "[]")
                if uid in members:
                    ml, score = [], 0
                    with db_lock:
                        for m in members:
                            row = db.execute("SELECT name,balance FROM players WHERE uid=?", (m,)).fetchone()
                            if row:
                                ml.append({"id": m, "name": row[0], "balance": row[1]})
                                score += row[1]
                    return self._json({"club": {"id": r[0], "name": r[1], "tag": r[2],
                                                "emblem": r[3], "owner": r[4],
                                                "members": ml, "score": score}})
            return self._json({"club": None})
        return self._json({"error": "not found"}, 404)

    def _stream(self):
        with clients_lock:
            if len(clients) >= MAX_SSE:
                return self._json({"error": "busy"}, 503)
            token = object()
            clients.add(token)
        try:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            seen_version = -1
            seen_chat = -1
            last_ping = 0
            last_push = 0
            while True:
                with version_cond:
                    version_cond.wait(timeout=5)
                    cur = board_version
                now = time.time()
                if cur != seen_version and now - last_push >= 1.0:
                    seen_version = cur
                    last_push = now
                    payload = json.dumps(get_board(), ensure_ascii=False)
                    self.wfile.write(f"event: board\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_ping = now
                if chat_version != seen_chat and last_chat_msg is not None:
                    seen_chat = chat_version
                    try:
                        self.wfile.write(("event: chat\ndata: " + json.dumps(last_chat_msg, ensure_ascii=False) + "\n\n").encode())
                        self.wfile.flush()
                    except Exception:
                        pass
                if now - last_ping >= 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
        except Exception:
            pass
        finally:
            with clients_lock:
                clients.discard(token)
            self.close_connection = True


def run_server():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


threading.Thread(target=run_server, daemon=True).start()
# --------------------------------------------------------------------


@bot.message_handler(commands=["start"])
def send_welcome(message):
  text = (
      "Привет! Добро пожаловать в официальное Telegram Casino 🎲\nНажми на"
      " кнопку ниже, чтобы открыть мини-приложение:"
  )
  if BASE_WEB_APP_URL:
    markup = InlineKeyboardMarkup()
    web_app_button = InlineKeyboardButton(
        text="🎰 Играть в Казино", web_app=WebAppInfo(url=BASE_WEB_APP_URL)
    )
    markup.add(web_app_button)
    bot.send_message(message.chat.id, text, reply_markup=markup)
  else:
    bot.send_message(message.chat.id, text + "\n\nСсылка на приложение пока не настроена.")

@bot.message_handler(commands=["give"])
def give_money(message):
  # Проверяем, что команду пишете именно вы
  if message.from_user.id != OWNER_ID:
    bot.reply_to(message, "⛔ У вас нет прав на использование этой команды.")
    return
  if not BASE_WEB_APP_URL:
    bot.reply_to(message, "Ссылка на приложение пока не настроена.")
    return

  args = message.text.split()
  if len(args) < 3:
    bot.reply_to(
        message,
        "⚠️ Использование: `/give ID_пользователя сумма`",
        parse_mode="Markdown",
    )
    return

  try:
    target_user_id = int(args[1])  # Принимаем чистый цифровой ID
    amount = int(args[2])
  except ValueError:
    bot.reply_to(message, "❌ Ошибка! ID и сумма должны быть числами.")
    return

  if amount <= 0:
    bot.reply_to(message, "❌ Сумма должна быть больше нуля.")
    return

  # Создаем защищенную ссылку с бонусом и привязкой к ID игрока
  bonus_url = f"{BASE_WEB_APP_URL}?bonus={amount}&for_user={target_user_id}"

  markup = InlineKeyboardMarkup()
  claim_button = InlineKeyboardButton(
      text=f"🎁 Забрать {amount} фишек!", web_app=WebAppInfo(url=bonus_url)
  )
  markup.add(claim_button)

  try:
    # Отправляем персональное сообщение НАПРЯМУЮ игроку по его ID
    bot.send_message(
        target_user_id,
        (
            "🎁 **Внимание!**\n👑 Владелец выделил вам персональный бонус:"
            f" **{amount} фишек**!\nНажмите кнопку ниже, чтобы зайти в казино и"
            " забрать их:"
        ),
        reply_markup=markup,
        parse_mode="Markdown",
    )
    # Отправляем вам подтверждение в чат
    bot.reply_to(
        message,
        (
            "✅ Бонус в размере "
            f"{amount} фишек успешно отправлен игроку с ID `{target_user_id}` в"
            " личные сообщения!"
        ),
    )

  except Exception as e:
    bot.reply_to(
        message,
        (
            "❌ Не удалось отправить сообщение игроку. Возможно, он еще не"
            f" запустил бота или заблокировал его.\nОшибка: {e}"
        ),
    )


@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    if message.from_user.id != OWNER_ID:
        return bot.reply_to(message, "⛔ Нет прав.")
    try:
        uid = int(message.text.split()[1])
    except Exception:
        return bot.reply_to(message, "Использование: /ban ID_игрока")
    with db_lock:
        db.execute(
            "INSERT INTO players (uid,name,balance) VALUES (?,?,?) ON CONFLICT(uid) DO NOTHING",
            (uid, "Игрок", START_BAL),
        )
        db.execute("UPDATE players SET banned=1,ban_until=0,ban_starts_at=0,ban_reason='Заблокировано через команду бота' WHERE uid=?", (uid,))
        db.commit()
    bot.reply_to(message, f"🔨 Игрок {uid} заблокирован.")


@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    if message.from_user.id != OWNER_ID:
        return bot.reply_to(message, "⛔ Нет прав.")
    try:
        uid = int(message.text.split()[1])
    except Exception:
        return bot.reply_to(message, "Использование: /unban ID_игрока")
    with db_lock:
        db.execute("UPDATE players SET banned=0,ban_until=0,ban_starts_at=0,ban_reason='' WHERE uid=?", (uid,))
        db.commit()
    bot.reply_to(message, f"✅ Игрок {uid} разблокирован.")


if __name__ == "__main__":
  if BASE_WEB_APP_URL:
    try:
      bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎰 Играть", web_app=WebAppInfo(url=BASE_WEB_APP_URL)))
      print("Кнопка открытия казино в меню Telegram обновлена.")
    except Exception as e:
      print(f"Не удалось обновить кнопку меню Telegram: {e}")
  print("Бот и веб-сервер запущены...")
  bot.infinity_polling()
