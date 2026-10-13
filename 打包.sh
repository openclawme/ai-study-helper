#!/bin/bash
# =============================================================================
#  把「我的AI学习助手」打成一个可以搬到另一台服务器的包。
#
#  用法：  ./打包.sh [输出目录]        默认写到 /tmp
#
#  ⚠️ 包里**故意不带**这些东西，看清楚再决定要不要手动补：
#     .session_secret   会话签名密钥 —— 新机器自己生成一份，代价是所有人重新登录一次
#     .admin_pass       管理员口令哈希 —— 新机器用环境变量里那把重新生成
#     password.txt      早期遗留文件，代码里已经没人读了
#     ~/.cuoti.env      DeepSeek key / 邮箱授权码 —— **凭据不该进 tar 包**，用 scp 单独传
#     .git/             版本历史，新机器上重新 clone 更干净
#     __pycache__/      字节码，没意义
#     mistakes.db.bak-* 历史备份，需要的话自己单独拿
# =============================================================================
set -e
cd "$(dirname "$(readlink -f "$0")")"

OUT="${1:-/tmp}"
NAME="cuoti-deploy-$(date +%Y%m%d)"
STAGE="$OUT/$NAME"

echo "打包到 $STAGE …"
rm -rf "$STAGE"
mkdir -p "$STAGE"

# ── 代码 ────────────────────────────────────────────────────────────────
for f in main.py index.html admin.html start.sh requirements.txt README.md .gitignore; do
  [ -f "$f" ] && cp -a "$f" "$STAGE/" || echo "  ⚠️  缺文件：$f"
done

# ── 静态资源：favicon + 学生的照片/缩略图/动画 ────────────────────────────
mkdir -p "$STAGE/static"
cp -a static/*.png static/*.ico static/*.svg "$STAGE/static/" 2>/dev/null || true
for d in origin clean thumb anim; do
  if [ -d "static/$d" ]; then
    cp -a "static/$d" "$STAGE/static/"
    echo "  static/$d  $(find "static/$d" -type f | wc -l) 个文件"
  fi
done

# ── 中文字体 ────────────────────────────────────────────────────────────
# 新服务器上多半一个中文字体都没有，而 fonts.google.com 在国内机器上不一定连得通。
# 带上这 17MB，导出 PDF 就不会卡在下载上。
if [ -f fonts/NotoSansSC.ttf ]; then
  mkdir -p "$STAGE/fonts"
  cp -a fonts/NotoSansSC.ttf "$STAGE/fonts/"
  echo "  fonts/NotoSansSC.ttf  $(du -h fonts/NotoSansSC.ttf | cut -f1)"
fi

# ── 数据 ────────────────────────────────────────────────────────────────
if [ -f mistakes.db ]; then
  # 用 sqlite 自己的 backup 命令而不是 cp：服务正在跑，直接拷可能拿到写了一半的库
  python3 - "$STAGE/mistakes.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect("mistakes.db")
dst = sqlite3.connect(sys.argv[1])
with dst:
    src.backup(dst)          # 在线热备份，不需要停服务
dst.close(); src.close()
print("  mistakes.db 已热备份（服务不用停）")
PY
fi

# ── 部署说明和 Caddyfile ────────────────────────────────────────────────
[ -f DEPLOY.md ] && cp -a DEPLOY.md "$STAGE/"
[ -f Caddyfile.example ] && cp -a Caddyfile.example "$STAGE/"

# ── 收尾检查：确认没把凭据打进去 ────────────────────────────────────────
echo
echo "── 敏感文件检查 ──"
LEAK=0
for bad in .session_secret .admin_pass password.txt .cuoti.env; do
  if [ -e "$STAGE/$bad" ]; then echo "  ❌ 包里混进了 $bad"; LEAK=1; fi
done
[ "$LEAK" = 0 ] && echo "  ✅ 没有凭据混进来"
if [ -d "$STAGE/.git" ]; then echo "  ❌ 包里混进了 .git"; fi

# ── 打包 ────────────────────────────────────────────────────────────────
cd "$OUT"
tar czf "$NAME.tar.gz" "$NAME"
SIZE=$(du -h "$NAME.tar.gz" | cut -f1)
echo
echo "✅ 打包完成：$OUT/$NAME.tar.gz  ($SIZE)"
echo
echo "   传到新服务器："
echo "     scp $OUT/$NAME.tar.gz root@新服务器IP:/root/"
echo
echo "   别忘了一起传凭据（**单独传，不进包**）："
echo "     scp ~/.cuoti.env root@新服务器IP:/root/cuoti.env"
