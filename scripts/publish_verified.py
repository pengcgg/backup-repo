#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把二级复验的结果落成正式产物。

流程：
  1) iptv/iptv.py 采集 + 首轮验真 → iptv/best_sorted.m3u
  2) scripts/check_streams.py 拿这份 m3u 当输入，再走一遍两阶段下钻，
     结果写到临时文件（VERIFIED_M3U / VERIFIED_TXT）
  3) 本脚本在「复验确实产出且条数没塌方」时，用它替换正式产物；
     否则保留采集结果，避免复验器抽风把列表砍空。

环境变量：
  VERIFIED_M3U     复验产出的 m3u（默认 /tmp/verified.m3u）
  TARGET_M3U       正式产物（默认 iptv/best_sorted.m3u）
  TARGET_M3U8      正式产物（默认 iptv/best_sorted.m3u8）
  MIN_KEEP_RATIO   复验后条数占原条数的比例低于此值则不替换（默认 0.5）
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

VERIFIED_M3U = os.environ.get("VERIFIED_M3U", "/tmp/verified.m3u")
TARGET_M3U = os.environ.get("TARGET_M3U", "iptv/best_sorted.m3u")
TARGET_M3U8 = os.environ.get("TARGET_M3U8", "iptv/best_sorted.m3u8")


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MIN_KEEP_RATIO = env_float("MIN_KEEP_RATIO", 0.5)

CST = timezone(timedelta(hours=8))
NAME_RE = re.compile(r'[,\s]"?([^",]*)$')


def log(msg):
    print(msg, flush=True)


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def parse_body(text):
    """返回 (extinf 行列表, url 行列表, 频道名集合)。"""
    extinf = []
    urls = []
    names = set()
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            extinf.append(line)
            q = line.rfind('"')
            if q != -1:
                name = line[q + 1:].lstrip(",").strip()
            else:
                name = line.split(",", 1)[1].strip() if "," in line else ""
            if not name:
                m = re.search(r'tvg-name="([^"]*)"', line)
                name = m.group(1) if m else ""
            if name:
                names.add(name)
            continue
        if line.startswith("#"):
            continue
        urls.append(line)
    return extinf, urls, names


def build_target(verified_text, source_count, channel_count):
    body = []
    for line in verified_text.splitlines():
        s = line.strip()
        if not s or s.upper() == "#EXTM3U":
            continue
        body.append(s)
    header = [
        "#EXTM3U",
        "# Generated-Time: %s" % datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST"),
        "# Channel-Count: %d" % channel_count,
        "# Source-Count: %d" % source_count,
        "# Verify: 二级复验通过（主地址 → 子列表 → 首个分片）",
    ]
    return "\n".join(header + body) + "\n"


def write(path, content):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    log("  已写入 %s（%d 字节）" % (path, len(content.encode("utf-8"))))


def main():
    log("=== 复验结果发布 ===")

    if not os.path.exists(VERIFIED_M3U):
        log("!! 复验没有产出文件（多半是保护触发：有效率过低或全部失效）")
        log("!! 保留本轮采集结果，不替换正式产物")
        return 0

    verified_text = read_text(VERIFIED_M3U)
    v_extinf, v_urls, v_names = parse_body(verified_text)
    new_count = len(v_urls)

    target_text = read_text(TARGET_M3U)
    _, old_urls, _ = parse_body(target_text)
    old_count = len(old_urls)

    log("复验前 %d 条 → 复验后 %d 条（频道 %d 个）" % (old_count, new_count, len(v_names)))

    if new_count == 0:
        log("!! 复验结果为空，保留本轮采集结果")
        return 0

    if old_count and new_count / float(old_count) < MIN_KEEP_RATIO:
        log("!! 复验后只剩 %.1f%%（低于保底阈值 %.0f%%），疑似复验器/网络抽风"
            % (new_count * 100.0 / old_count, MIN_KEEP_RATIO * 100))
        log("!! 保留本轮采集结果，不替换正式产物")
        return 0

    content = build_target(verified_text, new_count, len(v_names))
    write(TARGET_M3U, content)
    if TARGET_M3U8 and TARGET_M3U8 != TARGET_M3U:
        write(TARGET_M3U8, content)

    log("完成：正式产物已替换为复验通过的 %d 条 / %d 个频道（剔除 %d 条）"
        % (new_count, len(v_names), max(old_count - new_count, 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
