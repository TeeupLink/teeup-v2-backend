from __future__ import annotations

# 실행:
#   .venv/bin/python scripts/loadtest/run_single_test_profile.py
# 테스트 요약:
#   - main.py 라우터를 import해 전체 endpoint 카탈로그 수집
#   - 테스트 모드 서버 기동 후 GET/POST/PATCH/PUT 라우트를 10회씩 호출(1회 워밍업 제외)
#   - body payload는 sample/routes, sample/schemas를 우선 사용
#   - endpoint 지연시간 집계, 커버리지 계산, 상위 구간 그래프/JSON 리포트 생성
#   - 출력물은 output/YYYYMMDD_HHMMSS/ 하위로 실행별 분리 저장
#   - 실행된 endpoint 전체를 실제 router 함수 대상으로 line_profiler(lprof) 수집

import argparse
import asyncio
import copy
import datetime
import enum
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import time
import types
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, get_args, get_origin
from urllib.parse import urljoin, urlparse

import httpx
import requests
from fastapi.routing import APIRoute

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.append(str(CURRENT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.auth_provider import AuthProvider
from lib.scenario_manager import ManagerScenario
from lib.scenario_user import UserScenario
from lib.state_store import StateStore

TARGET_HTTP_METHODS = {"GET", "POST", "PATCH", "PUT"}
SAMPLE_SCHEMA_DIR = CURRENT_DIR / "sample" / "schemas"
SAMPLE_ROUTE_DIR = CURRENT_DIR / "sample" / "routes"
# 외부 API endpoint 제외용 블랙리스트(정확 매칭)
ENDPOINT_BLACKLIST: set[tuple[str, str]] = {
    ("POST", "/region-list"),
    ("GET", "/openapi.json"),
}
# 외부 연동 도메인 제외용 블랙리스트(prefix 매칭)
ENDPOINT_BLACKLIST_PATH_PREFIXES: set[str] = {
    "/payments",
    "/payment-methods",
    "/subscriptions",
    "/admin/payments",
    "/upload",
    "/admin/upload",
    "/admin/notices/upload",
}
ENDPOINT_REPEAT_COUNT = 10
ENDPOINT_WARMUP_DROPS = 1


@dataclass
class RequestRecord:
    method: str
    path: str
    name: str
    status_code: int
    elapsed_ms: float
    failed: bool
    error: str | None = None


@dataclass
class ReplayRequest:
    method: str
    path: str
    path_url: str
    name: str
    headers: dict[str, str]
    json_body: dict | None
    elapsed_ms: float
    status_code: int


class ResponseContext:
    def __init__(self, response: requests.Response, record: RequestRecord, sink: list[RequestRecord]):
        """Locust catch_response 호환 응답 컨텍스트를 초기화"""
        self._response = response
        self._record = record
        self._sink = sink
        self._finalized = False

    @property
    def status_code(self) -> int:
        """응답 상태 코드를 반환"""
        return self._response.status_code

    @property
    def text(self) -> str:
        """응답 본문 텍스트를 반환"""
        return self._response.text

    def json(self):
        """응답 본문 JSON을 반환"""
        return self._response.json()

    def success(self) -> None:
        """현재 요청을 성공으로 표시"""
        self._record.failed = False
        self._record.error = None

    def failure(self, message: str) -> None:
        """현재 요청을 실패로 표시하고 메시지를 남긴다."""
        self._record.failed = True
        self._record.error = message

    def _finalize(self) -> None:
        """기록이 중복 추가되지 않도록 1회만 확정"""
        if self._finalized:
            return
        self._finalized = True
        self._sink.append(self._record)

    def __enter__(self):
        """with 블록 진입 시 컨텍스트 자신을 반환"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """with 블록 종료 시 예외 여부를 기록하고 요청을 확정"""
        if exc is not None:
            self._record.failed = True
            self._record.error = str(exc)
        self._finalize()
        return False


class LocustLikeHttpClient:
    def __init__(self, base_url: str, timeout_sec: int = 30):
        """요청 기록/리플레이를 위한 경량 HTTP 클라이언트를 초기화"""
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.session = requests.Session()
        self.records: list[RequestRecord] = []
        self.replay_requests: list[ReplayRequest] = []
        self._csrf_exempt_paths = {
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/api/v1/auth/login",
            "/api/v1/auth/register",
            "/api/v1/auth/refresh",
            "/api/v1/auth/push-token",
            "/api/v1/auth/logout",
            "/api/v1/auth/oauth/google/callback",
            "/api/v1/admin/login",
            "/api/v1/auth/csrf-token",
        }

    def _needs_csrf(self, method: str, path: str) -> bool:
        """요청 메서드/경로 기준으로 CSRF 토큰 필요 여부를 계산"""
        if method in {"GET", "HEAD", "OPTIONS"}:
            return False
        return path not in self._csrf_exempt_paths

    def _fetch_csrf_token(self) -> str:
        """테스트 서버에서 CSRF 토큰을 발급받아 반환"""
        response = self.session.get(
            f"{self.base_url}/api/v1/auth/csrf-token",
            timeout=self.timeout_sec,
        )
        if response.status_code != 200:
            raise RuntimeError(f"CSRF token fetch failed: {response.status_code} {response.text}")
        payload = response.json() or {}
        token = payload.get("csrf_token")
        if not token:
            raise RuntimeError("CSRF token missing in /api/v1/auth/csrf-token response")
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        json_body: dict | None = None,
        name: str | None = None,
        catch_response: bool = False,
    ):
        """요청을 전송하고 응답/프로파일링용 리플레이 정보를 함께 기록"""
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.base_url}{path}"
        request_headers = dict(headers or {})
        final_headers = dict(request_headers)
        if self._needs_csrf(method, path) and "X-CSRF-Token" not in request_headers:
            request_headers["X-CSRF-Token"] = self._fetch_csrf_token()
            final_headers = dict(request_headers)

        started = perf_counter()
        response = self.session.request(
            method=method,
            url=url,
            headers=request_headers,
            json=json_body,
            timeout=self.timeout_sec,
            allow_redirects=False,
        )

        if response.status_code in {307, 308} and response.headers.get("location"):
            redirected = response.headers["location"]
            if redirected.startswith("http://") or redirected.startswith("https://"):
                redirected_url = redirected
                redirected_path = urlparse(redirected_url).path
            else:
                redirected_path = redirected
                redirected_url = urljoin(f"{self.base_url}/", redirected_path.lstrip("/"))

            redirected_headers = dict(request_headers)
            if self._needs_csrf(method, redirected_path):
                redirected_headers["X-CSRF-Token"] = self._fetch_csrf_token()
            final_headers = dict(redirected_headers)

            response = self.session.request(
                method=method,
                url=redirected_url,
                headers=redirected_headers,
                json=json_body,
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
        elapsed_ms = (perf_counter() - started) * 1000
        request_path = urlparse(str(response.request.url)).path if response.request else path
        request_path_url = response.request.path_url if response.request else path

        record = RequestRecord(
            method=method,
            path=request_path,
            name=name or path,
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            failed=response.status_code >= 400,
            error=None if response.status_code < 400 else response.text[:300],
        )
        replay_headers = dict(final_headers)
        replay_headers.pop("X-CSRF-Token", None)
        self.replay_requests.append(
            ReplayRequest(
                method=method,
                path=request_path,
                path_url=request_path_url,
                name=name or path,
                headers=replay_headers,
                json_body=json_body,
                elapsed_ms=elapsed_ms,
                status_code=response.status_code,
            )
        )

        if catch_response:
            return ResponseContext(response=response, record=record, sink=self.records)

        self.records.append(record)
        return response

    def get(self, path: str, *, headers: dict | None = None, name: str | None = None, catch_response: bool = False):
        """GET 요청을 전송"""
        return self._request("GET", path, headers=headers, name=name, catch_response=catch_response)

    def post(
        self,
        path: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        name: str | None = None,
        catch_response: bool = False,
    ):
        """POST 요청을 전송"""
        return self._request(
            "POST",
            path,
            headers=headers,
            json_body=json,
            name=name,
            catch_response=catch_response,
        )

    def put(
        self,
        path: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        name: str | None = None,
        catch_response: bool = False,
    ):
        """PUT 요청을 전송"""
        return self._request(
            "PUT",
            path,
            headers=headers,
            json_body=json,
            name=name,
            catch_response=catch_response,
        )


def ensure_user_ready(auth_provider: AuthProvider, state, label: str) -> None:
    """테스트 사용자 계정의 온보딩 필수 단계를 완료"""
    if not auth_provider.ensure_bootstrap():
        raise RuntimeError(f"{label}: bootstrap failed")
    if not auth_provider.oauth_mock_login(state):
        raise RuntimeError(f"{label}: oauth mock login failed")
    if not auth_provider.agree_required_terms(state):
        raise RuntimeError(f"{label}: agree required terms failed")

    # current backend validation rejects numbers in english realname.
    payload = {
        "realname": "Load User",
        "phone_number": f"0100000{state.vu_id:04d}"[-11:],
        "birthdate": "1990-01-01",
        "gender": "MALE" if state.vu_id % 2 == 0 else "FEMALE",
        "average_score_init": 95,
    }
    headers = {
        "Authorization": f"Bearer {state.access_token}",
        "Content-Type": "application/json",
    }
    response = auth_provider.client.put(
        "/api/v1/users/me",
        json=payload,
        headers=headers,
        name="users.complete_profile",
    )
    if response.status_code != 200:
        raise RuntimeError(f"{label}: complete profile failed - {response.status_code} {response.text}")


def print_route_sweep_summary(total_routes: int, elapsed_ms: float, requests: list[dict]) -> None:
    """라우터 전수 실행 결과를 요약 출력"""
    total_requests = len(requests)
    failed = sum(1 for row in requests if row.get("failed"))
    success = total_requests - failed
    print("\n=== Router Sweep Summary ===")
    print(f"Target routes : {total_routes}")
    print(f"Requests made : {total_requests}")
    print(f"Success/Fail  : {success}/{failed}")
    print(f"Elapsed(ms)   : {elapsed_ms:.2f}")


def start_test_server(
    python_executable: str,
    port: int,
    log_path: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen, object]:
    """테스트 모드 환경변수로 main.py 서버 프로세스를 기동"""
    log_file = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "ENVIRONMENT": "test",
            "ENABLE_LOADTEST_AUTH_MOCK": "true",
            "LOADTEST_AUTH_MOCK_ALLOWED_IPS": "127.0.0.1,::1",
            "BACKEND_PORT": str(port),
            "PYTHONUNBUFFERED": "1",
        }
    )
    if extra_env:
        env.update(extra_env)

    process = subprocess.Popen(
        [python_executable, "main.py"],
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )
    return process, log_file


def apply_test_env_defaults_for_inprocess(port: int) -> None:
    """in-process app import/replay용 테스트 환경변수 기본값을 설정"""
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("ENABLE_LOADTEST_AUTH_MOCK", "true")
    os.environ.setdefault("LOADTEST_AUTH_MOCK_ALLOWED_IPS", "127.0.0.1,::1")
    os.environ.setdefault("BACKEND_PORT", str(port))


def stop_server(process: subprocess.Popen, log_file) -> None:
    """기동한 서버 프로세스를 안전하게 종료하고 로그 파일을 닫는다."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    log_file.close()


def wait_until_ready(base_url: str, process: subprocess.Popen, timeout_sec: int = 60) -> None:
    """health 체크가 성공할 때까지 서버 기동을 대기"""
    deadline = time.time() + timeout_sec
    health_url = f"{base_url.rstrip('/')}/health"

    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited early with code {process.returncode}")
        try:
            response = requests.get(health_url, timeout=1.5)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)

    raise RuntimeError("Server did not become healthy within timeout")


def normalize_endpoint_path(path: str) -> str:
    """경로를 정규화하고 /api/v1 prefix를 제거해 비교 가능한 형태로 만든다."""
    parsed_path = urlparse(path).path if path else "/"
    if not parsed_path.startswith("/"):
        parsed_path = f"/{parsed_path}"

    prefix = "/api/v1"
    if parsed_path.startswith(prefix):
        parsed_path = parsed_path[len(prefix):] or "/"
        if not parsed_path.startswith("/"):
            parsed_path = f"/{parsed_path}"
    return parsed_path


def _unwrap_annotation(annotation: Any) -> Any:
    """Annotated/Optional 등 래핑된 annotation을 본 타입으로 단순화"""
    if annotation is None:
        return None

    origin = get_origin(annotation)
    if origin is None:
        return annotation

    args = get_args(annotation)
    origin_text = str(origin)
    if origin_text.endswith("Annotated"):
        return _unwrap_annotation(args[0]) if args else annotation
    if origin_text.endswith("Union"):
        non_none = [arg for arg in args if arg is not type(None)]
        return _unwrap_annotation(non_none[0]) if non_none else annotation
    return annotation


def _is_blacklisted_endpoint(method: str, path_template: str) -> bool:
    """endpoint 블랙리스트 포함 여부를 반환"""
    if (method, path_template) in ENDPOINT_BLACKLIST:
        return True
    for prefix in ENDPOINT_BLACKLIST_PATH_PREFIXES:
        if path_template == prefix or path_template.startswith(f"{prefix}/"):
            return True
    return False


def load_backend_app_from_main() -> Any:
    """프로젝트 루트 main.py를 동적 import해 FastAPI app 객체를 가져온다."""
    main_path = PROJECT_ROOT / "main.py"
    spec = importlib.util.spec_from_file_location("teeup_backend_main", main_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load main.py: {main_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    app = getattr(module, "app", None)
    if app is None:
        raise RuntimeError("main.py has no `app` object")
    return app


def _route_router_name(route: APIRoute) -> str:
    """APIRoute에서 라우터 식별용 이름(태그/모듈)을 추출"""
    tags = list(getattr(route, "tags", []) or [])
    if tags:
        return str(tags[0])

    endpoint_module = getattr(route.endpoint, "__module__", "unknown")
    if endpoint_module.startswith("routers."):
        parts = endpoint_module.split(".")
        if len(parts) >= 2:
            return parts[1]
    return endpoint_module


def collect_router_endpoints(app: Any) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    """앱의 모든 APIRoute를 수집해 카탈로그/라우터별 목록/매처를 생성"""
    route_catalog: list[dict] = []
    router_endpoints: dict[str, list[dict]] = defaultdict(list)
    route_matchers: list[dict] = []

    seen_catalog_keys: set[tuple[str, str]] = set()
    seen_matcher_keys: set[tuple[str, str]] = set()

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue

        methods = sorted(method for method in (route.methods or set()) if method in TARGET_HTTP_METHODS)
        if not methods:
            continue

        router_name = _route_router_name(route)
        path_template = normalize_endpoint_path(route.path)
        full_path_template = route.path
        body_field = route.body_field
        body_annotation = None
        if body_field is not None:
            body_annotation = getattr(getattr(body_field, "field_info", None), "annotation", None)
            if body_annotation is None:
                body_annotation = getattr(body_field, "annotation", None)
        body_annotation = _unwrap_annotation(body_annotation)
        path_param_names = [param.name for param in route.dependant.path_params]
        path_param_annotations = {
            param.name: getattr(getattr(param, "field_info", None), "annotation", None)
            for param in route.dependant.path_params
        }

        for method in methods:
            if _is_blacklisted_endpoint(method, path_template):
                continue
            endpoint_label = f"[{method}] {path_template}"
            catalog_key = (method, path_template)
            matcher_key = (method, full_path_template)

            if catalog_key not in seen_catalog_keys:
                entry = {
                    "router": router_name,
                    "method": method,
                    "path": path_template,
                    "full_path": full_path_template,
                    "endpoint": endpoint_label,
                }
                route_catalog.append(entry)
                router_endpoints[router_name].append(entry)
                seen_catalog_keys.add(catalog_key)

            if matcher_key not in seen_matcher_keys:
                route_matchers.append(
                    {
                        "router": router_name,
                        "method": method,
                        "path": path_template,
                        "full_path": full_path_template,
                        "endpoint": endpoint_label,
                        "endpoint_callable": route.endpoint,
                        "path_param_names": path_param_names,
                        "path_param_annotations": path_param_annotations,
                        "body_annotation": body_annotation,
                        "route_sample_key": _route_sample_key(method, path_template),
                        "path_regex": route.path_regex,
                    }
                )
                seen_matcher_keys.add(matcher_key)

    route_catalog.sort(key=lambda x: (x["router"], x["path"], x["method"]))
    sorted_router_endpoints = {
        router_name: sorted(entries, key=lambda x: (x["path"], x["method"]))
        for router_name, entries in sorted(router_endpoints.items(), key=lambda x: x[0])
    }
    return route_catalog, sorted_router_endpoints, route_matchers


def print_router_endpoint_inventory(router_endpoints: dict[str, list[dict]]) -> None:
    """라우터별 endpoint 개수를 콘솔 표로 출력"""
    print("\n=== Router Endpoint Inventory ===")
    print(f"{'Router':24} {'Endpoints':>10}")
    print("-" * 38)
    total = 0
    for router_name, entries in router_endpoints.items():
        count = len(entries)
        total += count
        print(f"{router_name[:24]:24} {count:10d}")
    print("-" * 38)
    print(f"{'Total':24} {total:10d}")


def _match_route_template(method: str, request_path: str, route_matchers: list[dict]) -> dict | None:
    """실제 요청 경로를 route regex에 매칭해 템플릿 endpoint를 찾는다."""
    for matcher in route_matchers:
        if matcher["method"] != method:
            continue
        if matcher["path_regex"].match(request_path):
            return matcher
    return None


def aggregate_endpoint_stats(records: list[dict], route_matchers: list[dict]) -> list[dict]:
    """요청 기록을 endpoint 단위로 집계해 평균/최대 지연시간을 계산"""
    agg: dict[tuple[str, str], dict] = {}

    for rec in records:
        method = (rec.get("method") or "GET").upper()
        request_path = urlparse(rec.get("path") or "/").path
        matcher = _match_route_template(method, request_path, route_matchers)

        if matcher:
            path = matcher["path"]
            endpoint = matcher["endpoint"]
            router_name = matcher["router"]
        else:
            path = normalize_endpoint_path(request_path)
            endpoint = f"[{method}] {path}"
            router_name = "(unknown)"

        key = (method, path)
        if key not in agg:
            agg[key] = {
                "endpoint": endpoint,
                "router": router_name,
                "method": method,
                "path": path,
                "count": 0,
                "sum_ms": 0.0,
                "avg_ms": 0.0,
                "max_ms": 0.0,
                "fail_count": 0,
            }

        item = agg[key]
        item["count"] += 1
        item["sum_ms"] += rec["elapsed_ms"]
        item["max_ms"] = max(item["max_ms"], rec["elapsed_ms"])
        item["fail_count"] += 1 if rec["failed"] else 0

    rows = []
    for item in agg.values():
        item["avg_ms"] = item["sum_ms"] / item["count"] if item["count"] else 0.0
        item.pop("sum_ms", None)
        rows.append(item)

    rows.sort(key=lambda x: x["avg_ms"], reverse=True)
    return rows


def build_endpoint_coverage(route_catalog: list[dict], endpoint_stats: list[dict]) -> list[dict]:
    """전체 라우트 카탈로그 대비 실제 호출 커버리지를 계산"""
    stats_map = {(row["method"], row["path"]): row for row in endpoint_stats}
    coverage_rows: list[dict] = []

    for endpoint in route_catalog:
        stat = stats_map.get((endpoint["method"], endpoint["path"]))
        coverage_rows.append(
            {
                "router": endpoint["router"],
                "method": endpoint["method"],
                "path": endpoint["path"],
                "endpoint": endpoint["endpoint"],
                "count": stat["count"] if stat else 0,
                "avg_ms": stat["avg_ms"] if stat else 0.0,
                "max_ms": stat["max_ms"] if stat else 0.0,
                "fail_count": stat["fail_count"] if stat else 0,
                "hit": bool(stat and stat["count"] > 0),
            }
        )

    coverage_rows.sort(key=lambda x: (0 if x["hit"] else 1, -x["avg_ms"], x["endpoint"]))
    return coverage_rows


def print_coverage_summary(coverage_rows: list[dict]) -> None:
    """endpoint 커버리지 요약(hit/total)을 출력"""
    total = len(coverage_rows)
    hit = sum(1 for row in coverage_rows if row["hit"])
    ratio = (hit / total * 100.0) if total else 0.0
    print(f"\n=== Endpoint Coverage ===\nHit: {hit}/{total} ({ratio:.2f}%)")


def print_endpoint_table(stats: list[dict], title: str = "Endpoint Latency Summary") -> None:
    """endpoint 지연시간 집계 결과를 표 형태로 출력"""
    print(f"\n=== {title} ===")
    print(f"{'Endpoint':32} {'Count':>6} {'Avg(ms)':>10} {'Max(ms)':>10} {'Fail':>6}")
    print("-" * 72)
    for row in stats:
        print(
            f"{row['endpoint'][:32]:32} "
            f"{row['count']:6d} "
            f"{row['avg_ms']:10.2f} "
            f"{row['max_ms']:10.2f} "
            f"{row['fail_count']:6d}"
        )
    print("-" * 72)


def save_endpoint_graph_matplotlib(
    stats: list[dict],
    output_png: Path,
    show_ms_labels: bool = True,
) -> None:
    """endpoint 평균 지연시간 막대 그래프를 PNG로 저장"""
    if not stats:
        print("\n[Graph] endpoint data 없음")
        return

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "matplotlib not installed. install with: pip install -r scripts/loadtest/requirements.txt"
        ) from exc

    labels = [row["endpoint"] for row in stats]
    values = [row["avg_ms"] for row in stats]

    fig_width = max(12, len(labels) * 0.8)
    fig, ax = plt.subplots(figsize=(fig_width, 7))
    x_positions = range(len(labels))
    bars = ax.bar(x_positions, values, color="#2878B5")

    ax.set_title(f"Top {len(labels)} Endpoint Avg Latency")
    ax.set_xlabel("Endpoint")
    ax.set_ylabel("Avg Latency (ms)")
    ax.set_xticks(list(x_positions))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=9)
    ax.grid(axis="y", linestyle="--", alpha=0.3)

    if show_ms_labels:
        max_value = max(values) if values else 0.0
        label_offset = max(max_value * 0.01, 1.0)
        for bar, value in zip(bars, values):
            label_value = int(round(value))
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                value + label_offset,
                f"{label_value} ms",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    fig.tight_layout()
    output_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_png, dpi=160)
    plt.close(fig)
    print(f"\nSaved endpoint graph: {output_png}")


