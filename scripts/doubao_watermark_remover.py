# -*- coding: utf-8 -*-
"""
doubao_watermark_remover.py — 豆包视频「AI生成」烧录水印去除专用工具

原理（基于对豆包分享视频的逆向实测）：
  1. 豆包生成的视频在导出时把浅色「AI生成」标识烧录进画面左上角，
     位置与尺寸按【视频宽度等比缩放】：
       x0 ≈ 0.0361*W, y0 ≈ 0.0278*W, 宽 ≈ 0.075*W, 高 ≈ 0.028*W
  2. 本工具先对视频抽多帧做时间恒定性分析自动精确定位水印
     （水印跨帧不变、比周围背景亮；视频内容是动态的），
     定位失败时回退到上述统一比例公式。
  3. 逐帧用 OpenCV Telea 图像修复(inpaint)从水印周围像素重建背景，
     通过 ffmpeg 管道解码/编码，音频流直拷，无转码质量二次损失之外的重压缩
     （视频 CRF 18 高质量重编码）。

用法：
  python doubao_watermark_remover.py                       # 处理 ./videos 下全部 mp4 -> ./videos_clean
  python doubao_watermark_remover.py -i 某个.mp4           # 处理单个文件
  python doubao_watermark_remover.py -i in.mp4 -o out.mp4 --box 24,18,80,41   # 手动指定水印框
  python doubao_watermark_remover.py -i in.mp4 --preview   # 只导出处理前后对比帧，不转码

依赖：opencv-python (cv2) + numpy + ffmpeg/ffprobe
"""
import argparse
import json
import os
import re
import subprocess
import sys

import cv2
import numpy as np

