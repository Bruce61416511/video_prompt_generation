# -*- coding: utf-8 -*-
"""长片固定块 + 本片锚点：全片信息的双路数据源（2026-11-12）。

两路，互不混淆：
  F（固定块）  —— 给豆包看的交付稿开头。代码持有、每段逐字一致、绝不进模型输入。
  A（本片锚点）—— 给模型看的写作参考。只含模型写画面/台词必须知道的具体值，
                 放进 message；措辞与 F 不同，并明令「照用、不许原样复述」。

为什么要有 A：只给 F 不给 A 时，模型不知道产品长什么样、声音怎么统一，
真实生成会自己编出「透明玻璃瓶／浅琥珀色液体」这种与 F 打架的画面
（2026-11-12 三段实测）。设定卡是唯一数据源，F 与 A 都从同一个 setting 渲染。

本模块只依赖标准库，可以脱离服务单独跑测试。
"""
from __future__ import annotations

import re

HEADING = "—— 固定块·全片不变（每段一致，不要改）——"
ANCHOR_HEADING = "—— 本片锚点（写画面时照用，不要把这几行原样抄进正文）——"

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
    # 2026-11-12 问题④：跨段道具无锚点。拆段模型会自加道具（杯子/发酵罐/果篮），
    # 每段各写各的外观 → 段 2 玻璃杯、段 5 陶瓷杯，剪起来接不上。
    # 解法和产品/主角同款：设定卡填一次（模型也会提议），F 与 A 都渲染。
    ("道具", "道具", "全片同一套道具，样式不许中途换", False),
    ("旁白方式", "旁白", "全片统一，不许这一段开口、下一段静音", True),
    ("音色语速", "音色", "", False),
    ("字幕与音乐", "字幕音乐", "", False),
    ("负面词", "负面词", "全程生效", False),
    ("镜头与光线", "镜头光线", "", False),
    ("参考图清单", "参考图", "", False),
)

REQUIRED_LABELS = tuple(label for label, _k, _c, req in FIXED_FIELDS if req)
# 选填且留空就不进 F，避免交付稿里堆一排「（未填写）」。
OPTIONAL_SKIP_KEYS = frozenset({"道具", "音色", "字幕音乐", "负面词", "镜头光线", "参考图"})

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
    "输出格式（全片所有段必须同一个骨架）：每个镜头一格，"
    "用加粗时间码标题开头，如「**[00:00-00:02] 开场**」「**[00:02-00:05] 中段**」；"
    "台词单独成行放在对应格后面。\n"
    "不要把正文包进代码围栏（```），不要用「0-2 秒｜开场」这种行内格式，"
    "不要输出 JSON。\n"
    "正文里不要出现任何外部链接、案例名或案例引用（goodcase.ai 等网址）—— "
    "案例只用来学写法，网址写进交付稿会被清洗掉。\n"
    "下面的「本片锚点」是全片统一的事实：写画面和台词必须严格照用，"
    "产品外观、人物长相、道具样式、声音规矩都不许自己另编；但不要把锚点那几行原样抄进正文。\n"
    "道具样式以锚点列的清单为准：清单里有的必须照写，"
    "不许这一段写玻璃杯、下一段写陶瓷杯；清单外的一次性道具只写通用样式，"
    "不给材质特写、不要反复出现。\n"
    "不要复述全片设定块（固定块、产品/人物锁定、合规红线、不变项、参考图清单）"
    "—— 这些由系统统一注入交付稿，正文里重复写会被清洗掉。\n"
    "画幅以「本片锚点」给的比例为唯一标准：正文里若提到比例，只能写这个比例，"
    "不许自己另写 9:16 / 16:9 / 竖屏 / 横屏；不确定就干脆不写比例。\n"
    "声音规矩每段都要一致：不许这一段写「本段无台词」、下一段又开口说话，"
    "也不许只在某一段自称「全局约束」。"
)

