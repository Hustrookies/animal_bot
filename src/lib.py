#!/usr/bin/env python3
"""共享工具：类群表、生物地理界、相似度、数据读写。

与 wiki-bot 的 lib.py 同源，改动点只有三处：
  1. CATS（历史类目）→ GROUPS（动物类群），并附 GBIF 高阶分类键
  2. REGIONS（行政地理）→ 六大生物地理界，配 GBIF distributions 的映射表
  3. queue.tsv 多一列 scientific_name（学名是本项目的第二去重键）

去重相似度沿用字符二元组，不用 SQLite FTS5：其 trigram 分词器要求检索词 ≥3 字符，
两字中文词（薮猫/蜜獾/棕熊）一律零命中且不报错 —— 会静默失效。
"""
import hashlib, json, os, re

# 部署后脚本就在项目根（/opt/animal），所以默认取脚本所在目录 —— 与 wiki-bot 同构。
# 本地 authoring 时源码在 animal-bot/src/，得上跳一级，否则 data/ 和 .cache/ 会埋进
# src/ 里另建一套。
#
# 这里**不靠「记得设 ANIMAL_ROOT」**：忘了设的失败方式是静默的 —— 脚本在 src/ 下
# 另开一套空的 data/，一声不响，然后你会以为池子是空的。已经踩过一次：wikitext.py
# 在 src/.cache 里白下了 42MB 索引又解压出 207MB 明文，而正确的那份就在隔壁。
# 所以自动认：目录名是 src、且父目录有 SPEC.md，就上跳一级。
def _root():
    env = os.environ.get("ANIMAL_ROOT")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    up = os.path.dirname(here)
    if os.path.basename(here) == "src" and os.path.exists(os.path.join(up, "SPEC.md")):
        return up
    return here


ROOT = _root()

# 半年不重复：183 天。这个数字是用户约束，不是调参空间 ——
# 改小它就等于放弃约束 ①，改大要先确认池子够（见 pick.py --stat 的 capacity 检查）。
WINDOW = int(os.environ.get("WINDOW", "183"))
QUEUE_LOW = int(os.environ.get("QUEUE_LOW", "200"))

# 单类群低水位线。**告警必须按类群判，不能只看全池。**
#
# 断更是单类群事件：某类群掉到 27 条时全池可能还有 220 条，全池阈值 200 不响，
# 而那个类群的星期已经取不出题了。probe/simulate.py --starve amphibia:26 实测：
# 全池 220 > QUEUE_LOW，告警不响，730 天里断更 3 天。
#
# 数字怎么来的：每类群每 7 天出 1 条，同一条要间隔 > WINDOW 天才能复现，所以一轮
# 必须长于窗口 → 下界 WINDOW//7+1 = 27 条。实测 27 条时仍有 1 天无候选（同属冷却
# 吃掉了那一条），28 才全绿。告警线取 30 —— 比违约点高 2 条，留出补池的时间。
GROUP_LOW = int(os.environ.get("GROUP_LOW", "30"))

# ---------- 类群表：ISO 星期 → (slug, 显示名) ----------
GROUPS = {
    1: ("carnivora", "食肉与有蹄"),
    2: ("aves",      "鸟类"),
    3: ("marine",    "海洋动物"),
    4: ("reptilia",  "爬行动物"),
    5: ("amphibia",  "两栖与淡水鱼"),
    6: ("inverts",   "无脊椎动物"),
    7: ("mammalia",  "其他哺乳类"),
}
SLUG2GROUP = {v[0]: (k, v[1]) for k, v in GROUPS.items()}

