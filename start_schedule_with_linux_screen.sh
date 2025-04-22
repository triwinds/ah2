#!/bin/bash
git pull
kill $(ps aux | grep '[p]ython linux_schedule.py' | awk '{print $2}')
export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY=http://127.0.0.1:7890
screen -S ah2 bash -c "cd /root/ah2 && uv run python linux_schedule.py"
