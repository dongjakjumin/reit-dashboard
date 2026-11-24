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
NEGATION = re.compile(r"원가\s*모형|관계기업|공동기업")
DEPRECIATION = re.compile(r"(?:감가)?상각(?:비)?(?:을|를|은|는)?\s*(?:인식|수행|계상)?\s*하지\s*(?:않|아니)")
# 절 끝: '~하며', '~하고'(진행형 '하고 있'은 빼고), '~으나', '~지만', 쉼표, 문장 끝
CLAUSE_END = re.compile(r"(?:하|되|않|있|이|았|었|였)고(?!\s*있)|며|으나|지만|[,，]|다\.")
TOTAL = re.compile(r"^(합계|총계|계)|합계$")
TOTAL_HEAD = re.compile(r"(?:^|\s)(?:합\s*계|총\s*계|계)\s*$")
SECTION = re.compile(r'<TITLE[^>]*AASSOCNOTE="D-0-[^"]*"[^>]*>(.*?)</TITLE>', re.S)
NO_REAPPRAISAL = re.compile(r"(?:공정가치\s*평가|감정\s*평가)를?\s*(?:재수행|재실시|다시\s*(?:수행|실시))하지\s*"
                            r"(?:아니하였|아니했|않았|않고|아니하고)")
PAST_END = re.compile(r"(?:였|했|았|었)(?:습니다|다)\.\s*$|(?:였|했|았|었)음\s*$")
# 현재 진행형('재수행하지 않고 ~ 감정평가액을 공정가치로 사용하고 있습니다')은 받지 않는다. 받게 해 보니(7926a39) 7차 검토에서
# 감정 주기 정책, 일부 자산만 평가하지 않은 경우, 전기말 비교 설명, 관계기업 자산 문장 등 오탐 12종이 나와 미탐으로 남긴다
PRIOR_PERIOD = re.compile(r"전기(?:말)?\s*(?:에는|에도|중에는)")
# [review5] 문장 어디든 전기 서술('전기에는', '전기 중', '전기의 경우')이 있으면 일치 절이 '당기'를 말할 때만 이번 결산의 사실로 본다
# ('전기 중 취득한 투자부동산'처럼 자산을 꾸미는 '전기 중'은 전기 서술로 보지 않는다)
PRIOR_ANY = re.compile(r"전기(?:말)?\s*(?:에는|에도|의\s*경우|중(?!\s*(?:에\s*)?(?:취득|처분|매각|대체|신규|증가|감소|완공|준공|편입|발생)))")
# [review5] '재수행하지 않았을 경우', '아니하였다면', '않았던'은 가정이나 다른 기간 서술
HYPOTHETICAL = re.compile(r"\s*(?:을|던|다면|더라면)")
# [review5] '투자부동산' 표지를 붙이지 않을 표: 다른 자산, 부채, 다른 회사 줄이 함께 있는 표
OTHER_ASSET = re.compile(r"금융자산|금융상품|금융부채|차입금|임대보증금|현금|수익증권|지분증권|관계기업|공동기업|유형자산|사용권자산")
COST_MODEL = re.compile(r"원가\s*모형")
# 뒤 문장이 이번 결산에 평가를 다시 했다고 적는 말('당기에는 ~ 감정평가를 다시 수행하였습니다'). 부정문과 신규 취득 자산 이야기는 뺀다
# [review6] 수행한 일이 감정평가(공정가치 평가) 자체여야 한다. '내부 검토를 수행하였습니다', '민감도 분석을 수행하였습니다',
# '평가기관이 수행하였던'은 이번 결산에 평가를 다시 했다는 말이 아니다
CURRENT_REDONE = re.compile(r"당기(?:말|에는|에|중|중에)?.{0,200}?(?:감정\s*평가|공정가치\s*평가|재평가)를?\s*"
                            r"(?:다시\s*|새로\s*|재)?(?:수행|실시)(?:하였|했)(?!던)")
