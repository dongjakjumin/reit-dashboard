"""배포 전 점검.

1) 저장한 자료 안에서 합계가 서로 맞는지 본다(구분별 합 = 전체 등).
2) 리츠정보시스템에 다시 물어 전체 건수가 저장분과 같은지 본다.
3) 리츠 몇 건을 뽑아 상세 화면(다른 조회 경로)의 값과 목록 값을 대조한다.
4) 상장리츠: 건수, 합계, P/B 재계산, 주가 이력, 첫 화면의 NAV와 FFO 대조.
5) 재무 구조와 수시공시: 자산 = 부채 + 자본, 상세 화면 재조회, 공시 건수.
6) 저장 파일에 전화번호 모양의 문자열이 없는지 본다.

숫자가 하나라도 어긋나면 종료 코드 1. 원문 접속이 안 돼 재조회를 못 한 항목은 건너뛴 것으로 적고 실패로 세지 않는다.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from itertools import groupby
from pathlib import Path
from statistics import median

import build
import ecos
import reits_api
from dart import previous_year
from textutil import CHANGE_REQUEST, PRIVATE_LABEL, mask_officer_names, mask_person, officer_names, phone_files

DATA = Path(__file__).parent / "data"


def fv_stale_of(fvr):
    """감정 시점 신호(build.appraisal_age와 같은 규칙): 주석의 '공정가치 평가를 다시 하지 않음' 문장이 먼저, 없으면 같은 주석 표의
    전기말 공정가치가 당기와 같은지 본다."""
    if fvr.get("not_reappraised"):
        return "skipped"
    if fvr.get("method") == "장부금액과 공정가치 표" and fvr.get("fv_prior") and fvr["fv_prior"] == fvr["fv"]:
        return "same"
    return None


def saved_json(filename):
    path = DATA / filename
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def verify_upcoming(check, notes, confirmed, upcoming, as_of):
    start = date.fromisoformat(as_of)
    first = start.replace(day=1)
    months = {}
    cutoffs = []
    for _ in range(13):
        following = (first + timedelta(days=32)).replace(day=1)
        cutoffs.append(min(first + timedelta(days=start.day-1), following - timedelta(days=1)))
        months[first.strftime("%Y-%m")] = dict.fromkeys(("고정", "변동", "모름"), 0)
        first = following

    expected = {}
    passed_keys = set()
    past_amount = overdue_amount = overdue_count = within6 = 0
    fields = ("code", "maturity", "kind", "rate_text", "rate_type", "fx")
    for code in confirmed:
        issuer = notes[code]
        for loan in issuer["loans"]:
            day = date.fromisoformat(loan["maturity"])
            amount = loan["amount"]
            key = (code, *(loan[k] for k in fields[1:]))
            if loan["maturity"] < issuer["end"]:
                overdue_amount += amount
                overdue_count += 1
            elif day < start:
                passed_keys.add(key)
                past_amount += amount
            elif day <= cutoffs[12]:
                expected[key] = expected.get(key, 0) + amount
                months[loan["maturity"][:7]][loan["rate_type"]] += amount
                if day <= cutoffs[6]:
                    within6 += amount

    rows = upcoming.get("rows", [])
    actual = {tuple(row[k] for k in fields): row["amount"] for row in rows}
    check(actual == expected and len(rows) == len(expected), "차환 일정 묶음별 금액과 건수")
    days = [date.fromisoformat(row["maturity"]) for row in rows]
    check(upcoming.get("as_of") == as_of and days == sorted(days)
          and all(start <= day <= cutoffs[12] and row["days"] == (day-start).days and row["code"] in confirmed
                  for day, row in zip(days, rows)), "차환 일정 수집 기준일, 만기 범위와 순서, 잔여 일수와 종목")
    check(sum(row["amount"] for row in rows) == upcoming.get("total") == sum(expected.values()),
          "차환 일정 전체 금액")
    shown_months = upcoming.get("months", {})
    check(shown_months == months and sum(sum(m.values()) for m in shown_months.values()) == upcoming.get("total"),
          "차환 일정 월별 금리 구분과 합계")
    floating = sum(m["변동"] for m in months.values())
    known = sum(m["고정"] + m["변동"] for m in months.values())
    check(upcoming.get("within6") == within6 and upcoming.get("float_share") == (floating / known if known else None),
          "차환 일정 6개월 내 만기 금액과 변동금리 비중")
    check(upcoming.get("passed") == dict(amount=past_amount, count=len(passed_keys),
                                         issuers=len({key[0] for key in passed_keys})), "보고서 기준일 이후 경과분")
    check(upcoming.get("overdue") == dict(amount=overdue_amount, count=overdue_count), "보고서 기준일 전 경과분")


def verify_loans(check, statements, page, listed, as_of):
    notes = saved_json("loans.json")
    visible = page.get("issuers", {})
    confirmed = set()
    buckets = {}
    due = known = fixed = total = priced = weighted = 0
    if notes:
        check(any(r.get("loans") for r in visible.values()), "저장한 주석의 화면 건별 내역 존재")
    markers = re.compile(r"은행|증권|보험|캐피탈|금고|공사|공단|기금|연금|조합|중앙회|회사|리츠|㈜|\(주\)|\(유\)|유한|펀드|신탁|운용|투자|저축|카드|생명|화재|공제|신협|새마을")
    names = {x["lender"] for r in notes.values() for x in r.get("loans", []) if x.get("lender")}
    count = sum(1 for name in names if re.fullmatch("[가-힣]{2,4}", name) and not markers.search(name))
    if count:
        print(f"  주의  법인 표지가 없는 짧은 차입처 이름 {count}개")
    listed_codes = {s["code"] for s in listed}
    missing = []
    for code,r in notes.items():
        name = r["name"]
        src = statements.get(code, {})
        cur = src.get("current", {})
        current = (src.get("rcept_no"), src.get("basis"), cur.get("end"), cur.get("total"))
        stamp = tuple(r.get(k) for k in ("rcept_no", "basis", "end", "bs_total"))
        if code not in listed_codes or not cur.get("total") or current != stamp or r.get("match") not in ("일치", "근사"):
            continue
        check(stamp == current, f"{name} 주석과 재무상태표 접수번호, 기준일, 기준 및 차입 합계")
        rows = r["loans"]
        nominal = sum(x["amount"] for x in rows)
        bs = r["bs_total"]
        diff = nominal + r["adjustments"] - bs
        if bs == 0:
            match = "차입 없음"
        elif any("XBRL 당기·전기 구분 불명" in s for s in r["skipped"]):
            match = "불일치"
        elif abs(diff)*100 <= bs:
            match = "일치"
        elif abs(nominal-bs)*100 <= 3*bs:
            match = "근사"
        else:
            match = "불일치"
        check(nominal == r["nominal"] and diff == r["diff"] and match == r["match"]
              and (bs != 0 or not rows), f"{name} 주석 명목 합과 조정 후 대조")
        try:
            valid = all(date.fromisoformat(x["maturity"]).isoformat() == x["maturity"]
                        and type(x["amount"]) is int and x["amount"] > 0
                        and isinstance(x.get("fx"), bool)
                        and x["rate_type"] in ("고정","변동","모름")
                        and (x["rate"] is None or x["rate_type"] == "고정") for x in rows)
        except (TypeError, ValueError):
            valid = False
        check(valid, f"{name} 건별 날짜와 금액 및 금리 구분")
        f = sum(x["amount"] for x in rows if x["rate_type"] == "고정")
        k = sum(x["amount"] for x in rows if x["rate_type"] != "모름")
        p = sum(x["amount"] for x in rows if x["rate"] is not None)
        w = sum(x["amount"]*x["rate"] for x in rows if x["rate"] is not None)
        check(r["fixed_share"] == (f/k if k else None) and r["avg_rate"] == (w/p if p else None),
              f"{name} 금리 구분을 확인한 금액과 고정금리 가중 평균")
        confirmed.add(code)
        shown = visible.get(code,{})
        detail = [{k:v for k,v in row.items() if k != "months"} for row in shown.get("loans",[])]
        check(detail == sorted(rows,key=lambda x:x["maturity"]) and all(shown.get(k) == r[k] for k in
              ("end","nominal","adjustments","bs_total","diff","match","rcept_no","name")), f"{name} 화면 건별 원문과 합계")
        start = date.fromisoformat(cur["end"])
        months = []
        for row in sorted(rows, key=lambda x: x["maturity"]):
            day = date.fromisoformat(row["maturity"])
            elapsed = 12 * (day.year - start.year) + day.month - start.month
            if day.day < start.day and day.day != monthrange(day.year, day.month)[1]:
                elapsed -= 1
            months.append(elapsed)
        check(months == [x.get("months") for x in shown.get("loans", [])], f"{name} 기준일과 만기일로 계산한 잔여 개월 수")
        total += nominal
        fixed += f
        known += k
        priced += p
        weighted += w
        for x in rows:
            if not valid:
                continue
            day = date.fromisoformat(x["maturity"])
            start = date.fromisoformat(r["end"])
            try:
                anniversary = start.replace(year=start.year+1)
            except ValueError:
                anniversary = start.replace(year=start.year+1,day=28)
            if start <= day <= anniversary:
                due += x["amount"]
            label = f"{day.year}년 {'하' if day.month > 6 else '상'}반기"
            buckets.setdefault(label,dict.fromkeys(("고정","변동","모름"),0))[x["rate_type"]] += x["amount"]
    borrowers = 0
    for s in listed:
        bs = statements.get(s["code"],{}).get("current",{}).get("total")
        borrowers += int(bool(bs))
        if bs != 0 and s["code"] not in confirmed:
            missing.append(s["name"])
            print(f"  주의  {s['name']} 차입 주석 미확인")
    check(set(visible) == confirmed and missing == page.get("missing") and page.get("borrowers") == borrowers,
          "화면 확인 종목과 미확인 종목 분리")
    # 미확인 이유는 같은 보고서의 주석을 읽었을 때만 적는다(만기일 표 없음 / 합계 불일치)
    reasons = {}
    for s in listed:
        n, src = notes.get(s["code"], {}), statements.get(s["code"], {})
        if s["name"] not in missing or n.get("rcept_no") != src.get("rcept_no"):
            continue
        if not n.get("loans") and "만기일과 이자율이 함께 있는 차입 잔액 표 없음" in (n.get("skipped") or []):
            reasons[s["name"]] = "만기일이 적힌 주석 차입 표를 찾지 못함"
        elif n.get("match") == "불일치":
            reasons[s["name"]] = "주석 합계가 재무상태표 차입과 다름"
    check(page.get("missing_reason") == reasons, f"건별 내역 미확인 이유 {len(reasons)}건")
    check(page.get("buckets") == buckets and page.get("nominal") == total == sum(sum(v.values()) for v in buckets.values()),
          "화면 반기별 금리 유형별 만기 합과 명목 합")
    expected = weighted/priced if priced else None
    verify_upcoming(check, notes, confirmed, page.get("upcoming", {}), as_of)
    avg = page.get("avg_rate")
    check(page.get("fixed_share") == (fixed/known if known else None)
          and (avg == expected or avg is not None and expected is not None and abs(avg-expected)<1e-10)
          and page.get("due") == due, "화면 고정 비중과 가중 평균 금리 및 12개월 내 만기 금액")


def secrets_only():
    keys = [os.environ.get(k, "") for k in ("DART_API_KEY", "ECOS_API_KEY")]
    needles = [key.encode() for key in keys if key and key != "sample"]
    if needles:
        for path in DATA.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                if any(key in content for key in needles):
                    print("실패  저장 자료에서 인증키를 발견했습니다.")
                    return 1
    print("통과  저장 자료 인증키 검사")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["--secrets-only"]:
        return secrets_only()
    bad = passed = 0

    def check(ok: bool, msg: str):
        nonlocal bad, passed
        print(("  통과  " if ok else "  실패  ") + msg)
        if ok:
            passed += 1
        else:
            bad += 1

    skipped = 0
    blocked = False

    def ask(path: str, data: dict):
        """원문 재조회. 접속이 안 되면 실패로 세지 않고 건너뛴 것으로 적는다(숫자가 어긋난 것과 구분).
        한 번 접속에 실패하면 남은 재조회도 건너뛴다. 원문이 연결을 끊은 뒤에 요청을 더 보내지 않기 위해서다."""
        nonlocal skipped, blocked
        if os.environ.get("VERIFY_OFFLINE") or blocked:  # 화면만 다시 올릴 때는 원문에 접속하지 않는다
            skipped += 1
            return None
        try:
            return reits_api.post(path, data, retries=1)
        except Exception as e:
            skipped += 1
            blocked = True
            print(f"  건너뜀  {path} 접속 실패, 남은 원문 재조회도 건너뜀: {str(e)[:90]}")
            return None

    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    listed_page = ""
    for name in ("index", "listed", "finance", "calc"):
        path = build.SITE / f"{name}.html"
        html = path.read_text(encoding="utf-8") if path.is_file() else ""
        check(bool(html.strip()), f"{path.name} 생성 및 내용 확인")
        if name == "listed":
            listed_page = html
    finance_payload = {}
    try:
        page_data, _ = json.JSONDecoder().raw_decode(listed_page.split("const D = ", 1)[1])
        codes = {row["code"] for row in page_data["rows"]}
    except (IndexError, ValueError, TypeError, KeyError):
        codes = None
    check(codes == {row["code"] for row in snap["listed"]}, "상장 화면과 수집 목록의 종목코드 집합 일치")
    d = build.build_data()
    s = d["summary"]
    reits = snap["reits"]

    print("[1] 내부 합계")
    check(sum(sum(v) for v in d["yearly"]["kind"].values()) + d["yearly"]["before"] == s["n_reits"],
          f"연도별 구분 합 + 이전 연도 = 전체 {s['n_reits']}건")
    check(sum(sum(v) for v in d["yearly"]["sector"].values()) + d["yearly"]["before"] == s["n_reits"],
          "연도별 투자대상 합 + 이전 연도 = 전체")
    check(sum(a["n"] for a in d["amc"]) == s["n_reits"], "자산관리회사별 리츠 수 합 = 전체")
    check(sum(a["assets_eok"] for a in d["amc"]) == s["assets_eok"], "자산관리회사별 자산총계 합 = 전체")
    by_amc_v = {}
    for r in d["reits"]:
        by_amc_v.setdefault(r["amc_s"], []).append(r)
    def first_stats(rows):
        first = min(r["date"] for r in rows)
        f = date.fromisoformat(first)
        return (first, sum(date.fromisoformat(r["date"]) < f + timedelta(days=365) for r in rows),
                sum(date.fromisoformat(r["date"]) < f + timedelta(days=730) for r in rows))
    check(all((a["first"], a["n_y1"], a["n_y2"]) == first_stats(by_amc_v[a["name"]]) and a["n_y1"] <= a["n_y2"] <= a["n"]
              and a["self"] == all("자기관리" in (r["type"] or "") for r in by_amc_v[a["name"]]) for a in d["amc"]),
          "관리주체별 첫 리츠 날짜와 첫 1년, 2년 리츠 수 재계산")
    check(sum(s["po_12"].values()) == s["new12"]
          and len(d["po_planned"]) == sum(r["public_offering"] == "공모예정" for r in d["reits"]),
          f"최근 12개월 공모 여부 합 = 신규 {s['new12']}건, 공모예정 리츠 {len(d['po_planned'])}개")
    check(sum(x["n"] for x in d["sector"]) == s["n_reits"], "투자대상별 리츠 수 합 = 전체")
    check(sum(s["new12_by_kind"].values()) == s["new12"], "최근 12개월 구분별 합 = 최근 12개월 전체")
    check(sum(s["filings_by_type"].values()) == s["filings_n"], "접수 유형별 합 = 접수 전체")
    check(sum(1 for r in reits if r["kind"] == "신고" or r.get("project") == "Y") == s["proj_n"],
          "신고 구분이거나 프로젝트리츠 표시가 있는 건수 = 프로젝트리츠 건수")
    check(sum(a["new"] + a["change"] + a["long"] for a in d["applicants"]) == len(snap["filings"])
          and all(a["recent"] == a["new"] + a["change"] for a in d["applicants"]),
          "운용사별 1년 안 신규, 전환, 변경과 1년 넘은 신청 합 = 처리 중인 신청 전체")
    check(sum(a["long"] for a in d["applicants"]) == sum(f["days"] > 365 for f in d["filings"]) == s["age_buckets"][3]
          and sum(s["age_buckets"]) == s["filings_n"] and sum(s["filings_by_group"].values()) == s["filings_n"],
          f"경과일 구간 {s['age_buckets']} 합과 신규, 전환, 변경 {s['filings_by_group']} 합 = 접수 전체")
    reit_ids_now = {r["id"] for r in reits}
    check(s["filings_by_group"]["전환"] == sum(f["req_type"] != CHANGE_REQUEST and f["reit_id"] in reit_ids_now for f in snap["filings"]),
          "운영 목록에 이미 있는 리츠의 인가, 등록 신청 수 = 전환 건수")
    project = d["project"]
    check(sum(a["n"] for a in project["companies"]) == s["proj_n"], "관리주체별 합 = 프로젝트리츠 전체")
    check(sum(project["sectors"].values()) == s["proj_n"], "투자대상별 합 = 프로젝트리츠 전체")
    check(sum(project["months"].values()) == s["proj_n"], "첫 신고 달부터 월별 합 = 프로젝트리츠 전체")
    check(sum(1 for e in d["events"] if e["what"] == "접수") == len(snap["filings"]),
          "최근 변동에 접수 중인 신청이 빠짐없이 들어 있음")
    check(all(x["result_date"] is None for x in d["done"] if x["is_change"]),
          "변경 신청에는 인가일을 결과로 붙이지 않음")
    check(len({r["id"] for r in reits}) == len(reits), "리츠 번호 중복 없음")
    # 프로젝트리츠 사업 수(모리츠와 자리츠를 한 사업으로), 초기와 자산 0원 건수, 모리츠 자산
    proj_rows = [r for r in d["reits"] if r["project"] == "Y"]
    bases = {re.sub(r"\s(?:모|자)리츠$", "", r["name_s"]) for r in proj_rows}
    mo_rows = [r for r in d["reits"] if r["name_s"].endswith(" 모리츠")]
    check(d["summary"]["proj_biz_n"] == len(bases) <= len(proj_rows)
          and d["summary"]["proj_early"] == sum(1 for r in proj_rows if 0 < r["assets_eok"] <= 60)
          and d["summary"]["proj_zero"] == sum(1 for r in proj_rows if not r["assets_eok"])
          and d["summary"]["mo_n"] == len(mo_rows) and d["summary"]["mo_assets_eok"] == sum(r["assets_eok"] for r in mo_rows),
          f"프로젝트리츠 {len(proj_rows)}건 = {len(bases)}개 사업, 초기 {d['summary']['proj_early']}건, 모리츠 {len(mo_rows)}개")
    by_id = {str(r["id"]): r for r in d["reits"]}
    pending_names = {f["name_s"]: f for f in d["filings"]}
    check(all(c["name_s"] in pending_names and c["name_s"].endswith("자리츠")
              and pending_names[c["name_s"]]["applicant"] == by_id[rid]["amc_s"]
              and c["name_s"].startswith(re.sub(r"(?:\s모리츠|프로젝트리츠|리츠)$", "", by_id[rid]["name_s"]))
              for rid, kids in d["children"].items() for c in kids),
          f"이름이 이어지는 자리츠 신청 {sum(len(k) for k in d['children'].values())}건: 처리 중 목록, 신청인, 이름 앞부분 일치")

    # 관리주체 상세의 최근 90일 주요 공시: 재무 화면의 주요 공시와 같은 묶음을 관리주체별로 나눈 것
    amc_names = {a["name"] for a in d["amc"]}
    amc_dis = d["amc_dis"]
    dis_saved = json.loads((DATA / "disclosures.json").read_text(encoding="utf-8")) if (DATA / "disclosures.json").is_file() else {}
    major90 = {no for no, x in dis_saved.items()
               if build.dis_group(x) and build.safe_date(x.get("date"))
               and x["date"] >= d["amc_dis_from"]}
    linked = [x["no"] for rows in amc_dis.values() for x in rows]
    check(set(amc_dis) <= amc_names, "관리주체 상세 공시의 회사가 모두 관리주체 목록에 있음")
    check(len(linked) == len(set(linked)) and set(linked) <= major90
          and len(linked) + d["amc_dis_unmatched"] == len(major90),
          f"관리주체 상세 공시 {len(linked)}건 + 연결 못 한 {d['amc_dis_unmatched']}건 = 최근 90일 주요 공시 {len(major90)}건")
    reit_names = {build._norm_company(r["name"]): build.clean_company(mask_person(r["amc"])) for r in reits}
    amc_norm = {build._norm_company(r["amc"]): build.clean_company(mask_person(r["amc"])) for r in reits}
    check(all(reit_names.get(build._norm_company(dis_saved[x["no"]]["company"]),
                             amc_norm.get(build._norm_company(dis_saved[x["no"]]["company"]))) == owner
              for owner, rows in amc_dis.items() for x in rows),
          "관리주체 상세 공시마다 공시 회사의 관리주체가 맞음")
    check(all(set(x) == {"no", "date", "group", "matter", "company_s", "self"} for rows in amc_dis.values() for x in rows),
          "관리주체 상세 공시에 본문을 싣지 않음")

    print("[2] 원문 건수 재조회")
    live = ask("/reits/prmpc/searchPrmpcOper", dict(reits_api.OPER_EMPTY, pageSize=10, pageNum=1))
    if live:  # 수집과 점검 사이에 원문이 바뀔 수 있어 3건까지는 차이를 허용한다
        check(abs(int(live["total"]) - len(reits)) <= 3, f"리츠조회 전체 {int(live['total'])}건, 저장 {len(reits)}건(차이 3건 이내)")
    live_f = ask("/reits/prmpc/searchPrmpcInvtCnfm", {"reitsNm": "", "cmpnyMberNm": "", "pageSize": 10, "pageNum": 1})
    if live_f:
        check(abs(int(live_f["total"]) - len(snap["filings"])) <= 3,
              f"접수현황 전체 {int(live_f['total'])}건, 저장 {len(snap['filings'])}건(차이 3건 이내)")

    print("[3] 상세 화면 대조(자산총계가 있는 리츠 5건 + 강조 회사)")
    pool = [r for r in reits if r["assets_eok"]]
    random.seed(snap["date"])
    sample = random.sample(pool, min(5, len(pool)))
    hl = d.get("highlight_amc")
    sample += [r for r in reits if hl and hl in r["amc"]][:1]
    for r in sample:
        det = ask("/reits/prmpc/searchPrmpcOperTab01_01", {"reitsNo": r["id"], "no": r["id"]})
        if det is None:
            continue
        text = json.dumps(det, ensure_ascii=False)
        name_ok = r["name"] in text
        m = re.search(r'"totAssets"\s*:\s*"?([\d,\.]+)', text)
        got = None
        if m:
            raw = float(m.group(1).replace(",", ""))
            got = round(raw / 1e8) if raw > 1e7 else round(raw)
        if got is None:  # 상세 화면에서 값을 찾지 못하면 통과로 치지 않고 건너뛴 것으로 적는다
            skipped += 1
            print(f"  건너뜀  {r['name']}: 상세 화면에서 자산총계를 찾지 못함")
            continue
        check(name_ok and abs(got - r["assets_eok"]) <= 1, f"{r['name']} | 목록 {r['assets_eok']:,}억 | 상세 {got}")

    print("[4] 상장리츠")
    ld = build.build_listed_data()
    ls = ld["summary"]
    listed = snap["listed"]
    prices = build.optional_data("prices.json")
    live_l = ask("/invt/lsted/searchLstedReits", {"pageSize": 10, "pageNum": 1})
    if live_l:
        check(int(live_l["total"]) == len(listed), f"상장리츠 현황 전체 {int(live_l['total'])}건 = 저장 {len(listed)}건")
    check(sum(x["mktcap_eok"] for x in ld["sectors"]) == ls["mktcap_eok"], "투자대상별 시가총액 합 = 전체 시가총액")
    check(sum(x["n"] for x in ld["sectors"]) == ls["n"], "투자대상별 종목 수 합 = 전체 종목 수")
    check(all(r["pnav"] is None or abs(r["mktcap_eok"] / r["nav_eok"] - r["pnav"]) < 0.006 for r in ld["rows"]),
          "P/B = 시가총액 / NAV 재계산 일치")
    # NAV가 결산기 자본총계와 같은 종목(장부가 기준): 재무 추이에서 결산일이 같은 줄의 자본총계와 견준다
    fin_saved = build.optional_data("financials.json")
    pairs = []
    for r in listed:
        rows_f = [x for x in fin_saved.get(str(r.get("reit_id")), {}).get("series", []) if x.get("end") == r.get("fiscal_end")]
        if r.get("nav_mil") and rows_f and rows_f[0].get("equity") is not None:
            pairs.append((r["name"], round(r["nav_mil"]) == round(rows_f[0]["equity"] / 1e6)))
    check(ls.get("nav_book_n") == len(pairs) and ls.get("nav_book_same") == sum(ok for _, ok in pairs)
          and ls.get("nav_book_diff") == [n for n, ok in pairs if not ok],
          f"NAV와 결산기 자본총계가 같은 종목 {sum(ok for _, ok in pairs)}개 / {len(pairs)}개")
    # NAV가 장부 자본과 다른 종목은 감정가 NAV 계산값과 나란히 보여 준다(값이 다르면 화면에 차이를 적으므로 일치는 요구하지 않음)
    by_name = {r["name"]: r for r in ld["rows"]}
    cross = sorted(({"name": n, "nav_eok": by_name[n]["nav_eok"], "nav_end": by_name[n]["fiscal_end"],
                     "nav_fv_eok": by_name[n]["nav_fv_eok"], "fv_end": by_name[n]["fv_end"]}
                    for n, ok in pairs if not ok and n in by_name and by_name[n].get("nav_fv_eok") is not None),
                   key=lambda x: x["name"])
    check(ls.get("fv_cross") == cross, f"장부 자본과 다른 NAV와 감정가 NAV 계산값 대조 {len(cross)}건"
          + "".join(f" · {x['name']} 리츠정보시스템 {x['nav_eok']:,}억원, 계산 {x['nav_fv_eok']:,}억원" for x in cross))
    # 상장 목록 변동: 빠진 종목은 지금 목록에 없고 그 전 원자료에는 있으며, 들어온 종목은 지금 목록에 있음
    lc = ld["list_changes"]
    now_codes = {str(r["code"]) for r in listed}
    seen_before = set()
    for p in sorted((DATA / "snapshots").glob("*.json")):
        if lc["from"] and lc["from"] <= p.stem < snap["date"]:
            seen_before |= {str(x.get("code")) for x in json.loads(p.read_text(encoding="utf-8")).get("listed") or []}
    check(all(x["code"] not in now_codes and x["code"] in seen_before for x in lc["removed"])
          and {x["code"] for x in lc["removed"]} == (seen_before - now_codes if now_codes else set())
          and all(x["code"] in now_codes for x in lc["added"]),
          f"상장 목록 변동 재계산(빠짐 {len(lc['removed'])}개, 들어옴 {len(lc['added'])}개)")
    # 빠진 종목의 DART 공시: 보고서명 규칙, 접수번호 14자리, 접수일 = 접수번호 앞 8자리, 화면에는 가장 최근 건
    exits = saved_json("exits.json")
    exit_rule = re.compile(r"해산사유|상장폐지|합병결정|분할결정|회생절차|파산")
    exit_files = [f for v in exits.values() for f in v.get("filings", [])]
    check(all(exit_rule.search(f["title"]) and re.fullmatch(r"\d{14}", f["rcept_no"])
              and f["rcept_no"][:8] == f["date"].replace("-", "") for f in exit_files)
          and all(x.get("exit") == next(iter((exits.get(x["code"]) or {}).get("filings") or []), None) for x in lc["removed"])
          and all(v.get("filings") == sorted(v.get("filings", []), key=lambda f: f["rcept_no"], reverse=True) for v in exits.values()),
          f"목록에서 빠진 종목의 DART 공시 {len(exit_files)}건 형식과 화면 표시"
          + "".join(f" · {x['name']} {x['exit']['date']} {x['exit']['title']}" for x in lc["removed"] if x.get("exit")))
    # 수익률을 가격 이력에서 따로 계산해 화면 값과 견준다(기준가가 바뀐 종목은 제외)
    from datetime import date as _date, timedelta as _td
    for row in [x for x in ld["rows"] if x["ret_1y"] is not None][:3]:
        hist = prices[str(row["code"])]
        days = sorted(hist)
        base_day = [x for x in days if x <= (_date.fromisoformat(days[-1]) - _td(days=365)).isoformat()][-1]
        mine = round((hist[days[-1]] / hist[base_day] - 1) * 100, 1)
        check(abs(mine - row["ret_1y"]) < 0.11, f"{row['name']} 1년 수익률 재계산 {mine}% = 화면 {row['ret_1y']}%")
    check(all(r["price"] == prices[str(r["code"])][r["price_date"]] for r in ld["rows"] if r["price_date"]),
          "종가 이력이 있는 종목의 현재가 = 가격 이력의 마지막 종가")
    if ld["index"]["values"]:
        check(abs(ld["index"]["values"][0] - 100) < 0.001, "주가 지수 첫날 = 100")
    else:
        check(ls["index_ret"] is None, "지수 자료가 없으면 수익률을 비움")
    start = date.fromisoformat(snap["date"])
    window_start = str(start - timedelta(days=372))
    index_start = str(start - timedelta(days=366))
    histories = {r["code"]: {day: price for day, price in prices.get(str(r["code"]), {}).items()
                             if day >= window_start and price} for r in listed}
    trading_days = sorted({day for h in histories.values() if len(h) >= 2 for day in h if day >= index_start})
    members, member_codes = [], []
    for stock in listed:
        h = histories[stock["code"]]
        series = [h[day] for day in sorted(h)]
        changed = any(abs(b / a - 1) > .31 for a, b in zip(series, series[1:]))
        flat = any(sum(1 for _ in run) >= 61 for _, run in groupby(series))
        if (len(h) >= 2 and any(day in h for day in trading_days[:6])
                and not changed and not (flat and not stock.get("volume"))):
            members.append(h)
            member_codes.append(stock["code"])
    dates = [day for day in trading_days if members and all(day in h for h in members)]
    moves = []
    for history in members if dates else []:
        last = max(history)
        cutoff = str(date.fromisoformat(last) - timedelta(days=365))
        base = max((day for day in history if day <= cutoff), default=None)
        if base:
            moves.append(round((history[last] / history[base] - 1) * 100, 1))
    summary = page_data.get("summary", {}) if codes is not None else {}
    check((len(members) if dates else 0, sum(v > 0 for v in moves), sum(v < 0 for v in moves)) ==
          tuple(summary.get(k) for k in ("index_n", "up_1y", "down_1y")), "지수 표본과 상승·하락 종목 수 재계산")
    # 시가총액 가중 지수(주식 수 = 시가총액 ÷ 현재가)와 배당 1년치 포함 근사(최근 12개월 주당배당금 ÷ 첫날 종가)
    if dates and codes is not None:
        page_rows = {r["code"]: r for r in page_data["rows"]}
        shares = {c: page_rows[c]["mktcap_eok"] * 1e8 / page_rows[c]["price"] for c in member_codes
                  if page_rows.get(c, {}).get("mktcap_eok") and page_rows[c].get("price")}
        cap = []
        if len(shares) == len(member_codes):
            base = sum(shares[c] * histories[c][dates[0]] for c in member_codes)
            cap = [sum(shares[c] * histories[c][x] for c in member_codes) / base * 100 for x in dates]
        shown = page_data["index"].get("cap") or []
        check(len(shown) == len(cap) and all(abs(a - b) < 0.006 for a, b in zip(shown, cap))
              and summary.get("index_cap_ret") == (round(shown[-1] - 100, 1) if shown else None),
              f"시가총액 가중 지수 {summary.get('index_cap_ret')}% 재계산")
        dart_div = build.optional_data("dart.json")
        tr = [(histories[c][dates[-1]] + dart_div[c]["trailing_dps"]) / histories[c][dates[0]] - 1
              for c in member_codes if (dart_div.get(c) or {}).get("trailing_dps") is not None]
        check(summary.get("index_tr_n") == len(tr) and tr and abs(summary.get("index_tr") - sum(tr) / len(tr) * 100) < 0.051,
              f"배당 1년치 포함 근사 {summary.get('index_tr')}%({len(tr)}개) 재계산")
    expected_dvd = {r["code"]: r.get("annual_dvd_pct") or None for r in listed}
    dividends = [v for v in expected_dvd.values() if v is not None]
    check(codes is not None and {r["code"]: r["annual_dvd_pct"] for r in page_data["rows"]} == expected_dvd
          and {r["id"]: r["dividend_pct"] for r in d["reits"]} == {r["id"]: r.get("dividend_pct") or None for r in reits}
          and summary.get("dvd_n") == len(dividends)
          and summary.get("dvd_median") == (round(median(dividends), 2) if dividends else None),
          "두 화면 연환산 배당률의 0% 빈값 처리와 중앙값 재계산")
    nav_rows = ask("/main/searchMainStock", {"stockTab": "3"})
    ffo_rows = ask("/main/searchMainStock", {"stockTab": "4"})
    nav_main = {str(x.get("stockNm")).strip(): x.get("navThstrm") for x in (nav_rows or [])}
    ffo_main = {str(x.get("stockNm")).strip(): x.get("ffoThstrm") for x in (ffo_rows or [])}
    with_metrics = [r for r in listed if r.get("nav_mil") is not None and r.get("ffo_mil") is not None]
    for r in (random.sample(with_metrics, min(5, len(with_metrics))) if nav_rows and ffo_rows else []):
        ok = nav_main.get(r["name"]) == r["nav_mil"] and ffo_main.get(r["name"]) == r["ffo_mil"]
        check(ok, f"{r['name']} | NAV {r['nav_mil']:,}백만원, FFO {r['ffo_mil']:,}백만원 | "
                  f"첫 화면 NAV {nav_main.get(r['name'])}, FFO {ffo_main.get(r['name'])}")

    print("[5] 재무 구조와 수시공시")
    fin = build.optional_data("financials.json")
    dis = build.optional_data("disclosures.json")
    fd = build.build_finance_data()
    missing = len(listed) - len(fd["rows"])  # 새로 상장해 재무 자료가 아직 없는 종목은 2개까지 허용
    check(0 <= missing <= 2, f"재무 자료 {len(fd['rows'])}개, 상장리츠 {len(listed)}개(없는 종목 2개까지 허용)")
    usable = [v for v in fin.values() if None not in (v.get("assets"), v.get("debt"), v.get("equity"))]
    check(all(abs(v["assets"] - v["debt"] - v["equity"]) <= 1e8 for v in usable),
          "자산총계 = 부채총계 + 자본총계(1억원 이내)")
    check(all(abs(r["debt_eok"] / r["assets_eok"] * 100 - r["debt_to_assets"]) < 0.2 for r in fd["rows"]),
          "부채 ÷ 자산 재계산 일치")
    # LTV = DART 사업보고서 차입금과 사채 합계 ÷ 자산총계(연결 우선). 표의 값, 기준, 기준일, 차입 금액과 중앙값을 다시 계산한다.
    dart_fs = build.optional_data("dart.json")
    ltv_ok, ltv_raw = [], []
    for r in fd["rows"]:
        rec = dart_fs.get(r["code"]) or {}
        cur = rec.get("current") or {}
        want = cur["total"] / cur["assets"] * 100 if cur.get("assets") and cur.get("total") is not None else None
        if want is not None:
            ltv_raw.append(want)
        ltv_ok.append(r.get("ltv") == (round(want, 1) if want is not None else None) and (want is None or (
            0 <= want <= 100 and r.get("ltv_basis") == ("연결" if rec["basis"] == "CFS" else "별도")
            and r.get("ltv_end") == cur["end"] and r.get("borrow_eok") == round(cur["total"] / 1e8)))
            and r.get("ltv_rcept") == (rec.get("rcept_no") if want is not None else None))
    check(all(ltv_ok) and len(ltv_raw) >= len(fd["rows"]) - 2,
          f"LTV(차입금과 사채 ÷ 자산총계) {sum(ltv_ok)}/{len(ltv_ok)}종목 재계산 일치")
    # 감정가 LTV = 차입 ÷ (자산총계 + 공정가치 - 장부금액). 같은 보고서의 주석일 때만 계산하고, 공정가치가 장부금액보다 크면 장부 LTV보다 낮아야 한다
    fv_saved = build.optional_data("fv.json")
    lf_ok, lf_raw, lf_book = [], [], []
    for r in fd["rows"]:
        rec = dart_fs.get(r["code"]) or {}
        cur = rec.get("current") or {}
        fvr = fv_saved.get(r["code"]) or {}
        want = None
        # 공정가치는 같은 보고서, 같은 기준, 같은 결산일, 같은 투자부동산 금액에서 읽었고 장부금액의 0.5~2배일 때만 쓴다
        same = (fvr.get("fv") is not None and bool(fvr.get("book"))
                and tuple(fvr.get(k) for k in ("rcept_no", "basis", "end", "book"))
                == (rec.get("rcept_no"), rec.get("basis"), cur.get("end"), cur.get("investment_property"))
                and 0.5 <= fvr["fv"] / fvr["book"] <= 2)
        if cur.get("assets") and cur.get("total") is not None and same and cur["assets"] + fvr["fv"] - fvr["book"] > 0:
            want = cur["total"] / (cur["assets"] + fvr["fv"] - fvr["book"]) * 100
            lf_raw.append(want)
            lf_book.append(cur["total"] / cur["assets"] * 100)
        lf_ok.append(r.get("fv_stale") == (fv_stale_of(fvr) if want is not None else None)
                     and r.get("ltv_fv") == (round(want, 1) if want is not None else None) and (want is None or (
            want >= 0 and ((r["ltv_fv"] <= r["ltv"]) if fvr["fv"] >= fvr["book"] else (r["ltv_fv"] >= r["ltv"])))))
    check(all(lf_ok) and fd["summary"].get("ltv_fv_n") == len(lf_raw)
          and fd["summary"].get("ltv_fv_median") == (round(median(lf_raw), 1) if lf_raw else None)
          and fd["summary"].get("ltv_fv_book_median") == (round(median(lf_book), 1) if lf_book else None),
          f"LTV(감정가) {sum(lf_ok)}/{len(lf_ok)}종목 재계산, 중앙값 {fd['summary'].get('ltv_fv_median')}%({len(lf_raw)}개)")
    listed_ltv = {r["code"]: r.get("ltv") for r in build.build_listed_data()["rows"]}
    check(all(listed_ltv.get(r["code"]) == r.get("ltv") for r in fd["rows"]), "상장리츠 표와 재무 표의 LTV 일치")
    via_sub = sum(1 for r in fd["rows"] if r["debt_to_assets"] < 20 and r.get("ltv_basis") == "연결"
                  and r.get("borrow_eok") and r["ltv"] > r["debt_to_assets"])
    check(fd["summary"].get("ltv_n") == len(ltv_raw) and fd["summary"].get("via_sub") == via_sub
          and fd["summary"].get("ltv_median") == (round(median(ltv_raw), 1) if ltv_raw else None),
          f"LTV 중앙값 {fd['summary'].get('ltv_median')}%({len(ltv_raw)}개), 자회사를 거쳐 차입한 종목 {via_sub}개 재계산")
    check(sum(g["n"] for g in fd["groups"]) == fd["summary"]["dis_major"], "공시 유형별 건수 합 = 주요 공시 건수")
    # 재무 구조 표의 FFO(연환산) = 리츠정보시스템 FFO(백만원) ÷ 100 × 연간 결산 횟수(상장리츠 화면과 같은 기준)
    listed_src = {r["code"]: r for r in snap["listed"]}
    ffo_ok = []
    for r in fd["rows"]:
        src = listed_src[r["code"]]
        ppy = build.periods_per_year(src, fin.get(str(src.get("reit_id")), {}).get("series", []))
        want = round(src["ffo_mil"] / 100 * ppy) if src.get("ffo_mil") is not None and ppy else None
        ffo_ok.append(r.get("ffo_y_eok") == want and "op_margin" not in r)
    check(all(ffo_ok), f"재무 구조 표 FFO(연환산) {sum(ffo_ok)}/{len(ffo_ok)}종목 재계산 일치, 영업이익률 열 없음")
    # 유형 코드가 '기타'여도 회생절차, 사채권자집회 공시는 위험 신호로, 해산, 청산 공시는 따로 묶는다
    group_cases = [({"matter_code": "RT02100020", "matter": "기타", "text": "서울회생법원 회생절차 개시"}, "위험 신호"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "무보증사채 사채권자집회 소집"}, "위험 신호"),
                   ({"matter_code": "RT02100004", "matter": "재무제표 및 감사보고서", "text": "청산일 기준 재무제표"}, "해산, 청산"),
                   ({"matter_code": "RT02100029", "matter": "대출 실행", "text": "기한의 이익 상실 사유 약정"}, "차입, 사채"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "자산관리회사 변경"}, "기타 공시"),
                   ({"matter_code": "RT02100004", "matter": "재무제표 및 감사보고서", "text": ""}, None),
                   # 유형이 '기타'나 투자설명서인 공시의 본문 규칙
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "2026년 제5차 이사회 의사록"}, None),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "(사임) 대표이사 (이름 생략) (취임) 대표이사 (이름 생략)"}, None),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "제2기 재무제표 및 이익잉여금처분계산서(안) 승인의 건"}, None),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "임시주주총회는 출석주식수 미달로 무산되었음"}, "기타 공시"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "이사회 의사록을 기한 내 공시하지 않았음"}, "위반, 제재"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "광고자료 배포로 내부통제기준을 위반하였음"}, "위반, 제재"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "영국 상사법원 소송 판결 결과(캐시트랩 지속)"}, "위험 신호"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "대주단 자율협약서 체결의 건"}, "위험 신호"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "손해배상 청구 소송 1심 판결"}, "소송, 분쟁"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "캐시트랩 해소"}, "기타 공시"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "주요주주인 A사와 PM계약 변경계약 체결"}, "이해관계자 거래"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "자산의 처분 결정(공공사업에 따른 기부채납)"}, "자산 취득, 처분"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "제3호 의안 : 부동산투자회사 영업인가 신청의 건"}, "인가 신청, 기금 출자"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "주택도시기금 출자 신청의 건"}, "인가 신청, 기금 출자"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "제 1호 의안 : 대출약정서 체결의 건"}, "차입, 사채"),
                   ({"matter_code": "RT02100020", "matter": "기타", "text": "현물출자(대토보상권)에 의한 신주 발행의 건"}, "자본 변동"),
                   ({"matter_code": "RT02100005", "matter": "투자설명서", "text": "담보부사채 투자설명서(예비)"}, "차입, 사채"),
                   ({"matter_code": "RT02100005", "matter": "투자설명서", "text": "영업인가에 따른 투자설명서 공시"}, "자본 변동"),
                   ({"matter_code": "RT02100005", "matter": "투자설명서", "text": "자회사의 환헷지 조기종료"}, "기타 공시"),
                   ({"matter_code": "RT02100006", "matter": "실사보고서", "text": ""}, "자산 취득, 처분")]
    for case, expected in group_cases:
        check(build.dis_group(case) == expected, f"공시 묶음 가상 입력 {case['text'] or case['matter']} → {expected or '정기, 일반'}")
    since = build.since(build.d(snap["date"]))
    risky = [no for no, x in dis.items() if (x.get("date") or "") >= since
             and re.search(r"회생절차|사채권자\s?집회", (x.get("matter") or "") + (x.get("text") or ""))]
    shown = {x["no"]: x["group"] for x in fd["disclosures"]}
    check(all(shown.get(no) == "위험 신호" for no in risky), f"최근 90일 회생절차, 사채권자집회 공시 {len(risky)}건이 모두 위험 신호")
    check(fd["summary"]["dis_from"] == (build.d(snap["date"]) - timedelta(days=89)).isoformat()
          and all(x["date"] >= fd["summary"]["dis_from"] for x in fd["disclosures"]),
          f"주요 공시 구간은 오늘을 넣어 90일({fd['summary']['dis_from']}부터)")
    timelines = fd["timelines"]
    finance_codes = [r["code"] for r in fd["rows"]]
    check(set(timelines) == set(finance_codes) and len(timelines) == len(finance_codes),
          "타임라인과 재무 표의 종목 수 및 코드 일치")
    reit_names = {r["id"]: r["name"] for r in reits}
    for stock in listed:
        code = stock["code"]
        if code not in timelines:
            continue
        events = timelines[code]
        changes = [e for e in reversed(events) if e["kind"] == "finance"]
        source = sorted((r for r in fin[str(stock["reit_id"])]["series"] if build.safe_date(r.get("end"))),
                        key=lambda r: r["end"])
        valid = [e["date"] for e in changes] == [r["end"] for r in source]
        for i, (e, row) in enumerate(zip(changes, source)):
            for key in ("debt", "equity"):
                prior = source[i - 1].get(key) if i else None
                delta = row[key] - prior if prior is not None and row.get(key) is not None else None
                flagged = bool(row.get("assets") and row["assets"] > 0 and delta is not None and delta * 20 > row["assets"])
                valid &= e[key] == row.get(key) and e[key + "_change"] == delta and e[key + "_up"] == flagged
        check(valid, f"{stock['name']} 기준일별 부채·자본 증감과 5% 표시 재계산")
        aliases = {build._norm_company(name) for name in (stock.get("company"), reit_names.get(stock["reit_id"])) if name}
        expected = sorted(no for no, x in dis.items() if build._norm_company(x["company"]) in aliases and build.safe_date(x.get("date")))
        notices = [e for e in events if e["kind"] == "disclosure"]
        check(sorted(e["no"] for e in notices) == expected, f"{stock['name']} 전체 기간 공시와 회사명 대조")
        check(all(not e.get("text") for e in notices if "주주" in dis.get(e["no"], {}).get("matter", "")),
              f"{stock['name']} 타임라인에 주주 공시 본문 없음")
    # 재무 자료는 주 1회만 받으므로, 오늘 받은 종목만 원문과 견준다(받은 지 며칠 된 값은 원문이 바뀌었을 수 있다)
    fresh = [r for r in listed if fin.get(str(r.get("reit_id")), {}).get("fetched") == snap["date"]]
    if not fresh:
        print("  건너뜀  오늘 새로 받은 재무 자료가 없어 상세 화면 재조회를 하지 않음")
    for r in random.sample(fresh, min(3, len(fresh))):
        b1 = ask("/reits/prmpc/searchPrmpcOperTab01_01", {"reitsNo": r["reit_id"], "no": r["reit_id"]})
        if b1 is None:
            continue
        v = fin[str(r["reit_id"])]
        check(str(b1.get("totAssets")) == str(v["assets"]) and str(b1.get("cmpnyDebt")) == str(v["debt"]),
              f"{r['name']} | 저장 자산 {v['assets']:,}원, 부채 {v['debt']:,}원 = 상세 화면 재조회")
    d1 = date.fromisoformat(snap["date"])
    d0 = d1 - timedelta(days=14)
    live_d = ask("/reits/pblntf/cmpny/searchPblntfCmpny", {
        "matterCode": "", "cmpnyNm": "", "pblntfContent": "", "pblntfNo": "",
        "pblntfDe1": d0.strftime("%Y%m%d"), "pblntfDe2": d1.strftime("%Y%m%d"), "pageSize": 10, "pageNum": 1})
    if live_d:
        stored = sum(1 for x in dis.values() if d0.isoformat() <= (x.get("date") or "") <= d1.isoformat())
        # 수집 뒤에 새 공시가 올라올 수 있으므로 원문이 조금 많은 것은 허용한다.
        # 원문에서 지운 공시는 저장분에 남으므로 저장이 3건까지 많은 것도 허용한다.
        check(-3 <= int(live_d["total"]) - stored <= 15,
              f"최근 14일 수시공시 원문 {int(live_d['total'])}건, 저장 {stored}건")

    print("[6] DART 차입과 배당")
    dart_path = DATA / "dart.json"
    dart_data = saved_json("dart.json")
    try:
        finance_page = (build.SITE / "finance.html").read_text(encoding="utf-8")
        finance_payload, _ = json.JSONDecoder().raw_decode(finance_page.split("const D = ", 1)[1])
        maturity = {r["code"]: r for r in finance_payload["maturity"]}
        market = {r["code"]: r for r in page_data["rows"]}
    except (OSError, IndexError, ValueError, KeyError, UnboundLocalError):
        maturity, market = {}, {}
    check(set(maturity) == set(market) == {r["code"] for r in listed}, "DART 표와 상장 종목 목록 일치")
    if dart_data:
        check(any(r.get("total") is not None for r in maturity.values()), "저장한 DART 차입 합계의 화면 표시")
    expected_yields, yield_by_code = [], {}
    weighted_yields, expected_runs = [], []  # (시가배당률, 시가총액), 현재 배당 수준
    print("[6-1] ECOS 시장금리")
    rate_data = saved_json("rates.json")
    series = rate_data.get("series", {})
    if (DATA / "rates.json").exists():
        check(set(series) == set(ecos.SERIES), "금리 6개 계열 확인")
    latest_rates = {}
    yield_summary = page_data.get("summary", {}) if codes is not None else {}
    if series:
        check(bool(yield_summary.get("rates") and finance_payload.get("rates")), "두 화면의 저장 금리 표시")
    for code, row in series.items():
        if code not in ecos.SERIES:
            continue
        name, stat, item = ecos.SERIES[code]
        values = row.get("values", {})
        days = list(values)
        try:
            dates = [date.fromisoformat(day) for day in days]
            fetched = date.fromisoformat(row.get("fetched", rate_data.get("fetched")))
            valid_dates = days == sorted(day.isoformat() for day in dates)
        except (ValueError, KeyError, TypeError):
            valid_dates = False
        check(bool(days) and valid_dates, f"{name} 날짜 형식과 오름차순")
        valid_values = bool(days) and all(type(v) in (float, int) and 0 < v < 20 for v in values.values())
        check(valid_values and (row.get("stat"), row.get("item")) == (stat, item), f"{name} 금리 범위와 계열 코드")
        if not (valid_dates and valid_values):
            continue
        latest = dates[-1]
        check(0 <= (fetched - latest).days, f"{name} 최근 관측일이 수집일보다 늦지 않음")
        if (d1 - latest).days > 10:
            # 금리 원문이 오래 막혀도 화면 전체 배포는 막지 않는다. 화면에 기준일이 함께 나온다.
            print(f"  주의  {name} 최근 관측일이 {latest}로 10일 넘게 지났습니다")
        value = values[latest.isoformat()]
        latest_rates[code] = value
        prior = next((day for day in reversed(dates) if 28 <= (latest - day).days <= 35), None)
        expected_summary = dict(value=value, date=latest.isoformat(),
                                change=round(value - values[str(prior)], 3) if prior else None)
        check(yield_summary.get("rates", {}).get(code) == expected_summary
              and finance_payload.get("rates", {}).get(code) == expected_summary, f"{name} 화면 기준일과 한 달 변동")
    secret = os.environ.get("ECOS_API_KEY", "").strip()
    if secret and secret != "sample":
        check(all(secret.encode() not in path.read_bytes() for root in (DATA, build.SITE)
                  for path in root.rglob("*") if path.is_file()), "금리 인증키 저장 여부")
    if os.environ.get("VERIFY_OFFLINE") or not series:
        skipped += 1
    else:
        start = d1 - timedelta(days=13)
        try:
            live = ecos.fetch(*ecos.SERIES["ktb10"][1:], start, d1, ecos.api_key())
        except Exception as exc:
            skipped += 1
            print(f"  건너뜀  국고채 10년 원문 재조회 ({type(exc).__name__})")
        else:
            stored = {day: value for day, value in series.get("ktb10", {}).get("values", {}).items()
                      if str(start) <= day <= str(d1)}
            check(all(live.get(day) == value for day, value in stored.items()), "국고채 10년 최근 14일 원문 대조")
    notes = saved_json("loans.json")
    expected_spreads = []
    price_window_start = (d1 - timedelta(days=372)).isoformat()

    def matches(x, y):
        return x == y or (x is not None and y is not None and abs(x-y) < 1e-9)

    for stock in listed:
        code, name = stock["code"], stock["name"]
        record = dart_data.get(code, {})
        share = rate = coverage = interest_source = None
        for key in ("current", "previous"):
            if not record:
                break
            s = record[key]
            accounts = [a for a in s["accounts"] if a.get("included", True)]
            amounts = [sum(a["amount"] or 0 for a in accounts if a["current"] == cur) for cur in (True, False)]
            check(amounts == [s["short"], s["long"]] and sum(amounts) == s["total"] <= s["liabilities"]
                  and 0 <= amounts[0] <= s["current_liabilities"] and 0 <= amounts[1] <= s["noncurrent_liabilities"],
                  f"{name} {'당기' if key == 'current' else '전기'} 차입 계정 합계와 부채 한도")
            if key == "current" and s["total"]:
                share = s["short"] / s["total"] * 100
            paid = s.get("interest_accounts", [])
            interest = sum(abs(a["amount"]) for a in paid) if paid else s["cost"]
            source = "이자 지급액" if paid else s["cost_name"]
            check(interest == s.get("interest", s["cost"]) and source == s.get("interest_source", s["cost_name"]),
                  f"{name} {'당기' if key == 'current' else '전기'} 이자 지급 계정 합계와 적용 근거")
            if key == "current":
                average = (s["total"] + record["previous"]["total"]) / 2
                if s["total"] and s["days"] and average and interest is not None:
                    rate = interest / average * 365 / s["days"] * 100
                if s["total"] and interest and s["op"] is not None:
                    coverage = s["op"] / interest
                interest_source = source
        maturity_row = maturity.get(code, {})
        if record:
            cur = record["current"]
            expected_amounts = {"total": cur["total"] / 1e8, "short": cur["short"] / 1e8,
                                "extra": cur["short"] * 0.01 / 1e8,
                                "impact": (cur["short"] * cur["days"] / (365 * cur["op"])
                                           if cur["op"] and cur["op"] > 0 and cur["days"] else None)}
            check(all(k in maturity_row and matches(v, maturity_row[k]) for k, v in expected_amounts.items()),
                  f"{name} 차입 합계, 12개월 내 만기, 연 이자 증가액과 영업이익 대비 비율")
        market_row = market.get(code, {})
        history = sorted((day, close) for day, close in prices.get(code, {}).items()
                         if close and day >= price_window_start)
        price = history[-1][1] if history else stock.get("price")
        price_break = next((day for (_, prior), (day, close) in reversed(list(zip(history, history[1:])))
                            if abs(close / prior - 1) > 0.31), None)
        yield_reason = None
        annual_dps = None
        if record.get("dividend_end"):
            end = date.fromisoformat(record["dividend_end"])
            cutoff = previous_year(end)
            periods = [x for x in record["dividends"] if str(cutoff) < x["end"] <= str(end)]
            if (any(x["end"] == str(end) for x in periods) and all(x["dps"] is not None and x["days"] for x in periods)
                    and sum(x["days"] for x in periods) == (end - cutoff).days):
                annual_dps = sum(x["dps"] for x in periods)
            check(annual_dps == record.get("trailing_dps"), f"{name} 최근 12개월 배당 기간과 합계")
            price_day = history[-1][0] if history else snap["date"]
            previous_close = None
            for day, close in sorted(prices.get(code, {}).items()):
                if not close or day > price_day:
                    continue
                if previous_close and day > str(cutoff) and abs(close / previous_close - 1) > 0.31:
                    yield_reason = "기준가 변경"
                    break
                previous_close = close
        expected = None
        if not yield_reason and annual_dps is not None and price and price > 0:
            expected = annual_dps / price * 100
            expected_yields.append(expected)
        yield_by_code[code] = expected
        benchmark = latest_rates.get("ktb10")
        spread = round(expected - benchmark, 2) if expected is not None and benchmark is not None else None
        if spread is not None:
            expected_spreads.append(expected - benchmark)  # 중앙값은 반올림 전 값으로 낸다
        corp = latest_rates.get("corp_aa")
        fx = (code in finance_payload.get("loans", {}).get("issuers", {})
              and any(x["fx"] for x in notes[code]["loans"]))
        difference = (round(rate - corp, 1) if not fx and rate is not None and corp is not None
                      and record["current"]["total"] else None)
        check("spread" in market_row and spread == market_row["spread"], f"{name} 국고채 대비 배당률 차이")
        check(fx == maturity_row.get("fx") and "corp_spread" in maturity_row and difference == maturity_row["corp_spread"], f"{name} 회사채 금리와 차이")
        check(matches(share, maturity_row.get("share")) and matches(rate, maturity_row.get("rate"))
              and matches(coverage, maturity_row.get("coverage")) and interest_source == maturity_row.get("interest_source"),
              f"{name} 화면 만기 비중, 추정 금리와 근거, 영업이익 ÷ 이자")
        check(price == market_row.get("price") and price_break == market_row.get("price_break")
              and record.get("dividend_end") == market_row.get("dividend_end")
              and yield_reason == market_row.get("market_yield_reason")
              and matches(expected, market_row.get("market_yield")), f"{name} 화면 시가배당률과 기준가 변경")
        if expected is not None and market_row.get("mktcap_eok"):
            weighted_yields.append((expected, market_row["mktcap_eok"]))
        # 현재 배당 수준: 마지막 결산기 주당배당금 × round(365 / 결산기 일수) ÷ 현재가
        divs = record.get("dividends") or []
        last_div = divs[-1] if divs else {}
        prev_div = divs[-2] if len(divs) > 1 else {}
        k = round(365 / last_div["days"]) if last_div.get("days") else None
        run = (last_div["dps"] * k / price * 100 if not yield_reason and not market_row.get("halted") and k
               and last_div.get("dps") is not None and price and price > 0 else None)
        if run is not None:
            expected_runs.append(run)
        chg = (round((last_div["dps"] / prev_div["dps"] - 1) * 100, 1)
               if last_div.get("dps") is not None and prev_div.get("dps") else None)
        jump = bool(record.get("dividend_end")) and any(
            str(previous_year(date.fromisoformat(record["dividend_end"]))) < b["end"] <= record["dividend_end"]
            and a.get("dps") and b.get("dps") is not None and b["dps"] >= 1.5 * a["dps"] for a, b in zip(divs, divs[1:]))
        check(matches(run, market_row.get("run_yield")) and chg == market_row.get("dps_chg") and jump == market_row.get("dps_jump"),
              f"{name} 현재 배당 수준, 주당배당금 전기 대비, 1.5배 넘는 배당 표시")
    spread_median = round(median(expected_spreads), 2) if expected_spreads else None
    check(yield_summary.get("spread_n") == len(expected_spreads) and yield_summary.get("spread_median") == spread_median,
          "국고채 대비 배당률 차이 중앙값과 종목 수")
    expected_median = round(median(expected_yields), 2) if expected_yields else None
    wavg = (sum(y * w for y, w in weighted_yields) / sum(w for _, w in weighted_yields)) if weighted_yields else None
    bench = latest_rates.get("ktb10")
    check(matches(yield_summary.get("yield_wavg"), round(wavg, 2) if wavg is not None else None)
          and matches(yield_summary.get("spread_wavg"), round(wavg - bench, 2) if wavg is not None and bench is not None else None),
          f"시가총액 가중 시가배당률 {yield_summary.get('yield_wavg')}%와 국고채 대비 차이 재계산")
    check(yield_summary.get("run_n") == len(expected_runs)
          and matches(yield_summary.get("run_median"), round(median(expected_runs), 2) if expected_runs else None),
          f"현재 배당 수준 중앙값 {yield_summary.get('run_median')}%({len(expected_runs)}개) 재계산")
    check(yield_summary.get("yield_n") == len(expected_yields) and matches(yield_summary.get("yield_median"), expected_median),
          "시가배당률 값이 있는 종목 수와 중앙값 재계산")
    verify_loans(check, dart_data, finance_payload.get("loans", {}), listed, snap["date"])
    # 감정가 기준 NAV = 지배기업 소유주지분 + (공정가치 - 장부금액) × 지배지분 비율. 비지배지분이 5%를 넘으면 비움
    fv_data = saved_json("fv.json")
    fv_ok, fv_vals, fv_by_code, stale_ok, stale_kind, stale_pnav = [], [], {}, [], Counter(), 0
    fv_methods = ("장부금액과 공정가치 표", "공정가치 모형", "공정가치 문장", "공정가치 서열체계 표")
    for row in (page_data.get("rows", []) if codes is not None else []):
        rec, fvr = dart_data.get(row["code"], {}), fv_data.get(row["code"], {})
        cur = rec.get("current", {})
        want = nav_want = pb_want = None
        # 공정가치는 같은 보고서, 같은 기준(연결, 별도), 같은 결산일, 같은 투자부동산 금액에서 읽었고 장부금액의 0.5~2배일 때만 쓴다
        usable = (fvr.get("fv") is not None and bool(fvr.get("book"))
                  and tuple(fvr.get(k) for k in ("rcept_no", "basis", "end", "book"))
                  == (rec.get("rcept_no"), rec.get("basis"), cur.get("end"), cur.get("investment_property"))
                  and 0.5 <= fvr["fv"] / fvr["book"] <= 2)
        owner = cur.get("equity_owner")
        if owner is None:  # 별도는 자본총계, 연결은 자본총계 - 비지배지분, 둘 다 없으면 비움
            owner = cur.get("equity") if rec.get("basis") != "CFS" else (
                cur["equity"] - cur["nci"] if cur.get("equity") is not None and cur.get("nci") is not None else None)
        same_report = usable and (cur.get("equity") or 0) > 0 and owner is not None
        if same_report and abs(cur["equity"] - owner) <= 0.05 * cur["equity"]:
            nav = owner + (fvr["fv"] - fvr["book"]) * min(1, owner / cur["equity"])
            nav_want = round(nav / 1e8)
            if nav > 0 and row["mktcap_eok"]:
                want = row["mktcap_eok"] * 1e8 / nav
                pb_want = round(row["mktcap_eok"] * 1e8 / owner, 2) if owner > 0 else None
        if want is not None:
            fv_vals.append(want)
        fv_by_code[row["code"]] = want
        ok = row.get("pnav_fv") == (round(want, 2) if want is not None else None) and row.get("nav_fv_eok") == nav_want
        ok &= row.get("pb_dart") == pb_want
        # 기준일 링크와 다른 방식 대조 결과는 공정가치를 읽은 보고서(= 재무상태표를 읽은 보고서)의 것이어야 한다
        ok &= row.get("fv_rcept") == (fvr["rcept_no"] if same_report else None)
        ok &= row.get("fv_confirm") == ([x for x in fvr.get("confirm") or [] if x in fv_methods] if same_report else [])
        # 감정 시점 신호: 주석의 '공정가치 평가를 다시 하지 않음' 문장이 먼저, 없으면 같은 주석 표의 전기말 공정가치가 당기와 같은지
        stale = fv_stale_of(fvr) if same_report else None
        dates = [d for d in fvr.get("appraisal_dates") or [] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d))] if stale == "skipped" else []
        # 평가기준일은 결산일 전 날짜이고, 전기말 공정가치는 표 방식일 때만 있다
        stale_ok.append(row.get("fv_stale") == stale and row.get("fv_appraisal_dates") == dates
                        and all(d < fvr["end"] for d in dates)
                        and (fvr.get("fv_prior") is None or fvr.get("method") == "장부금액과 공정가치 표"))
        if stale:
            stale_kind[stale] += 1
            stale_pnav += want is not None
        if usable:
            # 같은 보고서일 때만 출처를 대조한다. 새 사업보고서가 들어오고 주석을 아직 못 읽었으면 화면이 비어 있는지만 본다
            ok &= fvr["method"] in fv_methods and fvr["method"] not in (fvr.get("confirm") or [])
            if fvr["method"] == "장부금액과 공정가치 표":
                ok &= abs(fvr["table_book"] - fvr["book"]) <= 0.002 * fvr["book"]
            elif fvr["method"] == "공정가치 모형":
                ok &= fvr["fv"] == fvr["book"]
        fv_ok.append(ok)
    check(all(fv_ok), f"감정가 기준 P/NAV {sum(fv_ok)}/{len(fv_ok)}종목 재계산과 공정가치 출처 대조")
    check(all(stale_ok) and yield_summary.get("pnav_fv_stale") == stale_pnav,
          f"감정 시점 신호 {sum(stale_kind.values())}종목(평가 생략 문장 {stale_kind['skipped']}, 전기말과 같음 {stale_kind['same']}), "
          f"감정가 P/NAV가 있는 종목 중 {stale_pnav}개 재계산")
    check(yield_summary.get("pnav_fv_n") == len(fv_vals)
          and yield_summary.get("pnav_fv_median") == (round(median(fv_vals), 2) if fv_vals else None),
          f"감정가 기준 P/NAV 중앙값 {yield_summary.get('pnav_fv_median')}배({len(fv_vals)}개) 재계산")
    # 투자대상별 중앙값: 수집 목록의 투자대상으로 다시 묶고 종목별 원값(P/B, 감정가 P/NAV, 시가배당률, LTV)으로 다시 낸다.
    # 1년 수익률은 화면 값을 쓰되 거래가 멈춘 종목은 뺀다
    if codes is not None:
        rows_by = {r["code"]: r for r in page_data["rows"]}

        def group_want(name, members):
            med = lambda xs, k: round(median(xs), k) if xs else None
            bs = lambda s: (dart_data.get(s["code"]) or {}).get("current") or {}
            pb = [s["mktcap_eok"] / (s["nav_mil"] / 100) for s in members
                  if s.get("nav_mil") and s["nav_mil"] > 0 and s.get("mktcap_eok")]
            fv = [fv_by_code[s["code"]] for s in members if fv_by_code.get(s["code"]) is not None]
            yl = [yield_by_code[s["code"]] for s in members if yield_by_code.get(s["code"]) is not None]
            yw = [(yield_by_code[s["code"]], s["mktcap_eok"]) for s in members
                  if yield_by_code.get(s["code"]) is not None and s.get("mktcap_eok")]
            lt = [bs(s)["total"] / bs(s)["assets"] * 100 for s in members
                  if bs(s).get("assets") and bs(s).get("total") is not None]
            rt = [rows_by[s["code"]]["ret_1y"] for s in members
                  if rows_by[s["code"]]["ret_1y"] is not None and not rows_by[s["code"]]["halted"]]
            return {"name": name, "n": len(members), "mktcap_eok": sum(s["mktcap_eok"] or 0 for s in members),
                    "pnav_median": med(pb, 2), "pnav_n": len(pb), "pnav_fv_median": med(fv, 2), "pnav_fv_n": len(fv),
                    "yield_median": med(yl, 2), "yield_n": len(yl),
                    "yield_wavg": round(sum(y * w for y, w in yw) / sum(w for _, w in yw), 2) if yw else None,
                    "ltv_median": med(lt, 1), "ltv_n": len(lt),
                    "ret_1y_median": med(rt, 1), "ret_1y_n": len(rt)}

        names = {s.get("sector") or "기타" for s in listed}
        want_groups = sorted((group_want(nm, [s for s in listed if (s.get("sector") or "기타") == nm]) for nm in names),
                             key=lambda x: (-x["mktcap_eok"], x["name"]))
        check(page_data.get("sectors") == want_groups and page_data.get("sector_all") == group_want("전체", listed)
              and page_data["sector_all"]["yield_wavg"] == page_data["summary"]["yield_wavg"],
              "투자대상별 P/B, 감정가 P/NAV, 시가배당률, LTV, 1년 수익률 중앙값 재계산"
              + "".join(f" · {g['name']} P/B {g['pnav_median']}배" for g in want_groups))
    raw = b"\n".join(p.read_bytes() for p in (dart_path, DATA / "loans.json") if p.exists())
    check(not re.search(rb"[0-9a-fA-F]{40}", raw), "DART 자료 40자리 인증정보 문자열 점검")
    secret = os.environ.get("DART_API_KEY")
    if secret:
        needle = secret.encode()
        check(needle not in raw and all(needle not in p.read_bytes() for p in build.SITE.rglob("*") if p.is_file()),
              "DART 자료와 배포 파일 전체 인증키 점검")

    print("[7] 개인정보")
    leaks = phone_files(DATA.rglob("*.json")) + phone_files(build.SITE.glob("*.html"))
    check(not leaks, "저장 파일과 화면 전화번호 점검" + " ".join(str(p) for p in leaks))
    check(all(not v.get("text") for v in dis.values() if "주주" in v["matter"]), "주주 공시 본문 없음")
    named = [no for no, v in dis.items() if officer_names(v.get("text"))]
    # 화면에 실리는 공시 본문(주요 공시 목록, 종목별 타임라인)을 한 건씩 본다. 가림 함수와 같은 범위로 봐야
    # 화면 전체에 흩어진 낱말을 이어 붙여 엉뚱한 이름을 잡지 않는다.
    page_texts = [x["text"] for x in fd["disclosures"]] + [e.get("text") or "" for ev in fd["timelines"].values() for e in ev]
    named_pages = [t[:40] for t in page_texts if officer_names(t)]
    check(not named and not named_pages, "공시 본문과 화면 본문에 임원 이름 없음(선임, 사임 문구)"
          + (f" 공시번호 {', '.join(named[:5])}" if named else "") + (f" 화면 {' / '.join(named_pages[:3])}" if named_pages else ""))
    officer_cases = [("(사임) 대표이사 홍길동, 사내이사 김철수", "(사임) 대표이사 (이름 생략), 사내이사 (이름 생략)"),
                     ("감사 박하늘 선임", "감사 (이름 생략) 선임"),
                     ("이사 보수 한도 승인", None), ("사외이사 선임의 건", None),
                     ("사내이사 홍길동, 김철수 선임", "사내이사 (이름 생략), (이름 생략) 선임"),
                     ("사내이사 홍길동 선임 및 김철수 사임", "사내이사 (이름 생략) 선임 및 (이름 생략) 사임"),
                     ("대표이사 홍길동 외 1인 선임", "대표이사 (이름 생략) 외 1인 선임"),
                     ("사내이사 홍길동·김철수 선임", "사내이사 (이름 생략)·(이름 생략) 선임"),
                     ("이사 선임: 홍길동", "이사 선임: (이름 생략)"),
                     ("대표이사 홍길동 선출", "대표이사 (이름 생략) 선출"),
                     ("대표이사 홍길동 임기만료", "대표이사 (이름 생략) 임기만료"),
                     ("(변경 전) 대표이사 홍길동 (변경 후) 대표이사 김철수",
                      "(변경 전) 대표이사 (이름 생략) (변경 후) 대표이사 (이름 생략)"),
                     ("제5기 외부감사 결과(적정의견) 사내이사 선임", None), ("회계감사 계약(3년) 감사인 선임", None),
                     ("감독이사 선임에는 적용되지 않는 집중투표제 배제", None),
                     ("대표이사 홍길동, 사내이사 선임", "대표이사 (이름 생략), 사내이사 선임")]
    for text, expected in officer_cases:
        expected = expected or text
        masked = mask_officer_names(text)
        check(masked == expected and not officer_names(masked), f"임원 이름 가상 입력 {expected}")
    cases = [(name, PRIVATE_LABEL) for name in ("홍길동(개인)", "홍길동 (본인)", "남궁민수", "김솔", "박하늘 외 2인")]
    cases += [(name, name) for name in ("㈜에스원", "주식회사 케이탑", "롯데쇼핑", "(주)한빛", "한솔 유한회사")]
    for name, expected in cases:
        check(mask_person(name) == expected, f"최대주주 가상 입력 {name}")
    # 수집기를 불러오지 않고 저장을 허용한 항목을 점검한다.
    allowed = {"id", "reit_id", "date", "req_type", "name", "applicant", "status",
               "project_name", "biz_type", "founding_capital", "public_duty"}
    flog = json.loads((DATA / "filings_log.json").read_text(encoding="utf-8"))
    extra_keys = {"first_seen", "last_seen", "status_history", "gone_on", "result_kind", "result_date"}
    check(all(set(f) <= allowed for f in snap["filings"]) and all(set(v) <= allowed | extra_keys for v in flog.values()),
          "접수 자료에 허용한 항목만 저장됨(담당자 이름, 연락처 항목 없음)")
    check(all(mask_person(v.get("top_shareholder")) == v.get("top_shareholder") for v in fin.values()),
          "최대주주 칸에 개인 이름 모양 값 없음")
    # 화면 값은 회사 표기를 뺀 이름이다. 빼고 나서 개인 이름처럼 보이는 회사는 ㈜를 남겨야 한다
    shown_holders = [r["top_shareholder"] for r in fd["rows"] if r.get("top_shareholder")]
    check(all(mask_person(h) == h and h.count("(") == h.count(")") for h in shown_holders),
          f"화면 최대주주 {len(shown_holders)}개가 개인 이름처럼 보이지 않고 괄호 짝이 맞음")

    print("[8] 최근 1주 동향")
    try:
        index_payload, _ = json.JSONDecoder().raw_decode((build.SITE / "index.html").read_text(encoding="utf-8").split("const D = ", 1)[1])
        week = index_payload.get("weekly") or {}
    except (OSError, IndexError, ValueError):
        week = {}
    end = snap["date"]
    start = (date.fromisoformat(end) - timedelta(days=6)).isoformat()
    check(week.get("start") == start and week.get("end") == end, f"최근 1주 동향 구간 {start}~{end}")
    new_reits = sorted((r["date"], r["kind"]) for r in reits if start <= (r["date"] or "") <= end)
    check(sorted((x["date"], x["kind"]) for x in week.get("new_reits", [])) == new_reits,
          f"최근 1주 새로 생긴 리츠 {len(new_reits)}개(운영 목록의 인가, 등록, 신고일 기준)")
    received = sorted(f["date"] for f in snap["filings"] if start <= (f["date"] or "") <= end)
    check(sorted(x["date"] for x in week.get("received", [])) == received, f"최근 1주 접수일인 신청 {len(received)}건")
    status_moves = sorted((day, after) for rec in flog.values()
                          for (_, _before), (day, after) in zip(rec.get("status_history") or [], (rec.get("status_history") or [])[1:])
                          if start <= day <= end)
    check(sorted((x["date"], x["after"]) for x in week.get("changed", [])) == status_moves,
          f"최근 1주 상태가 바뀐 신청 {len(status_moves)}건(접수 기록의 상태 이력 기준)")
    gone_week = sorted(rec["gone_on"] for rec in flog.values() if rec.get("gone_on") and start <= rec["gone_on"] <= end)
    check(sorted(x["date"] for x in week.get("gone", [])) == gone_week, f"최근 1주 접수 목록에서 빠진 신청 {len(gone_week)}건")
    week_groups = Counter(build.dis_group(x) for x in dis_saved.values()
                          if build.dis_group(x) and start <= (x.get("date") or "") <= end)
    check(week.get("dis_n") == sum(week_groups.values())
          and week.get("dis_groups") == {g: week_groups[g] for g in build.GROUP_ORDER if week_groups[g]},
          f"최근 1주 주요 공시 {sum(week_groups.values())}건과 유형별 건수(저장한 공시에서 다시 셈)")
    # 종목별 1주 수익률: 저장한 종가에서 마지막 종가일과 그 7일 전(그날이나 그 앞의 마지막 거래일)을 견준다
    row_flags = {str(r["code"]): r for r in ld["rows"]}
    moves = []
    for code, row in row_flags.items():
        hist = {k: v for k, v in prices.get(code, {}).items() if v}
        days = sorted(hist)
        if len(days) < 2 or row.get("halted"):
            continue
        base_days = [x for x in days if x <= (date.fromisoformat(days[-1]) - timedelta(days=7)).isoformat()]
        if not base_days:
            continue
        span = [x for x in days if x >= base_days[-1]]
        if any(abs(hist[b] / hist[a] - 1) > 0.31 for a, b in zip(span, span[1:])):
            continue  # 기준가가 바뀐 날이 끼면 비교하지 않는다
        moves.append((round((hist[days[-1]] / hist[base_days[-1]] - 1) * 100, 1), row["name"]))
    moves.sort()
    check(week.get("up") == sum(m > 0 for m, _ in moves) and week.get("down") == sum(m < 0 for m, _ in moves),
          f"최근 1주 오른 종목 {sum(m > 0 for m, _ in moves)}개, 내린 종목 {sum(m < 0 for m, _ in moves)}개")
    check([(x["ret"], x["name"]) for x in week.get("bottom", [])] == moves[:3]
          and [(x["ret"], x["name"]) for x in week.get("top", [])] == moves[::-1][:3],
          "최근 1주 많이 오르고 내린 종목 3개씩과 수익률")
    idx_pairs = list(zip(ld["index"]["dates"], ld["index"]["values"]))
    if idx_pairs:
        base_pairs = [p for p in idx_pairs if p[0] <= (date.fromisoformat(idx_pairs[-1][0]) - timedelta(days=7)).isoformat()]
        expected_week = round((idx_pairs[-1][1] / base_pairs[-1][1] - 1) * 100, 1) if base_pairs else None
        check((week.get("index") or {}).get("ret") == expected_week, f"최근 1주 주가 지수 변화 {expected_week}%")
    saved_rates = build.optional_data("rates.json").get("series", {})
    for code in ("ktb10", "corp_aa"):
        values = saved_rates.get(code, {}).get("values") or {}
        if not values:
            continue
        latest = max(values)
        prev = [x for x in values if x <= (date.fromisoformat(latest) - timedelta(days=7)).isoformat()]
        expected_rate = round(values[latest] - values[max(prev)], 3) if prev else None
        got_rate = week.get("rates", {}).get(code, {})
        check(got_rate.get("value") == values[latest] and got_rate.get("change") == expected_rate,
              f"최근 1주 금리 {code} {values[latest]}%, 1주 전 대비 {expected_rate}%p")

    print("결과:", f"통과 {passed}건, 실패 {bad}건"
          + (f", 건너뛴 원문 재조회 {skipped}건" if skipped else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
