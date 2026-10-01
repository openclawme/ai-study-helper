# -*- coding: utf-8 -*-
"""
===============================================================================
 我的AI学习助手（初三 7 科 AI 错题本）——  后端服务 (main.py)
===============================================================================
 技术栈：Python 3.10+ / FastAPI / Uvicorn / openai SDK / SQLite / OpenCV / reportlab
 AI 后端：DeepSeek 官方 API，模型 deepseek-flash（V4.1 Flash，原生多模态，文本+图片 -> 文本）

 关于"版本B 纯净题"的说明（务必阅读）：
   DeepSeek 的 deepseek-flash 是「文本+图片 -> 文本」的模型，**无法输出图片**。
   因此"让 AI 把红笔擦掉再吐回一张干净图片"在物理上不可行。本项目采用双轨实现：
     · 版本B-图片  clean_[id].jpg  —— 由本机 OpenCV 真实做像素级处理：
                                     HSV 去红笔 + 光照归一化漂白背景 + 对比度拉伸。
     · 版本B-文字  clean_text      —— 由 deepseek-flash 视觉识别，输出纯净的
                                     "黑白印刷体题干"（Markdown + LaTeX），
                                     概念最干净、最适合考前重做与打印。
   两者都存库，前端可一键切换，PDF 导出也可自选"图片版 / 文字版"。

 启动：
     pip install -r requirements.txt
     python main.py
   手机与电脑处于同一 WiFi，浏览器打开启动时打印的「局域网地址」即可拍照上传。
===============================================================================
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import shutil
import socket
import sqlite3
import threading
import subprocess
import tempfile
import time
import urllib.request
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import Any, Dict, List, Optional, Tuple

import cv2
import httpx2                      # openai SDK 内部用的 http 库（不是 httpx）
import numpy as np
import pypdfium2 as pdfium
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas

# =============================================================================
# 一、配置区（部署时只需要改这里）
# =============================================================================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "YOUR_KEY_HERE")
BASE_URL = "https://api.deepseek.com"
MODEL_NAME = "deepseek-flash"          # 必须使用最新的多模态统一名称（V4.1 Flash）

# 上传图片的处理上限：手机原图 4000~6000px，压到 2400px 足够看清手写与批改
MAX_STORED_EDGE = 2400                 # 落盘原图的最长边
MAX_AI_EDGE = 1600                     # 送进 AI 的最长边（官方按 ~1300x1300 折算 token，再大是浪费）
JPEG_QUALITY = 92
AI_MAX_RETRY = 3                       # json_object 模式官方承认「有概率返回空 content」，故重试
# ⚠️ max_tokens 是「推理 + 正文」的总预算，不是正文长度。
# deepseek-flash 默认带思考模式，实测：看图 + 真实提问会烧掉 3000+ 个 reasoning token。
# 预算给小了会得到 finish_reason=length 且 content 为空——看起来像「模型不吭声」，
# 实际是思考过程把配额吃光了。故这里给足。
AI_MAX_TOKENS = 8000
CHAT_MAX_TOKENS = 32000     # 生成可交互动画时，光思考就可能烧掉 8000+

# ── 防止 AI 无休止地想下去 ────────────────────────────────────────────────────
#
# 这个模型的思考 token 是算在 max_tokens 里的，光靠 token 上限拦不住「想很久」：
# 实测一次普通答疑的思考过程就有 21092 字，用户盯着屏幕干等一分多钟。
# 三层保护，缺一不可：
#
#   ① reasoning_effort —— 从源头少想。实测（一道需要多步推理的概率题，非流式）：
#        基线                思考 4118 tok / 19.8s / 正文 877 字
#        reasoning_effort=low 思考 2246 tok / 12.5s / 正文 932 字   ← 采用
#        thinking.budget_tokens=512  思考 3700 tok（**服务端直接忽略了，不生效**）
#        thinking 完全关掉    思考 无     /  6.2s  / 正文 2178 字（能跑，但太激进）
#      注意 low 只是「少想」，不是「不想」；质量没有下降（正文反而更长）。
#   ② deadline —— 硬时限，到点就断。这是唯一真正兜底的一层：
#      前面 ① 是「劝」模型少想，只有这一条是**保证**有天花板。
#   ③ idle —— 卡死看门狗。连接没断但一个字都不来，判定为卡住。
#
# ⚠️ 这三样**必须按场景分档，不能一刀切**。
# 一开始我图省事给全局设了「一律 low + 一律 180 秒」，那是错的：
# 生成可交互动画要规划分步、还要吐几百行 HTML/JS，本来就该多花时间——
# 一刀切会把动画**截断在半路**，得到一段讲到一半的答案或一个残缺的动画。
# 孩子点「动画讲解」是明确要求了"慢工出细活"，这里就该给足预算。
#
# 分档原则：**问答求快，创作求好**。上限仍然有，只是天花板抬高到「不可能停不下来」。
AI_PROFILES: Dict[str, Dict[str, Any]] = {
    # 普通答疑：快是第一位。实测 low 不会掉质量（正文反而更长）。
    "chat": {
        "reasoning_effort": os.getenv("AI_REASONING_EFFORT", "low"),
        "deadline": int(os.getenv("AI_DEADLINE_SEC", "180")),        # 3 分钟
        "idle": int(os.getenv("AI_IDLE_SEC", "45")),
    },
    # 生成动画：效果第一位。不限制思考（留空 = 用模型默认），时限放宽到 10 分钟。
    # 10 分钟不是「够用」的估计，而是「再久就一定是卡住了」的上限——
    # 真跑到 10 分钟本身就是异常，切断并把已生成的部分保住才是对的。
    "anim": {
        "reasoning_effort": os.getenv("AI_ANIM_REASONING_EFFORT", ""),
        "deadline": int(os.getenv("AI_ANIM_DEADLINE_SEC", "600")),   # 10 分钟
        "idle": int(os.getenv("AI_ANIM_IDLE_SEC", "90")),
    },
}
AI_HTTP_TIMEOUT = float(os.getenv("AI_HTTP_TIMEOUT", "300"))    # 非流式调用的 socket 超时

# TCP 连接的空闲保活时长。**uvicorn 默认只有 5 秒，这是个坑。**
#
# 浏览器会把用过的连接留在池子里好几分钟（Chrome 约 5 分钟），
# 而服务端 5 秒就把两头都以为还活着的连接关掉了。等浏览器下次拿这条
# 已经死掉的连接发请求 —— 网络层直接失败，前端看到的是一句
# `Failed to fetch`，后端日志里连一条记录都没有（请求根本没到）。
# 表现为「偶尔」出错：只有刚好复用到过期连接时才发生。
#
# 实测（同一条 TCP 连接，隔 N 秒发第二个请求）：
#   间隔 2s/4s → 连接还在；间隔 6s/10s → 已被服务端关闭（读到 EOF）
#
# 服务端保活必须长过浏览器的池子超时，否则这个竞态永远存在。
KEEPALIVE_SEC = int(os.getenv("KEEPALIVE_SEC", "75"))


def _profile(name: str) -> Dict[str, Any]:
    """取场景档位，认不出来就退回 chat（快档，兜底永远选保守的那个）。"""
    return AI_PROFILES.get(name) or AI_PROFILES["chat"]
                              # 个 token，预算给小了会得到「只有思考、没有正文」
AI_TEMPERATURE = 0.4

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
ORIGIN_DIR = os.path.join(STATIC_DIR, "origin")
CLEAN_DIR = os.path.join(STATIC_DIR, "clean")
# 列表缩略图。**必须有**：卡片上那个位置只有 56×56 像素，之前直接加载整张原图，
# 而 clean 图是二值化的，边缘锐利、JPEG 压缩率极差 —— 实测单张能到 1.3MB，
# 一个科目 7 张卡片就是好几 MB。缩略图把单张压到 10KB 上下。
THUMB_DIR = os.path.join(STATIC_DIR, "thumb")
THUMB_EDGE = 240        # 缩略图最长边。卡片显示 56px，两倍屏也只要 112px，240 足够清晰
DB_PATH = os.path.join(BASE_DIR, "mistakes.db")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

SUBJECTS = ["语文", "数学", "英语", "物理", "化学", "历史", "道德与法治"]

# 启动即建目录（用户要求启动时 os.makedirs；此处提前建好，保证 mount 不报错）
for _d in (STATIC_DIR, ORIGIN_DIR, CLEAN_DIR):
    os.makedirs(_d, exist_ok=True)


# =============================================================================
# 二、数据库（标准库 sqlite3，零依赖）
# =============================================================================

def get_conn() -> sqlite3.Connection:
    """
    每次请求开一条连接：SQLite 最稳的并发姿势，避免跨线程复用。

    顺带自愈：如果运行期间有人把 mistakes.db 删了（想清空数据），
    新连接会建出一个**没有任何表的空库**，之后每个请求都 500 且报
    "no such table: mistakes"，非常难排查。这里检测到表缺失就自动重建。
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # 检查两张表（而不是一张）：这样从旧版本升级上来、缺 chats 表时也能自动补建
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' "
        "AND name IN ('mistakes','chats','users','signups','mail_log')"
    ).fetchone()["n"]
    if n < 5:
        _create_schema(conn)
    else:
        # 老库补列 / 改列。CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，只能手动 ALTER。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(mistakes)")}
        if "error_analysis" not in cols and "analysis" not in cols:
            # 最老的库：连错因分析这一列都没有
            conn.execute("ALTER TABLE mistakes ADD COLUMN analysis TEXT NOT NULL DEFAULT ''")
        elif "error_analysis" in cols and "analysis" not in cols:
            # 改名：这一列现在装的不只是「错因」——经典题存的是「好在哪」，
            # 名字不改的话，以后读到 `error_analysis` 里是一段表扬会当场懵住。
            # RENAME COLUMN 需要 SQLite ≥ 3.25（Python 3.12 自带 3.45，够）。
            conn.execute("ALTER TABLE mistakes RENAME COLUMN error_analysis TO analysis")
        if "kind" not in cols:
            # 默认 'mistake'：老数据全都是错题，这个默认值正好把它们标记对
            conn.execute("ALTER TABLE mistakes ADD COLUMN kind TEXT NOT NULL DEFAULT 'mistake'")
        # ── 学习行为字段 ────────────────────────────────────────────────
        # 在此之前，两张表记的全是「AI 产出了什么」，关于孩子本人一个字段都没有。
        # 而「虚假精通」恰恰只能从孩子的行为里看出来 —— 所以补上这几列。
        if "variant_result" not in cols:
            # 变式题做了没、做对没：'' = 还没做 / right / wrong
            # 这是全应用唯一能证伪「虚假精通」的信号，之前做完没有任何回写。
            conn.execute("ALTER TABLE mistakes ADD COLUMN variant_result TEXT NOT NULL DEFAULT ''")
        if "variant_done_at" not in cols:
            conn.execute("ALTER TABLE mistakes ADD COLUMN variant_done_at TEXT NOT NULL DEFAULT ''")
        if "review_due_at" not in cols:
            # 间隔重复：下次该把这题翻出来看的日期（YYYY-MM-DD）。
            # 错题本天然适合复习调度，但在这之前所有错题都是「存进去就沉底」。
            conn.execute("ALTER TABLE mistakes ADD COLUMN review_due_at TEXT NOT NULL DEFAULT ''")
        if "review_stage" not in cols:
            # 连续答对了几轮。答对一次进一级、间隔拉长；答错直接归零。
            conn.execute("ALTER TABLE mistakes ADD COLUMN review_stage INTEGER NOT NULL DEFAULT 0")
            # 老数据：全部按「今天就该复习」处理 —— 它们从没被复习过，本来就该先过一遍。
            conn.execute("UPDATE mistakes SET review_due_at=? WHERE review_due_at=''",
                         (datetime.now().strftime("%Y-%m-%d"),))
        if "email" not in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "user_id" not in cols:
            # 老库里的题还没有归属。留 0，等第一个注册的账号来认领
            # （见 /api/register）—— 直接猜一个用户塞进去反而更糟。
            conn.execute("ALTER TABLE mistakes ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_owner ON mistakes(user_id, id)")
        if "source" not in cols:
            # 来自哪里：照片 / 《期中卷》第 3 页 / 文本文件。
            # 一份 PDF 拆出来的多道题会共用同一张页面图，没有这列就说不清为什么
            # 三道题的「原图」长得一模一样。
            conn.execute("ALTER TABLE mistakes ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            # 加这列之前**只能**上传照片，所以老数据一律回填「照片」——
            # 这是事实不是猜测。不回填的话，老题在详情页会缺一行来源，看着像丢了数据。
            conn.execute("UPDATE mistakes SET source='照片' WHERE source=''")
        conn.commit()
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    # 账号。口令用 pbkdf2 + **每个用户独立的随机盐**存，
    # 不是全局盐 —— 全局盐会让「相同口令 → 相同哈希」，而且换密钥等于所有人密码失效。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT    NOT NULL UNIQUE,
            pw_hash    TEXT    NOT NULL,            -- pbkdf2_sha256$轮数$盐$哈希
            email      TEXT    NOT NULL DEFAULT '', -- 找回密码用；注册时已验证过
            created_at TEXT    NOT NULL
        )
        """
    )
    # 待验证的注册。**为什么单独一张表，而不是给 users 加 verified 标志**：
    # 未验证的行混在 users 里，UNIQUE(username) 会挡住用户重填，
    # 而且每条查询都得记得过滤 verified —— 漏一处就是个能登录的空账号。
    # 独立表 + 验证通过才写 users，这类漏检天然不存在。
    # 发信节流。**必须独立于 signups 的生命周期**：验证成功时那条待验证记录会被删掉，
    # 如果节流信息也存在那里，删完就查不到「刚发过」，等于没有节流 ——
    # 循环调用就能把人邮箱轰炸一遍、顺带烧光 SMTP 配额。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_log (
            key     TEXT NOT NULL,
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maillog ON mail_log(key, sent_at DESC)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signups (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT    NOT NULL UNIQUE,
            email      TEXT    NOT NULL,
            pw_hash    TEXT    NOT NULL,   -- 先存哈希，验证通过直接搬进 users，明文密码不落库
            code_hash  TEXT    NOT NULL,   -- 验证码的哈希，不存明文
            expires_at TEXT    NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0,
            sent_at    TEXT    NOT NULL,
            ip         TEXT    NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mistakes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            subject     TEXT    NOT NULL,           -- 七科之一
            tag         TEXT    NOT NULL DEFAULT '',-- 中考考点，如「数学-二次函数动点」
            title       TEXT    NOT NULL DEFAULT '',-- 卡片列表用的一句话摘要
            orig_path   TEXT    NOT NULL DEFAULT '',-- 版本A 原图（含手写+红笔）
            clean_path  TEXT    NOT NULL DEFAULT '',-- 版本B 去红笔图片
            clean_text  TEXT    NOT NULL DEFAULT '',-- 版本B 纯净印刷体题干（Markdown+LaTeX）
            variant_q   TEXT    NOT NULL DEFAULT '',-- 同考点变式练习题
            variant_a   TEXT    NOT NULL DEFAULT '',-- 变式题详细解析
            kind        TEXT    NOT NULL DEFAULT 'mistake', -- mistake 错题 / classic 经典题
            analysis    TEXT    NOT NULL DEFAULT '',-- 核心字段：错题=错因，经典题=好在哪
            source      TEXT    NOT NULL DEFAULT '',-- 来源：照片 / 《期中卷》第 3 页 / 文本文件
            user_id     INTEGER NOT NULL DEFAULT 0, -- 归属账号；0 = 还没认领的老数据
            ai_status   TEXT    NOT NULL DEFAULT '',-- ok / no_key / error:xxx
            created_at  TEXT    NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subject ON mistakes(subject)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON mistakes(created_at DESC)")
    # 每个请求都要按 user_id 过滤，这个索引是必须的
    conn.execute("CREATE INDEX IF NOT EXISTS idx_owner ON mistakes(user_id, id)")
    # 与 AI 的对话记录（按错题隔离，持久化，关了浏览器也还在）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            mistake_id INTEGER NOT NULL,
            role       TEXT    NOT NULL,   -- user / assistant
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL,
            anim_url   TEXT    NOT NULL DEFAULT '',  -- AI 生成的可交互动画
            anim_title TEXT    NOT NULL DEFAULT '',
            mode       TEXT    NOT NULL DEFAULT '',  -- guide / full：这次是引导还是直接给讲解
            -- 归属。**自由问答线程也必须有它**：那条线程用 mistake_id=0 当哨兵，
            -- 没有 user_id 的话所有用户的自由问答会撞在同一批行里。
            user_id    INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_mid ON chats(mistake_id, id)")
    # ⚠️ 别在这里建 idx_chat_owner —— 此时 user_id 这一列可能还不存在
    # （老库要先走下面的 ALTER）。建索引必须排在补列之后，见本函数末尾。
    # 轻量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，老库要手动 ALTER
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(chats)")}
    for col in ("anim_url", "anim_title", "mode"):
        if col not in existing:
            conn.execute(f"ALTER TABLE chats ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    if "user_id" not in existing:
        conn.execute("ALTER TABLE chats ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        # 老对话的归属跟着它所属的错题走；自由问答（mistake_id=0）留 0，
        # 和错题一样等第一个注册的账号认领。
        conn.execute("UPDATE chats SET user_id = "
                     "COALESCE((SELECT m.user_id FROM mistakes m WHERE m.id = chats.mistake_id), 0)")
    # 列一定存在了，现在才能建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_owner ON chats(user_id, id)")
    conn.commit()


def init_db() -> None:
    with closing(get_conn()) as conn:
        _create_schema(conn)


# 题目分两类。错题是本应用的主体，经典题是「好题收藏」——
# 两者的区别只在语义（analysis 字段的含义、AI 答疑的口径），数据结构完全一样。
KINDS = {"mistake": "错题", "classic": "经典题"}
KIND_LABEL = KINDS


def norm_kind(v: Any, default: str = "mistake") -> str:
    """把任意输入规整成合法的 kind。认不出来就退回 default（错题是主体，兜底选它）。"""
    s = str(v or "").strip().lower()
    if s in KINDS:
        return s
    # 容错：模型偶尔会回中文
    if "经典" in s or "好题" in s or "classic" in s:
        return "classic"
    return default


def _think_kwargs(profile: str = "chat") -> Dict[str, Any]:
    """限制思考的采样参数。空值就不传，交回服务端默认（anim 档就是这么放的）。"""
    eff = _profile(profile)["reasoning_effort"]
    return {"reasoning_effort": eff} if eff else {}


def _ai_fail(status: str) -> Dict[str, Any]:
    """AI 失败时的空壳结果。集中一处，免得每处 error 分支都手写一遍字段——漏一个就是 KeyError。"""
    return {"_status": status, "kind": "mistake", "tag": "", "analysis": "", "clean_text": ""}


def row_to_item(row: sqlite3.Row) -> Dict[str, Any]:
    """DB 行 -> 前端 JSON（附带可直接使用的静态资源 URL）。"""
    d = dict(row)
    # 走按归属鉴权的路由，不再直接暴露 /static 路径（见 serve_media 的说明）
    mid = d.get("id")
    d["orig_url"] = f"/media/origin/{mid}" if d.get("orig_path") else ""
    d["clean_url"] = f"/media/clean/{mid}" if d.get("clean_path") else ""
    # 列表卡片用缩略图，详情页仍用大图
    d["thumb_url"] = (f"/media/thumb/{mid}"
                      if (d.get("clean_path") or d.get("orig_path")) else "")
    d["ai_ok"] = str(d.get("ai_status", "")).startswith("ok")
    d["kind"] = norm_kind(d.get("kind"))
    d["kind_label"] = KINDS[d["kind"]]
    # 今天该不该复习这道题（前端据此打标/筛选）
    d["due"] = bool(d.get("review_due_at")) and str(d["review_due_at"]) <= _today()
    return d


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# 答对一次就往后推多久（天）。答错直接回到 1 天。
# 这套间隔取自间隔重复的常规做法（1 → 3 → 7 → 16 → 35），不是精确的 SM-2，
# 对这个场景够用：错题本的关键是「别沉底」，不是把算法调到最优。
REVIEW_INTERVALS = [1, 3, 7, 16, 35]


# =============================================================================
# 三、图片处理：版本A 落盘 + 版本B（OpenCV 去红笔 & 漂白背景）
# =============================================================================

def normalize_and_save(raw: bytes, dst_path: str, max_edge: int = MAX_STORED_EDGE) -> Tuple[int, int]:
    """
    把手机上传的原始字节标准化后落盘：
      · ImageOps.exif_transpose 修正手机竖拍照片的 EXIF 旋转（否则 cv2/浏览器都会看到横躺的卷子）
      · 等比缩放到 max_edge 以内，控制体积与后续 AI token 消耗
    返回 (宽, 高)。
    """
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_edge / float(max(w, h)))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        im.save(dst_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return im.size


def bytes_to_data_uri(raw: bytes, max_edge: int = MAX_AI_EDGE) -> str:
    """图片字节 -> 缩到 max_edge -> data:image/jpeg;base64,...（官方 vision 文档规定的内联格式）。"""
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_edge / float(max(w, h)))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def image_to_data_uri(path: str, max_edge: int = MAX_AI_EDGE) -> str:
    """读文件版。文档页是字节、照片是文件，两条路都留着，共用同一段缩放逻辑。"""
    with open(path, "rb") as f:
        return bytes_to_data_uri(f.read(), max_edge)


def make_clean_image(src_path: str, dst_path: str) -> Tuple[bool, str]:
    """
    版本B-图片：本机 OpenCV 处理，生成「去红笔 + 扫描件级白纸化」的重做底图。

    处理链（每一环都是实测调出来的，不是拍脑袋选的）：
      1) HSV 双区间抠出红笔批改。红色在 H 轴上跨 0 度环绕，必须分 [0,15] 与 [156,180] 两段。
      2) 膨胀掩膜后用 cv2.inpaint 把红笔区域「修复」掉（详见下方注释：置白和羽化
         都会在阈值后描出一圈黑疤，只有 inpaint 干净）。
      3) 转灰度。**不做中值滤波**——实测它会把中文细笔画抹断（见下方注释）。
      4) 自适应阈值（高斯加权，块 51，C=12）。
         实测对比：背景归一化(divide) 需要手工指定黑白场，换张暗照片就失效；
         自适应阈值基于局部对比，对光照梯度天然免疫，输出乌黑印刷体 + 纯白纸面。
    注意：黑笔手写痕迹无法用像素方法安全剥离（会连带把印刷体一起削掉），
         因此「手写过程」请使用版本B-文字（AI 纯净重排）来看。
    """
    img = cv2.imread(src_path, cv2.IMREAD_COLOR)
    if img is None:
        return False, "OpenCV 无法读取该图片"

    # --- 1) 红笔掩膜（覆盖正红/朱红/偏橙的批改笔） ---
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 32, 45]), np.array([15, 255, 255])) | \
           cv2.inRange(hsv, np.array([156, 32, 45]), np.array([180, 255, 255]))
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=2)

    # --- 2) 用 inpaint 把红笔区域「修复」掉 ---
    # 三种做法在真实中文卷子上的实测对比（都接同样的自适应阈值后段）：
    #   · img[mask]=255 直接置白 -> 红笔位置留下硬边台阶，阈值沿台阶描出一圈黑疤
    #   · 羽化掩膜再 alpha 混合  -> 过渡带是中间灰，被阈值判成黑，同样描出黑疤（更丑）
    #   · cv2.inpaint(TELEA)     -> 用周围纸面内容插值填补，既无台阶也无灰带，
    #                               红框彻底消失且纸面自然  ← 采用
    img = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)

    # --- 3) 灰度 ---
    # 这里刻意【不做中值滤波】：实测 medianBlur(3) 会把中文 1~2px 的细笔画直接抹掉，
    # 输出「已知二次函数」这类字会出现断笔缺画（英文笔画粗，所以早期用英文样张测不出来）。
    # 纸面颗粒改用「提高自适应阈值的 C 值」来抑制，代价可控且不伤笔画。
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # --- 4) 自适应阈值 -> 扫描件级黑白图 ---
    # C 值实测结论（仿真噪点照片上对比）：
    #   C=8  笔画最完整，但空白纸面出现大量颗粒黑点
    #   C=15 纸面最干净，但细笔画又被削断
    #   C=12 两者平衡点：笔画完整乌实，纸面颗粒可接受
    clean = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 12
    )

    # 二值图用高质量 JPEG（沿用 clean_[id].jpg 命名；q=95 时文字边缘几乎无振铃）
    ok, buf = cv2.imencode(".jpg", clean, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        return False, "图片编码失败"
    with open(dst_path, "wb") as f:
        f.write(buf.tobytes())
    return True, "ok"


# =============================================================================
# 四、DeepSeek 多模态：一次调用同时拿到 考点 / 纯净题干 / 变式题 / 解析
# =============================================================================

# 提取「一道题」的四条规则。单张照片上传（SYSTEM_PROMPT）和文档拆题（SPLIT_PROMPT）
# 共用同一份 —— 复制两份的话，以后调了一边的口径、另一边不会跟着变。
#
# ⚠️ 这里**不能**写成 r"""原始字符串"""：规则里有 $\\frac{a}{b}$ 这样的 LaTeX 示例，
# 普通字符串会把它变成 \frac（一个反斜杠，正是要给模型看的写法），
# 而原始字符串会原样留下两个反斜杠 —— 模型就会照着学成 \\frac。
# 这个项目之前在 TUTOR_SYSTEM 上正好栽过一次反向的坑（该用 r"" 却没用，
# 导致 \b、\f 被 Python 吃掉），两个方向都要当心。
_EXTRACT_RULES = """1. kind：判断这道题是「错题」还是「经典题」，只能取 mistake 或 classic 两个值之一。
   - mistake（错题）：**照片上有学生做错的证据** —— 有手写过程但算错/答错、
     老师打了叉、扣了分、写了订正或批注。
   - classic（经典题）：**照片上没有"做错"的痕迹** —— 空白题、作业本上做对的题、
     试卷上的优质题、老师发的例卷，或者学生手写过程完全正确。
   - 判不准时选 mistake：这个应用的主体是错题本，而且错题的字段更全，改起来也更容易。
   - **不要**因为"题目看起来有点难"就判成经典题，判据只有一条：**有没有做错的痕迹**。

