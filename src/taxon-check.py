#!/usr/bin/env python3
"""阶段 2：分类闸门（SPEC §5.2 / §5.3）的验收入口。

判据本身不在这里 —— 在 lib.py 的「闸门判据」一节，这里只负责跑它、报它。
这样 refine-candidates.py 和本脚本用的是同一份实现，不会分叉。

用法：
    python3 taxon-check.py --selftest      # 跑用例表，退出码非 0 表示有用例挂了
    python3 taxon-check.py                 # 逐行验收 data/candidates.jsonl
    python3 taxon-check.py --pool          # 验收 data/pool.jsonl（定名后的池）
    python3 taxon-check.py --pool -v       # 连每条被拒的行一起打

逐行验收的语义照 wiki-bot 的 refill-check.py：**任一条不过就丢弃该行，不中止整批**。
一批里有几条脏数据是常态，中止整批只会让人去改数据迁就校验。
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import (ROOT, candidates_path, load_wordlist, taxon_verdict,
                 zh_verdict)

# ───────────────────────── 用例表 ─────────────────────────
# SPEC §12 阶段 2 要求 8 条：必拒 猫科/老虎/狮子/家犬，必过 孟加拉虎/美洲狮/薮猫/棕熊。
#
# 后四条是**回归用例，不是选题**。用户已定只做物种级，所以「孟加拉虎」这个亚种压根不会
# 出现在池子里。它留在这里的作用是钉死一件事：把它拒掉的**只能是枚举范围，不能是闸门**。
# 哪天有人把黑名单改成子串匹配，「老虎」就会连带干掉「孟加拉虎」「东北虎」，
# 而那种失败在池子里看不出来 —— 只会表现为「虎类条目莫名其妙一个都没有」。
#
# 后面还有一组是我自己踩出来的：种加词子串匹配的四个误杀。它们必过。
CASES = [
    # (说明, row, 期望通过?, 期望拒因)
    ("猫科（科级，rank 不对）",
     dict(sci="Felidae", rank="FAMILY", zh="猫科"), False, "bad-sci"),
    ("豹属（属级中文名）",
     dict(sci="Panthera", rank="GENUS", zh="豹属"), False, "bad-sci"),
    ("老虎（黑名单统称）",
     dict(sci="Panthera tigris", rank="SPECIES", zh="老虎"), False, "blacklist"),
    ("狮子（黑名单统称）",
     dict(sci="Panthera leo", rank="SPECIES", zh="狮子"), False, "blacklist"),
    ("家犬（家养种）",
     dict(sci="Canis lupus familiaris", rank="SUBSPECIES", zh="家犬"), False, "domestic"),
    ("家猫（家养种，实测混进过池子）",
     dict(sci="Felis catus", rank="SPECIES", zh="家猫"), False, "domestic"),
    # ↓ 阶段 5 逐条看锚时抓出来的：它把四道闸门每一道都合法走完了，然后以
    #   「靠一身汗腺把猎物活活跑垮的猿」进了 queue.tsv。判据集少了一条，不是判据写错。
    ("智人（人属，实测混进过 queue.tsv）",
     dict(sci="Homo sapiens", rank="SPECIES", zh="智人"), False, "excluded-genus"),
    # ↓ 而人属之外的类人猿必须放行 —— 证明排除按属，没有连坐到整个人科
    ("黑猩猩（人科但非人属，闸门必须放行）",
     dict(sci="Pan troglodytes", rank="SPECIES", zh="黑猩猩"), True, ""),

    ("孟加拉虎（亚种，闸门必须放行）",
     dict(sci="Panthera tigris tigris", rank="SUBSPECIES", zh="孟加拉虎"), True, ""),
    ("美洲狮（用户点名的正例）",
     dict(sci="Puma concolor", rank="SPECIES", zh="美洲狮"), True, ""),
    ("薮猫（2 字具体物种，防字数判据）",
     dict(sci="Leptailurus serval", rank="SPECIES", zh="薮猫"), True, ""),
    ("棕熊（2 字，亚种最多的物种，防数值判据）",
     dict(sci="Ursus arctos", rank="SPECIES", zh="棕熊"), True, ""),

    # ↓ 种加词子串匹配的误杀。SPEC 原来那条判据会把这四个全砍掉。
    ("沙虎鲨（种加词真是 taurus）",
     dict(sci="Carcharias taurus", rank="SPECIES", zh="沙虎鲨"), True, ""),
    ("棕尾刀翅蜂鸟（falcatus 含 catus）",
     dict(sci="Campylopterus falcatus", rank="SPECIES", zh="棕尾刀翅蜂鸟"), True, ""),
    ("松鸡（urogallus 含 gallus）",
     dict(sci="Tetrao urogallus", rank="SPECIES", zh="松鸡"), True, ""),
    ("野猪（家猪是 Sus domesticus）",
     dict(sci="Sus scrofa", rank="SPECIES", zh="野猪"), True, ""),
    ("白钩蛱蝶（种加词含合法连字符）",
     dict(sci="Polygonia c-album", rank="SPECIES", zh="白钩蛱蝶"), True, ""),


    # ↓ 其余踩过的坑
    ("海象属（分类层级名，统称检测抓不到）",
     dict(sci="Odobenus rosmarus", rank="SPECIES", zh="海象属"), False, "rank-name"),
    ("拼音俗名（GBIF 给 Panthera tigris 的只有拼音）",
     dict(sci="Panthera tigris", rank="SPECIES", zh="Lǎohǔ"), False, "bad-zh"),
    ("沙条（18 个鲨鱼种共用的渔业统称）",
     dict(sci="Carcharhinus sorrah", rank="SPECIES", zh="沙条"), False, "generic"),
]

# 自测用的统称集。真集合由 refine-candidates.py 从全池算出来，这里只给用例够用的一条。
SELFTEST_GENERIC = {"沙条"}


def check(row, generic, black):
    """完整判定一行：先分类闸门，再中文名闸门。返回 (ok, reason)。"""
    ok, why = taxon_verdict(row)
    if not ok:
        return ok, why
    return zh_verdict(row.get("zh") or row.get("subject"), generic, black)


def whitelist_wiring(black_raw):
    """whitelist 必须真的能豁免 blacklist 里的词。

    whitelist.txt 当前是空的，这条链路平时**没有任何用例经过它**。等到某天有人往
    whitelist 加词、发现不生效，才回来查 load_wordlist 的路径拼错了，就太晚了。
    所以这里拿黑名单里的一个真词现场造一次豁免。
    """
    if not black_raw:
        return False, "blacklist.txt 一个词都没读到"
    w = sorted(black_raw)[0]
    blocked, _ = zh_verdict(w, (), black_raw)
    freed, _ = zh_verdict(w, (), black_raw - {w})
    if blocked:
        return False, "「%s」在黑名单里却没被拦" % w
    if not freed:
        return False, "「%s」被豁免后仍然被拦" % w
    return True, w


def selftest(black, black_raw):
    print("用例 %d 条（黑名单 %d 词，统称 %d 词）\n" % (
        len(CASES), len(black), len(SELFTEST_GENERIC)))
    bad = 0
    for desc, row, want_ok, want_why in CASES:
        ok, why = check(row, SELFTEST_GENERIC, black)
        # 拒因也要对。只看通过/拒绝的话，「老虎」被 bad-sci 拒掉也算过 —— 那是巧合，
        # 不是黑名单在工作，下次改动就会静默失效。
        good = (ok == want_ok) and (want_ok or why == want_why)
        if not good:
            bad += 1
        print("  %s %-34s %s" % (
            "OK  " if good else "FAIL",
            desc,
            "通过" if ok else "拒(%s)" % why) +
            ("" if good else "   ← 期望 %s" % ("通过" if want_ok else "拒(%s)" % want_why)))

    ok, info = whitelist_wiring(black_raw)
    if not ok:
        bad += 1
    print("  %s %-34s %s" % ("OK  " if ok else "FAIL", "whitelist 豁免链路",
                             "以「%s」验证" % info if ok else info))

    n = len(CASES) + 1
    print("\n%d/%d 通过" % (n - bad, n))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--pool", action="store_true", help="验收 pool.jsonl 而非 candidates.jsonl")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    black_raw = load_wordlist("blacklist.txt")
    black = black_raw - load_wordlist("whitelist.txt")
    if a.selftest:
        return selftest(black, black_raw)


    path = os.path.join(ROOT, "data", "pool.jsonl") if a.pool else candidates_path()
    if not os.path.exists(path):
        print("没有 %s，先跑 import-gbif.py" % path)
        return 2

    # candidates.jsonl 还没定名，只有 zh_all；pool.jsonl 已定名，有 zh。
    # 前者按「至少有一个候选名能过」判，后者按「定下来那个名字能过」判。
    import collections
    total, rejected = 0, []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            total += 1
            r = json.loads(ln)
            ok, why = taxon_verdict(r)
            if ok:
                names = [r["zh"]] if r.get("zh") else (r.get("zh_all") or [])
                verdicts = [zh_verdict(z, (), black) for z in names] or [(False, "no-zh")]
                if any(v[0] for v in verdicts):
                    continue
                why = verdicts[0][1]
            rejected.append((r, why))

    print("%s：%d 行，拒 %d" % (os.path.basename(path), total, len(rejected)))
    for why, n in collections.Counter(w for _, w in rejected).most_common():
        print("   %-10s %d" % (why, n))
    if a.verbose:
        for r, why in rejected:
            print("   %-10s %-30s %s" % (why, r.get("sci"),
                                         r.get("zh") or "|".join(r.get("zh_all") or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
