#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包（Doubao）分享页无水印视频下载器
====================================

适用对象：豆包 AI 生成的视频分享链接，例如
    https://www.doubao.com/thread/x7Eed3YbQ4Xeo3K9B

工作原理（已逆向验证）：
  1. 分享页是 Next.js 风格的 SSR 页面，会话数据以 HTML 转义的 JSON 内嵌在
     <script data-fn-name="mergeLoaderData" data-fn-args="..."> 中。
     这一步【不需要登录】，可直接拿到每条视频的 vid 与原始提示词。
  2. 真正的视频播放直链由接口
        POST https://www.doubao.com/samantha/video/get_play_info
     返回，入参 { "vid": "..." }。该接口【必须登录态】（session cookie）。
     返回的 play_url 指向火山引擎 VOD 上的原始生成视频（即无水印版本，
     页面里看到的“水印”只存在于聊天内的预览缩略图模板上）。
  3. 下载 play_url 指向的 mp4 即可。

因此本工具分两段：
  A. 解析（免登录）：提取全部视频的 序号 / 提示词 / vid。
  B. 下载（需登录）：用你的豆包 Cookie 调 get_play_info 拿直链并落盘。

零依赖：仅使用 Python 标准库（urllib / json / re / argparse ...），
用户机器无需 pip install 即可运行。

用法：
  # 仅解析并列出视频（不需要 Cookie）
  python doubao_video_downloader.py --url "https://www.doubao.com/thread/xxxx" --parse-only

  # 解析 + 下载视频（需要 Cookie，见下方“如何获取 Cookie”）
  python doubao_video_downloader.py --url "https://www.doubao.com/thread/xxxx" --cookies cookies.txt --out ./videos

  # 也可以直接解析本地保存的 HTML
  python doubao_video_downloader.py --html thread.html --cookies cookies.txt

如何获取 Cookie（只需做一次）：
  1. 用 Chrome / Edge 登录 https://www.doubao.com
  2. 打开任一豆包分享页，按 F12 → Network（网络）标签
  3. 刷新页面，点任意一个 www.doubao.com 的请求
  4. 在 Request Headers（请求头）里找到 “Cookie:” 那一行，复制整行值
  5. 把复制的内容原样粘贴进一个文本文件 cookies.txt 保存即可
     （也可以使用“Cookie-Editor”等插件导出 Netscape 格式，本工具同样兼容）

注意：Cookie 含登录凭证，请仅保存在本机、勿外传。
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DOUBAO_HOST = "https://www.doubao.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# 1. 解析分享页，提取视频元数据（免登录）
# ---------------------------------------------------------------------------
def extract_merge_loader_args(page_html: str):
    """从 HTML 中找到 mergeLoaderData 的 data-fn-args 并解码为 JSON。"""
    patterns = [
        r'data-fn-name="mergeLoaderData"[^>]*data-fn-args="([^"]*)"',
        r'data-fn-args="([^"]*)"[^>]*data-fn-name="mergeLoaderData"',
    ]
    for pat in patterns:
        m = re.search(pat, page_html, re.S)
        if m:
            raw = html.unescape(m.group(1))
            return raw
    return None


