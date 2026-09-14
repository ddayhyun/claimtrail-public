"""실행 환경 지문. 같은 파일에 다른 답이 나올 수 있는 나머지 입력.

파일 지문은 코드가 같다는 것만 말한다. 인터프리터·설치 패키지·Node 가
바뀌면 같은 코드에 다른 판정이 나온다 -- pytest 가 바뀌거나 의존성이
갈리는 경우다. 그래서 캐시 판단에는 이 지문도 같아야 한다.

도구마다 --version 을 부르지 않는다. importlib.metadata 로 이 인터프리터에
설치된 패키지 전체를 읽으면 pytest 버전은 그대로인데 프로젝트 의존성만
바뀐 경우까지 잡힌다. Node 는 npm 프로젝트일 때만 본다.

계산에 실패하면 digest 가 None 이다. 그때는 캐시만 포기하고 검증은 한다.
모르는 환경에서 이전 PASS 를 믿는 것이 문제지, 검증 자체는 늘 가능하다.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path

from .detect import Detection

NPM_KIND = "npm test"
_PROBE_TIMEOUT_SEC = 15


@dataclass(frozen=True)
class ContextFingerprint:
    """실행 환경 요약. digest 가 None 이면 계산하지 못한 것이다."""

    digest: str | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.digest is not None


def _tool_output(cmd: Sequence[str], timeout: int = _PROBE_TIMEOUT_SEC) -> str:
    """도구의 --version 출력. 실패는 예외로 올린다 -- 호출자가 None 으로 바꾼다."""
    proc = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=True,
    )
    return (proc.stdout or proc.stderr).strip()


def _interpreter_lines() -> list[str]:
    exe = Path(sys.executable).resolve()
    return [
        f"python.executable={exe}",
        f"python.impl={platform.python_implementation()}",
        f"python.version={sys.version}",
    ]


def _distribution_lines() -> list[str]:
    """설치된 패키지 이름·버전. 이름은 소문자로 맞춰 정렬한다."""
    seen: dict[str, str] = {}
    for dist in distributions():
        try:
            raw = dist.metadata["Name"]
        except KeyError:
            raw = None  # 3.12+ 는 없는 키에 KeyError, 그 전은 None 을 준다
        name = (raw or "").strip()
        if not name:
            continue  # 깨진 메타데이터. 이름 없는 항목은 비교할 수 없다
        seen[name.lower()] = f"dist={name.lower()}=={dist.version}"
    return sorted(seen.values())


def _node_lines(root: Path) -> list[str]:
    lines: list[str] = []
    for tool in ("node", "npm"):
        path = shutil.which(tool)
        if path is None:
            lines.append(f"{tool}=missing")
            continue
        lines.append(f"{tool}.path={Path(path).resolve()}")
        lines.append(f"{tool}.version={_tool_output([path, '--version'])}")
    # node_modules 는 파일 지문에서 빠진다. 설치 상태는 npm 7+ 이 남기는 이
    # 파일로 본다. 없으면(yarn·pnpm·미설치) 설치 상태를 모르므로 캐시하지 않는다.
    lines.append(f"npm.install_state={_install_state_digest(Path(root))}")
    return lines


_CHUNK = 1 << 20


def _sha256_file(path: Path) -> str:
    """청크 단위로 읽는다. lock 파일은 수십 MB 가 될 수 있다."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _install_state_digest(root: Path) -> str:
    """node_modules/.package-lock.json 의 해시.

    node_modules 는 파일 지문에서 빠진다. 설치 상태는 npm 7+ 이 남기는 이
    파일로 본다. 없으면(yarn·pnpm·미설치) 설치 상태를 모르므로 캐시하지
    않는다. 파일이나 node_modules 가 루트 밖을 가리키는 링크여도 읽지
    않는다 -- 파일 지문과 같은 경계다. 실패는 LookupError 로 올리고
    호출자가 context_unavailable 로 바꾼다.
    """
    rel = "node_modules/.package-lock.json"
    lock = root / "node_modules" / ".package-lock.json"
    try:
        resolved = lock.resolve(strict=True)
        canonical_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LookupError(f"{rel} 이 없어 npm 설치 상태를 알 수 없다 — 캐시하지 않는다") from exc
    if not resolved.is_relative_to(canonical_root):
        raise LookupError(f"{rel} 이 루트 밖을 가리켜 읽지 않는다 — 캐시하지 않는다")
    if not resolved.is_file():
        raise LookupError(f"{rel} 이 일반 파일이 아니다 — 캐시하지 않는다")
    return _sha256_file(resolved)


def execution_context(root: Path, detections: Sequence[Detection]) -> ContextFingerprint:
    """탐지된 검증기가 실제로 쓰는 실행 환경의 지문.

    root 는 npm 설치 상태 파일을 찾는 데 쓴다.
    """
    try:
        lines = _interpreter_lines() + _distribution_lines()
        if any(d.kind == NPM_KIND and d.found for d in detections):
            lines += _node_lines(root)
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 캐시만 포기한다
        return ContextFingerprint(None, f"{type(exc).__name__}: {exc}")
    payload = "\n".join(lines).encode("utf-8")
    return ContextFingerprint(hashlib.sha256(payload).hexdigest())
