# animal-bot 构建规格

每日定时推送一种**具体野生动物**的介绍（分布区域 + 生活习性 + AI 配图），照 wiki-bot
的构造做：0 token 的状态机 + 单点花 token 的 agent + 机械闸门验收。

本规格中所有"实测"结论均在目标机器上验证过，日期 2026-08-31。

---

## 0. 三条用户约束如何落地

| 约束 | 落地手段 | 能不能机械保证 |
|---|---|---|
| ① 半年无重复词条 | `WINDOW=183` 天去重窗口 + 池子 ≥220 条 + LRU 取题 + selfcheck 二次硬校验 | **能**，且是算术保证 |
| ② 主要是具体野生动物 | GBIF `rank ∈ {SPECIES, SUBSPECIES}` + 排除家养名录 | **能** |
| ③ 不是一类动物的宽泛介绍 | GBIF `rank` 挡科属级 + 中文统称黑名单 | **不能完全**，见 §5.3 |

约束 ③ 必须先说清能力边界，否则后面全是错觉：

实测 GBIF `numDescendants`（下级分类单元数）——

```
Panthera tigris   (老虎)    rank=SPECIES  numDescendants=10   ← 要排除
Panthera leo      (狮子)    rank=SPECIES  numDescendants=10   ← 要排除
Panthera onca     (美洲豹)  rank=SPECIES  numDescendants=10
Puma concolor     (美洲狮)  rank=SPECIES  numDescendants=7    ← 用户明确要的正例
Felidae           (猫科)    rank=FAMILY   numDescendants=596  ← 要排除
```

**"老虎"和"美洲狮"在任何单一数值判据上都落在同一区间。** 二者都是 ACCEPTED SPECIES，
都有多个下级亚种，差别只在中文语境里"老虎"是个统称而"美洲狮"是个专名——这是语言事实，
不是分类学事实，任何 taxonomy API 都答不出来。

更进一步，`numDescendants` **连当告警信号都不合格**。实测抽样食肉目 300 条 ACCEPTED
SPECIES，`numDescendants ≥ 8` 的 34 条（11.3%）里是这些——

```
desc=20  Leptailurus serval          薮猫        desc=18  Ursus americanus   美洲黑熊
desc=17  Ursus arctos                棕熊        desc=15  Prionailurus bengalensis  豹猫
desc=13  Lutra lutra                 欧亚水獭    desc=13  Lynx rufus         短尾猫
desc=12  Mellivora capensis          蜜獾        desc=17  Paguma larvata     果子狸
```

全是完全合格的具体物种，而且恰恰是最有意思的那批选题。`numDescendants` 高只反映
"分类学家给这个种划了很多亚种"，与中文名是否统称无关。**11.3% 的误报会让人工确认清单
被正例填满，人很快对清单脱敏——那比没有闸门更糟。所以本规格完全不使用该字段做判定。**

所以 ③ 的判定只有两条：**GBIF `rank` 挡住科属级（可靠、机械）+ 中文统称黑名单
（唯一能识别"统称"的判据，人工增量维护）**。好在中文动物统称是可枚举的有限集合
（几十个词，见 §5.3），黑名单反而比任何启发式都可靠。

---

## 1. 实测前提：网络可达性

新项目的数据源选择完全由这张表决定，改动前必须重测。

| 数据源 | 实测结果 | 在本项目中的角色 |
|---|---|---|
| `api.gbif.org` | ✅ 200 / 1.5s | **选题池唯一权威来源**：学名、rank、分类树、分布、**中文俗名**、**IUCN 等级** |
| `dumps.wikimedia.org` | ✅ 200 | 事实锚（multistream index + HTTP Range 取单条正文） |
| 本地 zhwiki 索引 | ✅ 已缓存 216MB 明文 | 中文名存在性闸门，单遍扫 494 万行 3.9s |
| `wikidata.org` API / SPARQL | ❌ **超时不可达** | 不可用。原计划的 taxon rank 校验改用 GBIF |
| `zh.wikipedia.org` REST API | ❌ 超时不可达 | 不可用 |
| `apiv3.iucnredlist.org` | ⚠️ 403 需注册 token，且 v3 已废弃 | **不依赖**，也不需要 —— GBIF 转发 IUCN 等级 |

两条硬结论：

- **不要在运行时链路里放任何 wikidata / zhwiki API 调用**，它们在这台机器上是死的。
- IUCN 全球等级取自 GBIF 的 `/v1/species/{key}/iucnRedListCategory`（单值 `category`，
  且回一个 `scientificName` 可与 key 对校）。**一物种一请求**，所以只对真正入队的条目查，
  不对全池查。

**关于 IUCN 等级，有一个必须守住的坑**：`species/search` 内联的 `threatStatuses`
**不能用来标注等级**。它把全球评估和各区域评估混在同一个数组里 ——
`Phocoena phocoena`（鼠海豚）有 7 个值，`[0]` 是 `CRITICALLY_ENDANGERED`（波罗的海种群），
而它的全球等级是 `LEAST_CONCERN`。照数组标就是在页面上写事实错误。
该字段只有一个正当用途：**当 `threat=` 查询参数用于枚举**，把化石类元筛掉（见 §6.1）。

**三条被实测推翻的早期判断**（原文已改，留档以免有人照着旧结论改回去）：

- ~~"GBIF 给不出中文名"~~ —— 错。GBIF **有** `language=zho` 的俗名。当初的测法有
  两个 bug：`vernacularNames` 端点默认 limit 只取前 100 条，而 `Puma concolor` 有 229
  条中文名排在后面；且抽样落在了化石类元上。修正后各类群实测覆盖率见 §6.1。
- ~~"IUCN 等级只能由 agent 从锚文提取"~~ —— 不必，走上面那个端点。
- ~~"内联 `threatStatuses` 可以直接当等级用"~~ —— 这是我自己在上一轮引入的错误，
  见上。教训是 GBIF 的数组字段几乎都是多来源汇总，取 `[0]` 从来不安全。

---

## 2. 与 wiki-bot 的关系

独立目录 `/opt/animal` + 独立 GitHub 仓库（独立 Pages）。共用宿主机与微信通道，但
**锁文件、cron 窗口、state 目录、git 工作树全部隔离**——一边挂了不影响另一边。

### 直接复用（改常量即可）

