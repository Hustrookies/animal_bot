#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""730 天排期模拟 —— 约束 ① 的交付证据（SPEC §9）。

    ① 出场覆盖率必须 = 池内条数（死库存 0）
    ② 同一 subject 的最小复现间隔必须 > 183 天    ← 约束 ① 的直接证据
    ③ 同一 scientific_name 最小复现间隔必须 > 183 天
    ④ 无候选天数必须 = 0

**只调 pick.pick_one()，不复刻任何判据。** SPEC §9 写明"模拟器必须 import 真实
逻辑，不许复刻——复刻的话测的是复刻件"。这不是洁癖：wiki-bot 上正是这套模拟发现了
FIFO 排序导致 30/157 条整年不出场，而如果模拟器自己写一份排序，它测的是自己。

判据不只有排序键。窗口过滤、同属间隔、相似度跳过同样决定覆盖率，所以它们全都在
pick_one() 里 —— 这个文件不许出现 lib.WINDOW / lib.GENUS_GAP / lib.HARD 的判断。

用法：
    simulate.py                    从明天起跑 730 天
    simulate.py --days 365         跑 365 天
    simulate.py --start 2026-09-01
    simulate.py --seed-posts       读现有 posts.jsonl 当初始历史（默认空历史）
    simulate.py --cap 28           每类群只留前 28 条（水位敏感性）
    simulate.py --scan             扫每类群水位 22→上限，找最小安全条数
    simulate.py --starve amphibia:26   只让一个类群缩水（验 QUEUE_LOW 的口径）
