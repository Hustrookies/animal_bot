#!/bin/bash
# animal-bot 流水线入口。0 token 的状态机，只有 openclaw 那一步花 token。
#
#   ./run.sh daily             取题 → agent 写稿 → 校验 → 配图 → 渲染 → 发布（不通知）
#   ./run.sh notify            等 Pages 生效 → 推微信（0 token，可反复跑）
#   ./run.sh once              daily + verify + notify
#   ./run.sh verify            0 token 终检：页面/buildid/jsonl/git 四项对齐
#   ./run.sh refill            逐类群分批补 queue.tsv
#   REFILL_ONLY=aves ./run.sh refill    # 只补一个类群（联调）
#
# stage 状态机（state/<date>.stage）：
#   none → content → imaged → rendered → pushed → notified
# 重复执行按 stage 分流，已完成直接 exit 0 —— 这是四个 cron 补跑窗口能安全存在的前提，
# 也是「连跑两次不重复计费」的实现方式（配图那一步还有第二道：文件已存在则 cached）。
set -uo pipefail
cd "$(dirname "$0")" || exit 1
. ./envload.sh    # 读 .env 但不覆盖已有环境变量；理由写在 envload.sh 里（阶段 9 被咬过一次）
export TZ="${TZN:-Asia/Shanghai}"

MODE="${1:-}"
TODAY=$(date +%F)
mkdir -p logs data state docs/p data/content
LOG="logs/$(date +%F).log"
ST="state/$TODAY.stage"
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
# 告警：落日志 + 推微信，按 key 限流。
# 上限是 **2 而不是 1**：第一条是"今天出问题了"，第二条是"兜底窗口重跑仍然失败"——
# 后者才是真正需要人动手的信号。只发一条的话，"重跑也没救回来"这件事会完全静默，
# 而那正是最需要知道的一天。
ALERT_MAX="${ALERT_MAX:-2}"
alert() {
  local key="$1" msg="$2" f n=0
  f="state/$TODAY.alert.$key"
  log "[ALERT $key] $msg"
  [ -f "$f" ] && n=$(cat "$f" 2>/dev/null || echo 0)
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  if [ "$n" -ge "$ALERT_MAX" ]; then
    log "[ALERT $key] 已达上限 $ALERT_MAX，本条只落日志"; return 0
  fi
  printf '%s\n' "$((n+1))" > "$f"
  # 告警推送失败**不能**让主流程挂掉：告警是附加动作，日志里已经留下了。
  ./notify.sh --kind alert --text "⚠️ animal-bot $TODAY：$msg" >>"$LOG" 2>&1 \
    || log "[ALERT $key] 告警推送失败（内容已在日志里）"
}
stage() { cat "$ST" 2>/dev/null || echo none; }
set_stage() { printf '%s\n' "$1" > "$ST.tmp" && mv -f "$ST.tmp" "$ST"; }  # 原子，防 kill -9 截断

qn() { python3 -c 'import sys;sys.path.insert(0,"src");import lib;print(len(lib.load_queue()))'; }

# 防并发（**不是**防重复，那是 stage 的事）。verify 不加锁：它只读，而且「因为 daily
# 正在跑就静默 exit 0」正好是终检最不该有的行为。
lock() {
  exec 9>"/tmp/animal-bot.lock"
  flock -n 9 || { log "上一次仍在运行，跳过"; exit 0; }
}

