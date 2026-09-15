# claimtrail

**AI 에이전트가 "완료했습니다"라고 말할 때, 그 주장을 실제로 검사하고 증빙을 남깁니다.**

> Verify what an AI agent claims it finished — run the project's real checks, and produce evidence of what was (and was not) verified.

---

## 왜 만들었나

지금 나와 있는 AI 코딩 도구는 거의 전부 **생성**에 집중합니다. 코드를 짜주고, 고쳐주고, "완료했습니다"라고 말합니다.

그런데 그 말을 믿을 근거는 아무도 주지 않습니다.

기업이 AI를 본격적으로 못 쓰는 이유는 성능이 부족해서가 아닙니다. **결재하고 감사에서 설명할 근거가 없어서**입니다. "확인했다"와 "이렇게 확인했다"는 완전히 다른 말입니다.

`claimtrail`는 그 둘 사이의 간격을 메웁니다.

## 무엇을 하나

1. 프로젝트에서 **실행 가능한 검증을 찾아냅니다** — 추측하지 않고, 근거를 찾은 것만.
2. 찾은 것을 **실제로 실행합니다**.
3. 결과를 **실제 숫자로** 수집합니다 — 테스트 수, 통과, 실패.
4. **확인하지 못한 것까지 함께** 적은 증빙 리포트를 냅니다.

마지막 항목이 핵심입니다. 통과한 것만 적으면 그건 증빙이 아니라 광고입니다.

## 설치

아직 PyPI에 배포하지 않았습니다. 소스에서 설치합니다.

```bash
git clone https://github.com/ddayhyun/claimtrail-public.git
cd claimtrail-public
pip install .
```

확인한 커밋으로 고정해 설치하려면 태그를 지정합니다. 새 가상환경에서 샘플까지 돌려 보는 절차는
[docs/getting-started.md](docs/getting-started.md) 에 있습니다.

```bash
pip install "git+https://github.com/ddayhyun/claimtrail-public.git@public-2026-09-14"
```

개발 중이라면 편집 가능 모드로 설치합니다.

```bash
pip install -e .
```

의존성이 없습니다. Python 표준 라이브러리만 씁니다.

> **PyPI의 `claimcheck`는 이 프로젝트가 아닙니다.** 이 프로젝트는 처음에 `claimcheck`라는 이름이었으나, PyPI에 동명의 무관한 타인 패키지가 이미 있어 `claimtrail`로 변경했습니다. `pip install claimcheck`를 실행하지 마세요.

## 사용

```bash
# 무엇을 검증할 수 있는지만 본다 (실행 안 함)
claimtrail detect

# 실제로 실행하고 증빙 리포트를 낸다
claimtrail run

# 파일로 저장하거나 JSON으로 받는다
claimtrail run -o evidence.md
claimtrail run --format json

# 검증 범위를 설정 파일로 지정한다 (아래 '검증 범위 설정' 참고)
claimtrail run <대상> --config /경로/claimtrail.json --format md -o /경로/report.md
```

세 경로는 서로 다른 것입니다. `<대상>` 은 검사할 저장소, `--config` 는 범위 설정 파일,
`-o` 는 리포트를 쓸 곳입니다. 설정과 리포트는 **검사 대상 밖**에 두십시오 — 대상 안에 두면
다음 실행의 지문과 수집 범위에 그 파일이 섞입니다.

### 어느 Python 으로 돌리는가, 종료 코드는 어디서 읽는가

검증기는 **Claimtrail 을 실행한 그 Python** 으로 돕니다(`sys.executable -m pytest`). 프로젝트가
준비한 `.venv` 의 python 을 명시하십시오. 그 환경에 의존성이 빠져 있으면(예: `jinja2` 부재로
수집 실패) 리포트는 그 사실을 **그 환경에서 실행하지 못했다**고 남깁니다 — 시스템 Python 으로
다시 돌렸다면 그것은 **별도의 실행**이고, 원래 `.venv` 가 복구된 것이 아닙니다. 리포트의
"실행 환경" 항목이 어느 Python·어느 버전으로 돌았는지 적으므로 두 실행을 구분할 수 있습니다.

판정은 **프로세스의 종료 코드**입니다. 실행 직후에 변수에 담으십시오. 뒤에 붙는 `cat`·`tail`·정리
명령은 자기 종료 코드로 덮어씁니다 — 파이프 뒤의 `tail` 이 0 을 돌려주는 바람에 실패를
통과로 읽은 사례가 있습니다.

