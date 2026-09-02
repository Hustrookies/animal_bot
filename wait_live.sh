#!/usr/bin/env bash
# 等 GitHub Pages 生效 —— 0 token，不花钱，只发 GET。
# 用法: ./wait_live.sh <url> <marker> [预算秒数=300]
# 退出: 0 LIVE / 0 LIVE_DIRTY_CACHE / 1 STALE / 1 NOT_FOUND / 2 NET_DOWN
#
# marker 传的是 state/<date>.buildid 的内容 —— 判的不是"页面存在"，而是"**这一期**的
# 页面存在"。只查 200 会让昨天的页面把今天的构建失败盖过去。
#
# 坑一，CDN 缓存：不加 cache-buster 会读到缓存的 404，轮询到超时判失败，而站点其实早就
# 好了 —— 表现为「有时莫名发降级消息」，随机且极难复现。反过来只用 ?cb= 探到就判 LIVE
# 也不行：用户点的是干净 URL，可能命中旧缓存。所以两步都要。
#
# 坑二，**别用 `curl -f`**（wiki-bot 那版用了，这里刻意不用）：-f 会让 HTTP 404 也返回
# 失败（exit 22），于是"页面还没上线"被记成"连不上网"，累加 6 次后判 NET_DOWN。而
# NET_DOWN 那条路径是既不发内容也不发告警的（本机断网时消息本来也发不出去，只 log）。
# 两者混在一起的后果：Pages 目录配错的那些天一条消息、一条告警都没有，**完全静默**。
# 这不是假设 —— 阶段 10 第一次真跑就撞上了：Pages 的发布源是仓库根而非 /docs，页面真实
# 地址多一段 /docs，探测得到 404，日志却报「本机出网异常」，把一个配置问题伪装成断网。
# 所以这里改成看 http_code：000 或 curl 自身失败才算网络问题，404/5xx 只是"还没好"。
set -uo pipefail
URL="${1:?用法: wait_live.sh <url> <marker> [秒]}"
MARK="${2:?缺少 marker（应传 state/<date>.buildid 的内容）}"
BUDGET="${3:-300}"
END=$(( $(date +%s) + BUDGET ))
SEEN_200=0; NETFAIL=0; LASTCODE=""
TMP=$(mktemp) || exit 2
trap 'rm -f "$TMP"' EXIT

sleep 20                       # Pages 最快也要 ~30s，t=0 探测必然白费

while [ "$(date +%s)" -lt "$END" ]; do
  sep='?'; case "$URL" in *\?*) sep='&';; esac
  code=$(curl -sS -o "$TMP" -w '%{http_code}' --max-time 10 \
              -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' \
              "${URL}${sep}cb=$(date +%s)" 2>/dev/null); crc=$?
  LASTCODE="$code"

  if [ "$crc" -ne 0 ] || [ "$code" = 000 ]; then
    # 真的连不上：DNS / TCP / TLS / 超时
    NETFAIL=$((NETFAIL+1))
    # 连续 1 分钟连不上 = 本机出网有问题，此时微信大概也发不出去，别把它当 Pages 的错
    [ $NETFAIL -ge 6 ] && {
      echo NET_DOWN
      printf 'wait_live: 连续 %s 次连不上（curl rc=%s, http_code=%s）—— 本机出网异常\n' \
             "$NETFAIL" "$crc" "$code" >&2
      exit 2; }
  elif [ "$code" = 200 ]; then
    SEEN_200=1; NETFAIL=0
    # -F 固定串（buildid 里有 `-`，别让它进正则）；`--` 防 marker 被当成选项
    if grep -qF -- "$MARK" "$TMP"; then
      # 复核用户真正会点的那个 URL（干净、无 cache-buster）。
      # **落文件再 grep，不能用 `curl | grep -q`。** grep -q 找到就立刻退出并关掉管道，
      # curl 写入失败返回 23，而 set -o pipefail 把它变成整个管道失败 —— 于是复核
      # 永远判不通过，这一步稳定输出 LIVE_DIRTY_CACHE，还白等 45 秒。
      # 它没有可见症状：LIVE 和 LIVE_DIRTY_CACHE 在 run.sh 里走同一个分支、都发正式
      # 消息，所以"干净 URL 命中旧缓存"这个真问题从来没有被真正检测过。
      # 实测：管道版 rc=23，落文件版 rc=0，同一个 URL 同一个 marker。
      for _ in 1 2 3; do
        if curl -sS --max-time 10 -o "$TMP" "$URL" 2>/dev/null \
           && grep -qF -- "$MARK" "$TMP"; then
          echo LIVE; exit 0
        fi
        sleep 15
      done
      echo LIVE_DIRTY_CACHE; exit 0
    fi
  else
    # 404 / 5xx：能连上，只是这一页还没好。这是等待期间的常态，**不算网络故障**。
    NETFAIL=0
  fi
  sleep 10
done

# 超预算。两种收尾，区别在于"到底有没有见过 200"。
# stdout 只放那一个词（run.sh 用 $(...) 捕获它做分支），诊断信息走 stderr —— 末次
# HTTP 码是这里最有用的一条线索：如果第一次跑就打印「末次 HTTP 404」，就不会有人
# 把发布目录配错误读成本机断网。
if [ $SEEN_200 = 1 ]; then
  echo STALE
  printf 'wait_live: 页面在线但不是这一期（末次 HTTP %s，marker=%s）—— 构建卡住或失败\n' \
         "${LASTCODE:-?}" "$MARK" >&2
  exit 1
fi
echo NOT_FOUND
printf 'wait_live: 从未见到 200（末次 HTTP %s）—— Pages 未启用 / 发布目录配错 / 首次部署\n' \
       "${LASTCODE:-?}" >&2
exit 1
