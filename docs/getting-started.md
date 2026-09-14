# Claimtrail 시작하기

Claimtrail 은 프로젝트에 이미 있는 검사(pytest, ruff 등)를 **실제로 실행**하고, 통과·실패·확인하지
못한 항목을 리포트 하나로 남기는 도구입니다. AI 코딩 도구가 "완료했습니다" 라고 말한 뒤 그 주장을
검사 결과로 확인하는 용도로 만들었습니다. AI 의 모든 주장을 이해하거나 코드의 안전성을 보증하지
않습니다. 여기 적히지 않은 것은 확인되지 않은 것입니다.

이 문서는 새 가상환경에서 **샘플 프로젝트**를 검사해 보는 것까지 안내합니다. 그다음 자기
프로젝트에 적용하는 방법을 짧게 덧붙입니다.

| 구분 | 값 |
|---|---|
| 패키지 버전 | `0.5.1` (`claimtrail --version`) |
| 확인한 소스 | 태그 `public-2026-09-14` (이 저장소의 첫 공개 커밋. 코드·샘플·이 문서가 같은 커밋에 있습니다. 실제 SHA 는 `git rev-parse public-2026-09-14`) |
| 샘플 위치 | 같은 커밋의 `examples/`. 샘플은 패키지가 아니라 소스와 함께 받습니다 |
| 확인한 환경 | 작성자 PC: Windows 11 / Python 3.11.9. CI: Ubuntu Bash · Windows PowerShell / Python 3.12 (`.github/workflows/sample.yml`) |

> **설치 경로.** 공개 저장소의 소스를 태그로 고정해 설치합니다. PyPI 에는 배포되어 있지 않으므로
> `pip install claimtrail` 로는 받을 수 없습니다(PyPI 의 `claimcheck` 는 무관한 패키지입니다).

## 1. 준비물

- Python 3.9 이상. 이 문서는 3.11·3.12 로 확인했습니다.
- `git` (샘플과 소스를 받는 데 씁니다).
- 인터넷 (pip 설치).

Claimtrail 자체는 의존성이 없습니다(표준 라이브러리만). 다만 **Claimtrail 을 설치해도 검사 도구(pytest, ruff)는
설치되지 않습니다.** 검사는 "Claimtrail 을 실행한 그 Python" 으로 돌기 때문에, 같은 가상환경에 검사 도구를
직접 설치해야 합니다.

## 2. 샘플 소스 받기

패키지 설치만으로는 `examples/` 폴더가 생기지 않습니다. 소스를 **확인한 태그로 고정**해 받습니다(`main` 끝은
움직이므로 결과가 이 문서와 달라질 수 있습니다).

```bash
git clone --branch public-2026-09-14 --depth 1 https://github.com/ddayhyun/claimtrail-public.git claimtrail-src
git -C claimtrail-src rev-parse HEAD                     # 실제 받은 커밋을 기록해 둔다
```

`claimtrail-src/examples/sample-project` 가 검사 대상, `claimtrail-src/examples/claimtrail.json` 이 범위 설정입니다.
같은 커밋의 `Sample` 워크플로(Ubuntu·Windows / Python 3.12)에서 5절의 결과를 확인했습니다.

## 3. 새 가상환경 만들고 설치하기

### Bash (Linux, macOS, Git Bash)

```bash
python3 -m venv ct-venv
PY="$PWD/ct-venv/bin/python"                 # Git Bash(Windows): "$PWD/ct-venv/Scripts/python.exe"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install "git+https://github.com/ddayhyun/claimtrail-public.git@public-2026-09-14"
"$PY" -m pip install pytest ruff             # 샘플이 쓰는 검사 도구
"$PY" -m claimtrail --version                # claimtrail 0.5.1
```

### PowerShell (Windows)

```powershell
python -m venv ct-venv
$py = "$PWD\ct-venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install "git+https://github.com/ddayhyun/claimtrail-public.git@public-2026-09-14"
& $py -m pip install pytest ruff
& $py -m claimtrail --version                # claimtrail 0.5.1
```

이미 받은 소스에서 설치해도 됩니다: `"$PY" -m pip install ./claimtrail-src` (같은 커밋이면 결과는 같습니다).

## 4. 샘플 검사 실행

세 경로를 구분하십시오. **검사 대상**(샘플 폴더), **설정 파일**(대상 밖), **리포트 저장 위치**(대상 밖).
설정과 리포트를 대상 안에 두면 다음 검사의 수집 범위에 섞입니다.

판정은 **프로세스의 종료 코드**입니다. 실행 직후에 변수에 담으십시오. 뒤에 오는 `cat` 이나 `Get-Content` 는
자기 종료 코드로 덮어씁니다.

### Bash

```bash
"$PY" -m claimtrail run claimtrail-src/examples/sample-project \
  --config claimtrail-src/examples/claimtrail.json --format md -o ct-out/report.md
rc=$?                                        # 바로 다음 줄에서 읽는다
cat ct-out/report.md
echo "claimtrail exit: $rc"                  # 샘플은 1 이 정상이다 (아래 참고)
```

### PowerShell

