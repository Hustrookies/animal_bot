#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取题 —— 0 token。stdout 一行 JSON，就是 agent 的全部输入。

排班（确定性，可审计）：
    ISO 星期 → 类群    1 食肉与有蹄 … 7 其他哺乳类（lib.GROUPS）
    ISO 周数 → 生物地理界倾向  week % 6（lib.REGIONS）
    类群内           最久未出场优先 + 日期种子扰动，见 rank()

移植自 wiki-bot 的 pick.py（commit 9d9fa02，LRU + 扰动那一版，已在生产验证
365 天覆盖率 157/157、死库存 0）。相对它有四处改动：

1. **`lib.CATS` → `lib.GROUPS`，比较的是 slug 不是中文标签。** animal-bot 的
   queue.tsv 第一列存 slug（`carnivora`），wiki-bot 存的是中文类目名。
2. **窗口 100 天 → `lib.WINDOW`（183）。** 这是用户约束 ①，不是可调参数。
3. **新增同属间隔（`lib.GENUS_GAP`，30 天）。** wiki-bot 没有这个概念 —— 历史词条
   没有"属"。而物种有：连着两周推豹属的三个种，读者看到的是同一只大猫。学名的属名
   一致就算同属，这比中文名可靠（美洲狮/山狮是一个种，豹猫/云豹不是一个属）。
4. **取题的全部判据抽进 `pick_one()`。** SPEC §9 要求模拟器 import 真实逻辑、
   不许复刻 —— 而判据不只有排序键：窗口过滤、同属间隔、相似度跳过同样会决定
   覆盖率。这些留在 main() 里的话，模拟器就得复刻它们，那测的是复刻件。

插队方式仍然是往 queue.tsv 追加一行：从未出场的题在 LRU 下自动是最高优先级。
同一天重跑取题结果不变（种子只含日期与 subject），四个 cron 补跑窗口不会换题。

用法：
    ./pick.py                      正常取题，输出一行 JSON（并落盘 pick.json）
    ./pick.py --date 2026-09-01    指定日期（联调）
    ./pick.py --skip 猎豹           排除某个 subject 后重取（agent 判 DUP 后由 run.sh 调）
    ./pick.py --stat               各类群水位概览（人看的）
