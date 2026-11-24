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
from textutil import CHANGE_REQUEST, PRIVATE_LABEL, filing_result, mask_officer_names, mask_person

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


def shareholder_label(name: str | None) -> str:
    """최대주주 표시 이름. 회사 표기를 빼되, 빼고 나서 개인 이름처럼 보이면(예: 주식회사 예지훈 외) ㈜를 남긴다.
    원문 칸에서 줄이 바뀌며 닫는 괄호가 잘린 이름은 괄호를 닫아 준다."""
    masked = mask_person(name) or ""
    s = clean_company(masked)
    if s != masked and mask_person(s) == PRIVATE_LABEL:
        s = "㈜" + s
    return s + ")" * max(0, s.count("(") - s.count(")"))


def short_reit(name: str) -> str:
    s = clean_company(name).replace("부동산투저회사", "부동산투자회사")  # 접수현황 원문 오타
    s = s.replace("위탁관리부동산투자회사", "리츠").replace("기업구조조정부동산투자회사", "CR리츠")
    s = s.replace("자기관리부동산투자회사", "자기관리리츠").replace("부동산투자회사", "리츠")
    s = s.replace("위탁관리모리츠", " 모리츠").replace("위탁관리자리츠", " 자리츠")
    return re.sub(r"\s+", " ", s).strip()


def applicant_name(name: str | None) -> str:
    """신청회사 표시 이름. 리츠가 직접 신청한 건(자기관리리츠)도 리츠 이름 규칙으로 줄인다."""
    return short_reit(mask_person(name) or "")


PUBLIC_RE = re.compile(r"(?:공사|공단)$")  # 주택도시보증공사, 한국토지주택공사, 인천도시공사 같은 공공기관


def d(s: str) -> date:
    return date.fromisoformat(s)


def since(today: date, days: int = 90) -> str:
    """오늘을 넣어 센 최근 days일의 첫날. 화면의 '최근 90일'은 모두 이 날부터 오늘까지다."""
    return (today - timedelta(days=days - 1)).isoformat()


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
            company = applicant_name(rec["applicant"])
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


def pair_base(name_s: str) -> str:
    """모리츠와 자리츠는 이름 끝(모리츠, 자리츠)만 다르면 한 사업으로 센다."""
    return re.sub(r"\s(?:모|자)리츠$", "", name_s)


def child_filings(reits: list, frows: list) -> dict:
    """처리 중인 자리츠 설립신고, 인가, 등록 신청을 이름이 이어지는 운영 리츠(같은 자산관리회사)에 붙인다.

    예: 운영 중인 '코람코개발앵커프로젝트리츠'와 접수 중인 '코람코개발앵커제1호프로젝트 자리츠'.
    이름 앞부분이 5자 이상 같고 신청인이 그 리츠의 자산관리회사일 때만 잇는다."""
    out = defaultdict(list)
    for f in frows:
        if f["group"] != "신규" or not f["name_s"].endswith("자리츠"):
            continue
        hits = []
        for r in reits:
            base = re.sub(r"(?:\s모리츠|프로젝트리츠|리츠)$", "", r["name_s"])
            if len(base) >= 5 and f["name_s"].startswith(base) and r["amc_s"] == f["applicant"] and r["name_s"] != f["name_s"]:
                hits.append((len(base), r["id"]))
        if hits:
            out[str(max(hits)[1])].append({k: f[k] for k in ("date", "name_s", "project_name", "req_type", "status")})
    return dict(out)


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
        "proj_biz_n": len({pair_base(r["name_s"]) for r in proj}),
        # 설립 자본금은 50억원 이상(법 26조의4 ① 2호). 자산총계가 60억원 이하이면 사업 초기로 본다
        "proj_early": sum(1 for r in proj if 0 < r["assets_eok"] <= 60),
        "proj_zero": sum(1 for r in proj if not r["assets_eok"]),
        "mo_n": sum(1 for r in reits if r["name_s"].endswith(" 모리츠")),
        "mo_assets_eok": sum(r["assets_eok"] for r in reits if r["name_s"].endswith(" 모리츠")),
        "proj_assets_eok": sum(r["assets_eok"] for r in proj),
        "proj_first": min(r["date"] for r in proj) if proj else None,
        "proj_amc_n": len({r["amc"] for r in proj}),
        "filings_n": len(filings),
        "filings_by_type": dict(Counter(f["req_type"] for f in filings)),
        "filings_by_status": dict(Counter(f["status"] for f in filings)),
        "listed_n": len(snap["listed"]),
    }

    # 공모 여부(리츠조회 목록의 공모 여부 칸, 빈 값은 미기재)
    summary["po_12"] = dict(Counter(r["public_offering"] or "미기재" for r in new12))
    po_planned = [{"date": safe_date(r["date"]), "kind": r["kind"], "name_s": r["name_s"], "amc_s": r["amc_s"],
                   "sector": r["sector"], "assets_eok": r["assets_eok"]}
                  for r in sorted(reits, key=lambda r: (r["date"], r["id"]), reverse=True) if r["public_offering"] == "공모예정"]

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
        first_r = min(rows, key=lambda r: (r["date"], r["id"]))
        first_d = d(first_r["date"])
        amc.append({
            "name": name,
            "n": len(rows),
            "assets_eok": sum(r["assets_eok"] for r in rows),
            "proj_n": sum(1 for r in rows if r["project"] == "Y"),
            "new12": sum(1 for r in rows if d(r["date"]) > cut12),
            "sectors": {s: sc.get(s, 0) for s in SECTORS if sc.get(s, 0)},
            "latest": safe_date(max(r["date"] for r in rows)),
            # 첫 리츠(현재 운영 목록 기준)와 그 뒤 1년, 2년 안에 생긴 리츠 수(첫 리츠 포함)
            "first": safe_date(first_r["date"]), "first_name": first_r["name_s"], "first_kind": first_r["kind"],
            "n_y1": sum(1 for r in rows if d(r["date"]) < first_d + timedelta(days=365)),
            "n_y2": sum(1 for r in rows if d(r["date"]) < first_d + timedelta(days=730)),
            "self": all("자기관리" in (r["type"] or "") for r in rows),
            "public": bool(PUBLIC_RE.search(name)),
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
            "applicant": applicant_name(f["applicant"]),
            "status": f["status"],
            "changed_on": safe_date(history[-1][0]) if len(history) > 1 else "",
            # 이미 운영 목록에 있는 리츠(등록, 설립신고)가 낸 영업인가, 등록 신청은 신규 설립이 아니라 전환으로 센다
            "group": "변경" if f["req_type"] == CHANGE_REQUEST else "전환" if f["reit_id"] in reit_ids else "신규",
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
                "name_s": short_reit(mask_person(rec["name"])), "applicant": applicant_name(rec["applicant"]),
                "is_change": is_change,
                "result_kind": result.get("result_kind") if end else None,
                "result_date": end,
                "days_to_result": (d(end) - d(rec["date"])).days if end else None,
            })
    done.sort(key=lambda x: x["gone_on"], reverse=True)
    dates = sorted(p.stem for p in (DATA / "snapshots").glob("*.json")
                   if safe_date(p.stem) and p.stem <= snap["date"])
    previous = max((day for day in dates if day < snap["date"]), default=None)
    # 운용사별 진행 중인 신청: 접수 후 1년이 넘은 신청은 '1년 넘게 계류'로 따로 세어 순위를 왜곡하지 않게 한다
    applicants = {}
    for f in frows:
        a = applicants.setdefault(f["applicant"], {"name": f["applicant"], "public": bool(PUBLIC_RE.search(f["applicant"])),
                                                  "recent": 0, "new": 0, "change": 0, "supplement": 0, "long": 0, "oldest": 0})
        if f["days"] > 365:
            a["long"] += 1
        else:
            a["recent"] += 1
            a["change" if f["group"] == "변경" else "new"] += 1
            a["supplement"] += f["status"] == "보완요청"
        a["oldest"] = max(a["oldest"], f["days"])
    summary["filings_by_group"] = {g: sum(f["group"] == g for f in frows) for g in ("신규", "전환", "변경")}
    # 법정 처리기간(인가, 등록, 변경인가 20일: 인가 및 등록 지침 8조 ②, 설립신고 20일: 법 26조의4 ②) 기준 구간
    summary["age_buckets"] = [sum(lo <= f["days"] <= hi for f in frows)
                              for lo, hi in ((0, 20), (21, 60), (61, 365), (366, 10 ** 6))]

    # 최근 변동(접수와 인가, 등록, 신고를 날짜순으로)
    events = []
    for r in reits:
        if r["date"] >= since(today):
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
        x["dividend_pct"] = x["dividend_pct"] or None
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    # 관리주체 상세: 최근 90일 주요 공시를 공시 회사명으로 운영 중 리츠나 관리주체에 연결한다.
    # 본문은 싣지 않는다(주주 관련 공시의 개인 정보가 섞이지 않게 제목과 유형만 쓴다).
    reit_by_norm = {_norm_company(r["name"]): r for r in reits}
    amc_by_norm = {_norm_company(r["amc"]): r["amc_s"] for r in reits}
    dis_from = since(today)
    amc_dis: dict[str, list] = defaultdict(list)
    dis_unmatched = 0
    for no, x in optional_data("disclosures.json").items():
        grp = dis_group(x)
        day = safe_date(x.get("date"))
        if not grp or not day or day < dis_from:
            continue
        key = _norm_company(x.get("company"))
        owner_reit = reit_by_norm.get(key)
        owner = owner_reit["amc_s"] if owner_reit else amc_by_norm.get(key)
        if not owner:
            dis_unmatched += 1
            continue
        amc_dis[owner].append({"no": no, "date": day, "group": grp, "matter": x.get("matter") or "",
                               "company_s": owner_reit["name_s"] if owner_reit else owner,
                               "self": owner_reit is None})
    for rows in amc_dis.values():
        rows.sort(key=lambda z: (z["date"], int(z["no"])), reverse=True)

    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "summary": summary, "yearly": yearly, "monthly": monthly, "sector": sector,
        "amc": amc, "amc_dis": dict(amc_dis), "amc_dis_from": dis_from, "amc_dis_unmatched": dis_unmatched,
        "filings": frows, "done": done, "log_days": len(dates), "first_log": min(dates, default=snap["date"]),
        "changes": collection_changes(previous, snap["date"], flog, rlog),
        "applicants": sorted(applicants.values(), key=lambda a: (-a["recent"], -a["long"], a["name"])),
        "project": project_comparison(proj, by_amc, today),
        "events": events, "reits": slim, "po_planned": po_planned, "children": child_filings(reits, frows),
        "kinds": KINDS, "sectors": SECTORS,
        "highlight_amc": cfg.get("highlight_amc", ""),
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


