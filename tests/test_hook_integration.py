"""shell 통합 시나리오를 pytest 에 연결한다.

CI 워크플로를 고치지 않고도 리눅스 CI 에서 자동으로 돌게 하려는 것이다.
별도 CI 스텝을 두면 그 스텝을 지우거나 조건을 잘못 걸었을 때 조용히 안 돌게
된다 -- 실행 보장을 한 곳에 모은다.

실패하면 shell 의 stdout·stderr 를 전부 assertion 메시지에 실어, CI 로그만
보고도 어느 시나리오가 깨졌는지 알 수 있게 한다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "hook_integration.sh"
ROOT = Path(__file__).resolve().parents[1]

# 실제 검증(pytest 하위 실행)이 여러 번 돈다. 넉넉히 잡되 무한은 아니다.
TIMEOUT_SEC = 600


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 가 없다")
def test_shell_통합_시나리오가_모두_통과한다():
    env = dict(os.environ)
    env["CLAIMTRAIL_PYTHON"] = sys.executable
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [_bash(), str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SEC,
        env=env,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, (
        "shell 통합 테스트가 실패했다.\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )


def _code_lines(path: Path) -> list[str]:
    """주석을 뺀 실행 줄만.

    설명에 이름이 나오는 것과 실제로 쓰는 것은 다르다. 문자열만 찾으면
    "임의 경로 rm -rf 없음" 같은 자기 주석에 스스로 걸린다 -- 실제로 걸렸다.
    """
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_통합_스크립트가_안전_규칙을_지킨다():
    """임의 경로를 지우는 스크립트를 CI 에서 돌리면 안 된다."""
    lines = _code_lines(SCRIPT)
    body = "\n".join(lines)
    assert "mktemp -d" in body
    assert "trap 'rm -rf \"$TMPROOT\"' EXIT" in body
    for line in lines:
        if "rm -rf" in line:
            assert "$TMPROOT" in line, f"임의 경로를 지운다: {line.strip()}"


def test_훅_래퍼가_판단하지_않는다():
    """판단이 shell 로 새면 테스트할 수 없는 코드가 다시 생긴다."""
    body = "\n".join(_code_lines(ROOT / ".claude" / "claimtrail-hook.sh"))
    for forbidden in ("find ", "-newer", "sha256sum", "date +%s%N"):
        assert forbidden not in body, f"래퍼가 여전히 {forbidden!r} 을 쓴다"
    assert "claimtrail.hookrun" in body


# ============================================================================
# 래퍼의 종료 코드·판별·probe 순서
# ============================================================================
#
# 래퍼는 얇지만 세 가지 판단을 한다 -- 어떤 Python 을 쓸지, 재호출인지,
# 어느 claimtrail 을 부를지. 셋 다 틀리면 조용히 잘못된 코드가 나간다.

HOOK = ROOT / ".claude" / "claimtrail-hook.sh"


def _bash() -> str:
    """Git Bash 의 경로. 이름 "bash" 로 부르지 않는다.

    Windows 의 CreateProcess 는 PATH 보다 System32 를 먼저 보고, 거기에는
    WSL 런처 bash.exe 가 있다. 배포판이 없으면 UTF-16 안내문만 찍고 1 로
    끝난다 -- Windows CI 에서 실제로 그랬다.
    """
    found = shutil.which("bash")
    assert found, "bash 가 없다"
    msg = f"WSL 런처가 잡혔다. Git Bash 가 PATH 앞에 있어야 한다: {found}"
    assert "system32" not in found.lower(), msg
    return found


def _fake_python(tmp_path: Path, exit_code: int) -> Path:
    """-m claimtrail.hookrun 만 정해진 코드로 끝내고, 나머지는 진짜에 넘긴다."""
    script = tmp_path / "fake-python.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-m" ]; then exit ' + str(exit_code) + "; fi\n"
        f'exec "{Path(sys.executable).as_posix()}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run_hook(payload: str, env_over: dict, cwd: Path | None = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.update(env_over)
    return subprocess.run(
        [_bash(), str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
        cwd=str(cwd or ROOT),
    )


def _stop(active: bool, cwd: Path) -> str:
    flag = "true" if active else "false"
    return (
        '{"hook_event_name":"Stop","session_id":"w","cwd":"'
        + cwd.as_posix()
        + '","stop_hook_active":'
        + flag
        + "}"
    )


@pytest.fixture()
def wrapper_env(tmp_path: Path) -> dict:
    return {"CLAIMTRAIL_STATE_DIR": str(tmp_path / "state")}


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 가 없다")
class TestWrapperExitCodes:
    """1. Python 프로세스의 0/2 만 전달하고 그 외는 hook_process_failed"""

    @pytest.mark.parametrize("code", [0, 2])
    def test_0과_2는_그대로_전달한다(self, tmp_path: Path, wrapper_env: dict, code: int):
        fake = _fake_python(tmp_path, code)
        r = _run_hook(_stop(False, tmp_path), {**wrapper_env, "CLAIMTRAIL_PYTHON": str(fake)})
        assert r.returncode == code, r.stderr

    @pytest.mark.parametrize("code", [1, 3, 7, 127])
    def test_그_외_코드는_최초_호출에서_2다(self, tmp_path: Path, wrapper_env: dict, code: int):
        fake = _fake_python(tmp_path, code)
        r = _run_hook(_stop(False, tmp_path), {**wrapper_env, "CLAIMTRAIL_PYTHON": str(fake)})
        assert r.returncode == 2, r.stderr
        assert "hook_process_failed" in r.stderr

    @pytest.mark.parametrize("code", [1, 3, 7, 127])
    def test_그_외_코드는_재호출에서_0이다(self, tmp_path: Path, wrapper_env: dict, code: int):
        """고장 난 훅이 재호출마다 2 를 내면 무한 루프다."""
        fake = _fake_python(tmp_path, code)
        r = _run_hook(_stop(True, tmp_path), {**wrapper_env, "CLAIMTRAIL_PYTHON": str(fake)})
        assert r.returncode == 0, r.stderr
        assert "hook_process_failed" in r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 가 없다")
class TestWrapperRehookDetection:
    """2. stop_hook_active 는 표준 JSON 파서로만 판별한다"""

    def test_Python을_못_찾으면_재호출이어도_2다(self, tmp_path: Path, wrapper_env: dict):
        """파서가 없으면 재호출인지 알 수 없다. 모르면 알린다."""
        r = _run_hook(
            _stop(True, tmp_path),
            {**wrapper_env, "CLAIMTRAIL_PYTHON": str(tmp_path / "없는-python")},
        )
        assert r.returncode == 2, r.stderr
        assert "no_python" in r.stderr

    def test_glob이_속을_문자열도_정확히_판별한다(self, tmp_path: Path, wrapper_env: dict):
        """값이 문자열 "true" 이거나 다른 키에 true 가 있어도 재호출이 아니다."""
        fake = _fake_python(tmp_path, 7)
        tricky = (
            '{"hook_event_name":"Stop","session_id":"w","cwd":"'
            + tmp_path.as_posix()
            + '","stop_hook_active":"true","other":{"stop_hook_active":true}}'
        )
        r = _run_hook(tricky, {**wrapper_env, "CLAIMTRAIL_PYTHON": str(fake)})
        assert r.returncode == 2, "문자열 true 는 재호출이 아니다"

    def test_진짜_true만_재호출이다(self, tmp_path: Path, wrapper_env: dict):
        fake = _fake_python(tmp_path, 7)
        r = _run_hook(_stop(True, tmp_path), {**wrapper_env, "CLAIMTRAIL_PYTHON": str(fake)})
        assert r.returncode == 0

    def test_래퍼에_glob_판별이_없다(self):
        body = "\n".join(_code_lines(HOOK))
        assert '*"stop_hook_active"' not in body
        assert "stop_hook_active\"'*" not in body


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 가 없다")
class TestWrapperSourcePreference:
    """3. 인접한 src 를 설치본보다 우선하고 claimtrail.hookrun 자체를 probe"""

    def test_오래된_설치본이_있어도_인접_src를_쓴다(self, tmp_path: Path, wrapper_env: dict):
        """hookrun 이 없는 옛 claimtrail 이 sys.path 앞에 있어도 src 가 이겨야 한다."""
        stale = tmp_path / "stale-site"
        (stale / "claimtrail").mkdir(parents=True)
        (stale / "claimtrail" / "__init__.py").write_text(
            '__version__ = "0.0.1-stale"\n', encoding="utf-8"
        )
        proj = tmp_path / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (proj / "tests" / "test_ok.py").write_text("def test_ok(): pass\n", encoding="utf-8")

        env = {
            **wrapper_env,
            "CLAIMTRAIL_PYTHON": sys.executable,
            "CLAIMTRAIL_HOOK_DEBUG": "1",
            # 옛 설치본을 PYTHONPATH 맨 앞에 둔다. _run_hook 이 src 를 그 앞에 붙이지 않게
            # 여기서는 PYTHONPATH 를 통째로 덮어쓴다.
            "PYTHONPATH": str(stale),
        }
        r = subprocess.run(
            [_bash(), str(HOOK)],
            input=_stop(False, proj),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, **env},
            cwd=str(ROOT),
        )
        assert "claimtrail_not_importable" not in r.stderr, r.stderr
        assert "source=" in r.stderr, "디버그 줄이 없다"
        assert (ROOT / "src").as_posix() in r.stderr.replace("\\", "/"), r.stderr
        assert r.returncode in (0, 2), r.stderr

    @pytest.mark.skipif(os.name != "nt", reason="Windows 의 로컬 .venv 분기")
    def test_Windows에서_인접_venv와_src를_고른다(self, tmp_path: Path, wrapper_env: dict):
        """래퍼 옆에 .venv 와 src 가 있으면 그것을 고른다.

        저장소의 .venv 에 기대지 않는다 -- CI 체크아웃에는 없다. 래퍼를 임시
        폴더로 복사하고 그 옆에 진짜 venv 와 src 를 만든다.
        """
        here = tmp_path / "tool"
        (here / ".claude").mkdir(parents=True)
        hook = here / ".claude" / HOOK.name
        shutil.copy(HOOK, hook)
        shutil.copytree(ROOT / "src", here / "src")
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(here / ".venv")],
            check=True,
            capture_output=True,
            timeout=120,
        )
        assert (here / ".venv" / "Scripts" / "python.exe").is_file()

        env = {**wrapper_env, "CLAIMTRAIL_HOOK_DEBUG": "1"}
        env.pop("CLAIMTRAIL_PYTHON", None)
        r = subprocess.run(
            [_bash(), str(hook)],
            input='{"hook_event_name":"PreToolUse","session_id":"w","cwd":"x","stop_hook_active":false}',
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env={k: v for k, v in {**os.environ, **env}.items() if k != "CLAIMTRAIL_PYTHON"},
            cwd=str(ROOT),
        )
        out = r.stderr.replace("\\", "/")
        venv_py = (here / ".venv" / "Scripts" / "python.exe").as_posix().lower()
        assert venv_py in out.lower(), r.stderr
        assert "source=" in out and (here / "src").as_posix().lower() in out.lower(), r.stderr

    def test_probe_대상은_hookrun이다(self):
        body = "\n".join(_code_lines(HOOK))
        assert "import claimtrail.hookrun" in body
        assert 'import claimtrail"' not in body, "패키지만 probe 하면 옛 설치본에 속는다"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash 가 없다")
class TestWrapperCwdShadowing:
    """python -m 은 cwd 를 sys.path[0] 에 둔다. 대상 저장소의 claimtrail/ 이 src 를 가린다."""

    def test_대상_cwd의_가짜_claimtrail이_src를_가리지_않는다(
        self, tmp_path: Path, wrapper_env: dict
    ):
        """Claude Code 는 프로젝트 폴더를 cwd 로 훅을 부른다. 거기 claimtrail/ 이
        있으면 -m 이 그것을 먼저 찾아 hookrun 이 없다고 죽는다 -- 재현됐다."""
        proj = tmp_path / "proj"
        (proj / "claimtrail").mkdir(parents=True)
        (proj / "claimtrail" / "__init__.py").write_text('__version__ = "FAKE"\n', encoding="utf-8")
        (proj / "tests").mkdir()
        (proj / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (proj / "tests" / "test_ok.py").write_text("def test_ok(): pass\n", encoding="utf-8")

        r = _run_hook(
            _stop(False, proj),
            {**wrapper_env, "CLAIMTRAIL_PYTHON": sys.executable, "CLAIMTRAIL_HOOK_DEBUG": "1"},
            cwd=proj,  # 실제 훅과 같은 조건: 대상 폴더가 프로세스 cwd 다
        )
        assert "claimtrail_not_importable" not in r.stderr, r.stderr
        assert "hook_process_failed" not in r.stderr, r.stderr
        assert "No module named" not in r.stderr, r.stderr
        assert r.returncode in (0, 2), r.stderr
        assert "source=" in r.stderr and "/src" in r.stderr.replace("\\", "/"), r.stderr
