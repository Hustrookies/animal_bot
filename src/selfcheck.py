#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""content.json 校验 —— 0 token 的信任边界。run.sh 在渲染前必须过它。

不要靠 agent 的退出码判断它成功了：headless agent 退 0 但写出残缺 JSON 是常见的。

exit 0 = 通过（可能带 WARN）   exit 1 = 不合格，不要渲染不要推送

── 本项目比 wiki-bot 多的四类检查 ──

1. **与 pick.json 的身份一致性（FAIL）**：`subject` / `scientific_name` / `group`
   必须与今天派下来的选题逐字相同。理由不是洁癖：去重主键是 subject + 学名（§7.3），
   模型把主体悄悄换掉（或学名写错一个字母）之后，半年窗口就形同虚设，而页面看起来
   完全正常。阶段 3 有过实证 —— agent 在没有锚的情况下写「紫晶林星蜂鸟」，写出来的
   内容其实是另一个种 `Calliphlox evelynae` 的分布。学名是这个项目唯一的身份证。

2. **半年重复硬闸门（FAIL，§5.1）**：subject 或学名在 posts.jsonl 最近 183 天里
   出现过就拒。pick 已经去重了，但 pick 与 publish 之间隔着 agent、配图、渲染多步，
   任何一步的人工干预都能绕过它。约束 ① 是用户明确要的，值得两道锁。

3. **薄锚下的编造嫌疑（WARN）**：58/225（26%）的锚正文不足 150 字（§6.3-1）。
   薄锚 + 填满的 profile + 一堆精确数字，这个组合几乎只能来自编造。机械地判不了
   真假，但能把「锚里没有的数字」逐个列出来给人看 —— 这是这个项目最值钱的一条提醒。

4. **伪注释键（FAIL）**：顶层出现 `__xxx__` 一律拒。wiki-bot 的骨架里写过一行
   `"__motif__": "按今日 cat 只填…"`（JSON 没有注释语法），模型把它当成真实容器，
   整期停更。§7.2 因此规定骨架里不得有伪注释键 —— 这里是那条规定的执行者。

用法：
    selfcheck.py                 校验 ROOT/content.json
    selfcheck.py path.json       校验指定文件
    selfcheck.py --pick p.json   指定 pick.json（默认 ROOT/pick.json）
    selfcheck.py --no-dup        跳过半年重复闸门（只在补写历史期时用）
    selfcheck.py --selftest      跑内置用例：一份合格样本 + 故意写错的若干种
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

PLACEHOLDER = re.compile(r"XXX|TODO|待补充|待填|占位|lorem|\{\{|\}\}", re.I)

# profile 的字段。**没有一个是必填的** —— 26% 的锚薄到写不出体长体重，逼它填就是
# 逼它编（§6.3-1）。缺失只统计，不判错；填了则要合格。
PROFILE_KEYS = ("iucn", "iucn_source", "body_length", "weight", "lifespan",
                "habitat", "range_text", "biogeo", "diet")
# 带数字的那几个：数字要能在锚里找到出处，否则列出来（见 _trace_numbers）
PROFILE_NUMERIC = ("body_length", "weight", "lifespan")

IUCN_OK = {"EX", "EW", "CR", "EN", "VU", "NT", "LC", "DD", "NE"}

# 配图禁用词。本项目比 wiki-bot 严：**一律不许写实摄影风格**，不分类群（§8.2 ①）。
# AI 生成的动物图极可能物种特征错误，照片风格会让读者当成真实影像，等于每天传播
# 一条错误的形态学知识。博物学图谱风格自带「这是绘制品」的信号。
#
# **这张表必须与 prompt.md「三之二 禁止项 1」逐词对齐。** 已经有过一次教训：
# prompt 的禁止项与校验器的词表各写一份、内容不同，结果 prompt 教的措辞被校验器拦掉，
# 整期停更。核对方向有两个，两个都要查：校验器拦的词 prompt 必须列出来（否则模型
# 无从避开），prompt 列的词校验器必须拦（否则那条禁止项是空话）。
BANNED_ART = ("照片", "摄影", "镜头", "景深", "胶片", "写实", "实拍",
              "4K", "8K", "大师", "电影感", "史诗", "国家地理", "油画", "水墨")
