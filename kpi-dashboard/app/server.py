"""Веб-сервер дашборда: авторизация, API, статика."""
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import sync
from .common import ROOT, connect, current_month, db_lock, load_config, load_rules, month_add, weeks_of_month
from .engine import CRITERIA_TITLES, Engine

WEB = os.path.join(ROOT, "web")
PAGE_HEAD = (b'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
             b'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>')
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PLAN_METRICS = {"gross_profit", "revenue"}
# защита от подбора пароля: не больше LOGIN_MAX_FAILS неудачных попыток за LOGIN_WINDOW секунд
LOGIN_MAX_FAILS = 10
LOGIN_WINDOW = 15 * 60


class BadRequest(Exception):
    pass


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 200_000).hex()
    return h, salt


def set_password(con, login, password):
    h, salt = hash_password(password)
    with db_lock():
        con.execute("INSERT OR REPLACE INTO users (login, pwd_hash, salt) VALUES (?,?,?)", (login, h, salt))
        con.commit()


class App:
    def __init__(self):
        self.config = load_config()
        self.rules = load_rules()
        self.con = connect()
        self.engine = Engine(self.con, self.rules, self.config)
        # та же блокировка, под которой синхронизация пишет в базу: расчёт не увидит недописанные таблицы
        self.lock = db_lock()
        self.login_fails = {}  # (ip, login) -> [время неудачных попыток]

    def login_blocked(self, key):
        now = time.monotonic()
        fails = [t for t in self.login_fails.get(key, []) if now - t < LOGIN_WINDOW]
        self.login_fails[key] = fails
        return len(fails) >= LOGIN_MAX_FAILS

    def login_failed(self, key):
        self.login_fails.setdefault(key, []).append(time.monotonic())

    def user(self, login):
        return next((e for e in self.config["employees"] if e["login"] == login), None)

    def check_password(self, login, password):
        r = self.con.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        if not r or not self.user(login):
            return False
        h, _ = hash_password(password, r["salt"])
        return hmac.compare_digest(h, r["pwd_hash"])

    def session_user(self, token):
        if not token:
            return None
        r = self.con.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        if not r or r["expires"] < dt.datetime.now().isoformat():
            return None
        return self.user(r["login"])

    def new_session(self, login):
        token = secrets.token_urlsafe(32)
        hours = self.config.get("server", {}).get("session_hours", 12)
        exp = (dt.datetime.now() + dt.timedelta(hours=hours)).isoformat()
        with db_lock():
            self.con.execute("INSERT INTO sessions VALUES (?,?,?)", (token, login, exp))
            self.con.execute("DELETE FROM sessions WHERE expires < ?", (dt.datetime.now().isoformat(),))
            self.con.commit()
        return token

    # ---------- данные для интерфейса
    def bootstrap(self, me):
        is_mgr = me.get("role") == "manager"
        emps = self.engine.employees() if is_mgr else [me]
        cm = current_month()
        last = sync.last_sync(self.con)
        return {
            "me": {"login": me["login"], "name": me["name"], "role": me.get("role", "employee")},
            "employees": [{"login": e["login"], "name": e["name"], "position": e["position"],
                           "position_title": self.rules["positions"][e["position"]]["title"]} for e in emps],
            "positions": {k: {"title": v["title"], "department": v["department"], "salary": v["salary"],
                              "kpi_bonus": v["kpi_bonus"], "doc": v.get("doc"),
                              "items": [{x: i.get(x) for x in ("n", "title", "weight", "period", "target_text", "description")}
                                        for i in v["items"]]}
                          for k, v in self.rules["positions"].items()},
            "months": [month_add(cm, -k) for k in range(0, 12)],
            "current_month": cm,
            "mode": self.config.get("mode", "demo"),
            "payout_mode": self.rules.get("payout", {}).get("mode", "binary"),
            "criteria_titles": CRITERIA_TITLES,
            "last_sync": last,
        }

    def employee(self, login, month):
        r = self.engine.compute(login, month)
        r["trend"] = self.engine.trend(login, month)
        r["weeks"] = weeks_of_month(month)
        return r


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "KPIDashboard/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- ответы
    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            body = f.read()
        if path.endswith(".html"):
            body = PAGE_HEAD + body + b"</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(body)

    def token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["kpi_session"].value if "kpi_session" in c else None

    def body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise BadRequest("Ожидается JSON")
        try:
            b = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise BadRequest("Некорректный JSON")
        if not isinstance(b, dict):
            raise BadRequest("Некорректный JSON")
        return b

    # ---------- проверка параметров
    @staticmethod
    def month_param(v):
        if not isinstance(v, str) or not MONTH_RE.match(v):
            raise BadRequest("Месяц указывается в формате ГГГГ-ММ")
        return v

    @staticmethod
    def number_param(v, lo=0.0, hi=None):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise BadRequest("Значение должно быть числом")
        if not math.isfinite(x) or x < lo or (hi is not None and x > hi):
            raise BadRequest(f"Значение должно быть от {lo:g}" + (f" до {hi:g}" if hi is not None else ""))
        return x

    def employee_param(self, login):
        e = self.app.engine.employee(login) if isinstance(login, str) else None
        if not e or not e.get("position"):
            raise BadRequest("Сотрудник не найден")
        return login

    @staticmethod
    def week_param(v):
        try:
            d = dt.date.fromisoformat(v)
        except (TypeError, ValueError):
            raise BadRequest("Неделя указывается датой понедельника ГГГГ-ММ-ДД")
        if d.weekday() != 0:
            raise BadRequest("Неделя указывается датой понедельника")
        return v

    # ---------- маршруты
    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            if u.path in ("/", "/index.html"):
                return self.send_file(os.path.join(WEB, "index.html"), "text/html; charset=utf-8")
            self.send_response(404)
            self.end_headers()
            return
        self.api("GET", u.path, {k: v[0] for k, v in parse_qs(u.query).items()})

    def do_POST(self):
        u = urlparse(self.path)
        self.api("POST", u.path, {})

    def api(self, method, path, q):
        app = self.app
        try:
            if path == "/api/login" and method == "POST":
                b = self.body()
                login = str(b.get("login") or "").strip().lower()
                key = (self.client_address[0], login)
                with app.lock:
                    if app.login_blocked(key):
                        return self.send_json({"error": "Слишком много неудачных попыток входа. Попробуйте через 15 минут"}, 429)
                    ok = app.check_password(login, str(b.get("password") or ""))
                    if not ok:
                        app.login_failed(key)
                        return self.send_json({"error": "Неверный логин или пароль"}, 401)
                    app.login_fails.pop(key, None)
                    tok = app.new_session(login)
                secure = "; Secure" if app.config.get("server", {}).get("secure_cookie") else ""
                return self.send_json({"ok": True}, cookie=f"kpi_session={tok}; HttpOnly; SameSite=Lax; Path=/{secure}")
            if path == "/api/logout" and method == "POST":
                with db_lock():
                    app.con.execute("DELETE FROM sessions WHERE token=?", (self.token(),))
                    app.con.commit()
                return self.send_json({"ok": True}, cookie="kpi_session=; Max-Age=0; Path=/")

            me = app.session_user(self.token())
            if not me:
                return self.send_json({"error": "Требуется вход"}, 401)
            is_mgr = me.get("role") == "manager"
            month = self.month_param(q["month"]) if q.get("month") else current_month()

            with app.lock:
                if path == "/api/bootstrap":
                    return self.send_json(app.bootstrap(me))
                if path == "/api/overview":
                    if not is_mgr:
                        return self.send_json({"error": "Доступно только руководителю"}, 403)
                    return self.send_json(app.engine.overview(month))
                if path == "/api/employee":
                    login = q.get("login") or me["login"]
                    if not is_mgr and login != me["login"]:
                        return self.send_json({"error": "Нет доступа к дашборду другого сотрудника"}, 403)
                    if not app.engine.employee(login) or not app.engine.employee(login).get("position"):
                        return self.send_json({"error": "Сотрудник не найден"}, 404)
                    return self.send_json(app.employee(login, month))
                if method == "POST" and not is_mgr:
                    return self.send_json({"error": "Изменять данные может только руководитель"}, 403)
                if path == "/api/score" and method == "POST":
                    b = self.body()
                    score = b.get("score")
                    if score is not None:
                        score = self.number_param(score, 0, 100)
                    if b.get("criterion") not in CRITERIA_TITLES:
                        raise BadRequest("Неизвестный критерий оценки")
                    self.employee_param(b.get("emp"))
                    self.week_param(b.get("week"))
                    with db_lock():
                        if score is None:
                            app.con.execute("DELETE FROM manual_scores WHERE emp=? AND week=? AND criterion=?",
                                            (b["emp"], b["week"], b["criterion"]))
                        else:
                            app.con.execute("INSERT OR REPLACE INTO manual_scores VALUES (?,?,?,?,?,?,datetime('now'))",
                                            (b["emp"], b["week"], b["criterion"], score, str(b.get("comment") or "")[:500],
                                             me["login"]))
                        app.con.commit()
                    return self.send_json({"ok": True})
                if path == "/api/plan" and method == "POST":
                    b = self.body()
                    metric = b.get("metric", "gross_profit")
                    if metric not in PLAN_METRICS:
                        raise BadRequest("Неизвестный показатель плана")
                    row = (self.employee_param(b.get("emp")), self.month_param(b.get("month")), metric,
                           self.number_param(b.get("value")))
                    with db_lock():
                        app.con.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,?)", row)
                        app.con.commit()
                    return self.send_json({"ok": True})
                if path == "/api/baseline" and method == "POST":
                    b = self.body()
                    if b.get("metric", "conversion_last_year") != "conversion_last_year":
                        raise BadRequest("Неизвестный показатель")
                    row = (self.employee_param(b.get("emp")), "conversion_last_year", self.number_param(b.get("value"), 0, 1))
                    with db_lock():
                        app.con.execute("INSERT OR REPLACE INTO baselines VALUES (?,?,?)", row)
                        app.con.commit()
                    return self.send_json({"ok": True})
            if path == "/api/sync" and method == "POST":
                res = sync.run(app.con, app.config)
                return self.send_json(res)
            return self.send_json({"error": "Не найдено"}, 404)
        except BadRequest as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa
            traceback.print_exc()
            return self.send_json({"error": f"Ошибка сервера: {e}"}, 500)


def serve():
    app = App()
    Handler.app = app
    cfg = app.config
    if not sync.last_sync(app.con):
        print("Первая синхронизация…")
        print(sync.run(app.con, cfg)["message"])
    if cfg.get("mode", "demo") == "demo":
        have = {r["login"] for r in app.con.execute("SELECT login FROM users")}
        for e in cfg["employees"]:
            if e["login"] not in have:
                set_password(app.con, e["login"], "demo")
        print("Демо-режим: у всех пользователей без пароля установлен пароль demo")
    sync.start_scheduler(app.con, cfg)
    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(cfg.get("server", {}).get("port", 8080))
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"Не удалось занять порт {port}: {e}. Возможно, дашборд уже запущен в другом окне "
              f"или порт занят другой программой — поменяйте server.port в config.json.")
        return
    url = f"http://localhost:{port}"
    print(f"Дашборд KPI работает: {url}  (Ctrl+C или закрытие окна — остановить)")
    if cfg.get("server", {}).get("open_browser", True):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    httpd.serve_forever()