2. tag：精准的中考考点标签，格式为「科目-考点」，
   例如「数学-二次函数动点」「化学-酸碱盐推断」「历史-近代史材料分析」「英语-定语从句」。

3. analysis：**这是最重要的字段，但内容随 kind 变。**

   【kind = mistake 时，写「错因分析」】你要像一位耐心的老师那样，
   对着照片上学生的**手写过程**和老师的**红笔批改**，说清楚三件事：
   - 他具体错在哪一步（引他写的那一行，例如「你第 2 行令 y=0 之后解成 x=…」）
   - 为什么会这么错（概念混淆？漏条件？符号/计算？审题？表述跳步？）
   - 正确的想法该是什么（一句话点破方向，但**不要写完整解答**）
   如果照片上看不出学生错在哪（例如只拍了空白题），就如实说
   「照片上看不出具体错在哪」，**绝不硬编一个错因**。

   【kind = classic 时，写「好在哪里」】换成一位欣赏这道题的命题人视角：
   - 这道题**妙在哪**（哪个设计让它成为好题：条件隐蔽？多知识点交汇？有陷阱？）
   - **关键一步**是什么（想出这一步就通了，点破但不写完整解答）
   - **该记住的通法**（这一类题的通用思路，下次遇到同类题怎么下手）

   两种情况的共同要求：
   - 用 Markdown 分点写，**250 字以内**，直击要害，不要面面俱到。
   - 这是给孩子看的，语气平和、对事不对人，别写成批评。

4. clean_text：擦除所有红笔批改与黑笔手写过程，仅还原出纯净的「黑白印刷体原题」。
   - 保留：题干、全部选项、图表/图形/表格的文字说明（如「如图，抛物线经过点 A(1,0)」）。
   - 丢弃：学生的解题步骤、答案、老师的勾叉、分数、批注、页眉页脚。
   - 公式一律用 LaTeX 行内写法：$x^2+2x-3=0$、$\\frac{a}{b}$、$\\sqrt{3}$。
   - 用 Markdown 组织（列表、加粗），保证可以直接打印给孩子重做。
   - 若原题信息不全（被遮挡/拍不全），就按可见部分还原，并在末尾用
     「（注：原题此处未拍全）」注明，绝不自己编造条件。"""

_WHO = """你是深耕中国初三中考 7 科（语文、数学、英语、物理、化学、历史、道德与法治）二十年的资深教研员，
同时是专业的中考文字提取器。用户上传的内容上可能有：
印刷体原题、学生黑笔手写过程、老师红笔批改痕迹（勾叉、分数、订正）。

用户收的不全是错题——**经典好题、压轴题、值得反复琢磨的题**也会传上来一起管理。
所以第一件事是判断这道题属于哪一类。"""

SYSTEM_PROMPT = _WHO + "\n\n你必须完成四件事，并严格以 json 对象输出（不要输出任何 json 以外的内容）：\n\n" \
    + _EXTRACT_RULES + "\n\n只输出 json，键名固定为 kind、tag、analysis、clean_text 四个字段。"

# 是否按 AI 给的范围把每道题从整页里裁出来。**默认关闭**（设 DOC_AUTO_CROP=1 打开）。
#
# 为什么默认关：让模型在一页里同时做「拆题」和「定位」两件事时，它会把整页**均分**成
# N 份，而不是逐题去读刻度尺。用尺子图当基准逐题核对过一版实测结果：
#     题1 模型 10%~25% / 真实 14.1%~16.6%   ✅
#     题2 模型 25%~31% / 真实 20.4%~24.6%   ❌ 完全没沾到
#     题3 模型 31%~38% / 真实 30.1%~31.2%   ⚠️
#     题4 模型 38%~44% / 真实 33.4%~34.4%   ❌
#     题5 模型 44%~50% / 真实 38.4%~46.6%   ⚠️
#     题6 模型 50%~60% / 真实 44.0%~46.6%   ❌
# 只有 1/6 完全正确 —— 而且区间**格式合法**，代码没法判断它对不对，只能照裁。
# 裁错的后果是：孩子点开第 4 题，看到的是第 2 题的图。这比不裁糟得多，
# 所以宁可不裁：一页上的每道题共用这张整页图，永远正确、也永远可解释。
DOC_AUTO_CROP = os.getenv("DOC_AUTO_CROP", "0") == "1"

# 文档拆题：一页上可能有好几道题，一次调用把它们全拆出来。
# 规则 1~4 与单张照片完全相同（同一份 _EXTRACT_RULES），只多一条「定位」。
_SPLIT_BODY = _WHO + """

这一次你拿到的**不是一道题，而是一整页（或多页）试卷**，上面通常印着好几道题。
请你把这一页**拆成一道一道的题目**，每道题各自按下面的规则处理。

拆题的规矩（很重要，拆错比拆少更麻烦）：
- **一道大题连同它下面的小问 (1)(2)(3) 算一道题**，绝对不要把小题拆开。
- 章节/大题标题（如「三、解答题（本题 12 分）」）归到它下面的第一道题里，不要单独成一条。
- **只提取题目，不要提取答案**：跳过参考答案页、解析页、答题卡、封面、目录、
  页眉页脚、装订线、以及已经印好的标准答案。
- 如果这一页压根没有题目（封面、答案页、空白页），返回空的 problems 数组，不要硬凑。
- 一页最多提取 12 道；确实超过就只提最前面的 12 道。

"""

# 下面这段用 % 而不是 str.format 拼：JSON 示例里的花括号会被 format 当成占位符
# （报 KeyError: '"problems"'），这类坑很隐蔽，直接用拼接最省事。
_SPLIT_JSON_HEAD = """只输出 json，结构固定为（不要输出任何 json 以外的内容）：
{
  "problems": [
    {
      "kind": "mistake 或 classic",
      "tag": "科目-考点",
      "analysis": "错因分析（kind=mistake）或好在哪（kind=classic），Markdown，250 字以内",
      "clean_text": "纯净印刷体题干（Markdown + LaTeX）"%s
    }
  ]
}"""

_SPLIT_LOCATE_RULE = r"""

5. top / bottom：这道题在这一页里的**纵向范围**，用**百分数**表示（0 = 页顶，100 = 页底）。
   看图**左侧那条尺子**：红字是百分数，每 5% 一条细线、每 10% 一条红线。
   - top 取「覆盖到这道题第一行」的那条刻度，bottom 取「覆盖到最后一问」的那条刻度。
   - **每道题各自独立地读刻度**，不要把整页均分成 N 份 —— 题目之间的空白有多大就留多大。
   - **宁可多留，不可切掉**：不确定时把范围往大取一整格。
   - 一道题跨越多条刻度线时，取最外圈的两条。"""

if DOC_AUTO_CROP:
    SPLIT_PROMPT = (_SPLIT_BODY
                    + "你必须对每道题完成下面 4 件事，外加第 5 件「定位」，并严格以 json 输出：\n\n"
                    + _EXTRACT_RULES + _SPLIT_LOCATE_RULE
                    + "\n" + (_SPLIT_JSON_HEAD % ',\n      "top": 20,\n      "bottom": 45'))
else:
    SPLIT_PROMPT = (_SPLIT_BODY
                    + "你必须对每道题完成下面 4 件事，并严格以 json 输出：\n\n"
                    + _EXTRACT_RULES + "\n" + (_SPLIT_JSON_HEAD % ""))

JSON_HINT = """
请以 json 格式输出，结构如下（严格遵守，不要加注释）：
{
  "kind": "mistake 或 classic",
  "tag": "科目-考点",
  "analysis": "错因分析（kind=mistake）或好在哪（kind=classic），Markdown，250 字以内",
  "clean_text": "纯净印刷体题干（Markdown + LaTeX）"
}
"""

# 变式题改成「按需生成」——上传时不再生成，孩子想看才点按钮。
# 理由：错题本的核心价值是「弄明白我错在哪」，做新题是下一步的事。
VARIANT_PROMPT = r"""你是初三中考命题老师。下面给你一道学生做过的错题（已提取成纯净题干）、
它的考点、以及学生的错因分析。请据此生成**一道同考点、同难度的变式练习题**。

要求：
1. 数值和情境全部换新，但考查的能力和解题方法完全一致，难度对标中考。
2. 重点针对学生的错因设计——尽量让他在这道新题上重新面对同一个坑。
3. 公式用 LaTeX：行内 $...$，独立成行 $$...$$。
4. 严格以 json 输出两个字段（不要输出别的内容）：
   {
     "variant_question": "变式题题干",
     "variant_analysis": "该变式题的详细解题步骤，分步写清『怎么想、怎么算』，并点破关键卡点与易错点，450 字以内"
   }"""
# 注意：这里原来还有第二个 JSON_HINT，是给变式题用的。但 call_variant 用的是
# VARIANT_PROMPT（JSON 结构已经写在它的第 4 条里），那个 JSON_HINT 根本没人用 ——
# 而 Python 会把同名的后一个定义覆盖前一个，于是**上传提取**那条链路拿到的
# 一直是变式题的 hint，两个提示自相矛盾。删掉。


def api_key_ready() -> bool:
    k = (DEEPSEEK_API_KEY or "").strip()
    return bool(k) and k != "YOUR_KEY_HERE" and len(k) > 12


def _client() -> OpenAI:
    return OpenAI(api_key=DEEPSEEK_API_KEY.strip(), base_url=BASE_URL, timeout=120.0, max_retries=0)


def extract_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    DeepSeek 开了 json_object 也可能夹带 ```json 围栏，或被截断。
    这里做三层兜底解析，保证拿到的字典一定可用。
    """
    if not text:
        return None
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    for candidate in (s, s[s.find("{"): s.rfind("}") + 1] if "{" in s and "}" in s else ""):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def call_deepseek(image_path: str, subject: str) -> Dict[str, Any]:
    """
    调用 deepseek-flash 的视觉能力，一次调用产出 kind / tag / analysis / clean_text。
    失败不抛异常，而是以 _status 说明原因，让上传流程依然能落库（原图 + 去红笔图不会丢）。
    """
    if not api_key_ready():
        return _ai_fail("no_key")

    try:
        data_uri = image_to_data_uri(image_path)
    except Exception as e:
        return _ai_fail(f"error:图片预处理失败 {e}")

    user_text = (
        f"这张照片来自一位初三学生，学生已选定的科目是【{subject}】。\n"
        f"请按系统要求，以 json 输出 kind / tag / analysis / clean_text 四个字段。"
        + JSON_HINT
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
            ],
        },
    ]

    last_err = "未知错误"
    for attempt in range(1, AI_MAX_RETRY + 1):
        try:
            # 第 1 遍走官方推荐的 json_object 模式；后面几遍去掉该模式，靠 prompt + 兜底解析
            kwargs: Dict[str, Any] = {}
            if attempt == 1:
                kwargs["response_format"] = {"type": "json_object"}

            resp = _client().chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=AI_TEMPERATURE,
                max_tokens=AI_MAX_TOKENS,
                timeout=AI_HTTP_TIMEOUT,      # 非流式调用没法中途打断，只能靠 socket 超时兜住
                **_think_kwargs(), **kwargs,
            )
            ch = resp.choices[0]
            data = extract_json(ch.message.content)
            if data:
                return {
                    "_status": "ok",
                    "kind": norm_kind(data.get("kind")),
                    "tag": str(data.get("tag", "")).strip(),
                    "analysis": str(data.get("analysis", "")).strip(),
                    "clean_text": str(data.get("clean_text", "")).strip(),
                }
            if ch.finish_reason == "length":
                # 被截断：多半是推理 token 吃光了预算。明确报出来，别伪装成「空内容」。
                rt = getattr(resp.usage.completion_tokens_details, "reasoning_tokens", "?")
                last_err = (f"推理占满 token 预算被截断（finish_reason=length，"
                            f"reasoning_tokens={rt}，上限 {AI_MAX_TOKENS}）")
            else:
                last_err = "模型返回内容为空或不是合法 json（官方已知偶发问题）"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1.2 * attempt)   # 退避后重试

    return _ai_fail(f"error:{last_err}")


# =============================================================================
# 四之二、文档上传：PDF / Word / 文本 -> 页 -> 逐页拆题 -> 一题一条
# =============================================================================
#
# 思路：把所有格式**先归一到「页」**，再走同一条路。
#   图片  -> 1 页（就是它自己）
#   PDF   -> N 页，逐页渲染成图（pypdfium2）
#   Word  -> 先用 LibreOffice 转成 PDF，再按 PDF 渲染（这样图形/表格/公式的排版都能保住，
#            只抽文字的话几何题的图就没了）
#   文本  -> 1 页，纯文字
# 拆题由 AI 按页做：一次调用返回这一页上的所有题目（含各自的纵向范围，用来裁图）。

DOC_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
DOC_PDF_EXT = {".pdf"}
DOC_WORD_EXT = {".doc", ".docx", ".rtf", ".odt"}
DOC_TEXT_EXT = {".txt", ".md", ".markdown", ".text"}

DOC_MAX_PAGES = 30                       # 一次最多处理多少页，防止有人传一本练习册
DOC_MAX_BYTES = 40 * 1024 * 1024         # 单个文件上限
DOC_RENDER_LONG_EDGE = 1600              # 页面渲染后的长边像素（和照片上传同一量级）
SPLIT_MAX_TOKENS = 16000                 # 拆题要一次吐出 N 道题，比单题费 token
SPLIT_MAX_PROBLEMS = 12                  # 单页最多拆几道
SOFFICE_TIMEOUT = 180                    # Word 转换超时（首次跑会慢，要建用户配置）



def doc_kind_of(filename: str) -> str:
    """按扩展名判断文件类型：image / pdf / word / text / unknown。"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in DOC_IMAGE_EXT:
        return "image"
    if ext in DOC_PDF_EXT:
        return "pdf"
    if ext in DOC_WORD_EXT:
        return "word"
    if ext in DOC_TEXT_EXT:
        return "text"
    return "unknown"


def decode_text(raw: bytes) -> str:
    """
    中文文本文件十有八九是 GB18030 而不是 UTF-8（尤其是 Windows 上老师发的 txt）。
    按「越严越好」的顺序试，utf-8 解不出来再退到 gb18030。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def pdf_to_page_images(pdf_bytes: bytes) -> List[bytes]:
    """把 PDF 逐页渲染成 JPEG 字节。返回的 list 长度就是页数。"""
    out: List[bytes] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        n = len(pdf)
        for i in range(min(n, DOC_MAX_PAGES)):
            page = pdf[i]
            w, h = page.get_size()
            scale = DOC_RENDER_LONG_EDGE / max(w, h, 1)
            pil = page.render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=92)
            out.append(buf.getvalue())
    finally:
        pdf.close()
    return out


def _soffice_convert(raw: bytes, ext: str, target: str, out_ext: str) -> bytes:
    """
    LibreOffice 无头转换。每次用独立的 UserInstallation 配置目录：
    共用一份配置时，并发转换会互相抢锁，报「source file could not be loaded」这类
    和真实原因八竿子打不着的错。
    """
    with tempfile.TemporaryDirectory(prefix="doc_") as td:
        src = os.path.join(td, "in" + ext)
        with open(src, "wb") as f:
            f.write(raw)
        prof = os.path.join(td, "profile")
        cmd = ["soffice", f"-env:UserInstallation=file://{prof}",
               "--headless", "--norestore", "--nolockcheck", "--nodefault",
               "--convert-to", out_ext, "--outdir", td, src]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=SOFFICE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"转换超时（>{SOFFICE_TIMEOUT} 秒）")
        target_path = os.path.join(td, target)
        if not os.path.exists(target_path):
            detail = (r.stderr or b"").decode("utf-8", "replace").strip()[-200:]
            raise RuntimeError(f"转换失败{('：' + detail) if detail else ''}")
        with open(target_path, "rb") as f:
            return f.read()


def doc_to_pages(raw: bytes, filename: str) -> List[Dict[str, Any]]:
    """
    把上传的文件拆成「页」。每页是：
      {"kind": "image", "raw": <图片字节>}   —— 走视觉识别
      {"kind": "text",  "text": <字符串>}    —— 纯文本，没有图
    """
    k = doc_kind_of(filename)
    if k == "image":
        return [{"kind": "image", "raw": raw}]
    if k == "pdf":
        return [{"kind": "image", "raw": b} for b in pdf_to_page_images(raw)]
    if k == "word":
        pdf_bytes = _soffice_convert(raw, os.path.splitext(filename)[1].lower(), "in.pdf", "pdf")
        return [{"kind": "image", "raw": b} for b in pdf_to_page_images(pdf_bytes)]
    if k == "text":
        return [{"kind": "text", "text": decode_text(raw)}]
    raise ValueError(
        "不支持这个格式。可以传：图片（jpg/png）、PDF、Word（doc/docx）、纯文本（txt/md）")


def _ruler_font(size: int):
    """尺子上的数字用中文字体渲染：默认位图字体太小、放大就糊，而且缺字时画不出来。"""
    for path in SYSTEM_CJK_FONTS + [BUNDLED_FONT]:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        get_cjk_font()
        if os.path.exists(BUNDLED_FONT):
            return ImageFont.truetype(BUNDLED_FONT, size)
    except Exception:
        pass
    return ImageFont.load_default()