# ---------------------------------------------------------------- verify（0 token）
# 本地终检：不发请求、不花 token，只查四样东西是否互相对齐。
# 它存在的理由是阶段 8 那次教训 —— 每一步自己都报成功（render 打印 rendered、
# publish 打印 push ok），拼起来仍然可能是错的（模板改了没重渲、jsonl 追了两行、
# commit 了但没推）。逐步的 exit 0 不等于这一期真的上线了。
verify() {
  local d="${1:-$TODAY}" rc=0 page bid raw
  page="docs/p/$d.html"

  [ -s "$page" ] || { log "verify: 缺页面 $page"; rc=1; }
  if [ -s "state/$d.buildid" ]; then
    bid=$(cat "state/$d.buildid")
    # buildid 同时钉住模板：render.py 把它算进 hash，模板改了没重渲这里就对不上。
    grep -q "$bid" "$page" 2>/dev/null || { log "verify: 页面里没有本期 buildid $bid（改了模板没重渲？）"; rc=1; }
    # index.html 是**最新一期**的副本，只有查今天才该拿它比 —— 拿它去验一个历史日期
    # 必然对不上，那是查法错了，不是发布错了。
    [ "$d" = "$TODAY" ] && { grep -q "$bid" docs/index.html 2>/dev/null || {
      log "verify: index.html 不是本期（buildid 不符）"; rc=1; }; }
  else
    log "verify: 缺 state/$d.buildid（这一期没渲染过）"; rc=1
  fi
  [ -s "data/content/$d.json" ] || { log "verify: 缺 data/content/$d.json，日后换模板无法 0 token 重渲"; rc=1; }

  # **数原始行，不用 lib.load_posts。** load_posts 按 (date,subject) 去重，重复追加的
  # 那一行会被它悄悄合掉 —— 而重复追加正是这里要查的东西。用去重后的视图验幂等，
  # 等于拿一块滤镜去找它专门滤掉的脏东西。
  raw=$(python3 -c "
import json
n = 0
try:
    f = open('data/posts.jsonl', encoding='utf-8')
except OSError:
    print(-1); raise SystemExit
for ln in f:
    ln = ln.strip()
    if not ln:
        continue
    try:
        if json.loads(ln).get('date') == '$d':
            n += 1
    except Exception:
        pass
print(n)")
  case "$raw" in
    1)  ;;
    0)  log "verify: posts.jsonl 里没有 $d 这一期（半年去重不认识它，下次可能重推同一物种）"; rc=1 ;;
    -1) log "verify: 读不到 data/posts.jsonl"; rc=1 ;;
    *)  log "verify: posts.jsonl 里 $d 有 $raw 行，幂等被破坏了"; rc=1 ;;
  esac
  # 契约那半交给 publish.py --check（lib.POST_FIELDS 是唯一定义处，这里不复制一份）
  python3 src/publish.py --check >/dev/null 2>&1 || {
    log "verify: posts.jsonl 有记录不合字段契约，详见 src/publish.py --check"; rc=1; }

  # 记录里的 buildid 必须**属于这一期**（形如 <date>-xxxxxxxx）。
  # 只断言归属、不断言等于 state 文件：那是首发签名，换过模板重渲之后本来就会不同，
  # 拿它当相等条件会让终检在一件没人能补救的事上长期发红。
  python3 -c "
import json, sys
for ln in open('data/posts.jsonl', encoding='utf-8'):
    ln = ln.strip()
    if not ln:
        continue
    try:
        d = json.loads(ln)
    except Exception:
        continue
    if d.get('date') == '$d':
        sys.exit(0 if (d.get('buildid') or '').startswith('$d-') else 1)
sys.exit(0)" 2>/dev/null || {
    log "verify: posts.jsonl 里 $d 的 buildid 不属于这一期（阶段 10 拿它对不上任何页面）"; rc=1; }

  if git rev-parse --git-dir >/dev/null 2>&1; then
    git update-index -q --refresh 2>/dev/null
    local dirty
    dirty=$(git status --porcelain -- docs data/posts.jsonl data/content 2>/dev/null | head -3 | tr '\n' ' ')
    [ -n "$dirty" ] && { log "verify: 发布产物有未提交改动：$dirty"; rc=1; }
    # commit 成功而 push 失败是最容易漏的一种：本地一切正常，线上什么都没变。
    # 先确认有上游再比 —— 没配上游时 `git diff @{u}` 本身报错，那会被误读成
    # 「有未推送的 commit」，把一个配置问题伪装成发布问题。
    if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
      git diff --quiet '@{u}' HEAD 2>/dev/null || { log "verify: 本地有未推送的 commit"; rc=1; }
    else
      log "verify: 当前分支没有上游（push 从未成功过？）"; rc=1
    fi
  fi

  [ "$rc" = 0 ] && log "verify $d: 页面/buildid/jsonl/git 四项对齐"
  return "$rc"
}

if [ "$MODE" = verify ]; then
  verify "${2:-$TODAY}"; exit $?
