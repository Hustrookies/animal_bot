#!/usr/bin/env bash
# 读 .env —— 但**不覆盖已经在环境里的变量**。
#
# 这一份被 run.sh / notify.sh / publish.sh 共同 source，因为三者对 .env 的语义必须一致：
# 只要有一个用了 `set -a; . ./.env; set +a`（wiki 那种写法），那个脚本里命令行传的值就会
# 被 .env 里的值盖回去，而它看起来完全正常。
#
# 阶段 9 已经被这件事咬过一次：.env 注释写着"联调请一律用 IMG_ON=0"，而
# `IMG_ON=0 ./run.sh daily` 会被 .env 里的 IMG_ON=1 盖掉 —— 也就是说那句注释本身是假的，
# 第一次联调就会真计费，而日志全绿。
# 到了 notify.sh 这边后果更难收拾：`WEIXIN_TARGET=测试群 ./notify.sh --kind ok` 会静默
# 发到真实接收人，而消息发错人是撤不回来的（钱可以再花，消息不能收回）。
#
# export 语义必须保留：不 export 的话 IMG_API_KEY 只是个 shell 变量，python 的
# os.environ 看不见，失败方式是 gen-image 报 no_key 而 .env 明明配好了。
#
# 被 source，所以**不能有 exit**（那会连带杀掉调用方）。
if [ -f .env ]; then
  _line=""    # set -u 下必须先初始化：.env 是空文件时 read 直接失败，下面那个 -n 会报未绑定
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|'#'*) continue ;; esac
    _k=${_line%%=*}; _v=${_line#*=}
    case "$_k" in *[!A-Za-z0-9_]*|'') continue ;; esac   # 不是合法变量名的行直接跳过
    [ -n "${!_k+x}" ] && continue                        # 已经在环境里 → 保留调用方的值
    export "$_k=$_v"
  done < .env
  unset _line _k _v
fi