Bash (개발 소스를 쓰는 경우 `PYTHONPATH` 는 그 명령 한 줄에만 붙입니다):

```bash
PY="/경로/프로젝트/.venv/bin/python"          # Windows: /경로/프로젝트/.venv/Scripts/python.exe
PYTHONPATH="/경로/claimtrail/src" "$PY" -m claimtrail run "/경로/프로젝트" \
  --config "/경로/밖/claimtrail.json" --format md -o "/경로/밖/report.md"
rc=$?                                         # 바로 다음 줄에서 읽는다
cat "/경로/밖/report.md"                      # 여기서 $? 는 cat 의 것이다
echo "claimtrail exit: $rc"                   # 0=통과, 1=실패, 2=검증 불가

# 출력을 파이프에 넘겨야 한다면 PIPESTATUS 로 Claimtrail 의 코드를 보존한다
"$PY" -m claimtrail run "/경로/프로젝트" --format md -o "/경로/밖/report.md" | tee run.log
rc=${PIPESTATUS[0]}
```

PowerShell (`$env:PYTHONPATH` 를 바꿨다면 실행 뒤 되돌립니다):

```powershell
$py = "D:\경로\프로젝트\.venv\Scripts\python.exe"
$old = $env:PYTHONPATH
$env:PYTHONPATH = "D:\경로\claimtrail\src"
& $py -m claimtrail run "D:\경로\프로젝트" --config "D:\경로\밖\claimtrail.json" --format md -o "D:\경로\밖\report.md"
$rc = $LASTEXITCODE                           # 바로 다음 줄에서 읽는다
$env:PYTHONPATH = $old
Get-Content "D:\경로\밖\report.md" -Encoding utf8   # 리포트는 UTF-8. Windows PowerShell 5.1 기본 인코딩으로는 한글이 깨질 수 있다
"claimtrail exit: $rc"                        # 0=통과, 1=실패, 2=검증 불가
```

`$?` 는 PowerShell 에서 직전 명령의 성공 여부(True/False)이지 종료 코드가 아닙니다. 외부
프로세스의 코드는 `$LASTEXITCODE` 입니다.

### 종료 코드

| 코드 | 의미 |
|---|---|
| `0` | 통과 — 검증을 실행했고 전부 통과 |
| `1` | 실패 — 검증을 실행했고 실패가 있음 |
| `2` | **검증 불가** — 실행하지 못함 |

`2`가 이 도구의 핵심 설계입니다. **검증하지 못한 것을 성공으로 취급하지 않습니다.** 대부분의 파이프라인은 "검사할 게 없으면 통과"로 넘어가는데, 그게 정확히 AI가 만든 변경분이 검증 없이 통과하는 경로입니다.

`0` 은 **리포트에 적힌 검사 범위**에 대한 통과입니다. 설정(`--config`)이 있으면 그 범위, 없으면
자동 탐지 범위입니다. 리포트의 "확인하지 못한 것" 은 그 범위 밖입니다. 코드는 Claimtrail
프로세스가 돌려주는 값이므로, 위 '사용' 절처럼 실행 직후 변수에 담아 읽으십시오.

## 리포트가 가리는 것과 실행 환경

**자격증명 가림.** 검증기 출력(실패 메시지, note, 출력 끝부분, 탐지 근거)이 리포트에 실리기 전에
아래 형태의 값을 `[REDACTED]` 로 바꿉니다. Markdown 과 JSON, 화면 출력과 파일 저장, 그리고
Stop 훅의 증빙 파일까지 같은 함수를 거칩니다. 훅의 판정·캐시 규칙은 바뀌지 않습니다.

| 형태 | 예 |
|---|---|
| 키=값 · 키: 값 · `"키": "값"` (키 이름 대소문자·따옴표 무시) | `api_key`, `apikey`, `access_token`, `auth_token`, `refresh_token`, `id_token`, `token`, `secret`, `secret_key`, `client_secret`, `password`, `passwd`, `pwd` |
| Authorization 헤더 | `Authorization: Bearer …`, `Authorization: Basic …` — 헤더가 있으면 값이 무엇이든 가림. 헤더 없이 `Bearer <값>`·`Basic <값>` 만 있으면 값이 16자 이상이거나 숫자·`-._~+/=`·둘째 글자 이후 대문자를 포함할 때만 가림 |
| URL 사용자 정보의 비밀번호 | `scheme://user:비밀번호@host` |
| 접두가 뚜렷한 토큰 형식 (이름 없이 값만 있어도) | `sk-…`(16자 이상), GitHub `ghp_…`·`github_pat_…`, AWS `AKIA…`, Slack `xoxb-…`, Google `AIza…`, JWT `eyJ….eyJ….…` |