"""
import argparse
import datetime as dt
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

NEAR_N = 3            # 交给模型的近似条目数
JITTER = 45           # 取题扰动幅度（天），见 rank()


def sched(date):
    """日期 → (类群 slug, 类群标签, 生物地理界)。"""
    iso = date.isocalendar()
    slug, label = lib.GROUPS[iso[2]]
    return slug, label, lib.REGIONS[iso[1] % len(lib.REGIONS)]


def rank(q, today, region, last):
    """取题排序键：当周地理界优先，然后最久未出场，年龄相近的随机打散。

    照搬 wiki-bot 的 LRU + 扰动。它换掉的是行号排序（FIFO），后者有个乘积效应上的
    硬伤：每个「类群 x 地理界」桶里永远是最小行号先出场，而地理界每 6 周才轮回一次，
    等下次轮到时上一条只过了 42 天、仍在窗口内，于是取次小行号 —— 窗口只容得下每桶
    2~3 条已用记录，桶里更靠后的题永远等不到。wiki-bot 实测 157 条里 30 条整年不出场。

    LRU 让每条都保证出场：一直没被选中的题只会越来越旧，迟早成为桶里最旧的那个。
    扰动是为了不让顺序退化成固定周期（纯 LRU 跑满一轮后就是一个不变的排列，年复一年
    同序）。幅度 45 天：足以打散年龄相近的候选，又远小于同桶复现间隔，所以不会让刚
    用过的题插到最久未出场的题前面。

    **`region` 为空的条目（实测 226 条里 26 条）在这里永远落在不匹配那一侧。**
    它们不会因此变成死库存 —— 第二项是年龄，等得越久排得越前，迟早胜出。但覆盖率
    必须由 730 天模拟证明，不能靠这句话推断（§9 ①）。
    """
    s = last.get(q["subject"])
    age = 10 ** 6 if s is None else (today - dt.date.fromisoformat(s)).days
    # 逐候选独立取种子。agent 判 DUP 后 run.sh 会调 --skip 重取，去掉一个候选时其余
    # 候选的扰动值必须不变 —— 按列表顺序调用同一个 Random 的话，重取会让整体重排。
    j = random.Random("%s|%s" % (today.isoformat(), q["subject"])).random() * JITTER
    return (q["region"] != region, -(age + j))


def pick_one(queue, posts, today, skip=()):
    """取一天的题。**取题的全部判据都在这个函数里。**

    返回 (chosen, near, skipped, reason)。chosen 为 None 时 reason 说明为什么。

    pick.main() 与 probe/simulate.py 都只调这一个入口 —— SPEC §9 要求模拟器
    import 真实逻辑而不是复刻。判据有四条，缺一条模拟结果就不作数：

        ① 本类群（ISO 星期）
        ② subject 与学名都不在 lib.WINDOW（183 天）窗口内   ← 约束 ①
        ③ 同属不在 lib.GENUS_GAP（30 天）内
        ④ 与窗口内已推条目的相似度 < lib.HARD

    ② 查两个键：中文名可以有异名（美洲狮/山狮），学名才是物种的唯一身份。只查
    subject 的话，同一个物种换个中文名就能骗过半年窗口。
    """
    slug, label, region = sched(today)
    cutoff = (today - dt.timedelta(days=lib.WINDOW)).isoformat()
    gcut = (today - dt.timedelta(days=lib.GENUS_GAP)).isoformat()
    skip = set(skip)

    recent = [p for p in posts if p.get("date", "") >= cutoff]
    used_subj = {p.get("subject") for p in recent if p.get("subject")}
    used_sci = {p.get("scientific_name") for p in recent if p.get("scientific_name")}
    # 同属最近出场日。只看 GENUS_GAP 窗口内的。
    hot_genus = {lib.genus_of(p.get("scientific_name", ""))
                 for p in posts if p.get("date", "") >= gcut}
    hot_genus.discard("")

    cands = [q for q in queue
             if q["group"] == slug
             and q["subject"] not in used_subj
             and q["scientific_name"] not in used_sci
             and lib.genus_of(q["scientific_name"]) not in hot_genus
             and q["subject"] not in skip]
    last = {}
    for p in posts:
        s, dd = p.get("subject"), p.get("date")
        if s and dd and dd > last.get(s, ""):
            last[s] = dd
    cands.sort(key=lambda q: rank(q, today, region, last))

    skipped = []
    for q in cands:
        probe = {"title": q["title"], "summary": q["note"], "entities": q["entities"]}
        scored = []
        for p in recent:
            s = lib.sim(probe, p)
            if s >= lib.SOFT:
                scored.append((s, p))
        scored.sort(key=lambda t: -t[0])
        if scored and scored[0][0] >= lib.HARD:
            # 硬重复 → 换下一个，不放弃今天。重复检测不该以停更一天为代价。
            skipped.append({"subject": q["subject"], "hit": scored[0][1].get("title"),
                            "score": round(scored[0][0], 3)})
            continue
        near = [{"title": p.get("title"), "summary": p.get("summary"),
                 "days_ago": (today - dt.date.fromisoformat(p["date"])).days,
                 "score": round(s, 3)}
                for s, p in scored[:NEAR_N]]
        return q, near, skipped, ""

    n_all = sum(1 for q in queue if q["group"] == slug)
    return None, [], skipped, (
        "%s 无可用候选（池内 %d 条，窗口内已用 %d 条，同属冷却 %d 条，硬重复跳过 %d 条）" % (
            label, n_all,
            sum(1 for q in queue if q["group"] == slug and q["subject"] in used_subj),
            sum(1 for q in queue if q["group"] == slug
                and lib.genus_of(q["scientific_name"]) in hot_genus),
            len(skipped)))


def material_of(subject_wiki):
    """事实锚：只读本地 data/material.json（fetch-material.py 预抓，阶段 5）。

    **运行时不走网络。** 中文维基的 API 在境内被阻断，运行时抓取必然失败 ——
    wiki-bot 已经用一个月的生产验证了这条：锚必须预抓并提交进 git。
    """
    mpath = os.path.join(lib.ROOT, "data", "material.json")
    if not os.path.exists(mpath):
        return "", "not_prefetched"
    try:
        local = json.load(open(mpath, encoding="utf-8"))
    except Exception:
        return "", "material_json_broken"
    ent = local.get(subject_wiki)
    if ent and ent.get("text"):
        return ent["text"], "local"
    if ent:
        return "", "local_empty:%s" % ent.get("status", "?")
    return "", "not_prefetched"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--no-wiki", action="store_true")
    a = ap.parse_args()

    today = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    queue, posts = lib.load_queue(), lib.load_posts()
    cutoff = (today - dt.timedelta(days=lib.WINDOW)).isoformat()
    used = {p.get("subject") for p in posts if p.get("date", "") >= cutoff}

    # ---------- --stat：各类群水位 ----------
    if a.stat:
        # left 与 QUEUE_LOW 同一口径：窗口内已推过的不计入水位（183 天后才回收）。
        # 所以它是「现在还能取的条数」，不是行数。
        left = {}
        for i in sorted(lib.GROUPS):
            sl, lb = lib.GROUPS[i]
            left[sl] = sum(1 for q in queue
                           if q["group"] == sl and q["subject"] not in used)
        print(json.dumps({"queue_left": left, "total": len(queue),
                          "window": lib.WINDOW, "genus_gap": lib.GENUS_GAP,
                          "group_low": lib.GROUP_LOW,
                          "groups_low": sorted(sl for sl, n in left.items()
                                               if n < lib.GROUP_LOW)},
                         ensure_ascii=False, indent=2))
        return 0

    slug, label, region = sched(today)
    chosen, near, skipped, reason = pick_one(queue, posts, today, a.skip)
    if chosen is None:
        print(json.dumps({"ok": False, "date": today.isoformat(), "group": slug,
                          "reason": reason, "skipped": skipped}, ensure_ascii=False))
        return 2

    # 183 天前推过 → 回收题，必须换切入角度；把旧文摘要塞进 near 供模型避开
    old = [p for p in posts if p.get("subject") == chosen["subject"]
           and p.get("date", "") < cutoff]
    recycled = bool(old)
    if recycled:
        o = max(old, key=lambda p: p["date"])
        near.insert(0, {"title": o.get("title"), "summary": o.get("summary"),
                        "days_ago": (today - dt.date.fromisoformat(o["date"])).days,
                        "score": 1.0, "is_previous_take": True})

    # 取锚文用第 8 列 wiki，不是 subject —— 226 条里 21% 两者不同串（SPEC §6.3）。
    wiki_title = chosen["wiki"] or chosen["subject"]
    material, mstatus = ("", "skipped_no_wiki") if a.no_wiki else material_of(wiki_title)

    left = {}
    for i in sorted(lib.GROUPS):
        sl, _ = lib.GROUPS[i]
        left[sl] = sum(1 for q in queue if q["group"] == sl and q["subject"] not in used)

    out = {
        "ok": True,
        "date": today.isoformat(),
        "date_label": "%d月%d日" % (today.month, today.day),
        "group": slug, "group_label": label,
        "region": region,
        "topic": {"title": chosen["title"], "subject": chosen["subject"],
                  "scientific_name": chosen["scientific_name"],
                  "note": chosen["note"], "entities": chosen["entities"],
                  "wiki": wiki_title},
        "material": material,
        "material_status": mstatus,
        "near": near[:NEAR_N + 1],
        "recycled": recycled,
        "queue_left": left,
        "skipped_as_dup": skipped,
    }
    out["queue_low"] = sum(left.values()) < lib.QUEUE_LOW
    # 触发补池的真正判据是**任一类群**见底，不是全池。全池阈值会漏掉单类群饥饿：
    # probe/simulate.py --starve amphibia:26 实测全池 220 > QUEUE_LOW=200、告警不响，
    # 而那一年断更 3 天。两个都报出来，让 run.sh 用 groups_low 决定要不要补哪一群。
    out["groups_low"] = sorted(sl for sl, n in left.items() if n < lib.GROUP_LOW)
    out["low"] = bool(out["groups_low"]) or out["queue_low"]

    line = json.dumps(out, ensure_ascii=False)
    with open(os.path.join(lib.ROOT, "pick.json"), "w", encoding="utf-8") as f:
        f.write(line)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
