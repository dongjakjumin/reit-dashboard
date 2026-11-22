"""data/latest.json과 누적 기록을 읽어 site/index.html 한 장을 만든다.

집계는 전부 여기(파이썬)에서 하고, 화면(template.html)은 받은 숫자를 그리기만 한다.
실행 중에 AI나 외부 서비스를 부르지 않으므로 같은 자료면 같은 화면이 나온다.
"""
from __future__ import annotations

import json
import re
import sys
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from dart import previous_year
from textutil import CHANGE_REQUEST, filing_result, mask_person

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "site"

KINDS = ["인가", "등록", "신고"]
SECTORS = ["주택", "오피스", "물류", "복합형", "리테일", "호텔", "기타"]
FIRST_CHART_YEAR = 2015


def clean_company(name: str) -> str:
    """회사 이름에서 (주), 주식회사 표기를 뺀다(표시용)."""
    s = re.sub(r"\(주\)|㈜|주식회사", "", name or "")
    return re.sub(r"\s+", " ", s).strip()


def short_reit(name: str) -> str:
    s = clean_company(name)
    s = s.replace("위탁관리부동산투자회사", "리츠").replace("기업구조조정부동산투자회사", "CR리츠")
    s = s.replace("자기관리부동산투자회사", "자기관리리츠").replace("부동산투자회사", "리츠")
    s = s.replace("위탁관리모리츠", " 모리츠").replace("위탁관리자리츠", " 자리츠")
    return re.sub(r"\s+", " ", s).strip()


def d(s: str) -> date:
    return date.fromisoformat(s)


def safe_date(s) -> str:
    """YYYY-MM-DD 모양일 때만 돌려준다. 화면에 그대로 들어가는 값이라 형식이 다르면 비운다."""
    s = str(s or "")
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else ""


def safe_url(u) -> str | None:
    u = str(u or "").strip()
    return u if re.match(r"https?://", u, re.I) else None