def parse_share_page(page_html: str):
    """返回 (share_id, [video_item, ...])。

    video_item = {
        "index": int,            # 第几个视频（从 1 开始）
        "prompt": str,           # 该视频对应的原始提示词
        "vid": str,              # 火山引擎 VOD 视频 id
        "cover": str|None,       # 封面（公开签名图，可能需会话才能下）
    }
    """
    raw = extract_merge_loader_args(page_html)
    if not raw:
        raise RuntimeError("未在页面中找到 mergeLoaderData 数据（可能不是豆包分享页，或页面结构已变化）")

    args = json.loads(raw)                      # [ "thread_(token)/page", [ {...} ] ]
    page_item = args[1][0] if (isinstance(args, list) and len(args) > 1) else {}
    # routerDataFnArgs[0] 是 share_info 的 JSON 字符串
    si_json = page_item.get("routerDataFnArgs", [None])[0]
    si = json.loads(si_json) if isinstance(si_json, str) else si_json

    share_id = si.get("data", {}).get("share_info", {}).get("share_id", "")
    snapshot = si.get("data", {}).get("message_snapshot") or {}
    msg_list = snapshot.get("message_list", [])

    videos = []
    # 消息交替出现：用户(20, 含提示词) -> 助手(2021, 含 vid)
    pending_prompt = ""
    idx = 0
    for msg in msg_list:
        ct = msg.get("content_type")
        content = msg.get("content") or ""
        try:
            cj = json.loads(content) if content else {}
        except Exception:
            cj = {}

        if ct == 20:
            # 用户消息：记录提示词（entity 之外的 text 字段）
            pending_prompt = (cj.get("text") or "").strip()
        elif ct == 2021:
            # 助手消息：视频
            vid = cj.get("vid")
            cover = None
            cov = cj.get("cover") or {}
            for ck in ("image_thumb", "preview_img"):
                cu = (cov.get(ck) or {}).get("url")
                if cu:
                    cover = cu
                    break
            if not vid:
                continue
            idx += 1
            videos.append({
                "index": idx,
                "prompt": pending_prompt,
                "vid": vid,
                "cover": cover,
            })
            pending_prompt = ""
    return share_id, videos


# ---------------------------------------------------------------------------
# 2. 调 get_play_info 拿播放直链（需登录 Cookie）
# ---------------------------------------------------------------------------
def load_cookie_header(cookies_path: str) -> str:
    """读取 cookies 文件：支持 Netscape 格式 或 直接粘贴的 Cookie 请求头值。"""
    with open(cookies_path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    # Netscape 格式以 "# Netscape HTTP Cookie File" 开头，且行内含 Tab
    if text.startswith("# Netscape") or ("\t" in text and "domain" not in text.splitlines()[0].lower() if text else False):
        cookies = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append(f"{parts[5]}={parts[6]}")
        return "; ".join(cookies)
    # 否则当作整段 Cookie 头值
    # 去掉可能的 "Cookie: " 前缀
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    return text


def call_get_play_info(vid: str, cookie_header: str, share_id: str = ""):
    """返回接口 JSON（dict）。"""
    url = DOUBAO_HOST + "/samantha/video/get_play_info"
    payload = {"vid": vid}
    if share_id:
        payload["share_id"] = share_id
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", "application/json")
    req.add_header("Referer", DOUBAO_HOST + "/thread/" + (share_id or ""))
    req.add_header("Origin", DOUBAO_HOST)
    if cookie_header:
        req.add_header("Cookie", cookie_header)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"get_play_info HTTP {e.code}: {body[:300]}")


def extract_video_url(resp: dict):
    """从接口返回里挑出视频直链（优先含 play/video 的 mp4 链接）。"""
    candidates = []

    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if isinstance(v, str) and v.startswith("http"):
                    candidates.append((kl, v))
                walk(v, path + "/" + str(k))
        elif isinstance(o, list):
            for x in o:
                walk(x, path)

    walk(resp)
    # 优先：键名含 play / video / url 且地址像视频
    priority = []
    for kl, v in candidates:
        if any(t in kl for t in ("play", "video", "main")) and re.search(r"\.mp4|\.mov|\.webm|vod|byteimg|volc|tos-", v, re.I):
            priority.append(v)
        elif re.search(r"\.mp4|\.mov|\.webm", v, re.I):
            priority.append(v)
    if priority:
        return priority[0]
    # 退而求其次：任何像视频的地址
    for kl, v in candidates:
        if re.search(r"\.mp4|\.mov|\.webm", v, re.I):
            return v
    return None