def _endpoint_key_for_request(
    method: str,
    request_path: str,
    route_matchers: list[dict],
) -> tuple[str, str]:
    """요청 메서드/경로를 라우트 템플릿 키(method, path)로 변환"""
    matched = _match_route_template(method, request_path, route_matchers)
    if matched:
        return matched["method"], matched["path"]
    return method, normalize_endpoint_path(request_path)


def _safe_filename(text: str, max_length: int = 120) -> str:
    """endpoint 이름을 파일 저장 가능한 안전한 문자열로 변환"""
    safe = text.lower()
    safe = safe.replace("[", "").replace("]", "")
    safe = safe.replace(" ", "_")
    safe = safe.replace("/", "_")
    safe = re.sub(r"[^a-z0-9._-]+", "_", safe)
    safe = re.sub(r"_+", "_", safe).strip("_.")
    if not safe:
        safe = "endpoint"
    return safe[:max_length]


def _route_sample_key(method: str, path: str) -> str:
    """라우트(method+path)를 sample/routes 파일명 키로 변환"""
    return _safe_filename(f"{method.lower()} {path}")


def _schema_sample_keys(annotation: Any) -> list[str]:
    """annotation에서 schema 샘플 검색 우선순위 키를 생성"""
    annotation = _unwrap_annotation(annotation)
    if annotation is None:
        return []

    name = getattr(annotation, "__name__", None)
    module = getattr(annotation, "__module__", None)
    keys: list[str] = []
    if module and name:
        keys.append(f"{module}.{name}")
    if name:
        keys.append(name)
    return keys