def dividend_profile(record: dict) -> dict:
    """DART 결산기별 주당배당금에서 최근 결산기 배당, 직전 대비 증감, 최근 12개월 안의 급증 배당을 뽑는다.

    급증 배당은 직전 결산기 주당배당금의 1.5배 이상인 결산기다(특별배당이나 자산 매각 차익 분배일 수 있다)."""
    divs = record.get("dividends") or []
    last = divs[-1] if divs else None
    prev = divs[-2] if len(divs) > 1 else None
    out = {"dps_last": None, "dps_k": None, "dps_chg": None, "dps_jump": False}
    if not last or last.get("dps") is None:
        return out
    out["dps_last"] = last["dps"]
    if last.get("days"):
        out["dps_k"] = round(365 / last["days"])  # 연간 결산 횟수
    if prev and prev.get("dps"):
        out["dps_chg"] = round((last["dps"] / prev["dps"] - 1) * 100, 1)
    end = record.get("dividend_end")
    if end:
        cutoff = previous_year(d(end)).isoformat()
        out["dps_jump"] = any(cutoff < b["end"] <= end and a.get("dps") and b.get("dps") is not None and b["dps"] >= 1.5 * a["dps"]
                              for a, b in zip(divs, divs[1:]))
    return out


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


def periods_per_year(r: dict, series: list) -> float | None:
    """연간 결산 횟수: FFO 기준 결산기의 일수로 정하고, 없으면 연환산 배당률 ÷ 배당률로 추정한다."""
    days = fiscal_days(series, r.get("fiscal_end"))
    if days:
        return 365 / days
    if r.get("dvd_pct") and r.get("annual_dvd_pct"):
        return min(4, max(1, round(r["annual_dvd_pct"] / r["dvd_pct"])))
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


def upcoming_loans(issuers, as_of):
    start = d(as_of)
    dates = []
    for offset in range(13):
        year, month = month_add(start.year, start.month, offset)
        dates.append(date(year, month, min(start.day, monthrange(year, month)[1])))
    months = {day.strftime("%Y-%m"): dict.fromkeys(("고정", "변동", "모름"), 0) for day in dates}
    rows = []
    passed = dict(amount=0, count=0, issuers=0)
    overdue = dict(amount=0, count=0)
    for code, issuer in issuers.items():
        groups = defaultdict(list)
        for loan in issuer["loans"]:
            key = (loan["maturity"], loan["kind"], loan["rate_text"], loan["rate_type"], loan.get("fx", False))
            groups[key].append(loan)
        has_passed = False
        for (maturity, kind, rate_text, rate_type, fx), loans in groups.items():
            amount = sum(x["amount"] for x in loans)
            if maturity < issuer["end"]:
                overdue["amount"] += amount
                overdue["count"] += len(loans)
            elif maturity < as_of:
                passed["amount"] += amount
                passed["count"] += 1
                has_passed = True
            elif d(maturity) <= dates[-1]:
                lenders = Counter()
                for loan in loans:
                    if loan["lender"]:
                        lenders[loan["lender"]] += loan["amount"]
                rows.append(dict(code=code, name=issuer["name"], maturity=maturity, days=(d(maturity)-start).days,
                                 kind=kind, rate_text=rate_text, rate_type=rate_type, amount=amount,
                                 lenders=[name for name, _ in lenders.most_common(3)], lender_n=len(lenders),
                                 end=issuer["end"], rate=loans[0]["rate"], fx=fx))
                months[maturity[:7]][rate_type] += amount
        passed["issuers"] += int(has_passed)
    rows.sort(key=lambda row: row["maturity"])
    floating = sum(month["변동"] for month in months.values())
    known = floating + sum(month["고정"] for month in months.values())
    return dict(as_of=as_of, rows=rows, months=months, total=sum(x["amount"] for x in rows),
                within6=sum(x["amount"] for x in rows if d(x["maturity"]) <= dates[6]),
                float_share=floating / known if known else None, passed=passed, overdue=overdue)


