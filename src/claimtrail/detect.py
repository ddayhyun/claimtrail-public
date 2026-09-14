"""프로젝트에서 실제로 실행할 수 있는 검증이 무엇인지 찾아낸다.

원칙: 추측하지 않는다. 근거(signal)를 찾은 것만 '있다'고 말한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 구버전 폴백
    tomllib = None  # type: ignore[assignment]


@dataclass
class Detection:
    """하나의 검증 종류에 대한 탐지 결과."""

    kind: str
    found: bool
    signals: list[str] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        if self.found:
            return f"{self.kind}: 탐지됨 ({len(self.signals)}개 근거)"
        return f"{self.kind}: 탐지되지 않음 — {self.reason}"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _load_toml(raw: str) -> dict | None:
    if tomllib is None:
        return None
    try:
        return tomllib.loads(raw)
    except Exception:
        return None


def _pyproject_signals(root: Path) -> list[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return []

    raw = _read(path)
    data = _load_toml(raw)
    signals: list[str] = []

    if data is None:
        # TOML 파서가 없거나 파싱 실패 — 텍스트 스캔으로 낮춰 잡는다.
        if "[tool.pytest" in raw:
            signals.append("pyproject.toml: [tool.pytest...] (텍스트 스캔)")
        elif re.search(r"[\"']pytest[\"'~=<>\s]", raw):
            signals.append("pyproject.toml: pytest 문자열 (텍스트 스캔)")
        return signals

    if "pytest" in data.get("tool", {}):
        signals.append("pyproject.toml: [tool.pytest.ini_options]")

    deps: list[str] = []
    project = data.get("project", {}) or {}
    deps += [d for d in (project.get("dependencies") or []) if isinstance(d, str)]
    for group in (project.get("optional-dependencies") or {}).values():
        deps += [d for d in (group or []) if isinstance(d, str)]
    for group in (data.get("dependency-groups") or {}).values():
        deps += [d for d in (group or []) if isinstance(d, str)]

    if any(re.match(r"\s*pytest\b", d) for d in deps):
        signals.append("pyproject.toml: 의존성 목록에 pytest")

    return signals


def _ini_signals(root: Path) -> list[str]:
    signals: list[str] = []

    if (root / "pytest.ini").is_file():
        signals.append("pytest.ini 존재")

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file() and "[tool:pytest]" in _read(setup_cfg):
        signals.append("setup.cfg: [tool:pytest]")

    tox_ini = root / "tox.ini"
    if tox_ini.is_file() and "[pytest]" in _read(tox_ini):
        signals.append("tox.ini: [pytest]")

    return signals


def _testfile_signals(root: Path) -> list[str]:
    for name in ("tests", "test"):
        directory = root / name
        if not directory.is_dir():
            continue
        # pytest 의 기본 python_files 는 test_*.py 와 *_test.py 둘 다다.
        hits = {
            p
            for pattern in ("test_*.py", "*_test.py")
            for p in directory.rglob(pattern)
            if "node_modules" not in p.parts and ".venv" not in p.parts
        }
        if hits:
            return [f"{name}/ 아래 test_*.py 또는 *_test.py {len(hits)}개"]
    return []


def detect_pytest(root: Path) -> Detection:
    """pytest를 실행할 근거가 있는지 찾는다."""
    signals = _pyproject_signals(root) + _ini_signals(root) + _testfile_signals(root)

    if signals:
        return Detection(kind="pytest", found=True, signals=signals)

    return Detection(
        kind="pytest",
        found=False,
        reason=(
            "pyproject.toml/pytest.ini/setup.cfg/tox.ini 어디에도 pytest 설정이 없고 "
            "test_*.py·*_test.py 파일도 찾지 못함"
        ),
    )


# lint 는 설정 파일이 있을 때만 '있다'고 말한다.
# .py 파일이 있다는 이유만으로 lint 를 돌리면, 그건 근거가 아니라 추측이다.
LINT_PRIORITY = ("ruff", "flake8")


def _lint_configs(root: Path) -> list[tuple[str, str]]:
    """(도구, 근거) 목록. 근거를 찾은 것만 넣는다."""
    found: list[tuple[str, str]] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        raw = _read(pyproject)
        data = _load_toml(raw)
        if data is not None:
            tool = data.get("tool", {}) or {}
            if "ruff" in tool:
                found.append(("ruff", "pyproject.toml: [tool.ruff]"))
            if "flake8" in tool:
                found.append(("flake8", "pyproject.toml: [tool.flake8]"))
        else:
            # TOML 파서가 없거나 파싱 실패 — 텍스트 스캔으로 낮춰 잡는다.
            if "[tool.ruff" in raw:
                found.append(("ruff", "pyproject.toml: [tool.ruff...] (텍스트 스캔)"))
            if "[tool.flake8" in raw:
                found.append(("flake8", "pyproject.toml: [tool.flake8] (텍스트 스캔)"))

    for name in ("ruff.toml", ".ruff.toml"):
        if (root / name).is_file():
            found.append(("ruff", f"{name} 존재"))

    if (root / ".flake8").is_file():
        found.append(("flake8", ".flake8 존재"))

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file() and "[flake8]" in _read(setup_cfg):
        found.append(("flake8", "setup.cfg: [flake8]"))

    tox_ini = root / "tox.ini"
    if tox_ini.is_file() and "[flake8]" in _read(tox_ini):
        found.append(("flake8", "tox.ini: [flake8]"))

    return found


def lint_tool(root: Path) -> str | None:
    """실행할 lint 도구를 고른다. 근거가 여럿이면 ruff 를 우선한다.

    ruff 를 앞세우는 이유는 구조화된 JSON 출력을 주기 때문이다.
    위반 건수를 출력에서 추정하지 않고 값으로 받을 수 있다.
    """
    tools = {t for t, _ in _lint_configs(root)}
    for preferred in LINT_PRIORITY:
        if preferred in tools:
            return preferred
    return None


def detect_format(root: Path, config: Config | None) -> Detection:
    """format 검사는 자동 탐지 대상이 아니다. claimtrail.json 이 켰을 때만,
    그리고 ruff 설정이 있을 때만 '있다' 고 말한다.

    [tool.ruff] 만 보고 자동으로 돌리지 않는 이유: 그러면 설정 없는 기존
    사용자의 판정이 어느 날 갑자기 바뀐다. 켜는 것은 사람이 한다.
    """
    if config is None or not config.wants_format:
        return Detection(
            kind="format",
            found=False,
            reason="claimtrail.json 에서 format 을 켜지 않아 실행하지 않음 (자동 탐지 대상 아님)",
        )
    ruff_signals = [s for t, s in _lint_configs(root) if t == "ruff"]
    if ruff_signals:
        return Detection(
            kind="format",
            found=True,
            signals=[f"{config.source.name}: format.tool = {config.format_tool}", *ruff_signals],
        )
    return Detection(
        kind="format",
        found=False,
        reason=(
            f"{config.source.name} 이 format 을 요구하지만 pyproject.toml [tool.ruff] 또는 "
            "ruff.toml 이 없어 ruff format 을 돌릴 근거가 없음"
        ),
    )


def detect_lint(root: Path) -> Detection:
    """ruff 또는 flake8 을 실행할 근거가 있는지 찾는다."""
    configs = _lint_configs(root)

    if configs:
        return Detection(kind="lint", found=True, signals=[s for _, s in configs])

    return Detection(
        kind="lint",
        found=False,
        reason=(
            "pyproject.toml/ruff.toml/.flake8/setup.cfg/tox.ini 어디에도 "
            "ruff 또는 flake8 설정이 없음"
        ),
    )


# type-check 도 lint 와 같다. 설정 파일이 근거다.
# 타입 힌트가 보인다는 이유로 mypy 를 돌리면 그건 추측이다.
TYPECHECK_PRIORITY = ("mypy", "pyright")


def _typecheck_configs(root: Path) -> list[tuple[str, str]]:
    """(도구, 근거) 목록. 근거를 찾은 것만 넣는다."""
    found: list[tuple[str, str]] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        raw = _read(pyproject)
        data = _load_toml(raw)
        if data is not None:
            tool = data.get("tool", {}) or {}
            if "mypy" in tool:
                found.append(("mypy", "pyproject.toml: [tool.mypy]"))
            if "pyright" in tool:
                found.append(("pyright", "pyproject.toml: [tool.pyright]"))
        else:
            if "[tool.mypy" in raw:
                found.append(("mypy", "pyproject.toml: [tool.mypy] (텍스트 스캔)"))
            if "[tool.pyright" in raw:
                found.append(("pyright", "pyproject.toml: [tool.pyright] (텍스트 스캔)"))

    for name in ("mypy.ini", ".mypy.ini"):
        if (root / name).is_file():
            found.append(("mypy", f"{name} 존재"))

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file() and "[mypy]" in _read(setup_cfg):
        found.append(("mypy", "setup.cfg: [mypy]"))

    if (root / "pyrightconfig.json").is_file():
        found.append(("pyright", "pyrightconfig.json 존재"))

    return found


def typecheck_tool(root: Path) -> str | None:
    """실행할 type-check 도구를 고른다. 근거가 여럿이면 mypy 를 우선한다."""
    tools = {t for t, _ in _typecheck_configs(root)}
    for preferred in TYPECHECK_PRIORITY:
        if preferred in tools:
            return preferred
    return None


def detect_typecheck(root: Path) -> Detection:
    """mypy 또는 pyright 를 실행할 근거가 있는지 찾는다."""
    configs = _typecheck_configs(root)

    if configs:
        return Detection(kind="type-check", found=True, signals=[s for _, s in configs])

    return Detection(
        kind="type-check",
        found=False,
        reason=(
            "pyproject.toml/mypy.ini/setup.cfg/pyrightconfig.json 어디에도 "
            "mypy 또는 pyright 설정이 없음"
        ),
    )


def _build_signals(root: Path) -> list[str]:
    """패키지를 빌드할 근거를 찾는다."""
    signals: list[str] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        raw = _read(pyproject)
        data = _load_toml(raw)
        if data is not None:
            backend = (data.get("build-system") or {}).get("build-backend")
            if "build-system" in data:
                label = f" (backend: {backend})" if backend else ""
                signals.append(f"pyproject.toml: [build-system]{label}")
        elif "[build-system]" in raw:
            # TOML 파서가 없는 구버전(3.9/3.10)도 백엔드 이름까지는 읽어낸다.
            # 폴백이라고 근거를 덜 남길 이유는 없다.
            hit = re.search(r"""build-backend\s*=\s*["']([^"']+)["']""", raw)
            label = f" (backend: {hit.group(1)})" if hit else ""
            signals.append(f"pyproject.toml: [build-system]{label} (텍스트 스캔)")

    if (root / "setup.py").is_file():
        signals.append("setup.py 존재")

    return signals


def detect_build(root: Path) -> Detection:
    """패키지 빌드를 실행할 근거가 있는지 찾는다."""
    signals = _build_signals(root)

    if signals:
        return Detection(kind="build", found=True, signals=signals)

    return Detection(
        kind="build",
        found=False,
        reason=(
            "pyproject.toml에 [build-system]이 없고 setup.py도 없어 "
            "빌드할 패키지로 볼 근거가 없음"
        ),
    )


# npm init -y 가 만들어 놓는 자리표시자. 테스트가 있다는 근거가 아니다.
# 이걸 근거로 잡으면 항상 실패하는 거짓 양성이 된다.
NPM_PLACEHOLDER = "no test specified"

LOCKFILES = {
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
}


def _load_package_json(root: Path) -> dict | None:
    path = root / "package.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(_read(path))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def npm_test_script(root: Path) -> str | None:
    """실행할 test 스크립트를 돌려준다. 자리표시자면 없는 것으로 본다."""
    data = _load_package_json(root)
    if data is None:
        return None

    script = (data.get("scripts") or {}).get("test")
    if not isinstance(script, str) or not script.strip():
        return None
    if NPM_PLACEHOLDER in script:
        return None
    return script


def npm_declared_deps(root: Path) -> int:
    """선언된 의존성 개수. node_modules 가 필요한지 판단하는 데 쓴다."""
    data = _load_package_json(root)
    if data is None:
        return 0
    total = 0
    for key in ("dependencies", "devDependencies"):
        group = data.get(key)
        if isinstance(group, dict):
            total += len(group)
    return total


def detect_npm_test(root: Path) -> Detection:
    """npm test 를 실행할 근거가 있는지 찾는다."""
    if not (root / "package.json").is_file():
        return Detection(
            kind="npm test",
            found=False,
            reason="package.json이 없어 Node 프로젝트로 볼 근거가 없음",
        )

    script = npm_test_script(root)
    if script is None:
        data = _load_package_json(root)
        if data is None:
            reason = "package.json을 JSON으로 읽지 못함"
        elif NPM_PLACEHOLDER in ((data.get("scripts") or {}).get("test") or ""):
            reason = "scripts.test가 npm init 기본 자리표시자라 실제 테스트로 볼 수 없음"
        else:
            reason = "package.json에 scripts.test가 없음"
        return Detection(kind="npm test", found=False, reason=reason)

    signals = [f"package.json: scripts.test = `{script}`"]

    for name, manager in LOCKFILES.items():
        if (root / name).is_file():
            signals.append(f"{name} 존재 ({manager})")

    deps = npm_declared_deps(root)
    if deps:
        installed = "설치됨" if (root / "node_modules").is_dir() else "설치 안 됨"
        signals.append(f"의존성 {deps}개 선언 (node_modules {installed})")

    return Detection(kind="npm test", found=True, signals=signals)


def detect_all(root: Path, config: Config | None = None) -> list[Detection]:
    """현재 범위에서 지원하는 검증 종류를 모두 탐지한다.

    config 가 없으면 예전과 같은 5종이다. config 가 있을 때만 format 이 목록에
    붙는다 -- 설정 없는 호출자(훅 포함)의 탐지 결과·리포트·지문이 그대로다.
    """
    items = [
        detect_pytest(root),
        detect_lint(root),
        detect_typecheck(root),
        detect_build(root),
        detect_npm_test(root),
    ]
    if config is not None:
        items.append(detect_format(root, config))
    return items
