#!/usr/bin/env python3
"""阶段 1b：给每个候选物种定一个中文名，产出 data/pool.jsonl。

candidates.jsonl 里每条带 zh_all（该物种全部可用中文名），这里做四件事：

  0. **单行闸门**（lib.taxon_verdict）：学名格式、rank、家养种。判据不在本文件里 ——
     和 taxon-check.py 共用 lib.py 那一份，两处各写一份迟早会分叉。
  1. **统称检测**（约束 ③ 的主力闸门）。一个中文名被多个 speciesKey 共用 → 它是统称，
     从所有物种的候选里剔除。这个判断只能在全局做：单看 Carcharhinus sorrah 一条记录，
     「沙条」看不出有任何问题；只有看到另外 17 个鲨鱼种也叫「沙条」才知道它是渔业统称。
  2. **黑名单**（§5.3）。全串精确匹配，不做子串 —— 子串匹配下「老虎」会连带干掉
     「东北虎」「孟加拉虎」。黑名单里也不收单字词。
  3. **zhwiki 存在性闸门**。定下来的名字必须在本地索引里有同名条目，否则
     fetch-material.py 取不到事实锚，agent 就只能凭记忆编。

过完这些闸门后取最短名，但那**只是初选**：剩下的名字里还有方言名和文化名，长度判不出来
（汤匙 / 仙鹤 / 小龙虾），见下面定名处的注释。终选在 build-queue 阶段靠事实锚做。

用法：
    python3 refine-candidates.py            # 产出 pool.jsonl + rejected.tsv
    python3 refine-candidates.py --stat     # 只看统计，不写文件
"""
import argparse, collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import (ROOT, candidates_path, load_wordlist, taxon_verdict,
                 zh_index_titles, zh_verdict)

MIN_GENERIC = 2     # 被 ≥2 个物种共用即判为统称


def data(name):
    return os.path.join(ROOT, "data", name)



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

    # ---- 0. 单行闸门：学名格式 / rank / 家养种 ----
    # 放在统称检测之前是有意的：家猫、家牛这些家养种的中文名是**独占**的（只有它们
    # 叫「家猫」），统称检测永远抓不到。而且要先把它们剔掉，它们贡献的名字才不会
    # 干扰后面的共用计数。
    dropped = []
    kept = []
    for r in rows:
        ok, why = taxon_verdict(r)
        (kept if ok else dropped).append(r if ok else (r, why, "|".join(r.get("zh_all") or [])))
    if dropped:
        print("单行闸门拒 %d 条：%s" % (
            len(dropped), dict(collections.Counter(w for _, w, _ in dropped))))
        for r, why, names in dropped:
            if why == "domestic":
                print("   domestic  %-24s %s" % (r["sci"], names))
    rows = kept

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
        keep = [z for z in r["zh_all"] if zh_verdict(z, generic, black)[0]]
        if keep:
            survivors[r["key"]] = keep
    wanted = {z for v in survivors.values() for z in v}
    print("过完前两道闸门：%d 个物种 / %d 个待查名字" % (len(survivors), len(wanted)))
    hit = zh_index_titles(wanted)
    print("zhwiki 有条目的 %d 个（%.0f%%）" % (hit and len(hit) or 0,
                                          100.0 * len(hit) / max(len(wanted), 1)))

    # ---- 定名 ----
    pool, rejected = [], list(dropped)

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
