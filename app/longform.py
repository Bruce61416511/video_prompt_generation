# -*- coding: utf-8 -*-
"""长片（品牌 / 科普）新流程 —— 数据层 + 三步逻辑。

2026-11-02 重构。旧方案（b1~b6 / k1~k6 十二个分类）已废弃，原因是层级错位：
十二个分类是「段型」不是「片型」，一条 60-300 秒的片子天然跨类
（如：一镜到底开场 + 工序中段 + 产品特写收尾）。把段型做成首轮入口，
等于逼用户给整片选一个它装不下的格子。

新流程四步：
    0. 设定卡   —— 全片共用的产品/角色/画风/不变项/红线，填一次
    1. 拆段     —— 模型按「内容单元」拆，每段一句话 + 时长（本文件 plan_segments）
    2. 配主角   —— 模型从 11 个主角里给每段选一个并给理由；底座由规则强制（assign_segments）
    3. 出提示词 —— 逐段渲染填空模板（render_segment），再交给 /api/stream 生成

三条不可退让的设计：
    A. 旧 12 份模板里的**字段**全部保留，改成「货架」按需取用（BLOCKS）。
       分类可以删，检查项不能丢 —— 缺「不变项」车间会变样，缺「尺度参照」微观尺度会飘。
    B. 主角由模型选但限定池（HERO_POOL），避免选出汽车片/恐怖片。
    C. 底座不走模型自觉，走规则（BASE_RULES）—— 它是解决具体问题的工具，不是风格选项。
"""

from __future__ import annotations

import json
import re

# ---------------------------------------------------------------- 全片设定卡

# 只保留两类默认值：画幅（平台操作项）和红线（合规护栏）。
# 其余（产品/拆件/画风/主角/不变项）全部留空 —— 这些依赖「这条片子拍什么」，
# 在拆段之前预填就是替用户拍板；拆段后由模型按主题+段内容提议，用户可改。
SETTING_DEFAULTS = {
    "brand": {
        "产品": "",
        "拆件": "",
        "画幅": "16:9 横屏",
        "画风": "",
        "主角": "",
        "不变项": "",
        "红线": "不说治疗/改善/增强免疫力/通便/降三高；画面不渲字，slogan 与字卡后期贴；不点名不贬低任何竞品",
    },
    "kepu": {
        "产品": "",
        "拆件": "",
        "画幅": "9:16 竖屏",
        "画风": "",
        "主角": "",
        "不变项": "",
        "红线": "不承诺功效、不说「提高 N 倍」、不把体外实验说成人体效果、检出≠有效、不引用无出处数据；画面不渲字",
    },
}

# 设定卡里由模型提议的字段（拆段后填充）
PROPOSED_KEYS = ("产品", "拆件", "画风", "主角", "不变项")

KIND_LABEL = {"brand": "品牌片", "kepu": "科普片"}


# ---------------------------------------------------------------- 主角池
#
# 只放「适合品牌/科普长片」的 11 个。刻意排除 8 个明显不搭的：
# car-vehicle / horror-suspense / game-ui-livestream / pet-animal /
# sports-extreme / combat-choreography / fashion-lookbook / travel-city-walk
# —— 留着它们，模型偶尔会给发酵罐配个恐怖悬疑。
HERO_POOL = {
    "seedance-cinematic-narrative-short": {
        "cn": "电影级叙事短片",
        "use": "有人在演、有情绪转折、要让观众记住「谁在做什么」",
    },
    "seedance-ugc-creator-review": {
        "cn": "真人出镜讲述",
        "use": "真人对着镜头讲，讲解/口播/测评/自述理念",
    },
    "seedance-process-transformation-montage": {
        "cn": "流程与变换蒙太奇",
        "use": "把「怎么做的」按物理顺序摊开：进料→加工→成型，一段一道工序",
    },
    "seedance-product-commercial-shotlist": {
        "cn": "产品质感分镜",
        "use": "没人出镜，产品是唯一主角，靠光和材质说话，投放级",
    },
    "seedance-pov-continuous-take": {
        "cn": "第一人称一镜到底",
        "use": "镜头就是观众的视角，一次不剪走完一条路线（注意：单镜连续，段内不能跳场景）",
    },
    "seedance-food-asmr": {
        "cn": "声音特写 ASMR",
        "use": "靠声音和微距镜头建立诱惑：倒液、气泡、冷凝水，几乎不需要情节",
    },
    "seedance-time-freeze-rewind": {
        "cn": "时间冻结与倒放奇观",
        "use": "把看不见的过程「显形」：分子、菌群、气泡，靠慢速/冻结/倒放解释机制",
    },
    "seedance-epic-fantasy-scifi": {
        "cn": "奇幻科幻大场面",
        "use": "卖「这东西不一般」：飞船、星云、巨构，尺度靠参照物",
    },
    "seedance-3d-cartoon": {
        "cn": "3D 卡通角色",
        "use": "微观世界拟人化（菌群长眼睛会说话），或品牌吉祥物",
    },
    "seedance-handheld-ugc-vlog": {
        "cn": "手持跟拍 Vlog",
        "use": "跟拍一个人一天/一段路，自然光、轻微晃动，生活感",
    },
    "seedance-meme-comedy": {
        "cn": "反转搞笑短片",
        "use": "一个包袱落地，适合传播向的片段（品牌主片里少用）",
    },
}