# 文字类禁用词单独一组：它们有安全的否定式写法（「远景看不出内容」），风格词没有。
# wiki-bot 实测过一次事故：「簿册字迹不可辨」被纯子串匹配拦掉，整期停更 —— 而那句
# 正是当时 prompt 教的措辞。所以扫描前先抹掉否定搭配，只对文字类词开这个口子。
BANNED_TEXT = ("文字", "铭文", "字迹", "字样", "招牌", "标牌", "水印")
NEG_OK = (
    r"(?:字迹|文字|铭文|字样|标牌|招牌|水印)[^，。；、]{0,4}?"
    r"(?:不可辨|不可辨认|无法辨|难以辨|难辨|不可读|看不清|不可见|不入画)",
    r"(?:没有|不含|不出现|不见|未见|避开|无)(?:任何)?"
    r"(?:字迹|文字|铭文|字样|标牌|招牌|水印)",
)

THIN_ANCHOR = 150      # 与 fetch-material.py 的 THIN 同一口径


def clen(s):
    """中文字数：按字符算，剔除空白。"""
    return len(re.sub(r"\s", "", s or ""))


def leaves(o):
    """递归取所有标量叶子。只扫字符串值本身，不扫 json.dumps 的结果 ——
    嵌套对象收尾的 }} 是合法 JSON，扫序列化结果会必然误报占位符。"""
    if isinstance(o, dict):
        for v in o.values():
            yield from leaves(v)
    elif isinstance(o, list):
        for v in o:
            yield from leaves(v)
    else:
        yield "" if o is None else str(o)


_NUM = re.compile(r"\d+(?:\.\d+)?")


def _trace_numbers(profile, material):
    """profile 里带数字的字段，逐个数字在锚里找出处。返回找不到的清单。

    **单位换算会造成假警报**（锚写「2.7米」而 profile 写「270 cm」），所以这只能是
    WARN 不能是 FAIL。它的用途不是判真假，是让「薄锚却填出一堆精确数字」这件事
    在日志里看得见 —— 否则没有任何环节会注意到。
    """
    miss = []
    for k in PROFILE_NUMERIC:
        v = str(profile.get(k) or "")
        for n in _NUM.findall(v):
            if len(n) >= 2 and n not in material:   # 一位数太容易偶然命中，跳过
                miss.append("%s 的 %s" % (k, n))
    return miss


def _cutoff(date_str):
    """半年窗口的起始日期（含）。date_str 无法解析时返回 None，闸门跳过。"""
    try:
        d = dt.date.fromisoformat(date_str)
    except Exception:
        return None
    return (d - dt.timedelta(days=lib.WINDOW)).isoformat()


