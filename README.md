# trend-automation

AI 트렌드와 Claude Code 스킬 소식을 매주 모아 보고서로 쓰고, Discord로 보내는 개인용 자동화입니다.
사람은 Discord에서 피드백만 하고, 수집 · 작성 · 검증 · 게시 · 전송은 정해진 시각에 저절로 돌아갑니다.

발행된 보고서 예시: <https://jina1003.github.io/ai-briefing/>

## 무엇을 하나

| 요일 · 시각 (KST) | 하는 일 |
|---|---|
| 화 11:00 생성 · 14:00 전송 | **AI 트렌드 노트**: 이번 주 AI 소식 가운데 실제로 쓸 만한 것 |
| 목 11:00 생성 · 14:00 전송 | **스킬 도감**: 급상승한 Claude Code 스킬 · 플러그인과 그 활용법 |
| 금 06:00 생성 · 09:00 전송 | **종합**: 그 주 화 · 목 호와 사용자가 보낸 소식을 골라 엮은 팀 공유용 호 |
| 토 09:00 | **주간 인사이트**: 이번 주 소식 사이의 연결, 반대 의견, 던질 질문 |
| 상시 | **Discord 대화 봇**: 질문에 답하고, 방향 피드백을 메모에 반영하고, 제보를 모은다 |

한 호는 이렇게 만들어집니다.

```
수집 (RSS · GitHub · Hacker News · 스킬 순위)
  → 급상승 판정 (규칙 기반, 결정론적)
  → claude -p 가 고르고 씀 (prompt_*.md + direction.md의 피드백)
  → 검증 (링크 · 설치 명령 · 형식)
  → HTML 렌더 → GitHub Pages 저장소에 게시
  → Discord에 요약 + 웹 링크 전송
```

피드백은 코드가 아니라 `direction.md`에 쌓입니다. Discord에서 「제목에 이모지 쓰지 마」라고 말하면 봇이 이 파일을 고치고, 다음 호부터 반영됩니다.

## 구성

| 파일 | 역할 |
|---|---|
| `trend.py` | 본체. 수집 · 생성 · 전송 · 대화 처리 · 자기검사 (`python3 trend.py` 머리말에 명령 목록) |
| `bot.py` | Discord 상시 봇 (Gateway). 받은 메시지를 `trend.handle()`에 순서대로 넘긴다 |
| `insight.py` | 주간 인사이트 |
| `prompt_report.md` · `prompt_weekly.md` · `prompt_insight.md` · `prompt_chat.md` | 각 호와 대화의 작성 지시 |
| `sources.txt` | 읽을 RSS 목록 (Discord에서 `add-source`로 추가 가능) |
| `report.css` | 웹 보고서 스타일 |
| `run.sh` | 스케줄러가 부르는 입구. `.env`를 읽고 `trend.py`를 실행 |
| `install.sh` · `tasks.ps1` | 설치. systemd 봇 서비스와 Windows 작업 스케줄러 등록 |
| `templates/` | 대화 봇이 고치는 메모 · 요청 대기열의 빈 견본 |

실행 중에 생기는 파일(`state/`, `direction.md`, `insight_direction.md`, `requests.md`, `.env`)은 git에 넣지 않습니다.

## 실행 환경

- Windows + WSL2 (Ubuntu). 일정은 Windows 작업 스케줄러가 WSL을 깨워 돌립니다. PC가 꺼져 있다가 켜지면 놓친 실행을 이어서 합니다.
- Python 3.11 이상, [uv](https://github.com/astral-sh/uv)
- [Claude Code](https://claude.com/claude-code) CLI (`claude -p`)
- [GitHub CLI](https://cli.github.com/) (`gh`, 로그인 상태). GitHub API 조회와 게시에 씁니다.
- Discord 봇 하나 (Message Content Intent 켜기)

## 설치

```bash
git clone https://github.com/JINA1003/trend-automation.git
cd trend-automation

mkdir -p ~/.config/trend-automation
cp .env.example ~/.config/trend-automation/.env
chmod 600 ~/.config/trend-automation/.env
# DISCORD_BOT_TOKEN을 채운 뒤, 채널 · 사용자 ID를 자동으로 찾는다
./run.sh discord-setup

./install.sh
python3 trend.py selftest
```

`.env`를 저장소 밖(`~/.config`)에 두는 것은 Windows 드라이브(`/mnt/c`)에서는 파일 권한(600)이 먹지 않기 때문입니다.

## 고쳐 써야 할 곳

이 저장소는 한 사람의 작업 환경에 맞춰져 있습니다. 다른 곳에서 쓰려면 아래를 바꿉니다.

- **보고서 저장 위치**: `trend.py`의 `VAULT` · `REPORTS` · `RAW`. 지금은 이 폴더 옆의 Obsidian 볼트(`../../Obsidian/Second_Brain`)에 보고서를 쓰고, 원문 사본을 위키 편입 대기로 넘깁니다.
- **게시 저장소**: `install.sh`의 GitHub Pages 저장소 주소(`JINA1003/ai-briefing`).
- **일정**: `tasks.ps1`의 요일 · 시각.

## 데이터 출처

- 스킬 순위: [Best Skills](https://github.com/LinklyAI/best-skills) by @BlueeonY, CC BY 4.0
- 플러그인 목록: [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official)
- Claude Code 변경 기록: [anthropics/claude-code CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
- GitHub Trending, Hacker News (Algolia API), `sources.txt`의 RSS
