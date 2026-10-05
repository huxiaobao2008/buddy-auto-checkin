#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CDP 截图录制：自动按固定间隔截 WorkBuddy 当前页面图，保存到 logs/screenshots/。

用法：
    python tools/screenshot_record.py [间隔秒数] [最大张数]

示例（每 2 秒一张，最多 200 张）：
    python tools/screenshot_record.py 2 200

停止方式：Ctrl+C 或生成 logs/screenshots/.stop 文件。
"""

import datetime
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cdp as C

DEFAULT_INTERVAL = 2.0  # 默认 2 秒一张
DEFAULT_MAX = 500  # 默认最多 500 张


def main():
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INTERVAL
    max_shots = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MAX

    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs",
        "screenshots",
    )
    os.makedirs(out_dir, exist_ok=True)

    stop_file = os.path.join(out_dir, ".stop")
    if os.path.exists(stop_file):
        os.remove(stop_file)

    running = True

    def on_sig(*_):
        nonlocal running
        running = False
        print("\n[录制] 收到信号，停止中...")

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    print(f"[录制] 开始: 间隔 {interval}s, 上限 {max_shots} 张, 输出目录: {out_dir}")
    print("[录制] 操作 WorkBuddy 时请保持窗口可见。按 Ctrl+C 或创建 .stop 文件停止。")

    cdp = None
    try:
        cdp, err = C.connect()
        if not cdp:
            print(f"[录制] 无法连接 CDP: {err}")
            return 1

        for i in range(1, max_shots + 1):
            if not running or os.path.exists(stop_file):
                break
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:18]
            path = os.path.join(out_dir, f"shot_{ts}_{i:04d}.png")
            try:
                C.screenshot(cdp, path, full=True)
                print(f"[{i:04d}] {path}")
            except Exception as e:
                print(f"[{i:04d}] 截图失败: {e}")
                # 尝试重连
                try:
                    if cdp:
                        cdp.close()
                except Exception:
                    pass
                time.sleep(0.5)
                cdp, err = C.connect()
                if not cdp:
                    print(f"[录制] 重连失败: {err}")
                    break
            time.sleep(interval)

    finally:
        if cdp:
            try:
                cdp.close()
            except Exception:
                pass
        print(f"[录制] 结束。共截 {i} 张，保存在 {out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