# ---------- ffmpeg 探测 ----------
def find_tool(name: str) -> str:
    cand = []
    env = os.environ.get("FFMPEG_DIR")
    if env:
        cand.append(os.path.join(env, name))
    cand += [name, os.path.join(os.path.dirname(os.path.abspath(__file__)), name)]
    for c in cand:
        try:
            r = subprocess.run([c, "-version"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    print(f"[错误] 找不到 {name}，请安装 ffmpeg 或设置 FFMPEG_DIR 环境变量")
    sys.exit(1)


FFMPEG = None
FFPROBE = None


def probe_video(path: str):
    """用 ffmpeg -i 输出解析视频信息（不依赖 ffprobe）
    返回 dict: width,height,fps,duration"""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                       capture_output=True, text=True, timeout=120)
    err = r.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", err)
    dur = 0.0
    if m:
        h, mi, s, cs = m.groups()
        dur = int(h) * 3600 + int(mi) * 60 + int(s) + int(cs) / 100
    vm = re.search(r"Stream #\d+:\d+.*?Video:\s*\w+.*?(\d{2,5})x(\d{2,5})", err)
    if not vm:
        raise RuntimeError(f"无法从 ffmpeg 输出解析分辨率: {err[-300:]}")
    W, H = int(vm.group(1)), int(vm.group(2))
    fm = re.search(r"(\d+(?:\.\d+)?)\s*fps", err) or re.search(r"(\d+)/(\d+)\s*fps", err)
    if fm:
        fps = float(fm.group(2)) / float(fm.group(1)) if fm.lastindex == 2 else float(fm.group(1))
    else:
        fps = 24.0
    return {"width": W, "height": H, "fps": fps, "duration": dur}


# ---------- 自动水印定位 ----------
def detect_watermark_box(path: str, n_samples: int = 12,
                         search_ratio=(0.40, 0.30)) -> tuple:
    """
    抽 n_samples 帧做时间分析，在左上 search_ratio 区域定位水印。
    返回 (x0, y0, x1, y1)；失败返回 None。
    水印特征：跨帧恒定(std低) 且 比周围背景亮。
    """
    info = probe_video(path)
    W, H, dur = info["width"], info["height"], info["duration"]
    sw, sh = int(W * search_ratio[0]), int(H * search_ratio[1])

    times = [dur * (i + 0.5) / n_samples for i in range(n_samples)] if dur > 0.5 \
        else [0.1 + 0.3 * i for i in range(n_samples)]
    frames = []
    for t in times:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
               "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if len(r.stdout) == W * H:
            frames.append(np.frombuffer(r.stdout, np.uint8).reshape(H, W))
    if len(frames) < 4:
        return None

    stack = np.stack(frames, 0).astype(np.float32)
    med = np.median(stack, 0).astype(np.uint8)
    std = stack.std(0)
    blur = cv2.GaussianBlur(med.astype(np.float32), (15, 15), 0)
    score = (med.astype(np.float32) - blur) * (std < 6.0)

    region = score[:sh, :sw]
    # 先在统一比例预测框附近找，命中即用（更稳）
    px0, py0 = int(0.030 * W), int(0.022 * W)
    px1, py1 = int(0.118 * W), int(0.064 * W)
    inner = region[max(0, py0):min(sh, py1), max(0, px0):min(sw, px1)]
    if inner.size and (inner > 15).sum() >= 30:
        ys, xs = np.where(inner > 15)
        bx0, by0 = px0 + int(xs.min()), py0 + int(ys.min())
        bx1, by1 = px0 + int(xs.max()), py0 + int(ys.max())
        return (bx0, by0, bx1, by1)

    # 兜底：全搜索区找最大亮恒定连通域
    _, binm = cv2.threshold(region.astype(np.uint8), 12, 255, cv2.THRESH_BINARY)
    binm = cv2.morphologyEx(binm, cv2.MORPH_CLOSE, np.ones((3, 9), np.uint8))
    cnts, _ = cv2.findContours(binm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if 8 <= w <= 0.25 * W and 6 <= h <= 0.1 * H and w * h >= 200:
            if best is None or w * h > best[2] * best[3]:
                best = (x, y, x + w, y + h)
    return best


def fallback_box(W: int, H: int) -> tuple:
    """统一比例公式（按宽度等比）"""
    return (int(0.0340 * W), int(0.0255 * W), int(0.1120 * W), int(0.0600 * W))


# ---------- 逐帧修复 ----------
def process_video(src: str, dst: str, box: tuple, crf: int = 18,
                  margin: int = 4, radius: int = 4, verbose=True):
    info = probe_video(src)
    W, H, fps = info["width"], info["height"], info["fps"]
    x0, y0, x1, y1 = box
    # 外扩 margin 并裁到画面内
    ex0, ey0 = max(0, x0 - margin), max(0, y0 - margin)
    ex1, ey1 = min(W - 1, x1 + margin), min(H - 1, y1 + margin)

    # 修复 mask：矩形(水印框+margin 再外扩3px) + 高斯羽化
    # 255 核心覆盖水印及光晕，羽化过渡带落在无水印区，混合无缝
    pad_out = 3
    mask = np.zeros((H, W), np.uint8)
    my0, my1 = max(0, ey0 - pad_out), min(H - 1, ey1 + pad_out)
    mx0, mx1 = max(0, ex0 - pad_out), min(W - 1, ex1 + pad_out)
    mask[my0:my1 + 1, mx0:mx1 + 1] = 255
    k = max(3, (pad_out * 2 + 1) | 1)
    mask = cv2.GaussianBlur(mask, (k, k), 0)

    frame_size = W * H * 3
    dec = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", src,
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=frame_size * 4)
    enc = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", f"{fps:.6f}", "-i", "-",
         "-i", src, "-map", "0:v:0", "-map", "1:a?",
         "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
         "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         "-c:a", "copy", dst],
        stdin=subprocess.PIPE, bufsize=frame_size * 4)

    alpha3 = (mask.astype(np.float32) / 255.0)[..., None]
    n = 0
    try:
        while True:
            raw = dec.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(H, W, 3).copy()
            # 全帧 inpaint：mask 外全是原始像素，修复质量最佳
            fixed = cv2.inpaint(frame, mask, radius, cv2.INPAINT_TELEA)
            # 羽化融合：mask 权重混合，边缘无硬接缝
            frame[:] = (
                fixed.astype(np.float32) * alpha3 + frame.astype(np.float32) * (1 - alpha3)
            ).astype(np.uint8)
            enc.stdin.write(frame.tobytes())
            n += 1
            if verbose and n % 60 == 0:
                print(f"    已处理 {n} 帧...", flush=True)
    finally:
        dec.stdout.close()
        dec.wait()
        enc.stdin.close()
        enc.wait()
    if enc.returncode != 0:
        raise RuntimeError(f"ffmpeg 编码失败 (exit {enc.returncode})")
    return n


