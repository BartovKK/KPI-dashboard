"""Общие функции: конфиг, база SQLite, даты."""
import calendar
import datetime as dt
import json
import os
import sqlite3
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "kpi.sqlite")

_db_lock = threading.RLock()


def load_json(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)


def load_config():
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        path = os.path.join(ROOT, "config.example.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rules():
    return load_json("kpi_rules.json")


SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, kind TEXT, direction TEXT, duration INTEGER,
  is_lpr INTEGER, secretary INTEGER, next_step INTEGER, status TEXT,
  has_protocol INTEGER, has_summary INTEGER, subject TEXT, client TEXT);
CREATE INDEX IF NOT EXISTS ix_act ON activities(emp, dt);

CREATE TABLE IF NOT EXISTS tenders (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, client TEXT, created TEXT,
  in_work INTEGER, price_review INTEGER, submitted_date TEXT,
  result TEXT, result_date TEXT, price REAL, planned_cost REAL, actual_cost REAL);

CREATE TABLE IF NOT EXISTS deals (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, client TEXT, created TEXT,
  closed_date TEXT, result TEXT, amount REAL);

CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, first_contact TEXT, qualified_date TEXT,
  need INTEGER, budget INTEGER, lpr INTEGER, solvency INTEGER, next_step INTEGER, status TEXT);

CREATE TABLE IF NOT EXISTS clients (
  id TEXT PRIMARY KEY, emp TEXT, name TEXT, need INTEGER, strategy INTEGER, updated TEXT);

CREATE TABLE IF NOT EXISTS kp (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, client TEXT, amount REAL,
  out_of_tender INTEGER, confirmed INTEGER, source TEXT);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, emp TEXT, title TEXT, created TEXT, deadline TEXT, closed TEXT, status TEXT);

CREATE TABLE IF NOT EXISTS sales (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, doc TEXT, client TEXT, revenue REAL, cost REAL, signed INTEGER);

CREATE TABLE IF NOT EXISTS receivables (
  id TEXT PRIMARY KEY, emp TEXT, doc TEXT, client TEXT, ship_date TEXT, due_date TEXT,
  paid_date TEXT, amount REAL);

CREATE TABLE IF NOT EXISTS guarantees (
  id TEXT PRIMARY KEY, emp TEXT, kind TEXT, ref TEXT, amount REAL,
  acts_date TEXT, unblock_date TEXT, return_date TEXT);

CREATE TABLE IF NOT EXISTS penalties (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, amount REAL, reason TEXT);

CREATE TABLE IF NOT EXISTS supplier_orders (
  id TEXT PRIMARY KEY, emp TEXT, doc TEXT, dt TEXT, supplier TEXT, lot TEXT, amount_novat REAL,
  planned_date TEXT, actual_date TEXT, complete INTEGER, defect INTEGER, mis_sort INTEGER,
  deferred_share REAL, deferral_days INTEGER,
  kp_count INTEGER, relevant INTEGER, evidence INTEGER, affiliated INTEGER,
  single_source INTEGER, emergency INTEGER, justified INTEGER, customer_order TEXT);

CREATE TABLE IF NOT EXISTS supply_funnel (
  id TEXT PRIMARY KEY, emp TEXT, dt TEXT, customer_order TEXT, client TEXT, stage TEXT);

CREATE TABLE IF NOT EXISTS manual_scores (
  emp TEXT, week TEXT, criterion TEXT, score REAL, comment TEXT, author TEXT, updated TEXT,
  PRIMARY KEY (emp, week, criterion));

CREATE TABLE IF NOT EXISTS plans (
  emp TEXT, month TEXT, metric TEXT, value REAL, PRIMARY KEY (emp, month, metric));

CREATE TABLE IF NOT EXISTS baselines (
  emp TEXT, metric TEXT, value REAL, PRIMARY KEY (emp, metric));

CREATE TABLE IF NOT EXISTS users (
  login TEXT PRIMARY KEY, pwd_hash TEXT, salt TEXT);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, login TEXT, expires TEXT);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, status TEXT, message TEXT);
"""

# таблицы, которые полностью перезаписываются при синхронизации
SYNC_TABLES = ["activities", "tenders", "deals", "leads", "clients", "kp", "tasks", "sales",
               "receivables", "guarantees", "penalties", "supplier_orders", "supply_funnel"]


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def db_lock():
    return _db_lock


def table_columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def replace_rows(con, table, rows, emps=None, date_field=None, date_from=None):
    """Заменить данные таблицы: удаляет записи сотрудников emps (за период, если задан) и вставляет rows."""
    cols = table_columns(con, table)
    with _db_lock:
        if emps is None:
            con.execute(f"DELETE FROM {table}")
        else:
            q = f"DELETE FROM {table} WHERE emp IN ({','.join('?' * len(emps))})"
            args = list(emps)
            if date_field and date_from:
                q += f" AND ({date_field} >= ? OR {date_field} IS NULL)"
                args.append(date_from)
            con.execute(q, args)
        for r in rows:
            keys = [k for k in r if k in cols]
            con.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                [r[k] for k in keys])
        con.commit()


# ---------- даты ----------

def parse_date(s):
    if not s:
        return None
    if isinstance(s, (dt.date, dt.datetime)):
        return s if isinstance(s, dt.date) and not isinstance(s, dt.datetime) else s.date()
    s = str(s)[:10]
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        for fmt in ("%d.%m.%Y",):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return None


def month_bounds(month):
    y, m = map(int, month.split("-"))
    first = dt.date(y, m, 1)
    last = dt.date(y, m, calendar.monthrange(y, m)[1])
    return first, last


def month_add(month, delta):
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def quarter_months(month):
    y, m = map(int, month.split("-"))
    q0 = (m - 1) // 3 * 3 + 1
    return [f"{y:04d}-{q0 + i:02d}" for i in range(3)]


def working_days(first, last, holidays):
    hol = set(holidays or [])
    n = 0
    d = first
    while d <= last:
        if d.weekday() < 5 and d.isoformat() not in hol:
            n += 1
        d += dt.timedelta(days=1)
    return n


def business_days_between(a, b, holidays):
    """Количество рабочих дней после a до b включительно (0, если b <= a)."""
    if not a or not b or b <= a:
        return 0
    return working_days(a + dt.timedelta(days=1), b, holidays)


def weeks_of_month(month):
    """Понедельники недель, которые пересекаются с месяцем."""
    first, last = month_bounds(month)
    monday = first - dt.timedelta(days=first.weekday())
    res = []
    while monday <= last:
        res.append(monday.isoformat())
        monday += dt.timedelta(days=7)
    return res


def today():
    return dt.date.today()


def current_month():
    t = today()
    return f"{t.year:04d}-{t.month:02d}"
