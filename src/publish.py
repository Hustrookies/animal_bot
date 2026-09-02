#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布的**数据**那一半：追加 posts.jsonl + 归档 content.json。0 token、不碰 git。

── 为什么把它从 publish.sh 里拆出来 ──

wiki-bot 的 publish.sh 把这段逻辑写成 shell 里的 python heredoc。那样写有一个具体的
后果：**它永远跑不了用例**。而这段代码干的正是本项目最脆的一件事 —— 往 posts.jsonl
写字段。posts.jsonl 是半年去重的唯一依据，少一个键的失败方式是**静默降级**（详见
`lib.POST_FIELDS` 那张表），不是报错。这种代码必须能被用例逐条打。

git 那一半留在 publish.sh：分类重试、跳空 commit、分支保护，都是 shell 的活。

用法：
  ./publish.py                 追加本期记录 + 归档 content.json（幂等）
  ./publish.py --check         只检查现有 posts.jsonl 全部记录是否合契约，不写
  ./publish.py --selftest      用例

── 三条不变量 ──

1. **顺序定死：先 posts.jsonl，再 data/content/。** jsonl 是去重的真相，content 归档
   是重渲的输入。反过来的话，中途挂掉会留下"页面能重渲、但去重不认识这一期"的状态，
   下一天可能再推同一个物种。
2. **幂等靠日期查 jsonl，不靠 stage。** 四个 cron 窗口都可能跑到这里，stage 文件被
   清掉过之后仍然不能重复追加。
3. **写之前先验契约，不合格就 exit 1 且不写。** 半条坏记录比没有记录难查得多：
   它会一直待在那儿，而 load_posts 从来不校验。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib                                                    # noqa: E402


def build_record(content, buildid):
    """content.json + buildid → posts.jsonl 记录。纯函数，用例直接打。

    **身份字段一律取 content.json，不取 pick.json。** selfcheck.py 已经逐字校验过
    content 的 subject/scientific_name/date/group 与 pick 一致，而 pick.json 每天被
    重写 —— 补跑窗口跑到这里时它可能已经是下一天的了。同一个事实有两个来源，
    就一定会有对不上的那天。

    `group_label` 是阶段 8 点名"今天还没有任何写入方"的那个键。这里是它的写入方。
    """
    ents = [x for x in (content.get("entities") or []) if str(x).strip()]
    subject = (content.get("subject") or "").strip()
    # subject 必须在 entities 里：实体重叠是 lib.sim 的主信号，而 load_queue 对队列行
    # 做了同样的结构性保证（那边是 ents.insert(0, subject)）。两处口径要一样，
    # 否则「入队时算得到重叠、发布后算不到」，去重强度在发布这一步无声地掉一档。
    if subject and subject not in ents:
        ents.insert(0, subject)
    date = (content.get("date") or "").strip()
    return {
        "date": date,
        "group": (content.get("group") or "").strip(),
        "group_label": (content.get("group_label") or "").strip(),
        "title": (content.get("title") or "").strip(),
        "subject": subject,
        "scientific_name": (content.get("scientific_name") or "").strip(),
        "summary": (content.get("summary") or "").strip(),
        "entities": ents,
        "tags": [x for x in (content.get("tags") or []) if str(x).strip()],
        "buildid": (buildid or "").strip(),
        "url": "p/%s.html" % date,
    }


