"""Загрузка данных из Битрикс24 через входящий вебхук (REST API) и нормализация в таблицы дашборда."""
import datetime as dt
import json
import time
import urllib.request

from .common import parse_date

OWNER_TYPES = {"LEAD": 1, "DEAL": 2, "CONTACT": 3, "COMPANY": 4}


class B24Error(Exception):
    pass


class Bitrix24:
    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.url = cfg["webhook"].rstrip("/") + "/"
        self.log = log
        self.f = cfg.get("fields", {})

    # ---------- транспорт
    def call(self, method, params=None):
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(self.url + method + ".json", data=data,
                                     headers={"Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    res = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")
                if "QUERY_LIMIT_EXCEEDED" in body and attempt < 4:
                    time.sleep(2 + attempt * 2)
                    continue
                raise B24Error(f"{method}: HTTP {e.code} {body[:300]}")
        if "error" in res:
            raise B24Error(f"{method}: {res.get('error')} {res.get('error_description', '')}")
        time.sleep(0.4)  # лимит Б24 — около 2 запросов в секунду
        return res

    def list_all(self, method, params, key=None):
        """Постраничная выборка (по 50 записей)."""
        out, start = [], 0
        while True:
            res = self.call(method, {**params, "start": start})
            chunk = res.get("result", [])
            if key:
                chunk = chunk.get(key, [])
            out.extend(chunk)
            if "next" not in res:
                return out
            start = res["next"]

    def by_ids(self, method, ids, select):
        ids = list({int(i) for i in ids if i})
        out = {}
        for i in range(0, len(ids), 50):
            for r in self.list_all(method, {"filter": {"ID": ids[i:i + 50]}, "select": select}):
                out[str(r["ID"])] = r
        return out

    # ---------- утилиты
    @staticmethod
    def flag(v):
        if v is None or v is False:
            return 0
        if isinstance(v, (list, dict)):
            return int(bool(v))
        s = str(v).strip().lower()
        return 0 if s in ("", "n", "0", "нет", "false", "none") else 1

    @staticmethod
    def num(v):
        """Число из поля Б24 (поле типа «Деньги» приходит как «100000|RUB»)."""
        if v in (None, "", False):
            return None
        try:
            return float(str(v).split("|")[0].replace(" ", "").replace(",", "."))
        except ValueError:
            return None

    @staticmethod
    def iso(v):
        if not v:
            return None
        return str(v)[:19]

    # ---------- сбор
    def fetch(self, employees, date_from):
        """employees: {b24_user_id: login}. Возвращает словарь таблица → список строк."""
        uids = [int(u) for u in employees]
        emp = lambda uid: employees.get(int(uid)) if uid not in (None, "") else None
        df = date_from.isoformat()
        df_long = (date_from - dt.timedelta(days=150)).isoformat()
        out = {}

        # активности CRM (встречи, следующие шаги)
        self.log("Б24: дела CRM")
        acts = self.list_all("crm.activity.list", {
            "filter": {"RESPONSIBLE_ID": uids, ">=CREATED": df},
            "select": ["ID", "OWNER_TYPE_ID", "OWNER_ID", "TYPE_ID", "PROVIDER_ID", "CREATED", "START_TIME", "END_TIME",
                       "COMPLETED", "SUBJECT", "DESCRIPTION", "RESPONSIBLE_ID", "STORAGE_ELEMENT_IDS", "FILES", "LAST_UPDATED"],
            "order": {"ID": "ASC"}})
        by_owner = {}
        for a in acts:
            by_owner.setdefault((int(a["OWNER_TYPE_ID"] or 0), str(a["OWNER_ID"])), []).append(a)

        # звонки (телефония)
        self.log("Б24: звонки")
        calls = self.list_all("voximplant.statistic.get", {
            "FILTER": {">=CALL_START_DATE": df, "PORTAL_USER_ID": uids, "CALL_TYPE": 1},
            "SORT": "CALL_START_DATE", "ORDER": "ASC"})
        contact_ids = [c["CRM_ENTITY_ID"] for c in calls if c.get("CRM_ENTITY_TYPE") == "CONTACT"]
        contact_ids += [a["OWNER_ID"] for a in acts if str(a["OWNER_TYPE_ID"]) == "3"]
        lpr_field = self.f.get("contact_is_lpr")
        contacts = self.by_ids("crm.contact.list", contact_ids, ["ID", "POST", "COMPANY_ID"] + ([lpr_field] if lpr_field else []))
        secr = [s.lower() for s in self.cfg.get("secretary_posts", [])]

        def contact_flags(cid):
            c = contacts.get(str(cid))
            if not c:
                return 0, 0
            post = (c.get("POST") or "").lower()
            is_secr = int(any(s in post for s in secr))
            is_lpr = self.flag(c.get(lpr_field)) if lpr_field else int(bool(post) and not is_secr)
            return is_lpr, is_secr

        rows = []
        for c in calls:
            login = emp(c.get("PORTAL_USER_ID"))
            if not login:
                continue
            start = str(c.get("CALL_START_DATE"))[:19]
            is_lpr, is_secr = (0, 0)
            if c.get("CRM_ENTITY_TYPE") == "CONTACT":
                is_lpr, is_secr = contact_flags(c.get("CRM_ENTITY_ID"))
            owner = (OWNER_TYPES.get(c.get("CRM_ENTITY_TYPE"), 0), str(c.get("CRM_ENTITY_ID")))
            t0 = dt.datetime.fromisoformat(start)
            nxt = 0
            for a in by_owner.get(owner, []):
                if str(a["ID"]) == str(c.get("CRM_ACTIVITY_ID")):
                    continue
                created = dt.datetime.fromisoformat(str(a["CREATED"])[:19])
                if t0 <= created <= t0 + dt.timedelta(days=1):
                    nxt = 1
                    break
            rows.append({"id": "call" + str(c["ID"]), "emp": login, "dt": start, "kind": "call", "direction": "out",
                         "duration": int(c.get("CALL_DURATION") or 0), "is_lpr": is_lpr, "secretary": is_secr,
                         "next_step": nxt, "status": "done", "subject": "", "client": c.get("CRM_ENTITY_TYPE", "")})
        for a in acts:
            if str(a.get("TYPE_ID")) != "1":  # 1 — встреча
                continue
            login = emp(a.get("RESPONSIBLE_ID"))
            if not login:
                continue
            is_lpr = contact_flags(a["OWNER_ID"])[0] if str(a["OWNER_TYPE_ID"]) == "3" else 1
            files = a.get("STORAGE_ELEMENT_IDS") or a.get("FILES")
            rows.append({"id": "act" + str(a["ID"]), "emp": login, "dt": self.iso(a.get("START_TIME")), "kind": "meeting",
                         "direction": "out", "duration": 0, "is_lpr": is_lpr, "secretary": 0, "next_step": 1,
                         "status": "done" if a.get("COMPLETED") == "Y" else "planned", "has_protocol": int(bool(files)),
                         "has_summary": int(len((a.get("DESCRIPTION") or "").strip()) >= 20),
                         "subject": a.get("SUBJECT"), "client": ""})
        out["activities"] = rows

        # тендеры: сделки воронки тендеров + история стадий
        self.log("Б24: торги")
        cat = self.cfg.get("tender_category_id")
        uf = [self.f.get("deal_planned_cost"), self.f.get("deal_actual_cost")]
        deals = self.list_all("crm.deal.list", {
            "filter": {"CATEGORY_ID": cat, "ASSIGNED_BY_ID": uids, ">=DATE_MODIFY": df_long},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "CLOSEDATE", "STAGE_ID",
                       "STAGE_SEMANTIC_ID", "OPPORTUNITY"] + [u for u in uf if u]})
        hist = self.list_all("crm.stagehistory.list", {
            "entityTypeId": 2, "filter": {"CATEGORY_ID": cat, ">=CREATED_TIME": df_long},
            "select": ["OWNER_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CREATED_TIME"], "order": {"ID": "ASC"}}, key="items")
        h_by = {}
        for h in hist:
            h_by.setdefault(str(h["OWNER_ID"]), []).append(h)
        st = self.cfg.get("tender_stages", {})
        companies = self.by_ids("crm.company.list", [d.get("COMPANY_ID") for d in deals], ["ID", "TITLE"])
        rows = []
        for d in deals:
            login = emp(d.get("ASSIGNED_BY_ID"))
            if not login:
                continue
            hs = h_by.get(str(d["ID"]), [])
            visited = {h["STAGE_ID"] for h in hs} | {d.get("STAGE_ID")}

            def first_time(stages):
                ts = [h["CREATED_TIME"] for h in hs if h["STAGE_ID"] in stages]
                return str(min(ts))[:10] if ts else None

            sem = d.get("STAGE_SEMANTIC_ID")
            result = "won" if sem == "S" else "lost" if sem == "F" else "open"
            final_ts = [h["CREATED_TIME"] for h in hs if h.get("STAGE_SEMANTIC_ID") in ("S", "F")]
            result_date = (str(max(final_ts))[:10] if final_ts else str(d.get("CLOSEDATE") or "")[:10]) if result != "open" else None
            rows.append({
                "id": "deal" + str(d["ID"]), "emp": login, "title": d.get("TITLE"),
                "client": companies.get(str(d.get("COMPANY_ID")), {}).get("TITLE", ""),
                "created": str(d.get("DATE_CREATE"))[:10],
                "in_work": int(bool(visited & set(st.get("in_work", [])))),
                "price_review": int(bool(visited & set(st.get("price_review", [])))),
                "submitted_date": first_time(set(st.get("submitted", []))),
                "result": result, "result_date": result_date, "price": float(d.get("OPPORTUNITY") or 0),
                "planned_cost": self.num(d.get(uf[0])) if uf[0] else None,
                "actual_cost": self.num(d.get(uf[1])) if uf[1] else None})
        out["tenders"] = rows

        # сделки воронки продаж (конверсия проектного менеджера)
        self.log("Б24: сделки продаж")
        deals = self.list_all("crm.deal.list", {
            "filter": {"CATEGORY_ID": self.cfg.get("sales_category_id", 0), "ASSIGNED_BY_ID": uids, "CLOSED": "Y", ">=CLOSEDATE": df},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "CLOSEDATE", "STAGE_SEMANTIC_ID", "OPPORTUNITY"]})
        companies = self.by_ids("crm.company.list", [d.get("COMPANY_ID") for d in deals], ["ID", "TITLE"])
        out["deals"] = [{"id": "deal" + str(d["ID"]), "emp": emp(d["ASSIGNED_BY_ID"]), "title": d.get("TITLE"),
                         "client": companies.get(str(d.get("COMPANY_ID")), {}).get("TITLE", ""),
                         "created": str(d.get("DATE_CREATE"))[:10], "closed_date": str(d.get("CLOSEDATE"))[:10],
                         "result": "won" if d.get("STAGE_SEMANTIC_ID") == "S" else "lost", "amount": float(d.get("OPPORTUNITY") or 0)}
                        for d in deals if emp(d.get("ASSIGNED_BY_ID"))]

        # лиды
        self.log("Б24: лиды")
        lf = {k: self.f.get("lead_" + k) for k in ("need", "budget", "lpr", "solvency", "next_step", "first_contact")}
        leads = self.list_all("crm.lead.list", {
            "filter": {"ASSIGNED_BY_ID": uids, ">=DATE_MODIFY": df},
            "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID", "DATE_CREATE"] + [v for v in lf.values() if v]})
        lhist = self.list_all("crm.stagehistory.list", {
            "entityTypeId": 1, "filter": {">=CREATED_TIME": df}, "select": ["OWNER_ID", "STATUS_ID", "CREATED_TIME"],
            "order": {"ID": "ASC"}}, key="items")
        qstat = set(self.cfg.get("lead_qualified_statuses", []))
        qdate = {}
        for h in lhist:
            if h.get("STATUS_ID") in qstat:
                qdate.setdefault(str(h["OWNER_ID"]), str(h["CREATED_TIME"])[:19])
        rows = []
        for l in leads:
            login = emp(l.get("ASSIGNED_BY_ID"))
            if not login or l.get("STATUS_ID") not in qstat:
                continue
            first_contact = str(l.get(lf["first_contact"]) or l.get("DATE_CREATE"))[:10] if lf["first_contact"] else str(l.get("DATE_CREATE"))[:10]
            q = qdate.get(str(l["ID"]))
            if not q:
                continue
            # лид относится к месяцу, когда и первый контакт, и квалификация уже состоялись
            q = max(q, first_contact)
            rows.append({"id": "lead" + str(l["ID"]), "emp": login, "title": l.get("TITLE"), "first_contact": first_contact,
                         "qualified_date": q, "status": l.get("STATUS_ID"),
                         **{k: self.flag(l.get(v)) if v else 0 for k, v in lf.items() if k != "first_contact"}})
        out["leads"] = rows

        # компании: активные клиенты с выявленной потребностью и стратегией
        self.log("Б24: компании")
        cf = [self.f.get("company_need"), self.f.get("company_strategy")]
        if all(cf):
            comps = self.list_all("crm.company.list", {"filter": {"ASSIGNED_BY_ID": uids},
                                                       "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "DATE_MODIFY"] + cf})
            out["clients"] = [{"id": "co" + str(c["ID"]), "emp": emp(c["ASSIGNED_BY_ID"]), "name": c.get("TITLE"),
                               "need": self.flag(c.get(cf[0])), "strategy": self.flag(c.get(cf[1])),
                               "updated": str(c.get("DATE_MODIFY"))[:19]} for c in comps if emp(c.get("ASSIGNED_BY_ID"))]

        # задачи
        self.log("Б24: задачи")
        tasks = self.list_all("tasks.task.list", {
            "filter": {"RESPONSIBLE_ID": uids, ">=DEADLINE": df},
            "select": ["ID", "TITLE", "RESPONSIBLE_ID", "CREATED_DATE", "DEADLINE", "CLOSED_DATE", "STATUS"]}, key="tasks")
        out["tasks"] = [{"id": "task" + str(t["id"]), "emp": emp(t.get("responsibleId")), "title": t.get("title"),
                         "created": self.iso(t.get("createdDate")), "deadline": self.iso(t.get("deadline")),
                         "closed": self.iso(t.get("closedDate")), "status": str(t.get("status"))}
                        for t in tasks if emp(t.get("responsibleId")) and t.get("deadline")]
        return out