def month_add(y: int, m: int, k: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + k
    return idx // 12, idx % 12 + 1


def collection_changes(previous, latest, filings, reits):
    changes = []
    if previous:
        for rec in filings.values():
            name = short_reit(mask_person(rec["name"]))
            company = clean_company(mask_person(rec["applicant"]))
            def add(kind, detail):
                changes.append({"kind": kind, "name": name, "company": company, "detail": detail})

            if rec.get("last_seen") == latest:
                if rec.get("first_seen") == latest:
                    add("새 신청", rec["req_type"])
                history = rec.get("status_history", [])
                if len(history) >= 2 and history[-1][0] == latest:
                    add("상태 변경", f"{history[-2][1]} → {history[-1][1]}")
            if rec.get("gone_on") == latest:
                add("목록에서 빠진 신청", rec["req_type"])
        for rec in reits.values():
            if rec.get("first_seen") == latest and rec.get("last_seen") == latest:
                changes.append({"kind": "운영 목록에 새로 올라온 리츠", "name": short_reit(mask_person(rec["name"])),
                                "company": clean_company(mask_person(rec["amc"])), "detail": rec["kind"]})
    return {"previous": previous, "rows": changes}


def project_comparison(proj, by_amc, today):
    groups = defaultdict(list)
    sectors = Counter()
    month_counts = Counter()
    for r in proj:
        groups[r["amc_s"]].append(r)
        sectors[r["sector"]] += 1
        month_counts[r["date"][:7]] += 1
    companies = [{"name": name, "n": len(rows), "total": len(by_amc[name]),
                  "sectors": dict(Counter(r["sector"] for r in rows).most_common()),
                  "latest": max(r["date"] for r in rows)} for name, rows in groups.items()]
    companies.sort(key=lambda a: (a["n"], a["latest"]), reverse=True)
    months = {}
    if month_counts:
        year, month = map(int, min(month_counts).split("-"))
        for offset in range((today.year - year) * 12 + today.month - month + 1):
            y, m = month_add(year, month, offset)
            key = f"{y}-{m:02}"
            months[key] = month_counts[key]
    return {"companies": companies, "sectors": sectors, "months": months}


def build_data() -> dict:
    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    flog = json.loads((DATA / "filings_log.json").read_text(encoding="utf-8"))
    rlog = json.loads((DATA / "reits_log.json").read_text(encoding="utf-8"))
    today = d(snap["date"])
    reits = snap["reits"]
    filings = snap["filings"]

    for r in reits:
        r["amc_s"] = clean_company(mask_person(r["amc"]))
        r["name_s"] = short_reit(mask_person(r["name"]))
        r["assets_eok"] = r["assets_eok"] or 0
        r["project"] = "Y" if r["kind"] == "신고" or r["project"] == "Y" else "N"

    def years_ago(years):
        year = today.year - years
        return date(year, today.month, min(today.day, monthrange(year, today.month)[1]))

    cut12 = years_ago(1)
    cut24 = years_ago(2)
    new12 = [r for r in reits if d(r["date"]) > cut12]
    prev12 = [r for r in reits if cut24 < d(r["date"]) <= cut12]
    proj = sorted([r for r in reits if r["project"] == "Y"], key=lambda r: r["date"], reverse=True)

    # 요약
    summary = {
        "n_reits": len(reits),
        "assets_eok": sum(r["assets_eok"] for r in reits),
        "n_amc": len({r["amc"] for r in reits}),
        "self_managed": len({r["amc_s"] for r in reits if r["amc_s"] and
                             r["amc_s"] == clean_company(r["name"]) and "자기관리" in (r["type"] or "")}),
        "new12": len(new12),
        "prev12": len(prev12),
        "new12_by_kind": {k: sum(1 for r in new12 if r["kind"] == k) for k in KINDS},
        "new12_by_sector": {s: sum(1 for r in new12 if r["sector"] == s) for s in SECTORS},
        "proj_n": len(proj),
        "proj_assets_eok": sum(r["assets_eok"] for r in proj),
        "proj_first": min(r["date"] for r in proj) if proj else None,
        "proj_amc_n": len({r["amc"] for r in proj}),
        "filings_n": len(filings),
        "filings_by_type": dict(Counter(f["req_type"] for f in filings)),
        "filings_by_status": dict(Counter(f["status"] for f in filings)),
        "listed_n": len(snap["listed"]),
    }

    # 연도별(현재 운영 중인 리츠의 인가, 등록, 신고 연도)
    this_year = today.year
    years = list(range(FIRST_CHART_YEAR, this_year + 1))
    yearly = {
        "years": years,
        "kind": {k: [sum(1 for r in reits if r["kind"] == k and int(r["date"][:4]) == y) for y in years] for k in KINDS},
        "sector": {s: [sum(1 for r in reits if r["sector"] == s and int(r["date"][:4]) == y) for y in years] for s in SECTORS},
        "before": sum(1 for r in reits if int(r["date"][:4]) < FIRST_CHART_YEAR),
        "ytd_label": f"{this_year}년은 {today.month}월 {today.day}일까지",
    }

    # 월별(최근 24개월)
    months = [month_add(today.year, today.month, k) for k in range(-23, 1)]
    mkey = [f"{y}-{m:02d}" for y, m in months]
    monthly = {
        "months": mkey,
        "kind": {k: [sum(1 for r in reits if r["kind"] == k and r["date"][:7] == mk) for mk in mkey] for k in KINDS},
    }

    # 투자대상별
    sector = [{"name": s,
               "n": sum(1 for r in reits if r["sector"] == s),
               "assets_eok": sum(r["assets_eok"] for r in reits if r["sector"] == s),
               "new12": sum(1 for r in new12 if r["sector"] == s)} for s in SECTORS]

    # 자산관리회사별
    by_amc: dict[str, list] = defaultdict(list)
    for r in reits:
        by_amc[r["amc_s"]].append(r)
    amc = []
    for name, rows in by_amc.items():
        sc = Counter(r["sector"] for r in rows)
        amc.append({
            "name": name,
            "n": len(rows),
            "assets_eok": sum(r["assets_eok"] for r in rows),
            "proj_n": sum(1 for r in rows if r["project"] == "Y"),
            "new12": sum(1 for r in rows if d(r["date"]) > cut12),
            "sectors": {s: sc.get(s, 0) for s in SECTORS if sc.get(s, 0)},
            "latest": safe_date(max(r["date"] for r in rows)),
        })
    amc.sort(key=lambda a: (-a["assets_eok"], -a["n"], a["name"]))
    for i, a in enumerate(amc, 1):
        a["rank_assets"] = i
    for i, a in enumerate(sorted(amc, key=lambda a: (-a["n"], -a["assets_eok"], a["name"])), 1):
        a["rank_n"] = i

    # 접수현황
    reit_ids = {r["id"] for r in reits}
    frows = []
    for f in sorted(filings, key=lambda f: f["date"], reverse=True):
        rec = flog.get(str(f["id"]), {})
        history = rec.get("status_history", [])
        frows.append({
            "date": safe_date(f["date"]),
            "days": (today - d(f["date"])).days,
            "req_type": f["req_type"],
            "name_s": short_reit(mask_person(f["name"])),
            "project_name": short_reit(mask_person(f["project_name"] or "")),
            "biz_type": f["biz_type"] or "",
            "applicant": clean_company(mask_person(f["applicant"])),
            "status": f["status"],
            "existing": f["reit_id"] in reit_ids,
            "first_seen": safe_date(rec.get("first_seen")),
            "changed_on": safe_date(history[-1][0]) if len(history) > 1 else "",
            "group": "변경" if f["req_type"] == CHANGE_REQUEST else "신규",
            "capital_eok": f["founding_capital"] / 100_000_000 if f.get("founding_capital") is not None else None,
        })

    # 접수 기록에서 처리 완료(목록에서 빠진 건) 집계: 기록이 쌓인 뒤에 의미가 생김
    done = []
    reit_by_id = {r["id"]: r for r in reits}
    for rec in flog.values():
        if rec.get("gone_on"):
            is_change = rec["req_type"] == CHANGE_REQUEST
            result = rec if "result_date" in rec else filing_result(rec, reit_by_id.get(rec.get("reit_id")))
            end = None if is_change else safe_date(result.get("result_date")) or None
            done.append({
                "date": safe_date(rec["date"]), "gone_on": safe_date(rec["gone_on"]), "req_type": rec["req_type"],
                "name_s": short_reit(mask_person(rec["name"])), "applicant": clean_company(mask_person(rec["applicant"])),
                "is_change": is_change,
                "result_kind": result.get("result_kind") if end else None,
                "result_date": end,
                "days_to_result": (d(end) - d(rec["date"])).days if end else None,
            })
    done.sort(key=lambda x: x["gone_on"], reverse=True)
    dates = sorted(p.stem for p in (DATA / "snapshots").glob("*.json")
                   if safe_date(p.stem) and p.stem <= snap["date"])
    previous = max((day for day in dates if day < snap["date"]), default=None)
    applicants = {}
    for f in frows:
        a = applicants.setdefault(f["applicant"], {"name": f["applicant"], "total": 0, "new": 0,
                                                  "change": 0, "supplement": 0, "oldest": 0})
        a["total"] += 1
        a["change" if f["group"] == "변경" else "new"] += 1
        a["supplement"] += f["status"] == "보완요청"
        a["oldest"] = max(a["oldest"], f["days"])
    summary["new_filings"] = sum(a["new"] for a in applicants.values())
    summary["old_new"] = sum(f["group"] == "신규" and f["days"] > 90 for f in frows)

    # 최근 변동(접수와 인가, 등록, 신고를 날짜순으로)
    events = []
    for r in reits:
        if d(r["date"]) > today - timedelta(days=90):
            events.append({"date": safe_date(r["date"]), "what": r["kind"], "name_s": r["name_s"],
                           "who": r["amc_s"], "note": r["sector"]})
    for f in frows:
        events.append({"date": f["date"], "what": "접수", "name_s": f["name_s"],
                       "who": f["applicant"], "note": f["req_type"]})
    events.sort(key=lambda e: (e["date"], e["what"] != "접수"), reverse=True)

    slim = [{k: r[k] for k in ("id", "date", "kind", "name_s", "amc_s", "type", "sector",
                               "public_offering", "assets_eok", "dividend_pct", "project")}
            for r in sorted(reits, key=lambda r: (r["date"], r["id"]), reverse=True)]

    for x in slim:
        x["date"] = safe_date(x["date"])
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "summary": summary, "yearly": yearly, "monthly": monthly, "sector": sector,
        "amc": amc, "filings": frows, "done": done, "log_days": len(dates), "first_log": min(dates, default=snap["date"]),
        "changes": collection_changes(previous, snap["date"], flog, rlog),
        "applicants": sorted(applicants.values(), key=lambda a: (-a["total"], a["name"])),
        "project": project_comparison(proj, by_amc, today),
        "events": events, "reits": slim,
        "kinds": KINDS, "sectors": SECTORS,
        "highlight_amc": cfg.get("highlight_amc", ""),
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


def _median(xs: list[float], digits: int) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    value = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    return round(value, digits)


def fiscal_days(series, end):
    """FFO 기준일과 일치하는 결산기와 바로 앞 기수의 말일 간격."""
    ends = {}
    for row in series:
        match = re.search(r"(\d+)기\s*결산기", row.get("label") or "")
        if match:
            ends.setdefault(int(match[1]), set()).add(row.get("end"))
    for period, dates in ends.items():
        previous = ends.get(period - 1, set())
        if end and dates == {end} and len(previous) == 1:
            try:
                days = (d(end) - d(next(iter(previous)))).days
            except (TypeError, ValueError):
                return None
            return days if days > 0 else None
    return None


def optional_data(name):
    path = DATA / name
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return json.loads(content) if content.strip() else {}


def rate_summary():
    result = {}
    for code, series in optional_data("rates.json").get("series", {}).items():
        values = series["values"]
        if not values:
            continue
        latest = max(values)
        end = d(latest)
        previous = max((day for day in values if 28 <= (end - d(day)).days <= 35), default=None)
        result[code] = {"value": values[latest], "date": latest,
                        "change": round(values[latest] - values[previous], 3) if previous else None}
    return result


def loan_schedule(stocks):
    from dart_notes import ratio

    notes = optional_data("loans.json")
    statements = optional_data("dart.json")
    issuers, missing, buckets = {}, [], {}
    all_loans = []
    borrowers = due = 0
    for stock in stocks:
        code = stock["code"]
        source = statements.get(code, {})
        balance = source.get("current", {})
        if balance.get("total") == 0:
            continue
        borrowers += int(bool(balance.get("total")))
        r = notes.get(code, {})
        if (r.get("match") not in ("일치", "근사") or r.get("rcept_no") != source.get("rcept_no")
                or r["end"] != balance.get("end") or r["bs_total"] != balance.get("total")
                or r["basis"] != source.get("basis")):
            missing.append(stock["name"])
            continue
        issuer = {k: r[k] for k in ("name", "end", "rcept_no", "nominal", "adjustments", "bs_total", "diff", "match")}
        issuers[code] = issuer
        issuer["loans"] = []
        end = d(r["end"])
        cutoff = end.replace(year=end.year+1, day=min(end.day, monthrange(end.year+1, end.month)[1]))
        for x in sorted(r["loans"], key=lambda v: v["maturity"]):
            day = d(x["maturity"])
            months = (day.year-end.year)*12 + day.month-end.month
            if day.day < min(end.day, monthrange(day.year, day.month)[1]):
                months -= 1
            issuer["loans"].append(dict(x, months=months))
            label = f"{day.year}년 {'상' if day.month <= 6 else '하'}반기"
            bucket = buckets.setdefault(label, {"고정": 0, "변동": 0, "모름": 0})
            bucket[x["rate_type"]] += x["amount"]
            if end <= day <= cutoff:
                due += x["amount"]
            all_loans.append(x)
    fixed, avg = ratio(all_loans)
    return dict(issuers=issuers, missing=missing, borrowers=borrowers, buckets=dict(sorted(buckets.items())),
                nominal=sum(x["amount"] for x in all_loans), due=due, fixed_share=fixed, avg_rate=avg)


def maturity_rows(stocks, corporate_rate=None):
    source = optional_data("dart.json")
    rows = []
    for stock in stocks:
        row = {"code": stock["code"], "name": stock["name"], "corp_spread": None}
        rows.append(row)
        record = source.get(stock["code"])
        if not record:
            continue
        s = record["current"]
        total, short = s["total"], s["short"]
        average = (record["previous"]["total"] + total) / 2
        days, op = s["days"], s["op"]
        interest = s.get("interest", s["cost"])
        annual = 365 / days if days else None
        row.update(basis="연결" if record["basis"] == "CFS" else "별도", end=s["end"],
                   total=total / 1e8, short=short / 1e8, share=100 * short / total if total else None,
                   rate=100 * interest * annual / average if annual and average and interest is not None else None,
                   interest_source=s.get("interest_source", s["cost_name"]),
                   coverage=op / interest if total and interest and op is not None else None, extra=short * 0.01 / 1e8,
                   impact=short / (op * annual) if op and op > 0 and annual else None)
        if total and row["rate"] is not None and corporate_rate is not None:
            row["corp_spread"] = round(row["rate"] - corporate_rate, 1)
    return rows


def build_listed_data() -> dict:
    """상장리츠 비교판 자료. P/NAV, P/FFO, 수익률, 지수는 여기서 계산한다."""
    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    dart = optional_data("dart.json")
    rates = rate_summary()
    benchmark = rates.get("ktb10", {}).get("value")
    prices = optional_data("prices.json")
    fin = optional_data("financials.json")
    rows = []
    win_start = (d(snap["date"]) - timedelta(days=372)).isoformat()
    window = {}  # 종목별 최근 1년 종가. 이력은 계속 쌓이지만 화면의 규칙은 이 구간에만 적용한다
    for r in snap["listed"]:
        hist = {k: v for k, v in prices.get(str(r["code"]), {}).items() if k >= win_start and v}
        dates = sorted(hist)
        if len(dates) >= 2:
            window[str(r["code"])] = hist
        last_d = dates[-1] if dates else None
        last = hist[last_d] if dates else r.get("price")

        # 하루 변동이 가격제한폭(30%)을 넘으면 감자, 권리락 등으로 기준가가 바뀐 날로 본다
        breaks = [b for a, b in zip(dates, dates[1:]) if abs(hist[b] / hist[a] - 1) > 0.31]
        streak = longest_flat = 0  # 종가가 같은 날이 이어진 가장 긴 구간
        for a, b in zip(dates, dates[1:]):
            streak = streak + 1 if hist[a] == hist[b] else 0
            longest_flat = max(longest_flat, streak)
        halted = longest_flat >= 60 and not r.get("volume")

        def ret(days: int):
            if len(dates) < 2:
                return None
            target = (d(last_d) - timedelta(days=days)).isoformat()
            prev = [x for x in dates if x <= target]
            if not prev or any(b > prev[-1] for b in breaks):
                return None  # 기준가가 바뀐 날을 사이에 둔 구간은 비교하지 않음
            return round((last / hist[prev[-1]] - 1) * 100, 1)

        dividends = dart.get(r["code"], {})
        dps = dividends.get("trailing_dps")
        dividend_end = safe_date(dividends.get("dividend_end"))
        price_break = breaks[-1] if breaks else None
        yield_blocked = bool(price_break and dividend_end
                             and previous_year(d(dividend_end)).isoformat() < price_break <= dividend_end)
        mktcap = r["mktcap_eok"] or 0
        nav_eok = round(r["nav_mil"] / 100) if r.get("nav_mil") else None
        ffo_eok = round(r["ffo_mil"] / 100, 1) if r.get("ffo_mil") is not None else None
        ppy = None  # 연간 결산 횟수: 연환산 배당률 / 배당수익률로 추정
        days = fiscal_days(fin.get(str(r.get("reit_id")), {}).get("series", []), r.get("fiscal_end"))
        if days:
            ppy = 365 / days
        elif r.get("dvd_pct") and r.get("annual_dvd_pct"):
            ppy = min(4, max(1, round(r["annual_dvd_pct"] / r["dvd_pct"])))
        market_yield = dps / last * 100 if not yield_blocked and dps is not None and last and last > 0 else None
        rows.append({
            "code": r["code"], "name": r["name"], "sector": r.get("sector") or "기타",
            "has_finance": _has_finance(fin.get(str(r.get("reit_id")))),
            "homepage": safe_url(r.get("homepage")), "listed_date": safe_date(r.get("listed_date")),
            "market_yield": market_yield,
            "spread": round(market_yield - benchmark, 2) if market_yield is not None and benchmark is not None else None,
            "market_yield_reason": "기준가 변경" if yield_blocked else None,
            "price": last, "mktcap_eok": mktcap,
            "chg_pct": round((last / hist[dates[-2]] - 1) * 100, 2) if len(dates) >= 2 and last_d not in breaks else None,
            "price_date": last_d,
            "ret_1m": ret(30), "ret_3m": ret(91), "ret_1y": ret(365),
            "dvd_pct": r.get("dvd_pct"), "annual_dvd_pct": r.get("annual_dvd_pct"),
            "nav_eok": nav_eok, "ffo_eok": ffo_eok, "periods_per_year": ppy,
            "pnav": round(mktcap / nav_eok, 2) if nav_eok and nav_eok > 0 and mktcap else None,
            "pffo": round(mktcap / (ffo_eok * ppy), 1) if ffo_eok and ffo_eok > 0 and ppy and mktcap else None,
            "wale_years": r.get("wale_years") or None,
            "price_break": price_break, "halted": halted,
            "fiscal_end": safe_date(r.get("fiscal_end")), "fiscal_period": r.get("fiscal_period"),
            "spark": [hist[x] for x in dates[::5]] + ([last] if dates and (len(dates) - 1) % 5 else []),
        })

    # 단순평균 지수: 1년 구간 내내 종가가 있는 종목만, 모든 종목에 종가가 있는 날짜만, 첫날 = 100
    codes = list(window)
    start_day = (d(snap["date"]) - timedelta(days=366)).isoformat()
    all_days = sorted({day for hist in window.values() for day in hist if day >= start_day})
    first_week = all_days[min(5, len(all_days) - 1)] if all_days else start_day
    skip = {str(r["code"]): r["name"] for r in rows if r["price_break"] or r["halted"]}
    full = [c for c in codes if c not in skip and any(start_day <= x <= first_week for x in window[c])]
    common = [day for day in all_days if all(day in window[c] for c in full)] if full else []
    idx = [round(sum(window[c][x] / window[c][common[0]] for c in full) / len(full) * 100, 2) for x in common]

    sec_names = sorted({r["sector"] for r in rows})
    sectors = sorted(({"name": s, "n": sum(1 for r in rows if r["sector"] == s),
                       "mktcap_eok": sum(r["mktcap_eok"] for r in rows if r["sector"] == s)} for s in sec_names),
                     key=lambda x: -x["mktcap_eok"])
    pnavs = [r["pnav"] for r in rows if r["pnav"] is not None]
    yields = [r["market_yield"] for r in rows if r["market_yield"] is not None]
    spreads = [r["spread"] for r in rows if r["spread"] is not None]
    dvds = [r["annual_dvd_pct"] for r in rows if r["annual_dvd_pct"]]
    r1y = [r["ret_1y"] for r in rows if r["ret_1y"] is not None]
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "price_as_of": max((r["price_date"] for r in rows if r["price_date"]), default=None),
        "summary": {
            "n": len(rows), "mktcap_eok": sum(r["mktcap_eok"] for r in rows),
            "index_ret": round(idx[-1] - 100, 1) if idx else None, "index_n": len(full) if idx else 0,
            "up_1y": sum(1 for v in r1y if v > 0), "down_1y": sum(1 for v in r1y if v < 0),
            "pnav_median": _median(pnavs, 2), "pnav_n": len(pnavs),
            "pnav_below1": sum(1 for v in pnavs if v < 1), "pnav_min": min(pnavs, default=None), "pnav_max": max(pnavs, default=None),
            "yield_median": _median(yields, 2), "yield_n": len(yields),
            "rates": rates, "spread_median": _median(spreads, 2), "spread_n": len(spreads),
            "dvd_median": _median(dvds, 2), "dvd_n": len(dvds),
        },
        "index": {"dates": common, "values": idx, "excluded": sorted(skip.values())},
        "sectors": sectors, "rows": rows,
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


# 수시공시 유형 묶음(화면의 필터 단추). 여기에 없는 유형은 '정기, 일반'으로 센다.
DISCLOSURE_GROUPS = {
    "RT02100018": "위험 신호", "RT02100019": "위험 신호", "RT02100027": "위험 신호",
    "RT02100021": "자산 취득, 처분", "RT02100022": "자산 취득, 처분",
    "RT02100023": "신용평가", "RT02100024": "최대주주 변경",
    "RT02100028": "특수관계 거래", "RT02100029": "대출 실행",
    "RT02100020": "기타 공시", "RT02100002": "기타 공시",
}
GROUP_ORDER = ["위험 신호", "자산 취득, 처분", "신용평가", "최대주주 변경", "특수관계 거래", "대출 실행", "기타 공시"]


def _norm_company(name: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사|\s", "", name or "")


def _has_finance(f):
    return bool(f and f.get("assets") and f.get("debt") is not None and f.get("equity") is not None)


def _finance_events(series):
    events = []
    previous = {}
    for row in sorted(series, key=lambda x: x.get("end") or ""):
        day = safe_date(row.get("end"))
        if not day:
            continue
        assets, debt = row.get("assets"), row.get("debt")
        event = {"date": day, "kind": "finance", "assets": assets, "debt": debt, "equity": row.get("equity"),
                 "ratio": round(debt / assets * 100, 1) if assets and debt is not None else None}
        for key in ("debt", "equity"):
            current, prior = row.get(key), previous.get(key)
            change = current - prior if current is not None and prior is not None else None
            event[key + "_change"] = change
            event[key + "_up"] = bool(assets and assets > 0 and change is not None and change * 20 > assets)
        events.append(event)
        previous = row
    return events


def build_finance_data() -> dict:
    """상장리츠 재무 구조와 리츠 주요 공시."""
    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    fin = optional_data("financials.json")
    rates = rate_summary()
    dis = optional_data("disclosures.json")
    today = d(snap["date"])
    eok = lambda won: round(won / 1e8) if won is not None else None

    rows, timelines = [], {}
    for r in snap["listed"]:
        f = fin.get(str(r.get("reit_id")))
        if not _has_finance(f):
            continue
        timelines[r["code"]] = _finance_events(f["series"])
        ser = [x for x in f["series"] if x.get("assets") and x.get("debt") is not None]
        last = ser[-1] if ser else {}
        m = re.search(r"(\d+기)\s*(결산기|\d분기|반기)?", (last.get("label") or "").replace("\\n", " "))
        # 추이의 마지막 줄은 분기 말일 수 있다. 영업수익과 영업이익은 가장 최근 '결산기' 줄에서 가져온다.
        fy = next((x for x in reversed(ser) if "결산기" in (x.get("label") or "") and x.get("revenue") is not None), None)
        rev, op = (fy or {}).get("revenue"), (fy or {}).get("op_profit")
        rows.append({
            "code": r["code"], "name": r["name"], "sector": r.get("sector") or "기타",
            "homepage": safe_url(r.get("homepage")), "mktcap_eok": r.get("mktcap_eok"),
            "fy_end": safe_date(fy["end"]) if fy else None,
            "as_of": safe_date(last.get("end")), "period": " ".join(x for x in (m.groups() if m else ()) if x),
            "assets_eok": eok(f["assets"]), "debt_eok": eok(f["debt"]), "equity_eok": eok(f["equity"]),
            "debt_to_assets": round(f["debt"] / f["assets"] * 100, 1),
            "debt_to_equity": round(f["debt"] / f["equity"], 2) if f.get("equity") and f["equity"] > 0 else None,
            "revenue_eok": round(rev / 1e8, 1) if rev is not None else None,
            "op_margin": round(op / rev * 100, 1) if rev and op is not None else None,
            "top_shareholder": clean_company(mask_person(f.get("top_shareholder")) or ""),
            "series": [{"end": safe_date(x["end"]), "assets_eok": eok(x["assets"]), "debt_eok": eok(x["debt"]),
                        "debt_to_assets": round(x["debt"] / x["assets"] * 100, 1)} for x in ser],
        })
    da = [r["debt_to_assets"] for r in rows]
    hi = max(rows, key=lambda r: r["debt_to_assets"], default=None)
    lo = min(rows, key=lambda r: r["debt_to_assets"], default=None)

    # 공시의 회사명과 상장 목록의 회사명은 원문에서 표기가 다를 수 있어, 리츠조회 목록의 이름도 함께 쓴다
    reit_name_by_id = {r["id"]: r["name"] for r in snap["reits"]}
    listed_names = {}
    for r in snap["listed"]:
        for nm in (r.get("company"), reit_name_by_id.get(r.get("reit_id"))):
            if nm:
                listed_names[_norm_company(nm)] = r
    cut90 = (today - timedelta(days=90)).isoformat()
    items, routine90 = [], 0
    for no, x in dis.items():
        grp = DISCLOSURE_GROUPS.get(x.get("matter_code"))
        stock = listed_names.get(_norm_company(x["company"]))
        code = stock["code"] if stock else None
        day = safe_date(x.get("date"))
        body = "" if "주주" in x["matter"] else (x.get("text") or "")
        if code in timelines and day:
            event = {"kind": "disclosure", "date": day, "no": no, "group": grp}
            if grp:
                event.update(matter=x["matter"], text=body)
            timelines[code].append(event)
        if (x.get("date") or "") < cut90:
            continue
        if not grp:
            routine90 += 1
            continue
        items.append({"no": no, "date": safe_date(x["date"]), "group": grp, "matter": x["matter"],
                      "company_s": short_reit(x["company"]), "type": x.get("company_type") or "",
                      "listed": stock["name"] if stock else None,
                      "timeline_code": code if code in timelines else None, "text": body})
    for events in timelines.values():
        events.sort(key=lambda e: (e["date"], e["kind"] == "finance", int(e.get("no", 0))), reverse=True)
    items.sort(key=lambda z: (z["date"], int(z["no"])), reverse=True)
    counts = {g: sum(1 for i in items if i["group"] == g) for g in GROUP_ORDER}
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "fin_fetched": max((f["fetched"] for f in fin.values()), default=None),
        "summary": {
            "n": len(rows), "da_median": _median(da, 1),
            "da_hi": {"name": hi["name"], "v": hi["debt_to_assets"]} if hi else None,
            "da_lo": {"name": lo["name"], "v": lo["debt_to_assets"]} if lo else None,
            "de_over2": sum(1 for r in rows if r["debt_to_equity"] and r["debt_to_equity"] > 2),
            "da_under20": sum(1 for r in rows if r["debt_to_assets"] < 20),
            "da_median_ex": _median([v for v in da if v >= 20], 1),
            "assets_eok": sum(r["assets_eok"] for r in rows), "debt_eok": sum(r["debt_eok"] for r in rows),
            "dis_major": len(items), "dis_routine": routine90, "dis_risk": counts["위험 신호"],
            "dis_from": cut90,
        },
        "rows": rows, "groups": [{"name": g, "n": counts[g]} for g in GROUP_ORDER if counts[g]],
        "disclosures": items,
        "rates": rates,
        "loans": loan_schedule(snap["listed"]),
        "maturity": maturity_rows(snap["listed"], rates.get("corp_aa", {}).get("value")),
        "timelines": timelines,
        "dis_start": min((day for x in dis.values() if (day := safe_date(x.get("date")))), default=None),
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


PAGES = [("index", "./", "인가와 프로젝트리츠"), ("listed", "listed.html", "상장리츠"),
         ("finance", "finance.html", "재무 구조와 공시"), ("calc", "calc.html", "사업성 계산기")]


def nav_html(current: str) -> str:
    links = "".join(f'\n  <a href="{href}"' + (' aria-current="page"' if key == current else "") + f">{label}</a>"
                    for key, href, label in PAGES)
    return f'<nav class="top" aria-label="화면 이동">{links}\n</nav>'


def render(template: str, out: str, data: dict | None, page: str, extra: dict[str, str] | None = None) -> int:
    tpl = (ROOT / template).read_text(encoding="utf-8")
    for mark, text in (extra or {}).items():
        if mark not in tpl:
            raise RuntimeError(f"{template}에서 {mark} 자리를 찾지 못함")
        tpl = tpl.replace(mark, text)
    css = (ROOT / "style.css").read_text(encoding="utf-8")
    for mark in ("/*__CSS__*/", "<!--__NAV__-->"):
        if mark not in tpl:
            raise RuntimeError(f"{template}에서 {mark} 자리를 찾지 못함")
    html = tpl.replace("/*__CSS__*/", css).replace("<!--__NAV__-->", nav_html(page))
    if data is not None:
        if "/*__DATA__*/null" not in html:
            raise RuntimeError(f"{template}에서 자료를 넣을 자리를 찾지 못함")
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        html = html.replace("/*__DATA__*/null", payload)
    SITE.mkdir(exist_ok=True)
    (SITE / out).write_text(html, encoding="utf-8", newline="\n")
    return len(html)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    data = build_data()
    n_index = render("template.html", "index.html", data, "index")
    listed = build_listed_data()
    n_listed = render("template_listed.html", "listed.html", listed, "listed")
    finance = build_finance_data()
    n_fin = render("template_finance.html", "finance.html", finance, "finance")
    # 사업성 계산기: 수집 자료를 쓰지 않는다. 기본 가정과 계산 엔진만 넣는다.
    defaults = json.loads((ROOT / "calc_defaults.json").read_text(encoding="utf-8"))
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    calc = {"base": defaults["base"], "downside": defaults["downside"],
            "author": cfg.get("author", ""), "contact": cfg.get("contact", "")}
    engine = (ROOT / "calc_engine.js").read_text(encoding="utf-8")
    n_calc = render("template_calc.html", "calc.html", calc, "calc", {"/*__ENGINE__*/": engine})
    s = data["summary"]
    print(f"[빌드] {data['collected_at']} | 리츠 {s['n_reits']}개, 자산총계 {s['assets_eok']/10000:.2f}조원, "
          f"관리주체 {s['n_amc']}곳 | 최근 12개월 {s['new12']}건(직전 {s['prev12']}건) | "
          f"프로젝트리츠 {s['proj_n']}건 | 접수 {s['filings_n']}건 | index.html {n_index:,}자")
    ls = listed["summary"]
    print(f"[빌드] 상장리츠 {ls['n']}개, 시가총액 {ls['mktcap_eok']/10000:.2f}조원, "
          f"지수 1년 {ls['index_ret']}%({ls['index_n']}개), "
          f"P/NAV 중앙값 {ls['pnav_median']}(1배 미만 {ls['pnav_below1']}/{ls['pnav_n']}), "
          f"연환산 배당률 중앙값 {ls['dvd_median']}% | listed.html {n_listed:,}자")
    fs = finance["summary"]
    print(f"[빌드] 재무 구조 {fs['n']}개, 부채/자산 중앙값 {fs['da_median']}%, "
          f"최근 90일 주요 공시 {fs['dis_major']}건(위험 신호 {fs['dis_risk']}건), 정기와 일반 {fs['dis_routine']}건 | "
          f"finance.html {n_fin:,}자")
    print(f"[빌드] 사업성 계산기 calc.html {n_calc:,}자")


if __name__ == "__main__":
    main()
