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

from .offer_forms import (BRAND_FORMS, BRAND_RULES, FORMS, KEPU_FORMS, KEPU_RULES,
                          PROFILE, RULES, SLOT_KEYS)

# 前缀决定分组：带货只在 t1~t8 里判，品牌只在 b1~b6、科普只在 k1~k6 里判，互不干扰
TRIGGER_RE = re.compile(r"^\s*(带货|品牌片?|宣传片|科普片?)\s*[:：]\s*", re.S)
_BRAND_WORDS = ("品牌", "品牌片", "宣传片")
_KEPU_WORDS = ("科普", "科普片")

FORMS_BY_GROUP = {"offer": FORMS, "brand": BRAND_FORMS, "kepu": KEPU_FORMS}
RULES_BY_GROUP = {"offer": RULES, "brand": BRAND_RULES, "kepu": KEPU_RULES}
# 模型没连上也没命中关键词时的兜底
DEFAULT_FORM = {"offer": "t3", "brand": "b1", "kepu": "k1"}

# 超过这个长度、或本来就写了多行【】——认为用户自己已经整理好了，不插手
MAX_BODY_CHARS = 200
MAX_BRACKETS = 2


def split_trigger(text: str) -> tuple[str, str] | None:
    """取出前缀和正文，返回 (分组, 正文)。没有前缀、或用不了，返回 None。"""
    m = TRIGGER_RE.match(text or "")
    if not m:
        return None
    word = m.group(1)
    if word in _BRAND_WORDS:
        group = "brand"
    elif word in _KEPU_WORDS:
        group = "kepu"
    else:
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


def rule_hit(body: str, group: str = "offer") -> str | None:
    """关键词规则树，命中即返回 form key。"""
    low = body.lower()
    for key, words in RULES_BY_GROUP.get(group, RULES):
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


_CLASSIFY_SYS_BRAND = """你在帮用户选品牌视频的拍法，并抽取可用的拍摄要素。
只输出一个 JSON 对象，不要 markdown、不要解释。

可选拍法：
{forms}

判断规则：
1. 用户在讲一个有转折的故事（谁、为什么、做了什么）→ b1；讲世界观／大场面／奇幻科幻设定 → b2。
2. 展示「东西是怎么做出来的」（工厂、工艺、工序）→ b3；第一人称一镜到底带看 → b4。
3. 没人出镜、只拍产品质感 → b5；真人对镜头讲品牌理念 → b6。
4. 拿不准时默认 b1。
5. 钩子必须是一个 3 秒内看得见的**动作**，不是状态。用户只说了状态时，改写成动作。

只输出这个结构：
{{"form":"b1","reason":"一句话，说清为什么是这个拍法","confidence":0.0,
 "slots":{{"产品":"","故事线":"","世界观":"","角色":"","钩子":"","场景":"","收尾":"","台词":"","引导语":"","卖点":"","段落":"","上段尾帧":"","本段目标":"","不变项":""}}}}

slots 规则：用户**明确说到**的才填，没说的留空字符串，不要猜、不要编。
- 故事线：2-3 幕的一句话概括（第1段…第2段…第3段…）
- 世界观：设定一句话（如：外星母星没有蓝色）
- 角色：主角外观＋体型参照物
- 钩子：一个具体可见的动作
- 场景：本段发生的事／路线／工序
- 收尾：本段最后一帧的画面
- 台词：一句第一人称口语（没有就留空）
- 引导语：收尾一句
- 卖点：只留 1 个，不夸功效
- 段落：本次第几段/共几段（用户说了总长才推段数，否则留空）
- 上段尾帧：上一段结尾画面（第 1 段留空）
- 本段目标：这一段只完成的那件事
- 不变项：全程不能变的东西（溯源片用）"""


