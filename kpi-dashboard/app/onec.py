"""Загрузка данных из 1С: HTTP-сервис (рекомендуется), стандартный OData-интерфейс или CSV-выгрузка.

Каждый набор данных приводится к полям таблицы дашборда. Ответственный сотрудник указывается
в поле manager (ФИО или GUID из 1С) и сопоставляется с onec_manager в config.json.
"""
import base64
import csv
import json
import os
import ssl
import urllib.parse
import urllib.request

from .common import ROOT, table_columns

# набор данных → таблица дашборда, поле даты для окна синхронизации
DATASETS = {
    "sales": ("sales", "dt"),
    "kp": ("kp", "dt"),
    "receivables": ("receivables", "ship_date"),
    "guarantees": ("guarantees", None),
    "penalties": ("penalties", "dt"),
    "supplier_orders": ("supplier_orders", "dt"),
    "supply_funnel": ("supply_funnel", "dt"),
}
BOOL_FIELDS = {"signed", "out_of_tender", "confirmed", "complete", "defect", "mis_sort", "relevant", "evidence",
               "affiliated", "single_source", "emergency", "justified"}
NUM_FIELDS = {"revenue", "cost", "amount", "amount_novat", "deferred_share", "deferral_days", "kp_count"}
DATE_FIELDS = {"dt", "signed_date", "ship_date", "due_date", "paid_date", "acts_date", "unblock_date", "return_date", "planned_date", "actual_date"}


def _get_path(obj, path):
    for part in path.split("/"):
        if obj is None:
            return None
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def _to_bool(v):
    if isinstance(v, bool):
        return int(v)
    s = str(v or "").strip().lower()
    return 0 if s in ("", "0", "false", "нет", "ложь", "n") else 1


def _to_date(v):
    if not v:
        return None
    s = str(v)
    if s.startswith("0001-01-01"):
        return None  # пустая дата в 1С
    if len(s) >= 10 and s[2] == "." and s[5] == ".":  # 31.12.2026
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
    return s[:10]


def _to_num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


class OneC:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.base = cfg.get("base_url", "").rstrip("/") + "/"
        self.log = log
        self.ctx = None if cfg.get("verify_ssl", True) else ssl._create_unverified_context()

    def _get(self, url):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.cfg.get("user"):
            tok = base64.b64encode(f"{self.cfg['user']}:{self.cfg.get('password', '')}".encode("utf-8")).decode()
            req.add_header("Authorization", "Basic " + tok)
        with urllib.request.urlopen(req, timeout=120, context=self.ctx) as r:
            return json.loads(r.read().decode("utf-8-sig"))

    def _load(self, ds, date_from, date_to):
        src = ds.get("source", "http")
        if src == "http":
            q = urllib.parse.urlencode({"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
            data = self._get(self.base + ds["path"].lstrip("/") + "?" + q)
            return data.get("rows", data) if isinstance(data, dict) else data
        if src == "odata":
            params = {"$format": "json"}
            if ds.get("filter"):
                params["$filter"] = ds["filter"].format(date_from=date_from.isoformat(), date_to=date_to.isoformat())
            if ds.get("expand"):
                params["$expand"] = ds["expand"]
            url = self.base + "odata/standard.odata/" + urllib.parse.quote(ds["entity"]) + "?" + \
                urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            rows = self._get(url).get("value", [])
            m = ds.get("map", {})
            return [{k: _get_path(r, v) for k, v in m.items()} for r in rows]
        if src == "csv":
            path = ds["file"] if os.path.isabs(ds["file"]) else os.path.join(ROOT, ds["file"])
            with open(path, encoding=ds.get("encoding", "utf-8-sig"), newline="") as f:
                return list(csv.DictReader(f, delimiter=ds.get("delimiter", ";")))
        raise ValueError(f"Неизвестный источник {src}")

    def fetch(self, con, managers, date_from, date_to):
        """managers: {ФИО или GUID в 1С (в нижнем регистре): login}."""
        out = {}
        for name, ds in self.cfg.get("datasets", {}).items():
            if name.startswith("_") or name not in DATASETS:
                continue
            table, _ = DATASETS[name]
            self.log(f"1С: {name}")
            raw = self._load(ds, date_from, date_to)
            cols = set(table_columns(con, table))
            rows = []
            for i, r in enumerate(raw):
                login = managers.get(str(r.get("manager") or r.get("emp") or "").strip().lower())
                if not login:
                    continue
                row = {"emp": login}
                for k, v in r.items():
                    if k not in cols or k == "emp":
                        continue
                    if k in BOOL_FIELDS:
                        v = _to_bool(v)
                    elif k in NUM_FIELDS:
                        v = _to_num(v)
                    elif k in DATE_FIELDS:
                        v = _to_date(v)
                    row[k] = v
                row["id"] = f"1c-{name}-{r.get('id') or r.get('doc') or i}"
                rows.append(row)
            out[table] = rows
        return out
