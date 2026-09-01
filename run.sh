#!/bin/bash
# animal-bot 流水线入口。
#
# 目前只实现 refill（阶段 3 补池）。daily / notify / once 待阶段 9 ——
# 未实现的 mode **明确 exit 2**，不静默走空：wiki-bot 的教训就是「什么都没干却
# exit 0」这类信号最贵，它会让 cron 每天安静地失败。
#
#   ./run.sh refill            # 逐类群分批补 queue.tsv
#   REFILL_ONLY=aves ./run.sh refill    # 只补一个类群（联调）
set -uo pipefail
cd "$(dirname "$0")" || exit 1
[ -f .env ] && . ./.env

MODE="${1:-}"
mkdir -p logs data
LOG="logs/$(date +%F).log"
log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
alert() { log "[ALERT $1] $2"; }     # 通知通道待阶段 10，先只落日志

qn() { python3 -c 'import sys;sys.path.insert(0,"src");import lib;print(len(lib.load_queue()))'; }

# ---------------------------------------------------------------- refill
if [ "$MODE" = refill ]; then
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

# ---------------------------------------------------------------- 其余 mode
case "$MODE" in
  daily|notify|once)
    log "mode $MODE 尚未实现（SPEC §12 阶段 9）"; exit 2 ;;
  *)
    echo "用法: $0 refill" >&2; exit 2 ;;
esac
