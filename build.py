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


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def build_listed_data() -> dict:
    """상장리츠 비교판 자료. P/NAV, P/FFO, 수익률, 지수는 여기서 계산한다."""
    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    prices = json.loads((DATA / "prices.json").read_text(encoding="utf-8"))
    rows = []
    for r in snap["listed"]:
        hist = prices.get(str(r["code"]), {})
        dates = sorted(hist)
        last_d = dates[-1]
        last = hist[last_d]

        # 하루 변동이 가격제한폭(30%)을 넘으면 감자, 권리락 등으로 기준가가 바뀐 날로 본다
        breaks = [b for a, b in zip(dates, dates[1:]) if abs(hist[b] / hist[a] - 1) > 0.31]
        flat_days = sum(1 for a, b in zip(dates, dates[1:]) if hist[a] == hist[b])
        halted = flat_days >= 60 and not r.get("volume")

        def ret(days: int):
            target = (d(last_d) - timedelta(days=days)).isoformat()
            prev = [x for x in dates if x <= target]
            if not prev or any(b > prev[-1] for b in breaks):
                return None  # 기준가가 바뀐 날을 사이에 둔 구간은 비교하지 않음
            return round((last / hist[prev[-1]] - 1) * 100, 1)

        mktcap = r["mktcap_eok"] or 0
        nav_eok = round(r["nav_mil"] / 100) if r.get("nav_mil") else None
        ffo_eok = round(r["ffo_mil"] / 100, 1) if r.get("ffo_mil") is not None else None
        ppy = None  # 연간 결산 횟수: 연환산 배당률 / 배당수익률로 추정
        if r.get("dvd_pct") and r.get("annual_dvd_pct"):
            ppy = min(4, max(1, round(r["annual_dvd_pct"] / r["dvd_pct"])))
        rows.append({
            "code": r["code"], "name": r["name"], "sector": r.get("sector") or "기타",
            "homepage": r.get("homepage"), "listed_date": r.get("listed_date"),
            "price": r["price"], "mktcap_eok": mktcap,
            "chg_pct": round((r["price"] / r["prev_price"] - 1) * 100, 2) if r.get("prev_price") else None,
            "ret_1m": ret(30), "ret_3m": ret(91), "ret_1y": ret(365),
            "dvd_pct": r.get("dvd_pct"), "annual_dvd_pct": r.get("annual_dvd_pct"),
            "nav_eok": nav_eok, "ffo_eok": ffo_eok, "periods_per_year": ppy,
            "pnav": round(mktcap / nav_eok, 2) if nav_eok and nav_eok > 0 and mktcap else None,
            "pffo": round(mktcap / (ffo_eok * ppy), 1) if ffo_eok and ffo_eok > 0 and ppy and mktcap else None,
            "wale_years": r.get("wale_years") or None,
            "price_break": breaks[-1] if breaks else None, "halted": halted,
            "fiscal_end": r.get("fiscal_end"), "fiscal_period": r.get("fiscal_period"),
            "spark": [hist[x] for x in dates[::5]] + ([last] if (len(dates) - 1) % 5 else []),
        })

    # 단순평균 지수: 1년 구간 내내 종가가 있는 종목만, 모든 종목에 종가가 있는 날짜만, 첫날 = 100
    codes = [str(r["code"]) for r in snap["listed"]]
    start_day = (d(snap["date"]) - timedelta(days=366)).isoformat()
    all_days = sorted(x for x in set().union(*[set(prices[c]) for c in codes]) if x >= start_day)
    first_week = all_days[min(5, len(all_days) - 1)]
    skip = {str(r["code"]): r["name"] for r in rows if r["price_break"] or r["halted"]}
    full = [c for c in codes if c not in skip and any(x <= first_week and x >= start_day for x in prices[c])]
    common = sorted(x for x in set.intersection(*[set(prices[c]) for c in full]) if x >= start_day)
    idx = [round(sum(prices[c][x] / prices[c][common[0]] for c in full) / len(full) * 100, 2) for x in common]

    sec_names = sorted({r["sector"] for r in rows})
    sectors = sorted(({"name": s, "n": sum(1 for r in rows if r["sector"] == s),
                       "mktcap_eok": sum(r["mktcap_eok"] for r in rows if r["sector"] == s)} for s in sec_names),
                     key=lambda x: -x["mktcap_eok"])
    pnavs = [r["pnav"] for r in rows if r["pnav"] is not None]
    dvds = [r["annual_dvd_pct"] for r in rows if r["annual_dvd_pct"]]
    r1y = [r["ret_1y"] for r in rows if r["ret_1y"] is not None]
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "price_as_of": max((r.get("as_of") or "") for r in snap["listed"]),
        "summary": {
            "n": len(rows), "mktcap_eok": sum(r["mktcap_eok"] for r in rows),
            "index_ret": round(idx[-1] - 100, 1), "index_n": len(full),
            "up_1y": sum(1 for v in r1y if v > 0), "down_1y": sum(1 for v in r1y if v < 0),
            "pnav_median": round(_median(pnavs), 2), "pnav_n": len(pnavs),
            "pnav_below1": sum(1 for v in pnavs if v < 1), "pnav_min": min(pnavs), "pnav_max": max(pnavs),
            "dvd_median": round(_median(dvds), 2), "dvd_n": len(dvds),
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


def build_finance_data() -> dict:
    """상장리츠 재무 구조와 리츠 주요 공시."""
    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    fin = json.loads((DATA / "financials.json").read_text(encoding="utf-8"))
    dis = json.loads((DATA / "disclosures.json").read_text(encoding="utf-8"))
    today = d(snap["date"])
    eok = lambda won: round(won / 1e8) if won is not None else None

    rows = []
    for r in snap["listed"]:
        f = fin.get(str(r.get("reit_id")))
        if not f or not f.get("assets"):
            continue
        ser = [x for x in f["series"] if x.get("assets")]
        last = ser[-1] if ser else {}
        m = re.search(r"(\d+기)\s*(결산기|\d분기|반기)?", (last.get("label") or "").replace("\\n", " "))
        rev, op = last.get("revenue"), last.get("op_profit")
        rows.append({
            "code": r["code"], "name": r["name"], "sector": r.get("sector") or "기타",
            "homepage": r.get("homepage"), "mktcap_eok": r.get("mktcap_eok"),
            "as_of": last.get("end"), "period": " ".join(x for x in (m.groups() if m else ()) if x),
            "assets_eok": eok(f["assets"]), "debt_eok": eok(f["debt"]), "equity_eok": eok(f["equity"]),
            "debt_to_assets": round(f["debt"] / f["assets"] * 100, 1),
            "debt_to_equity": round(f["debt"] / f["equity"], 2) if f.get("equity") and f["equity"] > 0 else None,
            "revenue_eok": round(rev / 1e8, 1) if rev is not None else None,
            "op_margin": round(op / rev * 100, 1) if rev and op is not None else None,
            "top_shareholder": clean_company(f.get("top_shareholder") or ""),
            "series": [{"end": x["end"], "assets_eok": eok(x["assets"]), "debt_eok": eok(x["debt"]),
                        "debt_to_assets": round(x["debt"] / x["assets"] * 100, 1)} for x in ser],
        })
    da = [r["debt_to_assets"] for r in rows]
    hi = max(rows, key=lambda r: r["debt_to_assets"])
    lo = min(rows, key=lambda r: r["debt_to_assets"])

    listed_names = {_norm_company(r["company"]): r["name"] for r in snap["listed"]}
    cut90 = (today - timedelta(days=90)).isoformat()
    items, routine90 = [], 0
    for no, x in dis.items():
        grp = DISCLOSURE_GROUPS.get(x.get("matter_code"))
        if (x.get("date") or "") < cut90:
            continue
        if not grp:
            routine90 += 1
            continue
        items.append({"no": no, "date": x["date"], "group": grp, "matter": x["matter"],
                      "company_s": short_reit(x["company"]), "type": x.get("company_type") or "",
                      "listed": listed_names.get(_norm_company(x["company"])), "text": x.get("text") or ""})
    items.sort(key=lambda z: (z["date"], int(z["no"])), reverse=True)
    counts = {g: sum(1 for i in items if i["group"] == g) for g in GROUP_ORDER}
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    return {
        "collected_at": snap["collected_at"], "date": snap["date"],
        "fin_fetched": max(f["fetched"] for f in fin.values()),
        "summary": {
            "n": len(rows), "da_median": round(_median(da), 1),
            "da_hi": {"name": hi["name"], "v": hi["debt_to_assets"]},
            "da_lo": {"name": lo["name"], "v": lo["debt_to_assets"]},
            "de_over2": sum(1 for r in rows if r["debt_to_equity"] and r["debt_to_equity"] > 2),
            "da_under20": sum(1 for r in rows if r["debt_to_assets"] < 20),
            "da_median_ex": round(_median([v for v in da if v >= 20]), 1),
            "assets_eok": sum(r["assets_eok"] for r in rows), "debt_eok": sum(r["debt_eok"] for r in rows),
            "dis_major": len(items), "dis_routine": routine90, "dis_risk": counts["위험 신호"],
            "dis_from": cut90,
        },
        "rows": rows, "groups": [{"name": g, "n": counts[g]} for g in GROUP_ORDER if counts[g]],
        "disclosures": items,
        "author": cfg.get("author", ""), "contact": cfg.get("contact", ""),
    }


PAGES = [("index", "./", "인가와 프로젝트리츠"), ("listed", "listed.html", "상장리츠"),
         ("finance", "finance.html", "재무 구조와 공시")]


def nav_html(current: str) -> str:
    links = "".join(f'\n  <a href="{href}"' + (' aria-current="page"' if key == current else "") + f">{label}</a>"
                    for key, href, label in PAGES)
    return f'<nav class="top" aria-label="화면 이동">{links}\n</nav>'


def render(template: str, out: str, data: dict | None, page: str) -> int:
    tpl = (ROOT / template).read_text(encoding="utf-8")
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
    s = data["summary"]
    print(f"[빌드] {data['collected_at']} | 리츠 {s['n_reits']}개, 자산총계 {s['assets_eok']/10000:.2f}조원, "
          f"자산관리회사 {s['n_amc']}곳 | 최근 12개월 {s['new12']}건(직전 {s['prev12']}건) | "
          f"프로젝트리츠 {s['proj_n']}건 | 접수 {s['filings_n']}건 | index.html {n_index:,}자")
    ls = listed["summary"]
    print(f"[빌드] 상장리츠 {ls['n']}개, 시가총액 {ls['mktcap_eok']/10000:.2f}조원, "
          f"지수 1년 {ls['index_ret']:+.1f}%({ls['index_n']}개), "
          f"P/NAV 중앙값 {ls['pnav_median']}(1배 미만 {ls['pnav_below1']}/{ls['pnav_n']}), "
          f"연환산 배당률 중앙값 {ls['dvd_median']}% | listed.html {n_listed:,}자")
    fs = finance["summary"]
    print(f"[빌드] 재무 구조 {fs['n']}개, 부채/자산 중앙값 {fs['da_median']}%, "
          f"최고 {fs['da_hi']['name']} {fs['da_hi']['v']}%, 최저 {fs['da_lo']['name']} {fs['da_lo']['v']}% | "
          f"최근 90일 주요 공시 {fs['dis_major']}건(위험 신호 {fs['dis_risk']}건), 정기와 일반 {fs['dis_routine']}건 | "
          f"finance.html {n_fin:,}자")


if __name__ == "__main__":
    main()
