#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo '未找到监控程序的 Python 环境。'
  exit 1
fi
.venv/bin/python monitor.py --stop