fi

# ---------------------------------------------------------------- notify（0 token）
# 等 Pages 生效 → 推微信。可反复跑，用法 `./run.sh notify [date]`。
#
# **不加 lock。** daily 那把锁要持两分钟以上（agent 写稿），notify 若因为拿不到锁就
# 静默 exit 0，那个窗口就白费了 —— 而它只发 GET 和一条消息，没有并发危险。防重复靠
# stage 加 notify.sh 里的等级判断，不靠锁。
if [ "$MODE" = notify ]; then
  D="${2:-$TODAY}"
  ST="state/$D.stage"        # 允许补发历史日期：stage()/set_stage 都读这个变量
  log "=== run.sh notify start (stage=$(stage), date=$D) ==="

  # 上一轮通知发的是哪一种 —— 这决定还要不要再探：
  #   ok / relive        终态，用户手里已经有一条带好链接的消息 → 什么都不做
  #   degraded / nolink  当时页面确实没好 → 允许再探，好了就补一条短消息（relive）
  # wiki-bot 只看 .notified 文件**是否存在**，于是"Pages 慢了 6 分钟"那天用户永远
  # 只剩那条"页面生成中"，而链接其实早就能点了 —— 兜底窗口存在却什么都救不了。
  PREVK=""
  [ -f "state/$D.notified" ] && PREVK=$(cut -d' ' -f1 < "state/$D.notified")
  case "$PREVK" in
    ok|relive) log "notify: $D 已通知（$PREVK），跳过"; exit 0 ;;
  esac

  case "$(stage)" in
    none|content|imaged)
      # 还没渲染 → 没有页面也没有 buildid。此刻能发的只有告警，不是内容；
      # 而"今天没出稿"这件事由 daily 自己的 alert 负责，这里安静退出就好。
      log "notify: $D 尚未渲染（stage=$(stage)），无可通知内容"; exit 0 ;;
  esac

  BID=$(cat "state/$D.buildid" 2>/dev/null || echo "")
  # 没有 buildid 就无法判断线上那一页是不是**这一期** —— 只查 200 会让昨天的页面
  # 把今天的构建失败盖过去。宁可报错也不发一条无法核实的消息。
  [ -n "$BID" ] || { log "notify: 缺 state/$D.buildid，无法确认线上是不是这一期"; exit 1; }

  if [ "$(stage)" = rendered ]; then
    # 渲染成功但 push 没成 → 正文照发，只是没有链接。内容比链接重要：
    # 消息本身就是一张完整卡片，这是「超时也推」的同一个理由。
    ./notify.sh --kind nolink --date "$D" && set_stage notified
    exit 0
  fi

  URL="${PAGE_BASE%/}/p/$D.html"
  RES=$(./wait_live.sh "$URL" "$BID" "${WAIT_BUDGET:-300}"); rc=$?
  log "wait_live → $RES (rc=$rc)"
  case "$RES" in
    LIVE|LIVE_DIRTY_CACHE)
      case "$PREVK" in
        degraded|nolink) ./notify.sh --kind relive --date "$D" && set_stage notified ;;
        *)               ./notify.sh --kind ok     --date "$D" && set_stage notified ;;
      esac ;;
    STALE|NOT_FOUND)
      if [ -n "$PREVK" ]; then
        # 已经发过降级消息了，第二条一样的没有信息量 —— 但"重跑仍未生效"要进告警，
        # 那才是需要人去看 Pages 构建日志的信号（ALERT_MAX=2 正是为这一条留的额度）。
        log "notify: $D 仍未生效（$RES），已发过 $PREVK，不重复发内容"
        alert pages "页面重跑仍未生效（$RES），上一条消息是 $PREVK，请查 Pages 构建"
      else
        ./notify.sh --kind degraded --date "$D" && set_stage notified
        alert pages "页面未生效（$RES），已发降级消息（正文完整，链接可能还要等）"
      fi ;;
    NET_DOWN)
      # 本机出网就有问题，此时微信大概也发不出去。**不置 notified**，留给下个窗口。
      log "notify: 本机出网异常，不通知，留待下个窗口重试"; exit 2 ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------- refill
