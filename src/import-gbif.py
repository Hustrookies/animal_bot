#!/usr/bin/env python3
"""阶段 1：从 GBIF backbone 枚举候选物种，产出 data/candidates.jsonl。

用法：
    python3 import-gbif.py                 # 全部 7 类群，可反复重跑（断点续跑）
    python3 import-gbif.py marine aves     # 只跑指定类群
    python3 import-gbif.py --target 100    # 改每类群目标条数
    python3 import-gbif.py --reset         # 丢掉进度重新枚举（不删已有候选）

产出每行一个候选，不含 region —— distributions 要每物种一次请求，Aves 有一万多个
物种，那是几小时。地域只在 build-queue 阶段对真正入选的 ~224 条查，见 SPEC §6.2。

═══ 枚举条件是实测定下来的，不要凭直觉改 ═══

1) 必须过滤化石类元。`rank=SPECIES&status=ACCEPTED` 拉到的 Carnivora 有 1736 条，
   绝大多数是 Amphicyonidae、Barbourofelidae、Ginsburgsmilus 这些已灭绝科 ——
   不过滤的话候选池里全是没人听过的化石，而且一个中文名都没有。

2) 过滤要走**两路并集**，单用任何一路都严重欠收（probe/enum.txt 实测）：

       类元              isExtinct=false   threat=<IUCN 6 级>
       Carnivora              342                262
       Squamata               687               9775
       Elasmobranchii          71               1150
       Cypriniformes           34               3352
       Aves                 10688               9938

   `isExtinct=false` 漏掉 isExtinct 字段为空的（鱼类、爬行类大面积为空）；
   `threat` 漏掉没被 IUCN 评估过的。所以两路都跑，按 speciesKey 并集去重。

3) `threat` 参数一次可传多值，语义是 OR（实测 VU=63、VU+EN=92）。所以 6 个等级
   一次请求就够，不必发 6 轮。

4) 中文名从 search 结果**内联的** vernacularNames 里取，不走 /species/{key}/
   vernacularNames 端点：内联与端点在 zho 上 19/20 一致，但内联是 1 请求 300 个
   物种，端点是 1 请求 1 个物种。
   注意 GBIF 的 zho 俗名常是繁体（藪貓/大熊貓）或拼音（Lǎohǔ/Yun Bao），
   所以要先 zhconv 转简，再用 CJK 正则把拼音那类扔掉。
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import TAXA, candidates_path

# zhconv 用来把 GBIF 的繁体俗名转简。优先本项目 vendor，回退 wiki-bot 那份。
for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor"),
           "/opt/wiki/vendor"):
    if os.path.isdir(_p):
        sys.path.insert(0, os.path.abspath(_p))
        break
import zhconv

API = "https://api.gbif.org/v1"
BACKBONE = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
PAGE = 300                      # GBIF 单页上限
IUCN = ["LEAST_CONCERN", "NEAR_THREATENED", "VULNERABLE",
        "ENDANGERED", "CRITICALLY_ENDANGERED", "DATA_DEFICIENT"]
# threat= 参数里故意不传 EXTINCT / EXTINCT_IN_THE_WILD —— 不主动去捞灭绝物种。
# 但 isExtinct=false 那一路仍会漏进来（GBIF 这两个字段不一致），所以还要在收录处再挡。

# threatStatuses 是**数组**，且同一物种可能同时带全球等级和区域等级：斑蝥走 threat=
# 路线进来，[0] 却是 REGIONALLY_EXTINCT —— 那不是我传的筛选值，是它的区域评估。
# 所以取等级要按下面这个集合挑，不能盲取 [0]。
GLOBAL_IUCN = set(IUCN) | {"EXTINCT", "EXTINCT_IN_THE_WILD"}

TARGET = 400        # 每类群目标候选数。远超 32×2 的实际需要，给闸门和去重留余量。
MAX_PAGES = 60      # 单个类元单路最多翻多少页。Insecta 中文名命中率 3%，不设上限
                    # 会为了凑够数把 60 万条虫子全翻一遍。

ZH_RE = re.compile(r"^[\u4e00-\u9fff]{2,8}$")


def progress_path():
    return os.path.join(os.path.dirname(candidates_path()), ".import-progress.json")


def get(path, **params):
    """GBIF 请求，4 次重试 + 线性退避。实测长循环里会冒 ssl.SSLEOFError。"""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    print("  ! 放弃 %s (%s: %s)" % (url[:90], type(last).__name__, last),
          file=sys.stderr)
    return None


def zh_all(rec):
    """从内联 vernacularNames 取全部可用的简体中文名，按稳定顺序返回。

    **不在这里挑哪个名字好用。** 第一版在这里取了「最短的那个」，理由是最短的通常是
    正名 —— 结果错得很彻底：GBIF 的 zho 俗名里混着大量台湾/闽南渔业统称，而统称恰恰
    比正名短。实测 18 个不同鲨鱼种的最短中文名都是「沙条」，20 个鳐种都是「鲂仔」，
    直接违反约束 ③（不能是一类动物的宽泛介绍）。

    统称只能在**全局**识别 —— 一个名字被多个物种共用，它就是统称。单看一条记录看不出来。
    所以这里把候选全存下来，定名交给 refine-candidates.py。
    """
    out = set()
    for v in (rec.get("vernacularNames") or []):
        if (v.get("language") or "").lower() != "zho":
            continue
        name = zhconv.convert((v.get("vernacularName") or "").strip(), "zh-cn")
        if ZH_RE.match(name):
            out.add(name)
    return sorted(out)


def page(taxon_key, route, offset, rank):
    """拉一页。route 决定用哪一路过滤条件（见模块 docstring 第 2 条）。"""
    p = dict(highertaxonKey=taxon_key, rank=rank, status="ACCEPTED",
             datasetKey=BACKBONE, limit=PAGE, offset=offset)
    if route == "extant":
        p["isExtinct"] = "false"
    else:
        p["threat"] = IUCN
    return get("/species/search", **p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="*", help="只跑这些类群，默认全部")
    ap.add_argument("--target", type=int, default=TARGET)
    ap.add_argument("--rank", default="SPECIES", choices=["SPECIES", "SUBSPECIES"])
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    # 默认顺序必须是 TAXA 的键顺序，**不是** GROUPS 的星期顺序。GROUPS 里 carnivora
    # 排周一、marine 排周三，照那个顺序跑的话 Carnivora 会先把 Phocidae / Otariidae /
    # Odobenidae 全收进 carnivora（鳍足类在分类上就在食肉目之下），marine 再跑就一个
    # 海豹都拿不到。抢占顺序只由 TAXA 定义，见 lib.py 里那段注释。
    groups = a.groups or list(TAXA)
    for g in groups:
        if g not in TAXA:
            sys.exit("未知类群 %r，可选：%s" % (g, ", ".join(TAXA)))

    os.makedirs(os.path.dirname(candidates_path()), exist_ok=True)

    # 已有候选：speciesKey 全局去重，同时实现类群抢占 —— 同一物种只进第一个抢到
    # 它的类群，所以 TAXA 里 marine 必须排在 carnivora 之前，否则海豹会被 Carnivora
    # 先吃掉（Pinnipedia 在分类上就在 Carnivora 之下）。
    seen_key, per_group = set(), {}
    if os.path.exists(candidates_path()):
        with open(candidates_path(), encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                seen_key.add(d["key"])
                per_group[d["group"]] = per_group.get(d["group"], 0) + 1
        print("已有候选 %d 条 %s" % (len(seen_key), per_group))

    prog = {}
    if os.path.exists(progress_path()) and not a.reset:
        prog = json.load(open(progress_path(), encoding="utf-8"))

    out = open(candidates_path(), "a", encoding="utf-8")
    try:
        for g in groups:
            have = per_group.get(g, 0)
            if have >= a.target:
                print("== %s 已有 %d ≥ %d，跳过" % (g, have, a.target))
                continue
            print("== %s（已有 %d，目标 %d）" % (g, have, a.target))

            # 每个 (类元, 路线) 是一条独立的游标。round-robin 轮流各拉一页，而不是
            # 拉干一个再下一个 —— 否则 inverts 的配额会被 Cephalopoda 一家占满，
            # 池子里全是乌贼。
            cursors = [{"key": k, "name": n, "route": r, "off": 0, "done": False}
                       for (n, _rk, k) in TAXA[g] for r in ("extant", "threat")]
            for c in cursors:
                st = prog.get("%s|%s|%s|%s" % (g, c["key"], c["route"], a.rank))
                if st:
                    c["off"], c["done"] = st["off"], st["done"]

            while have < a.target and any(not c["done"] for c in cursors):
                for c in cursors:
                    if c["done"] or have >= a.target:
                        continue
                    d = page(c["key"], c["route"], c["off"], a.rank)
                    if d is None:                  # 重试耗尽，本游标作废但不影响其他
                        c["done"] = True
                        continue
                    res = d.get("results") or []
                    new = 0
                    for rec in res:
                        k, sci = rec.get("key"), rec.get("canonicalName")
                        if not k or not sci or k in seen_key:
                            continue
                        if rec.get("rank") != a.rank:
                            continue
                        st = rec.get("threatStatuses") or []
                        iucn = next((s for s in st if s in GLOBAL_IUCN), "")
                        # EXTINCT 排除：真正灭绝的物种写不了「生活习性」，白鲟就是
                        # 从 isExtinct=false 那一路漏进来的。
                        # EXTINCT_IN_THE_WILD 保留 —— 麋鹿野外灭绝但有大量半野放种群，
                        # 保育故事本身就是好选题。区域性灭绝同理，全球还在。
                        if iucn == "EXTINCT":
                            continue
                        names = zh_all(rec)
                        if not names:
                            continue
                        seen_key.add(k)
                        out.write(json.dumps({
                            "group": g, "zh_all": names, "sci": sci, "key": k,
                            "rank": rec.get("rank"),
                            "family": rec.get("family") or "",
                            "genus": rec.get("genus") or "",
                            "iucn": iucn, "iucn_all": st,
                            "taxon": c["name"], "route": c["route"],
                        }, ensure_ascii=False) + "\n")
                        new += 1
                        have += 1
                    c["off"] += PAGE
                    if d.get("endOfRecords") or c["off"] >= MAX_PAGES * PAGE:
                        c["done"] = True
                    print("   %-16s %-6s off=%-5d 收 %-3d 累计 %d"
                          % (c["name"], c["route"], c["off"] - PAGE, new, have))
                    out.flush()
                    prog["%s|%s|%s|%s" % (g, c["key"], c["route"], a.rank)] = \
                        {"off": c["off"], "done": c["done"]}
                    json.dump(prog, open(progress_path(), "w"), indent=0)
            per_group[g] = have
    finally:
        out.close()

    print("\n===== 汇总 =====")
    for g in groups:
        print("%-10s %d" % (g, per_group.get(g, 0)))


if __name__ == "__main__":
    main()
