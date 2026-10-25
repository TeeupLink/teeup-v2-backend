# 타입 어노테이션 신문법 전환 작업

`Optional[X]` → `X | None`, `List[X]` → `list[X]` 로 코드베이스 전체를 옮긴다.
Ruff 가 에디터에서 계속 경고를 띄우는 것이 발단이고, 동작 변경은 목표가 아니다.

- 대상: `teeup-v2-backend` (파이썬만 해당)
- 브랜치: `dev`
- 시작 시점 HEAD: `cd079a0`

---

## 1. 왜 하는가

VS Code 가 `schemas/admin.py` 한 파일에만 경고 33개를 띄운다. 전부 Ruff 의
pyupgrade 계열 권고이고 **에러는 0개**다.

| 코드 | 내용 |
| --- | --- |
| `UP045` | `Optional[X]` → `X \| None` |
| `UP006` | `List[X]` → `list[X]` |
| `UP035` | `typing.List` 은 deprecated |
| `I001` | import 정렬 |

동작은 완전히 동일하다. Pydantic 에게 `Optional[datetime] = None` 과
`datetime | None = None` 은 같은 것이다. 순수하게 가독성·일관성 작업이다.

파이썬 3.12 (`Dockerfile:29` 가 `python:3.12-slim`) 이므로 신문법을 쓸 수 있다.
`from __future__ import annotations` 는 필요 없다.

## 2. 범위

Ruff 로 센 실제 위반 건수 (`--select UP006,UP007,UP045,UP035`):

```
UP045  Optional[X] -> X | None     730
UP006  List[X]     -> list[X]      161
UP035  deprecated import            62
UP007  Union[X, Y] -> X | Y          2
                               ---------
                                    955
```

단순 문자열로 세면 `Optional[` 734 / `List[` 139 이지만, 주석·독스트링 안의
텍스트가 섞여 있어서 위 숫자와 다르다. 기준은 Ruff 쪽이다.

## 3. 미리 짚어둔 위험

### 3-1. SQLAlchemy `Mapped[]` — 유일하게 신경 쓸 곳

`models/region.py` 에 `Mapped[Optional[str]]` 이 3곳 있다. SQLAlchemy 는 이
어노테이션을 **런타임에 읽어서** 컬럼 nullable 을 결정하므로, 단순 문자열
치환과 달리 실제로 의미가 있는 자리다.

SQLAlchemy 2.0.48 은 `Mapped[str | None]` 을 지원한다. 다만 지원한다는 말만
믿지 않고, 전환 후 **매핑된 컬럼의 nullable 이 그대로인지 직접 비교**한다.

### 3-2. 맥과의 충돌

최근 3일간 맥에서 `schemas/` 13개 파일을 건드렸다. 69개 파일을 한 번에
바꾸면 그쪽 작업과 충돌한다. **작업 전후로 `git pull` 을 확인**하고,
한 커밋으로 끝내서 충돌 구간을 짧게 가져간다.

### 3-3. 런타임 평가

FastAPI·Pydantic 은 어노테이션을 런타임에 평가한다. 문법만 바뀌고 평가
결과가 달라지면 안 된다. → OpenAPI 스키마 전후 비교로 검증한다 (4-2).

## 4. 검증 방법

"돌려봤더니 되더라" 로 끝내지 않는다. 바꾼 게 734곳이라 눈으로는 못 본다.

### 4-1. 전환 전 기준선 확보
- 전체 모듈 import
- OpenAPI 스키마 JSON 덤프
- SQLAlchemy 매핑 컬럼의 (이름, 타입, nullable) 덤프

### 4-2. 전환 후 동일성 증명
- **OpenAPI 스키마가 바이트 단위로 동일**해야 한다. 하나라도 다르면 API 계약이
  바뀐 것이므로 되돌린다.
- **매핑 컬럼 덤프가 동일**해야 한다 (3-1 검증).
- `test/test_scores_null_timestamp.py` 통과 (sqlite 로 독립 실행되는 테스트).
- 회귀 가드가 신문법에서도 작동하는지 확인 — 가드는 어노테이션을
  `"datetime"` 문자열과 비교하므로 **`datetime | None` 으로 바뀌면 가드가
  멍청해질 수 있다.** 반드시 다시 깨뜨려 보고 잡는지 확인한다.

> `test/` 전체는 원래부터 `55 failed, 17 passed` 다. `localhost:8002` 의 살아있는
> 서버를 찾는 통합 테스트들이고 이 작업과 무관하다. 숫자가 그대로인지만 본다.

## 5. 진행

- [x] 작업 문서 작성
- [x] 기준선 확보 (OpenAPI / 매핑 컬럼)
- [x] ruff 설치 및 전환 실행
- [x] OpenAPI 동일성 확인
- [x] SQLAlchemy 매핑 동일성 확인
- [x] 회귀 가드 재검증
- [x] `ruff.toml` 추가 (규칙 고정)
- [x] 커밋·푸시

