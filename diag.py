#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""怡悠记忆健康检查（只读）。

读取日志 / 主记忆 / 状态 / 附件，输出健康报告到 diag_report.md 并打印到终端。
绝不写 yiyou.md / yiyou.log.md / state.json / config.json。

用法：python diag.py
"""
import json
import os
import re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))                    # 开源版根目录
MEMORY_PATH = os.path.join(BASE_DIR, "yiyou.md")
LOG_PATH = os.path.join(BASE_DIR, "yiyou.log.md")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
RECALL_STATS_PATH = os.path.join(BASE_DIR, "recall_stats.log")
ATTACH_DIR = os.path.join(BASE_DIR, "attachments")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
REPORT_PATH = os.path.join(BASE_DIR, "diag_report.md")
ATTACHMENT_KEEP_DAYS = 30

_STOP = set("的了着过是在有我这你他她它都也不就还和与或很更最一个们之其而因为所以如果那么什么怎么多少")


def read_text(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_lines(path):
    t = read_text(path)
    return [ln for ln in t.splitlines() if ln.strip()] if t else []


def bigrams(text):
    clean = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    grams = set()
    for i in range(len(clean) - 1):
        g = clean[i:i + 2]
        if g[0] in _STOP or g[1] in _STOP:
            continue
        grams.add(g)
    return grams


def main():
    report = []
    report.append("# 怡悠记忆健康检查报告")
    report.append("\n> 生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    report.append("> 本报告只读，不修改任何记忆/状态文件。")

    log_lines = read_lines(LOG_PATH)
    mem_text = read_text(MEMORY_PATH)

    report.append("\n## 一、基本量")
    report.append("- 日志行数：%d" % len(log_lines))
    report.append("- 主记忆大小：%d 字节（%d 字符）" % (len(mem_text.encode("utf-8")), len(mem_text)))
    snaps = []
    if os.path.isdir(BACKUP_DIR):
        snaps = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("yiyou-") and f.endswith(".md"))
    report.append("- 主记忆快照份数：%d" % len(snaps))

    state = {}
    if os.path.exists(STATE_PATH):
        try:
            state = json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            state = {}

    report.append("\n## 二、提炼与压缩进度")
    report.append("- 日志已提炼行数（log_processed_lines）：%s" % (state.get("log_processed_lines", "暂无"),))
    report.append("- 生活快照已提炼天数（log_daily_processed）：%s" % (state.get("log_daily_processed", "暂无"),))
    report.append("- 最近主记忆快照：%s" % (snaps[-1] if snaps else "暂无",))

    report.append("\n## 三、关键节点保留率（抽样日志里的约定/承诺）")
    promise_pat = re.compile(r"(约定|答应|承诺|提醒|记得|别忘了)")
    promise_lines = [ln for ln in log_lines if promise_pat.search(ln)]
    sample = promise_lines[-20:] if len(promise_lines) > 20 else promise_lines
    if not sample:
        report.append("- 日志里暂无「约定/承诺」相关行（数据不足）")
    else:
        hit = 0
        for ln in sample:
            body = re.sub(r"^\[[^\]]*\]\s*(用户|怡悠|内部分析|情绪)：", "", ln).strip()
            g = bigrams(body)
            if not g:
                continue
            overlap = len(g & bigrams(mem_text)) / len(g)
            if overlap >= 0.5:
                hit += 1
        report.append("- 抽样「约定/承诺」行：%d 条" % len(sample))
        report.append("- 主记忆仍能找到相关内容的：%d 条" % hit)
        report.append("- 保留率：%d%%" % round(hit / len(sample) * 100))

    report.append("\n## 四、主动消息重复率")
    yiyou_says = [ln for ln in log_lines if "怡悠：" in ln]
    recent = yiyou_says[-50:]
    if not recent:
        report.append("- 暂无怡悠发言记录")
    else:
        bodies = [re.sub(r"^\[[^\]]*\]\s*怡悠：", "", ln).strip() for ln in recent]
        unique = set(bodies)
        report.append("- 最近 %d 条怡悠发言，去重后 %d 条" % (len(bodies), len(unique)))
        report.append("- 重复率：%d%%" % round((1 - len(unique) / len(bodies)) * 100))

    report.append("\n## 五、检索命中率")
    recall_lines = read_lines(RECALL_STATS_PATH)
    if not recall_lines:
        report.append("- 暂无检索诊断数据（recall_stats.log 尚未积累）")
    else:
        recent_r = recall_lines[-100:]
        dist = {}
        for ln in recent_r:
            try:
                d = json.loads(ln)
                dist[d.get("status", "?")] = dist.get(d.get("status", "?"), 0) + 1
            except Exception:
                pass
        total = sum(dist.values()) or 1
        for k in ("hit", "partial", "miss"):
            report.append("- %s: %d 次（%d%%）" % (k, dist.get(k, 0), round(dist.get(k, 0) / total * 100)))

    report.append("\n## 六、附件")
    if not os.path.isdir(ATTACH_DIR):
        report.append("- 附件目录不存在或为空")
    else:
        files = [f for f in os.listdir(ATTACH_DIR) if os.path.isfile(os.path.join(ATTACH_DIR, f))]
        total_bytes = sum(os.path.getsize(os.path.join(ATTACH_DIR, f)) for f in files)
        cutoff = datetime.now().timestamp() - ATTACHMENT_KEEP_DAYS * 86400
        expired = [f for f in files if os.path.getmtime(os.path.join(ATTACH_DIR, f)) < cutoff]
        report.append("- 附件数：%d 个，占用 %.1f KB" % (len(files), total_bytes / 1024))
        report.append("- 过期未回收（>%d 天）：%d 个" % (ATTACHMENT_KEEP_DAYS, len(expired)))

    text = "\n".join(report) + "\n"
    print(text)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    print("[已写入] " + REPORT_PATH)


if __name__ == "__main__":
    main()
