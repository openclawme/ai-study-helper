# 部署到另一台服务器

> 全程约 20 分钟。命令里的 `新服务器IP` 和 `你的域名` 自己替换。

---

## ⚠️ 先看这一条：论文里写着旧地址

论文（`~/paper/`，会提交的 Word 版）的 4.4.12 里写着试用地址：

```
http://8.219.92.145:8000
```

**工具一搬家，这个地址就失效了。** 三种处理方式，先想好再动手：

| 做法 | 说明 |
|---|---|
| **旧服务器继续开着** | 最省事。论文里的地址照旧能用，新机器另作他用 |
| **改论文里的地址** | 换成新域名，重新跑 `~/paper/存档.sh` 出新版本和 Word |
| **加一条跳转** | 旧机器上留着 8000 端口，把 `/` 跳转到新地址 |

---

## 一、新服务器上先装系统依赖

Ubuntu / Debian（阿里云默认镜像）:

```bash
apt update
apt install -y python3 python3-venv python3-pip fonts-noto-cjk
```

- `fonts-noto-cjk` 是**中文字体**。不装的话导出 PDF 会变空白或方块
  （包里的 `fonts/NotoSansSC.ttf` 是兜底，两个都有更稳）
- 如果新机器要跑 `opencv`，**不要**装 `libgl1` —— requirements 里是
  `opencv-python-headless`，本来就不需要图形库

---

## 二、在本机打包

```bash
cd /home/admin/gcl
./打包.sh                    # 产物在 /tmp/cuoti-deploy-日期.tar.gz，约 32MB
```

包里**故意不带凭据**（`.session_secret` / `.admin_pass` / `~/.cuoti.env`），
脚本最后会检查一遍。带的东西：

```
main.py  index.html  admin.html  start.sh  requirements.txt  README.md
static/   学生的照片、缩略图、动画（约 23MB）
fonts/    NotoSansSC.ttf（17MB，免得新机器连不上 Google Fonts）
mistakes.db   在线热备份出来的，服务不用停
```

---

## 三、传到新服务器

```bash
# 应用包
scp /tmp/cuoti-deploy-*.tar.gz root@新服务器IP:/root/

# 凭据单独传 —— 它不该进 tar 包
scp ~/.cuoti.env root@新服务器IP:/root/cuoti.env
```

`~/.cuoti.env` 里有 DeepSeek key 和邮箱授权码。传完在**两台机器上**都确认一下权限：

```bash
chmod 600 ~/.cuoti.env
```

---

## 四、解包、装依赖

在**新服务器**上：

```bash
cd /root
tar xzf cuoti-deploy-*.tar.gz
mv cuoti-deploy-* /root/cuoti            # 目录名统一成 cuoti
cd /root/cuoti

# 用 venv，不污染系统 Python（Ubuntu 24.04 起 pip 直接装会被 PEP 668 拦住）
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

装完自检一下（**这一步能提前发现依赖缺失**）：

```bash
.venv/bin/python -c "import fastapi, uvicorn, openai, cv2, numpy, PIL, reportlab, pypdfium2, httpx2; print('依赖 OK')"
```

---

## 五、配环境变量

```bash
mv /root/cuoti.env ~/.cuoti.env
chmod 600 ~/.cuoti.env
nano ~/.cuoti.env
```

**必须改/加这几行**（其余照抄原来的）：

```bash
# 原来的照抄
DEEPSEEK_API_KEY=sk-xxxx
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的邮箱
SMTP_PASS=授权码

# ↓ 新机器上要加的
ADMIN_PASS=管理员口令，至少12位

# 用 venv 的解释器（第四步建的那个）
PYTHON=/root/cuoti/.venv/bin/python

