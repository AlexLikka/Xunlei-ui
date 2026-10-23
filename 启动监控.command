#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按 README.md 安装 Python 依赖。'
  exit 1
fi
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i .venv/bin/python monitor.py --open-browser
else
  exec .venv/bin/python monitor.py --open-browser
fi