# ---------------------------------------------------------------- 底座触发规则
#
# 底座不是风格选项，是解决具体问题的工具。所以不走模型自觉，走确定性规则。
# (触发条件说明, 技能名, 判定函数)
BASE_RULES = [
    (
        "seedance-dialogue-performance-beats",
        "本段有台词/有人说话",
        lambda seg: bool(seg.get("lines") or seg.get("talk")) or _has_any(
            seg.get("content", ""), ("讲", "说", "问", "答", "口播", "台词", "对话", "解释")
        ),
    ),
    (
        "seedance-timeline-shot-script",
        "本段要切多个镜头（非一镜到底）",
        lambda seg: seg.get("hero") != "seedance-pov-continuous-take"
        and not _has_any(seg.get("content", ""), ("一镜到底", "不切", "一次不剪")),
    ),
    (
        "seedance-character-reference-lock",
        "同一角色/产品在多个镜头或多个段里出现",
        lambda seg: bool(seg.get("cross")) or _has_any(
            seg.get("content", ""), ("产品", "瓶", "袋", "营养师", "外星人", "主角", "人")
        ),
    ),
]

BASE_CN = {
    "seedance-dialogue-performance-beats": "对白与表演节拍",
    "seedance-timeline-shot-script": "逐秒时间轴分镜",
    "seedance-character-reference-lock": "参考图身份锁定",
    "seedance-storyboard-grid-to-video": "分镜网格转视频",
}