# ---------- 各类群对应的 GBIF 高阶分类单元 ----------
# (canonicalName, rank, key)。三元组都要，因为 import-gbif.py 会拿 name+rank 回查
# GBIF 校验这个 key 仍然指向同一个类元 —— backbone 会变，静默拉错一整支比报错难查得多。
#
# 实测踩到的坑（2026-08-31，都是静默错误，不是报错）：
#   - backbone 里**没有 Reptilia**。Squamata / Testudines / Crocodylia 在当前
#     backbone 里的 rank 是 CLASS 而不是 ORDER。
#   - /species/match 对高阶名不可靠：Sirenia 会 HIGHERRANK 匹配到 Mammalia(359)，
#     Amphibia / Anura / Proboscidea 直接返回 NONE。所以键写死在这里，用
#     /species/{key} 回查校验，不靠 match。
#   - 类群顺序即抢占顺序：同一物种只会进第一个抢到它的类群（见 import-gbif.py 去重）。
#     marine 排在 carnivora 之前，海豹海狮才不会被 Carnivora 先吃掉。
#   - 这张表里**每个 key 都是回查过的**（probe/resolve-keys.py，27/27 通过）。改这张表
#     必须重跑那个脚本：我第一版凭印象写的三个鳍足类科键全是错的 —— 9703 其实是
#     Felidae、5510 是 Muridae、9787 根本不存在，而 GBIF 不会报错，只会安静地
#     给你拉一整支猫科当海豹。
TAXA = {
    "marine":    [("Cetacea", "ORDER", 733), ("Sirenia", "ORDER", 802),
                  ("Elasmobranchii", "CLASS", 121), ("Phocidae", "FAMILY", 5310),
                  ("Otariidae", "FAMILY", 5309), ("Odobenidae", "FAMILY", 9680)],
    "carnivora": [("Carnivora", "ORDER", 732), ("Artiodactyla", "ORDER", 731),
                  ("Perissodactyla", "ORDER", 795), ("Proboscidea", "ORDER", 799)],
    "aves":      [("Aves", "CLASS", 212)],
    "reptilia":  [("Squamata", "CLASS", 11592253), ("Testudines", "CLASS", 11418114),
                  ("Crocodylia", "CLASS", 11493978)],
    "amphibia":  [("Amphibia", "CLASS", 131), ("Cypriniformes", "ORDER", 1153),
                  ("Siluriformes", "ORDER", 708), ("Salmoniformes", "ORDER", 1313),
                  ("Acipenseriformes", "ORDER", 1103)],
    # inverts 的类元是**挑过的**，不是「无脊椎动物里最大的几个桶」。原来收了
    # Arachnida 和 Insecta，实测中文名覆盖率只有 1% / 3%，而且捞上来的是「粒步甲」
    # 「长毛伪瓢虫」「小狂蛛」这类连爱好者都不认识的物种 —— 占掉每周七分之一的推送
    # 不值得。改成主推头足类（13%，章鱼乌贼）和甲壳类（10%，龙虾蟹），蜻蜓和鳞翅目
    # 排后面补量。Insecta 移除还顺带省掉重复枚举：Odonata 和 Lepidoptera 本来就在它下面。
    "inverts":   [("Cephalopoda", "CLASS", 136), ("Malacostraca", "CLASS", 229),
                  ("Odonata", "ORDER", 789), ("Lepidoptera", "ORDER", 797)],
    "mammalia":  [("Primates", "ORDER", 798), ("Rodentia", "ORDER", 1459),
                  ("Chiroptera", "ORDER", 734), ("Diprotodontia", "ORDER", 1452),
                  ("Lagomorpha", "ORDER", 785), ("Monotremata", "ORDER", 791),
                  ("Peramelemorphia", "ORDER", 794)],
}

# ISO 周数 % 6 → 生物地理界。比行政地理更贴动物分布，也顺手打散「一周全是非洲大兽」。
REGIONS = ["古北界", "新北界", "新热带界", "非洲热带界", "东洋界", "澳新界·海洋"]

# GBIF /species/{key}/distributions 的 locality 串 → 生物地理界。
# 实测该端点只给洲际粒度，但常直接给出界名（"Oriental (Indomalaya)"、"Nearctic"、
# "Afrotropical"）；给不出界名时才落到洲际兜底。顺序即优先级，界名一律排在洲际之前。
# "Global" 之类的无信息值不在表内，天然被忽略。
REALM_RULES = [
    ("Palearctic", "古北界"), ("Palaearctic", "古北界"),
    ("Nearctic", "新北界"),
    ("Neotropic", "新热带界"),
    ("Afrotropic", "非洲热带界"),
    ("Oriental", "东洋界"), ("Indomalaya", "东洋界"),
    ("Australasia", "澳新界·海洋"), ("Oceania", "澳新界·海洋"),
    ("Antarctic", "澳新界·海洋"),
    # ↓ 洲际兜底
    ("Europe", "古北界"), ("Northern Asia", "古北界"), ("Siberia", "古北界"),
    ("North America", "新北界"), ("Middle America", "新北界"),
    ("South America", "新热带界"), ("Caribbean", "新热带界"),
    ("Africa", "非洲热带界"), ("Madagascar", "非洲热带界"),
    ("Southern Asia", "东洋界"), ("Asia-Tropical", "东洋界"),
    ("Australia", "澳新界·海洋"), ("New Zealand", "澳新界·海洋"),
    ("Pacific", "澳新界·海洋"), ("Indian Ocean", "澳新界·海洋"),
    ("Atlantic", "澳新界·海洋"),
]