# 兜底文案：一旦出现在成稿里就说明必填校验没拦住，verify 会报出来。
BAD_PLACEHOLDERS = (
    "按主题自定",
    "【待填——写到能照着复原外观】",
    "未指定（按主题自定）",
)

# 「旁白方式」是必填，但只填「全片统一」这类空话等于没给规矩：
# 实测 2026-11-12 用户 7 段品牌片出现 4 种声音（同期声 / 画外音 / 纯环境音 / 口播）。
VAGUE_VOICE = frozenset({
    "全片统一", "统一", "全片一致", "一致", "按主题自定", "未指定",
    "无", "无要求", "随便", "同前", "同全片", "-", "—",
})
VOICE_KEYS = (
    "同期", "口播", "旁白", "画外", "配音", "台词", "环境音", "ASMR",
    "静音", "无声", "女声", "男声", "音色", "语速", "字幕", "BGM", "配乐",
)


def is_vague_voice(value: str) -> bool:
    """旁白值是否没有信息量。空、纯空话、或不含任何声音关键词都算无效。"""
    v = str(value or "").strip()
    if not v or v in VAGUE_VOICE:
        return True
    return not any(k in v for k in VOICE_KEYS)


def validate_setting(setting: dict) -> list[str]:
    """返回没填 / 填了空话的必填项标签。空列表 = 可以生成。"""
    s = setting or {}
    out = []
    for label, key, _constraint, required in FIXED_FIELDS:
        if not required:
            continue
        v = str(s.get(key) or "").strip()
        if not v or (key == "旁白" and is_vague_voice(v)):
            out.append(label)
    return out


def build_fixed_block(setting: dict, kind: str = "brand") -> str:
    """生成全片唯一一份固定块 F。给交付稿用，绝不进模型输入。"""
    s = setting or {}
    lines = [HEADING]
    for label, key, constraint, _required in FIXED_FIELDS:
        value = str(s.get(key) or "").strip()
        if not value and key == "画幅":
            value = DEFAULT_FRAME.get(kind, DEFAULT_FRAME["brand"])
        if not value and key == "红线":
            value = DEFAULT_REDLINE.get(kind, DEFAULT_REDLINE["brand"])
        if not value and key in OPTIONAL_SKIP_KEYS:
            continue
        # 缺值只标「（未填写）」，绝不写「按主题自定」这类把不确定性塞进稿子的话。
        line = f"{label}：{value}" if value else f"{label}：（未填写）"
        if constraint:
            line += f" —— {constraint}"
        lines.append(line)
    lines.append(REF_LINE)
    return "\n".join(lines)