# ---------------------------------------------------------------- 字段货架
#
# 旧 12 份模板里所有检查项的归宿。分类删了，检查项留在这里，按段内容取用。
# key -> (行标签, 取值说明/default, 判定函数)
BLOCKS = {
    "不变项": (
        "不变项（全程锁定，声明后不许变）",
        "同一场景／同一束光／同一身工服",
        lambda seg: _has_any(
            seg.get("content", ""),
            ("车间", "工厂", "生产线", "罐", "实验室", "同一", "工序", "流程", "产线"),
        ),
    ),
    "尺度参照": (
        "尺度参照（每个非日常大小的主体都要给参照物，不许只写「巨大/微小」）",
        "旁边站一个人／旁边一颗蓝莓当参照",
        lambda seg: _has_any(
            seg.get("content", ""),
            ("微观", "分子", "细胞", "肠道", "体内", "显微", "纳米", "飞船", "星际", "星球", "巨构"),
        ),
    ),
    "先立后破": (
        "先立后破（① 先把观众心里那句原话说出来让他点头 ② 再推翻 ③ 给替代说法，别只否定）",
        "① 大家都以为… ② 其实… ③ 那该怎么办＝…",
        lambda seg: _has_any(
            seg.get("content", ""), ("误区", "辟谣", "以为", "其实", "错", "不是", "纠正", "谣言")
        ),
    ),
    "逐项拆解": (
        "逐项拆解（手指一项一项指过去，每项配一句人话；每个数字一张字卡，标清「检出值／添加量／检测方法」，不写结论词）",
        "项目 1＝… ／ 项目 2＝… ／ 项目 3＝…",
        lambda seg: _has_any(
            seg.get("content", ""), ("配料表", "检测", "标签", "成分", "含量", "数据", "指标", "报告")
        ),
    ),
    "对话规则": (
        "对话规则（两人不同时开口；答话的人看对方不看镜头；问句≤8 秒，答话先给结论再解释）",
        "答话的人看对方，不看镜头",
        lambda seg: _has_any(seg.get("content", ""), ("问答", "主持", "访谈", "采访", "对话", "提问")),
    ),
    "知识递进": (
        "知识递进（本段只讲一个知识点，讲完就停，不许把后面段的结论提前说掉）",
        "本段只讲：…",
        lambda seg: seg.get("kind") == "kepu",
    ),
    "画风锁定": (
        "画风锁定（选定不许中途混用另一种）",
        "写实微观特效（不混 3D 卡通）",
        lambda seg: _has_any(
            seg.get("content", ""), ("微观", "分子", "卡通", "拟人", "动画", "插画", "细胞")
        ),
    ),
    "产品拆件": (
        "产品拆件（逐件写外观，写到能照着复原）",
        "哑光自立袋／袋身偏紫罗兰／白色螺旋盖／内容物深紫红略稠",
        # 「特写」「倒」太泛（「浅景深特写果皮」「蓝莓倒入罐」都会命中），只认产品本体的词
        lambda seg: _has_any(seg.get("content", ""), ("产品", "瓶", "袋", "包装", "自立袋", "饮品", "喝")),
    ),
    "卖点": (
        "卖点（全段只带 1 个，且不进画面——只贴字卡）",
        "益生菌发酵",
        lambda seg: bool(seg.get("sell")) or _has_any(seg.get("content", ""), ("卖点", "功效", "好在")),
    ),
    "台词": (
        "台词（第一人称口语，像聊天不像宣言；一句一个节拍，不念广告词）",
        "「…」",
        lambda seg: bool(seg.get("lines")) or _has_any(seg.get("content", ""), ("讲", "说", "台词", "口播")),
    ),
    "穿帮红线": (
        "穿帮红线（整段不切镜头；每个转场写明「镜头继续运动」；运动必须符合载体物理）",
        "整段不切镜头",
        lambda seg: seg.get("hero") == "seedance-pov-continuous-take"
        or _has_any(seg.get("content", ""), ("一镜到底", "不切", "一次不剪", "fpv")),
    ),
}

# ---------------------------------------------------------------- 工具

def _has_any(text: str, words) -> bool:
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


def seg_cap(seconds: int) -> int:
    """段数上限：按每段约 18 秒估，最多 12 段。

    早期按「每段最少 8 秒」算，3 分钟会被拆成 13-14 段，碎到没法看；
    改成按 18 秒估：60s→3 段、180s→10 段、300s→12 段（封顶）。
    """
    return max(2, min(12, round(int(seconds) / 18)))


def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------- 步 1：拆段

_PLAN_SYS = """你在给一条{kind_label}做分段规划。

【唯一的硬约束】一次生成最多 30 秒，所以整片必须切成若干段，每段 8~30 秒。
【怎么切】按「内容单元」切，不是按时间均分：
  - 一个情节点 / 一道工序 / 一个知识点 / 一个误区 / 一次问答 = 一段；
  - 一段只做一件事，做完就停，不要在一段里塞两件事；
  - 总时长要和用户要求对得上（各段相加 ≈ 总秒数）。
【首尾】首段必须有钩子（前 3 秒抓住人），尾段必须收束（不留悬空）。
【段数】建议 {cap} 段左右（每段 12-25 秒）；宁可少拆几段、每段讲透，也不要切成十几段碎片。

{kind_tip}

只输出 JSON，不要解释：
{{"segments":[{{"title":"6字以内的段名","content":"这一段发生什么，一句话20-40字，写可见的动作/画面，不写"要讲清楚XX"这种空话","sec":12}}]}}
"""

_KIND_TIP = {
    "brand": "品牌片的段通常沿「钩子→建立→展开→证据→情绪高潮→收束」的情绪线走；卖点只允许在尾段带 1 个。",
    "kepu": "科普片的段之间有知识依赖：后一段必须建立在前面已讲过的概念上。所以顺序不能换，且每段只推进一个概念。",
}


