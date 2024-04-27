#!/bin/bash

export HTTP_PROXY=http://127.0.0.1:7890
export HTTPS_PROXY=http://127.0.0.1:7890
screen -S ah2 bash -c "cd /root/ah2 && poetry run python linux_schedule.py"