## 6. 결과

Ruff 0.17.2 로 `UP006/UP007/UP035/UP045` 를 전부 정리했다. 위반 955건 → 0건.

| 검증 | 결과 |
| --- | --- |
| OpenAPI 스키마 (742KB, 경로 264 / 스키마 166) | md5 `59450c5e...` **전후 동일** — API 계약 변화 없음 |
| SQLAlchemy 매핑 컬럼 399개 | 전부 동일 (`Mapped[str \| None]` 포함) |
| 앱 전체 import | 성공 |
| `test_scores_null_timestamp.py` | 5 passed |
| `test/` 전체 | `55 failed, 17 passed` — 작업 전과 **같은 숫자** |

> `test/` 의 55개 실패는 원래부터 있던 것이다. `test_members.py` 등이
> `localhost:8002` 의 살아 있는 서버를 찾는 통합 테스트인데 로컬 컨테이너가
> 내려가 있다. 이 작업과 무관하며, 숫자가 그대로인 것으로 확인했다.

OpenAPI md5 가 전후 동일하다는 것이 이 작업의 핵심 근거다. 734곳을 눈으로
볼 수는 없으니, 모든 Pydantic 스키마가 만들어내는 최종 산출물이 바이트 단위로
같다는 것으로 대신한다.

### 6-1. 미사용이 된 typing import 는 내가 만든 것만 걷어냈다

문법을 바꾸고 나니 `from typing import Optional, List` 가 쓸모없어진 파일이
생겼다. 다만 레포에는 **원래부터 미사용 import 가 244건** 있었고, 그중에는
`schemas/__init__.py` 처럼 재수출 목적이라 지우면 깨지는 것이 섞여 있다.

그래서 전후 F401 목록을 비교해 **이번 변경으로 새로 생긴 113건만** 골라
제거했다. 제거 후 F401 이 정확히 244 로 돌아오는 것으로 확인했다.
(이후 UP035 로 걸린 기존 미사용 import 5건이 추가로 빠져 239 가 됐다.)

### 6-2. 회귀 가드가 신문법에서도 작동하는지 확인했다

가드가 어노테이션을 `"datetime"` 문자열과 비교하므로 전환으로 무력화될 수
있었다. 필수 필드는 `| None` 이 안 붙어 모양이 그대로여서 실제로는 영향이
없었지만, 믿지 않고 세 곳을 다시 깨뜨려 전부 잡는 것을 확인했다.

### 6-3. 못 바꾼 곳 1군데 (의도적)

`routers/auth.py:116` 의 `Optional["TokenRefreshRequest"]` 는 문자열
전방참조라 `"TokenRefreshRequest" | None` 로 바꾸면 런타임에 깨진다.
Ruff 도 이 경우는 건드리지 않는다. 그대로 뒀다.
나머지 잔여 `Optional[`/`List[` 는 전부 주석·독스트링 안의 텍스트다.

### 6-4. `ruff.toml` 을 추가했다

레포에 린트 설정이 아예 없어서 지금까지 각자 에디터 확장 기본값으로 돌고
있었다. 이번에 맞춘 신문법이 사람마다 달라지지 않게 규칙을 고정했다.

규칙은 **지금 실제로 지키는 것만** 켰다. 처음에 `F`/`I` 를 통째로 켰더니
317건이 나왔는데, 안 지킬 규칙을 켜 두면 경고가 배경 소음이 되어 진짜 문제가
묻힌다. 끄면서 사유를 파일에 적어뒀다.

---

## 7. 이 작업 중에 발견한 것 (별건, 손대지 않음)

`F821 undefined-name` 14건 + `F823` 1건이 나왔다. **전환 전에도 똑같이 15건**
이었으므로 이번 작업과 무관한 기존 문제다. 다만 호출되면 `NameError` 다.

가장 심한 곳은 `routers/admin/notices.py` 의 `POST /notices/upload` 로,
`validate_file` `MAX_FILE_SIZE` `generate_filename` `os` `requests`
`settings` `get_mime_type` `load_access_token` 8개가 import 되어 있지 않다.
이 라우터는 `routers/admin/__init__.py:45` 에서 등록되고 실제 경로
`/api/v1/admin/notices/upload` 로 떠 있다. **백오피스 공지 첨부 업로드는
현재 동작하지 않을 것이다.**

나머지:

| 위치 | 정의되지 않은 이름 |
| --- | --- |
| `routers/admin/inquiries.py:128` | `datetime` |
| `routers/admin/meetings.py:2936` | `Meeting` |
| `routers/payments.py:576` | `status` (대입 전 참조) |

이번 커밋 범위가 아니라 고치지 않았다. 별도 작업으로 다룰 것.
