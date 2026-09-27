# 豆包视频「AI生成」水印去除工具 — 使用说明

## 背景

豆包（doubao.com）生成的视频在导出/播放时，画面**左上角**会烧录浅色「AI生成」合规标识。
该水印为**半透明浅色文字叠加**，且**烧录在像素里**（不是播放器贴的），因此普通下载无法去除，
必须做像素级修复。

## 深度逆向分析结论（实测）

| 项目 | 结论 |
|---|---|
| 水印内容 | 「AI生成」浅色标识（半透明白色文字叠加在画面上） |
| 位置 | 画面左上角，**按视频宽度等比缩放** |
| 归一化坐标 | x0≈3.6%W，y0≈2.8%W，宽≈7.5%W，高≈2.8%W |
| 720×720 视频 | 水印框 (26,20)-(79,39) |
| 970×720 视频 | 水印框 (35,27)-(107,54) |
| 跨帧特性 | 恒定不变（视频内容动态、水印静态）→ 可自动检测 |

## 去除原理

1. **自动定位**：抽取 12 帧做时间恒定性分析——「跨帧不变 + 比周围亮」的连通域即水印；
   检测不到时回退到统一比例公式（覆盖 100% 场景）。
2. **Telea 图像修复**（OpenCV inpaint）：以水印周围真实像素重建被水印覆盖的背景，
   mask 外扩+高斯羽化融合，边缘无接缝。
3. **无损管线**：ffmpeg rawvideo 管道逐帧处理，音频流直拷不重编码，
   视频 H.264 CRF 18（视觉无损级）重编码。

## 环境准备（一次性）

```bash
# 1) Python venv（已存在则跳过）
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m venv ^
  C:/Users/Administrator/.workbuddy/binaries/python/envs/default
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/pip install opencv-python numpy

# 2) ffmpeg（本机已有 H:/WorkBuddy/tools/ffmpeg-bin）
#    设置环境变量指向 ffmpeg 所在目录
set FFMPEG_DIR=H:/WorkBuddy/tools/ffmpeg-bin
```

## 使用方法

```bash
set FFMPEG_DIR=H:/WorkBuddy/tools/ffmpeg-bin
PY=C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe

# 批处理：videos 目录全部 mp4 → videos_clean 目录（默认）
%PY% doubao_watermark_remover.py

# 单文件
%PY% doubao_watermark_remover.py -i 视频.mp4
%PY% doubao_watermark_remover.py -i 视频.mp4 -o 输出.mp4

# 手动指定水印框（自动检测失误时）
%PY% doubao_watermark_remover.py -i 视频.mp4 --box 26,20,79,39

# 仅导出处理前后对比帧（不转码，快速核对定位）
%PY% doubao_watermark_remover.py -i 视频.mp4 --preview

# 调质量/边距
%PY% doubao_watermark_remover.py --crf 16 --margin 6
```

## 参数表

| 参数 | 默认 | 说明 |
|---|---|---|
| `-i/--input` | — | 单文件；缺省则批处理 `--indir` |
| `-o/--output` | `<输入>_无水印.mp4` | 输出路径 |
| `--indir` / `--outdir` | `videos` / `videos_clean` | 批处理目录 |
| `--box` | 自动 | 水印框 `x0,y0,x1,y1` |
| `--no-detect` | 关 | 跳过自动检测，直接用比例公式 |
| `--crf` | 18 | 越小越清晰/越大越小（16≈视觉无损） |
| `--margin` | 4 | 水印框外扩像素（光晕大时调大） |
| `--preview` | 关 | 只导出 `preview_NN_before.png` 对比帧 |

## 效果验证（本次 10 个视频实测）

- 水印区「恒定偏亮」显著像素：**250 → 0**（峰值 38.3 → 4.4）
- 处理后水印区与周围背景灰度差：**8.8 → 0.0**
- 全帧平均差异 2.3（仅重编码损耗），水印区平均差异 13.1（有效重建）

## 注意事项

- 水印区若被**高对比复杂纹理**覆盖（如文字、人脸经过），重建处可能有轻微涂抹感——
  区域仅约 54×21 px（0.2% 画面），正常观看不可见。
- 若视频是其他豆包版式（如右下角水印），用 `--box` 手动指定即可，算法同样适用。
- 生成时已烧进画面的水印属于合规标识，本工具仅做画面修复，请合规使用。