def plan_segments(kind: str, theme: str, seconds: int, setting: dict, client=None, model: str = "") -> dict:
    """步 1：把「主题 + 时长」拆成段表。模型不可用时退回等分兜底。"""
    seconds = max(15, int(seconds or 60))
    cap = seg_cap(seconds)
    if client is None or not model:
        return {"segments": _fallback_segments(seconds), "source": "fallback"}

    user = (
        f"视频类型：{KIND_LABEL.get(kind, '品牌片')}\n"
        f"主题：{theme}\n"
        f"总时长：{seconds} 秒\n"
        f"产品：{setting.get('产品') or '未指定'}\n"
        f"产品形态（写产品时必须用这个形态，不许写成瓶/罐/盒）：{setting.get('拆件') or '未指定'}\n"
        f"全片画风：{setting.get('画风') or '未指定（按主题自定，全片统一）'}\n"
        f"画幅：{setting.get('画幅') or '16:9 横屏'}（切段时按这个比例想镜头，竖屏不能安排横向摇过三个工位这类调度）\n"
        f"主角/出镜人：{setting.get('主角') or '未指定'}\n"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": _PLAN_SYS.format(
                        kind_label=KIND_LABEL.get(kind, "品牌片"),
                        cap=cap,
                        kind_tip=_KIND_TIP.get(kind, ""),
                    ),
                },
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            timeout=45,
        )
        data = _parse_json(resp.choices[0].message.content or "")
    except Exception:
        return {"segments": _fallback_segments(seconds), "source": "fallback"}

    segs = []
    for i, s in enumerate(data.get("segments") or [], start=1):
        content = str(s.get("content") or "").strip()
        if not content:
            continue
        try:
            sec = int(s.get("sec") or 0)
        except Exception:
            sec = 0
        segs.append(
            {
                "i": i,
                "title": str(s.get("title") or f"第{i}段").strip()[:12],
                "content": content,
                "sec": min(30, max(5, sec or 12)),
                "kind": kind,
            }
        )
    if not segs:
        return {"segments": _fallback_segments(seconds), "source": "fallback"}

    # 段数只卡绝对上限 12，不再按 cap 硬砍。
    # cap 是 prompt 里的「建议段数」，不是判决 —— 实测 60 秒的「蓝莓发酵」模型给 4 段
    # （每段 15 秒，相加正好 60），按 cap=3 砍掉的是第 4 段「袋装收束」，
    # 等于把整条品牌片的产品登场给删了。判「碎不碎」看每段秒数，不看段数。
    if len(segs) > 12:
        segs = segs[:12]
    for i, s in enumerate(segs, start=1):
        s["i"] = i
    segs = _fit_seconds(segs, seconds)
    return {"segments": segs, "source": "model", "note": _dur_note(segs, seconds)}


def _fit_seconds(segs: list[dict], seconds: int) -> list[dict]:
    """把各段秒数按比例拉到总目标。

    模型常把总时长算少（实测要 60 秒只给 45），段内容没错、只是秒数偏小。
    差在 8% 以内不动（模型本来就准，别瞎改）；超了就整体缩放，余数摊到最长的段。
    """
    total = sum(int(s.get("sec") or 0) for s in segs)
    if not segs or total <= 0:
        return segs
    if abs(total - seconds) <= max(3, round(seconds * 0.08)):
        return segs
    k = seconds / total
    for s in segs:
        s["sec"] = min(30, max(5, int(round(s["sec"] * k))))
    gap = seconds - sum(s["sec"] for s in segs)
    order = sorted(segs, key=lambda s: -s["sec"])
    n, steps = len(order), 0
    while gap and steps < n * 30:
        s = order[steps % n]
        step = 1 if gap > 0 else -1
        if 5 <= s["sec"] + step <= 30:
            s["sec"] += step
            gap -= step
        steps += 1
    return segs