# 走 HTTPS 时打开；调试阶段先别开，否则浏览器不回传 Cookie，登录会失效
COOKIE_SECURE=1
```

> 数据库里的用户口令是加密存的，**原样搬过来就能用**，不用重设。
> 但 `.session_secret` 没搬 —— 新机器会自己生成一份，代价是**所有人重新登录一次**。

---

## 六、起服务、先验证 HTTP

```bash
cd /root/cuoti
./start.sh
```

看到启动横幅、且写着「管理后台：已启用」就对了。然后：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login   # 应 200
```

**先从本机 IP 访问一次**，确认能看到登录页、能登进去、错题列表有 35 道。

> ⚠️ 这时候 `COOKIE_SECURE=1` 会让 http 访问登录不上——如果第六步就要用 IP 试，
> 先把 `COOKIE_SECURE` 注释掉，等第七步上了 HTTPS 再打开、重启。

---

## 七、上 HTTPS（Caddy）

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

**先把域名解析好**：在你的域名服务商那里加一条 A 记录，指向新服务器的公网 IP。
等 `ping 你的域名` 能返回新 IP 再往下走（证书申请要求域名已经指过来）。

```bash
cp /root/cuoti/Caddyfile.example /etc/caddy/Caddyfile
nano /etc/caddy/Caddyfile          # 把 cuoti.example.com 换成你的域名
systemctl reload caddy
systemctl status caddy             # 看有没有报错
```

Caddy 会自己去 Let's Encrypt 申请证书并自动续期，不用 certbot。

**然后把 8000 端口收起来**：在 `~/.cuoti.env` 里加

```bash
BIND_HOST=127.0.0.1
COOKIE_SECURE=1
```

重启服务：

```bash
cd /root/cuoti && ./start.sh
```

现在 `http://新IP:8000` 从公网**连不上了**，只能走 `https://你的域名`。
记得在阿里云安全组里放行 80 和 443（8000 可以关掉了）。

---

## 八、验证清单

```
□ https://你的域名/login 能打开
□ 用原来的账号密码能登进去
□ 错题列表有 35 道、缩略图都显示得出来
□ 点开一道题，原图/干净图/对照都能看
□ 导出 PDF 一次（验证中文字体没问题）
□ https://你的域名/admin 能开，管理员口令能进
□ 后台「系统」卡片里磁盘/内存有数（CPU 第一次显示"采样中"，刷新即出）
□ http://新IP:8000 从公网访问连不上（说明端口收好了）
□ curl -I https://你的域名 看证书是不是 Let's Encrypt 签的
```

---

## 九、常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: httpx2` | 用旧的 requirements 装的 | 已修，确认 `requirements.txt` 里有 `httpx2` |
| 登录点下去没反应 / 一直跳登录页 | `COOKIE_SECURE=1` 但还在用 http 访问 | 要么上 HTTPS，要么先注释掉这一行 |
| 导出 PDF 中文变方块 | 没装中文字体，且 `fonts/` 没搬过来 | `apt install fonts-noto-cjk` |
| 证书申请失败 | 域名没解析过来，或 80 端口没放行 | `dig 你的域名` 确认指向新 IP；安全组放行 80 |
| 所有人都被限流锁住 | 前面有反代但 `_client_ip()` 拿不到真实 IP | 已修：只在直连方是本机时才采信 `X-Forwarded-For`（Caddy 会自动带） |
| 改了 main.py 界面却不变 | admin.html 每次从磁盘读，main.py 不是 | `./start.sh` 重启 |
| 忘了管理员口令 | —— | `rm /root/cuoti/.admin_pass` 再重启，回到环境变量里那把 |

---

## 十、以后怎么更新

新机器上改完代码：

```bash
cd /root/cuoti && ./start.sh
```

如果代码是从本机传过去的，重新打包只传代码部分也行（数据不用再传）：

```bash
# 本机
cd /home/admin/gcl && tar czf /tmp/code.tgz main.py index.html admin.html start.sh requirements.txt
scp /tmp/code.tgz root@新服务器IP:/root/cuoti/
# 新服务器
cd /root/cuoti && tar xzf code.tgz && ./start.sh
```
