"""Управление дашбордом KPI.

  python manage.py run                 — запустить веб-сервер
  python manage.py sync                — выгрузить данные из Битрикс24 и 1С (или пересоздать демо-данные)
  python manage.py password ЛОГИН      — задать пароль пользователю
  python manage.py check               — проверить подключение к Битрикс24 и 1С
  python manage.py preview             — собрать автономную HTML-страницу с текущими данными (web/preview.html)
"""
import getpass
import sys

sys.stdout.reconfigure(encoding="utf-8")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        from app.server import serve
        serve()
    elif cmd == "sync":
        from app import sync
        from app.common import connect, load_config
        print(sync.run(connect(), load_config())["message"])
    elif cmd == "password":
        from app.common import connect, load_config
        from app.server import set_password
        login = sys.argv[2] if len(sys.argv) > 2 else input("Логин: ")
        if not any(e["login"] == login for e in load_config()["employees"]):
            sys.exit(f"Пользователя {login} нет в config.json → employees")
        pwd = getpass.getpass("Новый пароль: ")
        if len(pwd) < 6:
            sys.exit("Пароль должен быть не короче 6 символов")
        set_password(connect(), login, pwd)
        print("Пароль сохранён")
    elif cmd == "check":
        from app.bitrix24 import Bitrix24
        from app.common import load_config
        cfg = load_config()
        try:
            r = Bitrix24(cfg["bitrix24"]).call("profile")
            print("Битрикс24: OK, вебхук от пользователя", r["result"].get("NAME"), r["result"].get("LAST_NAME"))
            r = Bitrix24(cfg["bitrix24"]).call("crm.category.list", {"entityTypeId": 2})
            print("Воронки сделок:", [(c["id"], c["name"]) for c in r["result"]["categories"]])
        except Exception as e:  # noqa
            print("Битрикс24: ошибка —", e)
        from app.common import today
        from app.onec import OneC
        onec = OneC(cfg["onec"])
        print("1С: проверка", cfg["onec"]["base_url"])
        for name, ds in cfg["onec"].get("datasets", {}).items():
            if name.startswith("_"):
                continue
            try:
                rows = onec._load(ds, today(), today())
                print(f"1С → {name} ({ds.get('source', 'http')}): OK, строк за сегодня: {len(rows)}")
            except Exception as e:  # noqa
                print(f"1С → {name} ({ds.get('source', 'http')}): ошибка — {e}")
    elif cmd == "preview":
        from build_preview import build
        print("Готово:", build())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
