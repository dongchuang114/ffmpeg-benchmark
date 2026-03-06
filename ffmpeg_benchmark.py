#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg Performance Benchmark Tool
==================================
测试服务器在不同内存 channel 配置下的 FFmpeg 编码性能。

每次修改内存 channel 配置后，用 --label 参数重新运行本脚本，
所有结果会汇总到同一份 HTML 报告，通过浏览器远端查看。

用法示例:
  # 安装依赖（可选，用于内存带宽测试）
  sudo apt install mbw

  # 首次运行（4-channel 配置）
  python3 ffmpeg_benchmark.py --label "4-channel"

  # 修改 BIOS/内存 channel 后再次运行（2-channel 配置）
  python3 ffmpeg_benchmark.py --label "2-channel"

  # 仅重新生成报告并启动服务
  python3 ffmpeg_benchmark.py --report-only --port 8080
"""

import argparse
import datetime
import http.server
import json
import os
import platform
import re
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
# 配置常量
# ══════════════════════════════════════════════════════════════════════════════

# 默认输出目录：脚本所在目录下的 benchmark_results 子目录
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "benchmark_results"
DEFAULT_PORT = 8080
DEFAULT_DURATION = 30   # 每项测试的秒数

# (name, codec, resolution, preset, extra_ffmpeg_flags)
TEST_CASES = [
    ("H.264  1080p  fast",   "libx264", "1920x1080", "fast",         []),
    ("H.264  1080p  medium", "libx264", "1920x1080", "medium",       []),
    ("H.264  4K     fast",   "libx264", "3840x2160", "fast",         []),
    ("H.264  4K     medium", "libx264", "3840x2160", "medium",       []),
    ("H.265  1080p  fast",   "libx265", "1920x1080", "fast",
     ["-x265-params", "log-level=error"]),
    ("H.265  1080p  medium", "libx265", "1920x1080", "medium",
     ["-x265-params", "log-level=error"]),
    ("H.265  4K     fast",   "libx265", "3840x2160", "fast",
     ["-x265-params", "log-level=error"]),
    ("VP9    1080p  speed4", "libvpx-vp9", "1920x1080", None,
     ["-cpu-used", "4", "-b:v", "0", "-crf", "33"]),
]

# Chart.js 调色板（每个 config 一种颜色）
PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
    "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
]

# ══════════════════════════════════════════════════════════════════════════════
# 系统信息收集
# ══════════════════════════════════════════════════════════════════════════════

def run_cmd(cmd, default="N/A", timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip() or default
    except Exception:
        return default


def get_os_info():
    return {
        "hostname":  platform.node(),
        "platform":  platform.platform(),
        "kernel":    run_cmd("uname -r"),
        "distro":    run_cmd(
            "cat /etc/os-release 2>/dev/null | grep PRETTY_NAME "
            "| cut -d= -f2 | tr -d '\"'"
        ),
        "uptime":    run_cmd("uptime"),
    }


def get_cpu_info():
    raw = run_cmd("lscpu")
    fields = {}
    for line in raw.splitlines():
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()

    return {
        "model":          fields.get("Model name", "N/A"),
        "logical_cpus":   fields.get("CPU(s)", "N/A"),
        "sockets":        fields.get("Socket(s)", "N/A"),
        "cores_socket":   fields.get("Core(s) per socket", "N/A"),
        "threads_core":   fields.get("Thread(s) per core", "N/A"),
        "max_mhz":        fields.get("CPU max MHz", fields.get("CPU MHz", "N/A")),
        "l1d":            fields.get("L1d cache", "N/A"),
        "l2":             fields.get("L2 cache", "N/A"),
        "l3":             fields.get("L3 cache", "N/A"),
        "numa_nodes":     fields.get("NUMA node(s)", "N/A"),
        "architecture":   fields.get("Architecture", "N/A"),
        "lscpu_full":     raw,
    }


def get_memory_info():
    info = {}

    # /proc/meminfo
    meminfo = run_cmd("cat /proc/meminfo")
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            info["total_gb"] = round(int(line.split()[1]) / 1048576, 2)
        elif line.startswith("MemFree:"):
            info["free_gb"] = round(int(line.split()[1]) / 1048576, 2)
        elif line.startswith("MemAvailable:"):
            info["available_gb"] = round(int(line.split()[1]) / 1048576, 2)

    # dmidecode — 需要 root 权限；失败时给出提示
    dmi = run_cmd("dmidecode -t 17 2>/dev/null")
    info["dmidecode_raw"] = dmi[:8000] if dmi != "N/A" else ""
    info["dmidecode_available"] = len(info["dmidecode_raw"]) > 50

    if info["dmidecode_available"]:
        slots, populated = [], []
        # 按 "Memory Device" 块切割
        blocks = re.split(r"(?=Memory Device\n)", dmi)
        for blk in blocks:
            if "Memory Device" not in blk:
                continue
            def _field(key):
                m = re.search(rf"^\s*{key}:\s*(.+)", blk, re.M)
                return m.group(1).strip() if m else "?"
            size   = _field("Size")
            slot   = {
                "size":    size,
                "locator": _field("Locator"),
                "bank":    _field("Bank Locator"),
                "speed":   _field("Speed"),
                "type":    _field("Type"),
                "rank":    _field("Rank"),
                "mfr":     _field("Manufacturer"),
            }
            slots.append(slot)
            empty_vals = {"no module installed", "not installed",
                          "unknown", "0 mb", "0 gb"}
            if size.lower() not in empty_vals and not size.startswith("0 "):
                populated.append(slot)

        info["total_slots"]     = len(slots)
        info["populated_slots"] = len(populated)
        info["dimm_details"]    = populated

        banks = {s["bank"] for s in populated
                 if s["bank"] not in ("?", "Unknown", "Not Specified")}
        if banks:
            info["channels_detected"] = len(banks)
            info["bank_locators"]     = sorted(banks)

    # numactl
    numa = run_cmd("numactl --hardware 2>/dev/null")
    if numa != "N/A":
        info["numa_info"] = numa

    # 内存带宽（mbw，可选）
    mbw = run_cmd("which mbw 2>/dev/null")
    if mbw and mbw != "N/A":
        bw_out = run_cmd("mbw -n 3 256 2>/dev/null | grep AVG", timeout=60)
        info["mbw_result"] = bw_out

    return info


def get_ffmpeg_info():
    ver_str = run_cmd("ffmpeg -version 2>&1 | head -20")
    m = re.search(r"ffmpeg version (\S+)", ver_str)
    enc_out = run_cmd(
        "ffmpeg -encoders 2>/dev/null | grep -E 'libx264|libx265|vp9|nvenc|vaapi'"
    )
    return {
        "version":           m.group(1) if m else "N/A",
        "version_string":    ver_str,
        "relevant_encoders": enc_out,
    }


def get_system_state():
    return {
        "timestamp":     datetime.datetime.now().isoformat(),
        "load_avg":      run_cmd("cat /proc/loadavg"),
        "free_output":   run_cmd("free -h"),
        "vmstat":        run_cmd("vmstat 1 2 2>/dev/null | tail -1"),
        "top_processes": run_cmd(
            "ps aux --sort=-%cpu | head -11 | "
            "awk '{printf \"%-10s %5s %5s %5s  %s\\n\",$1,$2,$3,$4,$11}'"
        ),
        "disk_usage":    run_cmd("df -h / 2>/dev/null | tail -1"),
        "irq_info":      run_cmd("cat /proc/interrupts 2>/dev/null | head -5"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 后台 CPU / 内存监控
# ══════════════════════════════════════════════════════════════════════════════

class SystemMonitor:
    """在后台线程中每秒采样 /proc/stat 和 /proc/meminfo。"""

    def __init__(self, interval=1.0):
        self.interval = interval
        self._samples  = []
        self._stop_evt = threading.Event()
        self._thread   = None

    def start(self):
        self._samples  = []
        self._stop_evt = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop_evt.is_set():
            sample = {}
            try:
                with open("/proc/stat") as f:
                    line = f.readline()
                vals = list(map(int, line.split()[1:]))
                sample["cpu"] = vals
            except Exception:
                pass
            try:
                mem = {}
                with open("/proc/meminfo") as f:
                    for l in f:
                        if l.startswith(("MemTotal:", "MemAvailable:")):
                            k, v = l.split(":")
                            mem[k.strip()] = int(v.split()[0])
                if "MemTotal" in mem and "MemAvailable" in mem:
                    used_gb = (mem["MemTotal"] - mem["MemAvailable"]) / 1048576
                    sample["mem_used_gb"] = round(used_gb, 2)
                    sample["mem_total_gb"] = round(mem["MemTotal"] / 1048576, 2)
            except Exception:
                pass
            if sample:
                self._samples.append(sample)
            self._stop_evt.wait(self.interval)

    def get_summary(self):
        if len(self._samples) < 2:
            return {}
        cpu_pcts, mem_used = [], []
        for i in range(1, len(self._samples)):
            prev = self._samples[i - 1].get("cpu")
            curr = self._samples[i].get("cpu")
            if prev and curr:
                d     = [c - p for c, p in zip(curr, prev)]
                total = sum(d)
                idle  = d[3] if len(d) > 3 else 0
                if total > 0:
                    cpu_pcts.append(round((1 - idle / total) * 100, 1))
            mg = self._samples[i].get("mem_used_gb")
            if mg is not None:
                mem_used.append(mg)

        return {
            "cpu_avg":     round(sum(cpu_pcts) / len(cpu_pcts), 1) if cpu_pcts else None,
            "cpu_max":     round(max(cpu_pcts), 1) if cpu_pcts else None,
            "cpu_samples": cpu_pcts,
            "mem_avg_gb":  round(sum(mem_used) / len(mem_used), 2) if mem_used else None,
            "mem_max_gb":  round(max(mem_used), 2) if mem_used else None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# FFmpeg 测试执行
# ══════════════════════════════════════════════════════════════════════════════

def check_encoder(codec):
    """返回该 codec 是否可用。"""
    out = run_cmd(f"ffmpeg -encoders 2>/dev/null | grep ' {codec} '")
    return out != "N/A" and codec in out


def run_single_test(name, codec, resolution, preset, extra_args, duration):
    result = {
        "label":    name,
        "codec":    codec,
        "resolution": resolution,
        "preset":   preset,
        "duration_target": duration,
        "status":   "unknown",
    }

    if not check_encoder(codec):
        result["status"] = "encoder_not_found"
        return result

    cmd = ["ffmpeg", "-y",
           "-f", "lavfi",
           "-i", f"testsrc2=size={resolution}:rate=30",
           "-t", str(duration),
           "-c:v", codec]

    if preset:
        cmd += ["-preset", preset]
    if "-b:v" not in extra_args and "-crf" not in extra_args:
        cmd += ["-crf", "23"]

    cmd += extra_args
    cmd += ["-f", "null", "-"]

    monitor = SystemMonitor()
    monitor.start()
    t0 = time.time()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=duration * 4 + 90)
        elapsed = time.time() - t0
        result["elapsed"]  = round(elapsed, 2)
        result["command"]  = " ".join(cmd)
        result["return_code"] = proc.returncode
        stderr = proc.stderr

        speed_vals = [float(x) for x in re.findall(r"speed=\s*(\d+\.?\d*)x", stderr)]
        frame_vals = re.findall(r"frame=\s*(\d+)", stderr)

        frames = int(frame_vals[-1]) if frame_vals else 0
        result["frames_encoded"] = frames

        # 优先用 frames/elapsed 计算真实 FPS（比进度行的 fps= 更准确）
        if frames > 0 and elapsed > 0:
            result["fps_avg"] = round(frames / elapsed, 2)
        else:
            # 备用：从进度行解析，过滤掉开头的 0 值
            fps_vals = [float(x) for x in re.findall(r"fps=\s*(\d+\.?\d*)", stderr)
                        if float(x) > 0]
            if fps_vals:
                result["fps_avg"] = round(sum(fps_vals) / len(fps_vals), 2)

        if speed_vals:
            result["speed_x"] = speed_vals[-1]

        result["status"] = "success" if proc.returncode == 0 else "error"
        if proc.returncode != 0:
            result["stderr_tail"] = stderr[-3000:]

    except subprocess.TimeoutExpired:
        result["status"]  = "timeout"
        result["elapsed"] = time.time() - t0
    except Exception as e:
        result["status"]  = "error"
        result["error"]   = str(e)
        result["elapsed"] = time.time() - t0
    finally:
        monitor.stop()
        result.update(monitor.get_summary())

    return result


def run_all_tests(label, output_dir, duration, test_indices=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    # 确保目录对当前用户可写（sudo 运行时目录可能被 root 建立）
    try:
        os.chmod(output_dir, 0o777)
    except Exception:
        pass

    print(f"\n{'═'*62}")
    print(f"  FFmpeg 性能基准测试  ·  配置标签: {label}")
    print(f"{'═'*62}")

    print("\n[1/3] 收集系统信息...")
    sys_info = {
        "label":        label,
        "timestamp":    datetime.datetime.now().isoformat(),
        "os":           get_os_info(),
        "cpu":          get_cpu_info(),
        "memory":       get_memory_info(),
        "ffmpeg":       get_ffmpeg_info(),
        "system_state": get_system_state(),
    }

    mem   = sys_info["memory"]
    cpu   = sys_info["cpu"]
    print(f"  主机名  : {sys_info['os']['hostname']}")
    print(f"  CPU     : {cpu.get('model','?')}")
    print(f"  内存    : {mem.get('total_gb','?')} GB")
    if "populated_slots" in mem:
        print(f"  DIMM    : {mem['populated_slots']} 条 / {mem['total_slots']} 槽")
    if "channels_detected" in mem:
        print(f"  Channel : {mem['channels_detected']} (自动检测)")
    print(f"  FFmpeg  : {sys_info['ffmpeg'].get('version','?')}")

    cases = TEST_CASES
    if test_indices:
        cases = [TEST_CASES[i] for i in test_indices if i < len(TEST_CASES)]

    print(f"\n[2/3] 运行 {len(cases)} 项 FFmpeg 测试（每项 {duration}s）...")
    test_results = []
    for idx, (name, codec, resolution, preset, extra) in enumerate(cases, 1):
        print(f"  [{idx:2d}/{len(cases)}] {name:<28} ", end="", flush=True)
        r = run_single_test(name, codec, resolution, preset, extra, duration)
        test_results.append(r)
        if r["status"] == "success":
            print(f"✓  {r.get('fps_avg','?'):>7} fps  |  "
                  f"CPU {r.get('cpu_avg','?')}%  |  "
                  f"x{r.get('speed_x','?')}")
        elif r["status"] == "encoder_not_found":
            print("⚠  编码器不可用，已跳过")
        else:
            print(f"✗  {r['status']}")

    print("\n[3/3] 保存结果...")
    ts         = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^\w\-]", "_", label)
    out_file   = output_dir / f"result_{safe_label}_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"system_info": sys_info, "test_results": test_results},
                  f, ensure_ascii=False, indent=2)
    print(f"  ✓ 已保存: {out_file}")
    return out_file


# ══════════════════════════════════════════════════════════════════════════════
# HTML 报告生成
# ══════════════════════════════════════════════════════════════════════════════

def load_all_results(output_dir):
    runs = []
    for p in sorted(output_dir.glob("result_*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
                d["_file"] = p.name
                runs.append(d)
        except Exception as e:
            print(f"警告: 无法读取 {p}: {e}")
    return runs


def svg_grouped_bar(datasets, x_labels, width=900, height=360):
    """
    生成分组柱状图 SVG（纯 Python，无外部依赖）。
    datasets: [{"label": str, "data": [float], "color": str}]
    x_labels: [str]
    """
    pad_l, pad_r, pad_t, pad_b = 60, 20, 30, 100
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b

    all_vals = [v for ds in datasets for v in ds["data"] if v]
    max_val  = max(all_vals) * 1.1 if all_vals else 1

    n_groups = len(x_labels)
    n_bars   = len(datasets)
    group_w  = chart_w / max(n_groups, 1)
    bar_w    = max(4, group_w / (n_bars + 1) * 0.85)

    def fy(v):
        return pad_t + chart_h - (v / max_val) * chart_h

    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'width="{width}" height="{height}" '
             f'style="background:#161b22;border-radius:6px;display:block;margin:auto">']

    # 网格线 & Y 轴刻度
    ticks = 5
    for i in range(ticks + 1):
        v   = max_val * i / ticks
        y   = fy(v)
        lbl = f"{v:.0f}"
        lines.append(f'<line x1="{pad_l}" y1="{y:.1f}" '
                     f'x2="{pad_l+chart_w}" y2="{y:.1f}" '
                     f'stroke="#30363d" stroke-width="1"/>')
        lines.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" '
                     f'text-anchor="end" font-size="11" fill="#8b949e">{lbl}</text>')

    # 轴线
    lines.append(f'<line x1="{pad_l}" y1="{pad_t}" '
                 f'x2="{pad_l}" y2="{pad_t+chart_h}" stroke="#8b949e" stroke-width="1"/>')
    lines.append(f'<line x1="{pad_l}" y1="{pad_t+chart_h}" '
                 f'x2="{pad_l+chart_w}" y2="{pad_t+chart_h}" stroke="#8b949e" stroke-width="1"/>')

    # 柱子
    for gi, glbl in enumerate(x_labels):
        gx = pad_l + gi * group_w + group_w / 2
        for bi, ds in enumerate(datasets):
            v = ds["data"][gi] if gi < len(ds["data"]) else 0
            if v <= 0:
                continue
            bx = gx - (n_bars * bar_w) / 2 + bi * bar_w + bar_w * 0.05
            by = fy(v)
            bh = pad_t + chart_h - by
            c  = ds["color"]
            lines.append(f'<rect x="{bx:.1f}" y="{by:.1f}" '
                         f'width="{bar_w*0.9:.1f}" height="{bh:.1f}" '
                         f'fill="{c}" opacity="0.85" rx="2">'
                         f'<title>{ds["label"]}: {v} fps</title></rect>')
            if bh > 16:
                lines.append(f'<text x="{bx+bar_w*0.45:.1f}" y="{by-3:.1f}" '
                             f'text-anchor="middle" font-size="9" fill="#c9d1d9">'
                             f'{v:.0f}</text>')

        # X 轴标签（斜 40°）
        lines.append(f'<text transform="translate({gx:.1f},{pad_t+chart_h+12}) rotate(35)" '
                     f'font-size="11" fill="#8b949e">{glbl}</text>')

    # 图例
    lx = pad_l
    for bi, ds in enumerate(datasets):
        lines.append(f'<rect x="{lx}" y="{height-18}" width="12" height="12" '
                     f'fill="{ds["color"]}" rx="2"/>')
        lines.append(f'<text x="{lx+15}" y="{height-8}" font-size="11" '
                     f'fill="#c9d1d9">{ds["label"]}</text>')
        lx += len(ds["label"]) * 7 + 30

    lines.append('</svg>')
    return "\n".join(lines)


def svg_bar(labels, values, colors, width=700, height=300):
    """简单柱状图 SVG。"""
    pad_l, pad_r, pad_t, pad_b = 60, 20, 30, 60
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b

    max_val = max(values) * 1.1 if values else 1
    n       = len(labels)
    bar_w   = chart_w / n * 0.6
    gap     = chart_w / n

    def fy(v):
        return pad_t + chart_h - (v / max_val) * chart_h

    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'width="{width}" height="{height}" '
             f'style="background:#161b22;border-radius:6px;display:block;margin:auto">']

    for i in range(6):
        v = max_val * i / 5
        y = fy(v)
        lines.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+chart_w}" y2="{y:.1f}" '
                     f'stroke="#30363d" stroke-width="1"/>')
        lines.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="#8b949e">{v:.0f}</text>')

    lines.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t+chart_h}" '
                 f'stroke="#8b949e" stroke-width="1"/>')
    lines.append(f'<line x1="{pad_l}" y1="{pad_t+chart_h}" '
                 f'x2="{pad_l+chart_w}" y2="{pad_t+chart_h}" stroke="#8b949e" stroke-width="1"/>')

    for i, (lbl, val, col) in enumerate(zip(labels, values, colors)):
        bx = pad_l + i * gap + (gap - bar_w) / 2
        by = fy(val)
        bh = pad_t + chart_h - by
        lines.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
                     f'fill="{col}" opacity="0.85" rx="3">'
                     f'<title>{lbl}: {val}</title></rect>')
        lines.append(f'<text x="{bx+bar_w/2:.1f}" y="{by-5:.1f}" '
                     f'text-anchor="middle" font-size="12" fill="#c9d1d9">{val:.1f}</text>')
        lines.append(f'<text x="{bx+bar_w/2:.1f}" y="{pad_t+chart_h+16}" '
                     f'text-anchor="middle" font-size="12" fill="#8b949e">{lbl}</text>')

    lines.append('</svg>')
    return "\n".join(lines)


def svg_line(labels, values, colors, width=700, height=260):
    """折线趋势图 SVG。"""
    if len(labels) < 2:
        return svg_bar(labels, values, colors, width, height)

    pad_l, pad_r, pad_t, pad_b = 60, 20, 30, 50
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b
    max_val = max(values) * 1.1 if values else 1
    n       = len(labels)

    def fx(i): return pad_l + i * chart_w / (n - 1)
    def fy(v): return pad_t + chart_h - (v / max_val) * chart_h

    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'width="{width}" height="{height}" '
             f'style="background:#161b22;border-radius:6px;display:block;margin:auto">']

    for i in range(6):
        v = max_val * i / 5
        y = fy(v)
        lines.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+chart_w}" y2="{y:.1f}" '
                     f'stroke="#30363d" stroke-width="1"/>')
        lines.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="#8b949e">{v:.0f}</text>')

    lines.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t+chart_h}" '
                 f'stroke="#8b949e" stroke-width="1"/>')
    lines.append(f'<line x1="{pad_l}" y1="{pad_t+chart_h}" '
                 f'x2="{pad_l+chart_w}" y2="{pad_t+chart_h}" stroke="#8b949e" stroke-width="1"/>')

    # 折线（填充区域）
    pts = [(fx(i), fy(v)) for i, v in enumerate(values)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    fill_pts = (f"{pts[0][0]:.1f},{pad_t+chart_h} " + poly +
                f" {pts[-1][0]:.1f},{pad_t+chart_h}")
    lines.append(f'<polygon points="{fill_pts}" fill="#58a6ff" opacity="0.15"/>')
    lines.append(f'<polyline points="{poly}" fill="none" '
                 f'stroke="#58a6ff" stroke-width="2.5" stroke-linejoin="round"/>')

    # 数据点
    for i, (x, y) in enumerate(pts):
        c = colors[i % len(colors)]
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" '
                     f'fill="{c}" stroke="#161b22" stroke-width="2">'
                     f'<title>{labels[i]}: {values[i]:.1f} fps</title></circle>')
        lines.append(f'<text x="{x:.1f}" y="{y-10:.1f}" text-anchor="middle" '
                     f'font-size="11" fill="#c9d1d9">{values[i]:.1f}</text>')
        lines.append(f'<text x="{x:.1f}" y="{pad_t+chart_h+16}" text-anchor="middle" '
                     f'font-size="11" fill="#8b949e">{labels[i]}</text>')

    lines.append('</svg>')
    return "\n".join(lines)


def _js_array(values):
    """Python list → JS array literal."""
    def fmt(v):
        if v is None:
            return "null"
        if isinstance(v, (int, float)):
            return str(v)
        return f'"{v}"'
    return "[" + ", ".join(fmt(v) for v in values) + "]"


def generate_html_report(output_dir):
    all_runs = load_all_results(output_dir)
    if not all_runs:
        print("输出目录中没有测试结果。")
        return None

    # 同一 label 只保留最新一次（文件名按时间戳排序，最后的最新）
    seen = {}
    for r in all_runs:
        lbl = r["system_info"]["label"]
        seen[lbl] = r          # 后来的覆盖前面的
    runs = list(seen.values()) # 保持插入顺序（Python 3.7+）

    config_labels  = [r["system_info"]["label"] for r in runs]
    all_test_names = list(dict.fromkeys(           # 保持顺序去重
        t["label"] for r in runs for t in r["test_results"]
    ))

    # FPS / CPU 数据矩阵
    fps_by_test = {n: [] for n in all_test_names}
    cpu_by_test = {n: [] for n in all_test_names}
    for run in runs:
        tmap = {t["label"]: t for t in run["test_results"]}
        for n in all_test_names:
            t = tmap.get(n, {})
            fps = t.get("fps_avg") or t.get("fps_final") or 0
            fps_by_test[n].append(fps if t.get("status") == "success" else 0)
            cpu_by_test[n].append(t.get("cpu_avg") or 0)

    # 用于 "按配置" 汇总（每个配置一条线）
    # Chart 1: grouped bar — x轴=测试项，组=配置，值=fps
    chart1_datasets = []
    for ci, cfg in enumerate(config_labels):
        color = PALETTE[ci % len(PALETTE)]
        vals  = []
        for n in all_test_names:
            fps_list = fps_by_test[n]
            vals.append(fps_list[ci] if ci < len(fps_list) else 0)
        chart1_datasets.append({
            "label": cfg,
            "data":  vals,
            "color": color,
        })

    # Chart 2: 总 FPS 对比
    total_fps = []
    for ci in range(len(config_labels)):
        s = sum(fps_by_test[n][ci] for n in all_test_names
                if ci < len(fps_by_test[n]))
        total_fps.append(round(s, 1))

    # ── 多配置对比分析 ──────────────────────────────────────────
    # 以第一个配置（channel 最多）为基准，计算各配置的性能衰减
    comparison_rows = ""
    baseline_idx = 0
    baseline_label = config_labels[0] if config_labels else "baseline"

    if len(runs) >= 2:
        for n in all_test_names:
            fps_list = fps_by_test[n]
            base_fps = fps_list[baseline_idx] if fps_list else 0
            row = f"<tr><td>{n}</td>"
            for ci, cfg in enumerate(config_labels):
                cur_fps = fps_list[ci] if ci < len(fps_list) else 0
                if ci == baseline_idx:
                    row += f"<td class='base'>{cur_fps} <small>fps</small></td>"
                elif base_fps > 0:
                    diff = round((cur_fps - base_fps) / base_fps * 100, 1)
                    cls  = "pos" if diff >= 0 else "neg"
                    row += (f"<td>{cur_fps} <small>fps</small>"
                            f"<span class='{cls}'> ({diff:+.1f}%)</span></td>")
                else:
                    row += f"<td>{cur_fps} <small>fps</small></td>"
            row += "</tr>"
            comparison_rows += row

        # 总分衰减
        comparison_rows += "<tr class='total-row'><td><b>合计 FPS</b></td>"
        base_total = total_fps[baseline_idx] if total_fps else 0
        for ci, cfg in enumerate(config_labels):
            cur_total = total_fps[ci] if ci < len(total_fps) else 0
            if ci == baseline_idx:
                comparison_rows += f"<td class='base'><b>{cur_total}</b></td>"
            elif base_total > 0:
                diff = round((cur_total - base_total) / base_total * 100, 1)
                cls  = "pos" if diff >= 0 else "neg"
                comparison_rows += (f"<td><b>{cur_total}</b>"
                                    f"<span class='{cls}'> ({diff:+.1f}%)</span></td>")
            else:
                comparison_rows += f"<td><b>{cur_total}</b></td>"
        comparison_rows += "</tr>"

    # 对比表表头
    comparison_header = "<tr><th>测试项</th>" + \
        "".join(f"<th>{lbl}{'  ★基准' if i==0 else ''}</th>"
                for i, lbl in enumerate(config_labels)) + "</tr>"

    has_comparison = len(runs) >= 2

    # 生成 DIMM 表格行
    def dimm_rows(run):
        mem = run["system_info"]["memory"]
        details = mem.get("dimm_details", [])
        if not details:
            return "<tr><td colspan='6'>未能获取 DIMM 详情（需要 root 权限运行 dmidecode）</td></tr>"
        rows = ""
        for d in details:
            rows += (
                f"<tr>"
                f"<td>{d.get('locator','?')}</td>"
                f"<td>{d.get('bank','?')}</td>"
                f"<td>{d.get('size','?')}</td>"
                f"<td>{d.get('type','?')}</td>"
                f"<td>{d.get('speed','?')}</td>"
                f"<td>{d.get('mfr','?')}</td>"
                f"</tr>"
            )
        return rows

    def test_table_rows(run):
        rows = ""
        for t in run["test_results"]:
            status_class = "ok" if t["status"] == "success" else "err"
            fps  = f"{t.get('fps_avg', '-')}"
            cpu  = f"{t.get('cpu_avg', '-')}%"
            spd  = f"x{t.get('speed_x', '-')}"
            ela  = f"{t.get('elapsed', '-')}s"
            frm  = f"{t.get('frames_encoded', '-')}"
            rows += (
                f"<tr class='{status_class}'>"
                f"<td>{t['label']}</td>"
                f"<td>{t.get('codec','-')}</td>"
                f"<td>{t.get('resolution','-')}</td>"
                f"<td>{fps}</td>"
                f"<td>{cpu}</td>"
                f"<td>{spd}</td>"
                f"<td>{ela}</td>"
                f"<td>{frm}</td>"
                f"<td>{t['status']}</td>"
                f"</tr>"
            )
        return rows

    # 每个 run 的系统信息卡片
    config_cards = ""
    for ci, run in enumerate(runs):
        si  = run["system_info"]
        mem = si["memory"]
        cpu = si["cpu"]
        oss = si["os"]
        color = PALETTE[ci % len(PALETTE)]
        ch_info = (f"{mem['channels_detected']} channel(s) detected"
                   if "channels_detected" in mem else "N/A (需 root)")
        config_cards += f"""
        <div class="card" style="border-top:3px solid {color}">
          <div class="card-header" style="color:{color}">
            {si['label']}
            <span class="ts">{si['timestamp'][:19]}</span>
          </div>
          <table class="info-table">
            <tr><th>主机名</th>     <td>{oss.get('hostname','N/A')}</td></tr>
            <tr><th>系统</th>       <td>{oss.get('distro','N/A')}</td></tr>
            <tr><th>内核</th>       <td>{oss.get('kernel','N/A')}</td></tr>
            <tr><th>CPU 型号</th>   <td>{cpu.get('model','N/A')}</td></tr>
            <tr><th>逻辑 CPU 数</th><td>{cpu.get('logical_cpus','N/A')}</td></tr>
            <tr><th>NUMA 节点</th>  <td>{cpu.get('numa_nodes','N/A')}</td></tr>
            <tr><th>L3 Cache</th>   <td>{cpu.get('l3','N/A')}</td></tr>
            <tr><th>内存总量</th>   <td>{mem.get('total_gb','N/A')} GB</td></tr>
            <tr><th>可用内存</th>   <td>{mem.get('available_gb','N/A')} GB</td></tr>
            <tr><th>DIMM 数量</th>  <td>{mem.get('populated_slots','N/A')} / {mem.get('total_slots','N/A')} 槽</td></tr>
            <tr><th>内存 Channel</th><td>{ch_info}</td></tr>
            <tr><th>FFmpeg</th>     <td>{si['ffmpeg'].get('version','N/A')}</td></tr>
            <tr><th>负载均值</th>   <td>{si['system_state'].get('load_avg','N/A')}</td></tr>
          </table>

          <h4 style="margin:12px 0 6px">DIMM 插槽详情</h4>
          <table class="result-table">
            <tr><th>Locator</th><th>Bank</th><th>Size</th>
                <th>Type</th><th>Speed</th><th>厂商</th></tr>
            {dimm_rows(run)}
          </table>

          <h4 style="margin:12px 0 6px">测试结果明细</h4>
          <table class="result-table">
            <tr><th>测试项</th><th>编码器</th><th>分辨率</th>
                <th>FPS均值</th><th>CPU均值</th><th>速度</th>
                <th>耗时</th><th>帧数</th><th>状态</th></tr>
            {test_table_rows(run)}
          </table>
        </div>
        """

    # ── 生成 SVG 图表（纯 Python，无外部依赖）──────────────────────
    svg_chart1 = svg_grouped_bar(chart1_datasets, all_test_names, width=980, height=400)
    svg_chart2 = svg_bar(
        config_labels, total_fps,
        [PALETTE[i % len(PALETTE)] for i in range(len(config_labels))],
        width=700, height=300
    )
    svg_chart3 = ""
    if has_comparison:
        svg_chart3 = svg_line(
            config_labels, total_fps,
            [PALETTE[i % len(PALETTE)] for i in range(len(config_labels))],
            width=700, height=260
        )

    # ── 预先计算 HTML 片段 ──────────────────────────────────────────
    summary_cards_html = "".join(
        f'<div class="stat-card" style="border-top:3px solid {PALETTE[i % len(PALETTE)]}">'
        f'<div class="val">{total_fps[i]}</div>'
        f'<div class="lbl">{cfg} &nbsp;·&nbsp; 总 FPS</div>'
        f'</div>'
        for i, cfg in enumerate(config_labels)
    )
    comparison_section_html = ""
    if has_comparison:
        comparison_section_html = f"""
  <!-- ───── 对比分析 ───── -->
  <section id="compare">
    <h2>多 Channel 配置性能对比分析</h2>
    <p style="color:var(--muted);font-size:12px;margin-bottom:12px">
      以 <b>{baseline_label}</b> 为基准（★），绿色表示性能提升，红色表示性能下降。
      百分比 = (当前 FPS − 基准 FPS) / 基准 FPS × 100%。
    </p>
    <div class="chart-wrap" style="overflow-x:auto">
      <table class="result-table cmp-table">
        {comparison_header}
        {comparison_rows}
      </table>
    </div>

    <div class="chart-wrap">
      <h3>各配置总 FPS 衰减趋势</h3>
      {svg_chart3}
    </div>
  </section>
