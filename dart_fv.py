"""사업보고서 주석에서 투자부동산 공정가치를 읽는다(감정가 기준 NAV 계산용).

읽는 방식(앞에서부터 처음 맞는 값을 쓴다)
1. 장부금액과 공정가치가 함께 있는 투자부동산 표: 표의 장부금액이 재무상태표 투자부동산과 0.2% 안에서 같을 때만 쓴다.
2. 공정가치 모형을 쓰는 리츠: 재무상태표의 투자부동산이 곧 공정가치다.
3. "당기말 현재 투자부동산의 공정가치는 OOO원" 문장.
4. 공정가치 서열체계 표의 '투자부동산' 줄. 없으면 바로 앞 문장이 투자부동산 서열체계를 말하는 표의 합계 줄(당기말 열).
어느 방식이든 공정가치가 장부금액의 0.5배에서 2배 사이일 때만 쓴다(열을 잘못 읽어 0이나 몇 배가 된 값을 거른다).
다른 방식도 값을 내면 장부금액의 0.5% 안에서 같은지 대조해 confirm에 적는다.
감정 시점 신호도 남긴다. 표 방식이면 같은 표의 전기말 공정가치(fv_prior)를 적고, 어느 방식이든 주석이 '최근 평가기준일 대비
변동이 작아 공정가치 평가를 다시 하지 않았다'고 적었으면 not_reappraised와 그 문장의 평가기준일(appraisal_dates)을 적는다.
그런 문장 없이 전기말 공정가치가 당기와 같으면, 표 바로 뒤 주석에서 '평가기준일'이 든 문장의 날짜를 appraisal_dates에 적는다.
사업보고서 본문 파일에는 연결과 별도 주석이 함께 있어, 재무제표 기준에 맞는 주석 구역만 읽는다.
"""
import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from dart import DATA, api_key, read
from dart_notes import CELL, ROW, TABLE, UNITS, clean, document, grid

OUTPUT = DATA / "fv.json"
UNIT = re.compile(r"단위\s*[:：]?\s*(백만원|천원|억원|원)")
FINANCIAL = re.compile(r"현금및현금성자산|차입금|금융자산|금융부채|임대보증금")
FV_MODEL = re.compile(r"최초\s*인식\s*(?:시점\s*)?(?:이후|후)에?(?:는)?\s*공정가치로\s*측정|공정가치\s*모형을?\s*(?:적용|선택|사용)")
SENTENCE = re.compile(r"(?:당기말|보고기간\s*말|보고기간\s*종료일)\s*현재\s*(?:연결회사의\s*|회사의\s*|당사의\s*)?"
                      r"투자부동산의?\s*공정가치는\s*([\d,]+)\s*(백만원|천원|억원|원)")
NEGATION = re.compile(r"않|원가\s*모형|관계기업|공동기업")
TOTAL = re.compile(r"^(합계|총계|계)|합계$")
TOTAL_HEAD = re.compile(r"(?:^|\s)(?:합\s*계|총\s*계|계)\s*$")
SECTION = re.compile(r'<TITLE[^>]*AASSOCNOTE="D-0-[^"]*"[^>]*>(.*?)</TITLE>', re.S)
NO_REAPPRAISAL = re.compile(r"(?:공정가치\s*평가|감정\s*평가)를?\s*(?:재수행|재실시|다시\s*(?:수행|실시))하지\s*(?:아니|않)")
DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?")


def num(cell):
    c = (cell or "").strip().replace(",", "")
    if c in ("", "-"):
        return None
    negative = c.startswith("(") and c.endswith(")")
    c = c.strip("()")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", c):
        return None
    return -float(c) if negative else float(c)


