#!/usr/bin/env python3
"""content.json → docs/p/<date>.html（+ index.html + archive.html）。0 token。

不写 posts.jsonl、不碰 git —— 那是 publish.sh 的事（推送成功才算数）。

── 与 wiki-bot 的实质差异：重渲入口是真的 ──

SPEC §12 阶段 8 的验收线是「data/content/*.json 可 0 token 重渲」。wiki-bot 的
render.py **没有任何读归档件的入口**，它只有 `--sample`（读 samples/*.json 输出到
/tmp）。也就是说那条性质在 wiki-bot 里今天没有任何一条命令能做到，是声称。

根因是它的归档件不自足：`publish.sh` 只存 content，而 `render_page` 需要
`cat_slug` / `cat_label` / `date_label` / `buildid` 四个 meta 字段 —— 全都不在归档件里
（要从 posts.jsonl 里捞回来）。

本项目**天然没有这个问题**，这是阶段 6 骨架设计的意外红利：content.json 自带
`date` / `group` / `group_label`，而 `selfcheck.py` 逐字校验过它们与 pick 一致。
所以渲染只需要 content.json 一个文件，`pick.json` 完全不参与 —— 于是
`--rebuild <date>` 与 `--rebuild-all` 是几行代码的事，而且能被真的跑一遍。

用法：
  ./render.py                    正常：读根目录 content.json
  ./render.py --rebuild 2026-09-02   从 data/content/<date>.json 重渲一期
  ./render.py --rebuild-all      重渲全部归档期次 + 归档索引（换模板后用它）
  ./render.py --archive-only     只重建归档索引
  ./render.py --selftest         模板与作用域的用例
"""
import argparse, datetime, html, json, os, re, sys
import lib

ROOT = lib.ROOT
NO_ESC = set()          # 本项目没有 CSS 注入（CSS 写在模板里），白名单为空

# 页面固定标注（SPEC §8.2 ②）。**不取任何 content 字段** —— agent 漏写就没有标注，
# 那是不可接受的失败方式。这里只留一份副本用于 selftest 逐字比对模板。
AI_NOTE = "插图由 AI 生成，仅供示意，非真实影像"

# IUCN 代码 → (中文, CSS class)。CSS class 决定徽章圆点颜色。
# 代码集合与 prompt.md 里给 agent 的枚举**必须一致**，selftest 钉住这一点：
# 多一个少一个都会让某个等级的页面掉进"有代码但没标签"的空白。
IUCN = {
    "EX": ("野生灭绝：已灭绝", "x"),
    "EW": ("野外灭绝", "ew"),
    "CR": ("极危", "cr"),
    "EN": ("濒危", "en"),
    "VU": ("易危", "vu"),
    "NT": ("近危", "nt"),
    "LC": ("无危", "lc"),
    "DD": ("数据缺乏", "dd"),
    "NE": ("未评估", "ne"),
}

# 名录卡的行：(content.profile 里的键, 页面上的标签)。**顺序就是页面顺序。**
# `biogeo` 不在这里 —— 它渲在页脚（它是本项目的排期维度，不是物种的生物学属性）。
PROFILE_ROWS = [
    ("body_length", "体长"),
    ("weight",      "体重"),
    ("lifespan",    "寿命"),
    ("diet",        "食性"),
    ("habitat",     "生境"),
    ("range_text",  "分布"),
]

WEEK = "一二三四五六日"


def date_label(d):
    """2026-09-02 → 9月2日 星期三。

    wiki-bot 的 date_label 是 pick.json 给的，本项目自己算 —— 因为重渲只读
    content.json，那时 pick.json 早被后面的日子覆盖了。日期是页面上唯一
    不能从内容推出来的东西，必须从文件名/字段算，不能依赖运行时状态。
    """
    try:
        t = datetime.date.fromisoformat(d)
    except ValueError:
        return d
    return "%d月%d日 星期%s" % (t.month, t.day, WEEK[t.weekday()])