_CLASSIFY_SYS_KEPU = """你在帮用户选科普视频的拍法，并抽取可用的讲解要素。
只输出一个 JSON 对象，不要 markdown、不要解释。

可选拍法：
{forms}

判断规则：
1. 讲「为什么会这样」的机制 → k1；展示「东西是怎么做出来的」的一串工序 → k2。
2. 要破一句错误说法（辟谣、纠错、智商税）→ k3；两人一问一答 → k4。
3. 讲看不见的过程（菌群、分子、肠道、体内）→ k5；逐项念配料表/检测报告/含量数据 → k6。
4. 拿不准时默认 k1。
5. 钩子必须是一个 3 秒内看得见的**动作**或一句反常识问句，不是状态。

只输出这个结构：
{{"form":"k1","reason":"一句话，说清为什么是这个拍法","confidence":0.0,
 "slots":{{"主题":"","知识点":"","工序":"","误区":"","问题":"","主持":"","讲师":"","对象":"","微观":"","主体":"","画风":"","钩子":"","讲法":"","立论":"","依据":"","画面":"","过程":"","逐项":"","结论":"","台词":"","不变项":"","已讲":"","段落":"","上段尾帧":"","红线":""}}}}

slots 规则：用户**明确说到**的才填，没说的留空字符串，不要猜、不要编。
- 主题：整条讲什么，一句话
- 知识点 / 工序 / 误区 / 问题 / 对象 / 微观：本段只讲的那一个（按拍法选其一填）
- 主持 / 讲师：人物外观与口吻
- 主体：微观片里每个主体单独写外观＋尺度参照物
- 画风：微观片才有，写实微观特效 或 3D 卡通拟人
- 钩子：一个具体可见的动作或反常识问句
- 讲法 / 立论 / 依据 / 画面 / 过程 / 逐项：本段的展开方式
- 结论：本段一句人话，不要把下一段的结论先说掉
- 台词：一句第一人称口语
- 不变项：全程不能变的东西（过程演示片用）
- 已讲：用户说明了前面讲过什么才填（第 1 段留空）
- 段落：本次第几段/共几段（用户说了总长才推段数，否则留空）
- 上段尾帧：上一段结尾画面（第 1 段留空）
- 红线：用户特别强调的准确性要求（没有就留空，系统有默认红线）"""


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


def llm_extract(body: str, hint: str | None, client, model: str, group: str = "offer") -> dict:
    """一次调用同时做「判拍法」和「抽槽」。失败返回 {}。"""
    if client is None or not model:
        return {}
    sys_prompt = {"brand": _CLASSIFY_SYS_BRAND, "kepu": _CLASSIFY_SYS_KEPU}.get(group, _CLASSIFY_SYS)
    forms = FORMS_BY_GROUP.get(group, FORMS)
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

def _form_of(key: str) -> dict | None:
    """在三个分组里查骨架：带货 t1~t8、品牌 b1~b6、科普 k1~k6。"""
    return FORMS.get(key) or BRAND_FORMS.get(key) or KEPU_FORMS.get(key)


def fill_form(key: str, extracted: dict | None, src: str) -> tuple[str, list[str]]:
    """把槽填进骨架，返回 (填好的文本, 待确认的槽名列表)。"""
    f = _form_of(key)
    if f is None:
        raise KeyError(f"未知的形式：{key}")
    extracted = extracted or {}
    ctx: dict[str, str] = {
        "产品": PROFILE["产品"],
        "拆件": PROFILE["拆件"],
        "合规": PROFILE["合规"],
    }
    if str(extracted.get("产品") or "").strip():
        ctx["产品"] = str(extracted["产品"]).strip()

    guessed: list[str] = []
    for name, default in f["slots"].items():
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
    """入口：text 是完整输入（含「带货：」「品牌：」或「科普：」前缀）。"""
    t0 = time.time()
    hit = split_trigger(text)
    if hit is None:
        raise ValueError("没有「带货：」「品牌：」或「科普：」前缀，或正文已经写了模板（超过 200 字/多个【】）")
    group, body = hit
    forms = FORMS_BY_GROUP.get(group, FORMS)

    hint = rule_hit(body, group)
    data = llm_extract(body, hint, client, model, group)

    form = str(data.get("form") or "").strip()
    conf = data.get("confidence")
    try:
        conf = float(conf)
    except Exception:
        conf = 0.0

    fallback = DEFAULT_FORM.get(group, "t3")
    source = "model"
    if form not in forms:
        form, source = (hint or fallback), ("rule" if hint else "default")
    elif hint and conf and conf < 0.45:
        # 模型自己也犹豫，且规则有明确命中 → 听规则的
        form, source = hint, "rule"

    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}

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
           "SLOT_KEYS", "FORMS", "BRAND_FORMS", "KEPU_FORMS"]