def build_seg_anchors(setting: dict, kind: str = "brand") -> str:
    """生成「本片锚点」A：模型写画面/台词必须知道的具体值，放进 message。

    与 F 的区别：措辞不同、含模型真正用得上的外观/声音细则、明令不许原样复述。
    每段注入同一份，保证跨段一致；本段是否出镜/入画仍由 render_segment 判定。
    """
    s = setting or {}

    def v(key: str) -> str:
        return str(s.get(key) or "").strip()

    # 画幅必须进锚点：只写在 F 里、又明令模型「不要复述画幅」时，
    # 模型拿不到值只能按短视频常识猜 9:16，品牌片 16:9 必然和 F 打架
    #（2026-11-12 用户 7 段实测 6/7 段写错比例）。
    frame = v("画幅") or DEFAULT_FRAME.get(kind, DEFAULT_FRAME["brand"])
    lines = [
        ANCHOR_HEADING,
        f"画幅（全片唯一）：{frame} —— 正文里只能写这个比例，"
        "不许自己另写 9:16 / 16:9 / 竖屏 / 横屏；不确定就别写比例",
        f"产品：{v('产品')}；外观：{v('拆件')} "
        "—— 画面里的容器/包装、颜色、液体必须与此处一致，不许自己另编材质或颜色",
    ]
    if v("主角"):
        lines.append(
            f"出镜人：{v('主角')} —— 本段有真人入画时按此外观写，同一张脸同一身衣着"
        )
    if v("画风"):
        lines.append(f"画风：{v('画风')}")
    if v("不变项"):
        lines.append(f"场景与光线：{v('不变项')}")
    # 问题④：道具与产品/主角同机制 —— 拆段模型自加的道具收进这一行统一锚定。
    if v("道具"):
        lines.append(
            f"道具（全片唯一）：{v('道具')} —— 画面里出现这些道具时，"
            "材质/颜色/形状必须与此处一致，不许自己另编；"
            "清单外的道具只作一次性背景，写通用样式、不给特写"
        )
    voice = f"声音规矩（全片统一，每段照此写）：{v('旁白')}"
    if v("音色"):
        voice += f"；音色语速：{v('音色')}"
    if v("字幕音乐"):
        voice += f"；字幕与音乐：{v('字幕音乐')}"
    voice += ("。不许这一段写「无台词」、下一段又开口说话；"
              "也不许只在某一段自称「全局约束」。")
    lines.append(voice)
    if v("负面词"):
        lines.append(f"负面词：{v('负面词')}")
    if v("镜头光线"):
        lines.append(f"镜头与光线：{v('镜头光线')}")
    if v("参考图"):
        lines.append(f"参考图文件：{v('参考图')} —— 只写清用哪张，不要编造 URL 或路径")
    lines.append(
        f"合规护栏：{v('红线') or DEFAULT_REDLINE.get(kind, DEFAULT_REDLINE['brand'])}"
        " —— 台词不许踩线"
    )
    return "\n".join(lines)


def body_instruction() -> str:
    """拼在发给模型的正文骨架开头，明确按锚点写、不许复述全片级块。"""
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
# 模型把「本片锚点」原样抄进正文时清掉。也兼容标题后面跟内容的同行写法。
# 2026-11-12 加道具行后锚点最多 12 行，留 2 行余量。
_ANCHOR_HEAD_RE = re.compile(
    r"^\s*(?:——[^—\n]*本片锚点|【[^】\n]*本片锚点|\*\*[^*]*本片锚点)"
)
ANCHOR_SKIP_LINES = 14
LOCK_SKIP_LINES = 6

