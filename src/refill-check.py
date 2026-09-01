#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收 agent 补的选题角度，产出可追加进 queue.tsv 的完整行。

用法：
    refill-check.py data/queue.add.tsv <类群slug>
    合格行写到 data/queue.add.tsv.ok（8 列，见 lib.QUEUE_COLS）
    一行都不合格 → exit 1

── agent 只写三段 ──
    subject <TAB> title <TAB> note

**其余五列（group / region / scientific_name / entities / wiki）由本脚本拿 subject 去
ready.jsonl 查出来填，不让 agent 抄。** 这不是嫌它麻烦，是让它在结构上**没有能力**
改坏那些字段 —— subject 已经过完四道闸门（学名格式、rank、家养种、统称/黑名单、
事实锚终选），让 agent 复制一遍就等于给约束 ③ 开一个抄写错误的口子。它认领不到的
subject 直接判废，比事后比对字符串可靠。

── entities 为什么不让 agent 写 ──
SPEC §6.2 说 agent 只出 title 和 note 两样，entities 由代码填 = [subject]。
去重主键是 subject + scientific_name（§7.3，精确匹配），entities 只是近似信号，
而对物种来说它极容易误报 —— 两个毫不相干的物种共享「东洋界」「热带雨林」就会被
判成近似选题。宁可这一列信息量低，不要它制造假阳性。
lib.load_queue() 本来就会把 subject 补进 entities，所以这一列写 subject 是等价的。

── 必须避开的坑（wiki-bot 已记录为项目缺陷经验）──
refill 不得用 `|| true` 掩盖 agent 失败，不得只数行数 —— 要比对前后增量。
wiki-bot 上就是 agent 超时零产出、脚本只数一遍行数就打印「refill 完成」并 exit 0，
失败完全静默。所以这里：零合格行一定 exit 1，且逐行打印判废理由。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

TITLE_MIN, TITLE_MAX = 6, 26
NOTE_MIN, NOTE_MAX = 8, 48

# 空泛模板。agent 偷懒时会写「XX的介绍」「XX是一种猫科动物」这类等于没写的钩子，
# 它们在字数上完全合格，只能靠词面拦。
LAZY = ("的介绍", "的简介", "简介", "是一种", "的故事", "小知识", "科普",
        "带你了解", "你知道吗", "揭秘", "大揭秘")


def zh_len(s):
    """中文字符数。用字符数而不是字节数 —— 字节数会让含拉丁学名的标题虚高一倍。"""
    return len(s)


def main():
    if len(sys.argv) < 3:
        print("用法: refill-check.py <add.tsv> <slug>", file=sys.stderr)
        return 2
    path, slug = sys.argv[1], sys.argv[2]
    if not os.path.exists(path):
        print("[refill-check] 文件不存在：%s" % path)
        return 1

    # 可认领的 subject → ready 记录。只认本类群的 —— agent 拿着 aves 的名字写进
    # carnivora 那一批，星期几就全错了。
    ready = {}
    with open(lib.ready_path(), encoding="utf-8") as f:
        import json
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            d = json.loads(ln)
            if d["group"] == slug:
                ready[d["subject"]] = d

    already = {q["subject"] for q in lib.load_queue()}
    seen_subj, seen_title, ok_rows, bad = set(), set(), [], 0

    with open(path, encoding="utf-8") as f:
        for i, ln in enumerate(f, 1):
            ln = ln.rstrip("\n")
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            c = ln.split("\t")
            if len(c) != 3:
                print("[%d] 废：要 3 列（subject/title/note），给了 %d 列" % (i, len(c)))
                bad += 1
                continue
            subject, title, note = (x.strip() for x in c)

            if subject not in ready:
                # 认领不到。最常见的是 agent 自己编了个物种名，或把繁体写成简体。
                print("[%d] 废：subject「%s」不在 %s 的 ready 清单里" % (i, subject, slug))
                bad += 1
                continue
            if subject in already:
                print("[%d] 废：subject「%s」已在 queue.tsv 里" % (i, subject))
                bad += 1
                continue
            if subject in seen_subj:
                print("[%d] 废：subject「%s」本批内重复" % (i, subject))
                bad += 1
                continue

            if not (TITLE_MIN <= zh_len(title) <= TITLE_MAX):
                print("[%d] 废：%s title %d 字，要 %d–%d" % (
                    i, subject, zh_len(title), TITLE_MIN, TITLE_MAX))
                bad += 1
                continue
            if not (NOTE_MIN <= zh_len(note) <= NOTE_MAX):
                print("[%d] 废：%s note %d 字，要 %d–%d" % (
                    i, subject, zh_len(note), NOTE_MIN, NOTE_MAX))
                bad += 1
                continue
            if title == subject or title.strip("。，、！？…「」《》") == subject:
                print("[%d] 废：%s title 就是物种名本身，不是钩子" % (i, subject))
                bad += 1
                continue
            lazy = [w for w in LAZY if w in title]
            if lazy:
                print("[%d] 废：%s title 含空泛模板 %s" % (i, subject, lazy))
                bad += 1
                continue
            if title in seen_title:
                print("[%d] 废：%s title 与本批另一条完全相同" % (i, subject))
                bad += 1
                continue

            d = ready[subject]
            # 8 列，顺序照 lib.QUEUE_COLS。entities 只放 subject，理由见模块 docstring。
            ok_rows.append("\t".join([
                d["group"], d["region"], title, subject, d["sci"],
                subject, note, d["wiki"]]))
            seen_subj.add(subject)
            seen_title.add(title)

    if not ok_rows:
        print("[refill-check] %s：%d 行全废，无一合格" % (slug, bad))
        return 1

    with open(path + ".ok", "w", encoding="utf-8") as f:
        f.write("\n".join(ok_rows) + "\n")
    print("[refill-check] %s：合格 %d 行，废 %d 行" % (slug, len(ok_rows), bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
