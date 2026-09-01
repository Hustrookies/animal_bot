#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事实锚预抓：queue.tsv → data/material.json。**运行时不联网。**

中文维基的 API 在境内被阻断，运行时抓取必然失败 —— wiki-bot 用一个月生产验证了这条。
所以锚必须预抓、清洗、**提交进 git**，`pick.py` 只读本地那一份。

── 取文层复用 wikitext.py，不再搬一遍 ──
wiki-bot 的 fetch-material.py 是「一个标题一次 HTTP Range」，226 条要 226 次请求。
本项目的 wikitext.Pages 按**流**取（一流 100 页，顺路的页一起收），而且建池时已经把
这 226 条的正文全抓进 .cache/pages.json —— 所以这一步实测是零网络。
zhwiki 那件事只该有一份实现（wikitext.py），这里只做「wikitext → 可读纯文本」。

── 清洗层搬 wiki-bot 的 clean()（已在生产跑了一个月）──
计数扫描删嵌套结构、-{zh-hans:…}- 繁简语法、双重 XML 反转义，这些坑它都踩过了。

── 物种条目与历史词条的三处实测差异，这个文件为它们各加了一条 ──

1. **52% 的条目，学名只存在于 {{Speciesbox}} 里；另有一大批只在 `{{lang|la|…}}` 里。**
   clean() 把模板整块删掉，学名跟着走 —— 实测 226 条里 159 条清洗后正文不含学名。
   而阶段 6 要校验模型写的学名，锚里没有就无从校验。两条修法：
     · 锚开头拼一个**事实块**，学名从 queue.tsv 取（GBIF 来的，已过三道闸门），
       命名人/异名/亚种从 Taxobox 抽，IUCN 与科/属从 ready.jsonl 取。
     · `{{lang|la|''X''}}` 这类**装正文文字**的模板不整块删，留最后一段
       （_keep_text_templates）。仅这一条就把「正文不含学名」从 159/226 降到 20/223。
   最终：**223 条锚全部含学名**，0 例外。

2. **物种条目普遍很薄。** clean 后中位 254 字，58/223（26%）不足 150 字（最短
   「加氏犬浣熊」49 字，它的 wikitext 原文总共只有 1136 B —— zhwiki 上就这么多）。
   历史词条动辄几千字，物种条目大量是 stub。所以 `thin` 不是失败而是一种状态，要统计
   出来 —— 阶段 6 的 prompt 必须允许「锚里没有就不写」，否则薄锚必然逼出编造。

3. **消歧义页能过锚判定。** 「马鹿」是消歧义页，列表项里写着 ''Cervus elaphus''，
   所以过得了含学名检测，clean 之后只剩 6 个字。判据已修在 wikitext.DAB（阶段 5
   补的第六个「非空 ≠ 可用」漏洞），这里再兜一道：抓的时候又遇到就明确报 dab，
   不要静静地写一条 6 字的锚进去。

── 顺带查出的第七个闸门漏洞 ──
逐条看锚时发现 `[212/224] 智人 ok 803 字` —— `Homo sapiens` 把四道闸门每一道都
合法走完了。判据集少了「不能是读者自己」这一条，因为它显然到没人写下来。
已修在 lib.EXCLUDE_GENUS。**每一批产出都得有一次人眼扫过去**，这是那次的唯一发现路径。

用法：
    fetch-material.py                抓所有缺锚的
    fetch-material.py --stat         覆盖率与逐条状态
    fetch-material.py --force 猎豹   强制重抓
    fetch-material.py --retry-thin   重试薄锚/失败的
    fetch-material.py --limit 10     只抓前 10 条（联调）
