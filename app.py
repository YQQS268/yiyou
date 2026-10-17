#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 怡悠（yiyou）· AI 恋人陪伴智能体
# Copyright (c) 2026 YQQS268
# 许可：个人/非商业使用，详见 LICENSE。
"""怡悠 —— 本地心理陪伴智能体后端（纯 Python 标准库，零第三方依赖）"""

import json
import os
import re
import shutil
import threading
import time
import urllib.request
import urllib.error
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PERSONA_PATH = os.path.join(BASE_DIR, "persona.md")
MEMORY_PATH = os.path.join(BASE_DIR, "yiyou.md")
LOG_PATH = os.path.join(BASE_DIR, "yiyou.log.md")
BACKUP_PATH = os.path.join(BASE_DIR, "yiyou.backup.md")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
PORT = 8787

# 头像等静态图片：仅允许 BASE_DIR 下的图片文件（防路径遍历）
IMG_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 情感模块：与 app.py 同目录（开源版平铺布局）
EMOTION_DIR = BASE_DIR
if EMOTION_DIR not in sys.path:
    sys.path.insert(0, EMOTION_DIR)
import emotion  # noqa: E402

HISTORY_START = "<!-- HISTORY_START -->"
HISTORY_END = "<!-- HISTORY_END -->"
NOTES_START = "<!-- NOTES_START -->"
NOTES_END = "<!-- NOTES_END -->"
SUMMARY_START = "<!-- SUMMARY_START -->"
SUMMARY_END = "<!-- SUMMARY_END -->"
PROFILE_START = "<!-- PROFILE_START -->"
PROFILE_END = "<!-- PROFILE_END -->"
DEFERRED_START = "<!-- DEFERRED_START -->"
DEFERRED_END = "<!-- DEFERRED_END -->"
KEYEVENTS_START = "<!-- KEYEVENTS_START -->"
KEYEVENTS_END = "<!-- KEYEVENTS_END -->"
TABOOS_START = "<!-- TABOOS_START -->"
TABOOS_END = "<!-- TABOOS_END -->"
HER_DAYS_START = "<!-- HER_DAYS_START -->"
HER_DAYS_END = "<!-- HER_DAYS_END -->"

# 记忆压缩：保留最近多少天的对话原文，更早的用模型压缩成摘要（原文不再归档）
KEEP_DAYS = 7

# 后台演化线程：定时做情绪稳态回归 + 记忆压缩调度的间隔（秒）
EVOLVE_INTERVAL = 600

# 主动通知：一天最多几次（只看次数，不看时间冷却）
NOTIFY_DAILY_MAX = 2

# 摘要区二次压缩：早期记忆摘要超过多少条时，把更早的再压成一条（保留最近 KEEP_RECENT_SUMMARY 条）
MAX_SUMMARY_LINES = 12
KEEP_RECENT_SUMMARY = 4

# 笔记区压缩：记忆文件里最多保留的笔记条数，超出的压成「观察理解」进摘要区（让「为什么」长期化）
MAX_NOTES_IN_FILE = 30

# 画像区封顶：超过 MAX_PROFILE_LINES 条时，早期画像压成「稳定画像」，保留最近 KEEP_RECENT_PROFILE 条
MAX_PROFILE_LINES = 20
KEEP_RECENT_PROFILE = 6

# 关键节点区封顶：超过 MAX_KEYEVENTS_LINES 条时，早期节点压成一条，保留最近 KEEP_RECENT_KEYEVENTS 条
# 重要性加权：关键节点是「大事 + 承诺/约定」的核心区，几乎不降级（阈值远高于普通区）；雷区本就无压缩、永不降级
MAX_KEYEVENTS_LINES = 30
KEEP_RECENT_KEYEVENTS = 10

# 主记忆编辑历史快照：改写前留一份，滚动保留最近 SNAPSHOT_KEEP 份（日志就绪后此为回退便利；日志之前它是唯一数据安全网）
SNAPSHOT_KEEP = 10

# 日志提炼：每积累多少行日志就提炼一批（约 20 行 ≈ 5 轮对话）
LOG_BATCH_LINES = 20

# 生活快照提炼：攒多少天（生活行）提炼成「她的日子」一段
DAILY_BATCH_DAYS = 7

MEMORY_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "city": "",
    "notify_enabled": True,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_text(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def ensure_memory_files():
    """首次运行时创建空白记忆库：主记忆 yiyou.md（含各区域锚点）与空日志 yiyou.log.md。
    主记忆文件必须预置锚点结构，否则日志提炼会因文件缺失而静默跳过。"""
    if not os.path.exists(MEMORY_PATH):
        skeleton = (
            "<!-- HISTORY_START -->\n（暂无）\n<!-- HISTORY_END -->\n"
            "<!-- NOTES_START -->\n（暂无）\n<!-- NOTES_END -->\n"
            "<!-- SUMMARY_START -->\n（暂无）\n<!-- SUMMARY_END -->\n"
            "<!-- PROFILE_START -->\n（暂无）\n<!-- PROFILE_END -->\n"
            "<!-- DEFERRED_START -->\n（暂无）\n<!-- DEFERRED_END -->\n"
            "<!-- KEYEVENTS_START -->\n（暂无）\n<!-- KEYEVENTS_END -->\n"
            "<!-- TABOOS_START -->\n（暂无）\n<!-- TABOOS_END -->\n"
            "<!-- HER_DAYS_START -->\n（暂无）\n<!-- HER_DAYS_END -->\n"
        )
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(skeleton)
    if not os.path.exists(LOG_PATH):
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            pass


def read_bytes(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def snapshot_memory():
    """主记忆改写前快照：把 yiyou.md 复制到 backup/，滚动保留最近 SNAPSHOT_KEEP 份。"""
    try:
        if not os.path.exists(MEMORY_PATH):
            return
        backup_dir = os.path.join(BASE_DIR, "backup")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(MEMORY_PATH, os.path.join(backup_dir, f"yiyou-{stamp}.md"))
        snaps = sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith("yiyou-") and f.endswith(".md")
        )
        for old in snaps[:max(0, len(snaps) - SNAPSHOT_KEEP)]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 快照失败：{e}")


def append_log(lines):
    """把若干行追加到日志（append-only，只增不改）。"""
    if not lines:
        return
    with LOG_LOCK:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln.rstrip("\n") + "\n")


