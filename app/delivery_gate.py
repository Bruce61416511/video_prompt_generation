# -*- coding: utf-8 -*-
"""最终交付闸门：净化（去文档外壳）+ 复检（只报不改）。

2026-11-12 P0 合并修复新增。

背景：技能库 27/28 个 SKILL.md 的 Workflow 第 5 步明确要求模型
「返回提示词 + 锚定案例链接 + 三条验收清单」，于是带货单条的「复制即用」稿
会带一整层文档外壳（# 标题 / 外链 / ``` 围栏 / 说明 / 清单）。
前端 unfence() 只在开头就是 ``` 时生效，长片有 BODY_INSTRUCTION + clean_model_output
兜住，单条带货全程裸奔。这里在出稿链路里补一道确定性的净化 + 复检。

与 fixed_block.py::clean_model_output 的区别（不要直接复用）：
  * 那个函数只删「整行围栏标记」和「含 goodcase.ai 的行」，删不掉 t1 的标题、
    t8 的无外链说明与清单；
  * 它的「锚点复述／自创锁定段」规则是长片专属，套到带货会误删正文。
本模块只做通用的「去外壳」最小子集，长片继续用完整版。

E=1 原则：check_delivery 只报不改，绝不擅自改写已生成的稿。
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- 外壳标记
# 约定：只删「整行」外壳，绝不碰提示词正文里的任何一句画面描述。
# 【全局约束】／【负面清单】这类本段画面负面词属于正文，一律保留。

_FENCE_RE = re.compile(r"^\s{0,3}(?:`{3,}|~{3,})[^\n]*$")
_URL_RE = re.compile(r"https?://|goodcase\.ai|goodcase\.com", re.I)
_RULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}\ufeff?\s{0,3}#{1,6}\s+\S")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s+\S")
_CHECKLIST_RE = re.compile(r"^\s{0,3}(?:[-•*]\s+|\d{1,2}\s*[.、)]\s+|[✅✓☑]\s*)")

_LABEL_RE = re.compile(
    r"^\s{0,3}\ufeff?\s{0,3}(?:\*{1,2}|__)?\s*"
    r"(?:锚定案例|选用锚点案例|参考案例|案例链接|锚点案例|时长说明|"
    r"三条验收清单|验收清单|检查清单|快速检查|交付说明|使用说明|"
    r"可直接复制的提示词|可直接复制的\s*Seedance\s*提示词|最终提示词|"
    r"生成后必查|生成后必查三条|提示词正文|说明)"
    r"\s*(?:\*{1,2}|__)?\s*[:：]?",
    re.I,
)

_META_RE = re.compile(
    r"^\s{0,3}(?:——|==|--)?\s*"
    r"(?:本段输出规则|本片锚点|输出规则|输出格式要求|本段任务|本片设定复述|"
    r"以下是给模型的|以下为内部工作指令)"
)

# 正文保护：命中这些的行即便长得像清单/标题也不删（时间码、锁定块、参考图引用）
_KEEP_RE = re.compile(r"\[\d{1,3}\s*[:：]|【|@image|@Image|台词\s*[:：「]")

_MAX_ROLLBACK_RATIO = 0.30   # 清洗量超过 30% → 回滚原文，宁可留外壳也不误删正文


def _agent_mod():
    """运行时再导入 agent，避开 agent.py 顶层 import 本模块造成的循环。"""
    from . import agent
    return agent


def _line_body(line: str) -> str:
    return line.rstrip("\r\n").strip()


def _shell_kind(line: str, in_tail: bool) -> str | None:
    """判断一整行是不是文档外壳；返回可读的类别名，不是外壳则返回 None。"""
    s = _line_body(line)
    if not s:
        return None
    if _FENCE_RE.match(line.rstrip("\r\n")):
        return "代码围栏"
    if _URL_RE.search(s):
        return "案例外链"
    if _LABEL_RE.match(s):
        return "说明标签"
    if _META_RE.match(s):
        return "内部元指令"
    if _RULE_RE.match(s):
        return "分隔线"
    if _HEADING_RE.match(line.rstrip("\r\n")) and not _KEEP_RE.search(s):
        return "文档标题"
    if in_tail and _QUOTE_RE.match(line.rstrip("\r\n")) and not _KEEP_RE.search(s):
        return "说明引用"
    if in_tail and _CHECKLIST_RE.match(line.rstrip("\r\n")) and not _KEEP_RE.search(s):
        return "验收清单项"
    return None


def _finish(raw: str, clean: str, removed: list[str], mode: str) -> dict[str, Any]:
    """统一收尾：30% 回滚保护 + 统计。"""
    ratio = (len(raw) - len(clean)) / max(len(raw), 1)
    if ratio > _MAX_ROLLBACK_RATIO:
        return {
            "text": raw,
            "removed": [],
            "removed_lines": 0,
            "removed_chars": 0,
            "rolled_back": True,
            "mode": mode,
            "issues": [{
                "code": "sanitize_rollback",
                "text": (f"净化会删掉 {ratio:.0%} 的字数，超过 {_MAX_ROLLBACK_RATIO:.0%} 保护线，"
                         "已回滚原文（宁可留外壳也不误删正文），请人工检查这份稿。"),
            }],
            "stats": {"chars_before": len(raw), "chars_after": len(raw), "ratio": 0.0},
        }
    # 没有删掉任何一行时原样返回，保证幂等且逐字不变
    if not removed:
        clean = raw
    return {
        "text": clean,
        "removed": removed,
        "removed_lines": len(removed),
        "removed_chars": len(raw) - len(clean),
        "rolled_back": False,
        "mode": mode,
        "issues": [],
        "stats": {
            "chars_before": len(raw),
            "chars_after": len(clean),
            "ratio": round(ratio, 4),
        },
    }


def sanitize_delivery(text: str | None) -> dict[str, Any]:
    """去掉交付稿的文档外壳，返回净化结果（不抛异常、可重复调用）。

    优先级：
      ① 存在成对 ``` 围栏 → 直接取围栏之间的内容（这是最精确的「正文」边界）；
      ② 否则按行删标题 / 标签 / 外链 / 围栏 / 分隔线 / 元指令 / 尾部清单；
      ③ 清洗量 > 30% 回滚原文并记 issue。

    保留：时间码、台词、【全局约束】等负面词、产品锁定、参考图 @ 引用。
    """
    raw = text or ""
    if not raw.strip():
        return _finish(raw, raw, [], "empty")

    lines = raw.splitlines(keepends=True)
    fence_idx = [i for i, ln in enumerate(lines)
                 if _FENCE_RE.match(ln.rstrip("\r\n"))]

    # ① 成对围栏：围栏内即正文，围栏外的标题/说明/清单整块丢掉
    if len(fence_idx) >= 2:
        a, b = fence_idx[0], fence_idx[1]
        body = "".join(lines[a + 1:b])
        if len(body.strip()) >= 0.45 * max(len(raw.strip()), 1):
            dropped = lines[:a + 1] + lines[b:]
            removed = [f"{_line_body(x)[:60]}" for x in dropped if _line_body(x)]
            return _finish(raw, body.strip(), removed, "fence_pair")

    # ② 逐行清洗。in_tail：遇到「验收清单/生成后必查」这类标题后，
    #    后续紧邻的编号/项目符号行一起视为外壳。
    out_lines: list[str] = []
    removed: list[str] = []
    in_tail = False
    tail_budget = 0
    for ln in lines:
        s = _line_body(ln)
        kind = _shell_kind(ln, in_tail)
        if kind:
            removed.append(f"[{kind}] {s[:60]}")
            if kind == "文档标题" and re.search(r"验收|必查|检查清单|清单", s):
                in_tail, tail_budget = True, 10
            elif kind == "说明标签" and re.search(r"验收|清单|必查", s):
                in_tail, tail_budget = True, 10
            continue
        if in_tail:
            tail_budget -= 1
            if tail_budget <= 0:
                in_tail = False
        out_lines.append(ln)

    clean = "".join(out_lines)
    if removed:
        clean = clean.strip()
    return _finish(raw, clean, removed, "by_line")


# ---------------------------------------------------------------- 时长声明扫描

_HEAD_TOTAL_RE = re.compile(r"^\s*(?:【[^】]*】)?[^。\n]{0,24}?(\d{1,3})\s*秒")
_HEAD_SIGNAL_RE = re.compile(r"竖屏|横屏|9:16|16:9|单条|全片|片长|时长|总时长")


def scan_declared_seconds(text: str | None) -> list[dict]:
    """收集稿内所有「总时长 N 秒」式声明（含首行「9:16 竖屏，28 秒」写法）。

    只做收集，不判断对错；判断交给 check_delivery / check_timing。
    刻意保守：只扫前 12 行、只认 8~300 秒、避开时间码行，宁可漏报也不误报。
    """
    agent = _agent_mod()
    out: list[dict] = []
    for m in agent._TOTAL_RE.finditer(text or ""):
        v = int(m.group(1))
        if 8 <= v <= 300:
            out.append({"value": v, "source": "声明句", "at": m.group(0)[:48]})
    for i, ln in enumerate((text or "").splitlines()[:12]):
        s = ln.strip()
        if not s or _KEEP_RE.search(s) or not _HEAD_SIGNAL_RE.search(s):
            continue
        m = _HEAD_TOTAL_RE.match(s)
        if not m:
            continue
        v = int(m.group(1))
        if not (8 <= v <= 300):
            continue
        if any(x["value"] == v and x["source"] == "首行声明" for x in out):
            continue
        out.append({"value": v, "source": "首行声明", "at": s[:48]})
    return out


# ---------------------------------------------------------------- 最终复检

def check_delivery(text: str | None, user_input: str = "") -> dict[str, Any]:
    """最终交付复检：**只报不改**（E=1）。返回 issues / warnings / cleaned_text / stats。

    检查：稿内时长声明是否唯一、声明值 == 末段时间码终点、时间码结构、
    外壳残留、字数上限。不修改任何已生成的稿，只把问题列出来给用户看。
    """
    san = sanitize_delivery(text)
    clean = san["text"]
    issues: list[dict] = list(san["issues"])
    warnings: list[dict] = []

    agent = _agent_mod()

    # 净化后仍残留的外壳（说明净化没兜住，要报出来，但绝不自动再删）
    for ln in (clean or "").splitlines():
        s = ln.strip()
        if _URL_RE.search(s):
            issues.append({"code": "external_link", "text": f"交付稿仍含外链：{s[:60]}"})
        if _FENCE_RE.match(s):
            issues.append({"code": "fence_left", "text": "交付稿仍含 ``` 代码围栏"})
        if _META_RE.match(s):
            issues.append({"code": "meta_left", "text": f"交付稿仍含内部元指令：{s[:60]}"})
    # 同类问题只留一条，避免刷屏
    uniq: list[dict] = []
    seen: set[str] = set()
    for x in issues:
        if x["code"] in seen:
            continue
        seen.add(x["code"])
        uniq.append(x)
    issues = uniq

    segs = agent._timing_segments(clean)
    declared = scan_declared_seconds(clean)
    values = sorted({d["value"] for d in declared})

    if len(values) > 1:
        issues.append({
            "code": "declared_seconds_conflict",
            "text": "稿内出现多个互相打架的总时长声明：" + " / ".join(f"{v} 秒" for v in values),
        })
    if segs and values:
        end = segs[-1]["end"]
        for v in values:
            if v != end:
                issues.append({
                    "code": "declared_vs_timeline",
                    "text": f"稿内声明 {v} 秒，但时间码终点是 {end} 秒，两者不一致",
                })

    total = values[0] if values else (segs[-1]["end"] if segs else 0)
    if len(segs) >= 2 and total:
        for it in agent._timing_issues(segs, total):
            issues.append({
                "code": "timing_" + it["code"],
                "text": it["text"] + "（本次只报不改，请人工核对或重新生成）",
            })

    if len(clean) > 15000:
        issues.append({
            "code": "too_long",
            "text": f"本条 {len(clean)} 字符，超过平台 15000 字符上限，建议拆段",
        })

    u = agent._user_seconds(user_input)
    if u and total and u != total:
        warnings.append({
            "code": "user_seconds_mismatch",
            "text": f"你输入的 {u} 秒与稿内 {total} 秒不一致；本次按稿内数值校验，未改动稿子",
        })

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "cleaned_text": clean,
        "sanitize": san,
        "stats": {
            "chars": len(clean),
            "segments": len(segs),
            "declared_seconds": values,
            "timeline_end": segs[-1]["end"] if segs else None,
            "removed_lines": san["removed_lines"],
            "rolled_back": san["rolled_back"],
        },
    }
