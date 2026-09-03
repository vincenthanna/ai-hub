# ai-hub 배포 환경 실측 사실

이 문서는 ai-hub 서버와 스킬을 설계하기 전에 직접 실행해서 확인한 환경 사실만 담는다. 추정이나 계획은
담지 않는다. 결론부터 말하면 ds30에서 서버를 띄울 포트는 16001이고(16000은 타 사용자 Caddy가 점유),
방화벽은 이미 열어두었으며, ds30에는 claude CLI가 설치되어 있어 자동분류를 서버 쪽에서 실행할 수 있다.
분류 비용을 실측한 결과 건별 동기 분류는 비용과 지연 양쪽에서 성립하지 않으므로 백그라운드 배치가
필요하다. 아래 수치는 2026-09-03에 측정한 값이다.

## 배포 대상 ds30

접속은 `ssh yeonhui@192.168.49.48` 로 하며 키 기반 인증이 이미 설정되어 있다.

| 항목 | 값 |
|---|---|
| OS | Ubuntu 22.04.5 LTS |
| Kernel | 6.5.0-45-generic |
| Python | 3.10.12 (`/usr/bin/python3`) |
| uv | `/snap/bin/uv` |
| node / npm | v20.19.4 / 있음 |
| git / curl | `/usr/bin/git`, `/usr/bin/curl` |
| claude CLI | `/usr/bin/claude`, 버전 2.1.29 |
| 디스크 | `/home/yeonhui` 가 속한 볼륨 1.8T 중 207G 여유 (89% 사용) |
| LAN IP | 192.168.49.48 |

Python 3.10 이 최고 버전이므로 서버 코드는 3.10에서 동작해야 한다. 3.11에서 추가된
`ExceptionGroup`, `asyncio.TaskGroup`, `tomllib` 은 쓰지 않는다.

## 포트와 방화벽

사용자가 처음 지정한 16000 포트는 다른 사용자의 Caddy 프로세스가 이미 점유하고 있다. 확인 방법과 응답은
다음과 같다.

```
$ curl -s -i http://127.0.0.1:16000/
HTTP/1.1 302 Found
Location: http://18.179.15.224:8123
Server: Caddy
```

`lsof -nP -iTCP:16000 -sTCP:LISTEN` 이 우리 계정에서 아무 것도 반환하지 않으므로 이 프로세스는 다른
사용자 소유이고 우리가 종료할 수 없다. 사용자와 협의해서 **16001** 을 쓰기로 결정했다.

ufw가 활성 상태이고 기본 정책이 차단이라서 16001은 처음에 LAN에서 접근할 수 없었다. NOPASSWD로 허용된
`ufw` 를 써서 규칙을 추가했고, macOS에서 실제 도달을 확인했다.

```
$ sudo ufw allow 16001/tcp
Rule added
Rule added (v6)

$ curl -s -m 8 http://192.168.49.48:16001/   # ds30에 임시 프로브 리스너를 띄운 상태
PROBE_OK
```

되돌리려면 ds30에서 `sudo ufw delete allow 16001/tcp` 를 실행한다.

## 권한과 프로세스 상주 방식

`sudo -l` 결과는 다음과 같다. 일반 sudo는 패스워드를 요구하므로 비대화식 배포 스크립트에서 쓸 수 없다.

```
User yeonhui may run the following commands on DS30:
    (ALL : ALL) ALL
    (root) NOPASSWD: /usr/bin/dpkg -i /home/yeonhui/Downloads/orca-ide_*.deb, /usr/sbin/ufw
```

`loginctl show-user yeonhui` 가 `Linger=yes` 를 보고하므로 `systemctl --user` 로 등록한 서비스는
사용자가 로그아웃해도 계속 돈다. 따라서 root systemd 유닛 없이 `systemctl --user` 만으로 상주 서비스를
구성할 수 있다.

## claude CLI 헤드리스 분류 비용 실측

ds30에서 헤드리스 모드가 동작하는 것을 확인했다. 두 가지 구성을 측정했다.

