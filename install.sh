#!/usr/bin/env bash
# 예약 등록. .env를 채운 뒤 한 번 실행한다. 다시 실행해도 같은 상태가 된다. WSL과 macOS 공용.
#  - macOS: 상시 봇 · 정기 작업 모두 launchd LaunchAgent (아래 Darwin 분기)
#  - 화 · 목 생성/전송: Windows 작업 스케줄러 (WSL이 내려가 있어도 깨워서 돌림, 놓친 실행은 켜지는 대로)
#  - Discord 대화 · 명령: systemd 상시 서비스 trend-bot (Gateway, 메시지가 오면 바로)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="$HOME/.config/trend-automation/.env"
grep -q "^DISCORD_OWNER_ID=[0-9]" "$ENV" || { echo "$ENV 의 Discord 값이 비었다. 먼저 ./run.sh discord-setup"; exit 1; }
ln -sfn "$HERE" "$HOME/trend-automation"   # Windows 쪽 명령줄에 한글 경로를 넣지 않으려고
# 대화 봇이 고치는 메모 · 요청 대기열은 git에 넣지 않는다. 없으면 견본에서 만든다
for f in direction.md insight_direction.md requests.md; do [ -e "$HERE/$f" ] || cp "$HERE/templates/$f" "$HERE/$f"; done

# 웹 게시 저장소(GitHub Pages). 새 PC면 받아 온다
PAGES="$HOME/.local/share/trend-automation/pages"
if [ ! -d "$PAGES/.git" ]; then
  mkdir -p "$(dirname "$PAGES")"
  git clone -q git@github.com:JINA1003/ai-briefing.git "$PAGES"
fi
[ -s "$PAGES/site.json" ] || echo '{"base_url": "https://jina1003.github.io/ai-briefing"}' > "$PAGES/site.json"

VENV="$HOME/.local/share/trend-automation/venv"
[ -x "$VENV/bin/python" ] || uv venv -q --python 3.12 "$VENV"
uv pip install -q --python "$VENV/bin/python" "discord.py>=2.4,<3"

if [ "$(uname)" = Darwin ]; then
  # macOS: 상시 봇과 정기 작업을 launchd LaunchAgent로. 시각은 맥의 로컬 시각(KST)이다.
  # 잠자는 동안 지난 정기 작업은 깨어나면 한 번 돈다. 봇은 잠자는 동안 답하지 못한다.
  LA="$HOME/Library/LaunchAgents"; LOGD="$HOME/Library/Logs/trend-automation"; mkdir -p "$LA" "$LOGD"
  PATHV="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  agent() {  # agent <이름> <예약 dict 또는 빈 값> <실행할 셸 명령>
    local label="trend-automation.$1" when="$2" cmd="$3"
    cat > "$LA/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>-c</string><string>$cmd</string></array>
  <key>WorkingDirectory</key><string>$HOME/trend-automation</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATHV</string></dict>
  <key>StandardOutPath</key><string>$LOGD/$1.log</string>
  <key>StandardErrorPath</key><string>$LOGD/$1.log</string>
  $when
</dict></plist>
PLIST
    plutil -lint -s "$LA/$label.plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$LA/$label.plist"
    echo "registered $label"
  }
  at() { local out="" d; for d in $1; do out="$out<dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$2</integer><key>Minute</key><integer>$3</integer></dict>"; done
         echo "<key>StartCalendarInterval</key><array>$out</array>"; }
  # 봇: 죽으면 15초 뒤 다시 띄운다(systemd Restart=always · RestartSec=15와 같다)
  agent bot "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer>" \
    "set -a; . \"\$HOME/.config/trend-automation/.env\"; set +a; exec \"$VENV/bin/python\" bot.py"
  # Weekday: 1=월 … 5=금, 6=토 (tasks.ps1과 같은 일정)
  # run.sh를 거치지 않는다: launchd가 띄운 bash는 개인정보 보호(TCC) 때문에 ~/Documents 안의 파일을 못 읽는다.
  # 문서 폴더 접근을 허용받은 uv python(venv)이 직접 읽고, 그 자식(claude -p)도 같은 허용을 물려받는다.
  run="set -a; . \"\$HOME/.config/trend-automation/.env\"; set +a; exec \"$VENV/bin/python\" trend.py"
  agent generate-daily     "$(at '1 2 3 4' 8 0)"  "$run generate daily"
  agent deliver            "$(at '1 2 3 4' 10 0)" "$run deliver daily"
  agent generate-daily-fri "$(at 5 7 0)"          "$run generate daily"
  agent generate-weekly    "$(at 5 8 30)"         "$run generate weekly"
  agent deliver-weekly     "$(at 5 10 0)"         "$run deliver weekly"
  agent insight            "$(at 6 9 0)"          "$run insight"
  echo "등록 완료. 확인: launchctl list | grep trend-automation ; tail $LOGD/bot.log"
  exit 0
fi

mkdir -p "$HOME/.config/systemd/user"
# Discord 봇: Gateway(웹소켓)로 메시지를 바로 받는다. 예전 1분 폴링 타이머는 끈다
cat > "$HOME/.config/systemd/user/trend-bot.service" <<UNIT
[Unit]
Description=trend-automation Discord bot (Gateway)
After=network-online.target
[Service]
EnvironmentFile=%h/.config/trend-automation/.env
Environment=PATH=$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin
WorkingDirectory=$HOME/trend-automation
ExecStart=$VENV/bin/python $HOME/trend-automation/bot.py
Restart=always
RestartSec=15
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user disable --now trend-poll.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/trend-poll.service" "$HOME/.config/systemd/user/trend-poll.timer"
systemctl --user daemon-reload
systemctl --user enable --now trend-bot.service
systemctl --user restart trend-bot.service
loginctl show-user "$USER" -p Linger | grep -q yes || echo "할 일: sudo loginctl enable-linger $USER (로그인 셸 없이도 1분 폴링이 돈다)"

ps1="$(wslpath -w "$HERE/tasks.ps1")"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps1" -Distro "$WSL_DISTRO_NAME" -User "$USER"
powershell.exe -NoProfile -Command "powercfg /change standby-timeout-ac 0"   # 전원 연결 시 절전 끔 — 절전 중엔 WSL이 멈춰 Discord 답이 늦는다
echo "등록 완료. 확인: systemctl --user status trend-bot ; schtasks.exe /query /tn '\\trend-automation\\'"