def fair_value_model(text):
    """투자부동산을 말하는 같은 문장에 '최초 인식 후 공정가치로 측정' 또는 '공정가치 모형 적용'이 있는지.
    '선택하지 않고'처럼 부정하거나 원가모형, 관계기업, 공동기업을 함께 말하는 문장은 뺀다."""
    for m in FV_MODEL.finditer(text):
        start = max(text.rfind("다.", 0, m.start()), text.rfind(". ", 0, m.start())) + 1
        stop = text.find("다.", m.end())
        sentence = text[start:stop + 2 if 0 <= stop - m.end() < 300 else m.end() + 80]
        head = text[start:m.end()]
        if "투자부동산" in head and "금융" not in head and not NEGATION.search(sentence):
            return True
    return False


def header(rows):
    h = 0
    while h < len(rows) and not any(num(c) is not None for c in rows[h][1:]):
        h += 1
    width = max(len(r) for r in rows)
    return h, [" ".join(rows[i][j] for i in range(h) if j < len(rows[i])) for j in range(width)]


def unit_of(heads, before):
    """표 머리의 단위, 없으면 표 바로 앞(300자)에 나온 마지막 단위."""
    m = UNIT.search(" ".join(heads))
    if m:
        return UNITS[m[1]]
    found = UNIT.findall(before[-300:])
    return UNITS[found[-1]] if found else None


def rowspan_copies(raw):
    """grid()가 rowspan 칸을 아래 줄마다 복제한 자리(줄, 열). 여러 줄을 더할 때 이 자리는 빼서 한 번만 센다."""
    occupied, copies = set(), set()
    for i, tr in enumerate(ROW.findall(raw)):
        col = 0
        for _tag, attrs, _value in CELL.findall(tr):
            while (i, col) in occupied:
                col += 1
            spans = dict((k.lower(), int(v)) for k, v in re.findall(
                r'''(rowspan|colspan)\s*=\s*["']?(\d+)''', attrs, re.I))
            for j in range(col, col + spans.get("colspan", 1)):
                for n in range(i, i + spans.get("rowspan", 1)):
                    occupied.add((n, j))
                    if n > i:
                        copies.add((n, j))
            col += spans.get("colspan", 1)
    return copies


def book_fv_table(xml, book):
    for index, m in enumerate(TABLE.finditer(xml), 1):
        rows = grid(m[0])
        if len(rows) < 2:
            continue
        h, heads = header(rows)
        if not h or h >= len(rows) or not any("장부" in x for x in heads) or not any("공정가치" in x for x in heads):
            continue
        before = clean(xml[max(0, m.start() - 2500):m.start()])
        if FINANCIAL.search(" ".join(" ".join(r) for r in rows[h:])) or "투자부동산" not in before[-1200:]:
            continue
        # 금액이 든 열만 고른다. 공정가치 서열체계 수준처럼 글자나 0만 든 열, 장부금액과 같은 열은 공정가치 열이 아니다
        amount = lambda j: any(num(r[j]) for r in rows[h:] if j < len(r))
        book_j = next((j for j in range(1, len(heads)) if "장부" in heads[j] and "전기" not in heads[j] and amount(j)), None)
        fv_j = next((j for j in range(1, len(heads)) if "공정가치" in heads[j] and "전기" not in heads[j]
                     and "수준" not in heads[j] and "서열" not in heads[j] and j != book_j and amount(j)), None)
        # 같은 표에 전기말 공정가치 열이 있으면 함께 읽는다(당기와 같으면 감정평가를 새로 하지 않았을 수 있다)
        prior_j = next((j for j in range(1, len(heads)) if "공정가치" in heads[j] and "전기" in heads[j] and "장부" not in heads[j]
                        and "수준" not in heads[j] and "서열" not in heads[j] and amount(j)), None)
        unit = unit_of(heads, before)
        if book_j is None or fv_j is None or not unit:
            continue
        copies = rowspan_copies(m[0])
        data = [(i, r) for i, r in enumerate(rows) if i >= h and len(r) > max(book_j, fv_j)]
        label = lambda r: "".join(r[:book_j]).replace(" ", "")
        totals = [(i, r) for i, r in data if TOTAL.search(label(r))]
        body = [(i, r) for i, r in data if not TOTAL.search(label(r)) and "소계" not in label(r) and num(r[book_j]) is not None]
        # 합계 줄을 앞에서부터 하나씩 대 보고(당기 블록 아래 전기 블록이 붙은 표는 앞 합계가 당기다), 맞는 합계가 없으면
        # 본문 줄을 더한다. 본문을 더할 때 여러 줄에 걸친 칸(토지와 건물에 감정가 하나)은 한 번만 센다
        for group, skip in [([t], set()) for t in totals] + ([(body, copies)] if body else []):
            total = lambda j: round(sum(num(r[j]) or 0 for i, r in group if (i, j) not in skip and j < len(r)) * unit)
            table_book = total(book_j)
            if abs(table_book - book) <= 0.002 * book:
                return dict(table=index, table_book=table_book, fv=total(fv_j), end_pos=m.end(),
                            fv_prior=(total(prior_j) or None) if prior_j is not None else None)
    return None


