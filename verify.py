"""배포 전 점검.

1) 저장한 자료 안에서 합계가 서로 맞는지 본다(구분별 합 = 전체 등).
2) 리츠정보시스템에 다시 물어 전체 건수가 저장분과 같은지 본다.
3) 리츠 몇 건을 뽑아 상세 화면(다른 조회 경로)의 값과 목록 값을 대조한다.
4) 상장리츠: 건수, 합계, P/NAV 재계산, 주가 이력, 첫 화면의 NAV와 FFO 대조.
5) 저장 파일에 전화번호 모양의 문자열이 없는지 본다.

숫자가 하나라도 어긋나면 종료 코드 1. 원문 접속이 안 돼 재조회를 못 한 항목은 건너뛴 것으로 적고 실패로 세지 않는다.
"""
from __future__ import annotations

import json
import random
import re
import sys
import urllib.request
from pathlib import Path

import build
import reits_api

DATA = Path(__file__).parent / "data"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = 0

    def check(ok: bool, msg: str):
        nonlocal bad
        print(("  통과  " if ok else "  실패  ") + msg)
        bad += 0 if ok else 1

    skipped = 0

    def ask(path: str, data: dict):
        """원문 재조회. 접속이 안 되면 실패로 세지 않고 건너뛴 것으로 적는다(숫자가 어긋난 것과 구분)."""
        nonlocal skipped
        try:
            return reits_api.post(path, data)
        except Exception as e:
            skipped += 1
            print(f"  건너뜀  {path} 접속 실패: {str(e)[:90]}")
            return None

    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
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
    check(sum(1 for r in reits if r["kind"] == "신고") == s["proj_n"], "구분이 신고인 건수 = 프로젝트리츠 건수")
    check(len({r["id"] for r in reits}) == len(reits), "리츠 번호 중복 없음")

    print("[2] 원문 건수 재조회")
    live = ask("/reits/prmpc/searchPrmpcOper", dict(reits_api.OPER_EMPTY, pageSize=10, pageNum=1))
    if live:
        check(int(live["total"]) == len(reits), f"리츠조회 전체 {int(live['total'])}건 = 저장 {len(reits)}건")
    live_f = ask("/reits/prmpc/searchPrmpcInvtCnfm", {"reitsNm": "", "cmpnyMberNm": "", "pageSize": 10, "pageNum": 1})
    if live_f:
        check(int(live_f["total"]) == len(snap["filings"]), f"접수현황 전체 {int(live_f['total'])}건 = 저장 {len(snap['filings'])}건")

    print("[3] 상세 화면 대조(자산총계가 있는 리츠 5건 + 강조 회사)")
    pool = [r for r in reits if r["assets_eok"]]
    random.seed(snap["date"])
    sample = random.sample(pool, 5)
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
        ok = name_ok and (got is None or abs(got - r["assets_eok"]) <= 1)
        check(ok, f"{r['name']} | 목록 {r['assets_eok']:,}억 | 상세 {got if got is not None else '값 없음'}")

    print("[4] 상장리츠")
    ld = build.build_listed_data()
    ls = ld["summary"]
    listed = snap["listed"]
    prices = json.loads((DATA / "prices.json").read_text(encoding="utf-8"))
    live_l = ask("/invt/lsted/searchLstedReits", {"pageSize": 10, "pageNum": 1})
    if live_l:
        check(int(live_l["total"]) == len(listed), f"상장리츠 현황 전체 {int(live_l['total'])}건 = 저장 {len(listed)}건")
    check(sum(x["mktcap_eok"] for x in ld["sectors"]) == ls["mktcap_eok"], "투자대상별 시가총액 합 = 전체 시가총액")
    check(sum(x["n"] for x in ld["sectors"]) == ls["n"], "투자대상별 종목 수 합 = 전체 종목 수")
    check(all(r["pnav"] is None or abs(r["mktcap_eok"] / r["nav_eok"] - r["pnav"]) < 0.006 for r in ld["rows"]),
          "P/NAV = 시가총액 / NAV 재계산 일치")
    check(all(prices[str(r["code"])][max(prices[str(r["code"])])] == r["price"] for r in listed),
          "주가 이력의 마지막 종가 = 상장리츠 현황의 현재가")
    check(abs(ld["index"]["values"][0] - 100) < 0.001, "주가 지수 첫날 = 100")
    nav_rows = ask("/main/searchMainStock", {"stockTab": "3"})
    ffo_rows = ask("/main/searchMainStock", {"stockTab": "4"})
    nav_main = {str(x.get("stockNm")).strip(): x.get("navThstrm") for x in (nav_rows or [])}
    ffo_main = {str(x.get("stockNm")).strip(): x.get("ffoThstrm") for x in (ffo_rows or [])}
    for r in (random.sample(listed, 5) if nav_rows and ffo_rows else []):
        ok = nav_main.get(r["name"]) == r["nav_mil"] and ffo_main.get(r["name"]) == r["ffo_mil"]
        check(ok, f"{r['name']} | NAV {r['nav_mil']:,}백만원, FFO {r['ffo_mil']:,}백만원 | "
                  f"첫 화면 NAV {nav_main.get(r['name'])}, FFO {ffo_main.get(r['name'])}")

    print("[5] 개인정보")
    blob = "".join(p.read_text(encoding="utf-8") for p in DATA.rglob("*.json"))
    for site in (Path(__file__).parent / "site").glob("*.html"):
        blob += site.read_text(encoding="utf-8")
    check(not re.search(r"01[016789]-?\d{3,4}-?\d{4}", blob), "저장 파일과 화면에 휴대전화 번호 모양 문자열 없음")
    check('"mberPhone"' not in blob and '"mberNm"' not in blob, "담당자 항목 이름 없음")

    print("결과:", ("모두 통과" if bad == 0 else f"실패 {bad}건")
          + (f", 접속 실패로 건너뛴 재조회 {skipped}건" if skipped else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