# ---------------- 模板引擎（沿用 wiki-bot，逐字未改） ----------------
def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def render(tpl, sc):
    def each(mo):
        body = mo.group(2)
        return "".join(render(body, {**sc, **it} if isinstance(it, dict) else sc)
                       for it in (sc.get(mo.group(1)) or []))
    tpl = re.sub(r"<!--\s*each:(\w+)\s*-->(.*?)<!--\s*/each:\1\s*-->", each, tpl, flags=re.S)

    def cond(mo):
        v = sc.get(mo.group(1))
        return render(mo.group(2), sc) if v not in (None, "", [], {}, 0, False) else ""
    tpl = re.sub(r"<!--\s*if:(\w+)\s*-->(.*?)<!--\s*/if:\1\s*-->", cond, tpl, flags=re.S)

    def var(mo):
        k = mo.group(1)
        v = sc.get(k, "")
        return ("" if v is None else str(v)) if k in NO_ESC else esc(v)
    return re.sub(r"\{\{(\w+)\}\}", var, tpl)


def strlist(xs):
    """模板引擎的 each 取不到裸字符串项，统一包成 {"v": ...}"""
    return [{"v": x} for x in (xs or []) if str(x).strip()]


# ---------------- 作用域 ----------------
def build_scope(content, archive_rel="../archive.html"):
    """content.json → 模板作用域。**只吃 content 一个参数**，这是重渲能成立的原因。"""
    group = (content.get("group") or "").strip()
    prof = content.get("profile") or {}

    # 名录行：空串整行不出现。prompt.md 明确许可留空（薄锚下多数数字填不出来），
    # 所以"空值消失"不是异常处理，是**每天都会走的正常路径**——58 条薄锚就靠它。
    rows = [{"k": lb, "v": str(prof.get(key) or "").strip()}
            for key, lb in PROFILE_ROWS if str(prof.get(key) or "").strip()]

    code = (prof.get("iucn") or "").strip().upper()
    label, cls = IUCN.get(code, ("", ""))

    sc = {
        "group":       group,
        "group_label": (content.get("group_label") or "").strip(),
        "theme_color": lib.THEME_COLOR.get(group, "#3b2b1f"),
        "date_label":  date_label((content.get("date") or "").strip()),
        "archive_rel": archive_rel,
        "source":      "中文维基百科",
        "title":   (content.get("title") or "").strip(),
        "subject": (content.get("subject") or "").strip(),
        "sci":     (content.get("scientific_name") or "").strip(),
        "summary": (content.get("summary") or "").strip(),
        "sections": [s for s in (content.get("sections") or []) if s.get("p")],
        "uncertain": strlist(content.get("uncertain")),
        "tags":      strlist(content.get("tags")),
        "profile_rows": rows,
        "iucn_label": label,
        "iucn_class": cls,
        # 等级为空时来源也不渲染 —— 光写「zhwiki」而不说等级是什么，是噪音。
        "iucn_source": (str(prof.get("iucn_source") or "").strip() if label else ""),
        "biogeo": str(prof.get("biogeo") or "").strip(),
    }
    # 名录卡的出现条件是「有数字行**或**有等级」。模板引擎的 if 不支持 or，所以在这里
    # 合成一个键。**这是 selftest 查出来的**：原来整块由 `if:profile_rows` 控制，于是
    # 薄锚选题（体长体重寿命全填不出来，58/225）会把 IUCN 等级一起吞掉 ——
    # 而等级恰恰是动物条目最有价值的单一事实，也常常是薄锚里唯一有的那条。
    sc["has_profile"] = bool(rows or label)

    # 配图。gen-image.py 失败时写回的是**空字符串而不是缺键**（SPEC §13.12），
    # 所以必须 `or ""` 取真值 —— 写成 `n.get("file", "占位图")` 会拿到空串，
    # 默认值形同虚设，页面得到 src="" 而日志全绿。
    art = content.get("art") or {}
    for k in ("main", "sub"):
        n = art.get(k) or {}
        sc["img_%s" % k] = (n.get("file") or "").strip()
        sc["img_%s_alt" % k] = (n.get("alt") or "").strip()
    return sc