# 整行代码围栏（``` / ```text）：模型偶尔把正文整段包进围栏，标记行本身没有内容。
_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$")
# goodcase.ai 外链行（skill 的「锚点案例」引用）：复制全文会把外链带给豆包。
_EXT_LINK_RE = re.compile(
    r"https?://(?:[A-Za-z0-9-]+\.)*goodcase\.ai\b"
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
    """删掉模型自创的「【产品锁定】」之类全片级段落，以及锚点复述块。

    保守策略：只删标题行 + 其后最多 N 行连续非空行，遇到空行/新标题立刻停；
    若删完剩下不到原文 30%，放弃清洗并报 issue —— 绝不把正文吃掉。
    注意：「【全局约束】无字幕、无 BGM」这类本段画面负面词是有用信息，不删；
    跨段不一致靠锚点里的统一声音规矩解决，而不是把负面词删掉。
    另有两类确定性垃圾先摘掉（2026-11-12 实测）：
      a) 整行代码围栏 ``` —— 模型有时把正文整个包进围栏，复制给豆包带一坨 markdown；
      b) 带 goodcase.ai 外链的「锚点案例」行 —— 外链不该进交付稿（上游站还有关停风险）。
    返回 (清洗后正文, 删掉的标题/垃圾行, 问题列表)。
    """
    raw = (text or "").strip()
    if not raw:
        return "", [], []
    pre, dropped = [], []
    for line in raw.splitlines():
        if _FENCE_RE.match(line):
            dropped.append(line.strip()[:70])
            continue
        if _EXT_LINK_RE.search(line):
            dropped.append(line.strip()[:70])
            continue
        pre.append(line)
    raw = "\n".join(pre)
    out, removed, skipping, dropped_n, limit = [], [], False, 0, LOCK_SKIP_LINES
    for line in raw.splitlines():
        s = line.strip()
        is_anchor = bool(_ANCHOR_HEAD_RE.match(s))
        if not skipping and (is_anchor or _LOCK_HEAD_RE.match(s)):
            removed.append(s[:70])
            skipping, dropped_n = True, 1
            limit = ANCHOR_SKIP_LINES if is_anchor else LOCK_SKIP_LINES
            continue
        if skipping:
            # 停止条件：空行 / 行数上限 / 新标题 / 时间码正文标题（**[00:00-…** ——
            # 模型把锚点块与正文连着写（中间没空行）时，靠这一条保住正文，
            # 否则 12 行锚点 + 若干正文行会被连着吃掉，2026-11-12 实测清洗成空串）
            if (not s or dropped_n >= limit or _LOCK_HEAD_RE.match(s)
                    or _ANCHOR_HEAD_RE.match(s)
                    or s.startswith(("【", "#", "——", "```", "**["))):
                skipping = False
                if s:
                    out.append(line)
                continue
            dropped_n += 1
            continue
        out.append(line)
    removed = dropped + removed
    cleaned = "\n".join(out).strip()
    issues = []
    # 保护回滚：清洗量过大就保留原文。原写法「if cleaned and …」在清洗成空串时
    # 会跳过判断、静默交出空稿 —— 空串必须也触发回滚。
    if len(cleaned) < len(raw) * 0.3:
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


# ---------------------------------------------------------------- 正文 vs F 冲突检测（问题②）

_RATIO_RE = re.compile(r"(\d+)\s*[:：]\s*(\d+)")


def _parse_fixed_field(fixed: str, label: str) -> str:
    """从 F 字符串取「标签：值」（值取到行尾或「 —— 」约束语之前）。"""
    for line in (fixed or "").splitlines():
        s = line.strip()
        if s.startswith(label + "：") or s.startswith(label + ":"):
            rest = s[len(label) + 1:].strip()
            rest = rest.split(" —— ")[0].split(" - ")[0]
            return rest
    return ""


def _near_product(text: str, word: str, nouns: list[str]) -> bool:
    """word 是否出现在某个产品名词的紧邻窗口内（约 6 字），避免泛色/泛材质词误报。"""
    idx = text.find(word)
    while idx != -1:
        window = text[max(0, idx - 6): idx + 6]
        if any(n in window for n in nouns):
            return True
        idx = text.find(word, idx + 1)
    return False


# 颜色解析：F 拆件里的「深紫红」「银色」「透明」是复合色，不能拆成单字当锁定色。
_COLOR_MOD = ("深", "浅", "亮", "暗", "冷", "暖", "淡", "浓")
_COLOR_BASE = ("透明", "红", "蓝", "绿", "黄", "白", "黑", "金", "银", "紫", "橙", "粉", "灰", "青", "棕")
_COLOR_RE = re.compile(
    "(?:" + "|".join(_COLOR_MOD) + ")?(?:" + "|".join(_COLOR_BASE) + ")+色?"
)
# 正文显式颜色断言：只认「X色」这类明确说法，不认裸色字（避免「蓝莓」里的蓝被当冲突）。
_COLOR_ASSERT_RE = re.compile(r"[深浅亮暗冷暖淡浓]?[红蓝绿黄白黑金银紫橙粉灰青棕]色")
_LIGHT_WORDS = (
    "冷白", "暖", "柔光", "自然光", "高光", "反光", "主光", "打光", "灯光",
    "暗部", "阴影", "背景", "环境光", "光", "色调",
)


def _extract_color_phrases(text: str) -> list[str]:
    """从 F 拆件里取出真实用过的颜色短语（深紫红/银色/透明…）。"""
    return [m.group(0) for m in _COLOR_RE.finditer(text or "")]


def _explicit_color_conflict(text: str, opp_char: str, nouns: list[str]) -> bool:
    """正文是否在产品上下文里「显式断言」了一个与 F 不同的颜色（X色），且不是打光词。"""
    for m in _COLOR_ASSERT_RE.finditer(text or ""):
        if opp_char not in m.group(0):
            continue
        s, e = m.start(), m.end()
        window = (text or "")[max(0, s - 12): e + 12]
        if any(lw in window for lw in _LIGHT_WORDS):
            continue
        if any(n in window for n in nouns):
            return True
    return False


_NEG_RE = re.compile(r"(无|不|没|禁止|避免)\s*(同期|口播|画外音|旁白)")


def _mode_word(f_voice: str, word: str) -> bool:
    """F 旁白里是否「真正要求」某声音词（排除「无同期声」「不画外音」等否定式）。"""
    for m in re.finditer(word, f_voice or ""):
        before = (f_voice or "")[max(0, m.start() - 2): m.start()]
        if any(neg in before for neg in ("无", "不", "没")):
            continue
        return True
    return False


def check_body_vs_fixed(body: str, fixed: str) -> list[str]:
    """正文 vs 固定块 F 的运行时冲突检测（问题②）。

    模型看不到 F，所以生成后必须由后端外部核对正文是否遵守 F 的锁定项。
    只报不修（E=1 原则）。命中以下冲突才报，避免误伤：
      1) 画幅：F 锁的比例/朝向 vs 正文里出现的比例/朝向词；
      2) 声音：F 锁的旁白模式 vs 正文里出现的相反声音信号；
      3) 画风：F 锁的风格关键词 vs 正文里出现的相反风格词（疑似）；
      4) 产品材质/颜色：F 拆件里的材质/颜色 vs 正文产品上下文里的相反词（疑似）。
    """
    issues: list[str] = []
    if not (body and fixed):
        return issues

    # —— 1) 画幅 ——
    f_frame = _parse_fixed_field(fixed, "画幅")
    if f_frame:
        m = _RATIO_RE.search(f_frame)
        f_ratio = f"{m.group(1)}:{m.group(2)}" if m else ""
        f_orient = "竖屏" if "竖" in f_frame else ("横屏" if "横" in f_frame else "")
        for mm in _RATIO_RE.finditer(body):
            # 跳过时间码（[00:00-00:03] 之类），否则每段都会误报画幅冲突。
            a = body[mm.start() - 1] if mm.start() > 0 else ""
            b = body[mm.end()] if mm.end() < len(body) else ""
            if a == "-" or b == "-":
                continue
            r = f"{mm.group(1)}:{mm.group(2)}"
            if f_ratio and r != f_ratio:
                issues.append(f"画幅冲突：正文出现比例 {r}，与固定块锁定的 {f_ratio} 不一致")
                break
        if f_orient == "竖屏" and "横屏" in body:
            issues.append("画幅冲突：正文写「横屏」，与固定块锁定的竖屏不一致")
        elif f_orient == "横屏" and "竖屏" in body:
            issues.append("画幅冲突：正文写「竖屏」，与固定块锁定的横屏不一致")

    # —— 2) 声音/旁白 ——
    # 注意：F 旁白里常写「无同期声」「无画外音」这类否定式，必须识别否定词，
    # 否则「无同期声」会被误读成要求同期声、把画外音正文错判成冲突（2026-11-12 复测）。
    f_voice = _parse_fixed_field(fixed, "旁白方式")
    if f_voice:
        has_tongqi = _mode_word(f_voice, "同期") or _mode_word(f_voice, "口播")
        has_huawai = _mode_word(f_voice, "画外音") or _mode_word(f_voice, "旁白")
        vsilent = not (has_tongqi or has_huawai) and any(
            k in f_voice for k in ("无台词", "纯环境", "ASMR", "静音", "无声", "无对白")
        )
        if vsilent:
            if any(k in body for k in ("台词", "口播", "旁白", "说话")):
                issues.append("声音冲突：固定块要求无台词/纯环境，正文却出现台词或口播")
        elif has_tongqi and not has_huawai:
            if "画外音" in body or "voiceover" in body.lower() or "画外" in body:
                issues.append("声音冲突：固定块要求同期声口播（画面有说话人），正文却出现画外音")
        elif has_huawai and not has_tongqi:
            if "同期声" in body or "对镜头说" in body or "口型" in body:
                issues.append("声音冲突：固定块要求画外音旁白（画面无说话人），正文却出现同期声/口型")

    # —— 3) 画风（疑似：关键词反向）——
    f_style = _parse_fixed_field(fixed, "画风")
    STYLE_PAIRS = [
        ("写实", ("二次元", "动漫", "动画", "卡通", "Q版", "3D动画")),
        ("二次元", ("写实", "真人", "实拍")),
        ("水墨", ("写实", "3D", "二次元", "手绘")),
        ("3D", ("写实", "水墨", "二次元", "手绘")),
    ]
    if f_style:
        for base, oppos in STYLE_PAIRS:
            if base in f_style:
                for opp in oppos:
                    if opp in body:
                        issues.append(f"疑似画风冲突：正文出现「{opp}」，与固定块锁定的「{base}」画风不一致")
                        break
                break

    # —— 4) 产品材质/颜色（疑似）——
    # 2026-11-12 复测：旧逻辑把 F 复合色拆成单字（深紫红→红/紫、银色→银），
    # 正文正常出现的红/蓝/紫/白/银全被判冲突，每段刷 6 条假警告。
    # 新逻辑：① 从 F 解析真实颜色短语；② 只在正文对产品上下文「显式断言一个不同的 X色」时才报；
    #         ③ 排除打光/环境词（冷白主光、暖光…），避免把光当颜色。
    f_part = _parse_fixed_field(fixed, "产品拆件") or _parse_fixed_field(fixed, "产品")
    MAT = ["玻璃", "陶瓷", "塑料", "金属", "不锈钢", "纸", "木质", "亚克力", "硅胶"]
    COL_BASE = ["红", "蓝", "绿", "黄", "白", "黑", "金", "银", "紫", "橙", "粉", "灰", "青", "棕"]
    PROD_NOUN = ["瓶", "杯", "罐", "盒", "袋", "包装", "饮品", "后生元", "液体"]
    if f_part:
        f_color_phrases = _extract_color_phrases(f_part)
        f_color_chars = set()
        for ph in f_color_phrases:
            f_color_chars.update(ph)
        # 材质：材质词本就是明确名词，邻近产品名词即视为冲突（误报少，保留原判定）。
        f_mats = [x for x in MAT if x in f_part]
        for mat in f_mats:
            for opp in MAT:
                if opp != mat and _near_product(body, opp, PROD_NOUN):
                    issues.append(f"疑似产品材质冲突：正文出现「{opp}」材质，与固定块锁定的「{mat}」不一致")
                    break
        # 颜色：只在正文显式断言「X色」且贴近产品、且非打光词时才报。
        for opp_char in COL_BASE:
            if opp_char in f_color_chars:
                continue
            if _explicit_color_conflict(body, opp_char, PROD_NOUN):
                issues.append(f"疑似产品颜色冲突：正文出现「{opp_char}色」的产品外观，与固定块锁定的颜色不一致")
                break
    return issues


def prepare_delivery(fixed: str, raw: str) -> dict:
    """模型输出 -> 清洗 -> 组装 -> 冲突检测 -> 校验，一次做完，供 /api/stream 调用。"""
    body, removed, clean_issues = clean_model_output(raw)
    delivery = assemble(fixed, body)
    conflicts = check_body_vs_fixed(body, fixed) if fixed else []
    issues = clean_issues + conflicts + verify(delivery, fixed)
    return {
        "delivery": delivery,
        "body": body,
        "removed": removed,
        "issues": issues,
    }
