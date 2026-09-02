#!/usr/bin/env bash
# 发布的 **git** 那一半 —— 0 token。数据那一半在 src/publish.py（那边有用例）。
#
#   src/publish.py            追加 posts.jsonl + 归档 data/content/<date>.json（验字段契约）
#   render.py --archive-only  归档索引依赖 jsonl，刚追加完必须重建
#   git                       白名单 add + 跳空 commit + 分类重试 push
#
# **绝不出现 --force。** 远端是这个项目唯一的备份：docs/img 里的图重新生成要花钱，
# posts.jsonl 是半年去重的唯一依据，两者都没有第二份。
#
# 为什么拆成两个文件：wiki-bot 把数据逻辑写成 shell 里的 python heredoc，那段代码
# 永远跑不了用例，而它干的正是本项目最脆的事（往 posts.jsonl 写字段，少一个键的
# 失败方式是静默降级）。git 这半留在 shell 是因为分类重试、分支保护本来就是 shell 的活。
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
[ -f .env ] && . ./envload.sh    # 与 run.sh / notify.sh 同一套语义：不覆盖已有环境变量

BR="${GIT_BRANCH:-main}"

# ---------- 1. 数据（先 jsonl 再归档件，顺序在 publish.py 里定死） ----------
python3 src/publish.py || exit 1

# 归档索引依赖 posts.jsonl，刚追加完要重建
python3 src/render.py --archive-only || exit 1

# ---------- 2. git ----------
command -v git >/dev/null || { echo "publish: 无 git"; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "publish: 不是 git 仓库"; exit 1; }

br=$(git rev-parse --abbrev-ref HEAD)
[ "$br" = "$BR" ] || { echo "publish: HEAD 不在 $BR（当前 $br），拒绝发布"; exit 3; }

# 只 add 显式路径。**绝不 git add -A** —— .gitignore 写错、目录里有临时文件、
# 甚至 .env 都会被顺手带进去，而这个仓库是公开的。
# data/queue.tsv 也带上：refill 自己会提交，但它 push 失败时那一批就压在本地，
# 由下一次 daily 顺路带走。
git add -- docs data/posts.jsonl data/content data/queue.tsv 2>/dev/null

NEED_PUSH=0
if git diff --cached --quiet; then
  echo "publish: 无变更，跳过 commit"
else
  DATE=$(python3 -c "import json;print(json.load(open('content.json'))['date'])" 2>/dev/null)
  SUBJ=$(python3 -c "import json;print(json.load(open('content.json'))['subject'])" 2>/dev/null)
  TITLE=$(python3 -c "import json;print(json.load(open('content.json'))['title'])" 2>/dev/null)
  git commit -q -m "animal: ${DATE:-?} ${SUBJ:-?} ${TITLE:-}" \
    || { echo "publish: commit 失败"; exit 1; }
  NEED_PUSH=1
fi

# 上次可能 commit 成功但 push 失败 —— 本地有未推的 commit 也要推。
# 没有这一条，一次网络抖动会让那一期永远停在本地，而 stage 已经是 pushed。
if [ "$NEED_PUSH" = 0 ]; then
  if ! git diff --quiet '@{u}' HEAD 2>/dev/null; then NEED_PUSH=1; fi
fi
[ "$NEED_PUSH" = 0 ] && { echo "publish: 无需 push"; exit 0; }

# ---------- 3. push，按 stderr 分类重试 ----------
# 不分类的话，一个过期 token 会被重试 3 次、白等 70 秒才告警。
# 免密靠 .env 里的 GIT_ASKPASS（PAT 在 /root/.animal_pat）—— cron 里没有交互终端，
# 缺了它 push 会卡在 "could not read Username" 上，而那正好落进下面的 auth 分类。
n=0
while :; do
  n=$((n + 1))
  out=$(git push origin "$BR" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then echo "publish: push ok (第 $n 次)"; exit 0; fi
  case "$out" in
    *non-fast-forward*|*"fetch first"*|*rejected*) kind=conflict ;;
    *"Authentication failed"*|*"Permission denied"*|*"could not read Username"*|*403*) kind=auth ;;
    *"Could not resolve host"*|*"Connection timed out"*|*"Failed to connect"*|*TLS*|*"Operation timed out"*) kind=net ;;
    *) kind=other ;;
  esac
  echo "publish: push 失败[$kind] 第 $n 次：$(printf '%s' "$out" | tail -2)"
  case "$kind" in
    net)      [ $n -ge 3 ] && exit 1; sleep $((n * n * 5)) ;;
    conflict) [ $n -ge 2 ] && exit 1
              git pull --rebase --autostash origin "$BR" || exit 1 ;;
    *)        exit 1 ;;      # auth / other 不重试，立刻让上层告警
  esac
done
