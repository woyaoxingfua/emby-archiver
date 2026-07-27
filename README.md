# emby-archiver

> A Python tool to archive Emby videos offline for Windows, Linux, Termux — with CLI, GUI and Web interfaces.
>
> 把 Emby 资源离线归档到本地的 Python 工具 —— 支持 Windows、Linux、Termux，提供 CLI、GUI 和 Web 三种入口。

---

## ⚠️ Disclaimer / 免责声明

> **仅限个人合法使用（For personal, legitimate use only）。**
>
> **emby-archiver 是一个技术中立的工具。** 与所有通用软件一样，它本身没有立场——**技术无罪，关键在于你怎么用**。
>
> 使用者须自行确认并承担以下责任：
> - 对你下载、归档的内容拥有合法权利（例如：你本人拥有该媒体，或内容位于你运营/获授权使用的服务器上）。
> - 遵守所在国家/地区的法律法规，以及所连接服务器的服务条款（ToS）。
>
> 本项目按 **「现状」(as is)** 提供，不附带任何明示或暗示的担保。作者**不鼓励、不纵容、亦不协助**任何侵犯版权、未经授权的再分发，或违反适用法律与第三方条款的行为。如因不当使用产生的任何后果，均由使用者自行承担。

> **For personal, legitimate use only.**
>
> **emby-archiver is a technologically neutral tool.** Like any general-purpose software, it takes no side — *the technology is innocent; what matters is how you use it*.
>
> You are solely responsible for ensuring and abiding by:
> - Having the legal right to archive the content you download (e.g. media you own, or content hosted on a server you operate or are authorized to use).
> - The laws and regulations of your jurisdiction, as well as the terms of service (ToS) of any server you connect to.
>
> This project is provided **"as is"**, without warranty of any kind. The authors do **not** encourage, condone, or assist copyright infringement, unauthorized redistribution, or any misuse that violates applicable law or third-party terms. Any consequences arising from misuse are solely the user's responsibility.

---

## Description / 项目描述

### English

**emby-archiver** is a lightweight Python downloader that logs into your Emby server and saves the media you can access to local storage for offline viewing. It is **not** a media player, not a streaming client, and not a "watch-while-downloading" buffer — it is purely an offline archiving tool.

Three entry points make it run anywhere:
- **CLI** — for terminal power users and scripting
- **GUI** (Tkinter) — for a familiar desktop experience
- **Web** (Starlette + uvicorn) — for headless boxes, remote access, phone or Termux browsers

Default `auto` mode picks the right one: GUI when a desktop is available, Web when not.

Key design choices:
- Multi-threaded segmented downloading with automatic single-thread fallback when the server doesn't support `Range`
- `.part`-based pause/resume so downloads survive network flake
- SQLite-backed record tracking so completed items are never re-downloaded
- Falls back to the playable stream (the same source the built-in player uses) when no download endpoint is available
- Optional experimental multipart probing for servers with unusual stream setups

### 中文

**emby-archiver** 是一个轻量级 Python 下载器，登录你的 Emby 服务器，把能访问的媒体资源离线保存到本地观看。它**不是**播放器、不是流媒体客户端、也不是"边下边播"的缓冲工具——它纯粹是一个离线归档工具。

三条入口让它随处可用：
- **CLI** — 终端重度玩家和脚本调用
- **GUI**（Tkinter）— 熟悉的桌面软件体验
- **Web**（Starlette + uvicorn）— 无图形环境、远程访问、手机或 Termux 浏览器

默认 `auto` 模式自动选择：有桌面走 GUI，没桌面走 Web。

核心设计：
- 多线程分段下载，服务端不支持 `Range` 时自动回退单线程
- 基于 `.part` 的暂停/续传，网络抖动后还能接着下
- SQLite 记录追踪，已完成项永不重复下载
- 服务端未开放下载入口时，回退到可播放流（与内置播放器同源）
- 可选的实验性分段探测，适配特殊服务端

---

## About emby-archiver / 关于

### English

If you run or are authorized to use an Emby server and want to archive the media you have the right to access — whether on a laptop, a small NAS box, or a phone running Termux — this tool is for you.

It was born from a few real, legitimate needs:

