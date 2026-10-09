"""Тесты расчёта KPI. Запуск из папки kpi-dashboard: python -m unittest discover tests"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import demo, sync  # noqa: E402
from app.common import (connect, load_json, load_rules, month_add, month_bounds, replace_rows,  # noqa: E402
                        weeks_of_month, working_days)
from app.engine import Engine  # noqa: E402

TODAY = dt.date(2026, 10, 20)
EXAMPLE = load_json("config.example.json")
CONFIG = {
    "holidays": EXAMPLE["holidays"],
    "employees": [
        {"login": "boss", "name": "Руководитель", "role": "manager"},
        {"login": "lead", "name": "Ведущий", "position": "tender_lead", "role": "employee"},
        {"login": "tender", "name": "Тендерный", "position": "tender", "role": "employee"},
        {"login": "pm", "name": "ПМ", "position": "pm", "role": "employee"},
        {"login": "supply", "name": "Снабженец", "position": "supply", "role": "employee"},
    ],
}


def insert(con, table, rows):
    replace_rows(con, table, rows, emps=["__none__"])


def call(i, emp, day, lpr=1, secretary=0, next_step=1, duration=180):
    return {"id": f"c{emp}{i}", "emp": emp, "dt": f"{day}T10:00:00", "kind": "call", "direction": "out",
            "duration": duration, "is_lpr": lpr, "secretary": secretary, "next_step": next_step, "status": "done"}


def order(i, dt_, amount, **kw):
    row = {"id": f"o{i}", "emp": "supply", "doc": f"ЗП-{i}", "dt": dt_, "supplier": "Поставщик", "lot": None,
           "amount_novat": amount, "kp_count": 3, "relevant": 1, "evidence": 1, "affiliated": 0, "single_source": 0,
           "emergency": 0, "justified": 0, "deferred_share": 0, "deferral_days": 0, "customer_order": None}
    row.update(kw)
    return row


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.rules = load_rules()
        self.engine = Engine(self.con, self.rules, CONFIG)
        patcher = mock.patch("app.engine.today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def params(self, position, code):
        return next(i["params"] for i in self.rules["positions"][position]["items"] if i["code"] == code)

    def calc(self, login, code, month):
        e = self.engine.employee(login)
        return getattr(self.engine, "calc_" + code)(e, self.engine._ctx(month), self.params(e["position"], code))

    def item(self, login, code, month):
        return next(i for i in self.engine.compute(login, month)["items"] if i["code"] == code)

    # ---------------------------------------------------------------- даты
    def test_each_week_belongs_to_one_month(self):
        months = [month_add("2026-01", k) for k in range(12)]
        weeks = [w for m in months for w in weeks_of_month(m)]
        self.assertEqual(len(weeks), len(set(weeks)))
        self.assertEqual(weeks_of_month("2026-10"), ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"])
        self.assertEqual(weeks_of_month("2026-09")[-1], "2026-09-21")

    def test_working_days_with_transferred_workday(self):
        first, last = month_bounds("2026-11")
        self.assertEqual(working_days(first, last, ["2026-11-04"]), 20)
        self.assertEqual(working_days(first, last, ["2026-11-04"], ["2026-11-07"]), 21)

    # ---------------------------------------------------------------- звонки
    def test_pm_calls_do_not_require_lpr_but_tender_calls_do(self):
        insert(self.con, "activities", [call(1, "pm", "2026-09-10", lpr=0), call(2, "pm", "2026-09-11", lpr=1),
                                        call(3, "pm", "2026-09-11", secretary=1),
                                        call(4, "tender", "2026-09-10", lpr=0), call(5, "tender", "2026-09-11", lpr=1)])
        pm = self.calc("pm", "operational_activity", "2026-09")["components"][0]
        self.assertEqual(pm["fact"], 2)
        tender = self.calc("tender", "base_development", "2026-09")["components"][0]
        self.assertEqual(tender["fact"], 1)

    def test_pm_calls_target_is_per_working_day(self):
        comp = self.calc("pm", "operational_activity", "2026-01")["components"][0]
        wd = self.engine._ctx("2026-01")["wd"]
        self.assertEqual(comp["target"], 15 * wd)
        self.assertLess(comp["target"], 320)

    def test_kp_needs_letter_in_b24(self):
        insert(self.con, "kp", [{"id": f"k{i}", "emp": "pm", "dt": "2026-09-10", "confirmed": 1} for i in range(5)])
        insert(self.con, "kp_letters", [{"id": f"m{i}", "emp": "pm", "dt": "2026-09-10T12:00:00"} for i in range(3)])
        comp = self.calc("pm", "operational_activity", "2026-09")["components"][1]
        self.assertEqual(comp["fact"], 3)

    # ---------------------------------------------------------------- валовая прибыль
    def test_gross_profit_counts_in_month_of_signing(self):
        insert(self.con, "sales", [
            {"id": "s1", "emp": "pm", "dt": "2026-09-25", "doc": "УПД-1", "revenue": 1000, "cost": 600, "signed": 1,
             "signed_date": "2026-10-03"},
            {"id": "s2", "emp": "pm", "dt": "2026-09-05", "doc": "УПД-2", "revenue": 500, "cost": 400, "signed": 1,
             "signed_date": "2026-09-08"},
            {"id": "s3", "emp": "pm", "dt": "2026-09-06", "doc": "УПД-3", "revenue": 900, "cost": 100, "signed": 0},
        ])
        sep = self.calc("pm", "gross_profit", "2026-09")
        self.assertEqual(sep["components"][0]["fact"], 100)
        self.assertEqual(len(sep["details"]["rows"]), 3)  # 1 засчитан + 2 ждут подписи на конец сентября
        self.assertEqual(self.calc("pm", "gross_profit", "2026-10")["components"][0]["fact"], 400)

    # ---------------------------------------------------------------- активные клиенты
    def test_active_clients_past_month_uses_snapshot(self):
        insert(self.con, "clients", [{"id": f"cl{i}", "emp": "lead", "name": f"К{i}", "need": 1, "strategy": 1,
                                      "updated": "2026-10-19T10:00:00"} for i in range(8)])
        self.con.executemany("INSERT INTO clients_monthly VALUES ('2026-09', ?, 'lead', ?, 1, ?)",
                             [(f"cl{i}", f"К{i}", int(i < 5)) for i in range(8)])
        sep = [c for c in self.calc("lead", "base_development", "2026-09")["components"] if "Активные" in c["name"]][0]
        octo = [c for c in self.calc("lead", "base_development", "2026-10")["components"] if "Активные" in c["name"]][0]
        self.assertEqual(sep["fact"], 5)
        self.assertEqual(octo["fact"], 8)

    def test_snapshot_clients(self):
        insert(self.con, "clients", [{"id": "a", "emp": "lead", "name": "А", "need": 1, "strategy": 1}])
        sync.snapshot_clients(self.con, "2026-10")
        sync.snapshot_clients(self.con, "2026-10")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM clients_monthly").fetchone()[0], 1)

    # ---------------------------------------------------------------- снабжение
    def test_lot_spanning_months_counted_once_in_first_month(self):
        insert(self.con, "supplier_orders", [order(1, "2026-08-28", 300000, lot="L1", kp_count=1),
                                             order(2, "2026-09-02", 300000, lot="L1", kp_count=3)])
        aug = self.calc("supply", "competitive_purchases", "2026-08")
        sep = self.calc("supply", "competitive_purchases", "2026-09")
        self.assertEqual(aug["components"][0]["fact"], 1.0)  # 3 КП в лоте, сумма 600 000
        self.assertIsNone(sep["components"][0]["fact"])

    def test_justification_does_not_cover_affiliation(self):
        insert(self.con, "supplier_orders", [order(1, "2026-09-02", 600000, kp_count=1, justified=1),
                                             order(2, "2026-09-03", 600000, affiliated=1, justified=1)])
        r = self.calc("supply", "competitive_purchases", "2026-09")
        self.assertEqual(r["components"][0]["fact"], 0.5)
        self.assertIn("аффилированность", r["details"]["rows"][1][4])

    def test_split_orders_raise_warning(self):
        insert(self.con, "supplier_orders", [order(1, "2026-09-02", 300000, lot="L1"),
                                             order(2, "2026-09-03", 300000, lot="L1"),
                                             order(3, "2026-09-04", 100000, customer_order="ЗК-1"),
                                             order(4, "2026-09-05", 100000, customer_order="ЗК-1")])
        r = self.engine.compute("supply", "2026-09")
        self.assertEqual(len(r["warnings"]), 1)
        alerts = self.engine.overview("2026-09")["alerts"]
        self.assertTrue(any(a["level"] == "warning" and a["login"] == "supply" for a in alerts))

    # ---------------------------------------------------------------- обнуление и выплата
    def test_win_below_planned_cost_zeroes_all_kpi(self):
        insert(self.con, "tenders", [{"id": "t1", "emp": "tender", "title": "Т", "result": "won", "result_date": "2026-09-10",
                                      "price": 100, "planned_cost": 105, "actual_cost": 104, "in_work": 1, "price_review": 1}])
        r = self.engine.compute("tender", "2026-09")
        self.assertTrue(r["zero_all"])
        self.assertEqual(r["payout"], 0)

    def test_pm_overdue_over_14_days_zeroes_all_kpi(self):
        insert(self.con, "receivables", [{"id": "r1", "emp": "pm", "doc": "УПД", "due_date": "2026-09-01",
                                          "paid_date": "2026-09-20", "amount": 1}])
        self.assertTrue(self.engine.compute("pm", "2026-09")["zero_all"])

    def test_proportional_close_threshold_follows_min_pct(self):
        self.rules["payout"] = {"mode": "proportional", "min_pct": 0.9}
        insert(self.con, "leads", [{"id": f"l{i}", "emp": "pm", "qualified_date": "2026-09-10T10:00:00", "need": 1,
                                    "budget": 1, "lpr": 1, "solvency": 1, "next_step": 1} for i in range(8)])
        it = self.item("pm", "qualified_leads", "2026-09")
        self.assertEqual(it["status"], "fail")  # 80% < min_pct 90%
        self.assertEqual(it["payout"], 0)

    # ---------------------------------------------------------------- демо целиком
    def test_demo_overview_runs_for_all_months(self):
        cfg = {**CONFIG, "employees": EXAMPLE["employees"]}
        con = connect(":memory:")
        self.addCleanup(con.close)
        demo.generate(con, cfg)
        engine = Engine(con, self.rules, cfg)
        with mock.patch("app.engine.today", return_value=dt.date.today()):
            cm = f"{dt.date.today():%Y-%m}"
            for k in range(7):
                o = engine.overview(month_add(cm, -k))
                self.assertLessEqual(o["totals"]["payout"], o["totals"]["fund"])
                self.assertEqual(o["totals"]["employees"], 8)


if __name__ == "__main__":
    unittest.main()
