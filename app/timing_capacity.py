# -*- coding: utf-8 -*-
"""
事后容量核算：模型把稿子写完之后，逐段估算「这段画面至少要演几秒」，
再跟它自己标的时间码比一遍。

设计口径（2026-11-09，A 方案已回退）：
  - 生成前不给任何时间格子。几段、每段多长，全由模型按内容自己排。
  - 代码只在成稿之后量一遍，回答的是「时间够不够用」，不是「数字对不对」。
    （数字自洽由 agent.py 的 C 方案 _fix_timing 负责，两者不重叠。）
  - 默认只报不改：中文动作切分靠规则必然有误报，拿误报去改时间码比不改更糟。
  - 不砍内容、不静默：不够就明说，稿子原样交付，由人判断。
  - 任何异常都吞掉，绝不影响出稿。

告警降噪：绝对估算（need_min）容易把「描述性细节写得多」误判成「演不完」，
所以同时看相对密度（load）——只有本段内容密度明显高于全片中位数、
且估算秒数确实超标的段，才升到「装不下」；其余超标段只记「偏紧」。
"""
from __future__ import annotations

import re
import statistics

# ---------------------------------------------------------------- 估算参数（经验值，可改）
BASE_SEC = 2.0           # 一个镜头起手的基本时间
CLAUSE_SEC = 0.5         # 每多一个动作小句
CONNECT_SEC = 0.8        # 每个动作链连接词（随后/接着/然后…）
SWITCH_SEC = 1.2         # 每次场景或机位切换
CHARS_PER_SEC = 4.0      # 中文口播约 4 字/秒
CAMERA_BONUS = 1.0       # 一段里出现 ≥2 种运镜
TOLERANCE_SEC = 1.0      # 超出标注秒数多少才算问题
LOAD_RATIO = 1.5         # 内容密度达到全片中位数的几倍才升为「装不下」
MAX_DETAIL = 3           # 黄字里最多列几段明细

# 与 agent.py 的 _TIME_RANGE 同款：匹配 [00:09-00:18] / 【00:09～00:18】等写法
_TIME_RANGE = re.compile(
    r"([\[【])\s*(\d{1,3})\s*[:：]\s*(\d{1,2})\s*([-–—~～至])\s*(\d{1,3})\s*[:：]\s*(\d{1,2})\s*([\]】])"
)

# 描述性 / 约束性标签：整句丢掉。真实稿里「细节：」「音效：」这类句子
# 往往比动作句还多，不排掉会把每一段都判成装不下。
_LABEL_DROP = re.compile(
    r"^(?:细节|音效|声音|声响|光线|光影|材质|画面要求|构图|色调|风格|"
    r"负面|不要|禁止|避免|无|保持|全局|参考|参数|画质|分辨率|帧率|景深|焦段|"
    r"运镜|字幕|文字|品牌|产品名|时长|总计|备注)\s*[：:]"
)
# 段头标题行（「镜头2：侧面伴随跟拍（…）」）不是动作，整行跳过。
_TITLE_LINE = re.compile(r"^(?:镜头|Shot|SHOT)\s*\d+\s*[：:.]")
_CONNECT = re.compile(r"随后|接着|然后|紧接着|继而|随之|之后|最后|同时|一边|再(?:次|度|回到|转向|转回)?")
_SWITCH = re.compile(r"切到|切回|转场|镜头切换|画面(?:依次)?转(?:过|到|回)|闪回|场景切换|推进到|拉回到")
_CAMERA = re.compile(r"推近|拉远|环绕|摇镜|跟拍|俯冲|升降|甩镜|变焦|一镜到底|慢推|横移")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_DIALOGUE_LABEL = re.compile(r"(?:台词|口播|对白|旁白|念道|说道|说)\s*[：:]\s*([^\n。；;]{2,})")
_QUOTED = re.compile(r"[「『\"“]([^」』\"”]{4,})[」』\"”]")
_SPLIT = re.compile(r"[\n。；;，,、]+")
_TAIL_BLOCK = re.compile(r"【(?:负面|全局|禁止|约束|NEGATIVE)|负面提示\s*[：:]|NEGATIVES?\s*[：:]", re.I)


def _segment_bodies(text: str) -> list[dict]:
    """按时间码把稿子切开，每个时间码后面到下一个时间码之前算作这一段的内容。"""
    ms = list(_TIME_RANGE.finditer(text or ""))
    out: list[dict] = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end():end]
        # 末尾的全局约束 / 负面提示不属于最后一段
        cut = _TAIL_BLOCK.search(body)
        if cut:
            body = body[:cut.start()]
        out.append({"raw": m.group(0), "body": body, "pos": m.span()})
    return out


def _cjk_len(s: str) -> int:
    return len(_CJK.findall(s or ""))


