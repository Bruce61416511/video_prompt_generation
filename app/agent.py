# -*- coding: utf-8 -*-
"""
智能体调度器 —— 让「大模型自己决定用哪个 skill」。

流程（这就是你要的 "load skill"）：

    用户输入
       ↓
    ① 选 skill：把 skill 清单（只有 name + description）给模型，让它挑
       ↓
    ② 注入：把挑中的 SKILL.md 全文拼进 system prompt
       ↓
    ③ 生成：模型按 skill 规则写出分镜提示词
       ↓
    ④ 质检：再跑一遍 seedance-prompt-lint
       ↓
    ⑤ 修正：拿质检报告把提示词改一遍，产出「可直接复制使用」的定稿
       ↓
    输出
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from openai import OpenAI

from .skill_loader import SkillLoader
from .timing_capacity import capacity_report
from .delivery_gate import sanitize_delivery, check_delivery, scan_declared_seconds


# ---------------------------------------------------------------- 配置

@dataclass
class LLMConfig:
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    model: str = "qwen-plus"
    temperature: float = 0.7


# ---------------------------------------------------------------- 技能分层
#
# 2026-11-02：此前给调度器的清单是 31 条平铺（name + description），
# 模型不知道谁是「主角」谁是「底座」，加上规则「大部分情况只选 1 个」，
# 实测带货口播需求只返回 [ugc-creator-review] 一件 —— 时间轴和参考图锁
# 全没挂，产出违反「1 主角 + N 底座」的组合铁律。
#
# 分层依据 docs/三场景x27技能_Seedance配方指南.html 的选型体系：
#   主角层 = 决定片型，一条提示词只选 1 个
#   底座层 = 技术件（timeline / 双锁 / 对白节拍 / 分镜格图），跟随主角按需挂载
#   工具层 = 质检 / 流程工具，需求明确提到时才选
#
# 不在映射表里的技能一律归入主角层兜底 —— 宁可多标主角（模型顶多不选它），
# 不能错标底座（会被当成可随意附加的技术件）。
SKILL_LAYERS: dict[str, str] = {
    # ---- 底座层（4 件）
    "seedance-timeline-shot-script": "base",
    "seedance-character-reference-lock": "base",
    "seedance-dialogue-performance-beats": "base",
    "seedance-storyboard-grid-to-video": "base",
    # ---- 工具层（2 件）
    # 注：seedance-production-workflow / seedance-storyboard-writer 两个目录已删除，
    # 条目一并移除。seedance-prompt-library 目录当前也不在（用户 11-02 删了），
    # 条目先留着 —— 不在目录里不报错、只是匹配不到，将来恢复目录时仍能正确分层。
    "seedance-prompt-lint": "tool",
    "seedance-prompt-library": "tool",
    # ---- 主角层（23 件）
    "seedance-ugc-creator-review": "hero",
    "seedance-product-commercial-shotlist": "hero",
    "seedance-cinematic-narrative-short": "hero",
    "seedance-process-transformation-montage": "hero",
    "seedance-food-asmr": "hero",
    "seedance-fashion-lookbook": "hero",
    "seedance-travel-city-walk": "hero",
    "seedance-epic-fantasy-scifi": "hero",
    "seedance-3d-cartoon": "hero",
    "seedance-anime-style-lock": "hero",
    "seedance-horror-suspense": "hero",
    "seedance-meme-comedy": "hero",
    "seedance-music-beat-sync-mv": "hero",
    "seedance-pet-animal": "hero",
    "seedance-car-vehicle": "hero",
    "seedance-sports-extreme": "hero",
    "seedance-game-ui-livestream": "hero",
    "seedance-retro-dv-home-video": "hero",
    "seedance-handheld-ugc-vlog": "hero",
    "seedance-pov-continuous-take": "hero",
    "seedance-combat-choreography": "hero",
    "seedance-time-freeze-rewind": "hero",
    "seedance-stop-motion-cadence": "hero",
}


# ---------------------------------------------------------------- 时间戳格式（脚本判定）
#
# 2026-11-03：lint 技能里加了 S12（[mm:ss-mm:ss] 必须两位补零），但真模型实测
# 把 `[00:09-0:18]` 判成「S12 通过、格式规范、全部两位补零」—— 正则类检查
# 交给模型不可靠。改成代码先扫一遍：能机械修的直接修（不花钱、不会改错），
# 修完再把结果告诉模型，避免它继续判「通过」。

_TIME_RANGE = re.compile(
    r"([\[【])\s*(\d{1,3})\s*[:：]\s*(\d{1,2})\s*([-–—~～至])\s*(\d{1,3})\s*[:：]\s*(\d{1,2})\s*([\]】])"
)


def _scan_timecodes(text: str) -> list[dict]:
    """扫出所有时间区间写法的毛病。[00:09-0:18] → 给出 [00:09-00:18]。"""
    out: list[dict] = []
    for m in _TIME_RANGE.finditer(text or ""):
        raw = m.group(0)
        lb, m1, s1, sep, m2, s2, rb = m.groups()
        mm1, ss1, mm2, ss2 = int(m1), int(s1), int(m2), int(s2)
        # 秒/分超过 59 的当普通文字，不判（避免把 00:90 这种笔误误修成别的东西）
        if max(ss1, ss2, mm1, mm2) > 59:
            continue
        fixed = f"[{mm1:02d}:{ss1:02d}-{mm2:02d}:{ss2:02d}]"
        reasons = []
        if len(m1) < 2 or len(s1) < 2 or len(m2) < 2 or len(s2) < 2:
            reasons.append("分/秒未两位补零")
        if "：" in raw:
            reasons.append("用了全角冒号")
        if sep != "-":
            reasons.append(f"连接符用了「{sep}」")
        if lb != "[" or rb != "]":
            reasons.append("用了全角方括号")
        if reasons:
            out.append({"raw": raw, "fixed": fixed, "reason": "、".join(reasons)})
    return out


def _fix_timecodes(text: str) -> tuple[str, list[dict]]:
    """机械修时间戳格式。只动标点与补零，绝不动秒数数值。"""
    issues = _scan_timecodes(text)
    if not issues:
        return text, []
    out = text
    for it in issues:
        out = out.replace(it["raw"], it["fixed"], 1)
    return out, issues


# ---------------------------------------------------------------- 时间码数值结构（C 方案 · 代码硬校验）
#
# 2026-11-09：此前秒数数值完全交给 lint 技能的 S2/S8（模型自查），而技能里压根
# 没有「单段不能超 5 秒」这一条。实测 11:22 产品片 2/2/5/6/5/5/3/2=30 质检判合格，
# 6 秒段就这么交付了。这里补四道纯算术硬判。
#
# 2026-11-12 P0 修复：**改为只报不改**。
#   旧逻辑会拿「用户原话」里的「每段8秒左右」当全片总时长，把 9/10/9 秒压成 3/2/3 秒，
#   且代码明确「只改数字，画面文字一字未动」，结果稿里「28 秒」和「8 秒」并存、
#   每格 2~3 秒却塞着 9~10 秒的动作 —— 直接产出废片（t3/t4/t7/科普1 实测）。
#   按 E=1：校验只给报告，绝不擅自改已生成的稿；问题列出来由人核对或重新生成。
#   单段上限同步放宽到 10 秒（原 5 秒是代码自写经验值，与模板 3×8=24~30 秒天生打架）。
#
#   ① 每段 1~10 秒  ② 段间首尾相接  ③ 首段从 0 / 末段 = 总时长  ④ 各段之和 = 总时长
#   总时长取值优先级（新）：稿内声明 > 末段终点（退化） > 用户原话（仅作校验，不采信）
TIMING_SEG_MIN = 1          # 单段下限（低于 1 秒平台解析不出）
TIMING_SEG_MAX = 10         # 单段上限（经验值，2026-11-12 由 5 放宽到 10）
TIMING_PLATFORM_MAX = 30    # 单次生成上限，只提醒不改
TIMING_COMPACT_WARN = 1     # 保留常量，兼容旧报告字段

# 「总时长 28 秒」「【时长】28 秒」「总和 28 秒」「时间轴按 28 秒排布」等
_TOTAL_RE = re.compile(
    r"(?:总时长|全片时长|【\s*时长\s*】|时长|总和|合计|时间轴按)"
    r"\s*[:：】]?\s*(?:为|是|=|约)?\s*(\d{1,3})\s*(?:秒|s\b)",
    re.I,
)


def _timing_segments(text: str) -> list[dict]:
    """把稿子里的时间码解析成秒。格式已由 _fix_timecodes 统一，这里只取数值。"""
    out: list[dict] = []
    for m in _TIME_RANGE.finditer(text or ""):
        _lb, m1, s1, _sep, m2, s2, _rb = m.groups()
        mm1, ss1, mm2, ss2 = int(m1), int(s1), int(m2), int(s2)
        # 沿用 _scan_timecodes 的保护：分/秒 > 59 当普通文字，不参与判定
        if max(ss1, ss2, mm1, mm2) > 59:
            continue
        start, end = mm1 * 60 + ss1, mm2 * 60 + ss2
        out.append({
            "raw": m.group(0), "pos": m.span(),
            "start": start, "end": end, "dur": end - start,
        })
    return out


# 局部/分段时长描述：这些「N 秒」说的是某一拍，不是全片总时长。
# 2026-11-12 P0：t4 模板的「每段8秒左右」曾被当成 total=8，把 28 秒的稿压坏。
_LOCAL_DUR_RE = re.compile(
    r"(?:每段|每拍|每镜|每一拍|单段|每片段)\s*\d{1,3}\s*(?:[-–—~～]\s*\d{1,3}\s*)?"
    r"(?:秒|s\b)\s*(?:左右|上下|以内|之内|内|约)?"
    r"|前\s*\d{1,3}\s*(?:秒|s\b)\s*(?:钩子|抓人|铺垫|开场|内)?"
    r"|第\s*\d{1,3}\s*[-–—~～]\s*\d{1,3}\s*(?:秒|s\b)"
    r"|第\s*\d{1,3}\s*(?:秒|s\b)"
    r"|\d{1,3}\s*[-–—~～]\s*\d{1,3}\s*(?:秒|s\b)",
    re.I,
)
# 模板里的时长区间：「【15-30】秒」「15-30 秒」——只当允许范围做校验，不当定值。
_RANGE_RE = re.compile(
    r"(?:【\s*)?(\d{1,3})\s*[-–—~～]\s*(\d{1,3})\s*(?:\s*】)?\s*(?:秒|s\b)",
    re.I,
)


def _user_seconds_detail(user_input: str) -> dict:
    """解析用户输入里的时长信息。

    返回 {"seconds": int|None, "allowed_range": (lo,hi)|None,
          "excluded": [被排除的局部时长描述], "notes": []}

    两条硬规矩（2026-11-12 P0）：
      ① 「每段 N 秒 / 前 N 秒钩子 / 第 a-b 秒」这类局部时长一律不算总时长；
      ② 「【a-b】秒」区间解析为 allowed_range，只做校验，绝不拿去当定值改稿。
    """
    detail: dict = {"seconds": None, "allowed_range": None, "excluded": [], "notes": []}
    if not user_input:
        return detail

    ranges = [
        (int(a), int(b)) for a, b in _RANGE_RE.findall(user_input)
        if 8 <= int(a) <= int(b) <= 300
    ]
    if ranges:
        detail["allowed_range"] = (min(a for a, _ in ranges), max(b for _, b in ranges))

    clean = re.sub(r"【[^】]*】", " ", user_input)
    for m in _LOCAL_DUR_RE.finditer(clean):
        detail["excluded"].append(m.group(0)[:30])
    clean = _LOCAL_DUR_RE.sub(" ", clean)
    clean = _RANGE_RE.sub(" ", clean)   # 无【】的裸区间也剔掉，别把右端点当总时长

    nums = [int(x) for x in re.findall(r"(?<!\d)(\d{1,3})\s*(?:秒|s\b)", clean, re.I)]
    candidates = [v for v in nums if 8 <= v <= 300]
    detail["seconds"] = max(candidates) if candidates else None
    return detail


def _user_seconds(user_input: str) -> int | None:
    """从用户原话里抽总时长（兼容旧调用）。细节见 _user_seconds_detail。"""
    return _user_seconds_detail(user_input)["seconds"]


def _declared_seconds(text: str) -> int | None:
    """从稿子自己写的「总时长 XX 秒」里取值。"""
    m = _TOTAL_RE.search(text or "")
    if not m:
        return None
    v = int(m.group(1))
    return v if 1 <= v <= 300 else None


def _resolve_total(text: str, user_input: str, segs: list[dict]) -> tuple[int, str, dict]:
    """总时长取值（2026-11-12 P0 改）：稿内声明 > 末段终点 > 用户原话（仅校验）。

    旧版把「用户原话」放第一位，错误值（模板里的「每段8秒左右」）会压过稿内
    声明，直接导致时间码被压坏。现在用户原话只用来做一致性提示，绝不参与改稿。
    """
    d = _declared_seconds(text)
    end = segs[-1]["end"] if segs else None
    detail = _user_seconds_detail(user_input)
    u = detail["seconds"]
    meta: dict = {
        "user_seconds": u,
        "allowed_range": detail["allowed_range"],
        "excluded_local": detail["excluded"],
        "notes": [],
        "low_confidence": False,
    }

    if d:
        total, source = d, "稿内声明"
    elif end:
        total, source = end, "末段终点（退化）"
        meta["low_confidence"] = True
    else:
        total, source = (u or 0), "用户原话（无稿内依据）"
        meta["low_confidence"] = True

    if d and end and d != end:
        meta["notes"].append(
            f"稿内声明 {d} 秒与时间码终点 {end} 秒不一致；按稿内声明 {d} 秒校验，本次未改稿。"
        )
        meta["low_confidence"] = True
    if u is not None and u != total:
        meta["notes"].append(
            f"你输入的 {u} 秒与稿内 {total} 秒不一致；已按稿内数值校验，未采信输入值（避免把稿子压坏）。"
        )
        meta["low_confidence"] = True
    elif u is not None:
        source += "（与用户原话一致）"

    rng = detail["allowed_range"]
    if rng and total and not (rng[0] <= total <= rng[1]):
        meta["notes"].append(
            f"模板标注允许区间 {rng[0]}-{rng[1]} 秒，本稿 {total} 秒在区间外，建议核对。"
        )
    return total, source, meta


def _timing_issues(segs: list[dict], total: int) -> list[dict]:
    """四道硬 check，只做加减法，不读画面文字。"""
    issues: list[dict] = []
    for i, s in enumerate(segs, 1):
        if s["dur"] > TIMING_SEG_MAX:
            issues.append({"code": "seg_too_long",
                           "text": f"第 {i} 段 {s['dur']} 秒，超过 {TIMING_SEG_MAX} 秒上限"})
        elif s["dur"] < TIMING_SEG_MIN:
            issues.append({"code": "seg_too_short",
                           "text": f"第 {i} 段 {s['dur']} 秒，短于 {TIMING_SEG_MIN} 秒下限"})
    for i in range(len(segs) - 1):
        if segs[i]["end"] != segs[i + 1]["start"]:
            gap = segs[i + 1]["start"] - segs[i]["end"]
            word = "断档" if gap > 0 else "重叠"
            issues.append({"code": "gap",
                           "text": f"第 {i+1}/{i+2} 段之间{word} {abs(gap)} 秒"})
    if segs[0]["start"] != 0:
        issues.append({"code": "start", "text": f"首段从 {segs[0]['start']} 秒起，不是 00:00"})
    if segs[-1]["end"] != total:
        issues.append({"code": "end",
                       "text": f"末段终点 {segs[-1]['end']} 秒 ≠ 总时长 {total} 秒"})
    if sum(s["dur"] for s in segs) != total:
        issues.append({"code": "sum",
                       "text": f"各段之和 {sum(s['dur'] for s in segs)} 秒 ≠ 总时长 {total} 秒"})
    return issues


def _redistribute(durations: list[int], total: int) -> list[int] | None:
    """按原节奏比例重排段长，尽量保留长短差异；排不出返回 None。

    可行性只有一条：n×下限 ≤ total ≤ n×上限。不成立时不硬凑，交回给第三档处理。
    """
    n = len(durations)
    if n == 0:
        return None
    lo, hi = TIMING_SEG_MIN, TIMING_SEG_MAX
    if not (n * lo <= total <= n * hi):
        return None

    old_sum = sum(max(lo, d) for d in durations) or 1
    out = [min(hi, max(lo, round(d * total / old_sum))) for d in durations]
    diff = total - sum(out)

    # 余量优先摊给原本长的段，节奏才不会被压平
    order = sorted(range(n), key=lambda i: -durations[i])
    guard = 0
    while diff and guard < n * (hi - lo) * 4 + 20:
        moved = False
        for i in order:
            if diff > 0 and out[i] < hi:
                out[i] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[i] > lo:
                out[i] -= 1
                diff += 1
                moved = True
            if diff == 0:
                break
        if not moved:
            break
        guard += 1

    if diff or any(not (lo <= v <= hi) for v in out):
        return None
    return out


def _fmt_range(start: int, dur: int) -> str:
    end = start + dur
    return f"[{start//60:02d}:{start%60:02d}-{end//60:02d}:{end%60:02d}]"


def _sync_declared_total(text: str, total: int) -> tuple[str, int]:
    """[已停用] 把稿内「总时长 N 秒」类声明里的数字同步成 total。

    2026-11-12 P0：这个函数是「28 秒 / 8 秒」两个数字并存的帮凶之一 ——
    它只同步 _TOTAL_RE 命中的那一处，首行「竖屏 9:16，28 秒」漏网。
    现在链路不再调用它，保留仅为兼容外部脚本；请改用 scan_declared_seconds()
    收集全部声明并只报不改。
    """
    changed = 0

    def repl(m):
        nonlocal changed
        whole = m.group(0)
        a, b = m.start(1) - m.start(0), m.end(1) - m.start(0)
        if whole[a:b] == str(total):
            return whole
        changed += 1
        return whole[:a] + str(total) + whole[b:]

    return _TOTAL_RE.sub(repl, text), changed


def _fix_timing_impl(text: str, user_input: str = "") -> tuple[str, dict]:
    """时间码数值结构校验 —— **只报不改**（E=1，2026-11-12 P0）。

    旧版会 _redistribute() 重排秒数并替换时间码，注释还写死「只改数字，
    画面文字一字未动」：一旦总时长取错（模板的「每段8秒左右」被当成 8），
    28 秒的稿就会被压成 8 秒，稿内两个总秒数打架，成片直接报废。
    现在无论有多少 issues，text 都原样返回；报告里给 issues + suggestions
    （建议值，明确标注「未应用」），由人决定核对还是重新生成。
    """
    report: dict = {
        "checked": False, "ok": True, "total": None, "total_source": "",
        "segments": 0, "issues": [], "fixed": [], "suggestions": [], "notes": [],
        "policy": "report_only", "low_confidence": False, "declared_seconds": [],
    }
    segs = _timing_segments(text)
    if len(segs) < 2:
        # 没有时间码 / 只有一段 → 不量、不硬造
        return text, report

    report["checked"] = True
    report["segments"] = len(segs)
    total, source, meta = _resolve_total(text, user_input, segs)
    report["total"], report["total_source"] = total, source
    report["low_confidence"] = meta["low_confidence"]
    report["notes"].extend(meta["notes"])

    if source.startswith("末段终点"):
        report["notes"].append(
            f"没找到明确总时长，按末段终点 {total} 秒作基准；此时「总和 = 总时长」为自证，建议人工核对。"
        )
    if total > TIMING_PLATFORM_MAX:
        report["notes"].append(
            f"本条声明 {total} 秒，超过单次生成 {TIMING_PLATFORM_MAX} 秒上限；"
            "数字已按声明值校验，实际生成仍需拆成多条提示词。"
        )

    # 稿内所有时长声明：多个值互相打架要报出来（旧版只改其中一处，越改越乱）
    declared = scan_declared_seconds(text)
    values = sorted({d["value"] for d in declared})
    report["declared_seconds"] = values
    if len(values) > 1:
        report["issues"].append({
            "code": "declared_seconds_conflict",
            "text": "稿内出现多个互相打架的总时长声明：" + " / ".join(f"{v} 秒" for v in values),
        })
    if values and total and any(v != total for v in values):
        report["issues"].append({
            "code": "declared_vs_total",
            "text": f"稿内声明 {values} 秒与本次校验基准 {total} 秒不一致，请核对",
        })

    report["issues"].extend(_timing_issues(segs, total))
    if report["issues"]:
        report["ok"] = False
        # 建议值只写进 suggestions，绝不写进 fixed（前端会把 fixed 渲染成「已自动修正 N 处」）
        new_durs = _redistribute([s["dur"] for s in segs], total)
        if new_durs:
            cursor, sugg = 0, []
            for i, d in enumerate(new_durs):
                raw = _fmt_range(cursor, d)
                cursor += d
                if raw != segs[i]["raw"]:
                    sugg.append({"index": i + 1, "from": segs[i]["raw"], "to": raw,
                                 "applied": False})
            report["suggestions"] = sugg
        report["notes"].append(
            "按 E=1：本次只报不改，时间码与画面文字一字未动；请人工核对或重新生成。"
        )
    return text, report


def _fix_timing(text: str, user_input: str = "") -> tuple[str, dict]:
    """C 方案主入口（现为只报不改）。任何异常都不许影响出稿，原样返回并记一条说明。"""
    try:
        return _fix_timing_impl(text, user_input)
    except Exception as e:  # noqa: BLE001
        return text, {
            "checked": False, "ok": True, "total": None, "total_source": "",
            "segments": 0, "issues": [], "fixed": [], "suggestions": [],
            "policy": "report_only", "low_confidence": True, "declared_seconds": [],
            "notes": [f"时间码数值校验异常，已跳过（稿子原样交付）：{e}"],
        }


# 语义化别名：链路里用 check_timing 强调「这里只收报告，不改文本」。
check_timing = _fix_timing


def _apply_sanitize(text: str) -> tuple[str, dict]:
    """出稿链路里调用净化的薄封装，返回 (净化稿, 净化报告)。"""
    info = sanitize_delivery(text)
    return info["text"], info


def _sanitize_log_entry(stage: str, info: dict) -> dict:
    return {
        "stage": stage,
        "removed_lines": info["removed_lines"],
        "removed": info["removed"],
        "removed_chars": info["removed_chars"],
        "rolled_back": info["rolled_back"],
        "mode": info["mode"],
        "issues": info["issues"],
    }


def _extract_product_name(user_input: str) -> str:
    """从用户输入里抽产品名。

    带货模板两种写法都要兜住：
      1. 「产品：X」独立成行（t3/t4/t5/t6/t8）；
      2. 产品名只出现在首行【】里（t1/t2/t7：「真人测评【X】」）。
    占位符（【待确认…】）、秒数（15-30）、画幅（9:16…）都不算产品名。
    抽不出返回空串 —— 抽不出就不做保全，绝不猜。
    """
    text = user_input or ""
    m = re.search(r"^产品[:：]\s*(.+)$", text, re.M)
    if m:
        name = m.group(1).strip()
        if name and "待确认" not in name and 3 <= len(name) <= 40:
            return name
    first = text.splitlines()[0] if text else ""
    for g in re.findall(r"【([^】]+)】", first):
        g = g.strip()
        if ("待确认" in g or "9:16" in g or ":" in g
                or re.fullmatch(r"[\d\-—~\s]+", g)):
            continue
        if 3 <= len(g) <= 40:
            return g
    return ""


# ---------------------------------------------------------------- 真人真实感规则
# 2026-11-03：模型写「人物锁定」时只会堆长相形容词（日系／自然裸妆／真实毛孔），
# 而视频里的真实感来自「光位 + 介质缺陷 + 生活痕迹」，不来自长相描写。
# 这里只在生成阶段注入规则 + 生成后按分档做一次硬伤修复，
# 技能结构（SKILL.md）保持原样不动 —— 用户要求技能固定。
#
# 四档判定（顺序不能乱：先排除项，再非人类，再只出手，最后才当真人）：
#   none     无真人出镜（纯产品／空镜）→ 全部跳过
#   nonhuman 外星人／生物／机器人／动画 → 只查光位与介质，不查素人长相
#   hands    只露手（开箱／ASMR）      → 查手部真实感，不查脸／发型／妆
#   human    真人出镜                  → 全套

_SUBJECT_NONE_MARKS = ("无人物出镜", "无真人出镜", "本段无真人", "不出现人物", "纯空镜")
_NONHUMAN_WORDS = ("外星", "生物", "机器人", "机械", "动画", "卡通", "拟人", "玩偶",
                   "动物", "猫", "狗", "昆虫", "细菌", "菌群", "细胞", "病毒")
_HANDS_MARKS = ("只露手", "不露全脸", "手部特写", "只出手", "手部出镜", "只拍双手")

# 强档（带货／UGC）：连「电影感」都不要，要手机随手拍
_FORBID_STRONG = ("日系", "网红", "裸妆", "精致", "完美", "无瑕", "精修", "瓷肌",
                  "4K", "8K", "超清", "Photorealistic", "电影感")
# 标准档（品牌／科普）：允许专业机位与电影感，但空话参数和完美词必须禁
_FORBID_STANDARD = ("完美", "无瑕", "精修", "瓷肌", "4K", "8K", "超清", "Photorealistic")
_NEGATION_WORDS = ("不磨皮", "不美颜", "无美颜", "不修图", "无滤镜")
_LIGHT_WORDS = ("光从", "侧光", "窗光", "逆光", "顺光", "顶光", "自然光", "环境光",
                "阴影", "暗部", "投影", "明暗")
_MEDIA_WORDS = ("手持", "晃动", "对焦漂移", "噪点", "手机", "呼吸", "构图", "焦外")
_HAND_WORDS = ("五指", "手指", "关节", "指甲", "手部")
_NEGATORS = ("不要", "不用", "无", "非", "禁", "别", "取消", "去掉", "不")


def _hits(text: str, words) -> list[str]:
    """命中的词；被否定词修饰的不算（「不要电影感」合规，「电影感」不合规）。

    词本身以「不／无／别」开头的（不磨皮、无瑕）不做豁免检查 ——
    否则「不磨皮不美颜」里的第二个词会被前一个「不」误判成合规。
    """
    low = (text or "").lower()
    out: list[str] = []
    for w in words:
        wl = w.lower()
        needs_neg_check = not w.startswith(("不", "无", "别"))
        pos = 0
        while True:
            i = low.find(wl, pos)
            if i < 0:
                break
            prev = low[max(0, i - 3):i]
            if not needs_neg_check or not any(n in prev for n in _NEGATORS):
                out.append(w)
                break
            pos = i + len(wl)
    return out


def _detect_subject(user_input: str) -> str:
    """判定这段提示词里的「人」属于哪一档。只看渲染文本里的出镜声明，不靠猜。"""
    t = user_input or ""
    if any(m in t for m in _SUBJECT_NONE_MARKS):
        return "none"
    # 只出手：带货的开箱／ASMR 模板会明说；有「出镜人设」的真人段不算
    if any(m in t for m in _HANDS_MARKS) and "出镜人设" not in t:
        return "hands"
    # 出镜人这一行的值才决定是不是非人类 —— 整段里出现「菌群」不算，
    # 科普片营养师讲发酵的段落常常两样都有。
    # 措辞不统一：长片/文案用「出镜人：」，带货 t1/t7 用「出镜人设：」，
    # t4 用「主角人设：」——三种都得认，否则 Vlog 会被误判成只出手。
    m = re.search(r"(?:出镜人(?:设)?|主角人设|主角)[:：]\s*(.+)$", t, re.M)
    who = (m.group(1).strip() if m else "")
    if who:
        return "nonhuman" if any(w in who for w in _NONHUMAN_WORDS) else "human"
    if any(w in t for w in _NONHUMAN_WORDS) and not re.search(r"(女生|男生|营养师|女主|博主|主持人|同事|妈妈|她|他)", t):
        return "nonhuman"
    if re.search(r"(女生|男生|营养师|女主|博主|主持人|同事|妈妈|她|他)", t):
        return "human"
    if re.search(r"(手|指)", t):
        return "hands"
    return "none"


def _realism_tier(user_input: str) -> str:
    """带货要「手机随手拍」，品牌／科普要「专业但人不假」——两套口径不能混用。"""
    return "strong" if "带货" in (user_input or "")[:60] else "standard"


def _check_realism(text: str, subject: str, tier: str) -> dict:
    """硬伤（必须修）与软伤（只提示）分开报。

    硬伤：禁用审美词、否定式描述 —— 模型从案例库里抄来的，直接让画面变假。
    软伤：缺光位、缺介质、缺手部描述 —— 生成阶段已注入规则，改反而容易改坏。
    """
    hard: list[str] = []
    soft: list[str] = []
    if subject == "none":
        return {"hard": hard, "soft": soft}

    if subject in ("human", "hands"):
        forbid = _FORBID_STRONG if tier == "strong" else _FORBID_STANDARD
        hard += [f"禁用审美词「{w}」" for w in _hits(text, forbid)]
        hard += [f"否定式描述「{w}」（改成正向可见细节）" for w in _hits(text, _NEGATION_WORDS)]

    if not _hits(text, _LIGHT_WORDS):
        soft.append("缺光位描述（光从哪来、哪半边在暗部）")
    if tier == "strong" and not _hits(text, _MEDIA_WORDS):
        soft.append("缺镜头介质描述（手持／晃动／对焦漂移／噪点）")
    if subject == "hands" and not _hits(text, _HAND_WORDS):
        soft.append("缺手部真实感描述（五指／关节／指甲）")
    return {"hard": hard, "soft": soft}


def _realism_rules(subject: str, tier: str) -> str:
    """按档位生成注入 system 的规则块。带货与品牌／科普要说的话不一样。"""
    head = "\n\n【人物真实感规则（优先级高于本文件所有结构与案例原文）】\n"
    if subject == "nonhuman":
        return head + (
            "本段主体不是人类（外星／生物／机械／动画角色），不要给它套人类皮肤与妆容描写。"
            "但镜头仍然必须真实：写明光从哪来、哪边落在暗部；写明介质（手持晃动／"
            "偶发对焦漂移／暗部噪点）。画面出现手部时补 realistic hands（五指、关节、指甲）。"
        )
    if subject == "hands":
        return head + (
            "本段只出手部，没有脸，禁止写长相、妆、肤色、发型。\n"
            "手部锁定要写具体：五指数量正确、关节自然、指甲短而干净、手背有细纹与真实肤色不均；"
            "手部与产品接触处的受力、按压、遮挡关系写清楚。\n"
            "光位必须写（光从哪来、手在哪一侧受光、投影落在哪）；"
            + ("介质必须写（手机主摄手持、轻微晃动、偶发对焦漂移、暗部噪点）。\n" if tier == "strong" else "\n")
            + "禁止：日系／网红／精致／完美／无瑕／精修／4K／8K／超清／Photorealistic；"
            "禁止否定式（不磨皮／不美颜），要写就写可见的正向细节。"
        )
    if tier == "strong":
        return head + (
            "带货类要的是「手机随手拍」，不是广告片。人物锁定只写四组，不许堆审美形容词：\n"
            "① 身份锚：普通人长相，不写「日系／网红／美女」；写明脸型是否对称、眼型、鼻梁高低。\n"
            "② 发型与服装：同色同款，另加使用痕迹（褶皱、洗白、磨边、被雨洇深的印子）。\n"
            "③ 真实感锚点：光从哪来（窗光／顶灯／阴天天光）、哪半边脸在暗部、鼻翼与下颌的投影；"
            "皮肤写可见毛孔、鼻翼泛红、额头油光、眼下细纹之类能看见的东西。\n"
            "④ 镜头介质：手机主摄手持、轻微手抖、偶发对焦漂移、曝光小幅呼吸、构图略偏、暗部有数码噪点。\n"
            "禁用：日系／网红／裸妆／精致／完美／无瑕／精修／瓷肌／4K／8K／超清／Photorealistic；"
            "「电影感」只能出现在否定句（不要电影感）。\n"
            "禁用否定式：不磨皮／不美颜／无滤镜 —— 一律改写成正向可见描述。\n"
            "动作不要「看着镜头微笑」，写成正在做事、没注意到镜头。"
        )
    return head + (
        "本段是品牌／科普片的真人段落：允许专业机位、浅景深、电影感，但人不能假。\n"
        "必须写：① 光位（光从哪来、哪半边在暗部、鼻翼与下颌的投影）；"
        "② 皮肤质感（可见毛孔、细微色差、油光或细纹，择二写具体）。\n"
        "禁用空话与完美词：4K／8K／超清／Photorealistic／完美／无瑕／精修／瓷肌；"
        "禁用否定式（不磨皮／不美颜），改写成正向可见细节。"
    )


# ---------------------------------------------------------------- 调度器
class Agent:
    def __init__(self, loader: SkillLoader, cfg: LLMConfig):
        self.loader = loader
        self.cfg = cfg
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if self._client is None or self._client.api_key != self.cfg.api_key:
            self._client = OpenAI(
                api_key=self.cfg.api_key,
                base_url=self.cfg.base_url,
                timeout=300,
            )
        return self._client

    # ------------------------------------------------------------ ① 选 skill

    def _layered_catalog(self) -> str:
        """给调度器看的分层技能清单（2026-11-02 替代平铺版）。

        仍复用 short_desc（≤1024 字符、按句截断），只是按 SKILL_LAYERS
        分成三段、每段带使用规则，让模型一眼看出「主角只能选 1、
        底座跟着主角挂」。
        """
        groups: dict[str, list[str]] = {"hero": [], "base": [], "tool": []}
        for name, s in self.loader.skills.items():
            groups[SKILL_LAYERS.get(name, "hero")].append(f"- {name}：{s.short_desc}")

        return (
            "【主角层】（决定片型，一次只能选 1 个）\n"
            + "\n".join(groups["hero"])
            + "\n\n【底座层】（技术件，跟随主角按需挂载，可与主角同选）\n"
            + "\n".join(groups["base"])
            + "\n\n【工具层】（质检/流程工具，需求明确提到时才选）\n"
            + "\n".join(groups["tool"])
        )

    def pick_skills(
        self,
        user_input: str,
        forced: list[str] | None = None,
        history: list[dict] | None = None,
        last_skills: list[str] | None = None,
    ) -> tuple[list[str], str]:
        """让大模型从技能清单里挑出该用的 skill。

        forced 非空时直接用它，跳过模型判断。
        模型选不出来就返回空列表 —— 不猜，把判断权交回用户。

        history / last_skills 是给【多轮对话】用的（2026-10-16 加）：
            用户第二轮往往只说「再改短点」「换成口播版」——光看这 5 个字，
            模型根本判断不出该用哪个技能，会被误判成「没找到合适技能」。
            所以要把上文（最近几条）+ 上一轮用过的技能一起给它看。

        返回 (技能列表, 来源)。来源取值：
            "user"           用户手动指定，且名称有效
            "model"          模型自己选的
            "reuse"          模型没选出来，沿用上一轮的技能（追问兜底）
            "forced_invalid" 指定了技能，但名字一个都不在库里（→ 当作没匹配，别静默生成）
            "no_catalog"     技能库本身是空的
            "error"          选技能这一步调用模型出错了（接口超时/鉴权/配额等）
        """
        if forced:
            # 2026-10-17 补洞：此前 forced 全无效时会返回 [] 却标成 "user"，
            # 上游据此跳过 no_skill 判断直接进生成；generate 见 system 为空就用
            # 一句通用兜底提示，模型凭空写一份交出去。现在明确区分出来。
            valid = [n for n in forced if n in self.loader.skills]
            return valid, ("user" if valid else "forced_invalid")
        if not self.loader.skills:
            return [], "no_catalog"

        # 组装上文摘要：只带最近 4 条，每条截断，够模型判断「这是不是同一件事」就行
        ctx = ""
        if history:
            recent: list[str] = []
            for m in history[-4:]:
                role = "用户" if m.get("role") == "user" else "助手"
                content = str(m.get("content") or "").replace("\n", " ")
                recent.append(f"  {role}：{content[:400]}")
            if recent:
                ctx = "以下是这段对话之前的内容（节选，用于理解上下文）：\n" + "\n".join(recent) + "\n\n"

        # 把「上一轮用了哪些技能」明写出来。
        # 2026-10-16 实测：只在上文里塞对话内容不够 —— 模型会把「再短一点，
        # 第二个镜头换成特写」判给另一个沾边的技能（workflow 而非 library），
        # 导致同一个需求两轮产出两套风格。直接点名就稳了。
        prev_line = ""
        if last_skills:
            valid_prev = [n for n in last_skills if n in self.loader.skills]
            if valid_prev:
                prev_line = f"上一轮用过的技能是：{'、'.join(valid_prev)}。\n\n"

        catalog = self._layered_catalog()
        judge_prompt = (
            "你是一个调度器。下面是一个「技能包清单」，每条是 `名称：描述`。\n\n"
            f"{catalog}\n\n"
            f"{ctx}"
            f"{prev_line}"
            "现在用户提出了一个需求，请判断需要加载哪些技能包来完成它。\n"
            "规则：\n"
            "1. 只从上面的清单里选，不要编造名称。\n"
            "2. 只选「真正能完成这个需求」的技能。不相关的、只是沾边的，一律不要选。\n"
            "3. 【组合结构】主角层技能一次只能选 1 个，不要同时选两个主角层技能。\n"
            "   选中主角后，按需求追加 0~4 个底座层技能（底座层一共 4 件，对得上才选）：\n"
            "   - 有逐秒时间轴 / 节奏控制需求 → timeline-shot-script\n"
            "   - 画面里有必须保持同一的人 / 产品 / 角色 → character-reference-lock\n"
            "   - 有台词对话、双人问答 → dialogue-performance-beats\n"
            "   - 多镜头顺序要求严格（先出分镜格图再转视频）→ storyboard-grid-to-video\n"
            "4. 用户只是要「检查/质检提示词」，就只选质检类技能 —— 不要连带选生成类。\n"
            "5. 【重要】如果这句话是对上文的追问或修改（例如「再短一点」「换个口播版」\n"
            "   「把第 2 个镜头改一下」「再来一版」），它和上文是同一个需求，\n"
            "   必须沿用「上一轮用过的技能」，不要换成别的技能。\n"
            "6. 如果从清单里找不到能完成这个需求的技能，返回空数组 []。\n"
            "   宁可返回空，也不要勉强选一个沾边的技能。\n"
            "7. 输出严格的 JSON 数组，例如 [\"技能名\"]，不要任何解释。\n\n"
            "组合示例（照这个结构输出）：\n"
            "  需求「真人口播带货 8 秒钩子」→\n"
            "    [\"seedance-ugc-creator-review\", \"seedance-timeline-shot-script\",\n"
            "     \"seedance-character-reference-lock\"]\n"
            "  需求「发酵工艺科普讲解，有问答」→\n"
            "    [\"seedance-ugc-creator-review\", \"seedance-process-transformation-montage\",\n"
            "     \"seedance-dialogue-performance-beats\"]\n"
            "  需求「投放级产品广告大片」→\n"
            "    [\"seedance-product-commercial-shotlist\", \"seedance-timeline-shot-script\"]\n"
            "  需求「检查这段提示词」→ [\"seedance-prompt-lint\"]\n\n"
            f"用户需求：{user_input}"
        )

        # 判断失败要和「确实没匹配」分开报：接口挂了让人去改措辞，纯属白折腾
        err: Exception | None = None
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.model,
                messages=[{"role": "user", "content": judge_prompt}],
                temperature=0,
            )
            raw = (resp.choices[0].message.content or "").strip()
            picked = self._extract_json_array(raw)
        except Exception as e:
            err = e
            picked = []

        picked = [n for n in picked if n in self.loader.skills]
        if picked:
            return picked, "model"

        # 兜底：模型没选出来，但这是在多轮对话里、上一轮刚用过技能 → 沿用。
        # 不然「再改短点」这种追问会被直接拒掉。
        # 注意判断调用失败时也走这条：接口一时不通，但上一轮的技能还知道，
        # 能接着干就接着干，比让人干等强。
        if history and last_skills:
            reuse = [n for n in last_skills if n in self.loader.skills]
            if reuse:
                return reuse, "reuse"

        if err is not None:
            print(f"[选技能] 判断调用失败：{err!r}", flush=True)
            return [], "error"

        return [], "model"

    @staticmethod
    def _extract_json_array(text: str) -> list[str]:
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
            return [str(x) for x in data if isinstance(x, (str, int))]
        except Exception:
            return []

    # 说明：这里原本有个 _main_skill() 兜底 —— 模型选不出来时硬塞一个
    # 名字含 "official"/"seedance25" 的技能。2026-10-16 按用户要求删除。
    # 理由：兜底会掩盖「用户意图不明确」这个信号，把不相关的技能塞进
    # system prompt，产出驴唇不对马嘴的结果。正确做法是如实告诉用户
    # 「没找到合适的技能」，让用户补充或换个说法。


    # ------------------------------------------------------------ ②③ 生成

    # read_reference 工具声明（OpenAI function-calling 格式）。
    # 作用：大附件（如 39 万字节的官方手册）不内联，模型需要时自己取。
    _READ_REF_TOOL = {
        "type": "function",
        "function": {
            "name": "read_reference",
            "description": (
                "读取当前已挂载技能包的参考资料全文。"
                "凡是 system prompt 里标注「需按需读取」的资料，"
                "都必须先调用本工具读出来，再动笔写。"
                "不要凭空推测资料内容，也不要凭通用知识替代它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "技能名，例如 seedance-prompt-library",
                    },
                    "filename": {
                        "type": "string",
                        "description": "资料文件名，例如 style-library.md",
                    },
                },
                "required": ["skill", "filename"],
            },
        },
    }

    # 一次对话里最多允许模型取几次资料（防止反复拉大文件烧 token）
    MAX_REF_READS = 3

    # 单份资料回填给模型的字符上限。
    #
    # 历史：这里原来是 80_000，是拍脑袋定的「防上下文撑爆」。
    # 结果把 seedance-prompt-library 的模板库（106,284 字符）砍掉后 26%，
    # 最后 5 个模板（3d-cartoon / epic-fantasy-scifi / sports-extreme /
    # game-ui-livestream / food-asmr）模型永远看不到，正文里还会冒一句
    # 「资料较长，已截断」。
    #
    # 2026-10-16 实测校准后改成 150_000：
    #   整份模板库 106,284 字符灌进去，输入只有 35,339 tokens（≈0.33 token/字符，
    #   因为它是中英双语，英文约 4 字符/token）。离 64k 上下文上限还很远。
    # 按纯中文估算要保守些（约 0.7 token/字符）→ 150k 字符 ≈ 105k tokens 会超，
    # 所以这个值不要盲目再往上调；真要支持更大的附件，应该改成「按章节取」而不是继续放宽。
    REF_READ_MAX_CHARS = 150_000

    def generate(
        self,
        user_input: str,
        skill_names: list[str],
        history: list[dict] | None = None,
        temperature: float | None = None,
    ) -> dict:
        """加载 skill → 生成提示词。"""
        system, truncated = self.loader.build_system_prompt(skill_names)

        if not system:
            system = "你是一个专业的 AI 视频提示词工程师。请根据用户需求写出分镜提示词。"

        # 全局语言要求：技能模板和案例原文大多是英文，模型会照着输出英文。
        # 2026-10-17 用户要求统一输出中文 —— 在这里对**所有**生成类调用生效
        # （网页、飞书、质检、修正共用 generate，一处加，处处生效）。
        system += (
            "\n\n【输出语言（必须遵守）】最终产出的提示词正文一律使用简体中文："
            "画面描述、镜头语言、主体动作、场景与光影等全部用中文书写；"
            "仅模型名、专有名词和无法翻译的参数可保留英文。所有说明性文字也用中文。"
        )

        # 交付稿纯净度（2026-11-12 P0-2 源头禁令）：
        # 技能库 27/28 个 SKILL.md 的 Workflow 第 5 步要求模型「返回提示词 +
        # 锚定案例链接 + 三条验收清单」，模型是在遵守技能，不是幻觉。这里在技能
        # 内容之后追加一条更高优先级的硬约束（技能文件由上游同步重生成，不能去改）。
        # 出稿链路还有 delivery_gate.sanitize_delivery 做确定性兜底，双保险。
        # 质检技能不走这条 —— 它的产出物本身就是带结构的报告。
        if not any("lint" in n.lower() for n in skill_names):
            system += (
                "\n\n【交付稿纯净度（最高优先级，压过上面技能 Workflow 中任何"
                "「返回案例链接／验收清单」的要求）】"
                "你的回复只能是提示词正文本身，前后不加任何说明。"
                "禁止写 markdown 标题（# 或 ## 开头）、禁止写「锚定案例／案例链接／"
                "时长说明／验收清单／生成后必查」等标签、禁止出现任何 URL、"
                "禁止用 ``` 代码围栏把正文包起来、禁止在正文后附检查清单或分隔线。"
                "注意：【全局约束】【负面清单】等本段画面负面词属于提示词正文，必须保留。"
            )

        # 人物真实感规则：只在「写提示词」时注入，质检调用不注入
        # （质检是拿规则去判别人，别再让规则反过来影响它的判词）。
        # 触发条件是「这段里有没有真人／手部」，与片型无关 ——
        # 带货、品牌、科普只要出现真人就都走这一套。
        if not any("lint" in n.lower() for n in skill_names):
            subject = _detect_subject(user_input)
            if subject != "none":
                system += _realism_rules(subject, _realism_tier(user_input))

        messages = [{"role": "system", "content": system}]
        if history:
            messages.extend(history[-8:])  # 只带最近几轮，防超长
        messages.append({"role": "user", "content": user_input})

        # 有「按需取」的附件时才给模型工具，否则不给（省 token、也免得它乱调）
        # ⚠️ 判断用 info["inline"] 这个「一次定死」的分类标记，
        #    不要用 info["content"] —— 它会被 read_reference 缓存污染。
        lazy_names = [
            f"{n}/{k}"
            for n in skill_names if n in self.loader.skills
            for k, v in self.loader.skills[n].references.items()
            if not v.get("inline")
        ]
        tools = [self._READ_REF_TOOL] if lazy_names else None

        t0 = time.time()
        ref_reads: list[str] = []
        usage = None
        nudged = False
        fallback_reads = 0

        for _ in range(self.MAX_REF_READS * 2 + 3):
            kwargs = {
                "model": self.cfg.model,
                "messages": messages,
                "temperature": self.cfg.temperature if temperature is None else temperature,
            }
            if tools:
                kwargs["tools"] = tools

            resp = self.client.chat.completions.create(**kwargs)
            usage = getattr(resp, "usage", None) or usage
            msg = resp.choices[0].message

            calls = getattr(msg, "tool_calls", None)
            text = msg.content or ""

            if not calls:
                # ---- 兜底 1：模型把工具调用「说」成了文本 ----
                # 2026-10-16 实测：deepseek-chat 约有一半概率不走结构化 tool_calls，
                # 而是吐 DeepSeek 的专有标记：
                #     <｜｜DSML｜｜ invoke name="read_reference">
                #     <｜｜DSML｜｜ parameter name="path">references/style-library.md</...
                # 或干脆写成 `read_reference("style-library.md")` 这种大白话。
                # 结果是 ref_reads 为空、正文只有一百多字，用户看到「我取不到，你贴给我吧」。
                #
                # 对策不解析 DSML 语法（各家格式不同、版本会变），只认一件事：
                #   「已知附件文件名有没有出现在模型的回复里」—— 最稳。
                wanted = [
                    n for n in lazy_names
                    if n.split("/", 1)[-1] in text and n not in ref_reads
                ]
                if wanted and fallback_reads < self.MAX_REF_READS and len(text) < 4000:
                    fallback_reads += 1
                    messages.append({"role": "assistant", "content": text})
                    blocks: list[str] = []
                    for ln in wanted:
                        skill, _, fname = ln.partition("/")
                        try:
                            body = self.loader.read_reference(skill, fname)
                        except Exception as e:
                            body = f"读取失败：{e}"
                        ref_reads.append(ln)
                        if len(body) > self.REF_READ_MAX_CHARS:
                            body = body[: self.REF_READ_MAX_CHARS] + "\n\n…（资料过长，已截断）"
                        blocks.append(f"【{ln}】\n\n{body}")
                    messages.append({
                        "role": "user",
                        "content": (
                            "（系统）已按你的要求读取参考资料，内容如下：\n\n"
                            + "\n\n".join(blocks)
                            + "\n\n请据此产出最终结果，不要再重复请求读取。"
                        ),
                    })
                    continue

                # ---- 兜底 2：连资料都没提，回得又特别短 ----
                # 典型表现：「我没取到这份资料，你贴给我吧」然后反问用户。
                # 补一条提醒逼它去读；只补一次，避免死循环。
                if tools and not ref_reads and not nudged and len(text) < 1500:
                    nudged = True
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "（系统提醒）你还没有读取本技能包的参考资料："
                            f"{'、'.join(lazy_names)}。"
                            "请现在调用 read_reference 工具把它读出来，"
                            "再据此产出最终结果。不要凭空推测，也不要让用户替你贴内容。"
                        ),
                    })
                    continue

                # ---- 兜底 3：资料明明已内联，模型却幻觉出「要读文件」 ----
                # 2026-10-17 实测：cases.md 内联进 system 后，SKILL.md 正文仍写着
                # 「Read references/cases.md」，模型照做去调 read_reference ——
                # 但此时 tools=None（没有可调的工具），上面两个兜底都不覆盖，
                # DSML 调用标记就原样漏进正文（用户看到 <｜DSML｜invoke…>）。
                # 对策：短回复 + 提到要读文件 + 无工具 → 提醒它资料已在上下文里，直接产出。
                if not tools and not nudged and len(text) < 2000 \
                        and ("read_reference" in text or "DSML" in text
                             or any(n.split("/", 1)[-1] in text for n in lazy_names)):
                    nudged = True
                    messages.append({"role": "assistant", "content": text})
                    messages.append({
                        "role": "user",
                        "content": (
                            "（系统）本技能的全部参考资料已经完整包含在系统提示词里，"
                            "没有任何文件需要读取，read_reference 也不可用。"
                            "请忽略刚才的读取动作，直接基于已有资料产出最终结果。"
                        ),
                    })
                    continue
                break   # 模型没要资料，正常结束

            # 把模型的调用意图登记进对话，再逐个执行、回填结果
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.function.name,
                            "arguments": c.function.arguments,
                        },
                    }
                    for c in calls
                ],
            })

            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                    tag = f"{args.get('skill')}/{args.get('filename')}"
                    if tag in ref_reads:
                        # 同一份资料重复请求：不再回灌全文，避免把上下文撑爆
                        result = f"（{tag} 已经给过你了，内容同上，不要重复请求。）"
                    else:
                        text = self.loader.read_reference(
                            args.get("skill", ""), args.get("filename", "")
                        )
                        ref_reads.append(tag)
                        result = text[: self.REF_READ_MAX_CHARS]   # 见 REF_READ_MAX_CHARS 注释
                        if len(text) > self.REF_READ_MAX_CHARS:
                            result += "\n\n…（资料过长，已截断）"
                except Exception as e:
                    result = f"读取失败：{e}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": result,
                })

        content = msg.content or ""

        return {
            "content": content,
            "skills_used": skill_names,
            "truncated": truncated,
            "system_chars": len(system),
            "references_read": ref_reads,       # 本次实际取过哪些资料
            "elapsed": round(time.time() - t0, 2),
            "tokens": {
                "prompt": getattr(usage, "prompt_tokens", None),
                "completion": getattr(usage, "completion_tokens", None),
            } if usage else None,
        }

    # ------------------------------------------------------------ ④ 质检

    def lint(self, prompt_text: str, skill_name: str | None = None, temperature: float = 0.2) -> dict:
        """用质检 skill 检查一段提示词。

        skill_name 指定用哪个质检技能；不传则自动找名字含 lint 的那个。
        """
        if not skill_name:
            skill_name = next(
                (n for n in self.loader.names() if "lint" in n.lower()), None
            )
        if not skill_name or skill_name not in self.loader.skills:
            return {"content": "（未找到质检类 skill，跳过质检）", "skills_used": []}

        # 脚本预检：模型对正则类检查不可靠（会把 [00:09-0:18] 判成「通过」），
        # 所以先把代码扫出来的结论摆进 user，报告末尾再原样附一份，双保险。
        tc_issues = _scan_timecodes(prompt_text or "")
        tc_block = ""
        if tc_issues:
            tc_block = (
                "【脚本预检：以下时间戳格式问题已由代码确认，必须写进「❌ 必改」，"
                "不许判为通过，也不许说「格式规范」】\n"
                + "\n".join(f"- {x['raw']} → 应为 {x['fixed']}（{x['reason']}）" for x in tc_issues)
                + "\n\n"
            )

        r = self.generate(
            user_input=tc_block + f"请质检下面这段视频提示词：\n\n{prompt_text}",
            skill_names=[skill_name],
            temperature=temperature,
        )

        # 报告末尾再附一份代码判定 —— 模型万一还是漏了，用户翻到底也能看到。
        if tc_issues:
            r["content"] = (r.get("content") or "") + (
                "\n\n---\n\n### ❌ 脚本判定（代码算出，不依赖模型）\n"
                + "\n".join(f"- {x['raw']} → 应为 {x['fixed']}（{x['reason']}）" for x in tc_issues)
            )
            r["timecode_issues"] = tc_issues
        return r

    # ------------------------------------------------------------ ⑤ 质检后修正

    # 「质检完发现问题」和「把问题改掉」是两件事：质检报告是给人看的诊断书，
    # 用户真正要的是改好的成稿。2026-10-17 用户发现网页端只出诊断书、不出成稿，
    # 而飞书里有 —— 因为这段逻辑原先只写在 feishu_bot.py 里，没有进 Agent。
    # 现在收进 Agent，网页 / 飞书 / 任何调用方共用同一份提示词与解析规则。
    REVISE_PROMPT = (
        "下面是一段 Seedance 视频提示词，以及它的质检报告。\n"
        "请做两件事：\n"
        "1. 只列「必须修的问题」，最多 5 条，每条一句话，不要展开解释。\n"
        "2. 输出修改后的完整提示词：保留原有结构与格式，把问题全部改掉，"
        "达到可直接复制去生成视频的程度，不要再附加任何检查、说明或总结。\n"
        "严格按以下格式输出（标记【问题】【修改后】原样保留）：\n"
        "【问题】\n"
        "1. ...\n"
        "【修改后】\n"
        "<修改后的完整提示词>"
    )

    @staticmethod
    def _split_revision(content: str) -> tuple[str | None, str | None]:
        """把「【问题】…【修改后】…」切成两段。模型不守格式时整段当修正稿。"""
        text = (content or "").strip()
        if not text:
            return None, None
        m1, m2 = text.find("【问题】"), text.find("【修改后】")
        if m1 == -1 or m2 == -1 or m2 < m1:
            return None, text
        return text[m1 + 4:m2].strip() or None, text[m2 + 5:].strip() or None

    def revise(
        self,
        prompt_text: str,
        lint_text: str,
        skill_name: str | None = None,
        temperature: float = 0.3,
    ) -> dict:
        """按质检报告修正提示词。

        返回 {issues, revised, content, elapsed}：
            issues   给用户看的「必改问题」（最多 5 条，一句话一条）
            revised  改好的完整提示词 —— 复制即可直接拿去生成视频
        两个都可能为 None（质检没内容 / 模型没守格式），调用方自行兜底。
        """
        if not prompt_text or not lint_text:
            return {"issues": None, "revised": None, "content": "", "skipped": True}

        user_input = (
            f"{self.REVISE_PROMPT}\n\n=== 提示词 ===\n{prompt_text}\n\n"
            f"=== 质检报告 ===\n{lint_text}"
        )
        try:
            out = self.generate(
                user_input=user_input,
                skill_names=[skill_name] if skill_name else [],
                temperature=temperature,
            )
        except Exception as e:
            return {"issues": None, "revised": None, "content": "", "error": str(e)}

        content = (out or {}).get("content") or ""
        issues, revised = self._split_revision(content)
        return {
            "issues": issues,
            "revised": revised,
            "content": content,
            "elapsed": (out or {}).get("elapsed"),
            "tokens": (out or {}).get("tokens"),
        }

    # ------------------------------------------------------------ 产品名保全

    def _fix_product_name(self, product: str, content: str) -> str:
        """成稿丢了产品名：一次轻量修复调用把名字加回去。

        只改产品描述处、其余不动；修完仍不含产品名就放弃返回原文 ——
        宁可缺名字也不让一次失败的修复把全文搅乱。
        """
        if not content:
            return content
        user = (
            f"下面这条视频提示词丢掉了产品名「{product}」。"
            f"请在产品锁定／产品首次出现的段落把「{product}」明确写进去"
            f"（袋身印刷区不渲文字的约束保留，产品名写在画面描述里即可），"
            f"其余内容一字不改，直接输出修改后的完整提示词，不要任何说明。\n\n{content}"
        )
        try:
            out = self.generate(user_input=user, skill_names=[], temperature=0.2)
            fixed = (out or {}).get("content") or ""
            return fixed if product in fixed else content
        except Exception:
            return content

    def _fix_realism(self, content: str, issues: list[str], subject: str, tier: str) -> str:
        """硬伤修复：只改人物／手部锁定段与其光线、介质描述，其余一字不动。

        失败策略和产品名修复一致 —— 改坏了（明显缩水、硬伤没减少）就回滚原文。
        宁可留一点小瑕疵，也不让一次失败的修复把整条稿子搅乱。
        """
        if not content or not issues:
            return content
        user = (
            "下面这条视频提示词里的人物描写有不真实的地方：\n- "
            + "\n- ".join(issues)
            + "\n\n请只改「人物／手部锁定段」以及它的光线、介质描述，把上面每条问题逐条改掉：\n"
            "· 禁用词删掉，换成正向可见的具体细节（可见毛孔、鼻翼泛红、额头油光、额角碎发、衣服褶皱等）；\n"
            "· 否定式（不磨皮／不美颜）改写成正向描述；\n"
            "· 缺光位就补「光从哪来、哪半边在暗部、投影落在哪」；\n"
            "· 缺介质就补「手机主摄手持、轻微晃动、偶发对焦漂移、暗部噪点」（仅带货类）；\n"
            "· 若主体不是人类，不要套人类皮肤与妆容描写。\n"
            "其余段落、时间轴、台词、产品锁定、合规约束一律原样保留，一个字都不要改。"
            "直接输出修改后的完整提示词，不要任何说明。\n\n"
            f"{_realism_rules(subject, tier)}\n\n{content}"
        )
        try:
            out = self.generate(user_input=user, skill_names=[], temperature=0.2)
            fixed = (out or {}).get("content") or ""
            if not fixed or len(fixed) < len(content) * 0.6:
                return content                                   # 明显缩水 → 判定改坏
            still = _check_realism(fixed, subject, tier)["hard"]
            return fixed if len(still) < len(issues) else content
        except Exception:
            return content

    # ------------------------------------------------------------ 技能顺序整理

    # 技能绑定（2026-11-02 恢复，做「模型忘了挂底座」的最后一道保险）。
    #
    # 历史：这里曾经是 [["seedance25-official", "seedance-prompt-lint"]]，
    # 2026-10-16 按用户要求清空 —— 当时的绑定把「质检决定权」从用户手里
    # 拿走了（只想检查提示词却被塞进整个编写技能）。
    #
    # 现在恢复的是另一种绑定：只做「主角 → 底座」的自动补齐，不涉及质检类。
    # 调度器的 judge_prompt 已带组合规则和 few-shot，正常情况模型自己会选对；
    # 这里兜的是它偶尔只返回主角一件的情况 —— resolve_pipeline 会自动补齐。
    # 想调整绑定，改下面的列表即可，代码不用动。
    PIPELINES: list[list[str]] = [
        ["seedance-ugc-creator-review", "seedance-timeline-shot-script"],
        ["seedance-product-commercial-shotlist", "seedance-timeline-shot-script"],
        ["seedance-cinematic-narrative-short", "seedance-character-reference-lock"],
        ["seedance-process-transformation-montage", "seedance-timeline-shot-script"],
    ]

    def resolve_pipeline(self, picked: list[str]) -> list[str]:
        """整理技能列表：主角只留 1 个、底座去重、按 PIPELINES 补主角缺的底座。

        2026-11-02 修了一个隐蔽 bug：
            旧写法是「这一行里**任意**一个技能出现，就展开整行」。
            而 timeline-shot-script 同时出现在 4 行绑定里，于是只要它被选中
            （模型几乎总会选它），4 行全部展开 —— 4 个主角被一起拉进来。
            这就是「一句话里冒出 5 个主角 / system 89.9k 字符 /
            所有 cases.md 被截断」真正的成因，跟模型选得贪不贪没关系。
            现在只认每行的第一项（主角），底座被选中不再反向拉主角。
        """
        valid = [n for n in picked if n in self.loader.skills]
        heroes = [n for n in valid if SKILL_LAYERS.get(n, "hero") == "hero"]
        bases = [n for n in valid if SKILL_LAYERS.get(n) == "base"]
        tools = [n for n in valid if SKILL_LAYERS.get(n) == "tool"]

        hero = heroes[0] if heroes else ""       # 多出来的主角直接丢弃，不静默保留
        ordered: list[str] = [hero] if hero else []

        # 补底座：只认这个主角自己那一行
        if hero:
            for line in self.PIPELINES:
                if line[0] != hero:
                    continue
                for n in line[1:]:
                    if n in self.loader.skills and n not in ordered:
                        ordered.append(n)

        for n in bases + tools:
            if n not in ordered:
                ordered.append(n)
        # 兜底：理论上不会剩，留着防止以后加新层时漏项
        for n in valid:
            if n not in ordered:
                ordered.append(n)
        return ordered

    # ------------------------------------------------------------ 完整链路

    @staticmethod
    def _no_skill_text(source: str) -> tuple[str, str]:
        """没选出技能时，按原因给不同的提示。返回 (reason, hint)。

        文案底线：只写用户真能执行的动作。侧栏现在没有「勾选」这回事
        （2026-10-17 改成点一下填召唤词），别再让人去勾选。
        """
        if source == "error":
            return (
                "选技能这一步没跑通：调用模型判断该用哪个技能时出错了。",
                "这不是你描述的问题，别急着改说法。稍等几秒再发一次；"
                "如果一直这样，看一眼后端窗口（start.bat 那个）的报错，"
                "多半是接口超时、key 失效或额度用完了。",
            )
        if source == "no_catalog":
            return (
                "技能库是空的，一个技能都没加载上，所以没法干活。",
                "点侧栏顶部的「重新扫描」；还空的话，检查项目里 skills 目录还在不在、"
                "里面的 SKILL.md 有没有被误删。",
            )
        if source == "forced_invalid":
            return (
                "指定的技能名不在当前技能库里，没法按它来写。",
                "别手填技能名，直接在左侧点技能——会把它的召唤词填进输入框；"
                "或者把需求说清楚，交给系统自己挑。",
            )
        return (
            "没找到能完成这个需求的技能。",
            "换个说法再描述一次（说清要做什么、要做成什么样）。"
            "也可以点左侧任意技能名，把它的召唤词填进输入框，再在后面补上你的需求。",
        )

    def run_full(
        self,
        user_input: str,
        forced: list[str] | None = None,
        do_lint: bool = True,
        history: list[dict] | None = None,
        last_skills: list[str] | None = None,
        on_step: Callable[[str, dict], None] | None = None,
    ) -> dict:
        """一步到位：选 skill → 补齐流水线 → 生成 → 质检。

        on_step 是个回调，可以让前端看到每一步的进展。
        last_skills：上一轮用过的技能，多轮追问时用来兜底（详见 pick_skills）。
        """
        def emit(stage: str, payload: dict):
            if on_step:
                try:
                    on_step(stage, payload)
                except Exception:
                    pass

        # ① 选
        emit("picking", {})
        picked, source = self.pick_skills(
            user_input, forced=forced, history=history, last_skills=last_skills
        )

        # 用户手动指定了技能 → 直接用，不再让模型判断
        # 只有来源是 "user"（即名字有效）才走这条；名字全无效走下面的 no_skill
        if forced and source == "user":
            picked = self.resolve_pipeline(picked)
            emit("picked", {"skills": picked, "source": "user"})
            return self._run_after_pick(user_input, picked, do_lint, history, emit)

        # 追问兜底命中：沿用上一轮技能，前端提示一下，用户看得见
        if source == "reuse" and picked:
            picked = self.resolve_pipeline(picked)
            emit("picked", {"skills": picked, "source": "reuse"})
            return self._run_after_pick(user_input, picked, do_lint, history, emit)

        # 没选出来 → 不兜底，直接把判断权交回用户
        #
        # 2026-10-16 按用户要求：不再硬塞「主技能」。
        # 硬塞的后果是把不相关的技能拼进 system prompt，产出不可控。
        # 2026-10-17 再细分：把「确实没匹配 / 指定名字无效 / 技能库空 / 判断接口出错」
        # 四种情况分开报 —— 前三种该改描述或修配置，最后一种该查服务，
        # 糊成一句「换个说法」会让人白折腾。
        if not picked:
            reason, hint = self._no_skill_text(source)
            emit("no_skill", {"reason": reason, "hint": hint, "kind": source})
            return {
                "skills_used": [],
                "generate": None,
                "lint": None,
                "need_user_action": True,
                "reason_kind": source,
                "message": f"{reason}\n\n{hint}",
            }

        # ①.⑤ 整理技能列表（当前不补齐，只去重校验）
        final = self.resolve_pipeline(picked)
        if final != picked:
            emit("expanded", {"from": picked, "to": final})
        picked = final
        emit("picked", {"skills": picked, "source": "model"})

        return self._run_after_pick(user_input, picked, do_lint, history, emit)

    def _run_after_pick(
        self,
        user_input: str,
        picked: list[str],
        do_lint: bool,
        history: list[dict] | None,
        emit: Callable[[str, dict], None],
    ) -> dict:
        """技能已确定，跑生成 + 质检。抽出来给「手动指定」和「模型自选」共用。"""
        # ②③ 生成
        emit("generating", {"skills": picked})
        gen = self.generate(user_input, picked, history=history)

        # 时间戳格式：纯机械问题，代码直接改 —— 比让模型改稳，也不花钱。
        # 2026-11-03 实测：lint 技能加了 S12 后，模型仍把 [00:09-0:18] 判成
        # 「格式规范、全部两位补零」，所以不能指望它。这里只补零、统一连接符，
        # 不动秒数数值；数值本身错（段间断档、跨度不符）仍交给质检。
        gen["content"], tc_fixed = _fix_timecodes(gen["content"])
        if tc_fixed:
            emit("timecode_fixed", {
                "count": len(tc_fixed),
                "items": [f"{x['raw']} → {x['fixed']}" for x in tc_fixed],
            })

        # 产品名保全：模板里写明的产品名不许在成稿里丢失。
        # 2026-11-03 实测：纯产品片/反差钩子会把「产品：百芙乐蓝莓后生元」
        # 改写成「一支哑光自立袋」这种无名描述，质检也没抓出来。
        # 生成后先查一道，修正稿出来再查一道；丢了就做一次轻量修复调用。
        prod = _extract_product_name(user_input)
        if prod and prod not in gen["content"]:
            emit("product_missing", {"product": prod})
            gen["content"] = self._fix_product_name(prod, gen["content"])

        # 真实感兜底（2026-11-03）：模型爱从案例库抄「日系／裸妆／4K／不磨皮」，
        # 这些是硬伤 —— 让人物一眼假，修一次；缺光位／缺介质只提示不修，
        # 因为生成阶段已注入规则，硬改反而容易把好句子改坏。
        subject = _detect_subject(user_input)
        tier = _realism_tier(user_input)
        realism = _check_realism(gen["content"], subject, tier)
        if realism["hard"]:
            emit("realism_fix", {"issues": realism["hard"], "subject": subject})
            gen["content"] = self._fix_realism(gen["content"], realism["hard"], subject, tier)
            recheck = _check_realism(gen["content"], subject, tier)
            realism = {
                "hard": recheck["hard"],
                "soft": recheck["soft"],
                "fixed": not recheck["hard"],
            }
        realism.setdefault("fixed", False)
        realism["subject"] = subject
        realism["tier"] = tier

        # 本次产出物本身就是「质检报告」时不走净化 —— 报告天然带 markdown 标题
        # 和编号清单，按交付稿规则删会把报告删残。带货/品牌/科普才需要纯净正文。
        is_lint_only = bool(picked) and all("lint" in n.lower() for n in picked)

        # 交付闸门①（2026-11-12 P0-2）：先去掉技能 Workflow 带出来的文档外壳，
        # 再让 lint / revise 看到干净稿。否则 lint 会把「# 标题／锚定案例链接／
        # 验收清单」当正文检查，revise 还会原样把外壳带回来。
        # 净化是幂等的，长片后续 prepare_delivery 再跑自己的完整版清洗不会冲突。
        sanitize_log: list[dict] = []
        if not is_lint_only:
            gen["content"], gen_san = _apply_sanitize(gen["content"])
            sanitize_log.append(_sanitize_log_entry("generate", gen_san))
            if gen_san["removed_lines"] or gen_san["rolled_back"]:
                emit("delivery_sanitized", {
                    "stage": "generate",
                    "removed_lines": gen_san["removed_lines"],
                    "rolled_back": gen_san["rolled_back"],
                })

        # 时间码数值结构校验（2026-11-12 P0-1 起改为**只报不改**）：
        # 旧版在这里 _fix_timing 重排秒数，把 28 秒的稿压成 8 秒；修正稿出来后
        # 又压一次，形成「lint 明明报了 S8 必改、交付稿却还是坏的」的闭环。
        timing_reports: list[dict] = []
        gen_timing = check_timing(gen["content"], user_input)[1]
        if gen_timing.get("issues") or gen_timing.get("notes"):
            timing_reports.append({"stage": "generate", **gen_timing})
        if gen_timing.get("issues"):
            emit("timing_issues", {
                "stage": "generate",
                "count": len(gen_timing["issues"]),
                "items": [x["text"] for x in gen_timing["issues"][:5]],
            })

        emit("generated", {"chars": len(gen["content"])})

        result = {
            "skills_used": picked,
            "generate": gen,
            "lint": None,
            "realism": realism,
            "timecode_fixed": tc_fixed,
        }

        # ④ 质检：固定环节（2026-10-16 22:50 按用户决定恢复）
        #
        # 历史：PIPELINES 清空后，质检退化成「模型真的选中 lint 才跑」。
        # 2026-10-16 实测三次真实调用：口语化需求（「写一条 15 秒美食短视频」）
        # —— 不论自动选还是手动锁定风格 —— lint 都返回 None，一次没跑过；
        # 而顶栏一直写着「自动质检」，标签名不副实。所以改成：
        # 只要 do_lint 打开，生成完就一律查一遍；本次没选中 lint 就用仓库里的兜底。
        #
        # 唯一例外：本次**生成的技能本身就是质检技能**（用户只说了「检查这段提示词」），
        # 那时产出物已经是质检报告了，再拿去审一遍等于「审报告」，纯浪费，跳过。
        gen_is_lint = is_lint_only

        lint_skill = next((n for n in picked if "lint" in n.lower()), None)
        if not lint_skill:
            lint_skill = next((n for n in self.loader.names() if "lint" in n.lower()), None)

        if gen_is_lint:
            # 产出物本身就是质检报告，标个记号让前端按「质检报告」渲染
            result["lint_only"] = True

        if do_lint and lint_skill and gen["content"] and not gen_is_lint:
            emit("linting", {"skill": lint_skill, "auto": lint_skill not in picked})
            try:
                result["lint"] = self.lint(gen["content"], skill_name=lint_skill)
                emit("linted", {"chars": len(result["lint"]["content"])})
            except Exception as e:
                result["lint"] = {"content": f"（质检失败：{e}）", "skills_used": []}
                emit("linted", {"error": str(e)})
        elif do_lint and not lint_skill:
            emit("lint_skipped", {"reason": "仓库里没有质检技能，跳过质检"})

        # ⑤ 修正：质检报告只说明「哪里不对」，用户要的是「改好的成稿」。
        # 2026-10-17 补齐 —— 此前这段只在 feishu_bot.py 里，网页端永远看不到 ③。
        # 修正失败不影响主流程：生成和质检结果照样返回，只是少了定稿那一段。
        lint_text = (result.get("lint") or {}).get("content") or ""
        if lint_text and gen["content"] and not gen_is_lint:
            gen_skill = next(
                (n for n in picked if "lint" not in n.lower()),
                picked[0] if picked else None,
            )
            emit("revising", {"skill": gen_skill})
            try:
                rev = self.revise(gen["content"], lint_text, gen_skill)
            except Exception as e:
                rev = {"issues": None, "revised": None, "content": "", "error": str(e)}
            if rev.get("revised") or rev.get("issues"):
                # 修正稿也可能把产品名改丢 —— 修正后补最后一道检查
                if rev.get("revised") and prod and prod not in rev["revised"]:
                    rev["revised"] = self._fix_product_name(prod, rev["revised"])

                # 交付闸门②：修正稿是「复制即用」和资料库存档的那一份，
                # 模型改稿时可能又带回外壳，或又写出不合法的时间码 —— 再净化、
                # 再校验一次（都只报不改，绝不擅自改已生成的稿）。
                if rev.get("revised"):
                    rev["revised"], rev_san = _apply_sanitize(rev["revised"])
                    sanitize_log.append(_sanitize_log_entry("revision", rev_san))
                    if rev_san["removed_lines"] or rev_san["rolled_back"]:
                        emit("delivery_sanitized", {
                            "stage": "revision",
                            "removed_lines": rev_san["removed_lines"],
                            "rolled_back": rev_san["rolled_back"],
                        })
                    rev_timing = check_timing(rev["revised"], user_input)[1]
                    if rev_timing.get("issues") or rev_timing.get("notes"):
                        timing_reports.append({"stage": "revision", **rev_timing})
                    if rev_timing.get("issues"):
                        emit("timing_issues", {
                            "stage": "revision",
                            "count": len(rev_timing["issues"]),
                            "items": [x["text"] for x in rev_timing["issues"][:5]],
                        })

                result["revision"] = rev
                emit("revised", {"chars": len(rev.get("revised") or "")})
            else:
                emit("revise_skipped", {
                    "reason": rev.get("error") or "模型没有给出修正稿，已跳过错",
                })

        # 事后容量核算（2026-11-09，A 方案已回退）：
        # C 只保证「数字自洽」，这里再量一遍「模型自己排的秒数够不够演完它自己写的内容」。
        # 只报不改 —— 不删动作、不压内容、不动时间码；中文动作切分有误报，
        # 所以报告里给估算依据和建议秒数，由人判断。
        # 只算最终交付的那一份（revision.revised 优先，其次 generate.content，
        # 与前端「复制即用」取的是同一份），避免第一稿/修正稿重复弹同一条。
        final_text = (result.get("revision") or {}).get("revised") or gen["content"]
        cap = capacity_report(final_text, user_input)
        if cap.get("notes"):
            timing_reports.append({"stage": "capacity", **cap})
        result["timing"] = timing_reports

        # 交付闸门③：最终复检（只报不改）。把结论挂在 result 上，前端 / 存档 /
        # 长片 fixed_block_check 都能看见；命中问题绝不自动改稿，按 E=1 由人决定。
        try:
            if is_lint_only:
                final_check = {
                    "ok": True, "issues": [], "warnings": [],
                    "cleaned_text": final_text, "sanitize": {}, "stats": {},
                    "skipped": "本次产出物是质检报告，跳过交付稿纯净度复检",
                }
            else:
                final_check = check_delivery(final_text, user_input)
        except Exception as e:  # noqa: BLE001 —— 复检失败绝不能拖垮出稿
            final_check = {
                "ok": True, "issues": [], "warnings": [],
                "cleaned_text": final_text, "sanitize": {},
                "stats": {}, "error": f"最终复检异常（稿子原样交付）：{e}",
            }
        result["final_check"] = final_check
        result["sanitize"] = sanitize_log
        if final_check.get("issues"):
            emit("delivery_issues", {
                "count": len(final_check["issues"]),
                "items": [x["text"] for x in final_check["issues"][:5]],
            })
        return result