def add_ruler(page_jpeg: bytes) -> bytes:
    """
    在页面左侧加一条带百分数刻度的尺子，再交给模型。

    为什么非加不可：直接让模型"估一个 0~1 的小数"来定位题目，实测**很不可靠** ——
    它会把第 2 题的范围给成"从第 1 题开始、到第 2 题选项中间结束"，
    裁出来的图既混进上一题、又把选项切掉半行。裁坏比不裁更糟。
    画上刻度之后，任务从"估一个抽象比例"变成"读出图上标的数字"，
    实测同一张图同一道题：真实范围 20.4%~24.6%，模型答 20%~25%，完全罩得住。

    尺子画在页面**左边另加的一条窄栏**里，页面本身一个像素都不改 ——
    这样纵向比例和原图完全一致，可以直接拿百分比去裁原图。
    """
    with Image.open(io.BytesIO(page_jpeg)) as im:
        src = im.convert("RGB")
        W, H = src.size
        gutter = 110
        canvas = Image.new("RGB", (W + gutter, H), "white")
        canvas.paste(src, (gutter, 0))
        d = ImageDraw.Draw(canvas)
        font = _ruler_font(max(14, H // 60))
        for pct in range(0, 101, 5):
            y = min(H - 1, int(H * pct / 100))
            major = (pct % 10 == 0)
            d.line([(gutter, y), (W + gutter, y)],
                   fill=(214, 40, 40) if major else (165, 200, 235),
                   width=3 if major else 1)
            if major:
                d.text((6, min(max(0, y - H // 120), H - H // 40)),
                       f"{pct}", font=font, fill=(200, 0, 0))
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


def _to_frac(v: Any) -> Optional[float]:
    """把模型给的定位值统一成 0~1。它可能答 0.2（比例）也可能答 20（百分数）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f > 1.0:
        f = f / 100.0
    return f if 0.0 <= f <= 1.0 else None


def crop_page(page_jpeg: bytes, top: Any, bottom: Any) -> bytes:
    """
    按 AI 给的纵向范围裁出这一道题。**裁坏了比不裁更糟**（孩子会看到一道残缺的题），
    所以这里只认「明显合理」的范围，其余一律退回整页。
    """
    t, b = _to_frac(top), _to_frac(bottom)
    if t is None or b is None:
        return page_jpeg
    top, bottom = t, b
    # 明显不合法 / 太窄：AI 没给出可信范围，用整页最保险
    if not (0.0 <= top < bottom <= 1.0) or (bottom - top) < 0.04:
        return page_jpeg
    if top <= 0.005 and bottom >= 0.995:
        return page_jpeg                    # 本来就是整页
    try:
        with Image.open(io.BytesIO(page_jpeg)) as im:
            im = im.convert("RGB")
            W, H = im.size
            # 上下游各留 2% 余量：刻度尺是 5% 一档，模型可能整体偏一格。
            # 多带一行邻居只是难看，切掉半行孩子就看不懂题了 —— 两害相权取其轻。
            pad = int(H * 0.02)
            y0 = max(0, int(H * top) - pad)
            y1 = min(H, int(H * bottom) + pad)
            if y1 - y0 < H * 0.04:
                return page_jpeg
            buf = io.BytesIO()
            im.crop((0, y0, W, y1)).save(buf, format="JPEG", quality=92)
            return buf.getvalue()
    except Exception:
        return page_jpeg


def call_split_page(page: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    把一页拆成若干道题。返回 [{"kind","tag","analysis","clean_text","top","bottom"}, ...]。
    失败抛异常，由调用方决定是跳过这一页还是整体报错。
    """
    if page["kind"] == "text":
        user_text = ("下面这一页的**文字**来自一个文本文件，请按系统要求拆成一道一道的题目。\n"
                     "因为是纯文字、没有页面图像，top/bottom 一律给 0 和 1。\n\n"
                     "——————\n" + page["text"][:12000])
        content: Any = user_text
    else:
        user_text = "下面这一页是一整页试卷，请按系统要求拆成一道一道的题目，输出 json。"
        # 开裁题时才发带尺子的那张（模型靠刻度定位），裁的时候用原图（比例一致，见 add_ruler）
        shot = add_ruler(page["raw"]) if DOC_AUTO_CROP else page["raw"]
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url",
             "image_url": {"url": bytes_to_data_uri(shot), "detail": "high"}},
        ]

    messages = [{"role": "system", "content": SPLIT_PROMPT},
                {"role": "user", "content": content}]

    last_err = "未知错误"
    for attempt in range(1, AI_MAX_RETRY + 1):
        try:
            kwargs: Dict[str, Any] = {}
            if attempt == 1:
                kwargs["response_format"] = {"type": "json_object"}
            resp = _client().chat.completions.create(
                model=MODEL_NAME, messages=messages,
                temperature=AI_TEMPERATURE, max_tokens=SPLIT_MAX_TOKENS,
                timeout=AI_HTTP_TIMEOUT, **_think_kwargs(), **kwargs,
            )
            ch = resp.choices[0]
            data = extract_json(ch.message.content)
            if data is not None:
                raw_list = data.get("problems")
                if raw_list is None:
                    raw_list = data if isinstance(data, list) else []
                out: List[Dict[str, Any]] = []
                for p in (raw_list or [])[:SPLIT_MAX_PROBLEMS]:
                    if not isinstance(p, dict):
                        continue
                    clean_text = str(p.get("clean_text", "")).strip()
                    if not clean_text:
                        continue          # 没有题干的一律丢掉，宁可少一条也不要空壳
                    out.append({
                        "kind": norm_kind(p.get("kind")),
                        "tag": str(p.get("tag", "")).strip(),
                        "analysis": str(p.get("analysis", "")).strip(),
                        "clean_text": clean_text,
                        "top": p.get("top", 0.0),
                        "bottom": p.get("bottom", 1.0),
                    })
                return out
            if ch.finish_reason == "length":
                last_err = (f"这一页题目太多，输出被 token 上限截断（上限 {SPLIT_MAX_TOKENS}）")
            else:
                last_err = "模型返回内容为空或不是合法 json"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1.2 * attempt)
    raise RuntimeError(last_err)


def _persist_item(*, raw_image: Optional[bytes], ai: Dict[str, Any],
                  subject: str, source: str) -> Tuple[Dict[str, Any], bool]:
    """
    落库一道题：占位行 -> 原图 -> 去红笔图 -> 回填 AI 字段。
    返回 (item, 去红笔是否真的成功)。

    raw_image 为 None 表示纯文本来源（没有原图），此时 orig/clean 都留空
    —— 前端本来就是 `v-if="orig_url"` 才渲染，缺图会自然降级，不会报错。
    """
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO mistakes (subject, tag, title, orig_path, clean_path, clean_text,"
            " variant_q, variant_a, ai_status, kind, analysis, source, created_at, review_due_at,"
            " user_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (subject, "", "", "", "", "", "", "", "processing",
             ai.get("kind", "mistake"), "", source,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             # 刚录入的题先放一天，明天进复习队列
             (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
             current_uid()),
        )
        new_id = int(cur.lastrowid)

    orig_rel, clean_rel = "", ""
    clean_ok = False
    if raw_image:
        orig_rel = f"origin/original_{new_id}.jpg"
        clean_rel = f"clean/clean_{new_id}.jpg"
        orig_abs = os.path.join(STATIC_DIR, orig_rel)
        clean_abs = os.path.join(STATIC_DIR, clean_rel)
        try:
            normalize_and_save(raw_image, orig_abs)
        except Exception:
            with closing(get_conn()) as conn, conn:
                conn.execute("DELETE FROM mistakes WHERE id = ?", (new_id,))
            raise
        # 去红笔失败就复用原图，保证「版本B」永远有东西可显示
        try:
            clean_ok, clean_msg = make_clean_image(orig_abs, clean_abs)
        except Exception as e:
            clean_ok, clean_msg = False, str(e)
        if not clean_ok:
            try:
                with open(orig_abs, "rb") as fs, open(clean_abs, "wb") as fd:
                    fd.write(fs.read())
            except Exception:
                clean_rel = ""

    with closing(get_conn()) as conn, conn:
        conn.execute(
            """UPDATE mistakes SET subject=?, tag=?, title=?, orig_path=?, clean_path=?,
               clean_text=?, analysis=?, kind=?, ai_status=? WHERE id=?""",
            (subject, ai["tag"], make_title(ai["clean_text"], ai["tag"], subject),
             orig_rel, clean_rel, ai["clean_text"], ai["analysis"], ai["kind"],
             ai["_status"], new_id),
        )
        row = conn.execute("SELECT * FROM mistakes WHERE id = ?", (new_id,)).fetchone()
    return row_to_item(row), clean_ok


def call_variant(it: Dict[str, Any]) -> Dict[str, str]:
    """
    按需生成「同考点变式题」。上传时**不再**生成——错题本的核心价值是弄明白错在哪，
    做新题是下一步的事，不该占着主界面、也不该每次都花这份 token。
    带上原图：图形类题目需要参考原题的图形风格。
    """
    ctx = [
        "【原题（纯净题干）】",
        it.get("clean_text") or "（题干缺失）",
        "",
        f"【考点】{it.get('tag') or '未标注'}",
    ]
    if it.get("analysis"):
        if norm_kind(it.get("kind")) == "classic":
            ctx += ["", "【这道题好在哪里 / 关键一步】", it["analysis"],
                    "", "变式题请保留这道题最妙的那个设计（条件隐蔽处、多知识点交汇处或陷阱），"
                        "数值情境全部换新。"]
        else:
            ctx += ["", "【学生在这道题上的错因】", it["analysis"],
                    "", "变式题请重点针对这个错因设计，让他重新面对同一个坑。"]
    question = "\n".join(ctx) + "\n\n请按系统要求以 json 输出 variant_question 与 variant_analysis。"

    orig_abs = os.path.join(STATIC_DIR, it.get("orig_path") or "")
    content: List[Dict[str, Any]] = [{"type": "text", "text": question}]
    if os.path.exists(orig_abs):
        try:
            content.append({"type": "image_url",
                            "image_url": {"url": image_to_data_uri(orig_abs), "detail": "high"}})
        except Exception:
            pass

    last_err = "未知错误"
    for attempt in range(1, 3):
        try:
            resp = _client().chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "system", "content": VARIANT_PROMPT},
                          {"role": "user", "content": content}],
                temperature=0.7,
                max_tokens=CHAT_MAX_TOKENS,
                timeout=AI_HTTP_TIMEOUT,
                response_format={"type": "json_object"} if attempt == 1 else None,
                **_think_kwargs(),
            )
            data = extract_json(resp.choices[0].message.content)
            if data and (data.get("variant_question") or data.get("variant_analysis")):
                return {"variant_question": str(data.get("variant_question", "")).strip(),
                        "variant_analysis": str(data.get("variant_analysis", "")).strip()}
            last_err = "模型没返回可用的变式题"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1.0 * attempt)
    raise RuntimeError(last_err)


def make_title(clean_text: str, tag: str, subject: str) -> str:
    """给卡片列表生成一句话摘要（去掉 Markdown/LaTeX 噪音）。"""
    src = clean_text or tag or subject
    src = re.sub(r"\$+", "", src)
    src = re.sub(r"[\\#*`>\[\]{}]", "", src)
    src = re.sub(r"\s+", " ", src).strip()
    return (src[:42] + "…") if len(src) > 42 else (src or f"{subject}错题")


# =============================================================================
# 五、PDF 导出（reportlab + A4）
# =============================================================================

_CJK_FONT: Optional[str] = None
_FONT_LOCK = threading.Lock()

FONT_DIR = os.path.join(BASE_DIR, "fonts")
BUNDLED_FONT = os.path.join(FONT_DIR, "NotoSansSC.ttf")
# Noto Sans SC（OFL 开源协议，允许自由嵌入 PDF）。用 glyf 轮廓的 TTF，
# reportlab 不支持 CFF/OTF 轮廓，所以这里特意选 TTF 而不是 OTF/woff2。
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"

# 系统中文字体候选（Linux / macOS / Windows 常见路径）
SYSTEM_CJK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]


def _try_register_font(path: str, name: str = "CJK") -> bool:
    try:
        pdfmetrics.registerFont(TTFont(name, path))
        return True
    except Exception:
        return False


def download_cjk_font(verbose: bool = True) -> bool:
    """下载 Noto Sans SC 到 ./fonts/ 并缓存（仅首次需要）。"""
    if os.path.exists(BUNDLED_FONT) and os.path.getsize(BUNDLED_FONT) > 100_000:
        return True
    try:
        os.makedirs(FONT_DIR, exist_ok=True)
        if verbose:
            print("  ⏬ 本机未找到中文字体，正在下载 Noto Sans SC（约 17MB，仅首次）…", flush=True)
        tmp = BUNDLED_FONT + ".part"
        req = urllib.request.Request(FONT_URL, headers={"User-Agent": "cuoti-app/1.0"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        if os.path.getsize(tmp) < 100_000:
            os.remove(tmp)
            return False
        os.replace(tmp, BUNDLED_FONT)
        if verbose:
            print(f"  ✅ 中文字体已就绪：{BUNDLED_FONT}", flush=True)
        return True
    except Exception as e:
        if verbose:
            print(f"  ⚠️  中文字体下载失败（{type(e).__name__}: {e}）", flush=True)
        return False


def get_cjk_font() -> str:
    """
    中文字体解析链（按优先级）：
      1) 系统中文字体（Linux/macOS/Windows 常见路径）
      2) 项目自带 ./fonts/NotoSansSC.ttf（上次下载缓存）
      3) 现下载 Noto Sans SC 并嵌入
      4) 最后兜底 reportlab 内置 CID 字体 STSong-Light

    ⚠️ 为什么不能只用 STSong-Light：它是 Adobe CID 字体，**不在 PDF 里嵌入字形**，
       依赖阅读器本地装有该字体。实测 pypdfium2 / Chrome 等常见阅读器都没有，
       中文会整片渲染成空白（页眉直接消失）。只有 1~3 全部失败时才退到它。
    """
    global _CJK_FONT
    if _CJK_FONT:
        return _CJK_FONT
    with _FONT_LOCK:
        if _CJK_FONT:
            return _CJK_FONT

        for p in SYSTEM_CJK_FONTS:                    # 1) 系统字体
            if os.path.exists(p) and _try_register_font(p):
                _CJK_FONT = "CJK"
                return _CJK_FONT

        if os.path.exists(BUNDLED_FONT) and _try_register_font(BUNDLED_FONT):   # 2) 缓存
            _CJK_FONT = "CJK"
            return _CJK_FONT

        if download_cjk_font() and _try_register_font(BUNDLED_FONT):            # 3) 现下
            _CJK_FONT = "CJK"
            return _CJK_FONT

        try:                                                                    # 4) 兜底
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            _CJK_FONT = "STSong-Light"
            print("  ⚠️  未能嵌入中文字体，已退到 STSong-Light："
                  "部分 PDF 阅读器可能显示空白中文。", flush=True)
        except Exception:
            _CJK_FONT = "Helvetica"
        return _CJK_FONT


_LATEX_MAP = {
    r"\times": "×", r"\div": "÷", r"\pm": "±", r"\mp": "∓", r"\cdot": "·",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
    r"\approx": "≈", r"\equiv": "≡", r"\infty": "∞", r"\angle": "∠",
    r"\triangle": "△", r"\parallel": "∥", r"\perp": "⊥", r"\circ": "°",
    r"\because": "∵", r"\therefore": "∴", r"\in": "∈", r"\notin": "∉",
    r"\subseteq": "⊆", r"\cup": "∪", r"\cap": "∩", r"\rightarrow": "→", r"\to": "→",
    r"\Rightarrow": "⇒", r"\leftarrow": "←", r"\Leftrightarrow": "⇔",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\theta": "θ",
    r"\lambda": "λ", r"\mu": "μ", r"\pi": "π", r"\sigma": "σ", r"\omega": "ω",
    r"\varphi": "φ", r"\rho": "ρ", r"\Delta": "Δ", r"\Omega": "Ω", r"\Phi": "Φ",
    r"\sum": "Σ", r"\int": "∫", r"\sqrt": "√", r"\quad": "  ", r"\,": " ", r"\;": " ",
}
_SUP = str.maketrans("0123456789+-()nxyabc", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾ⁿˣʸᵃᵇᶜ")
_SUB = str.maketrans("0123456789+-()nxyabc", "₀₁₂₃₄₅₆₇₈₉₊₋₍₎ₙₓᵧₐ♭꜀")


def latex_to_plain(text: str) -> str:
    """把 Markdown+LaTeX 降级成 reportlab 能画的纯文本（PDF 用；网页端走 KaTeX 真渲染）。"""
    if not text:
        return ""
    s = text
    s = re.sub(r"\$\$(.+?)\$\$", r"\1", s, flags=re.S)
    s = s.replace("$", "")
    s = re.sub(r"\\\[(.+?)\\\]", r"\1", s, flags=re.S)
    s = re.sub(r"\\\((.+?)\\\)", r"\1", s, flags=re.S)
    # \frac12 这类简写先补回花括号，否则会被下面的「残余命令删除」吃掉、只剩 "12"
    s = re.sub(r"\\(?:d|t)?frac\s*([0-9a-zA-Z])\s*([0-9a-zA-Z])",
               r"\\frac{\1}{\2}", s)
    # \frac{a}{b} -> (a)/(b)，嵌套两层
    for _ in range(2):
        s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
        s = re.sub(r"\\dfrac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", s)
    s = re.sub(r"\\text\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathbf\{([^{}]*)\}", r"\1", s)
    # ^{...} / _{...}
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: m.group(1).translate(_SUB), s)
    # ^2 / _1 单字符
    s = re.sub(r"\^(\S)", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"_(\S)", lambda m: m.group(1).translate(_SUB), s)
    for k, v in _LATEX_MAP.items():
        s = s.replace(k, v)
    s = re.sub(r"\\[a-zA-Z]+", "", s)          # 残余命令直接删
    s = s.replace("\\", "")
    # Markdown 降级
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.S)
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", s, flags=re.S)
    s = re.sub(r"`{1,3}(.+?)`{1,3}", r"\1", s, flags=re.S)
    s = re.sub(r"^\s{0,3}[-*+]\s+", "· ", s, flags=re.M)    # 无序列表 -> 圆点
    s = re.sub(r"^\s{0,3}(\d+)[.)]\s+", r"\1. ", s, flags=re.M)
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)
    s = re.sub(r"^\s*\|.*\|\s*$", "", s, flags=re.M)         # 表格行丢弃
    s = re.sub(r"^\s*[-:|\s]+$", "", s, flags=re.M)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _char_w(ch: str, font: str, size: float) -> float:
    try:
        return pdfmetrics.stringWidth(ch, font, size)
    except Exception:
        return size * 0.5