# 문장 끝: '~다.'와 마침표 없는 각주 끝('~하였음', '~없음')
SENT_END = re.compile(r"다\.|(?:였|했|았|었|없)음(?=[\s.,]|$)")
# 날짜 나열: 'A 및 B', 'A, B', '지배기업은 A, 종속기업은 B'의 이음말과 날짜(DATE에서 앞쪽 경계 조건을 뺀 본체)
DATE_BODY = r"\d{4}\s*(?:년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?|[.\-]\s*\d{1,2}\s*[.\-]\s*\d{1,2})"
# [review8] 다른 자산을 이번 결산에 감정했다는 말('당기말(…)을 평가기준일로 감정평가를 실시하였', '결산월 날짜를 평가기준일로 ~ 실시하였').
# '당기말'이나 날짜가 평가기준일 자리에 바로 붙을 때만 본다('당기말 현재 ~', '매 당기말을'은 아님)
AT_END_APPRAISED = re.compile(r"(?<!매\s)(?<!매)(당기말|" + DATE_BODY + r")\s*(?:\([^)]*\))?\s*[을를]?\s*평가\s*기준일로?\s*"
                              r"(?:하여\s*)?(?:감정\s*평가|공정가치\s*평가)를?\s*(?:실시|수행)(?:하였|했)")
