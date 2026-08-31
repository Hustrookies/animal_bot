#!/usr/bin/env python3
"""把 lib.py 的 TAXA 里每个 key 回查 /v1/species/{key}，报出名字或 rank 不符的。

对不符的项再用 /v1/species?name=&rank= 在 backbone 里正查一遍，给出候选 key。
不用 /species/match：实测它对高阶名会静默 HIGHERRANK（Sirenia → Mammalia）。
"""
import json, sys, time, urllib.parse, urllib.request

sys.path.insert(0, "/root/workspace/animal-bot/src")
from lib import TAXA

API = "https://api.gbif.org/v1"
BACKBONE = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"


def get(path, **params):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 3:
                return {"__error": "%s: %s" % (type(e).__name__, e)}
            time.sleep(2 * (attempt + 1))


def lookup(name, rank):
    """在 backbone 里正查 name+rank，返回候选 (key, canonicalName, rank, status)。"""
    d = get("/species", datasetKey=BACKBONE, name=name, rank=rank, limit=20)
    out = []
    for r in (d.get("results") or []):
        if r.get("canonicalName") == name and r.get("rank") == rank:
            out.append((r.get("key"), r.get("canonicalName"), r.get("rank"),
                        r.get("taxonomicStatus")))
    return out


bad = []
for group, items in TAXA.items():
    for name, rank, key in items:
        d = get("/species/%d" % key)
        got_name = d.get("canonicalName") or d.get("scientificName")
        got_rank = d.get("rank")
        status = d.get("taxonomicStatus")
        ok = (got_name == name and got_rank == rank)
        flag = "ok " if ok else "BAD"
        print("%s %-10s %-18s %-8s %-9s -> %-18s %-8s %s" % (
            flag, group, name, rank, key, got_name, got_rank, status))
        if not ok:
            bad.append((group, name, rank, key))

print("\n===== 反查候选 =====")
for group, name, rank, key in bad:
    cands = lookup(name, rank)
    print("%-10s %-18s %-8s 写死=%s  候选=%s" % (group, name, rank, key, cands or "无"))