def _load_json_samples(directory: Path) -> dict[str, dict]:
    """sample 디렉터리의 json 파일을 stem 기준으로 로드"""
    samples: dict[str, dict] = {}
    if not directory.exists():
        return samples

    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            samples[path.stem] = payload
    return samples


def _default_path_param_value(param_name: str) -> str:
    """알 수 없는 path param에 대한 기본 샘플 값을 반환"""
    if param_name.endswith("_id") or param_name in {"id"}:
        return "1"
    if param_name in {"terms_type"}:
        return "SERVICE"
    if param_name in {"payment_key"}:
        return "test-payment-key"
    return "test"


def _build_path_param_values(regular_state, manager_state) -> dict[str, str]:
    """현재 준비된 상태값으로 path param 샘플 맵을 구성"""
    meeting_id = manager_state.target_meeting_id or regular_state.target_meeting_id or 1
    participant_id = regular_state.target_participant_id or 1
    club_numeric_id = manager_state.managed_club_numeric_id
    if club_numeric_id is None:
        joined_club_id = regular_state.joined_club_id
        if joined_club_id and str(joined_club_id).isdigit():
            club_numeric_id = int(joined_club_id)
    if club_numeric_id is None:
        club_numeric_id = 1

    return {
        "admin_id": "1",
        "category_id": "1",
        "club_id": str(club_numeric_id),
        "expense_id": "1",
        "faq_id": "1",
        "fee_id": "1",
        "file_id": "1",
        "inquiry_id": "1",
        "meeting_id": str(meeting_id),
        "member_id": str(regular_state.user_id or 1),
        "notice_id": "1",
        "notification_id": "1",
        "participant_id": str(participant_id),
        "payment_id": "1",
        "payment_key": "test-payment-key",
        "payment_method_id": "1",
        "plan_id": "1",
        "regulation_id": "1",
        "response_id": "1",
        "score_id": "1",
        "subscription_id": "1",
        "team_id": "1",
        "term_id": "1",
        "terms_id": "1",
        "terms_type": "SERVICE",
        "user_id": str(regular_state.user_id or manager_state.user_id or 1),
        "club_numeric_id": str(club_numeric_id),
    }