def wrap_paragraph(text: str, font: str, size: float, max_w: float) -> List[str]:
    """
    中英混排折行：中文逐字断行，英文/数字按单词整体挪行，避免把单词劈开。
    """
    lines: List[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur, cur_w = "", 0.0
        tokens, buf = [], ""
        for ch in para:
            if ch.isascii() and (ch.isalnum() or ch in "-_./'\"()[]"):
                buf += ch
            else:
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(ch)
        if buf:
            tokens.append(buf)
        for tk in tokens:
            w = _char_w(tk, font, size)
            if cur_w + w > max_w and cur:
                lines.append(cur)
                cur, cur_w = tk, w
            else:
                cur += tk
                cur_w += w
        if cur:
            lines.append(cur)
    return lines


def draw_wrapped(c: pdfcanvas.Canvas, text: str, x: float, y: float,
                 max_w: float, font: str, size: float, leading: float,
                 bottom: float = 60.0) -> float:
    """从 y 往下绘制折行文本，触到底部边界 bottom 就停。返回新的 y。"""
    c.setFont(font, size)
    for ln in wrap_paragraph(text, font, size, max_w):
        if y < bottom:
            break
        c.drawString(x, y, ln)
        y -= leading
    return y


class ExportReq(BaseModel):
    ids: List[int] = Field(default_factory=list)
    scope: str = "selected"          # selected=勾选的 / filter=当前筛选 / all=全部
    subject: Optional[str] = None    # scope=filter 时生效
    kind: Optional[str] = None       # scope=filter 时生效：只导出这一类（错题/经典题）
    content: str = "redo"            # redo=纯净重做题 / orig=原图复习 / clean=去红笔图 / both=原图+重做对照
    layout: str = "roomy"            # roomy=每题一页(留足作答空间) / compact=紧凑省纸
    with_variant: bool = False       # 是否把 AI 变式题也印上
    with_answer_page: bool = False   # 卷末附「原图答案页」（含当时的手写过程与老师批改）
    order: str = "time_asc"          # time_asc / time_desc / subject
    title: str = ""


def fetch_for_export(req: ExportReq) -> List[Dict[str, Any]]:
    """按导出范围与排序取数。"""
    order_sql = {
        "time_asc":  "ORDER BY created_at ASC,  id ASC",
        "time_desc": "ORDER BY created_at DESC, id DESC",
        "subject":   "ORDER BY subject, id",
    }.get(req.order, "ORDER BY created_at ASC, id ASC")

    # ⚠️ 归属过滤无条件加上，且**不参与 scope 分支**。
    # 之前这里没有它：构造一个 scope="selected" 带上别人的 id，
    # 就能把别人的错题导出成 PDF 拿走 —— 多用户下这是最直白的数据泄露。
    conds, args = ["user_id = ?"], [current_uid()]
    if req.scope == "filter":
        # 「当前筛选」= 左边列表正在显示的那些条件，所以要跟列表用同一套筛选，
        # 否则用户筛出「经典题」再点导出，印出来的却混着错题。
        if req.subject and req.subject != "全部":
            conds.append("subject = ?")
            args.append(req.subject)
        if req.kind in KINDS:
            conds.append("kind = ?")
            args.append(req.kind)
    elif req.scope == "selected":
        if not req.ids:
            return []
        conds.append(f"id IN ({','.join('?' * len(req.ids))})")
        args += list(req.ids)
    where = "WHERE " + " AND ".join(conds)

    with closing(get_conn()) as conn:
        rows = conn.execute(f"SELECT * FROM mistakes {where} {order_sql}", args).fetchall()
    return [row_to_item(r) for r in rows]


def build_pdf(items: List[Dict[str, Any]], *, content: str = "redo", layout: str = "roomy",
              with_variant: bool = False, with_answer_page: bool = False,
              title: str = "") -> bytes:
    """
    A4 错题文档。四种内容 × 两种版式：

      content = redo   重做卷：纯净题（优先 AI 文字版，无则用去红笔图）+ 作答区
      content = orig   复习卷：原图（含当时手写与红笔批改），用于看错因、查卡点
      content = clean  去红笔图：去掉批改的题面
      content = both   对照卷：上半页原图、下半页重做题

      layout = roomy    每题一页，底部留大片虚线作答区（推荐给重做）
      layout = compact  自动流式排版，多题一页，省纸（推荐给复习/打印多份）
    """
    font = get_cjk_font()
    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    M = 40.0
    content_w = page_w - 2 * M
    doc_title = (title or "").strip()
    page_no = [0]
    # 当前页「不许越过的下边界」：roomy 版式下要先给作答区留位置，
    # 所有绘制函数都读这个值，避免图片/文字压到作答区上。
    limit = {"bottom": M + 24}

    def abs_of(rel: str) -> str:
        p = os.path.join(STATIC_DIR, rel) if rel else ""
        return p if p and os.path.exists(p) else ""

    def header_footer():
        """每页顶部标题栏 + 底部页码"""
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.setFont(font, 8)
        if doc_title:
            c.drawString(M, page_h - M + 16, doc_title)
        page_no[0] += 1
        c.drawRightString(page_w - M, page_h - M + 16, f"第 {page_no[0]} 页")
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.6)
        c.line(M, page_h - M + 12, page_w - M, page_h - M + 12)

    def new_page():
        c.showPage()
        limit["bottom"] = M + 24
        c.setFillColorRGB(0, 0, 0)
        header_footer()
        return page_h - M

    def subject_bar(y: float, subj: str) -> float:
        """科目分节条（导出全部/按科目时用来分节）"""
        c.setFillColorRGB(0.31, 0.27, 0.90)
        c.rect(M, y - 20, content_w, 22, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont(font, 12)
        c.drawString(M + 8, y - 14, f"{subj}　错题专练")
        c.setFillColorRGB(0, 0, 0)
        return y - 34

    def q_head(y: float, idx: int, it: Dict[str, Any], compact: bool) -> float:
        c.setFillColorRGB(0.10, 0.12, 0.16)
        c.setFont(font, 12 if not compact else 11)
        tag = f"　{it.get('tag')}" if it.get("tag") else ""
        c.drawString(M, y, f"{idx}. 【{it['subject']}】{tag}")
        c.setFont(font, 7.5)
        c.setFillColorRGB(0.55, 0.55, 0.55)
        c.drawRightString(page_w - M, y + 1, str(it.get("created_at", ""))[:10])
        c.setFillColorRGB(0, 0, 0)
        c.setStrokeColorRGB(0.86, 0.86, 0.86)
        c.setLineWidth(0.6)
        c.line(M, y - 6, page_w - M, y - 6)
        return y - 14

    def draw_image(rel: str, y: float, max_h: float, label: str = "") -> Tuple[float, bool]:
        p = abs_of(rel)
        if not p:
            return y, False
        try:
            iw, ih = Image.open(p).size
        except Exception:
            return y, False
        avail = max(50.0, y - limit["bottom"])
        scale = min(content_w / iw, min(max_h, avail) / ih)
        dw, dh = iw * scale, ih * scale
        if label:
            c.setFont(font, 8.5)
            c.setFillColorRGB(0.45, 0.45, 0.45)
            c.drawString(M, y - 2, label)
            y -= 12
            scale = min(content_w / iw, min(max_h, max(50.0, y - limit["bottom"])) / ih)
            dw, dh = iw * scale, ih * scale
        c.drawImage(ImageReader(p), M + (content_w - dw) / 2, y - dh, dw, dh,
                    preserveAspectRatio=True, anchor="n", mask="auto")
        c.setFillColorRGB(0, 0, 0)
        return y - dh - 10, True

    def draw_variant(y: float, it: Dict[str, Any]) -> float:
        if not (with_variant and (it.get("variant_q") or it.get("variant_a"))):
            return y
        y -= 6
        c.setStrokeColorRGB(0.80, 0.80, 0.80)
        c.setDash(3, 3); c.setLineWidth(0.6)
        c.line(M, y + 4, page_w - M, y + 4)
        c.setDash()
        c.setFont(font, 9.5)
        c.setFillColorRGB(0.24, 0.16, 0.60)
        c.drawString(M, y - 6, "【同考点变式练习】")
        c.setFillColorRGB(0, 0, 0)
        y -= 20
        b = limit["bottom"]
        if it.get("variant_q"):
            y = draw_wrapped(c, latex_to_plain(it["variant_q"]), M, y, content_w, font, 10, 15, b)
        if it.get("variant_a"):
            y -= 4
            y = draw_wrapped(c, "【变式题解析】", M, y, content_w, font, 9, 14, b)
            y = draw_wrapped(c, latex_to_plain(it["variant_a"]), M, y, content_w, font, 8.8, 13.5, b)
        return y - 6

    def draw_answer_area(y: float, to: float, label: str = "作答区（请独立重做，不看原答案）"):
        c.setStrokeColorRGB(0.72, 0.72, 0.72)
        c.setDash(3, 3); c.setLineWidth(0.7)
        c.line(M, y, page_w - M, y)
        c.setDash()
        c.setFont(font, 9)
        c.setFillColorRGB(0.55, 0.55, 0.55)
        c.drawString(M, y - 13, label)
        c.setStrokeColorRGB(0.87, 0.87, 0.87)
        c.setDash(2, 4)
        gy = y - 32
        while gy > to:
            c.line(M, gy, page_w - M, gy)
            gy -= 26
        c.setDash()
        c.setFillColorRGB(0, 0, 0)

    def draw_item(y: float, idx: int, it: Dict[str, Any], compact: bool) -> float:
        """画一道题，返回新的 y。调用方保证 y 足够。"""
        y = q_head(y, idx, it, compact)
        b = limit["bottom"]
        redo_text = latex_to_plain(it.get("clean_text") or "")
        clean_img = it.get("clean_path") or ""
        orig_img = it.get("orig_path") or ""

        max_img_h = (page_h - 2 * M) * (0.42 if compact else 0.55)

        if content == "orig":
            y, ok = draw_image(orig_img, y, max_img_h)
            if not ok:
                y = draw_wrapped(c, redo_text or "（本题无原图）", M, y, content_w, font, 10.5, 17, b)
        elif content == "clean":
            y, ok = draw_image(clean_img, y, max_img_h)
            if not ok:
                y = draw_wrapped(c, redo_text or "（本题无去红笔图）", M, y, content_w, font, 10.5, 17, b)
        elif content == "redo":
            # 重做优先用 AI 纯净文字版（手写红笔全没了，最适合重做）；
            # 没有文字版才退回「去红笔图」；图形类题目退回图更稳妥。
            drew = False
            if redo_text:
                y = draw_wrapped(c, redo_text, M, y, content_w, font, 11.5, 19, b)
                drew = True
            if not drew:
                y, drew = draw_image(clean_img or orig_img, y, max_img_h)
            if not drew:
                c.setFont(font, 10); c.setFillColorRGB(0.6, 0.2, 0.2)
                c.drawString(M, y, "（本题缺少可用内容，请在网页端检查）")
                c.setFillColorRGB(0, 0, 0)
                y -= 18
        else:  # both 对照：上原图、下重做题
            y, _ = draw_image(orig_img, y, max_img_h * 0.6, label="版本A · 原图（看错因）")
            y -= 4
            c.setStrokeColorRGB(0.85, 0.85, 0.85); c.setDash(2, 3)
            c.line(M, y + 4, page_w - M, y + 4); c.setDash()
            c.setFont(font, 8.5); c.setFillColorRGB(0.45, 0.45, 0.45)
            c.drawString(M, y - 6, "版本B · 重做题")
            c.setFillColorRGB(0, 0, 0)
            y -= 20
            if redo_text:
                y = draw_wrapped(c, redo_text, M, y, content_w, font, 11, 18, b)

        y = draw_variant(y, it)
        return y

    # ================= 主循环 =================
    c.setFillColorRGB(0, 0, 0)
    header_footer()
    y = page_h - M
    idx = 0
    last_subject = None
    compact = (layout == "compact")

    for it in items:
        idx += 1
        need_subject_bar = (it["subject"] != last_subject)
        redo_text = latex_to_plain(it.get("clean_text") or "")

        if compact:
            # 先估算本题高度，放不下就翻页（图片类题目按经验取一个保守值）
            est = (46 + (len(redo_text) // 42) * 19) if redo_text else 175
            est += 175 if with_variant else 0
            est += (100 if content == "redo" else 12)
            if need_subject_bar:
                est += 34
            if y - est < M + 24:
                y = new_page()
                need_subject_bar = True
            if need_subject_bar:
                y = subject_bar(y, it["subject"])
                last_subject = it["subject"]
            y = draw_item(y, idx, it, compact=True)
            if content == "redo":
                draw_answer_area(y - 6, max(M + 10, y - 96), "作答区")
                y -= 112
            else:
                y -= 22
        else:
            # roomy：每题独占一页（第一题沿用当前页）
            if idx > 1:
                y = new_page()
                need_subject_bar = True
            if need_subject_bar:
                y = subject_bar(y, it["subject"])
                last_subject = it["subject"]
            # 底部先扣掉作答区高度，题目内容不许压过去
            limit["bottom"] = M + (150.0 if content == "redo" else 40.0)
            y = draw_item(y, idx, it, compact=False)
            if content == "redo":
                draw_answer_area(limit["bottom"], M)

    # ---------- 卷末答案页：附原图（含当时手写与老师批改） ----------
    if with_answer_page and content == "redo":
        for it in items:
            y = new_page()
            c.setFont(font, 14)
            c.drawString(M, y - 4, f"答案与错因 · 【{it['subject']}】{it.get('tag') or ''}")
            y -= 26
            y, ok = draw_image(it.get("orig_path") or "", y, (page_h - 2 * M) * 0.72,
                               label="原图（含当时的手写过程与老师红笔批改）")
            if not ok:
                c.setFont(font, 10)
                c.drawString(M, y, "（本题无原图）")

    c.save()
    return buf.getvalue()


# =============================================================================
# 六、访问认证（公网部署必读）
# =============================================================================
# 威胁模型：这台机器是公网 ECS，全网扫描器几分钟内就会扫到 8000 端口。
# 没有认证 = 孩子的错题照片、全部数据、以及你的 DeepSeek 额度对全世界开放。
# 这里的防护：口令登录（PBKDF2 慢哈希 + 限流）+ HMAC 签名会话 Cookie，
# 并且 **/static 也一并拦截**（照片 URL 泄露 = 数据泄露，只保护 API 是不够的）。
#
# ⚠️ 已知局限：当前跑在明文 HTTP 上，口令在传输途中未加密。
#    这能挡住扫描器/爬虫/陌生人（主要威胁），但挡不住同网络路径上的被动嗅探。
#    要彻底解决需要 HTTPS（域名 + Caddy 自动证书），见 README。

SESSION_COOKIE = "cuoti_session"
SESSION_TTL = 14 * 24 * 3600          # 14 天免登录；手机端不用天天输口令
SECRET_FILE = os.path.join(BASE_DIR, ".session_secret")
PASSWORD_FILE = os.path.join(BASE_DIR, "password.txt")
# ── 邮件 ────────────────────────────────────────────────────────────────────
# 用标准库 smtplib，不引第三方依赖。配置全部走环境变量，**不要写进代码**：
#   SMTP_HOST      smtp.qq.com
#   SMTP_PORT      465            （465 = SSL，587 = STARTTLS，按端口自动选）
#   SMTP_USER      你的邮箱账号
#   SMTP_PASS      授权码（不是登录密码）
#   SMTP_FROM      发件人，留空则用 SMTP_USER
# 阿里云默认封 25 端口，所以走 465/587；实测这两个都能出网。
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER
MAIL_READY = bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

VERIFY_TTL_MIN = 10           # 验证码有效期
VERIFY_MAX_TRY = 5            # 一个验证码最多试几次
VERIFY_RESEND_SEC = 60        # 同一邮箱多久才能重发一次（防轰炸）

# 邀请码：留空 = 开放注册。设成任意字符串后，注册必须填对才放行。
# 这个应用在公网 IP 上、DeepSeek key 是计费的 —— 万一被扫描器盯上批量注册，
# 想收紧时把下面这行改成 SIGNUP_CODE = os.getenv("SIGNUP_CODE", "你的邀请码") 重启即可。
SIGNUP_CODE = os.getenv("SIGNUP_CODE", "").strip()
LOGIN_MAX_FAIL = 8                    # 单 IP 窗口内允许的失败次数
LOGIN_WINDOW = 600                    # 限流窗口（秒）
PBKDF2_ROUNDS = 120_000               # 每次校验约 50ms，本身就是一道暴力破解门槛

_secret: bytes = b""
_login_fails: Dict[str, list] = {}
_login_lock = threading.Lock()


def _load_secret() -> bytes:
    """会话签名密钥，落盘复用（否则每次重启所有人都被登出）。"""
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            data = f.read().strip()
        if len(data) >= 32:
            return data
    data = secrets.token_bytes(32)
    with open(SECRET_FILE, "wb") as f:
        f.write(data)
    os.chmod(SECRET_FILE, 0o600)
    return data


def hash_password(pw: str, salt: Optional[bytes] = None) -> str:
    """
    口令哈希：pbkdf2_sha256 + **每用户独立随机盐**。

    为什么不能用全局盐（改之前就是）：相同口令会得到相同哈希，彩虹表一次命中一片；
    而且换会话密钥等于所有人密码同时失效。盐必须跟用户走、存在用户行里。
    """
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    """校验口令。格式不对、字段缺失一律当作失败，绝不抛异常。"""
    try:
        algo, rounds_s, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ── 会话 ────────────────────────────────────────────────────────────────────
# 令牌形如 {uid}.{过期时间戳}.{签名}，签名覆盖 uid 和过期时间。
# ⚠️ uid 必须在签名**里面**：只签过期时间的话，谁都能把 uid 改成别人的
#    从而直接读到对方的错题和照片。
_uid: ContextVar[int] = ContextVar("uid", default=0)


def make_token(uid: int) -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    payload = f"{uid}.{exp}"
    sig = hmac.new(_secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(tok: Optional[str]) -> Optional[int]:
    """校验通过返回 user_id，否则返回 None。"""
    if not tok:
        return None
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    uid_s, exp_s, sig = parts
    try:
        if int(exp_s) < time.time():
            return None
        uid = int(uid_s)
    except ValueError:
        return None
    want = hmac.new(_secret, f"{uid_s}.{exp_s}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return None
    return uid


def current_uid() -> int:
    """
    当前请求的 user_id。由 auth_guard 中间件写入，同步/异步 endpoint 都读得到
    （实测 ContextVar 在 FastAPI 的线程池里能正确传递）。

    ⚠️ 所有涉及数据的查询都必须用它做过滤。漏掉一处 = 那个接口能读到别人的数据。
    """
    return _uid.get()


def _code_hash(username: str, code: str) -> str:
    """
    验证码只存哈希，不存明文。

    说实话：6 位数字只有 100 万种可能，拿到库的人跑一遍是瞬间的事 ——
    哈希在这里挡不住有备而来的攻击者。它挡的是「顺手看到明文」，
    真正的防线是下面的**尝试次数上限**和**有效期**。
    """
    return hmac.new(_secret, f"verify:{username}:{code}".encode(), hashlib.sha256).hexdigest()


def send_mail(to: str, subject: str, body: str) -> Tuple[bool, str]:
    """
    发一封纯文本邮件。返回 (成功与否, 说明)。

    ⚠️ 这是在同步 endpoint 里调用的阻塞操作（SMTP 握手可能好几秒）。
    FastAPI 会把同步 endpoint 丢进线程池，所以不会卡住整个服务 ——
    但也不要在异步 endpoint 里直接调它。
    """
    if not MAIL_READY:
        return False, "服务器未配置邮件服务"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    try:
        if SMTP_PORT == 465:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
            srv.ehlo()
            if srv.has_extn("starttls"):
                srv.starttls()
                srv.ehlo()
        with srv:
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_FROM, [to], msg.as_string())
        return True, "已发送"
    except smtplib.SMTPAuthenticationError:
        # 最常见的一种：填的是登录密码而不是「授权码」
        return False, "邮件服务认证失败（注意要用授权码，不是邮箱登录密码）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def mail_throttle(key: str) -> int:
    """
    返回还要等几秒才能重发；0 表示可以发。可以发时会**立刻记账**，
    避免并发请求同时通过检查。
    """
    now = datetime.now()
    with closing(get_conn()) as conn, conn:
        row = conn.execute(
            "SELECT sent_at FROM mail_log WHERE key=? ORDER BY sent_at DESC LIMIT 1",
            (key,)).fetchone()
        if row:
            try:
                gap = (now - datetime.strptime(row["sent_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
                if gap < VERIFY_RESEND_SEC:
                    return int(VERIFY_RESEND_SEC - gap) + 1
            except ValueError:
                pass
        conn.execute("INSERT INTO mail_log (key, sent_at) VALUES (?,?)",
                     (key, now.strftime("%Y-%m-%d %H:%M:%S")))
    return 0


def mask_email(addr: str) -> str:
    """a@b.com → a***@b.com。回给前端只用于提示「发到哪个邮箱了」。"""
    try:
        name, domain = addr.split("@", 1)
        keep = name[:1] if len(name) > 1 else ""
        return f"{keep}***@{domain}"
    except ValueError:
        return "***"


def _client_ip(request) -> str:
    return request.client.host if request.client else "unknown"


def _login_locked(ip: str) -> bool:
    with _login_lock:
        hits = [t for t in _login_fails.get(ip, []) if time.time() - t < LOGIN_WINDOW]
        _login_fails[ip] = hits
        return len(hits) >= LOGIN_MAX_FAIL


def _record_fail(ip: str) -> None:
    with _login_lock:
        _login_fails.setdefault(ip, []).append(time.time())


def _clear_fails(ip: str) -> None:
    with _login_lock:
        _login_fails.pop(ip, None)


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#4f46e5">
<title>登录 · 我的AI学习助手</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:20px;
       font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
  .card{background:#fff;border-radius:18px;box-shadow:0 18px 50px rgba(0,0,0,.28);
        width:100%;max-width:380px;padding:32px 26px}
  .logo{text-align:center;line-height:0}
  .logo svg{width:60px;height:60px}
  h1{font-size:19px;text-align:center;margin:14px 0 4px;color:#1e293b}
  p.sub{text-align:center;color:#94a3b8;font-size:12px;margin:0 0 22px}
  label{display:block;font-size:13px;color:#475569;font-weight:600;margin-bottom:6px}
  input{width:100%;padding:13px 14px;font-size:16px;border:1.5px solid #e2e8f0;border-radius:11px;
        outline:none;transition:.15s;background:#f8fafc}
  input:focus{border-color:#4f46e5;background:#fff;box-shadow:0 0 0 3px rgba(79,70,229,.12)}
  .field{margin-bottom:14px}
  button{width:100%;margin-top:6px;padding:14px;font-size:16px;font-weight:700;color:#fff;
         background:#4f46e5;border:0;border-radius:11px;cursor:pointer;transition:.15s}
  button:hover{background:#4338ca} button:active{transform:scale(.985)}
  button:disabled{opacity:.6;cursor:not-allowed}
  .err{margin-top:14px;padding:11px;border-radius:10px;background:#fee2e2;color:#b91c1c;
       font-size:13px;text-align:center;display:none}
  .tip{margin-top:20px;font-size:11.5px;color:#94a3b8;line-height:1.7;text-align:center}
  /* 登录 / 注册切换。做成页内切换而不是两个页面：注册完直接就是登录态，
     来回跳页面反而多一次输入。 */
  .tabs{display:flex;gap:6px;background:#f1f5f9;border-radius:11px;padding:4px;margin-bottom:20px}
  .tabs button{margin:0;padding:9px;font-size:14px;background:transparent;color:#64748b;
               border-radius:8px;font-weight:600}
  .tabs button.on{background:#fff;color:#4f46e5;box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .tabs button:hover{background:transparent}
  .tabs button.on:hover{background:#fff}
</style></head><body>
<form class="card" id="f">
  <div class="logo"><!-- 与 favicon / 顶栏 #i-book 同一枚图标 -->
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#4f46e5"/><stop offset="1" stop-color="#7c3aed"/>
      </linearGradient></defs>
      <rect width="32" height="32" rx="7.2" fill="url(#lg)"/>
      <g fill="none" stroke="#ffffff" stroke-width="1.95"
         stroke-linecap="round" stroke-linejoin="round"
         transform="translate(2.6667 2.6667) scale(1.1111)">
        <path d="M4 19V5.5A2.5 2.5 0 0 1 6.5 3H19a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-2H20"/>
        <path d="M8 7h7"/>
      </g>
    </svg>
  </div>
  <h1>我的AI学习助手</h1>
  <p class="sub" id="sub">登录后看到的是你自己的错题</p>

  <div class="tabs">
    <button type="button" id="tab-login" class="on">登录</button>
    <button type="button" id="tab-reg" >注册</button>
  </div>
  <p id="reset-link" style="text-align:right;margin:-8px 0 14px">
    <a href="#" id="forgot" style="font-size:12px;color:#6366f1;text-decoration:none">忘记密码？</a>
  </p>

  <div class="field">
    <label for="u">用户名</label>
    <input id="u" type="text" autocomplete="username" autofocus
           inputmode="latin" autocapitalize="off" autocorrect="off" spellcheck="false"
           placeholder="3~20 位字母、数字、_ 或 -">
  </div>
  <div class="field">
    <label for="p">密码</label>
    <input id="p" type="password" autocomplete="current-password"
           inputmode="latin" autocapitalize="off" autocorrect="off" placeholder="请输入密码">
  </div>
  <div class="field" id="email-field" style="display:none">
    <label for="em">邮箱</label>
    <input id="em" type="email" autocomplete="email" inputmode="email"
           autocapitalize="off" autocorrect="off" placeholder="用于接收验证码和找回密码">
  </div>
  <div class="field" id="code-field" style="display:none">
    <label for="cd">验证码</label>
    <input id="cd" type="text" inputmode="numeric" autocomplete="one-time-code"
           maxlength="6" placeholder="6 位数字" style="letter-spacing:.4em;font-size:20px;text-align:center">
    <p id="code-hint" style="margin:8px 0 0;font-size:11.5px;color:#94a3b8;line-height:1.6"></p>
    <!-- 重发的出口。错误提示写着「请重新获取」，就必须真的给一个能点的地方 ——
         之前只有提示没有出口，用户只能点回「注册」把密码和邮箱重填一遍。
         带倒计时是因为服务端有 60 秒节流：不给倒计时，用户点了会撞 429，又一个死胡同。 -->
    <p style="margin:10px 0 0;text-align:center">
      <button type="button" id="resend"
              style="width:auto;margin:0;padding:6px 14px;font-size:12.5px;font-weight:600;
                     background:#eef0fe;color:#4f46e5;border:0;border-radius:8px">
        重新发送验证码
      </button>
    </p>
  </div>
  <div class="field" id="invite-field" style="display:none">
    <label for="iv">邀请码</label>
    <input id="iv" type="text" inputmode="latin" autocapitalize="off" autocorrect="off"
           placeholder="管理员提供的邀请码">
  </div>

  <button type="submit" id="b">进入</button>
  <div class="err" id="e"></div>
  <div class="tip" id="tip">
    每个账号只看到自己的错题、照片和对话。<br>还没有账号？点上面的「注册」。
  </div>
</form>
<script>
  var f=document.getElementById('f'),u=document.getElementById('u'),p=document.getElementById('p'),
      iv=document.getElementById('iv'),em=document.getElementById('em'),cd=document.getElementById('cd'),
      codeHint=document.getElementById('code-hint'),codeField=document.getElementById('code-field'),
      emailField=document.getElementById('email-field'),
      b=document.getElementById('b'),e=document.getElementById('e'),
      tip=document.getElementById('tip'),sub=document.getElementById('sub'),
      inviteField=document.getElementById('invite-field'),resetLink=document.getElementById('reset-link'),
      tabLogin=document.getElementById('tab-login'),tabReg=document.getElementById('tab-reg'),
      mode='login';
  // 邮件服务是否可用。**初始按可用算**，拿到服务端答复才降级 ——
  // 反过来（默认不可用）会让接口偶发失败时把注册永久锁死。
  var mailReady = true;
  var pending = {username:'', email:''};   // 跨步骤要用的中间状态
  var resendTimer = null;

  function show(el, on){ el.style.display = on ? '' : 'none'; }
  function err(msg){ e.textContent=msg; e.style.display='block'; }

  /* 按钮文字的**唯一来源**。之前提交处理器会在开头记下当时的文字、结尾再写回去，
     结果把 setMode('verify') 刚设好的「验证并进入」覆盖成了「发送验证码」——
     跟标签页那个 bug 同一个病根：同一处 DOM 由两个地方写。 */
  function btnText(m){
    return {login:'进入', register:'发送验证码', verify:'验证并进入',
            reset:'发送验证码', reset2:'重置并进入'}[m] || '确定';
  }

  /* 唯一的渲染入口：四种（五种）状态全在这里决定显隐和文案。
     之前还有个 setMode 包装函数和第二个 submit 处理器，那两处 hack 正是
     「同一处 DOM 两处写」的来源，已经并进来了。 */
  function setMode(m){
    mode = m;
    var isReg   = (m === 'register');
    var isVerify= (m === 'verify');
    var isReset = (m === 'reset');
    var isReset2= (m === 'reset2');
    var inRegFlow = isReg || isVerify;

    tabLogin.className = inRegFlow ? '' : 'on';
    tabReg.className   = inRegFlow ? 'on' : '';
    // ⚠️ 这两个标签必须**始终可点**（除非服务端根本不支持注册）。
    // 我上一版在 reset 模式下把两个都禁用了 —— 用户点了「忘记密码」之后
    // 就再也切不回登录，被困在一张重填密码的表单里。标签是出口，出口不能锁。
    tabLogin.disabled = false;
    tabReg.disabled   = !mailReady;

    // 字段显隐：**只留这一步真正用得上的**
    // 「重置第一步」只要邮箱。之前把用户名和密码也留着，用户点完「忘记密码」
    // 看到的还是一张登录表单，自然会觉得「点了没反应」。
    show(u.closest('.field'),  !isReset && !isReset2);   // 重置流程里用户名无关
    show(p.closest('.field'),  !isReset);                // 第一步不要密码；第二步当「新密码」
    show(emailField,           isReg || isReset || isReset2);
    show(codeField,            isVerify || isReset2);
    show(inviteField,          isReg && inviteField.dataset.needed==='1');
    show(resetLink,            m === 'login' && mailReady);

    // 第二步的「密码」其实要填的是新密码，标签得跟着改
    document.querySelector('label[for="p"]').textContent =
      isReset2 ? '新密码（至少 6 位）' : '密码';
    p.setAttribute('autocomplete', (isReg || isReset2) ? 'new-password' : 'current-password');
    if (isReset2) p.value = '';

    u.disabled = isVerify || isReset || isReset2;        // 这几个阶段用户名/邮箱已经定了
    em.disabled = isReset2;

    b.textContent = btnText(m);
    sub.textContent = {
      login:   '登录后看到的是你自己的错题',
      register:'注册需要邮箱验证，验证通过才算建成',
      verify:  '验证码已发到你的邮箱',
      reset:   '输入注册时的邮箱，我们把验证码发过去',
      reset2:  '输入邮件里的验证码，设置新密码'
    }[m];
    // 邮件没配好时，说明必须在**每一次**渲染里都带上 —— 之前在 fetch 回调里
    // 直接改 tip.innerHTML，用户一点「登录」就被这里覆盖掉，于是
    // 「为什么注册点不动」变得没有任何解释。
    tip.innerHTML = !mailReady
      ? '服务器还没有配置邮件服务，暂时无法注册或找回密码。<br>已有账号仍可正常登录。'
      : {
        login:   '每个账号只看到自己的错题、照片和对话。<br>还没有账号？点上面的「注册」。',
        register:'用户名注册后不能改。<br>密码至少 6 位。',
        verify:  '收不到？看看垃圾邮件，或点下面的按钮重新发送。',
        reset:   '这个邮箱有没有注册过，我们都会给出同样的提示。',
        reset2:  '重置成功后会自动登录，其它设备的登录状态不受影响。'
      }[m];
    e.style.display='none';
  }

  tabLogin.onclick = function(){ setMode('login'); };
  tabReg.onclick   = function(){ setMode('register'); };
  document.getElementById('forgot').onclick = function(ev){
    ev.preventDefault(); setMode('reset'); em.focus();
  };

  /* 重发倒计时。服务端 VERIFY_RESEND_SEC 秒内不让重发，
     这里把它显示出来，否则用户点一次吃一个 429。 */
  function startResendCountdown(sec){
    var el = document.getElementById('resend');
    if(resendTimer) clearInterval(resendTimer);
    var left = sec;
    function tick(){
      if(left <= 0){
        clearInterval(resendTimer); resendTimer = null;
        el.disabled = false; el.textContent = '重新发送验证码';
        return;
      }
      el.disabled = true;
      el.textContent = '重新发送（' + left + 's）';
      left--;
    }
    tick();
    resendTimer = setInterval(tick, 1000);
  }

  document.getElementById('resend').onclick = async function(){
    var el = this;
    if(el.disabled) return;
    el.disabled = true; el.textContent = '发送中…';
    try{
      var reg = (mode === 'verify');
      var url = reg ? '/api/register/resend' : '/api/reset/request';
      var body = reg ? {username: pending.username} : {email: pending.email};
      var r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify(body)});
      var d = null; try{ d = await r.json(); } catch(_){}
      if(r.ok){
        codeHint.textContent = reg
          ? ('新验证码已发送到 ' + (d.email || '') + '，' + d.ttl_min + ' 分钟内有效。')
          : ((d && d.msg) || '如果这个邮箱注册过，验证码已经发出去了');
        cd.value = ''; cd.focus();
        e.style.display = 'none';
        startResendCountdown((d && d.resend_after) || 60);
      } else {
        err((d && d.detail) || ('重发失败（HTTP ' + r.status + '）'));
        // 被节流时按剩余时间起倒计时，而不是让按钮一直可点一直失败
        var m = ((d && d.detail) || '').match(/(\\d+)\\s*秒/);
        startResendCountdown(m ? Math.min(+m[1], 60) : 5);
      }
    }catch(ex){
      err('网络错误：' + ex.message);
      el.disabled = false; el.textContent = '重新发送验证码';
    }
  };

  fetch('/api/signup_policy').then(function(r){ return r.json(); }).then(function(d){
    if(d && d.invite_required){ inviteField.dataset.needed='1'; }
    fetch('/api/mail_policy').then(function(r2){ return r2.json(); }).then(function(d2){
      // 只有在**明确得知**「邮件没配好」时才禁用注册。
      // 写成 `if(!d2.mail_ready)` 是错的：请求失败、接口 404、返回体不是预期结构，
      // 都会让 mail_ready 是 undefined，于是把注册按钮禁掉 ——
      // 而禁用的按钮连 onclick 都不触发，用户看到的是「点了没反应」，极难排查。
      if(d2 && d2.mail_ready === false){
        mailReady = false;
        setMode(mode);   // 只改状态，渲染全交给 setMode —— 回调里一个字都不碰 DOM
      }
    }).catch(function(){});
  }).catch(function(){});

  /* 一个处理器管五种状态：按 mode 选接口和请求体。
     之前这里和另一个捕获阶段的处理器并存，两个都在改同样的 DOM —— 已经合并。 */
  f.addEventListener('submit', async function(ev){
    ev.preventDefault(); e.style.display='none'; b.disabled=true;
    var savedMode = mode;
    b.textContent = '处理中…';
    try{
      var url, body;
      if(mode==='login'){
        url='/api/login'; body={username:u.value.trim(), password:p.value};
      } else if(mode==='register'){
        url='/api/register';
        body={username:u.value.trim(), password:p.value, email:em.value.trim(), invite:iv.value.trim()};
      } else if(mode==='verify'){
        url='/api/register/verify'; body={username:pending.username, code:cd.value.trim()};
      } else if(mode==='reset'){
        url='/api/reset/request'; body={email:em.value.trim()};
      } else {
        url='/api/reset/do';
        body={email:pending.email, code:cd.value.trim(), new:p.value};
      }
      var r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                body:JSON.stringify(body)});
      var d = null; try{ d = await r.json(); } catch(_){}
      if(r.ok){
        if(mode==='register'){
          pending.username = u.value.trim().toLowerCase();
          setMode('verify');
          codeHint.textContent = '验证码已发送到 ' + (d.email || em.value) + '，'
                               + d.ttl_min + ' 分钟内有效。';
          cd.value=''; cd.focus();
          startResendCountdown(d.resend_after || 60);
        } else if(mode==='reset'){
          pending.email = em.value.trim();
          codeHint.textContent = (d && d.msg) || '如果这个邮箱注册过，验证码已经发出去了';
          setMode('reset2');
          cd.value=''; cd.focus();
          startResendCountdown((d && d.resend_after) || 60);
        } else {
          location.href='/'; return;      // 登录 / 验码 / 重置成功
        }
      } else {
        err((d && d.detail) || ('失败（HTTP ' + r.status + '）'));
      }
    }catch(ex){ err('网络错误：'+ex.message); }
    // 成功且切换了模式时，文字由 setMode 管，这里别碰 —— 否则又把它覆盖回去。
    // 失败或原地不动时，按**当前** mode 重算（不是开头那个快照，它可能已经过期）。
    b.disabled = false;
    if (mode === savedMode) b.textContent = btnText(mode);
  });

  setMode('login');
</script></body></html>"""


# =============================================================================
# 七、AI 对话答疑（和 AI 讨论这道题怎么做）
# =============================================================================

# 注意：这里用 r""" 原始字符串。普通字符串里 \b \f 会被 Python 当成退格/换页符吃掉，
# 结果 "\\begin{aligned}" 变成 "egin{aligned}"、"\\frac" 变成 "rac"
# —— 等于把坏掉的格式示范喂给模型，它当然照着学。
TUTOR_SYSTEM = r"""你是一位耐心、细致的初三中考辅导老师，正在陪学生复盘一道错题。

你能看到这道题的**原始照片**——上面有学生当时的手写解答过程和老师的红笔批改。
这是你最大的优势：先看清楚**学生是在哪一步走偏的**，再针对性地讲，不要泛泛而谈。

回答要求：
1. **先点破卡点**：一句话说清学生错在哪 / 卡在哪（若照片上看不出来，就别硬猜）。
2. **再给思路**：分步骤讲，每一步都要说清「为什么这么做」，而不是只报算式。
3. **公式必须用 $ 包起来**（这条最重要，漏了的话孩子看到的就是一堆 \frac 源码）：
   行内写 $x^2-2x-3=0$，独立成行的推导写 $$...$$，多行对齐用
   $$\begin{aligned}...\\...\end{aligned}$$。化学式也照此办理：$Na_2CO_3$、$Ca(OH)_2$。
   **不要**直接输出不带 $ 的裸公式（如 I=\frac{U}{R}），也不要用 \( \) 之外的其它写法。
4. **语气鼓励**：初三孩子压力大，指出问题的同时要肯定他已经做对的部分。
5. **长度**：控制在 500 字以内，抓住重点，不要面面俱到。

如果学生只是问概念、不涉及这道题，就正常回答即可。

【插图】—— 你是会画图的
一段静态示意图就能讲清楚时（几何图形、受力分析、电路、光路、函数图像），
直接在你回答的位置插一段 SVG，**孩子会看到图直接画在讲解里**，不用他动手。

**绝对不要用 ASCII 字符拼图**（`┌───S───[R1]───┐` 这种），也不要写「你照着画就行」——
那种图手机上会错位、没法缩放、也没法标注。要画就画真的 SVG。

格式长这样（这是个完整的串联电路例子，照这个风格写其他的）：

```svg
<svg viewBox="0 0 440 200" xmlns="http://www.w3.org/2000/svg">
  <title>串联电路：6V 电源与 R₁=10Ω、R₂=20Ω</title>
  <defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="7" markerHeight="7" orient="auto">
    <path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>
  <g fill="none" stroke="currentColor" stroke-width="2">
    <path d="M50,40 H150 M190,40 H310 M350,40 H390 V160 H50 V130 M50,90 V40"/>
    <rect x="150" y="28" width="40" height="24"/>
    <rect x="310" y="28" width="40" height="24"/>
  </g>
  <g stroke="currentColor" stroke-width="2">
    <line x1="26" y1="100" x2="74" y2="100"/>
    <line x1="38" y1="118" x2="62" y2="118"/>
  </g>
  <line x1="230" y1="70" x2="290" y2="70" stroke="currentColor"
        stroke-width="2" marker-end="url(#ar)"/>
  <g font-size="13" fill="currentColor" font-family="system-ui,sans-serif">
    <text x="140" y="20">R₁=10Ω</text><text x="300" y="20">R₂=20Ω</text>
    <text x="8" y="113">6V</text><text x="232" y="88">I=0.2A</text>
  </g>
</svg>
```

画图规矩（都是为了让图在深色/浅色背景下都看得清）：
- **必写 viewBox**，不要写 width/height（页面会自适应宽度，手机上也好看）。
- **线条和文字颜色一律用 `currentColor`**，这样在任何背景上都可读；
  只给「最关键的那一个元素」用具体颜色（如 `#e11d48`）强调，且要深到白底上也看得清。
- 线宽 2，字号 12~14，标签用短词（A、B、C、30Ω、6V、G、F），
  不要把整句话塞进图里——解释写在图外面的正文里。
- 箭头用 `<defs><marker>` + `marker-end="url(#ar)"`，不要用图片。
- 图上必须有标注：几何题标顶点和边长，受力图标力的大小和方向，
  电路图标元件符号和数值。孩子要能只对着图把题读懂。
- **禁止** `<script>`、`<style>`、`<foreignObject>` 和外部图片——会被安全过滤掉。
- 静态示意用 `svg`；需要播放、拖动、分步演示的用下面的 `html-anim`。

【可视化动画】
这个应用会自动托管你的动画：**你输出 HTML 围栏的那一刻，界面上就已经出现了绿色按钮
「打开动画 ↗」**，用户点一下就能在浏览器里看到会动、能交互的页面。按钮是系统生成的，
不需要你做任何事，也不需要用户做任何事。

所以正文里只写一句类似「动画我做好了，点上面的绿色按钮就能看」，然后说清动画演示了哪几步。
**禁止**出现下面这些内容（它们都是错的）：
- 「我无法直接生成文件 / 动图我没法生成」
- 「复制代码」「新建文本文档」「保存为 .html」「双击打开」「粘贴到」——用户不需要保存任何文件
- 在正文里再贴一遍 HTML 代码

什么时候画：涉及「看得见就懂了」的内容就画——函数图像与动点、几何图形、电路、
受力分析、光路、化学实验装置等。画不出真正的动画就别画。

输出格式（标题写在语言标记后面）：

```html-anim 标题
<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>标题</title>
<style>/* 内联样式 */</style></head>
<body>
  <canvas id="c"></canvas>
  <script>/* 内联脚本 */</script>
</body></html>
```

正确的回答长这样（照这个风格写正文）：

> 动画我做好了，点上面的绿色按钮「打开动画」就能看：抛物线逐步画出 → 标出顶点
> (1, −4) → 标出与 x 轴交点 A(−1,0)、B(3,0) 和与 y 轴交点 C(0,−3) → 连成三角形 →
> 高亮底边 AB=4 和高 OC=3 → 最后显示面积 6。可以点「上一步/下一步」慢慢看。

```html-anim y=x²−2x−3 动画讲解
（这里是完整 HTML）
```

硬性要求：
- 单个 HTML 文件，CSS/JS 全部内联；外部资源只允许 https://cdn.jsdelivr.net 下的库。
- **打开就自动播放**，不要让孩子先去找按钮；同时给「重播」和「上一步/下一步」。
- 手机能正常看：宽度用 `max-width:100%` 自适应，canvas 用 devicePixelRatio 适配高清屏。
- 必须标出关键点、坐标、公式和最终结论（比如把 A、B、C 的坐标和面积 6 直接写在画面上）。
- 用 Canvas 或内联 SVG 画，不要引用图片。
- 动画代码控制在 250 行以内，简洁优先。
- 整段回答最多给一个动画。"""

CHAT_MAX_TURNS = 40          # 单题最多保留的对话轮数（防止上下文无限增长）

# 「引导模式」的提示词。设计依据：直接给答案会产生"虚假精通"——看的时候觉得懂了，
# 考试还是不会。所以默认走这条路：一次只推一小步，让孩子自己跨过去。
# 想看完整讲解必须由孩子再点一次明确的按钮（前端切换成 TUTOR_SYSTEM）。
_GUIDE_ROLE = r"""你是一位耐心、细致的初三中考辅导老师，正在陪学生复盘一道错题。

【当前是「引导模式」——这条最重要】
你的目标不是把答案讲出来，而是**把孩子自己推过去**。直接看答案会产生
"虚假精通"：看的时候觉得懂了，考试还是不会。所以：

铁律：
1. **绝不输出完整解题过程，绝不给出最终答案。** 一次只推进一小步。
2. 开头先反问一句，让孩子说出他想到哪一步了。比如「你先说说，你打算从哪下手？」
3. 然后给**一层**提示——只给方向和思路，不给具体算式。
   例如「想想抛物线与 x 轴的交点，在解析式里对应什么条件？」
4. 等孩子回应后，再给下一层提示。一层一层来，不要一口气全倒出来。
5. 孩子说「我还是不会」时，可以给更具体的提示，但**仍然不给最终答案**；
   最多把上一次的提示换个说法、再往前挪半步。
6. 语气要鼓励，别让孩子挫败；肯定他已经做对的部分。

如果孩子直接要答案，就温和地挡回去：「先自己写两步试试，或者告诉我你卡在哪，
我再帮你——直接看答案对中考帮助不大。」

你能看到这道题的原图（含学生当时的手写过程和老师的红笔批改），
可以据此判断他到底卡在哪一步，针对性提问。

需要画图帮助理解时可以画示意图，但**不要**把关键步骤或最终结果画上去。"""

# 引导模式复用完整模式里的绘图规范：TUTOR_SYSTEM 里从「【插图】」往后就是这部分。
# 切片复用而不是复制一份，否则两边的画图要求迟早会走样。
_SPEC_START = TUTOR_SYSTEM.find("【插图】")
_SPEC = TUTOR_SYSTEM[_SPEC_START:] if _SPEC_START > 0 else ""
GUIDE_SYSTEM = _GUIDE_ROLE + "\n\n" + _SPEC

# 「自由问答」线程占 0 号位。mistakes 表的 id 从 1 开始自增，0 永远空着，
# 拿它当「不挂靠任何一道题」的哨兵值最省事：不用改表结构、不用加列，
# chats 表原有的索引、动画文件命名（anim_m0_*.html）全都照常工作。
GENERAL_CHAT_ID = 0

# 没选中错题时的「自由问答」人格。和错题答疑的关键区别：
# 这里**没有题图、没有考点上下文**，学生问什么答什么，
# 所以不能套用「先点破卡点」那一套（没卡点可点）。
_GENERAL_ROLE = r"""你是一位耐心、细致的初三中考**全科**辅导老师，学生随时来找你提问。

你能回答初三七科（语文、数学、英语、物理、化学、历史、道德与法治）范围内的任何学习问题：
知识点讲解、解题方法、复习规划、应试技巧、记忆方法、心态调整等，都可以。

注意：这一次你没有看到任何题目照片，学生也没指定是哪道题。
如果他从上下文看明显是在问某一道具体的题、却没把题给你，**先请他描述题目或上传照片**，
不要凭空猜题——猜错了比不答更浪费时间。

学生是初三的孩子，正扛着中考压力：
- 说人话。别用教学大纲式的书面语，术语第一次出现时顺带解释一句。
- 语气鼓励，别否定孩子的感受（「我数学就是不行」这类话要正面接住）。
- 不给鸡汤、不说教，给能马上执行的具体办法。"""

_GENERAL_DIRECT = r"""【本轮：直接讲解】
学生问的是知识性 / 方法性的问题，**直接把答案讲清楚**，不要反问他。
- 结论先行：第一句话就给答案，别绕圈子。
- 再讲为什么：给出理由或推导，让他知其所以然。
- 最后举个例子：具体的、初三范围内的例子。
- 长度控制在 600 字以内；问题确实复杂时可以稍长，但别写成小论文。"""

_GENERAL_GUIDE = r"""【本轮：引导模式】
学生点了「引导我自己想」——这是他主动挑了难走的那条路，要珍惜，别辜负。
铁律：
1. **绝不输出最终答案**，一次只推进一小步。
2. 开头先反问一句，让他说说自己已经想到哪了。
3. 每次只给**一层**提示：给方向，不给具体算式或结论。
4. 他说「还是不会」时，把提示换个说法、再往前挪半步，仍然不给答案。
5. 但如果这压根是道知识性问题、没有「想」的过程（比如「这首诗的作者是谁」），
   就别硬引导了，直接告诉他。引导是用来跨过障碍的，不是用来刁难人的。"""

GENERAL_SYSTEM = _GENERAL_ROLE + "\n\n" + _GENERAL_DIRECT + "\n\n" + _SPEC
GENERAL_GUIDE_SYSTEM = _GENERAL_ROLE + "\n\n" + _GENERAL_GUIDE + "\n\n" + _SPEC

ANIM_DIR = os.path.join(STATIC_DIR, "anim")
os.makedirs(ANIM_DIR, exist_ok=True)
ANIM_MAX_BYTES = 300 * 1024  # 单个动画 HTML 上限，防止模型跑飞


class AnimFenceParser:
    """
    从流式文本里剥出 ```html-anim 围栏，把源码拦在服务端。

    难点：围栏会被 delta 切成任意碎片。**标题长度不定**，所以不能简单地
    「扣留前 N 个字符」——早期版本只扣 11 字符（关键词长度），标题还没到齐，
    缓冲区就已被放行，代码直接漏到界面上（逐字符喂入时 100% 复现）。
    因此这里用三状态机：text → header（收标题行）→ anim（收 HTML）。

    自测：把整段围栏逐字符喂进来，结果必须和一次性喂入完全一致。
    """

    # 也收 ```html：实测模型对「输出 HTML」的默认习惯就是 ```html，
    # 提示词里再怎么强调 ```html-anim 它也会时不时退回老习惯（本次实测就是）。
    # 与其跟模型的先验较劲，不如两种都认——反正「错题答疑里冒出一段 HTML 代码块」
    # 这件事本身，唯一的合理解释就是要做动画。
    KW = ("```html-anim", "```anim", "```html")
    KW_MAX = max(len(k) for k in KW)
    HEADER_LIMIT = 200          # 标题行长度上限，防止畸形输出把缓冲撑爆

    def __init__(self) -> None:
        self.buf = ""
        self.state = "text"     # text / header / anim
        self.kw = ""
        self.title = ""
        self.anim: List[str] = []

    def _hold_len(self) -> int:
        """必须扣在手里的最长后缀长度（它可能是某个关键词的开头）。"""
        for k in range(min(self.KW_MAX, len(self.buf)), 0, -1):
            tail = self.buf[-k:]
            if any(kw.startswith(tail) for kw in self.KW):
                return k
        return 0

    def _find_kw(self) -> Optional[Tuple[int, str]]:
        """找围栏关键词；后面必须跟空白/换行才算命中，避免误吃 ```animation。"""
        best: Optional[Tuple[int, str]] = None
        for kw in self.KW:
            i = self.buf.find(kw)
            while i >= 0:
                nxt = self.buf[i + len(kw): i + len(kw) + 1]
                if nxt and nxt in " \t\r\n":
                    if best is None or i < best[0]:
                        best = (i, kw)
                    break
                i = self.buf.find(kw, i + 1)
        return best

    def feed(self, text: str) -> Tuple[str, Optional[Dict[str, str]]]:
        """返回 (可放行的正文, 若恰好收完一个动画则为 {"title","html"})。"""
        self.buf += text
        out: List[str] = []
        while True:
            if self.state == "text":
                hit = self._find_kw()
                if hit is None:
                    hold = self._hold_len()
                    out.append(self.buf[:-hold] if hold else self.buf)
                    self.buf = self.buf[-hold:] if hold else ""
                    return "".join(out), None
                i, self.kw = hit
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.kw):]
                self.state = "header"
                continue

            if self.state == "header":
                nl = self.buf.find("\n")
                if nl < 0:
                    # 标题行还没结束。正常很快就有；畸形输出则退回普通文本。
                    if len(self.buf) > self.HEADER_LIMIT:
                        out.append(self.kw + self.buf)
                        self.kw, self.buf, self.state = "", "", "text"
                        return "".join(out), None
                    return "".join(out), None
                self.title = self.buf[:nl].strip() or "可视化讲解"
                self.buf = self.buf[nl + 1:]
                self.state, self.anim = "anim", []
                continue

            # state == "anim"
            idx = self.buf.find("\n```")
            if idx >= 0:
                self.anim.append(self.buf[:idx])
                html = "".join(self.anim)
                self.buf = self.buf[idx + 4:]
                self.state, self.anim = "text", []
                return "".join(out), {"title": self.title, "html": html}
            if len(self.buf) > 4:      # 留尾巴，防止 "```" 被 delta 切断
                self.anim.append(self.buf[:-4])
                self.buf = self.buf[-4:]
            return "".join(out), None

    def close(self) -> Tuple[str, Optional[Dict[str, str]]]:
        """流结束时收尾：没闭合的围栏按已有内容尽力交付。"""
        if self.state == "anim":
            html = "".join(self.anim) + self.buf
            self.state, self.anim, self.buf = "text", [], ""
            return "", {"title": self.title or "可视化讲解", "html": html}
        if self.state == "header":
            # 关键词之后一直没换行：当成普通文字吐回去，别吞内容
            text = self.kw + self.buf
            self.kw, self.buf, self.state = "", "", "text"
            return text, None
        tail, self.buf = self.buf, ""
        return tail, None


# 模型「教你手工保存 HTML」是训练出来的强先验，实测提示词里写「禁止」也压不住
# （会反复说「我没法直接生成文件 → 新建文本文档 → 粘贴 → 保存为 .html → 双击打开」，
#  甚至推荐用 LICEcap 录屏）。这里做一层确定性兜底：按行丢掉这类句子。
_MANUAL_STEP_PATTERNS = [
    re.compile(r"(新建|创建|打开).{0,8}(文本文档|文本文件|记事本|文档|txt)"),
    re.compile(r"(保存为|另存为|存为|改(一下)?后缀|后缀名).{0,20}(html|网页)", re.I),
    re.compile(r"双击.{0,12}(打开|运行)"),
    re.compile(r"(复制|粘贴).{0,14}(代码|内容|进去|进去|到)"),
    re.compile(r"(代码|内容).{0,14}(复制|粘贴|贴进|贴到)"),
    re.compile(r"(没法|无法|不能).{0,10}(直接)?(生成|给你|输出).{0,10}(图片|gif|动图|文件)", re.I),
    re.compile(r"(licecap|录屏|屏幕录制|导出\s*gif)", re.I),
    re.compile(r"^\s*#{1,6}\s*(用法|使用方法|使用说明|如何打开|怎么打开)\s*$"),
    re.compile(r"^(用法|使用方法|使用说明)\s*[:：]"),
    # 「想改速度就找到代码里的 stages 数组」——用户手里根本没有代码，纯噪音
    re.compile(r"(找到|修改|调整|编辑|打开).{0,12}(代码|源码|脚本).{0,8}(里|中|的)"),
    re.compile(r"^\s*\**优化建议\s*[:：]"),
]


def strip_manual_steps(text: str) -> str:
    keep = [ln for ln in text.split("\n")
            if not (ln.strip() and any(p.search(ln.strip()) for p in _MANUAL_STEP_PATTERNS))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep))


class ManualStepFilter:
    """
    流式版本的手工步骤过滤器。

    只能按**整行**判断，所以必须扣住「当前还没写完的那一行」——但为了不毁掉流式手感，
    扣留上限设为 MAX_HOLD：超长行（多半是正常段落）直接放行，不再等换行。
    """

    MAX_HOLD = 200

    def __init__(self) -> None:
        self.pending = ""

    def _clean(self, line: str) -> str:
        s = line.strip()
        if s and any(p.search(s) for p in _MANUAL_STEP_PATTERNS):
            return ""
        return line

    def feed(self, txt: str, final: bool = False) -> str:
        self.pending += txt
        out: List[str] = []
        while True:
            nl = self.pending.find("\n")
            if nl < 0:
                if self.pending and (final or len(self.pending) > self.MAX_HOLD):
                    out.append(self._clean(self.pending))
                    self.pending = ""
                break
            out.append(self._clean(self.pending[:nl + 1]))
            self.pending = self.pending[nl + 1:]
        if final and self.pending:
            out.append(self._clean(self.pending))
            self.pending = ""
        return "".join(out)


def save_animation(mid: int, title: str, html: str) -> Optional[Dict[str, str]]:
    """把 AI 生成的动画存成静态文件，返回可点击的 URL。"""
    html = (html or "").strip()
    if len(html) < 80 or "</" not in html:
        return None                      # 明显不成型，丢弃
    if len(html.encode("utf-8", "ignore")) > ANIM_MAX_BYTES:
        return None
    if "<!DOCTYPE" not in html.upper()[:200]:
        html = ('<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '</head><body>\n' + html + "\n</body></html>")
    name = f"anim_m{mid}_{int(time.time() * 1000)}.html"
    with open(os.path.join(ANIM_DIR, name), "w", encoding="utf-8") as f:
        f.write(html)

    # 模型常用裸 ```html（那行没地方写标题），这时就从页面自己的 <title> 里取
    title = (title or "").strip()
    if not title or title == "可视化讲解":
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    return {"title": (title or "可视化讲解")[:60], "url": f"/anim/{name}"}


def purge_animations(mid: int) -> None:
    """清空对话时，把这道题生成的动画文件一并删掉。"""
    try:
        for fn in os.listdir(ANIM_DIR):
            if fn.startswith(f"anim_m{mid}_"):
                os.remove(os.path.join(ANIM_DIR, fn))
    except OSError:
        pass


def build_question_context(it: Dict[str, Any]) -> str:
    """把这道题的已知信息拼成系统提示里的上下文块。"""
    kind = norm_kind(it.get("kind"))
    parts = [
        "【本题信息】",
        f"题目类型：{KINDS[kind]}"
        + ("（学生做错过这道题，照片上有他的错误过程）" if kind == "mistake"
           else "（学生没做错——这是他要收藏的好题，照片上看不到错误过程）"),
        f"科目：{it.get('subject') or '未知'}",
        f"考点：{it.get('tag') or '未标注'}",
    ]
    if it.get("clean_text"):
        parts.append("题目（AI 提取的印刷体题面）：\n" + it["clean_text"])
    if it.get("variant_q"):
        parts.append("（系统此前生成的同考点变式题，供你参考，学生没问就不用提）\n" + it["variant_q"])
    parts.append("题目原始照片见本次对话的第一张图片。")

    if kind == "classic":
        # 系统提示是围绕「复盘错题」写的，开头那句「先点破卡点」对经典题完全不适用。
        # 放在上下文最后、显式声明优先级，比复制一整套 TUTOR_SYSTEM 便宜得多，
        # 也不会出现两份提示各自漂移的问题。
        parts.append(
            "\n【重要：这题没做错，请据此调整口径（本条覆盖以上提示里与之冲突的部分）】\n"
            "上面「先点破卡点 / 说清他错在哪一步 / 为什么会这么错」那套是给**错题**用的，"
            "这道题不适用——照片上没有错误过程，硬找一个错因就是编造。改成：\n"
            "· 讲这道题**妙在哪**（哪个设计让它成为好题）、**关键一步**是哪一步、"
            "**这一类题的通法**是什么；\n"
            "· 学生问「怎么做」时正常讲思路（引导模式下仍然是先反问、一次只给一层提示、"
            "不给最终答案）；\n"
            "· **不要**说「你当时怎么错的」「你卡在哪一步」这类话——他没有做错这道题。"
        )
    return "\n".join(parts)


def build_chat_messages(history: List[Dict[str, Any]], ctx: str,
                        data_uri: str, new_msg: str) -> List[Dict[str, Any]]:
    """
    组装对话消息。图片只挂在「第一条 user 消息」上（之后的轮次是纯文本），
    既保证模型随时能回看原图，又不会每轮都重复消耗图片 token。

    data_uri 为空时（自由问答没有题图）整条链路退化成纯文本：
    绝不能塞一个 content 为空的 image_url 进去，那种消息会被 API 直接判为非法。
    """
    msgs: List[Dict[str, Any]] = [{"role": "system", "content": ctx}]
    if not data_uri:
        for h in history:
            msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": new_msg})
        return msgs
    if not history:
        msgs.append({
            "role": "user",
            "content": [
                {"type": "text", "text": new_msg},
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
            ],
        })
        return msgs

    for i, h in enumerate(history):
        if i == 0 and h["role"] == "user":
            msgs.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": h["content"]},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                ],
            })
        else:
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": new_msg})
    return msgs


def build_tutor_messages(it: Optional[Dict[str, Any]], history: List[Dict[str, Any]],
                         question: str, mode: str = "full") -> List[Dict[str, Any]]:
    """
    mode = "guide" 引导模式：先反问、给一层提示，不给完整解答
    mode = "full"  完整讲解

    it 为 None 表示自由问答（没选中错题）：换成全科老师人格，没有题图。
    """
    if it is None:
        ctx = GENERAL_SYSTEM if mode == "full" else GENERAL_GUIDE_SYSTEM
        return build_chat_messages(history, ctx, "", question)

    orig_abs = os.path.join(STATIC_DIR, it.get("orig_path") or "")
    if not os.path.exists(orig_abs):
        orig_abs = os.path.join(STATIC_DIR, it.get("clean_path") or "")
    data_uri = image_to_data_uri(orig_abs) if os.path.exists(orig_abs) else ""

    base = GUIDE_SYSTEM if mode == "guide" else TUTOR_SYSTEM
    ctx = base + "\n\n" + build_question_context(it)
    if not data_uri:
        ctx += "\n（注意：本题原图缺失，你只能依据上面的题面文字回答。）"
    return build_chat_messages(history, ctx, data_uri, question)


def call_tutor_stream(it: Optional[Dict[str, Any]], history: List[Dict[str, Any]], question: str,
                      mode: str = "full", profile: str = "chat"):
    """
    流式答疑生成器。依次产出：
        {"type": "reasoning", "text": 思考片段}   模型思考过程（先到，可用来消解等待焦虑）
        {"type": "delta",     "text": 正文片段}   正式回答
        {"type": "final",     "text": 完整正文}   结束标记，附带全文
        {"type": "error",     "text": 错误说明}
    """
    messages = build_tutor_messages(it, history, question, mode)
    parts: List[str] = []
    last_err = "未知错误"
    started = time.time()
    _dl, _idle = _profile(profile)["deadline"], _profile(profile)["idle"]
    for attempt in range(1, 3):
        parts = []
        try:
            stream = _client().chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.6,
                max_tokens=CHAT_MAX_TOKENS,
                stream=True,
                # ⚠️ 这一行才是真正的「看门狗」，不能省。
                # 下面循环体里那个 idle 检查**管不到连接卡死**：连接一沉默，
                # `for ev in stream` 那一行就永远阻塞，循环体根本进不去，
                # 检查和硬时限都形同虚设（实测过：假装卡死它就一直干等）。
                # httpx2 的 read 超时是**按每次读取**算的，正好就是
                # 「多久没收到新数据就报错」——这才是卡死该有的兜底。
                timeout=httpx2.Timeout(connect=10.0, read=float(_idle),
                                       write=30.0, pool=10.0),
                **_think_kwargs(profile),
            )
            parser = AnimFenceParser()
            msf = ManualStepFilter()
            last_chunk = time.time()
            try:
                for ev in stream:
                    now = time.time()
                    # 硬时限：模型偶尔会无休止地想下去。这是**唯一真正兜底**的一层保护 ——
                    # reasoning_effort 只是「劝」它少想，拦不住极端情况。到点必须断。
                    if now - started > _dl:
                        # 不足一分钟要显示秒，写「0 分钟」会让人以为出 bug 了
                        span = f"{_dl // 60} 分钟" if _dl >= 60 else f"{_dl} 秒"
                        yield {"type": "deadline",
                               "text": f"已经跑了 {span}，超过上限，已中断"}
                        return
                    # 看门狗：连接没断但一个字都不来，判定卡死
                    if now - last_chunk > _idle:
                        yield {"type": "deadline",
                               "text": f"连续 {_idle} 秒没有响应，已中断"}
                        return
                    last_chunk = now
                    if not ev.choices:
                        continue
                    d = ev.choices[0].delta
                    rc = getattr(d, "reasoning_content", None)
                    if rc:
                        yield {"type": "reasoning", "text": rc}
                    ct = getattr(d, "content", None)
                    if ct:
                        plain, anim = parser.feed(ct)
                        if plain:
                            clean = msf.feed(plain)
                            if clean:
                                parts.append(clean)
                                yield {"type": "delta", "text": clean}
                        if anim:
                            yield {"type": "anim", "title": anim["title"], "html": anim["html"]}
                tail_plain, anim = parser.close()
                flushed = msf.feed(tail_plain or "", final=True)
                if flushed:
                    parts.append(flushed)
                    yield {"type": "delta", "text": flushed}
                if anim:
                    yield {"type": "anim", "title": anim["title"], "html": anim["html"]}
            finally:
                # 主动关闭上游连接：只 return 不关的话，DeepSeek 那边还会继续生成，
                # 钱照烧。close() 直接断开 HTTP，让它停。
                try:
                    stream.close()
                except Exception:
                    pass
            full = "".join(parts).strip()
            if full:
                yield {"type": "final", "text": full}
                return
            last_err = ("模型只输出了思考过程就被截断（推理 token 吃满预算），"
                        "可调大 CHAT_MAX_TOKENS")
        except Exception as e:
            # 读超时单独给句人话：原始信息是 "ReadTimeout: timed out"，
            # 甩给孩子看没有任何意义
            if "Timeout" in type(e).__name__ or "timeout" in str(e).lower():
                last_err = f"连接卡住了（{_idle} 秒没收到新内容），已中断"
            else:
                last_err = f"{type(e).__name__}: {e}"
        time.sleep(1.0 * attempt)
    yield {"type": "error", "text": last_err}


def call_tutor(it: Optional[Dict[str, Any]], history: List[Dict[str, Any]], question: str,
               mode: str = "full", profile: str = "chat") -> str:
    """非流式答疑（供 curl / 外部调用）。失败直接抛异常，由接口层转成 502。"""
    messages = build_tutor_messages(it, history, question, mode)

    last_err = "未知错误"
    for attempt in range(1, 3):
        try:
            resp = _client().chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.6,
                max_tokens=CHAT_MAX_TOKENS,
                timeout=AI_HTTP_TIMEOUT,
                **_think_kwargs(profile),
            )
            ch = resp.choices[0]
            text = (ch.message.content or "").strip()
            if text:
                return text
            if ch.finish_reason == "length":
                rt = getattr(resp.usage.completion_tokens_details, "reasoning_tokens", "?")
                last_err = (f"思考过程占满了 token 预算被截断"
                            f"（reasoning_tokens={rt}，上限 {CHAT_MAX_TOKENS}）")
            else:
                last_err = "模型返回了空内容"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1.0 * attempt)
    raise RuntimeError(last_err)


# =============================================================================
# 八、FastAPI 应用
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保静态目录存在（用户明确要求）
    os.makedirs(ORIGIN_DIR, exist_ok=True)
    os.makedirs(CLEAN_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    init_db()

    # 初始化会话签名密钥。口令不再有全局的 —— 每个账号存自己的哈希。
    global _secret
    _secret = _load_secret()
    with closing(get_conn()) as _c:
        _users = _c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    ip = get_lan_ip()
    lines = [
        "",
        "=" * 66,
        "  我的AI学习助手  已启动",
        "=" * 66,
        f"  本机     ：http://127.0.0.1:8000",
        f"  局域网   ：http://{ip}:8000",
        f"  数据库   ：{DB_PATH}",
        f"  AI 模型  ：{MODEL_NAME}  @  {BASE_URL}",
        "",
        "  ── 账号 ─────────────────────────────────────────",
        f"  已有账号 ：  {_users} 个",
        ("  首次使用 ：  打开网页点「注册」建第一个账号" if _users == 0
         else ("  注册门槛 ：  需要邀请码（SIGNUP_CODE）" if SIGNUP_CODE
               else "  注册门槛 ：  开放注册（设 SIGNUP_CODE 可加邀请码）")),
        f"  免登录期 ：  {SESSION_TTL // 86400} 天",
        "  公网访问 ：  需在阿里云安全组放行 TCP 8000",
        "=" * 66,
    ]
    if not api_key_ready():
        lines += [
            "  ⚠️  未配置 DEEPSEEK_API_KEY：上传仍可用（原图+去红笔图会正常保存），",
            "     但「考点识别 / 纯净题干 / 变式题 / 解析」不会生成。",
            "     配置方式：export DEEPSEEK_API_KEY=sk-xxxx  或在 main.py 顶部修改常量。",
            "=" * 66,
        ]
    lines += [""]
    # flush=True：nohup / systemd 重定向时 stdout 是块缓冲，不刷就看不到横幅
    print("\n".join(lines), flush=True)

    # 后台预热中文字体：本机没有任何中文字体时先下好，避免第一次导出 PDF 干等 17MB 下载。
    # 放线程里跑，不阻塞启动。
    def _prewarm():
        try:
            get_cjk_font()
        except Exception as e:
            print(f"  ⚠️  字体预热失败：{e}", flush=True)

    if not any(os.path.exists(p) for p in SYSTEM_CJK_FONTS):
        threading.Thread(target=_prewarm, daemon=True).start()
    yield


app = FastAPI(title="我的AI学习助手", version="1.0.0", lifespan=lifespan)
# 说明：前端与后端同源部署，不需要 CORS。公网服务上开 allow_origins=["*"]
# 只会白白扩大攻击面（配合 Cookie 认证还可能被跨站利用），故不启用。
# ⚠️ 这里原本是 app.mount("/static", StaticFiles(...))。
# 多用户下**必须**去掉：StaticFiles 只校验「登录了没」，不校验「这文件是不是你的」，
# 而文件名是 original_{id}.jpg —— id 连续自增，登录用户换个数字就能拿到别人的照片。
# 改成下面按归属鉴权的 /media/{kind}/{mid} 路由。

# 免登录路径：登录页、登录接口，以及站点图标。
# 图标必须放行 —— 浏览器请求 favicon 时不带任何上下文，若被 302 到 /login，
# 标签页/收藏夹/添加到主屏幕都会拿到一张 HTML 当图片，图标直接空白。
# /api/signup_policy 也必须在里面：登录页要在**登录之前**调它，
# 才知道要不要显示邀请码输入框。漏了它，配了邀请码也不会出现那个框。
PUBLIC_PATHS = {"/login", "/api/login", "/api/register", "/api/register/verify",
                "/api/register/resend", "/api/reset/request", "/api/reset/do",
                "/api/signup_policy", "/api/mail_policy",
                "/favicon.ico", "/favicon.svg", "/favicon-192.png",
                "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """
    全局认证闸门。必须在路由之前拦，这样 /static 下的照片也一并受保护
    —— 只保护 API 是不够的：照片 URL 一旦泄露就等于数据泄露。
    """
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    uid = verify_token(request.cookies.get(SESSION_COOKIE))
    if uid:
        # 写进 ContextVar，供各 endpoint 的 current_uid() 读取。
        # 每个请求一个独立的 context，不会串。
        _uid.set(uid)
        return await call_next(request)

    # 未登录：API 请求回 401（前端据此跳登录页），页面请求直接 302
    if path.startswith("/api/"):
        return JSONResponse({"detail": "未登录或登录已过期，请重新登录"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------- 路由：登录 / 登出 ------------------------------
class LoginReq(BaseModel):
    username: str = ""
    password: str = ""


class RegisterReq(BaseModel):
    username: str = ""
    password: str = ""
    email: str = ""
    invite: str = ""       # 只有设了 SIGNUP_CODE 时才需要填


class VerifyReq(BaseModel):
    username: str = ""
    code: str = ""


class ResetReq(BaseModel):
    email: str = ""


class ResetDoReq(BaseModel):
    email: str = ""
    code: str = ""
    new: str = ""


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


# 用户名规则：3~20 位，字母数字下划线连字符，必须以字母或数字开头。
# 收窄字符集是为了避免「长得几乎一样的用户名」用来冒充（同形字攻击）。
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,19}$")
MIN_PW_LEN = 6


def _session_response(uid: int, extra: Optional[Dict[str, Any]] = None) -> JSONResponse:
    body = {"ok": True, "ttl": SESSION_TTL}
    if extra:
        body.update(extra)
    resp = JSONResponse(body)
    # 注意：这里刻意不设 secure=True —— 当前是明文 HTTP，设了浏览器就不会回传 Cookie，
    # 登录会直接失效。等上了 HTTPS（见 README）再打开该标志。
    resp.set_cookie(
        SESSION_COOKIE, make_token(uid),
        max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
    )
    return resp


@app.get("/login")
def login_page():
    return HTMLResponse(
        LOGIN_PAGE,
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


@app.post("/api/login")
def api_login(req: LoginReq, request: Request):
    ip = _client_ip(request)
    if _login_locked(ip):
        raise HTTPException(429, f"失败次数过多，请 {LOGIN_WINDOW // 60} 分钟后再试")

    name = (req.username or "").strip().lower()
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT id, pw_hash FROM users WHERE username=?", (name,)).fetchone()

    # 用户不存在时也走一遍哈希：否则「账号不存在」会比「密码错误」快得多，
    # 用响应时间就能把哪些用户名存在给枚举出来。
    stored = row["pw_hash"] if row else "pbkdf2_sha256$%d$00$00" % PBKDF2_ROUNDS
    ok = verify_password(req.password, stored)
    if not row or not ok:
        _record_fail(ip)
        time.sleep(0.6)          # 配合 PBKDF2 的 50ms，进一步压低暴力破解速率
        # 不区分「用户不存在」和「密码错」：那等于免费告诉攻击者哪些账号有效
        raise HTTPException(401, "用户名或密码不正确")

    _clear_fails(ip)
    return _session_response(int(row["id"]), {"username": name})


def _send_code(to: str, username: str, code: str, purpose: str) -> Tuple[bool, str]:
    what = "注册验证" if purpose == "signup" else "重置密码"
    body = (
        f"你的{what}验证码是：\n\n"
        f"    {code}\n\n"
        f"{VERIFY_TTL_MIN} 分钟内有效，只能使用一次。\n"
        f"如果不是你本人操作，忽略这封邮件即可，你的账号不会有任何变化。\n\n"
        f"—— 我的AI学习助手"
    )
    return send_mail(to, f"【我的AI学习助手】{what}验证码：{code}", body)


@app.post("/api/register")
def api_register(req: RegisterReq, request: Request):
    """
    注册第一步：校验资料 → 发验证码。**这一步不创建账号。**

    账号要等验证码验过才写进 users（见 /api/register/verify）。
    这样「邮箱是真的」就成了注册的前提 —— 对一个跑在公网、
    背后挂着计费 API key 的服务来说，这是最有效的一道防机器人门槛。
    """
    ip = _client_ip(request)
    if _login_locked(ip):
        raise HTTPException(429, f"尝试次数过多，请 {LOGIN_WINDOW // 60} 分钟后再试")
    if SIGNUP_CODE and not hmac.compare_digest((req.invite or "").strip(), SIGNUP_CODE):
        _record_fail(ip)
        raise HTTPException(403, "邀请码不正确")
    if not MAIL_READY:
        raise HTTPException(503, "服务器还没有配置邮件服务，暂时无法注册。请联系管理员。")

    name = (req.username or "").strip().lower()
    email = (req.email or "").strip()
    pw = req.password or ""
    if not USERNAME_RE.match(name):
        raise HTTPException(400, "用户名需 3~20 位，只能是字母、数字、下划线或连字符，且以字母或数字开头")
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise HTTPException(400, "邮箱格式不正确")
    if len(pw) < MIN_PW_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PW_LEN} 位")
    if len(pw) > 200:
        raise HTTPException(400, "密码太长了")

    now = datetime.now()
    with closing(get_conn()) as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            _record_fail(ip)
            raise HTTPException(409, "这个用户名已经被注册了")
        if conn.execute("SELECT 1 FROM users WHERE email=? AND email!=''",
                        (email,)).fetchone():
            _record_fail(ip)
            raise HTTPException(409, "这个邮箱已经注册过了")
    # 重发节流：不然这个接口就是个免费的邮件轰炸机
    wait = mail_throttle("signup:" + name)
    if wait:
        raise HTTPException(429, f"验证码刚发过，请 {wait} 秒后再试")

    code = "".join(secrets.choice("0123456789") for _ in range(6))
    ok, msg = _send_code(email, name, code, "signup")
    if not ok:
        raise HTTPException(502, f"验证码发送失败：{msg}")

    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM signups WHERE username=?", (name,))
        conn.execute(
            "INSERT INTO signups (username, email, pw_hash, code_hash, expires_at, sent_at, ip)"
            " VALUES (?,?,?,?,?,?,?)",
            (name, email, hash_password(pw), _code_hash(name, code),
             (now + timedelta(minutes=VERIFY_TTL_MIN)).strftime("%Y-%m-%d %H:%M:%S"),
             now.strftime("%Y-%m-%d %H:%M:%S"), ip))
    return {"ok": True, "need_code": True, "email": mask_email(email),
            "ttl_min": VERIFY_TTL_MIN, "resend_after": VERIFY_RESEND_SEC}


class ResendReq(BaseModel):
    username: str = ""


@app.post("/api/register/resend")
def api_register_resend(req: ResendReq, request: Request):
    """
    重新发送注册验证码。

    **为什么需要这个接口**：验证码 10 分钟过期，过期后用户看到的是
    「请重新获取」—— 光有提示没有出口，他就只能点回「注册」把密码和邮箱
    重填一遍。这个接口只凭用户名就能重发，因为要用的资料（邮箱、口令哈希）
    服务端本来就存着，没必要让客户端再交一次。

    只能发给「待验证记录里那个邮箱」，改不了收件地址 —— 否则它就成了
    一个任意发信接口。
    """
    ip = _client_ip(request)
    if _login_locked(ip):
        raise HTTPException(429, f"尝试次数过多，请 {LOGIN_WINDOW // 60} 分钟后再试")
    if not MAIL_READY:
        raise HTTPException(503, "服务器还没有配置邮件服务")
    name = (req.username or "").strip().lower()

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM signups WHERE username=?", (name,)).fetchone()
    # 不区分「没这条记录」和「用户名不对」：这条接口是公开的
    if not row:
        raise HTTPException(404, "没有待验证的注册，请重新填写注册信息")

    wait = mail_throttle("signup:" + name)
    if wait:
        raise HTTPException(429, f"验证码刚发过，请 {wait} 秒后再试")

    code = "".join(secrets.choice("0123456789") for _ in range(6))
    ok, msg = _send_code(row["email"], name, code, "signup")
    if not ok:
        raise HTTPException(502, f"验证码发送失败：{msg}")

    now = datetime.now()
    with closing(get_conn()) as conn, conn:
        # 重发等于作废旧码，并把试错次数清零（否则用户试错几次后重发也没用）
        conn.execute(
            "UPDATE signups SET code_hash=?, expires_at=?, sent_at=?, attempts=0 WHERE id=?",
            (_code_hash(name, code),
             (now + timedelta(minutes=VERIFY_TTL_MIN)).strftime("%Y-%m-%d %H:%M:%S"),
             now.strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
    return {"ok": True, "email": mask_email(row["email"]), "ttl_min": VERIFY_TTL_MIN,
            "resend_after": VERIFY_RESEND_SEC}


@app.post("/api/register/verify")
def api_register_verify(req: VerifyReq, request: Request):
    """注册第二步：验码 → 建账号 → 直接登录。"""
    ip = _client_ip(request)
    name = (req.username or "").strip().lower()
    code = (req.code or "").strip()
    if _login_locked(ip):
        raise HTTPException(429, f"尝试次数过多，请 {LOGIN_WINDOW // 60} 分钟后再试")

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM signups WHERE username=?", (name,)).fetchone()
    # 统一的失败措辞：不区分「没这条待验证记录」和「码错了」
    bad = HTTPException(400, "验证码不正确或已过期，请重新获取")
    if not row or not code:
        _record_fail(ip)
        raise bad
    if row["attempts"] >= VERIFY_MAX_TRY:
        _record_fail(ip)
        raise HTTPException(429, "这个验证码试错太多次了，请重新获取")
    if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
        _record_fail(ip)
        raise bad
    if not hmac.compare_digest(row["code_hash"], _code_hash(name, code)):
        with closing(get_conn()) as conn, conn:
            conn.execute("UPDATE signups SET attempts = attempts + 1 WHERE id=?", (row["id"],))
        _record_fail(ip)
        raise bad

    with closing(get_conn()) as conn, conn:
        # 抢在并发之前再确认一次用户名没被占（两个请求同时验同一个码）
        if conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            conn.execute("DELETE FROM signups WHERE id=?", (row["id"],))
            raise HTTPException(409, "这个用户名已经被注册了")
        cur = conn.execute(
            "INSERT INTO users (username, pw_hash, email, created_at) VALUES (?,?,?,?)",
            (name, row["pw_hash"], row["email"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        uid = int(cur.lastrowid)
        conn.execute("DELETE FROM signups WHERE id=?", (row["id"],))
        # 第一个注册的账号认领升级上来的老数据
        claimed = 0
        if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1:
            claimed = conn.execute(
                "UPDATE mistakes SET user_id=? WHERE user_id=0", (uid,)).rowcount
            conn.execute("UPDATE chats SET user_id=? WHERE user_id=0", (uid,))

    _clear_fails(ip)
    return _session_response(uid, {"username": name, "claimed": claimed})


# ── 忘记密码 ────────────────────────────────────────────────────────────────
@app.post("/api/reset/request")
def api_reset_request(req: ResetReq, request: Request):
    """
    发重置验证码。

    ⚠️ 无论这个邮箱是否注册过，**返回完全一样**。否则这个接口就成了
    「查某个邮箱有没有注册」的免费查询器。
    """
    ip = _client_ip(request)
    if _login_locked(ip):
        raise HTTPException(429, f"尝试次数过多，请 {LOGIN_WINDOW // 60} 分钟后再试")
    if not MAIL_READY:
        raise HTTPException(503, "服务器还没有配置邮件服务，暂时无法重置密码。请联系管理员。")

    email = (req.email or "").strip()
    generic = {"ok": True, "msg": "如果这个邮箱注册过，验证码已经发出去了"}
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")

    with closing(get_conn()) as conn:
        u = conn.execute("SELECT id, username FROM users WHERE email=? AND email!=''",
                         (email,)).fetchone()
    wait = mail_throttle("reset:" + email)
    if wait:
        raise HTTPException(429, f"验证码刚发过，请 {wait} 秒后再试")

    if u:
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        ok, msg = _send_code(email, u["username"], code, "reset")
        if not ok:
            # 发信失败要让本人知道，不然他会一直等一封永远不来的邮件
            raise HTTPException(502, f"验证码发送失败：{msg}")
        now = datetime.now()
        with closing(get_conn()) as conn, conn:
            conn.execute("DELETE FROM signups WHERE username=? AND ip='reset'", (email,))
            conn.execute(
                "INSERT INTO signups (username, email, pw_hash, code_hash, expires_at, sent_at, ip)"
                " VALUES (?,?,?,?,?,?,?)",
                (email, email, "", _code_hash(email, code),
                 (now + timedelta(minutes=VERIFY_TTL_MIN)).strftime("%Y-%m-%d %H:%M:%S"),
                 now.strftime("%Y-%m-%d %H:%M:%S"), "reset"))
    return generic


@app.post("/api/reset/do")
def api_reset_do(req: ResetDoReq, request: Request):
    ip = _client_ip(request)
    if _login_locked(ip):
        raise HTTPException(429, f"尝试次数过多，请 {LOGIN_WINDOW // 60} 分钟后再试")
    email = (req.email or "").strip()
    code = (req.code or "").strip()
    new = req.new or ""
    if len(new) < MIN_PW_LEN:
        raise HTTPException(400, f"新密码至少 {MIN_PW_LEN} 位")

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM signups WHERE username=? AND ip='reset'",
                           (email,)).fetchone()
        u = conn.execute("SELECT id FROM users WHERE email=? AND email!=''", (email,)).fetchone()
    bad = HTTPException(400, "验证码不正确或已过期，请重新获取")
    if not row or not u or not code:
        _record_fail(ip); raise bad
    if row["attempts"] >= VERIFY_MAX_TRY:
        raise HTTPException(429, "这个验证码试错太多次了，请重新获取")
    if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
        _record_fail(ip); raise bad
    if not hmac.compare_digest(row["code_hash"], _code_hash(email, code)):
        with closing(get_conn()) as conn, conn:
            conn.execute("UPDATE signups SET attempts = attempts + 1 WHERE id=?", (row["id"],))
        _record_fail(ip); raise bad

    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(new), u["id"]))
        conn.execute("DELETE FROM signups WHERE id=?", (row["id"],))
    _clear_fails(ip)
    # 重置成功直接给会话：他已经证明了对邮箱的控制权，没必要再输一遍
    return _session_response(int(u["id"]), {"username": ""})


@app.get("/api/mail_policy")
def mail_policy():
    """登录页用它决定要不要禁用「注册 / 找回密码」——没配邮件就别让用户白填一堆。"""
    return {"mail_ready": MAIL_READY}


@app.get("/api/signup_policy")
def signup_policy():
    """登录页用它决定要不要显示「邀请码」输入框。不含邀请码本身，只说要不要。"""
    return {"invite_required": bool(SIGNUP_CODE)}


class PwReq(BaseModel):
    old: str = ""
    new: str = ""


@app.post("/api/password")
def api_change_password(req: PwReq):
    """改自己的密码。必须验旧密码 —— 否则会话被劫持就等于账号被夺。"""
    uid = current_uid()
    if len(req.new or "") < MIN_PW_LEN:
        raise HTTPException(400, f"新密码至少 {MIN_PW_LEN} 位")
    if len(req.new) > 200:
        raise HTTPException(400, "新密码太长了")
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT pw_hash FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not verify_password(req.old or "", row["pw_hash"]):
        time.sleep(0.4)
        raise HTTPException(401, "原密码不正确")
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(req.new), uid))
    return {"ok": True}


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/session")
def api_session():
    """前端用它拿当前用户名显示在顶栏。"""
    uid = current_uid()
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
    return {"ok": True, "ttl": SESSION_TTL,
            "username": row["username"] if row else "",
            "user_id": uid}


# ---------------------------- 路由：站点图标 ---------------------------------
# 与页面顶栏 #i-book 同一枚图标（靛蓝渐变圆角方块 + 白色书）。
# 走独立静态文件而不是 data: URI，是为了能 HTTP 缓存、且 iOS 的
# apple-touch-icon 只认真实 URL。
_ICON_FILES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon-192.png": ("favicon-192.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    # iOS 会先探这个「预合成」名字，缺了就回落到 apple-touch-icon.png。
    # 直接给同一份，省掉一次 404 往返。
    "/apple-touch-icon-precomposed.png": ("apple-touch-icon.png", "image/png"),
}


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon-192.png", include_in_schema=False)
@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def site_icon(request: Request):
    name, media = _ICON_FILES[request.url.path]
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "图标缺失")
    return FileResponse(path, media_type=media,
                        headers={"Cache-Control": "public, max-age=604800"})


# ---------------------------- 路由：首页 -------------------------------------
@app.get("/")
def index():
    if not os.path.exists(INDEX_FILE):
        raise HTTPException(500, "index.html 缺失，请确认与 main.py 在同一目录")
    # 必须禁用缓存：手机浏览器对 HTML 缓存很激进，改了前端却刷不出来会很难排查
    return FileResponse(
        INDEX_FILE,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


ANIM_NAME_RE = re.compile(r"^anim_m\d+_\d+\.html$")


@app.get("/anim/{name}")
def serve_animation(name: str):
    """
    提供 AI 生成的交互动画页面。

    安全（重要）：这段 HTML + JS 是模型生成的，会在浏览器里执行。若按普通同源页面提供，
    它就能带着用户的登录态去读写 /api/*（上传、删除、刷 AI 额度——而它的输入里包含
    用户上传的试卷图片，存在提示注入的可能）。这里用 CSP sandbox 把它降级成**不透明源**：

      · sandbox allow-scripts（**故意不给 allow-same-origin**）
        → 读不到 Cookie / localStorage，发出的同源请求也带不上登录态、读不到响应
      · default-src 'none' + connect-src 'none'
        → 掐断一切外部加载与网络请求，杜绝把数据外传
      · 只放行内联脚本/样式与 jsDelivr（动画要画图、可能要引 KaTeX）
    """
    if not ANIM_NAME_RE.match(name):
        raise HTTPException(404, "动画不存在")
    # 文件名形如 anim_m{错题id}_{时间戳}.html —— 从里面取出 mid 校验归属。
    # 不校验的话，登录用户遍历一下就能看别人题目的动画讲解。
    try:
        mid = int(name.split("_")[1][1:])
    except (IndexError, ValueError):
        raise HTTPException(404, "动画不存在")
    with closing(get_conn()) as conn:
        own = conn.execute("SELECT 1 FROM mistakes WHERE id=? AND user_id=?",
                           (mid, current_uid())).fetchone()
    if not own:
        raise HTTPException(404, "动画不存在")
    path = os.path.join(ANIM_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "动画不存在")
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": (
                "sandbox allow-scripts; "
                "default-src 'none'; "
                "script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src data: blob:; "
                "font-src data: https://cdn.jsdelivr.net; "
                "connect-src 'none'"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/health")
def health():
    return {"ok": True, "model": MODEL_NAME, "ai_ready": api_key_ready(), "subjects": SUBJECTS}


# ---------------------------- 路由：错题列表 ---------------------------------
@app.get("/api/mistakes")
def list_mistakes(subject: Optional[str] = None, order: str = "time_desc",
                  q: Optional[str] = None, kind: Optional[str] = None,
                  due: bool = False, limit: int = 500):
    """题目列表。支持科目 / 题型 / 待复习筛选、关键词搜索、三种排序。"""
    where, args = [], []
    if due:
        # 到期的（含从没排过复习的老数据）
        where.append("(review_due_at = '' OR review_due_at <= ?)")
        args.append(_today())
    if subject and subject != "全部":
        where.append("subject = ?")
        args.append(subject)
    if kind and kind in KINDS:
        where.append("kind = ?")
        args.append(kind)
    if q and q.strip():
        kw = f"%{q.strip()}%"
        where.append("(title LIKE ? OR tag LIKE ? OR clean_text LIKE ?)")
        args += [kw, kw, kw]

    # 归属过滤永远在最前面：后面无论怎么拼 where 都跑不掉
    where.insert(0, "user_id = ?")
    args.insert(0, current_uid())

    sql = "SELECT * FROM mistakes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += {
        "time_asc":  " ORDER BY created_at ASC, id ASC",
        "time_desc": " ORDER BY created_at DESC, id DESC",
        "subject":   " ORDER BY subject, id",
    }.get(order, " ORDER BY created_at DESC, id DESC")
    sql += " LIMIT ?"
    args.append(max(1, min(limit, 2000)))

    with closing(get_conn()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return {"items": [row_to_item(r) for r in rows], "total": len(rows)}


@app.get("/api/stats")
def stats():
    """各科目 / 各题型数量（导出弹窗和顶部计数用）。"""
    with closing(get_conn()) as conn:
        uid = current_uid()
        rows = conn.execute(
            "SELECT subject, COUNT(*) AS n FROM mistakes WHERE user_id=? GROUP BY subject",
            (uid,)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM mistakes WHERE user_id=?", (uid,)).fetchone()["n"]
        krows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM mistakes WHERE user_id=? GROUP BY kind",
            (uid,)).fetchall()
        # 科目 × 题型 的交叉计数。只给「各题型总数」是不够的：筛了「语文」之后，
        # 题型按钮上还挂着全库的数字，看起来就像筛选没生效。
        xrows = conn.execute(
            "SELECT subject, kind, COUNT(*) AS n FROM mistakes WHERE user_id=? GROUP BY subject, kind",
            (uid,)
        ).fetchall()
    # 本周问 AI 的次数：报告建议每周 1~2 次效果最好，多了要温和提醒
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    asked = conn2 = None
    with closing(get_conn()) as c2:
        asked = c2.execute(
            # chats 没有 user 列，归属靠 join 到 mistakes
            "SELECT COUNT(*) AS n FROM chats c JOIN mistakes m ON m.id = c.mistake_id"
            " WHERE m.user_id = ? AND c.role='user' AND c.created_at >= ?",
            (uid, week_ago)).fetchone()["n"]
    with closing(get_conn()) as c2:
        due_count = c2.execute(
            "SELECT COUNT(*) AS n FROM mistakes WHERE user_id = ?"
            " AND (review_due_at = '' OR review_due_at <= ?)",
            (uid, _today())).fetchone()["n"]
    by_kind = {k: 0 for k in KINDS}
    for r in krows:
        by_kind[norm_kind(r["kind"])] += r["n"]
    by_subject_kind: Dict[str, Dict[str, int]] = {}
    for r in xrows:
        d = by_subject_kind.setdefault(r["subject"], {k: 0 for k in KINDS})
        d[norm_kind(r["kind"])] += r["n"]
    return {"total": total, "by_subject": {r["subject"]: r["n"] for r in rows},
            "by_kind": by_kind, "by_subject_kind": by_subject_kind,
            "due": due_count, "week_asked": asked}


# ---------------------------- 路由：题目图片（按归属鉴权）--------------------
def thumb_path(mid: int) -> str:
    return os.path.join(THUMB_DIR, f"thumb_{mid}.jpg")


def drop_thumb(mid: int) -> None:
    """
    删掉这道题的缩略图缓存。

    删题时必须跟着删：SQLite 的 rowid 会**复用**（删掉最大 id 再新增，
    新题会拿到同一个 id），留下的旧缩略图就会挂到新题上 —— 列表里出现
    一道根本不存在的题目，而且是孩子别的作业的照片。
    """
    try:
        os.remove(thumb_path(mid))
    except OSError:
        pass


def ensure_thumb(mid: int, src_abs: str) -> str:
    """
    按需生成缩略图并落盘，返回文件路径（失败返回空串）。

    为什么不在上传时就生成：老数据没有缩略图，补一遍要写迁移脚本。
    按需生成 + 落盘，第一次访问算一次，之后都是直接读文件 —— 两边的活都省了。
    """
    dst = thumb_path(mid)
    if os.path.exists(dst):
        try:
            # 光判断「文件在不在」不够：reclean_all()（python3 main.py --reclean）
            # 是**原地覆盖** clean 图的 —— 路径没变、内容变了。
            # 那样列表会一直显示旧图，而点进去的详情页已经是新的。
            if os.stat(dst).st_mtime >= os.stat(src_abs).st_mtime:
                return dst
        except OSError:
            pass          # 原图没了就往下走，让下面的 open 去报错并退回大图
    try:
        with Image.open(src_abs) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82, optimize=True)
        # 目录**必须在这里兜一次**，不能只靠启动时那次 makedirs：
        # 目录被删掉（清理脚本、手工 rm、磁盘操作）之后，写文件会失败，
        # 而下面的 except 会把失败吞掉、退回大图 —— 表现是「列表又变慢了」，
        # 但接口照常返回 200，从日志上完全看不出来。
        os.makedirs(THUMB_DIR, exist_ok=True)
        tmp = dst + ".tmp"
        with open(tmp, "wb") as f:
            f.write(buf.getvalue())
        os.replace(tmp, dst)          # 原子替换：并发请求不会读到写了一半的文件
        return dst
    except Exception as e:
        # 退回大图是**对的**（宁可慢也别让卡片开天窗），但不能一声不吭：
        # 目录被删掉那回就是这么藏住的 —— 接口照常 200，唯一的现象是「又变慢了」。
        print(f"⚠️ 缩略图生成失败 #{mid}：{type(e).__name__}: {e}")
        return ""


@app.get("/media/thumb/{mid}")
def serve_thumb(mid: int):
    """列表卡片用的小图。归属校验跟大图一样。"""
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT orig_path, clean_path FROM mistakes WHERE id=? AND user_id=?",
            (mid, current_uid())).fetchone()
    if not row:
        raise HTTPException(404, "不存在")
    src = row["clean_path"] or row["orig_path"]
    if not src:
        raise HTTPException(404, "这道题没有图")
    src_abs = os.path.join(STATIC_DIR, src)
    if not os.path.exists(src_abs):
        raise HTTPException(404, "图片文件缺失")
    path = ensure_thumb(mid, src_abs)
    if not path:
        # 缩略图生成失败就退回大图，宁可慢也别让卡片开天窗
        return FileResponse(src_abs, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@app.get("/media/{kind}/{mid}")
def serve_media(kind: str, mid: int):
    """
    原图 / 去红笔图。**这是多用户下最容易出事的一个口子。**

    原来直接 mount 了 StaticFiles：只要登录就能按 URL 拿到任意文件，
    而文件名是 original_{id}.jpg、id 连续自增 —— 换个数字就是别人的照片。
    现在改成先查这道题属不属于当前用户，不属于一律 404（不是 403：
    403 等于确认「这个 id 存在，只是不给你看」，那也是信息泄露）。
    """
    if kind not in ("origin", "clean"):
        raise HTTPException(404, "不存在")
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT orig_path, clean_path FROM mistakes WHERE id=? AND user_id=?",
            (mid, current_uid())).fetchone()
    if not row:
        raise HTTPException(404, "不存在")
    rel = row["orig_path"] if kind == "origin" else row["clean_path"]
    if not rel:
        raise HTTPException(404, "这道题没有这张图")
    path = os.path.join(STATIC_DIR, rel)
    if not os.path.exists(path):
        raise HTTPException(404, "图片文件缺失")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------- 路由：上传错题 ---------------------------------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...), subject: str = Form("数学")):
    """
    双轨制 AI 错题处理主流程：
      1. 版本A：原图标准化落盘 static/origin/original_[id].jpg（保留手写 + 红笔批改）
      2. 版本B-图片：OpenCV 去红笔 + 白纸化 -> static/clean/clean_[id].jpg
      3. 版本B-文字 + 考点 + 变式题 + 解析：deepseek-flash 一次多模态调用产出
      4. 全部写入 SQLite 并返回
    """
    if subject not in SUBJECTS:
        raise HTTPException(400, f"科目必须是：{'、'.join(SUBJECTS)}")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "上传的文件为空")
    if len(raw) > 40 * 1024 * 1024:
        raise HTTPException(400, "图片过大（>40MB），请用手机默认相机压缩后再传")

    # 先把图交给 AI（AI 才是「看过这张图」的一方），再统一落库。
    # 落库这一步和文档上传共用 _persist_item —— 两条路各写一遍的话，
    # 以后加字段（比如这次的 source）必然漏掉一边。
    with tempfile.TemporaryDirectory(prefix="up_") as td:
        tmp_img = os.path.join(td, "in.jpg")
        try:
            normalize_and_save(raw, tmp_img)
        except Exception as e:
            raise HTTPException(400, f"图片解析失败，请换一张 JPG/PNG 图片重试（{e}）")
        ai = call_deepseek(tmp_img, subject)
        with open(tmp_img, "rb") as f:
            norm_bytes = f.read()

    # AI 的科目判断比用户手选更可信：前端在「全部」筛选下上传时科目会默认成某一种，
    # 很容易把语文题归到数学，这里若标签形如「语文-xxx」且与所选不符，就按 AI 的纠正过来。
    final_subject, fixed_from = subject, None
    m = re.match(r"^\s*(语文|数学|英语|物理|化学|历史|道德与法治)\s*[-－—]", ai["tag"] or "")
    if m and m.group(1) != subject:
        final_subject, fixed_from = m.group(1), subject

    try:
        it, clean_ok = _persist_item(raw_image=norm_bytes, ai=ai,
                                     subject=final_subject, source="照片")
    except Exception as e:
        raise HTTPException(400, f"图片保存失败：{e}")

    return {
        "ok": True,
        "item": it,
        "clean_image_ok": clean_ok,         # 去红笔是否真的成功（失败时版本B复用原图）
        "subject_fixed_from": fixed_from,   # 非空表示 AI 纠正了归档科目
    }


# ---------------------------- 路由：上传文档（PDF/Word/文本）--------------------
@app.post("/api/upload_doc")
async def upload_doc(file: UploadFile = File(...), subject: str = Form("数学")):
    """
    文档上传：一份 PDF / Word / 文本 -> 拆成一道一道的题 -> 一题一条。

    为什么走 SSE 而不是普通 POST：一份 10 页的卷子要调 10 次 AI，可能要几分钟。
    普通 POST 只能干等到最后，用户看到的是「转圈」；SSE 能把「正在分析第 3/10 页，
    已入库 7 道」实时推出来，等待才不焦虑。

    ⚠️ 每页一次 AI 调用是**串行**的：并行发十几个请求容易被限流，
    而且失败时不好定位是哪一页出的问题。
    """
    if subject not in SUBJECTS:
        subject = "数学"
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "上传的文件为空")
    if len(raw) > DOC_MAX_BYTES:
        raise HTTPException(400, f"文件过大（>{DOC_MAX_BYTES // 1024 // 1024}MB）")
    if not api_key_ready():
        raise HTTPException(503, "后端未配置 DEEPSEEK_API_KEY，无法识别题目")

    fname = file.filename or "未命名"

    def gen():
        def ev(obj: Dict[str, Any]) -> str:
            return _sse(obj)

        try:
            yield ev({"type": "stage", "stage": "convert",
                      "message": "正在解析文件…"})
            try:
                pages = doc_to_pages(raw, fname)
            except Exception as e:
                yield ev({"type": "error", "message": f"文件解析失败：{e}"})
                return
            if not pages:
                yield ev({"type": "error", "message": "这个文件里没有可识别的页面"})
                return

            suffix = f"《{os.path.splitext(fname)[0][:24]}》"
            multi = len(pages) > 1
            yield ev({"type": "start", "doc": fname, "total_pages": len(pages),
                      "message": f"共 {len(pages)} 页，开始逐页拆题…"})

            created: List[Dict[str, Any]] = []
            failed: List[Dict[str, Any]] = []

            for pi, page in enumerate(pages, 1):
                label = f"{suffix}第 {pi} 页" if multi else suffix
                yield ev({"type": "page", "page": pi, "total": len(pages),
                          "message": f"正在分析第 {pi}/{len(pages)} 页…"})
                try:
                    problems = call_split_page(page)
                except Exception as e:
                    failed.append({"page": pi, "reason": str(e)})
                    yield ev({"type": "page_error", "page": pi, "message": str(e)})
                    continue

                if not problems:
                    yield ev({"type": "page_empty", "page": pi})
                    continue

                for p in problems:
                    # 每道题按 AI 给的纵向范围裁出来当「原图」——不裁的话，
                    # 同一页上的三道题会共用一张整页图，分屏对比时根本看不出是哪道。
                    img = None
                    if page["kind"] == "image":
                        # 默认整页直接用（见 DOC_AUTO_CROP 的说明）
                        img = (crop_page(page["raw"], p.get("top"), p.get("bottom"))
                               if DOC_AUTO_CROP else page["raw"])
                    tag = p["tag"]
                    m = re.match(r"^\s*(语文|数学|英语|物理|化学|历史|道德与法治)\s*[-－—]", tag or "")
                    subj = m.group(1) if m else subject
                    try:
                        it, _ = _persist_item(
                            raw_image=img,
                            ai={"_status": "ok", "kind": p["kind"], "tag": tag,
                                "analysis": p["analysis"], "clean_text": p["clean_text"]},
                            subject=subj, source=label)
                    except Exception as e:
                        failed.append({"page": pi, "reason": f"入库失败：{e}"})
                        continue
                    created.append({"id": it["id"], "subject": it["subject"],
                                    "kind": it["kind"], "kind_label": it["kind_label"],
                                    "tag": it["tag"], "title": it["title"],
                                    "page": pi})
                    yield ev({"type": "item", "page": pi, "item": created[-1],
                              "count": len(created)})

                yield ev({"type": "page_done", "page": pi, "total": len(pages),
                          "found": len(created)})

            yield ev({"type": "done", "created": created, "failed": failed,
                      "checked_pages": len(pages) - len(failed)})
        except GeneratorExit:
            # 浏览器断开（用户切页/刷新）：已经入库的题目照样保留，不白花 token
            raise
        except Exception as e:
            yield ev({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ---------------------------- 路由：按需生成变式题 ---------------------------
@app.post("/api/variant/{mid}")
def make_variant(mid: int, force: bool = False):
    """
    生成「同考点变式题」。已经有且非 force 就直接返回，不重复花钱。
    上传时不再预生成，所以老数据可能为空 —— 这是预期行为。
    """
    if not api_key_ready():
        raise HTTPException(503, "后端未配置 DEEPSEEK_API_KEY，无法生成变式题")
    with closing(get_conn()) as conn:
        row = owned_mistake(conn, mid)
    if not row:
        raise HTTPException(404, "错题不存在")
    it = row_to_item(row)
    if it.get("variant_q") and not force:
        return {"ok": True, "cached": True, "variant_q": it["variant_q"], "variant_a": it["variant_a"]}

    try:
        v = call_variant(it)
    except Exception as e:
        raise HTTPException(502, f"生成变式题失败：{e}")
    with closing(get_conn()) as conn, conn:
        conn.execute("UPDATE mistakes SET variant_q=?, variant_a=? WHERE id=?",
                     (v["variant_question"], v["variant_analysis"], mid))
    return {"ok": True, "cached": False, "variant_q": v["variant_question"], "variant_a": v["variant_analysis"]}


# ---------------------------- 路由：变式题结果 / 复习调度 ----------------------
class ReviewReq(BaseModel):
    # right = 做对了 / wrong = 做错了 / skip = 还没做（撤回到未记录状态）
    result: str


@app.post("/api/mistakes/{mid}/review")
def record_review(mid: int, req: ReviewReq):
    """
    记录变式题「做没做、做对没做对」，并据此排下一次复习。

    这是全应用**唯一**能证伪「虚假精通」的信号：在此之前，孩子看完 AI 的讲解
    觉得懂了、和真的能独立做出来，在数据库里长得一模一样。
    现在的规则很朴素：
        做对 → 进一级，间隔拉长（1→3→7→16→35 天）
        做错 → 归零，明天再来
        还没做 → 撤回记录，但不改变已排的复习
    """
    if req.result not in ("right", "wrong", "skip"):
        raise HTTPException(400, "result 只能是 right / wrong / skip")

    with closing(get_conn()) as conn, conn:
        row = conn.execute(
            "SELECT review_stage FROM mistakes WHERE id=? AND user_id=?",
            (mid, current_uid())).fetchone()
        if not row:
            raise HTTPException(404, "错题不存在")
        stage = int(row["review_stage"] or 0)

        if req.result == "skip":
            # 撤销误点：只清结果，不动复习排期
            conn.execute(
                "UPDATE mistakes SET variant_result='', variant_done_at='' WHERE id=?", (mid,))
            new_stage, due = stage, None
        else:
            if req.result == "right":
                new_stage = min(stage + 1, len(REVIEW_INTERVALS) - 1)
            else:
                new_stage = 0
            days = REVIEW_INTERVALS[new_stage]
            due = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            conn.execute(
                "UPDATE mistakes SET variant_result=?, variant_done_at=?,"
                " review_stage=?, review_due_at=? WHERE id=?",
                (req.result, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 new_stage, due, mid))

        r = conn.execute("SELECT * FROM mistakes WHERE id=?", (mid,)).fetchone()

    return {"ok": True, "item": row_to_item(r),
            "stage": int(r["review_stage"] or 0),
            "next_due": r["review_due_at"],
            "interval_days": REVIEW_INTERVALS[int(r["review_stage"] or 0)]}


# ---------------------------- 路由：修改科目 ---------------------------------
class SubjectReq(BaseModel):
    subject: Optional[str] = None
    # 题型：AI 判错了可以改成另一个。不改的字段传 None 即可。
    kind: Optional[str] = None


@app.patch("/api/mistakes/{mid}")
def update_mistake(mid: int, req: SubjectReq):
    """
    改归档科目 / 题型。上传时这两项都由 AI 从图上自动识别，这条接口是给
    「AI 认错了」兜底的 —— 与其在上传前让用户先选一遍（AI 本来就要认），
    不如认错了再改。
    """
    sets, args = [], []
    if req.subject is not None:
        if req.subject not in SUBJECTS:
            raise HTTPException(400, f"科目必须是：{'、'.join(SUBJECTS)}")
        sets.append("subject=?")
        args.append(req.subject)
    if req.kind is not None:
        if req.kind not in KINDS:
            raise HTTPException(400, f"题型必须是：{'、'.join(KINDS)}")
        sets.append("kind=?")
        args.append(req.kind)
    if not sets:
        raise HTTPException(400, "没有要修改的字段")

    with closing(get_conn()) as conn, conn:
        if not owned_mistake(conn, mid):
            raise HTTPException(404, "错题不存在")
        args.append(mid)
        conn.execute(f"UPDATE mistakes SET {', '.join(sets)} WHERE id=?", args)
    return {"ok": True, "id": mid, "subject": req.subject, "kind": req.kind}


# ---------------------------- 路由：删除错题 ---------------------------------
@app.delete("/api/mistakes/{mid}")
def delete_mistake(mid: int):
    with closing(get_conn()) as conn, conn:
        row = owned_mistake(conn, mid)
        if not row:
            raise HTTPException(404, "错题不存在")
        for rel in (row["orig_path"], row["clean_path"]):
            if rel:
                p = os.path.join(STATIC_DIR, rel)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        drop_thumb(mid)
        conn.execute("DELETE FROM mistakes WHERE id = ?", (mid,))
    return {"ok": True, "deleted": mid}


# ---------------------------- 路由：AI 对话答疑 ------------------------------
class ChatReq(BaseModel):
    id: int
    message: str = ""
    # guide=引导模式 / full=完整讲解 / ""=没指定，由服务端按场景取默认值：
    #   错题答疑默认 guide（先问后答，这是本应用的核心设计），
    #   自由问答默认 full（学生问的是知识性问题，反问他没有意义）。
    mode: str = ""
    # 这一轮是不是「生成动画」。由前端显式声明，**不做关键词猜测**：
    # 猜错了要么把动画截断（当成问答档），要么让普通问答白等 10 分钟（当成动画档），
    # 两个方向都很糟。前端点的是哪个按钮，它自己最清楚。
    want_anim: bool = False


@app.get("/api/chat/{mid}")
def get_chat(mid: int):
    uid = current_uid()
    with closing(get_conn()) as conn:
        # 自由问答（0 号）没有对应的错题，天然没有归属可查；其余必须校验。
        # 两种情况都要按 user_id 过滤 —— 0 号线程是所有用户共用的哨兵值。
        if mid != GENERAL_CHAT_ID and not owned_mistake(conn, mid):
            raise HTTPException(404, "错题不存在")
        rows = conn.execute(
            f"SELECT {_CHAT_COLS} FROM chats WHERE mistake_id=? AND user_id=? ORDER BY id",
            (mid, uid)).fetchall()
    return {"messages": [dict(r) for r in rows]}


@app.delete("/api/chat/{mid}")
def clear_chat(mid: int):
    uid = current_uid()
    with closing(get_conn()) as conn, conn:
        if mid != GENERAL_CHAT_ID and not owned_mistake(conn, mid):
            raise HTTPException(404, "错题不存在")
        conn.execute("DELETE FROM chats WHERE mistake_id=? AND user_id=?", (mid, uid))
    purge_animations(mid)          # 动画文件也一并清掉
    return {"ok": True}


def _sse(obj: Dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _load_chat_context(req: ChatReq):
    """
    校验请求并取出错题与历史对话。校验失败直接抛 HTTPException。

    req.id == GENERAL_CHAT_ID(0) 时是自由问答：it 返回 None，不查 mistakes 表。
    """
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(400, "请先输入你的问题")
    if len(msg) > 2000:
        raise HTTPException(400, "问题太长了，请精简到 2000 字以内")
    if not api_key_ready():
        raise HTTPException(503, "后端未配置 DEEPSEEK_API_KEY，AI 答疑不可用")

    general = (req.id == GENERAL_CHAT_ID)
    with closing(get_conn()) as conn:
        if general:
            it = None
        else:
            row = owned_mistake(conn, req.id)
            if not row:
                raise HTTPException(404, "错题不存在")
            it = row_to_item(row)
        hist = conn.execute(
            "SELECT role, content FROM chats WHERE mistake_id=? AND user_id=? ORDER BY id",
            (req.id, current_uid())).fetchall()

    mode = req.mode if req.mode in ("guide", "full") else ("full" if general else "guide")
    return it, [dict(h) for h in hist][-CHAT_MAX_TURNS:], msg, mode


_CHAT_COLS = "role, content, created_at, anim_url, anim_title, mode"


def _chat_begin(mid: int, user_msg: str) -> int:
    """
    开一轮对话：插入用户消息 + 一条空的助手行，返回助手行的 id。
    之后边生成边 update 这一行。

    为什么要「边生成边存」而不是结束了一次性写：
    原来靠 `except GeneratorExit` 兜「客户端断开」，但那条路**根本不会触发** ——
    starlette 的 StreamingResponse 把同步生成器丢进线程池迭代，客户端断开时
    只是不再消费，并不会 close 生成器，所以 GeneratorExit 永远不会抛出。
    结果：用户等了两分钟、看到一半回答、一刷新全没了，token 也白烧。
    实测确认过：掐断 curl 后数据库一个字都没多。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO chats (mistake_id, role, content, created_at, user_id)"
            " VALUES (?,?,?,?,?)",
            (mid, "user", user_msg, now, current_uid()),
        )
        cur = conn.execute(
            "INSERT INTO chats (mistake_id, role, content, created_at, user_id)"
            " VALUES (?,?,?,?,?)",
            (mid, "assistant", "", now, current_uid()),
        )
        return int(cur.lastrowid)


def _chat_update(row_id: int, content: str,
                 anim: Optional[Dict[str, str]] = None, mode: str = "") -> None:
    anim = anim or {}
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE chats SET content=?, anim_url=?, anim_title=?, mode=? WHERE id=?",
            (content, anim.get("url", ""), anim.get("title", ""), mode, row_id),
        )


def owned_mistake(conn, mid: int):
    """
    取一道**属于当前用户**的题；不是自己的就当作不存在。

    所有按 mistake_id 操作的接口都必须先过这一关 —— chats 表本身没有 user 列，
    它的归属完全靠所属错题，直接按 mistake_id 查会读到别人的对话。
    """
    return conn.execute(
        "SELECT * FROM mistakes WHERE id=? AND user_id=?", (mid, current_uid())).fetchone()


def _chat_rows(mid: int) -> List[Dict[str, Any]]:
    with closing(get_conn()) as conn:
        rows = conn.execute(
            f"SELECT {_CHAT_COLS} FROM chats WHERE mistake_id=? AND user_id=? ORDER BY id",
            (mid, current_uid())).fetchall()
    return [dict(r) for r in rows]


def _append_chat(mid: int, user_msg: str, reply: str,
                 anim: Optional[Dict[str, str]] = None,
                 mode: str = "") -> List[Dict[str, Any]]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    anim = anim or {}
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO chats (mistake_id, role, content, created_at, user_id)"
            " VALUES (?,?,?,?,?)",
            (mid, "user", user_msg, now, current_uid()),
        )
        conn.execute(
            "INSERT INTO chats (mistake_id, role, content, created_at, anim_url, anim_title, mode,"
            " user_id) VALUES (?,?,?,?,?,?,?,?)",
            (mid, "assistant", reply, now, anim.get("url", ""), anim.get("title", ""), mode,
             current_uid()),
        )
        rows = conn.execute(
            f"SELECT {_CHAT_COLS} FROM chats WHERE mistake_id=? AND user_id=? ORDER BY id",
            (mid, current_uid())).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/chat")
def post_chat(req: ChatReq):
    """非流式答疑（留给脚本 / curl；网页端走 /api/chat/stream 以获得逐字输出）。"""
    it, history, msg, mode = _load_chat_context(req)
    try:
        reply = call_tutor(it, history, msg, mode, "anim" if req.want_anim else "chat")
    except Exception as e:
        raise HTTPException(502, f"AI 答疑失败：{e}")

    # 同样要把动画围栏剥出来，别把源码甩给调用方
    parser = AnimFenceParser()
    clean, raw = parser.feed(reply)
    tail, raw2 = parser.close()
    clean = strip_manual_steps((clean + tail).strip())
    raw = raw or raw2
    anim = save_animation(req.id, raw["title"], raw["html"]) if raw else None
    return {"reply": clean, "messages": _append_chat(req.id, msg, clean, anim, mode)}


@app.post("/api/chat/stream")
def chat_stream(req: ChatReq):
    """
    流式答疑（SSE）。事件类型：
        reasoning  模型思考过程片段 —— 最先到达，把「干等 20 秒」变成「看得见的过程」
        delta      正式回答片段
        done       结束，附带完整对话记录
        error      出错说明
    """
    it, history, msg, mode = _load_chat_context(req)
    # 动画档：不限制思考、时限放宽到 10 分钟（见 AI_PROFILES 的说明）
    profile = "anim" if req.want_anim else "chat"

    def gen():
        acc: List[str] = []          # 累积正文
        rbuf: List[str] = []         # 思考缓冲
        cbuf: List[str] = []         # 正文缓冲
        last_emit = time.time()
        anim_saved: Optional[Dict[str, str]] = None
        # 一轮对话的「空壳」在**动手调模型之前**就落库：这样哪怕孩子在思考阶段
        # 就关了页面，他问过什么也不会丢（实测过：模型光思考不吐正文时，
        # 掐断连接等于整轮对话凭空消失，连问题都得重打一遍）。
        row_id = _chat_begin(req.id, msg)
        last_save = 0.0

        def save_partial(force: bool = False) -> None:
            """
            把已经生成的部分写进数据库。

            **必须边生成边写**，不能等结束再一次写：客户端断开时 starlette
            只会停止消费同步生成器，不会 close 它，GeneratorExit 永远不触发
            （下面那个 except GeneratorExit 是防服务端自己关停的，
            挡不住「用户关页面 / 刷新 / 点停止」）。实测过：掐断连接后库里一个字都没有。
            节流 2 秒一次，SQLite 上这点写入量可以忽略。
            """
            nonlocal last_save
            text = "".join(acc).strip()
            if not text:
                return
            now = time.time()
            if not force and now - last_save < 2.0:
                return
            _chat_update(row_id, text, anim_saved, mode)
            last_save = now

        def finalize(note: str = "") -> None:
            """收尾：无论从哪条路退出，都把这一轮完整落库一次，不留半截状态。"""
            text = "".join(acc).strip()
            _chat_update(row_id, text + note, anim_saved, mode)

        def drain(force: bool = False) -> List[str]:
            """
            模型吐出的 delta 细到单字符（实测一次回答 2000+ 个）。
            每个 SSE 事件有约 40 字节协议开销，逐字转发等于为 2KB 的回答付 80KB 网络，
            浏览器端还会触发 2000 次重渲染。这里攒够 24 字或隔 60ms 才合并发一次。
            """
            nonlocal last_emit
            out: List[str] = []
            for kind, buf in (("reasoning", rbuf), ("delta", cbuf)):
                if not buf:
                    continue
                if force or sum(len(x) for x in buf) >= 24 or (time.time() - last_emit) >= 0.06:
                    out.append(_sse({"type": kind, "text": "".join(buf)}))
                    buf.clear()
            if out:
                last_emit = time.time()
            return out

        try:
            for chunk in call_tutor_stream(it, history, msg, mode, profile):
                t = chunk["type"]
                if t == "reasoning":
                    rbuf.append(chunk["text"])
                elif t == "delta":
                    cbuf.append(chunk["text"])
                    acc.append(chunk["text"])
                    save_partial()
                elif t == "anim":
                    # 先把已攒的文字吐干净，再落盘动画并推链接事件
                    for e in drain(True):
                        yield e
                    saved = save_animation(req.id, chunk["title"], chunk["html"])
                    if saved:
                        anim_saved = saved
                        yield _sse({"type": "anim", "title": saved["title"], "url": saved["url"]})
                        continue
                    # 存不下来（太短/不成型）：原样塞回缓冲吐给用户，绝不静默吞掉内容
                    raw = f"\n```html\n{chunk['html']}\n```\n"
                    cbuf.append(raw)
                    acc.append(raw)
                elif t == "deadline":
                    # 超时中断。**已经想出来的正文要留住** —— 白等了几分钟还全丢掉是最糟的。
                    # 有正文就正常收尾（附一句说明），一个字都没有才报错。
                    for e in drain(True):
                        yield e
                    note = f"\n\n（{chunk['text']}。上面是已经生成的部分，可以先看看；" \
                           f"想继续就把问题问得更具体一点，比如只问某一步。）"
                    if "".join(acc).strip():
                        yield _sse({"type": "delta", "text": note})
                        finalize(note)
                        yield _sse({"type": "done", "messages": _chat_rows(req.id)})
                    else:
                        # 光思考没正文：问题的壳已经存了，这里给个诚实的交代
                        finalize(f"（{chunk['text']}，还没来得及写出内容。换个更具体的问法会快很多，"
                                 f"比如只问某一步、某一问。）")
                        yield _sse({"type": "error",
                                    "message": f"{chunk['text']}。换个更具体的问法会快很多，"
                                               f"比如只问某一步、某一问。"})
                    return
                elif t == "error":
                    for e in drain(True):
                        yield e
                    finalize(f"（生成失败：{chunk['text']}）" if not "".join(acc).strip() else "")
                    yield _sse({"type": "error", "message": chunk["text"]})
                    return
                for e in drain():
                    yield e

            for e in drain(True):
                yield e
            full = "".join(acc).strip()
            if not full:
                finalize("（模型这次没有返回内容，可以再问一次。）")
                yield _sse({"type": "error", "message": "模型没有返回正文"})
                return
            finalize()
            yield _sse({"type": "done", "messages": _chat_rows(req.id)})
        except GeneratorExit:
            # 服务端自己关停（重启/取消）时会走到这里。**用户断开不会走到** ——
            # 那种情况靠 save_partial() 的边生成边存兜住，见它的说明。
            save_partial(True)
            raise
        except Exception as e:
            # 兜底：任何没预料到的异常也别把已经生成的内容弄丢
            try:
                finalize(f"（出错了：{type(e).__name__}）" if not "".join(acc).strip() else "")
            except Exception:
                pass
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 万一以后挂 nginx 反代，别让它把 SSE 缓冲成一坨再吐
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------- 路由：导出 A4 PDF ------------------------------
@app.post("/api/export_pdf")
def export_pdf(req: ExportReq):
    items = fetch_for_export(req)
    if not items:
        raise HTTPException(404, "没有符合条件的错题（请检查导出范围，或先勾选几道）")
    if len(items) > 200:
        raise HTTPException(400, f"一次最多导出 200 道（当前 {len(items)} 道），请缩小范围或分批")

    content = req.content if req.content in ("redo", "orig", "clean", "both") else "redo"
    layout = "compact" if req.layout == "compact" else "roomy"
    pdf_bytes = build_pdf(
        items, content=content, layout=layout,
        with_variant=req.with_variant, with_answer_page=req.with_answer_page,
        title=req.title,
    )
    # 文件名用纯 ASCII，避免 Content-Disposition 的编码坑
    ascii_tag = {"redo": "redo", "orig": "review", "clean": "clean", "both": "compare"}[content]
    fname = f"cuoti_{ascii_tag}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length": str(len(pdf_bytes)),
            "X-Item-Count": str(len(items)),
            "X-Content-Mode": content,
            "X-Layout": layout,
            "Access-Control-Expose-Headers": "Content-Disposition, X-Item-Count, X-Content-Mode, X-Layout",
        },
    )


# =============================================================================
# 八、启动入口
# =============================================================================

def reclean_all() -> None:
    """
    用当前算法重跑所有错题的「版本B-去红笔图」，**不重新调用 AI、不花额度**。
    调整 make_clean_image 的参数后，用它刷新历史数据即可。
        python3 main.py --reclean
    """
    init_db()
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT id, orig_path, clean_path FROM mistakes WHERE user_id=?",
                            (current_uid(),)).fetchall()
    if not rows:
        print("数据库里还没有错题。")
        return
    ok = fail = 0
    for r in rows:
        src = os.path.join(STATIC_DIR, r["orig_path"] or "")
        if not r["orig_path"] or not os.path.exists(src):
            print(f"  #{r['id']}  ❌ 原图缺失，跳过")
            fail += 1
            continue
        dst_rel = r["clean_path"] or f"clean/clean_{r['id']}.jpg"
        dst = os.path.join(STATIC_DIR, dst_rel)
        good, msg = make_clean_image(src, dst)
        print(f"  #{r['id']}  {'✅ 已重新生成' if good else '❌ ' + msg}")
        if good:
            ok += 1
            # 列表缩略图是另存的一份，必须跟着失效 —— 否则详情页已经是新图，
            # 列表中那个 56px 的小图还是旧的（浏览器那边还缓存了一天）。
            drop_thumb(r["id"])
            if not r["clean_path"]:
                with closing(get_conn()) as conn, conn:
                    conn.execute("UPDATE mistakes SET clean_path=? WHERE id=?", (dst_rel, r["id"]))
        else:
            fail += 1
    print(f"\n完成：成功 {ok} 张，失败 {fail} 张。")


if __name__ == "__main__":
    import sys
    if "--reclean" in sys.argv:
        reclean_all()
    else:
        # host 必须是 0.0.0.0，手机才能通过局域网 IP 访问
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info",
                    timeout_keep_alive=KEEPALIVE_SEC)
