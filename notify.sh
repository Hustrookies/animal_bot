#!/usr/bin/env bash
# 推送到微信 —— 0 token。
# 用法:
#   ./notify.sh --kind ok                  页面已验证，正文 + 读全文
#   ./notify.sh --kind degraded            页面未验证，正文 + 链接标注生成中
#   ./notify.sh --kind relive              先发过 degraded/nolink，页面后来好了 → 补一条短消息
#   ./notify.sh --kind nolink              push 失败，只有正文，无链接
#   ./notify.sh --kind alert --text "..."  纯告警
#   ./notify.sh --kind ok --date 2026-09-02   指定日期（默认今天）
#   ./notify.sh --kind ok --dry-run        只打印消息，不发、不记 .notified
#
# 设计要点一：消息正文自带 title + 物种 + summary，链接只是「读全文」。github.io 在国内
# 可达性是这套方案最弱的一环且无法靠工程修好；正文自带内容意味着链接打不开时这条消息
# 仍然是一张有用的卡片。这也是「超时也推」的前提。
#
# 设计要点二：正文一律读 data/content/<date>.json（按日期存档的那份），**不读根目录的
# content.json**。根目录那份是"agent 最近一次写的"，补发历史日期时它是别人家的内容 ——
# 那会发出一条链接指向 9/2、正文却是 9/3 的消息，而两半各自都"没错"。pick.json 同理
# （group_label 也从存档里取，不从 pick.json 取）。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
. ./envload.sh            # 不覆盖已有环境变量 —— WEIXIN_TARGET=测试群 ./notify.sh 必须真的生效

KIND=ok; TEXT=""; DATE=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --kind) KIND="$2"; shift 2 ;;
    --text) TEXT="$2"; shift 2 ;;
    --date) DATE="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "未知参数 $1" >&2; exit 2 ;;
  esac
done
case "$KIND" in
  ok|degraded|relive|nolink|alert) ;;
  *) echo "notify: 未知 kind「$KIND」" >&2; exit 2 ;;   # 拼错的 kind 必须报错，不能默认当 ok 发
esac

export TZ="${TZN:-Asia/Shanghai}"
TODAY=$(date +%F)
D="${DATE:-$TODAY}"
mkdir -p state
NF="state/$D.notified"

# ---------- 消息层的不变量：只许升级，不许降级或重复 ----------
# 阶段 10 要避开 wiki 的一个缺陷：它的 .notified 只看文件**是否存在**。于是某天 Pages 慢了
# 5 分钟以上，第一个窗口发出"页面生成中"，stage 置 notified，兜底窗口直接跳过 —— 页面
# 6 分钟后真的好了，用户手里却永远只有那条"生成中"。
# 这里改成按等级比较：nolink < degraded < ok = relive。
#   · ok 之后不会再发 degraded（不许降级：页面好过一次就不该说它没好）
#   · degraded / nolink 之后可以发 relive（允许升级，每天最多因此多一条）
#   · relive / ok 之后什么都不发（终态，有界）
# run.sh 那边也会判一次；两道都要，跟 stage 与 cached 那两道同构 —— run.sh 判漏了，
# 这里仍然发不出重复消息。
rank() { case "$1" in nolink) echo 1 ;; degraded) echo 2 ;; ok|relive) echo 3 ;; *) echo 0 ;; esac; }
if [ "$KIND" != alert ] && [ -f "$NF" ]; then
  PREV=$(cut -d' ' -f1 < "$NF")
  if [ "$(rank "$PREV")" -ge "$(rank "$KIND")" ]; then
    echo "notify: $D 已通知（$PREV），不发 $KIND"; exit 0
  fi
fi

# ---------- 组装 markdown ----------
if [ "$KIND" = alert ]; then
  MD="${TEXT:-⚠️ animal-bot 异常}"
