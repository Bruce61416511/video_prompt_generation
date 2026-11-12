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
        "红线": "不说治疗/改善/增强免疫力/通便/降三高；画面不生成美术字/标语（口播台词照常，字卡后期贴）；不点名不贬低任何竞品",
    },
    "kepu": {
        "产品": "",
        "拆件": "",
        "画幅": "9:16 竖屏",
        "画风": "",
        "主角": "",
        "不变项": "",
        "红线": "不承诺功效、不说「提高 N 倍」、不把体外实验说成人体效果、检出≠有效、不引用无出处数据；画面不生成美术字/标语（口播台词照常）",
    },
}

# 设定卡里由模型提议的字段（拆段后填充）
# 声音三项一起提议，避免用户面对空白输入框无从下手；
# 道具（问题④）也从段内容里收：拆段模型自加的杯子/发酵罐集中提议成一份清单，
# 用户改定后进固定块与锚点，避免每段各编各的外观；
# 负面词 / 镜头光线 / 参考图清单依赖具体素材，不提议，由用户手填。
PROPOSED_KEYS = ("产品", "拆件", "画风", "主角", "不变项", "道具",
                 "旁白", "音色", "字幕音乐")

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
        "蓝色吸嘴软袋／上段蓝莓摄影满铺·中段深藏青块·下段道奇蓝＋紫罗兰圆点／蓝色竖棱纹螺旋盖／液体深蓝紫红微稠",
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
        lambda seg: bool(seg.get("lines")) or _has_any((seg.get("content", "") + seg.get("open", "") + seg.get("close", "")), ("讲", "说", "台词", "口播")),
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
【首尾】首段必须有钩子（前 3 秒抓住人，钩子可以是一句口播狠话），尾段必须收束（不留悬空）。
【段数】建议 {cap} 段左右（每段 12-25 秒）；宁可少拆几段、每段讲透，也不要切成十几段碎片。

{kind_tip}

只输出 JSON，不要解释：
{{"segments":[{{"title":"6字以内的段名","content":"这一段发生什么，一句话20-40字，写可见的动作/画面，需要台词时把口播/狠话焊进动作里，不写"要讲清楚XX"这种空话","sec":12}}]}}
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
3. 硬上限：全片选用的主角技能不得超过 3 种 —— 内容相近的段必须共用同一主角（例如视觉奇观类、真人叙事类、产品质感类各算一种）；
4. 一段只能一个主角 —— 如果这段真的需要两种拍法，说明它该拆成两段；
5. 理由必须具体到「这段要做什么，所以需要这套镜头逻辑」，禁止写「内容合适」「比较搭」这种空话。

