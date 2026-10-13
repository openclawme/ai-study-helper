#!/bin/bash
# =============================================================================
#  一条命令把整套东西迁到另一台服务器（装依赖 → 传包 → 建 venv → 起服务 → 验证）。
#
#  用法：
#     ./迁移.sh root@新服务器IP                    # 只用 HTTP
#     ./迁移.sh root@新服务器IP cuoti.你的域名.com  # 顺带把 HTTPS 也配好
#
#  前提：本机能免密登上目标机（见脚本末尾的说明）。
#
#  ⚠️ 什么会传过去：
#     代码 + 学生的错题数据 + 照片 + 中文字体 + ~/.cuoti.env（**含 DeepSeek key**）
#     ~/.cuoti.env 是 scp 直传，不经过任何中间文件、也不会打印到屏幕上。
#
#  ⚠️ 什么**不会**传：
#     .session_secret（新机器自己生成，代价是所有人重新登录一次）
#     .admin_pass    （新机器用环境变量里那把重新生成）
# =============================================================================
set -euo pipefail

TARGET="${1:-}"
DOMAIN="${2:-}"

if [ -z "$TARGET" ]; then
  echo "用法： ./迁移.sh root@新服务器IP [你的域名]"
  exit 1
fi

HOST="${TARGET#*@}"
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# 专用密钥（`ssh-keygen -t ed25519 -f ~/.ssh/cuoti_deploy`）。
# 不写进 ~/.ssh/config 是为了不干扰你其它用途的默认密钥。
KEY="$HOME/.ssh/cuoti_deploy"
SSHOPT=(-i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)
SSH() { ssh "${SSHOPT[@]}" "$@"; }
SCP() { scp "${SSHOPT[@]}" "$@"; }

echo "════════════════════════════════════════════════════════"
echo "  迁到 $TARGET ${DOMAIN:+（HTTPS: $DOMAIN）}"
echo "════════════════════════════════════════════════════════"

# ── 0. 别把自己迁给自己 ────────────────────────────────────────────────
MY_IPS=$(hostname -I 2>/dev/null || true)
if echo " $MY_IPS " | grep -q " $HOST "; then
  echo "❌ $HOST 看起来就是本机，中止。"
  exit 1
fi

# ── 1. 先测能不能连上 ──────────────────────────────────────────────────
echo
echo "① 测试连接 …"
if ! ssh "${SSHOPT[@]}" -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" 'echo ok' >/dev/null 2>&1; then
  echo "❌ 连不上 $TARGET（或者需要输密码）。"
  echo
  echo "   本机还没有能登上那台服务器的身份。两种办法："
  echo
  echo "   办法 A（推荐，之后我就能全自动做）："
  echo "     1) 本机生成一把专用密钥： ssh-keygen -t ed25519 -f ~/.ssh/cuoti_deploy -N ''"
  echo "     2) 把公钥装到新机器（下面这条会让你输一次新机器的密码）："
  echo "        ssh-copy-id -i ~/.ssh/cuoti_deploy.pub $TARGET"
  echo "     3) 回来重跑本脚本"
  echo
  echo "   办法 B：你自己在新机器的阿里云控制台里，把这把公钥加到 root 的"
  echo "     ~/.ssh/authorized_keys（文件内容用 cat ~/.ssh/cuoti_deploy.pub 看）"
  exit 1
fi
echo "   ✅ 通了"

# ── 2. 本机打包 ────────────────────────────────────────────────────────
echo
echo "② 打包 …"
"$SRC/打包.sh" /tmp >/dev/null
PKG=$(ls -t /tmp/cuoti-deploy-*.tar.gz | head -1)
echo "   $(basename "$PKG")  $(du -h "$PKG" | cut -f1)"

# ── 3. 新机器装系统依赖 ────────────────────────────────────────────────
echo
echo "③ 新机器装系统依赖（python3-venv / 中文字体）…"
SSH "$TARGET" 'export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip fonts-noto-cjk rsync >/dev/null
  echo "   ✅ $(python3 --version)"'

# ── 4. 传包和凭据 ──────────────────────────────────────────────────────
echo
echo "④ 传文件 …"
SCP -q "$PKG" "$TARGET:/root/"
SCP -q "$SRC/DEPLOY.md" "$SRC/Caddyfile.example" "$TARGET:/root/"
if [ -f "$HOME/.cuoti.env" ]; then
  SCP -q "$HOME/.cuoti.env" "$TARGET:/root/cuoti.env"
  echo "   ✅ 代码+数据+字体，以及 ~/.cuoti.env（凭据，不打印）"
else
  echo "   ⚠️  本机没有 ~/.cuoti.env，新机器起来后 AI 功能会不可用"
fi