else
  # IUCN 中文标签复用 render.py 的那张表，不在这里重写第二份 —— 两处各写一份，
  # 改了一处就会出现"页面写濒危、消息写易危"，而两边各自都自洽。
  IFS=$'\x01' read -r TITLE SUBJ SCI IUCN GLAB DLAB < <(python3 - "$D" <<'PY'
import json, os, sys
sys.path.insert(0, "src")
import render
d = sys.argv[1]
c = json.load(open(os.path.join("data", "content", d + ".json"), encoding="utf-8"))
one = lambda s: " ".join((s or "").split())
iu = (c.get("profile") or {}).get("iucn") or ""
y, m, dd = d.split("-")
print("\x01".join([
    one(c.get("title")), one(c.get("subject")), one(c.get("scientific_name")),
    render.IUCN.get(iu, ("", ""))[0], one(c.get("group_label")),
    "%d月%d日" % (int(m), int(dd)),
]))
PY
) || { echo "notify: 读不到 data/content/$D.json，无法组装消息" >&2; exit 1; }
  SUMMARY=$(python3 -c "
import json,sys
c=json.load(open('data/content/$D.json',encoding='utf-8'))
print(' '.join((c.get('summary') or '').split()))
")
  URL="${PAGE_BASE%/}/p/${D}.html"
  ARCH="${PAGE_BASE%/}/archive.html"
  LINE2="$SUBJ"
  [ -n "$SCI" ]  && LINE2="$LINE2 · $SCI"
  [ -n "$IUCN" ] && LINE2="$LINE2 · $IUCN"

  if [ "$KIND" = relive ]; then
    # 升级消息刻意做短：全文在上一条里已经发过了，这条只解决"那个链接现在能点了"。
    MD="✅ 今日页面已就绪：**${TITLE}**（${SUBJ}）
[读全文 →](${URL})"
  else
    MD="**${TITLE}**
${LINE2}

${SUMMARY}
"
    case "$KIND" in
      ok)       MD="${MD}
[读全文 →](${URL})" ;;
      degraded) MD="${MD}
[读全文 →](${URL})（页面生成中，1–2 分钟后可访问）" ;;
      nolink)   MD="${MD}
⚠️ 今日页面未发布，稍后补" ;;
    esac
    MD="${MD}
${GLAB} · ${DLAB} · [往期](${ARCH})"
  fi
fi

# ============================================================================
# 推送实现：openclaw 微信通道，账号/目标由 .env 提供（与 wiki-bot 同一套凭证，
# 同一个接收人 —— 早上会先后收到两条，wiki 一条、animal 一条）。
send() {
  local md="$1"
  [ -n "${WEIXIN_TARGET:-}" ] || { echo "notify: WEIXIN_TARGET 未设置" >&2; return 1; }
  openclaw message send --channel "${WEIXIN_CHANNEL:-openclaw-weixin}" \
    --account "${WEIXIN_ACCOUNT:-}" --target "$WEIXIN_TARGET" -m "$md"
}
# ============================================================================

# --dry-run 放在 send 之前、组装之后：联调时要看的正是"将要发出去的那串字"。
# 它**不写 .notified** —— 否则一次 dry-run 会把当天的真通知顶掉，那比不发更糟。
if [ "$DRY" = 1 ]; then
  printf '===== dry-run [%s] %s =====\n%s\n===== 以上未发送 =====\n' "$KIND" "$D" "$MD"
  exit 0
fi

if send "$MD"; then
  # 记 kind + buildid：kind 供上面那套等级判断用，buildid 让人事后能对上是哪一版页面。
  [ "$KIND" = alert ] || printf '%s %s\n' "$KIND" \
      "$(cat "state/${D}.buildid" 2>/dev/null || echo -)" > "$NF"
  echo "notify: 已发送 [$KIND]"
  rm -f "state/${D}.notify_pending"
else
  echo "notify: 发送失败 [$KIND]" >&2
  # 通知失败必须能在不重跑 agent、不重新出图的前提下重试 —— 记下想发的是哪一种。
  [ "$KIND" = alert ] || printf '%s\n' "$KIND" > "state/${D}.notify_pending"
  exit 1
fi