【还要给每段写两个画面锚点，用来做段与段的接缝】
  - open：本段开场 0-3 秒那一帧「看得见的画面或一句口播钩子」，一句话，写动作/构图/光线或台词，台词焊进动作、不念广告词；
  - close：本段最后一帧「看得见的画面或一句落点台词」，一句话。下一段的 open 要能从这一帧接上；
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

    # 主角收敛：全片主角技能种类有硬上限（3 种）。2026-11-03 实测：提示词里的
    # 「7 段 7 种是杂乱」压不住模型，120s 真实调用仍是 7 段 7 种。双保险：
    #   第一道：超限时让模型把拍法相近的段合并重选一次（保留语义匹配）；
    #   第二道：仍超限就代码强制归并——按出现次数保留前 3 种，其余段就近并入。
    MAX_HERO_KINDS = 3

    def _kinds() -> list[str]:
        seen: list[str] = []
        for i in sorted(hero_by_i):
            h = hero_by_i[i][0]
            if h not in seen:
                seen.append(h)
        return seen

    if source == "model" and len(_kinds()) > MAX_HERO_KINDS:
        current = "\n".join(f"段{i}：{hero_by_i[i][0]}" for i in sorted(hero_by_i))
        conv_user = (
            f"视频类型：{KIND_LABEL.get(kind, '品牌片')}\n主题：{theme}\n\n"
            f"你刚才给每段选的主角共有 {len(_kinds())} 种，太散了。"
            f"全片主角技能不得超过 {MAX_HERO_KINDS} 种："
            f"把拍法相近的段合并到同一主角（例如视觉奇观类、真人叙事类、"
            f"产品质感类各算一种），每段仍要是它在那个大类里最合适的。"
            f"重新输出完整 JSON（含每段 hero/why/open/close），"
            f"覆盖全部 {len(segs)} 段。\n\n当前选择：\n{current}\n\n分段：\n"
            f"{_listing(enumerate(segs, start=1))}\n"
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _ASSIGN_SYS.format(pool=pool_lines)},
                    {"role": "user", "content": conv_user},
                ],
                temperature=0.2,
                timeout=45,
            )
            _collect(_parse_json(resp.choices[0].message.content or ""))
        except Exception:
            pass

    merged_note = ""
    kinds = _kinds()
    if source == "model" and len(kinds) > MAX_HERO_KINDS:
        from collections import Counter
        freq = Counter(h for h, _ in hero_by_i.values())
        first_seen = {h: idx for idx, h in enumerate(kinds)}
        kept = sorted(freq, key=lambda h: (-freq[h], first_seen[h]))[:MAX_HERO_KINDS]
        kept_set = set(kept)
        last_kept = ""
        for i in range(1, len(segs) + 1):
            if i not in hero_by_i:
                continue                      # 空缺段留给 _guess_hero 兜底
            if hero_by_i[i][0] in kept_set:
                last_kept = hero_by_i[i][0]
            else:
                hero_by_i[i] = (
                    last_kept or kept[0],
                    f"全片主角超过 {MAX_HERO_KINDS} 种，代码收敛到「{HERO_POOL[last_kept or kept[0]]['cn']}」"
                    f"（原选 {HERO_POOL[hero_by_i[i][0]]['cn']}）",
                )
        merged_note = (
            f"全片主角超过 {MAX_HERO_KINDS} 种，已收敛为："
            + "、".join(HERO_POOL[h]["cn"] for h in kept)
        )

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
    if merged_note:
        warnings.append(merged_note)
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
根据主题和分段内容填写这九个字段：
- 产品：这条片子里的产品/主体是什么。主题里没出现具体品牌或产品就写「按主题推断不出，请用户填」。
- 拆件：产品的外观拆解，写到能照着复原（材质、颜色、开关方式、内容物）。推断不出就留空字符串。
- 画风：一句话定全片的视觉风格。必须全片统一，不许写「前段 X 后段 Y」。
- 主角：出镜人物的年龄/性别/穿着/体态。段里没有真人就留空字符串。
- 不变项：全片复用的场景与光线锚点（同一场景/同一光源/同一机位基准），3-4 个，用「／」分隔。
- 道具：从分段内容里收出「跨段反复出现的关键道具」，逐件写外观（材质/颜色/形状，写到能照着复原），2-4 件用「／」分隔，
  如「透明直筒玻璃试饮杯 250ml／银色不锈钢圆柱发酵罐」。只列会在两段以上出现的（试饮杯、发酵罐、果篮等）；
  一次性杂物和场景家具不列。段内容里没有就留空字符串。
- 旁白：全片唯一的声音规矩，必须写具体，从下面选一种并写清「有没有画外音、有没有字幕、有没有 BGM」：
  A 同期声本人对镜头口播（无画外音）；B 画外音旁白（画面里不出现说话人、不出现口型）；
  C 全片无台词只有现场环境音；D 全片无台词纯 ASMR（只收物体与动作声）。
  A/B/C/D 只能选一种，A 与 B 互斥：选了同期声就不许再写「有画外音」，选了画外音就不许写口型或同期声。
  绝对不许只写「全片统一」「统一」这类空话 —— 那样模型会每段自己编声音。