"""
import argparse
import collections
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import lib      # noqa: E402
import pick     # noqa: E402


def simulate(queue, days, start, posts0):
    """跑 days 天，返回 (posts, gaps, fails)。

    posts 是模拟出的推送历史，字段与 posts.jsonl 同构 —— pick_one 读的就是它，
    所以字段名必须对得上，不能只放 subject。
    """
    posts = list(posts0)
    fails = []
    for i in range(days):
        d = start + dt.timedelta(days=i)
        chosen, near, skipped, reason = pick.pick_one(queue, posts, d)
        if chosen is None:
            fails.append({"date": d.isoformat(), "reason": reason})
            continue
        posts.append({
            "date": d.isoformat(),
            "group": chosen["group"],
            "subject": chosen["subject"],
            "scientific_name": chosen["scientific_name"],
            "title": chosen["title"],
            # summary 与 entities 参与相似度判定（lib.sim），不能省 —— 省了就等于
            # 把判据 ④ 关掉，模拟会比生产宽松。
            "summary": chosen["note"],
            "entities": chosen["entities"],
        })
    return posts, fails


def min_gap(posts, key):
    """同一 key 值两次出场的最小间隔（天）。只出场一次的不参与。返回 (最小间隔, 那一对)。"""
    seen = collections.defaultdict(list)
    for p in posts:
        v = p.get(key)
        if v:
            seen[v].append(p["date"])
    worst, who = None, None
    for v, ds in seen.items():
        ds.sort()
        for a, b in zip(ds, ds[1:]):
            g = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
            if worst is None or g < worst:
                worst, who = g, (v, a, b)
    return worst, who


def cap_queue(queue, n):
    """每类群只留前 n 条。用于水位敏感性 —— 池子缩水到哪一档开始违约。

    取"前 n 条"而不是随机抽：queue.tsv 的行序就是 build-queue.py 的属内轮转序，
    截前 n 条正是"少补了几批"的真实形态。随机抽会把属打散，测出来的数偏乐观。
    """
    got = collections.Counter()
    out = []
    for q in queue:
        if got[q["group"]] < n:
            out.append(q)
            got[q["group"]] += 1
    return out


def verdict(queue, days, start, posts0):
    """跑一次模拟，返回 (ok, 覆盖率, 最小 subject 间隔, 最小学名间隔, 无候选天数)。

    scan 模式复用它 —— 判据只有 main() 一份表述，这里只回报数字。
    """
    posts, fails = simulate(queue, days, start, posts0)
    sim = posts[len(posts0):]
    pool = {q["subject"] for q in queue}
    shown = {p["subject"] for p in sim}
    gs, _ = min_gap(sim, "subject")
    gc, _ = min_gap(sim, "scientific_name")
    return len(shown & pool), len(pool), gs, gc, len(fails)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--start")
    ap.add_argument("--seed-posts", action="store_true")
    ap.add_argument("--cap", type=int)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--starve", help="slug:n —— 只让这一个类群缩到 n 条")
    a = ap.parse_args()

    queue = lib.load_queue()
    if not queue:
        print("queue.tsv 是空的，没什么可模拟", file=sys.stderr)
        return 2
    start = (dt.date.fromisoformat(a.start) if a.start
             else dt.date.today() + dt.timedelta(days=1))
    posts0 = lib.load_posts() if a.seed_posts else []

    # ---------- --scan：找最小安全水位 ----------
    if a.scan:
        per = collections.Counter(q["group"] for q in queue)
        top = max(per.values())
        print("每类群实际条数：%s\n" % dict(per))
        print("每类群条数   覆盖率      subject 间隔   学名间隔   无候选  判定")
        first_ok = None
        for n in range(22, top + 1):
            sub = cap_queue(queue, n)
            cov, pool, gs, gc, nf = verdict(sub, a.days, start, posts0)
            good = (cov == pool and nf == 0
                    and (gs is None or gs > lib.WINDOW)
                    and (gc is None or gc > lib.WINDOW))
            if good and first_ok is None:
                first_ok = n
            print("  %2d       %3d/%-3d      %-12s   %-8s   %-4d   %s" % (
                n, cov, pool, gs if gs is not None else "无重复",
                gc if gc is not None else "无重复", nf, "✓" if good else "✗"))
        # 理论下界：每类群每 7 天出 1 条，要让同一条的复现间隔 > WINDOW，
        # 一轮必须长于 WINDOW → n * 7 > WINDOW。
        need = lib.WINDOW // 7 + 1
        print("\n理论下界 %d 条/类群（%d*7=%d > %d），实测最小安全水位 %s 条/类群" % (
            need, need, need * 7, lib.WINDOW, first_ok if first_ok else "未找到"))
        if first_ok:
            print("→ 全池安全线 %d*7 = %d 条。lib.QUEUE_LOW 当前 %d，%s" % (
                first_ok, first_ok * 7, lib.QUEUE_LOW,
                "有余量" if lib.QUEUE_LOW >= first_ok * 7 else
                "**偏低：告警会在已经违约之后才响**"))
        return 0

    if a.cap:
        before = len(queue)
        queue = cap_queue(queue, a.cap)
        print("水位敏感性：每类群截到 %d 条（%d → %d 条）" % (a.cap, before, len(queue)))

    if a.starve:
        # 只饿一个类群。QUEUE_LOW 是全池口径，而无候选天数是单类群决定的 ——
        # 这两个口径不一致的话，告警会在已经断更之后才响。这个开关就是为了量出那个缺口。
        sl, n = a.starve.split(":")
        n, got, out = int(n), 0, []
        for q in queue:
            if q["group"] != sl:
                out.append(q)
            elif got < n:
                out.append(q)
                got += 1
        before, queue = len(queue), out
        print("单类群饥饿：%s 截到 %d 条（全池 %d → %d 条，QUEUE_LOW=%d，告警%s）" % (
            sl, n, before, len(queue), lib.QUEUE_LOW,
            "会响" if len(queue) < lib.QUEUE_LOW else "**不会响**"))

    print("池内 %d 条，窗口 %d 天，同属间隔 %d 天，从 %s 起模拟 %d 天%s" % (
        len(queue), lib.WINDOW, lib.GENUS_GAP, start, a.days,
        "（含现有 %d 条历史）" % len(posts0) if posts0 else "（空历史）"))

    posts, fails = simulate(queue, a.days, start, posts0)
    sim = posts[len(posts0):]          # 只统计本次模拟产生的
    ok = True

    # ---- ① 覆盖率 ----
    pool = {q["subject"] for q in queue}
    shown = {p["subject"] for p in sim}
    dead = sorted(pool - shown)
    print("\n① 覆盖率 %d/%d" % (len(shown & pool), len(pool)), end="  ")
    if dead:
        ok = False
        print("✗ 死库存 %d 条：%s" % (len(dead), "、".join(dead[:12])
                                  + ("…" if len(dead) > 12 else "")))
        by_g = collections.Counter(q["group"] for q in queue if q["subject"] in set(dead))
        print("   死库存按类群：%s" % dict(by_g))
    else:
        print("✓ 死库存 0")

    # ---- ②③ 最小复现间隔 ----
    for i, key in ((2, "subject"), (3, "scientific_name")):
        g, who = min_gap(sim, key)
        label = "subject" if key == "subject" else "学名"
        if g is None:
            print("%s 最小复现间隔（%s）：无重复出场（%d 天内每条最多出场一次）  ✓" % (
                "②" if i == 2 else "③", label, a.days))
        elif g > lib.WINDOW:
            print("%s 最小复现间隔（%s）%d 天 > %d  ✓   最紧的一对：%s %s → %s" % (
                "②" if i == 2 else "③", label, g, lib.WINDOW, who[0], who[1], who[2]))
        else:
            ok = False
            print("%s 最小复现间隔（%s）%d 天 ≤ %d  ✗   %s %s → %s" % (
                "②" if i == 2 else "③", label, g, lib.WINDOW, who[0], who[1], who[2]))

    # ---- ④ 无候选天数 ----
    print("\n④ 无候选天数 %d" % len(fails), end="  ")
    if fails:
        ok = False
        print("✗")
        for f in fails[:8]:
            print("   %s  %s" % (f["date"], f["reason"]))
        if len(fails) > 8:
            print("   …另有 %d 天" % (len(fails) - 8))
    else:
        print("✓")

    # ---- 参考信息：不是验收项，但能看出排期健康度 ----
    print("\n── 参考 ──")
    cnt = collections.Counter(p["subject"] for p in sim)
    print("出场次数 min/max：%d / %d（%d 条题共 %d 天）" % (
        min(cnt.values()), max(cnt.values()), len(cnt), len(sim)))
    print("类群分布：%s" % dict(collections.Counter(p["group"] for p in sim)))
    # 同属间隔是硬过滤，这里回验一次 —— 如果它 ≤ GENUS_GAP，说明 pick_one 的
    # hot_genus 过滤有 bug（模拟器只观察，不重新实现判据）
    gg, _ = min_gap([{**p, "g": lib.genus_of(p["scientific_name"])} for p in sim], "g")
    print("同属最小间隔 %s 天（阈值 %d）%s" % (
        gg, lib.GENUS_GAP, "✓" if gg is None or gg > lib.GENUS_GAP else "✗ pick_one 的同属过滤失效"))
    if gg is not None and gg <= lib.GENUS_GAP:
        ok = False

    print("水位：池内 %d 条 / 7 类群 = 每类群约 %.1f 条，"
          "每类群每 7 天出 1 条 → 一轮约 %.0f 天" % (
              len(pool), len(pool) / 7.0, len(pool) / 7.0 * 7))

    # ---- 告警口径回验：告警必须**早于**断更 ----
    # 这一项不是 §9 原有的，是 --starve 实测暴露出来的：全池阈值 QUEUE_LOW 会漏掉
    # 单类群饥饿。既然阈值是靠模拟定出来的，它对不对也该由模拟说话。
    per = collections.Counter(q["group"] for q in queue)
    thin = sorted(sl for sl, n in per.items() if n < lib.GROUP_LOW)
    print("\n告警口径：单类群线 GROUP_LOW=%d，低于线的类群 %s；全池 %d 条 vs QUEUE_LOW=%d %s" % (
        lib.GROUP_LOW, thin or "无", len(queue), lib.QUEUE_LOW,
        "（会响）" if len(queue) < lib.QUEUE_LOW else "（不响）"))
    if fails and not thin:
        ok = False
        print("   ✗ 有 %d 天无候选却没有任何类群低于 GROUP_LOW —— 告警响得太晚，"
              "调高 GROUP_LOW" % len(fails))
    elif thin and not fails:
        print("   ✓ 告警先响、还没断更（这正是它该有的样子）")

    print("\n%s" % ("§9 四项全绿" if ok else "§9 未通过 —— 上线前必须修"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