| 文件 | 复用方式 |
|---|---|
| `run.sh` | 状态机骨架、stage 分流、alert 限流、flock、日志轮转全部照搬。锁改 `/tmp/animal-bot.lock` |
| `lib.py` | `bigrams`/`jaccard`/`ent_overlap`/`sim` 去重算法原样；`CATS`/`REGIONS`/`THEME_COLOR` 换表 |
| `pick.py` | **含刚做的 LRU + 随机扰动取题**（见 §4.2）。`WINDOW` 100→183 |
| `gen-image.py` | 整体复用，`STYLE` 换表，`call_model` 完全不动（同一 endpoint、同一 `IMG_API_KEY`） |
| `render.py` / `template.html` | 换模板与字段；「data/content/*.json 可 0 token 重渲全站」的性质保留 |
| `publish.sh` / `notify.sh` / `wait_live.sh` / `check-net.sh` | 只改 `PAGE_BASE` 与提交白名单 |
| `selfcheck.py` | 检查项换成动物 schema，**新增半年重复硬闸门**（§5.1） |
| `fetch-material.py` | 索引与 Range 取正文的机制原样，`subject` 来源换成物种中文名 |

### 有意不复用

**不做 motif 轮转。** wiki-bot 每个类目有专属版式字段（`timeline`/`span`/`layers`…），
8-31 整期停更就是因为 agent 把 `timeline` 包进了 prompt 里一个伪注释键。动物条目每天
的信息结构完全相同（分布 + 习性），改用一个固定的 `profile` 结构化卡片即可。
**主动砍掉一个复杂度来源，连带消灭了那整类故障。**

### 新增

| 文件 | 职责 |
|---|---|
| `import-gbif.py` | 从 GBIF 拉候选物种 → 生成 `data/candidates.jsonl`（一次性 + 月度增量） |
| `taxon-check.py` | 三重闸门验收，产出 `queue.add.tsv` 与 `data/review-needed.tsv` |
| `data/blacklist.txt` | 中文统称黑名单，全串精确匹配，人工增量维护 |
| `data/whitelist.txt` | 黑名单误伤时的放行例外（如"美洲狮"）|

---

## 3. 容量算术（约束 ① 的硬前提）

半年不重复 = 去重窗口 183 天，每天消耗 1 条：

```
第 184 天时，窗口内已用 183 条 → 可用候选 = 池子总数 − 183
池子 = 183 条 → 候选 0 条 → 断供
```

所以：

- **池子下限 184 条，工程目标 220 条**（留 20% 余量吸收闸门丢弃与 refill 延迟）
- 低水位告警阈值 `QUEUE_LOW = 200`（全池），`GROUP_LOW = 30`（**单类群，真正管用的那个**）
- 7 个类群轮转，每类群目标 **≥32 条**（32×7 = 224）

"下限 184 条"这个算法（183 天 + 1）是**错的**，它假设一个 183 天窗口里全池随便取。
实际排班是「ISO 星期 → 类群」，所以约束落在**每个类群**上：一轮必须长于窗口 →
每类群 ≥ `WINDOW//7 + 1 = 27` 条，实测下界 28（§9.2）。全池安全线 28×7 = **196 条**。
184 这个数字既比 196 松、又给不出该往哪个类群补 —— §9.3 实测过它漏掉单类群饥饿。

wiki-bot 靠 7 类目 × 100 天窗口，157 条就够；本项目窗口几乎翻倍，池子必须一次性建到位。
这是 `import-gbif.py` 存在的唯一理由——靠 agent 每批 8 条攒 220 条要 28 批。

---

## 4. 排班与取题

### 4.1 类群轮转（ISO 星期）

替代 wiki-bot 的历史类目。按 GBIF 分类阶元切，保证每类群都有充足物种，且**天然防止
一周七天全是猫科**：

| ISO 星期 | 类群 slug | 显示名 | GBIF 分类阶元（`lib.TAXA` 实际取值） |
|---|---|---|---|
| 1 | `carnivora` | 食肉与有蹄 | order: Carnivora, Artiodactyla, Perissodactyla, Proboscidea |
| 2 | `aves` | 鸟类 | class: Aves |
| 3 | `marine` | 海洋动物 | order: Cetacea, Sirenia；class: Elasmobranchii；family: Phocidae, Otariidae, Odobenidae |
| 4 | `reptilia` | 爬行动物 | Squamata, Testudines, Crocodylia |
| 5 | `amphibia` | 两栖与淡水鱼 | class: Amphibia；order: Cypriniformes, Siluriformes, Salmoniformes, Acipenseriformes |
| 6 | `inverts` | 无脊椎动物 | class: Cephalopoda, Malacostraca；order: Odonata, Lepidoptera |
| 7 | `mammalia` | 其他哺乳类 | order: Primates, Rodentia, Chiroptera, Diprotodontia, Lagomorpha, Monotremata, Peramelemorphia |

这张表的初版写的是 `habitat=MARINE`、`habitat=FRESHWATER` 这类**条件式**过滤，实现时
换成了**枚举分类阶元 + GBIF taxonKey**：GBIF 的 occurrence 端点才有 habitat 参数，
species 端点没有，按 habitat 筛得先取回全部再本地过滤 —— 而分类阶元一步就能定位。

两处偏离要记住：

- **`inverts` 不含 Insecta 全纲、也不含 Arachnida。** 初版是"昆虫与无脊椎"。昆虫纲有
  上百万物种、绝大多数没有中文名也没有 zhwiki 条目，全纲进来只会让候选池被无名甲虫
  淹掉；蛛形纲同理。改成头足（章鱼、乌贼、船蛸）+ 甲壳（龙虾、螯虾）为主，昆虫只取
  蜻蜓目和鳞翅目 —— 这两个目的常见种有中文名、有条目、也确实有可写的行为。
- **`amphibia` 名不副实**：slug 叫两栖，实际含四个淡水鱼目（鲤形、鲇形、鲑形、鲟形）。
  实测入选的 40 条里有扁吻鱼、大西洋鲑、胭脂鱼这类鱼。**这是设计如此**，显示名"两栖
  与淡水鱼"是准的，slug 留着不改（改 slug 会让已发布的 `posts.jsonl` 对不上）。


### 4.2 生物地理区轮转（ISO 周数 % 6）

沿用 wiki-bot 的 6 位轮转结构，但换成**生物地理界**——比行政地理更贴动物分布：

```python
REGIONS = ["古北界", "新北界", "新热带界", "非洲热带界", "东洋界", "澳新界·海洋"]
```

### 4.3 取题算法

直接用 wiki-bot 刚改好的 `pick.py`（commit `9d9fa02`）：排序键
`(地域不匹配, -(未出场天数 + 日期种子扰动))`。该改动在 wiki-bot 上已验证：

- 365 天覆盖率 127/157 → **157/157，死库存 0**
- 同一天重跑结果一致（四个 cron 补跑窗口不会换题）
- `--skip` 重取时其余候选次序不变（逐候选独立取种子）

本项目 `JITTER` 仍取 45 天，`WINDOW` 改 183。建池完成后须重跑一次 365 天模拟，
确认**最小复现间隔 > 183 天**——这是约束 ① 的验收证据，见 §9。

---

## 5. 三层闸门

### 5.1 selfcheck.py：半年重复硬闸门（新增，约束 ①）

不只依赖 pick.py 不选重复——发布前再查一次，命中直接 FAIL 拒绝渲染：

```
若 content.json 的 subject 或 scientific_name
   在 data/posts.jsonl 中最近 183 天内出现过 → FAIL
```

理由：pick 与 publish 之间隔着 agent、配图、渲染多步，任何一步的人工干预（比如手工
改 content.json 换主题）都可能绕过 pick 的去重。约束 ① 是用户明确要的，值得两道锁。

### 5.2 taxon-check.py：格式与分类闸门（约束 ②）

逐行验收 `candidates.jsonl`，任一条不过就丢弃该行（不中止整批，照 wiki-bot
`refill-check.py` 的做法）：

1. **学名格式**：`^[A-Z][a-z]+ [a-z]+(-[a-z]+)?( [a-z]+(-[a-z]+)?)?$`（双名或三名法）。
   **种加词必须允许一个连字符** —— 动物命名法里合法，实测 `Polygonia c-album`
   （白钩蛱蝶，因翅上 C 形白斑得名）就被不带连字符的第一版正则误杀了。这类误杀只丢一条，
   不专门去数 `rejected.tsv` 是发现不了的。
2. **GBIF rank**：必须 ∈ {`SPECIES`, `SUBSPECIES`}。`FAMILY`/`GENUS`/`ORDER` 一律拒
3. **GBIF taxonomicStatus**：必须 `ACCEPTED`。这一条在 `import-gbif.py` 的查询里用
   `status=ACCEPTED` 做服务端过滤，不在本地重判（该字段没落盘）。
   已抽 30 条池内物种回查 `/v1/species/{key}`：`taxonomicStatus` / `canonicalName` /
   `rank` 三项全部相符，0 条不符 —— 服务端过滤可信，不必为这一条多存一个字段。

4. **野生**：学名**全串**不在家养种名录（`lib.DOMESTIC`）里。

   **这一条原来写的是"学名不含 `familiaris`/`domesticus`/`taurus` 等家养种加词"，那是错的**，
   错法和黑名单子串匹配一模一样。按种加词子串匹配会误杀四条真野生物种：

   | 学名 | 中文名 | 为什么被误杀 |
   |---|---|---|
   | *Carcharias taurus* | 沙虎鲨 | 种加词真的就是 `taurus` |
   | *Campylopterus falcatus* | 棕尾刀翅蜂鸟 | `falcatus` 里含 `catus` |
   | *Tetrao urogallus* | 松鸡 | `urogallus` 里含 `gallus` |
   | *Sus scrofa* | 野猪 | 家猪是 *Sus domesticus*，`scrofa` 是野生种 |

   所以只认**完整学名相等**。代价是名录要人工枚举、会漏；收益是不会静默误杀 ——
   漏一个野生动物进不了池没人看得出来，而把沙虎鲨当家畜删掉是没法从结果里发现的。

   收录标准是**"读者会把它当家畜家禽"**，不是"分类学上有过驯化史"。实测从池子里剔掉
   12 条：家猫、家牛、家山羊、绵羊、水牛、大羊驼、单峰骆驼、岬马、珠鸡、日本鹌鹑等。
   *Oryctolagus cuniculus*（穴兔，IUCN 濒危）、*Rangifer tarandus*（驯鹿）、
   *Anas platyrhynchos*（绿头鸭）**不收** —— 本种都是野生的，被驯化的是它们的亚种，
   只做物种级就天然拿到野生那一支。
5. **中文名可得**：中文名必须在本地 zhwiki 索引中命中（整串相等，不能子串）——
   这一条同时保证了**事实锚一定抓得到**，把 wiki-bot 上"锚文覆盖率只有 24%"那类问题
   前移到建池阶段解决
6. **池内不重复**：中文名与学名都不得与 `queue.tsv` 已有行冲突

前 4 条是**单行离线可判**的，实现在 `lib.taxon_verdict()` / `lib.zh_verdict()`；
后 2 条要么要扫 216MB 索引、要么要读全池，实现在 `refine-candidates.py`。
**判据只有这一份实现**，`taxon-check.py` 和 `refine-candidates.py` 都从 `lib.py` 取 ——
两处各写一份迟早会分叉，而分叉那天池子里就会混进不该有的东西。

`taxon-check.py --selftest` 跑一张 19 条的用例表，退出码非 0 即有用例挂了。表里除了
§12 要求的 8 条，还钉着上面那四个误杀、`Polygonia c-album`、"海象属"、拼音俗名、
渔业统称"沙条"，以及 whitelist 豁免链路本身（whitelist 空的时候没有任何用例会经过它）。
**用例断言的是"拒因"而不只是"拒了没"** —— 否则"老虎"被格式校验拒掉也算通过，那是巧合
不是黑名单在工作，下次改动就静默失效了。

### 5.3 宽泛条目闸门（约束 ③）

按 §0 的实测结论，只有两条硬判据，都是 FAIL/PASS 的二值判定，没有中间档：

| 判据 | 动作 |
|---|---|
| GBIF `rank` ∉ {`SPECIES`, `SUBSPECIES`} | **FAIL** 丢弃（科属目级，§5.2 第 2 条已挡） |
| 中文名**全串精确等于** `data/blacklist.txt` 中某词，且不在 `whitelist.txt` | **FAIL** 丢弃 |
| 其余 | **通过** |

两条实现上的硬要求：

- **黑名单必须全串精确匹配，不能子串匹配。** 子串匹配下"老虎"会连带干掉"东北虎"
  "孟加拉虎"，把最该收的条目全打掉。同理黑名单里**不得收单字词**（虎、熊、狼）。
- **不能用"中文名字数 ≤2"当判据。** 薮猫、蜜獾、棕熊、豹猫、兔狲都是 2 字的具体物种。
- **不使用 `numDescendants`**，理由见 §0 的 300 条抽样数据。

`blacklist.txt` 的收录规则（凡"一个中文词覆盖多个物种"的统称），以及三条**实测定下来的
偏离**——照初始名单字面抄会出问题：

**① 单字词一个都不收。** 初始名单里的 `虎 熊 狼 豹 鹿 猴 猿 鹰 隼 雕 鸮 鹤 鲸 龟 蛇 蛙 虾`
全部剔除。匹配是全串相等，而中文名闸门要求 ≥2 字（`lib.ZH_RE`），单字名根本进不到这一步 ——
收进来纯是死条目，还会让人误以为这张表在做子串匹配。

**② `猎豹`、`河马`、`长颈鹿` 不收。** 实测这三个词在候选池里**各自只有一个持有者**
（*Acinonyx jubatus* / *Hippopotamus amphibius* / *Giraffa camelopardalis*）——
它们是单型属的正名，不是伞状词。照名单字面收进来就是删掉三个好选题。

**③ `老虎`、`狮子` 必须收，理由和别的词不同。** 这两个字面上都是**单一物种**的俗名
（老虎 = *Panthera tigris*，狮子 = *Panthera leo*），按"多物种共用"的标准统称检测
永远抓不到。收它们的唯一依据是约束 ③ 直接点了名。

而用户同时把"美洲狮"列为**正例**，它也是物种级 —— 所以**约束 ③ 不是按分类阶元划的，
是按"中文名读起来指不指一个具体动物"划的**。老虎/狮子在中文语境里是伞状词，美洲狮不是。

> 现在 *Panthera tigris* 恰好进不了池：GBIF 给它的 zho 俗名只有拼音 `Lǎohǔ`，被
> `ZH_RE` 挡掉了。**但那是运气不是保证** —— GBIF 哪天补一条"老虎"进去，它就直接走进池子。
> 黑名单这两行是那种情况下唯一的拦阻。

实际收录（54 词）：

```
# 用户点名        老虎 狮子 狐狸 羚羊 天鹅 企鹅 大象 犀牛 斑马
# 哺乳类高阶统称   猫科 犬科 熊科 鼬科 灵长类 有蹄类 啮齿类 鲸类 鲸鱼 海豚 海豹 蝙蝠
# 鸟类高阶统称     猛禽 涉禽 水禽 鸣禽 鹦鹉 蜂鸟 海鸟
# 爬行与两栖       蜥蜴 毒蛇 蟒蛇 海龟 乌龟 陆龟 鳄鱼 青蛙 蟾蜍
# 鱼类与海洋       鲨鱼 鳐鱼 魔鬼鱼 鲤鱼 鲇鱼 鲟鱼
# 无脊椎           蝴蝶 飞蛾 甲虫 蜘蛛 螃蟹 龙虾 乌贼 章鱼 蜻蜓 蚂蚁 蜜蜂
```

`whitelist.txt` 目前**为空**。"美洲狮"不需要豁免 —— 它压根不在黑名单里。豁免链路由
`taxon-check.py --selftest` 现场造一次来验（拿黑名单里的真词减一次），否则这条路径
在 whitelist 空着的时候没有任何用例经过。

`data/rejected.tsv` 收**被闸门拒掉的行**（不是待审清单），供人工翻查有无误伤；
确认误伤就把该名补进 `whitelist.txt`，下次 refill 自动放行。当前 1508 行，拒因只有三类：
`no-wiki` 1474（中文名在 zhwiki 没有同名条目）、`all-generic` 22、`domestic` 12。

**这一层会漏放。** 黑名单是枚举，没收进去的统称会溜进池子。运维姿态是：
发现一个补一个，改 `blacklist.txt` 一行即生效，不需要改代码。

---

## 6. 选题池构建

### 6.1 `import-gbif.py`（一次性 + 月度增量）

枚举条件是实测定下来的，每一条都有对应的失败案例。**改这一节前先跑 `probe/enum-filter.py`。**

```
对 7 个类群各拉 TARGET 条候选（默认 400，实际只需 32）：
  GET /v1/species/search?highertaxonKey=<类元 key>&rank=SPECIES&status=ACCEPTED
      &datasetKey=<GBIF Backbone>&limit=300&offset=...
      + 两路过滤各跑一遍：  isExtinct=false   |   threat=<IUCN 六级>
  → 取 canonicalName / rank / key / family / genus / 内联 vernacularNames
  → 落 data/candidates.jsonl（每条带 zh_all = 该物种全部可用中文名）
```

**必须过滤化石类元。** 不加过滤的 `rank=SPECIES&status=ACCEPTED` 拉到的食肉目有
1736 条，绝大多数是 Amphicyonidae、Barbourofelidae、Ginsburgsmilus 这些已灭绝科 ——
一个中文名都没有。存量看着充足其实是假的。

**过滤要走两路并集，单用任何一路都严重欠收：**

| 类元 | `isExtinct=false` | `threat=<IUCN 六级>` |
|---|---|---|
| Carnivora | 342 | 262 |
| Squamata | 687 | 9775 |
| Elasmobranchii | 71 | 1150 |
| Cypriniformes | 34 | 3352 |
| Aves | 10688 | 9938 |

`isExtinct=false` 漏掉该字段为空的（鱼类、爬行类大面积为空）；`threat=` 漏掉未被 IUCN
评估的。两路都跑，按 `speciesKey` 并集去重。`threat=` 可一次传多值，语义是 OR
（实测 VU=63、VU+EN=92）。

**类元键写死在 `lib.py` 的 `TAXA` 里，且必须回查校验**（`probe/resolve-keys.py`，
当前 27/27 通过）。理由：`/species/match` 对高阶名会静默出错 —— `Sirenia` 会
`HIGHERRANK` 匹配到 `Mammalia`，`Reptilia` 在当前 backbone 里干脆不存在
（`Squamata`/`Testudines`/`Crocodylia` 的 rank 是 **CLASS** 不是 ORDER）。
我第一版凭印象写的三个鳍足类科键全是错的，GBIF 不报错，只会安静地拉一整支猫科当海豹。

**类群顺序即抢占顺序。** 同一物种只进第一个抢到它的类群，顺序由 `TAXA` 的键顺序决定，
**不是** `GROUPS` 的星期顺序。`marine` 必须排在 `carnivora` 之前 —— 鳍足类在分类上就在
食肉目之下，顺序错了 marine 一个海豹都拿不到（实测被抢走 14 个：海象、豹海豹、斑海豹…）。

**中文名从内联 `vernacularNames` 取**，不走 `/species/{key}/vernacularNames` 端点：
内联与端点在 `zho` 上 19/20 一致，但内联是 1 请求 300 个物种。
GBIF 的 zho 俗名常是繁体（`藪貓`/`大熊貓`）或拼音（`Lǎohǔ`），所以要先 zhconv 转简，
再用 `^[\u4e00-\u9fff]{2,8}$` 过滤。各类群实测覆盖率（extant 集合）：

| 类群 | 覆盖率 | 类群 | 覆盖率 |
|---|---|---|---|
| aves | 81% | mammalia | 3–50% |
| carnivora | 61–100% | amphibia | 4–82% |
| reptilia | 36–91% | inverts | 1–13% |
| marine | 19–50% | | |

`inverts` 命中率最低（Lepidoptera / Arachnida 仅 1%），但已评估基数大
（Insecta 24853 条），翻页仍能凑够 400 条 —— 只是慢。`MAX_PAGES` 存在的唯一理由就是
防止它为了凑数把 60 万条虫子全翻一遍。

**`import-gbif.py` 不定中文名。** 它把候选全存进 `zh_all`，定名交给 §6.1b —— 统称
只能在全局识别，理由见下。

字段清单里**故意没有 `numDescendants`**：不落盘就不会有人日后拿它写判定。

**`distributions` 端点只给得出洲际粒度。** 实测 `Panthera tigris tigris` 返回
`locality="Southern Asia", country=None`。这对映射到 6 个生物地理界（§4.2）**够用**，
但 `profile.range_text` 需要的具体分布国列表（"印度、孟加拉国、尼泊尔、不丹"）**拿不到**，
必须由 agent 从事实锚提取。不要在 `import-gbif.py` 里伪造国家列表。
它也是 1 物种 1 请求，所以只对入队条目查，不对全池 2700 条查。

### 6.1b `refine-candidates.py`（定中文名，约束 ③ 的主力闸门）

三道闸门，顺序不能换：

1. **统称检测**。一个中文名被 ≥2 个 `speciesKey` 共用 → 判为统称，从所有物种的候选里
   剔除。**这个判断只能在全局做**：单看 `Carcharhinus sorrah` 一条记录，"沙条"看不出
   任何问题；只有看到另外 17 个鲨鱼种也叫"沙条"，才知道它是台湾渔业统称。
   实测一次抓出 26 个：沙条 18 种、鲂仔 20 种、大沙 12 种、沙鱼 11 种。
   **这类词不可能靠手写黑名单穷举**，所以统称检测是主力，黑名单只是兜底。
2. **黑名单**（§5.3）。全串精确匹配。只收统称检测抓不到的那一类 —— 池子里只有一个
   物种叫这个名字，但它其实是科/目级统称。
3. **zhwiki 存在性闸门**。定下的名字必须在本地索引里有同名条目，否则 §6.3 取不到事实锚。

**定名分两步：`refine` 出初选，事实锚做终选。** 第一版直接在 `import` 里取最短名，
理由是"最短的通常是正名" —— 错得很彻底：统称恰恰比正名短，18 个鲨鱼种的最短名都是
"沙条"。剔掉统称和黑名单词之后取最短**只是一个够用的起点**，仍然不安全，见下。

产出 `data/pool.jsonl`（定名后的池）、`rejected.tsv`（被拒行 + 原因，留档不是待审清单）、
`generic-names.txt`（自动识别的统称，机器生成不要手改）。

**还有一道闸门在 `refine` 之外，`zh` 只是初选。** 剔掉统称、黑名单词、分类层级名之后，
剩下的仍有方言名、文化名、器物名，而"取最短"挡不住：

| 初选（取最短） | 学名 | 正确的名字 |
|---|---|---|
| 汤匙 | *Rhinobatos hynnicephalus* | 斑纹犁头鳐 |
| 仙鹤 | *Grus japonensis* | 丹顶鹤 |
| 小龙虾 | *Procambarus clarkii* | 克氏原螯虾 |
| 猫熊 | *Ailuropoda melanoleuca* | 大熊猫 |

三个正确答案都在 `zh_alt` 里。统称检测抓不到它们 —— 只有一个物种叫"汤匙"。
**改成取最长会引入新错误**：`海象`→`海象属`（属级条目，直接违反约束 ③）、
`大白鲨`→`食人鲨`、`小熊猫`→`红熊猫`。所以**长度不是判据**，两个方向都在猜；
"猫熊 / 大熊猫"这类"哪个是大陆通用名"更是没有任何本地信号可判。

以"属/科/目/纲/门/族/亚种/类"结尾的名字在 `refine` 里直接剔除（这条无争议，`海象属`
就是这么挡掉的）。**剩下的靠事实锚终检**：`build-queue.py` 按 `zh` → `zh_alt` 顺序
逐个取 zhwiki 正文，要求正文出现该物种学名，第一个过关的才落进 `ready.jsonl`。
所以 `zh_alt` 不是备注，是**有序的后备候选**，下游不得丢弃。

这也是唯一能同时解决"汤匙"和"猫熊"的判据 —— zhwiki 上"汤匙"讲餐具（不含学名），
"猫熊"是重定向页。

#### 6.1b-1 重定向必须分两类（实测推翻了本节的初版判据）

本节原来写的是"正文出现学名、**且不是重定向页**"。`probe/anchor-verdict.py` 实测证明
**这条判据是错的**：zhwiki 大量条目存在于繁体标题下，简体名只是重定向。

```
薮猫   → 藪貓      繁简同名     跟随。subject 用简体，锚文从繁体标题取
猫熊   → 大熊猫    真别名       拒，继续试下一个候选名
小熊猫 → 小熊猫属  别名且属级   拒，且救不回来
```

正确的判据是**繁简归一化后同名才跟随**（`wikitext.zh_key()`，zhconv 转 zh-cn 后比较）。
照初版判据实现的代价有实测数字：**280 条 `ready.jsonl` 里有 60 条（21%）走的是繁简
重定向** —— 驼鹿/駝鹿、弓头鲸/弓頭鯨、棱皮龟/棱皮龜、荨麻蛱蝶/蕁麻蛺蝶……
初版判据会把这五分之一成片误杀，而且失败方式看起来像"zhwiki 没这个条目"。

真别名**不自动采用重定向目标**：那个串没过统称检测和黑名单，采用它等于给约束 ③ 开
后门（`小熊猫 → 小熊猫属` 就是这个后门通向的地方 —— 属级条目正是"一类动物的宽泛
介绍"）。所以拒因要分开记：

| 拒因 | 含义 | 还能救吗 |
|---|---|---|
| `alias` | 重定向到另一个具体物种名 | 能，靠 `zh_alt` 的下一个候选 |
| `alias-rank` | 重定向目标带分类阶元后缀 | 不能，整条丢掉 |
| `no-sci` | 有条目但正文不含学名 | 换候选名 |
| `not-in-index` | 索引里没这个标题 | 换候选名 |

跟随繁简重定向有个前提：**重定向目标（繁体串）不在候选名单里**，必须由
`wikitext.hant_variants()` 预先算出来一并放进 wanted，否则跟随时会 `not-in-index`。

`probe/anchor-verdict.py` 与 `src/wikitext.py` 各有一份判据实现，**这是有意的**：
探针走 wiki-bot 的取文机制，正式件走自己重写的那套，11 条用例两边结果必须一致 ——
探针是独立见证，不要"去重"掉它。

实测拒因分布（全池 7 类群、280 条入选）：
`ok 275 / alias 40 / no-sci 12 / alias-rank 2 / hans-not-in-index 2 / hans-no-sci 1`。

### 6.2 agent 补选题角度

复用 wiki-bot 的 refill 结构（单类群 + 单批 ≤8 行 + agent 只写增量文件 + 逐行验收
+ 零产出告警）。输入是 `build-queue.py --emit N --group X` 打印的批次清单（拼在
`refill-prompt.md` 末尾），agent 输出每行**三段**：

```
subject <TAB> title <TAB> note
```

- `subject`：照抄清单，一字不改。它不是让 agent 想的，是让它**认领**的
- `title`：一句话钩子（如"用尿液标记两百平方公里的独居者"）
- `note`：这条值得写的角度，一句话

**其余五列（group / region / scientific_name / entities / wiki）由 `refill-check.py`
拿 `subject` 去 `ready.jsonl` 查出来填，不让 agent 抄。** 这不是嫌它麻烦 —— 是让它在
结构上**没有能力**改坏那些字段。`subject` 已经过完四道闸门，让 agent 复制一遍就等于
给约束 ③ 开一个抄写错误的口子。它认领不到的 `subject` 直接判废，比事后比对字符串可靠。

**`subject`（中文名）不由 agent 提供。** 它由 §6.1b 定死并已过四道闸门。让 agent 补
中文名等于把约束 ③ 交给一个会顺手写出"老虎"的模型 —— 而且它给的名字未必在 zhwiki
索引里，事实锚会直接断。agent 只负责"这条为什么值得读"，那才是它的强项。

验收链（`refill-check.py`，逐行、任一不过即判废该行）：3 列 → `subject` 在**本类群**的
ready 清单里 → 未在 `queue.tsv` 里 → 批内不重复 → `title` 6–26 字 → `note` 8–48 字
→ `title` ≠ `subject` 本身 → 不含空泛模板词面 → `title` 批内不重复。

空泛模板靠词面拦（`的介绍` `是一种` `科普` `揭秘` …）：agent 偷懒时写的"XX的介绍"
在字数上完全合格，只有词面能挡。

**必须避开 wiki-bot 踩过的坑**（已记录为项目缺陷经验）：refill 不得用 `|| true`
掩盖 agent 失败，不得只数行数——要比对前后增量，零产出必须告警并 exit 1。

还有一条是本项目自己踩出来的：`run.sh` 里判断验收结果**只能看退出码，不能 grep 输出**。
初版写的是 `| grep -q '合格'`，而全废时 `refill-check.py` 打印的是"无一合格"，
**含"合格"子串**，判据恒真 —— 当时只因为 `pipefail` 恰好把 python 的 exit 1 透出来
才没出事故，也就是说这个判据一直是坏的、被另一个判据掩盖着。这是本项目第五次踩
"子串匹配当相等用"（前四次：黑名单误杀东北虎、`falcatus` 含 `catus`、索引里"虎"
命中"虎鲸"、重定向的繁简字面比较）。


### 6.3 事实锚

`fetch-material.py` 原样复用，只把 subject 来源从 `queue.tsv` 的历史词条换成物种中文名。
因为闸门 5.2-5 已保证中文名在索引里，锚文覆盖率应接近 100%（wiki-bot 改造后是 92%，
剩下 8% 正是当初建池时没验索引的历史条目）。

**取锚文要用 `queue.tsv` 的第 8 列 `wiki`，不是 `subject`。** 两者 21% 的情况下不同串
（§7.1）—— 拿 `subject` 去索引里找，`藪貓`/`弓頭鯨`/`棱皮龜` 这一类会全部扑空。
`subject` 只用于给读者显示。


**wiki-bot 的教训要带过来**：锚文是 zhwiki 的机器清洗产物，有病句、繁简混排、600 字
硬截断残句。prompt 必须写明"material 只提供事实，不提供行文"（wiki-bot commit `0b4d7d3`）。

#### 6.3-1 实测：物种条目与历史词条有三处结构性差异

"原样复用"这句话只对了一半。取文层根本没搬 —— wiki-bot 是「一个标题一次 HTTP Range」，
本项目已有 `wikitext.Pages`（按流取、落盘缓存），建池时那 225 条正文早就缓存好了，
所以**这一步实测零网络、5 秒跑完**。搬过来的只是 `clean()`。而清洗层实测要为物种条目
额外加三条：

**① 学名大量不在正文里。** 226 条清洗后有 159 条（70%）正文不含学名 —— 它只存在于
`{{Speciesbox}}` 或 `{{lang|la|''X''}}` 里，而 `clean()` 把模板整块删掉。阶段 6 要校验
模型写的学名，锚里没有就无从校验。两条修法：

- 锚开头拼一个**事实块**：学名取 `queue.tsv` 第 5 列（GBIF 来的，已过三道闸门），
  命名人/异名/亚种从 Taxobox 抽，IUCN 与科/属从 `ready.jsonl` 取。
- `{{lang|la|…}}`/`{{snamei|…}}` 这类**装正文文字**的模板不整块删，留最后一段。
  仅这一条就把"正文不含学名"从 159/226 降到 20/223。

结果：**225 条锚全部含学名，0 例外。**

**② 物种条目普遍很薄。** 清洗后正文中位 254 字，58/225（26%）不足 150 字，最短
「加氏犬浣熊」49 字 —— 它的 wikitext 原文总共只有 1136 B，zhwiki 上就这么多。历史词条
动辄几千字，物种条目大量是 stub。所以 `MAXLEN` 从 wiki-bot 的 600 放到 1400（厚条目
多给一些），并把 `thin` 记成一种**状态而不是失败**。

这条直接约束阶段 6：**prompt 必须允许「锚里没有就不写」**，否则 26% 的选题必然逼出编造。

**③ 消歧义页能过锚判定 —— 判据的第六个漏洞，形态还是「非空 ≠ 可用」。**
「马鹿」在 zhwiki 是消歧义页（`'''马鹿'''可以指：` + 一串带学名的列表项），所以它
**过得了**含学名检测，而清洗之后只剩 6 个字。判据补在 `wikitext.DAB`（靠模板名认，
不靠「可以指」这类词面 —— 词面会误伤正常条目的行文），探针加了两条用例：`马鹿` 必须拒、
`欧洲马鹿` 必须过（证明没有连坐）。实测 226 条里 2 条（马鹿、紫晶林星蜂鸟），
两条的 `zh_alt` 回退链都正好接住了正确条目。

#### 6.3-2 顺带查出闸门的第七个漏洞：`Homo sapiens`

逐条看锚时看到 `[212/224] 智人 ok 803 字`。它把四道闸门**每一道都合法走完了**：
二名法格式对、rank=SPECIES、order=Primates 在 `TAXA` 里、不是家养种、不是统称、
zhwiki「智人」正文里确有学名。于是它以「靠一身汗腺把猎物活活跑垮的猿」进了 `queue.tsv`。

这次不是判据写错，是**判据集里少了一条**：「不能是读者自己」显然到没人写下来。
修在 `lib.EXCLUDE_GENUS`（按属排除，人属其他物种眼下靠 `route=extant` 挡着，那是运气
不是判据），`taxon-check.py` 加两条用例：`Homo sapiens` 拒、`Pan troglodytes` 放行。

**发现路径只有一条：人眼扫了一遍 225 行输出。** 闸门是枚举出来的，枚举就会漏 ——
所以每一批产出都必须有一次逐条过目，没有自动化能替代它。

#### 6.3-3 锚建好之后仍然取不到：`pick.py` 拿 `wiki` 当键查 `material.json`

阶段 6 开工前顺手核对 `pick.json`，看到 `material_status: not_prefetched` —— 而阶段 5
刚刚测出覆盖率 225/225 = 100%。两个数字不可能同时对。

`material.json` 的键取自 **`subject`**（`fetch-material.py` 写的是 `mat[r["subject"]]`），
`pick.py` 却把第 8 列 `wiki` 传了进去。于是**凡 `subject` 与 `wiki` 不同串的选题全都取不到锚**
——实测 225 条里 48 条（21%）：驼鹿/駝鹿、猪獾/豬獾、沼泽鹿/沼澤鹿…都是简繁差异。
两成的选题会在有锚的情况下按"无锚"去写，而日志报的是 `not_prefetched`，
看起来像"锚还没抓"，没有任何环节会指向键名。

**当时的注释是对的**："取锚文用第 8 列 wiki" —— 抓的时候确实用 `wiki` 去 zhwiki 找。
错的是用法：`wiki` 是**取文用的标题**，`subject` 是**锚的索引键**，两件事共用一个变量名之后
就分不出来了。**正确的注释掩盖了错误的用法**，这比没有注释更难发现。

这是"取字段不核键名"的第二次（第一次是阶段 5 的 `iucn_raw`）。这一类 bug 的共性是
**不报错、有默认值、日志里还有一句像样的解释**。修完的验证方式也就只有一条：
全池 225 条逐条查锚，看状态分布是不是 `{'local': 225}`。


---

## 7. 数据模型

### 7.1 `data/queue.tsv`（TSV，8 列）

```
类群slug  生物地理界  title  subject  scientific_name  entities(|分隔)  note  wiki
```

第 8 列 `wiki` 是**锚文实际所在的 zhwiki 标题**，与 `subject` 可以不是同一个串：
`subject=薮猫` 是读者看到的名字，`wiki=藪貓` 是取锚文要用的标题。实测 280 条里
60 条（21%）两者不同（§6.1b-1），没有这一列，下游 `fetch-material.py` 拿 `subject`
去索引里找就会成片扑空 —— 而且看起来像"这个物种 zhwiki 上没有"。

`生物地理界` 允许为空：GBIF 的 distributions 端点只给洲际粒度，实测 280 条里 32 条
（11%）映射不出 6 个界中的任何一个。**拿不到不丢条目** —— 界只是排期时的多样性偏好，
不是选题资格。

第 6 列 `entities` **由脚本填 `[subject]`，不由 agent 写**（偏离 §6.2 的初版设想）。
去重主键是 `subject` + `scientific_name`（§7.3，精确匹配），`entities` 只是近似信号，
而对物种它极容易误报 —— 两个毫不相干的物种共享"东洋界""热带雨林"就会被判成近似
选题。宁可这一列信息量低，不要它制造假阳性。

### 7.2 `content.json` / `data/content/<date>.json`

agent 唯一产物。骨架里**不得出现任何伪注释键**——wiki-bot 就是因为骨架里写了一行
`"__motif__": "按今日 cat 只填…"`（JSON 没有注释语法，模型把它当成了真实容器）
而整期停更。要注释就写在骨架外面。

```json
{
  "date": "2026-09-01",
  "group": "carnivora",
  "group_label": "食肉与有蹄",
  "title": "一句话钩子",
  "subject": "孟加拉虎",
  "scientific_name": "Panthera tigris tigris",
  "summary": "80–120 字导语",
  "entities": ["孟加拉虎", "印度次大陆", "红树林", "..."],
  "tags": ["猫科", "亚洲", "濒危"],
  "profile": {
    "iucn": "EN",
    "iucn_source": "zhwiki",
    "body_length": "体长 270–310 cm（含尾）",
    "weight": "雄性 180–260 kg",
    "lifespan": "野外 8–10 年",
    "habitat": "热带季雨林、红树林沼泽、草地",
    "range_text": "印度、孟加拉国、尼泊尔、不丹",
    "biogeo": "东洋界",
    "diet": "白斑鹿、野猪、水鹿"
  },
  "sections": [
    {"h": "它住在哪里", "p": "…（分布区域，80–260 字）"},
    {"h": "怎么活下来的", "p": "…（生活习性，80–260 字）"},
    {"h": "一个反直觉的点", "p": "…（80–260 字）"}
  ],
  "uncertain": ["文中不完全确定的说法，渲染时标「存疑」"],
  "art": {
    "main": {"subject": "配图主体描述", "alt": "≤40 字"},
    "sub":  {"subject": "配图主体描述", "alt": "≤40 字"}
  }
}
```

`profile.iucn_source` 是必填字段：保护等级不来自 IUCN 官方接口（§1），页面必须能标出处。

**这个骨架有三份副本，它们必须同形**：这里（人读的规格）、`prompt.md` §二（模型读的，
是权威 —— agent 只看得到它）、`selfcheck.py`（机器执行的校验）。写这一节时就已经分叉过一次
——上面的 `uncertain` 与 `art.*.alt` 是回头补的，SPEC 原稿漏了，而 prompt 与校验器都有。
`selfcheck.py` 的 `_prompt_sync()` 用例钉住了其中一对（禁用词表 → prompt），剩下的对齐
只能靠改一处时把三处都翻出来看。


### 7.3 `data/posts.jsonl`

照 wiki-bot，追加 `scientific_name` 与 `group`。去重靠 `subject` + `scientific_name`
双键——同一物种的不同中文名（美洲狮/山狮）不能骗过半年窗口。

---

## 8. AI 配图

`gen-image.py` 整体复用（三条硬约束照抄：任何失败 exit 0、文件已存在则跳过绝不重复
计费、只允许新增 `art.*.file`/`art.*.status`）。`call_model` 一字不改。

### 8.1 风格分两层：类群决定画种，图位决定姿态

**原稿把两件事混在一张表里，`--dry-run` 一跑就发现是错的**（§12.7）。现在是两层：

```python
STYLE = {          # 类群 → 画种语汇（技法、纸感、色调），不含姿态与背景
    "carnivora": "博物学手绘图谱，奥杜邦风格，米白纸底，笔触细腻",
    "aves":      "古典鸟类图谱，铜版手工上色，米白纸底",
    "marine":    "科学插画，冷调，形态细节精确",
    "reptilia":  "19世纪爬虫学图谱，细密线刻上色，鳞片纹理清晰",
    "amphibia":  "湿版水彩图谱，高细节皮肤质感",
    "inverts":   "昆虫学图谱，极高细节，细线描边",
    "mammalia":  "博物学手绘图谱，柔和淡彩，米白纸底",
}
FRAME = {          # 图位 → 姿态与背景，与 prompt.md「主图是行为，附图是结构」对应
    "main": "生境中的一个行为瞬间，环境与季节可辨",
    "sub":  "标本图谱式的平光特写，浅色纯底，无背景杂物",
}
```

`STYLE` 里不得再出现姿态、背景、介质词（「水下」「侧视」「栖枝」「等距排布」「浅色底」
「无背景杂物」）—— `gen-image.py --selftest` 有一条用例盯着这件事。

### 8.2 两条本项目独有的硬要求

**① 一律不用写实摄影风格。** wiki-bot 只对 `geo` 类目开放写实摄影，理由是摄影术
（1839）之前的题材渲染成照片会被读者当成史料证据。动物项目这个风险更严重且更直接：
AI 生成的动物图**极可能物种特征错误**（把美洲狮画成美洲豹、给亚洲物种配上非洲背景），
一旦是照片风格，读者会当成真实影像，等于每天传播一条错误的形态学知识。
博物学图谱风格自带"这是绘制品"的信号。

**② prompt 必须锚定物种，页面必须标注。**

- **学名紧跟中文名排在句首**：`{中文名}（学名 {学名}）。{画面}。{FRAME}。{STYLE}`。
  拉丁名是图像模型训练数据里最强的物种锚（中文名在英文语料里几乎没有对应），
  位置放前面比放后面有效。**这是 60% 的选题唯一的物种锚定手段**，见下一条。
- **近似种排除只用同属，不退同科。** 原稿写的是"画孟加拉虎时排除豹、美洲豹、狮"——
  那是**人**凭形态知识挑的，机械做不到。实测：同属兄弟可用（普通翠鸟 → 蓝耳翠鸟／
  斑头大翠鸟），覆盖 90/225 = 40%；退到同科则产出噪声（§12.7）。剩下 60% 留空不编。
- **`wan2.7-image` 不支持 `negative_prompt` 参数**（阿里云官方文档：「对于不希望出现的
  元素，请在正向提示词中描述（不要出现xxx）」）。所以负向词是**并入正向 prompt** 发出去
  的，写法必须是完整句子（「画的必须是 X 本身，不要画成近似物种：A、B」），
  不能是逗号分隔的裸词表 —— 裸词更容易被当成要画的东西。
- `template.html` 图注固定渲染一行：**「插图由 AI 生成，仅供示意，非真实影像」**
- `img_urls.jsonl` 比 wiki-bot 多记一个 `prompt` 字段：物种画错是本项目最主要的失败
  模式，事后追查时唯一有用的证据就是「当时到底发了什么」。


---

## 9. 约束 ① 的验收方法

建池完成后、上线前必须跑一次，产出证据而不是"应该没问题"：

```
读 queue.tsv + 空的 posts.jsonl，用真实的 pick.rank 模拟 730 天：
  ① 出场覆盖率必须 = 池内条数（死库存 0）
  ② 同一 subject 的最小复现间隔必须 > 183 天    ← 约束 ① 的直接证据
  ③ 同一 scientific_name 最小复现间隔必须 > 183 天
  ④ 无候选天数必须 = 0
```

模拟器必须 `import pick` 调真实 `rank()`，不许复刻逻辑——复刻的话测的是复刻件。
（wiki-bot 上这套模拟已发现真实缺陷：FIFO 排序导致 30/157 条整年不出场。）

`probe/simulate.py` 只调 `pick.pick_one()` 一个入口。所以**取题的全部判据都得在
`pick_one()` 里**，不能留在 `main()` ——判据不只有排序键，窗口过滤、同属间隔、
相似度跳过同样决定覆盖率，落在 `main()` 里模拟器就只能复刻它们。

### 9.1 实测结果（226 条池，2026-09-02 起）

```
① 覆盖率 226/226            死库存 0
② subject 最小复现间隔 189 天 > 183
③ 学名   最小复现间隔 189 天 > 183
④ 无候选天数 0
同属最小间隔 42 天（阈值 30）
```

跑了 730 / 1095 天、四个不同起始日（跨 ISO 周对齐），②③ **恒为 189 天**。这不是
巧合，是结构性下界：条目出窗口后最早能复现的时机就是下一个同类群日，
`ceil(183/7)*7 = 189`。所以余量看着只有 6 天，但它不会再缩。

### 9.2 最小安全水位 = 28 条/类群（`simulate.py --scan` 实测）

```
每类群条数   覆盖率      无候选天数   判定
  26       182/182      22        ✗
  27       189/189       1        ✗     ← 理论下界，但同属冷却吃掉了一条
  28       196/196       0        ✓
```

理论下界是 `WINDOW//7 + 1 = 27`（一轮必须长于窗口）。实测 27 仍会断更 1 天 ——
同属冷却和相似度跳过会临时抽走候选，所以真实下界是 **28**。

### 9.3 `QUEUE_LOW` 的口径是错的，必须按类群判

断更是**单类群**事件。`simulate.py --starve amphibia:26` 实测：

```
amphibia 32 → 26 条，全池 226 → 220 条
QUEUE_LOW=200 → 220 > 200，告警不响
730 天里断更 3 天
```

全池阈值永远漏得掉单类群饥饿。所以新增 `lib.GROUP_LOW = 30`，`pick.py` 输出
`groups_low`（低于线的类群列表）与 `low`（两个口径取或）。30 而不是 28：告警必须
**早于**违约，留 2 条 ≈ 两周的补池时间。`--starve amphibia:29` 实测告警响、未断更。

这条判据本身也进了模拟的验收项：**有断更天却没有类群低于 `GROUP_LOW`** → 判失败。
阈值是靠模拟定出来的，它对不对也该由模拟说话。

### 9.4 容量余量（回答 §13 的养殖鱼/黑名单问题）

| 类群 | 已入队 | `ready.jsonl` 未取用 | 不补池可承受剔除 | 补池后可承受 |
|---|---|---|---|---|
| 各类群 | 32–33 | 7–8 | **4 条** | **12 条** |

所以 §13 里"剔除 6 条养殖经济鱼"这个动作**不能只删不补**：amphibia 32−6=26 会直接
撞穿安全线（实测断更 3 天）。删完必须从 `ready.jsonl` 补回，补完 34 条 ≥ 28。
`pool.jsonl` 每类群还有 81–248 条，补池不缺料。

---

## 9b. 页面与渲染

`src/render.py` + `template.html`，0 token。

### 9b.1 只吃 content.json 一个文件

**渲染不读 `pick.json`。** 这不是洁癖，它是「`data/content/*.json` 可 0 token 重渲」
这条性质能不能成立的分水岭：

| | wiki-bot | 本项目 |
|---|---|---|
| 归档件内容 | 只有 content | 只有 content |
| 渲染还需要什么 | `cat_slug`/`cat_label`/`date_label`/`buildid` 四个 meta，**都不在归档件里** | 无 |
| 重渲入口 | **没有**（只有 `--sample` 读 samples/ 输出到 /tmp） | `--rebuild <date>` / `--rebuild-all` |
| 结论 | 那条性质今天没有任何一条命令能做到，是声称 | 实测 3 期重渲通过 |

本项目天然自足是**阶段 6 骨架设计的意外红利**：content.json 自带 `date`/`group`/
`group_label`，而 `selfcheck.py` 已逐字校验过它们与 pick 一致（§7.2）。
`date_label` 由 `date` 现算（不从 pick 抄）—— 重渲时 pick.json 早被后面的日子覆盖了。

### 9b.2 buildid 必须覆盖模板

`page_buildid() = f(date, content, **template**)`。**实测踩出来的**：照 wiki-bot 只哈希
content 时，把 CSS 的 `max-width:640px` 改成 641 重渲，页面确实变了而 buildid
一个字符没变。后果有两个 —— 阶段 10 的 `wait_live` 会立刻"通过"（线上 buildid 本来
就等于新算的这个），而线上样式还是旧的；`publish.sh` 若拿 buildid 判断要不要提交，
会把整次模板更新跳过。**页面 = 内容 + 模板，签名就该覆盖两者。**
这条对 wiki-bot 同样成立。

**反过来也要成立：页面没变时签名不许变。** 阶段 9 实测撞出来的 —— 同一期连跑两次
`daily`，第二次出图全部 `cached`、图和文一个字节没变，buildid 却从 `a25ddb71` 变成了
`643ca3a3`。根因是 `gen-image.py` 把「这次是怎么拿到图的」写回了 content.json
（`ok_8090kb_webp_306kb` → `cached`），而 buildid 哈希了整个 content。而 `render.py`
从头到尾只读 `art.*.file` 与 `art.*.alt`，**status 一次都没进过页面**。

后果比"每天多一次 git diff"重：posts.jsonl 里的 buildid 是首发时记下的，幂等跳过追加
之后不会更新，于是阶段 10 的 `wait_live` 会拿着一个**谁都不等于**的值去比线上页面 ——
每天判 STALE、每天发降级消息，而页面其实完全正常。所以 `page_buildid` 先过
`_signed_content()` 剔掉 `art.*.status`，并用两条互补用例钉住：**页面确实不含
status**（否则剔掉它就是撒谎），**签名也不含**；再加一条反面 —— 换了 `art.*.file` 则
buildid 必须变（图是页面真依赖的）。修完连跑三次，buildid 三次相同，git 两次跳空。


### 9b.3 不做 themes/，但要剥注释

7 个类群版式完全一样，差异只有主题色（`lib.THEME_COLOR`），所以 CSS 直接写在
`template.html` 里 —— 不设 `themes/` 目录，少一个"缺文件就 sys.exit"的故障点。
（wiki-bot 需要 7 个皮肤文件是因为它 7 个类目的版式真的不同。）

代价是模板顶部的实现说明会原样进入页面，所以渲染后要**剥掉说明性注释**。
这不是体积问题：wiki-bot 的线上 `index.html` 至今带着「观感差异全部来自
themes/t-\*.css」和整段 base.css 注释，等于把内部实现说明发到公网；更荒唐的是注释里
写的 `{{key}}` 示例被模板引擎当成变量替换成了空串。

剥的时候必须用负向前查排除 `each:`/`if:` ——那两类也是 HTML 注释形式，但它们是
**引擎语法**，剥早了 `selfcheck_html` 的"残留模板标记"检查就静默失效了。

### 9b.4 名录卡：空值消失是每天的正常路径

`profile` 的每个字段都可以是空串（prompt.md 明确许可，薄锚下多数数字填不出来），
空行整行不出现。所以「空值消失」不是异常处理，是 **58 条薄锚每天都要走的路径**。

**但整块的出现条件必须是「有数字行**或**有等级」。** selftest 查出来的：原来整块由
`if:profile_rows` 控制，于是薄锚选题会把 IUCN 等级一起吞掉 —— 而等级恰恰是动物条目
最有价值的单一事实，也常常是薄锚里唯一有的那条。模板引擎的 `if` 不支持 `or`，
所以在 `build_scope` 里合成 `has_profile`。

**同一条原则在归档页上漏了一次。** 缺 `group_label` 时那行仍然输出
`<span class="c"></span>` —— 那个 span 带边框，空着就是一排小空框，而 `group_label`
今天没有写入方，所以是**每一行**。24 条用例全过（其中一条还专门验了「缺 group_label
不崩」），这是部署侧真渲染看出来的：**不崩和不难看是两条，得分开钉**。现在空值时整个
span 不输出，并补了第 25 条用例（`class="c"` 不得出现）。

### 9b.5 页面上的两条硬要求

- **AI 标注是模板固定文本**，不取任何 content 字段 —— agent 漏写就没有标注。
  `selfcheck_html` 逐条比对「几张图就要有几处标注」，改坏模板会在渲染时停下，
  而不是发布后靠人眼发现。
- **学名排斜体、紧跟中文名。** 它是读者核对物种身份的唯一凭据（AI 图会画错，
  §13.2），归档页也列出学名 —— 那里是发现"两期其实是同一个种"最容易的地方。

### 9b.6 posts.jsonl 的字段契约（阶段 9 已落地：`lib.POST_FIELDS`）

归档页用 `render.POSTS_FIELDS = date / group_label / title / scientific_name / summary`，
它是 **`lib.POST_FIELDS`（10 键）的子集** —— 后者是唯一的定义处，`publish.py` 拿它断言、
`render.py` 拿子集验归档页，`publish.py --selftest` 用 `importlib` **真的 import
render.py** 去比这个子集关系。复制一份常量就是"两处对不上"的起点，而那条用例存在的
全部意义正是防这件事。

单独立一张可断言的表（而不是写注释），理由是**少一个键的后果全都不是崩，而是静默降级**：

| 缺的键 | 症状 |
|---|---|
| `scientific_name` | `pick.py` 的 `used_sci` 与同属冷却双双失效，中文名换个写法（美洲狮／山狮）半年内能再推一次，**日志全绿** |
| `entities` | `lib.sim` 的实体重叠恒为 0，那是去重主信号，软重复再也拦不住 |
| `group_label` | 归档页每行类群标签空着（§9b.4 那次空壳就是它） |

**空值等于缺键。** `scientific_name: ""` 对 `pick.py` 的伤害与没有这个键完全一样
（`used_sci` 里多一个空串，什么都拦不住），而它**更难看出来** —— 键在，grep 得到，
人就以为没问题。所以 `post_defects()` 把空串和空列表一律算违反。

`group_label` 的写入方是 `src/publish.py` 的 `build_record()`。身份字段（`subject` /
`scientific_name` / `date` / `group`）**一律取 content.json，不取 pick.json**：
`selfcheck.py` 已逐字校验过两者一致，而 pick.json 每天被重写 —— 补跑窗口跑到 publish
时它可能已经是下一天的了。同一个事实有两个来源，就一定会有对不上的那天。


---

## 10. 流水线与 cron

stage 状态机与 wiki-bot 完全一致：`none → content → imaged → rendered → pushed → notified`。
重复执行按 stage 分流，已完成直接 exit 0。

cron 窗口由**推送时刻**倒推（用户 2026-09-02 定：微信在 **12:00 整**收到）：

```
25 11 * * *  /opt/animal/run.sh daily   >/dev/null 2>>/opt/animal/logs/cron.log
0 12 * * *   /opt/animal/run.sh notify  >/dev/null 2>>/opt/animal/logs/cron.log
20 12 * * *  /opt/animal/run.sh daily   >/dev/null 2>>/opt/animal/logs/cron.log   # 补跑
25 12 * * *  /opt/animal/run.sh notify  >/dev/null 2>>/opt/animal/logs/cron.log
23 3 2 * *   /opt/animal/run.sh refill  >/dev/null 2>>/opt/animal/logs/cron.log   # 月度补池，与 wiki-bot 的 1 号错开
```

**先定 notify、再倒推 daily**，不是先挑一个出稿时刻。"推送时间 12:00"这句需求约束的是
`notify` 那一行；`daily` 提前 35 分钟到 11:25 是余量而非平均耗时 —— agent 写稿+出图实测
2–5 分钟、Pages 构建 30s–2min，35 分钟留给慢的那天。把 daily 排在 12:00 会让用户在 12:35
才收到消息，需求就落空了。

同机三家 cron 互不重叠：wiki-bot 占 07:03/07:38/07:47/07:52，trend-digest 占 12:15。
12:00 那次 notify 最迟 12:05 收工（`wait_live` 预算 300s + 起步 sleep 20s），**补跑窗口
特意放在 12:20 让开 12:15** —— 补跑那次通常 0.1 秒就按 stage 跳过，但异常日它会真去调
agent，那时才可能和别人抢资源。

**stdout 丢掉、stderr 留档**（wiki-bot 是 `2>&1` 一起丢）：stdout 已由 `run.sh` 自己 tee 进
`logs/<date>.log`，重复没必要；而 stderr 才装着真正需要人看的东西 —— `wait_live` 的末次
HTTP 码、python traceback、`command not found`。`logs/cron.log` 正常情况下应当是空文件，
一有内容就说明出过事。

`CRON_TZ=Asia/Shanghai` 在 wiki-bot 那一段已声明，对其后所有行生效。
本机 cron 的 PATH 实测为 `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:...`，**含
`/usr/local/bin`**（`openclaw` 装在那里），所以不必在 crontab 里显式声明 PATH；换机器部署
时要重新验这一条 —— "手动跑一切正常、cron 里 agent 那步 command not found"是这类项目最
常见的首日故障。

`daily` 与 `notify` 分开的理由同 wiki-bot：Pages 构建有 30s–2min 延迟，分两个窗口后
这个竞态结构性消失。

`run.sh` 另有三个 mode：`once` = `daily` + `verify` + `notify`（联调用），`verify` = 0 token
本地终检，`notify` = 等 Pages 生效 + 推微信（0 token，可反复跑）。**终检是独立一步，不是每步
exit 0 的累加** —— 每一步自己都会报成功（render 打印 `rendered`、publish 打印 `push ok`），
拼起来仍然可能是错的：模板改了没重渲、jsonl 追了两行、commit 了但没推。verify 查四项：
页面存在且含本期 buildid、`data/content/<date>.json` 在（否则日后换模板无法 0 token 重渲）、
posts.jsonl 恰好一行且合契约、git 无未提交且无未推。

其中**数 posts.jsonl 的原始行数、不走 `lib.load_posts()`**：后者按 `(date,subject)` 去重，
重复追加的那一行会被它悄悄合掉 —— 而重复追加正是这里要查的东西。用去重后的视图验幂等，
等于拿一块滤镜去找它专门滤掉的脏东西。

`once` 里 **verify 红也照发通知**：notify 会亲自探线上页面，探不到自然发降级消息；
反过来"终检红就不通知"会让用户在真出问题的那天什么都收不到，那是最糟的组合。

### 10.1 通知的消息等级（阶段 10 落地）

`notify` 发四种消息，`state/<date>.notified` 记 `kind buildid`：

| kind | 何时 | 内容 |
|---|---|---|
| `ok` | wait_live 判 LIVE | 标题 + 物种/学名/IUCN + summary + 读全文链接 |
| `degraded` | STALE / NOT_FOUND | 同上，链接标注"页面生成中" |
| `nolink` | stage 停在 rendered（push 没成） | 同上，无链接 |
| `relive` | 先发过 degraded/nolink，页面后来好了 | 一行短消息 + 链接 |

等级 `nolink(1) < degraded(2) < ok(3) = relive(3)`，**只许升级，不许降级或重复**。
这是为了避开 wiki-bot 的一个缺陷：它的 `.notified` 只看文件**是否存在**，于是 Pages 慢过
5 分钟预算的那天，第一个窗口发出"页面生成中"、stage 置 notified，兜底窗口直接跳过 ——
页面 6 分钟后真的好了，用户手里却永远只剩那条"生成中"，而链接其实早就能点。
每天最多因此多一条（relive 是终态），有界。

判断做**两道**：`run.sh` 读 `.notified` 的 kind 决定要不要再探，`notify.sh` 自己再按等级
拦一次 —— 与 stage/cached 那两道同构，run.sh 判漏了这里仍然发不出重复消息。

正文一律读 `data/content/<date>.json`（按日期存档的那份），**不读根目录 `content.json`**：
后者是"agent 最近一次写的"，补发历史日期时它是别人家的内容，会发出一条链接指向 9/2、
正文却是 9/3 的消息，而两半各自都"没错"。`group_label` 同理不从 `pick.json` 取。

IUCN 中文标签**复用 `render.py` 的那张表**，不在 notify 里重写第二份 —— 两处各写一份，
改了一处就会出现"页面写濒危、消息写易危"，而两边各自都自洽。

`.env` 的读取统一走 `envload.sh`（三个脚本共用）：**不覆盖已在环境里的变量**。
阶段 9 已被咬过一次（`IMG_ON=0` 被 `.env` 盖回 1，第一次联调就真计费而日志全绿）；
到 notify 这边后果更难收拾 —— `WEIXIN_TARGET=测试群 ./notify.sh` 若不生效就会静默发到
真实接收人，而消息发错人是撤不回来的。


---

## 11. 目录与配置

```
/opt/animal/
  run.sh  publish.sh  notify.sh  wait_live.sh  envload.sh
  refill-prompt.md  prompt.md  template.html  SPEC.md
  .nojekyll  index.html                   # 见下：Jekyll 与根路径
  src/    lib.py  import-gbif.py  refine-candidates.py  taxon-check.py
          wikitext.py  build-queue.py  refill-check.py
          pick.py  selfcheck.py  render.py  gen-image.py  fetch-material.py
          publish.py                      # 发布的数据那一半，见 §12.9
  probe/  anchor-verdict.py            # 判据的独立见证，不参与生产
  data/   queue.tsv  ready.jsonl  posts.jsonl  material.json  content/
          candidates.jsonl  pool.jsonl  blacklist.txt  whitelist.txt
          generic-names.txt  rejected.tsv  anchor-rejected.tsv
  docs/   index.html  archive.html  p/  img/  .nojekyll
  state/  logs/  .cache/  .env  img_urls.jsonl
```

`envload.sh` 被 `run.sh` / `notify.sh` / `publish.sh` 共同 source，**三者对 `.env` 的语义
必须一致**：只要有一个用了 `set -a; . ./.env; set +a`，那个脚本里命令行传的值就会被 `.env`
盖回去，而它看起来完全正常（§10.1 末段记了两次代价）。

`.nojekyll` 放**两处**：`docs/` 那份对当前配置生效（Source 目录已是 `/docs`），根目录那份
是 Source 改回仓库根时的备用。没有它，正文里万一出现 `{{` 或 `{%`，Liquid 会让整站构建
失败 —— 而那时所有页面都停在旧版本，`wait_live` 会判 STALE。

根 `index.html` 是个到 `docs/` 的重定向，**Source 改成 `/docs` 之后它不再被服务**（留着
无害，是回退备用）。现在的根路径由 `docs/index.html` 承担 —— 它是 `render.py` 生成的**当天
页面本身**，不是重定向，实测 `GET /animal_bot/` 直接返回带 `x-build` 的正文，比重定向更好。

**发布拆成两个文件，是为了让脆的那一半能跑用例。** wiki-bot 把追加 posts.jsonl 的逻辑
写成 `publish.sh` 里的 python heredoc —— 那样写有一个具体后果：**它永远跑不了用例**，
而它干的正是本项目最脆的一件事（往 posts.jsonl 写字段，少一个键是静默降级，见 §9b.6）。
所以数据那一半是 `src/publish.py`（20 条用例），git 那一半留在 `publish.sh`
（分类重试、跳空 commit、分支保护，都是 shell 的活）。


Python 脚本都在 `src/`（初版这一节把它们平铺在根，实现时收进子目录了）。因此
`lib.ROOT` **必须自动认路**，不能依赖"记得设 `ANIMAL_ROOT`"：目录名是 `src` 且父目录
有 `SPEC.md` 就上跳一级。这条是踩出来的 —— 忘了设环境变量时的失败方式是**静默的**：
脚本在 `src/` 下另开一套空的 `data/` 和 `.cache/`，一声不响，`wikitext.py` 就这么在
`src/.cache` 里白下了 42MB 索引又解压出 207MB，而正确的那份就在隔壁。

`.env` 相对 wiki-bot 的差异：`PAGE_BASE` 换新仓库；`IMG_API_KEY` 复用同一个；
新增 `GBIF_TIMEOUT`、`QUEUE_LOW=200`、`WINDOW=183`。`.env` 与 `.cache/`、`state/`、
`img_urls.jsonl` 一律 gitignore。

### 11.1 openclaw agent 条目（部署必做，且不在本仓库里）

refill 与日更都要调 `openclaw agent`，而 agent 的 **workspace 由 `~/.openclaw/openclaw.json`
固定，命令行没有覆盖参数**。所以那份配置里必须有一个 `animal` 条目：

```json
{ "id": "animal", "name": "animal", "workspace": "/opt/animal",
  "tools": { "allow": ["write"] } }
```

三件事要注意：

1. **`workspace` 必须等于 `run.sh` 的工作目录。** agent 写的是相对路径
   `data/queue.add.tsv`，两边不一致时脚本会在自己那边找不到文件，报"agent 无产出" ——
   而 agent 那边其实写成功了。所以 refill 也要在 `/opt/animal` 跑，不在开发目录跑。
2. **`tools.allow` 只给 `write`。** agent 唯一该做的事是写那个增量文件。
3. **这份配置同时驱动着 wiki-bot 的生产日更。** 改之前备份，改完 `json.load` 校验并
   确认 `wiki` 条目一字未动 —— 用 dump 前后 diff 看，只该多出 `animal` 那几行。

调用形式（`--message-file` 而不是 `--message`：prompt 是多行的，塞命令行会被 shell 咬）：

```
openclaw agent --agent animal --message-file <prompt 文件>
```


---

## 12. 实施顺序

每阶段都有可验证产出，不往下走就能停。

| 阶段 | 产出 | 验收 |
|---|---|---|
| 1 | `import-gbif.py` + `candidates.jsonl` | 7 类群各拉到 ≥50 条候选，字段完整 |
| 1b | `refine-candidates.py` + `pool.jsonl` | 统称检测生效；7 类群各 ≥50 条定名候选 |
| 2 | `taxon-check.py --selftest` | 19 条用例全绿且**拒因**也对（不只看拒没拒）。含 §12 原定的 8 条，加上四个种加词误杀、`Polygonia c-album`、"海象属"、拼音俗名、whitelist 豁免链路 |
| 2b | `probe/anchor-verdict.py` | 11 条用例全绿。**这一阶段是插进来的** —— 原计划把 §6.1b 的锚判据直接写进 build-queue.py，实测发现判据本身是错的（§6.1b-1）。判据没验之前不写会往池子里写东西的脚本 |
| 2c | `wikitext.py` | 与探针同 11 条用例**逐条结果一致**。取文层重写过，不能只看"能取到文" |
| 3a | `build-queue.py` → `ready.jsonl` | 7 类群各 40 条、共 280 条，`subject`/学名均无重复；拒因分类统计可解释（§6.1b-1） |
| 3b | agent 补选题角度 → `queue.tsv` ≥220 条 | 闸门全过；`anchor-rejected.tsv` 人工过一遍；每批 `refill-check.py` 的判废理由逐条可读 |
| 4 | `pick.py` + `probe/simulate.py` 730 天模拟 | §9 四项全绿——**这是约束 ① 的交付证据**。`pick.py` 从阶段 9 提前到这里：模拟必须调真实取题逻辑，没有它就只能测复刻件。附带产出 `--scan`（最小安全水位）与 `--starve`（单类群饥饿），后者查出 `QUEUE_LOW` 口径错（§9.3） |
| 5 | `fetch-material.py` 建锚 | 覆盖率 ≥95%（闸门已保证中文名在索引里）。**实测 225/225 = 100%、零网络、5 秒**，因为取文层复用 `wikitext.Pages` 且正文在建池时已缓存。附带查出三条物种条目特有的清洗缺陷与闸门的第七个漏洞（§6.3-1、§6.3-2）；逐条人眼过一遍锚是这一阶段不可省的动作 |
| 6 | `prompt.md` + `selfcheck.py` | 手写一份 content.json 过校验；故意写错 6 种情况都被拦。**实测 15/15**：11 种写错被拦（含学名错一字母、悄悄换主体、伪注释键、换个中文名撞同一学名）+ 合格样本通过 + 「字迹不可辨」必须放行 + prompt 与禁用词表同步。这一阶段唯一无法靠搬完成，两个产出互为契约，风险不在各自写错而在**两者对不上**（§12.6） |
| 7 | `gen-image.py --dry-run` | prompt 含学名与近似种负向词。**实测 18 条用例全绿**，并查出 §8.1 风格表本身是错的（类群风格里混着姿态与背景，与主图的生境描述打架）、近似种只能做到同属（40%）、硬约束 3 此前只有注释没有检查 —— 详见 §12.7。这一阶段的价值全在 `--dry-run`：不花一分钱，把每天都会发生的缺陷看出来了 |
| 8 | `render.py` + 模板 | 页面含 AI 标注；`data/content/*.json` 可 0 token 重渲。**实测 25 条用例全绿**，3 期归档件 `--rebuild-all` 通过。查出的三件事都是**参照物本身的错**：wiki-bot 那条重渲能力今天没有任何命令能做到、模板说明注释会原样发到公网、buildid 漏掉模板 —— 详见 §9b 与 §12.8 |
| 9 | 全链路 `run.sh daily` / `once` / `verify` + `publish.sh` + `src/publish.py` | 端到端出一期，检查 stage 流转与幂等。**实测通过**：`none→content→imaged→rendered→pushed` 一次跑通（2 分 09 秒），第二次全程跳过；真出图后连跑三次 `main=cached sub=cached`、图片 md5 不变、`img_urls.jsonl` 不增行、posts.jsonl 仍 1 行、后两次 git 跳空 commit。查出四件事：buildid 掺了出图状态（§9b.2）、`gen-image.py` 有一整块重复定义、`.env` 会把命令行传的 `IMG_ON=0` 盖回 1、`img_urls.jsonl` 规格里说要 gitignore 而实现漏了 —— 详见 §12.9 |
| 10 | 挂 cron + 通知 | **已完成**：`notify.sh`/`wait_live.sh`/`envload.sh` 落地，5 条 cron 已挂，9/2 这期真推到微信（Message ID 已回）。查出四个缺陷，两个是照抄 wiki 时抄进来的（§12.10）。观察 3 天中 |

### 12.6 阶段 6 实测：两个产出互为契约，风险在"对不上"

`prompt.md`（244 行）与 `selfcheck.py`（550 行）分别是给模型的指令和给机器的校验。
各自写对不难，**对不上就会停更** —— wiki-bot 有过实证：prompt 教的措辞被自己的校验器
拦掉，整期废掉。所以写完之后逐条比对了一遍，查出 4 处不一致：

| 不一致 | 后果 |
|---|---|
| prompt 禁「油画」「水墨」，`BANNED_ART` 里没有 | 那条禁止项是空话 |
| prompt 承诺 `alt` 必填 ≤40 字，校验器**完全没查** | 不会报错，只会让页面裂图时什么都不说 |
| prompt 三处让模型"写进 `uncertain`"，校验器不查 | 长期空数组没人会发现 |
| `title` 上限 prompt 22 / 校验器 24 | 22–24 字这一档写了会过，规格却说不行 |

四处都修在校验器侧（**prompt 是契约，校验器要覆盖它承诺的每一条硬项**），只有第一处
反过来补进 prompt。然后把这件事里可机械化的一半做成用例 `_prompt_sync()`：**校验器拦的
每个禁用词，`prompt.md` 里必须都写了**（22 个，全绿）。

**只做单向。** 反方向（prompt 列的词校验器必须拦）没法机械判 —— prompt 里还有「壮美」
「震撼」这类只做举例、不进词表的词，双向比对会一直红着，最后被人关掉，**那比没有更糟**。

另外两条只能靠实跑得到的东西：

- **断言方向决定空转用例会不会暴露。** `summary 超长`那条第一版只加到 119 字（阈值 120），
  差 1 字没越界 —— 用例"看起来在测超长"，其实什么都没测到。它立刻变红只是因为期望是
  "被拦"；**期望"通过"的空转用例则永远是绿的，没有任何信号。**
- **单位换算造成的假警报，决定了数字溯源只能是 WARN。** `_trace_numbers` 把 `profile` 的
  数字回锚里找出处，首跑报「`body_length` 的 210 找不到」—— 查锚原文写的是「长约2.1米」。
  数值对，单位换算过了：**真阳性同时是假警报**。这条实测倒推出了 prompt 里那句
  「有数字的字段照锚的原文单位抄，不要换算」。

薄锚 26%（§6.3-1）是这一阶段最硬的输入约束，落成两条 prompt 规则：`profile` 的每个字段
都可以是空串；**锚薄的时候就写薄**。原话写进了 prompt：一篇 320 字全对的短文是合格产出，
一篇 600 字里有两句编的，是当天的事故。

合格样本不单独维护文件 —— 它就是 `selfcheck.GOOD`，需要时一条命令导出：
`python3 -c "import sys,json;sys.path.insert(0,'src');import selfcheck;json.dump(selfcheck.GOOD,open('content.json','w'),ensure_ascii=False,indent=2)"`。
第一版真的另存过一份 `content.json`，改 `GOOD` 时它没跟上，于是校验结果对不上 —— 又是同一个形态。

### 12.7 阶段 7 实测：`--dry-run` 查出的两件事都在规格里

这一阶段代码大半是从 wiki-bot 搬的（`call_model` 一字未改），真正的产出是**两个每天
都会发生的缺陷**，都在不花钱的 `--dry-run` 里现形。

**① §8.1 的风格表混了两个维度，与主图必然打架。** 第一次 dry-run 输出：北海狗主图
写「立在砾石滩上，晨光斜照」，而 `marine` 的风格是「科学插画式**水下剖面**」——
陆上场景配了水下画风。逐条查下去 7 个类群里 **6 个**有同样的毛病：`carnivora` 的
「全身侧视，无背景杂物」、`aves` 的「栖枝姿态」（水鸟涉禽根本不栖枝）、`inverts` 的
「等距排布」（那是多个标本排列，与「一个行为瞬间」完全对立）。

根因：原表是照着**附图**（标本式特写）的思路写的，而主附图分工在 prompt.md 里早已定死
（主图是行为，附图是结构），风格表没跟上。修法是拆成 `STYLE`（类群 → 画种）+ `FRAME`
（图位 → 姿态背景）两层，并加一条用例盯住「`STYLE` 里不许再出现姿态与背景词」。

顺带暴露了同一处的**职责越界**：prompt.md 教 agent 给附图写「浅色底，标本图谱式的平光」，
而那正是脚本要附加的东西 —— dry-run 里那句话真的出现了两次。prompt 的第一句写着
「你只描述画面内容，画风由脚本附加」，它自己违背了这句。已从 prompt 侧删掉。

**② 近似种排除只能做到同属，而漏掉的恰好是最需要它的。**

| 做法 | 实测结果 |
|---|---|
| 同属兄弟 | 可用：普通翠鸟 → 蓝耳翠鸟／斑头大翠鸟，石鸡 → 北非石鸡／欧石鸡。覆盖 **90/225 = 40%** |
| 同科兜底 | **噪声**：猎豹配上「渔猫、锈斑豹猫」；旋角羚、高角羚、狷羚、跳羚、印度黑羚**五个不同的羚羊全配到同一组**「银犬羚、柯氏犬羚」—— 牛科一百多个属，按文件顺序取前 3 个等于随机抽样 |

所以只用同属，剩下 60% 返回空、整段不出现（与阶段 6「锚里没有就不写」同一条原则）。
关键在于**漏掉的是谁**：猎豹、大熊猫、驼鹿、叉角羚都是单型属，属里就它一个种；而猎豹
恰恰是最容易被画成豹或美洲豹的物种。结论是 **分类学距离不等于视觉相似度**，
机械无解（§13.11）。

留了 `data/similar.tsv` 的读取口（人工要补就补，不存在则跳过），但**本项目自己不预填
任何一行** —— 单型属的视觉混淆要靠形态知识判断，这里唯一能"凭印象"填表的就是模型自己，
而 prompt.md 明确禁止 agent 干这件事（不要从近似物种借、不要凭记得填），写脚本的时候
没有理由自己破例。

**③ `_prompt_sync()` 当场证明了自己不是空转用例。** 构造翠鸟样本时我自己写出了
「背景是**虚化**的芦苇」—— 而 `gen-image.NEGATIVE` 正在排除「镜头虚化」，`BANNED_ART`
里却没有这个词。往词表里加上「虚化」之后，`selfcheck --selftest` **立刻从 15/15 掉到
14/15 并点名缺的词**，补进 prompt.md 才回绿。阶段 6 建的那条用例第一次真的挡了一次分叉。

**④ 硬约束 3 只写在 docstring 里，等于没写。** 部署侧验「不篡改 content.json」时我拿
`md5sum` 去比，结果 FAILED —— 而这是**断言本身错了**：`no_key` 路径本就该写回
`art.*.status`，md5 必然变。改成逐键摊平比对才看清真相（新增 4 个键、删除 0、改值 0）。

问题不在那次误报，在于**这条约束是本脚本与 `selfcheck.py` 之间信任边界的分界线**：
selfcheck 放行的是内容，本脚本若顺手改了正文或 `subject`，等于绕过闸门发布未校验的内容。
这么重的一条，此前唯一的保障是模块 docstring 里的一句话和一段内联 `if`。现在写回统一
走 `apply_result()`，由三条用例钉住：只新增 `WRITABLE` 四个键、不改任何原有字段、
同值重复写回返回 `False`（不幂等的话，补跑窗口每天会多写几次盘、多出几次 git diff）。
用例数因此从 15 条到 18 条。

这是本项目第三次遇到同一形态：**约束写在注释里而没有可执行的检查**（前两次是 §12.6 的
`alt` 必填与 `uncertain`，prompt 承诺了、校验器完全没查）。


### 12.8 阶段 8 实测：三个缺陷全在参照物本身

这一阶段本以为是最没有悬念的一段（照 wiki-bot 的 `render.py` 搬，模板换个骨架）。结果
查出的三件事**没有一件是本项目写错的**，全是照搬对象自己的问题；而它们都是靠
`--selftest` 和一次 `sed` 改 CSS 的实测撞出来的，**没有一个是读代码想出来的**。

**① wiki-bot 的"0 token 重渲全站"是声称，不是能力。** 它的 `render.py` 只有 `--sample`
（读 `samples/*.json` 输出到 `/tmp`），**没有任何读归档件的入口**；而 `publish.sh` 写的
归档件只存 `content`，渲染需要的 `cat_slug`/`cat_label`/`date_label`/`buildid` 四个 meta
字段全都不在里面。也就是说那条性质今天没有任何一条命令能做到（§9b.1 有对比表）。

本项目**天然没有这个问题**，这是阶段 6 骨架设计的意外红利：content.json 自带
`date`/`group`/`group_label`，而 `selfcheck.py` 逐字校验过它们与 pick 一致，于是渲染
只读 content.json 一个文件、`pick.json` 完全不参与，`--rebuild` 是几行代码的事。
`date_label` 由 `date` 现算而不是存下来 —— 重渲时 pick.json 早被后面的日子覆盖了。

**② 模板顶部的说明注释会原样发到公网。** 首跑 selftest 报「AI 标注 3 处而不是 2 处」，
根因是模板注释整段进了页面 —— 我在里面写的实现吐槽也会一起发出去。回头核实 wiki-bot
的线上 `index.html`：它至今带着「观感差异全部来自 themes/t-*.css」和整段 base.css 注释，
更荒唐的是注释里的 `{{key}}` 示例被引擎当成变量替换成了空串。

修法必须精确：剥注释的正则要用**负向前查排除 `each:`/`if:`** —— 那两类也是 HTML 注释
形式，但它们是引擎语法，剥早了 `selfcheck_html` 的「残留模板标记」检查就静默失效
（又一次"检查看起来在跑、其实什么都没查"）。

**③ `buildid = f(date, content)` 漏掉了模板。** 把 CSS 的 `max-width:640px` 改成 641
重渲，页面真的变了而 buildid **一个字符都没变**。后果有两个：阶段 10 的 `wait_live` 会
立刻"通过"（线上 buildid 本来就等于新算的这个）而线上样式还是旧的；`publish.sh` 若拿
buildid 判断要不要提交，还会整次跳过。**页面 = 内容 + 模板，签名就该覆盖两者。**
这条对 wiki-bot 同样成立。修的时候顺手把 `main()` 里那处独立实现也改走同一个
`page_buildid()`，否则将来两处会对不上 —— 那是比漏算更难查的形态。

**④ selftest 还查出 IUCN 徽章被 `if:profile_rows` 连带吃掉。** 58/225 = 26% 是薄锚选题
（体长体重寿命都填不出来），而 IUCN 等级恰恰是它们唯一有的那条，也是动物条目最有价值
的单一事实。模板引擎的 `if` 不支持 `or`，所以在 `build_scope` 里合成 `has_profile`
（有数字行**或**有等级）。空值消失在本项目是每天的正常路径，不是异常分支（§9b.4）。

顺带把 `posts.jsonl` 的字段契约固化成 `POSTS_FIELDS` 常量 + 4 条用例：其中
**`group_label` 今天还没有任何写入方**（`publish.sh` 属阶段 9）。写在这里是为了让
阶段 9 有明确的对象可核，而不是等页面上类群标签全空了才发现 —— 本项目已经栽过两次
「取字段不核键名」（`iucn_raw`、`pick.py` 拿 `wiki` 当键查 material）。

**⑤ 最后一个缺陷是 24 条用例全过之后、部署侧真渲染看出来的。** 归档页缺
`group_label` 时仍然输出空的 `<span class="c"></span>`，而那个 span 带边框 —— 今天
每一行都会挂一个小空框。用例里明明有一条「缺 group_label 不崩」，它只验了不崩。
**不崩和不难看是两条，得分开钉**（补成第 25 条）。这也说明 §9b.4 那条"空值消失"的
原则当时只落在详情页的名录卡上，归档页漏了 —— 原则写进 SPEC 不等于每处都照做了。

### 12.9 阶段 9 实测：四个缺陷，两个来自"规格写了但实现没照做"

端到端结果先摆着：第一跑 `IMG_ON=0`，`none→content→imaged→rendered→pushed` 一次通过，
2 分 09 秒（其中 agent 写稿约 2 分）；第二跑 0 秒、全程跳过。之后放开真出图
（main 8090KB→WebP 306KB，sub 5937KB→WebP 100KB），**连跑三次全部 `main=cached
sub=cached`，图片 md5 逐字节不变，`img_urls.jsonl` 保持 2 行，posts.jsonl 保持 1 行，
后两次 git 跳空 commit** —— §13.6 点名要在本阶段实测的那条幂等性成立。

**① buildid 掺了出图状态，只有连跑才看得见。** 详见 §9b.2。这个缺陷的形状值得单记：
它不会让任何一步失败，两次 `daily` 都打印 `push ok`，页面也完全正常；它只是让 posts.jsonl
里的签名和页面的签名永远不相等，而那个不相等要到阶段 10 的 `wait_live` 才发作，
表现为**每天判 STALE、每天发降级消息**，届时最自然的怀疑对象是 GitHub Pages 缓存。
能在这里撞上它，只因为我把两次的 buildid 打在了同一屏日志里。

**② `gen-image.py` 里 `flat` 与 `apply_result` 各有两份完整定义**（第 331–365 行与
366–400 行，代码逐字相同，只有 docstring 措辞不同）。阶段 7 那次工具延迟落盘、我重试
一遍留下的。它躲过了 18/18 用例和一次通读，因为 **Python 只用最后一份定义，行为完全
正确** —— 这类缺陷没有任何症状，只能靠 `grep -c "^def "` 这种机械核对发现。
现在删掉第一份，用例仍 18/18。

**③ `.env` 会把命令行传的 `IMG_ON=0` 盖回 1。** 原来是 `set -a; . ./.env; set +a`。
`.env` 里那句注释写着"联调请一律用 `--dry-run` 或 `IMG_ON=0`"，而按这句话做**根本不
生效**：`. ./.env` 无条件重设 `IMG_ON=1`，第一次联调就会真计费，而日志看起来一切正常。
改成逐行读、**已在环境里的变量不覆盖**，并实测了三种情形（默认取 .env / 命令行覆盖
成功且 python 的 `os.environ` 确实看到 0 / .env 为空文件不报错）。
`set -a` 那半必须留着 —— 不 export 的话 `IMG_API_KEY` 只是个 shell 变量，python 看不见，
失败方式是 `gen-image` 报 `no_key` 而 `.env` 明明配好了。

**④ `img_urls.jsonl` 规格里写了要 gitignore，实现漏了。** §11 早写着它和 `.env`、
`.cache/`、`state/` 一样不入库，wiki-bot 的 `.gitignore` 第 9 行也有 —— 只有本项目漏了，
`git check-ignore` 一问就知道。不入库的理由是 URL 带签名、几小时后失效，入库只留一堆
过期串；**但本机那份要留着**：物种画错是本项目最主要的失败模式（§13.2），事后追查唯一
有用的证据就是「当时到底把什么 prompt 发出去了」，而那只在这个文件里。

②③④ 有一个共同点：**它们都不需要跑就能查出来**（一次 `grep -c "^def "`、一次
`git check-ignore`、一遍读 `.env` 加载那三行），但都是在准备跑的时候才被看见。规格写过
的事情不等于实现照做了，这已经是本项目第二次撞上同一形态（上一次是 §12.8 的第⑤条，
"空值消失"原则在归档页没照做）。

**另外两件本阶段刻意做的事：**

- **`notify` 明确 `exit 2`**，不给 `once` 留一条"假装通知过"的路。理由见 §10。
- **agent 判 `DUP` 时换题重取一次，并且告警。** `prompt.md` §一给了 `DUP` 契约，但在
  wiki-bot 那边它没有任何消费方 —— agent 说了也白说。DUP 的含义是「queue 里两条行其实
  是同一个物种，而学名没识破」，那是**队列的缺陷**，不只是今天的意外，得有人去合并那
  两行。判 DUP 用行首锚定 `^\s*DUP(\s|$)` 而不是 `grep -q DUP`：「子串匹配当相等用」
  在这个项目已经栽过五次（黑名单误杀东北虎、`falcatus` 含 `catus`、索引里「虎」命中
  「虎鲸」、重定向繁简字面比较、refill `grep '合格'` 命中"无一合格"）。




---

### 12.10 阶段 10 实测：四个缺陷，两个是照抄参照物时抄进来的

触发点是用户的一句话：**"为什么我微信没收到推送？"** 当时的真实状态是通知整块还没写
（`notify` 明确 `exit 2`）、cron 一条没挂、`.env` 里连推送凭证都没有 —— 但顺着这句话查下去，
在真跑第一次 `notify` 的 30 秒里连撞出四个缺陷。

**① Pages 的发布源是仓库根，不是 `/docs` —— 线上真实路径多一段 `/docs`。**
`PAGE_BASE` 拼出来的 `/animal_bot/p/2026-09-02.html` 是 404，而 `/animal_bot/docs/p/...`
才是 200。根路径 200 是 Jekyll 拿 README 生成的默认页，它让"站点是好的"这个印象很有说服力。
阶段 9 的 verify 查不出这个 —— 它是**本地**终检，四项全绿说的是"本地产物自洽且已推送"，
与"线上那个 URL 能不能打开"是两件事。这也正是 `wait_live` 存在的理由：它是唯一会去点那个
链接的一步。当时的修法：`PAGE_BASE` 加 `/docs`（PAT 没有 Pages:write 权限，API 改不了，403）。

> **后续（同日）**：用户在 Settings → Pages 把 Source 目录改成了 `/docs`，`PAGE_BASE` 随即
> 去掉那一段，恢复成 `https://hustrookies.github.io/animal_bot`。**这一改要成对做** ——
> Source 已切而 `PAGE_BASE` 还带 `/docs` 的那一刻，线上 `/docs/p/*.html` 立刻变成 404，
> 下一次 notify 就会判 NOT_FOUND 发降级消息，而页面其实好着。改完实测：`wait_live → LIVE`
> 21.9s，消息里两个链接（正文页、往期）都是 200。
>
> 改 `PAGE_BASE` **不需要重渲历史页面**：它只被 `run.sh` 和 `notify.sh` 读，`render.py` 一个
> 环境变量都不读，页面里没有 canonical/og:url，站内链接一律相对（`p/*.html` 里是
> `../archive.html`），`posts.jsonl` 的 `url` 也是相对的。`.env` 里原本那句"render.py 用它拼
> canonical/og:url"是照抄 wiki-bot 时没验的**假注释**，按它理解会以为改 base 必须重渲全站 ——
> 已就地更正。注释写错比不写更贵：它会让下一次改动多做一堆无用功，且没人会去质疑它。

**② `wait_live` 把 404 报成"本机断网" —— 而那条路径既不发消息也不发告警，完全静默。**
照抄 wiki-bot 的 `curl -fsS`：`-f` 让 HTTP 404 也返回失败（exit 22），于是"页面还没上线"
被计入 `NETFAIL`，累加 6 次判 `NET_DOWN`。而 `NET_DOWN` 的处置是"本机出网有问题，此时
微信大概也发不出去"→ 只 log、不发内容、不发告警。两者混在一起的后果：**Pages 目录配错的
那些天一条消息、一条告警都没有。** 第一次真跑的日志原文就是
`wait_live → NET_DOWN`，把一个配置问题伪装成断网。
修法：不用 `-f`，改看 `http_code` —— `000` 或 curl 自身失败才算网络问题，404/5xx 只是
"还没好"；并把末次 HTTP 码打到 stderr。**如果第一次跑就打印"末次 HTTP 404"，这个问题
本来根本不需要查。**

**③ `curl … | grep -q` 在 `pipefail` 下必然失败 —— 复核步骤每天白等 45 秒且形同虚设。**
`grep -q` 命中即退出并关闭管道，curl 写入失败返回 23，`set -o pipefail` 把它变成整个管道
失败。于是"复核用户真正会点的干净 URL"这一步**永远判不通过**，稳定输出
`LIVE_DIRTY_CACHE`。实测：同一个 URL 同一个 marker，管道版 rc=23，落文件版 rc=0。
它没有任何可见症状 —— `LIVE` 与 `LIVE_DIRTY_CACHE` 在 `run.sh` 里走同一分支、都发正式
消息，所以"干净 URL 命中旧缓存"这个它专门要防的问题**从来没有被真正检测过**，同时每天
多等 45 秒。修法：落文件再 grep。

**④ stage 机漏了 `notified`，agent 会重跑一遍。**
阶段 9 的分流表是 `content|imaged|rendered|pushed`。加上 `notified` 这一级之后，通知发完、
补跑窗口（现在是 12:20）进来会认为"今天还没出稿"→ agent 重写一篇、覆盖 `content.json`、
两张图重出 —— **白花两份钱，而日志一路全绿**。这是本项目第二次遇到"枚举漏项导致判据
整体失效"（上次是闸门枚举漏项放行 `Homo sapiens`）。

**四个之中三个的共同形状：它们都不让任何一步失败。**
①的每一步都报成功、②在日志里给出一个看似合理的错误原因、③连日志都是正常的。能撞上它们
只因为这次是"带着一个具体问题（为什么没收到）去跑"，而不是"跑通了就算过"。

**②③是照抄 wiki-bot 时抄进来的**，与阶段 8 那次同一形态（三个缺陷全在参照物本身）。
参照物能正常工作，不等于它的每一段判据都是对的 —— wiki-bot 的通知天天能收到，恰恰是因为
②③的后果都被"另一条路径正好也发消息"掩盖着。**抄一段代码，就要把它的判据重验一遍。**

顺带落地的两件小事：`.nojekyll`（根 + `docs/`）—— 正文里万一出现 `{{` 或 `{%`，Liquid
会让**整站构建失败**；根 `index.html` 补一个到 `docs/` 的重定向，因为加了 `.nojekyll`
之后 Jekyll 不再拿 README 生成首页。

---

## 13. 已知风险与未决项


1. **约束 ③ 靠黑名单兜底**（§0/§5.3）。黑名单未收录的统称会漏进池，发现一个补一个。
   不要尝试用 `numDescendants` 或字数等数值判据代替——§0 已用 300 条实测证明那会误伤
   薮猫、棕熊、蜜獾这类正例。
2. **AI 图物种特征错误无法机械检测**。§8 的三条措施（学名入 prompt、近似种负向词、
   图谱风格 + 页面标注）都是降低危害，不是消除错误。语病与形态错误一样，都是
   0 token 闸门原理上抓不到的东西——流水线里没有任何环节看懂过图。
3. **IUCN 等级非权威来源**（§1）。若日后申请到 IUCN v4 API key，可把 `iucn_source`
   升级为官方并加校验。
4. **GBIF 单点依赖**。它是本项目唯一可达的分类权威；若长期不可达，建池与月度 refill
   都会停。建议首次导入后把 `candidates.jsonl` 提交进仓库，作为离线兜底。
5. **docs/img 体积**。wiki-bot 已有 700MB 告警阈值；本项目每天 2 张 WebP（100–300KB），
   一年约 150MB，同样接 `alert size`。
6. **配图计费**：每天 2 张，四个 cron 补跑窗口靠"文件已存在则跳过"保证不重复扣费。
   **阶段 9 已实测**：真出图一次后连跑三次 `daily`，三次都是 `main=cached sub=cached`，
   图片 md5 逐字节不变，`img_urls.jsonl` 不增行（§12.9）。两道防线都在且互相独立 ——
   stage 分流管住"整期跳过"，`gen-image` 内部的文件存在检查管住"stage 被清掉那天"。
   顺带查出：连跑时 buildid 会变，根因与计费无关但更隐蔽（§9b.2）。

7. **`alias->繁体正名` 这一类拒因是一笔未取用的容量储备（≈40 条）。** 人工过
   `anchor-rejected.tsv` 时发现，被判 `alias` 的多数并不是"错名字"，而是海峡两岸用词
   不同、zhwiki 采了台湾用法：

   ```
   浅海长尾鲨 → 淺海狐鯊      梅花鲨   → 豹鮫
   绿豹斑蝶   → 綠豹蛺蝶      欧亚鲤   → 歐洲鯉
   ```

   判据拒它们是**对的**（§6.1b-1：重定向目标没过统称检测和黑名单，`鹦鹉螺 → 鹦鹉螺科`
   就是这个后门通向的地方）。但存在一个安全的收窄：**若目标简体化后不带分类阶元后缀、
   且过统称与黑名单检测，就可采用它作为 `subject`**（`wiki` 仍记繁体标题）。这能救回
   约 40 个物种。**当前不做** —— 226 条已超过 183 天窗口（§3），改判据要重跑建池加
   重跑 refill，而现有产出已经验收通过。等 `QUEUE_LOW` 告警响了再取用这笔储备。
8. **`amphibia` 里混进了养殖经济鱼类，需要产品判断（未决）。** 实测入队的 32 条里有
   草鱼、青鱼、武昌鱼、泥鳅、大鳞鲢 —— 四大家鱼及其近亲。它们**不违反**任何一条约束：
   有野生种群、不是驯化品种（与家猫家犬不同类）、也是具体物种。但"草鱼"作为选题，
   读者第一反应是餐桌而不是野生动物，agent 自己写出的钩子也承认了这点
   （"吃水草吃到被全世界放出去的鱼"讲的是入侵）。

   要不要排除是**产品判断，不是技术判断**，与挂着的"老虎/狮子该不该进黑名单"同类，
   留给使用者定。**但代价现在有数字了（§9.4）：不补池只能承受剔除 4 条，这里是 6 条。**
   `simulate.py --starve amphibia:26` 实测：全池 220 条、`QUEUE_LOW` 不响，730 天断更
   3 天。所以动作是「加黑名单 + 立刻从 `ready.jsonl` 补回」两步，不是一步；补完 34 条
   ≥ 安全线 28。**不要顺手删掉** —— 淡水鱼是 `amphibia` 类群的一半供给，连带把裂腹鱼、
   哲罗鲑这些真野生种删了就伤到容量了。
9. **薄锚下的编造，0 token 闸门原理上判不了真假。** 26% 的选题锚不足 150 字（§6.3-1），
   而模型有能力用它"记得"的近似物种把 `profile` 填满 —— 数字看着像真的，页面也好看。
   `selfcheck.py` 能做的只有把「锚里找不出处的数字」逐条列出来给人看（WARN，理由见 §12.6：
   单位换算会造成假警报，判成 FAIL 会天天误杀）。**这意味着 exit 0 不等于内容正确**，
   它只等于"没有可机械检出的问题"。与第 2 条（图的形态错误）是同一类缺口：
   流水线里没有任何环节核对过事实本身。缓解只有两条 —— prompt 明确许可留空（已做），
   以及上线后每周人眼抽看几期（阶段 10 的观察期就是干这个的）。
10. **`RATIO` 目前发不出去（未实测）。** 页面期望主图 16:9 通栏、附图 4:3，而实际恒定
    2048×2048 方图 —— wiki-bot 的实测结论是「顶层 size 被兼容模式忽略」。但阿里云文档
    写着 `wan2.7-image` 支持 1:8–8:1，参数位置是 **`parameters.size`**，也就是说这大概是
    参数放错了位置而不是模型不支持。**没有实测**（要真发一次请求，会计费），所以
    `call_model` 保持一字不改，`RATIO` 只作日志标注与 CSS 侧的期望值。
    这条对 wiki-bot 同样成立 —— 它每天在出方图。
11. **单型属的视觉混淆机械无解。** 近似种排除只对同属兄弟有效（40%），而猎豹、大熊猫、
    驼鹿、叉角羚这些单型属没有兄弟种可排除 —— 偏偏猎豹是最容易被画成豹的那一类。
    **分类学距离不等于视觉相似度**（§12.7）。缓解手段只有学名锚定与页面标注；
    要更进一步就得人工维护 `data/similar.tsv`，那是形态知识，本项目不自动生成。
12. **`art.*.file` 在失败时是空字符串，不是缺键**（留给阶段 8）。`no_key`、`http_429_quota`、
    `dl_not_an_image` 这些路径都会写回 `file: ""`。渲染侧若写成
    `art.main.get("file", "默认图")`，空串会**绕过默认值**拿到一个 `src=""` ——
    页面裂图而日志全绿。阶段 8 取图必须判真值而不是判键存在。
13. **`PAGE_BASE` 与 GitHub Pages 的 Source 目录是一对必须同时改的耦合**（阶段 10 实测）。
    **已于 2026-09-02 处理完**：用户把 Source 改成 `/docs`，`PAGE_BASE` 同步去掉 `/docs`，
    现在是 `https://hustrookies.github.io/animal_bot`。改完实测 `wait_live → LIVE`。
    留下这一条不是遗留问题，而是**约束仍然在**：以后任何一侧单独变动都会让 notify 判
    NOT_FOUND、每天发降级消息，而页面其实完全正常 —— 症状指向 Pages 构建，病根在这一行
    配置，方向是错的。另外 PAT 没有 Pages:write 权限（`PUT /repos/../pages` 返回 403
    `Resource not accessible by personal access token`），所以 Source 那一侧**只能人工改**，
    脚本无法自愈，也无法自检"两侧是否仍然一致"。
14. **`logs/cron.log` 只收 stderr，正常应为空文件。** 它不参与 `find logs -mtime +14 -delete`
    的日常轮转判断（一直在写就不会被删），但**没有人会主动去看它** —— 观察期结束后应当
    确认它是空的；若有内容，里面装的是 cron 环境下才会暴露的问题（PATH、权限、tty）。




