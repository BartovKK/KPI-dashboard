"""Расчёт KPI по правилам из kpi_rules.json на нормализованных данных SQLite."""
import datetime as dt

from .common import (business_days_between, month_add, month_bounds, parse_date,
                     quarter_months, today, weeks_of_month, working_days)

SRC_B24, SRC_1C, SRC_MAN = "Битрикс24", "1С", "Оценка руководителя"
CRITERIA_TITLES = {"docs": "Документооборот", "tasks": "Задачи в Битрикс24", "quality": "Качество документации"}
EPS = 1e-9


def _in(d, first, last):
    d = parse_date(d)
    return d is not None and first <= d <= last


def comp(name, fact, target, unit="", op=">=", source=None, informative=False, pace=None, fmt=None):
    """Компонент показателя: факт против нормы. pct — степень выполнения (1.0 = норма выполнена)."""
    pct = None
    if fact is not None and target is not None:
        if op == ">=":
            pct = 1.0 if target <= 0 else fact / target
        else:  # "<=" — чем меньше, тем лучше
            if fact <= target + EPS:
                pct = 1.0
            elif target <= 0:
                pct = 0.0
            else:
                pct = target / fact
    return {"name": name, "fact": fact, "target": target, "unit": unit, "op": op, "pct": pct,
            "source": source, "informative": informative, "pace": pace, "fmt": fmt}


