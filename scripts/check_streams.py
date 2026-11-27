#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播/流媒体源有效性检查器
--------------------------------
- 拉取远程 txt（名称,地址 / 分组,#genre#）与 m3u（#EXTINF）播放列表
- 逐条探测是否真的可播：HTTP 状态 + 内容类型 + m3u8 结构 + 子播放列表 + 首个分片
- 失效条目删除，有效条目保留，重新生成一份干净的 txt 和 m3u
- 自带保护：源站拉不到 / 有效率过低时不覆盖已有文件，避免把列表清空

环境变量（都有默认值）:
  SRC_TXT    源 txt 地址
  SRC_M3U    源 m3u 地址（留空则按 SRC_TXT 同名自动推导）
  OUT_TXT    输出 txt 文件名
  OUT_M3U    输出 m3u 文件名
  WORKERS    并发数（默认 16）
  TIMEOUT    单次请求超时秒（默认 10）
  RETRIES    失败重试次数（默认 2）
  DEEP       1=深检子列表/分片（默认 1），0=只看主列表
  MIN_RATIO  有效率保护阈值（默认 0.05，设为 0 关闭）
  MIN_ITEMS  条目数达到多少才启用保护（默认 20）
  REPORT     报告 json 落盘路径（可选）
"""

import os
import re
import sys
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
UA_VLC = "VLC/3.0.20 LibVLC/3.0.20"
UA_MPV = "mpv 0.37.0"

HLS_CT = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/mpegurl",
)

# ------------------------------------------------------------------ 配置


def env(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def env_int(name, default):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


SRC_TXT = env("SRC_TXT", "http://luobuqingcai.hostingem.ru/zb/juhe29.txt")
SRC_M3U = env("SRC_M3U", "")
OUT_TXT = env("OUT_TXT", "juhe29.txt")
OUT_M3U = env("OUT_M3U", "juhe29.m3u")
WORKERS = max(1, env_int("WORKERS", 16))
TIMEOUT = max(3, env_int("TIMEOUT", 10))
RETRIES = max(0, env_int("RETRIES", 2))
DEEP = env("DEEP", "1") not in ("0", "false", "False", "no")
MIN_RATIO = env_float("MIN_RATIO", 0.05)
MIN_ITEMS = env_int("MIN_ITEMS", 20)
REPORT = env("REPORT", "")

SEC_PER_URL_CAP = 60  # 单条 URL 总耗时上限（秒），防止个别源把任务拖死

# ------------------------------------------------------------------ 工具


def log(msg):
    print(msg, flush=True)


def decode_bytes(b):
    if not b:
        return ""
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace")
    for enc in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


_thread_local = threading.local()


def get_session():
    s = getattr(_thread_local, "s", None)
    if s is None:
        s = requests.Session()
        s.verify = False
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _thread_local.s = s
    return s


def fetch_bytes(url, timeout=TIMEOUT, retries=RETRIES, referer=""):
    """拉取一个文本类资源，返回 (ok, bytes 或 错误说明)。"""
    last = "unknown"
    headers = {"User-Agent": UA_BROWSER, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    for attempt in range(retries + 1):
        try:
            r = get_session().get(
                url, headers=headers, timeout=(6, timeout), allow_redirects=True
            )
            if r.status_code >= 400:
                last = "HTTP %d" % r.status_code
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                continue
            return True, r.content
        except requests.exceptions.Timeout:
            last = "超时"
        except requests.exceptions.SSLError:
            last = "SSL 错误"
        except requests.exceptions.ConnectionError:
            last = "连接失败"
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    return False, last


# ------------------------------------------------------------------ 解析


class Item(object):
    __slots__ = ("name", "url", "group", "attrs")

    def __init__(self, name, url, group="", attrs=None):
        self.name = (name or "").strip()
        self.url = (url or "").strip()
        self.group = (group or "").strip()
        self.attrs = attrs or {}

    def key(self):
        return self.url.strip()


def looks_like_url(s):
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s or ""))


def parse_txt(text):
    """解析常见聚合直播 txt：
        分组名,#genre#
        频道名,http://xxx.m3u8
    兼容：只有 URL 的行、空格/竖线/制表符分隔、注释行。
    """
    items = []
    group = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        low = line.lower()
        if "#genre#" in low:
            group = line[: low.index("#genre#")].strip().strip(",").strip()
            continue
        if line.startswith("#") and not looks_like_url(line):
            continue

        name, url = "", ""
        if "," in line:
            head, tail = line.split(",", 1)
            if looks_like_url(tail.strip()):
                name, url = head.strip(), tail.strip()
            elif looks_like_url(head.strip()):
                name, url = "", head.strip()
            else:
                parts = [p.strip() for p in line.split(",") if p.strip()]
                for p in parts:
                    if looks_like_url(p):
                        url, name = p, ""
                        break
        else:
            for sep in ("\t", "|", "$", " "):
                if sep in line:
                    head, tail = line.split(sep, 1)
                    if looks_like_url(tail.strip()):
                        name, url = head.strip(), tail.strip()
                        break
                    if looks_like_url(head.strip()):
                        name, url = "", head.strip()
                        break
        if not url and looks_like_url(line):
            url = line
        if not url:
            continue
        if not name:
            name = os.path.basename(urlparse(url).path) or url
        items.append(Item(name, url, group))
    return items


EXTINF_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def parse_m3u(text):
    """解析标准 m3u/m3u8 播放列表。"""
    items = []
    if "#EXTM3U" not in text and "#EXTINF" not in text:
        return items
    pending = None
    group = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            attrs = dict(EXTINF_ATTR_RE.findall(line))
            q = line.rfind('"')
            if q != -1:
                title = line[q + 1:].lstrip(",").strip()
            else:
                title = line.split(",", 1)[1].strip() if "," in line else ""
            g = attrs.get("group-title", "").strip()
            if g:
                group = g
            pending = {"title": title, "attrs": attrs, "group": g or group}
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            name = pending["title"] or pending["attrs"].get("tvg-name", "")
            if not name:
                name = os.path.basename(urlparse(line).path) or line
            items.append(
                Item(name, line, pending["group"] or group, pending["attrs"])
            )
            pending = None
        elif looks_like_url(line):
            items.append(Item(os.path.basename(urlparse(line).path) or line, line, group))
    return items


def merge_items(*groups):
    """按 URL 去重合并，后者缺失的名称/分组用前者补齐。"""
    seen = {}
    order = []
    for items in groups:
        for it in items:
            k = it.key()
            if not k:
                continue
            if k in seen:
                cur = seen[k]
                if not cur.name and it.name:
                    cur.name = it.name
                if not cur.group and it.group:
                    cur.group = it.group
                for ak, av in it.attrs.items():
                    cur.attrs.setdefault(ak, av)
            else:
                seen[k] = it
                order.append(k)
    return [seen[k] for k in order]


# ------------------------------------------------------------------ 探测


def looks_hls(url, content_type, head_text):
    if "#EXTM3U" in head_text:
        return True
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8") or path.endswith(".m3u"):
        return True
    ct = (content_type or "").lower()
    return any(k in ct for k in HLS_CT)


def first_uri(text):
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def probe_playlist(base_url, text, timeout):
    """对播放列表做一级下钻：master -> 子列表 -> 首个分片；media -> 首个分片。"""
    uri = first_uri(text)
    if not uri:
        # 纯 master 列表里第一个非注释行一定存在；为空说明列表没内容
        return False, "播放列表为空"
    target = urljoin(base_url, uri)

    ok, data = fetch_bytes(target, timeout=timeout, retries=1, referer=base_url)
    if not ok:
        return False, "子列表/分片不可用(%s)" % data
    body = decode_bytes(data[:32768])
    if not body.strip():
        return False, "子列表/分片为空"

    if "#EXTM3U" in body:
        # 还有一层（master 里套 media，或 media 里还有子列表）
        sub_uri = first_uri(body)
        if not sub_uri:
            return False, "子列表为空"
        seg_url = urljoin(target, sub_uri)
        ok2, seg = fetch_bytes(seg_url, timeout=timeout, retries=1, referer=target)
        if not ok2:
            return False, "分片不可用(%s)" % seg
        if not seg:
            return False, "分片为空"
        return True, "ok"
    return True, "ok"


def check_url(url, timeout=TIMEOUT, retries=RETRIES, deep=DEEP):
    """返回 (是否有效, 原因)。"""
    started = time.time()
    last = "unknown"
    for attempt in range(retries + 1):
        if time.time() - started > SEC_PER_URL_CAP:
            return False, "超时(%s)" % last
        ua = UA_BROWSER if attempt % 3 == 0 else (UA_VLC if attempt % 3 == 1 else UA_MPV)
        headers = {"User-Agent": ua, "Accept": "*/*"}
        try:
            r = get_session().get(
                url, headers=headers, timeout=(6, timeout), stream=True, allow_redirects=True
            )
            code = r.status_code
            if code >= 400:
                last = "HTTP %d" % code
                r.close()
                if code in (401, 403) and attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if code >= 500 and attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return False, last

            ctype = (r.headers.get("content-type") or "").lower()
            head = b""
            try:
                head = r.raw.read(32 * 1024, decode_content=True) or b""
            except Exception:  # noqa: BLE001  有的源读一半就断
                head = b""
            finally:
                r.close()

            if not head:
                last = "空响应"
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return False, last

            text = decode_bytes(head)
            if looks_hls(url, ctype, text):
                if "#EXTM3U" not in text:
                    last = "m3u8 结构异常"
                    if attempt < retries:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    return False, last
                if deep:
                    ok, why = probe_playlist(url, text, timeout)
                    if not ok:
                        last = why
                        if attempt < retries:
                            time.sleep(1.0 * (attempt + 1))
                            continue
                        return False, last
                return True, "ok"

            # 非 m3u8：flv / mp4 / ts 等，能读到数据才算活；返回网页/报错页的判死
            low = text[:512].lstrip().lower()
            if "text/html" in ctype or low.startswith("<!doctype") or low.startswith("<html"):
                last = "返回网页而非流"
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return False, last
            return True, "ok"
        except requests.exceptions.Timeout:
            last = "超时"
        except requests.exceptions.SSLError:
            last = "SSL 错误"
        except requests.exceptions.ConnectionError:
            last = "连接失败"
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    return False, last


# ------------------------------------------------------------------ 输出


def build_txt(items):
    out = []
    current = None
    for it in items:
        g = it.group or ""
        if g != current:
            if g:
                out.append("")
                out.append("%s,#genre#" % g)
            current = g
        out.append("%s,%s" % (it.name or "未命名", it.url))
    text = "\n".join(out).strip() + "\n"
    return text


def build_m3u(items):
    out = ["#EXTM3U"]
    for it in items:
        attrs = []
        name = it.name or "未命名"
        attrs.append('tvg-name="%s"' % name.replace('"', "'"))
        if it.group:
            attrs.append('group-title="%s"' % it.group.replace('"', "'"))
        logo = it.attrs.get("tvg-logo") if it.attrs else ""
        if logo:
            attrs.append('tvg-logo="%s"' % logo.replace('"', "'"))
        out.append("#EXTINF:-1 %s,%s" % (" ".join(attrs), name))
        out.append(it.url)
    return "\n".join(out) + "\n"


def write_if_changed(path, content):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == content:
                log("  %s 无变化" % path)
                return False
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    log("  已写入 %s（%d 字节）" % (path, len(content.encode("utf-8"))))
    return True


# ------------------------------------------------------------------ 主流程


def derive_m3u_url(txt_url):
    base, _ = os.path.splitext(txt_url)
    return base + ".m3u"


def main():
    log("=== 直播源检查开始 ===")
    log("txt 源: %s" % SRC_TXT)

    ok_txt, data_txt = fetch_bytes(SRC_TXT)
    if not ok_txt:
        log("!! 无法获取 txt 源: %s" % data_txt)
        log("!! 保留现有文件不改动，退出。")
        return 0

    txt_items = parse_txt(decode_bytes(data_txt))
    log("txt 解析到 %d 条" % len(txt_items))

    m3u_items = []
    m3u_url = SRC_M3U.strip() or derive_m3u_url(SRC_TXT)
    log("m3u 源: %s" % m3u_url)
    ok_m3u, data_m3u = fetch_bytes(m3u_url)
    if ok_m3u:
        m3u_items = parse_m3u(decode_bytes(data_m3u))
        log("m3u 解析到 %d 条" % len(m3u_items))
    else:
        log("!! m3u 源不可用（%s），仅按 txt 生成" % data_m3u)

    # txt 为主源（名称/分组以它为准），m3u 只用来补 logo 等元信息
    items = merge_items(txt_items, m3u_items)
    total = len(items)
    log("合并去重后 %d 条，开始并发探测（并发 %d，超时 %ds，深检=%s）"
        % (total, WORKERS, TIMEOUT, "开" if DEEP else "关"))

    if total == 0:
        log("!! 没有任何条目，保留现有文件不改动。")
        return 0

    results = {}
    done = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(check_url, it.url, TIMEOUT, RETRIES, DEEP): it for it in items
        }
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                ok, reason = fut.result()
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, type(exc).__name__
            results[it.url] = (ok, reason)
            done += 1
            if done % 25 == 0 or done == total:
                log("  进度 %d/%d（有效 %d）" % (done, total,
                                               sum(1 for v in results.values() if v[0])))

    alive = [it for it in items if results.get(it.url, (False, ""))[0]]
    dead = [it for it in items if not results.get(it.url, (False, ""))[0]]
    elapsed = time.time() - started
    log("=== 探测完成：有效 %d / 共 %d，耗时 %.1fs ===" % (len(alive), total, elapsed))

    ratio = len(alive) / float(total) if total else 0.0
    protected = total >= MIN_ITEMS and MIN_RATIO > 0 and ratio < MIN_RATIO
    if len(alive) == 0 or protected:
        log("!! 有效率 %.1f%% 低于保护阈值 %.1f%%（或全部失效），判定为源站/网络异常。"
            % (ratio * 100, MIN_RATIO * 100))
        log("!! 本次不覆盖已有文件，退出码 2。确认是源本身大量失效的话，"
            "把 MIN_RATIO 环境变量设为 0 再跑一次。")
        write_report(total, alive, dead, results, ratio, protected=True)
        return 2

    log("删除失效 %d 条，保留有效 %d 条" % (len(dead), len(alive)))
    for it, (_, reason) in sorted(
        ((it, results[it.url]) for it in dead), key=lambda x: x[0].name
    )[:30]:
        log("  - 删除: %s | %s | %s" % (it.name, it.url, reason))
    if len(dead) > 30:
        log("  ... 其余 %d 条省略" % (len(dead) - 30))

    changed_txt = write_if_changed(OUT_TXT, build_txt(alive))
    changed_m3u = write_if_changed(OUT_M3U, build_m3u(alive))
    write_report(total, alive, dead, results, ratio, protected=False)

    log("完成：%s%s" % (
        "已更新" if (changed_txt or changed_m3u) else "内容无变化",
        "" if (changed_txt or changed_m3u) else "（全量条目与上次一致）",
    ))
    return 0


def write_report(total, alive, dead, results, ratio, protected):
    summary = "有效 %d / 共 %d（%.1f%%）" % (len(alive), total, ratio * 100)
    if protected:
        summary += " —— 低于保护阈值，本次未覆盖文件"
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write("### 直播源检查结果\n\n")
                fh.write("- %s\n" % summary)
                fh.write("- 删除失效：%d 条\n\n" % len(dead))
                if dead:
                    fh.write("| 频道 | 地址 | 原因 |\n|---|---|---|\n")
                    for it in dead[:40]:
                        fh.write("| %s | `%s` | %s |\n" %
                                 (it.name, it.url, results[it.url][1]))
                    if len(dead) > 40:
                        fh.write("\n（其余 %d 条省略）\n" % (len(dead) - 40))
        except Exception:  # noqa: BLE001
            pass
    if REPORT:
        try:
            payload = {
                "total": total,
                "alive": len(alive),
                "dead": len(dead),
                "ratio": round(ratio, 4),
                "protected": protected,
                "items": [
                    {
                        "name": it.name,
                        "url": it.url,
                        "group": it.group,
                        "ok": results[it.url][0],
                        "reason": results[it.url][1],
                    }
                    for it in alive + dead
                ],
            }
            with open(REPORT, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            log("报告已写入 %s" % REPORT)
        except Exception as exc:  # noqa: BLE001
            log("报告写入失败: %s" % exc)


if __name__ == "__main__":
    sys.exit(main())