키=값 형태는 값의 길이나 숫자 포함 여부를 보지 않고 가립니다 — `password=abc` 도 비밀입니다.
대가로 `password: must be at least 8` 같은 일반 문장에서는 키 바로 뒤의 한 단어(`must`)가
지워지고 나머지는 남습니다. `tokenizer`·`max_tokens`·`token_count` 처럼 키 이름이 단어의
일부인 경우와 `KeyError: 'secret'` 처럼 값이 없는 경우는 건드리지 않습니다. 위 규칙으로 한 번
잡힌 값이 **6자 이상**이고 같은 텍스트의 다른 자리에 이름 없이 다시 나오면 그것도
가립니다(pytest 는 `assert 'sk-…' == 'expected'  where token=sk-…` 처럼 값을 먼저 보여
줍니다). 짧은 값은 전파하지 않아 무관한 단어를 훼손하지 않습니다. 가림은 축약보다 먼저
하므로 잘린 조각에 값 일부가 남지 않습니다. 헤더 없는 `basic`·`bearer` 뒤의 평범한 단어는
그대로 둡니다(`the basic idea is simple` 은 바뀌지 않습니다). 대가로 헤더 없이 나오는
알파벳만으로 된 16자 미만 토큰은 가려지지 않을 수 있습니다 — 문맥 없이는 단어와 구분할 수
없기 때문입니다.

한계를 그대로 적습니다. 이것은 **위 형태의 값**만 알아봅니다. 이름 없는 긴 난수, 개인정보,
사설 경로, 다른 이름의 키는 그대로 실립니다. 환경변수나 `.env` 를 읽어 치환하지 않습니다(그
값을 읽는 것 자체가 노출 경로입니다). 검증기가 남기는 원본 로그·JUnit 파일·화면 출력은 이
도구의 리포트가 아니므로 가려지지 않습니다. 실행 코드를 격리하지도 않습니다. 과거 리포트를
소급해 고치지 않습니다.

**실행 환경.** 일반 CLI 의 리포트에는 `## 실행 환경`(JSON 은 `environment`)이 붙습니다: Python
실행 파일(`sys.executable`)과 버전, 실제 로드된 Claimtrail 소스 폴더, 그 인터프리터의 설치
메타데이터로 읽은 검증기 버전(`pytest`, `ruff`/`flake8`, `mypy`, `build`). 한 번 수집해 두 형식이
같은 값을 씁니다. 도구를 다시 실행하거나 대상 프로젝트를 import 하지 않습니다. 값의 상태는
넷입니다 — 설치된 버전 / `미설치` / `조회 실패` / `확인하지 않음`(이번 실행에 그 검사가 없어
조회하지 않음). npm 은 Python 패키지가 아니라 여기 없고, 명령 줄로만 남습니다. 수집이 실패하면
그 사실을 적을 뿐 판정과 종료 코드는 바뀌지 않습니다. Stop 훅의 증빙에는 이 항목이 없습니다
(훅은 환경 지문을 따로 계산합니다).

## 리포트 예시

```markdown
## 판정 — 실패

## 확인한 것
| 검증 | 결과 | 숫자 | 소요 |
|---|---|---|---|
| pytest | 실패 | 3개 중 1 통과, 1 실패, 1 건너뜀 | 0.03s |
| lint | 실패 | 위반 2건 | 0.16s |

## 실패한 항목
- `tests.test_mixed::test_broken`
  - AssertionError: 숫자가 맞지 않음 assert 3 == 4
- `src/a.py:1:8`
  - F401 `os` imported but unused

## 확인하지 못한 것
- **type-check** — pyproject.toml/mypy.ini/setup.cfg/pyrightconfig.json 어디에도 mypy 또는 pyright 설정이 없음
- **npm test** — package.json이 없어 Node 프로젝트로 볼 근거가 없음

---
이 리포트는 위에 적힌 명령을 **실제로 실행한** 결과다.
여기 적히지 않은 것은 확인되지 않았다.
```

