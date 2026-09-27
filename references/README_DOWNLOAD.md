# 豆包分享页无水印视频下载工具

专门下载豆包（Doubao）AI 生成视频分享页里的视频，保存为无水印原片。

## 一、逆向分析结论（已实测验证）

目标链接：`https://www.doubao.com/thread/x7Eed3YbQ4Xeo3K9B`

1. **页面结构**：豆包分享页是 SSR（服务端渲染）页面，会话数据以 HTML 转义的 JSON 内嵌在
   `<script data-fn-name="mergeLoaderData" data-fn-args="...">` 中，包含 `share_info` 与
   `message_snapshot`。这一步**不需要登录**，可直接拿到每条视频的 `vid` 与原始提示词。
2. **视频标识**：助手消息 `content_type=2021`，内容里含 `vid`（火山引擎 VOD 文件 ID，如
   `v02b5eg10004d2vcok7og65mfohone90`）和封面（封面图地址带 `-watermark` 模板，是聊天内预览缩略图的水印，**不是视频本身**）。
3. **播放直链接口**：
   `POST https://www.doubao.com/samantha/video/get_play_info` ，入参 `{"vid":"..."}`。
   该接口**强制要求登录态**（session cookie），返回 `play_url` 指向火山引擎 VOD 上的原始生成视频
   ——即**无水印版本**（页面里看到的“水印”只存在于聊天内预览缩略图模板上）。
4. **免登录尝试均失败**：只用设备 `web_id`/`device_id` cookie 调用接口仍返回 `登录已过期`；
   分享快照接口 `thread/share/snapshot/get` 返回 `record not found`；浏览器自动化在沙箱内无法启动
   Chromium。因此**下载视频文件必须用户提供登录 Cookie**。

结论：解析（拿 vid + 提示词）可零登录完成；下载原片需登录 Cookie，二者通过本工具分步实现。

## 二、环境要求

- Python 3（无需安装任何第三方库，纯标准库）。

## 三、使用步骤

### 第 1 步：仅解析（可选，不需要 Cookie，用于确认能拿到哪些视频）

```bash
python doubao_video_downloader.py --url "https://www.doubao.com/thread/xxxx" --parse-only
# 或直接解析本地 HTML
python doubao_video_downloader.py --html doubao_thread.html --parse-only
```

会输出全部视频的序号 / vid / 提示词，并生成 `metadata.json`、`prompts.txt`。

### 第 2 步：获取豆包 Cookie（只需做一次）

1. 用 Chrome / Edge 登录 https://www.doubao.com
2. 打开任意一个豆包分享页，按 `F12` → **Network（网络）** 标签
3. 刷新页面，点列表中任意一个 `www.doubao.com` 的请求
4. 在 **Request Headers（请求头）** 里找到 `Cookie:` 那一行，复制整行的值
5. 把复制内容原样粘贴进一个文本文件，保存为 `cookies.txt`
   （也可以用 “Cookie-Editor” 等插件导出 Netscape 格式，本工具同样兼容）

> 安全提示：Cookie 等同于你的登录凭证，请仅保存在本机，不要外传或提交到公开仓库。

### 第 3 步：解析 + 下载视频（需 Cookie）

```bash
python doubao_video_downloader.py \
  --url "https://www.doubao.com/thread/xxxx" \
  --cookies cookies.txt \
  --out ./videos
```

视频会以 `01_提示词前40字.mp4` 这样的命名保存到 `--out` 目录（默认当前目录）。

## 四、参数说明

| 参数 | 说明 |
|------|------|
| `--url` | 豆包分享页链接（与 `--html` 二选一，必填） |
| `--html` | 本地保存的分享页 HTML 文件（免联网解析） |
| `--cookies` | Cookie 文件路径（下载视频必需；解析可省略） |
| `--out` | 视频保存目录，默认当前目录 |
| `--parse-only` | 仅解析列出视频、不下载 |
| `--limit N` | 只下载前 N 个（调试用） |

## 五、产物说明

- `doubao_video_downloader.py`：下载器主程序（零依赖）
- `metadata.json`：解析出的视频元数据（序号 / 提示词 / vid / 封面）
- `prompts.txt`：每个视频的原始提示词文本
- `doubao_thread.html`：抓取的分享页原始 HTML（分析用）
- `share_info.json` / `message_snapshot_raw.json`：内嵌会话数据（分析用）

## 六、关于“水印”

下载到的是豆包生成的**原始视频文件**，不含聊天界面里预览缩略图上的水印。
若豆包在生成时已将 logo 烧录进视频画面，则属生成时自带，非本工具可去除。