def _read_log_lines():
    """读日志所有非空行（按顺序）。"""
    if not os.path.exists(LOG_PATH):
        return []
    with LOG_LOCK:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]


def ensure_api_key(cfg):
    if cfg.get("api_key"):
        return
    print("=" * 56)
    print("  怡悠 · 首次启动")
    print("  需要配置 DeepSeek API Key 才能对话。")
    print("  把 key 粘贴到这里，然后回车：")
    print("  （跳过的话，之后手动编辑 config.json 也能填）")
    print("=" * 56)
    try:
        key = input("API Key: ").strip()
    except EOFError:
        key = ""
    if key:
        cfg["api_key"] = key
        save_config(cfg)
        print("已保存到 config.json。")
    else:
        print("未输入 key。")
    print()


def call_deepseek(cfg, messages, thinking=None):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
    }
    if thinking:
        payload["thinking"] = {"type": thinking}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 请求失败（HTTP {e.code}）：{err}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 API：{e.reason}")
    try:
        return body["choices"][0]["message"]
    except Exception:
        raise RuntimeError(f"API 返回格式异常：{json.dumps(body, ensure_ascii=False)[:500]}")


# 兜底话：解析失败时返回给用户的占位回复。
# do_POST 靠 _is_fallback 判断是否自动重试，改动这里时记得同步。
FALLBACK_STUCK = "（她刚才卡了一下，没说出话来。你再说一遍？）"
FALLBACK_MESSY = "（她刚才有点乱，没组织好话。你再说一遍？）"


def _is_fallback(reply):
    """是否为兜底话（解析失败时的占位回复）。"""
    return reply in (FALLBACK_STUCK, FALLBACK_MESSY)


def parse_reply(message):
    content = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or "").strip()

    # thinking 模式：思考(reasoning_content)与回复(content)由 API 原生分离，
    # 不再依赖模型遵守自定义输出格式，从根上消除"卡住"。
    if reasoning:
        if not content:
            return reasoning, FALLBACK_STUCK
        return reasoning, content

    # 兼容：若关闭 thinking 且模型输出了旧分隔符格式
    analysis_marker = "<<<ANALYSIS>>>"
    reply_marker = "<<<REPLY>>>"
    if reply_marker in content:
        parts = content.split(reply_marker, 1)
        head = parts[0].strip()
        reply = parts[1].strip()
        if analysis_marker in head:
            analysis = head.split(analysis_marker, 1)[1].strip()
        else:
            analysis = head
        if not reply:
            return analysis, FALLBACK_STUCK
        return analysis, reply

    # 兜底：既无 reasoning 也无分隔符，记录原始内容，绝不泄露给用户
    try:
        with open(os.path.join(BASE_DIR, "parse_errors.log"), "a", encoding="utf-8") as f:
            f.write("[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] " + repr(content) + "\n")
    except Exception:
        pass
    return "", FALLBACK_MESSY


def _flat(s):
    return s.replace("\r", " ").replace("\n", " ").strip()


def insert_between(text, start_marker, end_marker, new_lines):
    pattern = re.compile(
        re.escape(start_marker) + r"\s*(.*?)\s*" + re.escape(end_marker),
        re.DOTALL,
    )

    def repl(m):
        inner = m.group(1).strip()
        if inner in ("", "（暂无）"):
            content = "\n".join(new_lines)
        else:
            content = inner + "\n" + "\n".join(new_lines)
        return start_marker + "\n" + content + "\n" + end_marker

    return pattern.sub(repl, text, count=1)


def _replace_region(text, start_marker, end_marker, new_lines):
    """用 new_lines 整体替换两个锚点之间的内容（区别于 insert_between 的追加）。"""
    pattern = re.compile(
        re.escape(start_marker) + r"\s*(.*?)\s*" + re.escape(end_marker),
        re.DOTALL,
    )
    content = "\n".join(new_lines)
    return pattern.sub(
        lambda m: start_marker + "\n" + content + "\n" + end_marker,
        text, count=1,
    )


def append_memory(user_msg, reply, analysis):
    """把一轮对话追加进日志（append-only），不再写 yiyou.md 的 HISTORY/NOTES。"""
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    lines = [
        f"[{stamp}] 用户：{_flat(user_msg)}",
        f"[{stamp}] 怡悠：{_flat(reply)}",
    ]
    if analysis:
        lines.append(f"[{stamp}] 内部分析：{_flat(analysis)}")
    append_log(lines)


def emotion_remember(cfg, user_msg, reply):
    """情绪标注 + 把情绪锚点 note 追加进日志（让「情绪为什么变」进原始记录）。"""
    note = emotion.on_conversation(cfg, user_msg, reply)
    if not note:
        return
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        append_log([f"[{stamp}] 情绪：{_flat(note)}"])
    except Exception as e:
        print(f"[warn] 情绪反哺记忆失败：{e}")


def _history_entries(text):
    """解析对话历史区，返回 [(日期字符串, 完整行), ...]，按出现顺序。"""
    m = re.search(
        re.escape(HISTORY_START) + r"\s*(.*?)\s*" + re.escape(HISTORY_END),
        text, re.DOTALL,
    )
    if not m:
        return []
    entries = []
    for line in m.group(1).splitlines():
        line = line.strip()
        mm = re.match(r"^- \[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}\]", line)
        if mm:
            entries.append((mm.group(1), line))
    return entries


def _remove_lines_from_history(text, lines_to_remove):
    """从对话历史区删除指定行。"""
    remove_set = set(lines_to_remove)
    pattern = re.compile(
        re.escape(HISTORY_START) + r"\s*(.*?)\s*" + re.escape(HISTORY_END),
        re.DOTALL,
    )

    def repl(m):
        kept = [ln for ln in m.group(1).splitlines() if ln.strip() not in remove_set]
        return HISTORY_START + "\n" + "\n".join(kept) + "\n" + HISTORY_END

    return pattern.sub(repl, text, count=1)