def sentence_fv(text):
    m = SENTENCE.search(text)
    return round(float(m[1].replace(",", "")) * UNITS[m[2]]) if m else None


def iso_dates(text, end):
    """글 속 'YYYY년 M월 D일'(일이 없으면 'YYYY년 M월')을 결산일 전 것만 골라 정렬한다."""
    found = {f"{y}-{int(mo):02d}" + (f"-{int(dd):02d}" if dd else "") for y, mo, dd in DATE.findall(text)}
    return sorted(d for d in found if d < end)


def cited_dates(text, end):
    """'평가기준일'이 든 문장의 날짜. 그 문장이나 바로 앞 문장이 투자부동산을 말하고, 유형자산 문장이 아닐 때만 센다."""
    found, prev = [], ""
    for s in re.split(r"(?<=다\.)", text):
        if "평가기준일" in s and "유형자산" not in s and "투자부동산" in prev + s:
            found += iso_dates(s, end)
        prev = s
    return sorted(set(found))


def reappraisal_skipped(text, end):
    """주석이 '최근 평가기준일 대비 공정가치 변동이 작아 평가를 다시 하지 않았다'고 적었으면 그 문장의 결산일 전 날짜를 돌려준다.
    문장에 날짜가 없으면 바로 앞 문장이 평가기준일을 말할 때 그 날짜를 쓴다. 그런 문장이 없으면 None."""
    for m in NO_REAPPRAISAL.finditer(text):
        p = text.rfind("다.", 0, m.start())
        start = p + 2 if p >= 0 else 0
        q = text.rfind("다.", 0, p) if p > 0 else -1
        prev = text[q + 2 if q >= 0 else 0:start] if p >= 0 else ""
        sentence = text[start:m.end()]
        if "투자부동산" not in prev + sentence or "유형자산" in sentence:
            continue
        pick = sentence if DATE.search(sentence) else (prev if "평가기준일" in prev else "")
        return iso_dates(pick, end)
    return None


def amount_in(row, heads):
    """서열체계 표 한 줄의 공정가치 금액. 합계 열, 당기 열, 금액이나 공정가치 열 순서로 찾고,
    그런 열이 없으면 100 넘는 숫자가 하나뿐일 때만 그 값을 쓴다(수준별 일부 금액이나 전기 값을 고르지 않게)."""
    tests = (lambda x: TOTAL_HEAD.search(x) and "전기" not in x,
             lambda x: "당기" in x and "전기" not in x,
             lambda x: ("금액" in x or "공정가치" in x) and not re.search("수준|서열|전기", x))
    for test in tests:
        for j in range(1, min(len(row), len(heads))):
            v = num(row[j]) if test(heads[j]) else None
            if v is not None and v > 100:
                return v
    vals = [v for v in (num(c) for c in row[1:]) if v is not None and v > 100]
    return vals[0] if len(vals) == 1 else None