if [ "$MODE" = refill ]; then
  lock
  ADD="data/queue.add.tsv"
  PFILE="data/.refill-prompt.txt"
  BATCH="${REFILL_BATCH:-8}"
  TARGET="${REFILL_TARGET:-32}"
  ROUNDS="${REFILL_ROUNDS:-8}"
  BUDGET="${REFILL_BUDGET:-4800}"
  AGENT_TIMEOUT="${AGENT_TIMEOUT:-300}"
  AGENT_ID="${ANIMAL_AGENT_ID:-animal}"
  T0=$(date +%s)
  added=0; thin=""

  # 类群顺序照 lib.GROUPS 的 ISO 星期序
  ALL=$(python3 -c 'import sys;sys.path.insert(0,"src");import lib;print(" ".join(lib.GROUPS[i][0] for i in sorted(lib.GROUPS)))')
  for slug in ${REFILL_ONLY:-$ALL}; do
    r=0
    while [ "$r" -lt "$ROUNDS" ]; do
      r=$((r + 1))
      if [ $(( $(date +%s) - T0 )) -gt "$BUDGET" ]; then
        log "refill 预算 ${BUDGET}s 用尽，提前收尾"; break 2
      fi

      have=$(python3 -c "
import sys;sys.path.insert(0,'src');import lib
print(sum(1 for q in lib.load_queue() if q['group']=='$slug'))")
      ask=$(( TARGET - have ))
      [ "$ask" -gt "$BATCH" ] && ask=$BATCH
      [ "$ask" -gt 0 ] || break

      # 批次清单。rc=3 表示 ready.jsonl 里这个类群已经没有可认领的了 ——
      # 那是「池子见底」，不是失败，但要说出来：否则会被当成 agent 不干活。
      if ! python3 src/build-queue.py --emit "$ask" --group "$slug" > "$PFILE.batch" 2>>"$LOG"; then
        log "refill $slug: ready.jsonl 已无可认领条目（要 $ask 行），需先跑 build-queue.py"
        thin="$thin $slug"; break
      fi
      cat refill-prompt.md "$PFILE.batch" > "$PFILE"

      before=$(qn)
      # 先删，让「文件存在」本身成为「本次真的写了它」的证据
      rm -f "$ADD" "$ADD.ok"

      # **不加 `|| true`。** wiki-bot 就是用 `|| true` 吞掉 agent 退出码，加上
      # 只数行数不比增量，于是 agent 超时零产出时脚本照样打印「refill 完成」并
      # exit 0，失败完全静默（已记录为项目缺陷经验）。这里退出码要留着报出来。
      timeout "$AGENT_TIMEOUT" openclaw agent --agent "$AGENT_ID" --message-file "$PFILE"
      rc=$?
      [ "$rc" -ne 0 ] && log "refill $slug: agent 退出码 $rc（124=超时）"

      if [ ! -s "$ADD" ]; then
        log "refill $slug: agent 无产出（本批要 $ask 行，rc=$rc）"
        thin="$thin $slug"; break
      fi
      # 只看 refill-check 的**退出码**，不 grep 它的输出。
      # 原来写的是 `| grep -q '合格'` —— 而全废时它打印的是「无一合格」，**含「合格」**，
      # 子串匹配为真。当时只因为 pipefail 恰好把 python 的 exit 1 透出来才没出错，
      # 也就是说这个判据一直是坏的、被另一个判据掩盖着。
      # 「子串匹配当相等用」是这个项目第五次踩的同一个坑（黑名单误杀东北虎、
      # falcatus 含 catus、索引里「虎」命中「虎鲸」、重定向繁简字面比较）。
      # 退出码才是契约，文本不是。
      python3 src/refill-check.py "$ADD" "$slug" 2>&1 | tee -a "$LOG"
      chk=${PIPESTATUS[0]}
      if [ "$chk" -ne 0 ]; then
        log "refill $slug: 验收未通过（rc=$chk），本批丢弃"
        thin="$thin $slug"; rm -f "$ADD" "$ADD.ok"; break
      fi

      # queue.tsv 末尾没有换行时直接 cat 会把两行粘成一行
      [ -s data/queue.tsv ] && [ -n "$(tail -c1 data/queue.tsv)" ] && printf '\n' >> data/queue.tsv
      cat "$ADD.ok" >> data/queue.tsv
      rm -f "$ADD" "$ADD.ok"

      after=$(qn)
      inc=$((after - before))
      # 比对**增量**，不是数行数。零增量说明验收放行了但没真写进去，那是脚本 bug，
      # 必须响 —— 这正是 wiki-bot 静默失败的那个位置。
      if [ "$inc" -le 0 ]; then
        alert refill "$slug 验收通过却零增量（$before → $after），流水线有 bug"
        exit 1
      fi
      log "refill $slug: $before → $after 行 (+$inc)"
      added=$((added + inc))
    done
  done

  rm -f "$PFILE" "$PFILE.batch"
  n=$(qn)
  if [ "$added" -eq 0 ]; then
    alert refill "补池零产出，水位仍 $n 条（未补足：${thin:-全部}）"
    log "refill 零产出，queue.tsv 仍 $n 行"
    exit 1
  fi
  log "refill 完成 +$added 行，queue.tsv 现有 $n 行${thin:+（未补足：$thin）}"
  exit 0
fi

# ---------------------------------------------------------------- daily / once
[ "$MODE" = daily ] || [ "$MODE" = once ] || {
  echo "用法: $0 daily|once|notify [date]|verify [date]|refill" >&2; exit 2; }

lock
log "=== run.sh $MODE start (stage=$(stage)) ==="

# stage 分流。**这就是「连跑两次不重复计费」的第一道**：第二次进来 stage 已是 pushed，
# 下面每个 if 都不成立，agent、出图、渲染、发布全部跳过。第二道在 gen-image.py 内部
# （文件已存在则 cached），两道都要有 —— state/ 被清掉过的那天只剩第二道兜着。
# **notified 必须在这张表里。** 漏了它，通知发完之后再跑 daily（08:49 那个补跑窗口就会）
# 会当成"今天还没出稿"，agent 重写一篇、覆盖 content.json、图重出一遍 —— 白花两份钱，
# 而日志一路全绿。
case "$(stage)" in
  content|imaged|rendered|pushed|notified) SKIP_AGENT=1 ;;
  *)                                       SKIP_AGENT=0 ;;
