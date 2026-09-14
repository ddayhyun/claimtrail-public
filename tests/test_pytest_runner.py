"""pytest 러너 테스트 — 수집 오류를 테스트 실행 결과로 읽지 않는다.

data/junit_collection_error.xml 은 실제 사례에서 나온 파일이다: 루트의
1회성 스크립트 test_e2e.py 가 playwright 를 import 하다 죽어 수집이
중단됐고, pytest 는 그것을 tests="1" errors="1" 인 testcase 하나로 적었다.
"""

from pathlib import Path

from claimtrail.runners.base import COLLECTION_ERROR, FAIL, PASS
from claimtrail.runners.pytest_runner import _parse_junit, run_pytest

DATA = Path(__file__).parent / "data"


def _junit(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="0" '
        f'tests="2" time="0.1">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


# --- JUnit 파싱 --------------------------------------------------------------


def test_실제_사례의_수집오류_원인이_짧은_message_뒤에_보존된다():
    parsed = _parse_junit(DATA / "junit_collection_error.xml")
    assert parsed is not None
    # 원본 집계는 그대로다 -- JUnit 이 센 값을 조용히 바꾸지 않는다.
    assert parsed["tests"] == 1
    assert parsed["errors"] == 1
    # 그 1개는 테스트가 아니라 수집 오류 항목이다.
    assert parsed["cases"] == 1
    assert parsed["collection_cases"] == 1
    assert parsed["failure_list"] == []
    [item] = parsed["collection_list"]
    assert item.test == "test_e2e"
    assert "collection failure" in item.message
    assert "ModuleNotFoundError" in item.message
    assert "playwright" in item.message


def test_짧은_message와_본문이_함께_있어도_본문의_원인이_남는다(tmp_path: Path):
    xml = _junit(
        tmp_path,
        '<testcase classname="" name="test_x" time="0"><error message="collection failure">'
        "x.py:1: in &lt;module&gt;\n    import nope\n"
        "E   ModuleNotFoundError: No module named 'nope'</error></testcase>",
    )
    parsed = _parse_junit(xml)
    assert parsed is not None
    [item] = parsed["collection_list"]
    assert item.message.startswith("collection failure")
    assert "No module named 'nope'" in item.message


def test_실제_실패는_수집오류와_섞이지_않는다(tmp_path: Path):
    xml = _junit(
        tmp_path,
        '<testcase classname="tests.test_a" name="test_b" time="0">'
        '<failure message="assert 1 == 2">def test_b():\n&gt;   assert 1 == 2\nE   assert 1 == 2'
        "</failure></testcase>"
        '<testcase classname="" name="test_broken" time="0">'
        '<error message="collection failure">E   ImportError: nope</error></testcase>',
    )
    parsed = _parse_junit(xml)
    assert parsed is not None
    assert parsed["cases"] == 2
    assert parsed["collection_cases"] == 1
    [real] = parsed["failure_list"]
    assert real.test == "tests.test_a::test_b"
    [collected] = parsed["collection_list"]
    assert collected.test == "test_broken"
    assert "ImportError" in collected.message


def test_수집오류가_없으면_collection_list_는_비어_있다(tmp_path: Path):
    xml = _junit(
        tmp_path,
        '<testcase classname="tests.test_a" name="test_ok" time="0"/>'
        '<testcase classname="tests.test_a" name="test_b" time="0">'
        '<failure message="boom">E   boom</failure></testcase>',
    )
    parsed = _parse_junit(xml)
    assert parsed is not None
    assert parsed["collection_list"] == []
    assert parsed["cases"] == 2
    assert parsed["collection_cases"] == 0


# --- 실제 실행 (이 환경의 pytest 로 작은 프로젝트를 돌린다) --------------------


def _project(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    # 루트의 test_*.py -- 테스트가 아니라 1회성 스크립트지만 이름 때문에 수집된다.
    (tmp_path / "test_broken.py").write_text("import module_that_does_not_exist_xyz\n")
    return tmp_path


def test_경로를_지정하지_않으면_루트의_깨진_파일에서_수집이_중단된다(tmp_path: Path):
    r = run_pytest(_project(tmp_path), timeout=120)
    assert r.status == FAIL
    assert r.reason_code == COLLECTION_ERROR
    assert r.phase == "collection"
    # JUnit 원본 집계(1개 항목)와 실행 상태(0개 실행)를 둘 다 남긴다.
    assert r.total == 1
    assert r.tests_ran == 0
    assert "수집" in r.summary
    assert r.failures[0].test == "test_broken"
    assert "ModuleNotFoundError" in r.failures[0].message
    assert "module_that_does_not_exist_xyz" in r.note
    assert "테스트 실행 결과가 아니다" in r.note
    assert r.cwd == str(tmp_path)
    # 진단은 pytest 출력 요약이지 환경 덤프가 아니다.
    assert "PATH=" not in r.note


def test_경로를_지정하면_그_범위만_수집한다(tmp_path: Path):
    r = run_pytest(_project(tmp_path), timeout=120, paths=("tests",))
    assert r.status == PASS
    assert r.total == 1
    assert r.passed == 1
    assert r.tests_ran == 1
    assert r.phase == "run"
    assert r.reason_code == ""
    assert r.command[-1] == "tests"
    assert r.command[:3] == [r.command[0], "-m", "pytest"]


def test_건너뛴_테스트는_실행_수에_세지_않는다(tmp_path: Path):
    """tests_ran 은 '실제로 돌았다고 확인되는 테스트 수' 다. skipped 는 수집됐지만
    돌지 않았다 -- JUnit testcase 요소로는 남으므로 요소 수에서 빼야 한다."""
    (tmp_path / "test_mix.py").write_text(
        "import pytest\n\n"
        "def test_ok():\n    assert True\n\n"
        "@pytest.mark.skip(reason='not now')\n"
        "def test_skipped():\n    assert False\n",
        encoding="utf-8",
    )
    r = run_pytest(tmp_path, timeout=120)
    assert r.status == PASS
    assert r.total == 2 and r.skipped == 1 and r.passed == 1
    assert r.phase == "run"
    assert r.tests_ran == 1, "skipped 를 실행으로 셌다"
