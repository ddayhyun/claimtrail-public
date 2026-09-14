"""캐시 경계. 지문이 못 보는 입력이 바뀌었는데 이전 PASS 를 재사용하는 일을 막는다.

세 층을 본다. ignored 입력 파일(.env), 실행 환경(인터프리터·설치 패키지),
세션. 셋 중 하나라도 다르면 cached_pass 가 아니다. 독립 검증에서 ignored
.env 만 바꿨는데 cached_pass 가 나오는 false PASS 가 실제로 재현됐다.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from claimtrail import cli, hookrun
from claimtrail.detect import Detection, detect_pytest
from claimtrail.hookrun import run
from claimtrail.hookscan import fingerprint, list_files, parse_watch
from claimtrail.hookstate import SCHEMA_VERSION, load_state, state_dir_for
from claimtrail.runners.base import PASS, RunResult


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    """ignored .env 가 있는 git 프로젝트. .env.example 은 tracked 다."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="p"\n', encoding="utf-8")
    (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n.env.*\n!.env.example\nlocal/\n", encoding="utf-8")
    (root / ".env").write_text("MODE=GOOD\n", encoding="utf-8")
    (root / ".env.example").write_text("MODE=\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    return {"CLAIMTRAIL_STATE_DIR": str(tmp_path / "state")}


def stop(root: Path, session: str = "sess-1", **over) -> str:
    payload = {
        "hook_event_name": "Stop",
        "session_id": session,
        "cwd": str(root),
        "stop_hook_active": False,
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False)


def sd_of(root: Path, env: dict[str, str]) -> Path:
    return state_dir_for(root, Path(env["CLAIMTRAIL_STATE_DIR"]))


def log_of(root: Path, env: dict[str, str]) -> str:
    return (sd_of(root, env) / "hook.log").read_text(encoding="utf-8")


def fake_execute(verdict: str = PASS):
    def _run(root, timeout, deadline=None, detections=None):
        dets = (
            detections
            if detections is not None
            else [Detection(kind="pytest", found=True, signals=["s"])]
        )
        res = [RunResult(kind="pytest", status=verdict, command=["pytest"], exit_code=0)]
        return dets, res

    return _run


@pytest.fixture(autouse=True)
def _no_real_verification(monkeypatch):
    monkeypatch.setattr(hookrun, "execute", fake_execute(PASS))


# --- 1. ignored 입력 파일 -----------------------------------------------------


def test_ignored_env가_바뀌면_이전_PASS를_재사용하지_않는다(repo: Path, env):
    """독립 검증에서 재현된 false PASS. .env 는 테스트가 읽는 실제 입력이다."""
    first = run(stop(repo), env)
    assert first.action == "run" and first.exit_code == 0

    (repo / ".env").write_text("MODE=BAD\n", encoding="utf-8")
    second = run(stop(repo), env)

    assert second.action == "run", "ignored 입력이 바뀌었는데 건너뛰었다"
    assert "cached_pass" not in log_of(repo, env).splitlines()[-1]


def test_같은_세션_같은_환경_같은_입력이면_건너뛴다(repo: Path, env):
    """캐시가 아예 죽은 것이 아님을 확인한다."""
    assert run(stop(repo), env).action == "run"
    second = run(stop(repo), env)
    assert second.action == "skip"
    assert second.reason_code == "cached_pass"


def test_기본으로_루트의_env_파일을_지문에_넣는다(repo: Path):
    policy = parse_watch(None)
    a = fingerprint(repo, policy)
    (repo / ".env").write_text("MODE=BAD\n", encoding="utf-8")
    b = fingerprint(repo, policy)
    assert a.ok and b.ok
    assert a.digest != b.digest


def test_루트의_env_변형도_기본에_든다(repo: Path):
    policy = parse_watch(None)
    a = fingerprint(repo, policy)
    (repo / ".env.local").write_text("X=1\n", encoding="utf-8")
    b = fingerprint(repo, policy)
    assert a.digest != b.digest


def test_하위_폴더의_env는_기본에_없다(repo: Path):
    """기본 범위는 루트뿐이다. 넓히면 어디까지 읽는지 예측할 수 없다."""
    policy = parse_watch(None)
    (repo / "local").mkdir()
    (repo / "local" / ".env").write_text("A=1\n", encoding="utf-8")
    a = fingerprint(repo, policy)
    (repo / "local" / ".env").write_text("A=2\n", encoding="utf-8")
    b = fingerprint(repo, policy)
    assert a.digest == b.digest


def test_include_ignored로_지정한_파일이_지문에_들어간다(repo: Path):
    policy = parse_watch('{"include_ignored": ["local/settings.json"]}')
    (repo / "local").mkdir()
    (repo / "local" / "settings.json").write_text("{}", encoding="utf-8")
    a = fingerprint(repo, policy)
    (repo / "local" / "settings.json").write_text('{"k": 1}', encoding="utf-8")
    b = fingerprint(repo, policy)
    assert a.digest != b.digest


def test_include_ignored_파일이_없어도_부재로_기록한다(repo: Path):
    """없던 파일이 생기는 것도 입력 변화다."""
    policy = parse_watch('{"include_ignored": ["local/x.toml"]}')
    a = fingerprint(repo, policy)
    (repo / "local").mkdir()
    (repo / "local" / "x.toml").write_text("a = 1\n", encoding="utf-8")
    b = fingerprint(repo, policy)
    assert a.ok and b.ok
    assert a.digest != b.digest


@pytest.mark.parametrize(
    "bad", ['["../x"]', '["/etc/passwd"]', '["C:/x"]', '["."]', '[""]']
)
def test_include_ignored는_루트_밖과_전체를_거부한다(bad: str):
    with pytest.raises(ValueError):
        parse_watch('{"include_ignored": ' + bad + "}")


def test_include_ignored는_다른_키와_함께_쓸_수_있다():
    p = parse_watch('{"add": ["docs"], "include_ignored": [".secrets"]}')
    assert p.extra == ("docs",)
    assert p.include_ignored == (".secrets",)


def test_include_ignored는_policy_hash에_들어간다():
    a = parse_watch(None).policy_hash()
    b = parse_watch('{"include_ignored": ["x"]}').policy_hash()
    assert a != b


def test_추적_파일은_두_번_세지_않는다(repo: Path):
    from claimtrail.hookscan import ignored_inputs

    policy = parse_watch(None)
    listed = set(list_files(repo, policy).names)
    assert ".env.example" in listed
    extra = ignored_inputs(repo, policy, listed)
    assert ".env" in extra
    assert ".env.example" not in extra


NO_SYMLINK = pytest.mark.skipif(
    os.name == "nt", reason="Windows 에서는 symlink 생성에 권한이 필요하다"
)


@NO_SYMLINK
def test_루트_밖을_가리키는_symlink는_캐시_근거가_되지_않는다(repo: Path, tmp_path: Path):
    """독립 검증에서 재현된 false PASS. 링크 문자열만 해시하면 대상 내용
    변화를 놓치는데, pytest 는 링크를 따라 실제 내용을 읽는다.
    루트 밖은 읽지 않는다 -- 대신 그 지문으로 건너뛰지도 않는다."""
    outside = tmp_path / "outside.env"
    outside.write_text("MODE=GOOD\n", encoding="utf-8")
    (repo / ".env").unlink()
    os.symlink(outside, repo / ".env")
    fp = fingerprint(repo, parse_watch(None))
    assert not fp.cacheable
    assert ".env" in fp.uncacheable


@NO_SYMLINK
def test_외부_symlink가_있으면_같은_세션_재호출도_cached_pass가_아니다(
    repo: Path, tmp_path: Path, env
):
    outside = tmp_path / "outside.env"
    outside.write_text("MODE=GOOD\n", encoding="utf-8")
    (repo / ".env").unlink()
    os.symlink(outside, repo / ".env")

    first = run(stop(repo), env)
    assert first.action == "run" and first.exit_code == 0, "검증 자체는 돼야 한다"
    outside.write_text("MODE=BAD\n", encoding="utf-8")
    second = run(stop(repo), env)
    assert second.action == "run", "외부 링크 대상이 바뀌었는데 건너뛰었다"
    third = run(stop(repo), env)
    assert third.action == "run", "외부 링크가 있으면 변화가 없어도 건너뛰지 않는다"
    assert "uncacheable_input" in log_of(repo, env)


@NO_SYMLINK
def test_루트_안_파일을_가리키는_symlink는_대상_내용까지_해시한다(repo: Path):
    """안쪽 링크는 따라가도 안전하다. 그래서 내용 변화를 잡고 캐시도 된다."""
    (repo / "local").mkdir()
    real = repo / "local" / "real.env"  # local/ 은 ignored 라 목록에 없다
    real.write_text("A=1\n", encoding="utf-8")
    (repo / ".env").unlink()
    os.symlink(Path("local") / "real.env", repo / ".env")
    policy = parse_watch(None)
    a = fingerprint(repo, policy)
    real.write_text("A=2\n", encoding="utf-8")
    b = fingerprint(repo, policy)
    assert a.cacheable and b.cacheable
    assert a.ok and b.ok and a.digest != b.digest


@NO_SYMLINK
def test_깨진_링크와_디렉터리_링크는_캐시_근거가_되지_않는다(repo: Path):
    os.symlink(Path("없는-파일"), repo / "src" / "broken.py")
    os.symlink(Path("src"), repo / "srclink")
    fp = fingerprint(repo, parse_watch(None))
    assert fp.ok, fp.reason
    assert not fp.cacheable
    assert {"src/broken.py", "srclink"} <= set(fp.uncacheable)


# --- 2b. Node 설치 상태 -------------------------------------------------------


def test_npm_프로젝트는_설치_상태_파일이_없으면_캐시를_포기한다(tmp_path: Path, monkeypatch):
    """node_modules 는 파일 지문에서 빠진다. 설치 상태를 모르면 캐시하지 않는다."""
    from claimtrail import hookcontext

    monkeypatch.setattr(hookcontext.shutil, "which", lambda tool: f"/fake/bin/{tool}")
    monkeypatch.setattr(hookcontext, "_tool_output", lambda cmd, *a, **k: "v1")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    r = hookcontext.execution_context(tmp_path, [Detection(kind="npm test", found=True)])
    assert not r.ok
    assert "package-lock" in r.reason


def test_npm_설치_상태가_바뀌면_환경_지문이_바뀐다(tmp_path: Path, monkeypatch):
    from claimtrail import hookcontext

    monkeypatch.setattr(hookcontext.shutil, "which", lambda tool: f"/fake/bin/{tool}")
    monkeypatch.setattr(hookcontext, "_tool_output", lambda cmd, *a, **k: "v1")
    lock = tmp_path / "node_modules" / ".package-lock.json"
    lock.parent.mkdir()
    lock.write_text('{"packages": {"a": "1"}}', encoding="utf-8")
    dets = [Detection(kind="npm test", found=True)]
    a = hookcontext.execution_context(tmp_path, dets)
    lock.write_text('{"packages": {"a": "2"}}', encoding="utf-8")
    b = hookcontext.execution_context(tmp_path, dets)
    assert a.ok and b.ok
    assert a.digest != b.digest


def _npm_env(monkeypatch):
    from claimtrail import hookcontext

    monkeypatch.setattr(hookcontext.shutil, "which", lambda tool: f"/fake/bin/{tool}")
    monkeypatch.setattr(hookcontext, "_tool_output", lambda cmd, *a, **k: "v1")
    return hookcontext


def test_npm_설치_상태_파일이_루트_안_일반_파일이면_해시한다(tmp_path: Path, monkeypatch):
    hookcontext = _npm_env(monkeypatch)
    lock = tmp_path / "node_modules" / ".package-lock.json"
    lock.parent.mkdir()
    lock.write_text("{}", encoding="utf-8")
    r = hookcontext.execution_context(tmp_path, [Detection(kind="npm test", found=True)])
    assert r.ok, r.reason


@NO_SYMLINK
def test_npm_설치_상태_파일이_루트_밖_symlink면_캐시를_포기한다(tmp_path: Path, monkeypatch):
    """지문에 넣는 파일도 루트 밖은 읽지 않는다. 읽지 않았으면 캐시하지 않는다."""
    hookcontext = _npm_env(monkeypatch)
    root = tmp_path / "proj"
    (root / "node_modules").mkdir(parents=True)
    outside = tmp_path / "outside-lock.json"
    outside.write_text("{}", encoding="utf-8")
    os.symlink(outside, root / "node_modules" / ".package-lock.json")
    r = hookcontext.execution_context(root, [Detection(kind="npm test", found=True)])
    assert not r.ok
    assert "package-lock" in r.reason


@NO_SYMLINK
def test_node_modules_자체가_루트_밖_symlink면_캐시를_포기한다(tmp_path: Path, monkeypatch):
    hookcontext = _npm_env(monkeypatch)
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "shared-node_modules"
    outside.mkdir()
    (outside / ".package-lock.json").write_text("{}", encoding="utf-8")
    os.symlink(outside, root / "node_modules", target_is_directory=True)
    r = hookcontext.execution_context(root, [Detection(kind="npm test", found=True)])
    assert not r.ok
    assert "package-lock" in r.reason


@pytest.mark.skipif(
    __import__("shutil").which("node") is None or __import__("shutil").which("npm") is None,
    reason="node/npm 이 없다",
)
def test_실제_node와_npm을_probe한다(tmp_path: Path):
    """가짜 없이 진짜 node/npm 을 부른다. Windows 의 npm.cmd 도 여기서 돈다."""
    from claimtrail import hookcontext

    lock = tmp_path / "node_modules" / ".package-lock.json"
    lock.parent.mkdir()
    lock.write_text("{}", encoding="utf-8")
    r = hookcontext.execution_context(tmp_path, [Detection(kind="npm test", found=True)])
    assert r.ok, r.reason


# --- 2. 실행 환경 ------------------------------------------------------------


def _ctx(digest, reason=""):
    from claimtrail.hookcontext import ContextFingerprint

    return ContextFingerprint(digest, reason)


def test_실행_환경이_바뀌면_이전_PASS를_재사용하지_않는다(repo: Path, env, monkeypatch):
    values = ["ctx-1", "ctx-2"]
    monkeypatch.setattr(hookrun, "execution_context", lambda root, dets: _ctx(values.pop(0)))
    assert run(stop(repo), env).action == "run"
    assert run(stop(repo), env).action == "run"


def test_환경_지문을_못_구하면_캐시만_포기하고_검증은_한다(repo: Path, env, monkeypatch):
    monkeypatch.setattr(hookrun, "execution_context", lambda root, dets: _ctx(None, "boom"))
    first = run(stop(repo), env)
    assert first.action == "run" and first.exit_code == 0
    second = run(stop(repo), env)
    assert second.action == "run" and second.exit_code == 0
    assert "context_unavailable" in log_of(repo, env)


def test_실행_환경_지문은_같은_환경에서_안정적이다(tmp_path: Path):
    from claimtrail.hookcontext import execution_context

    dets = [Detection(kind="pytest", found=True)]
    a = execution_context(tmp_path, dets)
    b = execution_context(tmp_path, dets)
    assert a.ok and a.digest == b.digest


def test_설치된_패키지가_바뀌면_환경_지문이_바뀐다(tmp_path: Path, monkeypatch):
    from claimtrail import hookcontext

    class _Dist:
        def __init__(self, name, version):
            self.metadata = {"Name": name}
            self.version = version

    dets = [Detection(kind="pytest", found=True)]
    monkeypatch.setattr(hookcontext, "distributions", lambda: [_Dist("pytest", "8.0.0")])
    a = hookcontext.execution_context(tmp_path, dets)
    monkeypatch.setattr(hookcontext, "distributions", lambda: [_Dist("pytest", "8.1.0")])
    b = hookcontext.execution_context(tmp_path, dets)
    assert a.digest != b.digest


def test_npm_프로젝트일_때만_node를_본다(tmp_path: Path, monkeypatch):
    from claimtrail import hookcontext

    calls: list[str] = []

    def probe(cmd, *a, **k):
        calls.append(cmd[0])
        return "v1"

    monkeypatch.setattr(hookcontext, "_tool_output", probe)
    # 이 PC 에 node 가 있든 없든 같은 결과여야 한다. which 를 고정한다.
    monkeypatch.setattr(hookcontext.shutil, "which", lambda tool: f"/fake/bin/{tool}")
    hookcontext.execution_context(tmp_path, [Detection(kind="pytest", found=True)])
    assert calls == []
    hookcontext.execution_context(tmp_path, [Detection(kind="npm test", found=True)])
    assert [Path(c).name for c in calls] == ["node", "npm"]


def test_환경_지문_계산_예외는_None과_이유로_돌아온다(tmp_path: Path, monkeypatch):
    from claimtrail import hookcontext

    def boom():
        raise RuntimeError("metadata 깨짐")

    monkeypatch.setattr(hookcontext, "distributions", boom)
    r = hookcontext.execution_context(tmp_path, [Detection(kind="pytest", found=True)])
    assert not r.ok and r.digest is None
    assert "metadata" in r.reason


# --- 3. 세션 ----------------------------------------------------------------


def test_다른_세션의_PASS는_재사용하지_않는다(repo: Path, env):
    """새 세션의 첫 Stop 은 항상 다시 검증한다. 며칠 전 PASS 를 믿지 않는다."""
    assert run(stop(repo, session="sess-1"), env).action == "run"
    second = run(stop(repo, session="sess-2"), env)
    assert second.action == "run"


# --- 4. 스위치와 스키마 ------------------------------------------------------


def test_CLAIMTRAIL_CACHE_0이면_항상_검증한다(repo: Path, env):
    env = {**env, "CLAIMTRAIL_CACHE": "0"}
    assert run(stop(repo), env).action == "run"
    assert run(stop(repo), env).action == "run"
    assert "cache_disabled" in log_of(repo, env)


def test_schema_2_상태는_한_번_재검증하고_3으로_바꾼다(repo: Path, env):
    assert SCHEMA_VERSION == 3
    assert run(stop(repo), env).action == "run"
    path = sd_of(repo, env) / "state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema"] = 2
    data.pop("execution_context", None)
    data.pop("verified_session_id", None)
    path.write_text(json.dumps(data), encoding="utf-8")

    assert run(stop(repo), env).action == "run", "schema 2 를 캐시 근거로 썼다"
    st = load_state(sd_of(repo, env))
    assert st is not None and st.schema == 3


# --- 5. 탐지는 한 번 ---------------------------------------------------------


def test_탐지는_한_번만_실행하고_execute에_넘긴다(repo: Path, env, monkeypatch):
    count = {"n": 0}
    seen: dict[str, object] = {"dets": None}

    def counted(root):
        count["n"] += 1
        return [Detection(kind="pytest", found=True, signals=["s"])]

    def execute(root, timeout, deadline=None, detections=None):
        seen["dets"] = detections
        return detections, [
            RunResult(kind="pytest", status=PASS, command=["pytest"], exit_code=0)
        ]

    monkeypatch.setattr(hookrun, "detect_all", counted)
    monkeypatch.setattr(hookrun, "execute", execute)
    assert run(stop(repo), env).action == "run"
    assert count["n"] == 1
    assert seen["dets"] is not None


def test_execute는_주어진_탐지를_다시_하지_않는다(tmp_path: Path, monkeypatch):
    def boom(root):
        raise AssertionError("탐지를 다시 했다")

    monkeypatch.setattr(cli, "detect_all", boom)
    given = [Detection(kind="pytest", found=False, reason="없음")]
    dets, results = cli.execute(tmp_path, 10, detections=given)
    assert dets is given and results == []


# --- 6. *_test.py ------------------------------------------------------------


def test_star_test_py만_있어도_pytest를_탐지한다(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "feature_test.py").write_text(
        "def test_a():\n    pass\n", encoding="utf-8"
    )
    d = detect_pytest(tmp_path)
    assert d.found, d.reason
