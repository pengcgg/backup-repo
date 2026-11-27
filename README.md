# 直播源有效性检查（txt / m3u 自动清理）

定时（每 4 小时）+ 手动触发，抓取远程播放列表，逐条探测是否真的能播，
**失效的删掉、有效的保留**，重新生成一份干净的 `juhe29.txt` 和 `juhe29.m3u` 并自动提交回仓库。

源地址默认：

- txt：`http://luobuqingcai.hostingem.ru/zb/juhe29.txt`
- m3u：`http://luobuqingcai.hostingem.ru/zb/juhe29.m3u`（可在 workflow 里改，留空则按 txt 同名自动推导）

---

## 一、怎么用（三步）

### 1. 建仓库并上传

新建一个 GitHub 仓库（public/private 都行），把本目录里的内容原样放到仓库根目录：

```
.github/workflows/check-streams.yml
scripts/check_streams.py
requirements.txt
README.md
```

也可以用命令行：

```bash
git init
git add .
git commit -m "init: 直播源检查"
git branch -M main
git remote add origin https://github.com/<用户名>/<仓库>.git
git push -u origin main
```

### 2. 打开写权限（必须做，否则提交一定失败）

仓库 **Settings → Actions → General → Workflow permissions**
→ 选 **Read and write permissions** → Save

### 3. 手动跑一次

仓库 **Actions → 直播源有效性检查 → Run workflow**（可填参数），
跑完看日志里的 `有效 X / 共 Y`，以及仓库根目录新出现的 `juhe29.txt`、`juhe29.m3u`。

换源不用改文件：Run workflow 时在 `src_txt` / `src_m3u` 里填地址就行，留空用默认；
`src_m3u` 留空时会自动按 txt 同名推导，先试 `.m3u` 再试 `.m3u8`。

之后按 cron 自动跑，不用管。

### 跑一次大概多久（v2 提速）

老版本每条源要串着做「主列表 → 子列表 → 分片」三次请求，死链还要重试三轮，
一条坏源能拖 30 秒以上，几百条就得跑十几分钟。

现在改成两阶段：

1. **快筛**：32 并发，每条只发一个带 `Range` 的请求，读 4KB 就知道是死是活；
   403/404 直接判死不再重试，建连超时压到 4 秒
2. **深检**：只对通过快筛的 HLS 再做下钻，24 并发，并且**复用快筛已经读到的列表内容**，
   每条少一次往返

同时加了单条 20 秒硬上限，避免个别源把整个任务拖死。
600 条左右的量，正常源站下一般 1~3 分钟能跑完（具体看源站响应速度）。

还想更快就调 `WORKERS`（比如 48~64），代价是对源站压力大、容易被限流；
`DEEP=0` 能再快一半，但「列表能下、点开黑屏」的死链会留下来，不建议长期开。

顺带修了一个老问题：`rtmp://` / `rtsp://` 这类源没法用 HTTP 验证，
以前会被当成连接失败删掉，现在默认保留（`KEEP_NON_HTTP=1`）。

---

## 二、判定标准（什么样的算“有效”）

| 检查项 | 说明 |
|---|---|
| HTTP 状态 | 4xx/5xx 判失效；403/416 会去掉 Range 再试一次，408/429/5xx 换 UA 重试 |
| 空响应 | 能连上但读不到任何数据 → 失效 |
| HLS 结构 | URL 是 m3u8 系列时，内容必须含 `#EXTM3U`，否则判失效 |
| 深检（默认开） | master 列表 → 子播放列表 → 首个分片，逐层拉一次；分片 404/超时就判失效 |
| 错误页 | 返回 `text/html` / `<!DOCTYPE html>` 的（伪 200）判失效 |
| 非 m3u8 流 | flv / mp4 / ts 等，能读到数据即算活 |

深检是关键：只看主列表 200 会留下一堆「列表能下、点开黑屏」的死链。

---

## 三、保护机制（防止一次网络抖动把列表清空）

- **源站拉不到** → 不改动任何文件，正常退出（保留上次结果）
- **有效率低于 `MIN_RATIO`（默认 5%）** → 判定为源站异常，**不覆盖文件**，退出码 2
- **一条都不剩** → 同样不覆盖
- **内容没变化** → 不产生 commit（不会刷一堆空提交）

真心想把 90% 都失效的源清掉，手动 Run 时把 `min_ratio` 填 `0` 即可。

---

## 四、可调参数（workflow 里的 `env:`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SRC_TXT` | 上面那个 txt | 源 txt 地址 |
| `SRC_M3U` | 同名 .m3u | 源 m3u 地址，留空自动推导 |
| `OUT_TXT` | `juhe29.txt` | 输出 txt 文件名 |
| `OUT_M3U` | `juhe29.m3u` | 输出 m3u 文件名 |
| `WORKERS` | 32 | 第一阶段并发数；源站扛不住就调小 |
| `DEEP_WORKERS` | 24 | 第二阶段（HLS 下钻）并发数 |
| `TIMEOUT` | 8 | 单次请求读超时（秒） |
| `CONNECT_TIMEOUT` | 4 | 建连超时（秒），坏域名快速放弃 |
| `RETRIES` | 1 | 失败重试次数；403/404 不重试 |
| `SEC_PER_URL_CAP` | 20 | 单条 URL 总耗时上限（秒） |
| `FAST_BYTES` | 4096 | 快筛阶段只读的字节数（Range 请求） |
| `KEEP_NON_HTTP` | 1 | rtmp/rtsp 等非 http 源无法用 HTTP 验证，1=保留 |
| `DEEP` | 1 | 1=深检子列表/分片，0=只查主列表（快但漏判多） |
| `MIN_RATIO` | 0.05 | 有效率保护阈值，0=关闭 |
| `MIN_ITEMS` | 20 | 条目数达到多少才启用保护 |
| `REPORT` | `check-report.json` | 每条源的探测结果明细（含失败原因） |

改定时：`.github/workflows/check-streams.yml` 里的 `cron: "0 */4 * * *"`（UTC）。
北京时间 2/6/10/14/18/22 点跑，想一天两次就改成 `0 */12 * * *`。

本地跑（可选）：

```bash
pip install -r requirements.txt
SRC_TXT="http://luobuqingcai.hostingem.ru/zb/juhe29.txt" python scripts/check_streams.py
```

---

## 五、几个要注意的地方

- 定时任务只认**默认分支**，且仓库 **60 天没有任何提交**会被 GitHub 自动暂停 schedule，手动 Run 一次即恢复。
- 私有仓库 Actions 每月 2000 分钟免费额度；这个任务一次通常 1~3 分钟，一天 6 次一个月约 200~400 分钟，够用。公开仓库不限。
- 源站本身要是挂了/换域名，日志里会写「无法获取 txt 源」，此时仓库里的上一版文件原样保留。
- 输出统一 UTF-8；如果把它挂成 raw 订阅链接，部分播放器对中文文件名有兼容问题，可以再加一个英文别名文件（`OUT_M3U` 改个英文名再跑一次即可）。