def hierarchy_fv(xml):
    tables = []
    for index, m in enumerate(TABLE.finditer(xml), 1):
        rows = grid(m[0])
        if len(rows) < 2:
            continue
        h, heads = header(rows)
        if h and h < len(rows):
            tables.append((index, rows, h, heads, clean(xml[max(0, m.start() - 1500):m.start()])))
    # 1차: 머리에 수준이나 서열이 있는 표의 '투자부동산' 줄
    for index, rows, h, heads, before in tables:
        joined = " ".join(heads)
        unit = unit_of(heads, before)
        if not unit or not ("수준" in joined or "서열" in joined) or "장부" in joined:
            continue
        for r in rows[h:]:
            v = amount_in(r, heads) if r and r[0].replace(" ", "") == "투자부동산" else None
            if v:
                return dict(table=index, fv=round(v * unit))
    # 2차: 바로 앞 문장이 투자부동산 서열체계를 말하고, 당기말 열이 전기말 열보다 앞이며, 금융상품 줄이 없는 표의 합계 줄
    for index, rows, h, heads, before in tables:
        joined, near = " ".join(heads), before[-300:]
        unit = unit_of(heads, before)
        if not unit or "장부" in joined or not ("서열체계" in near and "투자부동산" in near):
            continue
        if FINANCIAL.search(" ".join(" ".join(r) for r in rows[h:])):
            continue
        cur_j = next((j for j, x in enumerate(heads) if "당기" in x), None)
        prev_j = next((j for j, x in enumerate(heads) if "전기" in x), None)
        if cur_j is None or (prev_j is not None and prev_j < cur_j):
            continue
        totals = [r for r in rows[h:] if r and r[0].replace(" ", "") in ("합계", "총계", "계") and len(r) > cur_j]
        v = num(totals[-1][cur_j]) if totals else None
        if v and v > 100:
            return dict(table=index, fv=round(v * unit))
    return None


def notes_section(xml, basis):
    """사업보고서 본문 파일에는 연결과 별도 주석이 함께 있다. 재무제표 기준에 맞는 주석 구역만 돌려준다(못 찾으면 전체)."""
    marks = [(m.start(), clean(m[1])) for m in SECTION.finditer(xml)]
    want = re.compile(r"^\d+\.\s*연결재무제표\s*주석" if basis == "CFS" else r"^\d+\.\s*재무제표\s*주석")
    for n, (pos, title) in enumerate(marks):
        if want.search(title):
            return xml[pos:marks[n + 1][0] if n + 1 < len(marks) else len(xml)]
    return xml


