"""하루 1회 실행하는 수집기.

리츠정보시스템에서 세 목록(운영 중 리츠, 접수현황, 상장리츠)을 받아
 - data/snapshots/날짜.json : 그날 받은 원자료(개인정보 항목 제외)
 - data/latest.json         : 가장 최근 수집분
 - data/filings_log.json    : 접수 건별 누적 기록(처음 본 날, 마지막으로 본 날, 진행상태 변화)
 - data/reits_log.json      : 리츠별 누적 기록(처음 본 날, 마지막으로 본 날)
을 만든다.

접수현황은 현재 처리 중인 건만 보여 주므로, 날마다 쌓아 둔 기록이 있어야
"접수에서 인가까지 걸린 기간"을 계산할 수 있다.

개인정보: 접수현황 응답에는 신청 담당자 이름과 전화번호가 들어 있다.
아래 FILING_KEEP에 적은 항목만 저장하고 나머지는 버린다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import reits_api

KST = timezone(timedelta(hours=9))
DATA = Path(__file__).parent / "data"
SNAP = DATA / "snapshots"

REIT_KEEP = {
    "reitsNo": "id", "reqstNo": "reqst_no", "reitsNm": "name", "mberNm": "amc",
    "cnfmAtNm": "kind", "prjctReitsAt": "project", "confmDe": "date",
    "reitsTyNm": "type", "invtTrgetNm": "sector", "pssrpNm": "public_offering",
    "totAssets": "assets_eok", "ratio": "dividend_pct", "lstDe": "listed_date",
}
FILING_KEEP = {
    "crrDetailNo": "id", "estateInvtCmpnyNo": "reit_id", "rceptDe": "date",
    "reqstTyNm": "req_type", "reitsNm": "name", "cmpnyMberNm": "applicant",
    "progrsSttusNm": "status", "bsnsNm": "project_name", "reitsBsnsTyNm": "biz_type",
    "fondCptl": "founding_capital", "pssrpDutyAt": "public_duty",
}
LISTED_KEEP = {
    "stockCd": "code", "stockNm": "name", "cmpnyNm": "company", "lstDt": "listed_date",
    "nowPc": "price", "beforePc": "prev_price", "mktcTotamt": "mktcap_eok",
    "tradeVolume": "volume", "hmpg": "homepage", "stockDtTitle1": "as_of",
}


def _num(v):
    """'10,300,000,000' 같은 문자열을 숫자로. 빈 값은 None."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s == "":
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f == int(f) else f


def _pick(row: dict, keep: dict, numeric: tuple[str, ...]) -> dict:
    out = {new: row.get(old) for old, new in keep.items()}
    for k in numeric:
        out[k] = _num(out.get(k))
    for k, v in out.items():
        if isinstance(v, str):
            out[k] = v.strip()
    return out


def collect() -> dict:
    now = datetime.now(KST)
    reits = [_pick(r, REIT_KEEP, ("id", "reqst_no", "assets_eok", "dividend_pct"))
             for r in reits_api.reits_list()]
    filings = [_pick(r, FILING_KEEP, ("id", "reit_id", "founding_capital"))
               for r in reits_api.filings_list()]
    listed = [_pick(r, LISTED_KEEP, ("price", "prev_price", "mktcap_eok", "volume"))
              for r in reits_api.listed_list()]
    if len(reits) < 300:
        raise RuntimeError(f"리츠 목록이 {len(reits)}건뿐임. 수집 이상으로 보고 저장하지 않음")
    return {
        "collected_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "date": now.strftime("%Y-%m-%d"),
        "source": "국토교통부 리츠정보시스템(reits.molit.go.kr)",
        "reits": reits,
        "filings": filings,
        "listed": listed,
    }


def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save(path: Path, obj, pretty: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None,
                      separators=None if pretty else (",", ":"))
    path.write_text(text + "\n", encoding="utf-8")


def update_logs(snap: dict):
    today = snap["date"]

    # 접수 건별 누적 기록
    flog = _load(DATA / "filings_log.json", {})
    seen = set()
    for f in snap["filings"]:
        key = str(f["id"])
        seen.add(key)
        rec = flog.get(key)
        if rec is None:
            rec = dict(f, first_seen=today, last_seen=today,
                       status_history=[[today, f["status"]]], gone_on=None)
            flog[key] = rec
        else:
            if rec["status_history"][-1][1] != f["status"]:
                rec["status_history"].append([today, f["status"]])
            rec.update({k: v for k, v in f.items()})
            rec["last_seen"] = today
            rec["gone_on"] = None
    for key, rec in flog.items():
        if key not in seen and rec.get("gone_on") is None:
            rec["gone_on"] = today  # 목록에서 빠진 것을 처음 확인한 날
    _save(DATA / "filings_log.json", flog, pretty=True)

    # 리츠별 누적 기록(목록에 새로 들어온 날, 빠진 날)
    rlog = _load(DATA / "reits_log.json", {})
    seen = set()
    for r in snap["reits"]:
        key = str(r["id"])
        seen.add(key)
        rec = rlog.get(key)
        if rec is None:
            rlog[key] = {"name": r["name"], "amc": r["amc"], "kind": r["kind"],
                         "date": r["date"], "first_seen": today, "last_seen": today,
                         "gone_on": None}
        else:
            rec.update({"name": r["name"], "amc": r["amc"], "kind": r["kind"],
                        "date": r["date"], "last_seen": today, "gone_on": None})
    for key, rec in rlog.items():
        if key not in seen and rec.get("gone_on") is None:
            rec["gone_on"] = today
    _save(DATA / "reits_log.json", rlog, pretty=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    snap = collect()
    _save(SNAP / f"{snap['date']}.json", snap)
    _save(DATA / "latest.json", snap)
    update_logs(snap)
    print(f"[수집] {snap['collected_at']} 리츠 {len(snap['reits'])}건, "
          f"접수 {len(snap['filings'])}건, 상장 {len(snap['listed'])}건")


if __name__ == "__main__":
    main()