def _fallback_segments(seconds: int) -> list[dict]:
    n = max(2, min(seg_cap(seconds), max(2, round(seconds / 20))))
    each = max(8, min(30, seconds // n))
    shape = ["开场钩子", "建立情境", "展开细节", "证据与过程", "情绪高点", "收束落点"]
    return [
        {
            "i": i,
            "title": shape[i - 1] if i <= len(shape) else f"中段{i - 1}",
            "content": "【待填：这一段发生什么，写可见的动作或画面】",
            "sec": each,
            "kind": "brand",
        }
        for i in range(1, n + 1)
    ]


def _dur_note(segs: list[dict], seconds: int) -> str:
    total = sum(s["sec"] for s in segs)
    if abs(total - seconds) <= 4:
        return ""
    return f"各段相加 {total} 秒，与要求的 {seconds} 秒差 {total - seconds:+d} 秒（可自行调整某段时长）"


# ---------------------------------------------------------------- 步 2：配主角 + 底座

_ASSIGN_SYS = """你在给已分好段的长片，逐段指定「用哪种镜头逻辑拍」，并说明理由。

【只能从下面这些主角里选，不许自创、不许选清单外的】
{pool}

【选择原则】
1. 看这一段「本质上在用什么方式说服人」，不是看它出现了什么东西；
2. 重复必须是有意的：首段与尾段可以用同一主角呼应（立世界与收束用同一镜头语言，片子才合得上）；中段同类内容可以交替复用。只禁止无意识的连用——同一主角连续 3 段以上要避免。不要为了「每段都不一样」而强行换拍法，7 段 7 种拍法不是节奏，是杂乱；
3. 一段只能一个主角 —— 如果这段真的需要两种拍法，说明它该拆成两段；
4. 理由必须具体到「这段要做什么，所以需要这套镜头逻辑」，禁止写「内容合适」「比较搭」这种空话。

【还要给每段写两个画面锚点，用来做段与段的接缝】
  - open：本段开场 0-3 秒那一帧「看得见的画面」，一句话，写动作/构图/光线，不许写旁白或情绪词；
  - close：本段最后一帧「看得见的画面」，一句话。下一段的 open 要能从这一帧接上；
  - 相邻两段必须对得上：上一段 close 的画面，下一段 open 要延续它（同一空间、同一运动方向、同一色温）。

只输出 JSON：
{{"segs":[{{"i":1,"hero":"技能名","why":"一句具体理由，20-40字","open":"开场那一帧的可见画面","close":"结尾那一帧的可见画面"}}]}}
"""


def assign_segments(
    kind: str, theme: str, segs: list[dict], setting: dict,
    client=None, model: str = "", descriptions: dict | None = None,
) -> dict:
    """步 2：模型选主角（限池内），底座按规则强制。"""
    if not segs:
        return {"segs": [], "source": "empty"}

    # 候选清单带两层信息：手写的「什么时候用」+ SKILL.md frontmatter 的 description。
    # 后者从 loader 实时读，改技能文件就自动生效，不用维护两份。
    descs = descriptions or {}
    pool_lines = "\n".join(
        f"- {name}｜{v['cn']}｜什么时候用：{v['use']}"
        + (f"｜技能自述：{descs[name]}" if descs.get(name) else "")
        for name, v in HERO_POOL.items()
    )

    hero_by_i: dict[int, tuple[str, str]] = {}
    anchor_by_i: dict[int, tuple[str, str]] = {}
    source = "fallback"

    def _listing(pairs) -> str:
        return "\n".join(
            f"段{i}（{s.get('sec', 12)} 秒，{s.get('title', '')}）：{s.get('content', '')}"
            for i, s in pairs
        )

    def _collect(data) -> None:
        for s in data.get("segs") or []:
            try:
                i = int(s.get("i"))
            except Exception:
                continue
            # 接缝锚点与「主角选得对不对」无关 —— 主角无效也必须保留，
            # 否则这段走兜底后 open/close 变空，骨架里的接缝退化成【待填】。
            anchor_by_i[i] = (
                str(s.get("open") or "").strip(),
                str(s.get("close") or "").strip(),
            )
            hero = str(s.get("hero") or "").strip()
            if hero in HERO_POOL:
                hero_by_i[i] = (hero, str(s.get("why") or "").strip())

    if client is not None and model:
        user = (
            f"视频类型：{KIND_LABEL.get(kind, '品牌片')}\n主题：{theme}\n"
            f"全片画风（已定，不参与选择）：{setting.get('画风') or '未指定（按主题自定）'}\n"
            f"出镜人（已定）：{setting.get('主角', '')}\n\n分段：\n{_listing(enumerate(segs, start=1))}\n"
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _ASSIGN_SYS.format(pool=pool_lines)},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                timeout=45,
            )
            _collect(_parse_json(resp.choices[0].message.content or ""))
            if hero_by_i:
                source = "model"
        except Exception:
            pass

        # 缺失重试：模型漏了某段（JSON 里少一条 i、或主角为空/池外），
        # 只把这几段单独补问一次，而不是直接认命走关键词兜底。
        missing = [(i, s) for i, s in enumerate(segs, start=1) if i not in hero_by_i]
        if missing:
            retry_user = (
                f"视频类型：{KIND_LABEL.get(kind, '品牌片')}\n主题：{theme}\n"
                f"全片画风（已定）：{setting.get('画风') or '未指定（按主题自定）'}\n"
                f"出镜人（已定）：{setting.get('主角', '')}\n\n"
                f"以下 {len(missing)} 段第一次没有选出主角，请只给这几段补选：\n"
                f"{_listing(missing)}\n"
            )
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _ASSIGN_SYS.format(pool=pool_lines)},
                        {"role": "user", "content": retry_user},
                    ],
                    temperature=0.2,
                    timeout=45,
                )
                _collect(_parse_json(resp.choices[0].message.content or ""))
            except Exception:
                pass

    out = []
    prev_hero = ""
    for i, s in enumerate(segs, start=1):
        if i in hero_by_i:
            hero, why = hero_by_i[i]
            guessed = False
        else:
            hero, why = _guess_hero(s.get("content", ""), prev_hero)
            guessed = True
            # 只降一级：model → mixed。不许连降 —— 两个以上兜底段会把 mixed
            # 再写成 fallback，前端就会误报「模型没连上」。
            if source == "model":
                source = "mixed"
        prev_hero = hero

        seg = dict(s)
        seg["hero"] = hero
        seg["hero_cn"] = HERO_POOL[hero]["cn"]
        seg["why"] = why or HERO_POOL[hero]["use"]
        seg["kind"] = kind
        seg["guessed"] = guessed          # 界面据此给这段的主角标「猜」
        seg["open"], seg["close"] = anchor_by_i.get(i, ("", ""))
        seg["base"] = [b for b, _label, test in BASE_RULES if _safe(test, seg)]
        seg["base"] = list(dict.fromkeys(seg["base"]))
        seg["skills"] = [hero] + seg["base"]
        seg["blocks"] = [k for k, (_l, _d, test) in BLOCKS.items() if _safe(test, seg)]
        out.append(seg)

    warnings = _consistency_warnings(out)
    return {"segs": out, "source": source, "warnings": warnings}