def summarize_history(cfg, lines):
    """用 deepseek-flash（非思考模式）把旧对话压缩成长期记忆要点。"""
    prompt = (
        "下面是一段较早的对话。请把它压缩成几句长期记忆要点，"
        "保留：说过的重要的事、对方的情绪和状态、约定或承诺、值得长期记住的细节，"
        "以及怡悠当时对对方的判断、担心和相处策略（她是怎么理解和回应他的）。"
        "去掉寒暄和重复。用中文写 2-5 句话，不要用列表、不要加标题。\n\n"
        + "\n".join(lines)
    )
    summary_cfg = dict(cfg)
    summary_cfg["model"] = "deepseek-flash"
    message = call_deepseek(summary_cfg, [
        {"role": "system", "content": "你是记忆整理助手，把对话压缩成简洁的长期记忆要点，只输出要点本身。"},
        {"role": "user", "content": prompt},
    ], thinking="disabled")
    return (message.get("content") or "").strip()


def summarize_summary(cfg, lines):
    """把多条早期记忆摘要再合并压缩成一条更精简的长期记忆要点。"""
    prompt = (
        "下面是一些「早期记忆摘要」条目，它们已经是对过往对话的压缩。"
        "请把它们再合并成一段更精简的长期记忆要点：保留最重要、最能代表这段关系走向的事实，"
        "以及这段关系里她形成的相处方式和对他的理解。"
        "去掉重复、次要细节和过时信息。用中文写 3-6 句话，不要用列表、不要加标题。\n\n"
        + "\n".join(lines)
    )
    summary_cfg = dict(cfg)
    summary_cfg["model"] = "deepseek-flash"
    message = call_deepseek(summary_cfg, [
        {"role": "system", "content": "你是记忆整理助手，把早期记忆摘要再压缩成精简的长期记忆要点，只输出要点本身。"},
        {"role": "user", "content": prompt},
    ], thinking="disabled")
    return (message.get("content") or "").strip()


def summarize_notes(cfg, lines):
    """把较早的内部分析笔记压成「她对这段关系的观察与理解」要点（保留「为什么」）。"""
    prompt = (
        "下面是一些较早的「内部分析笔记」，记录的是怡悠当时对对方和这段关系的观察、判断、情绪来由与相处策略。"
        "请把它们压缩成几句「她对这段关系的理解」要点："
        "保留她对对方性格/状态/习惯的洞察、她判断对方状态的模式、她情绪变化的来由、以及她因此形成的相处方式。"
        "去掉流水账式的思考过程和已经过时的临时判断。用中文写 2-5 句话，不要用列表、不要加标题。\n\n"
        + "\n".join(lines)
    )
    summary_cfg = dict(cfg)
    summary_cfg["model"] = "deepseek-flash"
    message = call_deepseek(summary_cfg, [
        {"role": "system", "content": "你是记忆整理助手，把分析笔记压缩成「她对这段关系的理解」要点，只输出要点本身。"},
        {"role": "user", "content": prompt},
    ], thinking="disabled")
    return (message.get("content") or "").strip()


def summarize_profile(cfg, lines):
    """把较早的用户画像条目压成「稳定画像」要点（保留长期稳定的身份/性格/习惯）。"""
    prompt = (
        "下面是一些较早的用户画像条目。请把它们压缩成几句「稳定画像」要点："
        "保留用户长期稳定的身份、性格、习惯、价值观和长期关系事实，"
        "去掉临时性的活动、当天发生的事、已经过期的状态。用中文写 2-5 句话，不要用列表、不要加标题。\n\n"
        + "\n".join(lines)
    )
    summary_cfg = dict(cfg)
    summary_cfg["model"] = "deepseek-flash"
    message = call_deepseek(summary_cfg, [
        {"role": "system", "content": "你是记忆整理助手，把早期画像条目压缩成「稳定画像」要点，只输出要点本身。"},
        {"role": "user", "content": prompt},
    ], thinking="disabled")
    return (message.get("content") or "").strip()


def summarize_keyevents(cfg, lines):
    """把较早的关键节点条目压成一条更精简的要点。"""
    prompt = (
        "下面是一些较早的「关键节点」条目。请把它们合并成一段更精简的长期要点："
        "保留最重要、最能代表这段关系走向的事件，去掉重复、次要细节和过时信息。"
        "用中文写 2-4 句话，不要用列表、不要加标题。\n\n"
        + "\n".join(lines)
    )
    summary_cfg = dict(cfg)
    summary_cfg["model"] = "deepseek-flash"
    message = call_deepseek(summary_cfg, [
        {"role": "system", "content": "你是记忆整理助手，把早期关键节点合并成精简要点，只输出要点本身。"},
        {"role": "user", "content": prompt},
    ], thinking="disabled")
    return (message.get("content") or "").strip()


def compress_old_history(cfg):
    """把超过 KEEP_DAYS 天的旧对话用模型压缩成摘要，原文从历史区移除（不归档）。"""
    try:
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            entries = _history_entries(text)
            if not entries:
                return
            cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            old_lines = [line for (d, line) in entries if d < cutoff]
        if not old_lines:
            return
        summary = summarize_history(cfg, old_lines)
        if not summary:
            return
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            # 并发保护：另一压缩线程可能已把 old_lines 删除并写入摘要。
            # 重新确认这些旧行是否仍在历史区，若已不在则说明被处理过，直接返回，避免重复摘要。
            current_set = {line for (d, line) in _history_entries(text)}
            still_there = [ln for ln in old_lines if ln in current_set]
            if not still_there:
                return
            stamp = datetime.now().strftime("%Y-%m-%d")
            text = insert_between(text, SUMMARY_START, SUMMARY_END, [f"- [{stamp}] {_flat(summary)}"])
            text = _remove_lines_from_history(text, still_there)
            snapshot_memory()
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 记忆压缩失败：{e}")


