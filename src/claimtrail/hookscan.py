"""검사 대상을 찾고 그 상태를 재는 일.

hookstate 와 나눈 이유
    이쪽은 '무엇을 볼 것인가'가 바뀔 때 손댄다 -- 모노레포 구조, 새 언어,
    ignore 규칙, 경로 안전성. 저쪽은 '한 번의 실행을 어떻게 기록할 것인가'가
    바뀔 때 손댄다 -- 상태 필드, 알림 억제, 증빙 승격. 두 축은 함께 움직이지
    않는다. 한 파일에 두면 모노레포 대응 하나 고치는데 알림 로직을 읽어야 한다.

여기서 지키는 것
    1. 홈 디렉터리나 상위 저장소를 대상으로 삼지 않는다.
    2. fingerprint 는 내용 기반이다. mtime+size 로는 같은 크기 수정을 놓친다.
    3. 루트 밖으로 나가지 않는다. symlink 도 따라가지 않는다.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

# 이 파일이 있으면 그 폴더를 프로젝트 루트로 본다. 검증 설정 파일
# (ruff.toml, mypy.ini 등)은 루트 표지로 쓰지 않는다 -- 하위 폴더에도
# 놓일 수 있어 루트를 잘못 좁힌다.
MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "package.json",
)

# 판정에 영향을 주는 설정 문서. 확장자와 무관하게 항상 포함한다.
# .claude/CLAUDE.md 는 .md 지만 에이전트의 행동을 바꾸므로 문서가 아니다.
FORCE_INCLUDE = (".claude", ".github")

# 판정에 영향을 주지 않는 문서. 여기를 감시하면 문서 한 줄 고칠 때마다
# 수십 초짜리 검증이 돌아 훅이 방해가 된다.
#
# 다만 확장자만으로 판단하지 않는다. src/architecture.md 처럼 소스 옆에 둔
# 설계 메모는 코드와 함께 바뀌고 판정에 영향을 줄 수 있다. 그래서 위치로
# 판단한다 -- 저장소 루트의 정형화된 문서와 docs/ 아래만 뺀다.
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
DOC_DIRS = ("docs", "doc")
ROOT_DOC_PREFIXES = ("readme", "changelog", "contributing", "license", "licence")

# 어떤 경로로 목록을 얻든 항상 빼는 폴더.
# git 의 --exclude-standard 는 그 저장소의 ignore 규칙만 따른다. 모노레포
# 루트의 .gitignore 에 .venv/ 가 없으면 하위 프로젝트의 .venv 가 딸려 온다.
# 실제로 그렇게 나온다 -- 확인했다.
SKIP_DIRS = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".eggs",
)

# fingerprint 항목 종류. 무엇을 어떻게 읽었는지 남겨야 나중에 해석할 수 있다.
KIND_FILE = "F"
KIND_LINK = "L"
KIND_MISSING = "M"
KIND_SPECIAL = "S"

_CHUNK = 1 << 20


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- git 호출 ---------------------------------------------------------------


def _git(root: Path, *args: str) -> str | None:
    """git 을 부른다. 없거나 실패하면 None. 예외로 훅을 죽이지 않는다."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def git_root(start: Path) -> Path | None:
    out = _git(start, "rev-parse", "--show-toplevel")
    if not out:
        return None
    line = out.strip()
    return Path(line) if line else None


def is_boundary(p: Path) -> bool:
    """더 올라가면 안 되는 지점.

    홈 디렉터리가 git 저장소인 사람이 있다. 그런 환경에서 rev-parse 는
    어떤 하위 폴더에서든 홈 전체를 반환한다. 그걸 프로젝트 루트로 받으면
    훅이 홈 전체를 해싱한다. 이 저장소를 개발한 PC 가 실제로 그렇다.
    """
    if p.parent == p:
        return True
    try:
        return p == Path.home().resolve()
    except (OSError, RuntimeError):
        return False


# --- 프로젝트 루트 ----------------------------------------------------------


@dataclass(frozen=True)
class RootResolution:
    """대상 저장소 루트와, 그 판단이 믿을 만한지."""

    root: Path
    source: str  # env | manifest | git | cwd
    scope_known: bool
    reason: str = ""


