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
