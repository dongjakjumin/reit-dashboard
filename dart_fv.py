"""사업보고서 주석에서 투자부동산 공정가치를 읽는다(감정가 기준 NAV 계산용).

읽는 순서
1. 공정가치 모형을 쓰는 리츠: 재무상태표의 투자부동산이 곧 공정가치다.
2. 장부금액과 공정가치가 함께 있는 투자부동산 표: 표의 장부금액이 재무상태표 투자부동산과 0.2% 안에서 같을 때만 쓴다.
3. "당기말 현재 투자부동산의 공정가치는 OOO원" 문장.
4. 공정가치 서열체계 표의 '투자부동산' 줄. 없으면 바로 앞 문장이 투자부동산 서열체계를 말하는 표의 합계 줄(당기말 열).
3과 4는 장부금액과 대조할 수 없어, 공정가치가 장부금액의 0.5배에서 2배 사이일 때만 쓴다.
"""
import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from dart import DATA, api_key, read
from dart_notes import TABLE, UNITS, clean, document, grid

OUTPUT = DATA / "fv.json"
UNIT = re.compile(r"단위\s*[:：]?\s*(백만원|천원|억원|원)")
FINANCIAL = re.compile(r"현금및현금성자산|차입금|금융자산|금융부채|임대보증금")
FV_MODEL = re.compile(r"최초\s*인식\s*(?:시점\s*)?(?:이후|후)에?(?:는)?\s*공정가치로\s*측정|공정가치\s*모형을?\s*(?:적용|선택|사용)")
SENTENCE = re.compile(r"(?:당기말|보고기간\s*말|보고기간\s*종료일)\s*현재\s*(?:연결회사의\s*|회사의\s*|당사의\s*)?"
                      r"투자부동산의?\s*공정가치는\s*([\d,]+)\s*(백만원|천원|억원|원)")


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
    """투자부동산을 말하는 같은 문장에 '최초 인식 후 공정가치로 측정' 또는 '공정가치 모형 적용'이 있는지."""
    for m in FV_MODEL.finditer(text):
        start = max(text.rfind("다.", 0, m.start()), text.rfind(". ", 0, m.start())) + 1
        sentence = text[start:m.end()]
        if "투자부동산" in sentence and "금융" not in sentence:
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
        book_j = next((j for j in range(1, len(heads)) if "장부" in heads[j] and "전기" not in heads[j]), None)
        fv_j = next((j for j in range(1, len(heads)) if "공정가치" in heads[j] and "전기" not in heads[j]
                     and "수준" not in heads[j]), None)
        unit = unit_of(heads, before)
        if book_j is None or fv_j is None or not unit:
            continue
        data = [r for r in rows[h:] if len(r) > max(book_j, fv_j)]
        label = lambda r: "".join(r[:book_j]).replace(" ", "")
        totals = [r for r in data if re.match(r"^(합계|총계|계)", label(r)) or label(r).endswith("합계")]
        pick = [totals[-1]] if totals else [r for r in data if "소계" not in label(r) and num(r[book_j]) is not None]
        table_book = round(sum(num(r[book_j]) or 0 for r in pick) * unit)
        fv = round(sum(num(r[fv_j]) or 0 for r in pick) * unit)
        if pick and abs(table_book - book) <= 0.002 * book:
            return dict(table=index, table_book=table_book, fv=fv)
    return None


def sentence_fv(text):
    m = SENTENCE.search(text)
    return round(float(m[1].replace(",", "")) * UNITS[m[2]]) if m else None


def hierarchy_fv(xml):
    tables = []
    for index, m in enumerate(TABLE.finditer(xml), 1):
        rows = grid(m[0])
        if len(rows) < 2:
            continue
        h, heads = header(rows)
        if h and h < len(rows):
            tables.append((index, rows, h, heads, clean(xml[max(0, m.start() - 1500):m.start()])))
    first = lambda r: next((v for v in (num(c) for c in r[1:]) if v is not None and v > 100), None)
    # 1차: 머리에 수준이나 서열이 있는 표의 '투자부동산' 줄
    for index, rows, h, heads, before in tables:
        joined = " ".join(heads)
        unit = unit_of(heads, before)
        if not unit or not ("수준" in joined or "서열" in joined) or "장부" in joined:
            continue
        for r in rows[h:]:
            if r and r[0].replace(" ", "") == "투자부동산" and first(r):
                return dict(table=index, fv=round(first(r) * unit))
    # 2차: 바로 앞 문장이 투자부동산 서열체계를 말하고, 당기말 열이 전기말 열보다 앞인 표의 합계 줄
    for index, rows, h, heads, before in tables:
        joined, near = " ".join(heads), before[-300:]
        unit = unit_of(heads, before)
        if not unit or "장부" in joined or not ("서열체계" in near and "투자부동산" in near):
            continue
        cur_j = next((j for j, x in enumerate(heads) if "당기" in x), None)
        prev_j = next((j for j, x in enumerate(heads) if "전기" in x), None)
        if cur_j is None or (prev_j is not None and prev_j < cur_j):
            continue
        totals = [r for r in rows[h:] if r and r[0].replace(" ", "") in ("합계", "총계", "계")]
        if totals and first(totals[-1]):
            return dict(table=index, fv=round(first(totals[-1]) * unit))
    return None


def extract(r, doc, xml):
    cur = r["current"]
    book = cur.get("investment_property")
    base = dict(name=r["name"], rcept_no=r["rcept_no"], basis=r["basis"], end=cur["end"], doc=doc, book=book,
                fv=None, method=None, table=None)
    if not book:
        return dict(base, note="재무상태표에 투자부동산 없음")
    text = clean(xml)
    if fair_value_model(text):
        return dict(base, fv=book, method="공정가치 모형", note="재무상태표의 투자부동산이 공정가치")
    hit = book_fv_table(xml, book)
    if hit:
        return dict(base, fv=hit["fv"], method="장부금액과 공정가치 표", table=hit["table"], table_book=hit["table_book"],
                    note=f"표 장부금액 {hit['table_book']:,}원, 재무상태표와 차이 {hit['table_book'] - book:,}원")
    for method, found in (("공정가치 문장", sentence_fv(text)), ("공정가치 서열체계 표", hierarchy_fv(xml))):
        fv = found["fv"] if isinstance(found, dict) else found
        if fv and 0.5 <= fv / book <= 2:
            return dict(base, fv=fv, method=method, table=found.get("table") if isinstance(found, dict) else None,
                        note="장부금액과 대조할 수 없는 방식이라 장부금액의 0.5~2배 범위만 사용")
    return dict(base, note="투자부동산 공정가치를 읽지 못함")


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
        print(f"{r['name']} ({code}) | {r['end']} | {r.get('method') or '없음'} | 장부 {book or 0:,}원 | 공정가치 {fv or 0:,}원 {gap} | {r['note']}")


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
