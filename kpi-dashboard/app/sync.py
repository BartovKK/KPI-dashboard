"""Синхронизация: демо-данные или выгрузка из Битрикс24 и 1С."""
import datetime as dt
import threading
import traceback

from . import demo
from .bitrix24 import Bitrix24
from .common import current_month, db_lock, month_add, month_bounds, replace_rows, today
from .onec import DATASETS, OneC

B24_DATE_FIELDS = {"activities": "dt", "kp_letters": "dt", "tenders": None, "deals": "closed_date",
                   "leads": "qualified_date", "clients": None, "tasks": "deadline"}

_running = threading.Lock()


def run(con, config, force_demo=False):
    if not _running.acquire(blocking=False):
        return {"status": "busy", "message": "Синхронизация уже выполняется"}
    started = dt.datetime.now().isoformat(timespec="seconds")
    log = []
    status = "ok"
    try:
        if config.get("mode", "demo") == "demo" or force_demo:
            counts = demo.generate(con, config)
            log.append("Демо-данные: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        else:
            emps = [e for e in config["employees"] if e.get("position")]
            logins = [e["login"] for e in emps]
            months = config.get("sync", {}).get("months_back", 6)
            date_from = month_bounds(month_add(current_month(), -months))[0]
            date_to = today()
            # сначала всё выгружаем (это долго), потом пишем одной транзакцией под db_lock,
            # чтобы дашборд никогда не видел наполовину обновлённые таблицы
            writes = []  # (источник, таблица, строки, поле даты)

            if config.get("bitrix24", {}).get("webhook") and "ВАШ-ПОРТАЛ" not in config["bitrix24"]["webhook"]:
                try:
                    b24 = Bitrix24(config["bitrix24"], log=log.append)
                    data = b24.fetch({int(e["b24_user_id"]): e["login"] for e in emps if e.get("b24_user_id")}, date_from)
                    for table, rows in data.items():
                        writes.append(("Б24", table, rows, B24_DATE_FIELDS.get(table)))
                except Exception as e:  # noqa
                    status = "error"
                    log.append(f"Ошибка Битрикс24: {e}")
                    traceback.print_exc()
            else:
                log.append("Битрикс24 не настроен (bitrix24.webhook)")

            if config.get("onec", {}).get("base_url") and "СЕРВЕР-1С" not in config["onec"]["base_url"]:
                try:
                    onec = OneC(config["onec"], log=log.append)
                    managers = {str(e["onec_manager"]).strip().lower(): e["login"] for e in emps if e.get("onec_manager")}
                    data = onec.fetch(con, managers, date_from, date_to)
                    for table, rows in data.items():
                        date_field = next((d for t, d in DATASETS.values() if t == table), None)
                        writes.append(("1С", table, rows, date_field))
                except Exception as e:  # noqa
                    status = "error"
                    log.append(f"Ошибка 1С: {e}")
                    traceback.print_exc()
            else:
                log.append("1С не настроена (onec.base_url)")

            if writes:
                with db_lock():
                    try:
                        for src, table, rows, date_field in writes:
                            replace_rows(con, table, rows, emps=logins, date_field=date_field,
                                         date_from=date_from.isoformat(), commit=False)
                        if any(table == "clients" for _, table, _, _ in writes):
                            snapshot_clients(con, current_month())
                        con.commit()
                    except Exception as e:  # noqa
                        con.rollback()
                        status = "error"
                        log.append(f"Ошибка записи в базу, данные не изменены: {e}")
                        traceback.print_exc()
                    else:
                        log += [f"{src} → {table}: {len(rows)}" for src, table, rows, _ in writes]
    finally:
        finished = dt.datetime.now().isoformat(timespec="seconds")
        with db_lock():
            con.execute("INSERT INTO sync_log (started, finished, status, message) VALUES (?,?,?,?)",
                        (started, finished, status, "\n".join(log)))
            con.commit()
        _running.release()
    return {"status": status, "message": "\n".join(log), "finished": finished}


def snapshot_clients(con, month):
    """Запомнить состояние клиентов на месяц: прошлые месяцы KPI «7 активных клиентов» считаются по снимку,
    а не по текущему состоянию CRM, которое со временем меняется."""
    con.execute("DELETE FROM clients_monthly WHERE month=?", (month,))
    con.execute("INSERT INTO clients_monthly (month, id, emp, name, need, strategy) "
                "SELECT ?, id, emp, name, need, strategy FROM clients", (month,))


def last_sync(con):
    r = con.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def start_scheduler(con, config):
    minutes = config.get("sync", {}).get("interval_minutes", 60)
    if config.get("mode", "demo") == "demo" or not minutes:
        return

    def loop():
        import time
        while True:
            time.sleep(minutes * 60)
            try:
                run(con, config)
            except Exception:  # noqa
                traceback.print_exc()

    threading.Thread(target=loop, daemon=True).start()
