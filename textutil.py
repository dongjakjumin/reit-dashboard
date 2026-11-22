"""수집과 집계가 같이 쓰는 글자 처리."""
from __future__ import annotations

import re

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
    core = re.sub(r"외\d*(인|명)?$", "", core)
    is_person = bool(re.fullmatch(r"[가-힣]{2,3}", core)) or (
        bool(re.fullmatch(r"[가-힣]{4}", core)) and core.startswith(_TWO_SYLLABLE_SURNAMES))
    return PRIVATE_LABEL if is_person else name
