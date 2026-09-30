"""자동 도출 목록(derived_checks.json)의 검증·제출·읽기.

핵심: 세션이 쓴 목록은 주장이다. 도구가 제출 시점의 입력 지문을 붙이고, 훅은
현재 작업(상태 폴더·session_id·prompt_id)과 일치하는 파일만 읽는다. 없으면
미수행, 형식이 틀리면 무효, 지문이 다르면 stale -- 이전 목록으로 대신하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimtrail import cli
from claimtrail.derive import (
    INVALID,
    NOT_APPLICABLE,
    NOT_PERFORMED,
    PERFORMED,
    STALE,
    DeriveError,
    load_derive,
    submit,
    validate_document,
)
from claimtrail.hookscan import fingerprint, parse_watch
from claimtrail.hookstate import state_dir_for

PYPROJECT = '[project]\nname = "p"\n'


def _doc(**over) -> dict:
    doc = {
        "schema": 1,
        "status": "performed",
        "request": "빈 비밀번호 거부 처리",
        "items": [
            {
                "id": "D1",
                "kind": "requirement",
                "behavior": "잠긴 계정은 거부한다",
                "why": "README 의 요구사항",
                "basis": "README.md:3",
                "how": {"generated": "test_derived.py::test_locked_rejected"},
            },
            {
                "id": "D2",
                "kind": "requirement",
                "behavior": "정상 자격증명은 허용한다",
                "why": "기존 테스트가 다룬다",
                "basis": "tests/test_auth.py:5",
                "how": {"existing": ["tests/test_auth.py::test_valid_credentials"]},
            },
            {
                "id": "D3",
                "kind": "question",
                "behavior": "빈 비밀번호는 거부해야 하는가",
                "why": "README 에 없음",
                "basis": "",
                "how": {"none": "근거 부족 -- 사용자에게 질문"},
            },
        ],
    }
    doc.update(over)
    return doc


@pytest.fixture()
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (root / "auth.py").write_text("def ok():\n    return True\n", encoding="utf-8")
    (root / "tests" / "test_auth.py").write_text(
        "def test_valid_credentials():\n    assert True\n", encoding="utf-8"
    )
    return root


@pytest.fixture()
def gen(tmp_path: Path) -> Path:
    p = tmp_path / "scratch" / "test_derived.py"
    p.parent.mkdir(parents=True)
    p.write_text("def test_locked_rejected():\n    assert True\n", encoding="utf-8")
    return p


# --- 문서 검증 --------------------------------------------------------------


def test_올바른_문서는_오류가_없다():
    assert validate_document(_doc()) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"schema": 2}, "schema"),
        ({"status": "done"}, "status"),
        ({"items": []}, "items"),
        ({"status": "not_applicable"}, "reason"),
    ],
)
def test_최상위_오류를_잡는다(over, needle):
    errors = validate_document(_doc(**over))
    assert errors and any(needle in e for e in errors)


def test_항목_필수_필드와_종류를_검사한다():
    bad = _doc()
    bad["items"][0].pop("basis")
    bad["items"][1]["kind"] = "guess"
    bad["items"][2]["how"] = {}
    errors = validate_document(bad)
    assert any("D1" in e and "basis" in e for e in errors)
    assert any("D2" in e and "kind" in e for e in errors)
    assert any("D3" in e and "how" in e for e in errors)


def test_항목_id_는_유일해야_한다():
    bad = _doc()
    bad["items"][1]["id"] = "D1"
    assert any("D1" in e and "중복" in e for e in validate_document(bad))


def test_해당_없음은_이유만_있으면_된다():
    doc = {"schema": 1, "status": "not_applicable", "reason": "설명 대화"}
    assert validate_document(doc) == []


# --- 제출 ------------------------------------------------------------------


def test_제출은_지문과_생성_파일_해시를_붙여_상태_폴더에_쓴다(
    proj: Path, gen: Path, tmp_path: Path
):
    sd = state_dir_for(proj, tmp_path / "state")
    policy = parse_watch(None)
    res = submit(sd, proj, policy, "sess-1", "prompt-1", _doc(), [gen])
    assert res.status == PERFORMED
    assert res.path.is_file()
    rec = json.loads(res.path.read_text(encoding="utf-8"))
    assert rec["session_id"] == "sess-1" and rec["prompt_id"] == "prompt-1"
    assert rec["input_fingerprint"] == fingerprint(proj, policy).digest
    assert rec["policy_hash"] == policy.policy_hash()
    assert "test_derived.py" in rec["generated"]
    assert rec["derive_digest"] == res.derive_digest and len(res.derive_digest) == 64
    copied = res.path.parent / "generated" / "test_derived.py"
    assert copied.read_text(encoding="utf-8") == gen.read_text(encoding="utf-8")


def test_생성_테스트_내용이_바뀌면_digest_도_바뀐다(proj: Path, gen: Path, tmp_path: Path):
    sd = state_dir_for(proj, tmp_path / "state")
    policy = parse_watch(None)
    a = submit(sd, proj, policy, "sess-1", "prompt-1", _doc(), [gen]).derive_digest
    gen.write_text("def test_locked_rejected():\n    assert False\n", encoding="utf-8")
    b = submit(sd, proj, policy, "sess-1", "prompt-1", _doc(), [gen]).derive_digest
    assert a != b, "같은 경로·다른 assertion 을 같은 목록으로 봤다"


def test_참조한_생성_파일이_없으면_제출을_거부한다(proj: Path, tmp_path: Path):
    sd = state_dir_for(proj, tmp_path / "state")
    with pytest.raises(DeriveError, match="test_derived.py"):
        submit(sd, proj, parse_watch(None), "sess-1", "prompt-1", _doc(), [])


def test_잘못된_문서는_제출을_거부한다(proj: Path, gen: Path, tmp_path: Path):
    sd = state_dir_for(proj, tmp_path / "state")
    with pytest.raises(DeriveError, match="status"):
        submit(sd, proj, parse_watch(None), "sess-1", "prompt-1", _doc(status="x"), [gen])


def test_식별자에_경로_문자가_있으면_거부한다(proj: Path, gen: Path, tmp_path: Path):
    sd = state_dir_for(proj, tmp_path / "state")
    with pytest.raises(DeriveError, match="prompt_id"):
        submit(sd, proj, parse_watch(None), "sess-1", "../x", _doc(), [gen])


# --- 읽기 (훅 쪽) -------------------------------------------------------------


def _submitted(proj: Path, gen: Path, tmp_path: Path, doc: dict | None = None):
    sd = state_dir_for(proj, tmp_path / "state")
    policy = parse_watch(None)
    res = submit(sd, proj, policy, "sess-1", "prompt-1", doc or _doc(), [gen])
    return sd, policy, res


def _load(sd, policy, proj, session="sess-1", prompt="prompt-1", policy_hash=None):
    return load_derive(
        sd,
        session,
        prompt,
        fingerprint(proj, policy).digest,
        policy_hash if policy_hash is not None else policy.policy_hash(),
    )


def test_현재_작업과_일치하면_performed_이고_항목을_돌려준다(proj: Path, gen: Path, tmp_path: Path):
    sd, policy, res = _submitted(proj, gen, tmp_path)
    ds = _load(sd, policy, proj)
    assert ds.status == PERFORMED
    assert ds.derive_digest == res.derive_digest
    assert [i["id"] for i in ds.items] == ["D1", "D2", "D3"]
    assert ds.cache_ok


def test_파일이_없으면_미수행이다(proj: Path, tmp_path: Path):
    sd = state_dir_for(proj, tmp_path / "state")
    ds = load_derive(sd, "sess-1", "prompt-1", "abc", "ph")
    assert ds.status == NOT_PERFORMED and ds.derive_digest == "" and ds.items == []


def test_식별자가_없거나_다르면_이전_목록으로_대신하지_않는다(
    proj: Path, gen: Path, tmp_path: Path
):
    sd, policy, _ = _submitted(proj, gen, tmp_path)
    assert _load(sd, policy, proj, prompt="prompt-2").status == NOT_PERFORMED
    assert _load(sd, policy, proj, session="sess-2").status == NOT_PERFORMED
    assert _load(sd, policy, proj, session="", prompt="").status == NOT_PERFORMED


def test_형식이_틀리면_무효이고_캐시_근거가_아니다(proj: Path, gen: Path, tmp_path: Path):
    sd, policy, res = _submitted(proj, gen, tmp_path)
    res.path.write_text("{not json", encoding="utf-8")
    ds = _load(sd, policy, proj)
    assert ds.status == INVALID and not ds.cache_ok


def test_파일_안_식별자가_경로와_다르면_무효다(proj: Path, gen: Path, tmp_path: Path):
    sd, policy, res = _submitted(proj, gen, tmp_path)
    rec = json.loads(res.path.read_text(encoding="utf-8"))
    rec["prompt_id"] = "other"
    res.path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    assert _load(sd, policy, proj).status == INVALID


def test_제출_뒤_코드가_바뀌면_stale_이다(proj: Path, gen: Path, tmp_path: Path):
    sd, policy, res = _submitted(proj, gen, tmp_path)
    (proj / "auth.py").write_text("def ok():\n    return False\n", encoding="utf-8")
    ds = _load(sd, policy, proj)
    assert ds.status == STALE and not ds.cache_ok
    assert ds.derive_digest == res.derive_digest, "stale 이어도 목록 digest 는 그대로 남긴다"


def test_생성_파일이_제출_뒤_바뀌면_stale_이다(proj: Path, gen: Path, tmp_path: Path):
    sd, policy, res = _submitted(proj, gen, tmp_path)
    (res.path.parent / "generated" / "test_derived.py").write_text("changed", encoding="utf-8")
    assert _load(sd, policy, proj).status == STALE


def test_감시_정책이_다르면_stale_이다(proj: Path, gen: Path, tmp_path: Path):
    sd, policy, _ = _submitted(proj, gen, tmp_path)
    assert _load(sd, policy, proj, policy_hash="other-policy").status == STALE


def test_해당_없음_제출은_그대로_읽힌다(proj: Path, tmp_path: Path):
    sd = state_dir_for(proj, tmp_path / "state")
    policy = parse_watch(None)
    doc = {"schema": 1, "status": "not_applicable", "reason": "설명 대화"}
    res = submit(sd, proj, policy, "sess-1", "prompt-1", doc, [])
    ds = _load(sd, policy, proj)
    assert res.status == NOT_APPLICABLE and ds.status == NOT_APPLICABLE
    assert ds.detail == "설명 대화" and ds.cache_ok


# --- CLI -------------------------------------------------------------------


def test_CLI_derive_submit_은_기록을_쓰고_digest_를_출력한다(
    proj: Path, gen: Path, tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.setenv("CLAIMTRAIL_STATE_DIR", str(tmp_path / "state"))
    docfile = tmp_path / "doc.json"
    docfile.write_text(json.dumps(_doc(), ensure_ascii=False), encoding="utf-8")
    code = cli.main(
        [
            "derive",
            "submit",
            str(proj),
            "--session-id",
            "sess-1",
            "--prompt-id",
            "prompt-1",
            "--file",
            str(docfile),
            "--generated",
            str(gen),
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == PERFORMED and len(out["derive_digest"]) == 64
    assert Path(out["path"]).is_file()


def test_CLI_derive_submit_은_잘못된_문서에_2_를_돌려준다(
    proj: Path, tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.setenv("CLAIMTRAIL_STATE_DIR", str(tmp_path / "state"))
    docfile = tmp_path / "doc.json"
    docfile.write_text('{"schema": 1, "status": "x"}', encoding="utf-8")
    args = ["derive", "submit", str(proj), "--session-id", "s", "--prompt-id", "p"]
    code = cli.main(args + ["--file", str(docfile)])
    assert code == 2
    assert "status" in capsys.readouterr().err


def test_CLI_derive_submit_not_applicable(proj: Path, tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("CLAIMTRAIL_STATE_DIR", str(tmp_path / "state"))
    args = ["derive", "submit", str(proj), "--session-id", "s", "--prompt-id", "p"]
    code = cli.main(args + ["--not-applicable", "--reason", "설명 대화"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == NOT_APPLICABLE


# --- 자격증명 가림 -------------------------------------------------------------


SECRETS = (
    "abc123secret",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "abcdef1234567890XYZ",
)


def _secret_doc() -> dict:
    return {
        "schema": 1,
        "status": "performed",
        "request": "요청 password=abc123secret",
        "items": [
            {
                "id": "D1",
                "kind": "requirement",
                "behavior": "로그인 처리 password=abc123secret",
                "why": "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                "basis": "README.md:1",
                "how": {"none": "Authorization: Bearer abcdef1234567890XYZ"},
            }
        ],
    }


def test_증빙_절은_자격증명을_가리고_해시는_원문으로_계산한다(proj: Path, tmp_path: Path):
    from claimtrail.derive import _digest, derive_markdown, derive_section

    sd = state_dir_for(proj, tmp_path / "state")
    policy = parse_watch(None)
    doc = _secret_doc()
    res = submit(sd, proj, policy, "sess-1", "prompt-1", doc, [])
    assert res.derive_digest == _digest(doc, {}), "해시는 가림 전 원문으로 계산한다"
    ds = _load(sd, policy, proj)
    assert ds.status == PERFORMED
    js = json.dumps(derive_section(ds), ensure_ascii=False)
    md = "".join(derive_markdown(ds))
    for secret in SECRETS:
        assert secret not in js, f"JSON 증빙에 노출: {secret}"
        assert secret not in md, f"Markdown 증빙에 노출: {secret}"
    assert "[REDACTED]" in js and "[REDACTED]" in md


# --- 활성화 표식 (2c) -----------------------------------------------------------


def test_활성화_표식은_상태_폴더에_두고_켜고_끌_수_있다(proj: Path, tmp_path: Path):
    from claimtrail.derive import derive_enabled, disable, enable

    sd = state_dir_for(proj, tmp_path / "state")
    assert not derive_enabled(sd)
    marker = enable(sd)
    assert marker.is_file() and derive_enabled(sd)
    rec = json.loads(marker.read_text(encoding="utf-8"))
    assert rec["tool_version"] and rec["enabled_at"]
    assert disable(sd) is True and not derive_enabled(sd)
    assert disable(sd) is False, "이미 꺼져 있으면 False"


def test_활성_상태에서_파일이_없으면_미수행이고_그_사실을_적는다(proj: Path, tmp_path: Path):
    sd = state_dir_for(proj, tmp_path / "state")
    inactive = load_derive(sd, "sess-1", "prompt-1", "abc", "ph")
    assert inactive.status == NOT_PERFORMED and inactive.active is False
    assert "비활성" in inactive.detail
    active = load_derive(sd, "sess-1", "prompt-1", "abc", "ph", active=True)
    assert active.status == NOT_PERFORMED and active.active is True
    assert "활성" in active.detail and "미수행" in active.detail


# --- 세션 되돌림 문구와 제출 명령 (2d) --------------------------------------------


def _ds(status, active=True, items=(), detail="d"):
    from claimtrail.derive import DeriveStatus

    return DeriveStatus(status, detail, "dg", list(items), active=active)


def test_비활성이면_어떤_상태여도_되돌림_문구가_없다():
    from claimtrail.derive import INVALID, NOT_PERFORMED, PERFORMED, derive_notice

    failed = [{"id": "D1", "link_status": "failed", "link_detail": "assert"}]
    assert derive_notice(_ds(NOT_PERFORMED, active=False)) == ""
    assert derive_notice(_ds(INVALID, active=False)) == ""
    assert derive_notice(_ds(PERFORMED, active=False, items=failed)) == ""


def test_활성_미수행_무효_stale_은_되돌림_문구를_낸다():
    from claimtrail.derive import INVALID, NOT_APPLICABLE, NOT_PERFORMED, STALE, derive_notice

    assert "미수행" in derive_notice(_ds(NOT_PERFORMED, detail="파일이 없다"))
    assert "무효" in derive_notice(_ds(INVALID, detail="형식 오류"))
    assert "시점" in derive_notice(_ds(STALE, detail="입력이 바뀜"))
    assert derive_notice(_ds(NOT_APPLICABLE, detail="설명 대화")) == ""


def test_활성_수행은_실패_미수집_확인불가_증거없음_항목만_되돌린다():
    from claimtrail.derive import PERFORMED, derive_notice

    ok = [
        {"id": "P1", "link_status": "passed"},
        {"id": "S1", "link_status": "skipped"},
        {"id": "N1", "link_status": "not_run", "link_detail": "질문"},
        {"id": "X1", "link_status": "deselected"},
    ]
    assert derive_notice(_ds(PERFORMED, items=ok)) == ""
    bad = ok + [
        {"id": "D2", "link_status": "failed", "link_detail": "call 실패: assert x"},
        {"id": "D3", "link_status": "not_collected"},
        {"id": "D4", "link_status": "incomplete"},
        {"id": "D5", "link_status": "no_evidence"},
    ]
    text = derive_notice(_ds(PERFORMED, items=bad))
    for ident in ("D2", "D3", "D4", "D5"):
        assert ident in text
    assert "P1" not in text and "assert x" in text
    assert "4개" in text


def test_되돌림_서명은_상태와_문제_항목으로_정해진다():
    from claimtrail.derive import NOT_PERFORMED, PERFORMED, notice_signature

    a = notice_signature("fp", _ds(NOT_PERFORMED))
    b = notice_signature("fp", _ds(NOT_PERFORMED))
    assert a == b and "fp" in a
    failed = [{"id": "D2", "link_status": "failed"}]
    c = notice_signature("fp", _ds(PERFORMED, items=failed))
    assert c != a
    assert notice_signature("fp2", _ds(PERFORMED, items=failed)) != c


def test_제출_명령은_설치본이_없으면_이_파이썬의_m_호출이다(tmp_path: Path, monkeypatch):
    import sys

    from claimtrail.derive import cli_invocation

    empty = tmp_path / "emptybin"
    empty.mkdir()
    cmd = cli_invocation({"PATH": str(empty)})
    assert "-m claimtrail" in cmd
    assert Path(sys.executable).name in cmd or Path(sys.executable).as_posix() in cmd
    assert "src" in cmd, "PYTHONPATH 로 이 패키지의 위치를 준다"

    fake = tmp_path / "bin"
    fake.mkdir()
    exe = fake / ("claimtrail.exe" if sys.platform == "win32" else "claimtrail")
    exe.write_bytes(b"")
    exe.chmod(0o755)
    assert cli_invocation({"PATH": str(fake), "PATHEXT": ".EXE"}) == "claimtrail"
