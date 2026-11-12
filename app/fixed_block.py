# -*- coding: utf-8 -*-
"""长片固定块：全片唯一数据源（2026-11-12）。

彻底改 1/2 之后的机制：
  - F 由代码生成，全片只有一份、8 行恒定，每段逐字相同；
  - F 从不进入发给模型的 message，模型只写本段正文 B；
  - 交付稿 delivery = F + B，由后端组装，复制 / 下载 / 存档统一取它；
  - 设定卡必填校验在生成前拦住空值，杜绝「按主题自定」「【待填】」进稿。

本模块只依赖标准库，可以脱离服务单独跑测试。
"""
from __future__ import annotations

import re

HEADING = "—— 固定块·全片不变（每段一致，不要改）——"

# (输出标签, 设定卡 key, 恒定约束语, 是否必填)
# 约束语与「值」解耦：不管用户填没填，规则行都在，填了值也不会把规则丢掉。
FIXED_FIELDS = (
    ("产品", "产品", "全片同一产品，不许中途换", True),
    ("产品拆件", "拆件", "按拆件复原外观，不许改版、不许渲包装文字", True),
    ("画幅", "画幅", "生成时必须在豆包选中同一比例，否则构图会被裁", False),
    ("画风", "画风", "全片必须统一成同一种画风，不要中途混用", True),
    ("出镜人", "主角", "全片同一人必须同一张脸、同一身衣着", True),
    ("合规红线", "红线", "", False),
    ("不变项", "不变项", "全程锁定，不许中途更换", True),
)

REQUIRED_LABELS = tuple(label for label, _k, _c, req in FIXED_FIELDS if req)

DEFAULT_FRAME = {"brand": "16:9 横屏", "kepu": "9:16 竖屏"}
DEFAULT_REDLINE = {
    "brand": (
        "不说治疗/改善/增强免疫力/通便/降三高；画面不生成美术字/标语"
        "（口播台词照常，字卡后期贴）；不点名不贬低任何竞品"
    ),
    "kepu": (
        "不承诺功效、不说「提高 N 倍」、不把体外实验说成人体效果、"
        "检出≠有效、不引用无出处数据；画面不生成美术字/标语（口播台词照常）"
    ),
}

# 只写全片级锚点，不写「本段产品出镜时@」这类随段变化的条件。
REF_LINE = (
    "参考图（生成时在豆包 @）：@人物参考 = 出镜人同款形象；"
    "@产品参考 = 产品外观（全片统一）"
)

BODY_INSTRUCTION = (
    "—— 本段输出规则 ——\n"
    "只写这一段的内容：开场画面、中段推进、结尾定格、台词等本段要素。\n"
    "不要输出任何全片级内容：固定块、全片设定、产品/人物锁定、画幅、画风、"
    "合规红线、参考图、不变项 —— 这些由系统统一注入，重复写会被清洗掉。"
)

# 兜底文案：一旦出现在成稿里就说明必填校验没拦住，verify 会报出来。
BAD_PLACEHOLDERS = (
    "按主题自定",
    "【待填——写到能照着复原外观】",
    "未指定（按主题自定）",
)


def validate_setting(setting: dict) -> list[str]:
    """返回没填的必填项标签。空列表 = 可以生成。"""
    s = setting or {}
    return [
        label
        for label, key, _constraint, required in FIXED_FIELDS
        if required and not str(s.get(key) or "").strip()
    ]


def build_fixed_block(setting: dict, kind: str = "brand") -> str:
    """生成全片唯一一份固定块。恒定 8 行正文 + 1 行标题，不依赖 seg。"""
    s = setting or {}
    lines = [HEADING]
    for label, key, constraint, _required in FIXED_FIELDS:
        value = str(s.get(key) or "").strip()
        if not value and key == "画幅":
            value = DEFAULT_FRAME.get(kind, DEFAULT_FRAME["brand"])
        if not value and key == "红线":
            value = DEFAULT_REDLINE.get(kind, DEFAULT_REDLINE["brand"])
        # 缺值只标「（未填写）」，绝不写「按主题自定」这类把不确定性塞进稿子的话。
        line = f"{label}：{value}" if value else f"{label}：（未填写）"
        if constraint:
            line += f" —— {constraint}"
        lines.append(line)
    lines.append(REF_LINE)
    return "\n".join(lines)