def _render_route_path(
    full_path: str,
    path_param_names: list[str],
    path_param_values: dict[str, str],
    path_param_annotations: dict[str, Any] | None = None,
) -> str:
    """라우트 템플릿(/x/{id})을 실제 호출 경로(/x/1)로 변환"""
    rendered = full_path
    for param_name in path_param_names:
        value = path_param_values.get(param_name, _default_path_param_value(param_name))
        annotation = (path_param_annotations or {}).get(param_name)
        if annotation is int:
            if param_name == "club_id" and "club_numeric_id" in path_param_values:
                value = path_param_values["club_numeric_id"]
            elif str(value).isdigit():
                value = str(value)
            else:
                value = "1"
        rendered = rendered.replace(f"{{{param_name}}}", str(value))
    return rendered


def _sample_value_for_annotation(annotation: Any, depth: int = 0) -> Any:
    """pydantic/typing annotation에서 샘플 값을 생성"""
    if depth > 4 or annotation is None:
        return None

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is not None:
        if origin in {list, tuple, set}:
            item_type = args[0] if args else str
            return [_sample_value_for_annotation(item_type, depth + 1)]
        if origin is dict:
            value_type = args[1] if len(args) > 1 else str
            return {"key": _sample_value_for_annotation(value_type, depth + 1)}
        if str(origin).endswith("Annotated"):
            return _sample_value_for_annotation(args[0], depth + 1) if args else None
        if str(origin).endswith("Literal"):
            return args[0] if args else None
        if str(origin).endswith("Union"):
            non_none = [arg for arg in args if arg is not type(None)]
            return _sample_value_for_annotation(non_none[0], depth + 1) if non_none else None

    if inspect.isclass(annotation):
        if issubclass(annotation, enum.Enum):
            first = next(iter(annotation), None)
            return first.value if first is not None else None

        model_fields = getattr(annotation, "model_fields", None)
        if isinstance(model_fields, dict):
            payload: dict[str, Any] = {}
            for field_name, field_info in model_fields.items():
                default = getattr(field_info, "default", None)
                if default is not None and str(default) != "PydanticUndefined":
                    payload[field_name] = default
                    continue
                value = _sample_value_for_annotation(getattr(field_info, "annotation", Any), depth + 1)
                if value is None and getattr(field_info, "is_required", lambda: False)():
                    value = "test"
                if value is not None:
                    payload[field_name] = value
            return payload

        if annotation is str:
            return "test"
        if annotation is int:
            return 1
        if annotation is float:
            return 1.0
        if annotation is bool:
            return True
        if annotation is datetime.datetime:
            return datetime.datetime.utcnow().isoformat()
        if annotation is datetime.date:
            return datetime.date.today().isoformat()
        if annotation is datetime.time:
            return "09:00:00"
        if annotation is uuid.UUID:
            return str(uuid.uuid4())

    if annotation in {Any, object}:
        return {}

    return None


