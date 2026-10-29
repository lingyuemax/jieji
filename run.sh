#!/usr/bin/env bash
# 一键启动（macOS / Linux）
#   ./run.sh            正常启动
#   ./run.sh --install  先装依赖
#   ./run.sh --check    只做自检
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8

if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3，先装一个 3.10+"
  exit 1
fi

if [[ "${1:-}" == "--install" ]]; then
  python3 -m pip install -r requirements.txt
fi

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  echo "已生成 .env，记得填 LLM_API_KEY"
fi

python3 scripts/selfcheck.py
[[ "${1:-}" == "--check" ]] && exit 0

echo
echo "启动中... 打开 http://127.0.0.1:8790"
exec python3 serve.py
