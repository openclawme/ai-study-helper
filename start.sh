#!/bin/bash
# 启动/重启「我的AI学习助手」服务。
# 之所以单独做成脚本、而不是写进 ~/.bashrc：
#   写进 .bashrc 的话，每开一个终端/每 SSH 登录一次都会先杀掉服务再重启，
#   正在上传的请求会被打断，多个终端之间还会互相杀。
# 用这个脚本：想重启时才重启。
set -u
cd "$(dirname "$(readlink -f "$0")")"

# 从受保护的文件里读 Key。注意：`set -u` 下要用 ${VAR:-} 取默认值，
# 否则变量未定义时脚本会直接退出。
if [ -f "$HOME/.cuoti.env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.cuoti.env"
fi

# 关键一步：`.` 引入的变量默认只是**当前 shell 的变量**，不导出。
# 如果 ~/.cuoti.env 里写的是 `DEEPSEEK_API_KEY=xxx`（没有 export），
# 后面的 python3 子进程根本拿不到它——表现为「明明配了 Key，服务却说没配」。
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"

if [ -z "$DEEPSEEK_API_KEY" ]; then
  echo "⚠️  没有找到 DEEPSEEK_API_KEY。AI 功能会不可用（上传/浏览/导出仍正常）。"
  echo "    配置方式： echo 'DEEPSEEK_API_KEY=sk-你的key' > ~/.cuoti.env && chmod 600 ~/.cuoti.env"
fi

OLD=$(fuser 8000/tcp 2>/dev/null | tr -d ' ')
if [ -n "$OLD" ]; then
  echo "停止旧进程 $OLD …"
  fuser -k 8000/tcp 2>/dev/null
  sleep 1.5
fi

nohup python3 main.py > "$HOME/cuoti.log" 2>&1 &
sleep 5

PID=$(fuser 8000/tcp 2>/dev/null | tr -d ' ')
if [ -n "$PID" ]; then
  echo "✅ 已启动 (PID $PID)"
  sed -n '3,18p' "$HOME/cuoti.log"
else
  echo "❌ 启动失败，日志末尾："
  tail -20 "$HOME/cuoti.log"
  exit 1
fi
