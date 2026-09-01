#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实测 SPEC §6.1b 的「事实锚终选」判据到底成不成立。

判据是：按 zh → zh_alt 顺序取 zhwiki 正文，要求
    ① 不是重定向页
    ② 正文出现该物种学名
第一个过关的名字才是终选。

**第一次实测就推翻了 ①。** 「薮猫」重定向到繁体「藪貓」，按 ① 直接判死 —— 而 zhwiki
把大量条目存在繁体标题下、简体标题只是 #REDIRECT，照 ① 会成片误杀。wiki-bot 的
fetch-material.py 早就记了这件事（"不跟随的话会拿到一个非空的垃圾串当事实锚"）。

所以 ① 要拆成两类重定向：
    薮猫 → 藪貓    繁简同名   通过。subject 用简体，锚文从繁体标题取
    猫熊 → 大熊猫  真别名     拒。zhwiki 认为正名是另一个，继续试下一个候选
判据变成「繁简归一化后同名才跟随」。这也是 queue.tsv 要有第 8 列 wiki 的原因：
读者看到的名字和锚文所在的标题可以不是一个串。

这个探针**不写任何产物**，只回答一个问题：拿 SPEC 自己列的四个误例
（汤匙 / 仙鹤 / 小龙虾 / 猫熊）去跑，判据能不能把它们判掉、并落到正确的名字上。
判据不成立就别写 build-queue.py —— 那才是真正会往池子里写东西的脚本。

取文机制直接借 wiki-bot 的 fetch-material.py（已在生产上跑了一个月），不重写：
重写一份的话，测的是重写件，不是将来真正用的那套。
"""
import importlib.util
import os
import re
import sys

# zhconv 没装在系统 site-packages 里，是 vendor 的。照 import-gbif.py 的同一套回退。
for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor"),
           "/opt/wiki/vendor"):
    if os.path.isdir(_p):
        sys.path.insert(0, os.path.abspath(_p))
        break
import zhconv

WIKI = "/opt/wiki"


def load_wiki_module():
    """把带连字符的 fetch-material.py 当模块加载。它不能 import，只能这么来。"""
    spec = importlib.util.spec_from_file_location(
        "fm", os.path.join(WIKI, "fetch-material.py"))
    fm = importlib.util.module_from_spec(spec)
    sys.modules["fm"] = fm
    # fetch-material.py 里的 CACHE 是相对路径，得先切进去，否则会去下载 41MB 索引。
    # sys.path 也得加：它 import lib，指的是 wiki-bot 的 lib 而不是本项目的。
    cwd = os.getcwd()
    os.chdir(WIKI)
    sys.path.insert(0, WIKI)
    try:
        spec.loader.exec_module(fm)
    finally:
        os.chdir(cwd)
        sys.path.remove(WIKI)
        # 把 wiki-bot 的 lib 从模块表里摘掉，否则本文件后面 import lib 会拿到它。
        # 两个项目各有一份 lib.py，同名不同物 —— 这一步不做，RANK_SUFFIX 就会
        # 静默取到 wiki-bot 那份（它压根没有这个名字，只会 AttributeError；
        # 但要是哪天它有了同名不同值的常量，就是静默错值了）。
        sys.modules.pop("lib", None)
    return fm


def load_self_lib():
    """本项目的 lib。必须在 load_wiki_module 之后调，理由见上。"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
    import lib
    return lib


# SPEC §6.1b 的误例表 + 对照的正例。
# 第三列是「期望终选落到哪个名字」，None 表示这一组应该全军覆没（没有可用的名字）。
CASES = [
    # (学名, 候选名按 refine 的初选顺序, 期望终选)
    ("Rhinobatos hynnicephalus", ["汤匙", "斑纹犁头鳐"], "斑纹犁头鳐"),
    ("Grus japonensis",          ["仙鹤", "丹顶鹤"],     "丹顶鹤"),
    ("Procambarus clarkii",      ["小龙虾", "克氏原螯虾"], "克氏原螯虾"),
    ("Ailuropoda melanoleuca",   ["猫熊", "大熊猫"],     "大熊猫"),
    # ↓ 正例：初选就是对的，判据不该把它们推翻
    ("Puma concolor",            ["美洲狮"],            "美洲狮"),
    ("Leptailurus serval",       ["薮猫"],              "薮猫"),
    ("Ursus arctos",             ["棕熊"],              "棕熊"),
    ("Panthera tigris",          ["虎"],                "虎"),
    # ↓ 繁简重定向必须跟随。这几条是「薮猫」那次误杀暴露出来的一整类
    ("Neofelis nebulosa",        ["云豹"],              "云豹"),
    # ↓ 期望「拒」。我原先凭语感写的是「小熊猫」，实测 zhwiki 把它重定向到属级条目
    #   「小熊猫属」—— 拿属级条目当事实锚，写出来就是一类动物的宽泛介绍，正撞约束 ③。
    #   所以拒是对的，错的是我的期望。这类别名救不回来（属级名会被 refine 剔掉），
    #   和「仙鹤→丹顶鹤」那种能靠 zh_alt 接住的不是一回事，拒因要分开记。
    ("Ailurus fulgens",          ["小熊猫"],            None),
    # ↓ 只有繁体名进了池的情况：subject 该用繁体串，锚也在那儿
    ("Leptailurus serval",       ["藪貓"],              "藪貓"),
]