def check(c, pick=None, posts=(), no_dup=False):
    """返回 (err, warn) 两个列表。纯函数，--selftest 直接调它。"""
    err, warn = [], []
    pick = pick or {}

    # ---- 伪注释键：整期停更过一次，放最前面 ----
    fake = [k for k in c if str(k).startswith("__")]
    if fake:
        err.append("顶层出现伪注释键 %s —— JSON 没有注释语法，模型会把它当成真实"
                   "容器（§7.2）" % fake)

    # ---- 必填标量 ----
    for k, lo, hi in (("date", 10, 10), ("group", 1, 20), ("title", 1, 22),
                      ("subject", 1, 30), ("scientific_name", 1, 60),
                      ("summary", 80, 120)):
        v = c.get(k).strip() if isinstance(c.get(k), str) else ""
        if not v:
            err.append("%s 缺失或为空" % k)
        elif not (lo <= clen(v) <= hi):
            # summary 的区间是硬的（§7.2 写明 80–120 字导语），其余长度只提醒
            (err if k == "summary" else warn).append(
                "%s 长度 %d 不在 %d–%d" % (k, clen(v), lo, hi))

    sci = (c.get("scientific_name") or "").strip()
    if sci and not lib.SCI_RE.match(sci):
        err.append("scientific_name「%s」不是合法的双名/三名法格式" % sci)

    # ---- 与 pick.json 的身份一致性 ----
    topic = pick.get("topic") or {}
    for k, want in (("subject", topic.get("subject")),
                    ("scientific_name", topic.get("scientific_name")),
                    ("group", pick.get("group")),
                    ("date", pick.get("date"))):
        got = (c.get(k) or "").strip() if isinstance(c.get(k), str) else ""
        if want and got and got != str(want).strip():
            err.append("%s 与今日选题不一致：写的是「%s」，派下来的是「%s」——"
                       "去重主键靠它，换主体等于绕过半年窗口" % (k, got, want))

    # ---- 半年重复硬闸门（§5.1）----
    cutoff = _cutoff(c.get("date") or pick.get("date"))
    if not no_dup and posts and cutoff:
        subj = (c.get("subject") or "").strip()
        for p in posts:
            if p.get("date", "") < cutoff or p.get("date") == c.get("date"):
                continue
            if subj and p.get("subject") == subj:
                err.append("subject「%s」在 %s 推过（%d 天窗口内）"
                           % (subj, p["date"], lib.WINDOW))
                break
            if sci and p.get("scientific_name") == sci:
                err.append("学名「%s」在 %s 以「%s」推过 —— 同一物种换个中文名"
                           "不能骗过半年窗口" % (sci, p["date"], p.get("subject")))
                break

    # ---- sections ----
    ss = c.get("sections")
    if not isinstance(ss, list) or len(ss) != 3:
        err.append("sections 必须恰好 3 段（实际 %s）"
                   % (len(ss) if isinstance(ss, list) else "None"))
        ss = ss if isinstance(ss, list) else []
    else:
        for i, s in enumerate(ss, 1):
            if not isinstance(s, dict) or not (s.get("h") or "").strip():
                err.append("sections[%d].h 缺失" % i)
                continue
            if not (s.get("p") or "").strip():
                err.append("sections[%d].p 缺失" % i)
            elif not (80 <= clen(s["p"]) <= 260):
                warn.append("sections[%d].p 长度 %d 不在 80–260" % (i, clen(s["p"])))

    total = sum(clen(s.get("p")) for s in ss if isinstance(s, dict))
    if total and not (240 <= total <= 800):
        warn.append("正文合计 %d 字，偏离 300–700 的目标区间" % total)

    # ---- entities ----
    ents = c.get("entities")
    if not isinstance(ents, list) or len(ents) < 3:
        err.append("entities 至少 3 个（实际 %s）"
                   % (len(ents) if isinstance(ents, list) else "None"))
    elif (c.get("subject") or "").strip() not in [str(e).strip() for e in ents]:
        err.append("entities 必须包含 subject「%s」" % c.get("subject"))

    # ---- profile ----
    prof = c.get("profile")
    if not isinstance(prof, dict):
        err.append("profile 缺失或不是对象")
        prof = {}
    else:
        iucn = (prof.get("iucn") or "").strip().upper()
        src = (prof.get("iucn_source") or "").strip()
        if iucn:
            if iucn not in IUCN_OK:
                err.append("profile.iucn「%s」不是 IUCN 等级代码（%s）"
                           % (prof.get("iucn"), "/".join(sorted(IUCN_OK))))
            # §7.2：等级不来自 IUCN 官方接口（§1），页面必须能标出处
            if not src:
                err.append("profile.iucn 有值但 iucn_source 为空 —— 保护等级不是"
                           "官方接口来的（§1），页面必须能标出处")
        elif src:
            warn.append("profile.iucn_source 有值但 iucn 为空")
        empty = [k for k in PROFILE_KEYS if not str(prof.get(k) or "").strip()]
        if len(empty) >= 7:
            warn.append("profile 有 %d/%d 个字段为空 %s —— 锚薄时这是正常的，"
                        "不要为了填满而编" % (len(empty), len(PROFILE_KEYS), empty))

    # ---- 薄锚下的编造嫌疑（本项目独有）----
    material = pick.get("material") or ""
    mlen = clen(material)
    if material:
        miss = _trace_numbers(prof, material)
        if miss:
            warn.append("这些数字在事实锚里找不到出处：%s —— 可能是单位换算（锚写"
                        "「2.7 米」而这里写「270 cm」），也可能是编的，请人工看一眼"
                        % "、".join(miss[:8]))
        if mlen < THIN_ANCHOR:
            filled = [k for k in PROFILE_KEYS if str(prof.get(k) or "").strip()]
            if len(filled) >= 7:
                warn.append("锚只有 %d 字（薄锚），profile 却填了 %d/%d 个字段 ——"
                            "这个组合几乎只能来自编造，逐条核一遍（§6.3-1）"
                            % (mlen, len(filled), len(PROFILE_KEYS)))
    elif pick:
        warn.append("今日无事实锚（material_status=%s）—— 正文只应写高置信度内容"
                    % pick.get("material_status", "?"))

    # ---- 约束 ③ 的弱信号：写成了「一类动物」而不是这一个物种 ----
    body = " ".join(str(s.get("p") or "") for s in ss if isinstance(s, dict))
    subj = (c.get("subject") or "").strip()
    if body and subj and body.count(subj) == 0:
        warn.append("三段正文里一次也没出现「%s」—— 检查是不是写成了类群的宽泛"
                    "介绍（约束 ③）" % subj)

    e2, w2 = _art_scan(c)
    return err + e2, warn + w2