"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FFmpeg 性能基准测试报告</title>
<style>
  :root {{
    --bg:     #0d1117;  --card:   #161b22;
    --border: #30363d;  --text:   #c9d1d9;
    --muted:  #8b949e;  --accent: #58a6ff;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text);
         font-family:'Segoe UI',Arial,sans-serif; font-size:14px; }}
  a {{ color:var(--accent); text-decoration:none; }}
  h1 {{ font-size:22px; font-weight:600; }}
  h2 {{ font-size:17px; font-weight:600; margin:28px 0 12px; color:var(--accent); }}
  h3 {{ font-size:15px; font-weight:600; margin:16px 0 8px; }}
  h4 {{ font-size:13px; font-weight:600; color:var(--muted); }}

  .header {{
    background:#161b22; border-bottom:1px solid var(--border);
    padding:16px 32px; display:flex; align-items:center; gap:20px;
  }}
  .nav {{ display:flex; gap:10px; margin-left:auto; flex-wrap:wrap; }}
  .nav a {{
    padding:5px 12px; border-radius:6px; border:1px solid var(--border);
    font-size:13px; color:var(--text);
  }}
  .nav a:hover {{ background:var(--border); }}

  .main {{ max-width:1500px; margin:0 auto; padding:24px 32px; }}

  .summary-row {{
    display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
    gap:12px; margin-bottom:16px;
  }}
  .stat-card {{
    background:var(--card); border:1px solid var(--border);
    border-radius:8px; padding:16px;
  }}
  .stat-card .val {{ font-size:28px; font-weight:700; line-height:1.2; }}
  .stat-card .lbl {{ font-size:12px; color:var(--muted); margin-top:4px; }}

  .card {{
    background:var(--card); border:1px solid var(--border);
    border-radius:8px; padding:20px; margin-bottom:20px;
  }}
  .card-header {{
    font-size:16px; font-weight:700; margin-bottom:12px;
    display:flex; align-items:center; gap:12px;
  }}
  .ts {{ font-size:12px; color:var(--muted); font-weight:400; }}

  .info-table, .result-table {{
    width:100%; border-collapse:collapse; font-size:13px; margin-bottom:8px;
  }}
  .info-table th {{
    text-align:right; padding:3px 10px 3px 0;
    color:var(--muted); width:130px; white-space:nowrap;
  }}
  .info-table td {{ padding:3px 0; }}
  .result-table th {{
    background:#21262d; padding:7px 10px; text-align:left;
    border-bottom:1px solid var(--border); white-space:nowrap;
  }}
  .result-table td {{ padding:6px 10px; border-bottom:1px solid #21262d; }}
  .result-table tr:hover td {{ background:#1c2128; }}
  .result-table tr.err td {{ color:#f85149; }}
  .result-table tr.total-row td {{ background:#21262d; font-weight:600; }}

  /* 对比表专用样式 */
  .cmp-table td.base {{ color:#58a6ff; font-weight:600; }}
  .cmp-table .pos {{ color:#3fb950; font-size:12px; }}
  .cmp-table .neg {{ color:#f85149; font-size:12px; }}
  .cmp-table small {{ color:var(--muted); }}

  .chart-wrap {{
    background:var(--card); border:1px solid var(--border);
    border-radius:8px; padding:20px; margin-bottom:20px;
  }}

  footer {{
    text-align:center; padding:24px; color:var(--muted); font-size:12px;
    border-top:1px solid var(--border); margin-top:40px;
  }}
  pre {{
    background:#21262d; border:1px solid var(--border); border-radius:6px;
    padding:12px; overflow-x:auto; font-size:12px;
    white-space:pre-wrap; word-break:break-all;
    max-height:260px; overflow-y:auto; margin-top:6px;
  }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>FFmpeg 性能基准测试报告</h1>
    <div style="color:var(--muted);font-size:12px;margin-top:4px">
      生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
      &nbsp;·&nbsp; 配置数: {len(runs)}
      &nbsp;·&nbsp; 测试项: {len(all_test_names)}
    </div>
  </div>
  <nav class="nav">
    <a href="#overview">总览</a>
    <a href="#charts">图表</a>
    {'<a href="#compare">对比分析</a>' if has_comparison else ''}
    <a href="#configs">配置详情</a>
  </nav>
</div>

<div class="main">

  <!-- ───── 总览 ───── -->
  <section id="overview">
    <h2>总览</h2>
    <div class="summary-row">
      {summary_cards_html}
    </div>
    <p style="color:var(--muted);font-size:12px">
      「总 FPS」= 该配置所有测试项 FPS 之和（横向趋势对比用）。
      FPS 由 <b>编码帧数 ÷ 实际耗时</b> 计算，精度高于 FFmpeg 进度行瞬时值。
    </p>
  </section>

  <!-- ───── 图表 ───── -->
  <section id="charts">
    <h2>性能图表</h2>

    <div class="chart-wrap">
      <h3>各测试项 FPS 对比（每组 = 一个内存 Channel 配置）</h3>
      {svg_chart1}
    </div>

    <div class="chart-wrap">
      <h3>各配置总 FPS（所有测试项之和）</h3>
      {svg_chart2}
    </div>
  </section>

  {comparison_section_html}

  <!-- ───── 配置详情 ───── -->
  <section id="configs">
    <h2>各配置详情</h2>
    {config_cards}
  </section>

</div>

<footer>FFmpeg Benchmark Tool &nbsp;·&nbsp; 报告自动生成（图表由纯 SVG 渲染，无需网络）</footer>
</body>
</html>
"""

    report_path = output_dir / "report.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path


# ══════════════════════════════════════════════════════════════════════════════
# HTTP 服务器
# ══════════════════════════════════════════════════════════════════════════════

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 只记录非 GET 或 错误
        if args and len(args) >= 2 and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def get_all_ips():
    """获取本机所有网络接口的 IP 地址。"""
    import socket
    ips = []

    # 方法1：通过连接外网获取主出口 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(("主出口", s.getsockname()[0]))
        s.close()
    except Exception:
        pass

    # 方法2：枚举所有接口（需要 Linux /proc/net/if_inet6 或 ip 命令）
    try:
        out = subprocess.run(
            "ip -4 addr show | grep 'inet ' | awk '{print $2, $NF}'",
            shell=True, capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                cidr, iface = parts[0], parts[1]
                ip = cidr.split("/")[0]
                if ip != "127.0.0.1" and not any(ip == x for _, x in ips):
                    ips.append((iface, ip))
    except Exception:
        pass

    # 方法3：fallback — gethostbyname
    if not ips:
        try:
            import socket
            ip = socket.gethostbyname(socket.gethostname())
            if ip != "127.0.0.1":
                ips.append(("hostname", ip))
        except Exception:
            pass

    return ips


def get_ssh_user():
    """尝试获取当前 SSH 登录用户名。"""
    return (os.environ.get("USER") or
            os.environ.get("LOGNAME") or
            run_cmd("whoami", default="user"))


def serve_report(output_dir, port, bind_host="0.0.0.0"):
    """启动 HTTP 服务器，供远端浏览器访问报告。"""
    import socket

    os.chdir(output_dir)

    # 尝试绑定端口（自动顺延）
    srv = None
    actual_port = port
    for p in range(port, port + 10):
        try:
            socketserver.TCPServer.allow_reuse_address = True
            srv = socketserver.TCPServer((bind_host, p), QuietHandler)
            actual_port = p
            break
        except OSError:
            print(f"  端口 {p} 被占用，尝试 {p + 1}...")

    if srv is None:
        print(f"错误：无法在 {port}~{port+9} 范围内找到可用端口，"
              f"请用 --port 指定其他端口。")
        return

    hostname   = socket.gethostname()
    all_ips    = get_all_ips()
    ssh_user   = get_ssh_user()
    report_url = f"/report.html"

    w = 62
    print(f"\n{'═' * w}")
    print(f"  报告服务器已启动".center(w))
    print(f"{'─' * w}")
    print(f"  主机名 : {hostname}")
    print(f"  端口   : {actual_port}")
    print(f"  目录   : {output_dir}")
    print(f"{'─' * w}")
    print(f"  【直接访问（服务器可从浏览器直达时）】")
    for iface, ip in all_ips:
        print(f"  http://{ip}:{actual_port}{report_url}   ({iface})")
    print()
    print(f"  【SSH 隧道访问（推荐，适合你通过 SSH 连接服务器）】")
    print(f"  在你的笔记本上执行以下命令：")
    print()
    for iface, ip in all_ips[:1]:   # 用主出口 IP 作为示例
        print(f"    ssh -N -L {actual_port}:{ip}:{actual_port} {ssh_user}@{ip}")
    if not all_ips:
        print(f"    ssh -N -L {actual_port}:localhost:{actual_port} "
              f"{ssh_user}@<服务器IP>")
    print()
    print(f"  然后在笔记本浏览器中打开：")
    print(f"    http://localhost:{actual_port}{report_url}")
    print(f"{'─' * w}")
    print(f"  按 Ctrl-C 停止服务器")
    print(f"{'═' * w}\n")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")
        srv.server_close()


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="FFmpeg 性能基准测试工具（内存 Channel 对比）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 4-channel 配置测试，完成后启动报告服务器
  sudo python3 ffmpeg_benchmark.py --label "4-channel"

  # 改完 BIOS 内存 channel 后追加测试
  sudo python3 ffmpeg_benchmark.py --label "2-channel"

  # 仅查看已有报告（不重新测试）
  python3 ffmpeg_benchmark.py --report-only

  # 快速测试（每项 10 秒）
  sudo python3 ffmpeg_benchmark.py --label "test" --duration 10

  # 指定端口 / 输出目录
  sudo python3 ffmpeg_benchmark.py --label "1-channel" --port 9090 --output-dir /data/bench

  # 从笔记本通过 SSH 隧道访问报告（在笔记本上执行）：
  #   ssh -N -L 8080:<服务器IP>:8080 user@<服务器IP>
  # 然后在笔记本浏览器打开：http://localhost:8080/report.html
        """
    )
    parser.add_argument("--label",       type=str,
                        help="本次测试的配置标签，如 '4-channel'（测试时必填）")
    parser.add_argument("--duration",    type=int, default=DEFAULT_DURATION,
                        help=f"每项测试秒数（默认 {DEFAULT_DURATION}）")
    parser.add_argument("--output-dir",  type=Path, default=DEFAULT_OUTPUT_DIR,
                        help=f"结果输出目录（默认 {DEFAULT_OUTPUT_DIR}）")
    parser.add_argument("--port",        type=int, default=DEFAULT_PORT,
                        help=f"HTTP 报告服务端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--bind",        type=str, default="0.0.0.0",
                        help="HTTP 服务器监听地址（默认 0.0.0.0 即所有接口）")
    parser.add_argument("--report-only", action="store_true",
                        help="不运行测试，只重新生成报告并启动服务器")
    parser.add_argument("--no-serve",    action="store_true",
                        help="运行测试后不启动 HTTP 服务器")
    parser.add_argument("--tests",       type=str,
                        help="只运行指定序号的测试（逗号分隔，从0开始）。"
                             "可用: " +
                             ", ".join(f"{i}={n}" for i,(n,*_) in enumerate(TEST_CASES)))
    args = parser.parse_args()

    output_dir = args.output_dir

    if not args.report_only:
        if not args.label:
            parser.error("运行测试时必须提供 --label，例如 --label '4-channel'")

        test_indices = None
        if args.tests:
            try:
                test_indices = [int(x.strip()) for x in args.tests.split(",")]
            except ValueError:
                parser.error("--tests 格式错误，请使用逗号分隔数字，如 '0,1,2'")

        run_all_tests(args.label, output_dir, args.duration, test_indices)

    # 生成 README（始终写到脚本所在目录）
    script_dir  = Path(__file__).resolve().parent
    readme_path = generate_readme(script_dir)
    if readme_path:
        print(f"README 已生成: {readme_path}")
    else:
        print("README 生成跳过（目录无写权限）")

    print("\n正在生成 HTML 报告...")
    report_path = generate_html_report(output_dir)
    if report_path:
        print(f"报告已生成: {report_path}")

    if not args.no_serve:
        serve_report(output_dir, args.port, bind_host=args.bind)
    else:
        print(f"\n提示：手动启动报告服务器：")
        print(f"  python3 ffmpeg_benchmark.py --report-only --port {args.port}")
        print(f"\n  或通过 SSH 隧道访问（在笔记本上执行）：")
        print(f"  ssh -N -L {args.port}:<服务器IP>:{args.port} user@<服务器IP>")


# ══════════════════════════════════════════════════════════════════════════════
# README 生成
# ══════════════════════════════════════════════════════════════════════════════

def generate_readme(script_dir: Path):
    """在脚本所在目录生成 README.md 使用说明。"""
    script_name = Path(__file__).name
    readme_path = script_dir / "README.md"

    test_list = "\n".join(
        f"| {i} | {name} | {codec} | {res} | {preset or 'N/A'} |"
        for i, (name, codec, res, preset, _) in enumerate(TEST_CASES)
    )

    content = f"""# FFmpeg 性能基准测试工具

测试服务器在**不同内存 Channel 配置**下的 FFmpeg 编码性能，
生成可通过浏览器远端查看的 HTML 对比报告。

---

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.6+ | 标准库，无需额外安装 |
| FFmpeg | 需包含 libx264 / libx265 / libvpx-vp9 编码器 |
| dmidecode | 读取 DIMM 内存插槽信息（需 root 权限） |
| mbw（可选） | 内存带宽测试 `sudo apt install mbw` |

---

## 快速开始

```bash
# 1. 上传脚本到服务器
scp {script_name} user@<服务器IP>:/home/user/

# 2. SSH 登录服务器
ssh user@<服务器IP>

# 3. 运行测试（建议 sudo，用于读取内存 channel 信息）
sudo python3 {script_name} --label "4-channel"

# 4. 在笔记本新开终端，建立 SSH 隧道
ssh -N -L 8080:<服务器IP>:8080 user@<服务器IP>

# 5. 笔记本浏览器打开
# http://localhost:8080/report.html
```

---

## 内存 Channel 对比测试流程

```bash
# 第一次：服务器处于 4-channel 配置
sudo python3 {script_name} --label "4-channel"

# 修改 BIOS/拔掉内存后，重启服务器，再次运行：
sudo python3 {script_name} --label "2-channel"

# 继续减少 channel：
sudo python3 {script_name} --label "1-channel"

# 每次运行完刷新浏览器，即可看到多配置对比图表
```

---

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--label TEXT` | 无（必填） | 本次测试的配置标签，如 `4-channel` |
| `--duration N` | `{DEFAULT_DURATION}` | 每项测试持续秒数 |
| `--output-dir PATH` | 脚本目录/benchmark_results | 结果和报告的保存目录 |
| `--port N` | `{DEFAULT_PORT}` | HTTP 报告服务端口 |
| `--bind HOST` | `0.0.0.0` | HTTP 服务器监听地址 |
| `--report-only` | — | 不测试，仅重新生成报告并启动服务器 |
| `--no-serve` | — | 测试完成后不启动 HTTP 服务器 |
| `--tests 0,1,2` | 全部 | 只运行指定序号的测试项 |

---

## 测试项列表

| 序号 | 名称 | 编码器 | 分辨率 | Preset |
|------|------|--------|--------|--------|
{test_list}

---

## 输出目录结构

```
benchmark_results/          ← 默认输出目录（脚本同级）
├── report.html             ← HTML 报告（浏览器打开）
├── result_4-channel_20250301_120000.json
├── result_2-channel_20250301_140000.json
└── result_1-channel_20250301_160000.json
```

---

## SSH 隧道访问说明

服务器一般没有公网 HTTP 访问权限，推荐通过 SSH 隧道访问报告：

```bash
# 在笔记本本地终端执行（保持运行）
ssh -N -L <本地端口>:<服务器IP>:<服务器端口> <用户名>@<服务器IP>

# 示例
ssh -N -L 8080:192.168.1.100:8080 root@192.168.1.100

# 然后笔记本浏览器打开
http://localhost:8080/report.html
```

脚本启动服务器时会自动打印该命令，直接复制使用即可。

---

## 报告内容说明

| 区域 | 内容 | 触发条件 |
|------|------|---------|
| 总览 | 各 Channel 配置的总 FPS 对比卡片 | 始终显示 |
| 图表 · Chart 1 | 分测试项 FPS 分组柱状图（每组 = 一个配置） | 始终显示 |
| 图表 · Chart 2 | 各配置总 FPS 柱状图 | 始终显示 |
| **对比分析** | 多配置 FPS 对比表 + 衰减百分比 + 趋势折线图 | **≥ 2 次测试后自动出现** |
| 配置详情 | CPU / 内存 / DIMM 插槽 / OS / FFmpeg 版本等 | 始终显示 |
| 测试明细 | 每项的 FPS 均值、CPU 占用、编码速度倍率、帧数 | 始终显示 |

> **FPS 计算说明**：使用 `编码帧数 ÷ 实际耗时` 计算，比 FFmpeg 进度行的瞬时 `fps=` 更准确。

---

## 对比分析说明

运行 ≥ 2 次（不同 `--label`）后，报告自动生成对比分析区块：

- **对比表格**：以第一次运行的配置为基准（★），显示每个测试项在各配置下的 FPS
  - 绿色 `+x.x%` = 相对基准性能提升
  - 红色 `-x.x%` = 相对基准性能下降
- **趋势折线图**：直观展示 channel 减少后总 FPS 的变化曲线

---

## 常见问题

**Q: DIMM 插槽信息显示"需 root 权限"？**
使用 `sudo python3 {script_name} ...` 运行即可。

**Q: H.265 / VP9 显示"编码器不可用"？**
检查 FFmpeg 编译时是否包含相应编码器：
```bash
ffmpeg -encoders 2>/dev/null | grep -E 'libx265|vp9'
```

**Q: 端口被占用？**
用 `--port 9090` 指定其他端口，脚本也会自动尝试顺延端口。

**Q: 如何只跑部分测试节省时间？**
```bash
sudo python3 {script_name} --label "4-channel" --tests 0,1,4 --duration 15
```

**Q: 对比分析没有出现？**
需要至少运行两次（使用不同的 `--label`），每次结果保存为独立 JSON 文件后，
重新生成报告时会自动出现对比分析区块。
```bash
# 确认结果文件是否存在
ls benchmark_results/result_*.json
```
"""

    try:
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(content)
        return readme_path
    except PermissionError:
        # 脚本目录无写权限时，尝试写到输出目录
        alt_path = Path(__file__).resolve().parent / "benchmark_results" / "README.md"
        try:
            alt_path.parent.mkdir(parents=True, exist_ok=True)
            with open(alt_path, "w", encoding="utf-8") as f:
                f.write(content)
            return alt_path
        except Exception:
            return None


if __name__ == "__main__":
    main()