def register_aumid():
    """注册 AUMID + 创建开始菜单快捷方式，让 Windows 通知进入操作中心留记录（否则 toast 一闪而过、通知栏无记录）。"""
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\怡悠")
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "怡悠")
        winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[warn] AUMID 注册失败：{e}")
    # 开始菜单快捷方式：Windows 10 要求非打包应用有快捷方式，通知才能进操作中心
    try:
        import subprocess, tempfile

        lnk_path = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "怡悠.lnk")
        if os.path.exists(lnk_path):
            return
        script = (
            "$ws=New-Object -ComObject WScript.Shell;"
            "$sc=$ws.CreateShortcut('" + lnk_path + "');"
            "$sc.TargetPath='" + sys.executable + "';"
            "$sc.Arguments='\"" + os.path.join(BASE_DIR, "app.py") + "\"';"
            "$sc.WorkingDirectory='" + BASE_DIR + "';"
            "$sc.Save();"
        )
        fd, tmp = tempfile.mkstemp(suffix='.ps1')
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as f:
            f.write(script)
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp], timeout=15, check=False)
        finally:
            os.remove(tmp)
    except Exception as e:
        print(f"[warn] 快捷方式创建失败：{e}")


def recompress_summary(cfg):
    """摘要区二次压缩：早期记忆摘要长期累积会膨胀，把过老的摘要再压成一条更精简的长期记忆。"""
    try:
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            summary = _region(text, SUMMARY_START, SUMMARY_END)
        lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
        if len(lines) <= MAX_SUMMARY_LINES:
            return
        recent = lines[-KEEP_RECENT_SUMMARY:]
        old = lines[:-KEEP_RECENT_SUMMARY]
        condensed = summarize_summary(cfg, old)
        if not condensed:
            return
        new_lines = [f"- [更早期记忆] {_flat(condensed)}"] + recent
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            text = _replace_region(text, SUMMARY_START, SUMMARY_END, new_lines)
            snapshot_memory()
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 摘要二次压缩失败：{e}")


def compress_old_notes(cfg):
    """把超过保留条数的旧笔记压成「观察理解」要点写进摘要区，原文从笔记区移除（让「为什么」长期化）。"""
    try:
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            notes = _region(text, NOTES_START, NOTES_END)
        note_lines = [ln.strip() for ln in notes.splitlines() if ln.strip()]
        if len(note_lines) <= MAX_NOTES_IN_FILE:
            return
        overflow = note_lines[:len(note_lines) - MAX_NOTES_IN_FILE]
        condensed = summarize_notes(cfg, overflow)
        if not condensed:
            return
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            # 并发保护：另一线程可能已处理过这些旧笔记，重新确认仍在
            current = [ln.strip() for ln in _region(text, NOTES_START, NOTES_END).splitlines() if ln.strip()]
            still_there = [ln for ln in overflow if ln in current]
            if not still_there:
                return
            stamp = datetime.now().strftime("%Y-%m-%d")
            text = insert_between(text, SUMMARY_START, SUMMARY_END, [f"- [{stamp}] [观察] {_flat(condensed)}"])
            kept = [ln for ln in current if ln not in still_there]
            text = _replace_region(text, NOTES_START, NOTES_END, kept)
            snapshot_memory()
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 笔记压缩失败：{e}")


def recompress_profile(cfg):
    """画像区封顶：超过 MAX_PROFILE_LINES 条时，早期画像压成「稳定画像」，保留最近几条细节。"""
    try:
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            profile = _region(text, PROFILE_START, PROFILE_END)
        lines = [ln.strip() for ln in profile.splitlines() if ln.strip()]
        if len(lines) <= MAX_PROFILE_LINES:
            return
        recent = lines[-KEEP_RECENT_PROFILE:]
        old = lines[:-KEEP_RECENT_PROFILE]
        condensed = summarize_profile(cfg, old)
        if not condensed:
            return
        new_lines = [f"- [稳定画像] {_flat(condensed)}"] + recent
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            text = _replace_region(text, PROFILE_START, PROFILE_END, new_lines)
            snapshot_memory()
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 画像封顶失败：{e}")


def recompress_keyevents(cfg):
    """关键节点区封顶：超过 MAX_KEYEVENTS_LINES 条时，早期节点压成一条，保留最近几条。"""
    try:
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            keyevents = _region(text, KEYEVENTS_START, KEYEVENTS_END)
        lines = [ln.strip() for ln in keyevents.splitlines() if ln.strip()]
        if len(lines) <= MAX_KEYEVENTS_LINES:
            return
        recent = lines[-KEEP_RECENT_KEYEVENTS:]
        old = lines[:-KEEP_RECENT_KEYEVENTS]
        condensed = summarize_keyevents(cfg, old)
        if not condensed:
            return
        new_lines = [f"- [早期节点] {_flat(condensed)}"] + recent
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            text = _replace_region(text, KEYEVENTS_START, KEYEVENTS_END, new_lines)
            snapshot_memory()
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 关键节点封顶失败：{e}")


def summarize_log_batch(cfg, lines):
    """把一批日志行（对话 + 内部分析 + 情绪）压缩成「关系脉络」摘要。失败返回空串。"""
    prompt = (
        "下面是一段时间里怡悠和对方的聊天记录，包含对方的话、怡悠的回复、怡悠的内部分析和情绪变化。"
        "请把它们压缩成一段「关系脉络」要点：这段时间发生了什么、对方的情绪和状态、重要的约定或承诺、"
        "怡悠对对方的理解和相处方式的变化。去掉寒暄和琐碎日常，只保留值得长期记住的。"
        "用中文写 2-5 句话，不要用列表、不要加标题。\n\n"
        + "\n".join(lines)
    )
    summary_cfg = dict(cfg)
    summary_cfg["model"] = "deepseek-flash"
    message = call_deepseek(summary_cfg, [
        {"role": "system", "content": "你是记忆整理助手，把聊天记录压缩成关系脉络要点，只输出要点本身。"},
        {"role": "user", "content": prompt},
    ], thinking="disabled")
    return (message.get("content") or "").strip()


