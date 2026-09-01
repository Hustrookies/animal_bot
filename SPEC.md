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
- 低水位告警阈值 `QUEUE_LOW = 200`
- 7 个类群轮转，每类群目标 **≥32 条**（32×7 = 224）

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
  "art": {
    "main": {"subject": "配图主体描述"},
    "sub":  {"subject": "配图主体描述"}
  }
}
```

`profile.iucn_source` 是必填字段：保护等级不来自 IUCN 官方接口（§1），页面必须能标出处。

### 7.3 `data/posts.jsonl`

照 wiki-bot，追加 `scientific_name` 与 `group`。去重靠 `subject` + `scientific_name`
双键——同一物种的不同中文名（美洲狮/山狮）不能骗过半年窗口。

---

## 8. AI 配图

`gen-image.py` 整体复用（三条硬约束照抄：任何失败 exit 0、文件已存在则跳过绝不重复
计费、只允许新增 `art.*.file`/`art.*.status`）。`call_model` 一字不改。

### 8.1 风格表按类群

```python
STYLE = {
    "carnivora": "博物学手绘图谱，奥杜邦风格，米白纸底，全身侧视，无背景杂物",
    "aves":      "古典鸟类图谱，铜版手工上色，栖枝姿态，米白底",
    "marine":    "科学插画式水下剖面，冷调，无气泡特效",
    "reptilia":  "19世纪爬虫学图谱，细密线刻上色，鳞片纹理清晰",
    "amphibia":  "湿版水彩图谱，高细节皮肤质感，浅色底",
    "inverts":   "昆虫标本图谱，等距排布，极高细节，纯色底",
    "mammalia":  "博物学手绘图谱，柔和淡彩，栖息地简笔背景",
}
```

### 8.2 两条本项目独有的硬要求

**① 一律不用写实摄影风格。** wiki-bot 只对 `geo` 类目开放写实摄影，理由是摄影术
（1839）之前的题材渲染成照片会被读者当成史料证据。动物项目这个风险更严重且更直接：
AI 生成的动物图**极可能物种特征错误**（把美洲狮画成美洲豹、给亚洲物种配上非洲背景），
一旦是照片风格，读者会当成真实影像，等于每天传播一条错误的形态学知识。
博物学图谱风格自带"这是绘制品"的信号。

**② prompt 必须锚定物种，页面必须标注。**

- prompt 拼入学名与关键辨识特征：`f"{中文名}（{学名}）。{辨识特征}。{STYLE[group]}"`，
  辨识特征取自 `profile` 与锚文
- `NEGATIVE` 在 wiki-bot 基础上追加**近似种排除**（画孟加拉虎时排除"豹、美洲豹、狮"）
- `template.html` 图注固定渲染一行：**「插图由 AI 生成，仅供示意，非真实影像」**

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

---

## 10. 流水线与 cron

stage 状态机与 wiki-bot 完全一致：`none → content → imaged → rendered → pushed → notified`。
重复执行按 stage 分流，已完成直接 exit 0。

cron 窗口**与 wiki-bot 错开**（wiki-bot 占 07:03/07:38/07:47/07:52）：

```
5  8 * * *   /opt/animal/run.sh daily
40 8 * * *   /opt/animal/run.sh notify
49 8 * * *   /opt/animal/run.sh daily     # 补跑
54 8 * * *   /opt/animal/run.sh notify
23 3 2 * *   /opt/animal/run.sh refill    # 月度补池，与 wiki-bot 的 1 号错开
```

`daily` 与 `notify` 分开的理由同 wiki-bot：Pages 构建有 30s–2min 延迟，分两个窗口后
这个竞态结构性消失。

---

## 11. 目录与配置

```
/opt/animal/
  run.sh  refill-prompt.md  prompt.md  template.html  SPEC.md
  src/    lib.py  import-gbif.py  refine-candidates.py  taxon-check.py
          wikitext.py  build-queue.py  refill-check.py
          pick.py  selfcheck.py  render.py  gen-image.py  fetch-material.py
  probe/  anchor-verdict.py            # 判据的独立见证，不参与生产
  data/   queue.tsv  ready.jsonl  posts.jsonl  material.json  content/
          candidates.jsonl  pool.jsonl  blacklist.txt  whitelist.txt
          generic-names.txt  rejected.tsv  anchor-rejected.tsv
  docs/   index.html  archive.html  p/  img/
  state/  logs/  .cache/  .env
```

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
| 4 | 730 天模拟 | §9 四项全绿——**这是约束 ① 的交付证据** |
| 5 | `fetch-material.py` 建锚 | 覆盖率 ≥95%（闸门已保证中文名在索引里） |
| 6 | `prompt.md` + `selfcheck.py` | 手写一份 content.json 过校验；故意写错 6 种情况都被拦 |
| 7 | `gen-image.py --dry-run` | prompt 含学名与近似种负向词 |
| 8 | `render.py` + 模板 | 页面含 AI 标注；`data/content/*.json` 可 0 token 重渲 |
| 9 | 全链路 `run.sh once` | 端到端出一期，检查 stage 流转与幂等（连跑两次不重复计费） |
| 10 | 挂 cron + 通知 | 观察 3 天 |

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
   这条幂等性在阶段 9 必须实测（连跑两次 `run.sh daily`，确认第二次全部 `cached`）。
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
   留给使用者定。排除的代价很低：往 `blacklist.txt` 加词面即可，`ready.jsonl` 尚有
   54 条余量可补位。**不要顺手删掉** —— 淡水鱼是 `amphibia` 类群的一半供给，
   连带把裂腹鱼、哲罗鲑这些真野生种删了就伤到容量了。

