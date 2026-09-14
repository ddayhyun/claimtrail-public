"""hookscan 단위 테스트 — 대상 찾기와 상태 재기.

'무엇을 보는가'가 틀리면 그 뒤의 모든 판정이 틀린다. 홈 디렉터리를 통째로
해싱하거나 형제 프로젝트를 끌어오면, 판정은 정확하지만 대상이 틀린 것이다.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from claimtrail import hookscan
from claimtrail.hookscan import (
    FORCE_INCLUDE,
    WatchPolicy,
    entry_line,
    fingerprint,
    index_entries,
    is_safe_relative,
    keep,
    list_files,
    parse_watch,
    resolve_root,
)

DEFAULT = WatchPolicy()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def _init(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _write(p: Path, text: str = "x\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _repo(root: Path) -> Path:
    """단일 프로젝트 저장소. 소스·테스트·문서·ignore 대상이 한 벌씩."""
    _write(root / "pyproject.toml", '[project]\nname="x"\n')
    _write(root / "src" / "core.py", "A = 1\n")
    _write(root / "tests" / "test_core.py", "def test_a(): pass\n")
    _write(root / "docs" / "guide.md", "# doc\n")
    _write(root / "README.md", "# readme\n")
    _write(root / "CHANGELOG.md", "# changes\n")
    _write(root / "CONTRIBUTING.md", "# contributing")
    _write(root / "LICENSE", "MIT")
    _write(root / "src" / "architecture.md", "# 설계 메모")
    _write(root / "notes.txt", "루트 메모")
    _write(root / ".claude" / "CLAUDE.md", "규칙\n")
    _write(root / ".claude" / "rules" / "safety.md", "규칙\n")
    _write(root / ".github" / "workflows" / "ci.yml", "on: push\n")
    _write(root / ".gitignore", "secret.txt\n")
    _write(root / "secret.txt", "ignored\n")
    _init(root)
    _commit(root)
    return root


def _fake_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


# ============================================================================
# 1. 프로젝트 루트
# ============================================================================


def test_명시된_루트가_최우선이다(tmp_path: Path):
    target = tmp_path / "explicit"
    target.mkdir()
    r = resolve_root(tmp_path, env={"CLAIMTRAIL_PROJECT_ROOT": str(target)})
    assert r.root == target.resolve()
    assert r.source == "env" and r.scope_known


def test_명시된_루트가_폴더가_아니면_범위를_모른다고_한다(tmp_path: Path):
    r = resolve_root(tmp_path, env={"CLAIMTRAIL_PROJECT_ROOT": str(tmp_path / "없음")})
    assert not r.scope_known
    assert "폴더가 아니다" in r.reason


def test_가장_가까운_manifest_폴더를_루트로_본다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    inner = repo / "sub" / "pkg"
    inner.mkdir(parents=True)
    _write(repo / "sub" / "pyproject.toml", '[project]\nname="sub"\n')
    r = resolve_root(inner, env={})
    assert r.root == (repo / "sub").resolve()
    assert r.source == "manifest"


def test_manifest_탐색이_Git_루트를_넘지_않는다(tmp_path: Path):
    _write(tmp_path / "pyproject.toml", '[project]\nname="outer"\n')
    repo = tmp_path / "repo"
    _write(repo / "src" / "a.py")
    _init(repo)
    _commit(repo)
    r = resolve_root(repo / "src", env={})
    assert r.root == repo.resolve()
    assert r.source == "git"


def test_manifest가_없으면_Git_루트를_쓴다(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(repo / "a.py")
    _init(repo)
    r = resolve_root(repo, env={})
    assert r.root == repo.resolve()
    assert r.source == "git" and r.scope_known


def test_Git도_manifest도_없으면_범위를_추측하지_않는다(tmp_path: Path, monkeypatch):
    _fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    plain = tmp_path / "plain"
    _write(plain / "a.py")
    r = resolve_root(plain, env={})
    assert r.source == "cwd"
    assert not r.scope_known
    assert "감시 범위를 정할 수 없다" in r.reason


def test_홈이_git_저장소여도_홈을_프로젝트_루트로_보지_않는다(tmp_path: Path, monkeypatch):
    """이 저장소를 개발한 PC 가 실제로 그런 환경이다."""
    home = tmp_path / "home"
    home.mkdir()
    _init(home)
    work = home / "scratch" / "proj"
    _write(work / "a.py")
    _fake_home(monkeypatch, home)

    r = resolve_root(work, env={})
    assert r.root != home.resolve()
    assert r.source == "cwd" and not r.scope_known


def test_홈이_저장소여도_manifest가_있으면_그_폴더를_쓴다(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _init(home)
    proj = home / "work" / "proj"
    _write(proj / "pyproject.toml", '[project]\nname="p"\n')
    _fake_home(monkeypatch, home)

    r = resolve_root(proj, env={})
    assert r.root == proj.resolve()
    assert r.source == "manifest" and r.scope_known


# ============================================================================
# 2. 파일 수집 — 중첩 Git 세 가지 경우
# ============================================================================


def _monorepo(root: Path) -> Path:
    """정상 모노레포. appA·appB 가 각자 manifest 를 가진다."""
    _write(root / ".gitignore", "ignored.txt\n")
    for app in ("appA", "appB"):
        _write(root / app / "pyproject.toml", f'[project]\nname="{app}"\n')
        _write(root / app / "src" / f"{app}.py")
    _write(root / "appA" / ".venv" / "junk.py")  # 루트 .gitignore 에 없다
    _write(root / "appA" / "node_modules" / "dep" / "index.js")
    _write(root / "appA" / "ignored.txt")
    _init(root)
    _commit(root)
    return root


def test_모노레포_중첩_프로젝트는_자기_것만_모은다(tmp_path: Path):
    mono = _monorepo(tmp_path / "mono")
    _write(mono / "appA" / "src" / "untracked.py")  # untracked

    listing = list_files(mono / "appA", DEFAULT)
    assert listing.source == "git"
    assert "src/appA.py" in listing.names  # tracked
    assert "src/untracked.py" in listing.names  # untracked


def test_모노레포에서_형제_프로젝트는_섞이지_않는다(tmp_path: Path):
    mono = _monorepo(tmp_path / "mono")
    names = list_files(mono / "appA", DEFAULT).names
    assert not any("appB" in n for n in names)
    assert not any(n.startswith("../") for n in names)


def test_대상_내부의_venv와_node_modules는_제외된다(tmp_path: Path):
    """상위 .gitignore 에 없어도 빠져야 한다. git 만 믿으면 딸려 온다."""
    mono = _monorepo(tmp_path / "mono")
    names = list_files(mono / "appA", DEFAULT).names
    assert not any(n.startswith(".venv/") for n in names)
    assert not any(n.startswith("node_modules/") for n in names)


def test_대상_내부의_ignored_파일은_제외된다(tmp_path: Path):
    mono = _monorepo(tmp_path / "mono")
    assert "ignored.txt" not in list_files(mono / "appA", DEFAULT).names


def test_모노레포에서_삭제된_tracked_파일이_변화로_나타난다(tmp_path: Path):
    mono = _monorepo(tmp_path / "mono")
    before = fingerprint(mono / "appA", DEFAULT)
    (mono / "appA" / "src" / "appA.py").unlink()
    after = fingerprint(mono / "appA", DEFAULT)
    assert before.ok and after.ok
    assert before.digest != after.digest


def test_홈의_우발적_git_아래_프로젝트는_홈을_순회하지_않는다(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _write(home / "개인메모.txt", "사적인 내용\n")
    _write(home / "다른폴더" / "무관.py")
    _init(home)
    proj = home / "work" / "proj"
    _write(proj / "pyproject.toml", '[project]\nname="p"\n')
    _write(proj / "src" / "a.py")
    _fake_home(monkeypatch, home)

    listing = list_files(proj, DEFAULT)
    assert listing.source == "walk", "홈이 상위 저장소면 git 목록을 쓰지 않는다"
    assert "src/a.py" in listing.names
    assert not any("개인메모" in n or "다른폴더" in n for n in listing.names)
    assert not any(n.startswith("..") for n in listing.names)


def test_자체_git을_가진_중첩_저장소는_자기_저장소를_쓴다(tmp_path: Path):
    outer = tmp_path / "outer"
    _write(outer / "outer.py")
    _init(outer)
    _commit(outer)

    inner = outer / "vendor" / "inner"
    _write(inner / "pyproject.toml", '[project]\nname="inner"\n')
    _write(inner / "src" / "inner.py")
    _init(inner)
    _commit(inner)

    listing = list_files(inner, DEFAULT)
    assert listing.source == "git"
    assert "src/inner.py" in listing.names
    assert not any("outer" in n for n in listing.names)


def test_git이_없으면_직접_훑는다(tmp_path: Path, monkeypatch):
    _fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    plain = tmp_path / "plain"
    _write(plain / "src" / "a.py")
    _write(plain / ".venv" / "junk.py")
    listing = list_files(plain, DEFAULT)
    assert listing.source == "walk"
    assert "src/a.py" in listing.names
    assert not any(n.startswith(".venv/") for n in listing.names)


# ============================================================================
# 3. 문서 제외 우선순위
# ============================================================================


def test_루트의_정형화된_문서는_제외된다(tmp_path: Path):
    names = list_files(_repo(tmp_path / "repo"), DEFAULT).names
    assert "README.md" not in names
    assert "CHANGELOG.md" not in names
    assert "CONTRIBUTING.md" not in names
    assert "LICENSE" not in names
    assert "docs/guide.md" not in names


def test_소스_경로의_일반_문서는_포함된다(tmp_path: Path):
    """src/architecture.md 는 코드와 함께 바뀌고 판정에 영향을 줄 수 있다.

    확장자만 보고 빼면 이런 파일이 조용히 감시 밖으로 나간다.
    """
    assert "src/architecture.md" in list_files(_repo(tmp_path / "repo"), DEFAULT).names


def test_루트여도_목록에_없는_이름은_포함된다(tmp_path: Path):
    assert "notes.txt" in list_files(_repo(tmp_path / "repo"), DEFAULT).names


def test_판정에_영향을_주는_설정_문서는_확장자와_무관하게_포함된다(tmp_path: Path):
    """.claude/CLAUDE.md 는 .md 지만 에이전트의 행동을 바꾼다. 문서가 아니다."""
    names = list_files(_repo(tmp_path / "repo"), DEFAULT).names
    assert ".claude/CLAUDE.md" in names
    assert ".claude/rules/safety.md" in names
    assert ".github/workflows/ci.yml" in names


@pytest.mark.parametrize(
    "rel,expected",
    [
        # 루트의 정형화된 문서 -> 제외
        ("README.md", False),
        ("README", False),
        ("README_ko.md", False),
        ("CHANGELOG.md", False),
        ("CHANGELOG-2026.md", False),
        ("CONTRIBUTING.rst", False),
        ("LICENSE", False),
        ("LICENSE.txt", False),
        ("LICENSE-MIT", False),
        # docs/** -> 제외
        ("docs/a.md", False),
        ("docs/deep/b.md", False),
        ("doc/a.rst", False),
        # 소스 경로의 일반 문서 -> 포함
        ("src/architecture.md", True),
        ("src/notes.txt", True),
        ("tests/fixtures/sample.rst", True),
        # 루트지만 목록에 없는 이름 -> 포함
        ("notes.txt", True),
        ("TODO.md", True),
        # 이름이 비슷한 소스 파일 -> 포함
        ("readme_generator.py", True),
        ("license_check.py", True),
        # 판정에 영향을 주는 설정 문서 -> 확장자 무관 포함
        (".claude/CLAUDE.md", True),
        (".claude/rules/x.md", True),
        (".github/workflows/ci.yml", True),
        (".github/ISSUE_TEMPLATE/bug.md", True),
        # 소스
        ("src/a.py", True),
        # 무조건 제외
        (".venv/lib/x.py", False),
        ("node_modules/x/index.js", False),
    ],
)
def test_감시_여부_우선순위가_고정된다(rel: str, expected: bool):
    assert keep(PurePosixPath(rel), DEFAULT) is expected


def test_FORCE_INCLUDE는_문서_제외보다_먼저다():
    assert ".claude" in FORCE_INCLUDE and ".github" in FORCE_INCLUDE


# ============================================================================
# 4. 감시 경로 설정 형식 (JSON)
# ============================================================================


def test_JSON_배열은_replace_다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    policy = parse_watch('["src"]')
    assert policy.mode == "replace"
    assert list_files(repo, policy).names == ("src/architecture.md", "src/core.py")


def test_replace_키도_같은_뜻이다():
    assert parse_watch('{"replace": ["src"]}').replace == ("src",)


def test_add_키는_기본에_더한다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    policy = parse_watch('{"add": ["docs"]}')
    assert policy.mode == "add"
    names = list_files(repo, policy).names
    assert "docs/guide.md" in names
    assert "src/core.py" in names
    assert "README.md" not in names


def test_공백이_든_경로도_표현할_수_있다(tmp_path: Path):
    """공백 분리 문자열로는 이 경로를 표현할 방법이 없다."""
    root = tmp_path / "proj"
    _write(root / "내 소스" / "a.py")
    _write(root / "other" / "b.py")
    policy = parse_watch('["내 소스"]')
    assert policy.replace == ("내 소스",)
    names = list_files(root, policy).names
    assert names == ("내 소스/a.py",)


def test_replace와_add를_함께_주면_오류다():
    with pytest.raises(ValueError, match="함께 줄 수 없다"):
        parse_watch('{"replace": ["a"], "add": ["b"]}')


@pytest.mark.parametrize("spec", ["src tests", "src", "{", "42"])
def test_JSON이_아니거나_형식을_모르면_오류다(spec: str):
    with pytest.raises(ValueError):
        parse_watch(spec)


def test_빈_설정은_기본_정책이다():
    assert parse_watch(None).mode == "default"
    assert parse_watch("   ").mode == "default"


def test_리스트를_직접_줘도_된다():
    assert parse_watch(["src", "tests"]).replace == ("src", "tests")


def test_감시_정책이_바뀌면_policy_hash도_바뀐다():
    a = parse_watch(None).policy_hash()
    b = parse_watch('["src"]').policy_hash()
    c = parse_watch('{"add": ["docs"]}').policy_hash()
    assert len({a, b, c}) == 3
    assert parse_watch('["src"]').policy_hash() == b


# ============================================================================
# 5. 경로·파일 안전성
# ============================================================================


@pytest.mark.parametrize(
    "rel",
    ["../escape.py", "a/../../b.py", "/abs/path.py", "\\abs\\path.py", "C:evil.py", ""],
)
def test_루트를_벗어나는_경로는_거부한다(rel: str):
    assert not is_safe_relative(rel)


@pytest.mark.parametrize("rel", ["src/a.py", "a.py", "a/b/c.py", "내 폴더/a.py"])
def test_정상_상대경로는_받는다(rel: str):
    assert is_safe_relative(rel)


class _FakeStat:
    def __init__(self, mode: int) -> None:
        self.st_mode = mode


def _patch_mode(monkeypatch, target_name: str, mode: int) -> None:
    """특정 파일에만 다른 st_mode 를 씌운다. 나머지는 진짜 os.lstat 을 쓴다.

    Windows 에서는 symlink·FIFO 를 만들 수 없다. 그래도 분기는 검증해야
    한다 -- 검증하지 못한 코드 경로를 '아마 될 것'으로 두지 않는다.
    """
    real = os.lstat

    def fake(path, *a, **k):
        if str(path).endswith(target_name):
            return _FakeStat(mode)
        return real(path, *a, **k)

    monkeypatch.setattr(hookscan.os, "lstat", fake)


POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="Windows 에서는 symlink 생성에 권한이 필요하다"
)


@POSIX_ONLY
def test_symlink_줄은_링크_문자열과_루트_안_대상_내용을_함께_담는다(tmp_path: Path):
    """가짜 readlink 로 흉내 내지 않는다. Path.resolve 가 플랫폼마다 다른 API 를
    써서 가짜가 Linux 와 Windows 에서 다른 답을 냈다."""
    root = tmp_path / "proj"
    _write(root / "src" / "a.py", "A\n")
    _write(root / "src" / "b.py", "A\n")  # 내용은 같고 경로만 다르다
    link = root / "src" / "link.py"
    link.symlink_to("a.py")

    line_a, read_a = entry_line(root, "src/link.py")
    link.unlink()
    link.symlink_to("b.py")
    line_b, read_b = entry_line(root, "src/link.py")

    assert b"\x00L:" in line_a, "링크로 기록해야 한다"
    assert line_a != line_b, "내용이 같아도 가리키는 곳이 바뀌면 달라져야 한다"
    assert read_a == read_b > 0, "루트 안 일반 파일이면 내용을 읽는다"


@POSIX_ONLY
def test_루트_안_symlink_대상_내용이_바뀌면_줄이_달라진다(tmp_path: Path):
    """예전에는 링크 문자열만 해싱했다. 그러면 pytest 가 링크를 따라 읽는
    내용의 변화를 놓친다 -- 독립 검증에서 false PASS 로 재현됐다."""
    root = tmp_path / "proj"
    target = _write(root / "src" / "real.py", "v1\n")
    (root / "src" / "link.py").symlink_to("real.py")

    line_1, _ = entry_line(root, "src/link.py")
    target.write_text("v2 아주 다른 내용\n", encoding="utf-8")
    line_2, _ = entry_line(root, "src/link.py")
    assert line_1 != line_2, "루트 안 대상의 내용 변화를 놓쳤다"


def test_특수_파일_분기는_열지_않는다(tmp_path: Path, monkeypatch):
    root = tmp_path / "proj"
    target = _write(root / "src" / "pipe", "내용 A\n")
    _patch_mode(monkeypatch, "pipe", stat.S_IFIFO | 0o666)

    line_1, read_1 = entry_line(root, "src/pipe")
    target.write_text("완전히 다른 내용 B\n", encoding="utf-8")
    line_2, read_2 = entry_line(root, "src/pipe")

    assert b"\x00S:" in line_1
    assert line_1 == line_2, "열었다면 내용이 바뀌었으니 달라졌을 것이다"
    assert read_1 == read_2 == 0


def test_없는_파일은_missing_으로_기록된다(tmp_path: Path):
    line, read = entry_line(tmp_path, "없는파일.py")
    assert b"\x00M:" in line
    assert read == 0


# --- POSIX 실제 파일 (CI 에서 확인) ---


@pytest.mark.skipif(os.name == "nt", reason="Windows 에서는 symlink 생성에 권한이 필요하다")
def test_실제_symlink도_대상을_따라가지_않는다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    outside = _write(tmp_path / "outside.txt", "v1\n")
    link = repo / "src" / "link.py"
    link.symlink_to(outside)

    before = fingerprint(repo, DEFAULT)
    outside.write_text("v2 아주 다른 내용\n", encoding="utf-8")
    assert fingerprint(repo, DEFAULT).digest == before.digest


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="이 플랫폼에는 mkfifo 가 없다")
def test_실제_FIFO도_읽지_않는다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    os.mkfifo(repo / "src" / "pipe")  # type: ignore[attr-defined]
    assert fingerprint(repo, DEFAULT).ok


# ============================================================================
# 6. fingerprint 정확도
# ============================================================================


def test_같은_크기_같은_mtime_수정도_탐지한다(tmp_path: Path):
    """mtime+size 로는 놓치는 경우다. 내용 해시라서 잡힌다."""
    repo = _repo(tmp_path / "repo")
    target = repo / "src" / "core.py"
    before = fingerprint(repo, DEFAULT)
    st = os.stat(target)
    target.write_text("A = 2\n", encoding="utf-8")  # 길이 동일
    os.utime(target, (st.st_atime, st.st_mtime))  # mtime 도 되돌린다
    after = fingerprint(repo, DEFAULT)

    assert os.stat(target).st_size == st.st_size
    assert os.stat(target).st_mtime == st.st_mtime
    assert before.digest != after.digest


def test_읽지_못한_파일이_있으면_digest가_없다(tmp_path: Path, monkeypatch):
    root = tmp_path / "proj"
    _write(root / "src" / "a.py")
    real_open = open

    def boom(path, *a, **k):
        if str(path).endswith("a.py"):
            raise OSError("읽을 수 없다")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", boom)
    fp = fingerprint(root, DEFAULT)
    assert not fp.ok
    assert fp.unreadable == ("src/a.py",)


def test_fingerprint가_파일_수와_바이트를_센다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    fp = fingerprint(repo, DEFAULT)
    assert fp.ok
    assert fp.file_count == len(list_files(repo, DEFAULT).names)
    assert fp.total_bytes > 0
    assert fp.source == "git"


def test_같은_입력이면_digest가_같다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    assert fingerprint(repo, DEFAULT).digest == fingerprint(repo, DEFAULT).digest


# ============================================================================
# 검토 지적 1 — Git index blob 과 파일 모드
# ============================================================================
#
# working tree 만 보면 두 가지를 놓친다. staged 된 다른 내용과, 실행 비트다.
# 둘 다 검증 결과를 바꿀 수 있다 -- 커밋되는 것은 index 이고, 실행 비트가
# 빠진 스크립트는 CI 에서만 실패한다.


def test_index에_staged된_내용이_fingerprint에_반영된다(tmp_path: Path):
    """worktree 를 되돌려도 index 에 다른 blob 이 남아 있으면 달라야 한다."""
    repo = _repo(tmp_path / "repo")
    target = repo / "src" / "core.py"
    original = target.read_text(encoding="utf-8")

    before = fingerprint(repo, DEFAULT)
    target.write_text("A = 999\n", encoding="utf-8")
    _git(repo, "add", "src/core.py")
    target.write_text(original, encoding="utf-8")  # worktree 만 원상복구

    after = fingerprint(repo, DEFAULT)
    assert before.ok and after.ok
    assert before.digest != after.digest


def test_index_파일_모드_변경이_fingerprint에_반영된다(tmp_path: Path):
    """update-index --chmod 는 Windows 에서도 동작한다. 플랫폼 독립 검증이다."""
    repo = _repo(tmp_path / "repo")
    before = fingerprint(repo, DEFAULT)
    _git(repo, "update-index", "--chmod=+x", "src/core.py")
    after = fingerprint(repo, DEFAULT)
    assert before.ok and after.ok
    assert before.digest != after.digest


def test_untracked_파일도_index_표기와_함께_기록된다(tmp_path: Path):
    """tracked 였다가 index 에서 빠지는 것도 변화다."""
    repo = _repo(tmp_path / "repo")
    before = fingerprint(repo, DEFAULT)
    _git(repo, "rm", "--cached", "-q", "src/core.py")  # 파일은 그대로, index 에서만 제거
    after = fingerprint(repo, DEFAULT)
    assert before.ok and after.ok
    assert before.digest != after.digest


def test_worktree_실행비트_변경이_fingerprint에_반영된다(tmp_path: Path, monkeypatch):
    """POSIX 에서 chmod +x 한 경우. 모드를 안 보면 놓친다."""
    root = tmp_path / "proj"
    _write(root / "src" / "run.sh", "#!/bin/sh\necho hi\n")

    real = os.lstat
    mode_box = {"mode": stat.S_IFREG | 0o644}

    def fake(path, *a, **k):
        if str(path).endswith("run.sh"):
            st = real(path, *a, **k)

            class M:
                st_mode = mode_box["mode"]
                st_size = st.st_size

            return M()
        return real(path, *a, **k)

    monkeypatch.setattr(hookscan.os, "lstat", fake)
    line_644, _ = entry_line(root, "src/run.sh")
    mode_box["mode"] = stat.S_IFREG | 0o755
    line_755, _ = entry_line(root, "src/run.sh")
    assert line_644 != line_755


# ============================================================================
# 검토 지적 5 — watch 상대경로 엄격 검증
# ============================================================================


@pytest.mark.parametrize(
    "bad", ["../escape", "/abs/path", "C:evil", "a/../../b", chr(92) + "abs", ""]
)
def test_watch_경로가_루트를_벗어나면_거부한다(bad: str):
    with pytest.raises(ValueError, match="상대 경로"):
        parse_watch(json.dumps([bad]))


def test_watch_경로가_문자열이_아니면_거부한다():
    with pytest.raises(ValueError):
        parse_watch('[1, 2]')


# ============================================================================
# 검토 지적 6 — 비 Git 순회의 디렉터리 symlink
# ============================================================================


def test_비Git_순회에서_디렉터리_symlink를_기록한다(tmp_path: Path, monkeypatch):
    """os.walk 는 디렉터리 symlink 를 dirnames 에만 넣고 파일로 남기지 않는다.

    그대로 두면 링크를 만들거나 방향을 바꿔도 fingerprint 가 그대로다.
    """
    _fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    root = tmp_path / "proj"
    _write(root / "src" / "a.py")
    (root / "linkdir").mkdir()

    real_islink = os.path.islink

    def fake_islink(p):
        return str(p).endswith("linkdir") or real_islink(p)

    monkeypatch.setattr(hookscan.os.path, "islink", fake_islink)
    monkeypatch.setattr(hookscan.os, "readlink", lambda p: "SOMEWHERE")

    listing = list_files(root, DEFAULT)
    assert listing.source == "walk"
    assert "linkdir" in listing.names, "디렉터리 symlink 자체가 기록돼야 한다"


def test_비Git_순회가_디렉터리_symlink_안으로_들어가지_않는다(tmp_path: Path, monkeypatch):
    _fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    root = tmp_path / "proj"
    _write(root / "src" / "a.py")
    _write(root / "linkdir" / "안쪽.py")  # 링크 대상인 척하는 실제 내용

    real_islink = os.path.islink
    monkeypatch.setattr(
        hookscan.os.path, "islink", lambda p: str(p).endswith("linkdir") or real_islink(p)
    )
    monkeypatch.setattr(hookscan.os, "readlink", lambda p: "SOMEWHERE")

    names = list_files(root, DEFAULT).names
    assert "linkdir" in names
    assert not any(n.startswith("linkdir/") for n in names), "대상을 따라가면 안 된다"


@pytest.mark.skipif(os.name == "nt", reason="Windows 에서는 symlink 생성에 권한이 필요하다")
def test_실제_디렉터리_symlink도_기록되고_따라가지_않는다(tmp_path: Path, monkeypatch):
    _fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    outside = tmp_path / "outside"
    _write(outside / "비밀.py", "밖의 내용\n")
    root = tmp_path / "proj"
    _write(root / "src" / "a.py")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    names = list_files(root, DEFAULT).names
    assert "linkdir" in names
    assert not any("비밀" in n for n in names)


# ============================================================================
# 재검토 지적 2 — Git index 조회 실패와 충돌 stage
# ============================================================================


def test_git_index를_읽지_못하면_fingerprint를_만들지_않는다(tmp_path: Path, monkeypatch):
    """index 를 못 읽은 것과 tracked 가 하나도 없는 것은 다르다.

    빈 dict 로 뭉개면 정상 digest 가 나오고, 그 digest 로 캐시 판단까지 한다.
    """
    repo = _repo(tmp_path / "repo")
    real = hookscan._git

    def fake(root, *args):
        if args and args[0] == "ls-files" and "--stage" in args:
            return None
        return real(root, *args)

    monkeypatch.setattr(hookscan, "_git", fake)
    fp = fingerprint(repo, DEFAULT)
    assert not fp.ok, "index 를 못 읽었으면 digest 를 만들면 안 된다"
    assert "index" in fp.reason


def test_충돌_stage_항목이_모두_반영된다(tmp_path: Path):
    """충돌 중에는 한 경로에 stage 1·2·3 이 함께 있다. 하나만 남기면 안 된다."""
    repo = _repo(tmp_path / "repo")
    target = repo / "src" / "core.py"
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()

    _git(repo, "checkout", "-q", "-b", "other")
    target.write_text("A = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "other")
    _git(repo, "checkout", "-q", base)
    target.write_text("A = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "base")
    _git(repo, "merge", "other")  # 충돌 발생

    entries = index_entries(repo)
    assert entries is not None
    stages = entries.get("src/core.py", "")
    assert stages.count(",") == 2, f"stage 3 개가 모두 있어야 한다: {stages!r}"


# ============================================================================
# 재검토 지적 3 — include_docs 는 진짜 bool 만
# ============================================================================


@pytest.mark.parametrize("bad", ["false", "true", 0, 1, None, []])
def test_include_docs는_진짜_bool만_허용한다(bad):
    """문자열 "false" 가 truthy 라서 참이 되면 문서 정책이 거꾸로 뒤집힌다."""
    with pytest.raises(ValueError, match="include_docs"):
        parse_watch({"add": ["docs"], "include_docs": bad})


@pytest.mark.parametrize("good", [True, False])
def test_include_docs가_bool이면_받는다(good: bool):
    assert parse_watch({"add": ["docs"], "include_docs": good}).include_docs is good


# ============================================================================
# 재검토 지적 4 — watch "." 와 빈 감시 대상
# ============================================================================


def test_watch_점은_프로젝트_전체를_뜻한다(tmp_path: Path):
    """지금은 아무것도 매칭하지 않아 감시 대상이 0 개가 된다."""
    repo = _repo(tmp_path / "repo")
    names = list_files(repo, parse_watch('["."]')).names
    assert "src/core.py" in names
    assert "README.md" in names, "명시적으로 전체를 지정했으므로 문서도 들어간다"


def test_watch_점_슬래시도_같다(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    assert list_files(repo, parse_watch('["./"]')).names == list_files(
        repo, parse_watch('["."]')
    ).names


def test_감시_대상이_비면_digest를_만들지_않는다(tmp_path: Path, monkeypatch):
    """아무것도 안 본 것과 아무것도 안 바뀐 것은 다르다.

    빈 목록의 sha256 도 그럴듯한 값이라, 그대로 두면 '변경 없음'으로 읽힌다.
    """
    _fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    fp = fingerprint(empty, DEFAULT)
    assert fp.file_count == 0
    assert not fp.ok, "감시한 것이 없으면 digest 를 만들면 안 된다"
    assert "감시" in fp.reason or "no_files" in fp.reason


# ============================================================================
# 재검토 지적 5 — git 파일 목록 실패 시 fail-open
# ============================================================================


def _break_ls_files(monkeypatch) -> None:
    """ls-files --cached --others 만 실패시킨다. rev-parse 는 그대로 둔다."""
    real = hookscan._git

    def fake(root, *args):
        if args and args[0] == "ls-files" and "--cached" in args:
            return None
        return real(root, *args)

    monkeypatch.setattr(hookscan, "_git", fake)


def test_git_파일_목록을_얻지_못하면_walk로_넘어가지_않는다(tmp_path: Path, monkeypatch):
    """Git 저장소인 줄 알면서 walk 로 대체하면 두 가지를 조용히 놓친다.

    staged 상태와, 삭제된 tracked 파일이다. walk 는 디스크에 있는 것만 본다.
    그래놓고 정상 digest 를 내면 '변경 없음'으로 읽힌다.
    """
    repo = _repo(tmp_path / "repo")
    _break_ls_files(monkeypatch)
    listing = list_files(repo, DEFAULT)
    assert listing.source != "walk"
    assert listing.names == ()


def test_git_파일_목록_실패는_fingerprint_unavailable이다(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _break_ls_files(monkeypatch)
    fp = fingerprint(repo, DEFAULT)
    assert not fp.ok
    assert "git_file_list_unavailable" in fp.reason


def test_git이_아예_없는_곳은_여전히_walk로_간다(tmp_path: Path, monkeypatch):
    """실패와 '애초에 저장소가 아님'을 뭉뚱그리지 않는다."""
    _fake_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    plain = tmp_path / "plain"
    _write(plain / "src" / "a.py")
    listing = list_files(plain, DEFAULT)
    assert listing.source == "walk"
    assert "src/a.py" in listing.names
