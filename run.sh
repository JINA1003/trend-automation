#!/usr/bin/env bash
# Windows 작업 스케줄러와 systemd 타이머가 부르는 입구. 인자는 trend.py에 그대로 넘긴다.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
set -a; . "$HOME/.config/trend-automation/.env"; set +a
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:$PATH"
exec python3 trend.py "$@"