def realm_of(localities):
    """一组 locality 串 → 生物地理界。无法判定返回 ""。

    广布种会同时命中多界（蜜獾的 distributions 里 Africa / Southern Asia /
    Europe & Northern Asia 都有），按命中次数取最多的一界，平票按 REGIONS 顺序 ——
    必须确定性，否则同一物种每次导入落到不同界，池子的地域配比就不可复现。
    """
    score = {}
    for s in localities:
        s = s or ""
        for pat, realm in REALM_RULES:
            if pat.lower() in s.lower():
                score[realm] = score.get(realm, 0) + 1
                break
    if not score:
        return ""
    return min(score, key=lambda r: (-score[r], REGIONS.index(r)))


THEME_COLOR = {
    "carnivora": "#3b2b1f", "aves": "#1d4e5f", "marine": "#12384f",
    "reptilia": "#3f4a25", "amphibia": "#274b3c", "inverts": "#4a2f47",
    "mammalia": "#4a3a28",
}

# ---------- 相似度 ----------
_PUNCT = re.compile(r"[\s，。、；：！？「」『』《》（）()·,.:;!?\"'\-—…]+")


def bigrams(s):
    """中文字符二元组。单字退化为自身。"""
    s = _PUNCT.sub("", (s or ""))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a, b):
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / len(A | B) if (A | B) else 0.0


def ent_overlap(a, b):
    """实体重叠率，按较短一侧归一。"""
    A, B = {x for x in (a or []) if x}, {x for x in (b or []) if x}
    return len(A & B) / min(len(A), len(B)) if (A and B) else 0.0


def sim(new, old):
    """new/old 均为 dict(title, summary, entities)。返回 0–1。"""
    t = jaccard((new.get("title") or "") + (new.get("summary") or ""),
                (old.get("title") or "") + (old.get("summary") or ""))
    e = ent_overlap(new.get("entities"), old.get("entities"))
    return max(t, e)


HARD = 0.60   # ≥ 自动跳过，不消耗当天的推送
SOFT = 0.34   # ≥ 交给模型判定 / 避免内容重叠

# 同属物种天然高相似（Panthera tigris / Panthera leo），但它们是完全不同的两个条目。
# 所以属名不参与相似度，只有「同属 + 同一天」才需要人管，靠 GENUS_GAP 拉开。
GENUS_GAP = int(os.environ.get("GENUS_GAP", "30"))   # 同属最小间隔（天）


# ---------- 闸门判据（§5.2 / §5.3）----------
# 这一节是闸门的**唯一实现**。taxon-check.py 和 refine-candidates.py 都从这里取，
# 不许各写一份 —— 两份判据迟早会分叉，而分叉的那天池子里就会混进不该有的东西。

# 学名格式：双名或三名法。留着三名不是为了收亚种（用户已定只做物种级），是因为 GBIF
# 偶尔把 "Bos taurus indicus" 这类三名当 SPECIES 返回，格式校验不该把它判成脏数据。
#
# 种加词允许一个连字符：动物命名法里这是合法的，实测 Polygonia c-album（白钩蛱蝶，
# 因翅上 C 形白斑得名）就被第一版正则误杀了 —— 一条真物种，而且只丢一条，
# 不专门去数 rejected.tsv 根本发现不了。同类还有 c-aureum、t-nigrum。
SCI_RE = re.compile(r"^[A-Z][a-z]+ [a-z]+(-[a-z]+)?( [a-z]+(-[a-z]+)?)?$")


# 中文名：全 CJK，2–8 字。全 CJK 这一条是必须的 —— GBIF 的 zho 俗名里混着拼音
# （Panthera tigris 只有 "Lǎohǔ"、Neofelis 有 "Yun Bao"），不过滤会把拼音当中文名。
ZH_RE = re.compile(r"^[\u4e00-\u9fff]{2,8}$")

RANK_OK = {"SPECIES", "SUBSPECIES"}

# 分类层级名后缀。GBIF 的 zho 俗名里混着它们 —— Odobenus rosmarus 的候选里就有
# 「海象属」。属级/科级条目直接违反约束 ③，而统称检测抓不到它：只有一个物种挂着
# 这个名字，共用计数是 1。
RANK_SUFFIX = ("属", "科", "目", "纲", "门", "族", "亚种", "类")