def refine_from_log(cfg):
    """提炼日志：把「未提炼」的日志行压缩成关系脉络摘要写进主记忆 SUMMARY，并推进进度标记。"""
    try:
        st = emotion.load_state()
        processed = int(st.get("log_processed_lines") or 0)
        log_lines = _read_log_lines()
        unprocessed = log_lines[processed:]
        if len(unprocessed) < LOG_BATCH_LINES:
            return
        batch = unprocessed[:LOG_BATCH_LINES]
        summary = summarize_log_batch(cfg, batch)
        if not summary:
            return
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            stamp = datetime.now().strftime("%Y-%m-%d")
            text = insert_between(text, SUMMARY_START, SUMMARY_END, [f"- [{stamp}] {_flat(summary)}"])
            snapshot_memory()
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
        st["log_processed_lines"] = processed + len(batch)
        emotion.save_state(st)
    except Exception as e:
        print(f"[warn] 日志提炼失败：{e}")


def summarize_daily_batch(cfg, lines):
    """把一段时间的生活快照提炼成「她的日子」段落。失败返回空串。"""
    prompt = (
        "下面是一段时间里怡悠「今天的生活快照」，记录她每天的日常。"
        "请把它们提炼成一段「她的这段日子」：这段时间她过得怎么样、在忙什么、情绪和状态如何、有没有特别想你或特别有意义的时刻。"
        "粗颗粒一点：概括这段日子的状态，不要逐条列。保留「某天特别想你」这类单句亮点。"
        "用中文写 2-4 句话，不要列表、不要标题。\n\n"
        + "\n".join(lines)
    )
    summary_cfg = dict(cfg)
    summary_cfg["model"] = "deepseek-flash"
    message = call_deepseek(summary_cfg, [
        {"role": "system", "content": "你是记忆整理助手，把生活快照提炼成一段「她的日子」，只输出段落本身。"},
        {"role": "user", "content": prompt},
    ], thinking="disabled")
    return (message.get("content") or "").strip()


def refine_daily_context(cfg):
    """提炼生活快照：日志里「未提炼」的生活行攒够 DAILY_BATCH_DAYS 天，提炼成「她的日子」段落写进 HER_DAYS 区。"""
    try:
        st = emotion.load_state()
        daily_processed = int(st.get("log_daily_processed") or 0)
        log_lines = _read_log_lines()
        daily_lines = [ln for ln in log_lines if "生活：" in ln]
        unprocessed = daily_lines[daily_processed:]
        if len(unprocessed) < DAILY_BATCH_DAYS:
            return
        batch = unprocessed[:DAILY_BATCH_DAYS]
        summary = summarize_daily_batch(cfg, batch)
        if not summary:
            return
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            stamp = datetime.now().strftime("%Y-%m-%d")
            text = insert_between(text, HER_DAYS_START, HER_DAYS_END, [f"- [{stamp}] {_flat(summary)}"])
            snapshot_memory()
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
        st["log_daily_processed"] = daily_processed + len(batch)
        emotion.save_state(st)
    except Exception as e:
        print(f"[warn] 生活快照提炼失败：{e}")


def show_notification(title, msg):
    """右下角原生 toast：设置进程 AUMID + 发带 launch 的通知（点击跳聊天页），进操作中心留记录。失败降级到 MessageBoxW。"""
    try:
        import subprocess, tempfile, os

        def _psq(s):
            """PowerShell 单引号字符串转义。"""
            return s.replace("'", "''")

        xml = (
            f'<toast activationType="protocol" launch="http://127.0.0.1:{PORT}" duration="long">'
            '<visual><binding template="ToastGeneric">'
            f'<text><![CDATA[{_psq(title)}]]></text>'
            f'<text><![CDATA[{_psq(msg)}]]></text>'
            '</binding></visual></toast>'
        )
        script = (
            "$k='HKCU:\\Software\\Classes\\AppUserModelId\\怡悠';"
            "New-Item -Path $k -Force | Out-Null;"
            "Set-ItemProperty -Path $k -Name DisplayName -Value '怡悠';"
            "Set-ItemProperty -Path $k -Name ShowInSettings -Value 1 -Type DWord;"
            "Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class AH { [DllImport(\"shell32.dll\", CharSet=CharSet.Unicode)] public static extern void SetCurrentProcessExplicitAppUserModelID(string a); }';"
            "[AH]::SetCurrentProcessExplicitAppUserModelID('怡悠');"
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
            "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$x.LoadXml('" + xml + "');"
            "$t=New-Object Windows.UI.Notifications.ToastNotification $x;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('怡悠').Show($t);"
        )
        fd, tmp = tempfile.mkstemp(suffix='.ps1')
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as f:
            f.write(script)
        try:
            subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', tmp], timeout=15, check=False)
        finally:
            os.remove(tmp)
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
    except Exception:
        pass


