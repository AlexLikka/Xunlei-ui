#!/bin/zsh
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按 README.md 安装 Python 依赖。'
  exit 1
fi
echo '开启自动清理：仅处理连续零速达到门槛的正在下载任务，保留文件。'
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i .venv/bin/python monitor.py --auto --open-browser
else
  exec .venv/bin/python monitor.py --auto --open-browser
fi