# ── 5. 解包、建 venv、装依赖 ───────────────────────────────────────────
echo
echo "⑤ 解包 + 建 venv + 装 Python 依赖（要一两分钟）…"
SSH "$TARGET" "set -e
  cd /root
  rm -rf cuoti && mkdir cuoti
  # --strip-components=1：直接把包里的顶层目录内容铺到 cuoti/。
  # 原来写的是 `tar xzf ...; mv cuoti-deploy-* cuoti`，但通配符会**同时**
  # 匹配到 tar 包本身和解出来的目录，mv 收到两个源就报 "target is not a directory"。
  tar xzf \$(ls -t cuoti-deploy-*.tar.gz | head -1) -C cuoti --strip-components=1
  cd cuoti
  python3 -m venv .venv
  .venv/bin/pip install -q -U pip
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/python -c 'import fastapi,uvicorn,openai,cv2,numpy,PIL,reportlab,pypdfium2,httpx2; print(\"   ✅ 依赖齐全\")'"

# ── 6. 配环境变量（只补缺的，不动原有值）──────────────────────────────
echo
echo "⑥ 配环境变量 …"
SSH "$TARGET" "set -e
  [ -f /root/cuoti.env ] || { echo 'DEEPSEEK_API_KEY=' > /root/cuoti.env; }
  mv -f /root/cuoti.env /root/.cuoti.env
  chmod 600 /root/.cuoti.env
  # PYTHON 指到 venv；已有就覆盖成对的
  sed -i '/^PYTHON=/d' /root/.cuoti.env
  echo 'PYTHON=/root/cuoti/.venv/bin/python' >> /root/.cuoti.env
  # ADMIN_PASS 没配就当场生成一个强口令（只打印这一次）
  if ! grep -q '^ADMIN_PASS=..' /root/.cuoti.env; then
    NEWPW=\$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
    echo \"ADMIN_PASS=\$NEWPW\" >> /root/.cuoti.env
    echo \"   ⚠️  新生成的管理员口令（只显示这一次）：\$NEWPW\"
  else
    echo '   ✅ 沿用原来的管理员口令'
  fi
  echo '   ✅ 环境文件就位（600）'"

# ── 7. 起服务并验证 ────────────────────────────────────────────────────
echo
echo "⑦ 起服务 …"
SSH "$TARGET" 'cd /root/cuoti && ./start.sh' | sed -n '1,20p'

echo
echo "⑧ 验证 …"
SSH "$TARGET" 'set -e
  printf "   /login            HTTP "; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/login
  printf "   /api/mistakes     HTTP "; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/mistakes
  printf "   /admin            HTTP "; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/admin
  python3 - <<PY
import sqlite3
c = sqlite3.connect("/root/cuoti/mistakes.db")
print("   账号 %d 个 / 错题 %d 道 / 对话 %d 条" % (
    c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
    c.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0],
    c.execute("SELECT COUNT(*) FROM chats").fetchone()[0]))
PY'

# ── 9. HTTPS（可选）────────────────────────────────────────────────────
if [ -n "$DOMAIN" ]; then
  echo
  echo "⑨ 配 HTTPS（Caddy + $DOMAIN）…"
  SSH "$TARGET" "set -e
    if ! command -v caddy >/dev/null; then
      apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
      apt-get update -qq && apt-get install -y -qq caddy >/dev/null
    fi
    # 同时覆盖 apex 和 www：只签 apex 的话，敲 www. 进来浏览器会直接报证书错误
    sed 's/cuoti.example.com/$DOMAIN/g' /root/Caddyfile.example > /etc/caddy/Caddyfile
    caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || echo '   ⚠️ Caddyfile 校验没过'
    systemctl reload caddy || systemctl restart caddy
    # 8000 只留给本机反代，公网扫不到
    sed -i '/^BIND_HOST=/d;/^COOKIE_SECURE=/d' /root/.cuoti.env
    echo 'BIND_HOST=127.0.0.1' >> /root/.cuoti.env
    echo 'COOKIE_SECURE=1' >> /root/.cuoti.env
    cd /root/cuoti && ./start.sh >/dev/null
    sleep 3
    printf '   https 自检        HTTP '; curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/login || true
    printf '   8000 从公网还通吗  '; curl -s -o /dev/null -m 5 -w 'HTTP %{http_code}（连不上才是对的）\n' http://$DOMAIN:8000/login || echo '连不上 ✅'"
fi

echo
echo "════════════════════════════════════════════════════════"
echo "  ✅ 迁完了"
echo
echo "   http://$HOST:8000/login"
[ -n "$DOMAIN" ] && echo "   https://$DOMAIN/login"
echo
echo "   下面这些**要自己确认**（我没法替你点）："
echo "   □ 用原来的账号密码登录，看错题列表是不是 35 道"
echo "   □ 打开 /admin 用管理员口令进去"
echo "   □ 导出一次 PDF，确认中文不是方块"
echo "   □ 阿里云安全组放行 80/443（走 HTTPS 时），8000 可以关掉"
echo
echo "   ⚠️ 论文 4.4.12 里写的还是旧地址 http://8.219.92.145:8000 ——"
echo "      要么旧机器继续开着，要么改论文重新存档。"
echo "════════════════════════════════════════════════════════"