## 현재 범위

**지원**

| 검증 | 도구 | 세는 것 |
|---|---|---|
| pytest | pytest | 테스트 개수, 통과/실패/오류/건너뜀 — 수집 오류는 따로 셉니다(아래) |
| lint | ruff 또는 flake8 | 위반 건수 |
| format | ruff format --check | 형식이 다른 파일 수 — **설정 파일로 켰을 때만** 실행 |
| type-check | mypy | 타입 오류 건수 |
| build | build (PEP 517) | 산출물 개수와 이름·크기 |
| npm test | npm | **세지 않음** (종료 코드로만 판정) |

**근거는 설정 파일입니다.** lint는 `[tool.ruff]`·`ruff.toml`·`.flake8`·`setup.cfg [flake8]`·`tox.ini [flake8]` 중 하나가, type-check는 `[tool.mypy]`·`mypy.ini`·`setup.cfg [mypy]`·`[tool.pyright]`·`pyrightconfig.json` 중 하나가 있어야 실행합니다. `.py` 파일이 있다거나 타입 힌트가 보인다는 이유만으로 돌리지는 않습니다 — 그건 근거가 아니라 추측입니다.

설정이 여럿이면 **구조화된 출력을 주는 쪽**을 고릅니다(lint는 ruff, type-check는 mypy). 위반·오류 건수를 출력에서 추정하지 않고 값으로 받기 위해서입니다.

pyright는 **탐지는 하지만 아직 실행하지 않습니다.** pyright 설정만 있는 프로젝트는 통과가 아니라 '확인하지 못함'으로 남습니다.

build는 **임시 폴더로 빌드합니다**(`--outdir`). 검증 도구가 대상 프로젝트에 `dist/`를 만들어 놓으면 그건 관측이 아니라 변경이기 때문입니다. 무엇이 나왔는지는 리포트에 이름과 크기로 남습니다. 다만 격리 환경을 만드느라 **10초 안팎이 걸리고 네트워크를 쓸 수 있습니다** — 다른 검증보다 눈에 띄게 느립니다.

npm test는 **개수를 세지 않습니다.** 실제로 도는 것은 `scripts.test`에 적힌 임의의 명령이고, jest·vitest·mocha·`node --test`가 저마다 다른 형식으로 출력합니다. 출력을 정규식으로 훑어 짐작할 수는 있지만 그건 추정입니다. 그래서 종료 코드로만 판정하고, **세지 못했다는 사실을 리포트에 그대로 적습니다.**

npm test에도 근거 규칙이 있습니다. `npm init -y`가 만들어 놓는 `echo "Error: no test specified" && exit 1`은 **근거로 치지 않습니다.** 그걸 테스트로 잡으면 항상 실패하는 거짓 양성이 됩니다. 또 의존성이 선언됐는데 `node_modules`가 없으면 실행하지 않고 '확인하지 못함'으로 남깁니다 — '돌릴 수 없다'와 '돌렸는데 깨졌다'는 종료 코드가 똑같이 1이라 구분해 주어야 합니다. 이 도구는 `npm install`을 대신 실행하지 않습니다.

**아직 아님**: pyright 실행, yarn/pnpm으로 실행하기

일부러 좁게 시작했습니다. 넓은 도구를 얕게 만드는 것보다, 좁은 도구가 정확한 편이 낫습니다.

### pytest 수집 오류는 테스트 결과가 아닙니다

pytest 는 어떤 파일을 import 하지 못해 수집 단계에서 죽어도 JUnit 을 씁니다. 그 파일에는
`tests="1" errors="1"` 인 testcase 하나가 들어 있고, 그건 테스트가 아니라 "이 파일을 읽지
못했다" 입니다. 실제 사례: 루트에 놓인 1회성 스크립트 `test_e2e.py` 가 설치되지 않은
`playwright` 를 import 하다 죽어, 의도한 758개 테스트가 하나도 돌지 않았는데 리포트는
"1개 중 0 통과" 로 읽혔습니다.

그래서 리포트는 이렇게 적습니다.