- Your own Emby server has great content but spotty internet; download once, watch anywhere offline
- You want a downloader on a headless VPS/NAS/Termux box with no GUI
- Emby's web player doesn't support every codec; offline archiving is more reliable
- Your server may not expose a download button (e.g. the admin hasn't enabled `DownloadContent`); the tool retrieves the same playable stream the built-in player uses, so you can archive your own library

Most existing Emby clients focus on playback. There was a gap for a **pure offline archiving tool**. emby-archiver fills that gap.

### 中文

如果你运营或获授权使用一个 Emby 服务器，想把有权访问的媒体资源归档保存——无论是笔记本、NAS 小主机、还是跑着 Termux 的手机——这就是为你做的工具。

它来自这些真实且正当的需求：

- 自己的 Emby 服务器资源不错，但网络不稳定，想下载一次随时离线看
- 想在无图形环境的服务器 / NAS / Termux 上也能跑下载工具
- Emby Web 播放器不支持所有编码，离线转存更靠谱
- 服务器可能未开放下载按钮（例如管理员未启用 `DownloadContent`）；工具获取的是内置播放器使用的同一路流，便于你归档自己的媒体库

现有 Emby 客户端大多偏播放，**纯粹的离线归档工具**是空白。emby-archiver 就是来填这个空白的。

---

## 预览

> UI 截图已移除，避免暴露真实服务器地址与媒体内容。后续补充截图时，请使用示例/脱敏数据。

支持多历史日志文件切换、级别过滤、关键词搜索、自动滚动、加载更多。

---

## 当前能力

### 三条入口

| 入口 | 说明 | 启动方式 |
|------|------|----------|
| CLI | 纯终端操作 | `python app.py --mode cli` |
| Tkinter GUI | 桌面图形界面 | `python app.py --mode gui` 或 `python gui.py` |
| Web | 浏览器访问，支持手机/Termux | `python app.py --mode web` |

默认 `auto` 模式：有图形环境走 Tkinter，没有自动回退 Web。

### 核心功能

- 用户名 + 密码登录 Emby
- 兼容 `access_token` 或 `api_key` 方式
- 列出媒体库、搜索电影 / 剧集 / 季 / 集
- 下载单个电影，或展开整季 / 整部剧集逐集下载
- 支持 `.part` 临时文件断点续传
- 用 SQLite 记录缓存状态
- 优先走多线程分段下载，不支持 `Range` 时自动回退单线程
- 可选开启实验性分段探测
- 支持为认证和下载链路配置 HTTP/HTTPS 代理

### Web 特有功能

- 搜索资源、发起下载、查看实时状态（速度/ETA/百分比/进度条）
- 暂停 / 继续 / 取消当前下载（取消时可选删除/保留 .part 临时文件）
- 设置默认下载目录（可持久化到 config.json）
- 调节并发分段数（1-32）
- 下载记录管理（继续未完成、删除已完成/未完成）
- 完整日志系统：多历史文件切换、级别过滤、关键词搜索、行号、自动滚动
- 摘要栏显示代理配置、experimental_force_multipart 状态

---

## 快速开始

### 1. 准备配置

```bash
cp config.example.json config.json
# 编辑 config.json 填入你的 Emby 地址和账号信息
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 启动

```bash
# 默认 auto 模式（有 GUI 走 Tkinter，没有走 Web）
python app.py

# 强制 Web 模式
python app.py --mode web --host 127.0.0.1 --port 8765

# 局域网/手机访问（需配合安全措施）
python app.py --mode web --host 0.0.0.0 --port 8765
```

打开浏览器访问：`http://127.0.0.1:8765`

---

## 配置说明

```json
{
  "server_url": "https://your-emby-server.example.com",
  "username": "your_username",
  "password": "your_password",
  "api_key": "",
  "access_token": "",
  "user_id": "",
  "download_dir": "downloads",
  "database_path": "store/cache.db",
  "device_name": "emby-cache-tool",
  "user_agent": "emby-cache-tool/0.1",
  "timeout": 30,
  "chunk_size": 1048576,
  "segments": 4,
  "experimental_force_multipart": false,
  "proxy": "",
  "proxy_http": "",
  "proxy_https": "",
  "log_dir": "logs",
  "log_level": "INFO",
  "stream_segments": 2
}
```

### 配置项详解

| 配置项 | 说明 |
|--------|------|
| `server_url` | Emby 服务器地址 |
| `username` / `password` | 登录账号（优先级最高） |
| `access_token` | 备选认证方式 |
| `api_key` | 备选认证方式，建议同时填 `user_id` |
| `download_dir` | 默认下载目录，相对路径或绝对路径均可 |
| `database_path` | SQLite 数据库路径 |
| `segments` | 分段并发数（1-32），仅服务端支持 Range 时生效 |
| `experimental_force_multipart` | 是否开启实验性分段探测 |
| `proxy` | 同时作用于 HTTP/HTTPS 的代理 |
| `proxy_http` / `proxy_https` | 分别配置 HTTP/HTTPS 代理 |
| `log_dir` | 日志目录，默认 `logs/` |
| `log_level` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `stream_segments` | Videos/stream 候选允许的最大分段数，默认 2 |

---

## 使用指南

### CLI 用法

```bash
# 登录测试
python app.py --mode cli login-test

# 列出媒体库
python app.py --mode cli libraries

# 搜索资源
python app.py --mode cli search "你的关键字"

# 下载指定 item
python app.py --mode cli download --item-id 123456

# 查看下载记录
python app.py --mode cli downloads

# 继续未完成的下载
python app.py --mode cli resume
```

### GUI 用法

```bash
python gui.py
```

GUI 支持：加载配置、测试登录、搜索资源、选择默认下载目录、双击下载、查看下载记录和日志、实时速度/ETA/进度百分比、暂停/继续/取消、播放已完成文件、删除文件和记录。

### Web 用法

```bash
python app.py --mode web --host 127.0.0.1 --port 8765
```

Web 支持：测试登录、搜索资源、发起下载、单次指定下载目录、忽略 .part 重新开始、查看当前下载状态（实时进度条+速度+ETA）、暂停/继续/取消（可选删除/保留临时文件）、设置默认下载目录（可持久化）、调节并发数、下载记录管理（继续/删除）、完整日志系统（历史文件切换+级别过滤+搜索）。

---

## 下载说明

### 文件组织

- 电影按 `片名 (年份)/片名 (年份).扩展名` 保存
- 剧集按 `剧名/Season 01/S01E01 - 标题.扩展名` 保存

### 分段下载逻辑

1. 默认使用 `segments` 设置的并发数
2. 服务端不支持 `Range` 时自动回退单线程
3. 开启 `experimental_force_multipart` 后，会直接试探真实分段请求验证是否可分段的转存链路
4. `stream_segments` 控制 `Videos/stream` 候选允许的最大分段数，避免一上来用太高并发把流地址打崩
5. 已有 `.part` 断点文件需要续传时，自动走单线程续传

### 断点续传

- 目标文件已存在且下载记录已完成 → 直接跳过
- 已有 `.part` 文件 → 自动续传（保留断点）
- 勾选"忽略 .part 重新开始" → 删除现有断点并重新下载
- 取消下载时 → 可选择删除或保留 `.part` 临时文件

### 日志说明

- GUI / Web 日志会显示候选地址类型、标准 Range 探测结果、实验分段探测结果和最终决策
- 下载记录会保存本次任务最终使用的模式，例如：
  - `分段并发下载（4 段）`
  - `实验性分段下载（4 段）`
  - `单线程续传（保留已有 .part）`
  - `单线程回退（服务端不支持分段）`

---

## 技术栈

| 组件 | 技术 |
|------|------|
| CLI | Python 标准库 |
| GUI | Tkinter |
| Web | Starlette + uvicorn |
| 数据库 | SQLite |
| 下载 | requests + 自研分段引擎 |
| 日志 | Python logging |

---

## 已知限制

- 部分服务器如果限制 API，可能无法登录或无法下载
- 某些服务器只给短期播放链接，失败时建议改用 `access_token`
- 正在运行中的旧下载进程不会热更新，必须重启 GUI / Web / 脚本后新逻辑才会生效
- 当前暂停/继续是进程内控制；如果直接关掉 GUI 或 Web 服务，需要靠 `.part` + `resume` 恢复
- 部分不稳定服务端在暂停太久后可能主动断流，继续时会自动按现有断点续传策略重试
- HLS 分片合并仍未实现，部分特殊资源可能无法直接离线

---

## 项目结构

```
emby-archiver/
├── app.py              # 统一启动入口
├── app_service.py      # 统一业务层
├── webapp.py           # Web 应用（Starlette）
├── gui.py              # Tkinter GUI
├── main.py             # CLI 入口
├── downloader.py       # 下载核心
├── emby_client.py      # Emby API 封装
├── library.py          # SQLite 下载记录
├── config.py           # 配置读取
├── models.py           # 数据模型
├── logger.py           # 日志配置
├── config.json         # 配置文件（需自行创建）
├── config.example.json # 配置示例
├── requirements.txt    # 依赖声明
├── docs/images/        # 截图
├── downloads/          # 默认下载目录
├── logs/               # 日志目录
└── store/              # SQLite 数据库目录
```

---

## 认证建议

优先级：
1. 直接用 `username` + `password`
2. 如果服务器不让密码登录，改用 `access_token`
3. 如果你拿到的是 `api_key`，最好同时填 `user_id`
