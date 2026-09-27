---
name: fmg-doubao-vdown
description: 豆包（doubao.com）分享页视频无水印下载器。输入分享链接 + 登录 Cookie，自动解析全部视频、下载 720p 源、OpenCV Telea 修复去除左上角「AI生成」烧录水印并自动验证残留。提供交互式 exe 打包入口脚本与 CLI 批量脚本，支持 ffmpeg 自动查找、GBK 终端兼容。触发词：豆包视频下载、豆包无水印、doubao video、去AI生成水印。
---

# 豆包视频无水印下载（fmg-doubao-vdown）

一键完成：**豆包分享链接 → 解析视频列表 → 下载 720p 源 → 自动定位并去除「AI生成」烧录水印 → 残留验证 → 输出 videos_nowm/**。

## 逆向结论（为什么必须"下载+修复"两步）

| 问题 | 实测结论 |
|---|---|
| 无水印源是否存在 | **不存在**。play_url 4 种 URL 变体 md5 完全相同，水印在豆包服务端生成时已烧录进 VOD 文件本体 |
| 官方去水印接口 | `/creativity/resource/get_without_watermark` 仅适用图片创意资源，视频报"系统错误" |
| 视频数据来源 | 分享页 SSR `<script data-fn-name="mergeLoaderData">`（免登录可解析 vid+提示词） |
| 播放地址 API | `POST /samantha/video/get_play_info {"vid":...}`，**需登录 Cookie** |
| 水印位置 | 左上角浅色半透明「AI生成」，按宽度等比缩放：720×720→框(26,20,79,39)；970×720→框(35,27,107,54) |

因此唯一可行方案 = 登录态下载 + OpenCV Telea 全帧像素修复。

## 快速开始

### 方式一：交互式（推荐，exe 打包入口）

```bash
pip install opencv-python numpy   # 仅此两个第三方依赖
python scripts/doubao_vdown_app.py
```

无参数运行进入交互模式：粘贴分享链接 → 选择 cookies.txt 或直接粘贴 Cookie → 自动跑完全流程并打开输出目录。

CLI 模式：

```bash
python scripts/doubao_vdown_app.py --url "https://www.doubao.com/thread/xxxx" --cookies cookies.txt --out videos_nowm
```

### 方式二：一键 CLI 脚本

```bash
python scripts/doubao_auto_nowm.py --url "https://www.doubao.com/thread/xxxx" --cookies cookies.txt --out videos_nowm --limit 0
```

### 方式三：仅去水印（已有本地视频）

```bash
python scripts/doubao_watermark_remover.py --indir videos --outdir videos_clean
python scripts/doubao_watermark_remover.py -i 视频.mp4 --box 26,20,79,39   # 自动检测失误时手动指定
```

## 取 Cookie 步骤

1. 浏览器登录 www.doubao.com，F12 → Network → 刷新分享页
2. 任选一个 `doubao.com` 请求 → Request Headers → 复制整行 `Cookie:`
3. 存为 `cookies.txt`（纯文本一行即可），或运行脚本时直接粘贴

## 依赖与环境

- Python 3.10+；`pip install opencv-python numpy`（ffmpeg 命令行工具另需存在）
- ffmpeg 查找顺序：exe 同目录 → `_MEIPASS` → cwd → `FFMPEG_BIN`/`FFMPEG_DIR` 环境变量 → PATH
- Windows GBK 终端已做 `safe_print` 兼容，中文输出不炸

## 打包 exe（可选）

```bash
pip install pyinstaller
pyinstaller --onefile --name doubao_vdown --console doubao_vdown_app.py
# dist/doubao_vdown.exe 与 ffmpeg.exe 放同一目录即可分发
```

## 去水印原理（三层保障）

1. **自动实测框**：抽 10 帧做时间恒定性检测（跨帧 std<6 且比高斯模糊背景亮的像素），在统一公式预测框附近搜索显著像素，命中即返回实测 bbox
2. **回退公式框**：检测失败按归一化比例 x0=3.6%W, y0=2.8%W, w=7.5%W, h=2.8%W
3. **Telea 全帧修复**：`cv2.inpaint(frame, mask, 4, INPAINT_TELEA)` + 高斯羽化融合，ffmpeg rawvideo 管道 + libx264 CRF18，音频直拷
4. **自动验证**：处理后残留显著像素 ≤40 判合格（本批 10 个视频实测残留 12~31）

## 脚本清单

| 文件 | 用途 |
|---|---|
| `scripts/doubao_vdown_app.py` | 交互式全流程入口（exe 打包用），CLI 双模式 |
| `scripts/doubao_auto_nowm.py` | 一键 CLI：下载+实测框去水印+验证 |
| `scripts/doubao_watermark_remover.py` | 独立去水印工具（单文件/批处理/手动框/preview） |
| `scripts/doubao_video_downloader.py` | 纯标准库下载器（下载原始含水印版，零依赖） |
| `references/README_DOWNLOAD.md` | 下载器完整参数与逆向记录 |
| `references/README_WATERMARK.md` | 去水印工具完整参数与效果数据 |

## 注意事项

- Cookie 属敏感凭据，仅本机使用，勿外传
- 「AI生成」为合规标识，本工具仅做画面修复，请合规使用生成内容
- 分享页需处于有效分享状态；Cookie 过期时 get_play_info 返回未登录错误，需重新提取