def _art_scan(c):
    """配图描述与占位残留。返回 (err, warn)。

    art 缺失是 FAIL（不同于 wiki-bot 的 WARN）：本项目每期两张图是既定形态，
    §8.2 还要求 prompt 必须锚定物种，缺了 art 等于 gen-image.py 无从拼 prompt。
    """
    err, warn = [], []
    art = c.get("art")
    if not isinstance(art, dict) or not art:
        err.append("art 缺失 —— 每期两张图是既定形态，gen-image.py 靠它拼 prompt")
        art = {}
    for k, lbl in (("main", "主图"), ("sub", "附图")):
        a = art.get(k) or {}
        s = (a.get("subject") or "").strip() if isinstance(a, dict) else ""
        if not s:
            if art:
                err.append("art.%s.subject 缺失（%s）" % (k, lbl))
            continue
        if not (25 <= clen(s) <= 90):
            warn.append("art.%s.subject 长度 %d 不在 25–90（%s）" % (k, clen(s), lbl))
        # alt 是 prompt 里承诺过的必填项（无障碍朗读 + 裂图兜底），校验器必须查，
        # 否则那条要求形同虚设 —— 它不会报错，只会让页面在裂图时什么也不说。
        al = (a.get("alt") or "").strip() if isinstance(a, dict) else ""
        if not al:
            err.append("art.%s.alt 缺失（%s）—— 无障碍与裂图兜底都靠它" % (k, lbl))
        elif clen(al) > 45:
            warn.append("art.%s.alt 长度 %d 超过 40（%s）" % (k, clen(al), lbl))
        hit = [b for b in BANNED_ART if b in s]
        if hit:
            err.append("art.%s.subject 含写实/摄影类禁用词 %s（%s）—— §8.2 ①：AI 动物图"
                       "极可能物种特征错误，照片风格会被读者当成真实影像" % (k, hit, lbl))
        # 文字类词先抹掉安全的否定式再扫（wiki-bot 曾因「字迹不可辨」整期停更）
        scan = s
        for p in NEG_OK:
            scan = re.sub(p, "", scan)
        hit2 = [b for b in BANNED_TEXT if b in scan]
        if hit2:
            err.append("art.%s.subject 含文字类禁用词 %s（%s）—— 图像模型产出的文字是"
                       "乱码字形" % (k, hit2, lbl))
        elif scan != s:
            warn.append("art.%s.subject 用否定句提到文字（%s）—— 已放行，但更稳的写法是"
                        "「远景里看不出内容」" % (k, lbl))
        if re.search(r"\d{3,4}\s*年|公元前?\s*\d+", s):
            err.append("art.%s.subject 含年份数字（%s）—— 模型只会画成乱码" % (k, lbl))
    ms = ((art.get("main") or {}).get("subject") or "").strip() if art else ""
    bs = ((art.get("sub") or {}).get("subject") or "").strip() if art else ""
    # 复用去重那套字符二元组：主图与附图必须是不同画面。不查的后果是「花两张图的钱
    # 拿到一张图的信息量」，且没有任何人会发现。
    if ms and bs and lib.jaccard(ms, bs) >= 0.50:
        err.append("主图与附图描述过于相似（%.2f），附图无信息增量" % lib.jaccard(ms, bs))

    hit3 = next((m for s in leaves(c) if (m := PLACEHOLDER.search(s))), None)
    if hit3:
        err.append("含占位/未完成文本：%r" % hit3.group())
    if not (c.get("tags") or []):
        warn.append("tags 为空")
    # uncertain 长期为空是可疑信号，不是错误 —— 物种条目里几乎总有存疑处（旧版评估的
    # 保护等级、事实块与正文冲突的数据）。空数组通常意味着模型在硬编而不是标注。
    if not (c.get("uncertain") or []):
        warn.append("uncertain 为空 —— 注意模型是不是在硬编而非标注存疑")
    return err, warn