def _safe(fn, seg) -> bool:
    try:
        return bool(fn(seg))
    except Exception:
        return False


def _guess_hero(content: str, prev: str) -> tuple[str, str]:
    """模型不可用时的兜底：按关键词猜，并记住上一段避免重复。"""
    rules = [
        ("seedance-pov-continuous-take", ("一镜到底", "第一人称", "俯冲", "穿过", "走一遍", "带看")),
        ("seedance-product-commercial-shotlist", ("特写", "质感", "倒杯", "瓶身", "英雄帧", "字卡")),
        ("seedance-process-transformation-montage", ("车间", "工序", "生产线", "罐", "清洗", "灌装", "流程")),
        ("seedance-time-freeze-rewind", ("微观", "菌", "分子", "细胞", "肠道", "体内")),
        ("seedance-epic-fantasy-scifi", ("飞船", "星球", "宇宙", "外星", "星云", "大场面")),
        ("seedance-food-asmr", ("声音", "气泡", "咕咚", "ASMR", "微距")),
        ("seedance-cinematic-narrative-short", ("他", "她", "走进", "对视", "情", "故事")),
        ("seedance-ugc-creator-review", ("讲", "说", "问", "答", "口播", "营养师")),
    ]
    for hero, words in rules:
        if _has_any(content, words):
            return hero, f"这段的内容是「{content[:16]}…」，按关键词归到这套镜头逻辑"
    fallback = "seedance-cinematic-narrative-short" if prev != "seedance-cinematic-narrative-short" else "seedance-ugc-creator-review"
    return fallback, "没有明显特征，按叙事收（可手动改）"


