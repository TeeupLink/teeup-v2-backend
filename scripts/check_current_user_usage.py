"""current_user 를 잘못된 모양으로 쓰는 엔드포인트를 찾는다.

배경:
    get_current_user / get_current_active_user / get_current_user_allow_both 는
    User(또는 Admin) **ORM 객체**를 돌려준다. dict 가 아니다.
    반면 백오피스의 get_admin_user 는 dict 를 돌려준다.
    ORM 객체에 .get() 을 호출하면 AttributeError 가 나고,
    라우터의 `except Exception` 에 삼켜져 500 "서버 내부 오류" 로만 보인다.
    (2026-10-24: routers/meetings/expenses.py 전 엔드포인트가 이 이유로 500)

사용법:
    python scripts/check_current_user_usage.py      # 문제가 있으면 종료코드 1

제외:
    `isinstance(current_user, dict)` 로 감싼 방어 코드는 정상이므로 세지 않는다.
"""
import ast, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORM_DEPS = {"get_current_user", "get_current_active_user", "get_current_user_allow_both"}
DICT_DEPS = {"get_admin_user"}


def dep_of(default):
    if (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
            and default.func.id == "Depends" and default.args):
        a = default.args[0]
        if isinstance(a, ast.Name):
            return a.id
        if isinstance(a, ast.Attribute):
            return a.attr
    return None


def is_dict_guard(test):
    """isinstance(current_user, dict) 인지."""
    return (isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
            and test.func.id == "isinstance" and len(test.args) == 2
            and isinstance(test.args[0], ast.Name) and test.args[0].id == "current_user"
            and isinstance(test.args[1], ast.Name) and test.args[1].id == "dict")


def guarded_nodes(fn):
    """dict 가드 안에 있는 노드 집합 (삼항식의 참 가지, if 문의 본문)."""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.IfExp) and is_dict_guard(node.test):
            out |= {id(n) for n in ast.walk(node.body)}
        elif isinstance(node, ast.If) and is_dict_guard(node.test):
            for stmt in node.body:
                out |= {id(n) for n in ast.walk(stmt)}
    return out


findings, guarded_ok, dict_ok = [], 0, 0
for dirpath, _, files in os.walk(os.path.join(ROOT, "routers")):
    if "__pycache__" in dirpath:
        continue
    for f in sorted(files):
        if not f.endswith(".py"):
            continue
        path = os.path.join(dirpath, f)
        rel = os.path.relpath(path, ROOT)
        tree = ast.parse(open(path).read(), path)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = fn.args
            params = a.args + a.kwonlyargs
            defaults = ([None] * (len(a.args) - len(a.defaults)) + list(a.defaults)
                        + list(a.kw_defaults))
            dep = None
            for p, d in zip(params, defaults):
                if p.arg == "current_user" and d is not None:
                    dep = dep_of(d)
            if dep is None:
                continue

            safe = guarded_nodes(fn)
            bad = []
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "current_user"):
                    if id(node) in safe:
                        guarded_ok += 1
                    else:
                        bad.append(node.lineno)
            if not bad:
                continue
            if dep in ORM_DEPS:
                findings.append((rel, fn.name, dep, sorted(bad)))
            elif dep in DICT_DEPS:
                dict_ok += 1

if findings:
    print(f"가드 없이 ORM 객체를 .get() 하는 엔드포인트: {len(findings)}개")
    print("(호출되면 AttributeError → except Exception → 500)\n")
    for rel, name, dep, lines in findings:
        print(f"  {rel}  {name}()  dep={dep}  줄={lines}")
else:
    print("가드 없이 ORM 객체를 .get() 하는 엔드포인트: 없음")
print(f"\n(참고) isinstance 가드로 보호된 호출 {guarded_ok}개, "
      f"get_admin_user(dict) 와 짝지어진 정상 사용 {dict_ok}개")
sys.exit(1 if findings else 0)
