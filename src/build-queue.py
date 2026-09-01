#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pool.jsonl → ready.jsonl：事实锚终选 + 生物地理界。

这是 §6.1b 说的「最后一道闸门」。refine 定的 zh 只是**初选**：剔掉统称、黑名单词、
分类层级名之后取最短，仍然挡不住方言名、文化名、器物名 ——

    汤匙   Rhinobatos hynnicephalus   正确的是 斑纹犁头鳐
    仙鹤   Grus japonensis             正确的是 丹顶鹤
    小龙虾 Procambarus clarkii         正确的是 克氏原螯虾
    猫熊   Ailuropoda melanoleuca      正确的是 大熊猫

判据是「zhwiki 正文里出现这个物种的学名」，实现在 wikitext.anchor_verdict。
四条全部判对，实测见 probe/anchor-verdict.py（11/11）。**取最长也不行**，会引入
新错误（海象→海象属、大白鲨→食人鲨），所以长度两个方向都是在猜。

── 为什么不直接写 queue.tsv ──
queue.tsv 的 title / note 要 agent 补（§6.2），而 agent 只写增量文件。所以这里产出的是
「待入队清单」：subject 已定死并过完四道闸门，agent 不许改。refill-check.py 会验这一点。

── 挑哪些条目 ──
每类群池里有 81–248 条，只需 32 条入队。按学名排序会让 carnivora 全落在 A 开头的属上，
所以走**属内轮转**：先每属取一条，再第二轮。40 条就覆盖 40 个属，正好呼应
lib.GENUS_GAP=30（同属最小间隔）。顺序是确定的 —— 否则每次重跑挑出不同的条目，
ready.jsonl 就不可复现。