def resolve_root(cwd: Path, env: Mapping[str, str] | None = None) -> RootResolution:
    """대상 저장소 루트를 정한다.

    훅 스크립트의 위치로 정하지 않는다. 그러면 도구 설치 위치와 검사 대상이
    묶여서 claimtrail 저장소 말고는 검사할 수 없다.

    우선순위
        1. CLAIMTRAIL_PROJECT_ROOT (명시)
        2. cwd 에서 위로 올라가며 만나는 첫 manifest 폴더.
           Git 루트나 경계에서 멈춘다.
        3. Git 루트 (경계가 아닐 때)
        4. canonical cwd -- 단 감시 범위가 불명확하므로 scope_known=False
    """
    envmap: Mapping[str, str] = os.environ if env is None else env

    explicit = (envmap.get("CLAIMTRAIL_PROJECT_ROOT") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_dir():
            return RootResolution(p.resolve(), "env", True)
        return RootResolution(
            Path(cwd).resolve(),
            "env",
            False,
            f"CLAIMTRAIL_PROJECT_ROOT 가 폴더가 아니다 — {explicit}",
        )

    cwd = Path(cwd).resolve()
    top = git_root(cwd)
    top = top.resolve() if top else None
    if top is not None and is_boundary(top):
        top = None  # 홈이 저장소인 경우. 저장소가 아닌 것으로 친다.

    here = cwd
    while True:
        if any((here / m).is_file() for m in MANIFESTS):
            return RootResolution(here, "manifest", True)
        if top is not None and here == top:
            break
        if is_boundary(here):
            break
        here = here.parent

    if top is not None:
        return RootResolution(top, "git", True)

    return RootResolution(
        cwd,
        "cwd",
        False,
        "Git 저장소가 아니고 프로젝트 manifest 도 없어 감시 범위를 정할 수 없다",
    )


# --- 감시 정책 --------------------------------------------------------------

# 감시 경로에서 "." 이 뜻하는 것. 아무것도 매칭하지 않는 빈 경로가 아니라
# 프로젝트 전체다. 예전에는 조용히 0 개를 감시했다.
WHOLE_PROJECT = "."

# 기본으로 지문에 넣는 ignored 입력. 루트의 .env 계열만이다. git 은 이 파일을
# 목록에 올리지 않지만 테스트는 읽는다 -- 이것만 바뀌어도 판정이 달라진다.
# 내용은 어디에도 저장하지 않고 결합 digest 에만 섞인다.
DEFAULT_IGNORED_INPUTS = (".env", ".env.*")

WATCH_FORMAT_HELP = (
    'CLAIMTRAIL_WATCH 는 JSON 이다. 대체는 ["src", "내 폴더"] 또는 '
    '{"replace": [...]}, 추가는 {"add": [...]}. '
    "공백 분리 문자열은 공백이 든 경로를 표현할 수 없어 받지 않는다."
)


@dataclass(frozen=True)
class WatchPolicy:
    """무엇을 감시할지. 정책이 바뀌면 이전 PASS 를 재사용하면 안 된다."""

    replace: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    include_docs: bool = False
    # git 이 무시하지만 검증이 읽는 파일. 정확한 상대 경로만 받는다.
    include_ignored: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        if self.replace:
            return "replace"
        return "add" if self.extra else "default"

    def policy_hash(self) -> str:
        payload = json.dumps(
            {
                "replace": sorted(self.replace),
                "extra": sorted(self.extra),
                "include_docs": self.include_docs,
                "include_ignored": sorted(self.include_ignored),
                "default_ignored": list(DEFAULT_IGNORED_INPUTS),
                "force_include": sorted(FORCE_INCLUDE),
                "doc_suffixes": sorted(DOC_SUFFIXES),
                "doc_dirs": sorted(DOC_DIRS),
                "root_doc_prefixes": sorted(ROOT_DOC_PREFIXES),
                "skip_dirs": sorted(SKIP_DIRS),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return _sha256(payload.encode("utf-8"))[:16]


def _as_bool(value: object, key: str) -> bool:
    """truthy 가 아니라 진짜 bool 만 받는다.

    문자열 "false" 는 truthy 다. 그대로 두면 문서 정책이 거꾸로 뒤집히고,
    아무도 눈치채지 못한 채 감시 범위가 달라진다.
    """
    if not isinstance(value, bool):
        raise ValueError(f"include_docs 는 true 또는 false 여야 한다: {value!r} ({key})")
    return value


def _as_paths(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} 는 문자열 배열이어야 한다. {WATCH_FORMAT_HELP}")
    out = []
    for v in value:
        item = v.strip().replace("\\", "/")
        # "." 와 "./" 는 프로젝트 전체를 뜻한다. 정규화해 한 형태로 모은다.
        # 빈 문자열은 여기 넣지 않는다 -- 그건 의도가 아니라 설정 실수이고,
        # 전체로 받아주면 감시 범위가 조용히 넓어진다.
        if item.rstrip("/") == ".":
            out.append(WHOLE_PROJECT)
            continue
        if not item or not is_safe_relative(item):
            raise ValueError(f"{key} 의 항목은 루트 안의 상대 경로여야 한다: {v!r}")
        out.append(item)
    return tuple(out)


def _as_ignored(value: object, key: str) -> tuple[str, ...]:
    """include_ignored 항목. 루트 안의 정확한 상대 경로만.

    "." 이나 빈 값은 받지 않는다 -- ignored 파일 전체를 읽겠다는 뜻이 되고,
    그것이 어디까지인지 아무도 예측할 수 없다. glob 은 뒤로 미뤘다.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} 는 문자열 배열이어야 한다. {WATCH_FORMAT_HELP}")
    out = []
    for v in value:
        item = v.strip().replace("\\", "/")
        if not item or item.rstrip("/") == "." or not is_safe_relative(item):
            raise ValueError(f"{key} 의 항목은 루트 안의 파일 상대 경로여야 한다: {v!r}")
        out.append(item.rstrip("/"))
    return tuple(sorted(set(out)))


def parse_watch(
    spec: str | Sequence[str] | Mapping[str, object] | None,
    include_docs: bool = False,
) -> WatchPolicy:
    """감시 경로 설정을 읽는다.

    공백 분리 문자열은 받지 않는다. "my dir/src" 같은 경로를 표현할 방법이
    없어서, 조용히 두 경로로 쪼개지고 아무도 눈치채지 못한다.
    """
    if spec is None:
        return WatchPolicy(include_docs=include_docs)

    data: object
    if isinstance(spec, str):
        text = spec.strip()
        if not text:
            return WatchPolicy(include_docs=include_docs)
        try:
            data = json.loads(text)
        except ValueError as exc:
            msg = f"CLAIMTRAIL_WATCH 를 JSON 으로 읽지 못했다: {exc}. {WATCH_FORMAT_HELP}"
            raise ValueError(msg) from exc
    else:
        data = spec

    if isinstance(data, list):
        return WatchPolicy(replace=_as_paths(data, "replace"), include_docs=include_docs)

    if isinstance(data, Mapping):
        docs = (
            _as_bool(data["include_docs"], "include_docs")
            if "include_docs" in data
            else include_docs
        )
        ignored = (
            _as_ignored(data["include_ignored"], "include_ignored")
            if "include_ignored" in data
            else ()
        )
        has_replace = "replace" in data
        has_add = "add" in data
        if has_replace and has_add:
            raise ValueError("replace 와 add 를 함께 줄 수 없다. 무엇을 의도했는지 알 수 없다.")
        if has_replace:
            return WatchPolicy(
                replace=_as_paths(data["replace"], "replace"),
                include_docs=docs,
                include_ignored=ignored,
            )
        if has_add:
            return WatchPolicy(
                extra=_as_paths(data["add"], "add"),
                include_docs=docs,
                include_ignored=ignored,
            )
        if "include_ignored" in data:
            return WatchPolicy(include_docs=docs, include_ignored=ignored)
        raise ValueError(
            f"replace, add, include_ignored 중 하나가 있어야 한다. {WATCH_FORMAT_HELP}"
        )

    raise ValueError(f"CLAIMTRAIL_WATCH 형식을 알 수 없다. {WATCH_FORMAT_HELP}")


# --- 경로 판단 --------------------------------------------------------------


def is_safe_relative(rel: str) -> bool:
    """프로젝트 루트 밖으로 벗어나는 경로를 거부한다.

    resolve() 로 판단하지 않는다. resolve 는 symlink 를 따라가므로, 루트
    안의 symlink 가 밖을 가리키면 '밖'으로 보이거나 그 반대가 된다.
    여기서는 경로 문자열만 본다.
    """
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return False
    if len(rel) > 1 and rel[1] == ":":  # C:foo 같은 Windows 드라이브 표기
        return False
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute():
        return False
    return ".." not in p.parts


def _under_any(rel: PurePosixPath, roots: Sequence[str]) -> bool:
    for r in roots:
        if r == WHOLE_PROJECT:
            return True
        rp = PurePosixPath(r.replace("\\", "/").strip("/"))
        if not rp.parts:
            continue
        if rel.parts[: len(rp.parts)] == rp.parts:
            return True
    return False


def _is_skipped(rel: PurePosixPath) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts[:-1]) or rel.parts[0] in SKIP_DIRS


def _is_root_doc_name(stem: str) -> bool:
    """README, CHANGELOG-2026, LICENSE-MIT 같은 이름인지.

    단순 startswith 는 readme_generator 까지 잡는다. 접두어 다음이 글자나
    숫자면 다른 낱말이므로 제외하지 않는다.
    """
    s = stem.lower()
    for prefix in ROOT_DOC_PREFIXES:
        if s == prefix:
            return True
        if s.startswith(prefix) and not s[len(prefix)].isalnum():
            return True
    return False


def _is_doc(rel: PurePosixPath) -> bool:
    """문서인지. 확장자가 아니라 위치와 이름으로 판단한다."""
    if rel.parts and rel.parts[0] in DOC_DIRS:
        return True
    if len(rel.parts) != 1:
        return False  # 루트가 아니면 문서로 보지 않는다
    if rel.suffix.lower() not in ("", *DOC_SUFFIXES):
        return False  # 코드 확장자면 이름이 비슷해도 문서가 아니다
    return _is_root_doc_name(rel.stem)


def keep(rel: PurePosixPath, policy: WatchPolicy) -> bool:
    """이 경로를 감시할지.

    우선순위가 중요하다. .claude/CLAUDE.md 는 .md 지만 에이전트의 행동을
    바꾸므로 문서 제외보다 먼저 포함으로 확정한다.
    """
    if _is_skipped(rel):
        return False
    if policy.replace:
        return _under_any(rel, policy.replace)
    if _under_any(rel, FORCE_INCLUDE):
        return True
    if _is_doc(rel) and not policy.include_docs:
        return bool(policy.extra) and _under_any(rel, policy.extra)
    return True


# --- 파일 수집 --------------------------------------------------------------


def index_entries(root: Path) -> dict[str, str] | None:
    """git index 의 파일 모드와 blob 해시.

    working tree 만 보면 staged 된 다른 내용을 놓친다. 커밋되는 것은 index
    이므로, worktree 를 되돌려도 index 가 다르면 검증 대상이 달라진 것이다.
    실행 비트도 여기 들어 있다 -- 빠진 실행 비트는 CI 에서만 터진다.

    읽지 못하면 None 을 돌려준다. 빈 dict 로 뭉개면 'tracked 가 하나도
    없다'와 구분이 안 되고, 그 상태로 정상 digest 가 만들어진다.

    충돌 중에는 한 경로에 stage 1·2·3 이 함께 있다. 마지막 것만 남기면
    무엇과 무엇이 충돌 중인지가 fingerprint 에서 사라진다.

    ls-files --stage 출력 한 줄: "<mode> <sha> <stage>" TAB "<path>"
    """
    out = _git(root, "ls-files", "--stage", "-z", "--", ".")
    if out is None:
        return None
    staged: dict[str, list[str]] = {}
    for chunk in out.split("\0"):
        if not chunk or "\t" not in chunk:
            continue
        meta, path = chunk.split("\t", 1)
        parts = meta.split()
        if len(parts) < 3:
            continue
        mode, sha, stage = parts[0], parts[1], parts[2]
        staged.setdefault(path.replace("\\", "/"), []).append(f"{mode}:{sha}:{stage}")
    return {k: ",".join(sorted(v)) for k, v in staged.items()}


@dataclass(frozen=True)
class FileList:
    names: tuple[str, ...]
    source: str  # git | walk | unavailable
    detail: str = ""
    # 목록을 얻지 못했을 때 왜인지. source == "unavailable" 일 때만 채운다.
    reason: str = ""


def list_files(root: Path, policy: WatchPolicy) -> FileList:
    """감시할 상대 경로 목록.

    git 이 있으면 tracked 와 untracked 를 모두 가져온다. tracked 만 보면 새로
    만든 파일이 안 보이고, untracked 만 보면 지워진 파일이 안 보인다.

    대상이 저장소 최상위가 아니어도 git 을 쓴다. `git -C <대상>` 으로 부르면
    출력이 대상 기준 상대경로이고 형제 프로젝트는 애초에 안 나온다 --
    확인했다. 다만 상위 저장소가 홈처럼 경계면 쓰지 않는다.
    """
    root = Path(root)
    top = git_root(root)
    top = top.resolve() if top else None
    use_git = top is not None and not is_boundary(top)

    raw: list[str] = []
    source = "walk"
    detail = ""
    if use_git:
        out = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".")
        if out is None:
            # Git 저장소인 줄 알면서 walk 로 대체하면 두 가지를 조용히 놓친다.
            # staged 상태와, 삭제된 tracked 파일이다. walk 는 디스크에 있는
            # 것만 본다. 그래놓고 정상 digest 를 내면 '변경 없음'으로 읽힌다.
            return FileList(
                (),
                "unavailable",
                "git 저장소인데 파일 목록을 얻지 못했다",
                "git_file_list_unavailable: git 저장소인데 파일 목록을 얻지 못했다",
            )
        source = "git"
        assert top is not None
        detail = "저장소 최상위" if top == root.resolve() else "저장소 하위 경로"
        raw = [n for n in out.split("\0") if n]
    else:
        detail = "git 없음 또는 상위 저장소가 경계"
        for dirpath, dirnames, filenames in os.walk(root):
            kept = []
            for d in dirnames:
                if d in SKIP_DIRS:
                    continue
                sub = Path(dirpath) / d
                # os.walk 는 디렉터리 symlink 를 dirnames 에만 넣고 아무것도
                # 남기지 않는다. 그대로 두면 링크를 만들거나 가리키는 곳을
                # 바꿔도 fingerprint 가 그대로다. 링크 자체는 기록하되
                # 내려가지는 않는다 -- 따라가면 루트 밖을 읽게 된다.
                if os.path.islink(str(sub)):
                    with contextlib.suppress(ValueError):
                        raw.append(sub.relative_to(root).as_posix())
                    continue
                kept.append(d)
            dirnames[:] = kept
            for fn in filenames:
                try:
                    raw.append((Path(dirpath) / fn).relative_to(root).as_posix())
                except ValueError:
                    continue

    seen: set[str] = set()
    keepers: list[str] = []
    for n in raw:
        if not is_safe_relative(n):
            continue
        rel = PurePosixPath(n.replace("\\", "/"))
        if not keep(rel, policy):
            continue
        key = rel.as_posix()
        if key not in seen:
            seen.add(key)
            keepers.append(key)

    keepers.sort()
    return FileList(tuple(keepers), source, detail)


# --- fingerprint ------------------------------------------------------------


class Unreadable(Exception):
    def __init__(self, rel: str) -> None:
        super().__init__(rel)
        self.rel = rel


@dataclass(frozen=True)
class Fingerprint:
    """감시 대상의 상태 요약.

    digest 가 None 이면 계산하지 못한 것이다. 그때는 PASS 가 아니라
    UNVERIFIED 다 -- 확인하지 못한 것을 성공으로 취급하지 않는다.
    """

    digest: str | None
    file_count: int = 0
    total_bytes: int = 0
    unreadable: tuple[str, ...] = ()
    elapsed_sec: float = 0.0
    source: str = ""
    # digest 가 없을 때 왜 없는지. '못 잰 것'과 '안 바뀐 것'은 다르다.
    reason: str = ""
    # 내용을 끝까지 따라가지 못한 항목. 루트 밖·깨진·디렉터리 링크다.
    # digest 는 있어서 stale 판단에는 쓰지만, 이 지문으로 건너뛰지는 않는다.
    # pytest 는 링크를 따라 실제 내용을 읽으므로 그 내용이 판정의 입력이다.
    uncacheable: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.digest is not None

    @property
    def cacheable(self) -> bool:
        return self.ok and not self.uncacheable


@dataclass(frozen=True)
class Entry:
    """한 항목의 fingerprint 한 줄. cacheable 이 False 면 내용을 끝까지 못 봤다."""

    line: bytes
    read: int = 0
    cacheable: bool = True


def _hash_file(full: Path, rel: str) -> tuple[str, int]:
    h = hashlib.sha256()
    read = 0
    try:
        with open(full, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
    except OSError as exc:
        raise Unreadable(rel) from exc
    return h.hexdigest(), read


def _link_entry(root: Path, rel: str, full: Path) -> Entry:
    """symlink 항목.

    링크 문자열은 늘 해싱한다 -- 가리키는 곳이 바뀐 사실을 놓치지 않기 위해.
    대상이 루트 안의 일반 파일이면 내용도 해싱한다. 검증기는 링크를 따라
    실제 내용을 읽으므로 그 내용이 판정의 입력이다. 독립 검증에서 루트 밖
    .env 링크의 대상만 바꿨는데 cached_pass 가 나온 false PASS 가 재현됐다.
    루트 밖·깨진·디렉터리·특수 대상은 읽지 않는다. 대신 그 지문으로는
    건너뛰지 않는다 -- 읽지 않은 것을 안다고 말하지 않는다.
    """
    try:
        target = os.readlink(full)
    except OSError as exc:
        raise Unreadable(rel) from exc
    head = f"{rel}\0{KIND_LINK}:{_sha256(target.encode('utf-8'))}"
    try:
        resolved = full.resolve(strict=True)
        inside = resolved.is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError):
        # 깨진 링크나 순환. 가리키는 것이 없으니 내용도 없다.
        return Entry(f"{head}:broken\n".encode(), 0, cacheable=False)
    if not inside:
        return Entry(f"{head}:outside\n".encode(), 0, cacheable=False)
    try:
        st = resolved.stat()
    except OSError:
        return Entry(f"{head}:broken\n".encode(), 0, cacheable=False)
    if not stat.S_ISREG(st.st_mode):
        return Entry(f"{head}:not-file\n".encode(), 0, cacheable=False)
    digest, read = _hash_file(resolved, rel)
    perm = stat.S_IMODE(st.st_mode)
    return Entry(f"{head}:{digest}:{perm:o}\n".encode(), read)


def entry(root: Path, rel: str) -> Entry:
    """한 파일의 fingerprint 항목.

    FIFO·device·socket 은 열지 않는다 -- 열면 훅이 영영 멈출 수 있다.
    """
    full = root / rel
    try:
        st = os.lstat(full)
    except OSError:
        return Entry(f"{rel}\0{KIND_MISSING}:-\n".encode())

    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return _link_entry(Path(root), rel, Path(full))
    if not stat.S_ISREG(mode):
        return Entry(f"{rel}\0{KIND_SPECIAL}:-\n".encode())

    digest, read = _hash_file(Path(full), rel)
    # 모드도 남긴다. 실행 비트만 바뀐 스크립트는 내용이 같아도 다른 대상이다.
    perm = stat.S_IMODE(mode)
    return Entry(f"{rel}\0{KIND_FILE}:{digest}:{perm:o}\n".encode(), read)


def entry_line(root: Path, rel: str) -> tuple[bytes, int]:
    """entry() 의 (줄, 읽은 바이트). 예전 호출자를 위해 남긴다."""
    e = entry(root, rel)
    return e.line, e.read


def ignored_inputs(root: Path, policy: WatchPolicy, listed: set[str]) -> tuple[str, ...]:
    """git 목록에 없지만 지문에 넣을 파일. 이미 목록에 있는 것은 뺀다.

    기본은 루트의 .env 계열뿐이다. 하위 폴더는 보지 않는다 -- 어디까지
    읽는지 예측할 수 있어야 한다. 없는 파일도 돌려준다. entry_line 이
    부재로 기록하므로 생기는 것도 변화로 잡힌다.
    """
    root = Path(root)
    found: set[str] = set()
    for pattern in DEFAULT_IGNORED_INPUTS:
        for p in root.glob(pattern):
            if p.is_dir():
                continue
            found.add(p.name)
    found.update(policy.include_ignored)
    return tuple(
        sorted(n for n in found if n not in listed and not _is_skipped(PurePosixPath(n)))
    )


def fingerprint(root: Path, policy: WatchPolicy) -> Fingerprint:
    """감시 대상 전체의 내용 기반 해시.

    mtime+size 로는 부족하다. 같은 크기의 빠른 수정과 파일시스템 시간
    해상도 때문에 동시 편집을 놓칠 수 있다. 놓치면 검증 중에 바뀐 코드에
    PASS 가 붙는다 -- 이 도구가 막으려는 바로 그 일이다.
    """
    started = datetime.now(timezone.utc)
    listing = list_files(root, policy)

    def elapsed() -> float:
        return (datetime.now(timezone.utc) - started).total_seconds()

    if listing.source == "unavailable":
        # 목록 자체를 못 얻었다. walk 로 대체하지 않은 것은 의도된 것이다.
        return Fingerprint(None, 0, 0, (), elapsed(), listing.source, listing.reason)

    index: dict[str, str] = {}
    if listing.source == "git":
        found = index_entries(Path(root))
        if found is None:
            # index 를 못 읽었다. 무엇이 staged 인지 모르는 채로 digest 를
            # 만들면, 그 digest 로 '변경 없음' 판단까지 하게 된다.
            return Fingerprint(
                None, len(listing.names), 0, (), elapsed(), listing.source,
                "git_index_unavailable: git index 를 읽지 못했다",
            )
        index = found

    if not listing.names:
        # 아무것도 안 본 것과 아무것도 안 바뀐 것은 다르다. 빈 목록의
        # sha256 도 그럴듯한 값이라, 그대로 두면 '변경 없음'으로 읽힌다.
        return Fingerprint(
            None, 0, 0, (), elapsed(), listing.source,
            "no_files_watched: 감시 대상이 하나도 없다",
        )

    h = hashlib.sha256()
    unreadable: list[str] = []
    uncacheable: list[str] = []
    total = 0
    for rel in listing.names:
        try:
            e = entry(Path(root), rel)
        except Unreadable as exc:
            unreadable.append(exc.rel)
            continue
        if not e.cacheable:
            uncacheable.append(rel)
        line, read = e.line, e.read
        h.update(line)
        # index 상태를 같은 순서로 이어 붙인다. tracked 가 아니면 "-".
        h.update(f"{rel}\0I:{index.get(rel, '-')}\n".encode())
        total += read

    # ignored 입력은 git 이 모른다. 접두사로 구분해 tracked 항목과 섞이지
    # 않게 한다. 없는 파일은 entry_line 이 부재로 적는다.
    for rel in ignored_inputs(Path(root), policy, set(listing.names)):
        try:
            e = entry(Path(root), rel)
        except Unreadable as exc:
            unreadable.append(exc.rel)
            continue
        if not e.cacheable:
            uncacheable.append(rel)
        h.update(b"IGNORED\0" + e.line)
        total += e.read

    took = elapsed()
    if unreadable:
        return Fingerprint(
            None, len(listing.names), total, tuple(sorted(unreadable)), took, listing.source,
            "unreadable_files: 읽지 못한 파일이 있다",
        )
    return Fingerprint(
        h.hexdigest(),
        len(listing.names),
        total,
        (),
        took,
        listing.source,
        uncacheable=tuple(sorted(uncacheable)),
    )
