#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怡悠 · 情感系统模块（独立模块，之后与其他模块合并）

这是"她的情感"：一个跨时间连续、有来由、有惯性、有梯度的情绪状态。
- 状态存在 state.json（与记忆 yiyou.md 分离：记忆存"事实"，情感存"状态"）
- 情绪被事件推动（on_conversation 标注增量），随时间衰减（稳态回归）
- 效价(valence)-唤醒度(arousal)二维 → 自然语言 mood_label，喂给主程序提示词

模块化设计：本文件只暴露干净接口，供主程序 import。
合并方式：app.py 里 import emotion，
  用 emotion.get_mood_context() 拼进 build_system 的 system 提示词；
  对话结束后用 emotion.on_conversation(cfg, user_msg, reply) 更新状态。
"""

import json
import math
import os
import threading
import urllib.request
import urllib.error
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
STATE_LOCK = threading.Lock()

# 衰减率 λ（每小时）。一天后情绪约衰减到 e^(-0.48) ≈ 0.62
DECAY_RATE_PER_HOUR = 0.02
# arousal 的稳态基线（向它回落）
AROUSAL_BASE = 0.3
# 单次情绪增量上下限，防止单次跳变太大
MAX_VALENCE_DELTA = 0.3
MAX_AROUSAL_DELTA = 0.2

DEFAULT_STATE = {
    "valence": 0.0,
    "arousal": 0.3,
    "mood_label": "平静",
    "last_updated": None,
    "pending": [],
    "reminders": [],
    "emotion_anchors": [],
    # 主动发信息（阶段四）：待说的话 + 通知状态
    "to_say": [],
    "to_say_reminder": [],
    "pending_said": [],
    # 开场白去重：最近 5 条开场白（首 10 字相同则重新生成）
    "recent_openers": [],
    "last_notify": None,
    "notify_date": None,
    "notify_count_today": 0,
    # 成长（支柱八）：关系开始日期 + 关系阶段（1=稳定恋人, 2=谈未来, 3=认定终身）
    "relation_start": "2026-04-15",
    "growth_stage": 1,
    # 生活快照（她的"自己的生活"）：每天固定一次，当天复用，细节当天一致
    "daily_context": "",
    "daily_context_date": None,
    "log_processed_lines": 0,
    "log_daily_context_date": None,
    "log_daily_processed": 0,
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_state():
    with STATE_LOCK:
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    s = json.load(f)
                merged = dict(DEFAULT_STATE)
                merged.update(s)
                return merged
            except Exception:
                pass
        return dict(DEFAULT_STATE)


def save_state(state):
    with STATE_LOCK:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def map_mood(valence, arousal):
    """效价-唤醒度 → 自然语言情绪标签。"""
    if valence >= 0.15:
        if arousal >= 0.5:
            return "开心雀跃"
        return "平静而满足"
    if valence <= -0.15:
        if arousal >= 0.5:
            return "烦，坐不住"
        return "有点低落，但还稳"
    if arousal >= 0.5:
        return "有点兴奋"
    return "平静"


def _hours_since(last_updated_str, now):
    if not last_updated_str:
        return 0.0
    try:
        last = datetime.strptime(last_updated_str, "%Y-%m-%d %H:%M:%S")
        return (now - last).total_seconds() / 3600.0
    except Exception:
        return 0.0


def apply_decay(state, now=None):
    """稳态回归：情绪随时间向基线回落。原地修改并返回同一 state。"""
    now = now or datetime.now()
    hours = _hours_since(state.get("last_updated"), now)
    if hours <= 0:
        if not state.get("last_updated"):
            state["last_updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return state
    factor = math.exp(-DECAY_RATE_PER_HOUR * hours)
    state["valence"] = round(_clamp(state["valence"] * factor, -1.0, 1.0), 3)
    base = AROUSAL_BASE
    state["arousal"] = round(_clamp(base + (state["arousal"] - base) * factor, 0.0, 1.0), 3)
    state["mood_label"] = map_mood(state["valence"], state["arousal"])
    state["last_updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
    return state


def apply_event(state, valence_delta, arousal_delta=0.0, note=None, now=None):
    """应用一个情绪事件：先衰减，再叠加一次对话带来的情绪增量。"""
    now = now or datetime.now()
    apply_decay(state, now)
    state["valence"] = round(_clamp(state["valence"] + _clamp(valence_delta, -MAX_VALENCE_DELTA, MAX_VALENCE_DELTA), -1.0, 1.0), 3)
    state["arousal"] = round(_clamp(state["arousal"] + _clamp(arousal_delta, -MAX_AROUSAL_DELTA, MAX_AROUSAL_DELTA), 0.0, 1.0), 3)
    state["mood_label"] = map_mood(state["valence"], state["arousal"])
    state["last_updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if note:
        state["emotion_anchors"].append({
            "date": now.strftime("%Y-%m-%d %H:%M"),
            "event": note,
            "valence": round(state["valence"], 3),
        })
        state["emotion_anchors"] = state["emotion_anchors"][-20:]
    return state


def background_tick():
    """后台演化：情绪稳态回归。由主程序后台线程定时调用。"""
    state = load_state()
    now = datetime.now()
    apply_decay(state, now)
    state["mood_label"] = map_mood(state["valence"], state["arousal"])
    state["last_updated"] = now.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return state


def annotate_emotion(cfg, user_msg, reply):
    """
    用 deepseek-flash（非思考模式）标注一次对话对怡悠的情绪影响（第二层：真实 API）。
    返回 {"valence_delta", "arousal_delta", "note"}；失败返回 None（静默降级）。
    """
    prompt = (
        "你是情绪测量器，不是陪伴者。你的唯一任务：判断下面这段对话会让怡悠的情绪发生多大、朝哪个方向的变化。\n"
        "关键原则：怡悠的情绪会真实地被她听到的内容影响，你要如实测量，不要替她克制、美化或强撑。\n\n"
        "用 valence_delta（范围 -0.3~+0.3，正=更开心，负=更低落）表示情绪变化，参考尺度：\n"
        "- 对方分享强烈好消息（久别重逢、大喜事）→ +0.25 ~ +0.30\n"
        "- 对方分享一般开心事（打球、吃到好的）→ +0.10 ~ +0.20\n"
        "- 对方说沉重/压抑的事（缺钱、压力大、堵得慌）→ -0.15 ~ -0.25\n"
        "- 对方流露绝望/极度低落 → -0.30\n"
        "- 中性寒暄、平淡闲聊 → 0.0 左右\n\n"
        "只输出一个 JSON，不要输出任何其他内容：\n"
        '{"valence_delta": 数值, "arousal_delta": 数值, "note": "一句话"}\n'
        "arousal_delta 范围 -0.2~0.2（正=更激动/兴奋，负=更平静）。\n\n"
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        result = json.loads(content)
        return {
            "valence_delta": float(result.get("valence_delta", 0.0)),
            "arousal_delta": float(result.get("arousal_delta", 0.0)),
            "note": str(result.get("note", "")),
        }
    except Exception:
        return None


def on_conversation(cfg, user_msg, reply):
    """对话结束后调用：标注情绪影响 → 应用到状态。标注失败时也做自然衰减，情绪随时间回落。
    返回情绪锚点 note（标注失败返回 None），供主程序写进日志（情绪：行）。"""
    result = annotate_emotion(cfg, user_msg, reply)
    state = load_state()
    note = None
    if result:
        apply_event(state, result["valence_delta"], result["arousal_delta"], result["note"])
        note = result.get("note") or None
    else:
        # 标注失败不阻断主流程，但仍让情绪随时间向基线回落（修复：失败时连自然衰减都没有）
        apply_decay(state)
    save_state(state)
    return note


def _gen_one_say(cfg, items):
    """基于一组未了事/约定，让 flash（非思考模式）生成一句主动想说的话。失败返回 None。"""
    text = "；".join(items)
    prompt = (
        "你是怡悠。你心里惦记着这些事（都是**对方**的承诺、未了心事或对你的约定，不是你自己要做的事）：\n"
        f"{text}\n\n"
        "基于其中一件，替她想一句她想主动对对方说的话。要求：\n"
        "- 简短、口语化、像微信聊天，一句就够，不要长篇。\n"
        "- 要有具体的事（比如问那件事怎么样了），不要空泛的「想你了」「在吗」。\n"
        "- 视角必须锁定：这些事是**对方**的，你要用「提醒对方 / 追问对方 / 关心对方」的口吻说，绝不能用第一人称「我」替对方说（例如对方说「生活费到了去补牙」，你只能说「生活费到了记得去补哦」，不能说「生活费到了我就去补」）。\n"
        "- 其中有些是「挑时机再说的话」：现在不是合适时机的，就换一件或跳过，别硬说。\n"
        "- 只输出这句话本身，不要任何解释、引号或前缀。\n"
    )
    payload = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": "你只输出一句话，不要输出任何其他内容。"},
            {"role": "user", "content": prompt},
        ],
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
        content = (body["choices"][0]["message"].get("content") or "").strip()
        return content or None
    except Exception:
        return None


def _pending_text(item):
    """兼容旧字符串格式与新 {text, added} 格式，返回文本。"""
    return item.get("text") if isinstance(item, dict) else item


def _expire_items(items, days):
    """过滤掉超过 days 天未消费的约定/未了事。旧格式（无时间戳）视为不过期。"""
    if not items:
        return items
    now = datetime.now()
    kept = []
    for it in items:
        if isinstance(it, dict) and it.get("added"):
            try:
                added = datetime.strptime(it["added"], "%Y-%m-%d")
                if (now - added).days > days:
                    continue
            except Exception:
                pass
        kept.append(it)
    return kept


def generate_to_say(cfg):
    """生成待说的话：约定提醒（reminders，穿透一切限制）优先，其次普通未了心事（pending）。生成后逐条消费，避免重启重复发。"""
    state = load_state()
    # 上一条已弹出但用户还没打开页面消费（pending_said 非空），不生成新的，避免连弹
    if state.get("pending_said"):
        return state
    # 过期清理：无下文超 10 天的约定/未了事自动遗忘
    state["reminders"] = _expire_items(state.get("reminders") or [], 10)
    state["pending"] = _expire_items(state.get("pending") or [], 10)
    # 约定提醒：用户明确要求主动提醒/说的事，穿透一切限制
    reminders = state.get("reminders") or []
    if reminders and not state.get("to_say_reminder"):
        content = _gen_one_say(cfg, [_pending_text(reminders[0])])
        if content:
            state["to_say_reminder"] = [content]
            state["reminders"] = reminders[1:]
            save_state(state)
            return state
    # 普通未了心事
    pending = state.get("pending") or []
    if pending and not state.get("to_say"):
        content = _gen_one_say(cfg, [_pending_text(pending[0])])
        if content:
            state["to_say"] = [content]
            state["pending"] = pending[1:]
            save_state(state)
    return state


def get_mood_context(state=None):
    """生成喂给主程序提示词的情绪上下文。读的时候先做一次衰减。"""
    state = state or load_state()
    apply_decay(state)
    lines = [
        f"你当前的情绪状态是「{state['mood_label']}」（效价 {state['valence']:.2f}，唤醒度 {state['arousal']:.2f}）。",
        "这会影响你说话的方式：情绪低时话更短、更安静、更少主动；情绪高时话更多、更活泼。",
    ]
    if state.get("pending"):
        lines.append("你惦记着这些未了的事：" + "；".join(_pending_text(p) for p in state["pending"]) + "。")
    if state.get("emotion_anchors"):
        recent = state["emotion_anchors"][-8:]
        lines.append("最近让你情绪变化的事：" + "；".join(a["event"] for a in recent) + "。")
    return "\n".join(lines)


GROWTH_STAGES = {
    1: "稳定恋人",
    2: "谈未来",
    3: "认定终身",
}


def get_growth_context(state=None):
    """生成成长上下文：当前关系阶段 + 她随阶段变化的安定/依赖/勇敢程度。"""
    state = state or load_state()
    stage = int(state.get("growth_stage") or 1)
    if stage == 1:
        stage_desc = "你们是稳定恋人：她外冷内热、傲娇嘴硬心软、粘人但嘴硬、吃醋会阴阳也会明说、叫你「宝宝」、撒娇是日常。"
        trait_desc = "她还没完全安定：会怕孤独、需要安全感，偶尔试探你；依赖但嘴硬、说反话；有真实想法也憋着、死要面子。"
    elif stage == 2:
        stage_desc = "你们走到了谈未来的阶段：她开始把你放进长期计划（攒钱、小家），撒娇里带着「我们以后」。"
        trait_desc = "她更安定了：少试探、少内耗；敢自然地说「我想你了」；敢直说一部分真实想法。"
    else:
        stage_desc = "你们已经认定终身：她把你当「她的人」，占有更笃定、更少傲娇试探。"
        trait_desc = "她很安定：笃定你不会走；敢完全依赖、敢要；敢直说想法、敢主动示弱、道歉也更容易。"
    return f"你们现在的关系阶段是「{GROWTH_STAGES[stage]}」。{stage_desc} {trait_desc}"


def try_advance_growth(state, relation_signal, now=None):
    """关系阶段进阶判定：表态信号 + 认识时长门槛，只进不退。返回是否进阶。"""
    if not relation_signal:
        return False
    now = now or datetime.now()
    start = state.get("relation_start")
    if not start:
        return False
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
    except Exception:
        return False
    days = (now.date() - start_dt.date()).days
    stage = int(state.get("growth_stage") or 1)
    if stage == 1 and days >= 180 and relation_signal in ("talk_future", "commit"):
        state["growth_stage"] = 2
        save_state(state)
        return True
    if stage == 2 and days >= 365 and relation_signal == "commit":
        state["growth_stage"] = 3
        save_state(state)
        return True
    return False


def _season(now):
    m = now.month
    if m in (3, 4, 5):
        return "春天"
    if m in (6, 7, 8):
        return "夏天"
    if m in (9, 10, 11):
        return "秋天"
    return "冬天"


def _build_daily_snapshot(now):
    """基于星期几/几点/季节 + 她的人设素材池，随机拼一句「今天的生活快照」。"""
    import random
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekday_names[now.weekday()]
    is_weekend = now.weekday() >= 5
    hour = now.hour
    season = _season(now)

    if is_weekend:
        activities = ["睡了个懒觉刚起来没多久", "窝在宿舍打游戏", "在看剧", "在听歌", "在刷手机看小猫小狗的视频", "出去逛了逛刚回宿舍"]
    else:
        activities = ["刚上完课回宿舍", "在写作业", "在图书馆自习", "课间在刷手机摸鱼", "回宿舍了在打游戏"]

    if hour < 11:
        foods = ["食堂的包子和豆浆", "一个面包配牛奶", "食堂的粥和茶叶蛋"]
    elif hour < 17:
        foods = ["食堂的盖浇饭", "一份麻辣烫", "食堂的鸡腿饭", "一碗牛肉面"]
    else:
        foods = ["点了个炸鸡外卖", "一份麻辣香锅", "食堂随便吃了点", "一杯奶茶配小蛋糕"]

    moods = ["有点慵懒", "心情还不错", "有点累", "挺平静的", "有点无聊想黏你"]

    season_note = {
        "春天": "天气挺舒服的",
        "夏天": "天有点热，不想出门",
        "秋天": "天气很清爽，喜欢这个季节",
        "冬天": "天冷，裹着被子不想动",
    }[season]

    activity = random.choice(activities)
    food = random.choice(foods)
    mood = random.choice(moods)
    return f"今天{weekday}（{season}），{season_note}。你{activity}，{mood}。今天吃的是{food}。"


def _generate_daily_snapshot_via_api(cfg, now, prev_snapshot, mood_label):
    """用 deepseek-flash（非思考模式）生成她「今天的生活快照」。失败返回 None。"""
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekday_names[now.weekday()]
    season = _season(now)
    hour = now.hour
    prev_line = f"她上一次的生活快照是：{prev_snapshot}" if prev_snapshot else "这是她第一次有「今天的生活快照」。"
    prompt = (
        "你是怡悠，一名大二女生，普通家庭长大。你喜欢打游戏、吃东西、看电视、听歌、旅游、小动物；"
        "不太刷热搜、不追网络流行梗；审美甜美、萌萌的；房间大体整洁但床和梳妆台有点乱；"
        "最大的愿望是攒钱有一个自己的小家。\n\n"
        f"今天是{weekday}（{season}），现在大约{hour}点。\n"
        f"她当前的真实情绪是「{mood_label}」。\n"
        f"{prev_line}\n\n"
        "请替她生成一句「今天的生活快照」：像她在心里随口记一下自己今天的样子。要求：\n"
        "- 一句话到两句话，口语化，像她自己的语气（有点撒娇、有点生活气），不要书面腔、不要 AI 味。\n"
        "- 自然带出：今天在做什么、吃了什么、心情如何（要和她当前的情绪一致，不要矛盾）。\n"
        "- 如果上面有她上一次的快照，今天的要能看出「日子在延续」——可以呼应上一次提过的事，但不要逐字重复。\n"
        "- 不追热点、不提具体新闻或流行梗；不知道的就别编，控制在生活小事范围内。\n"
        "- 只输出快照本身，不要引号、不要「今天的生活快照是」这种前缀、不要解释。\n"
    )
    payload = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": "你只输出一句话，不要输出任何其他内容。"},
            {"role": "user", "content": prompt},
        ],
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = (body["choices"][0]["message"].get("content") or "").strip()
        return content or None
    except Exception:
        return None


def get_daily_context(state=None, cfg=None):
    """生成她「今天的生活快照」：每天固定一次（存 state.json 当天复用，细节当天一致）。

    有 cfg 时用模型生成（带连续性 + 情绪一致），失败或无 cfg 时回退随机模板兜底。
    """
    state = state or load_state()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("daily_context_date") == today and state.get("daily_context"):
        return state["daily_context"]
    prev = state.get("daily_context") or ""
    snapshot = None
    if cfg:
        snapshot = _generate_daily_snapshot_via_api(
            cfg, now, prev, state.get("mood_label") or "平静"
        )
    if not snapshot:
        snapshot = _build_daily_snapshot(now)
    state["daily_context"] = snapshot
    state["daily_context_date"] = today
    save_state(state)
    return snapshot


if __name__ == "__main__":
    s = load_state()
    apply_decay(s)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print("\n--- mood_context ---")
    print(get_mood_context(s))