- `total`/`errors` 는 JUnit 이 센 값 **그대로** (원본 집계를 조용히 바꾸지 않습니다)
- `phase: "collection"` — 위 숫자가 수집 오류 항목을 센 것이라는 표시
- `tests_ran` — JUnit 의 testcase 중 수집 오류 항목을 뺀 개수. 실행됐다고 **확인되는** 테스트 수.
  JUnit 이 없어 셀 근거가 없으면 `null` 입니다 — 0 이 아닙니다.
- `reason_code: "collection_error"`
- 실패 항목의 메시지에 짧은 `collection failure` 와 본문의 원인
  (`ModuleNotFoundError: No module named 'playwright'`) 을 **둘 다** 남깁니다. 예전에는 짧은
  message 가 있으면 본문을 읽지 않아 원인이 사라졌습니다.
- `note` 에 pytest 가 찍은 오류 요약 줄 몇 개를 남깁니다. 환경변수나 비밀값은 남기지 않습니다.

## 검증 범위 설정 (claimtrail.json)

자동 탐지는 "무엇을 돌릴 수 있는가" 를 찾습니다. 프로젝트가 "무엇을 돌려야 하는가" 는
사람이 적어야 합니다. CI 가 `ruff check` · `ruff format --check` · `pytest tests/` 세 단계인데
자동 탐지는 `ruff check` 와 루트 기준 `pytest` 만 돌린다면, 형식 검사는 영영 확인되지
않고 루트의 스크립트가 수집을 깨뜨립니다. 설정 파일이 그 간격을 메웁니다.

```json
{
  "required": ["pytest", "lint", "format"],
  "pytest": { "paths": ["tests"] },
  "format": { "tool": "ruff" }
}
```

| 항목 | 뜻 |
|---|---|
| `required` | 반드시 실행되어 통과해야 하는 검사. 이름은 러너 이름(`pytest`, `lint`, `format`, `type-check`, `build`, `npm test`) |
| `pytest.paths` | pytest 에 넘길 경로(대상 폴더 기준 상대경로). 비우면 예전처럼 루트에서 수집 |
| `format.tool` | 형식 검사 도구. 지금은 `ruff` 만. `required` 에 `format` 을 적으면 기본값이 `ruff` |

**어디에 두는가.** `<대상>/claimtrail.json` 에 두면 자동으로 읽습니다. 대상 저장소를 건드리고
싶지 않으면 아무 데나 두고 `--config 경로` 로 넘깁니다. 프로젝트 단위로 한 번 적어 두고
재사용하는 파일이지, 작업마다 완료 조건을 새로 쓰는 파일이 아닙니다.

**판정 규칙이 하나 늘어납니다.** 실패가 하나라도 있으면 실패(예전과 같음). 그다음, `required` 에
적힌 검사 중 하나라도 **실행되지 않았거나 검증 불가**면 전체는 통과가 아니라 **검증 불가(종료
코드 2)** 입니다 — 형식 검사를 요구했는데 ruff 설정이 없어 돌리지 못했다면, 나머지가 다
통과해도 통과가 아닙니다. 필수 검사가 모두 통과했을 때의 통과는 **그 설정 범위의 통과**이고,
리포트가 그렇게 적습니다. 설정에 없는 검사는 확인한 적이 없습니다.

**호환성.** 설정 파일이 없으면 탐지 목록(5종), 판정 규칙, Markdown 리포트, 종료 코드가 이 기능이
생기기 전과 같습니다. `format` 은 설정으로 켰을 때만 탐지 목록에 붙습니다 — `[tool.ruff]` 가
있다는 이유로 자동 실행하면 설정 없는 사용자의 판정이 어느 날 갑자기 바뀝니다. JSON 리포트에는
`scope`(설정 없으면 `null`), 결과마다 `cwd` · `reason_code` · `phase` · `tests_ran` 키가
추가됐습니다. 예전 키의 뜻은 바뀌지 않았습니다.

**Stop 훅은 이 설정을 읽지 않습니다.** 훅의 탐지·실행·환경 지문·캐시는 이 기능이 생기기 전과
같습니다. 그래서 `claimtrail.json` 을 바꿔도 훅의 `cached_pass` 는 영향받지 않습니다 — 훅이
설정을 반영하지 않기 때문이지, 설정 변경을 감지해서가 아닙니다. 훅에 같은 범위를 적용하려면
훅이 설정을 읽고 설정 파일을 지문에 넣는 별도 변경이 필요하며, 이 버전에는 없습니다.

## 자기 자신을 검사합니다

