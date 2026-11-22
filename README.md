# 리츠 인가와 프로젝트리츠 현황판

국토교통부 리츠정보시스템(reits.molit.go.kr)의 공개 자료를 영업일마다 한 번 받아 정적 HTML 한 장으로 만드는 개인 프로젝트입니다.

## 실행

파이썬 3.10 이상이면 되고, 설치할 패키지는 없습니다.

```
python collect.py   # 수집: data/ 아래에 저장
python build.py     # 화면 생성: site/index.html
python verify.py    # 배포 전 점검: 합계, 원문 건수, 상세 화면 대조, 개인정보
```

`run_daily.bat`은 위 세 가지를 차례로 실행하고 결과를 `logs/run.log`에 남깁니다. 내 PC에서 직접 돌려 볼 때 씁니다. 정기 수집은 GitHub Actions가 맡습니다(아래 배포 참조).

## 파일

| 파일 | 하는 일 |
|---|---|
| `reits_api.py` | 리츠정보시스템 조회 화면과 같은 요청을 보냄. 요청 사이 2초 간격 |
| `collect.py` | 운영 중 리츠, 접수현황, 상장리츠를 받아 저장. 접수 건별, 리츠별 누적 기록 갱신 |
| `build.py` | 집계 후 `template.html`, `template_listed.html`에 넣어 `site/index.html`(인가와 프로젝트리츠), `site/listed.html`(상장리츠 비교판) 생성. 공통 스타일은 `style.css` |
| `data/prices.json` | 상장리츠 종목별 일별 종가. 처음에 최근 1년치를 받고 이후 영업일마다 하루씩 추가 |
| `verify.py` | 배포 전 점검. 하나라도 어긋나면 종료 코드 1 |
| `config.json` | 강조할 자산관리회사, 화면 아래 표기할 이름과 연락처 |
| `data/snapshots/` | 날짜별 수집 원자료. 지우지 않고 쌓음 |
| `data/filings_log.json` | 접수 건별 기록(처음 본 날, 진행상태 변화, 목록에서 빠진 날) |

## 원칙

- 화면이 열릴 때 AI나 외부 서비스를 부르지 않습니다. 같은 자료면 같은 화면이 나옵니다.
- 공개 자료만 씁니다. 접수현황 응답에 들어 있는 신청 담당자 이름과 연락처는 저장하지 않습니다.
- 영업일에 하루 1회만 수집합니다.
- 숫자는 배포 전에 `verify.py`로 원문과 대조합니다.

## 배포

- 주소: https://dongjakjumin.github.io/reit-dashboard/
- `.github/workflows/daily.yml`이 평일 10:00(KST)에 수집, 화면 생성, 점검을 실행하고 수집 자료를 커밋한 뒤 GitHub Pages에 올립니다. 점검에서 하나라도 어긋나면 배포하지 않습니다.
- 주소 끝에 `?amc=회사이름`을 붙이면 자산관리회사별 표에서 그 회사 행을 강조합니다.

## 남은 일

- 상장리츠 시가배당률(주당 배당금 기준)과 차입 만기 일정: DART 공시 연동 필요
- 국고채 금리와 배당률 비교: 한국은행 ECOS 연동 필요
