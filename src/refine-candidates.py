#!/usr/bin/env python3
"""阶段 1b：给每个候选物种定一个中文名，产出 data/pool.jsonl。

candidates.jsonl 里每条带 zh_all（该物种全部可用中文名），这里做三件事：

  1. **统称检测**（约束 ③ 的主力闸门）。一个中文名被多个 speciesKey 共用 → 它是统称，
     从所有物种的候选里剔除。这个判断只能在全局做：单看 Carcharhinus sorrah 一条记录，
     「沙条」看不出有任何问题；只有看到另外 17 个鲨鱼种也叫「沙条」才知道它是渔业统称。
  2. **黑名单**（§5.3）。全串精确匹配，不做子串 —— 子串匹配下「老虎」会连带干掉
     「东北虎」「孟加拉虎」。黑名单里也不收单字词。
  3. **zhwiki 存在性闸门**。定下来的名字必须在本地索引里有同名条目，否则
     fetch-material.py 取不到事实锚，agent 就只能凭记忆编。

定名顺序是「先过闸门，再取最短」：剔掉统称和黑名单词、只留 zhwiki 里有条目的，
剩下的取最短。此时最短是安全的 —— 统称已经被前两道剔掉了，剩下的短名就是正名
（「美洲狮」而不是「北美金猫」）。

用法：
    python3 refine-candidates.py            # 产出 pool.jsonl + rejected.tsv
    python3 refine-candidates.py --stat     # 只看统计，不写文件
"""
import argparse, collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import ROOT, candidates_path, zh_index_titles

MIN_GENERIC = 2     # 被 ≥2 个物种共用即判为统称

# 以这些字结尾的名字是**分类层级名**，不是物种名。GBIF 的 zho 俗名里混着它们：
# Odobenus rosmarus 的候选里就有「海象属」。属级条目直接违反约束 ③，而且统称检测
# 抓不到（只有一个物种挂着这个名字）。
RANK_SUFFIX = ("属", "科", "目", "纲", "门", "族", "亚种", "类")


def data(name):
    return os.path.join(ROOT, "data", name)