def _build_body_from_annotation(annotation: Any) -> dict | None:
    """요청 본문 annotation에서 JSON 샘플 payload를 생성"""
    sample = _sample_value_for_annotation(annotation)
    if sample is None:
        return None
    if isinstance(sample, dict):
        return sample
    return {"value": sample}


def _build_body_for_route(
    matcher: dict,
    schema_samples: dict[str, dict],
    route_samples: dict[str, dict],
) -> dict | None:
    """라우트별 body 샘플을 route > schema > annotation 순으로 선택"""
    route_sample_key = matcher.get("route_sample_key")
    if route_sample_key and route_sample_key in route_samples:
        return copy.deepcopy(route_samples[route_sample_key])

    annotation = matcher.get("body_annotation")
    for schema_key in _schema_sample_keys(annotation):
        if schema_key in schema_samples:
            return copy.deepcopy(schema_samples[schema_key])

    return _build_body_from_annotation(annotation)


def _build_headers_for_route(method: str, auth_token: str | None) -> dict[str, str]:
    """엔드포인트 호출용 기본 헤더를 구성"""
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if method in {"POST", "PUT", "PATCH"}:
        headers["Content-Type"] = "application/json"
    return headers


def _invoke_route_once(
    client: LocustLikeHttpClient,
    method: str,
    path: str,
    headers: dict[str, str],
    json_body: dict | None,
    name: str,
) -> None:
    """단일 라우트를 실행하고 예외를 RequestRecord로 기록"""
    started = perf_counter()
    try:
        client._request(
            method=method,
            path=path,
            headers=headers,
            json_body=json_body,
            name=name,
            catch_response=False,
        )
    except Exception as exc:  # pragma: no cover - runtime guard
        elapsed_ms = (perf_counter() - started) * 1000
        request_path = urlparse(path).path if path else "/"
        client.records.append(
            RequestRecord(
                method=method,
                path=request_path,
                name=name,
                status_code=0,
                elapsed_ms=elapsed_ms,
                failed=True,
                error=str(exc),
            )
        )


def run_all_router_endpoints_once(
    base_url: str,
    run_id: str,
    route_matchers: list[dict],
    schema_samples: dict[str, dict],
    route_samples: dict[str, dict],
    repeat_count: int = ENDPOINT_REPEAT_COUNT,
    warmup_drop_count: int = ENDPOINT_WARMUP_DROPS,
) -> dict:
    """main.py 라우트(GET/POST/PATCH/PUT)를 반복 호출하고 워밍업 요청을 제외"""
    client = LocustLikeHttpClient(base_url=base_url, timeout_sec=10)
    store = StateStore()

    regular_vu = (int(time.time()) % 8000) + 1000
    manager_vu = regular_vu + 1
    regular_state = store.get_or_create(vu_id=regular_vu, run_id=run_id)
    manager_state = store.get_or_create(vu_id=manager_vu, run_id=run_id)

    regular_auth = AuthProvider(client, run_id)
    manager_auth = AuthProvider(client, run_id)
    manager_scenario = ManagerScenario(client, store)
    regular_scenario = UserScenario(client, store)

    # setup 단계: 라우트 실행에 필요한 최소 인증/데이터 준비
    ensure_user_ready(regular_auth, regular_state, "regular")
    ensure_user_ready(manager_auth, manager_state, "manager")
    manager_scenario.ensure_managed_club(manager_state)
    manager_scenario.create_round_meeting(manager_state)
    regular_scenario.join_shared_club(regular_state)
    regular_scenario.apply_shared_meeting(regular_state)

    # setup 트래픽은 본 측정에서 제외
    client.records.clear()
    client.replay_requests.clear()

    path_param_values = _build_path_param_values(regular_state, manager_state)
    auth_token = manager_state.access_token or regular_state.access_token

    started = perf_counter()
    repeats = max(1, int(repeat_count))
    warmup_drops = max(0, min(int(warmup_drop_count), repeats - 1))
    for matcher in sorted(route_matchers, key=lambda x: (x["router"], x["path"], x["method"])):
        method = matcher["method"]
        path = _render_route_path(
            full_path=matcher["full_path"],
            path_param_names=matcher.get("path_param_names", []),
            path_param_values=path_param_values,
            path_param_annotations=matcher.get("path_param_annotations", {}),
        )
        body = None
        if method in {"POST", "PUT", "PATCH"}:
            body = _build_body_for_route(
                matcher=matcher,
                schema_samples=schema_samples,
                route_samples=route_samples,
            )

        headers = _build_headers_for_route(method, auth_token)
        for attempt_index in range(repeats):
            records_len_before = len(client.records)
            replay_len_before = len(client.replay_requests)
            _invoke_route_once(
                client=client,
                method=method,
                path=path,
                headers=headers,
                json_body=body,
                name=f"route.{method} {matcher['path']}",
            )
            if attempt_index < warmup_drops:
                del client.records[records_len_before:]
                del client.replay_requests[replay_len_before:]

    total_elapsed_ms = (perf_counter() - started) * 1000
    request_dicts = [asdict(item) for item in client.records]
    return {
        "requests": request_dicts,
        "replay_requests": client.replay_requests,
        "client": client,
        "route_sweep_elapsed_ms": total_elapsed_ms,
        "route_sweep_count": len(route_matchers),
        "repeat_count": repeats,
        "warmup_drop_count": warmup_drops,
    }


