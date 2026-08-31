#!/usr/bin/env python3
"""调研 2：确定 import-gbif.py 的枚举过滤条件。

第一版调研（zh-coverage.py）用 rank=SPECIES+ACCEPTED 随机抽样，Carnivora 命中 0/24。
原因不是 GBIF 没中文名，而是 backbone 的 1736 条 Carnivora「物种」里绝大多数是
**化石类元**（Amphicyonidae、Barbourofelidae 这些已灭绝科）。isExtinct=false 之后
只剩 342 条，中文名命中率 46%。

本脚本量三件事，决定枚举条件：
  1. 每个高阶类元的 extant 物种数、IUCN 已评估物种数
  2. threat 参数能否一次传多值（决定要发几轮请求）
  3. search 结果内联的 vernacularNames 里 zho 的命中率（内联省掉每物种一次请求，
     实测与专用端点 19/20 一致）
"""
import json, sys, time, urllib.parse, urllib.request

sys.path.insert(0, "/opt/wiki/vendor")
import re

import zhconv

BB = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
CJK = re.compile(r"^[\u4e00-\u9fff]{2,8}$")
ASSESSED = ["LEAST_CONCERN", "NEAR_THREATENED", "VULNERABLE",
            "ENDANGERED", "CRITICALLY_ENDANGERED", "DATA_DEFICIENT"]

TAXA = [
    ("marine", "Cetacea", 733), ("marine", "Sirenia", 802),
    ("marine", "Elasmobranchii", 121),
    ("carnivora", "Carnivora", 732), ("carnivora", "Artiodactyla", 731),
    ("carnivora", "Perissodactyla", 795), ("carnivora", "Proboscidea", 799),
    ("aves", "Aves", 212),
    ("reptilia", "Squamata", 11592253), ("reptilia", "Testudines", 11418114),
    ("reptilia", "Crocodylia", 11493978),
    ("amphibia", "Amphibia", 131), ("amphibia", "Cypriniformes", 1153),
    ("amphibia", "Siluriformes", 708), ("amphibia", "Salmoniformes", 1313),
    ("amphibia", "Acipenseriformes", 1103),
    ("inverts", "Cephalopoda", 136), ("inverts", "Malacostraca", 229),
    ("inverts", "Lepidoptera", 797), ("inverts", "Odonata", 789),
    ("inverts", "Arachnida", 367), ("inverts", "Insecta", 216),
    ("mammalia", "Primates", 798), ("mammalia", "Rodentia", 1459),
    ("mammalia", "Chiroptera", 734), ("mammalia", "Diprotodontia", 1452),
    ("mammalia", "Lagomorpha", 785), ("mammalia", "Monotremata", 791),
    ("mammalia", "Peramelemorphia", 794),
]


def get(path, **q):
    u = "https://api.gbif.org/v1/" + path + ("?" + urllib.parse.urlencode(q, doseq=True) if q else "")
    for i in range(4):
        try:
            with urllib.request.urlopen(u, timeout=60) as r:
                return json.loads(r.read())
        except Exception:
            if i == 3:
                raise
            time.sleep(1.5 * (i + 1))


def zh_of(rec):
    out = set()
    for v in rec.get("vernacularNames") or []:
        if (v.get("language") or "").lower() in ("zho", "zh", "chi", "cmn"):
            s = zhconv.convert((v.get("vernacularName") or "").strip(), "zh-cn")
            if CJK.match(s):
                out.add(s)
    return sorted(out, key=lambda s: (len(s), s))


def main():
    base = dict(rank="SPECIES", status="ACCEPTED", datasetKey=BB)
    # threat 能否多值
    one = get("species/search", highertaxonKey=732, threat="VULNERABLE", limit=0, **base)["count"]
    multi = get("species/search", highertaxonKey=732,
                threat=["VULNERABLE", "ENDANGERED"], limit=0, **base)["count"]
    print(f"threat 多值: VU={one}  VU+EN={multi}  → {'OR 生效' if multi > one else '只取第一个'}\n")

    print(f"{'类群':10s} {'类元':17s} {'extant':>7s} {'已评估':>7s} {'样本':>5s} {'有中文名':>7s}  例")
    for slug, name, key in TAXA:
        ext = get("species/search", highertaxonKey=key, isExtinct="false", limit=0, **base)["count"]
        asd = get("species/search", highertaxonKey=key, threat=ASSESSED, limit=0, **base)["count"]
        d = get("species/search", highertaxonKey=key, threat=ASSESSED, limit=300, **base)
        rows = d.get("results", [])
        hits = [zh_of(r)[0] for r in rows if zh_of(r)]
        n = len(rows)
        rate = f"{len(hits) / n:.0%}" if n else "-"
        print(f"{slug:10s} {name:17s} {ext:7d} {asd:7d} {n:5d} {len(hits):4d} {rate:>4s}  "
              f"{' '.join(hits[:5])}", flush=True)


if __name__ == "__main__":
    main()
