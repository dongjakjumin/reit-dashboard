"""배포 전 점검.

1) 저장한 자료 안에서 합계가 서로 맞는지 본다(구분별 합 = 전체 등).
2) 리츠정보시스템에 다시 물어 전체 건수가 저장분과 같은지 본다.
3) 리츠 몇 건을 뽑아 상세 화면(다른 조회 경로)의 값과 목록 값을 대조한다.
4) 상장리츠: 건수, 합계, P/NAV 재계산, 주가 이력, 첫 화면의 NAV와 FFO 대조.
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
from datetime import date, timedelta
from pathlib import Path
from statistics import median

import build
import reits_api
from dart import previous_year
from textutil import PRIVATE_LABEL, mask_person, phone_files

DATA = Path(__file__).parent / "data"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = passed = 0

    def check(ok: bool, msg: str):
        nonlocal bad, passed
        print(("  통과  " if ok else "  실패  ") + msg)
        if ok:
            passed += 1
        else:
            bad += 1

    skipped = 0

    def ask(path: str, data: dict):
        """원문 재조회. 접속이 안 되면 실패로 세지 않고 건너뛴 것으로 적는다(숫자가 어긋난 것과 구분)."""
        nonlocal skipped
        if os.environ.get("VERIFY_OFFLINE"):  # 화면만 다시 올릴 때는 원문에 접속하지 않는다
            skipped += 1
            return None
        try:
            return reits_api.post(path, data)
        except Exception as e:
            skipped += 1
            print(f"  건너뜀  {path} 접속 실패: {str(e)[:90]}")
            return None

    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    listed_page = ""
    for name in ("index", "listed", "finance", "calc"):
        path = build.SITE / f"{name}.html"
        html = path.read_text(encoding="utf-8") if path.is_file() else ""
        check(bool(html.strip()), f"{path.name} 생성 및 내용 확인")
        if name == "listed":
            listed_page = html
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
    check(sum(x["n"] for x in d["sector"]) == s["n_reits"], "투자대상별 리츠 수 합 = 전체")
    check(sum(s["new12_by_kind"].values()) == s["new12"], "최근 12개월 구분별 합 = 최근 12개월 전체")
    check(sum(s["filings_by_type"].values()) == s["filings_n"], "접수 유형별 합 = 접수 전체")
    check(sum(1 for r in reits if r["kind"] == "신고" or r.get("project") == "Y") == s["proj_n"],
          "신고 구분이거나 프로젝트리츠 표시가 있는 건수 = 프로젝트리츠 건수")
    check(sum(a["new"] + a["change"] for a in d["applicants"]) == len(snap["filings"]),
          "신청인별 신규와 변경 합 = 처리 중인 신청 전체")
    project = d["project"]
    check(sum(a["n"] for a in project["companies"]) == s["proj_n"], "관리주체별 합 = 프로젝트리츠 전체")
    check(sum(project["sectors"].values()) == s["proj_n"], "투자대상별 합 = 프로젝트리츠 전체")
    check(sum(project["months"].values()) == s["proj_n"], "첫 신고 달부터 월별 합 = 프로젝트리츠 전체")
    check(sum(1 for e in d["events"] if e["what"] == "접수") == len(snap["filings"]),
          "최근 변동에 접수 중인 신청이 빠짐없이 들어 있음")
    check(all(x["result_date"] is None for x in d["done"] if x["is_change"]),
          "변경 신청에는 인가일을 결과로 붙이지 않음")
    check(len({r["id"] for r in reits}) == len(reits), "리츠 번호 중복 없음")

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
          "P/NAV = 시가총액 / NAV 재계산 일치")
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
    check(sum(g["n"] for g in fd["groups"]) == fd["summary"]["dis_major"], "공시 유형별 건수 합 = 주요 공시 건수")
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
        # 수집 뒤에 새 공시가 올라올 수 있으므로 원문이 조금 많은 것은 허용한다
        check(0 <= int(live_d["total"]) - stored <= 15,
              f"최근 14일 수시공시 원문 {int(live_d['total'])}건, 저장 {stored}건")

    print("[6] DART 차입과 배당")
    dart_path = DATA / "dart.json"
    dart_data = build.optional_data("dart.json")
    try:
        finance_page = (build.SITE / "finance.html").read_text(encoding="utf-8")
        finance_payload, _ = json.JSONDecoder().raw_decode(finance_page.split("const D = ", 1)[1])
        maturity = {r["code"]: r for r in finance_payload["maturity"]}
        market = {r["code"]: r for r in page_data["rows"]}
    except (OSError, IndexError, ValueError, KeyError, UnboundLocalError):
        maturity, market = {}, {}
    check(set(maturity) == set(market) == {r["code"] for r in listed}, "DART 표와 상장 종목 목록 일치")
    expected_yields = []
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
                if s["days"] and average and interest is not None:
                    rate = interest / average * 365 / s["days"] * 100
                if s["total"] and interest and s["op"] is not None:
                    coverage = s["op"] / interest
                interest_source = source
        maturity_row = maturity.get(code, {})
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
            if price_break and str(cutoff) < price_break <= str(end):
                yield_reason = "기준가 변경"
        expected = None
        if not yield_reason and annual_dps is not None and price and price > 0:
            expected = annual_dps / price * 100
            expected_yields.append(expected)
        check(matches(share, maturity_row.get("share")) and matches(rate, maturity_row.get("rate"))
              and matches(coverage, maturity_row.get("coverage")) and interest_source == maturity_row.get("interest_source"),
              f"{name} 화면 만기 비중, 추정 금리와 근거, 영업이익 ÷ 이자")
        check(price == market_row.get("price") and price_break == market_row.get("price_break")
              and yield_reason == market_row.get("market_yield_reason")
              and matches(expected, market_row.get("market_yield")), f"{name} 화면 시가배당률과 기준가 변경")
    yield_summary = page_data.get("summary", {}) if codes is not None else {}
    expected_median = round(median(expected_yields), 2) if expected_yields else None
    check(yield_summary.get("yield_n") == len(expected_yields) and matches(yield_summary.get("yield_median"), expected_median),
          "시가배당률 값이 있는 종목 수와 중앙값 재계산")
    raw = dart_path.read_bytes() if dart_path.exists() else b""
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

    print("결과:", f"통과 {passed}건, 실패 {bad}건"
          + (f", 건너뛴 원문 재조회 {skipped}건" if skipped else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
