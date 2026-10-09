"""Синхронизация: демо-данные или выгрузка из Битрикс24 и 1С."""
import datetime as dt
import threading
import traceback

from . import demo
from .bitrix24 import Bitrix24
from .common import current_month, db_lock, month_add, month_bounds, replace_rows, today
from .onec import DATASETS, OneC

B24_DATE_FIELDS = {"activities": "dt", "tenders": None, "deals": "closed_date", "leads": "qualified_date",
                   "clients": None, "tasks": "deadline"}

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

            if config.get("bitrix24", {}).get("webhook") and "ВАШ-ПОРТАЛ" not in config["bitrix24"]["webhook"]:
                try:
                    b24 = Bitrix24(config["bitrix24"], log=log.append)
                    data = b24.fetch({int(e["b24_user_id"]): e["login"] for e in emps if e.get("b24_user_id")}, date_from)
                    for table, rows in data.items():
                        replace_rows(con, table, rows, emps=logins, date_field=B24_DATE_FIELDS.get(table),
                                     date_from=date_from.isoformat())
                        log.append(f"Б24 → {table}: {len(rows)}")
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
                        replace_rows(con, table, rows, emps=logins, date_field=date_field, date_from=date_from.isoformat())
                        log.append(f"1С → {table}: {len(rows)}")
                except Exception as e:  # noqa
                    status = "error"
                    log.append(f"Ошибка 1С: {e}")
                    traceback.print_exc()
            else:
                log.append("1С не настроена (onec.base_url)")
    finally:
        finished = dt.datetime.now().isoformat(timespec="seconds")
        with db_lock():
            con.execute("INSERT INTO sync_log (started, finished, status, message) VALUES (?,?,?,?)",
                        (started, finished, status, "\n".join(log)))
            con.commit()
        _running.release()
    return {"status": status, "message": "\n".join(log), "finished": finished}


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