def append_active_say(msg):
    """把怡悠主动弹出去的话补进历史区（措辞留痕）。"""
    try:
        with MEMORY_LOCK:
            text = read_text(MEMORY_PATH)
            if not text:
                return
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            lines = [f"- [{stamp}] 怡悠（主动）：{_flat(msg)}"]
            text = insert_between(text, HISTORY_START, HISTORY_END, lines)
            with open(MEMORY_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
            except Exception:
                pass
    except Exception as e:
        print(f"[warn] 主动说落盘失败：{e}")


def try_notify(cfg):
    """尝试主动通知：约定提醒（穿透一切限制）优先，普通待说的话只看每天次数（无时间冷却、无深夜限制）。"""
    if not cfg.get("notify_enabled", True):
        return  # 防线：总开关
    state = emotion.load_state()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("notify_date") != today:
        state["notify_date"] = today
        state["notify_count_today"] = 0

    # 约定提醒：用户明确要求主动说的事，穿透一切限制（不占每日次数、无冷却、无深夜）
    rem = state.get("to_say_reminder") or []
    if rem:
        msg = rem[0]
        show_notification("怡悠", msg)
        append_active_say(msg)
        state["to_say_reminder"] = rem[1:]
        state["pending_said"] = (state.get("pending_said") or []) + [msg]
        state["last_notify"] = now.strftime("%Y-%m-%d %H:%M:%S")
        emotion.save_state(state)
        return

    # 普通待说的话：只看每天次数（删除时间冷却与深夜限制）
    to_say = state.get("to_say") or []
    if not to_say:
        return  # 防线：没正事不弹
    if state.get("notify_count_today", 0) >= NOTIFY_DAILY_MAX:
        return  # 防线：每日上限
    msg = to_say[0]
    show_notification("怡悠", msg)
    append_active_say(msg)
    state["to_say"] = to_say[1:]
    state["pending_said"] = (state.get("pending_said") or []) + [msg]
    state["last_notify"] = now.strftime("%Y-%m-%d %H:%M:%S")
    state["notify_count_today"] = state.get("notify_count_today", 0) + 1
    emotion.save_state(state)


def background_evolve(cfg):
    """后台演化线程：定时做情绪稳态回归 + 记忆压缩调度（解决 #5）。"""
    while True:
        try:
            emotion.background_tick()
        except Exception as e:
            print(f"[warn] 情绪演化失败：{e}")
        try:
            refine_from_log(cfg)
        except Exception as e:
            print(f"[warn] 后台压缩失败：{e}")
        try:
            refine_daily_context(cfg)
        except Exception as e:
            print(f"[warn] 生活快照提炼失败：{e}")
        try:
            recompress_summary(cfg)
        except Exception as e:
            print(f"[warn] 摘要二次压缩失败：{e}")
        try:
            compress_old_notes(cfg)
        except Exception as e:
            print(f"[warn] 笔记压缩失败：{e}")
        try:
            recompress_profile(cfg)
        except Exception as e:
            print(f"[warn] 画像封顶失败：{e}")
        try:
            recompress_keyevents(cfg)
        except Exception as e:
            print(f"[warn] 关键节点封顶失败：{e}")
        try:
            # 主动发信息：克制生成待说的话 + 五道防线过滤后通知
            emotion.generate_to_say(cfg)
            try_notify(cfg)
        except Exception as e:
            print(f"[warn] 主动发信息失败：{e}")
        time.sleep(EVOLVE_INTERVAL)


def extract_structured(cfg, user_msg, reply):
    """用 deepseek-flash（非思考模式）从本轮对话提取结构化信息（画像/雷区/关键节点/挑时机的话/城市）。失败返回 None。"""
    prompt = (
        "你是记忆结构化提取器。从下面这段对话里，提取值得长期记住的结构化信息，供怡悠的记忆库使用。\n"
        "只输出一个 JSON，不要输出任何其他内容。字段和判定标准：\n"
        '{"profile_facts": ["关于用户的新画像事实（身份、城市、爱好、家庭、工作学习、计划、习惯等，值得长期记住的）"], '
        '"taboos": ["雷区：用户明确表示不愿再谈的话题，或明显敏感、再提会刺痛对方的话题"], '
        '"key_events": ["关键节点/重要事件：值得长期记住的重要事情"], '
        '"deferred": ["挑时机再说的话：怡悠看出来了但觉得现在直说会起反效果、想以后挑时机再说的分析"], '
        '"pending": ["未了心事：对方承诺过或说过会做、但还没兑现或还没下文的事（例如「结果出来告诉你」「忙完就找你」「下次带你去」），怡悠会惦记着、之后想主动追问的"], '
        '"reminders": ["约定提醒：用户明确要求「怡悠要主动提醒/主动说」的事（例如「睡前和我说晚安」「到点了提醒我」「记得催我」），这是用户对怡悠的明确授权，要优先兑现"], '
        '"relation_signal": "关系升级表态：用户本轮是否明确说了让关系更进一步的话——talk_future=聊到未来/小家/攒钱/一直在一起；commit=明确认定（这辈子/娶你/认定你）；没有则为空字符串", '
        '"city": "用户所在城市，仅当本轮明确提到新城市时填，否则为空字符串"}\n\n'
        "判定要点：\n"
        "- 没有可提取的就给空数组，不要硬凑。\n"
        "- profile_facts 只收「新」信息，寒暄和重复已知信息不收。\n"
        "- taboos 宁缺毋滥：只有对方明确回避/拒绝，或话题明显会刺痛对方时才记；普通负面情绪（累、烦、压力大）不算雷区。\n"
        "- deferred 是「怡悠觉察到但暂不宜直说、留待以后」的分析，不是普通对话流水。\n\n"
        f"用户说：{user_msg}\n\n怡悠回复：{reply}\n"
    )
    payload = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON，不要输出任何其他内容。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        result = json.loads(content)
        for k in ("profile_facts", "taboos", "key_events", "deferred", "pending", "reminders"):
            v = result.get(k)
            result[k] = [str(x) for x in v] if isinstance(v, list) else []
        result["city"] = str(result.get("city") or "").strip()
        result["relation_signal"] = str(result.get("relation_signal") or "").strip()
        return result
    except Exception:
        return None


def _char_overlap(a, b):
    """两个字符串的字符集重叠率（0~1），用于结构化条目去重。"""
    if not a or not b:
        return 0.0
    sa = set(a)
    sb = set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def apply_structured(extracted):
    """把结构化提取结果写进 yiyou.md 对应区（画像/雷区/关键节点/挑时机）。"""
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    with MEMORY_LOCK:
        text = read_text(MEMORY_PATH)
        if not text:
            return
        key_events = list(extracted.get("key_events") or [])
        key_events += [f"约定：{x}" for x in (extracted.get("pending") or [])]
        sections = (
            (PROFILE_START, PROFILE_END, extracted.get("profile_facts")),
            (TABOOS_START, TABOOS_END, extracted.get("taboos")),
            (KEYEVENTS_START, KEYEVENTS_END, key_events),
            (DEFERRED_START, DEFERRED_END, extracted.get("deferred")),
        )
        for start_marker, end_marker, items in sections:
            if not items:
                continue
            existing = [ln.strip() for ln in _region(text, start_marker, end_marker).splitlines() if ln.strip()]
            new_items = []
            for x in items:
                s = _flat(x)
                if not s:
                    continue
                if any(_char_overlap(s, old) >= 0.7 for old in existing):
                    continue
                new_items.append(s)
            if not new_items:
                continue
            lines = [f"- [{stamp}] {x}" for x in new_items]
            text = insert_between(text, start_marker, end_marker, lines)
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            shutil.copyfile(MEMORY_PATH, BACKUP_PATH)
        except Exception:
            pass


def structured_extract_and_apply(cfg, user_msg, reply):
    """后台线程入口：提取结构化信息 → 写入记忆 + 未了心事写进情绪状态 + 更新城市。失败静默降级。"""
    try:
        extracted = extract_structured(cfg, user_msg, reply)
        if not extracted:
            return
        apply_structured(extracted)
        # pending（未了心事）+ deferred（挑时机再说的话）合并写进 state.json，供 generate_to_say 触发主动发信息
        pending = (extracted.get("pending") or []) + (extracted.get("deferred") or [])
        if pending:
            st = emotion.load_state()
            merged = list(dict.fromkeys((st.get("pending") or []) + pending))
            st["pending"] = merged[-10:]
            emotion.save_state(st)
        # reminders（约定提醒）写进 state.json，穿透一切限制、优先兑现
        reminders = extracted.get("reminders") or []
        if reminders:
            st = emotion.load_state()
            merged_rem = list(dict.fromkeys((st.get("reminders") or []) + reminders))
            st["reminders"] = merged_rem[-10:]
            emotion.save_state(st)
        city = extracted.get("city") or ""
        if city and city != cfg.get("city"):
            cfg["city"] = city
            save_config(cfg)
        # 成长（支柱八）：关系升级表态 → 阶段进阶判定（只进不退）
        signal = (extracted.get("relation_signal") or "").strip()
        if signal:
            st = emotion.load_state()
            if emotion.try_advance_growth(st, signal):
                print(f"[growth] 关系阶段进阶 → {st.get('growth_stage')}")
    except Exception as e:
        print(f"[warn] 结构化提取失败：{e}")


# 检索式记忆：控制喂给模型的记忆量，避免「整本摊开」越长越慢越贵
MAX_HISTORY_LINES = 30
MAX_NOTE_LINES = 10


def _region(text, start_marker, end_marker):
    """提取两个锚点之间的内容。"""
    m = re.search(re.escape(start_marker) + r"\s*(.*?)\s*" + re.escape(end_marker), text, re.DOTALL)
    return m.group(1).strip() if m else ""


_STOP_CHARS = set("的了着过是在有我这你他她它都也不就还和与或很更最一个们之其而因为所以如果那么什么怎么多少")


def _char_bigrams(text):
    """零依赖中文关键词：提取连续两字（bigram）集合。"""
    clean = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    grams = set()
    for i in range(len(clean) - 1):
        g = clean[i:i + 2]
        if g[0] in _STOP_CHARS or g[1] in _STOP_CHARS:
            continue
        grams.add(g)
    return grams


def retrieve_memory(user_msg):
    """检索式记忆：主记忆（画像/雷区/关键节点/摘要/挑时机）全量喂；对话原文与内部分析从日志读。"""
    with MEMORY_LOCK:
        text = read_text(MEMORY_PATH)

    profile = _region(text, PROFILE_START, PROFILE_END)
    summary = _region(text, SUMMARY_START, SUMMARY_END)
    taboos = _region(text, TABOOS_START, TABOOS_END)
    keyevents = _region(text, KEYEVENTS_START, KEYEVENTS_END)
    deferred = _region(text, DEFERRED_START, DEFERRED_END)
    her_days = _region(text, HER_DAYS_START, HER_DAYS_END)

    log_lines = _read_log_lines()
    conv_lines = [ln for ln in log_lines if ("用户：" in ln or "怡悠：" in ln)]
    note_lines = [ln for ln in log_lines if ("内部分析：" in ln or "情绪：" in ln)]

    hist_lines = conv_lines
    q_grams = _char_bigrams(user_msg)
    _time_hint = any(k in user_msg for k in ("上次", "那天", "之前", "以前", "还记得"))
    if len(hist_lines) > MAX_HISTORY_LINES:
        if _time_hint:
            hist_lines = hist_lines[-MAX_HISTORY_LINES:]
        elif q_grams:
            scored = []
            for i, ln in enumerate(hist_lines):
                overlap = len(q_grams & _char_bigrams(ln))
                scored.append((overlap, i, ln))
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            selected = scored[:MAX_HISTORY_LINES]
            selected.sort(key=lambda x: x[1])
            hist_lines = [x[2] for x in selected]
    history = "\n".join(hist_lines)

    if len(note_lines) > MAX_NOTE_LINES:
        notes = "\n".join(note_lines[-MAX_NOTE_LINES:])
    else:
        notes = "\n".join(note_lines)

    blocks = []
    for label, content in (
        ("用户画像", profile),
        ("雷区（绝不能碰）", taboos),
        ("关键节点", keyevents),
        ("挑时机再说的话", deferred),
        ("她的日子", her_days),
        ("早期记忆摘要", summary),
        ("对话历史", history),
        ("内部分析笔记（最近）", notes),
    ):
        if content and content != "（暂无）":
            blocks.append(f"【{label}】\n{content}")
    return "\n\n".join(blocks)


def build_system(cfg, user_msg=""):
    persona = read_text(PERSONA_PATH)
    memory = retrieve_memory(user_msg)
    now = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    date_line = now.strftime(f"%Y-%m-%d（星期{weekdays[now.weekday()]}）%H:%M")
    parts = [persona.strip(), "", f"今天是 {date_line}。"]
    if cfg.get("city"):
        parts.append(f"对方所在城市：{cfg['city']}")
    parts += [
        "",
        "【你的情绪状态——此刻真实的内在状态，有来由、有惯性，会影响你说话的方式】",
        emotion.get_mood_context(),
        "",
        "【你们的关系阶段——会随相处推进，影响你的分寸、亲昵和依赖程度】",
        emotion.get_growth_context(),
    ]
    daily = emotion.get_daily_context(cfg=cfg)
    if daily:
        # 生活快照沉淀侧：新生成的快照写进日志（带「生活」标记），供后续提炼成「她的日子」
        try:
            _dst = emotion.load_state()
            if _dst.get("daily_context_date") != _dst.get("log_daily_context_date"):
                _stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                _line = "[" + _stamp + "] 生活：" + daily
                append_log([_line])
                _dst["log_daily_context_date"] = _dst["daily_context_date"]
                emotion.save_state(_dst)
        except Exception:
            pass
        parts += [
            "",
            "【你此刻的生活状态——你「自己的日子」的当下快照，被问到时自然带出，别编造快照之外的具体细节】",
            daily,
        ]
    parts += [
        "",
        "【你的记忆库（已按相关性检索）内容如下，你要基于它延续对话、记住它】",
        memory.strip() or "（记忆库为空，第一次对话）",
        "【记忆的精确性】雷区、承诺、约定、关键节点，严格按记忆库里的原意说，不要脑补、不要改动、不要凭印象重写；其余关于过去细节、时间的事，基于记忆自然回忆即可，不必精确复述。",
        "",
        "【输出要求】你在思考过程中完成对用户的内部分析（模式识别、历史关联、判断对方状态、是否该挑时机说，这段不会给用户看），",
        "最终回复里只写你要对用户说的话，严格符合人设与规则，不要出现任何分析内容或格式标记。",
        "回复要简短、口语化，像微信聊天：每条回复一句到两三句就停，一个意思拆成几条短消息、中间用空行隔开，不要写成一长段，不要书面腔、不要 AI 味。",
    ]
    return "\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    server_version = "Yiyou/1.0"

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = read_text(INDEX_PATH)
            if not html:
                self._send(404, "index.html 不存在", "text/plain; charset=utf-8")
                return
            self._send(200, html, "text/html; charset=utf-8")
            return
        # 静态图片（头像等）：仅允许 BASE_DIR 下的图片文件，防路径遍历
        name = os.path.basename(self.path)
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        # 1) 按请求的完整文件名找
        if ext in IMG_CONTENT_TYPES:
            img_path = os.path.join(BASE_DIR, name)
            data = read_bytes(img_path)
            if data is not None:
                self._send(200, data, IMG_CONTENT_TYPES[ext])
                return
        # 2) 找不到时，按文件名主干匹配任意支持的图片扩展名（如请求 .png 但用户放的是 .jpg）
        if stem:
            for e, ct in IMG_CONTENT_TYPES.items():
                if e == ext:
                    continue  # 第 1 步已试过
                data = read_bytes(os.path.join(BASE_DIR, stem + e))
                if data is not None:
                    self._send(200, data, ct)
                    return
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/chat":
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            req = json.loads(raw)
            messages = req.get("messages") or []
        except Exception as e:
            self._send(400, json.dumps({"error": f"请求解析失败：{e}"}, ensure_ascii=False))
            return

        cfg = load_config()
        if not cfg.get("api_key"):
            self._send(500, json.dumps({"error": "尚未配置 API Key，请在 config.json 里填入。"}, ensure_ascii=False))
            return

        # 先取最后一条用户消息，用于检索记忆
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        system_msg = build_system(cfg, user_msg)

        # 空 messages = 刚打开窗口，让怡悠主动开场问候
        if not messages:
            # 优先：她主动发的话（通知已弹出、还没在页面展示），直接原样展示，不再生成新开场
            _state = emotion.load_state()
            _said = _state.get("pending_said") or list()
            if _said:
                reply = "\n\n".join(_said)
                _state["pending_said"] = list()
                emotion.save_state(_state)
                self._send(200, json.dumps({"reply": reply, "analysis": ""}, ensure_ascii=False))
                return
            opening = "（对方刚刚打开对话窗口，还没说话。请你主动、简短地打个招呼，口语化一点，带一点熟悉感，不要每次都一样，别写成长篇大论。）"
            # 补说（形态 C 兜底）：有攒着的话（约定提醒优先 + 普通待说的话），开场时顺带说出来，然后清空
            _state = emotion.load_state()
            _rem = _state.get("to_say_reminder") or list()
            _to_say = _state.get("to_say") or list()
            _all = _rem + _to_say
            if _all:
                opening += " 另外，你有几句之前攒着想跟他说的话，可以顺带说出来（自然一点，别生硬）：" + "；".join(_all)
                _state["to_say_reminder"] = list()
                _state["to_say"] = list()
                emotion.save_state(_state)
            api_messages = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": opening},
            ]
        else:
            api_messages = [{"role": "system", "content": system_msg}] + messages

        try:
            message = call_deepseek(cfg, api_messages, thinking="enabled")
            analysis, reply = parse_reply(message)
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
            return

        # reasoner 偶发只返回思考、没返回正文（表现为"卡了一下"兜底话），自动重试一次
        if _is_fallback(reply):
            try:
                message = call_deepseek(cfg, api_messages, thinking="enabled")
                analysis2, reply2 = parse_reply(message)
                if reply2 and not _is_fallback(reply2):
                    analysis, reply = analysis2, reply2
            except Exception:
                pass

        if user_msg and reply and not _is_fallback(reply):
            try:
                append_memory(user_msg, reply, analysis)
            except Exception as e:
                print(f"[warn] 记忆写入失败：{e}")
            # 情感系统：后台线程标注这次对话对怡悠情绪的影响并更新状态（失败静默降级，不阻塞回复）
            try:
                threading.Thread(target=emotion_remember, args=(cfg, user_msg, reply), daemon=True).start()
            except Exception as e:
                print(f"[warn] 情绪更新失败：{e}")
            # 结构化提取：后台线程提取画像/雷区/关键节点/挑时机的话并写入记忆（失败静默降级）
            try:
                threading.Thread(target=structured_extract_and_apply, args=(cfg, user_msg, reply), daemon=True).start()
            except Exception as e:
                print(f"[warn] 结构化提取失败：{e}")
            # 渐进式压缩：后台线程做，不阻塞回复
            try:
                threading.Thread(target=refine_from_log, args=(cfg,), daemon=True).start()
            except Exception:
                pass

        self._send(200, json.dumps({"reply": reply, "analysis": analysis}, ensure_ascii=False))

    def log_message(self, format, *args):
        pass


def main():
    cfg = load_config()
    ensure_api_key(cfg)
    ensure_memory_files()
    # 注册 AUMID，让通知进入操作中心留记录
    register_aumid()
    # 后台演化线程：情绪稳态 + 定时压缩（daemon，随主程序退出）
    threading.Thread(target=background_evolve, args=(cfg,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print("怡悠已启动：", url)
    print("按 Ctrl+C 停止。")
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