이 저장소는 `claimtrail`로 검증됩니다. CI가 매 푸시마다 `claimtrail run`을 실행하고, 그 종료 코드가 곧 게이트입니다. 증빙 리포트는 잡 요약에 렌더되고 아티팩트로 남습니다.

```
| 검증 | 결과 | 숫자 | 소요 |
|---|---|---|---|
| pytest | 통과 | 88개 중 88 통과 | 33.62s |
| lint | 통과 | 위반 없음 | 0.17s |
| type-check | 통과 | 오류 없음 | 0.42s |
| build | 통과 | 산출물 2개 (wheel, sdist) | 9.31s |

## 확인하지 못한 것
- **npm test** — package.json이 없어 Node 프로젝트로 볼 근거가 없음
```

마지막 줄이 핵심입니다. 해당 없는 검증을 조용히 넘기지 않고 **왜 확인하지 못했는지**를 남깁니다.

CI는 `pytest`를 두 번 돌립니다. 한 번은 직접, 한 번은 `claimtrail`을 통해서입니다. 두 결과가 갈리면 그건 `claimtrail` 자체의 버그이므로, 이 구성이 자기 회귀 테스트 역할을 합니다.

## 설계 원칙

- **추측하지 않는다** — 근거(설정 파일, 테스트 파일)를 찾은 것만 "있다"고 말합니다.
- **실패를 숨기지 않는다** — 실패한 테스트를 이름과 메시지까지 드러냅니다.
- **못 한 것을 적는다** — 확인하지 못한 항목이 리포트에 반드시 들어갑니다.
- **판단은 사람이 한다** — 코드를 자동으로 고치지 않습니다.
- **의존성을 만들지 않는다** — 표준 라이브러리만 씁니다.

## 라이선스

MIT

## Stop 훅으로 자기 검증하기