# 说明性 HTML 注释。**渲染后必须剥掉** —— 模板顶部那段实现说明会原样进入每个页面。
# 这不是体积问题：wiki-bot 的线上 index.html 至今带着「7 个类目共用这一份结构，观感差异
# 全部来自 themes/t-*.css」和整段 base.css 注释，等于把内部实现说明发到公网；更荒唐的是
# 注释里的 `{{key}}` 示例被模板引擎当成变量替换成了空串。
#
# 负向前查排除 `each:`/`if:` 标记：那两类也是 HTML 注释形式，但它们是**引擎的语法**，
# 剥早了 selfcheck_html 就查不出残留标记 —— 那条检查会静默失效。
_NOTE_COMMENT = re.compile(r"<!--(?!\s*/?(?:each|if):)(?:(?!-->).)*?-->", re.S)


def read_template():
    return open(os.path.join(ROOT, "template.html"), encoding="utf-8").read()


def page_buildid(content, tpl=None):
    """页面签名 = f(date, content, **模板**)。

    **模板必须参与，这是实测出来的。** 原来照 wiki-bot 只哈希 content，把
    `max-width:640px` 改成 641 重渲后页面确实变了，而 buildid 一个字符都没变。后果有两个：
    阶段 10 的 `wait_live` 会立刻"通过"（线上 buildid 本来就等于新算的这个），而线上
    样式还是旧的；publish.sh 若拿 buildid 判断要不要提交，还会整个跳过。
    页面 = 内容 + 模板，签名就该覆盖两者。

    仍然是内容派生、无 nonce —— 同内容同模板渲两次字节相同，空 commit 才跳得掉。
    """
    tpl = read_template() if tpl is None else tpl
    return lib.buildid((content.get("date") or "").strip(),
                       lib.canonical(content) + tpl.encode("utf-8"))


def render_page(content, archive_rel="../archive.html", buildid=None):
    sc = build_scope(content, archive_rel)
    tpl = read_template()
    sc["buildid"] = buildid or page_buildid(content, tpl)
    page = render(tpl, sc)
    page = _NOTE_COMMENT.sub("", page)
    # 剥注释会留下空行，压掉连续空行让页面干净（不影响渲染结果的确定性）
    return re.sub(r"\n{3,}", "\n\n", page)


def selfcheck_html(page, where):
    """每次渲染必跑。残留占位符与不配对标签都是**页面看着正常**的故障。"""
    bad = []
    left = sorted(set(re.findall(r"\{\{\w+\}\}", page)))
    if left:
        bad.append("残留占位符 %s" % left)
    marks = sorted(set(re.findall(r"<!--\s*(?:each|if|/each|/if):\w+\s*-->", page)))
    if marks:
        bad.append("残留模板标记 %s" % marks)
    for a, b in (("<section", "</section"), ("<div", "</div"),
                 ("<ul", "</ul"), ("<figure", "</figure")):
        if page.count(a) != page.count(b):
            bad.append("标签不配对 %s %d≠%d" % (a, page.count(a), page.count(b)))
    # 有图就必须有 AI 标注。这条是 SPEC §8.2 的硬要求，不能只靠模板里那行字
    # ——模板是会被人改的，改坏了要在渲染时就停下，而不是发布后靠人眼发现。
    if "<figure" in page and page.count(AI_NOTE) < page.count("<figure"):
        bad.append("有 %d 张图但 AI 标注只有 %d 处"
                   % (page.count("<figure"), page.count(AI_NOTE)))
    if bad:
        sys.exit("渲染自检失败（%s）：%s" % (where, "；".join(bad)))


# ---------------- 归档索引 ----------------
ARCHIVE_TPL = """<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>归档 · 每日一种野生动物</title><style>
:root{--paper:#f7f6f2;--card:#fff;--fg:#17181a;--dim:#54565b;--muted:#8a8c92;--line:#e6e4dd}
@media(prefers-color-scheme:dark){:root{--paper:#0e1013;--card:#181b20;--fg:#e8eaed;--dim:#a2a8b0;--muted:#7d838b;--line:#282c33}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--fg);
font:16px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:28px 16px 40px}
h1{font-size:21px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
ul{list-style:none;margin:0;padding:0}
li{background:var(--card);border-radius:12px;padding:14px 16px;margin-bottom:10px}
a{text-decoration:none;color:inherit;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.d{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
.c{font-size:11.5px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:1px 6px;flex:none}
.t{font-weight:600;font-size:16px}
.s{font-size:12.5px;color:var(--muted);font-style:italic;flex:none}
li p{margin:7px 0 0;font-size:13.5px;color:var(--dim);line-height:1.65}
</style></head><body><div class="wrap">
<h1>每日一种野生动物 · 归档</h1><p class="sub">共 {{n}} 期</p>
<ul>{{rows}}</ul></div></body></html>"""