# ---------------------------------------------------------------- 内置用例
# 一份手写的合格样本 + 故意写错的 11 种。§12 阶段 6 的验收线是「手写一份 content.json
# 过校验；故意写错 6 种情况都被拦」—— 多出来的几种都是有来历的：伪注释键、
# 「字迹不可辨」误杀、学名错一个字母，三件都真的让某个项目停更或写错过。
GOOD_PICK = {
    "date": "2026-09-02", "group": "marine", "group_label": "海洋动物",
    "region": "古北界",
    "topic": {"title": "一场淘皮热几乎把它捕到绝迹", "subject": "北海狗",
              "scientific_name": "Callorhinus ursinus",
              "note": "雄性守领域组成一夫多妻群；十九世纪皮毛贸易几乎捕绝",
              "wiki": "北海狗"},
    "material": "学名：Callorhinus ursinus（Linnaeus, 1758）\n"
                "IUCN 红色名录：易危\n生物地理界：古北界\n————\n"
                "北海狗是海狗科的一种，雄性成年长约 2.1 米，重达 270 公斤，"
                "雌性长约 1.5 米。繁殖期集群于白令海的岛屿，雄性占据领域并与多头雌性"
                "交配。十九世纪的毛皮贸易使其数量锐减，1911 年的条约后才开始恢复。",
    "material_status": "local", "near": [], "recycled": False,
}

GOOD = {
    "date": "2026-09-02",
    "group": "marine",
    "group_label": "海洋动物",
    "title": "一场淘皮热几乎把它捕到绝迹",
    "subject": "北海狗",
    "scientific_name": "Callorhinus ursinus",
    "summary": "北海狗是分布于北太平洋的海狗科动物，雄性体重可达雌性的四倍以上，"
               "繁殖期在白令海岛屿上占据领域。十九世纪的毛皮贸易几乎把它捕绝，"
               "1911 年的国际条约之后种群才缓慢恢复。",
    "entities": ["北海狗", "白令海", "北太平洋", "毛皮贸易"],
    "tags": ["海狗科", "北太平洋", "易危"],
    "uncertain": ["1911 年条约的具体配额安排 —— 锚里只提到条约存在，细节未写明"],
    "profile": {
        "iucn": "VU", "iucn_source": "zhwiki",
        # 单位照锚写。第一版这里写「210 厘米」而真实锚写的是「长约2.1米」，
        # _trace_numbers 当场报了一条找不到出处 —— 那是**真阳性也是假警报**：数值对，
        # 单位换算过了。这就是它只能是 WARN 的原因，也是「照锚抄，别换算」的理由。
        "body_length": "雄性成年长约 2.1 米",
        "weight": "雄性重达 270 公斤",
        "lifespan": "", "habitat": "北太平洋岛屿海岸与近海",
        "range_text": "白令海、鄂霍次克海、加利福尼亚沿岸",
        "biogeo": "古北界", "diet": "",
    },
    "sections": [
        {"h": "它住在哪里", "p": "北海狗一年里大半时间在北太平洋的开阔海域游荡，"
         "只在繁殖期回到白令海的几处岛屿。上岸的地点极其集中，一小片砾石滩上可以挤下"
         "成千上万头，气味与叫声在数公里外就能察觉。这种把整个种群压缩到少数几个滩头"
         "的习性，让它在毛皮贸易时代格外脆弱：找到一处滩，就等于找到了一整年的猎物。"},
        {"h": "怎么活下来的", "p": "雄性体型远大于雌性，上岸后先占下一块砾石地，再与"
         "进入这块地的雌性交配。守领域期间它几乎不下海进食，靠此前积累的脂肪硬撑，"
         "掉下来的体重相当可观。这套一夫多妻的安排把繁殖机会集中到少数个体身上，"
         "代价是雄性要用整个夏天的饥饿去换。"},
        {"h": "一个反直觉的点", "p": "让北海狗种群止跌的不是禁猎，而是一份把猎捕权"
         "重新分配的条约。1911 年的协定让相关国家在陆上停止大规模捕杀，改由管理方"
         "统一收获并分成，猎捕由此从抢夺变成了有配额的生意。保护在这里不是靠禁止，"
         "而是靠让每一方都不愿意看到这个种群消失。"},
    ],
    "art": {
        "main": {"subject": "一头雄性北海狗立在砾石滩上，周围散布着体型小得多的雌性，"
                            "背景是低矮的岛屿丘陵与灰白的海面，晨光斜照，视角略低",
                 "alt": "砾石滩上一头大型雄性海狗与周围的雌性"},
        "sub": {"subject": "同一物种的头部特写，吻部前伸、耳廓外露，须毛清晰可数，"
                           "浅色底，标本图谱式的平光",
                "alt": "海狗头部侧面，耳廓与须毛清晰"},
    },
}