def run_lprof_for_top_endpoints(
    backend_app: Any,
    client: LocustLikeHttpClient,
    endpoint_rows: list[dict],
    replay_requests: list[ReplayRequest],
    route_matchers: list[dict],
    output_dir: Path,
) -> list[dict]:
    """지연 상위 endpoint의 실제 라우터 함수를 lprof 대상으로 리플레이/저장"""
    if not endpoint_rows:
        print("\n[lprof] profile 대상 endpoint 없음")
        return []

    try:
        from line_profiler import LineProfiler
    except ImportError:
        print("\n[lprof] line-profiler 미설치로 skip (pip install -r scripts/loadtest/requirements.txt)")
        return [
            {
                "endpoint": row["endpoint"],
                "status": "skipped",
                "reason": "line_profiler_not_installed",
            }
            for row in endpoint_rows
        ]

    target_keys = {(row["method"], row["path"]) for row in endpoint_rows}
    matcher_by_key: dict[tuple[str, str], dict] = {}
    for matcher in route_matchers:
        key = (matcher["method"], matcher["path"])
        if key not in matcher_by_key:
            matcher_by_key[key] = matcher

    representative_requests: dict[tuple[str, str], ReplayRequest] = {}
    for replay in replay_requests:
        key = _endpoint_key_for_request(replay.method, replay.path, route_matchers)
        if key not in target_keys:
            continue
        selected = representative_requests.get(key)
        if selected is None or replay.elapsed_ms > selected.elapsed_ms:
            representative_requests[key] = replay

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    print(f"\n=== lprof Replay (Targets {len(endpoint_rows)}) ===")
    for index, row in enumerate(endpoint_rows, start=1):
        key = (row["method"], row["path"])
        replay = representative_requests.get(key)
        matcher = matcher_by_key.get(key)
        if replay is None:
            print(f"{index:02d}. {row['endpoint']} -> skip (no replay sample)")
            results.append(
                {
                    "rank": index,
                    "endpoint": row["endpoint"],
                    "status": "skipped",
                    "reason": "no_replay_sample",
                }
            )
            continue

        endpoint_callable = matcher.get("endpoint_callable") if matcher else None
        if not callable(endpoint_callable):
            print(f"{index:02d}. {row['endpoint']} -> skip (router endpoint callable not found)")
            results.append(
                {
                    "rank": index,
                    "endpoint": row["endpoint"],
                    "status": "skipped",
                    "reason": "endpoint_callable_not_found",
                }
            )
            continue

        profiled_function = f"{endpoint_callable.__module__}.{getattr(endpoint_callable, '__name__', 'unknown')}"

        async def _replay_against_asgi() -> httpx.Response:
            """ASGI in-process 재요청으로 실제 라우터 함수를 실행"""
            asgi_transport = httpx.ASGITransport(app=backend_app)
            async with httpx.AsyncClient(
                transport=asgi_transport,
                base_url="http://testserver",
                timeout=client.timeout_sec,
                follow_redirects=False,
            ) as asgi_client:
                method = replay.method.upper()
                path_url = replay.path_url
                request_headers = dict(replay.headers)
                request_path = urlparse(path_url).path or replay.path

                if client._needs_csrf(method, request_path):
                    csrf_resp = await asgi_client.get("/api/v1/auth/csrf-token")
                    if csrf_resp.status_code != 200:
                        raise RuntimeError(f"CSRF token fetch failed during lprof replay: {csrf_resp.status_code}")
                    csrf_payload = csrf_resp.json() or {}
                    csrf_token = csrf_payload.get("csrf_token")
                    if not csrf_token:
                        raise RuntimeError("CSRF token missing during lprof replay")
                    request_headers["X-CSRF-Token"] = csrf_token

                response = await asgi_client.request(
                    method=method,
                    url=path_url,
                    headers=request_headers,
                    json=replay.json_body,
                )

                if response.status_code in {307, 308} and response.headers.get("location"):
                    redirected = response.headers["location"]
                    redirected_path = urlparse(redirected).path or request_path
                    redirected_headers = dict(request_headers)
                    if client._needs_csrf(method, redirected_path):
                        csrf_resp = await asgi_client.get("/api/v1/auth/csrf-token")
                        if csrf_resp.status_code != 200:
                            raise RuntimeError(
                                f"CSRF token fetch for redirect failed during lprof replay: {csrf_resp.status_code}"
                            )
                        csrf_payload = csrf_resp.json() or {}
                        csrf_token = csrf_payload.get("csrf_token")
                        if not csrf_token:
                            raise RuntimeError("Redirect CSRF token missing during lprof replay")
                        redirected_headers["X-CSRF-Token"] = csrf_token
                    response = await asgi_client.request(
                        method=method,
                        url=redirected,
                        headers=redirected_headers,
                        json=replay.json_body,
                    )
                return response

        def _run_replay_sync() -> httpx.Response:
            return asyncio.run(_replay_against_asgi())

        profiler = LineProfiler()
        profiler.add_function(endpoint_callable)
        wrapped_callable = getattr(endpoint_callable, "__wrapped__", None)
        if callable(wrapped_callable):
            profiler.add_function(wrapped_callable)

        status = "ok"
        error = None
        replay_status_code = None
        started = perf_counter()
        try:
            response = profiler.runcall(_run_replay_sync)
            replay_status_code = getattr(response, "status_code", None)
        except Exception as exc:  # pragma: no cover - runtime guard
            status = "error"
            error = str(exc)
        elapsed_ms = (perf_counter() - started) * 1000

        file_stub = _safe_filename(f"{index:02d}_{row['method']}_{row['path']}")
        lprof_path = output_dir / f"{file_stub}.lprof"
        txt_path = output_dir / f"{file_stub}.txt"
        if status == "ok":
            profiler.dump_stats(str(lprof_path))
            with txt_path.open("w", encoding="utf-8") as stat_file:
                profiler.print_stats(stream=stat_file, stripzeros=True)

        print(
            f"{index:02d}. {row['endpoint']} -> {status} "
            f"(replay_status={replay_status_code}, elapsed={elapsed_ms:.2f}ms)"
        )
        results.append(
            {
                "rank": index,
                "endpoint": row["endpoint"],
                "status": status,
                "error": error,
                "replay_status_code": replay_status_code,
                "elapsed_ms": elapsed_ms,
                "profiled_function": profiled_function,
                "lprof_path": str(lprof_path) if status == "ok" else None,
                "txt_path": str(txt_path) if status == "ok" else None,
                "source_replay_elapsed_ms": replay.elapsed_ms,
            }
        )

    return results