def archive_content(content, date):
    """一天一个新文件、永不改写 —— 对 git 是最优形状，也是 render --rebuild-all 的输入。"""
    d = os.path.join(lib.ROOT, "data", "content")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s.json" % date)
    json.dump(content, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return p


def publish():
    cpath = os.path.join(lib.ROOT, "content.json")
    if not os.path.exists(cpath):
        print("publish: 无 content.json，无可发布内容")
        return 1
    content = json.load(open(cpath, encoding="utf-8"))
    date = (content.get("date") or "").strip()
    if not date:
        print("publish: content.json 缺 date")
        return 1

    bpath = os.path.join(lib.ROOT, "state", "%s.buildid" % date)
    if not os.path.exists(bpath):
        # buildid 由 render.py 落盘。没有它说明这一期还没渲染过 —— 那是 stage 机
        # 串错了，不是数据问题，要说清楚而不是补一个空串糊过去。
        print("publish: 缺 state/%s.buildid，说明还没渲染过（stage 串错了）" % date)
        return 1
    buildid = open(bpath, encoding="utf-8").read().strip()

    rec = build_record(content, buildid)
    bad = lib.post_defects(rec)
    if bad:
        print("publish: 记录不合 posts.jsonl 契约，拒绝写入：")
        for b in bad:
            print("  - %s" % b)
        return 1

    # 幂等：同一天已在 jsonl 里就不再追加。查的是文件内容，不是 stage 文件。
    old = [p for p in lib.load_posts() if p.get("date") == date]
    if old:
        print("publish: %s 已在 posts.jsonl，跳过追加" % date)
        # 记录里的 buildid 是**首发时**的签名，这里不改写它（jsonl 只追加）。
        # 但两者一旦不同就要说出来：那意味着页面在首发之后被重渲过（改了模板、
        # 或补跑时内容变了）。阶段 9 曾经每次重渲都触发这一行 —— 根因是 gen-image
        # 把出图状态写进了 content.json 而 buildid 哈希了它（已在 render.py 剔掉）。
        # 阶段 10 的 wait_live 一律读 state/<date>.buildid，不读这里，所以它不影响通知；
        # 留这行是为了让"页面已经不是首发那一版"这件事有人知道。
        if old[-1].get("buildid") != buildid:
            print("publish: 注意 —— posts.jsonl 记的是首发签名 %s，当前页面是 %s"
                  % (old[-1].get("buildid"), buildid))
    else:
        with open(lib.jsonl_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("publish: posts.jsonl +1（%s %s／%s）"
              % (date, rec["subject"], rec["scientific_name"]))

    archive_content(content, date)
    print("publish: 归档 data/content/%s.json" % date)
    return 0


def check_all():
    """把现有 posts.jsonl 全部记录过一遍契约。人工排障用，也是回归历史脏数据的入口。"""
    posts = lib.load_posts()
    if not posts:
        print("check: posts.jsonl 是空的（还没有任何一期）")
        return 0
    n = 0
    for p in posts:
        bad = lib.post_defects(p)
        if bad:
            n += 1
            print("%s %s：" % (p.get("date", "????"), p.get("subject", "?")))
            for b in bad:
                print("  - %s" % b)
    print("check: %d 期，%d 期不合契约" % (len(posts), n))
    return 1 if n else 0


def _render_fields():
    """读 render.py 的 POSTS_FIELDS（归档页子集）。

    用 import 而不是复制一份常量：复制就是"两处对不上"的起点，而这条用例存在的
    全部意义正是防这件事。
    """
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render.py")
    spec = importlib.util.spec_from_file_location("_render", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.POSTS_FIELDS


# ────────────────────────── 用例 ──────────────────────────

GOOD_CONTENT = {
    "date": "2026-09-02", "group": "marine", "group_label": "海洋动物",
    "title": "在砾石滩上守着领地的北海狗", "subject": "北海狗",
    "scientific_name": "Callorhinus ursinus",
    "summary": "雄兽整个繁殖季不下水，靠体脂撑过两个月。",
    "entities": ["北海狗", "白令海"], "tags": ["海洋哺乳动物", "繁殖行为"],
}


def selftest():
    bad = 0

    def ck(name, cond, extra=""):
        nonlocal bad
        if not cond:
            bad += 1
        print("  %-4s %-32s %s" % ("OK" if cond else "FAIL", name, extra))

    rec = build_record(GOOD_CONTENT, "2026-09-02-abc12345")
    ck("合格 content 产出合格记录", lib.post_defects(rec) == [],
       str(lib.post_defects(rec)))
    ck("group_label 真的被写进去了", rec["group_label"] == "海洋动物")
    ck("url 由 date 派生", rec["url"] == "p/2026-09-02.html")

    # 契约表与消费者对不上，是这一层唯一致命的错。逐个消费者点名。
    miss = sorted(set(_render_fields()) - set(lib.POST_FIELDS))
    ck("契约覆盖 render 归档页所需", not miss, str(miss))
    ck("契约覆盖 pick.py 去重所需",
       {"date", "subject", "scientific_name", "title", "summary", "entities"}
       <= set(lib.POST_FIELDS))

    # subject ∈ entities：与 load_queue 同一条结构性保证
    r2 = build_record({**GOOD_CONTENT, "entities": ["白令海"]}, "b")
    ck("subject 不在 entities 里会补进去", r2["entities"][0] == "北海狗")
    r3 = build_record({**GOOD_CONTENT, "entities": []}, "b")
    ck("entities 全空也至少有 subject", r3["entities"] == ["北海狗"])

    # 每一个契约字段单独抽掉都必须被抓住。**逐个抽**而不是抽一个代表：
    # 「循环里写错下标、只查了第一个键」这类错，抽一个是看不出来的。
    for k in lib.POST_FIELDS:
        v = [] if k == "entities" else ""
        ck("抽掉 %s 会被拦" % k, lib.post_defects({**rec, k: v}) != [])

    # 空串必须与缺键同罪 —— 这是本项目栽过的形态：键在、grep 得到、人就以为没问题。
    ck("学名是空白串也算违反",
       lib.post_defects({**rec, "scientific_name": "   "}) != [])
    ck("entities 全是空串也算违反",
       lib.post_defects({**rec, "entities": ["", "  "]}) != [])

    # buildid 缺失时必须停下，而不是写一条 buildid="" 的记录进去 —— 那条记录会让
    # 阶段 10 的 wait_live 永远判不出线上是不是本期。
    ck("buildid 为空的记录不合契约",
       lib.post_defects(build_record(GOOD_CONTENT, "")) != [])

    print("\n%s" % ("全部通过" if not bad else "%d 条不通过" % bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.check:
        return check_all()
    return publish()


if __name__ == "__main__":
    sys.exit(main())