# 家养种名录。**键是完整学名，不是种加词。**
#
# SPEC 原来写的是「学名不含 familiaris / domesticus / taurus 等家养种加词」。那是错的，
# 而且错法和黑名单子串匹配一模一样。实测按种加词子串匹配会误杀四条真野生物种：
#     Carcharias taurus       沙虎鲨        种加词真的就是 taurus
#     Campylopterus falcatus  棕尾刀翅蜂鸟   falcatus 里含 "catus"
#     Tetrao urogallus        松鸡          urogallus 里含 "gallus"
#     Sus scrofa              野猪          家猪是 Sus domesticus，scrofa 是野生种
# 所以只认全串相等。代价是名录得人工枚举、会漏；收益是不会静默误杀 —— 漏一个野生动物
# 进不了池没人看得出来，而把沙虎鲨当家畜删掉是没法从结果里发现的。
#
# 收录标准是「读者会把它当家畜家禽」，不是「分类学上有过驯化史」。按这个标准：
#   收   Bubalus bubalis（水牛）      野生的是 Bubalus arnee
#   收   Lama glama（大羊驼）         野生的是 Lama guanicoe 原驼
#   收   Numida meleagris / Coturnix japonica
#        非洲和东亚确有野生种群，但中文名读起来就是家禽，占掉一天的推送不值当
#   不收 Oryctolagus cuniculus（穴兔）它就是野生欧洲兔本种，IUCN 濒危，是个好选题
#   不收 Rangifer tarandus（驯鹿）、Anas platyrhynchos（绿头鸭）、Columba livia（原鸽）、
#        Gallus gallus（红原鸡）、Meleagris gallopavo（野火鸡）、Sus scrofa（野猪）
#        本种都是野生的，被驯化的是它们的亚种 —— 只做物种级就天然拿到野生那一支
DOMESTIC = {
    "Felis catus", "Canis familiaris", "Canis lupus familiaris",
    "Bos taurus", "Bos indicus", "Bos frontalis", "Bos grunniens",
    "Capra hircus", "Ovis aries", "Bubalus bubalis",
    "Equus caballus", "Equus asinus", "Sus domesticus",
    "Camelus dromedarius", "Camelus bactrianus",
    "Lama glama", "Vicugna pacos",
    "Cavia porcellus", "Mustela putorius furo", "Bombyx mori",
    "Gallus gallus domesticus", "Anser anser domesticus",
    "Anas platyrhynchos domesticus", "Columba livia domestica",
    "Numida meleagris", "Coturnix japonica",
}

# 人属。**闸门的第七个漏洞，而且这次不是判据写错，是判据集里少了一条。**
#
# `Homo sapiens` 把四道闸门**每一道都合法走完了**：二名法格式对、rank=SPECIES、
# order=Primates 在 TAXA 里、不是家养种、不是统称、zhwiki「智人」正文里确有学名。
# 于是它以「靠一身汗腺把猎物活活跑垮的猿」进了 queue.tsv，是阶段 5 逐条看锚时
# 才发现的（`[212/224] 智人 ok 803 字`）。
#
# 教训：闸门是**枚举**出来的，枚举就会漏。「不是读者自己」这条约束从来没人写下来过，
# 因为它太显然了 —— 显然到没进 SPEC。所以每一批产出都得有一次人眼扫过去。
#
# 按属排除而不是按学名：人属其他物种（尼安德特人等）眼下靠 route=extant 挡着，
# 但那是运气，不是判据。
EXCLUDE_GENUS = {"Homo"}


def taxon_verdict(row):
    """单条候选的分类闸门。返回 (ok, reason)，reason 在 ok 时为 ""。

    只做**离线且单行可判**的四条：学名格式、rank、家养种、排除属（人属）。其余三条判据
    都不是单行能判的，不在这里：
      - 统称检测要看全池（一个名字被几个物种共用）
      - zhwiki 存在性要扫 216MB 索引，得批量做
      - 池内不重复要读 queue.tsv
    见 refine-candidates.py。
    """
    sci = (row.get("sci") or row.get("scientific_name") or "").strip()
    if not SCI_RE.match(sci):
        return False, "bad-sci"
    if (row.get("rank") or "") not in RANK_OK:
        return False, "bad-rank"
    if sci in DOMESTIC:
        return False, "domestic"
    # 属名取 sci 的第一段而不是 row["genus"] —— 后者不是每个调用方都填。
    # SCI_RE 已经过了，所以 split 一定有第一段。
    if sci.split()[0] in EXCLUDE_GENUS:
        return False, "excluded-genus"
    return True, ""


def zh_verdict(zh, generic=(), black=()):
    """单个中文名的闸门。返回 (ok, reason)。

    black 必须是**已经减去 whitelist 的**集合，且这里只做全串相等 —— 子串匹配下
    「老虎」会连带干掉「东北虎」「孟加拉虎」，把最该收的条目全打掉。
    """
    zh = (zh or "").strip()
    if not ZH_RE.match(zh):
        return False, "bad-zh"
    if zh.endswith(RANK_SUFFIX):
        return False, "rank-name"
    if zh in black:
        return False, "blacklist"
    if zh in generic:
        return False, "generic"
    return True, ""