def _consistency_warnings(segs: list[dict]) -> list[str]:
    """相邻段风格断裂预警：一镜到底紧跟静态产品陈列，剪起来会跳。"""
    out = []
    static = {"seedance-product-commercial-shotlist", "seedance-food-asmr"}
    for a, b in zip(segs, segs[1:]):
        if a.get("hero") == "seedance-pov-continuous-take" and b.get("hero") in static:
            out.append(
                f"段{a['i']}（一镜到底）→ 段{b['i']}（静态产品陈列），镜头语言会断，"
                "建议中间加一个过渡段，或把后一段换成叙事/手持"
            )
    run = best = 1
    for a, b in zip(segs, segs[1:]):
        run = run + 1 if a.get("hero") == b.get("hero") else 1
        best = max(best, run)
    if best >= 3:
        out.append(f"有连续 {best} 段用同一主角，节奏可能单调，建议至少换一段")
    return out


# ------------------------------------------------- 步 2.5：提议全片设定（拆段之后）

_PROPOSE_SYS = """你在给一条视频提议「全片设定」——不是拍板，是给起草稿，用户会改。
根据主题和分段内容填写这五个字段：
- 产品：这条片子里的产品/主体是什么。主题里没出现具体品牌或产品就写「按主题推断不出，请用户填」。
- 拆件：产品的外观拆解，写到能照着复原（材质、颜色、开关方式、内容物）。推断不出就留空字符串。
- 画风：一句话定全片的视觉风格。必须全片统一，不许写「前段 X 后段 Y」。
- 主角：出镜人物的年龄/性别/穿着/体态。段里没有真人就留空字符串。
- 不变项：全片复用的场景与光线锚点（同一场景/同一光源/同一机位基准），3-4 个，用「／」分隔。
只输出 JSON：{"产品": "...", "拆件": "...", "画风": "...", "主角": "...", "不变项": "..."}
推断不出就留空字符串，不要编造具体品牌。"""


def propose_setting(kind: str, theme: str, segs: list[dict], client=None, model: str = "") -> dict:
    """拆段之后，让模型按主题+段内容提议全片设定（画幅/红线不在提议范围，由代码定）。"""
    out = {k: "" for k in PROPOSED_KEYS}
    if client is None or not model:
        return out
    listing = "\n".join(
        f"段{s.get('i', i + 1)}｜{s.get('title', '')}｜{s.get('content', '')}" for i, s in enumerate(segs)
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _PROPOSE_SYS},
                {"role": "user", "content": f"视频类型：{KIND_LABEL.get(kind, '品牌片')}\n主题：{theme}\n分段：\n{listing}"},
            ],
            temperature=0.4,
        )
        data = _parse_json(resp.choices[0].message.content or "")
        for k in PROPOSED_KEYS:
            v = str(data.get(k) or "").strip()
            if v and "推断不出" not in v and "请用户填" not in v:
                out[k] = v
    except Exception:                               # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- 步 3：渲染本段提示词