def _load_of(body: str) -> dict:
    """估算一段的内容量与「至少演几秒」。只做字符串统计，不调模型。"""
    # 按「行」先过一遍：标题行、细节/音效/负面这类描述整行不参与动作计数。
    # 再按行内标点切小句。这样「细节：额角渗汗，背部出现汗渍」不会被切成
    # 两个「动作」而虚增容量，真正在演的「动作：…」行则完整保留。
    clauses = 0
    kept: list[str] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or _TITLE_LINE.match(line) or _LABEL_DROP.match(line):
            continue
        kept.append(line)
        for c in _SPLIT.split(line):
            c = c.strip(" 　\t·-—*:：()（）")
            if len(c) >= 3 and _CJK.search(c):
                clauses += 1
    action_text = "\n".join(kept)

    connectors = len(_CONNECT.findall(action_text))
    switches = len(_SWITCH.findall(action_text))
    camera_kinds = len(set(_CAMERA.findall(action_text)))
    camera = CAMERA_BONUS if camera_kinds >= 2 else 0.0

    dlg_chars = 0
    for m in _DIALOGUE_LABEL.finditer(action_text):
        dlg_chars += _cjk_len(m.group(1))
    for m in _QUOTED.finditer(action_text):
        dlg_chars += _cjk_len(m.group(1))
    dlg_sec = dlg_chars / CHARS_PER_SEC

    if clauses == 0 and dlg_chars == 0:
        need = 1.0                      # 几乎没内容，别让 BASE_SEC 把 1 秒段误判成不够
    else:
        need = (BASE_SEC
                + max(0, clauses - 1) * CLAUSE_SEC
                + connectors * CONNECT_SEC
                + switches * SWITCH_SEC
                + dlg_sec
                + camera)
    load = clauses + connectors * 1.5 + switches * 2.0 + dlg_sec + camera
    return {
        "clauses": clauses, "connectors": connectors, "switches": switches,
        "camera_kinds": camera_kinds, "dialogue_chars": dlg_chars,
        "need_min": round(need, 1), "need_int": max(1, int(need + 0.5)),
        "load": round(load, 1),
    }


def _dur_of(raw: str) -> int | None:
    m = _TIME_RANGE.match(raw)
    if not m:
        return None
    _lb, m1, s1, _sep, m2, s2, _rb = m.groups()
    mm1, ss1, mm2, ss2 = int(m1), int(s1), int(m2), int(s2)
    if max(ss1, ss2, mm1, mm2) > 59:
        return None
    return mm2 * 60 + ss2 - (mm1 * 60 + ss1)


def check_capacity(text: str, user_input: str = "") -> dict:
    """主入口。只读成稿，返回报告；**不修改文本**。

    user_input 暂不参与判定（总时长由 C 负责），留着是为了和 _fix_timing 调用形式一致。
    """
    report: dict = {
        "checked": False, "segments": 0, "ok": True,
        "items": [], "short": 0, "tight": 0,
        "marked_total": 0, "suggest_total": 0,
        "notes": [], "issues": [],
    }
    segs = _segment_bodies(text)
    if len(segs) < 2:
        return report                    # 没时间码 / 只有一段，不量、不硬造
    report["checked"] = True
    report["segments"] = len(segs)

    rows: list[dict] = []
    for i, s in enumerate(segs, 1):
        dur = _dur_of(s["raw"])
        if dur is None or dur <= 0:
            continue
        est = _load_of(s["body"])
        rows.append({"index": i, "raw": s["raw"], "dur": dur, **est})
    if not rows:
        return report

    med = statistics.median(r["load"] for r in rows)
    for r in rows:
        if r["need_min"] > r["dur"] + TOLERANCE_SEC:
            r["verdict"] = "short" if r["load"] >= med * LOAD_RATIO else "tight"
        else:
            r["verdict"] = "ok"
    report["items"] = rows
    report["short"] = sum(1 for r in rows if r["verdict"] == "short")
    report["tight"] = sum(1 for r in rows if r["verdict"] == "tight")
    report["marked_total"] = sum(r["dur"] for r in rows)
    report["suggest_total"] = sum(r["need_int"] for r in rows)
    report["ok"] = not (report["short"] or report["tight"])

    if report["ok"]:
        return report                    # 全够用：不打扰，一条黄字都不挂

    flagged = [r for r in rows if r["verdict"] == "short"]
    detail = (flagged or [r for r in rows if r["verdict"] == "tight"])[:MAX_DETAIL]
    for r in detail:
        level = "装不下" if r["verdict"] == "short" else "偏紧"
        report["notes"].append(
            f"· 第 {r['index']} 段 {r['raw']} 标 {r['dur']} 秒、估算至少 {r['need_int']} 秒（{level}）："
            f"动作小句 {r['clauses']}、动作链 {r['connectors']}、场景切换 {r['switches']}、"
            f"台词约 {r['dialogue_chars']} 字。建议给到 {r['need_int']} 秒，或把部分动作挪到相邻段。"
        )
    report["notes"].insert(0, (
        f"【容量核算】共 {len(rows)} 段，{report['short']} 段明显装不下、{report['tight']} 段偏紧；"
        f"按内容估算全片至少需 {report['suggest_total']} 秒（现标 {report['marked_total']} 秒）。"
        "这是规则估算、会有误报，描述性细节多不等于演不完，请结合画面自行判断。"
    ))
    report["notes"].append(
        "本次只报不改：没有删动作、没有压缩内容、时间码数字一个都没动。"
    )
    return report


def capacity_report(text: str, user_input: str = "") -> dict:
    """带异常保护的对外入口。任何异常都不许影响出稿。"""
    try:
        return check_capacity(text, user_input)
    except Exception as e:  # noqa: BLE001
        return {
            "checked": False, "segments": 0, "ok": True,
            "items": [], "short": 0, "tight": 0,
            "marked_total": 0, "suggest_total": 0,
            "notes": [f"容量核算异常，已跳过（不影响出稿）：{e}"], "issues": [],
        }