def zh_key(s):
    """繁简归一化。判断两个标题是不是「同一个名字的两种写法」用这个，不用字面相等。"""
    return zhconv.convert(s or "", "zh-cn")


def verdict(fm, idx, offs, title, sci, depth=0):
    """单个候选名的锚判定。返回 (ok, 原因, 锚文所在标题, 正文长度)。

    重定向要分两类，见模块 docstring。返回的标题可能与传入的 title 不同（繁简），
    那个才是取锚文该用的串。
    """
    if title not in idx:
        return False, "not_in_index", "", 0
    off, _ = idx[title]
    xml = fm.fetch_stream(offs, off)
    raw = fm.extract(xml, title)
    if raw is None:
        return False, "no_text", "", 0

    # ① 重定向
    m = fm.REDIR.match(raw)
    if m:
        tgt = m.group(1).strip()
        if depth >= 2:
            return False, "redirect_too_deep", "", len(raw)
        if zh_key(tgt) != zh_key(title):
            # 真别名：zhwiki 认为正名是另一个串。不自动采用 —— 那个串没过统称/黑名单
            # 闸门，采用它等于给约束 ③ 开后门。记下来，继续试下一个候选。
            #
            # 目标是分类层级名的要单独记（alias-rank）：那一类救不回来。实测
            # 「小熊猫」重定向到「小熊猫属」，而属级名在 refine 就被剔了，不可能
            # 出现在 zh_alt 里。而「仙鹤→丹顶鹤」这种目标是具体物种名的，
            # 通常就在 zh_alt 里等着，下一轮就接住了。
            kind = "alias-rank" if tgt.endswith(RANK_SUFFIX) else "alias"
            return False, kind + "->" + tgt, "", len(raw)
        ok, why, at, n = verdict(fm, idx, offs, tgt, sci, depth + 1)
        return ok, ("hans->" + tgt + "/" + why), at, n

    # ② 正文出现学名。属名+种加词分开找 —— zhwiki 常写成
    #    「''Panthera tigris''」但也有写「''P. tigris''」的
    genus, _, epithet = sci.partition(" ")
    epithet = epithet.split(" ")[0]
    hit_full = sci.lower() in raw.lower()
    hit_abbr = bool(re.search(
        r"\b" + re.escape(genus[0]) + r"\.\s*" + re.escape(epithet), raw, re.I))
    if not (hit_full or hit_abbr):
        return False, "no_sci", "", len(raw)
    return True, "full" if hit_full else "abbr", title, len(raw)


def main():
    fm = load_wiki_module()
    global RANK_SUFFIX
    RANK_SUFFIX = load_self_lib().RANK_SUFFIX
    idx, offs = fm.load_index()
    print()

    bad = 0
    for sci, cands, want in CASES:
        picked, anchor, trace = None, "", []
        for t in cands:
            ok, why, at, n = verdict(fm, idx, offs, t, sci)
            trace.append("%s[%s%s]" % (t, why, "" if not n else " %dB" % n))
            if ok:
                picked, anchor = t, at
                break
        good = (picked == want)
        bad += 0 if good else 1
        print("%s %-26s 终选=%-8s 锚=%-8s 期望=%-8s  %s" % (
            "✓" if good else "✗", sci, picked or "—", anchor or "—",
            want or "—", " → ".join(trace)))

    print()
    print("判据成立 %d/%d" % (len(CASES) - bad, len(CASES)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