| 구성 | 1건 비용 | API 시간 | wall 시간 | cache_creation 토큰 |
|---|---|---|---|---|
| 기본 모델 (claude-opus-4-5) | $0.1379 | 2.4s | 3.1s | 21,986 |
| `--model claude-haiku-4-5-20251001` + `--disallowed-tools` | $0.0126 | 1.6s | 8.5s | 9,792 |

두 번째 구성에서 사용한 커맨드는 다음과 같다.

```
echo "<본문>" | claude -p "<분류 지시>" \
  --model claude-haiku-4-5-20251001 \
  --output-format json \
  --disallowed-tools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit"
```

비용의 대부분은 본문이 아니라 Claude Code 시스템 프롬프트를 매번 캐시에 올리는 고정 오버헤드다. 툴을
전부 끄면 그 오버헤드가 21,986 토큰에서 9,792 토큰으로 줄지만 없어지지는 않는다. 아이템 1건마다
claude를 한 번씩 부르면 1,000건에 약 $12.6이 들고 업로드 응답이 최소 3초 이상 느려진다. 그래서 분류는
업로드 요청 경로에서 분리한 백그라운드 처리로 두고, 여러 건을 한 번의 claude 호출로 묶는 배치 방식을
쓴다.

응답의 `result` 필드는 다음처럼 코드펜스로 감싸져서 나온다. 파싱할 때 펜스를 벗겨야 한다.

```
"result": "```json\n{\n  \"topic\": \"bug_fix\",\n  ...\n}\n```"
```

## GitHub 리포

`vincenthanna/ai-hub` 는 public이고 비어 있다. `gh` CLI와 SSH 인증이 모두 정상이라 push와
`/plugin marketplace add` 양쪽 모두 추가 인증 없이 된다.

```
$ gh repo view vincenthanna/ai-hub --json name,visibility,isEmpty
{"isEmpty":true,"name":"ai-hub","visibility":"PUBLIC"}
```

## 검증된 플러그인 마켓플레이스 구조

사용자는 이미 `vincenthanna/vh1981_skills` 리포를 마켓플레이스로 쓰고 있다. 그 리포의 실제 구조를 읽어서
확인한 배치는 다음과 같고, ai-hub도 같은 구조를 따르면 추가 검증 없이 설치가 된다.

```
<repo-root>/
  .claude-plugin/
    marketplace.json          # 마켓플레이스 매니페스트. name 필드가 설치 시 마켓플레이스 이름이 된다
  plugins/
    <plugin-name>/
      .claude-plugin/
        plugin.json           # name, description, version, author
      skills/
        <skill-name>/
          SKILL.md            # name + description frontmatter
          scripts/            # 스킬이 호출하는 실행 파일
      hooks/
        hooks.json            # 선택. SessionStart 등
      scripts/
```

`marketplace.json` 의 실제 형태는 다음과 같다.

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "vh1981_skills",
  "description": "Development workflow skills for PLUSINSIGHT projects",
  "owner": { "name": "vh1981" },
  "plugins": [
    {
      "name": "vh1981",
      "description": "...",
      "version": "1.5.0",
      "author": { "name": "vh1981" },
      "source": "./plugins/vh1981",
      "category": "productivity"
    }
  ]
}
```

플러그인이 배포하는 스크립트는 `${CLAUDE_PLUGIN_ROOT}` 환경변수로 절대경로를 얻는다. 실제 사용 예는
`hooks.json` 에서 `/bin/sh "${CLAUDE_PLUGIN_ROOT}/scripts/check-statusline.sh"` 형태이고,
SKILL.md 본문에서는 `${CLAUDE_PLUGIN_ROOT}/skills/md-tidy/scripts/normalize_whitespace.py` 형태로 쓴다.

## 개발 머신 macOS

| 항목 | 값 |
|---|---|
| OS | darwin 25.5.0 |
| Python | 3.12.4 (pyenv shim) |
| uv | 0.8.19 |
| node | v24.18.1 |
| claude CLI | 2.1.259 |
| 리포 경로 | `/Users/yeonhuigim/workspace/ai_hub` |