# ---------- 对比帧导出 ----------
def export_preview(src: str, box: tuple, out_prefix: str, margin: int = 4):
    info = probe_video(src)
    W, H = info["width"], info["height"]
    x0, y0, x1, y1 = box
    ex0, ey0 = max(0, x0 - margin), max(0, y0 - margin)
    ex1, ey1 = min(W - 1, x1 + margin), min(H - 1, y1 + margin)
    mask = np.zeros((H, W), np.uint8)
    mask[ey0:ey1 + 1, ex0:ex1 + 1] = 255
    k = max(3, (margin * 2) | 1)
    mask = cv2.GaussianBlur(mask, (k, k), 0)

    pad = 40
    cz = (max(0, ey0 - pad), min(H, ey1 + pad), max(0, ex0 - pad), min(W, ex1 + pad))

    for tag, t in (("before", info["duration"] * 0.5),):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
               "-i", src, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if len(r.stdout) != W * H * 3:
            continue
        frame = np.frombuffer(r.stdout, np.uint8).reshape(H, W, 3).copy()
        roi = frame[ey0:ey1 + 1, ex0:ex1 + 1]
        mroi = mask[ey0:ey1 + 1, ex0:ex1 + 1]
        fixed = cv2.inpaint(roi, mroi, 4, cv2.INPAINT_TELEA)
        alpha = (mroi.astype(np.float32) / 255.0)[..., None]
        frame[ey0:ey1 + 1, ex0:ex1 + 1] = (
            fixed.astype(np.float32) * alpha + roi.astype(np.float32) * (1 - alpha)
        ).astype(np.uint8)
        zoom = 5
        a = cv2.resize(frame[cz[0]:cz[1], cz[2]:cz[3]], None, fx=zoom, fy=zoom,
                       interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(f"{out_prefix}_{tag}.png", a)
    return f"{out_prefix}_before.png"


# ---------- 主流程 ----------
def resolve_box(path: str, manual: str | None, detect: bool):
    info = probe_video(path)
    W, H = info["width"], info["height"]
    if manual:
        x0, y0, x1, y1 = [int(v) for v in manual.split(",")]
        return (x0, y0, x1, y1), "manual"
    if detect:
        b = detect_watermark_box(path)
        if b:
            return b, "auto"
    return fallback_box(W, H), "fallback-ratio"


def main():
    global FFMPEG, FFPROBE
    ap = argparse.ArgumentParser(description="豆包视频「AI生成」水印去除工具")
    ap.add_argument("-i", "--input", help="单个视频文件（缺省处理 videos 目录全部 mp4）")
    ap.add_argument("-o", "--output", help="输出文件/目录")
    ap.add_argument("--box", help="手动指定水印框 x0,y0,x1,y1（覆盖自动检测）")
    ap.add_argument("--no-detect", action="store_true", help="跳过自动检测，直接用比例公式")
    ap.add_argument("--crf", type=int, default=18, help="输出质量 CRF（默认18，越小越清晰）")
    ap.add_argument("--margin", type=int, default=4, help="水印框外扩像素（默认4）")
    ap.add_argument("--preview", action="store_true", help="仅导出处理前后对比帧")
    ap.add_argument("--indir", default="videos", help="批处理输入目录（默认 videos）")
    ap.add_argument("--outdir", default="videos_clean", help="批处理输出目录（默认 videos_clean）")
    args = ap.parse_args()

    FFMPEG = find_tool("ffmpeg")

    jobs = []
    if args.input:
        out = args.output or re.sub(r"\.mp4$", "", args.input) + "_无水印.mp4"
        jobs = [(args.input, out)]
    else:
        os.makedirs(args.outdir, exist_ok=True)
        for f in sorted(os.listdir(args.indir)):
            if f.lower().endswith(".mp4"):
                jobs.append((os.path.join(args.indir, f),
                             os.path.join(args.outdir, f)))

    print(f"共 {len(jobs)} 个待处理任务\n")
    report = []
    for i, (src, dst) in enumerate(jobs, 1):
        name = os.path.basename(src)
        print(f"[{i}/{len(jobs)}] {name}")
        box, how = resolve_box(src, args.box, not args.no_detect)
        info = probe_video(src)
        print(f"    分辨率 {info['width']}x{info['height']} | 水印框 {box} ({how})")
        if args.preview:
            p = export_preview(src, box, f"preview_{i:02d}", args.margin)
            print(f"    对比帧: {p}")
            report.append((name, box, how, "preview"))
            continue
        try:
            n = process_video(src, dst, box, crf=args.crf, margin=args.margin)
            sz = os.path.getsize(dst) / 1024
            print(f"    完成: {n} 帧, 输出 {dst} ({sz:.0f} KB)")
            report.append((name, box, how, f"{n}帧 OK"))
        except Exception as e:
            print(f"    [失败] {e}")
            report.append((name, box, how, f"FAIL {e}"))

    print("\n===== 处理汇总 =====")
    for name, box, how, st in report:
        print(f"  {st:<12} box={box} ({how})  {name[:40]}")


if __name__ == "__main__":
    main()
