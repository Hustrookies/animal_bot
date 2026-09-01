#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zhwiki 取文与事实锚判定。**这个文件是「zhwiki 这件事」的唯一实现。**

为什么单独一个文件而不塞进 lib.py：lib.py 管项目常量与队列，这里管一个外部数据源的
协议细节（multistream 偏移、bz2 流边界、重定向语义）。两件事内聚方向不同。
但判据常量 RANK_SUFFIX 仍从 lib 取 —— 那是约束 ③ 的一部分，不在这里复制一份。

取文机制移植自 wiki-bot 的 fetch-material.py（已在生产跑了一个月），改了三处：

1. **索引只装用得上的标题。** wiki-bot 把 494 万条全塞进 dict，那是 GB 级内存。
   这里只装 wanted 里的（几千条），流边界表 offs 仍然要全部 —— 但那只有 4.9 万个 int。
2. **取一个流时，把流内所有 wanted 标题一起提取并缓存。** 一个流含 100 页，
   实测单流取回 1.4–10.3s（平均 ~3.7s，77–187KB/s）。流数是唯一的成本项，
   所以顺路的页一定要收，不能同一个流下两次。
3. **正文落盘缓存。** 建池要反复调参重跑，没有缓存的话每次都要几十分钟网络往返。

重定向必须分两类，这是实测推翻 SPEC §6.1b 原判据得到的（probe/anchor-verdict.py）：

    薮猫 → 藪貓     繁简同名   跟随。subject 用简体，锚文从繁体标题取
    云豹 → 雲豹     繁简同名   同上
    仙鹤 → 丹顶鹤   真别名     拒。zhwiki 认为正名是另一个串
    小熊猫 → 小熊猫属 别名且属级 拒，且救不回来