def render_segment(kind: str, seg: dict, all_segs: list[dict], setting: dict) -> str:
    """把一段渲染成填空模板。不调模型 —— 生成链交给 /api/stream（forced 技能）。

    字段骨架对每段是同一套，但下面四处必须按段判定，一刀切会出事：
    1) 产品出没出镜：不出镜的段不展开「产品拆件」，否则边说不出镜边要求复原外观；
    2) 有没有真人：纯产品／装置段若还写「出镜人：营养师」，模型会硬塞一个人进画面；
    3) 开场占几秒：短段 2 秒、中段 3 秒、长段 4 秒 —— 固定写 0-3 秒，对 8 秒的段占掉三分之一；
    4) 红线：设定里已含「不渲字」就不再补一句，否则同一句在骨架里出现两次。
    """
    i = int(seg.get("i") or 1)
    n = len(all_segs) or i
    prev = all_segs[i - 2] if i > 1 else None
    nxt = all_segs[i] if i < n else None
    sec = int(seg.get("sec") or 12)

    # 产品出镜：只认产品本体的词（以前含「特写」「倒」，结果「浅景深特写果皮」也命中）
    prod_on = _has_any(seg.get("content", ""), ("瓶", "袋", "包装", "后生元", "饮品", "举起", "喝"))
    # 真人出镜：不认「人」这个单字 —— 「外星人」「人物」都会误命中；
    # 也不认「接过」—— 第 10 段是外星生物接过饮品，会把它误判成有真人
    person_on = _has_any(
        seg.get("content", ""),
        ("营养师", "主持", "女主", "她", "他的手", "手指", "手部", "脸", "蹲", "站", "走"),
    )
    head = 2 if sec <= 10 else (3 if sec <= 18 else 4)

    lines = [
        f"{KIND_LABEL.get(kind, '品牌片')}·第 {i} 段／共 {n} 段｜{seg.get('title', '')}｜{sec} 秒",
        f"本段镜头逻辑：{seg.get('hero_cn', '')}",
        "",
        "—— 全片设定（每段一致，不要改）——",
        f"产品：{setting.get('产品') or '未指定（按主题自定）—— 全片同一产品，不许中途换'}",
    ]
    if prod_on:
        lines.append(f"产品拆件：{setting.get('拆件') or '【待填——写到能照着复原外观】'}")
        lines.append("产品是否出镜：本段出镜 —— 按上面拆件复原外观，不许改版、不许渲包装文字")
    else:
        lines.append("产品是否出镜：本段不出镜 —— 只登记外观备查，画面里不得出现瓶／袋／包装／饮品，也不要给它安排位置")
    # 画幅：豆包界面上点选的那个才是真生效的，提示词里这行负责两件事 ——
    # ① 让模型按这个比例做镜头调度（竖屏不能写横向摇过三个工位）；
    # ② 提醒你自己复制到豆包时点对比例，点错整条片构图会被裁。
    _frame = setting.get("画幅") or (SETTING_DEFAULTS.get(kind) or SETTING_DEFAULTS["brand"])["画幅"]
    lines.append(f"画幅：{_frame} —— 生成时必须在豆包选中同一比例，否则构图会被裁")
    _style = setting.get("画风") or "未指定（按主题自定）—— 全片必须统一成同一种画风，不要中途混用"
    lines.append(f"画风：{_style}")
    if person_on:
        lines.append(f"出镜人：{setting.get('主角') or '未指定 —— 按段内容自定，全片同一人必须同一张脸、同一身衣着'}")
    else:
        lines.append("出镜人：本段无真人出镜 —— 上面设定的人不入画，只拍装置／产品／空镜，也不要临时加别的人或人手")

    # 全片级字段：只在判定需要时出（不变项不是每段都有）
    if "不变项" in (seg.get("blocks") or []):
        lines.append(f"不变项（全程锁定）：{setting.get('不变项') or '未指定 —— 自定后全片不得中途更换'}")
    lines.append(f"合规红线：{setting.get('红线', '')}")
    if "不渲" not in (setting.get("红线") or ""):
        lines.append("画面不渲任何文字，slogan 与字卡后期贴")
    lines.append("")

    # 位置块：用文字交代接缝，不依赖图片（提示词阶段还没有成片）
    prev_close = (prev or {}).get("close") or (
        f"（上一段结尾待填——见第 {i - 1} 段的「结尾定格」）" if prev else "（无，本段是第一段）"
    )
    next_open = (nxt or {}).get("open") or (
        f"（下一段开头待填——见第 {i + 1} 段的「开场画面」）" if nxt else "（无，本段是最后一段）"
    )
    lines.append("—— 本段在全片中的位置（只作参照，不要写别的段的内容）——")
    lines.append(f"上一段结尾：{prev_close}")
    lines.append(f"下一段开头：{next_open}")
    lines.append(f"本段目标（只完成一件事）：{seg.get('content', '')}")
    lines.append("")

    # 本段必填四行 —— 对应「开场/推进/定格」三段式
    lines.append("—— 本段必填（只写这一段）——")
    lines.append(f"开场画面（0-{head} 秒，必须是一个可见的动作，不用旁白交代）：{seg.get('open') or '【待填】'}")
    lines.append("中段推进（按时间码，每个时间码一个可见变化，2-4 秒一格）：【待填】")
    lines.append(f"结尾定格（本段最后一帧，下一段要从这里接）：{seg.get('close') or '【待填】'}")

    for key in seg.get("blocks") or []:
        # 不变项已在上面出过；产品拆件已按「出不出镜」在上面处理过，都不再重复挂
        if key in ("不变项", "产品拆件"):
            continue
        label, default, _t = BLOCKS[key]
        lines.append(f"{label}：【{default}】")

    lines.append("")
    lines.append("—— 硬要求 ——")
    lines.append(f"本段只有 {sec} 秒：一次生成、不跨段、不重复别段内容；结尾必须停在「结尾定格」那一帧。")
    return "\n".join(lines)