- 音色：谁的声音、语速快慢，例「女中音、语速 0.9x」。必须给值，主题没提就写「自然女声、语速正常」。
- 字幕音乐：字幕与背景音乐的规矩，例「不加字幕、不加 BGM」。必须给值，主题没提就写「不加字幕、不加 BGM」。
只输出 JSON：{"产品": "...", "拆件": "...", "画风": "...", "主角": "...", "不变项": "...", "道具": "...",
"旁白": "...", "音色": "...", "字幕音乐": "..."}
产品/拆件/道具推断不出可留空字符串；旁白、音色、字幕音乐三项必须给值。不要编造具体品牌。"""


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
    """把一段渲染成【本段正文骨架】。不调模型 —— 生成链交给 /api/stream。

    2026-11-12 彻底改 1/2：固定块不再出现在这里。
    全片级设定由 app/fixed_block.py 的 build_fixed_block() 生成唯一一份 F，
    后端在 /api/stream 之后把 F 拼到交付稿开头。模型全程看不到 F，
    因此既不会漏写、也不会改写成 21 种标题。setting 参数保留仅为兼容调用方。

    字段骨架对每段是同一套，但下面三处必须按段判定，一刀切会出事：
    1) 产品出没出镜、2) 有没有真人：这两个判定结果放进「本段配置」；
    3) 开场占几秒：短段 2 秒、中段 3 秒、长段 4 秒 —— 固定写 0-3 秒，
       对 8 秒的段占掉三分之一。
    """
    i = int(seg.get("i") or 1)
    n = len(all_segs) or i
    prev = all_segs[i - 2] if i > 1 else None
    nxt = all_segs[i] if i < n else None
    sec = int(seg.get("sec") or 12)

    content = seg.get("content", "") or ""
    hero = seg.get("hero_cn", "") or ""
    # 镜头逻辑也参与出入画判定：content 没提人/产品、hero 提了会漏判（问题①）。
    lens_text = content + " " + hero

    # 问题①修复（2026-11-12 实测）：出入画判定以「设定卡/F 的出镜人、产品声明」为权威，
    # 关键词只作补充，避免「F 写了工人/研究员/年轻人，段配置却说真人不出画」这类自相矛盾。
    # 关键词拓宽：原先只认营养师/女主/她/手… 漏掉 工人/研究员/年轻人/男人/女人/杯/产品特写。
    PERSON_KW = (
        "营养师", "主持", "女主", "男主", "主播", "她", "他", "手部", "手指", "脸",
        "蹲", "站", "走", "工人", "研究员", "工程师", "年轻", "男人", "女人", "顾客",
        "小孩", "儿童", "消费者", "用户", "手", "握", "端", "拿", "捧", "倒", "递",
        "接", "试饮", "品尝", "讲解", "演示", "操作", "坐", "跑", "蹲着", "站着", "走着",
    )
    PROD_KW = (
        "瓶", "袋", "包装", "后生元", "饮品", "举起", "喝", "产品", "特写", "质感",
        "杯", "试饮", "液体", "倒出", "倒入", "饮料", "瓶身", "展示", "陈列", "拿",
        "端", "捧", "握", "倒", "递", "接",
    )
    # 真正的「无产品」上下文：微观/抽象示意，产品本就不该出现。
    NO_PROD_CTX = (
        "空镜", "微观", "肠道", "菌群", "细胞", "分子", "机理", "结构示意",
        "动画示意", "示意图",
    )

    cast = str((setting or {}).get("主角") or "").strip()
    f_prod = str((setting or {}).get("产品") or "").strip()

    person_on = _has_any(lens_text, PERSON_KW)
    # F 出镜人声明点名本段（「段2工人」「第3研究员」）→ 强制有真人，覆盖关键词漏判。
    if cast and (f"段{i}" in cast or f"第{i}" in cast or f"{i}段" in cast):
        person_on = True

    prod_on = _has_any(lens_text, PROD_KW)
    if f_prod:
        # F 声明了产品：微观/抽象示意段（菌群特写、肠道微观、结构示意…）产品本就不该出镜，
        # 强制 False，否则「特写」等关键词会把微观段误判成产品出镜。
        # 其余段一律默认「出镜」—— 2026-11-12 复测发现：科普片把蓝莓/紫甘蓝当产品，
        # 段正文已出现这些食材，但「纯口播」判断 + 关键词漏掉果蔬，导致配置说「不出镜」、
        # 正文却出现产品，与 F 自相矛盾。宁可让纯口播段也默认出镜（至多过度声明，不矛盾），
        # 也不要配置与正文打架。
        no_prod_ctx = _has_any(lens_text, NO_PROD_CTX)
        prod_on = not no_prod_ctx

    # 2026-11-13 P1-2：open/close 接缝锚点是模型按故事线写出来的。若锚点明确
    # 安排了产品/人物入画，而上面机械判定说「不出镜/不入画」，模型会同时收到
    # 两套相反指令（实测科普4段：配置写「真人不出画／产品不出镜」，close 却要
    # 「那杯发酵饮品被推入画面中央」，正文跟了锚点 → 配置与正文打架）。
    # 以锚点为权威校正配置：只改本段配置行，不改锚点文字、不改模型正文。
    ANCHOR_PERSON_KW = (
        "营养师", "女主", "男主", "工人", "研究员", "外星人", "她", "他",
        "手", "人脸", "镜头前", "对镜头", "人物",
    )
    ANCHOR_PROD_KW = (
        "瓶", "袋", "包装", "饮品", "产品", "后生元", "软袋", "自立袋",
        "试饮杯", "玻璃杯", "吸嘴", "拧盖",
    )
    ANCHOR_NEG = ("不出现", "不得出现", "禁止", "不要", "不得", "不拍", "只拍装置", "无人")

    def _anchor_requires(kws):
        anchor_text = str(seg.get("open") or "") + "\n" + str(seg.get("close") or "")
        for ln in anchor_text.splitlines():
            s = ln.strip()
            if not s or any(n in s for n in ANCHOR_NEG):
                continue
            if _has_any(s, kws):
                return True
        return False

    corrections = []
    anchor_person_fixed = False
    if not person_on and _anchor_requires(ANCHOR_PERSON_KW):
        person_on, anchor_person_fixed = True, True
        corrections.append("开场/结尾锚点安排了人物或手部入画")
    if not prod_on and _anchor_requires(ANCHOR_PROD_KW):
        prod_on = True
        corrections.append("开场/结尾锚点安排了产品入画")

    # 产品是否被人物手持/端起/倾倒：决定镜头必须露出手，否则易出「悬浮瓶」（问题①）。
    prod_held = _has_any(
        lens_text,
        ("端起", "举起", "拿起", "握住", "手持", "倾倒", "捧", "喝", "接", "递"),
    )
    head = 2 if sec <= 10 else (3 if sec <= 18 else 4)

    lines = [
        f"{KIND_LABEL.get(kind, '品牌片')}·第 {i} 段／共 {n} 段｜{seg.get('title', '')}｜{sec} 秒",
        "",
    ]
    # 固定块不进模板：由后端在交付稿里统一注入，模型看不到、改不了。
    # 本段配置：随段变化的部分，格式统一。
    lines.append("—— 本段配置（仅本段，格式统一）——")
    lines.append(f"本段镜头逻辑：{hero}")
    if prod_on:
        lines.append("本段产品是否出镜：出镜 —— 按「本片锚点」里的产品外观复原，不许改版、不许渲包装文字")
    else:
        lines.append("本段产品是否出镜：不出镜 —— 只登记外观备查，画面里不得出现瓶／袋／包装／饮品，也不要给它安排位置")
    if person_on:
        lines.append("本段真人是否入画：是 —— 按「本片锚点」里的出镜人外观入画，必须同一张脸同一身衣着")
    else:
        lines.append("本段真人是否入画：否 —— 「本片锚点」里设定的人不入画，只拍装置／产品／空镜，也不要临时加别的人或人手")
    if corrections:
        lines.append("本段配置校正（以开场/结尾锚点为准）：" + "；".join(corrections)
                     + " —— 以上出镜判定已同步，本段不要再写「不得出现」。")
    # 问题①：出入画判定与镜头逻辑联动，杜绝「悬浮瓶 / 反物理穿帮」。
    reconcile = []
    if person_on and prod_held:
        reconcile.append(
            "真人入画且本段涉及手持/端起/倾倒/拿起产品：镜头必须露出该人物持握产品的手/手臂，"
            "禁止产品脱离人体自行漂浮、自行倾斜、凭空升起或独自运动（Seedance 常见穿帮）。"
        )
    # 锚点已明确要人物入画的段，不再挂「若镜头逻辑是空镜则作废」这条，
    # 否则又会把刚校正好的判定推翻（P1-2）。
    if person_on and not anchor_person_fixed:
        reconcile.append(
            "若本段「镜头逻辑」写的是产品特写/空镜、并未安排人物入画，则以上「真人入画」判定作废，"
            "改按「产品出镜、真人不出画」处理：只拍装置/产品，绝不临时加人手。"
        )
    elif prod_on:
        reconcile.append(
            "产品出镜但真人不出画：产品不得被任何手持有或操作，只能静置或由镜头运动展示；"
            "同样禁止产品自行漂浮、自行倾斜、凭空移动。"
        )
    if reconcile:
        lines.append("—— 出入画与镜头逻辑联动（问题①）——")
        lines.extend(reconcile)
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
    lines.append(f"开场画面（0-{head} 秒，可以是一句口播钩子开场、台词焊进动作；纯画面时写可见动作）：{seg.get('open') or '【待填】'}")
    lines.append("中段推进（按时间码，每个时间码一个可见变化，2-4 秒一格）：【待填】")
    lines.append(f"结尾定格（本段最后一帧，可带一句落点台词，下一段要从这里接）：{seg.get('close') or '【待填】'}")

    for key in seg.get("blocks") or []:
        # 不变项已在上面出过；产品拆件已按「出不出镜」在上面处理过，都不再重复挂
        if key in ("不变项", "产品拆件"):
            continue
        label, default, _t = BLOCKS[key]
        if key == "画风锁定":
            # 2026-11-13 P1-1：原默认值硬编码「写实微观特效（不混 3D 卡通）」，
            # 科普片设定卡锁 3D 时，模型把示例当规则照抄 → 一句里两种画风打架。
            # 这里不写任何具体风格：全片画风由设定卡/F 决定，代码不替用户拍板
            # 「3D 卡通 vs 写实微观」，用户在设定卡里选哪个，正文就只能是哪个。
            lines.append(
                f"{label}：全片画风以「本片锚点」为准，本段不另写画风；"
                "只按该风格写画面，不混用另一种、不中途切换"
            )
            continue
        lines.append(f"{label}：【{default}】")

    lines.append("")
    lines.append("—— 硬要求 ——")
    lines.append(f"本段只有 {sec} 秒：一次生成、不跨段、不重复别段内容；结尾必须停在「结尾定格」那一帧。")
    return "\n".join(lines)