@dataclass
class RunOutputPaths:
    run_output_dir: Path
    server_log_path: Path
    output_json_path: Path
    output_plot_path: Path
    lprof_output_dir: Path


def parse_args() -> argparse.Namespace:
    """스크립트 실행 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description=(
            "단일 실행: main.py 라우트 전수 수집 -> 테스트 모드 실행 -> "
            "모든 GET/POST/PATCH/PUT 라우트 10회 호출(1회 워밍업 제외) -> "
            "endpoint latency 그래프 출력"
        )
    )
    parser.add_argument("--port", type=int, default=8210, help="테스트 서버 포트 (기본: 8210)")
    parser.add_argument("--run-id", default=f"single-{int(time.time())}", help="로드테스트 run_id")
    parser.add_argument(
        "--python-exec",
        default=str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        help="main.py 실행용 Python 경로",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="결과 JSON 경로 (미지정 시 output/YYYYMMDD_HHMMSS/single_test_profile_report.json)",
    )
    parser.add_argument(
        "--output-plot",
        default=None,
        help="엔드포인트 지연시간 막대그래프 PNG 경로 (미지정 시 output/YYYYMMDD_HHMMSS/single_test_profile_top20.png)",
    )
    parser.add_argument(
        "--plot-ms-labels",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="막대그래프 각 막대 위에 평균 지연시간(ms)을 정수 라벨로 표시할지 여부 (기본: True)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="지연시간 상위 N개 엔드포인트만 출력/그래프화 (기본: 20)",
    )
    parser.add_argument(
        "--lprof-top-n",
        type=int,
        default=0,
        help="line_profiler 재실행 대상 endpoint 수 (0 이하면 전체, 기본: 0)",
    )
    parser.add_argument(
        "--lprof-dir",
        default=None,
        help="line_profiler 결과(.lprof/.txt) 저장 디렉터리 (미지정 시 output/YYYYMMDD_HHMMSS/lprof)",
    )
    parser.add_argument(
        "--server-log",
        default=None,
        help="테스트 서버 로그 파일 경로 (미지정 시 output/YYYYMMDD_HHMMSS/server.log)",
    )
    return parser.parse_args()


def resolve_run_output_paths(args: argparse.Namespace, run_started_at: datetime.datetime) -> RunOutputPaths:
    """실행 시점과 인자로부터 결과물 경로를 결정한다."""
    run_output_dir = CURRENT_DIR / "output" / run_started_at.strftime("%Y%m%d_%H%M%S")
    server_log_path = Path(args.server_log) if args.server_log else run_output_dir / "server.log"
    output_json_path = Path(args.output_json) if args.output_json else run_output_dir / "single_test_profile_report.json"
    output_plot_path = Path(args.output_plot) if args.output_plot else run_output_dir / "single_test_profile_top20.png"
    lprof_output_dir = Path(args.lprof_dir) if args.lprof_dir else run_output_dir / "lprof"
    return RunOutputPaths(
        run_output_dir=run_output_dir,
        server_log_path=server_log_path,
        output_json_path=output_json_path,
        output_plot_path=output_plot_path,
        lprof_output_dir=lprof_output_dir,
    )


def ensure_output_dirs(paths: RunOutputPaths) -> None:
    """리포트/그래프/로그/lprof 결과 저장 디렉터리를 생성한다."""
    paths.run_output_dir.mkdir(parents=True, exist_ok=True)
    paths.server_log_path.parent.mkdir(parents=True, exist_ok=True)
    paths.output_json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.output_plot_path.parent.mkdir(parents=True, exist_ok=True)
    paths.lprof_output_dir.mkdir(parents=True, exist_ok=True)


def print_endpoint_blacklist() -> None:
    """현재 적용 중인 endpoint 블랙리스트를 콘솔에 출력한다."""
    if not ENDPOINT_BLACKLIST and not ENDPOINT_BLACKLIST_PATH_PREFIXES:
        return

    exact_text = ", ".join(sorted(f"[{method}] {path}" for method, path in ENDPOINT_BLACKLIST))
    prefix_text = ", ".join(sorted(ENDPOINT_BLACKLIST_PATH_PREFIXES))
    if exact_text:
        print(f"Endpoint blacklist exact: {exact_text}")
    if prefix_text:
        print(f"Endpoint blacklist prefixes: {prefix_text}")


def ensure_server_ready(
    base_url: str,
    python_exec: str,
    port: int,
    server_log_path: Path,
) -> tuple[subprocess.Popen | None, Any | None, bool]:
    """health 확인 후 기존 서버를 재사용하거나 테스트 서버를 새로 기동한다."""
    process = None
    log_file = None
    started_server = False

    try:
        response = requests.get(f"{base_url}/health", timeout=1.2)
        if response.status_code == 200:
            print(f"Reusing existing server: {base_url}")
            return process, log_file, started_server
        raise requests.RequestException("health not 200")
    except requests.RequestException:
        process, log_file = start_test_server(
            python_executable=python_exec,
            port=port,
            log_path=server_log_path,
        )
        started_server = True
        wait_until_ready(base_url, process, timeout_sec=80)
        print(f"Started test-mode server: {base_url}")
        return process, log_file, started_server


def select_profile_targets(
    endpoint_coverage: list[dict],
    top_n: int,
    requested_lprof_top_n: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """커버리지 결과에서 출력 상위 endpoint와 lprof 대상을 추린다."""
    executed_endpoints = [row for row in endpoint_coverage if row["hit"]]
    executed_endpoints.sort(key=lambda x: x["avg_ms"], reverse=True)
    endpoint_top_stats = executed_endpoints[:top_n]
    if requested_lprof_top_n <= 0:
        lprof_targets = executed_endpoints
    else:
        lprof_targets = executed_endpoints[:requested_lprof_top_n]
    return executed_endpoints, endpoint_top_stats, lprof_targets


def build_report_payload(
    *,
    base_url: str,
    run_id: str,
    run_result: dict,
    endpoint_stats: list[dict],
    endpoint_coverage: list[dict],
    endpoint_top_stats: list[dict],
    route_catalog: list[dict],
    router_counts: list[dict],
    executed_endpoints: list[dict],
    top_n: int,
    lprof_top_n: int,
    requested_lprof_top_n: int,
    lprof_results: list[dict],
    paths: RunOutputPaths,
    schema_samples: dict[str, dict],
    route_samples: dict[str, dict],
    plot_ms_labels: bool,
) -> dict:
    """JSON 리포트 payload를 조립한다."""
    return {
        "base_url": base_url,
        "run_id": run_id,
        "generated_at_epoch": int(time.time()),
        "route_sweep_count": run_result["route_sweep_count"],
        "route_sweep_elapsed_ms": run_result["route_sweep_elapsed_ms"],
        "route_repeat_count": run_result["repeat_count"],
        "route_warmup_drop_count": run_result["warmup_drop_count"],
        "endpoints": endpoint_top_stats,
        "endpoint_stats_all": endpoint_stats,
        "endpoint_coverage": endpoint_coverage,
        "route_catalog": route_catalog,
        "route_catalog_count": len(route_catalog),
        "router_counts": router_counts,
        "covered_endpoint_count": len(executed_endpoints),
        "top_n": top_n,
        "lprof_top_n": lprof_top_n,
        "requested_lprof_top_n": requested_lprof_top_n,
        "endpoint_blacklist_exact": sorted(
            [{"method": method, "path": path} for method, path in ENDPOINT_BLACKLIST],
            key=lambda x: (x["method"], x["path"]),
        ),
        "endpoint_blacklist_path_prefixes": sorted(ENDPOINT_BLACKLIST_PATH_PREFIXES),
        "lprof_output_dir": str(paths.lprof_output_dir),
        "lprof_results": lprof_results,
        "requests": run_result["requests"],
        "sample_schema_dir": str(SAMPLE_SCHEMA_DIR),
        "sample_route_dir": str(SAMPLE_ROUTE_DIR),
        "sample_schema_count": len(schema_samples),
        "sample_route_count": len(route_samples),
        "plot_path": str(paths.output_plot_path),
        "plot_ms_labels": plot_ms_labels,
        "run_output_dir": str(paths.run_output_dir),
        "server_log": str(paths.server_log_path),
    }


def main() -> int:
    """단일 프로파일링 실행 전체 흐름을 구성하고 결과를 저장"""
    args = parse_args()

    apply_test_env_defaults_for_inprocess(args.port)
    backend_app = load_backend_app_from_main()
    route_catalog, router_endpoints, route_matchers = collect_router_endpoints(backend_app)
    schema_samples = _load_json_samples(SAMPLE_SCHEMA_DIR)
    route_samples = _load_json_samples(SAMPLE_ROUTE_DIR)
    router_counts = [
        {"router": router_name, "endpoint_count": len(entries)}
        for router_name, entries in router_endpoints.items()
    ]
    print_router_endpoint_inventory(router_endpoints)
    print_endpoint_blacklist()
    print(
        f"\nLoaded samples: schemas={len(schema_samples)} ({SAMPLE_SCHEMA_DIR}), "
        f"routes={len(route_samples)} ({SAMPLE_ROUTE_DIR})"
    )

    run_started_at = datetime.datetime.now()
    paths = resolve_run_output_paths(args, run_started_at)
    base_url = f"http://127.0.0.1:{args.port}"
    ensure_output_dirs(paths)
    process, log_file, started_server = ensure_server_ready(
        base_url=base_url,
        python_exec=args.python_exec,
        port=args.port,
        server_log_path=paths.server_log_path,
    )

    try:
        result = run_all_router_endpoints_once(
            base_url=base_url,
            run_id=args.run_id,
            route_matchers=route_matchers,
            schema_samples=schema_samples,
            route_samples=route_samples,
            repeat_count=ENDPOINT_REPEAT_COUNT,
            warmup_drop_count=ENDPOINT_WARMUP_DROPS,
        )
        endpoint_stats = aggregate_endpoint_stats(result["requests"], route_matchers)
        endpoint_coverage = build_endpoint_coverage(route_catalog, endpoint_stats)
        print_coverage_summary(endpoint_coverage)

        top_n = max(1, int(args.top_n))
        requested_lprof_top_n = int(args.lprof_top_n)
        executed_endpoints, endpoint_top_stats, lprof_targets = select_profile_targets(
            endpoint_coverage=endpoint_coverage,
            top_n=top_n,
            requested_lprof_top_n=requested_lprof_top_n,
        )
        lprof_top_n = len(lprof_targets)

        print_route_sweep_summary(
            total_routes=result["route_sweep_count"],
            elapsed_ms=result["route_sweep_elapsed_ms"],
            requests=result["requests"],
        )
        print_endpoint_table(endpoint_top_stats, title=f"Endpoint Latency Summary (Top {len(endpoint_top_stats)})")
        save_endpoint_graph_matplotlib(
            endpoint_top_stats,
            paths.output_plot_path,
            show_ms_labels=bool(args.plot_ms_labels),
        )

        # line_profiler 실행
        lprof_results = run_lprof_for_top_endpoints(
            backend_app=backend_app,
            client=result["client"],
            endpoint_rows=lprof_targets,
            replay_requests=result["replay_requests"],
            route_matchers=route_matchers,
            output_dir=paths.lprof_output_dir,
        )

        payload = build_report_payload(
            base_url=base_url,
            run_id=args.run_id,
            run_result=result,
            endpoint_stats=endpoint_stats,
            endpoint_coverage=endpoint_coverage,
            endpoint_top_stats=endpoint_top_stats,
            route_catalog=route_catalog,
            router_counts=router_counts,
            executed_endpoints=executed_endpoints,
            top_n=top_n,
            lprof_top_n=lprof_top_n,
            requested_lprof_top_n=requested_lprof_top_n,
            lprof_results=lprof_results,
            paths=paths,
            schema_samples=schema_samples,
            route_samples=route_samples,
            plot_ms_labels=bool(args.plot_ms_labels),
        )
        paths.output_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSaved report: {paths.output_json_path}")
        return 0
    finally:
        if started_server and process is not None and log_file is not None:
            stop_server(process, log_file)
            print(f"Stopped test server. log={paths.server_log_path}")


if __name__ == "__main__":
    raise SystemExit(main())