def extract(r, doc, xml):
    cur = r["current"]
    book = cur.get("investment_property")
    base = dict(name=r["name"], rcept_no=r["rcept_no"], basis=r["basis"], end=cur["end"], doc=doc, book=book,
                fv=None, method=None, table=None, confirm=[], fv_prior=None, not_reappraised=False, appraisal_dates=[])
    if not book:
        return dict(base, note="재무상태표에 투자부동산 없음")
    if doc == r["rcept_no"] + ".xml":
        xml = notes_section(xml, r["basis"])
    text = clean(xml)
    skipped = reappraisal_skipped(text, cur["end"])
    base.update(not_reappraised=skipped is not None, appraisal_dates=skipped or [])
    found = []  # (방식, 공정가치, 표 번호, 더 남길 항목)
    table_hit = book_fv_table(xml, book)
    if table_hit:
        found.append(("장부금액과 공정가치 표", table_hit["fv"], table_hit["table"],
                      dict(table_book=table_hit["table_book"], fv_prior=table_hit["fv_prior"])))
    if fair_value_model(text):
        found.append(("공정가치 모형", book, None, {}))
    if sentence_fv(text):
        found.append(("공정가치 문장", sentence_fv(text), None, {}))
    hit = hierarchy_fv(xml)
    if hit:
        found.append(("공정가치 서열체계 표", hit["fv"], hit["table"], {}))
    usable = [x for x in found if 0.5 <= x[1] / book <= 2]
    if not usable:
        return dict(base, note="투자부동산 공정가치를 읽지 못함" + (
            f"(읽은 값이 장부금액의 0.5~2배 밖: {', '.join(x[0] for x in found)})" if found else ""))
    method, fv, table, extra = usable[0]
    note = {"장부금액과 공정가치 표": f"표 장부금액 {extra.get('table_book', 0):,}원, 재무상태표와 차이 {extra.get('table_book', 0) - book:,}원",
            "공정가치 모형": "재무상태표의 투자부동산이 공정가치"}.get(method, "장부금액과 대조할 수 없는 방식이라 장부금액의 0.5~2배 범위만 사용")
    dates = base["appraisal_dates"]
    if method == "장부금액과 공정가치 표" and not base["not_reappraised"] and extra.get("fv_prior") == fv:
        # 공정가치가 전기말 그대로면 표 바로 뒤 주석이 적은 평가기준일이 곧 지금 감정가의 기준일이다
        dates = cited_dates(clean(xml[table_hit["end_pos"]:table_hit["end_pos"] + 8000])[:2500], cur["end"])
    return dict(base, fv=fv, method=method, table=table, **extra, note=note, appraisal_dates=dates,
                confirm=[x[0] for x in usable[1:] if abs(x[1] - fv) <= 0.005 * book])


def update(snap=None, folder=None, force=False):
    source = read(DATA / "dart.json")
    stored = read(OUTPUT) if OUTPUT.exists() else {}
    due = [code for code, r in source.items() if folder or force or
           tuple(stored.get(code, {}).get(k) for k in ("rcept_no", "basis", "end", "book"))
           != (r["rcept_no"], r["basis"], r["current"]["end"], r["current"].get("investment_property"))]
    if not due:
        return
    key = None if folder else api_key()
    if not folder and not key:
        print("DART 인증키가 없어 공정가치 수집을 건너뜁니다.")
        return
    changed = 0
    for n, code in enumerate(due):
        try:
            if n and not folder:
                time.sleep(1)
            r = source[code]
            result = extract(r, *document(code, r, folder, key))
            payload = json.dumps(result, ensure_ascii=False)
            if (key and key in payload) or re.search(r"[0-9a-fA-F]{40}", payload):
                raise ValueError("인증정보 확인 필요")
            stored[code] = result
            changed += 1
        except Exception as exc:
            print(code, type(exc).__name__)
    if changed:
        with tempfile.NamedTemporaryFile(dir=DATA, mode="w", encoding="utf-8", delete=False) as f:
            json.dump(stored, f, ensure_ascii=False, indent=2)
        Path(f.name).replace(OUTPUT)
    print(f"DART 공정가치 {len(due)}개 종목 중 {changed}개 종목 갱신")


def explain():
    for code, r in (read(OUTPUT) if OUTPUT.exists() else {}).items():
        book, fv = r.get("book"), r.get("fv")
        gap = f"차익 {(fv - book) / 1e8:,.0f}억원" if book and fv else ""
        same = f" | 대조 일치: {', '.join(r['confirm'])}" if r.get("confirm") else ""
        age = (f" | 주석에 평가 생략 {', '.join(r.get('appraisal_dates') or [])}" if r.get("not_reappraised")
               else " | 전기말 공정가치와 같음" if fv and r.get("fv_prior") == fv else "")
        print(f"{r['name']} ({code}) | {r['end']} | {r.get('method') or '없음'} | 장부 {book or 0:,}원 | 공정가치 {fv or 0:,}원 {gap} | {r['note']}{same}{age}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    if args.explain:
        explain()
    else:
        update(folder=args.from_dir, force=args.force)
