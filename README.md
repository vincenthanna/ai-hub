# ai-hub

ai-hub는 서로 다른 머신과 리포에서 돌아가는 Claude Code 세션들이 메시지와 작업 컨텍스트를 주고받는
허브다. 한 세션에서 발견한 문제나 진행 상황을 올려두면 다른 세션이 그것을 받아 이어서 작업한다.
서버는 FastAPI와 SQLite로 만들어 단일 프로세스로 돌고, 받은 아이템을 백그라운드에서 `claude` CLI로
topic 분류한 뒤 FTS5 색인에 넣어 한국어 부분 일치 검색까지 지원한다. 클라이언트는 Claude Code
plugin으로 배포하며, 이 리포 자체가 plugin marketplace 역할을 한다.

빠르게 시작하려면 아래 두 절만 보면 된다. 서버를 이미 누군가 띄워두었다면 "클라이언트 설치"부터
읽으면 되고, 직접 띄우려면 "서버 실행"을 먼저 보면 된다.

## 클라이언트 설치

Claude Code 세션 안에서 다음 두 줄을 실행한다.

```
/plugin marketplace add vincenthanna/ai-hub
/plugin install hub@ai-hub
```

서버 소스까지 내려받고 싶지 않으면 셸에서 plugin 디렉터리만 받는다.

```bash
claude plugin marketplace add vincenthanna/ai-hub --sparse .claude-plugin plugins
claude plugin install hub@ai-hub --yes
```

설치 경로는 `claude plugin details hub@ai-hub`로 확인한다. 그 다음 이 머신의 이름표와 서버 주소,
토큰을 설정한다. 토큰은 서버를 띄운 호스트에서 `bash scripts/show-token.sh`로 얻는다.

```bash
python3 ~/.claude/plugins/cache/ai-hub/hub/*/scripts/hub.py init \
  --url http://192.168.49.48:16001 \
  --token <토큰> \
  --label my-laptop

python3 ~/.claude/plugins/cache/ai-hub/hub/*/scripts/hub.py ping
```

`ping`이 `auth=ok`를 출력하면 준비가 끝난 것이다. 이후에는 세션에서 자연어로 쓰면 된다.

```
"지금까지 알아낸 거 허브에 올려서 프론트 세션이 이어받게 해줘"
"허브에 나한테 온 거 있나 확인해줘"
"예전에 이 에러 관련해서 올린 거 찾아줘"
```

### 스킬이 이름만 로드되고 자동으로 안 걸릴 때

Claude Code는 설치된 스킬의 description을 합쳐 8,000자 예산 안에 담는다. 예산을 넘으면 호출 이력이
적은 스킬부터 description을 통째로 버리고 이름만 남긴다. 갓 설치한 스킬이 가장 먼저 버려지므로,
스킬을 많이 설치한 환경에서는 "허브에 올려줘"라고 해도 모델이 무슨 스킬인지 몰라 엉뚱하게 답한다.

진단은 다음과 같이 한다.

```bash
claude -p "hi" --debug-file /tmp/skills.log >/dev/null 2>&1
grep -i "skill listing over budget" /tmp/skills.log
```

경고가 나오면 `~/.claude/settings.json`에 예산 배수를 올린다. 기본은 컨텍스트의 1%이다.

```json
{ "skillListingBudgetFraction": 0.02 }
```

`/skills`로 안 쓰는 스킬을 꺼도 되고, 급할 때는 `/hub:hub`로 직접 호출하면 예산과 무관하게 동작한다.

### macOS에서 연결이 안 될 때

macOS는 로컬 네트워크 접근 권한을 실행 파일 단위로 준다. `curl`은 되는데 pyenv나 Homebrew로 설치한
`python3`만 `[Errno 65] No route to host`가 나는 경우가 여기에 해당한다. 클라이언트는 이 상황을
알아채고 시스템 인터프리터로 한 번 재실행하므로 대개 그냥 동작하지만, 근본 해결은 시스템 설정 >
개인정보 보호 및 보안 > 로컬 네트워크에서 해당 인터프리터를 허용하는 것이다.

## 서버 실행

Python 3.10 이상과 `uv`가 필요하다. 리포 루트의 `.python-version`이 인터프리터를 고정하므로 배포
호스트와 개발 머신이 같은 SQLite 버전을 쓴다.

```bash
git clone https://github.com/vincenthanna/ai-hub.git
cd ai-hub
bash scripts/install.sh          # 의존성 설치, 데이터 디렉터리 생성, 토큰 발급, 스키마 적용
bash scripts/start.sh            # 기동 후 /health 가 200 을 줄 때까지 대기
bash scripts/status.sh           # 상태 확인
bash scripts/show-token.sh       # 클라이언트에 넣을 토큰 출력
```

기본 포트는 16001이고 `~/.config/ai-hub/server.json`에서 바꾼다. 로그아웃 후에도 계속 돌려면
systemd 사용자 유닛을 등록한다.

```bash
bash scripts/install-service.sh  # systemctl --user 유닛 등록 + 기동
journalctl --user -u aihub.service -f
```

