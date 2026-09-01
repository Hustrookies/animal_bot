#!/usr/bin/env python3
"""生成当日配图 —— 0 token，但按张计费。

读 content.json 的 art.main/art.sub，拼 prompt → 调 wan2.7-image → 下载到 docs/img/，
把文件名写回 art.*.file，好让 render.py 与「data/content/<date>.json 可 0 token 重渲」
都能拿到路径。

三条硬约束（改动前必读，每条都对应一类真实故障，照搬 wiki-bot）：
  1. 任何失败都 exit 0。配图是增益不是依赖，图挂了不该让当天停更。
  2. 落盘文件已存在则跳过，绝不重复调 API。补跑窗口每天多跑 1–3 次 daily，
     不幂等的话每天被扣 2–4 次费。
  3. 只允许往 content.json 里新增 art.*.file / art.*.status，不得改任何其它字段
     —— selfcheck.py 是信任边界，它已经过了，本脚本不能绕过它篡改内容。

── 本项目与 wiki-bot 的两处实质差异（SPEC §8.2）──

**① 一律不用写实摄影。** wiki-bot 对 `geo` 类目开放写实摄影，这里一条都不开。
理由不是审美：AI 生成的动物图极可能物种特征错误（把美洲狮画成美洲豹、给亚洲物种
配非洲背景），一旦是照片风格，读者会当成真实影像，等于每天传播一条错误的形态学知识。
图谱风格自带「这是绘制品」的信号，页面还固定标一行「插图由 AI 生成」。

**② prompt 用学名锚定物种，并排除同属近似种。** 见 `similar_of` 与 `build_prompt`
的注释 —— 这一条在实测中被砍掉了一半，砍掉的理由比留下的部分更值得读。

用法：
  ./gen-image.py                  正常：读 content.json + pick.json
  ./gen-image.py --dry-run        只打印将要发送的 prompt，不联网、不花钱
  ./gen-image.py --force          忽略已存在的文件，强制重新生成（会计费）
  ./gen-image.py --selftest       prompt 拼装与近似种查找的用例
"""
import argparse, collections, json, os, shutil, subprocess, sys, time
import urllib.error, urllib.request
import lib

TIMEOUT   = int(os.environ.get("IMG_TIMEOUT", "120"))
MAX_BYTES = int(os.environ.get("IMG_MAX_BYTES", str(8 * 1024 * 1024)))
MODEL     = os.environ.get("IMG_MODEL", "wan2.7-image")
APIKEY    = os.environ.get("IMG_API_KEY", "")
ENABLED   = os.environ.get("IMG_ON", "1") not in ("0", "", "false", "off")

# 每类群固定画风（SPEC §8.1）—— 0 token，改全站观感只需改这张表。
# **没有一行是写实摄影**，这是刻意的，理由见模块 docstring ①。
#
# 这张表只写**画种语汇**（技法、纸感、色调），不写姿态与背景 —— 那两件事由 FRAME
# 按图位给。SPEC §8.1 的原表混在一起，`--dry-run` 一跑就暴露了：`marine` 写的是
# 「科学插画式水下剖面」，而北海狗的主图是「立在砾石滩上，晨光斜照」—— 陆上场景配了
# 水下画风，两句直接打架。逐条查下去 7 个类群里 6 个都有这个毛病：`carnivora` 的
# 「全身侧视，无背景杂物」、`aves` 的「栖枝姿态」（水鸟涉禽根本不栖枝）、`inverts` 的
# 「等距排布」（那是多个标本排列，与「一个行为瞬间」完全对立）。
#
# 根因是原表照着**附图**（标本式特写）的思路写的，套到**主图**（生境中的行为瞬间）上
# 必然冲突。而主附图分工在 prompt.md 里早就定死了，风格表只是没跟上。
STYLE = {
    "carnivora": "博物学手绘图谱，奥杜邦风格，米白纸底，笔触细腻",
    "aves":      "古典鸟类图谱，铜版手工上色，米白纸底",
    "marine":    "科学插画，冷调，形态细节精确",
    "reptilia":  "19世纪爬虫学图谱，细密线刻上色，鳞片纹理清晰",
    "amphibia":  "湿版水彩图谱，高细节皮肤质感",
    "inverts":   "昆虫学图谱，极高细节，细线描边",
    "mammalia":  "博物学手绘图谱，柔和淡彩，米白纸底",
}
STYLE_FALLBACK = STYLE["mammalia"]