── 成本 ──
取文按流计费（单流 1.4–10.3s，100 页/流），所以不做全池：每类群做到够数就停。
正文落盘缓存在 .cache/pages.json，重跑几乎免费。中断可续 —— ready.jsonl 是追加写的。
"""
import argparse
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib
import wikitext

API = "https://api.gbif.org/v1"
UA = {"User-Agent": "animal-bot/1.0 (personal daily digest)"}


def ready_path():
    return lib.ready_path()


def anchor_rejected_path():
    return os.path.join(lib.ROOT, "data", "anchor-rejected.tsv")


def gbif(path, tries=3):
    """GET api.gbif.org。失败重试，全败抛异常 —— 静默返回空会让 region 全空而不报错。"""
    for i in range(tries):
        try:
            req = urllib.request.Request(API + path, headers=dict(UA))
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def region_of(key):
    """物种 key → 生物地理界。拿不到返回 ""。

    distributions 端点只给洲际粒度（实测 Panthera tigris tigris 返回
    locality="Southern Asia", country=None）。对映射到 6 个界够用，但**别指望它给
    分布国列表** —— profile.range_text 要的"印度、孟加拉国、尼泊尔"得 agent 从事实锚提。
    """
    d = gbif("/species/%d/distributions?limit=100" % key)
    locs = []
    for it in d.get("results") or ():
        for f in ("locality", "country", "region", "locationId"):
            v = it.get(f)
            if v:
                locs.append(str(v))
    return lib.realm_of(locs)


def genus_round_robin(rows):
    """属内轮转排序：先每属一条，再第二轮。确定性 —— 属与属内都按学名排。

    直接按学名排序的话，取前 40 条会集中在少数几个属上（carnivora 全是 A 开头）。
    池子里挑 40/246，多样性是免费拿的，没理由不拿。
    """
    by_genus = collections.defaultdict(list)
    for r in rows:
        by_genus[r.get("genus") or ""].append(r)
    for g in by_genus:
        by_genus[g].sort(key=lambda r: r.get("sci") or "")
    out, genera = [], sorted(by_genus)
    i = 0
    while True:
        added = False
        for g in genera:
            if i < len(by_genus[g]):
                out.append(by_genus[g][i])
                added = True
        if not added:
            break
        i += 1
    return out


def load_done():
    """已处理过的 key → 已入 ready 的行。用于增量重跑。"""
    done = {}
    p = ready_path()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                done[d["key"]] = d
    return done


def emit_batch(slug, n):
    """打印一批待补角度的物种，供 refill-prompt.md 末尾拼接。

    放在这个文件里而不是单开一个脚本：ready.jsonl 的读法只该有一处。
    「哪些还没入队」这个判断如果散在两个文件里，迟早一处改了另一处没改，
    表现就是 agent 反复收到已经入队的物种、每批全废。

    输出是给模型看的，所以给足能写出钩子的上下文（科、IUCN、地理界），
    但**不给学名以外的任何可改字段** —— agent 只需照抄 subject。
    """
    done = load_done()
    already = {q["subject"] for q in lib.load_queue()}
    todo = [d for d in done.values()
            if d["group"] == slug and d["subject"] not in already]
    todo.sort(key=lambda d: d["sci"])
    todo = todo[:n]
    if not todo:
        print("ask=0")
        return 0
    print("类群 = %s（%s）" % (slug, dict(lib.GROUPS.values()).get(slug, slug)))
    print("要写 %d 行。以下每个物种写一行。\n" % len(todo))
    print("| subject（照抄） | 学名 | 科 | IUCN | 生物地理界 |")
    print("|---|---|---|---|---|")
    for d in todo:
        print("| %s | *%s* | %s | %s | %s |" % (
            d["subject"], d["sci"], d["family"] or "—",
            d["iucn_raw"] or "—", d["region"] or "—"))
    return len(todo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=32,
                    help="每类群目标条数（SPEC §3：32×7=224）")
    ap.add_argument("--margin", type=float, default=1.25,
                    help="多做几条余量，吸收后续闸门丢弃")
    ap.add_argument("--group", help="只做一个类群（联调用）")
    ap.add_argument("--budget", type=int, default=3600, help="墙钟预算（秒）")
    ap.add_argument("--no-net", action="store_true",
                    help="只用已有缓存，不发任何网络请求（验判据用）")
    ap.add_argument("--emit", type=int, metavar="N",
                    help="不建池，只打印一批待补角度的物种（refill 循环用），需配 --group")
    args = ap.parse_args()

    if args.emit:
        if not args.group:
            print("--emit 需要 --group", file=sys.stderr)
            return 2
        return 0 if emit_batch(args.group, args.emit) else 3

    rows = [json.loads(l) for l in open(lib.pool_path(), encoding="utf-8") if l.strip()]
    done = load_done()
    print("池内 %d 条，ready 已有 %d 条" % (len(rows), len(done)), flush=True)

    # wanted 传**全池**候选名，不只这次要做的那些：load_index 扫索引是固定 4s 开销，
    # 而 by_stream 越全，取一个流时顺路能收的页越多 —— 顺路的页是免费的。
    wanted = []
    for r in rows:
        wanted.append(r.get("zh") or "")
        wanted += list(r.get("zh_alt") or [])
    pages = wikitext.Pages([w for w in wanted if w])

    # 类群顺序按 GROUPS 的 ISO 星期序，确定性 —— 换成 set 或 dict 遍历会让预算用尽时
    # 每次停在不同的类群上，ready.jsonl 就不可复现了。
    order = [lib.GROUPS[i][0] for i in sorted(lib.GROUPS)]
    groups = [args.group] if args.group else order
    need = max(1, int(args.target * args.margin))
    t0 = time.time()
    rej = open(anchor_rejected_path(), "a", encoding="utf-8")
    out = open(ready_path(), "a", encoding="utf-8")
    stat = collections.Counter()
    added_total = 0

    for slug in groups:
        pool = genus_round_robin([r for r in rows if r["group"] == slug])
        have = sum(1 for d in done.values() if d["group"] == slug)
        n = 0
        for r in pool:
            if have + n >= need:
                break
            if r["key"] in done:
                continue
            if time.time() - t0 > args.budget:
                print("预算 %ds 用尽，收尾" % args.budget, flush=True)
                break
            if args.no_net and (r.get("zh") not in pages.cache):
                continue

            subject, wiki, why, trace = wikitext.final_name(pages, r)
            if not subject:
                stat[why.split("->")[0]] += 1
                rej.write("%s\t%s\t%s\t%s\n" % (
                    slug, r["sci"], why, " ".join(trace)))
                continue

            region = ""
            if not args.no_net:
                try:
                    region = region_of(r["key"])
                except Exception as e:
                    # region 拿不到不该丢条目：pick.py 只把它当排序偏好（"地域不匹配"），
                    # 空值退化成「任何周都不优先」，不是硬淘汰。
                    print("  region 查询失败 %s：%s" % (r["sci"], type(e).__name__), flush=True)
            rec = {"group": slug, "region": region, "subject": subject,
                   "sci": r["sci"], "wiki": wiki, "key": r["key"],
                   "family": r.get("family") or "", "genus": r.get("genus") or "",
                   "iucn_raw": r.get("iucn_raw") or "", "anchor": why}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            done[r["key"]] = rec
            n += 1
            stat["ok"] += 1
            if n % 10 == 0:
                pages.save()
                print("  %s %d/%d  取流 %d" % (slug, have + n, need, pages.miss), flush=True)
        added_total += n
        print("%-10s +%d → %d 条（目标 %d）" % (slug, n, have + n, need), flush=True)
        pages.save()

    rej.close()
    out.close()
    pages.save()
    print()
    print("新增 %d 条，ready 共 %d 条" % (added_total, len(done)), flush=True)
    print("拒因：%s" % dict(stat), flush=True)
    print("取流 %d 次，缓存命中 %d 次，耗时 %.0fs" % (
        pages.miss, pages.hits, time.time() - t0), flush=True)
    by_g = collections.Counter(d["group"] for d in done.values())
    by_r = collections.Counter(d["region"] or "(未定)" for d in done.values())
    print("类群：%s" % dict(by_g), flush=True)
    print("生物地理界：%s" % dict(by_r), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