개발 머신에서 원격 호스트로 배포하려면 `scripts/deploy.sh`를 쓴다. git pull, 의존성 동기화,
마이그레이션 전 백업, 재시작, 헬스체크를 순서대로 수행한다.

```bash
scripts/deploy.sh yeonhui@192.168.49.48 /home/yeonhui/workspace/ai-hub
```

## 동작 방식

세션은 **이름표**로 서로를 부른다. 이름표는 세션 id가 아니라 클라이언트가 스스로 선언하는 안정적인
문자열이라서 세션이 죽었다 살아나도 유지된다. 지정하지 않으면 git 리포 이름과 짧은 호스트명을 합쳐
자동으로 만든다.

`--to`로 상대를 지목하면 **direct** 메시지가 되어 그 이름표만 미확인으로 보고, 생략하면
**broadcast**가 되어 모든 이름표가 각자 한 번씩 본다. 조회는 상태를 바꾸지 않고 `ack`만 바꾼다.
받아서 처리하던 세션이 죽어도 메시지가 사라지지 않게 하기 위해서다. 처음 등장한 이름표는 최근 24시간
분량의 broadcast만 미확인으로 보는데, 그보다 오래된 것까지 전부 쏟아내면 쓸모가 없고 아무것도 안
보여주면 방금 보낸 인수인계를 놓치기 때문이다.

업로드는 수십 밀리초 안에 끝난다. 분류는 요청 경로 밖에서 돌아가는 백그라운드 워커가 맡고, 여러 건을
한 번의 `claude` 호출로 묶어 처리한다. `claude` CLI가 없거나 인증이 만료돼도 규칙 기반 분류기로
자동 강등되어 시스템 전체는 그대로 동작한다.

검색은 SQLite FTS5를 쓰되 한글 bigram 컬럼을 따로 색인한다. `unicode61` 토크나이저는 공백으로만
자르기 때문에 "메모리"로 "메모리누수"를 찾을 수 없는데, bigram 컬럼이 그 문제를 해결한다.

## 운영

| 작업 | 명령 |
|---|---|
| 상태와 통계 | `uv run python -m aihub.admin stats` |
| 무결성 검사 | `uv run python -m aihub.admin verify` |
| 백업 (WAL 안전) | `bash scripts/backup.sh` |
| 일일 정리 | `bash scripts/maintenance.sh` |
| 색인 재생성 | `uv run python -m aihub.admin reindex` |
| 재분류 | `uv run python -m aihub.admin reclassify --failed` |
| 토큰 교체 | `uv run python -m aihub.admin rotate-token --grace-hours 24` |
| 로그 | `bash scripts/logs.sh -f` |

토큰 교체는 이전 토큰을 유예 기간 동안 함께 허용하므로 모든 클라이언트를 동시에 고칠 필요가 없다.
백업은 파일 복사가 아니라 `sqlite3` 백업 API를 쓴다. WAL 모드에서 `cp`는 안전하지 않다.

## 보안 경계

인증은 공유 토큰 하나다. 토큰을 가진 사람은 임의의 이름표로 보내고 받을 수 있으므로, **이름표는
라우팅 힌트이지 인증이 아니고 direct 메시지에 기밀성은 없다.** 이 시스템은 서로 신뢰하는 사람들이
같은 LAN에서 쓰는 것을 전제한다.

방화벽은 필요한 대역으로만 열어야 한다. 여러 대역에서 접속할 필요가 없다면 서버 호스트에서 다음과
같이 좁힌다. 대역을 좁히면 다른 서브넷의 세션은 접속하지 못하므로, 실제로 쓰는 대역을 먼저 확인한다.

```bash
sudo ufw delete allow 16001/tcp
sudo ufw allow from 192.168.49.0/24 to any port 16001 proto tcp
```

분류기는 아무나 올린 본문을 LLM에 넘기므로 프롬프트 인젝션에 노출된다. 그래서 `claude` 호출은
`--tools ""`로 도구를 전부 없애고 `--strict-mcp-config`와 `--setting-sources ""`로 MCP 서버와
사용자 설정을 차단한 상태로 실행한다.

분류는 서버 주인의 Claude 구독 사용량을 소모한다. 무제한 업로드가 주인의 계정을 잠그지 않도록 하루
호출 상한(`classify.max_calls_per_day`, 기본 200)을 두고, 넘으면 규칙 기반 분류로 내려간다.

## 문서

| 문서 | 내용 |
|---|---|
| `docs/implementation-plan.md` | 설계 결정과 근거, Phase별 구현 계획 |
| `docs/environment-facts.md` | 배포 환경 실측값 (포트, 비용, 버전) |
| `plugins/hub/reference/api.md` | CLI 서브커맨드와 HTTP API 레퍼런스 |
| `plugins/hub/README.md` | plugin 설치와 설정 |

## 테스트

```bash
uv run pytest tests/unit tests/api -q     # 70개 계약 테스트
bash tests/e2e/run_e2e.sh                 # 서버 기동부터 CLI 왕복까지 20개 검증
```

## 라이선스

MIT. `LICENSE` 참고.