esac

if [ "$SKIP_AGENT" = 0 ]; then
  SKIP=()
  try=0
  while :; do
    try=$((try + 1))
    PICK=$(python3 src/pick.py "${SKIP[@]}") || {
      r=$(python3 -c "import json;print(json.load(open('pick.json')).get('reason',''))" 2>/dev/null || echo 取题失败)
      alert pick "${r:0:60}"; exit 1; }

    # 多行 prompt 走 --message-file，不塞命令行（refill 分支同一写法）。
    PFILE="data/.daily-prompt.txt"
    printf '%s\n\n%s\n' "$(cat prompt.md)" "$PICK" > "$PFILE"

    # 先删旧产物，让「文件存在」本身成为「本次真的写了它」的证据。
    # 不信 agent 的退出码 —— headless agent 退 0 却什么都没写是常见的。
    rm -f content.json
    out=$(timeout "${AGENT_TIMEOUT:-300}" openclaw agent --agent "${ANIMAL_AGENT_ID:-animal}" \
            --message-file "$PFILE" 2>&1); rc=$?
    printf '%s\n' "$out" | tail -20 >> "$LOG"
    [ "$rc" -ne 0 ] && log "daily: agent 退出码 $rc（124=超时）"

    [ -s content.json ] && break

    # 判 DUP 用**行首锚定**，不是子串。`grep -q DUP` 会被正文里任何一处 DUP 命中，
    # 而「子串匹配当相等用」是这个项目第六次要防的同一个坑（黑名单误杀东北虎、
    # falcatus 含 catus、索引「虎」命中「虎鲸」、重定向繁简字面比较、refill grep 合格）。
    if [ "$try" -lt 2 ] && printf '%s\n' "$out" | grep -qE '^[[:space:]]*DUP([[:space:]]|$)'; then
      subj=$(python3 -c "import json;print(json.load(open('pick.json'))['topic']['subject'])" 2>/dev/null || echo "")
      # DUP 意味着两条 queue 行其实是同一个物种、而学名没识破 —— 那是队列的缺陷，
      # 不只是今天的意外，要留告警让人去合并那两行。wiki-bot 这条路径根本没做。
      alert dup "agent 判「$subj」与近期重复（学名没识破，queue 里可能有两行同一物种，需人工合并）"
      log "daily: 换题重取一次（--skip $subj）"
      [ -n "$subj" ] && SKIP+=(--skip "$subj")
      continue
    fi
    # agent 判 ABORT 而故意不写文件是正常路径，不该告警。
    log "daily: agent 未产出 content.json（ABORT 或超时），今日不推送"
    set_stage none; rm -f "$PFILE"; exit 0
  done
  rm -f "$PFILE"

  # 留一份失败样本：下个 cron 窗口补跑时会 rm -f content.json，不留证据就没法
  # 事后定位到底错在哪个字段。
  python3 src/selfcheck.py || {
    cp -f content.json "state/$TODAY.content.bad" 2>/dev/null || true
    alert schema "content.json 校验不通过（样本留在 state/$TODAY.content.bad）"; exit 1; }
  set_stage content
