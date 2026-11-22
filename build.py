"""data/latest.json과 누적 기록을 읽어 site/index.html 한 장을 만든다.

집계는 전부 여기(파이썬)에서 하고, 화면(template.html)은 받은 숫자를 그리기만 한다.
실행 중에 AI나 외부 서비스를 부르지 않으므로 같은 자료면 같은 화면이 나온다.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

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


def month_add(y: int, m: int, k: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + k
    return idx // 12, idx % 12 + 1


def build_data() -> dict:
    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    flog = json.loads((DATA / "filings_log.json").read_text(encoding="utf-8"))
    today = d(snap["date"])
    reits = snap["reits"]
    filings = snap["filings"]

    for r in reits:
        r["amc_s"] = clean_company(r["amc"])
        r["name_s"] = short_reit(r["name"])
        r["assets_eok"] = r["assets_eok"] or 0
        r["project"] = "Y" if r["kind"] == "신고" or r["project"] == "Y" else "N"

    cut12 = today - timedelta(days=365)
    cut24 = today - timedelta(days=730)
    new12 = [r for r in reits if d(r["date"]) > cut12]
    prev12 = [r for r in reits if cut24 < d(r["date"]) <= cut12]
    proj = sorted([r for r in reits if r["project"] == "Y"], key=lambda r: r["date"], reverse=True)

    # 요약
    summary = {
        "n_reits": len(reits),
        "assets_eok": sum(r["assets_eok"] for r in reits),
        "n_amc": len({r["amc"] for r in reits}),
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
            "latest": max(r["date"] for r in rows),
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
        frows.append({
            "date": f["date"],
            "days": (today - d(f["date"])).days,
            "req_type": f["req_type"],
            "name_s": short_reit(f["name"]),
            "project_name": short_reit(f["project_name"] or ""),
            "biz_type": f["biz_type"] or "",
            "applicant": clean_company(f["applicant"]),
            "status": f["status"],
            "existing": f["reit_id"] in reit_ids,
            "first_seen": rec.get("first_seen"),
        })

    # 접수 기록에서 처리 완료(목록에서 빠진 건) 집계: 기록이 쌓인 뒤에 의미가 생김
    done = []
    reit_by_id = {r["id"]: r for r in reits}
    for rec in flog.values():
        if rec.get("gone_on"):
            r = reit_by_id.get(rec.get("reit_id"))
            done.append({
                "date": rec["date"], "gone_on": rec["gone_on"], "req_type": rec["req_type"],
                "name_s": short_reit(rec["name"]), "applicant": clean_company(rec["applicant"]),
                "result_kind": r["kind"] if r else None,
                "result_date": r["date"] if r else None,
                "days_to_result": (d(r["date"]) - d(rec["date"])).days
                if r and rec["req_type"] != "변경인가/등록/신고" and d(r["date"]) >= d(rec["date"]) else None,
            })
    done.sort(key=lambda x: x["gone_on"], reverse=True)
    log_days = len(list((DATA / "snapshots").glob("*.json")))
    first_log = min((p.stem for p in (DATA / "snapshots").glob("*.json")), default=snap["date"])

    # 최근 변동(접수와 인가, 등록, 신고를 날짜순으로)
    events = []
    for r in reits:
        if d(r["date"]) > today - timedelta(days=90):
            events.append({"date": r["date"], "what": r["kind"], "name_s": r["name_s"],
                           "who": r["amc_s"], "note": r["sector"]})
    for f in frows:
        events.append({"date": f["date"], "what": "접수", "name_s": f["name_s"],
                       "who": f["applicant"], "note": f["req_type"]})
    events.sort(key=lambda e: (e["date"], e["what"] != "접수"), reverse=True)

    slim = [{k: r[k] for k in ("id", "date", "kind", "name_s", "amc_s", "type", "sector",
                               "public_offering", "assets_eok", "dividend_pct", "project")}
            for r in sorted(reits, key=lambda r: (r["date"], r["id"]), reverse=True)]

    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "summary": summary, "yearly": yearly, "monthly": monthly, "sector": sector,
        "amc": amc, "filings": frows, "done": done, "log_days": log_days, "first_log": first_log,
        "events": events[:40], "reits": slim,
        "kinds": KINDS, "sectors": SECTORS,
        "highlight_amc": cfg.get("highlight_amc", ""),
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    data = build_data()
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", payload)
    if html == tpl:
        raise RuntimeError("template.html에서 자료를 넣을 자리를 찾지 못함")
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(html, encoding="utf-8")
    s = data["summary"]
    print(f"[빌드] {data['collected_at']} | 리츠 {s['n_reits']}개, 자산총계 {s['assets_eok']/10000:.2f}조원, "
          f"자산관리회사 {s['n_amc']}곳 | 최근 12개월 {s['new12']}건(직전 {s['prev12']}건) | "
          f"프로젝트리츠 {s['proj_n']}건 | 접수 {s['filings_n']}건 | index.html {len(html):,}자")


if __name__ == "__main__":
    main()
