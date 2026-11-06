#!/usr/bin/env bash
# 예약 등록. .env를 채운 뒤 한 번 실행한다. 다시 실행해도 같은 상태가 된다.
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

mkdir -p "$HOME/.config/systemd/user"
# Discord 봇: Gateway(웹소켓)로 메시지를 바로 받는다. 예전 1분 폴링 타이머는 끈다
VENV="$HOME/.local/share/trend-automation/venv"
[ -x "$VENV/bin/python" ] || uv venv -q --python 3.12 "$VENV"
uv pip install -q --python "$VENV/bin/python" "discord.py>=2.4,<3"
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
