"""수집과 집계가 같이 쓰는 글자 처리."""
from __future__ import annotations

import re

# 전화번호 모양: 02-123-4567, 010-1234-5678, 01012345678 등. 금액 같은 긴 숫자의 일부는 잡지 않는다.
PHONE_RE = r"(?<![\d.,])(?:0\d{1,2}[-.\s]\d{3,4}[-.\s]\d{4}|01[016789]\d{7,8})(?![\d,])"


PRIVATE_LABEL = "개인으로 보여 이름 생략"
_TWO_SYLLABLE_SURNAMES = ("남궁", "선우", "제갈", "황보", "독고", "사공", "서문")
_HANJA_ORG = re.compile(r"證券|銀行|生命|保險|信託|資産|資產|投資|會社|株式|商事|産業|建設|集團|公司|基金|金融|不動産|物産")


def mask_person(name: str | None) -> str | None:
    """최대주주 칸에 개인 이름으로 보이는 값이 있으면 이름을 지운다.

    회사 표기((주), 주식회사)가 없고, 공백과 끝의 '외 N인'을 떼고 남은 글자가 한글 2~3자이면 개인 이름으로 본다.
    4자는 두 글자 성으로 시작할 때만 개인 이름으로 본다(롯데쇼핑 같은 회사 이름을 지우지 않기 위해).
    """
    if not name:
        return name
    if re.search(r"\(주\)|㈜|주식회사|유한회사", name):
        return name  # 회사 표기가 붙어 있으면 회사로 본다
    # [review16] 띄어 쓴 직함과 '씨', '님'만 뗀다('케이씨씨'의 끝 '씨'는 떼지 않는다). '홍길동 사장 등'처럼 여럿 붙어도 뗀다
    name_w = name
    for _ in range(3):
        name_w = re.sub(r"\s+(?:대표이사|부회장|회장|부사장|사장|전무|상무|이사|대표|씨|님|등(?:\s*\d+\s*(?:인|명))?)\s*$", "", name_w)
    parts = [x for x in re.split(r"\s*(?:,|，|·|및)\s*", name_w) if x]
    if len(parts) >= 2 and all(mask_person(x) == PRIVATE_LABEL for x in parts):
        return PRIVATE_LABEL  # '김철수, 이영희', '김철수 및 이영희'
    core = re.sub(r"\s", "", name_w)
    core = re.sub(r"(?:\([^()]*\))+", "", core)
    core = re.sub(r"(?:외|및)?(?:특수관계인|특별관계자|관계인)?\d*(?:인|명)?$", "", core) if re.search(r"(?:외|및|특수관계인|특별관계자)", core) else core
    core = re.sub(r"외\d*(인|명)?$", "", core)
    core = re.sub(r"^개인", "", core)
    core = re.sub(r"(?:대표이사|대표|회장|사장|등)$", "", core)
    is_person = bool(re.fullmatch(r"[가-힣]{2,3}", core)) or (
        bool(re.fullmatch(r"[가-힣]{4}", core)) and core.startswith(_TWO_SYLLABLE_SURNAMES))
    # [review16] 한자는 기관 표지(證券, 銀行 등)가 없을 때만 개인 이름으로 본다
    is_person = is_person or (bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", core)) and not _HANJA_ORG.search(core))
    return PRIVATE_LABEL if is_person else name


# 공시 본문의 임원 선임, 사임 문구에서 사람 이름을 지운다. 임원 변경 공시는 본문을 저장하지 않는 원칙을
# '기타' 공시에 섞여 들어온 같은 내용에도 적용한다. 예: "사내이사 홍길동 선임", "(사임) 대표이사 홍길동, 이사 김철수",
# "사내이사 홍길동, 김철수 선임", "대표이사 홍길동 외 1인 선임", "이사 선임: 홍길동", "사내이사 홍길동 선임 및 김철수 사임"
OFFICER_LABEL = "(이름 생략)"
# [review6] '공동대표이사', '각자대표이사', '대표집행임원', '상근감사위원'처럼 앞에 붙는 말도 직함의 일부로 본다
_TITLES = (r"(?:(?:공동|각자|단독)?대표이사|사내이사|사외이사|기타\s?비상무이사|비상무이사|감독이사|법인이사|(?:대표)?집행임원"
           r"|(?:상근|비상근)?감사위원|상근감사|청산인|감사|이사)")
# [review6] 이름 앞뒤에 붙는 직함에는 '대표', 'CEO'도 넣고, 직함 뒤에는 조사만 올 수 있게 한다
# ('이사회', '감사보고서', '감사인'의 '이사', '감사'는 직함이 아니다)
_TITLE_X = rf"(?:{_TITLES}|대표|(?<![A-Za-z])(?:CEO|CFO|COO)(?![A-Za-z]))"
_TB = rf"{_TITLE_X}(?:으로|로|의|가|이|은|는|을|를|에|와|과)?(?![가-힣])"
# [review6] '중도퇴임', '신규 선임', '각 선임', '일신상의 이유로 2026.11.20.자로 사임', '11월 20일자 사임'처럼
# 이름과 동작 사이에 붙는 말(세 개까지)도 동작의 일부로 본다
_DAY = r"(?:\d{4}\s*[.\-/년]\s*)?\d{1,2}\s*[.\-/월]\s*\d{1,2}\s*(?:일|\.)?"
_LEAD = (rf"(?:(?:중도|자진|금일|금번|신규|추가|정식|각|일신상의?\s*(?:사유|이유)로|(?:개인|건강상의?)\s*(?:사정|사유|이유)(?:으로|로)"
         rf"|{_DAY}\s*(?:자로|자|부로|부터|부|에|로)?)\s*){{0,3}}")
_ACTION_WORDS = r"(?:재선임|선임|사임|해임|중임|연임|퇴임|취임|선출|임기\s?만료|임기\s?종료|임명|사퇴|사직|위촉|해촉)"
_PERSON_ACTIONS = rf"(?:{_LEAD}{_ACTION_WORDS})"
_ACTIONS = rf"(?:{_PERSON_ACTIONS}|변경\s?전|변경\s?후)"
_NOT_NAMES = {"보수", "선임", "사임", "해임", "중임", "연임", "퇴임", "취임", "재선임", "후보", "후보자", "전원", "변경",
              "승인", "신규", "선출", "등", "및", "각", "선임의", "사임의", "해임의", "임원", "후임", "신임", "이사회",
              "감사인", "위원", "위원장", "의장", "대표", "상근", "비상근", "추가", "일괄", "동시", "임기", "만료",
              "이름", "생략", "취소", "결의", "예정", "완료", "현재", "기존", "중도", "사유로", "사정으로", "일신상의",
              # [review6] 이유 말, 직함을 꾸미는 말, 값이 없다는 말
              "자진", "금일", "이유로", "일신상", "건강상", "건강상의", "개인", "당사", "동사", "회사", "리츠", "임시", "일시",
              "사외", "사내", "기타", "외부", "내부", "공동", "각자", "단독", "차기", "전임", "직전", "종전", "없음", "미정",
              "공석", "해당", "해당사항", "해당없음", "별도", "추후", "정관", "내정", "내정자", "직무", "대행", "직무대행",
              "본인", "사항", "주총", "임시주총", "정기주총", "주주총회", "안건", "공시", "관련", "회장", "사장", "부사장",
              "전무", "상무", "본부장", "겸직", "겸임", "겸무", "자산", "따른", "의한", "관한", "대한", "위한", "인한", "통한",
              "따라", "의해", "대해", "위해", "인해", "통해", "사이", "다수", "일부", "모두", "각각", "전체", "상호", "해당", "공동",
              "직원", "전문인력", "운용인력", "운용역", "과장", "부장", "차장", "대리", "팀장", "실장", "매니저", "주임", "사원", "관리인", "관재인", "수탁자", "수탁인", "대리인", "조사위원", "감정인", "청산인"}
_NOT_NAME_END = re.compile(r"(?:회사|(?:주관|수탁|운용|신탁|증권|보관|관리|대행|평가|시공|시행|자문|중개|용역|설계|감리|위탁|운영)사"
                           r"|신탁|증권|은행|법인|리츠|펀드|조합|위원회|이사회|총회|주총|대표|이사)$")
_NAME = r"[가-힣]{2,4}"
_SEP = r"(?:\s*(?:,|，|、|·|ㆍ|・|∙|/|및)\s*|(?<=[가-힣])(?:과|와)\s+)"
# 이름 목록. '홍길동, 사내이사 김철수'처럼 목록 안에 직함이 끼어도 잇는다(직함 뒤 띄어쓰기를 둬서 풀이가 하나뿐이게).
# 겹침 허용으로 찾으므로 한 목록은 20명까지만 이어(긴 글에서 자리마다 끝까지 훑지 않게) 시간을 글 길이에 비례하게 둔다
_LIST = rf"{_NAME}(?:{_SEP}(?:{_TITLES}\s+)?{_NAME}){{0,20}}"
# 쌍점 뒤 목록은 띄어쓰기로만 이은 이름('사임: 홍길동 김철수')도 잇는다
_LIST_WS = rf"{_NAME}(?:(?:{_SEP}|\s+)(?:{_TITLES}\s+)?{_NAME}){{0,20}}"
_END = (rf"(?=\s*(?:[,，()]|/\s*(?:변경|{_TITLE_X}|{_ACTIONS})|외\s*\d|등\s*\d|{_ACTIONS}|$)|\s+{_TITLE_X}|\s+-|\s*;|\s*\.(?!\d)"
        rf"|\s*(?:님|씨)(?![가-힣]))")
# [review6] 아래 목록 패턴은 모두 앞보기(겹침 허용)로 찾는다. 앞 일치가 '선임', '변경후' 같은 다음 동작 낱말이나 직함을
# 이름 목록으로 먹어 버려 뒤 이름('사임: 홍길동, 선임: 김철수'의 김철수)을 놓치지 않게 한다
# 직함 바로 뒤의 이름(여럿이면 쉼표, 가운뎃점, '및', '과/와'로 이어진 것까지). 앞뒤 경계로 '외부감사', '이사회'는 잡지 않는다.
_AFTER_TITLE = re.compile(rf"(?=(?<![가-힣]){_TB}\s*[:：]?\s*({_LIST}){_END})")
# "선임: 홍길동", "후보: 홍길동, 김철수", "성명: 홍길동"처럼 쌍점에 이어진 이름
_AFTER_COLON = re.compile(rf"(?=(?:{_ACTIONS}|후보자?|성명|임원)\s*(?:[:：]|[-–—](?!\d))\s*({_LIST_WS})(?![가-힣]))")
# [review6] "대표이사(홍길동) 사임", "사내이사 선임(홍길동, 김철수)", "감독이사 2인(홍길동, 김철수) 선임"처럼
# 직함이나 동작 바로 뒤 괄호 속 이름
_IN_PAREN = re.compile(rf"(?=(?<![가-힣])(?:{_TB}|{_ACTIONS})\s*(?:\d+\s*[인명]\s*)?[(（]\s*({_LIST})\s*[)）])")
# [review6] "대표이사 변경(종전 홍길동, 신규 김철수)"처럼 꼬리표 뒤에 오고 바로 쉼표, 괄호, 글 끝으로 닫히는 이름
_LABELED = re.compile(rf"(?<![가-힣])(?:종전|전임|신규|신임|후임)\s*[:：]?\s*({_NAME})(?=\s*(?:[,，/)）\]]|$))")
# "및 김철수 사임", "홍길동 일신상의 이유로 사임"처럼 직함 없이 동작 앞에 온 이름(숫자에 붙은 '20일자'는 빼려고 숫자 경계도 둔다)
_BEFORE_ACTION = re.compile(rf"(?<![가-힣\d])(?<!회사\s)(?<!법인\s)(?<!기관\s)(?<!관리사\s)(?<!운용사\s)(?<!신탁사\s)(?<!시공사\s)(?<!위탁사\s)(?<!수탁사\s)({_NAME})(?=\s*[(（]?\s*{_PERSON_ACTIONS})")
_STAFF = re.compile(rf"(?<![가-힣])(?:(?:자산|투자)\s?)?(?:운용\s?)?(?:전문인력|운용역|운용인력|직원|임원)\s*[:：]?\s*({_NAME})(?:\s+(?:과장|부장|차장|대리|팀장|실장|본부장|매니저|주임|사원))?\s*[(（]?\s*(?:퇴사|입사|교체)")
_STAFF2 = re.compile(rf"(?:(?<=및\s)|(?<=[,，]\s)|(?<![가-힣]))({_NAME})\s*(?:(?:과장|부장|차장|대리|팀장|실장|본부장|매니저|주임|사원)\s*)?[(（]?\s*(?:퇴사|입사)")
# [review13] 개인이 거래 당사자로 나오는 글: "최대주주 홍길동으로부터 차입", "대주: 홍길동", "원고: 홍길동"
_PARTY = re.compile(rf"(?<![가-힣])(?:최대주주|주요주주|대주|대여자|차주|매도인|매수인|원고|피고|채권자|채무자|담당자|전문인력|운용역|운용인력)"
                    rf"(?:인|은|는|가|이)?\s*[:：]?\s*['\"]?([가-힣]{{2,4}}?)(?=\s*(?:으로부터|로부터|에게|외\s*\d|씨|님|[,，/'\"]|[(（](?!\s*[주유]\s*[)）])|$|(?:은|는|이|가)\s)(?!\s*㈜)|\s+(?:과장|부장|차장|대리|팀장|실장|본부장|매니저|주임|사원)(?![가-힣]))")
# [review13] "대표이사를 홍길동으로 변경", "전문인력 변경: 홍길동 → 김철수"
_TO_CHANGE = re.compile(rf"(?<![가-힣]){_TITLE_X}(?:을|를|이|가)\s*([가-힣]{{2,4}}?)(?:\s*(?:씨|님))?(?:으로|로)\s*(?:변경|교체)")
# [review16] "대표이사 홍길동을 김철수로 교체"처럼 직함 뒤 두 이름
_SWAP = re.compile(rf"(?<![가-힣]){_TITLE_X}\s+([가-힣]{{2,4}}?)(?:을|를)\s*([가-힣]{{2,4}}?)(?:\s*(?:씨|님))?(?:으로|로)\s*(?:변경|교체)")
# [review6] "홍길동 대표이사 사임", "제1호 의안: 홍길동, 김철수 사내이사 선임의 건"처럼 이름이 직함 앞에 오는 어순.
# 글머리, 문장부호, 이음말('~으로', '~하고', '및', '금번') 바로 뒤에서 시작할 때만 본다('이사회 결의로 감사 선임'의 '결의로'는 안 잡음)
_CLAUSE_START = (r"(?:^|(?<=[-:：,，(（)\]·.;])|(?<=[-:：,，(（)\]·.;]\s)|(?<=및\s)|(?<=[로고며서에라여해후]\s)|(?<=금번\s)"
                 r"|(?<=이번\s))")
_BEFORE_TITLE = re.compile(rf"(?={_CLAUSE_START}({_LIST})\s+{_TB}\s*(?:(?:및|,|，)\s*{_NAME}\s+{_TB}\s*)*\(?\s*{_ACTIONS})")
# [review9] "(변경전) 홍길동 (변경후) 김철수"처럼 변경 꼬리표 괄호 바로 뒤의 이름
_TAGGED = re.compile(rf"(?<![가-힣])(?:{_TB}|임원)\s*(?:의\s*)?변경\s*[:：\-]?\s*\((?:변경\s?전|구|전임)\)\s*({_NAME})\s*[,，/]?\s*\((?:변경\s?후|신|후임)\)\s*({_NAME})(?![가-힣])")
# [review6] "대표이사로 선임된 홍길동"
_AFTER_ACTION = re.compile(rf"(?=(?<![가-힣]){_TB}\s*{_ACTION_WORDS}(?:된|한|하신|되신|되는|하는)\s+({_LIST})(?![가-힣]))")
# "신임 대표이사 김철수", "후임 감사: 김철수", "신규 대표이사 김철수 및 감사 이영희", "(전임 홍길동)"
_NEW_OFFICER = re.compile(
    rf"(?=(?:(?:신규|차기|종전|기존)\s*{_TB}|(?:신임|후임|전임)(?:\s*{_TB})?)\s*(?:(?:내정자|후보자?)\s*)?[:：]?\s*({_LIST})(?![가-힣]))")
# "대표이사 변경(홍길동→김철수)", "대표이사: 홍길동 -> 김철수", "대표이사 변경(2026.11.20): (구) 홍길동 → (신) 김철수",
# "대표이사 변경: 홍길동(사임) → 김철수(선임)", "임원 변경: 홍길동 → 김철수(대표이사)"
_ARROW = r"(?:→|->|=>|⇒|➔|➜|▶|►|⟶|⇨)"
_GAP = r"(?:[^.\n]|(?<=\d)\.)"
_TAG = r"(?:\((?:구|신|전|후|전임|후임|사임|선임|퇴임|취임|변경\s?전|변경\s?후)\)\s*)?"
_PAREN = r"(?:\s*\([^()]{0,10}\))?"
_ARROW_CHANGE = re.compile(
    rf"(?<![가-힣])(?:{_TB}|임원|(?:자산운용)?전문인력|운용역|운용인력){_GAP}{{0,30}}?(?<![가-힣]){_TAG}({_NAME})(?:\s*{_TITLE_X})?{_PAREN}\s*\(?\s*{_ARROW}"
    rf"\s*{_TAG}(?:{_TITLE_X}\s*[:：]?\s*)?({_NAME})(?![가-힣])")
# [review6] "대표이사 홍길동에서 김철수로 변경", "대표이사가 홍길동 대표에서 김철수 대표로 바뀜"
_FROM_TO = re.compile(
    rf"(?<![가-힣])(?:{_TB}|임원){_GAP}{{0,30}}?(?<![가-힣]){_TAG}({_NAME})(?:\s*{_TITLE_X})?{_PAREN}\s*(?:에서|으로부터|로부터)"
    rf"\s*{_TAG}({_NAME})(?:\s*{_TITLE_X})?{_PAREN}\s*(?:으로|로)(?![가-힣])")


_PARTY_COMPANY = re.compile(r"(?:은행|증권|보험|생명|화재|손보|캐피탈|금고|신협|농협|수협|공사|공단|기금|공제|물산|쇼핑|건설|산업|홀딩스|리츠|펀드|신탁|운용)$")


def _is_name(token: str) -> bool:
    # [review6] '이사회는', '당사의'처럼 조사가 붙은 말은 조사를 떼고도 본다
    base = re.sub(r"(?:은|는|이|가|을|를|의|에서|에|으로|로|와|과)$", "", token)
    if base != token and (base in _NOT_NAMES or re.fullmatch(_TITLE_X, base) or _NOT_NAME_END.search(base)):
        return False
    return (len(token) >= 2 and token not in _NOT_NAMES and not re.fullmatch(_TITLE_X, token)
            and not re.fullmatch(_ACTIONS, token)  # '임기만료', '변경후', '중도퇴임'
            and not re.match(r"(?:재선임|선임|사임|해임|중임|퇴임|취임|선출|사퇴|사직|위촉|해촉|변경|퇴사|입사|교체)", token)  # '선임된'
            and not re.search(r"(?:하는|되는|있는|없는|하던|했던|하고|하며|되며|으며|이며|이고|하여|되어|해서|에서|에게|부터|까지)$",
                              token)
            and not _NOT_NAME_END.search(token))


def _officer_spans(text: str | None) -> list[tuple[int, int]]:
    """임원 문구 안에서 이름으로 보이는 곳의 (시작, 끝). 직함(또는 '임원', '대표')과 함께 동작 낱말, '신임/후임',
    화살표, '~에서 ~로', '후보:' 같은 말이 있는 글만 본다."""
    # [review6] 직함이 없어도 '홍길동 선임, 김철수 사임'처럼 동작 낱말이 있으면 본다
    if not text or not re.search(rf"{_TITLE_X}|임원|{_ACTION_WORDS}|최대주주|주요주주|대주|대여자|차주|매도인|매수인|원고|피고|채권자|채무자|담당자|전문인력|운용역|운용인력|퇴사|입사|교체", text):
        return []
    patterns = []
    if re.search(_ACTIONS, text):
        patterns += [_AFTER_TITLE, _AFTER_COLON, _BEFORE_ACTION, _BEFORE_TITLE, _AFTER_ACTION, _IN_PAREN]
    elif re.search(r"후보|성명", text):
        patterns.append(_AFTER_COLON)
    if re.search(r"신임|후임|전임|종전|기존|신규|차기", text):
        patterns.append(_NEW_OFFICER)
        if re.search(r"신임|후임|전임|종전", text):
            patterns.append(_AFTER_TITLE)  # '대표이사 김철수 (전임 홍길동)'
        # 꼬리표 방식은 경계가 있는 직함(또는 '임원')과 '변경'이 함께 있는 글에서만 본다('기존 자산, 신규 자산'을 잡지 않게)
        if "변경" in text and re.search(rf"(?<![가-힣]){_TB}|임원", text):
            patterns.append(_LABELED)
    if re.search(r"\(", text) and not re.search(_ACTIONS, text):
        patterns.append(_IN_PAREN)
    if re.search(r"\((?:변경\s?전|변경\s?후|구|신|전임|후임)\)", text):
        patterns.append(_TAGGED)
    if re.search(_ARROW, text):
        patterns.append(_ARROW_CHANGE)
    if re.search(r"에서|로부터", text):
        patterns.append(_FROM_TO)
    patterns += [_PARTY, _TO_CHANGE, _SWAP, _STAFF, _STAFF2]
    if re.search(r"전문인력|운용역|운용인력", text) and re.search(_ARROW, text):
        patterns.append(_ARROW_CHANGE)
    spans = set()
    for pattern in patterns:
        for m in pattern.finditer(text):
            if pattern is _PARTY and _PARTY_COMPANY.search(m.group(1)):
                continue
            for g in range(1, pattern.groups + 1):
                if m.group(g) is None:
                    continue
                for t in re.finditer(r"[가-힣]+", m.group(g)):
                    if _is_name(t.group(0)):
                        # [review16] 이름 뒤 조사는 남긴다('홍길동으로 변경' → '(이름 생략)으로 변경')
                        tok = t.group(0)
                        cut = re.search(r"(?:으로|로|은|는|이|가|의|과|와)$", tok)
                        trim = len(cut.group(0)) if cut and len(tok) - len(cut.group(0)) >= 2 and _is_name(tok[:cut.start()]) else 0
                        spans.add((m.start(g) + t.start(), m.start(g) + t.end() - trim))
                    elif re.match(_ACTION_WORDS, t.group(0)) and pattern is not _AFTER_COLON:
                        break
    # 겹치는 구간은 합친다(뒤에서부터 바꿀 때 글이 어긋나지 않게)
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def mask_officer_names(text: str | None) -> str | None:
    spans = _officer_spans(text)
    for start, end in reversed(spans):
        text = text[:start] + OFFICER_LABEL + text[end:]
    return text


def officer_names(text: str | None) -> list[str]:
    """가리지 않고 남은 임원 이름 후보(점검용). mask_officer_names를 거친 글에서는 빈 목록이어야 한다."""
    return [text[a:b] for a, b in _officer_spans(text)]


def redact_phones(value):
    if isinstance(value, dict):
        return {k: redact_phones(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_phones(v) for v in value]
    return re.sub(PHONE_RE, "(번호 생략)", value) if isinstance(value, str) else value


def phone_files(paths):
    return [p for p in paths if re.search(PHONE_RE, p.read_text(encoding="utf-8"))]


CHANGE_REQUEST = "변경인가/등록/신고"


def filing_result(rec, reit):
    if rec["req_type"] == CHANGE_REQUEST or not reit or not reit.get("date") or reit["date"] < rec["date"]:
        return {"result_kind": None, "result_date": None}
    return {"result_kind": reit["kind"], "result_date": reit["date"]}


LENDER_MARKERS = re.compile(r"은행|증권|보험|캐피탈|금고|공사|공단|기금|연금|조합|중앙회|회사|리츠|㈜|\(주\)|\(유\)|유한|펀드|신탁|운용|투자"
                            r"|저축|카드|생명|화재|공제|신협|새마을|농협|수협|뱅크|[A-Za-z]|證券|銀行|生命|保險|信託|金融")
# [review16] 한글 3자 이하는 표지가 이름 글자와 겹치기 쉬워('홍운용', '홍기금') 알려진 짧은 기관 이름 꼴만 남긴다
SHORT_LENDERS = re.compile(r"^(?:[가-힣]{0,2}신협|농협|수협|[가-힣]{0,2}금고)$")


def lender_label(name):
    """차입처가 개인 이름처럼 보이면 개인 표시로 바꾼다. '신협'처럼 짧은 금융기관 이름은 남긴다."""
    if name and mask_person(name) == PRIVATE_LABEL and not (
            SHORT_LENDERS.search(name.strip()) or (len(name.strip()) > 3 and LENDER_MARKERS.search(name))):
        return PRIVATE_LABEL
    return name