def build_archive(posts):
    """posts.jsonl 记录列表 → 归档页 HTML。纯函数，便于用例直接验。

    **学名也列出来** —— 归档页是发现"两期其实是同一个种"最容易的地方，
    而中文名可以不同（薮猫／藪貓 那一类）。

    **`POSTS_FIELDS` 是与 publish.sh（阶段 9）的字段契约。** 其中
    `date`/`subject`/`scientific_name`/`title`/`summary` 已由 `pick.py` 在用（它靠后两者
    做 183 天去重），但 **`group_label` 今天还没有任何写入方** —— publish.sh 尚未实现。
    写在这里是为了让阶段 9 有个明确的对象可核，而不是等页面上类群标签全空了才发现。
    本项目已经栽过两次「取字段不核键名」（`iucn_raw`、`pick.py` 拿 wiki 当键查 material）。

    也正因为它没有写入方，**类群标签必须缺则整个 span 消失**而不是渲染成空串：
    `<span class="c">` 带边框，空着就是一排小空框。这是部署侧真渲染看出来的 ——
    24 条用例都过了，因为没有一条盯住"空值不留空壳"（§9b.4 说的是详情页的名录卡，
    归档页漏了同一条原则）。
    """
    rows = []
    for p in sorted(posts, key=lambda d: d.get("date", ""), reverse=True):
        sci = html.escape(p.get("scientific_name") or "")
        gl = html.escape(p.get("group_label") or "")
        rows.append(
            '<li><a href="p/%s.html">'
            '<span class="d">%s</span>%s'
            '<span class="t">%s</span>%s</a><p>%s</p></li>'
            % (html.escape(p.get("date", "")), html.escape(p.get("date", "")),
               ('<span class="c">%s</span>' % gl) if gl else "",
               html.escape(p.get("title") or ""),
               ('<span class="s">%s</span>' % sci) if sci else "",
               html.escape(p.get("summary") or "")))
    return (ARCHIVE_TPL.replace("{{n}}", str(len(posts)))
                       .replace("{{rows}}", "\n".join(rows)))


# 归档页依赖的 posts.jsonl 字段。前五个 pick.py 已在用，group_label 待 publish.sh 写入。
POSTS_FIELDS = ["date", "group_label", "title", "scientific_name", "summary"]


