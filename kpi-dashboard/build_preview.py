"""Сборка автономной HTML-страницы (превью) с данными, рассчитанными движком KPI."""
import json
import os

from app.common import ROOT, connect, current_month, load_config, load_rules, month_add
from app.engine import Engine
from app.server import App


def build(months=3, out="web/preview.html"):
    app = App()
    e = Engine(app.con, load_rules(), app.config)
    mgr = next(x for x in app.config["employees"] if x.get("role") == "manager")
    cm = current_month()
    ms = [month_add(cm, -k) for k in range(months)]
    data = {
        "bootstrap": app.bootstrap(mgr),
        "months": ms,
        "default_month": ms[1] if len(ms) > 1 else ms[0],
        "overview": {m: e.overview(m) for m in ms},
        "employee": {x["login"]: {m: app.employee(x["login"], m) for m in ms} for x in e.employees()},
    }
    with open(os.path.join(ROOT, "web/index.html"), encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = html.replace("/* __PREVIEW_DATA__ */", "window.__PREVIEW__ = " + blob + ";", 1)
    path = os.path.join(ROOT, out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


if __name__ == "__main__":
    print(build())