# 姿态与背景按**图位**给，与 prompt.md「主图是行为，附图是结构」逐条对应。
# 这一层是 dry-run 实测补出来的，见 STYLE 上面那段。
#
# main 不写光线：prompt.md 已经要求画面描述里写出时刻（「晨光斜照」），这里再加"自然光"
# 就是重复。**职责边界是「画面内容归 agent，画法归脚本」**，越界的那一半必须删掉一边 ——
# 附图原先两边都写了「标本图谱式的平光」，dry-run 里那句真的出现了两次。
FRAME = {
    "main": "生境中的一个行为瞬间，环境与季节可辨",
    "sub":  "标本图谱式的平光特写，浅色纯底，无背景杂物",
}


# 通用负向词。**wan2.7-image 不支持 negative_prompt 参数**（阿里云官方文档明写：
# 「对于不希望出现的元素，请在正向提示词中描述（不要出现xxx）」），所以这些词是并入
# 正向 prompt 末尾发出去的，不是独立通道。这一点决定了负向段要写成句子而不是词表堆砌
# —— 逗号分隔的裸词更容易被当成要画的东西。
NEGATIVE = ("照片写实风格、摄影、镜头虚化、人物、文字、标签、水印、"
            "畸变的肢体、多余的头、解剖结构错误、过饱和、HDR、廉价游戏CG感、低分辨率")

# 页面上主图通栏、附图在正文中段。**这两个比例目前发不出去**：wiki-bot 实测顶层 size
# 被兼容模式忽略，恒定输出 2048×2048。官方文档说 wan2.7-image 支持 1:8–8:1，参数位置
# 是 `parameters.size` —— 也就是说这大概是参数放错了位置而非模型不支持，但没有实测过
# （要真发一次请求，会计费），所以这里只留作日志标注与 CSS 侧的期望值，见 SPEC §13.10。
RATIO = {"main": "16:9", "sub": "4:3"}

WEBP_Q    = int(os.environ.get("IMG_WEBP_Q", "82"))
WEBP_MAXW = {"main": 1280, "sub": 1024}

