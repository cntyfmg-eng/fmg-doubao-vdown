# -*- coding: utf-8 -*-
"""
doubao_vdown_app.py — 豆包视频无水印下载器（exe 交互入口）

流程：输入分享链接 -> 读取/粘贴 Cookie -> 下载原始视频 -> 自动去水印(Telea修复)
      -> 自动验证 -> 输出 videos_nowm/

技术依据（逆向实测）：
  * 分享页 SSR 数据含全部视频 vid（免登录）
  * POST /samantha/video/get_play_info {vid} 需登录 Cookie，返回 play_url（720p 源）
  * 豆包在服务端生成时已把「AI生成」水印烧录进视频本体（play_url 全部变体 md5 相同，
    官方无水印接口不适用视频），故采用 OpenCV Telea 全帧修复去除左上角水印，
    修复前自动实测水印框（时间恒定性检测），失败回退宽度等比公式，
    修复后自动验证（残留像素<=40 判合格）。
"""
import argparse
import glob
import html as html_mod
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

import cv2
import numpy as np

APP_NAME = "豆包视频无水印下载器"
APP_VER = "1.0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


# ---------- 输出安全（GBK 终端兼容） ----------
def safe_print(s=""):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("gbk", "replace").decode("gbk"))


def find_ffmpeg():
    cands = []
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    exedir = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    for base in (exedir, here, os.getcwd()):
        cands += [os.path.join(base, "ffmpeg.exe"), os.path.join(base, "ffmpeg")]
    env = os.environ.get("FFMPEG_BIN") or os.environ.get("FFMPEG_DIR")
    if env:
        cands.append(env if os.path.isfile(env) else os.path.join(env, "ffmpeg.exe"))
    cands += ["ffmpeg", r"H:\WorkBuddy\tools\ffmpeg-bin\ffmpeg.exe"]
    for c in cands:
        try:
            r = subprocess.run([c, "-version"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


FF = None  # 启动时解析


# ---------- 网络 ----------
def http_json(url, body, cookie):
    req = urllib.request.Request(url, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Referer", "https://www.doubao.com/")
    req.add_header("Origin", "https://www.doubao.com")
    req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, data=json.dumps(body).encode(), timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_download(url, referer="https://www.doubao.com/"):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def parse_cookie_text(x):
    if os.path.exists(x):
        text = open(x, encoding="utf-8", errors="replace").read()
    else:
        text = x
    if text.startswith("# Netscape") or "\t" in text:
        pairs = []
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            p = line.split("\t")
            if len(p) >= 7:
                pairs.append(f"{p[5]}={p[6]}")
        return "; ".join(pairs)
    return text.strip().rstrip(";")


# ---------- 解析 vid + 提示词 ----------
def extract_from_html(page):
    items, last_prompt = [], ""
    si = None
    for m in re.finditer(r'data-fn-name="mergeLoaderData"[^>]*data-fn-args="([^"]*)"', page):
        try:
            argsj = json.loads(html_mod.unescape(m.group(1)))
            si = json.loads(argsj[1][0]["routerDataFnArgs"][0])
            break
        except Exception:
            continue
    if si:
        for msg in si["data"]["message_snapshot"]["message_list"]:
            try:
                c = json.loads(msg.get("content") or "{}")
            except Exception:
                continue
            if msg.get("content_type") == 20 and c.get("text"):
                last_prompt = c["text"]
            elif msg.get("content_type") == 2021 and c.get("vid"):
                items.append((c["vid"], last_prompt))
    return items


def load_items(share_url):
    page = http_download(share_url).decode("utf-8", "replace")
    items = extract_from_html(page)
    return list(dict.fromkeys(items)) if items else []


# ---------- 下载 ----------
def get_play_url(vid, cookie):
    resp = http_json("https://www.doubao.com/samantha/video/get_play_info", {"vid": vid}, cookie)
    if resp.get("code") != 0:
        raise RuntimeError(f"接口错误 {resp.get('code')}: {resp.get('msg')}")
    for pi in resp.get("data", {}).get("play_infos") or []:
        u = pi.get("main") or pi.get("backup")
        if u:
            return u
    raise RuntimeError("无可用播放地址")


# ---------- 水印 ----------
def wm_box_for_width(W):
    return int(0.0361 * W), int(0.0278 * W), int(0.1097 * W), int(0.0542 * W)


def detect_box_from_video(src, n_samples=10):
    cap = cv2.VideoCapture(src)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, min(180, total) // n_samples)
    frames, i = [], 0
    while len(frames) < n_samples:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, f = cap.read()
        if not ok:
            break
        frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
        i += step
    cap.release()
    if len(frames) < 5:
        return None
    stack = np.stack(frames, 0).astype(np.float32)
    med = np.median(stack, 0).astype(np.uint8)
    std = stack.std(0)
    blur = cv2.GaussianBlur(med.astype(np.float32), (15, 15), 0)
    score = (med.astype(np.float32) - blur) * (std < 6.0)
    sw, sh = int(0.30 * W), int(0.22 * H)
    region = score[:sh, :sw]
    px0, py0, px1, py1 = wm_box_for_width(W)
    pad = int(0.025 * W)
    bx0, by0 = max(0, px0 - pad), max(0, py0 - pad)
    bx1, by1 = min(sw - 1, px1 + pad), min(sh - 1, py1 + pad)
    inner = region[by0:by1, bx0:bx1]
    if inner.size and (inner > 15).sum() >= 30:
        ys, xs = np.where(inner > 15)
        return (bx0 + int(xs.min()), by0 + int(ys.min()),
                bx0 + int(xs.max()), by0 + int(ys.max()))
    _, binm = cv2.threshold(region.astype(np.uint8), 12, 255, cv2.THRESH_BINARY)
    binm = cv2.morphologyEx(binm, cv2.MORPH_CLOSE, np.ones((3, 9), np.uint8))
    cnts, _ = cv2.findContours(binm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if 0.04 * W <= w <= 0.16 * W and 0.015 * H <= h <= 0.05 * H:
            if best is None or w * h > (best[2] - best[0]) * (best[3] - best[1]):
                best = (x, y, x + w, y + h)
    return best


def remove_watermark(src, dst, crf=18):
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    cap.release()
    det = detect_box_from_video(src)
    x0, y0, x1, y1 = det if det else wm_box_for_width(W)
    box_src = "auto" if det else "ratio"
    margin, pad_out = 4, 3
    ex0, ey0 = max(0, x0 - margin), max(0, y0 - margin)
    ex1, ey1 = min(W - 1, x1 + margin), min(H - 1, y1 + margin)
    mask = np.zeros((H, W), np.uint8)
    mask[max(0, ey0 - pad_out):min(H, ey1 + pad_out + 1),
         max(0, ex0 - pad_out):min(W, ex1 + pad_out + 1)] = 255
    k = max(3, (pad_out * 2 + 1) | 1)
    mask = cv2.GaussianBlur(mask, (k, k), 0)
    alpha3 = (mask.astype(np.float32) / 255.0)[..., None]

    cap = cv2.VideoCapture(src)
    enc = subprocess.Popen(
        [FF, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
         "-r", f"{fps:.6f}", "-i", "-",
         "-i", src, "-map", "0:v:0", "-map", "1:a?",
         "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "copy", dst],
        stdin=subprocess.PIPE)
    n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        fixed = cv2.inpaint(frame, mask, 4, cv2.INPAINT_TELEA)
        frame[:] = (fixed.astype(np.float32) * alpha3 + frame.astype(np.float32) * (1 - alpha3)).astype(np.uint8)
        enc.stdin.write(frame.tobytes())
        n += 1
        if n % 60 == 0:
            safe_print(f"      ...已处理 {n} 帧")
    cap.release()
    enc.stdin.close()
    enc.wait()
    if enc.returncode != 0:
        raise RuntimeError(f"编码失败 exit={enc.returncode}")
    return n, (x0, y0, x1, y1), box_src


def wm_residual(mp4):
    cap = cv2.VideoCapture(mp4)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    step = max(1, int(fps * 0.5))
    frames, i = [], 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
        i += 1
    cap.release()
    if len(frames) < 3:
        return -1
    stack = np.stack(frames, 0).astype(np.float32)
    med = np.median(stack, 0).astype(np.uint8)
    std = stack.std(0)
    blur = cv2.GaussianBlur(med.astype(np.float32), (15, 15), 0)
    score = (med.astype(np.float32) - blur) * (std < 6.0)
    x0, y0, x1, y1 = int(0.034 * W), int(0.026 * W), int(0.112 * W), int(0.060 * W)
    return int((score[y0:y1, x0:x1] > 15).sum())


def safe_name(idx, prompt):
    p = re.sub(r'[\\/:*?"<>|\n\r\t]', "", (prompt or "")[:40]).strip() or "video"
    return f"{idx:02d}_{p}.mp4"


# ---------- 主流程 ----------
def run_pipeline(share_url, cookie, outdir, limit=0, keep_cookies=False, cookie_file=None):
    items = load_items(share_url)
    if not items:
        safe_print("[错误] 未能从分享页解析到视频，请确认链接有效（需含 /thread/ 或 /share/）")
        return 0, 0
    if limit:
        items = items[:limit]
    safe_print(f"解析成功：共 {len(items)} 个视频\n")
    os.makedirs(outdir, exist_ok=True)
    tmpdir = os.path.join(outdir, "_raw_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    okc, failc = 0, 0
    for idx, (vid, prompt) in enumerate(items, 1):
        safe_print(f"[{idx}/{len(items)}] {vid}  提示词: {(prompt or '')[:36]}")
        try:
            purl = get_play_url(vid, cookie)
        except Exception as e:
            safe_print(f"    [FAIL] 获取直链: {e}（Cookie 可能已过期，请重新导出）")
            failc += 1
            continue
        raw = os.path.join(tmpdir, f"raw_{idx:02d}.mp4")
        try:
            data = http_download(purl)
            with open(raw, "wb") as f:
                f.write(data)
            safe_print(f"    下载源 {len(data) // 1024} KB")
        except Exception as e:
            safe_print(f"    [FAIL] 下载: {e}")
            failc += 1
            continue
        out = os.path.join(outdir, safe_name(idx, prompt))
        try:
            n, box, bs = remove_watermark(raw, out)
            res = wm_residual(out)
            tag = "[OK]无水印" if res <= 40 else f"[WARN]残留{res}"
            safe_print(f"    去水印完成 ({n}帧, 框{box}/{bs}) | 验证: {tag}")
            okc += 1
        except Exception as e:
            safe_print(f"    [FAIL] 去水印: {e}")
            failc += 1
        finally:
            if os.path.exists(raw):
                os.remove(raw)
    shutil.rmtree(tmpdir, ignore_errors=True)
    safe_print(f"\n===== 完成：成功 {okc} / 失败 {failc}，输出目录: {outdir} =====")
    if not keep_cookies and cookie_file and os.path.exists(cookie_file):
        os.remove(cookie_file)
        safe_print("已删除 cookies.txt（登录凭证不落盘）")
    return okc, failc


def interactive():
    safe_print("=" * 46)
    safe_print(f"  {APP_NAME} v{APP_VER}")
    safe_print("  下载豆包分享页视频 + 自动去除「AI生成」水印")
    safe_print("  by数智化教学大冯老师@冯萌刚")
    safe_print("=" * 46)
    if FF is None:
        safe_print("[错误] 未找到 ffmpeg。请将 ffmpeg.exe 放到本程序同目录，")
        safe_print("       或设置环境变量 FFMPEG_BIN 指向 ffmpeg.exe 完整路径。")
        return
    safe_print(f"[ffmpeg] {FF}\n")

    url = input("第1步：粘贴豆包分享链接 (https://www.doubao.com/thread/xxx)：").strip()
    if not url:
        safe_print("[错误] 链接不能为空")
        return
    if "doubao.com" not in url:
        safe_print("[提示] 链接里没有 doubao.com，仍会尝试…")

    cookie = ""
    local = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "cookies.txt")
    if os.path.exists(local):
        cookie = parse_cookie_text(local)
        safe_print(f"[Cookie] 已从同目录 cookies.txt 读取（{len(cookie)} 字符）")
    if not cookie:
        safe_print("第2步：请粘贴网页 Cookie（豆包 F12 -> Network -> 任一请求的 Cookie 头整行）")
        safe_print("       或把 Cookie 存到本程序同目录 cookies.txt 后重新运行")
        cookie = input("Cookie: ").strip()
        cookie = parse_cookie_text(cookie) if cookie else ""
    if not cookie:
        safe_print("[错误] 没有 Cookie 无法下载（接口需登录态）")
        return
    if "sessionid" not in cookie:
        safe_print("[WARN] Cookie 里未见 sessionid 字段，可能未登录，仍会尝试…")

    outdir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "videos_nowm")
    safe_print(f"[输出] {outdir}\n")
    okc, failc = run_pipeline(url, cookie, outdir, keep_cookies=False,
                              cookie_file=local if os.path.exists(local) else None)
    if okc:
        try:
            os.startfile(outdir)
        except Exception:
            pass


def main():
    if len(sys.argv) <= 1:
        interactive()
        safe_print("\n按回车键退出…")
        try:
            input()
        except EOFError:
            pass
        return
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VER}")
    ap.add_argument("--url", required=True)
    ap.add_argument("--cookies", required=True, help="cookies.txt 路径或 Cookie 文本")
    ap.add_argument("--out", default="videos_nowm")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keep-cookies", action="store_true")
    a = ap.parse_args()
    cookie = parse_cookie_text(a.cookies)
    run_pipeline(a.url, cookie, a.out, a.limit, a.keep_cookies,
                 a.cookies if os.path.exists(a.cookies) else None)


if __name__ == "__main__":
    FF = find_ffmpeg()
    main()
