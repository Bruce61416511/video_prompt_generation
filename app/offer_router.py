# -*- coding: utf-8 -*-
"""选型：把「带货：一句话」/「品牌：一句话」/「科普：一句话」变成填好的模板（只填表，不生成）。

分工（2026-11-02）：
    offer_forms.py  数据层：带货 8 类 + 品牌 6 类 + 科普 6 类的骨架与默认值
    offer_router.py 本文件：判定拍法 + 抽槽 + 套模板
    server.py       /api/offer/route 接口
    index.html      前端拦截「带货：」「品牌：」「科普：」前缀，回填输入框

这里**不调用** agent.run_full，也不碰 /api/stream —— 用户点发送才进生成链。
"""
from __future__ import annotations

import json
import re
import time

from .offer_forms import FORMS, PROFILE, RULES, SLOT_KEYS

# 只服务带货。品牌 / 科普走 app/longform.py 的三步流程，不在这里判定。
TRIGGER_RE = re.compile(r"^\s*带货\s*[:：]\s*", re.S)

# 超过这个长度、或本来就写了多行【】——认为用户自己已经整理好了，不插手
MAX_BODY_CHARS = 200
MAX_BRACKETS = 2


def split_trigger(text: str) -> tuple[str, str] | None:
    """取出「带货：」后的正文，返回 ("offer", 正文)。用不了返回 None。"""
    m = TRIGGER_RE.match(text or "")
    if not m:
        return None
    group = "offer"
    body = (text[m.end():] or "").strip()
    if not body:
        return None
    # 已经写得足够详细就不改写，直接放行给生成链
    if len(body) > MAX_BODY_CHARS or body.count("【") > MAX_BRACKETS:
        return None
    return group, body


def strip_trigger(text: str) -> str | None:
    """兼容旧签名：只取正文，不区分分组。"""
    hit = split_trigger(text)
    return hit[1] if hit else None


def rule_hit(body: str) -> str | None:
    """关键词规则树，命中即返回 form key。"""
    low = body.lower()
    for key, words in RULES:
        for w in words:
            if w.lower() in low:
                return key
    return None


# ---------------------------------------------------------------- 模型判定

_CLASSIFY_SYS = """你在给带货短视频选拍摄形式，并抽取可用的拍摄要素。
只输出一个 JSON 对象，不要 markdown、不要解释。

可用形式：
{forms}

判断规则：
1. 先看「人和产品的关系」：有人对着镜头讲 → 口播/开箱/对比；人在故事里不看镜头 → 剧情/Vlog/反差；没有人 → 产品片/ASMR。
2. 有明确「对比物」→ t7；只有一个「以为…结果…」的包袱 → t8；有一整天的时间线 → t4。
3. 用户只想展示产品质感、没人出镜 → t5；强调声音与手感 → t6。
4. 其余有生活情境、人物互动的，一律 t3。
5. 钩子（hook）必须是一个 3 秒内看得见的**动作**，不是状态。用户只说了状态时，改写成动作。

只输出这个结构：
{{"form":"t3","reason":"一句话，说清为什么是这个形式","confidence":0.0,
 "slots":{{"产品":"","场景":"","角色":"","钩子":"","卖点":"","台词":"","对比物":"","包袱":"","引导语":""}}}}

slots 规则：用户**明确说到**的才填，没说的留空字符串，不要猜、不要编。
- 产品：产品名（没说就留空）
- 场景：时间＋地点＋正在发生的小事
- 角色：人物年龄/性别/穿搭（孩子要写「只拍侧脸与手」）
- 钩子：一个具体可见的动作
- 卖点：只留 1 个，不能是功效词
- 台词：一句第一人称口语
- 对比物：对比测评里被比的那个东西（必须泛化、不点名）
- 包袱：反差短片里那句反转台词/反转点
- 引导语：收尾时对观众说的一句"""


def _parse_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return {}


def llm_extract(body: str, hint: str | None, client, model: str) -> dict:
    """一次调用同时做「判拍法」和「抽槽」。失败返回 {}。"""
    if client is None or not model:
        return {}
    sys_prompt = _CLASSIFY_SYS
    forms = FORMS
    hint_line = f"\n关键词初判：{hint}（可能不准，以你的判断为准）" if hint else ""
    user = f"用户描述：{body}{hint_line}"
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt.format(
                    forms="\n".join(f"- {k} {f['name']}：{f['when']}" for k, f in forms.items()))},
                {"role": "user", "content": user},
            ],
            temperature=0,
            timeout=25,
        )
        return _parse_json(resp.choices[0].message.content or "")
    except Exception:
        return {}


# ---------------------------------------------------------------- 套模板