LIST_GAP = r"\s*(?:및|와|과|,|，|、|그리고)\s*(?:[가-힣A-Za-z0-9()\s]{1,15}?(?:은|는)\s*)?"
# 표 안에서도 남길 칸: 문장이거나 평가기준일, 재평가 생략을 말하는 칸('(*)' 옆의 각주 문장 등)
PROSE_CELL = re.compile(r"다\.|평가\s*기준일|재수행|재실시|다시\s*(?:수행|실시)")
DATE = re.compile(r"(?<![\d,.])(\d{4})\s*(?:년\s*(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?|[.\-]\s*(\d{1,2})\s*[.\-]\s*(\d{1,2})(?![\d,]))")


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
    구절 앞에 원가모형, 관계기업, 공동기업이 나오거나, 구절 뒤 첫 절('~하며', '~하고', '~으나', 쉼표까지)에
    '않', '아니', '수 있'이 있으면(부정이나 선택 가능 서술) 뺀다. '감가상각하지 않습니다' 같은 말은 부정으로 보지 않는다."""
    for m in FV_MODEL.finditer(text):
        start = max(text.rfind("다.", 0, m.start()), text.rfind(". ", 0, m.start())) + 1
        head = text[start:m.end()]
        stop = text.find("다.", m.end())
        sentence = text[start:stop + 2 if 0 <= stop - m.end() < 300 else m.end() + 80]
        b = CLAUSE_END.search(text, m.end(), m.end() + 150)
        clause = DEPRECIATION.sub("", text[m.end():b.end() if b else m.end() + 80])
        if ("투자부동산" in head and "금융" not in head and not NEGATION.search(head) and not COST_MODEL.search(sentence)
                and not re.search(r"않|아니|수\s*(?:도\s*)?있", clause)):
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
        blank = False
        for group, skip in [([t], set()) for t in totals] + ([(body, copies)] if body else []):
            if blank and group is not body:
                continue
            total = lambda j: round(sum(num(r[j]) or 0 for i, r in group if (i, j) not in skip and j < len(r)) * unit)
            table_book = total(book_j)
            if abs(table_book - book) <= 0.002 * book:
                if total(fv_j) > 0:
                    return dict(table=index, table_book=table_book, fv=total(fv_j), end_pos=m.end(),
                                fv_prior=(total(prior_j) or None) if prior_j is not None else None)
                # 장부금액이 맞는 합계 줄의 공정가치 칸이 비었으면('-') 다른 합계 줄(흔히 전기 블록)은 보지 않고 본문 합산만 본다
                blank = True
    return None


def sentence_fv(text):
    m = SENTENCE.search(text)
    return round(float(m[1].replace(",", "")) * UNITS[m[2]]) if m else None


def iso_dates(text):
    """글 속 날짜('YYYY년 M월 D일', 일 없는 'YYYY년 M월', 'YYYY.MM.DD', 'YYYY-MM-DD')를 YYYY-MM(-DD)로 바꿔 정렬한다."""
    found = set()
    for m in DATE.finditer(text):
        y, mo, dd = int(m[1]), int(m[2] or m[4]), m[3] or m[5]
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and (not dd or 1 <= int(dd) <= 31):
            found.add(f"{y}-{mo:02d}" + (f"-{int(dd):02d}" if dd else ""))
    return sorted(found)


def before_end(dates, end):
    """결산월 전 날짜만(일 없는 연월도 월 단위로 견준다)."""
    return [d for d in dates if d[:7] < end[:7]]


def prose(xml):
    """표를 지운 주석 글. 표 안에서도 문장이 든 칸('(*)' 옆의 각주 문장, 글상자)은 남기고 숫자와 날짜 머리 칸은 지운다.
    지운 표에 '투자부동산'이 있으면 그 말만 남겨, 표 뒤 각주가 투자부동산 이야기인지 알 수 있게 한다."""
    def keep(m):
        # [review5] 본문 파일(XBRL)의 전기 표(칸이 모두 ACONTEXT="PFY...")는 전기 결산의 글이라 남기지 않는다
        if set(re.findall(r'ACONTEXT="([CP])FY', m[0])) == {"P"}:
            return " "
        cells = [clean(v) for _tag, _attrs, v in CELL.findall(m[0])]
        texts = [c for c in cells if PROSE_CELL.search(c)]
        whole = clean(m[0])
        mark = "투자부동산" if ("투자부동산" in whole and not OTHER_ASSET.search(whole)
                               and not any("투자부동산" in c for c in texts)) else ""
        return f" {mark} {' '.join(texts)} "
    return clean(TABLE.sub(keep, xml))


def sentences(text):
    """'~다.'와 마침표 없는 각주 끝('~하였음')에서 문장을 나눈다."""
    out, pos = [], 0
    for m in SENT_END.finditer(text):
        out.append(text[pos:m.end()])
        pos = m.end()
    return out + ([text[pos:]] if pos < len(text) else [])


def attached_dates(text):
    """평가기준일에 바로 붙은 날짜만: 'DATE을 평가기준일로', '평가기준일(은|이|:) DATE', '평가기준일(DATE)'.
    'A 및 B을 평가기준일로', '평가기준일은 각각 A, B'처럼 나열된 날짜도 센다.
    '보고기간 종료일(2026년 6월 30일)'처럼 평가 시점이 아닌 날짜는 뺀다."""
    found = []
    for m in DATE.finditer(text):
        after = re.sub(rf"^(?:{LIST_GAP}{DATE_BODY})+", "", text[m.end():m.end() + 120])
        before = re.sub(rf"(?:{DATE_BODY}{LIST_GAP})+$", "", text[max(0, m.start() - 120):m.start()])
        if (re.match(r"\s*(?:현재|자)?\s*(?:을|를|로|이|가|의)?\s*(?:각각\s*|모두\s*)?평가\s*기준일|\s*자\s*(?:로\s*)?(?:감정\s*)?평가", after)
                or re.search(r"평가\s*기준일\s*(?:은|는|이|가|인|:|：)?\s*(?:각각\s*|모두\s*)?"
                             r"(?:(?:보고기간\s*(?:종료일|말)|당기말|결산일)\s*(?:인|이자)?\s*)?[(\[]?\s*$", before)):
            found += iso_dates(m[0])
    return found


def cited_dates(text):
    """'평가기준일'이 든 문장의 (날짜, 그 문장들, 평가기준일에 바로 붙은 날짜). 표를 지운 글에서 문장을 나누고,
    그 문장이나 바로 앞 문장이 투자부동산을 말하며 유형자산 문장이 아닐 때만 센다."""
    found, attached, prev, sents = [], [], "", []
    for s in sentences(text):
        if "평가기준일" in s and "유형자산" not in s and "투자부동산" in prev + s:
            found += iso_dates(s)
            attached += attached_dates(s)
            sents.append(s.strip())
        prev = s
    return sorted(set(found)), " ".join(sents), sorted(set(attached))


def reappraisal_skipped(text, end):
    """주석이 '최근 평가기준일 대비 공정가치 변동이 작아 평가를 다시 하지 않았다'고 적었으면
    (결산월 전 날짜 목록, 근거 글)을 돌려준다. 근거 글은 그 문장과 바로 앞 문장이다.
    문장에 날짜가 없으면 바로 앞 문장이 평가기준일을 말할 때 그 날짜를 쓴다. 그런 문장이 없으면 None."""
    sents = sentences(text)
    for i, sentence in enumerate(sents):
        m = NO_REAPPRAISAL.search(sentence)
        if not m:
            continue
        # 앞 문장은 끝 800자만 본다. 문장이 1,500자를 넘으면 문장 나누기가 어긋난 글이라 쓰지 않는다
        prev = sents[i - 1][-800:] if i else ""
        # 일치 구절이 든 절('~하였으나,', '~하며,' 뒤)에 전기 서술('전기에는 ~')이 있으면 이번 결산의 사실이 아니다
        cut = max((x.end() for x in re.finditer(r"(?:으나|하나|지만|며|고)\s*[,，]", sentence[:m.start()])), default=0)
        # '아니할 수 있습니다' 같은 회계정책 조건문은 과거형이 아니라 걸린다. '~하지 않고'는 문장 끝이 과거형이어야 한다
        past = not m[0].endswith("고") or PAST_END.search(sentence.strip())
        if (len(sentence) > 1500 or "투자부동산" not in prev + sentence or "유형자산" in sentence
                or PRIOR_PERIOD.search(sentence[cut:m.end()]) or not past
                or (PRIOR_ANY.search(sentence[:m.end()]) and "당기" not in sentence[cut:m.end()])
                or HYPOTHETICAL.match(sentence, m.end())):
            continue
        # [review7] 같은 문장 앞 절이 다른 자산을 이번 결산에 감정했다고 적으면('A빌딩은 당기말(2026년 6월 30일)을
        # 평가기준일로 감정평가를 실시하였으며, 신규 취득한 C빌딩은 재수행하지 아니하였습니다') 일부 자산 이야기라 신호를 내지 않는다.
        # [review8] '당기말'이나 결산월 날짜가 평가기준일 자리에 바로 붙을 때만 본다. 앞 절 어디에든 '당기말'이 있으면 거르던
        # 4179cf0 규칙은 '당기말 현재 투자부동산의 공정가치는 2025년 12월 31일을 평가기준일로 ~' 같은 진짜 생략 문장까지 지웠다
        head = sentence[:cut]
        if any(x[1] == "당기말" or any(d[:7] == end[:7] for d in iso_dates(x[1])) for x in AT_END_APPRAISED.finditer(head)):
            continue
        # 앞에 전기 서술이 있고 바로 뒤 문장이 당기에 다시 평가했다고 적으면 생략 문장은 전기 이야기다
        nxt = sents[i + 1][:400] if i + 1 < len(sents) else ""
        # [review6] 일치 절이 '당기에는 ~ 재수행하지 아니하였습니다'처럼 당기를 분명히 말하면 이 규칙을 쓰지 않는다.
        # 뒤 문장이 전기를 다시 말하면('전기 중 평가기관이 수행하였습니다') 당기 재평가 서술로 보지 않는다
        if ("당기" not in sentence[cut:m.end()] and PRIOR_ANY.search(prev + sentence[:m.start()])
                and CURRENT_REDONE.search(nxt) and not re.search(r"하지\s*(?:않|아니)|취득|신규|편입|전기", nxt)):
            continue
        pick = sentence if DATE.search(sentence) else (prev if "평가기준일" in prev else "")
        # 근거 글은 자르지 않는다(verify.py가 날짜마다 근거 글에 있는지 본다)
        return before_end(iso_dates(pick), end), (prev + sentence).strip()
    return None


def amount_in(row, heads):
    """서열체계 표 한 줄의 공정가치 금액. 합계 열, 수준별 열(합계 열 없이 당기 숫자 열이 모두 수준별이면 더함),
    당기 열, 금액이나 공정가치 열 순서로 찾고, 그런 열이 없으면 100 넘는 숫자가 하나뿐일 때만 그 값을 쓴다
    (수준별 일부 금액이나 전기 값을 고르지 않게)."""
    cols = [j for j in range(1, min(len(row), len(heads))) if num(row[j]) is not None]
    for j in cols:
        if TOTAL_HEAD.search(heads[j]) and "전기" not in heads[j] and num(row[j]) > 100:
            return num(row[j])
    current = [j for j in cols if "전기" not in heads[j]]
    levels = [re.search(r"수준\s*(III|II|I|[123ⅠⅡⅢ])", heads[j]) for j in current]
    if current and all(levels):
        # 같은 수준이 두 번 나오면 머리에 '전기'가 없는 전기 블록이 붙은 표라, 더하지 않고 비운다(로마 숫자 머리도 같은 수준으로 셈)
        level_no = {"I": "1", "II": "2", "III": "3", "Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3"}
        if len({level_no.get(x[1], x[1]) for x in levels}) < len(levels):
            return None
        if sum(num(row[j]) for j in current) > 100:
            return sum(num(row[j]) for j in current)
    tests = (lambda x: "당기" in x and "전기" not in x,
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
            if r and r[0].replace(" ", "") == "투자부동산":
                # 처음 만난 '투자부동산' 줄에서 멈춘다. 못 읽었다고 다음 표(흔히 전기 표)로 넘어가지 않는다
                v = amount_in(r, heads)
                return dict(table=index, fv=round(v * unit)) if v else None
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
    """사업보고서 본문 파일에는 연결과 별도 주석이 함께 있다. 재무제표 기준에 맞는 주석 구역만 돌려준다(못 찾으면 전체).
    본문 파일이면 fv.json의 표 번호(table)는 이 구역 안에서 센 순서다."""
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
                fv=None, method=None, table=None, confirm=[], fv_prior=None, not_reappraised=False, appraisal_dates=[],
                appraised_at_end=False, reappraisal_text="", appraisal_text="")
    if not book:
        return dict(base, note="재무상태표에 투자부동산 없음")
    if doc == r["rcept_no"] + ".xml":
        xml = notes_section(xml, r["basis"])
    text = clean(xml)
    # 표를 지운 글에서 문장을 찾는다(표 머리의 '전기말(2025년 12월 31일)' 같은 날짜가 섞이지 않게)
    skipped = reappraisal_skipped(prose(xml), cur["end"])
    base.update(not_reappraised=skipped is not None, appraisal_dates=skipped[0] if skipped else [],
                reappraisal_text=skipped[1] if skipped else "")
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
    dates, age = base["appraisal_dates"], {}
    if (base["not_reappraised"] and method == "장부금액과 공정가치 표" and extra.get("fv_prior") not in (None, fv)
            and PRIOR_ANY.search(base["reappraisal_text"])):
        # [review5] 근거 글이 전기를 말하고 표의 전기말 공정가치도 당기와 다르면 이번 결산에 다시 평가한 것이라 신호를 보류한다
        dates, age = [], dict(not_reappraised=False, reappraisal_text="")
    elif method == "장부금액과 공정가치 표" and not base["not_reappraised"] and extra.get("fv_prior") == fv:
        # 공정가치가 전기말 그대로면 표 바로 뒤 주석이 적은 평가기준일이 곧 지금 감정가의 기준일이다.
        # 평가기준일에 바로 붙은 날짜가 결산월이나 그 뒤면 결산일에 다시 평가한 것이라 '전기말과 같음' 신호를 쓰지 않는다
        seg = xml[table_hit["end_pos"]:table_hit["end_pos"] + 20000]
        opened, closed = seg.upper().rfind("<TABLE"), seg.upper().rfind("</TABLE")
        if opened > closed:  # 창 끝에서 잘린 표는 머리의 날짜가 섞이지 않게 뺀다
            seg = seg[:opened]
        cited, proof, attached = cited_dates(prose(seg)[:2500])
        at_end = any(d[:7] >= cur["end"][:7] for d in attached)
        dates = [] if at_end else before_end(cited, cur["end"])
        age = dict(appraised_at_end=at_end, appraisal_text=proof)
    return dict(base, fv=fv, method=method, table=table, **extra, **age, note=note, appraisal_dates=dates,
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