"""
import argparse
import json
import os
import re
import sys
from html import unescape as html_unescape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib
import wikitext

MPATH = os.path.join(lib.ROOT, "data", "material.json")

# 锚的取用长度。wiki-bot 用 600 —— 那是历史词条，几千字里取头 600 字就够开个头。
# 物种条目中位只有 246 字，p90 是 1901 字，600 会把长条目（猎豹 6593 字）最有用的
# 形态/行为描述截在导言里。放到 1400：厚条目多给一些，薄条目本来也到不了这个数。
MAXLEN = 1400

# 低于这个字数记 thin。不是失败 —— 薄是物种条目的常态（43% 不足 200 字），判失败会让
# 覆盖率永远不达标，而真正的问题不是"抓不到"而是"维基上就这么多"。分开记才看得清。
THIN = 150


# ---------------------------------------------------------------- wikitext → 纯文本
def _strip_nested(s, open_, close, keep_last_field=False):
    """用计数扫描删除成对嵌套结构。**正则做不到这件事** —— {{Speciesbox}} 跨几十行、
    内含嵌套模板与 [[链接]]，`\\{\\{[^{}]*\\}\\}` 那种写法永远匹配不上；图片说明里的
    嵌套 [[ ]] 也会让 `[^\\]]*` 提前收尾。

    keep_last_field=True 时保留 | 之后的最后一段（[[目标|显示文字]] → 显示文字）。
    搬自 wiki-bot fetch-material.py，一字未改。
    """
    out, i, n, ol, cl = [], 0, len(s), len(open_), len(close)
    while i < n:
        if s.startswith(open_, i):
            depth, j, start = 1, i + ol, i + ol
            while j < n and depth:
                if s.startswith(open_, j):
                    depth += 1
                    j += ol
                elif s.startswith(close, j):
                    depth -= 1
                    j += cl
                else:
                    j += 1
            inner = s[start: j - cl] if depth == 0 else s[start:]
            if keep_last_field:
                head = inner.split("|")[0]
                if re.match(r"^\s*(?:File|Image|文件|檔案|图像|圖像|Category|分类|分類)"
                            r"\s*:", head, re.I):
                    pass                        # 整块丢掉
                else:
                    out.append(_strip_nested(inner.split("|")[-1], open_, close, True))
            i = j
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


_ZH = re.compile(r"-\{(.*?)\}-", re.S)

# 装正文文字的模板：{{lang|la|''Geothelphusa caesia''}}、{{snamei|Panthera tigris}}。
# **它们不能和 Taxobox 一样整块删掉。** 「蓝灰泽蟹」的导言就是
# `'''蓝灰泽蟹'''（[[学名]]：{{lang|la|''Geothelphusa caesia''}}）为溪蟹科…`，
# 删掉模板后剩下「蓝灰泽蟹（学名：）为…」—— 一个空括号，看着像清洗坏了，而且正文里
# 本来有的学名跟着没了（这正是"52% 的锚正文不含学名"的一部分成因）。
# 白名单式处理，不能对所有模板都留最后一段 —— Taxobox 的最后一段会漏出参数值。
_TEXT_TPL = re.compile(
    r"\{\{\s*(?:lang(?:-[a-z-]+)?|snamei?|sname|taxon(?:name)?|拉|學名|学名|italic title)"
    r"\s*\|([^{}]*)\}\}", re.I)


def _keep_text_templates(w):
    """把装正文文字的模板换成它的最后一个参数。反复做 —— 可能嵌在别的结构里。"""
    for _ in range(3):
        prev = w
        w = _TEXT_TPL.sub(lambda mo: mo.group(1).split("|")[-1].strip(), w)
        if w == prev:
            break
    return w


def _zh_variant(w, prefer=("zh-hans", "zh-cn", "zh-sg", "zh-hant", "zh-tw", "zh-hk")):
    """处理 zhwiki 的繁简转换语法 -{zh-hans:X; zh-hant:Y;}-，优先取简体。
    不处理的话锚里会出现一长串 `-{zh-hant:…; zh-hans:…;}-` 原文。"""
    def one(mo):
        body = mo.group(1)
        # -{zh-hans;zh-hant|又稱曲紋灰蝶}- ：**带转换标志的形式**，标志在 | 之前。
        # 不剥掉的话锚里会出现「，zh-hans;zh-hant|又稱曲紋灰蝶」这种半截语法
        # （实测「亮灰蝶」就是）。判断条件是 | 出现在任何 : 之前 —— 否则会误伤
        # -{zh-hans:X; zh-hant:Y}- 里本来含 | 的正常文字。
        if "|" in body and (":" not in body or body.index("|") < body.index(":")):
            body = body.split("|", 1)[1]
        if ":" not in body:
            return body                      # -{纯文本}- 保护标记，去壳即可
        parts = {}
        for seg in body.split(";"):
            if ":" in seg:
                k, _, v = seg.partition(":")
                parts[k.strip().lower()] = v.strip()
        for k in prefer:
            if parts.get(k):
                return parts[k]
        return next((v for v in parts.values() if v), "")
    prev = None
    while prev != w:                         # 可能嵌套
        prev = w
        w = _ZH.sub(one, w)
    return w


def clean(w):
    """wikitext → 纯文本。锚只要陈述句，模板/表格/引用/图注全部丢掉。"""
    # ① 先反转义：dump 里的 XML 是实体化的，不做这步 <ref> 之类永远匹配不到，
    #    &lt;ref&gt; 会原样进锚，比没有锚更糟。个别页面双重转义，所以做两遍。
    for _ in range(2):
        prev = w
        w = html_unescape(w)
        if w == prev:
            break

    w = re.sub(r"<!--.*?-->", "", w, flags=re.S)
    w = re.sub(r"<ref[^>]*/\s*>", "", w, flags=re.I)
    w = re.sub(r"<ref[^>]*>.*?</ref\s*>", "", w, flags=re.S | re.I)
    w = re.sub(r"<(math|score|syntaxhighlight|gallery|timeline|imagemap)[^>]*>.*?</\1\s*>",
               "", w, flags=re.S | re.I)

    # ② 表格、模板、链接：一律用计数扫描，不用正则
    w = re.sub(r"^\s*\{\|.*?^\s*\|\}", "", w, flags=re.S | re.M)   # 表格（行首锚定）
    w = _keep_text_templates(w)                                     # {{lang|la|…}} 留文字
    w = _strip_nested(w, "{{", "}}")                                # 其余模板全删
    w = _zh_variant(w)                                              # -{zh-hans:…}-
    w = _strip_nested(w, "[[", "]]", keep_last_field=True)          # 内链留显示文字
    w = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", w)            # 外链留文字
    w = re.sub(r"\[https?://\S+\]", "", w)
    w = re.sub(r"https?://\S+", "", w)

    w = re.sub(r"'''''|'''|''", "", w)
    w = re.sub(r"<[^>]+>", "", w)                                   # 残余 HTML 标签
    w = re.sub(r"^\s*[*#:;].*$", "", w, flags=re.M)                 # 列表行
    w = re.sub(r"^\s*=+.*?=+\s*$", "", w, flags=re.M)               # 小节标题

    # ③ 清掉模板被剥离后留下的空壳：「猎豹（），是」这种
    for _ in range(3):
        prev = w
        w = re.sub(r"（\s*[，、；\s]*\s*）|\(\s*[,;\s]*\s*\)|「\s*」|《\s*》|\[\s*\]", "", w)
        w = re.sub(r"，\s*，", "，", w)
        # 删掉一段行内结构后会留下「。；」「，。」这类叠标点（实测「猎豹」有一处）
        w = re.sub(r"。\s*[；;，,]", "。", w)
        w = re.sub(r"，\s*。", "。", w)
        if w == prev:
            break
    w = w.replace("\u00a0", " ").replace("&nbsp;", " ")
    w = re.sub(r"[ \t]+", " ", w)
    w = re.sub(r"\n{2,}", "\n", w)
    w = re.sub(r"^[\s，。、；：]+", "", w)
    return w.strip()


# ---------------------------------------------------------------- Taxobox 抽字段
def _template_body(s, start):
    """从 s[start] 处的 `{{` 开始，按配平取出模板体（不含收尾的 }}）。

    **不能用「切 4000 字然后正则找字段」那种写法。** 实测「狷羚」的 Speciesbox 里
    `authority` 是最后一个字段，紧跟着 `\\n}}` 和正文；`_field` 的续行规则只排除了
    行首的 `|`，于是它把 `}}` 连同整篇正文一起当成 authority 的值吞了进去 ——
    锚的事实块变成「学名：Alcelaphus buselaphus（Pallas, 1766 }} 狷羚（）是一種…）」。
    非空、看着像有值，所以任何"抽到了没有"的检查都拦不住它。
    模板的边界只能由配平决定，这是第 N 次同一结论：结构性文本别用正则划边界。
    """
    depth, i, n = 1, start + 2, len(s)
    while i < n and depth:
        if s.startswith("{{", i):
            depth += 1
            i += 2
        elif s.startswith("}}", i):
            depth -= 1
            i += 2
        else:
            i += 1
    return s[start + 2: i - 2] if depth == 0 else s[start + 2:]


def _field(box, key):
    """从 Taxobox 文本里取一个字段值。到下一个行首 | 或结尾为止。

    box 必须是配平截出来的模板体（见 _template_body），否则续行会跑出模板外。
    """
    m = re.search(r"^\s*\|\s*" + key + r"\s*=([^\n]*(?:\n(?!\s*\|)[^\n]*)*)",
                  box, re.I | re.M)
    if not m:
        return ""
    v = m.group(1)
    v = html_unescape(html_unescape(v))
    v = re.sub(r"<ref[^>]*>.*?</ref\s*>", "", v, flags=re.S | re.I)
    v = re.sub(r"<ref[^>]*/\s*>", "", v, flags=re.I)
    v = _strip_nested(v, "{{", "}}")
    v = _strip_nested(v, "[[", "]]", keep_last_field=True)
    v = re.sub(r"<[^>]+>", " ", v)
    v = re.sub(r"'''|''", "", v)
    # 模板被剥离后留下的空括号：异名字段里常见「Ptychobarbus laticeps Day, 1877 （）」
    v = re.sub(r"（\s*）|\(\s*\)", "", v)
    v = re.sub(r"\s+", " ", v)
    return v.strip(" ,;|")


def taxobox_facts(raw):
    """从 {{Speciesbox}}/{{Taxobox}} 抽可核查的事实。返回 dict。

    只抽**不会被 clean() 保留下来、而且模型编不出来**的那几样：
        authority  命名人与年份（440/512 条有）——「Schreber, 1775」这种编不出来
        synonyms   异名（284 条有）—— 解释"为什么有的资料叫另一个名字"
        subspecies 亚种（27 条有）
    分布/体重这类**不抽** —— 它们在 range_map_caption 里只是图注文字，
    真正的分布描述在正文，抽出来只会和正文重复且更不准。
    """
    m = wikitext.TAXOBOX.search(raw or "")
    if not m:
        return {}
    box = _template_body(raw, m.start())
    out = {}
    for k, keys in (("authority", ("authority", "binomial_authority")),
                    ("synonyms", ("synonyms",)),
                    ("subspecies", ("subspecies", "subdivision"))):
        for key in keys:
            v = _field(box, key)
            if v:
                # authority 常写成「(Schreber, 1775)」——括号是分类学的「该学名已换属」
                # 记号，但 fact_block 还要再套一层中文括号，不去掉就成了「（(…)）」。
                if k == "authority":
                    v = v.strip().strip("()（）").strip()
                else:
                    # 亚种/异名在 wikitext 里是 `* 甲\n* 乙` 的列表，_field 抽出来就是
                    # 一行带 * 的串。换成顿号分隔，锚里才读得通。
                    v = re.sub(r"\s*\*\s*", "、", v).strip("、 ")
                out[k] = v[:120]
                break
    return out


IUCN_ZH = {"LEAST_CONCERN": "无危", "NEAR_THREATENED": "近危", "VULNERABLE": "易危",
           "ENDANGERED": "濒危", "CRITICALLY_ENDANGERED": "极危",
           "EXTINCT_IN_THE_WILD": "野外灭绝", "DATA_DEFICIENT": "数据缺乏"}


def fact_block(row, facts, gbif):
    """锚开头的事实块。

    **学名必须在这里出现**，因为 52% 的条目清洗后正文里没有它（只在 Taxobox 内）。
    学名取自 queue.tsv 第 5 列 —— 那是 GBIF 来的、已过三道分类闸门，比从正文里
    正则抠可靠。IUCN 与科/属同理，来自建池时的 GBIF 记录（ready.jsonl）。
    这一块是**给模型看的已核实事实**，也是阶段 6 selfcheck 校验学名时的依据。

    两个来源分得很清楚：`facts` 来自 zhwiki 的 Taxobox（命名人、异名、亚种），
    `gbif` 来自 GBIF（IUCN、科、属）。冲突时不做仲裁 —— 这里只有事实并列，
    判断留给写正文的那一步，锚不替它做决定。
    """
    ls = ["学名：%s" % row["scientific_name"]]
    if facts.get("authority"):
        ls[0] += "（%s）" % facts["authority"]
    tax = "、".join(x for x in (gbif.get("family"), gbif.get("genus")) if x)
    if tax:
        ls.append("分类（GBIF）：%s" % tax)
    if gbif.get("iucn_raw"):
        ls.append("IUCN 红色名录：%s" % IUCN_ZH.get(gbif["iucn_raw"], gbif["iucn_raw"]))
    if row.get("region"):
        ls.append("生物地理界：%s" % row["region"])
    if facts.get("synonyms"):
        ls.append("异名：%s" % facts["synonyms"])
    if facts.get("subspecies"):
        ls.append("亚种：%s" % facts["subspecies"])
    return "\n".join(ls)


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="append", default=[])
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--retry-thin", action="store_true",
                    help="重试薄锚或失败的条目")
    a = ap.parse_args()

    mat = json.load(open(MPATH, encoding="utf-8")) if os.path.exists(MPATH) else {}
    queue = lib.load_queue()
    # 键用 subject（读者看到的名字，pick.py 拿它查），查维基用第 8 列 wiki。
    # 两者 21% 的情况下不同串 —— 拿 subject 去索引里找会成片扑空（SPEC §6.3）。
    rows, seen = [], set()
    for q in queue:
        if q["subject"] in seen:
            continue
        seen.add(q["subject"])
        rows.append(q)

    if a.stat:
        ok = [r for r in rows if mat.get(r["subject"], {}).get("text")]
        # 薄不薄看 **body_len**（正文），不看 len（含事实块的总长）。
        # 第一版这里用了 len，于是 --stat 报 20 条薄锚而抓取时报 63 条 —— 事实块恒定
        # 加 60–100 字，正好把薄锚糊到阈值以上。同一个判据两处各写一遍，必然分叉；
        # 而这次分叉的方向是**让报告更好看**，最危险的那个方向。
        thin = [r for r in ok if mat[r["subject"]].get("body_len", 0) < THIN]
        print("覆盖率 %d/%d = %d%%（其中薄锚 %d 条，正文 <%d 字）\n" % (
            len(ok), len(rows), len(ok) * 100 // max(1, len(rows)), len(thin), THIN))
        for r in rows:
            e = mat.get(r["subject"])
            mark = "✓" if (e and e.get("text")) else "✗"
            print("  %s %-14s %-18s 正文 %5d 字  %s" % (
                mark, r["subject"], (e.get("status") if e else "未抓取"),
                (e.get("body_len") if e else 0), (e.get("title") if e else "")))
        return 0

    def pending(r):
        s = r["subject"]
        if s in a.force or s not in mat:
            return True
        e = mat[s]
        # 已成功且不薄的不重抓。薄锚在 --retry-thin 时重试 —— 换了 MAXLEN 或
        # clean() 改进后它们可能变厚。判薄同样看 body_len（见 --stat 的注释）。
        return a.retry_thin and (not e.get("text") or e.get("body_len", 0) < THIN)

    todo = [r for r in rows if pending(r)]
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        print("没有需要抓取的条目（--force <subject> 强制重抓，--retry-thin 重试薄锚）")
        return 0
    print("待抓 %d 条（共 %d 条 subject）\n" % (len(todo), len(rows)))

    # wanted 传**全部**标题的繁简形态：Pages 取一个流时会把流内所有 wanted 一起收，
    # 名单越全，顺路收到的页越多，流数（唯一的成本项）越少。
    wanted = set()
    for r in rows:
        for t in (r["wiki"], r["subject"]):
            if t:
                wanted |= wikitext.hant_variants(t)
    pages = wikitext.Pages(wanted)

    stat = {"ok": 0, "thin": 0, "fail": 0}
    for i, r in enumerate(todo, 1):
        s = r["subject"]
        # 候选：wiki 列优先（它已过锚判定，一定在索引里且含学名），subject 兜底。
        # 兜底是为了 wiki 列万一为空 —— 不是不信它。
        txt, status, used = "", "no-candidate", ""
        for t in [r["wiki"]] + sorted(wikitext.hant_variants(r["subject"])):
            if not t:
                continue
            raw = pages.text(t)
            if raw is None:
                status = "not-in-index" if status == "no-candidate" else status
                continue
            m = wikitext.REDIR.match(raw)
            if m:
                # 跟随一跳。wiki 列不该是重定向（锚判定时已跟过），但 subject 兜底时会遇到。
                raw = pages.text(m.group(1).strip()) or ""
            if wikitext.DAB.search(raw):
                # 消歧义页。判据已修（wikitext.DAB），这里兜一道 —— 否则会静静地写
                # 一条 6 字的锚进去，而 6 字非空、能过所有"有没有锚"的检查。
                status = "dab"
                continue
            body = clean(raw)
            if not body:
                status = "empty-after-clean"
                continue
            facts = taxobox_facts(raw)
            head = fact_block(r, facts, _ready_of(s))
            txt = head + "\n————\n" + body[:MAXLEN]
            used = t
            status = "ok" if len(body) >= THIN else "thin"
            break

        mat[s] = {"text": txt, "status": status, "len": len(txt),
                  "body_len": len(txt.split("————\n")[-1]) if txt else 0,
                  "title": used}
        stat["ok" if status == "ok" else ("thin" if status == "thin" else "fail")] += 1
        # flush：输出重定向到文件时是块缓冲，跑 200 多条时进度全卡在缓冲区里，
        # 看上去像卡死。长任务必须逐行刷。
        print("[%d/%d] %-14s %-18s %5d 字  %s" % (
            i, len(todo), s, status, mat[s]["body_len"],
            "" if used == r["wiki"] else "<-" + used), flush=True)

    pages.save()
    os.makedirs(os.path.dirname(MPATH), exist_ok=True)
    json.dump(mat, open(MPATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)
    print("\n本轮 ok %d / thin %d / fail %d，已写入 data/material.json" % (
        stat["ok"], stat["thin"], stat["fail"]))
    print("取流 %d 次，缓存命中 %d 次" % (pages.miss, pages.hits))
    print("提醒：material.json 必须提交进 git —— 运行时只读它，不联网。")
    return 1 if stat["fail"] else 0


_READY = None


def _ready_of(subject):
    """从 ready.jsonl 取 GBIF 那一份已核实字段（iucn_raw / family / genus）。

    queue.tsv 只有 8 列，放不下这些；而它们是锚里最该有的硬事实 —— 保护级别是模型
    最容易说反的一项，科/属则是 63 条薄锚（正文 <150 字）唯一能加厚的可信信息。

    键名是 `iucn_raw`（build-queue.py 原样留着 GBIF 的枚举串），**不是 `iucn`**。
    第一版写成 `.get("iucn")`，于是 223 条锚全部静静少了 IUCN 那一行 —— 不报错、
    不为空、只是少一行，靠看输出才发现。取字段一定要拿真文件核一遍键名。
    """
    global _READY
    if _READY is None:
        _READY = {}
        p = lib.ready_path()
        if os.path.exists(p):
            for ln in open(p, encoding="utf-8"):
                ln = ln.strip()
                if ln:
                    d = json.loads(ln)
                    _READY[d["subject"]] = d
    return _READY.get(subject, {})


if __name__ == "__main__":
    sys.exit(main())