def render_archive():
    posts = lib.load_posts()
    out = os.path.join(ROOT, "docs", "archive.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(build_archive(posts))
    return len(posts)


# ---------------- 落盘 ----------------
def write_page(content, date, also_index):
    """渲染并写 docs/p/<date>.html；also_index 时同时更新 index.html。"""
    page = render_page(content)
    selfcheck_html(page, date)
    docs = os.path.join(ROOT, "docs")
    os.makedirs(os.path.join(docs, "p"), exist_ok=True)
    open(os.path.join(docs, "p", "%s.html" % date), "w", encoding="utf-8").write(page)
    if also_index:
        # index.html 在 docs/ 根，比 p/ 浅一层，两处相对路径都要改。
        open(os.path.join(docs, "index.html"), "w", encoding="utf-8").write(
            page.replace('href="../archive.html"', 'href="archive.html"')
                .replace('src="../img/', 'src="img/'))
    return page


def archive_path(date):
    return os.path.join(ROOT, "data", "content", "%s.json" % date)


# ---------------- 用例 ----------------
def selftest():
    bad = 0

    def ck(name, cond, info=""):
        nonlocal bad
        bad += 0 if cond else 1
        print("  %s %-32s %s" % ("OK  " if cond else "FAIL", name, info))

    import selfcheck
    good = json.loads(json.dumps(selfcheck.GOOD))    # 深拷贝，用例不污染模块常量
    good["art"]["main"]["file"] = "../img/x-main.webp"
    good["art"]["sub"]["file"] = "../img/x-sub.webp"

    page = render_page(good)
    ck("无残留占位符", not re.findall(r"\{\{\w+\}\}", page))
    ck("无残留模板标记",
       not re.findall(r"<!--\s*(?:each|if|/each|/if):\w+\s*-->", page))
    # 学名必须在页面上，且排斜体 —— 它是读者核对物种身份的唯一凭据（AI 图会画错）。
    ck("学名在页面上且斜体",
       '<span class="sci">Callorhinus ursinus</span>' in page)
    ck("两张图各有一条 AI 标注", page.count(AI_NOTE) == 2, str(page.count(AI_NOTE)))
    ck("主题色按类群注入", lib.THEME_COLOR["marine"] in page)
    ck("日期算成中文标签", "9月2日 星期三" in page, date_label("2026-09-02"))

    # ---- §13.12：失败时 file 是空串而不是缺键 ----
    nofile = json.loads(json.dumps(good))
    nofile["art"]["main"]["file"] = ""          # gen-image.py 的 no_key 路径
    nofile["art"]["sub"]["file"] = ""
    p2 = render_page(nofile)
    ck("空 file 时图整块消失而不是 src=''",
       "<figure" not in p2 and 'src=""' not in p2)
    ck("无图时不留孤零零的 AI 标注", AI_NOTE not in p2)
    # 缺键与空串必须同样处理 —— 前者是 agent 没写 art，后者是出图失败。
    delkey = json.loads(json.dumps(good))
    del delkey["art"]["main"]["file"]
    ck("缺 file 键与空串同样处理", "<figure" in render_page(delkey))

    # ---- 薄锚：profile 全空 ----
    thin = json.loads(json.dumps(good))
    thin["profile"] = {k: "" for k in thin["profile"]}
    p3 = render_page(thin)
    ck("profile 全空则名录卡整块消失", 'class="profile"' not in p3)
    ck("名录卡消失后页面仍自洽", "{{" not in p3 and "<h1>" in p3)
    # 只有 IUCN 而没有任何数字时，也要能出卡（等级是最有价值的单一事实）
    only = json.loads(json.dumps(thin))
    only["profile"]["iucn"] = "VU"
    only["profile"]["iucn_source"] = "zhwiki"
    p4 = render_page(only)
    ck("只有 IUCN 也出名录卡", 'class="profile"' in p4 and "易危" in p4)
    ck("IUCN 徽章带等级 class", 'class="iucn vu"' in p4, "vu")

    # IUCN 表必须与 prompt.md 给 agent 的枚举一致：多一个少一个都会让某个等级
    # 渲成空白徽章，而那一天的页面看着只是"少了一行"。
    pm = open(os.path.join(ROOT, "prompt.md"), encoding="utf-8").read()
    mo = re.search(r"只能是\s*([A-Z/]+)\s*之一", pm)
    codes = set(mo.group(1).split("/")) if mo else set()
    ck("IUCN 代码表与 prompt 一致", codes == set(IUCN),
       "prompt=%d 表=%d" % (len(codes), len(IUCN)))

    # 转义：agent 写出 < > & 时不能破页。这是每天由模型产出的文本，必须假定会有。
    ev = json.loads(json.dumps(good))
    ev["title"] = '<script>alert(1)</script>与"引号"'
    pe = render_page(ev)
    ck("标题里的标签被转义", "<script>" not in pe and "&lt;script&gt;" in pe)

    # ---- 重渲：只吃 content 一个文件 ----
    ck("build_scope 不需要 pick.json",
       build_scope(good)["group_label"] == good["group_label"])
    # 同一份内容渲两次必须逐字节相同，否则 buildid 会变、git 每天都有 diff、
    # wait_live 也永远等不到匹配。
    ck("同内容重渲字节相同", render_page(good) == page)

    # ---- buildid 必须覆盖模板 ----
    # 实测过：只哈希 content 时，把 CSS 的 max-width 从 640 改成 641，页面真的变了
    # 而 buildid 一个字符没变 —— wait_live 会立刻"通过"，publish 还可能整个跳过。
    ck("改模板则 buildid 变",
       page_buildid(good, "<html>A</html>") != page_buildid(good, "<html>B</html>"))
    ck("同模板同内容 buildid 稳定",
       page_buildid(good, "<html>A</html>") == page_buildid(good, "<html>A</html>"))
    ck("改内容则 buildid 变",
       page_buildid(good, "x") != page_buildid({**good, "title": "换个标题"}, "x"))

    # ---- 归档页：字段契约 ----
    # 完整记录必须每个字段都出现在页面上。缺 group_label 时不能崩 —— 那是 publish.sh
    # 还没写这个键的情形，页面降级成没有类群标签，但不能 500 也不能漏掉整行。
    full = {"date": "2026-09-02", "group_label": "海洋与两栖", "title": "标题",
            "scientific_name": "Callorhinus ursinus", "summary": "摘要"}
    ap_ = build_archive([full])
    ck("归档页含全部契约字段",
       all(str(full[k]) in ap_ for k in POSTS_FIELDS),
       str([k for k in POSTS_FIELDS if str(full[k]) not in ap_]))
    ck("归档页缺 group_label 不崩",
       "标题" in build_archive([{k: v for k, v in full.items() if k != "group_label"}]))
    # 上面那条只保证"不崩"，而部署侧真渲染看出来它还留下一个空的 <span class="c">——
    # 那个 span 带边框，空着就是一排小空框，而 group_label 今天没有写入方，所以是**每一行**。
    # 不崩和不难看是两条，得分开钉。
    ck("归档页空 group_label 不留空壳",
       'class="c"' not in build_archive([{k: v for k, v in full.items()
                                          if k != "group_label"}]))
    ck("归档页按日期倒序",
       build_archive([{"date": "2026-01-01", "title": "旧"},
                      {"date": "2026-09-02", "title": "新"}]).index("新")
       < build_archive([{"date": "2026-01-01", "title": "旧"},
                        {"date": "2026-09-02", "title": "新"}]).index("旧"))
    ck("归档页也转义", "&lt;b&gt;" in build_archive([{"date": "d", "title": "<b>"}]))

    print("\n%s" % ("全部通过" if not bad else "%d 条不通过" % bad))
    return 1 if bad else 0


# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", metavar="DATE", help="从 data/content/<DATE>.json 重渲")
    ap.add_argument("--rebuild-all", action="store_true", help="重渲全部归档期次")
    ap.add_argument("--archive-only", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    if a.archive_only:
        print("archive rebuilt: %d 期" % render_archive())
        return 0

    # 重渲全部：换模板/改 CSS 之后用它，0 token 重刷全站。
    # 最新那一期额外写 index.html —— 按日期排序取最后一个，不依赖 pick.json。
    if a.rebuild_all:
        d = os.path.join(ROOT, "data", "content")
        dates = sorted(f[:-5] for f in os.listdir(d)) if os.path.isdir(d) else []
        if not dates:
            print("skip: data/content/ 是空的，还没有归档期次"); return 0
        for i, date in enumerate(dates):
            content = json.load(open(archive_path(date), encoding="utf-8"))
            write_page(content, date, also_index=(i == len(dates) - 1))
        print("rebuilt %d 期（%s … %s）· archive=%d 期"
              % (len(dates), dates[0], dates[-1], render_archive()))
        return 0

    if a.rebuild:
        p = archive_path(a.rebuild)
        if not os.path.exists(p):
            print("skip: 没有 %s" % p); return 0
        content = json.load(open(p, encoding="utf-8"))
        write_page(content, a.rebuild, also_index=False)
        print("rebuilt %s（未动 index.html）" % a.rebuild)
        return 0

    cpath = os.path.join(ROOT, "content.json")
    if not os.path.exists(cpath):
        print("skip: 无 content.json"); return 0
    content = json.load(open(cpath, encoding="utf-8"))
    date = (content.get("date") or "").strip()
    if not date:
        sys.exit("content.json 缺 date，无法确定页面路径")

    page = write_page(content, date, also_index=True)
    # 与 render_page 内部**同一个函数**算出来的值，不再各自实现一遍
    # —— 两处独立算同一个东西，就是"两处对不上"的标准起点。
    bid = page_buildid(content)

    # buildid 落盘给 wait_live 用（阶段 10）。内容派生、无 nonce。
    os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
    open(os.path.join(ROOT, "state", "%s.buildid" % date), "w").write(bid)

    n = render_archive()
    print("rendered %s %s buildid=%s %.1fKB archive=%d 期"
          % (date, content.get("group", ""), bid, len(page.encode()) / 1024, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