class Engine:
    def __init__(self, con, rules, config):
        self.con = con
        self.rules = rules
        self.config = config
        self.holidays = config.get("holidays", [])
        self.min_call = config.get("bitrix24", {}).get("min_call_seconds", 120)

    # ------------------------------------------------------------------ helpers
    def q(self, sql, *args):
        return [dict(r) for r in self.con.execute(sql, args)]

    def employees(self):
        return [e for e in self.config["employees"] if e.get("role") != "manager" and e.get("position")]

    def employee(self, login):
        for e in self.config["employees"]:
            if e["login"] == login:
                return e
        return None

    def _ctx(self, month):
        first, last = month_bounds(month)
        t = today()
        asof = min(last, t)
        wd_total = working_days(first, last, self.holidays)
        wd_elapsed = working_days(first, asof, self.holidays) if asof >= first else 0
        in_progress = first <= t <= last
        return {"month": month, "first": first, "last": last, "asof": asof, "wd": wd_total,
                "wd_elapsed": wd_elapsed, "in_progress": in_progress, "future": first > t}

    def _pace(self, ctx, target):
        if not ctx["in_progress"] or target is None or not ctx["wd"]:
            return None
        return round(target * ctx["wd_elapsed"] / ctx["wd"], 1)

    def _result_calls(self, emp, ctx):
        rows = self.q("SELECT * FROM activities WHERE emp=? AND kind='call' AND dt>=? AND dt<=?",
                      emp, ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        out = [r for r in rows if r["direction"] == "out"]
        long_ = [r for r in out if (r["duration"] or 0) >= self.min_call]
        not_secr = [r for r in long_ if not r["secretary"]]
        lpr = [r for r in not_secr if r["is_lpr"]]
        result = [r for r in lpr if r["next_step"]]
        funnel = [("Исходящие звонки", len(out)), (f"От {self.min_call // 60} мин", len(long_)),
                  ("Не секретарь", len(not_secr)), ("С ЛПР", len(lpr)), ("Зафиксирован след. шаг", len(result))]
        return result, funnel

    def _meetings(self, emp, ctx, need_protocol=False):
        rows = self.q("SELECT * FROM activities WHERE emp=? AND kind='meeting' AND status='done' AND dt>=? AND dt<=?",
                      emp, ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        if need_protocol:
            return [r for r in rows if r["has_protocol"] and r["has_summary"]], rows
        return rows, rows

    def _manual(self, emp, month, criterion):
        weeks = weeks_of_month(month)
        rows = self.q(f"SELECT * FROM manual_scores WHERE emp=? AND criterion=? AND week IN ({','.join('?' * len(weeks))})",
                      emp, criterion, *weeks)
        by_week = {r["week"]: r for r in rows}
        vals = [r["score"] for r in rows if r["score"] is not None]
        avg = sum(vals) / len(vals) if vals else None
        return avg, [{"week": w, "score": by_week.get(w, {}).get("score"), "comment": by_week.get(w, {}).get("comment")}
                     for w in weeks]

    # ------------------------------------------------------------------ calculators
    def calc_conversion_tender(self, e, ctx, p):
        rows = self.q("SELECT * FROM tenders WHERE emp=? AND in_work=1 AND price_review=1 AND result IN ('won','lost')", e["login"])

        def conv(first, last):
            sel = [r for r in rows if _in(r["result_date"], first, last)]
            won = [r for r in sel if r["result"] == "won"]
            return (len(won) / len(sel) if sel else None), sel, won

        val, sel, won = conv(ctx["first"], ctx["last"])
        qm = quarter_months(ctx["month"])
        qv, qsel, qwon = conv(month_bounds(qm[0])[0], month_bounds(qm[-1])[1])
        all_m = self.q("SELECT COUNT(*) c FROM tenders WHERE emp=? AND created>=? AND created<=?",
                       e["login"], ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")[0]["c"]
        return {
            "components": [comp("Конверсия в победу", val, p["target"], "%", source=SRC_B24)],
            "extra": [
                {"label": "Торгов с итогом (прошли «В работе» и рассмотрение ЦП)", "value": len(sel)},
                {"label": "Выиграно", "value": len(won)},
                {"label": "Конверсия за квартал", "value": qv, "fmt": "pct",
                 "hint": f"{len(qwon)} из {len(qsel)}"},
                {"label": "Новых торгов в воронке за месяц", "value": all_m},
            ],
            "details": {"columns": ["Торги", "Заказчик", "Итог", "Дата итога", "Цена, ₽"],
                        "rows": [[r["title"], r["client"], "Победа" if r["result"] == "won" else "Проигрыш",
                                  r["result_date"], r["price"]] for r in sel]},
            "funnel": None,
        }

    def calc_base_development(self, e, ctx, p):
        emp = e["login"]
        result_calls, call_funnel = self._result_calls(emp, ctx)
        meetings, _ = self._meetings(emp, ctx)
        comps, extra = [], []
        if "touches" in p:
            t = p["touches"].get(ctx["month"], p["touches"]["default"])
            touches = len(result_calls) + len([m for m in meetings if m["is_lpr"]])
            comps.append(comp("Результативные касания", touches, t, "шт", source=SRC_B24, pace=self._pace(ctx, t)))
        if "result_calls_per_day" in p:
            t = p["result_calls_per_day"] * ctx["wd"]
            comps.append(comp(f"Результативные звонки ({p['result_calls_per_day']} в день × {ctx['wd']} раб. дн.)",
                              len(result_calls), t, "шт", source=SRC_B24, pace=self._pace(ctx, t)))
        if "meetings_min" in p:
            comps.append(comp("Встречи / ВКС", len(meetings), p["meetings_min"], "шт", source=SRC_B24,
                              pace=self._pace(ctx, p["meetings_min"])))
        if "kp_out_of_tender" in p:
            n = self.q("SELECT COUNT(*) c FROM kp WHERE emp=? AND out_of_tender=1 AND dt>=? AND dt<=?",
                       emp, ctx["first"].isoformat(), ctx["last"].isoformat())[0]["c"]
            comps.append(comp("КП вне тендера", n, p["kp_out_of_tender"], "шт", source=SRC_1C,
                              pace=self._pace(ctx, p["kp_out_of_tender"])))
        if "active_clients" in p:
            rows = self.q("SELECT * FROM clients WHERE emp=? AND need=1 AND strategy=1 AND updated<=?",
                          emp, ctx["last"].isoformat() + "T23:59:59")
            comps.append(comp("Активные клиенты с потребностью и стратегией", len(rows), p["active_clients"], "шт",
                              source=SRC_B24))
            extra.append({"label": "Активные клиенты", "value": ", ".join(r["name"] for r in rows[:12]) or "—"})
        return {
            "components": comps, "extra": extra,
            "funnel": {"title": "Как отбираются результативные звонки", "steps": call_funnel},
            "details": {"columns": ["Дата", "Клиент", "Длительность, мин", "Тема"],
                        "rows": [[r["dt"][:16].replace("T", " "), r["client"], round((r["duration"] or 0) / 60, 1),
                                  r["subject"]] for r in result_calls[:200]]},
        }

    def calc_tender_operations(self, e, ctx, p):
        emp = e["login"]
        rows = self.q("SELECT * FROM tenders WHERE emp=? AND submitted_date>=? AND submitted_date<=?",
                      emp, ctx["first"].isoformat(), ctx["last"].isoformat())
        quality, weeks = self._manual(emp, ctx["month"], "quality")
        return {
            "components": [
                comp("Подано заявок", len(rows), p["applications_norm"], "шт", source=SRC_B24,
                     pace=self._pace(ctx, p["applications_norm"])),
                comp("Качество документации", quality, p.get("quality_min", 100), "балл", source=SRC_MAN),
            ],
            "manual": {"criteria": ["quality"], "weeks": {"quality": weeks}},
            "details": {"columns": ["Торги", "Заказчик", "Заявка подана", "Цена, ₽"],
                        "rows": [[r["title"], r["client"], r["submitted_date"], r["price"]] for r in rows]},
        }

    def calc_tender_economics(self, e, ctx, p):
        rows = self.q("SELECT * FROM tenders WHERE emp=? AND result='won' AND result_date>=? AND result_date<=?",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat())
        margins, det, below = [], [], []
        for r in rows:
            cost = r["actual_cost"] or r["planned_cost"]
            m = (r["price"] - cost) / r["price"] if r["price"] and cost is not None else None
            if m is not None:
                margins.append(m)
            if r["planned_cost"] and r["price"] is not None and r["price"] < r["planned_cost"]:
                below.append(r)
            det.append([r["title"], r["client"], r["result_date"], r["price"], cost, m])
        avg = sum(margins) / len(margins) if margins else None
        res = {
            "components": [comp("Средняя маржинальность выигранных проектов", avg, p["min_margin"], "%", source=SRC_B24)],
            "extra": [{"label": "Выиграно проектов", "value": len(rows)},
                      {"label": "Ниже плановой себестоимости", "value": len(below)}],
            "details": {"columns": ["Проект", "Заказчик", "Дата победы", "Цена, ₽", "Себестоимость, ₽", "Маржа"],
                        "rows": det, "pct_cols": [5]},
        }
        if below and p.get("zero_all_if_below_cost"):
            res["zero_all"] = "Сделка выиграна ниже плановой себестоимости: " + ", ".join(r["title"] for r in below) + \
                              ". По регламенту — коэффициент 0 ко всему KPI."
        return res

    def calc_exec_discipline(self, e, ctx, p):
        emp = e["login"]
        comps, weeks_all = [], {}
        for c in p.get("criteria", ["docs", "tasks"]):
            avg, weeks = self._manual(emp, ctx["month"], c)
            weeks_all[c] = weeks
            comps.append(comp(CRITERIA_TITLES[c], avg, 100, "балл", source=SRC_MAN))
        tasks = self.q("SELECT * FROM tasks WHERE emp=? AND deadline>=? AND deadline<=?",
                       emp, ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        due = [t for t in tasks if parse_date(t["deadline"]) <= ctx["asof"]]
        on_time = [t for t in due if t["closed"] and t["closed"][:19] <= t["deadline"][:19]]
        share = len(on_time) / len(due) if due else None
        comps.append(comp("Задачи Б24 закрыты в срок (справочно)", share, 1.0, "%", source=SRC_B24, informative=True))
        overdue = [t for t in due if t not in on_time]
        return {
            "components": comps,
            "manual": {"criteria": p.get("criteria", ["docs", "tasks"]), "weeks": weeks_all},
            "extra": [{"label": "Задач со сроком в месяце", "value": len(tasks)},
                      {"label": "Просрочено / закрыто с опозданием", "value": len(overdue)}],
            "details": {"columns": ["Задача", "Срок", "Закрыта"],
                        "rows": [[t["title"], (t["deadline"] or "")[:10], (t["closed"] or "не закрыта")[:10]] for t in overdue]},
        }

    def calc_tender_receivables(self, e, ctx, p):
        emp = e["login"]
        g = self.q("SELECT * FROM guarantees WHERE emp=?", emp)
        contract = [r for r in g if r["kind"] == "contract" and _in(r["return_date"], ctx["first"], ctx["last"])]
        days = [(parse_date(r["return_date"]) - parse_date(r["acts_date"])).days for r in contract if r["acts_date"]]
        avg_days = sum(days) / len(days) if days else None
        apps = [r for r in g if r["kind"] == "application" and _in(r["unblock_date"], ctx["first"], ctx["last"])]
        ok = [r for r in apps if r["return_date"] and
              business_days_between(parse_date(r["unblock_date"]), parse_date(r["return_date"]), self.holidays) <= 1]
        share = len(ok) / len(apps) if apps else None
        pen = self.q("SELECT * FROM penalties WHERE emp=? AND dt>=? AND dt<=?", emp, ctx["first"].isoformat(), ctx["last"].isoformat())
        rows = [["Обеспечение контракта", r["ref"], r["amount"], r["acts_date"], r["return_date"],
                 f"{(parse_date(r['return_date']) - parse_date(r['acts_date'])).days} дн."] for r in contract if r["acts_date"]]
        rows += [["Обеспечение заявки", r["ref"], r["amount"], r["unblock_date"], r["return_date"] or "не возвращено",
                  "в срок" if r in ok else "просрочено"] for r in apps]
        rows += [["Штраф", r["reason"], r["amount"], r["dt"], "", ""] for r in pen]
        return {
            "components": [
                comp("Среднее время возврата обеспечения контракта", avg_days, p["contract_return_days_max"], "дн", op="<=", source=SRC_1C),
                comp("Обеспечения заявок возвращены за 1 рабочий день", share, p["unblock_share_min"], "%", source=SRC_1C),
                comp("Штрафы за нарушение фин. дисциплины", len(pen), p["penalties_max"], "шт", op="<=", source=SRC_1C),
            ],
            "details": {"columns": ["Тип", "Основание", "Сумма, ₽", "Дата события", "Возврат", "Итог"], "rows": rows},
        }

    def calc_conversion_sales(self, e, ctx, p):
        emp = e["login"]
        rows = self.q("SELECT * FROM deals WHERE emp=? AND result IN ('won','lost') AND closed_date>=? AND closed_date<=?",
                      emp, ctx["first"].isoformat(), ctx["last"].isoformat())
        won = [r for r in rows if r["result"] == "won"]
        val = len(won) / len(rows) if rows else None
        base = self.q("SELECT value FROM baselines WHERE emp=? AND metric='conversion_last_year'", emp)
        if base and base[0]["value"]:
            target = base[0]["value"] * (1 + p.get("growth_vs_last_year", 0.1))
            rule = f"Базовый уровень прошлого года {base[0]['value'] * 100:.1f}% + {p.get('growth_vs_last_year', 0.1) * 100:.0f}%"
        else:
            target = p["target"]
            rule = "Базовый уровень прошлого года не задан — используется целевая конверсия из регламента"
        return {
            "components": [comp("Конверсия сделок в успех", val, target, "%", source=SRC_B24)],
            "extra": [{"label": "Закрыто сделок", "value": len(rows)}, {"label": "Успешных", "value": len(won)},
                      {"label": "Как задана цель", "value": rule}],
            "details": {"columns": ["Сделка", "Клиент", "Итог", "Закрыта", "Сумма, ₽"],
                        "rows": [[r["title"], r["client"], "Успех" if r["result"] == "won" else "Провал", r["closed_date"], r["amount"]]
                                 for r in rows]},
        }

    def calc_gross_profit(self, e, ctx, p):
        emp = e["login"]
        rows = self.q("SELECT * FROM sales WHERE emp=? AND dt>=? AND dt<=?", emp, ctx["first"].isoformat(), ctx["last"].isoformat())
        signed = [r for r in rows if r["signed"]]
        gp = sum((r["revenue"] or 0) - (r["cost"] or 0) for r in signed)
        pending = sum((r["revenue"] or 0) - (r["cost"] or 0) for r in rows if not r["signed"])
        plan_row = self.q("SELECT value FROM plans WHERE emp=? AND month=? AND metric='gross_profit'", emp, ctx["month"])
        plan = plan_row[0]["value"] if plan_row else p.get("default_plan")
        return {
            "components": [comp("Валовая прибыль по подписанным УПД/актам", gp, plan, "₽", source=SRC_1C,
                                pace=self._pace(ctx, plan))],
            "plan": {"metric": "gross_profit", "value": plan, "is_default": not plan_row},
            "extra": [{"label": "Выручка (подписано)", "value": sum(r["revenue"] or 0 for r in signed), "fmt": "rub"},
                      {"label": "Ожидают подписания (ВП)", "value": pending, "fmt": "rub",
                       "hint": "не засчитывается до подписания обеими сторонами"}],
            "details": {"columns": ["Документ", "Клиент", "Дата", "Выручка, ₽", "Себестоимость, ₽", "ВП, ₽", "Подписан"],
                        "rows": [[r["doc"], r["client"], r["dt"], r["revenue"], r["cost"], (r["revenue"] or 0) - (r["cost"] or 0),
                                  "да" if r["signed"] else "нет"] for r in rows]},
        }

    def calc_qualified_leads(self, e, ctx, p):
        rows = self.q("SELECT * FROM leads WHERE emp=? AND qualified_date>=? AND qualified_date<=?",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat() + "T23:59:59")
        names = [("need", "потребность"), ("budget", "бюджет и сроки"), ("lpr", "ЛПР"), ("solvency", "платёжеспособность"),
                 ("next_step", "следующий шаг")]
        good, det = [], []
        for r in rows:
            miss = [n for k, n in names if not r[k]]
            if not miss:
                good.append(r)
            det.append([r["title"], (r["qualified_date"] or "")[:10], "целевой" if not miss else "не заполнено: " + ", ".join(miss)])
        return {
            "components": [comp("Новые целевые лиды", len(good), p["min_leads"], "шт", source=SRC_B24,
                                pace=self._pace(ctx, p["min_leads"]))],
            "extra": [{"label": "Лидов прошли квалификацию", "value": len(rows)},
                      {"label": "Не признаны целевыми (не заполнены поля)", "value": len(rows) - len(good)}],
            "details": {"columns": ["Лид", "Квалифицирован", "Статус"], "rows": det},
        }

    def calc_operational_activity(self, e, ctx, p):
        emp = e["login"]
        result_calls, call_funnel = self._result_calls(emp, ctx)
        kp_all = self.q("SELECT * FROM kp WHERE emp=? AND dt>=? AND dt<=?", emp, ctx["first"].isoformat(), ctx["last"].isoformat())
        kp_ok = [k for k in kp_all if k["confirmed"]]
        meetings, meetings_all = self._meetings(emp, ctx, need_protocol=True)
        kp_target = p["kp_per_day"] * ctx["wd"]
        return {
            "components": [
                comp("Результативные исходящие звонки", len(result_calls), p["result_calls_month"], "шт", source=SRC_B24,
                     pace=self._pace(ctx, p["result_calls_month"])),
                comp(f"КП в 1С со сканом письма ({p['kp_per_day']} в день × {ctx['wd']} раб. дн.)", len(kp_ok), kp_target, "шт",
                     source=SRC_1C, pace=self._pace(ctx, kp_target)),
                comp("Встречи с протоколом и резюме", len(meetings), p["meetings_min"], "шт", source=SRC_B24,
                     pace=self._pace(ctx, p["meetings_min"])),
            ],
            "funnel": {"title": "Как отбираются результативные звонки", "steps": call_funnel},
            "extra": [{"label": "КП без подтверждения отправки", "value": len(kp_all) - len(kp_ok)},
                      {"label": "Встречи без протокола/резюме", "value": len(meetings_all) - len(meetings)}],
            "details": {"columns": ["Дата", "Клиент", "Тип", "Комментарий"],
                        "rows": [[m["dt"][:10], m["client"], "Встреча", "нет протокола или резюме"]
                                 for m in meetings_all if m not in meetings]},
        }

    def calc_receivables_pm(self, e, ctx, p):
        rows = self.q("SELECT * FROM receivables WHERE emp=?", e["login"])
        due_in = [r for r in rows if _in(r["due_date"], ctx["first"], min(ctx["last"], ctx["asof"]))]
        on_time = [r for r in due_in if r["paid_date"] and parse_date(r["paid_date"]) <= parse_date(r["due_date"])]
        share = len(on_time) / len(due_in) if due_in else None
        # максимальная просрочка на конец месяца (или на сегодня) по всем документам
        worst, det = 0, []
        for r in rows:
            due = parse_date(r["due_date"])
            if not due or due > ctx["asof"]:
                continue
            paid = parse_date(r["paid_date"])
            if paid and paid < ctx["first"]:
                continue
            end = paid if paid and paid <= ctx["asof"] else ctx["asof"]
            od = (end - due).days
            if od > 0:
                worst = max(worst, od)
                det.append([r["doc"], r["client"], r["amount"], r["due_date"], r["paid_date"] or "не оплачено", od])
        det.sort(key=lambda x: -x[5])
        res = {
            "components": [
                comp("Оплачено в срок (по отгрузкам со сроком в месяце)", share, 1.0, "%", source=SRC_1C),
                comp("Максимальная просрочка", worst, p["overdue_days_max"], "дн", op="<=", source=SRC_1C),
            ],
            "details": {"columns": ["Документ", "Клиент", "Сумма, ₽", "Срок оплаты", "Оплачено", "Просрочка, дн"], "rows": det},
        }
        if worst > p["overdue_days_max"] and p.get("zero_all_if_overdue"):
            res["zero_all"] = f"Просрочка дебиторской задолженности {worst} дн. (более {p['overdue_days_max']}). " \
                              f"По регламенту KPI не выплачивается."
        return res

    def calc_on_time_delivery(self, e, ctx, p):
        rows = self.q("SELECT * FROM supplier_orders WHERE emp=? AND planned_date>=? AND planned_date<=?",
                      e["login"], ctx["first"].isoformat(), min(ctx["last"], ctx["asof"]).isoformat())
        det, ok = [], 0
        for r in rows:
            reasons = []
            if not r["actual_date"]:
                reasons.append("не поставлено")
            elif r["actual_date"] > r["planned_date"]:
                reasons.append(f"опоздание {(parse_date(r['actual_date']) - parse_date(r['planned_date'])).days} дн.")
            if r["actual_date"] and not r["complete"]:
                reasons.append("недопоставка")
            if r["defect"]:
                reasons.append("брак")
            if r["mis_sort"]:
                reasons.append("пересортица")
            if not reasons:
                ok += 1
            else:
                det.append([r["doc"], r["supplier"], r["planned_date"], r["actual_date"] or "—", ", ".join(reasons)])
        late_share = (len(rows) - ok) / len(rows) if rows else None
        return {
            "components": [comp("Доля просроченных поставок", late_share, p["late_share_max"], "%", op="<=", source=SRC_1C)],
            "extra": [{"label": "Поставок с плановой датой в месяце", "value": len(rows)},
                      {"label": "Вовремя и в полном объёме", "value": ok}],
            "details": {"columns": ["Заказ поставщику", "Поставщик", "План", "Факт (УПД/ТН)", "Нарушение"], "rows": det},
        }

    def calc_deferred_payment(self, e, ctx, p):
        rows = self.q("SELECT * FROM supplier_orders WHERE emp=? AND dt>=? AND dt<=? AND amount_novat>=?",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat(), p["threshold"])
        good = [r for r in rows if (r["deferred_share"] or 0) >= p["deferred_share_min"] - EPS
                and (r["deferral_days"] or 0) >= p["deferral_days_min"]]
        if p.get("basis") == "amount":
            tot = sum(r["amount_novat"] for r in rows)
            share = sum(r["amount_novat"] for r in good) / tot if tot else None
        else:
            share = len(good) / len(rows) if rows else None
        return {
            "components": [comp("Доля заказов с отсрочкой", share, p["target"], "%", source=SRC_1C)],
            "extra": [{"label": f"Заказов от {p['threshold']:,.0f} ₽ без НДС".replace(",", " "), "value": len(rows)},
                      {"label": "С отсрочкой ≥ 30% суммы на ≥ 14 дней", "value": len(good)}],
            "details": {"columns": ["Заказ", "Поставщик", "Сумма без НДС, ₽", "Доля отсрочки", "Дней отсрочки", "Засчитан"],
                        "rows": [[r["doc"], r["supplier"], r["amount_novat"], r["deferred_share"], r["deferral_days"],
                                  "да" if r in good else "нет"] for r in rows], "pct_cols": [3]},
        }

    def calc_competitive_purchases(self, e, ctx, p):
        rows = self.q("SELECT * FROM supplier_orders WHERE emp=? AND dt>=? AND dt<=?",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat())
        lots = {}
        for r in rows:
            lots.setdefault(r["lot"] or r["doc"], []).append(r)
        det, base, good, excluded = [], 0, 0, 0
        for lot, items in lots.items():
            total = sum(i["amount_novat"] or 0 for i in items)
            if total < p["threshold"]:
                continue
            h = items[0]
            if h["single_source"] or h["emergency"]:
                excluded += 1
                det.append([lot, h["supplier"], total, h["kp_count"], "исключена: " + ("монополия" if h["single_source"] else "аварийная")])
                continue
            base += 1
            reasons = []
            if (h["kp_count"] or 0) < p["kp_min"]:
                reasons.append(f"КП: {h['kp_count'] or 0} из {p['kp_min']}")
            if not h["relevant"]:
                reasons.append("КП не соответствуют ТЗ")
            if not h["evidence"]:
                reasons.append("нет сканов/сравнительной таблицы")
            if h["affiliated"]:
                reasons.append("аффилированность")
            if not reasons:
                good += 1
                status = "конкурентная"
            elif h["justified"]:
                good += 1
                status = "без конкурса, есть обоснование"
            else:
                status = "; ".join(reasons)
            det.append([lot, h["supplier"], total, h["kp_count"], status])
        share = good / base if base else None
        return {
            "components": [comp("Доля конкурентных закупок", share, p["target"], "%", source=SRC_1C)],
            "extra": [{"label": f"Лотов от {p['threshold']:,.0f} ₽ в базе расчёта".replace(",", " "), "value": base},
                      {"label": "Засчитано", "value": good}, {"label": "Исключено (монополия, авария)", "value": excluded}],
            "details": {"columns": ["Лот / заказ", "Поставщик", "Сумма без НДС, ₽", "КП", "Итог"], "rows": det},
        }

    def calc_supply_funnel(self, e, ctx, p):
        rows = self.q("SELECT * FROM supply_funnel WHERE emp=? AND dt>=? AND dt<=?",
                      e["login"], ctx["first"].isoformat(), ctx["last"].isoformat())
        order = ["need", "search", "kp", "contract", "delivered"]
        titles = ["Потребность (заказ клиента)", "Поиск и квалификация поставщиков", "КП получены", "Договор подписан", "Поставка"]
        idx = {s: i for i, s in enumerate(order)}
        steps = [(titles[i], len([r for r in rows if idx.get(r["stage"], 0) >= i])) for i in range(len(order))]
        val = steps[-1][1] / steps[0][1] if steps[0][1] else None
        return {
            "components": [comp("Конверсия: потребность → поставка", val, p["target"], "%", source=SRC_1C)],
            "funnel": {"title": "Воронка снабжения по заказам клиентов", "steps": steps},
            "details": {"columns": ["Заказ клиента", "Клиент", "Дата", "Достигнутый этап"],
                        "rows": [[r["customer_order"], r["client"], r["dt"], titles[idx.get(r["stage"], 0)]] for r in rows]},
        }

    # ------------------------------------------------------------------ assembly
    def compute(self, login, month):
        e = self.employee(login)
        pos = self.rules["positions"][e["position"]]
        ctx = self._ctx(month)
        payout_cfg = self.rules.get("payout", {})
        bonus = pos["kpi_bonus"]
        items, zero_all = [], []
        for it in pos["items"]:
            fn = getattr(self, "calc_" + it["code"])
            r = fn(e, ctx, it.get("params", {}))
            comps = r["components"]
            scored = [c for c in comps if not c["informative"]]
            pcts = [c["pct"] for c in scored if c["pct"] is not None]
            missing = [c for c in scored if c["pct"] is None]
            pct = min(pcts) if pcts else None
            amount = round(bonus * it["weight"])
            awaiting_manual = any(c["source"] == SRC_MAN for c in missing)
            if pct is None:
                status = "awaiting" if awaiting_manual else "nodata"
                payout = amount if payout_cfg.get("pay_when_no_data") else 0
            else:
                met = pct >= 1 - EPS
                if payout_cfg.get("mode") == "proportional":
                    payout = amount * min(pct, 1) if pct >= payout_cfg.get("min_pct", 0.8) - EPS else 0
                else:
                    payout = amount if met else 0
                status = "met" if met else ("close" if pct >= 0.8 else "fail")
                if missing:
                    status = "awaiting" if awaiting_manual else status
            if ctx["in_progress"] and status in ("fail", "close", "nodata"):
                status = "progress_" + status
            if r.get("zero_all"):
                zero_all.append(r["zero_all"])
            items.append({**{k: it[k] for k in ("code", "n", "title", "weight", "period", "target_text", "description")},
                          "amount": amount, "pct": pct, "status": status, "payout": round(payout),
                          **{k: v for k, v in r.items() if k != "zero_all"}})
        total = sum(i["payout"] for i in items)
        if zero_all:
            total = 0
        return {
            "login": login, "name": e["name"], "position": e["position"], "position_title": pos["title"],
            "department": pos["department"], "salary": pos["salary"], "kpi_bonus": bonus, "month": month,
            "working_days": ctx["wd"], "working_days_elapsed": ctx["wd_elapsed"], "in_progress": ctx["in_progress"],
            "payout_mode": payout_cfg.get("mode", "binary"),
            "items": items, "zero_all": zero_all, "payout": total, "payout_before_zero": sum(i["payout"] for i in items),
            "share": total / bonus if bonus else 0,
            "met_count": len([i for i in items if i["status"] == "met"]), "item_count": len(items),
        }

    def summary(self, login, month):
        r = self.compute(login, month)
        return {"month": month, "payout": r["payout"], "share": r["share"],
                "items": [{"n": i["n"], "pct": i["pct"], "status": i["status"], "payout": i["payout"]} for i in r["items"]]}

    def trend(self, login, month, n=6):
        return [self.summary(login, month_add(month, -k)) for k in range(n - 1, -1, -1)]

    def overview(self, month):
        groups = {}
        alerts = []
        for e in self.employees():
            r = self.compute(e["login"], month)
            r["trend"] = [{"month": t["month"], "share": t["share"], "payout": t["payout"]} for t in self.trend(e["login"], month)]
            for i in r["items"]:
                i.pop("details", None)
                i.pop("funnel", None)
            groups.setdefault(e["position"], []).append(r)
            for z in r["zero_all"]:
                alerts.append({"login": e["login"], "name": e["name"], "level": "critical", "text": z})
            for i in r["items"]:
                if i["status"] == "awaiting":
                    alerts.append({"login": e["login"], "name": e["name"], "level": "info",
                                   "text": f"KPI {i['n']} «{i['title']}»: нет оценки руководителя за месяц"})
        out = []
        for key, pos in self.rules["positions"].items():
            if key in groups:
                out.append({"position": key, "title": pos["title"], "department": pos["department"],
                            "columns": [{"n": i["n"], "title": i["title"], "weight": i["weight"]} for i in pos["items"]],
                            "employees": groups[key]})
        all_emps = [x for g in out for x in g["employees"]]
        return {
            "month": month,
            "groups": out,
            "alerts": alerts,
            "totals": {
                "fund": sum(x["kpi_bonus"] for x in all_emps),
                "payout": sum(x["payout"] for x in all_emps),
                "items": sum(x["item_count"] for x in all_emps),
                "items_met": sum(x["met_count"] for x in all_emps),
                "full": len([x for x in all_emps if x["share"] >= 1 - EPS]),
                "employees": len(all_emps),
            },
        }
