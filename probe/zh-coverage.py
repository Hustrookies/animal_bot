#!/usr/bin/env python3
"""一次性调研：各高阶类群里「有可用中文名的 ACCEPTED SPECIES」占多少。

结论决定 lib.py 的 TAXA 表怎么排 —— GBIF 的中文俗名覆盖率按类群差异极大
（实测昆虫接近 0），不先量一遍就会做出一个永远填不满的类群。
"""
import json, random, re, sys, time, urllib.parse, urllib.request

sys.path.insert(0, "/opt/wiki/vendor")
import zhconv

BB = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
CJK = re.compile(r"^[\u4e00-\u9fff]{2,8}$")
SAMPLE = 24

TAXA = [
    ("Carnivora", 732), ("Artiodactyla", 731), ("Perissodactyla", 795),
    ("Aves", 212), ("Sphenisciformes", 7190978),
    ("Cetacea", 733), ("Sirenia", 802), ("Elasmobranchii", 121),
    ("Squamata", 11592253), ("Testudines", 11418114), ("Crocodylia", 11493978),
    ("Amphibia", 131), ("Anura", 952),
    ("Cypriniformes", 1153), ("Siluriformes", 708), ("Salmoniformes", 1313),
    ("Insecta", 216), ("Lepidoptera", 797), ("Odonata", 789),
    ("Arachnida", 367), ("Malacostraca", 229), ("Cephalopoda", 136),
    ("Primates", 798), ("Rodentia", 1459), ("Chiroptera", 734),
    ("Diprotodontia", 1452), ("Proboscidea", 799),
]


def get(path, **q):
    u = "https://api.gbif.org/v1/" + path + ("?" + urllib.parse.urlencode(q, doseq=True) if q else "")
    for i in range(4):
        try:
            with urllib.request.urlopen(u, timeout=40) as r:
                return json.loads(r.read())
        except Exception:
            if i == 3:
                raise
            time.sleep(1.5 * (i + 1))


def zh_names(key):
    out = []
    for off in (0, 300, 600):
        d = get(f"species/{key}/vernacularNames", limit=300, offset=off)
        for v in d.get("results", []):
            if (v.get("language") or "").lower() in ("zho", "zh", "chi", "cmn"):
                s = zhconv.convert((v.get("vernacularName") or "").strip(), "zh-cn")
                if CJK.match(s):
                    out.append(s)
        if d.get("endOfRecords"):
            break
    return sorted(set(out), key=lambda s: (len(s), s))


def main():
    rnd = random.Random(20260831)
    print(f"{'类群':16s} {'物种总数':>8s} {'抽样':>4s} {'有中文名':>7s} {'比率':>5s}  例")
    for name, key in TAXA:
        head = get("species/search", highertaxonKey=key, rank="SPECIES",
                   status="ACCEPTED", datasetKey=BB, limit=1)
        total = head.get("count", 0)
        if total == 0:
            print(f"{name:16s} {'0':>8s}  ← 空，弃用")
            continue
        hit, examples = 0, []
        for _ in range(SAMPLE):
            off = rnd.randrange(0, max(1, min(total, 90000)))
            d = get("species/search", highertaxonKey=key, rank="SPECIES",
                    status="ACCEPTED", datasetKey=BB, limit=1, offset=off)
            rs = d.get("results") or []
            if not rs:
                continue
            zs = zh_names(rs[0]["key"])
            if zs:
                hit += 1
                if len(examples) < 4:
                    examples.append(zs[0])
        print(f"{name:16s} {total:8d} {SAMPLE:4d} {hit:7d} {hit / SAMPLE:5.0%}  {' '.join(examples)}",
              flush=True)


if __name__ == "__main__":
    main()