def _mut(**kw):
    """深拷一份 GOOD 再改几个键。用例只写差异，样本本身只有一份。"""
    c = json.loads(json.dumps(GOOD))
    for k, v in kw.items():
        if "." in k:
            a, b = k.split(".", 1)
            c.setdefault(a, {})[b] = v
        elif v is None:
            c.pop(k, None)
        else:
            c[k] = v
    return c


def _cases():
    """(描述, content, pick, posts, 期望拦不拦, 期望拒因里含的关键词)"""
    dup_post = [{"date": "2026-05-20", "subject": "北海狗",
                 "scientific_name": "Callorhinus ursinus"}]
    alias_post = [{"date": "2026-06-01", "subject": "海熊",
                   "scientific_name": "Callorhinus ursinus"}]
    art_txt = _mut()
    art_txt["art"]["sub"]["subject"] = "同一物种的头骨标本置于浅色台面，旁边一枚标签" \
                                       "字迹不可辨，平光，无背景"
    art_same = _mut()
    art_same["art"]["sub"]["subject"] = art_same["art"]["main"]["subject"]
    return [
        ("合格样本", GOOD, GOOD_PICK, [], True, ""),
        # ↓ 学名错一个字母。这是本项目最危险的一种错误：页面看起来完全正常，
        #   而去重主键与物种身份都已经错了（阶段 3 的紫晶林星蜂鸟就是这么写错的）
        ("学名错一个字母", _mut(scientific_name="Callorhinus ursinis"),
         GOOD_PICK, [], False, "与今日选题不一致"),
        ("悄悄换了主体", _mut(subject="南海狗",
                        entities=["南海狗", "白令海", "北太平洋"]),
         GOOD_PICK, [], False, "与今日选题不一致"),
        ("sections 只有 2 段", _mut(sections=GOOD["sections"][:2]),
         GOOD_PICK, [], False, "恰好 3 段"),
        ("entities 不含 subject", _mut(entities=["白令海", "北太平洋", "毛皮贸易"]),
         GOOD_PICK, [], False, "必须包含 subject"),
        # ↓ 第一版这句只加到 119 字，差 1 字没越界，用例「看起来在测超长」而其实
        #   什么都没测到。断言方向选对了才暴露出来：期望「被拦」的用例空转会立刻变红，
        #   期望「通过」的用例空转则永远是绿的。
        ("summary 超长", _mut(summary=GOOD["summary"] + "此外它还是北太平洋"
                            "海洋哺乳动物研究里最常被引用的物种之一，相关的观测"
                            "与标记记录相当多。"),
         GOOD_PICK, [], False, "summary 长度"),
        ("iucn 有值但没写出处", _mut(**{"profile.iucn_source": ""}),
         GOOD_PICK, [], False, "iucn_source 为空"),
        # ↓ JSON 没有注释语法。wiki-bot 的骨架里写过一个 __motif__ 伪键，模型把它
        #   当成真实容器，整期停更（§7.2）
        ("伪注释键", _mut(__note__="按今日 group 填"),
         GOOD_PICK, [], False, "伪注释键"),
        ("配图写成照片风格",
         _mut(**{"art.main": {"subject": "一头雄性北海狗立在砾石滩上的高清照片，"
                                         "长焦镜头压缩背景，浅景深", "alt": "海狗"}}),
         GOOD_PICK, [], False, "写实/摄影类禁用词"),
        ("主图附图几乎同一画面", art_same, GOOD_PICK, [], False, "过于相似"),
        ("占位残留", _mut(**{"profile.diet": "TODO"}),
         GOOD_PICK, [], False, "占位"),
        # ↓ 半年窗口：同名撞、以及换个中文名撞同一学名
        ("半年内同名重复", GOOD, GOOD_PICK, dup_post, False, "天窗口内"),
        ("换个中文名撞同一学名", GOOD, GOOD_PICK, alias_post, False, "同一物种换个中文名"),
        # ↓ 必须放行：文字类禁用词的安全否定式。wiki-bot 就是按字面拦掉
        #   「簿册字迹不可辨」而整期停更的，而那句正是它自己的 prompt 教的
        ("「字迹不可辨」必须放行", art_txt, GOOD_PICK, [], True, ""),
    ]