Claude Code 의 Stop 훅에 걸면, 에이전트가 턴을 마칠 때마다 대상 저장소를
실제로 검증하고 증빙을 남깁니다.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash /경로/claimtrail/.claude/claimtrail-hook.sh",
            "asyncRewake": true,
            "timeout": 300
          }
        ]
      }
    ]
  }
}
```

### 이 훅은 대상 저장소의 코드를 사용자 권한으로 실행합니다

Claimtrail 은 대상 프로젝트의 검증기를 **그 저장소가 정한 대로** 실행합니다.
실행되는 것은 Claimtrail 의 코드만이 아닙니다.

- pytest: `conftest.py`, 플러그인, 테스트 자체
- build: `pyproject.toml` 이 지정한 build backend
- npm test: `package.json` 의 임의 script
- mypy: 프로젝트가 설정한 mypy plugin

이 코드들은 훅을 돌리는 사용자의 권한으로, 그 사용자의 환경에서 돕니다.
**신뢰하지 않는 저장소를 연 세션에서는 훅을 걸지 마십시오.** 저장소를 여는
것만으로 그 저장소의 코드가 실행됩니다. 이것은 pytest 나 npm 을 직접 돌릴 때와
같은 위험이며, Claimtrail 이 새로 만드는 위험은 아니지만 자동으로 반복된다는
점이 다릅니다.

### asyncRewake 는 품질 게이트가 아닙니다

`asyncRewake: true` 는 **동기식 게이트가 아닙니다.** 에이전트의 최초 응답을
막지 않습니다. 검증은 백그라운드에서 돌고, 실패했을 때 에이전트를 **다시
깨웁니다.** "훅이 있으니 잘못된 응답이 사용자에게 가지 않는다"는 보장은
없습니다.

`asyncRewake` 를 끄면 턴이 훅 완료까지 막힙니다. 검증이 수십 초 걸리는
프로젝트에서는 사용성이 크게 나빠지므로, 그 모드에서는
`CLAIMTRAIL_HOOK_BUDGET` 을 짧게 잡아야 합니다.

### 종료 코드

| 코드 | 뜻 |
|---|---|
| 0 | 통과 / 건너뜀 / 연기, 또는 재호출에서 루프를 끊음 |
| 2 | 검증 실패·검증 불가, 또는 최초 호출에서 훅이 제 일을 못 함 |

**종료 0 은 "이번엔 막지 않는다"이지 "통과했다"가 아닙니다.** 재호출에서
루프를 끊을 때도 저장된 판정은 실패 그대로 남습니다.

### 무엇을 감시하는가

판정에 영향을 주는 것만 봅니다.

| 포함 | 제외 |
|---|---|
| 소스·테스트 전부 (tracked + untracked) | 루트의 `README*` `CHANGELOG*` `CONTRIBUTING*` `LICENSE*` |
| `.claude/**`, `.github/**` (확장자 무관) | `docs/**`, `doc/**` |
| `src/architecture.md` 같은 소스 옆 문서 | `.venv`, `node_modules`, 빌드 산출물 |

`.claude/CLAUDE.md` 는 `.md` 지만 에이전트의 행동을 바꾸므로 문서가 아닙니다.
`src/architecture.md` 는 코드와 함께 바뀌므로 감시합니다.

fingerprint 는 **내용 기반 해시**입니다. mtime+size 로는 같은 크기의 빠른
수정을 놓칩니다. git index 의 blob 과 파일 모드도 함께 봅니다 — 커밋되는
것은 index 이고, 빠진 실행 비트는 CI 에서만 터집니다.

### 환경 변수

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `CLAIMTRAIL_PROJECT_ROOT` | (자동) | 검사 대상을 명시한다 |
| `CLAIMTRAIL_WATCH` | (기본 정책) | 감시 경로. JSON 이다 |
| `CLAIMTRAIL_CACHE` | `1` | `0` 이면 이전 PASS 를 재사용하지 않고 매번 검증한다 |
| `CLAIMTRAIL_STATE_DIR` | `~/.claude/claimtrail` | 상태·증빙 보관 위치 |
| `CLAIMTRAIL_PYTHON` | (자동 탐색) | 쓸 Python 실행기 |
| `CLAIMTRAIL_HOOK_BUDGET` | `240` | 잠금 대기와 검증기 실행의 예산(초). 아래 참고 |
| `CLAIMTRAIL_RUN_TIMEOUT` | `900` | 검증기 하나의 제한 시간(초) |
| `CLAIMTRAIL_MIN_RUN_RESERVE` | `90` | 검증에 남겨 둘 시간(초) |
| `CLAIMTRAIL_BUSY_WAIT_CAP` | `90` | 다른 실행을 기다릴 상한(초) |

`CLAIMTRAIL_HOOK_BUDGET` 은 **잠금 대기와 검증기 실행**을 제한합니다. 지문 계산,
환경 지문, 증빙 기록과 보관은 예산 밖이며 보통 수 초입니다. Stop 훅 설정의
`timeout` 은 예산보다 넉넉히(예: 240 → 300 이상) 잡으십시오. 훅 timeout 이
먼저 끝나면 Claude Code 가 프로세스를 끊고, 그 실행은 `interrupted_run` 으로
남아 다음 Stop 에서 다시 검증됩니다.

`CLAIMTRAIL_WATCH` 는 JSON 입니다. 공백 분리 문자열은 받지 않습니다 —
`"내 소스/a.py"` 같은 경로를 표현할 방법이 없어 조용히 쪼개집니다.

```bash
CLAIMTRAIL_WATCH='["src", "내 폴더"]'        # 이 경로들만
CLAIMTRAIL_WATCH='{"add": ["docs"]}'          # 기본 정책 + docs
CLAIMTRAIL_WATCH='["."]'                      # 프로젝트 전체
CLAIMTRAIL_WATCH='{"include_ignored": ["config/local.toml"]}'  # git 이 무시하는 입력 파일
```

git 이 무시하는 파일은 목록에 오르지 않지만, 검증은 그것을 읽습니다. 루트의
`.env`, `.env.*` 는 기본으로 지문에 들어가고, 그 밖의 파일은 `include_ignored`
에 정확한 상대 경로로 적습니다. 내용은 어디에도 저장되지 않고 결합 digest 에만
섞입니다. 이전 PASS 는 **같은 세션, 같은 파일, 같은 ignored 입력, 같은 실행
환경(인터프리터·설치 패키지·Node)** 일 때만 재사용합니다. 새 Claude 세션의
첫 Stop 은 항상 다시 검증합니다. 루트 밖이나 깨진 곳을 가리키는 symlink 가
있으면 검증은 하되 그 결과로 건너뛰지 않습니다 -- 링크 너머는 읽지 않기
때문입니다. npm 프로젝트는 `node_modules/.package-lock.json` 이 있어야
설치 상태를 지문에 넣을 수 있고, 없으면 매번 검증합니다.

### 상태 폴더

```text
~/.claude/claimtrail/<프로젝트명>-<경로해시>/
├── state.json          마지막 실행에 대해 아는 것
├── evidence.md         최신 증빙 (fresh 로 끝난 실행만 공개)
├── hook.log            모든 호출 기록
├── runs/<run_id>.json  실행별 원시 증빙 (변경되지 않음)
├── run.lock            OS 가 프로세스 종료 시 풀어 주는 잠금
└── tmp/                실행 중 임시 파일
```

**Claimtrail 이 소유한 파일(상태·증빙·로그·잠금)은 대상 저장소에 만들지
않습니다.** 다만 검증기 자체가 남기는 것 -- pytest 의 `.pytest_cache`,
`__pycache__`, 빌드 산출물 -- 은 그 도구의 몫이며 Claimtrail 이 막지
않습니다. 폴더명에 canonical 경로 해시를 붙이므로, `project-a/backend` 와
`project-b/backend` 가 섞이지 않습니다.

**git worktree 는 별개의 프로젝트로 봅니다.** 상태 폴더는 검사 루트의
canonical 경로로 정해지므로, Claude Code 가 worktree 에서 세션을 열면
(예: `.claude/worktrees/<이름>/`) 그 worktree 만의 상태 폴더가 새로 생기고
첫 Stop 은 전체 검증을 다시 실행합니다. 본 체크아웃의 `cached_pass` 는
worktree 로 이어지지 않으며, worktree 를 지워도 그 상태 폴더는 남습니다.

### 검증하지 않고 넘어가는 경우

| 상황 | 이유 |
|---|---|
| `fresh` + `pass` + 같은 fingerprint + 유효한 증빙 | 다시 볼 이유가 없다 |
| `background_tasks` 가 실행 중 | 턴이 실제로 끝난 것이 아니다. 검증하지 않고 연기한다 |
| 다른 실행이 잠금을 보유 중 | 예산 안에서 기다렸다가, 못 잡으면 검증 불가로 남긴다 |

**실패·검증 불가·stale 은 파일이 그대로여도 다시 검증합니다.** 그러지 않으면
실패가 "변경 없음"이라는 이유로 조용히 사라집니다.

### 시간은 어디에 남는가

`hook.log` 의 `run` 과 `cached_pass` 줄 끝에 단계별 벽시계가 붙습니다.

```text
t=hook_internal_total:113.2 lock:0.000 fp_before:0.31 detect:0.005 context:1.2
  execute:68.4 evidence:0.4 fp_after:0.3 archive:0.1 state_publish:0.2 other:42.3
```

`hook_internal_total` 은 Python 프로세스 안의 시간입니다. 셸 래퍼와 인터프리터
기동은 밖입니다. `other` 는 위 단계에 속하지 않은 나머지입니다.

증빙의 표에는 두 시간이 있습니다. **보고 시간**은 도구가 말한 것이고(pytest 는
JUnit 의 테스트 시간 합계라 수집·import·기동이 빠집니다), **벽시계**는 프로세스가
실제로 쓴 시간입니다. pytest 에서 둘은 10초 넘게 다를 수 있습니다.

### 훅 떼기와 복원

훅은 `.claude/settings.local.json` 의 Stop 항목 하나입니다. 떼려면 그 항목을
지우거나 파일을 지웁니다. 다음을 함께 확인하십시오.

- **worktree.** 일부 Claude Desktop/Code 의 worktree 생성 흐름은 gitignored
  `settings.local.json` 을 worktree 로 복사합니다. 본 체크아웃의 파일만 지우면
  그 worktree 에서 연 세션은 훅을 계속 부릅니다. `.claude/worktrees/*/.claude/`
  와 `git worktree list` 의 각 경로를 확인하십시오.
- **상태 폴더.** `~/.claude/claimtrail/<프로젝트명>-<해시>/` 는 훅을 떼도 남습니다.
  증빙과 로그가 필요 없으면 폴더째 지워도 됩니다. Claimtrail 은 대상 저장소 안에
  아무것도 만들지 않으므로 저장소 쪽에는 지울 것이 없습니다.
- **다시 걸기.** 같은 설정 항목을 되돌리면 됩니다. 상태 폴더가 남아 있어도 새
  세션의 첫 Stop 은 다시 검증하므로 옛 PASS 가 재사용되지 않습니다.