```powershell
& $py -m claimtrail run claimtrail-src\examples\sample-project --config claimtrail-src\examples\claimtrail.json --format md -o ct-out\report.md
$rc = $LASTEXITCODE                          # 바로 다음 줄에서 읽는다 ($? 는 종료 코드가 아니다)
Get-Content ct-out\report.md -Encoding utf8  # 리포트는 UTF-8 이다. Windows PowerShell 5.1 은 기본 인코딩이 달라 한글이 깨질 수 있다
"claimtrail exit: $rc"                       # 샘플은 1 이 정상이다 (아래 참고)
```

JSON 이 필요하면 `--format json -o ct-out/report.json` 으로 한 번 더 실행합니다. Markdown 과 JSON 은
**별도 실행**입니다 — 검사를 다시 돌리므로 실행 시각·소요 시간·임시 경로가 다르고, 환경이나 테스트의
비결정성에 따라 판정·개수도 달라질 수 있습니다. 같은 실행의 두 형식이 아닙니다.

CI 스크립트처럼 `set -e` 가 켜진 셸(GitHub Actions 의 `shell: bash` 기본값 포함)에서는 종료 코드 1 에서
스크립트가 그 자리에서 죽어 `rc=$?` 에 닿지 못합니다. 그 명령만 `set +e` … `set -e` 로 감싸거나
`if "$PY" -m claimtrail run …; then rc=0; else rc=$?; fi` 로 받으십시오. 이 저장소의 `sample.yml` 이 그 예입니다.

## 5. 결과 읽기

샘플은 **일부러 실패하도록** 만들어져 있습니다. 기대 결과:

| 항목 | 기대 |
|---|---|
| 종료 코드 | `1` (실패) |
| `## 판정` | 실패 |
| 필수 검사 | pytest 통과 · lint 통과 · **format 실패** |
| pytest | 5개 중 5 통과 |
| 실패한 항목 | `sample_calc/calc.py` — ruff format 결과와 다르다 |
| 확인하지 못한 것 | type-check · build · npm test (설정이 없어 탐지되지 않음) |
| 실행 환경 | 사용한 Python 경로·버전, pytest·ruff 버전, Claimtrail 소스 위치 |

종료 코드의 뜻은 셋뿐입니다. **0 = 보고된 검사 범위의 통과, 1 = 실패, 2 = 검증 불가**(실행하지 못함).
`2` 를 통과로 읽지 마십시오. 통과는 리포트에 적힌 범위(여기서는 설정의 pytest·lint·format)에 대한
것이고, "확인하지 못한 것" 은 그 범위 밖입니다.

고쳐서 통과시켜 보기: 샘플 폴더에서 `"$PY" -m ruff format .` 을 실행한 뒤 4절을 다시 실행하면 종료 코드
`0`, 판정 통과가 됩니다. 확인하지 못한 항목은 그대로 남습니다.

실제 실행 결과(CI 의 Ubuntu·Windows / Python 3.12)는 이 저장소 `Sample` 워크플로의 잡 요약·아티팩트에
있습니다. 아티팩트 내려받기는 GitHub 로그인이 필요하므로 정적 사본을 `site/sample/` 에 둡니다.

## 6. 자기 프로젝트에 적용하기

1. **그 프로젝트의 의존성이 준비된 Python** 을 고릅니다(대개 프로젝트 `.venv`). 거기에 Claimtrail 을 설치합니다.
   의존성이 빠진 환경에서 돌리면 리포트는 "그 환경에서 실행하지 못했다" 고 남깁니다. 시스템 Python 으로
   다시 돌린 것은 별도 실행이며, 원래 환경이 복구된 것이 아닙니다. 리포트의 "실행 환경" 항목으로 두
   실행을 구분할 수 있습니다.
2. `claimtrail detect <프로젝트>` 로 무엇이 탐지되는지 먼저 봅니다(실행하지 않음).
3. 필요하면 설정 파일을 **프로젝트 밖**에 만들어 `--config` 로 넘깁니다. 형식 검사(`format`)는 설정으로
   켰을 때만 실행됩니다. 필수 검사(`required`)가 실행되지 않으면 전체는 통과가 아니라 검증 불가입니다.
4. `claimtrail run <프로젝트> --config … --format md -o <프로젝트 밖>/report.md` 를 실행하고 종료 코드를
   바로 읽습니다.

## 7. 지원 범위와 한계

- 검사 종류: pytest, lint(ruff 또는 flake8), format(ruff, 설정 필요), type-check(mypy), build, npm test.
  자세한 근거 규칙은 README 를 봅니다.
- npm test 는 개수를 세지 않고 종료 코드로만 판정합니다.
- 리포트의 자격증명 가림은 **알려진 형태**(키=값, Bearer, URL 사용자 정보, 접두가 뚜렷한 토큰)만
  다룹니다. 모든 민감정보 제거를 보장하지 않으며 원본 로그는 가리지 않습니다.
- Claude Code Stop 훅 연동은 별도 절차이며, 이 설치만으로 훅이 설정되지 않습니다(README 의 훅 절 참고).
- 확인한 환경은 위 표와 같습니다. 그 외 조합은 확인하지 않았습니다.

## 8. 피드백

막힌 곳, 이해되지 않은 결과, 다시 쓸 이유가 있었는지를 알려 주십시오. 세 가지만 물어봅니다:
어떤 작업에서 썼는지, 어디서 막혔는지, 다음 작업에서 다시 썼는지(아니라면 이유).
GitHub Issues 에 남겨 주십시오: https://github.com/ddayhyun/claimtrail-public/issues (GitHub 계정만 있으면 됩니다).