# P1-3（2026-11-13）：模型抽槽常把产品名抽坏 —— t4 抽成「一袋」、t2 抽成
# 「蓝莓后生元饮品」（丢了品牌）。模板里产品名一错，成稿就会丢品牌名，
# 系统的事后 product fix 也不一定救得回来。这里在套模板前统一兜底。
_BRAND = "百芙乐"
_PRODUCT_HINT = ("后生元", "蓝莓", "饮品", "软袋", "自立袋", "饮料", "发酵", "果汁")
_ALIEN_HINT = ("别的", "其他", "竞品", "另一", "对面", "别家", "某品牌", "对比物")
_JUNK_PRODUCT = frozenset({
    "一袋", "这袋", "那袋", "一包", "一瓶", "它", "这个", "那个", "产品", "饮品",
    "一个", "一盒", "袋", "瓶",
})


def _safe_product(val: str | None) -> str:
    """把抽出来的产品槽规范成完整产品名；抽坏了就回落到 PROFILE 默认值。"""
    default = str(PROFILE["产品"]).strip()
    v = str(val or "").strip().strip("「」【】()（）:：")
    if not v or v in _JUNK_PRODUCT or len(v) < 4:
        return default
    if _BRAND in v:
        return v
    if any(a in v for a in _ALIEN_HINT):
        return default          # 「别的果汁」「其他品牌」不是自家产品，别补品牌前缀
    if any(h in v for h in _PRODUCT_HINT):
        return _BRAND + v
    return default


def _form_of(key: str) -> dict | None:
    """查骨架：只剩带货 t1~t8。"""
    return FORMS.get(key)


def fill_form(key: str, extracted: dict | None, src: str) -> tuple[str, list[str]]:
    """把槽填进骨架，返回 (填好的文本, 待确认的槽名列表)。"""
    f = _form_of(key)
    if f is None:
        raise KeyError(f"未知的形式：{key}")
    extracted = extracted or {}
    ctx: dict[str, str] = {
        "产品": _safe_product(extracted.get("产品")),
        "拆件": PROFILE["拆件"],
        "合规": PROFILE["合规"],
    }

    guessed: list[str] = []
    for name, default in f["slots"].items():
        if name == "产品":          # 已由 _safe_product 统一兜底，不许被原始抽槽覆盖
            continue
        val = str(extracted.get(name) or "").strip()
        if val:
            ctx[name] = val
        else:
            ctx[name] = default.replace("__SRC__", src)
            if "待确认" in default:
                guessed.append(name)

    tpl = f["template"]
    for name, val in ctx.items():
        tpl = tpl.replace("{" + name + "}", val)
    return tpl.strip() + "\n", guessed


def route(text: str, client=None, model: str = "") -> dict:
    """入口：text 是完整输入（含「带货：」前缀）。"""
    t0 = time.time()
    hit = split_trigger(text)
    if hit is None:
        raise ValueError("没有「带货：」前缀，或正文已经写了模板（超过 200 字/多个【】）")
    group, body = hit
    forms = FORMS

    hint = rule_hit(body)
    data = llm_extract(body, hint, client, model)

    form = str(data.get("form") or "").strip()
    conf = data.get("confidence")
    try:
        conf = float(conf)
    except Exception:
        conf = 0.0

    fallback = "t3"          # 模型没连上、关键词也没命中时的兜底：剧情
    source = "model"
    if form not in forms:
        form, source = (hint or fallback), ("rule" if hint else "default")
    elif hint and conf and conf < 0.45:
        # 模型自己也犹豫，且规则有明确命中 → 听规则的
        form, source = hint, "rule"

    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    if str(slots.get("产品") or "").strip():
        slots = dict(slots)
        slots["产品"] = _safe_product(slots["产品"])   # 前端展示与模板保持一致

    chosen_text, guessed = fill_form(form, slots, body)
    forms_list = []
    for k, f in forms.items():
        txt, _ = fill_form(k, slots, body)
        forms_list.append({
            "key": k,
            "short": f["short"],
            "name": f["name"],
            "when": f["when"],
            "text": txt,
            "active": k == form,
        })

    return {
        "group": group,
        "form": form,
        "form_name": forms[form]["name"],
        "short": forms[form]["short"],
        "confidence": round(conf, 2) if conf else 0.0,
        "reason": str(data.get("reason") or "").strip(),
        "source": source,
        "template": chosen_text,
        "guessed": guessed,
        "forms": forms_list,
        "slots": {k: str(v) for k, v in slots.items() if str(v).strip()},
        "elapsed": round(time.time() - t0, 2),
    }


__all__ = ["route", "strip_trigger", "split_trigger", "rule_hit", "fill_form",
           "SLOT_KEYS", "FORMS"]