def loan_missing_reason(r: dict, source: dict) -> str | None:
    """같은 보고서의 주석을 읽었는데 건별 내역을 못 쓴 이유. 다른 보고서 기준이면(주석을 아직 못 읽음) 이유를 적지 않는다."""
    if r.get("rcept_no") != source.get("rcept_no"):
        return None
    if not r.get("loans") and "만기일과 이자율이 함께 있는 차입 잔액 표 없음" in (r.get("skipped") or []):
        return "만기일이 적힌 주석 차입 표를 찾지 못함"
    if r.get("match") == "불일치":
        return "주석 합계가 재무상태표 차입과 다름"
    return None


def loan_schedule(stocks, as_of):
    from dart_notes import ratio

    notes = optional_data("loans.json")
    statements = optional_data("dart.json")
    issuers, missing, buckets, reasons = {}, [], {}, {}
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
                or r.get("end") != balance.get("end") or r.get("bs_total") != balance.get("total")
                or r.get("basis") != source.get("basis")):
            missing.append(stock["name"])
            if loan_missing_reason(r, source):
                reasons[stock["name"]] = loan_missing_reason(r, source)
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
    return dict(issuers=issuers, missing=missing, missing_reason=reasons, borrowers=borrowers, buckets=dict(sorted(buckets.items())),
                nominal=sum(x["amount"] for x in all_loans), due=due, fixed_share=fixed, avg_rate=avg,
                upcoming=upcoming_loans(issuers, as_of))


def maturity_rows(stocks, issuers, corporate_rate=None):
    source = optional_data("dart.json")
    rows = []
    for stock in stocks:
        row = {"code": stock["code"], "name": stock["name"], "corp_spread": None}
        row["fx"] = any(x.get("fx") for x in issuers.get(stock["code"], {}).get("loans", []))
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
                   rate=100 * interest * annual / average if total and annual and average and interest is not None else None,
                   interest_source=s.get("interest_source", s["cost_name"]),
                   coverage=op / interest if total and interest and op is not None else None, extra=short * 0.01 / 1e8,
                   impact=short / (op * annual) if op and op > 0 and annual else None)
        if not row["fx"] and total and row["rate"] is not None and corporate_rate is not None:
            row["corp_spread"] = round(row["rate"] - corporate_rate, 1)
    return rows


FV_METHODS = ("장부금액과 공정가치 표", "공정가치 모형", "공정가치 문장", "공정가치 서열체계 표")


def fv_usable(record: dict, fv: dict | None) -> bool:
    """fv.json 값이 이 재무상태표와 같은 보고서, 같은 기준, 같은 결산일, 같은 투자부동산 금액에서 읽은 것이고
    장부금액의 0.5~2배 안인지. 하나라도 어긋나면(그날 주석 읽기가 실패해 옛 기록이 남은 경우 등) 감정가 계산을 비운다."""
    cur = (record or {}).get("current") or {}
    return bool(fv) and fv.get("fv") is not None and bool(fv.get("book")) and (
        tuple(fv.get(k) for k in ("rcept_no", "basis", "end", "book"))
        == (record.get("rcept_no"), record.get("basis"), cur.get("end"), cur.get("investment_property"))
        and 0.5 <= fv["fv"] / fv["book"] <= 2)


def appraisal_age(fv: dict) -> dict:
    """감정평가를 새로 하지 않았을 수 있다는 신호. 주석이 공정가치 평가를 다시 하지 않았다고 적었으면 skipped(그 문장의
    평가기준일 포함), 그렇지 않고 같은 주석 표의 전기말 공정가치가 당기와 같으면 same."""
    if fv.get("not_reappraised"):
        return dict(fv_stale="skipped", fv_appraisal_dates=[d for d in fv.get("appraisal_dates") or [] if safe_date(d)])
    if fv.get("method") == "장부금액과 공정가치 표" and fv.get("fv_prior") and fv["fv_prior"] == fv["fv"]:
        return dict(fv_stale="same", fv_appraisal_dates=[])
    return dict(fv_stale=None, fv_appraisal_dates=[])


def nav_fair(record: dict, fv: dict | None, mktcap_eok) -> dict:
    """감정가 기준 NAV = 지배기업 소유주지분 + (투자부동산 공정가치 - 장부금액) × 지배지분 비율.

    공정가치는 사업보고서 주석(dart_fv.py), 자본은 같은 보고서의 재무상태표(dart.py)에서 온다.
    비지배지분이 자본의 5%를 넘으면 공정가치 차익을 지배지분과 나눌 근거가 없어 비운다.
    """
    out = dict(nav_fv_eok=None, pnav_fv=None, pnav_raw=None, pb_dart=None, fv_gain_eok=None, fv_end=None, fv_method=None,
               fv_rcept=None, fv_confirm=[], fv_stale=None, fv_appraisal_dates=[], nav_fv_reason=None)
    cur = (record or {}).get("current") or {}
    if not record or not fv or tuple(fv.get(k) for k in ("rcept_no", "basis", "end", "book")) != (
            record.get("rcept_no"), record.get("basis"), cur.get("end"), cur.get("investment_property")):
        out["nav_fv_reason"] = "공정가치 자료 없음"
        return out
    if fv.get("fv") is None:
        out["nav_fv_reason"] = "투자부동산 없음" if not fv.get("book") else "공정가치 못 읽음"
        return out
    if not fv_usable(record, fv):
        out["nav_fv_reason"] = "공정가치가 장부금액의 0.5~2배 밖"
        return out
    equity = cur.get("equity")
    owner = cur.get("equity_owner")
    if owner is None:
        # 별도 재무제표에는 비지배지분이 없다. 연결인데 지배지분 줄을 못 읽었으면 자본총계 - 비지배지분, 그것도 없으면 비운다
        owner = equity if record.get("basis") != "CFS" else (
            equity - cur["nci"] if equity is not None and cur.get("nci") is not None else None)
    if not equity or equity <= 0 or owner is None:
        out["nav_fv_reason"] = "자본 자료 없음" if not equity or equity <= 0 else "지배지분 자료 없음"
        return out
    gain = fv["fv"] - fv["book"]
    out.update(fv_gain_eok=round(gain / 1e8), fv_end=safe_date(fv["end"]), fv_method=fv["method"],
               fv_rcept=fv["rcept_no"] if re.fullmatch(r"\d{14}", str(fv.get("rcept_no") or "")) else None,
               fv_confirm=[x for x in fv.get("confirm") or [] if x in FV_METHODS], **appraisal_age(fv))
    if abs(equity - owner) > 0.05 * equity:
        out["nav_fv_reason"] = f"비지배지분 {abs(equity - owner) / equity * 100:.0f}%"
        return out
    nav = owner + gain * min(1, owner / equity)
    out["nav_fv_eok"] = round(nav / 1e8)
    if nav <= 0:
        out["nav_fv_reason"] = "감정가 NAV 0 이하"
    elif mktcap_eok:
        out["pnav_raw"] = mktcap_eok * 1e8 / nav
        out["pnav_fv"] = round(out["pnav_raw"], 2)
        # 같은 DART 자본으로 낸 P/B. P/NAV(감정가)와의 차이가 곧 감정차익의 효과다(막대 P/B는 리츠정보시스템 NAV 기준)
        out["pb_dart"] = round(mktcap_eok * 1e8 / owner, 2) if owner > 0 else None
    return out


