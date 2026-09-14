"""npm test 러너 테스트.

npm 이 없는 환경에서도 돌아야 한다. 실행 전 판단(스크립트 없음, 의존성
미설치)은 npm 없이도 검증되고, 실제 실행은 npm 이 있을 때만 돌린다.

실제 실행 샘플은 Node 내장 테스트 러너(node --test)를 쓴다. 외부 패키지가
필요 없어 npm install 없이 돌아간다.
"""

import json
import shutil
from pathlib import Path

import pytest

from claimtrail.runners.base import FAIL, PASS, UNVERIFIED
from claimtrail.runners.npm_runner import run_npm_test

_HAS_NPM = shutil.which("npm") is not None

PASSING_TEST = """\
const test = require('node:test');
const assert = require('node:assert');
test('더하기', () => { assert.strictEqual(1 + 1, 2); });
"""

FAILING_TEST = """\
const test = require('node:test');
const assert = require('node:assert');
test('깨진 테스트', () => { assert.strictEqual(1 + 1, 3); });
"""


def _node_project(root: Path, body: str) -> None:
    (root / "package.json").write_text(
        json.dumps({"name": "sample", "version": "1.0.0", "scripts": {"test": "node --test"}}),
        encoding="utf-8",
    )
    (root / "test").mkdir()
    (root / "test" / "a.test.js").write_text(body, encoding="utf-8")


# --- 실행 전 판단 (npm 없이도 검증된다) --------------------------------------


def test_test_스크립트가_없으면_통과가_아니라_검증불가다(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    r = run_npm_test(tmp_path)
    assert r.status == UNVERIFIED
    assert r.status != PASS


def test_자리표시자_스크립트도_실행하지_않는다(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
        encoding="utf-8",
    )
    assert run_npm_test(tmp_path).status == UNVERIFIED


def test_의존성이_설치되지_않았으면_실패가_아니라_검증불가다(tmp_path: Path):
    """'돌릴 수 없다'와 '돌렸는데 깨졌다'는 다르다. 종료 코드는 둘 다 1이다."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "^29"}}),
        encoding="utf-8",
    )
    r = run_npm_test(tmp_path)
    assert r.status == UNVERIFIED
    assert r.status != FAIL
    assert "node_modules" in r.note
    assert r.command == []  # 실행 자체를 하지 않았다


# --- 실제 실행 --------------------------------------------------------------


@pytest.mark.skipif(not _HAS_NPM, reason="이 환경에 npm이 없다")
def test_테스트가_통과하면_통과로_적는다(tmp_path: Path):
    _node_project(tmp_path, PASSING_TEST)
    r = run_npm_test(tmp_path)
    assert r.status == PASS
    assert r.exit_code == 0


@pytest.mark.skipif(not _HAS_NPM, reason="이 환경에 npm이 없다")
def test_테스트가_깨지면_실패로_적는다(tmp_path: Path):
    _node_project(tmp_path, FAILING_TEST)
    r = run_npm_test(tmp_path)
    assert r.status == FAIL
    assert r.exit_code != 0


@pytest.mark.skipif(not _HAS_NPM, reason="이 환경에 npm이 없다")
def test_숫자를_세지_못했다는_사실을_리포트에_적는다(tmp_path: Path):
    """세지 못한 것을 센 척하지 않는다."""
    _node_project(tmp_path, PASSING_TEST)
    r = run_npm_test(tmp_path)
    assert "숫자 미수집" in r.summary
    assert r.total is None
    assert "개수를 세지 않는다" in r.note