def body_instruction() -> str:
    """拼在发给模型的正文骨架开头，明确禁止模型再写全片级内容。"""
    return BODY_INSTRUCTION


# ---------------------------------------------------------------- 文本清洗

_OLD_HEAD_RE = re.compile(
    r"^\s*(?:——[^—\n]*(?:固定块|全片设定|全片不变|全片一致)[^—\n]*——"
    r"|【[^】\n]*(?:固定块|全片设定|全片不变|全片一致)[^】\n]*】)\s*$"
)
_LOCK_HEAD_RE = re.compile(
    r"^\s*(?:"
    r"【[^】\n]*(?:固定|锁定|全片|全局|不变量)[^】\n]*】"
    r"|——[^—\n]*(?:固定块|全片设定)[^—\n]*——"
    r"|#[^#\n]*(?:固定块|全片设定|全局设定)"
    r")\s*$"
)


def strip_fixed_block(text: str) -> str:
    """从模板里剥掉固定块段（防御用）。

    正常情况下 render_segment 已经不输出固定块，这里是 no-op；
    万一部署时 longform.py 没同步、或有人塞了旧模板，也能保证 F 不进模型输入。
    """
    out, skipping = [], False
    for line in (text or "").splitlines():
        if not skipping and _OLD_HEAD_RE.match(line):
            skipping = True
            continue
        if skipping:
            if not line.strip():
                skipping = False
                out.append(line)
                continue
            if line.strip().startswith("——"):
                skipping = False
            else:
                continue
        out.append(line)
    return "\n".join(out).strip()


def clean_model_output(text: str) -> tuple[str, list[str], list[str]]:
    """删掉模型自创的「【产品锁定】」之类全片级段落。

    保守策略：只删标题行 + 其后最多 6 行连续非空行，遇到空行/新标题立刻停；
    若删完剩下不到原文 30%，放弃清洗并报 issue —— 绝不把正文吃掉。
    返回 (清洗后正文, 删掉的标题, 问题列表)。
    """
    raw = (text or "").strip()
    if not raw:
        return "", [], []
    out, removed, skipping, dropped = [], [], False, 0
    for line in raw.splitlines():
        s = line.strip()
        if not skipping and _LOCK_HEAD_RE.match(s):
            removed.append(s[:70])
            skipping, dropped = True, 1
            continue
        if skipping:
            if not s or dropped >= 6 or _LOCK_HEAD_RE.match(s) or s.startswith(("【", "#", "——", "```")):
                skipping = False
                if s:
                    out.append(line)
                continue
            dropped += 1
            continue
        out.append(line)
    cleaned = "\n".join(out).strip()
    issues = []
    if cleaned and len(cleaned) < len(raw) * 0.3:
        issues.append("清洗会删掉过多正文，已保留原文（可能仍含模型自创的锁定段）")
        return raw, removed, issues
    return cleaned, removed, issues


def assemble(fixed: str, body: str) -> str:
    """交付稿 = 固定块 + 本段正文。唯一的组装入口。"""
    return (fixed or "").strip() + "\n\n" + (body or "").strip()


def verify(delivery: str, fixed: str) -> list[str]:
    """交付前硬校验。空列表 = 通过。"""
    d, f = delivery or "", (fixed or "").strip()
    issues = []
    if not f:
        issues.append("固定块为空")
        return issues
    if not d.startswith(f):
        issues.append("固定块不在交付稿开头")
    if d.count(HEADING) != 1:
        issues.append(f"固定块标题出现 {d.count(HEADING)} 次（应为 1 次）")
    for bad in BAD_PLACEHOLDERS:
        if bad in d:
            issues.append("交付稿里还有未填写的兜底文案：" + bad)
            break
    return issues


def prepare_delivery(fixed: str, raw: str) -> dict:
    """模型输出 -> 清洗 -> 组装 -> 校验，一次做完，供 /api/stream 调用。"""
    body, removed, clean_issues = clean_model_output(raw)
    delivery = assemble(fixed, body)
    issues = clean_issues + verify(delivery, fixed)
    return {
        "delivery": delivery,
        "body": body,
        "removed": removed,
        "issues": issues,
    }