def build_listed_data() -> dict:
    """상장리츠 비교판 자료. P/B(시가총액 ÷ 리츠정보시스템 NAV), P/FFO, 수익률, 지수는 여기서 계산한다."""
    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    dart = optional_data("dart.json")
    fvdata = optional_data("fv.json")
    rates = rate_summary()
    benchmark = rates.get("ktb10", {}).get("value")
    prices = optional_data("prices.json")
    fin = optional_data("financials.json")
    rows = []
    raw = {}  # 반올림 전 비율. 중앙값은 이 값으로 내야 화면 끝자리가 맞는다
    win_start = (d(snap["date"]) - timedelta(days=372)).isoformat()
    window = {}  # 종목별 최근 1년 종가. 이력은 계속 쌓이지만 화면의 규칙은 이 구간에만 적용한다
    for r in snap["listed"]:
        all_prices = {k: v for k, v in prices.get(str(r["code"]), {}).items() if v}
        hist = {k: v for k, v in all_prices.items() if k >= win_start}
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
        yield_blocked = False
        if dividend_end:
            cutoff = previous_year(d(dividend_end)).isoformat()
            price_day = last_d or snap["date"]
            history = sorted(all_prices)
            yield_blocked = any(cutoff < b <= price_day and abs(all_prices[b] / all_prices[a] - 1) > 0.31
                                for a, b in zip(history, history[1:]))
        mktcap = r["mktcap_eok"] or 0
        nav_eok = round(r["nav_mil"] / 100) if r.get("nav_mil") else None
        ffo_eok = round(r["ffo_mil"] / 100, 1) if r.get("ffo_mil") is not None else None
        ppy = periods_per_year(r, fin.get(str(r.get("reit_id")), {}).get("series", []))
        market_yield = dps / last * 100 if not yield_blocked and dps is not None and last and last > 0 else None
        # 현재 배당 수준: 가장 최근 결산기 주당배당금 × 연간 결산 횟수 ÷ 현재가. 최근 12개월 합계에 섞인 특별배당과 감액을 덜어 낸다
        prof = dividend_profile(dividends)
        run_yield = (prof["dps_last"] * prof["dps_k"] / last * 100
                     if not yield_blocked and not halted and prof["dps_k"] and prof["dps_last"] is not None and last and last > 0 else None)
        # 비율은 원자료(백만원)로 계산한다. 억원으로 반올림한 NAV, FFO로 나누면 끝자리가 틀린다.
        pnav = mktcap / (r["nav_mil"] / 100) if r.get("nav_mil") and r["nav_mil"] > 0 and mktcap else None
        pffo = mktcap / (r["ffo_mil"] / 100 * ppy) if r.get("ffo_mil") and r["ffo_mil"] > 0 and ppy and mktcap else None
        spread = market_yield - benchmark if market_yield is not None and benchmark is not None else None
        fair = nav_fair(dividends, fvdata.get(r["code"]), mktcap)
        # 재무 화면과 같은 LTV(DART 사업보고서 차입금과 사채 ÷ 자산총계, 연결 우선)
        cur_bs = dividends.get("current") or {}
        ltv = cur_bs["total"] / cur_bs["assets"] * 100 if cur_bs.get("assets") and cur_bs.get("total") is not None else None
        raw[r["code"]] = {"pnav": pnav, "spread": spread, "pnav_fv": fair.pop("pnav_raw"), "ltv": ltv}
        rows.append({
            **fair,
            "code": r["code"], "name": r["name"], "sector": r.get("sector") or "기타",
            "has_finance": _has_finance(fin.get(str(r.get("reit_id")))),
            "homepage": safe_url(r.get("homepage")), "listed_date": safe_date(r.get("listed_date")),
            "market_yield": market_yield, "dividend_end": dividend_end or None,
            "run_yield": run_yield, "dps_last": prof["dps_last"], "dps_k": prof["dps_k"],
            "dps_chg": prof["dps_chg"], "dps_jump": prof["dps_jump"],
            "spread": round(spread, 2) if spread is not None else None,
            "market_yield_reason": "기준가 변경" if yield_blocked else None,
            "price": last, "mktcap_eok": mktcap,
            "chg_pct": round((last / hist[dates[-2]] - 1) * 100, 1) if len(dates) >= 2 and last_d not in breaks else None,
            "price_date": last_d,
            "ret_1w": ret(7), "ret_1m": ret(30), "ret_3m": ret(91), "ret_1y": ret(365),
            "dvd_pct": r.get("dvd_pct"), "annual_dvd_pct": r.get("annual_dvd_pct") or None,
            "nav_eok": nav_eok, "ffo_eok": ffo_eok, "periods_per_year": ppy,
            "pnav": round(pnav, 2) if pnav is not None else None,
            "pffo": round(pffo, 1) if pffo is not None else None,
            "wale_years": r.get("wale_years") or None,
            "ltv": round(ltv, 1) if ltv is not None else None,
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
    # 시가총액 가중 지수: 지금의 상장주식 수(시가총액 ÷ 현재가)가 1년 내내 같다고 보고 종가에 곱한다.
    # 그 사이 유상증자나 감자로 주식 수가 바뀐 종목이 있으면 근사다.
    row_by = {str(r["code"]): r for r in rows}
    shares = {c: row_by[c]["mktcap_eok"] * 1e8 / row_by[c]["price"] for c in full
              if row_by[c].get("mktcap_eok") and row_by[c].get("price")}
    idx_cap = []
    if common and len(shares) == len(full):
        cap_base = sum(shares[c] * window[c][common[0]] for c in full)
        idx_cap = [round(sum(shares[c] * window[c][x] for c in full) / cap_base * 100, 2) for x in common]
    # 배당 1년치 포함 근사: 종목별 (마지막 날 종가 + 최근 12개월 주당배당금) ÷ 첫날 종가 - 1의 단순평균.
    # 주당배당금은 DART 사업보고서 기준이라 마지막 결산기가 지수 기간보다 몇 달 앞설 수 있다.
    tr = {c: (window[c][common[-1]] + dart[c]["trailing_dps"]) / window[c][common[0]] - 1
          for c in full if common and (dart.get(c) or {}).get("trailing_dps") is not None}

    # 상장 목록 변동: 최근 90일의 날짜별 수집 원자료에서 종목이 처음과 마지막으로 보인 날.
    # 그날 상장 목록을 받지 못한 원자료(빈 목록)는 건너뛴다.
    list_from = since(d(snap["date"]))
    first_seen, last_seen, seen_name = {}, {}, {}
    snap_days = []
    for p in sorted((DATA / "snapshots").glob("*.json")):
        day = safe_date(p.stem)
        if not day or not (list_from <= day <= snap["date"]):
            continue
        try:
            day_listed = json.loads(p.read_text(encoding="utf-8")).get("listed") or []
        except (OSError, ValueError):
            continue
        if not day_listed:
            continue
        snap_days.append(day)
        for x in day_listed:
            code = str(x.get("code") or "")
            if code:
                first_seen.setdefault(code, day)
                last_seen[code] = day
                seen_name[code] = x.get("name") or code
    current = {str(r["code"]) for r in snap["listed"]}
    list_changes = {"from": snap_days[0] if snap_days else None, "removed": [], "added": []}
    if current and snap_days:
        later = lambda day: min((x for x in snap_days if x > day), default=None)
        list_changes["removed"] = sorted(({"code": c, "name": seen_name[c], "last_seen": last_seen[c],
                                           "missing_from": later(last_seen[c])}
                                          for c in last_seen if c not in current), key=lambda x: x["last_seen"], reverse=True)
        list_changes["added"] = sorted(({"code": c, "name": seen_name[c], "first_seen": first_seen[c]}
                                        for c in current if first_seen.get(c, snap_days[0]) > snap_days[0]),
                                       key=lambda x: x["first_seen"], reverse=True)
    # 빠진 종목에는 DART에서 찾은 가장 최근의 해산, 상장폐지 같은 공시를 붙인다(dart.py update_exits)
    exits = optional_data("exits.json")
    for x in list_changes["removed"]:
        found = (exits.get(x["code"]) or {}).get("filings") or []
        x["exit"] = found[0] if found else None

    def group_stats(name: str, members: list[dict]) -> dict:
        """투자대상별 중앙값. P/B, 감정가 P/NAV, LTV는 반올림 전 값으로 낸다. 시가배당률은 요약 칸처럼 시가총액 가중 평균도 낸다.
        1년 수익률은 주가 지수처럼 거래가 멈춘 종목을 뺀다."""
        vals = {k: [raw[r["code"]][k] for r in members if raw[r["code"]][k] is not None] for k in ("pnav", "pnav_fv", "ltv")}
        ylds = [r["market_yield"] for r in members if r["market_yield"] is not None]
        wts = [(r["market_yield"], r["mktcap_eok"]) for r in members if r["market_yield"] is not None and r["mktcap_eok"]]
        rets = [r["ret_1y"] for r in members if r["ret_1y"] is not None and not r["halted"]]
        return {"name": name, "n": len(members), "mktcap_eok": sum(r["mktcap_eok"] for r in members),
                "pnav_median": _median(vals["pnav"], 2), "pnav_n": len(vals["pnav"]),
                "pnav_fv_median": _median(vals["pnav_fv"], 2), "pnav_fv_n": len(vals["pnav_fv"]),
                "yield_median": _median(ylds, 2), "yield_n": len(ylds),
                "yield_wavg": round(sum(y * w for y, w in wts) / sum(w for _, w in wts), 2) if wts else None,
                "ltv_median": _median(vals["ltv"], 1), "ltv_n": len(vals["ltv"]),
                "ret_1y_median": _median(rets, 1), "ret_1y_n": len(rets)}

    sec_names = sorted({r["sector"] for r in rows})
    sectors = sorted((group_stats(s, [r for r in rows if r["sector"] == s]) for s in sec_names),
                     key=lambda x: (-x["mktcap_eok"], x["name"]))
    # 리츠정보시스템 NAV가 결산기 재무상태표의 자본총계와 같은지(같으면 감정평가 차익이 빠진 장부가 기준)
    nav_book = []
    for r in snap["listed"]:
        series = fin.get(str(r.get("reit_id")), {}).get("series", [])
        same_end = next((x for x in series if x.get("end") == r.get("fiscal_end")), None)
        if r.get("nav_mil") and same_end and same_end.get("equity") is not None:
            nav_book.append((r["name"], abs(r["nav_mil"] * 1e6 - same_end["equity"]) < 1e6))
    # 리츠정보시스템 NAV가 장부 자본과 다른 종목은 DART 주석으로 계산한 감정가 NAV와 견준다(계산 방식 검산)
    nav_diff = {name for name, ok in nav_book if not ok}
    fv_cross = sorted(({"name": r["name"], "nav_eok": r["nav_eok"], "nav_end": r["fiscal_end"],
                        "nav_fv_eok": r["nav_fv_eok"], "fv_end": r["fv_end"]}
                       for r in rows if r["name"] in nav_diff and r["nav_fv_eok"] is not None), key=lambda x: x["name"])
    pnavs = [r["pnav"] for r in rows if r["pnav"] is not None]  # 1배 미만 개수와 최저, 최고는 화면에 보이는 값으로 센다
    pnav_raw = [v["pnav"] for v in raw.values() if v["pnav"] is not None]
    yields = [r["market_yield"] for r in rows if r["market_yield"] is not None]
    weighted = [(r["market_yield"], r["mktcap_eok"]) for r in rows if r["market_yield"] is not None and r["mktcap_eok"]]
    yield_wavg = sum(y * w for y, w in weighted) / sum(w for _, w in weighted) if weighted else None
    runs = [r["run_yield"] for r in rows if r["run_yield"] is not None]
    spreads = [v["spread"] for v in raw.values() if v["spread"] is not None]
    dvds = [r["annual_dvd_pct"] for r in rows if r["annual_dvd_pct"]]
    changes = [r["ret_1y"] for r in rows if str(r["code"]) in full and r["ret_1y"] is not None] if common else []
    excluded = Counter("기준가 변경" if r["price_break"] else "장기 동일 종가와 거래량 없음" if r["halted"]
                       else "종가 이력 부족" for r in rows if not common or str(r["code"]) not in full)
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "price_as_of": max((r["price_date"] for r in rows if r["price_date"]), default=None),
        "summary": {
            "n": len(rows), "mktcap_eok": sum(r["mktcap_eok"] for r in rows),
            "index_ret": round(idx[-1] - 100, 1) if idx else None, "index_n": len(full) if idx else 0,
            "index_cap_ret": round(idx_cap[-1] - 100, 1) if idx_cap else None,
            "index_tr": round(sum(tr.values()) / len(tr) * 100, 1) if tr else None, "index_tr_n": len(tr),
            "index_tr_missing": [row_by[c]["name"] for c in full if c not in tr] if idx else [],
            "up_1y": sum(1 for v in changes if v > 0), "down_1y": sum(1 for v in changes if v < 0),
            "pnav_median": _median(pnav_raw, 2), "pnav_n": len(pnavs),
            "pnav_fv_median": _median([v["pnav_fv"] for v in raw.values() if v["pnav_fv"] is not None], 2),
            "pnav_fv_n": sum(1 for v in raw.values() if v["pnav_fv"] is not None),
            "pnav_fv_below1": sum(1 for r in rows if r["pnav_fv"] is not None and r["pnav_fv"] < 1),
            "pnav_fv_stale": sum(1 for r in rows if r["pnav_fv"] is not None and r.get("fv_stale")),
            "nav_book_n": len(nav_book), "nav_book_same": sum(ok for _, ok in nav_book),
            "nav_book_diff": [name for name, ok in nav_book if not ok], "fv_cross": fv_cross,
            "pnav_below1": sum(1 for v in pnavs if v < 1), "pnav_min": min(pnavs, default=None), "pnav_max": max(pnavs, default=None),
            "yield_median": _median(yields, 2), "yield_n": len(yields),
            "yield_wavg": round(yield_wavg, 2) if yield_wavg is not None else None,
            "spread_wavg": round(yield_wavg - benchmark, 2) if yield_wavg is not None and benchmark is not None else None,
            "run_median": _median(runs, 2), "run_n": len(runs),
            "jump": [r["name"] for r in rows if r["dps_jump"]],
            "rates": rates, "spread_median": _median(spreads, 2), "spread_n": len(spreads),
            "dvd_median": _median(dvds, 2), "dvd_n": len(dvds),
        },
        "index": {"dates": common, "values": idx, "cap": idx_cap, "excluded": sorted(skip.values()), "exclusion_counts": excluded},
        "sectors": sectors, "sector_all": group_stats("전체", rows), "rows": rows, "list_changes": list_changes,
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


# 수시공시 유형 묶음(화면의 필터 단추). 여기에 없는 유형은 '정기, 일반'으로 센다.
DISCLOSURE_GROUPS = {
    "RT02100018": "위험 신호", "RT02100019": "위험 신호", "RT02100027": "위험 신호",
    "RT02100021": "자산 취득, 처분", "RT02100022": "자산 취득, 처분",
    "RT02100006": "자산 취득, 처분",  # 실사보고서: 부동산을 취득하거나 처분할 때 작성(법 24조 ③)
    "RT02100023": "신용평가", "RT02100024": "최대주주 변경",
    "RT02100028": "이해관계자 거래", "RT02100029": "차입, 사채",
}
# 유형 코드가 '기타'이거나 투자설명서인 공시는 본문의 말로 다시 나눈다(앞의 규칙이 먼저).
TEXT_CODES = {"RT02100020", "RT02100002", "RT02100005"}
TEXT_RULES = [
    ("위반, 제재", re.compile(r"기한\s?내\s?공시\s?하지|공시\s?기한.{0,6}(?:미준수|위반|경과)|지연\s?공시|위반\s?하였|위반했"
                          r"|과태료|과징금|기관\s?(?:경고|주의)|영업\s?정지|시정\s?명령|제재")),
    ("소송, 분쟁", re.compile(r"소송|판결|중재|가압류|가처분")),
    ("이해관계자 거래", re.compile(r"주요\s?주주인|이해관계인|이해관계자와")),
    ("자산 취득, 처분", re.compile(r"실사\s?보고서|본\s?계약\s?체결|(?:토지|부동산|자산|건물|사전)\s?매매\s?계약"
                                r"|(?:부동산|자산)의?\s?(?:취득|매각|처분)|양수도\s?계약|기부\s?채납")),
    ("인가 신청, 기금 출자", re.compile(r"(?:영업\s?인가|영업\s?등록|변경\s?인가|변경\s?등록|설립\s?신고)\s?(?:신청|신고)|기금\s?출자")),
    ("차입, 사채", re.compile(r"대출\s?약정|차입\s?(?:계약|약정)|사채|리파이낸싱|대환")),
    ("자본 변동", re.compile(r"유상\s?증자|무상\s?증자|신주의?\s?발행|현물\s?출자|자본금?\s?감소|감자|투자설명서|증권신고서|투자자\s?모집")),
    ("기타 공시", re.compile(r"무산|부결")),  # 주주총회 무산이나 안건 부결은 의사록이어도 목록에 둔다
]
# 규칙에 걸리지 않고 의사록, 정기 보고서, 정관, 임원 변경으로만 보이는 '기타' 공시는 정기, 일반 공시로 센다.
ROUTINE_WORDS = re.compile(r"의사록|주주\s?총회|감사\s?보고서|투자\s?보고서|영업\s?보고서|재무제표|정관|조문|내부\s?통제|위탁\s?계약"
                           r"|이사.{0,12}(?:선임|사임|중임|해임)|\((?:사임|취임|선임|중임|해임)\)")
GROUP_ORDER = ["위험 신호", "해산, 청산", "위반, 제재", "소송, 분쟁", "자산 취득, 처분", "인가 신청, 기금 출자",
               "자본 변동", "차입, 사채", "신용평가", "최대주주 변경", "이해관계자 거래", "기타 공시"]
# 유형 코드가 '기타'이거나 정기 공시여도 제목과 본문에 아래 말이 있으면 따로 묶는다.
# (회생절차나 사채권자집회 공시가 '기타'로 올라와 위험 신호에서 빠지던 것을 바로잡는다.)
RISK_WORDS = re.compile(r"회생절차|회생법원|사채권자\s?집회|기한의?\s?이익.{0,6}상실|미상환|부도|파산|채무\s?불이행|자율\s?(?:구조조정|협약)"
                        r"|캐시\s?트랩(?!.{0,6}(?:해제|해소|종료))")
WIND_UP_WORDS = re.compile(r"해산|청산")


def dis_group(x: dict) -> str | None:
    """수시공시 한 건의 화면 묶음. 없으면 정기, 일반 공시(None)."""
    code = x.get("matter_code")
    grp = DISCLOSURE_GROUPS.get(code)
    if grp:
        return grp
    text = x.get("text") or ""
    words = f"{x.get('matter') or ''} {text}"
    if RISK_WORDS.search(words):
        return "위험 신호"
    if WIND_UP_WORDS.search(words):
        return "해산, 청산"
    if code not in TEXT_CODES:
        return None
    for name, rx in TEXT_RULES:
        if rx.search(text):
            return name
    return None if ROUTINE_WORDS.search(text) else "기타 공시"


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
    dart_fs = optional_data("dart.json")
    fv_fs = optional_data("fv.json")
    raw_ltv_fv, raw_ltv_fv_book = [], []

    rows, timelines, raw_fin = [], {}, []
    for r in snap["listed"]:
        f = fin.get(str(r.get("reit_id")))
        if not _has_finance(f):
            continue
        raw_fin.append((f["assets"], f["debt"]))
        timelines[r["code"]] = _finance_events(f["series"])
        ser = [x for x in f["series"] if x.get("assets") and x.get("debt") is not None]
        last = ser[-1] if ser else {}
        m = re.search(r"(\d+기)\s*(결산기|\d분기|반기)?", (last.get("label") or "").replace("\\n", " "))
        # 추이의 마지막 줄은 분기 말일 수 있다. 영업수익과 영업이익은 가장 최근 '결산기' 줄에서 가져온다.
        fy = next((x for x in reversed(ser) if "결산기" in (x.get("label") or "") and x.get("revenue") is not None), None)
        rev = (fy or {}).get("revenue")
        # FFO는 리츠정보시스템 성과지표의 최근 결산기 값 × 연간 결산 횟수(상장리츠 화면의 P/FFO와 같은 기준)
        ppy = periods_per_year(r, f["series"])
        # LTV = DART 사업보고서 재무상태표의 차입금과 사채 합계 ÷ 자산총계(연결 재무제표가 있으면 연결)
        drec = dart_fs.get(r["code"]) or {}
        dcur = drec.get("current") or {}
        ltv = dcur["total"] / dcur["assets"] * 100 if dcur.get("assets") and dcur.get("total") is not None else None
        # 감정가 LTV: 분모의 투자부동산을 같은 사업보고서 주석의 공정가치로 바꾼다(자산총계 + 공정가치 - 장부금액)
        fvr = fv_fs.get(r["code"]) or {}
        ltv_fv = (dcur["total"] / (dcur["assets"] + fvr["fv"] - fvr["book"]) * 100
                  if ltv is not None and fv_usable(drec, fvr) and dcur["assets"] + fvr["fv"] - fvr["book"] > 0 else None)
        if ltv_fv is not None:
            raw_ltv_fv.append(ltv_fv)
            raw_ltv_fv_book.append(ltv)
        rows.append({
            "code": r["code"], "name": r["name"], "sector": r.get("sector") or "기타",
            "homepage": safe_url(r.get("homepage")), "mktcap_eok": r.get("mktcap_eok"),
            "fy_end": safe_date(fy["end"]) if fy else None,
            "as_of": safe_date(last.get("end")), "period": " ".join(x for x in (m.groups() if m else ()) if x),
            "assets_eok": eok(f["assets"]), "debt_eok": eok(f["debt"]), "equity_eok": eok(f["equity"]),
            "debt_to_assets": round(f["debt"] / f["assets"] * 100, 1),
            "debt_to_equity": round(f["debt"] / f["equity"], 2) if f.get("equity") and f["equity"] > 0 else None,
            "ltv": round(ltv, 1) if ltv is not None else None,
            "ltv_basis": ("연결" if drec.get("basis") == "CFS" else "별도") if ltv is not None else None,
            "ltv_end": safe_date(dcur.get("end")) if ltv is not None else None,
            "ltv_rcept": (drec["rcept_no"] if ltv is not None and re.fullmatch(r"\d{14}", str(drec.get("rcept_no") or ""))
                          else None),
            "borrow_eok": eok(dcur["total"]) if ltv is not None else None,
            "ltv_fv": round(ltv_fv, 1) if ltv_fv is not None else None,
            "fv_stale": appraisal_age(fvr)["fv_stale"] if ltv_fv is not None else None,
            "revenue_eok": round(rev / 1e8, 1) if rev is not None else None,
            "ffo_y_eok": round(r["ffo_mil"] / 100 * ppy) if r.get("ffo_mil") is not None and ppy else None,
            "ffo_end": safe_date(r.get("fiscal_end")) if r.get("ffo_mil") is not None and ppy else None,
            "top_shareholder": shareholder_label(f.get("top_shareholder")),
            "series": [{"end": safe_date(x["end"]), "assets_eok": eok(x["assets"]), "debt_eok": eok(x["debt"]),
                        "debt_to_assets": round(x["debt"] / x["assets"] * 100, 1)} for x in ser],
        })
    # 부채 비중 20% 기준은 화면에 보이는 값으로 가르고, 중앙값과 합계는 반올림 전 원 단위 값으로 낸다
    da = [r["debt_to_assets"] for r in rows]
    raw_da = [(shown, debt / assets * 100) for shown, (assets, debt) in zip(da, raw_fin)]
    hi = max(rows, key=lambda r: r["debt_to_assets"], default=None)
    lo = min(rows, key=lambda r: r["debt_to_assets"], default=None)
    # LTV 중앙값도 반올림 전 값으로 낸다. 별도 기준 부채 비중이 20% 미만인데 연결 기준 차입 비율이 그보다 높으면
    # 리츠 바깥(자회사)에 차입이 있다는 뜻이라 자회사를 거쳐 보유하는 구조로 본다.
    ltv_rows = [r for r in rows if r["ltv"] is not None]
    raw_ltv = [dart_fs[r["code"]]["current"]["total"] / dart_fs[r["code"]]["current"]["assets"] * 100 for r in ltv_rows]
    ltv_hi = max(ltv_rows, key=lambda r: r["ltv"], default=None)
    via_sub = [r for r in ltv_rows if r["debt_to_assets"] < 20 and r["ltv_basis"] == "연결" and r["borrow_eok"]
               and r["ltv"] > r["debt_to_assets"]]

    # 공시의 회사명과 상장 목록의 회사명은 원문에서 표기가 다를 수 있어, 리츠조회 목록의 이름도 함께 쓴다
    reit_name_by_id = {r["id"]: r["name"] for r in snap["reits"]}
    listed_names = {}
    for r in snap["listed"]:
        for nm in (r.get("company"), reit_name_by_id.get(r.get("reit_id"))):
            if nm:
                listed_names[_norm_company(nm)] = r
    cut90 = since(today)
    items, routine90 = [], 0
    for no, x in dis.items():
        grp = dis_group(x)
        stock = listed_names.get(_norm_company(x["company"]))
        code = stock["code"] if stock else None
        day = safe_date(x.get("date"))
        body = "" if "주주" in x["matter"] else (mask_officer_names(x.get("text")) or "")
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
    loans = loan_schedule(snap["listed"], snap["date"])
    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "fin_fetched": max((f["fetched"] for f in fin.values()), default=None),
        "summary": {
            "n": len(rows), "da_median": _median([v for _, v in raw_da], 1),
            "da_hi": {"name": hi["name"], "v": hi["debt_to_assets"]} if hi else None,
            "da_lo": {"name": lo["name"], "v": lo["debt_to_assets"]} if lo else None,
            "de_over2": sum(1 for r in rows if r["debt_to_equity"] and r["debt_to_equity"] > 2),
            "da_under20": sum(1 for r in rows if r["debt_to_assets"] < 20),
            "da_median_ex": _median([v for shown, v in raw_da if shown >= 20], 1),
            "ltv_median": _median(raw_ltv, 1), "ltv_n": len(ltv_rows),
            "ltv_cfs": sum(1 for r in ltv_rows if r["ltv_basis"] == "연결"),
            "ltv_hi": {"name": ltv_hi["name"], "v": ltv_hi["ltv"]} if ltv_hi else None,
            "via_sub": len(via_sub),
            "ltv_fv_median": _median(raw_ltv_fv, 1), "ltv_fv_n": len(raw_ltv_fv),
            "ltv_fv_book_median": _median(raw_ltv_fv_book, 1),
            "assets_eok": round(sum(a for a, _ in raw_fin) / 1e8), "debt_eok": round(sum(b for _, b in raw_fin) / 1e8),
            "dis_major": len(items), "dis_routine": routine90, "dis_risk": counts["위험 신호"],
            "dis_from": cut90,
        },
        "rows": rows, "groups": [{"name": g, "n": counts[g]} for g in GROUP_ORDER if counts[g]],
        "disclosures": items,
        "rates": rates,
        "loans": loans,
        "maturity": maturity_rows(snap["listed"], loans["issuers"], rates.get("corp_aa", {}).get("value")),
        "timelines": timelines,
        "dis_start": min((day for x in dis.values() if (day := safe_date(x.get("date")))), default=None),
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


PAGES = [("index", "./", "인가와 프로젝트리츠"), ("listed", "listed.html", "상장리츠"),
         ("finance", "finance.html", "재무 구조와 공시"), ("calc", "calc.html", "사업성 계산기")]


def weekly_digest(data: dict, listed: dict, finance: dict) -> dict:
    """첫 화면의 '최근 1주 동향'. 네 화면에서 이미 계산한 자료 가운데 최근 7일(수집일 포함)에 생긴 일만 모은다."""
    end = data["date"]
    start = (d(end) - timedelta(days=6)).isoformat()
    flog = json.loads((DATA / "filings_log.json").read_text(encoding="utf-8"))

    new_reits = [{k: r[k] for k in ("date", "kind", "name_s", "amc_s", "sector", "project")}
                 for r in data["reits"] if r["date"] >= start]
    received = [{k: f[k] for k in ("date", "req_type", "name_s", "applicant", "status")}
                for f in data["filings"] if f["date"] >= start]
    changed, gone = [], []
    for rec in flog.values():
        name, company = short_reit(mask_person(rec["name"])), applicant_name(rec["applicant"])
        history = rec.get("status_history") or []
        for (_, before), (day, after) in zip(history, history[1:]):
            if start <= day <= end:
                changed.append({"date": day, "name_s": name, "applicant": company, "before": before, "after": after})
        if rec.get("gone_on") and start <= rec["gone_on"] <= end:
            gone.append({"date": rec["gone_on"], "name_s": name, "applicant": company, "req_type": rec["req_type"]})
    changed.sort(key=lambda x: (x["date"], x["name_s"]), reverse=True)
    gone.sort(key=lambda x: (x["date"], x["name_s"]), reverse=True)

    dis = [x for x in finance["disclosures"] if start <= x["date"] <= end]
    groups = Counter(x["group"] for x in dis)
    notable = [{k: x[k] for k in ("date", "group", "matter", "company_s", "timeline_code")}
               for x in dis if x["group"] in ("위험 신호", "해산, 청산", "위반, 제재", "소송, 분쟁", "자산 취득, 처분",
                                              "인가 신청, 기금 출자", "신용평가", "최대주주 변경")]

    # 상장리츠: 지수와 종목 수익률은 마지막 종가일과 그 7일 전(그날이나 그 앞의 마지막 거래일)을 견준다
    idx = list(zip(listed["index"]["dates"], listed["index"]["values"]))
    index_week = None
    if idx:
        base = [(day, v) for day, v in idx if day <= (d(idx[-1][0]) - timedelta(days=7)).isoformat()]
        if base:
            index_week = {"from": base[-1][0], "to": idx[-1][0],
                          "ret": round((idx[-1][1] / base[-1][1] - 1) * 100, 1)}
    movers = sorted(({"code": r["code"], "name": r["name"], "ret": r["ret_1w"]} for r in listed["rows"]
                     if r.get("ret_1w") is not None and not r.get("halted")),
                    key=lambda x: (x["ret"], x["name"]))

    rates = {}
    for code, series in optional_data("rates.json").get("series", {}).items():
        values = series.get("values") or {}
        if code not in ("ktb10", "corp_aa") or not values:
            continue
        latest = max(values)
        prev = max((day for day in values if day <= (d(latest) - timedelta(days=7)).isoformat()), default=None)
        rates[code] = {"value": values[latest], "date": latest, "prev_date": prev,
                       "change": round(values[latest] - values[prev], 3) if prev else None}

    return {"start": start, "end": end, "log_from": data["first_log"],
            "new_reits": new_reits, "received": received, "changed": changed, "gone": gone,
            "dis_n": len(dis), "dis_groups": {g: groups[g] for g in GROUP_ORDER if groups[g]}, "notable": notable,
            "index": index_week, "index_n": listed["summary"].get("index_n"),
            "up": sum(m["ret"] > 0 for m in movers), "down": sum(m["ret"] < 0 for m in movers),
            "flat": sum(m["ret"] == 0 for m in movers),
            "top": movers[::-1][:3], "bottom": movers[:3], "rates": rates}


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
    headings = re.findall(r'<h2 id="([^"]+)"[^>]*>([^<]+)</h2>', tpl)
    links = "".join(f'<a href="#{anchor}">{title}</a>' for anchor, title in headings)
    html = html.replace("</header>", f'<nav class="sections" aria-label="구역 바로가기">{links}</nav>\n</header>')
    if "/*__FOLD_TABLE__*/" in html:
        html = html.replace("/*__FOLD_TABLE__*/", (ROOT / "fold_table.js").read_text(encoding="utf-8"))
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
    listed = build_listed_data()
    finance = build_finance_data()
    data["weekly"] = weekly_digest(data, listed, finance)
    n_index = render("template.html", "index.html", data, "index")
    n_listed = render("template_listed.html", "listed.html", listed, "listed")
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
          f"P/B 중앙값 {ls['pnav_median']}(1배 미만 {ls['pnav_below1']}/{ls['pnav_n']}), "
          f"시가배당률 가중 {ls['yield_wavg']}%(중앙값 {ls['yield_median']}%) | listed.html {n_listed:,}자")
    fs = finance["summary"]
    print(f"[빌드] 재무 구조 {fs['n']}개, LTV 중앙값 {fs['ltv_median']}%({fs['ltv_n']}개), 부채/자산 중앙값 {fs['da_median']}%, "
          f"최근 90일 주요 공시 {fs['dis_major']}건(위험 신호 {fs['dis_risk']}건), 정기와 일반 {fs['dis_routine']}건 | "
          f"finance.html {n_fin:,}자")
    print(f"[빌드] 사업성 계산기 calc.html {n_calc:,}자")


if __name__ == "__main__":
    main()