def load_wordlist(name):
    """一行一词，# 开头是注释。文件不存在返回空集。"""
    p = data(name)
    if not os.path.exists(p):
        return set()
    out = set()
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.split("#", 1)[0].strip()
            if ln:
                out.add(ln)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stat", action="store_true")
    a = ap.parse_args()

    rows = []
    with open(candidates_path(), encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                rows.append(json.loads(ln))
    print("候选 %d 条" % len(rows))

    # ---- 1. 统称检测 ----
    holders = collections.defaultdict(set)
    for r in rows:
        for z in r["zh_all"]:
            holders[z].add(r["key"])
    generic = {z for z, ks in holders.items() if len(ks) >= MIN_GENERIC}
    print("统称 %d 个（被 ≥%d 个物种共用）" % (len(generic), MIN_GENERIC))
    worst = sorted(generic, key=lambda z: -len(holders[z]))[:10]
    for z in worst:
        print("   %-8s 被 %d 个物种共用" % (z, len(holders[z])))

    # ---- 2. 黑名单 ----
    black = load_wordlist("blacklist.txt") - load_wordlist("whitelist.txt")
    print("黑名单 %d 词" % len(black))

    # ---- 3. zhwiki 闸门。一次扫完 216MB 索引，不要每个名字扫一遍 ----
    survivors = {}
    for r in rows:
        keep = [z for z in r["zh_all"]
                if z not in generic and z not in black
                and not z.endswith(RANK_SUFFIX)]
        if keep:
            survivors[r["key"]] = keep
    wanted = {z for v in survivors.values() for z in v}
    print("过完前两道闸门：%d 个物种 / %d 个待查名字" % (len(survivors), len(wanted)))
    hit = zh_index_titles(wanted)
    print("zhwiki 有条目的 %d 个（%.0f%%）" % (hit and len(hit) or 0,
                                          100.0 * len(hit) / max(len(wanted), 1)))

    # ---- 定名 ----
    pool, rejected = [], []
    for r in rows:
        keep = survivors.get(r["key"], [])
        if not keep:
            why = "all-generic" if r["zh_all"] else "no-zh"
            rejected.append((r, why, "|".join(r["zh_all"])))
            continue
        ok = [z for z in keep if z in hit]
        if not ok:
            rejected.append((r, "no-wiki", "|".join(keep)))
            continue
        r = dict(r)
        # 这里定的 zh 是**初选，不是终选**。取最短只是一个够用的起点，它挡不住方言名
        # 和文化名 —— 实测踩到的三个：
        #     汤匙   ← Rhinobatos hynnicephalus，正确的是「斑纹犁头鳐」
        #     仙鹤   ← Grus japonensis，正确的是「丹顶鹤」
        #     小龙虾 ← Procambarus clarkii，正确的是「克氏原螯虾」
        # 三个正确答案都在 zh_alt 里。统称检测抓不到它们，因为只有一个物种叫「汤匙」。
        #
        # 试过改成取最长，反而更糟：会把「海象」选成「海象属」、「大白鲨」选成「食人鲨」、
        # 「小熊猫」选成「红熊猫」。**长度不是判据**，两个方向都在猜。
        # 「猫熊 / 大熊猫」这类"哪个是大陆通用名"更是没有任何本地信号可判。
        #
        # 终选靠事实锚：build-queue.py 按 zh → zh_alt 的顺序逐个取 zhwiki 正文，
        # 要求正文里出现该物种的学名、且不是重定向页，第一个过关的才落进 queue.tsv。
        # 所以 zh_alt 不是"备注"，是**有序的后备候选**，不要在下游丢掉它。
        r["zh"] = min(ok, key=lambda s: (len(s), s))
        r["zh_alt"] = sorted((z for z in ok if z != r["zh"]),
                             key=lambda s: (len(s), s))
        # threatStatuses 不能拿来展示，改名落盘以免下游误用。它是 GBIF 把**全球评估和
        # 各区域评估混在一个数组里**的结果：Phocoena phocoena 有 7 个值，第一个是
        # CRITICALLY_ENDANGERED（波罗的海种群），而它的全球等级是 LEAST_CONCERN。
        # 照着数组标等级就是在页面上写事实错误。
        #
        # 准确的全球等级要走 /v1/species/{key}/iucnRedListCategory（单值 category，
        # 且回一个 scientificName 可以跟 key 对校）。那是 1 物种 1 请求，所以只在
        # build-queue 阶段对真正入队的条目查，不在这里对全池 2700 条查。
        r["iucn_raw"] = r.pop("iucn", "")
        r.pop("iucn_all", None)
        pool.append(r)

    print("\n入池 %d / 拒 %d" % (len(pool), len(rejected)))
    by_group = collections.Counter(r["group"] for r in pool)
    for g, n in by_group.most_common():
        print("   %-10s %d" % (g, n))
    why = collections.Counter(w for _, w, _ in rejected)
    print("拒因：%s" % dict(why))

    if a.stat:
        return

    with open(data("pool.jsonl"), "w", encoding="utf-8") as f:
        for r in pool:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 被拒的全部留档。不是待审清单 —— 是给「某个物种为什么没进池」留可查的证据。
    with open(data("rejected.tsv"), "w", encoding="utf-8") as f:
        f.write("group\tsci\tkey\treason\tzh_candidates\n")
        for r, w, names in rejected:
            f.write("%s\t%s\t%s\t%s\t%s\n" % (r["group"], r["sci"], r["key"], w, names))
    with open(data("generic-names.txt"), "w", encoding="utf-8") as f:
        f.write("# 自动识别的统称：被 ≥%d 个物种共用的中文名。\n" % MIN_GENERIC)
        f.write("# 由 refine-candidates.py 生成，不要手改 —— 手改的会在下次重跑时丢失。\n")
        for z in sorted(generic, key=lambda z: (-len(holders[z]), z)):
            f.write("%s\t%d\n" % (z, len(holders[z])))
    print("\n已写 data/pool.jsonl、rejected.tsv、generic-names.txt")


if __name__ == "__main__":
    main()
