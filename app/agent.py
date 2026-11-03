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

        return self.generate(
            user_input=f"请质检下面这段视频提示词：\n\n{prompt_text}",
            skill_names=[skill_name],
            temperature=temperature,
        )

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

        # 产品名保全：模板里写明的产品名不许在成稿里丢失。
        # 2026-11-03 实测：纯产品片/反差钩子会把「产品：百芙乐蓝莓后生元」
        # 改写成「一支哑光自立袋」这种无名描述，质检也没抓出来。
        # 生成后先查一道，修正稿出来再查一道；丢了就做一次轻量修复调用。
        prod = _extract_product_name(user_input)
        if prod and prod not in gen["content"]:
            emit("product_missing", {"product": prod})
            gen["content"] = self._fix_product_name(prod, gen["content"])

        emit("generated", {"chars": len(gen["content"])})

        result = {
            "skills_used": picked,
            "generate": gen,
            "lint": None,
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
        gen_is_lint = bool(picked) and all("lint" in n.lower() for n in picked)

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
                result["revision"] = rev
                emit("revised", {"chars": len(rev.get("revised") or "")})
            else:
                emit("revise_skipped", {
                    "reason": rev.get("error") or "模型没有给出修正稿，已跳过错",
                })

        return result

