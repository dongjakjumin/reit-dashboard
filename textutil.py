"""수집과 집계가 같이 쓰는 글자 처리."""
from __future__ import annotations

import re

# 전화번호 모양: 02-123-4567, 010-1234-5678, 01012345678 등. 금액 같은 긴 숫자의 일부는 잡지 않는다.
PHONE_RE = r"(?<![\d.,])(?:0\d{1,2}[-.\s]\d{3,4}[-.\s]\d{4}|01[016789]\d{7,8})(?![\d,])"


PRIVATE_LABEL = "개인으로 보여 이름 생략"
_TWO_SYLLABLE_SURNAMES = ("남궁", "선우", "제갈", "황보", "독고", "사공", "서문")


def mask_person(name: str | None) -> str | None:
    """최대주주 칸에 개인 이름으로 보이는 값이 있으면 이름을 지운다.

    회사 표기((주), 주식회사)가 없고, 공백과 끝의 '외 N인'을 떼고 남은 글자가 한글 2~3자이면 개인 이름으로 본다.
    4자는 두 글자 성으로 시작할 때만 개인 이름으로 본다(롯데쇼핑 같은 회사 이름을 지우지 않기 위해).
    """
    if not name:
        return name
    if re.search(r"\(주\)|㈜|주식회사|유한회사", name):
        return name  # 회사 표기가 붙어 있으면 회사로 본다
    core = re.sub(r"\s", "", name)
    core = re.sub(r"(?:\([^()]*\))+", "", core)
    core = re.sub(r"외\d*(인|명)?$", "", core)
    is_person = bool(re.fullmatch(r"[가-힣]{2,3}", core)) or (
        bool(re.fullmatch(r"[가-힣]{4}", core)) and core.startswith(_TWO_SYLLABLE_SURNAMES))
    return PRIVATE_LABEL if is_person else name


# 공시 본문의 임원 선임, 사임 문구에서 사람 이름을 지운다. 임원 변경 공시는 본문을 저장하지 않는 원칙을
# '기타' 공시에 섞여 들어온 같은 내용에도 적용한다. 예: "사내이사 홍길동 선임", "(사임) 대표이사 홍길동, 이사 김철수",
# "사내이사 홍길동, 김철수 선임", "대표이사 홍길동 외 1인 선임", "이사 선임: 홍길동", "사내이사 홍길동 선임 및 김철수 사임"
OFFICER_LABEL = "(이름 생략)"
_TITLES = (r"(?:대표이사|사내이사|사외이사|기타비상무이사|비상무이사|감독이사|법인이사|집행임원|상근감사|감사위원"
           r"|청산인|감사|이사)")
# '중도퇴임', '일신상의 사유로 사임'처럼 이름과 동작 사이에 붙는 말도 동작의 일부로 본다
_PERSON_ACTIONS = (r"(?:(?:중도|일신상의?\s*사유로|개인\s*사정으로)\s*)?"
                   r"(?:재선임|선임|사임|해임|중임|연임|퇴임|취임|선출|임기\s?만료)")
_ACTIONS = rf"(?:{_PERSON_ACTIONS}|변경\s?전|변경\s?후)"
_NOT_NAMES = {"보수", "선임", "사임", "해임", "중임", "연임", "퇴임", "취임", "재선임", "후보", "후보자", "전원", "변경",
              "승인", "신규", "선출", "등", "및", "각", "선임의", "사임의", "해임의", "임원", "후임", "신임", "이사회",
              "감사인", "위원", "위원장", "의장", "대표", "상근", "비상근", "추가", "일괄", "동시", "임기", "만료",
              "이름", "생략", "취소", "결의", "예정", "완료", "현재", "기존", "중도", "사유로", "사정으로", "일신상의"}
_NAME = r"[가-힣]{2,4}"
_SEP = r"\s*(?:,|·|ㆍ|/|및)\s*"
_END = rf"(?=\s*(?:,|\(|\)|외\s*\d|{_ACTIONS}|$)|\s+{_TITLES}|\s+-|\s*;)"
# 직함 바로 뒤의 이름(여럿이면 쉼표, 가운뎃점, '및'으로 이어진 것까지). 왼쪽 경계를 두어 '외부감사'의 '감사'는 잡지 않는다.
_AFTER_TITLE = re.compile(rf"(?<![가-힣]){_TITLES}\s*[:：]?\s*({_NAME}(?:{_SEP}{_NAME})*){_END}")
# "선임: 홍길동"처럼 동작 뒤 쌍점에 이어진 이름
_AFTER_COLON = re.compile(rf"{_ACTIONS}\s*[:：]\s*({_NAME}(?:{_SEP}{_NAME})*)(?![가-힣])")
# "및 김철수 사임"처럼 직함 없이 동작 바로 앞에 온 이름
_BEFORE_ACTION = re.compile(rf"(?<![가-힣])({_NAME})(?=\s*{_PERSON_ACTIONS})")
# "신임 대표이사 김철수", "후임 감사: 김철수"
_NEW_OFFICER = re.compile(rf"(?:신임|후임)\s*{_TITLES}\s*[:：]?\s*({_NAME}(?:{_SEP}{_NAME})*)(?![가-힣])")
# "대표이사 변경(홍길동→김철수)", "대표이사: 홍길동 -> 김철수"처럼 직함 뒤 화살표로 이은 두 이름
_ARROW = r"(?:→|->|⇒|➔|➜)"
_ARROW_CHANGE = re.compile(
    rf"(?<![가-힣]){_TITLES}[^.\n]{{0,12}}?(?<![가-힣])({_NAME})\s*\(?\s*{_ARROW}\s*({_NAME})(?![가-힣])")


def _is_name(token: str) -> bool:
    return len(token) >= 2 and token not in _NOT_NAMES and not re.fullmatch(_TITLES, token)


def _officer_spans(text: str | None) -> list[tuple[int, int]]:
    """임원 문구 안에서 이름으로 보이는 곳의 (시작, 끝). 직함과 함께 동작 낱말, '신임/후임', 화살표가 있는 글만 본다."""
    if not text or not re.search(_TITLES, text):
        return []
    patterns = []
    if re.search(_ACTIONS, text):
        patterns += [_AFTER_TITLE, _AFTER_COLON, _BEFORE_ACTION]
    if re.search(r"신임|후임", text):
        patterns.append(_NEW_OFFICER)
    if re.search(_ARROW, text):
        patterns.append(_ARROW_CHANGE)
    spans = set()
    for pattern in patterns:
        for m in pattern.finditer(text):
            for g in range(1, pattern.groups + 1):
                if m.group(g) is None:
                    continue
                for t in re.finditer(r"[가-힣]+", m.group(g)):
                    if _is_name(t.group(0)):
                        spans.add((m.start(g) + t.start(), m.start(g) + t.end()))
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
