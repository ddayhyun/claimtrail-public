"""버전 선언은 두 곳이다. 어긋나면 태그·배포 검사가 거짓 근거 위에 선다."""

from __future__ import annotations

import re
from pathlib import Path

from claimtrail import __version__

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    # 3.9 에는 tomllib 이 없다. [project] 절의 version 한 줄만 읽는다.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    m = re.search(r'^version\s*=\s*"([^"]+)"', project, flags=re.M)
    assert m, "pyproject.toml [project] 에 version 이 없다"
    return m.group(1)


def test_pyproject_와_패키지의_버전이_같다():
    assert _pyproject_version() == __version__