fi

if [ "$(stage)" = content ]; then
  # 失败不致命：配图是增益不是依赖，gen-image.py 自己保证 exit 0 与幂等。
  python3 src/gen-image.py || log "gen-image 异常退出（已忽略，无图继续）"
  set_stage imaged
fi

if [ "$(stage)" = imaged ]; then
  python3 src/render.py || { alert render "渲染失败"; exit 1; }
  set_stage rendered
fi

if [ "$(stage)" = rendered ]; then
  # 失败时**不回退 stage**：内容和图都已落盘，下个补跑窗口从 rendered 接着推就行。
  # 把 stage 退回去会让 agent 重跑一次，白花 token 重写同一篇。
  if ./publish.sh; then
    set_stage pushed
  else
    alert git "发布失败，内容已保留在 rendered，下个窗口重试"; exit 1
  fi
fi

log "=== $MODE 完成 stage=$(stage) ==="

RC=0
if [ "$MODE" = once ]; then
  verify "$TODAY" || { RC=1
    alert verify "终检未通过，详见日志（通知照发，由 wait_live 决定是正式消息还是降级）"; }
  # 通知是独立一步，不因终检红就跳过 —— notify 会亲自探线上页面，探不到自然发降级消息。
  # 反过来"终检红就不通知"会让用户在真出问题的那天什么都收不到，那是最糟的组合。
  # 先放锁再调：notify 里的 wait_live 最多等 5 分钟，握着锁等会让下一个补跑窗口白跳过。
  flock -u 9
  ./run.sh notify "$TODAY" || RC=1
fi

# 队列低水位自愈：月度 refill 漏了也不断供。判据用 pick.py 的 low（= 任一类群见底
# 或全池见底），不是只看全池 —— 单类群饥饿会被全池阈值整个漏掉（probe/simulate.py
# --starve amphibia:26 实测全池 220 > QUEUE_LOW=200、告警不响，而那一年断更 3 天）。
if [ -f pick.json ] && python3 -c "import json,sys;sys.exit(0 if json.load(open('pick.json')).get('low') else 1)" 2>/dev/null; then
  log "队列低水位，追加一次 refill"
  # 必须先放锁：refill 是子进程，flock -n 拿不到父进程正持着的同一把锁 ——
  # wiki-bot 这条自愈路径每次都只在日志里留下「上一次仍在运行，跳过」，从未真正跑过。
  flock -u 9
  REFILL_BUDGET="${REFILL_SELFHEAL_BUDGET:-600}" ./run.sh refill || true
fi

if [ -d docs/img ]; then
  IMGMB=$(du -sm docs/img 2>/dev/null | cut -f1)
  log "docs/img 累计 ${IMGMB}MB"
  [ "${IMGMB:-0}" -gt 700 ] && alert size "docs/img 已 ${IMGMB}MB，接近 GitHub 仓库软限制，需处理"
fi

find logs -name '*.log' -mtime +14 -delete 2>/dev/null
exit "$RC"
