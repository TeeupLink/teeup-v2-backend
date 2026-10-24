#!/usr/bin/env python3
"""앱이 부르는 경로가 서버에 그대로 있는지 확인한다.

앱이 /clubs 를 부르는데 서버 경로가 /clubs/ 뿐이면 FastAPI 가 307 로
정식 경로를 알려 준다. 리다이렉트 한 번이 더 붙는 것도 손해지만, 그보다
운영에서 iOS 가 그 리다이렉트를 막아 버린 적이 있다 (2026-10-24).
프록시 뒤에서 Location 이 http:// 로 나가 ATS 에 걸렸고, 앱에는
"Network request failed" 만 보였다. 서버 쪽은 고쳤지만, 애초에
리다이렉트가 안 생기게 경로를 맞춰 두는 편이 낫다.

사용:
    python scripts/check_app_api_paths.py [--app-dir ../teeup-v2-reactnative]

찾은 게 있으면 종료 코드 1.
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
DEFAULT_APP = os.path.join(os.path.dirname(BACKEND), "teeup-v2-reactnative")

API_PREFIX = "/api/v1"

# apiClient.get('/clubs', ...) / .post(`/rounds/${id}/expenses`, ...)
CALL = re.compile(
    r"""\.(?:get|post|put|patch|delete)\(\s*['"`](/[^'"`]*)['"`]""",
    re.VERBOSE,
)
# `${anything}` -> 경로 파라미터 자리
PARAM = re.compile(r"\$\{[^}]*\}")


def app_paths(app_dir: str):
    """앱 소스에서 호출 경로를 모은다. {파일: {경로: [줄번호]}}"""
    api_dir = os.path.join(app_dir, "src", "lib", "api")
    if not os.path.isdir(api_dir):
        print(f"앱 API 폴더를 찾지 못했습니다: {api_dir}", file=sys.stderr)
        raise SystemExit(2)

    found = {}
    for name in sorted(os.listdir(api_dir)):
        if not name.endswith(".js"):
            continue
        path = os.path.join(api_dir, name)
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for raw in CALL.findall(line):
                    found.setdefault(os.path.join("src/lib/api", name), []).append(
                        (lineno, raw)
                    )
    return found


def server_paths():
    """FastAPI 에 등록된 경로를 모은다."""
    sys.path.insert(0, BACKEND)
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    os.environ.setdefault("BACKEND_PORT", "7777")
    from main import app  # noqa: E402

    return set(app.openapi()["paths"].keys())


def normalize(raw: str) -> str:
    """앱 경로를 서버 경로 모양으로 바꾼다. ${x} 는 어떤 이름이든 같은 자리다."""
    return API_PREFIX + PARAM.sub("{p}", raw.split("?")[0])


def shape(path: str) -> str:
    """서버 경로의 파라미터 이름도 같은 자리로 지운다."""
    return re.sub(r"\{[^}]*\}", "{p}", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", default=DEFAULT_APP)
    args = ap.parse_args()

    server = {shape(p) for p in server_paths()}
    calls = app_paths(args.app_dir)

    findings = []
    for filename, entries in calls.items():
        for lineno, raw in entries:
            want = normalize(raw)
            if want in server:
                continue
            alt = want + "/" if not want.endswith("/") else want.rstrip("/")
            if alt in server:
                findings.append((filename, lineno, raw, alt))

    total = sum(len(v) for v in calls.values())
    print(f"앱 호출 경로 {total}개 확인")
    if not findings:
        print("[끝 슬래시 불일치] 없음")
        return 0

    print(f"\n[끝 슬래시 불일치] {len(findings)}건 — 307 리다이렉트가 생깁니다")
    for filename, lineno, raw, alt in findings:
        print(f"  {filename}:{lineno}  '{raw}'  ->  서버 경로는 '{alt}'")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