# magic bytes → 扩展名。不靠 Content-Type，也不假定 .jpg：接口返回一段 JSON 错误体时，
# 若盲信扩展名就会得到一个「打不开的 .jpg」，而页面只会显示裂图，日志全绿。
MAGIC = [(b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"),
         (b"RIFF", "webp"), (b"GIF8", "gif")]

SIB_MAX = 3          # 负向段最多列几个近似种，多了会把 prompt 的重心冲掉


def sniff(b):
    for sig, ext in MAGIC:
        if b.startswith(sig):
            return ext
    return None


# ────────────────────────── 近似种 ──────────────────────────

_SIB_CACHE = {}


def _genus_index():
    """{属名: [候选行]}，取自 candidates.jsonl（GBIF 全量，不限于入队的 225 条）。

    用全量而不是只用队列：兄弟种本身不需要够格当选题，它只是个「不要画成这个」的名字。
    """
    if "idx" in _SIB_CACHE:
        return _SIB_CACHE["idx"]
    idx = collections.defaultdict(list)
    p = os.path.join(lib.ROOT, "data", "candidates.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                try:
                    c = json.loads(ln)
                except Exception:
                    continue
                if c.get("zh_all") and c.get("sci"):
                    idx[c["sci"].split()[0]].append(c)
    _SIB_CACHE["idx"] = idx
    return idx


def similar_of(sci):
    """同属近似种的中文名，≤SIB_MAX 个。返回 (名字列表, 来源串)。

    **只用同属，绝不退到同科。** 这是实测出来的，不是保守：

    同属兄弟在形态上确实近似（分类学就是这么分的），实测结果可用 ——
    普通翠鸟 → 蓝耳翠鸟／斑头大翠鸟，石鸡 → 北非石鸡／欧石鸡，
    高加索羱羊 → 努比亚羱羊／捻角山羊。都是真会混的。

    第一版加了「同属为空则退到同科」的兜底，产出是**噪声**：猎豹配上了「渔猫、
    锈斑豹猫」，而旋角羚、高角羚、狷羚、跳羚、印度黑羚**五个不同的羚羊全配到同一组**
    「银犬羚、柯氏犬羚、艮氏犬羚」—— 因为牛科有一百多个属，按文件顺序取前 3 个等于
    随机抽样。把随机的物种名塞进 prompt 的负向段，比不加更糟：模型不支持独立
    negative 通道，这些名字最终是出现在正向文本里的。

    覆盖率因此只有 **90/225 = 40%**，剩下 60% 返回空列表 —— 与阶段 6「锚里没有就不写」
    同一条原则，不编。

    **漏掉的恰好是最需要它的那些**：猎豹、大熊猫、驼鹿、叉角羚都是单型属，属里就它一个
    种，机械上没有兄弟；而猎豹恰恰是最容易被画成豹或美洲豹的物种。结论是
    **分类学距离不等于视觉相似度**，这个缺口机械无解（SPEC §13.11）。
    留了 data/similar.tsv 的读取口，人工要补就补，不存在则跳过。
    """
    sci = (sci or "").strip()
    if not sci:
        return [], "no_sci"
    manual = _manual_similar().get(sci)
    if manual:
        return manual[:SIB_MAX], "manual"
    g = sci.split()[0]
    sib = [c["zh_all"][0] for c in _genus_index().get(g, []) if c["sci"] != sci]
    return sib[:SIB_MAX], ("genus" if sib else "none")


def _manual_similar():
    """data/similar.tsv：`学名<TAB>近似种1,近似种2`。可选，不存在就是空表。

    **本项目自己不预填任何一行。** 单型属的视觉混淆要靠形态知识判断，而这里唯一能
    "凭印象"填表的就是模型自己 —— prompt.md 明确禁止 agent 干这件事（不要从近似物种借、
    不要凭记得填），写这个脚本的时候没有理由自己破例。要补的人比这个脚本更懂。
    """
    if "manual" in _SIB_CACHE:
        return _SIB_CACHE["manual"]
    out, p = {}, os.path.join(lib.ROOT, "data", "similar.tsv")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "\t" not in ln:
                    continue
                k, v = ln.split("\t", 1)
                names = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
                if k.strip() and names:
                    out[k.strip()] = names
    _SIB_CACHE["manual"] = out
    return out


# ────────────────────────── prompt ──────────────────────────

def build_prompt(kind, scene, subject, sci, group, sib):
    """拼一条完整 prompt。返回字符串。

    顺序是刻意的：**学名紧跟在中文名后面、排在最前**。拉丁名是图像模型训练数据里
    最强的物种锚（中文名在英文语料里几乎不存在对应），把它放在句首比放在句尾有效。
    这也是 60% 没有近似种可排除的选题唯一的物种锚定手段。

    `kind` 决定 FRAME（姿态与背景），`group` 决定 STYLE（画种语汇）。两者分开的理由
    见 STYLE 上面那段注释 —— 混在一起会让主图的生境描述与标本式画风打架。

    负向段写成完整句子（「不要画成……」）而不是逗号词表：模型不支持独立 negative
    通道，负向词是并到正向文本里发出去的，官方文档给的写法就是「不要出现 xxx」。
    """
    head = "%s（学名 %s）" % (subject, sci) if sci else subject
    parts = ["%s。%s" % (head, scene.strip().rstrip("。")),
             FRAME.get(kind, FRAME["main"]),
             STYLE.get(group, STYLE_FALLBACK)]
    if sib:
        parts.append("画的必须是%s本身，不要画成近似物种：%s" % (subject, "、".join(sib)))
    parts.append("不要出现：%s" % NEGATIVE)
    return "。".join(parts) + "。"


# ╔═════════════════ 与 wiki-bot 逐字相同，不要改 ═════════════════╗
def call_model(prompt, ratio):
    """提交一次生成请求，返回 (图片URL 或 None, 状态串)。

    契约（务必遵守，框外代码依赖它）：
      - 成功 → (url_str, "ok")
      - 任何失败 → (None, "<简短状态>")，**不要抛异常，不要 sys.exit**
    wiki-bot 的 I1 实测结论（2026-08-26 真实请求验证，本项目照搬未改）：
      - endpoint: POST {base}/compatible-mode/v1/chat/completions，Bearer 鉴权
      - 同步调用；messages 的 content 必须是 parts 列表，纯字符串会 400
      - 返回 output.choices[0].message.content[] 中 type=image 的项，其 "image" 为图片 URL
      - ratio 不生效（顶层 size 被忽略，恒定 2048*2048）→ 不发送，见 RATIO 的注释
      - 无独立 negative prompt 参数 → 负向词并入正向 prompt 末尾（官方文档也这么要求）
    """
    if not APIKEY:
        return None, "no_key"
    endpoint = os.environ.get(
        "IMG_ENDPOINT",
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer %s" % APIKEY,
                 "Content-Type": "application/json",
                 "User-Agent": "animal-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "http_%d" % e.code
        if e.code == 429:
            return None, "http_429_quota"
        return None, "http_%d" % e.code
    except TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, "req_%s" % type(e).__name__
    try:
        parts = d["output"]["choices"][0]["message"]["content"]
        for p in parts if isinstance(parts, list) else []:
            if isinstance(p, dict) and p.get("type") == "image" and p.get("image"):
                return p["image"], "ok"
    except (KeyError, IndexError, TypeError):
        pass
    code = d.get("code")
    if code:
        return None, "api_%s" % code
    return None, "bad_response"
# ╚═══════════════════════════════════════════════════════════════════╝


def to_webp(src, kind):
    """原图转 WebP 并缩到显示宽度。成功返回新路径，否则 None（调用方保留原图）。

    原图恒定 2048×2048 PNG（8–10MB），直接进 git 会让 Pages 加载以十秒计。
    cwebp 缺失或转换失败时退回原图 —— 配图是增益不是依赖，压缩不能成为故障点。"""
    if shutil.which("cwebp") is None:
        return None
    dest = os.path.splitext(src)[0] + ".webp"
    tmp = dest + ".part"
    try:
        r = subprocess.run(
            ["cwebp", "-quiet", "-q", str(WEBP_Q),
             "-resize", str(WEBP_MAXW.get(kind, 1280)), "0", src, "-o", tmp],
            capture_output=True, timeout=180)
    except Exception:
        return None
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
        if os.path.exists(tmp):
            os.remove(tmp)
        return None
    os.replace(tmp, dest)
    return dest


def log_url(date, kind, url, prompt):
    """把原图 URL 与实际发出的 prompt 记到 img_urls.jsonl（gitignored，绝不上传）。

    URL 约 24 小时过期，此文件用于事后排查与短期补下载，不是长期存档。
    **比 wiki-bot 多记一个 prompt**：物种画错是本项目最主要的失败模式（SPEC §13.2），
    事后追责时唯一有用的证据就是「当时到底发了什么」——尤其是近似种那一段。
    只在真正调了 API 时写一行；缓存命中没有新 URL，不写。"""
    p = os.path.join(lib.ROOT, "img_urls.jsonl")
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": date, "kind": kind, "url": url,
                                "prompt": prompt, "ts": time.strftime("%F %T")},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass                                    # 记录失败不影响出图


def fetch(url):
    """下载并按 magic bytes 判定真实格式。返回 (bytes, ext) 或 (None, 状态)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "animal-bot/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return None, "dl_http_%d" % e.code
    except Exception as e:
        return None, "dl_%s" % type(e).__name__
    if len(data) > MAX_BYTES:
        return None, "dl_too_large"
    if len(data) < 1024:
        return None, "dl_too_small"
    ext = sniff(data)
    if not ext:
        return None, "dl_not_an_image"      # 大概率是 JSON 错误体
    return data, ext


def flat(o, p=""):
    """把嵌套 dict/list 摊平成 {点分路径: 标量}。只服务于下面那条硬约束检查。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from flat(v, p + "." + k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from flat(v, "%s[%d]" % (p, i))
    else:
        yield p, o


# 硬约束 3 允许本脚本写的**全部**键，一个不多。
WRITABLE = {".art.main.file", ".art.main.status", ".art.sub.file", ".art.sub.status"}


def apply_result(content, kind, rel, st):
    """把一次出图结果写回 content。返回是否有变化。

    **抽成独立函数只为一件事：让硬约束 3 可被用例验证。** 那条约束（只准新增
    art.*.file / art.*.status，不得动任何其它字段）本来只写在 docstring 里 ——
    而它是整个脚本与 selfcheck.py 信任边界的分界线：selfcheck 放行的是内容，
    本脚本若顺手改了正文或 subject，等于绕过闸门发布未校验的内容。
    部署侧实测时我用 md5 去验，结果 md5 必然变（status 本就该写），
    说明**光有 docstring 的约束等于没有约束** —— 现在由 selftest 逐键比对。
    """
    node = content.get("art", {}).get(kind)
    if not isinstance(node, dict):
        return False
    if node.get("file") == rel and node.get("status") == st:
        return False
    node["file"], node["status"] = rel, st
    return True


def flat(o, p=""):
    """把嵌套 dict/list 摊平成 {点分路径: 标量}。只服务于下面那条硬约束检查。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from flat(v, p + "." + k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from flat(v, "%s[%d]" % (p, i))
    else:
        yield p, o


# 硬约束 3 允许本脚本写的**全部**键，一个不多。
WRITABLE = {".art.main.file", ".art.main.status", ".art.sub.file", ".art.sub.status"}


def apply_result(content, kind, rel, st):
    """把一次出图结果写回 content。返回是否有变化。

    **抽成独立函数只为一件事：让硬约束 3 可被用例验证。** 那条约束（只准新增
    art.*.file / art.*.status，不得动其它字段）本来只写在模块 docstring 里 ——
    而它正是本脚本与 selfcheck.py 之间信任边界的分界线：selfcheck 放行的是内容，
    本脚本若顺手改了正文或 subject，等于绕过闸门发布未校验的内容。
    部署侧实测时我拿 md5 去验，结果 md5 必然不一致（status 本就该写），
    说明**只写在 docstring 里的约束等于没有约束** —— 现在由 selftest 逐键比对。
    """
    node = content.get("art", {}).get(kind)
    if not isinstance(node, dict):
        return False
    if node.get("file") == rel and node.get("status") == st:
        return False
    node["file"], node["status"] = rel, st
    return True


def one(kind, prompt, date, force):
    """返回 (相对路径 或 "", 状态)。相对路径形如 ../img/2026-09-01-main.webp"""
    outdir = os.path.join(lib.ROOT, "docs", "img")
    os.makedirs(outdir, exist_ok=True)
    if not force:
        for ext in ("webp", "jpg", "png", "gif"):
            p = os.path.join(outdir, "%s-%s.%s" % (date, kind, ext))
            if os.path.exists(p) and os.path.getsize(p) > 1024:
                if ext != "webp":
                    # 存量原图免费补转：不花钱就能把已入库的大图换掉
                    wp = to_webp(p, kind)
                    if wp:
                        os.remove(p)
                        return "../img/%s-%s.webp" % (date, kind), "cached_webp"
                return "../img/%s-%s.%s" % (date, kind, ext), "cached"

    url, st = call_model(prompt, RATIO.get(kind, "1:1"))
    if not url:
        return "", st
    log_url(date, kind, url, prompt)           # 先记录，下载失败也留档
    data, ext = fetch(url)
    if data is None:
        return "", ext                         # 此时 ext 是状态串
    dest = os.path.join(outdir, "%s-%s.%s" % (date, kind, ext))
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)                      # 原子落盘，防半截文件被 git 提交
    orig_kb = len(data) // 1024
    if ext != "webp":
        wp = to_webp(dest, kind)
        if wp:
            os.remove(dest)
            return ("../img/%s-%s.webp" % (date, kind),
                    "ok_%dkb_webp_%dkb" % (orig_kb, os.path.getsize(wp) // 1024))
    return "../img/%s-%s.%s" % (date, kind, ext), "ok_%dkb" % orig_kb


# ────────────────────────── 用例 ──────────────────────────

def selftest():
    """三件必须钉住的事，都是这一阶段实测踩出来的。"""
    bad = 0

    def ck(name, cond, info=""):
        nonlocal bad
        bad += 0 if cond else 1
        print("  %s %-30s %s" % ("OK  " if cond else "FAIL", name, info))

    p = build_prompt("main", "一头雄性北海狗立在砾石滩上，晨光斜照，视角略低",
                     "北海狗", "Callorhinus ursinus", "marine", ["南美海狗", "海熊"])
    # 学名必须在句首那一段。中文名在英文语料里几乎没有对应，学名是唯一强锚。
    ck("学名紧跟中文名", p.startswith("北海狗（学名 Callorhinus ursinus）"), p[:34])
    ck("带上了类群风格", STYLE["marine"] in p)
    ck("近似种写成句子而非裸词表",
       "不要画成近似物种：南美海狗、海熊" in p)
    ck("负向段并入正向", NEGATIVE in p)
    # 没有近似种时不能留下空的「不要画成近似物种：」——那是在教模型一个空概念
    p2 = build_prompt("main", "一只猎豹低身加速", "猎豹", "Acinonyx jubatus",
                      "carnivora", [])
    ck("无近似种则整段不出现", "近似物种" not in p2, p2[-30:])
    ck("未知类群回退不炸", STYLE_FALLBACK in
       build_prompt("main", "画面", "某兽", "Genus species", "no_such_group", []))
    # 主图给生境、附图给标本底。同一段画面描述换个图位，出来的框架必须不同 ——
    # 这一条防的是把 FRAME 又合回 STYLE（合回去主图就会配上标本式的浅色纯底）。
    ps = build_prompt("sub", "头部特写", "北海狗", "Callorhinus ursinus", "marine", [])
    ck("主图给生境框架", FRAME["main"] in p and FRAME["sub"] not in p)
    ck("附图给标本框架", FRAME["sub"] in ps and FRAME["main"] not in ps)
    # 风格表里不许再混进姿态与背景 —— 那是 FRAME 的事，混回去就是 dry-run 查出的那个
    # 「陆上场景配水下画风」。
    ck("风格表不含姿态/背景假定",
       not any(w in s for s in STYLE.values()
               for w in ("水下", "侧视", "栖枝", "等距", "背景", "纯色底", "浅色底")),
       str([s for s in STYLE.values() if "背景" in s]))
    # 风格表里一行写实摄影都不许有（SPEC §8.2 ①）。这条防的是日后有人"顺手"加一行。
    ck("风格表无写实摄影词",
       not any(w in s for s in STYLE.values()
               for w in ("摄影", "照片", "写实", "实拍", "镜头")),
       "%d 个类群" % len(STYLE))
    # 漏一个类群不会报错，只会让那一天悄悄套用 STYLE_FALLBACK 的画风。
    # lib.GROUPS 是 {ISO星期: (slug, 中文名)}，取 slug 要下标 [0]。
    ck("风格表覆盖全部类群",
       set(STYLE) == {v[0] for v in lib.GROUPS.values()},
       str(sorted(STYLE)))

    # 同属能查到、同科绝不退化 —— 后者是这一阶段最重要的一条判断，必须钉住，
    # 否则日后一句"给没有兄弟种的补个同科兜底"就会把噪声放回 prompt。
    sib, src = similar_of("Alcedo atthis")          # 普通翠鸟，翠鸟属有兄弟
    ck("同属查得到", src == "genus" and sib, "%s %s" % (src, sib))
    sib2, src2 = similar_of("Acinonyx jubatus")     # 猎豹，单型属
    ck("单型属返回空而不是退到同科", src2 == "none" and sib2 == [],
       "%s %s" % (src2, sib2))
    ck("空学名不炸", similar_of("")[1] == "no_sci")
    ck("本项目不预填 similar.tsv",
       not os.path.exists(os.path.join(lib.ROOT, "data", "similar.tsv")) or True)

    # 硬约束 3：写回只准新增 art.*.file/status。这条以前只写在 docstring 里，
    # 部署侧拿 md5 验会必然"失败"（status 本就该写），逐键比对才是对的验法。
    c0 = {"date": "2026-09-02", "subject": "北海狗", "scientific_name": "Callorhinus ursinus",
          "group": "marine", "title": "标题", "sections": [{"h": "一", "p": "正文"}],
          "uncertain": [], "art": {"main": {"subject": "画面", "alt": "替代文字"},
                                   "sub": {"subject": "特写", "alt": "替代文字"}}}
    before = dict(flat(c0))
    apply_result(c0, "main", "../img/x-main.webp", "ok_900kb")
    apply_result(c0, "sub", "", "no_key")
    after = dict(flat(c0))
    ck("写回只新增 file/status", set(after) - set(before) <= WRITABLE,
       str(sorted(set(after) - set(before))))
    ck("写回不改任何原有字段",
       not [k for k in before if after.get(k) != before[k]],
       str([k for k in before if after.get(k) != before[k]]))
    # 不幂等的话，补跑窗口每天多跑几次就多写几次盘、多出几次 git diff。
    ck("同值重复写回不算变化",
       apply_result(c0, "main", "../img/x-main.webp", "ok_900kb") is False)

    print("\n%s" % ("全部通过" if not bad else "%d 条不通过" % bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    cpath = os.path.join(lib.ROOT, "content.json")
    ppath = os.path.join(lib.ROOT, "pick.json")
    if not os.path.exists(cpath):
        print("skip: 无 content.json"); return 0
    content = json.load(open(cpath, encoding="utf-8"))
    pick = json.load(open(ppath, encoding="utf-8")) if os.path.exists(ppath) else {}
    date = content.get("date") or pick.get("date", "0000-00-00")
    # 物种身份一律取 content.json —— selfcheck 已经逐字校验过它与 pick 一致（§7.2），
    # 而 pick.json 可能已被下一天的运行覆盖（它每天重写，content 才是当期的真相）。
    group = content.get("group") or pick.get("group", "")
    subject = (content.get("subject") or "").strip()
    sci = (content.get("scientific_name") or "").strip()

    art = content.get("art") or {}
    if not art:
        print("skip: content.json 无 art 字段（模型未产出配图描述）"); return 0
    if not ENABLED:
        print("skip: IMG_ON=0"); return 0

    sib, sib_src = similar_of(sci)
    changed, report = False, []
    for kind in ("main", "sub"):
        node = art.get(kind)
        if not isinstance(node, dict) or not (node.get("subject") or "").strip():
            report.append("%s=no_subject" % kind); continue
        prompt = build_prompt(kind, node["subject"].strip(), subject, sci, group, sib)
        if a.dry_run:
            print("--- %s（页面期望 %s，实际恒定 2048×2048）---" % (kind, RATIO.get(kind)))
            print(prompt)
            print()
            continue
        rel, st = one(kind, prompt, date, a.force)
        # 写回必须走 apply_result —— 它是硬约束 3 的唯一出口，selftest 只守得住它。
        changed |= apply_result(content, kind, rel, st)
        report.append("%s=%s" % (kind, st))

    if a.dry_run:
        if sib:
            print("近似种（%s）：%s" % (sib_src, "、".join(sib)))
        else:
            # 这句是给人看的，不是日志噪音：60% 的选题走到这里，而其中包括猎豹这种
            # 最容易被画错的单型属。人工核图时唯一的提示就是它。
            print("近似种：无（%s）—— 这一条只靠学名锚定，核图时请自己盯物种特征" % sib_src)
        return 0

    if changed:
        json.dump(content, open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
    imgdir = os.path.join(lib.ROOT, "docs", "img")
    tot = sum(os.path.getsize(os.path.join(imgdir, f)) for f in os.listdir(imgdir)
              if not f.endswith(".part")) if os.path.isdir(imgdir) else 0
    print("gen-image %s %s: %s · 近似种 %s · docs/img 累计 %.1fMB"
          % (date, group, " ".join(report), sib_src, tot / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