def load_wordlist(name):
    """读 data/<name>：一行一词，# 开头及行内 # 之后是注释。文件不存在返回空集。"""
    p = os.path.join(ROOT, "data", name)
    if not os.path.exists(p):
        return set()
    out = set()
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.split("#", 1)[0].strip()
            if ln:
                out.add(ln)
    return out


# ---------- 数据读写 ----------
def jsonl_path():       return os.path.join(ROOT, "data", "posts.jsonl")
def queue_path():       return os.path.join(ROOT, "data", "queue.tsv")
def candidates_path():  return os.path.join(ROOT, "data", "candidates.jsonl")
def pool_path():        return os.path.join(ROOT, "data", "pool.jsonl")

# 待入队清单：过完四道闸门（含事实锚终选）、subject 已定死、只缺 agent 补的
# title/note。build-queue.py 产出，refill 循环消费，refill-check.py 拿它验
# agent 有没有偷改 subject。
def ready_path():       return os.path.join(ROOT, "data", "ready.jsonl")
def index_path():
    """zhwiki multistream 索引明文。与 wiki-bot 硬链接同一份（216MB，不重复下载）。"""
    return os.environ.get(
        "ZH_INDEX",
        os.path.join(ROOT, ".cache", "zhwiki-latest-pages-articles-multistream-index.txt"))


def load_posts():
    """读 data/posts.jsonl（真相）。按 (date,subject) 去重。"""
    p, seen, out = jsonl_path(), set(), []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            k = (d.get("date"), d.get("subject"))
            if k in seen:
                continue
            seen.add(k)
            out.append(d)
    return out


QUEUE_COLS = ["group", "region", "title", "subject", "scientific_name",
              "entities", "note", "wiki"]


def load_queue():
    """读 data/queue.tsv。8 列，见 QUEUE_COLS。

    比 wiki-bot 多一列 scientific_name：中文名可以有异名（美洲狮/山狮/美洲金猫），
    学名才是物种的唯一身份。半年去重两个键都查（见 selfcheck.py）。
    """
    p, out = queue_path(), []
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for i, ln in enumerate(f):
            ln = ln.rstrip("\n")
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            c = ln.split("\t")
            if len(c) < 5:
                continue
            c += [""] * (len(QUEUE_COLS) - len(c))
            subject = c[3].strip()
            ents = [x.strip() for x in c[5].split("|") if x.strip()]
            # 结构性保证 subject ∈ entities：实体重叠是去重主信号，subject 缺席会
            # 直接削弱去重，而且 agent 会照抄这一对。
            if subject and subject not in ents:
                ents.insert(0, subject)
            out.append({
                "row": i + 1, "group": c[0].strip(), "region": c[1].strip(),
                "title": c[2].strip(), "subject": subject,
                "scientific_name": c[4].strip(), "entities": ents,
                "note": c[6].strip(),
                # 第 8 列：维基条目真实标题，与 subject 分开。两者要求相反 ——
                # subject 要专指（去重键），wiki 要能匹配到条目名。留空则回退用 subject。
                "wiki": c[7].strip(),
            })
    return out


def genus_of(sci):
    """学名 → 属名。'Panthera tigris tigris' → 'Panthera'。"""
    return (sci or "").strip().split(" ")[0]


def zh_index_titles(wanted):
    """一次扫过 216MB 索引，返回 wanted 中确实存在 zhwiki 条目的名字集合。

    单次全表扫描 3.9s（实测 494 万行）。不用 grep -F -f：那样没法锚定「标题整串
    相等」，"虎" 会命中 "虎鲸"、"虎皮鹦鹉"，闸门就废了。
    """
    want, hit = set(wanted), set()
    p = index_path()
    if not want or not os.path.exists(p):
        return hit
    with open(p, encoding="utf-8", errors="replace") as f:
        for ln in f:
            # 行格式 offset:pageid:title，title 本身可能含冒号（"Wikipedia:..."）
            parts = ln.rstrip("\n").split(":", 2)
            if len(parts) == 3 and parts[2] in want:
                hit.add(parts[2])
                if len(hit) == len(want):
                    break
    return hit


def buildid(date, content_bytes):
    """内容派生、无 nonce —— 否则同一份内容重渲染字节不同，空 commit 跳过就永远不触发。"""
    return f"{date}-{hashlib.sha1(content_bytes).hexdigest()[:8]}"


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