def _prompt_sync():
    """校验器拦的每一个禁用词，prompt.md 里必须都写了。返回 (ok, 说明)。

    **两份词表无法合并成一份**：一份是给模型读的散文，一份是给机器执行的元组。
    合不了就得钉住 —— 这是阶段 5 那条「同一判据两处实现必然分叉」的应对，而分叉在
    这里的后果是具体的：新增一个禁用词却忘了写进 prompt，模型无从避开，撞上就停更；
    wiki-bot 真的这样停过一期（prompt 教的措辞被自己的校验器拦掉）。

    只做单向（校验器 → prompt）。反方向没法机械判：prompt 里还有「壮美」「震撼」
    这类只做举例、不进词表的词，双向比对会一直红着，最后被人关掉 —— 那比没有更糟。
    """
    ppath = os.path.join(lib.ROOT, "prompt.md")
    if not os.path.exists(ppath):
        return False, "找不到 %s" % ppath
    txt = open(ppath, encoding="utf-8").read()
    miss = [w for w in BANNED_ART + BANNED_TEXT if w not in txt]
    if miss:
        return False, "这些禁用词 prompt.md 里没写，模型无从避开：%s" % miss
    return True, "%d 个禁用词全部在 prompt.md 里列出" % len(BANNED_ART + BANNED_TEXT)


def selftest():
    cases = _cases()
    bad = 0
    print("用例 %d 条\n" % len(cases))
    for desc, c, pick, posts, want_ok, want_kw in cases:
        err, warn = check(c, pick, posts)
        ok = not err
        good = (ok == want_ok) and (want_ok or any(want_kw in e for e in err))
        bad += 0 if good else 1
        detail = "通过（%d 提醒）" % len(warn) if ok else "拒：%s" % err[0][:46]
        print("  %s %-26s %s" % ("OK  " if good else "FAIL", desc, detail))
        if not good:
            if want_ok:
                for e in err:
                    print("        ← 不该被拦：%s" % e)
            else:
                print("        ← 期望拒因含「%s」" % want_kw)

    ok, info = _prompt_sync()
    bad += 0 if ok else 1
    print("  %s %-26s %s" % ("OK  " if ok else "FAIL", "prompt 与禁用词表同步", info))

    n = len(cases) + 1
    print("\n%d/%d 通过" % (n - bad, n))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?")
    ap.add_argument("--pick")
    ap.add_argument("--no-dup", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    cpath = a.path or os.path.join(lib.ROOT, "content.json")
    ppath = a.pick or os.path.join(lib.ROOT, "pick.json")
    try:
        c = json.load(open(cpath, encoding="utf-8"))
    except FileNotFoundError:
        print("FAIL 缺少 %s" % cpath, file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print("FAIL %s 不是合法 JSON：%s" % (cpath, e), file=sys.stderr)
        return 1
    if not isinstance(c, dict):
        print("FAIL content.json 顶层不是对象", file=sys.stderr)
        return 1

    pick = json.load(open(ppath, encoding="utf-8")) if os.path.exists(ppath) else {}
    err, warn = check(c, pick, lib.load_posts(), a.no_dup)

    for w in warn:
        print("WARN %s" % w)
    for e in err:
        print("FAIL %s" % e, file=sys.stderr)
    if err:
        print("—— 共 %d 项不合格，拒绝渲染" % len(err), file=sys.stderr)
        return 1
    total = sum(clen(s.get("p")) for s in (c.get("sections") or [])
                if isinstance(s, dict))
    print("OK selfcheck 通过（%d 项提醒）正文 %d 字" % (len(warn), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