原判据是「不是重定向页」。照那个写会把「简体名 → 繁体条目」这一整类成片误杀 ——
zhwiki 大量条目就存在繁体标题下。
"""
import bz2
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

# zhconv 用来做繁简归一化。优先本项目 vendor，回退 wiki-bot 那份 —— 与 import-gbif.py
# 同一套回退，别改成只认一处。
for _p in (os.path.join(lib.ROOT, "vendor"), "/opt/wiki/vendor"):
    if os.path.isdir(_p):
        sys.path.insert(0, os.path.abspath(_p))
        break
import zhconv

BASE = "https://dumps.wikimedia.org/zhwiki/latest/"
INDEX = "zhwiki-latest-pages-articles-multistream-index.txt.bz2"
DATA = "zhwiki-latest-pages-articles-multistream.xml.bz2"
CACHE = os.path.join(lib.ROOT, ".cache")
PAGES = os.path.join(CACHE, "pages.json")
UA = {"User-Agent": "animal-bot/1.0 (personal daily digest)"}

# 取流之间的间隔。dumps.wikimedia.org 是志愿资源，单线程顺序请求已经很温和，
# 这点延时只是别让重试风暴打上去。
POLITE = 0.2


# ---------------------------------------------------------------- HTTP
def get(url, rng=None, timeout=120):
    req = urllib.request.Request(url, headers=dict(UA))
    if rng:
        req.add_header("Range", "bytes=%d-%d" % (rng[0], rng[1]))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_resumable(url, dest, chunk=262144):
    """分块流式下载 + 断点续传。照抄 wiki-bot：整文件顺序下载会被限速到 ~35KB/s，
    41MB 索引要跑近 20 分钟，中断不续传就全丢。"""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    total = None
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers=dict(UA), method="HEAD"), timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0) or None
    except Exception:
        pass
    print("下载 %s（已有 %.1fMB）" % (os.path.basename(url), have / 1048576), flush=True)
    stall = 0
    while total is None or have < total:
        req = urllib.request.Request(url, headers=dict(UA))
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        got0 = have
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "ab") as f:
                while True:
                    b = r.read(chunk)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
        except Exception as e:
            print("  中断（%s），5 秒后续传…" % type(e).__name__, flush=True)
            time.sleep(5)
        if have == got0:
            stall += 1
            if stall >= 5:
                raise RuntimeError("连续 5 次无进展，已下 %d 字节，重跑可续传" % have)
        else:
            stall = 0
        if total is None:
            break
    os.replace(part, dest)


def ensure_index():
    """索引明文路径。不存在就下载 41MB bz2 并解压。

    实际部署里它是指向 wiki-bot .cache 的软链接（207MB，两个项目共用一份，
    不重复下载也不重复占盘）。"""
    txt = lib.index_path()
    if os.path.exists(txt):
        return txt
    os.makedirs(CACHE, exist_ok=True)
    raw = os.path.join(CACHE, INDEX)
    if not os.path.exists(raw):
        download_resumable(BASE + INDEX, raw)
    print("解压索引…", flush=True)
    with open(txt, "wb") as f:
        f.write(bz2.decompress(open(raw, "rb").read()))
    return txt


# ---------------------------------------------------------------- 索引
def hant_variants(title):
    """一个简体标题可能对应的繁体标题。

    繁简重定向的目标就是这些串，而它们**不在候选名单里** —— 不预先算出来放进 wanted，
    跟随重定向时就会 not_in_index，「薮猫」「云豹」这一整类全部误杀。
    实测 zh-hant / zh-tw / zh-hk 在动物名上结果一致，三个都留着只是不亏。
    """
    out = {title}
    for v in ("zh-hant", "zh-tw", "zh-hk"):
        try:
            out.add(zhconv.convert(title, v))
        except Exception:
            pass
    return out


def load_index(wanted):
    """扫一遍索引。返回 (idx, offs, by_stream)。

    idx      : {title: offset}，**只含 wanted 及其繁体变体**
    offs     : 全部流起始偏移，升序 —— 求流结束位置要用，必须完整
    by_stream: {offset: [title, ...]}，取一个流时顺路提取哪些标题
    """
    txt = ensure_index()
    want = set()
    for t in wanted:
        want |= hant_variants(t)
    idx, offs, by_stream = {}, set(), {}
    with open(txt, encoding="utf-8", errors="replace") as f:
        for ln in f:
            # 行格式 offset:pageid:title，title 本身可能含冒号（"Wikipedia:..."）
            p = ln.rstrip("\n").split(":", 2)
            if len(p) != 3:
                continue
            o = int(p[0])
            offs.add(o)
            if p[2] in want:
                idx[p[2]] = o
                by_stream.setdefault(o, []).append(p[2])
    print("索引：%d 个流，命中 %d/%d 个标题（含繁体变体）" % (
        len(offs), len(idx), len(want)), flush=True)
    return idx, sorted(offs), by_stream


# ---------------------------------------------------------------- 页面仓库
REDIR = re.compile(r"^\s*#(?:REDIRECT|重定向|重定向至|重定向到)\s*\[\[([^\]|#]+)", re.I)


def extract(xml, title):
    """从一个流的 XML 里抠出指定标题的 wikitext。"""
    m = re.search(r"<title>" + re.escape(title) + r"</title>(.*?)</page>", xml, re.S)
    if not m:
        return None
    t = re.search(r"<text[^>]*>(.*?)</text>", m.group(1), re.S)
    return t.group(1) if t else None


def zh_key(s):
    """繁简归一化。判断两个标题是不是「同一个名字的两种写法」用这个，不用字面相等 ——
    字面相等会把「薮猫 / 藪貓」判成两个不同的名字，于是当成别名重定向拒掉。"""
    return zhconv.convert(s or "", "zh-cn")


class Pages:
    """zhwiki 正文仓库。按流取回，落盘缓存，同一个流绝不下两次。

    成本模型：**流数是唯一的成本项**（单流 1.4–10.3s），一个流里有 100 页。
    所以 fetch 一个标题时，会把同一流里所有 wanted 标题一起提取存下 —— 顺路的页
    不收就等于同一个流要下多次。
    """

    def __init__(self, wanted):
        self.idx, self.offs, self.by_stream = load_index(wanted)
        self.cache = {}
        self.hits = self.miss = 0
        if os.path.exists(PAGES):
            try:
                self.cache = json.load(open(PAGES, encoding="utf-8"))
                print("正文缓存：%d 条" % len(self.cache), flush=True)
            except Exception:
                # 缓存损坏不该让建池失败 —— 它是加速件，不是数据源
                print("正文缓存读取失败，当空处理", flush=True)
        self._done_streams = set()

    def save(self):
        os.makedirs(CACHE, exist_ok=True)
        tmp = PAGES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)
        os.replace(tmp, PAGES)

    def _stream_end(self, start):
        import bisect
        i = bisect.bisect_right(self.offs, start)
        return self.offs[i] - 1 if i < len(self.offs) else start + 2_000_000

    def _load_stream(self, start):
        if start in self._done_streams:
            return
        self._done_streams.add(start)
        blob = get(BASE + DATA, rng=(start, self._stream_end(start)))
        try:
            xml = bz2.decompress(blob).decode("utf-8", "ignore")
        except Exception:
            d = bz2.BZ2Decompressor()          # 末尾流可能被 Range 截断
            try:
                xml = d.decompress(blob).decode("utf-8", "ignore")
            except Exception:
                return
        for t in self.by_stream.get(start, ()):
            raw = extract(xml, t)
            self.cache[t] = raw if raw is not None else ""
        time.sleep(POLITE)

    def text(self, title):
        """标题 → wikitext。不在索引里返回 None，条目为空返回 ""。"""
        if title in self.cache:
            self.hits += 1
            return self.cache[title] or None
        if title not in self.idx:
            return None
        self.miss += 1
        self._load_stream(self.idx[title])
        return self.cache.get(title) or None


# ---------------------------------------------------------------- 锚判定
def sci_in_text(raw, sci):
    """正文里有没有这个学名。返回 "full" / "abbr" / ""。

    要认缩写形式：zhwiki 正文常写「''Panthera tigris''」，但也有通篇写
    「''P. tigris''」的。只认全串会把后者判成 no_sci。
    """
    genus, _, epithet = sci.partition(" ")
    epithet = epithet.split(" ")[0]
    if not epithet:
        return ""
    if sci.lower() in raw.lower():
        return "full"
    if re.search(r"\b" + re.escape(genus[:1]) + r"\.\s*" + re.escape(epithet), raw, re.I):
        return "abbr"
    return ""


# 消歧义页。**这是判据的第六个漏洞，形态又是老一套：非空 ≠ 可用。**
#
# 「马鹿」在 zhwiki 是消歧义页：`'''马鹿'''可以指：` + 一串 `*[[加拿大馬鹿]]（''Cervus
# canadensis''）`。它过了 sci_in_text —— 因为列表项里确实写着学名。于是一个"不是动物
# 条目"的页面成了事实锚，撞约束 ③ 最直接的那一条（连"一类动物的宽泛介绍"都不是，
# 它是个目录）。实测 226 条里 2 条（马鹿、紫晶林星蜂鸟）。
#
# 靠模板名认，不靠「可以指」这类词面 —— 词面会误伤正常条目里的行文。
DAB = re.compile(r"\{\{\s*(?:Disambig|disambiguation|消歧[义義][^}]*|Dab|Hndis|Setindex"
                 r"|Geodis|Letter-NumberCombDisambig)\s*[|}]", re.I)

# 物种条目的分类信息框。**没有它基本就不是物种条目**（实测 226 条里只有 3 条没有，
# 其中 2 条正是上面那两个消歧义页）。所以它是消歧义检测的第二道确认，也是
# fetch-material.py 抽命名人/异名的地方。
TAXOBOX = re.compile(r"\{\{\s*(?:Speciesbox|Taxobox|Automatic ?Taxobox|Subspeciesbox"
                     r"|Infraspeciesbox|生物分类|生物分類|物種資訊)", re.I)


def anchor_verdict(pages, title, sci, depth=0):
    """单个候选名的事实锚判定。返回 (ok, why, anchor_title)。

    why 在 ok 时是命中方式（full/abbr 或 hans->…），拒时是拒因。
    anchor_title 是**锚文实际所在的 zhwiki 标题**，可能与 title 繁简不同 ——
    这就是 queue.tsv 要有第 8 列 wiki 的原因：读者看到的名字和取锚文的标题
    可以不是一个串（subject=薮猫，wiki=藪貓）。
    """
    raw = pages.text(title)
    if raw is None:
        return False, "not-in-index", ""

    m = REDIR.match(raw)
    if m:
        tgt = m.group(1).strip()
        if depth >= 2:
            return False, "redirect-deep", ""
        if zh_key(tgt) != zh_key(title):
            # 真别名：zhwiki 认为正名是另一个串。**不自动采用** —— 那个串没过统称
            # 检测和黑名单，采用它等于给约束 ③ 开一个后门。继续试下一个候选名。
            #
            # 目标是分类层级名的单独记（alias-rank）：那一类救不回来。实测「小熊猫」
            # 重定向到「小熊猫属」，而属级名在 refine 就被剔了，不可能出现在 zh_alt 里。
            # 「仙鹤 → 丹顶鹤」这种目标是具体物种名的，通常就在 zh_alt 里等着接住。
            kind = "alias-rank" if tgt.endswith(lib.RANK_SUFFIX) else "alias"
            return False, "%s->%s" % (kind, tgt), ""
        ok, why, at = anchor_verdict(pages, tgt, sci, depth + 1)
        return ok, "hans/" + why, at

    hit = sci_in_text(raw, sci)
    if not hit:
        return False, "no-sci", ""
    # 消歧义页会列出多个物种连带学名，所以它一定能过 sci_in_text —— 必须单独拦。
    # 顺序在 sci_in_text 之后：拒因里更该看到「dab」而不是「no-sci」，前者说明
    # 条目存在但类型不对，是能靠下一个候选名救回来的那种。
    if DAB.search(raw):
        return False, "dab", ""
    return True, hit, title


def final_name(pages, row):
    """按 zh → zh_alt 顺序取第一个过锚的名字。返回 (subject, wiki_title, why, trace)。

    全败时 subject 为 ""，why 取**最有信息量**的那条拒因而不是第一条 —— not-in-index
    只说明标题没查到，no-sci / alias 才说明内容不对。照 wiki-bot 的同一条教训：
    报第一条会把真实问题藏起来。
    """
    order = [row.get("zh") or ""] + list(row.get("zh_alt") or [])
    seen, trace, best = set(), [], ""
    sci = row.get("sci") or ""
    for t in order:
        t = (t or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        ok, why, at = anchor_verdict(pages, t, sci)
        trace.append("%s[%s]" % (t, why))
        if ok:
            return t, at, why, trace
        if not best or (best.startswith("not-in-index") and not why.startswith("not-in-index")):
            best = why
    return "", "", best or "no-candidate", trace