def extract_poster(resp: dict):
    """尝试拿封面图地址。"""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() in ("cover_url", "poster_url", "poster", "cover") and isinstance(v, str) and v.startswith("http"):
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(resp)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# 3. 下载
# ---------------------------------------------------------------------------
def download_file(url: str, dest: str, cookie_header: str = "", max_retry: int = 3):
    for attempt in range(1, max_retry + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", UA)
            req.add_header("Referer", DOUBAO_HOST + "/")
            if cookie_header:
                req.add_header("Cookie", cookie_header)
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                downloaded = 0
                chunk = 1024 * 256
                with open(dest, "wb") as out:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        out.write(buf)
                        downloaded += len(buf)
                return dest
        except Exception as e:
            if attempt == max_retry:
                raise
            time.sleep(2 * attempt)


def sanitize_filename(name: str, maxlen: int = 40) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:maxlen] or "video"


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="豆包分享页无水印视频下载器（零依赖）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="豆包分享页链接")
    src.add_argument("--html", help="本地保存的分享页 HTML 文件")
    ap.add_argument("--cookies", help="Cookie 文件路径（下载视频必需，解析可省略）")
    ap.add_argument("--out", default=".", help="视频保存目录，默认当前目录")
    ap.add_argument("--parse-only", action="store_true", help="仅解析并列出视频，不下载")
    ap.add_argument("--limit", type=int, default=0, help="只下载前 N 个（调试用）")
    args = ap.parse_args()

    # 取页面 HTML
    if args.html:
        with open(args.html, "r", encoding="utf-8", errors="replace") as f:
            page_html = f.read()
    else:
        req = urllib.request.Request(args.url, method="GET")
        req.add_header("User-Agent", UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            page_html = resp.read().decode("utf-8", "ignore")

    share_id, videos = parse_share_page(page_html)
    print(f"[解析] share_id = {share_id}")
    print(f"[解析] 共发现 {len(videos)} 个视频\n")
    for v in videos:
        p = v["prompt"].replace("\n", " ")
        print(f"  #{v['index']:02d}  vid={v['vid']}")
        print(f"        提示词: {p[:80]}{'...' if len(p) > 80 else ''}")

    # 保存元数据
    os.makedirs(args.out, exist_ok=True)
    meta_path = os.path.join(args.out, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "share_id": share_id,
            "source": args.url or args.html,
            "count": len(videos),
            "videos": videos,
        }, f, ensure_ascii=False, indent=2)

    # 提示词文本
    prompt_path = os.path.join(args.out, "prompts.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        for v in videos:
            f.write(f"#{v['index']:02d}  {v['prompt']}\n")
            f.write(f"vid: {v['vid']}\n\n")

    print(f"\n[输出] 已保存元数据 -> {meta_path}")
    print(f"[输出] 已保存提示词 -> {prompt_path}")

    if args.parse_only:
        print("\n[提示] 当前为仅解析模式。要下载视频，请加上 --cookies cookies.txt（见脚本顶部说明）。")
        return

    if not args.cookies:
        print("\n[错误] 下载视频需要提供 Cookie（--cookies）。解析已完成，视频需登录态才能拿到直链。")
        sys.exit(2)

    cookie_header = load_cookie_header(args.cookies)
    todo = videos[:args.limit] if args.limit else videos
    ok = 0
    for v in todo:
        print(f"\n[#{v['index']:02d}] 获取播放直链 vid={v['vid']} ...")
        try:
            resp = call_get_play_info(v["vid"], cookie_header, share_id)
        except RuntimeError as e:
            print(f"    失败: {e}")
            continue
        if resp.get("code") not in (0, None, 200) and "data" not in resp:
            print(f"    接口返回错误: {json.dumps(resp, ensure_ascii=False)[:300]}")
            continue
        video_url = extract_video_url(resp)
        if not video_url:
            print("    未能从返回中提取视频直链，原始返回：")
            print("    " + json.dumps(resp, ensure_ascii=False)[:400])
            continue
        fname = f"{v['index']:02d}_{sanitize_filename(v['prompt'])}.mp4"
        dest = os.path.join(args.out, fname)
        print(f"    直链: {video_url[:120]}")
        try:
            download_file(video_url, dest, cookie_header)
            size = os.path.getsize(dest)
            print(f"    已下载 -> {dest}  ({size/1024/1024:.2f} MB)")
            ok += 1
        except Exception as e:
            print(f"    下载失败: {e}")
    print(f"\n[完成] 成功下载 {ok}/{len(todo)} 个视频到 {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
