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
  # ⚠️ set -a（allexport）让文件里**每一个**赋值都自动导出。
  #
  # 原来只手动 export 了 DEEPSEEK_API_KEY，后面加 SMTP_* 时就漏了 ——
  # 表现为「明明配了 SMTP，服务却报 mail_ready:false」，而且不会报任何错。
  # 用 set -a 就不必每次加新配置都记得补一行 export。
  # 先把「配置文件里出现的那些变量」的当前值记下来 —— 显式传进来的环境变量优先。
  # 这是惯例（env > config file），也避免了「以为在打桩、其实走了真实通道」这种事故：
  # 之前用 `SMTP_HOST=127.0.0.1 ./start.sh` 想指到本地假 SMTP，被文件里的
  # smtp.qq.com 悄悄覆盖，结果真的通过真实邮箱发出去了。
  __vars=$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*' "$HOME/.cuoti.env" | tr '\n' ' ')
  # ⚠️ 快照必须在 source **之前**存。原来是在 source 之后才 declare -p，
  # 拿到的已经是被覆盖后的新值，等于把新值又写了一遍 —— 什么都没恢复。
  __snap=$(mktemp)
  for __v in $__vars; do declare -p "$__v" 2>/dev/null; done > "$__snap"
  set -a
  # shellcheck disable=SC1091
  . "$HOME/.cuoti.env"
  set +a
  # declare -p 的输出本身就是合法 shell，直接 source 回来即可（带引号，安全）
  # shellcheck disable=SC1090
  . "$__snap"
  rm -f "$__snap"
  unset __vars __v __snap
fi

# 这行保留只为配合下面的空值检查（set -u 下的取默认值写法）
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
