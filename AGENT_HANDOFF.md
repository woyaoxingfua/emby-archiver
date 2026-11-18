# Emby Cache Tool — Agent 交接文档

> 目的：让下一位 Agent 不依赖此前聊天记录，也能安全、准确地继续开发与验证。
>
> 本文包含本机路径，仅用于本地协作；不要把本文、账号配置或运行数据公开发布。

## 1. 当前项目与 Git 状态

### 本机工作仓库

- 操作系统：Windows
- 仓库根目录：`C:\Users\Lenovo\Downloads\emby-cache-tool`
- 当前分支：`main`
- 当前 HEAD：`83486a52a3e89364452355a2c2d45ce835091d94`
- 当前提交说明：`feat: 完成下载可靠性、任务并发与实时推送优化`
- 上一原始基线：`e5e50e7140bc063df61c9b265ccccb683b5bf327`
- 创建 `83486a5` 后、写入本文前，工作区是干净的。
- 本文是新增的交接文档，可能显示为未跟踪文件；不要把它误判为产品代码改动。

### 云端历史（仅用于理解来源）

- 云端环境 ID：`vm`，Linux。
- 云端工作目录：`/home/user/Doubao/chats/38453732456272898/emby-cache-tool`
- 云端最终提交：`f00f4e8e6594931efb91d4f9f7362d589019b9c2`
- 云端改动已经通过累计补丁应用到本机，并形成独立本机提交 `83486a5`。
- **不要再 cherry-pick 云端 `f00f4e8` 或重复应用旧补丁，否则会制造重复改动/冲突。**
- 目前只做了本机 Git commit，**没有执行 GitHub push 或其他远端写入**。

开始任何工作前仍须重新执行：

```powershell
$repo = "C:\Users\Lenovo\Downloads\emby-cache-tool"
git -C $repo status --short
git -C $repo rev-parse HEAD
git -C $repo log --oneline -5
```

如果状态与本文不同，以真实仓库为准；先辨认用户或其他 Agent 的新改动，禁止 `reset --hard`、强制覆盖或清理未知文件。

## 2. 本机 Python 与常用命令

项目已创建独立虚拟环境，避免污染系统 Python：

- Python：`3.12.5`
- venv 解释器：`C:\Users\Lenovo\Downloads\emby-cache-tool\.venv\Scripts\python.exe`
- 已安装：`requirements.txt`、`pytest`、`httpx`

### 完整测试

```powershell
$repo = "C:\Users\Lenovo\Downloads\emby-cache-tool"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$env:PYTHONDONTWRITEBYTECODE = "1"
& $python -m pytest (Join-Path $repo "tests") -q -p no:cacheprovider
```

### 启动 Web

```powershell
Set-Location "C:\Users\Lenovo\Downloads\emby-cache-tool"
& ".\.venv\Scripts\python.exe" ".\app.py" --mode web --host 127.0.0.1 --port 8765
```

启动后使用控制台打印的带 token 链接访问。不要在日志、回复或文档中泄露该 token。

### 如需重新创建环境（通常不需要）

```powershell
Set-Location "C:\Users\Lenovo\Downloads\emby-cache-tool"
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt pytest httpx2 httpx
```

## 3. 已验证结果

本机 Windows 实机已运行完整测试（2026-11-19 更新，HEAD 详见 Git log）：

```text
104 passed in 71.83s（历史基线：84 passed, 1 warning in 57.79s）
```

曾有的唯一警告（Starlette TestClient/httpx 弃用提示）已通过安装 httpx2 消除（见 §8 P3）；不要通过删除测试、吞掉警告或放宽断言来“修复”任何回归。

云端还曾验证：

- 工作树完整测试：84 passed。
- 最终 ZIP 解压后独立测试：84 passed。
- 累计补丁可应用到原始基线。
- Python AST、JavaScript `node --check`、JSON 解析和差异检查通过。

这些是特定版本、特定环境的证据，不能替代后续改动后的重新验证。

## 4. 已完成的核心改动

### 4.1 下载数据正确性

主要文件：`downloader.py`、`library.py`、`tests/test_p0_data_integrity.py`

- 续传、候选切换使用 ETag / If-Range 等机制校验源同一性。
- 同尺寸但源内容变化时，不允许混拼旧字节与新字节。
- 同名不同 item、未登记正式文件等冲突采用安全报错“拒绝覆盖”，不是自动改名。
- `TargetExistsError` 不应被其他候选重试吞掉。

### 4.2 预分配分段引擎

主要文件：`downloader.py`、`tests/test_preallocated_multipart.py`

- 新分段任务使用单个预分配 `.part`：先 `truncate(total)`，各线程用独立文件句柄 `seek + write` 写入自身区间。
- 没有依赖 Windows 不存在的 `os.pwrite`，本机 Windows 测试已通过。
- 每批数据写入后 `flush + fsync`，再在锁内原子替换 `.part.meta.json`。
- 元数据记录版本、总大小、ETag 和每段 `index/start/end/received`；不保存 `source_url`。
- 预分配后的 `.part` 文件大小不等于已下载进度，恢复进度必须读取元数据。
- 元数据损坏、ETag 改变、计划改变或边界不合法时，不得盲目信任旧进度。
- 没有 ETag 且已有进度时，不跨候选/进程复用无法验证的旧字节。

**兼容红线：**旧 `.seg` 断点仍走 `_download_segments_with_merge`。不要删除旧合并函数，否则旧断点及多项回归会失去兼容路径。只有新任务完全避免最终合并的第二份完整文件；旧任务仍有一次性合并开销。

### 4.3 Web 任务队列与并发

主要文件：`app_service.py`、`models.py`、`config.py`、`tests/test_download_queue.py`

- `download_workers` 可配置为 1–8，默认 1。
- 整季/整剧先展开成独立条目，再由 worker 池消费；不是父任务占住 worker 等子任务。
- 每个 job 使用 `dataclasses.replace(config)` 克隆配置，并创建独立 `EmbyClient` / `Downloader`。
- 单次覆盖下载目录不能修改其他任务共享配置。
- 同一活动资源的重复入队会被拒绝，另有每 item 锁作为写入保护。
- 任务状态包括 `queued/running/completed/failed/cancelled`，结果另记 `skipped` 等。
- 单集失败不阻断同批其他集，保留“成功 / 失败 / 跳过 / 取消”汇总。
- 删除活动记录等待目标 job 的 `done` 事件；未停止则保留文件与记录并报错，不能等待整个 worker 处理完无关任务。
- 队列目前只在内存中，进程重启不会自动恢复排队任务；已有磁盘断点可从下载记录继续。

### 4.4 SSE 实时状态推送

主要文件：`app_service.py`、`webapp.py`、`tests/test_sse.py`

- `/api/events` 推送状态、任务、下载记录与日志快照。
- 使用现有 Web token/Host/Origin 安全中间件，不要额外开放无鉴权事件端点。
- `AppService.wait_for_status` 用条件变量等待变更；进度事件有节流，状态变更主动通知。
- 前端正常时使用 `EventSource("/api/events")`。
- 原 3 秒盲轮询已移除；只有 SSE 断线时才启用 5 秒轮询兜底，恢复后停止。
- 页面卸载时关闭 EventSource 和兜底计时器。
- 任务名、错误等进入 HTML 前必须转义，不能引入 XSS。

### 4.5 每线程 HTTP Session

主要文件：`emby_client.py`、`tests/test_session_isolation.py`

- `requests.Session` 已改为 thread-local，每线程复用自己的 Session。
- 新 Session 继承认证、User-Agent 和代理配置。
- 认证刷新会同步更新已登记 Session。
- job 结束后关闭所属 client 的 Session。
- 不要把“urllib3 连接池线程安全”误当成整个 Session 的可变状态都无竞态。

### 4.6 GUI 与磁盘安全

主要文件：`gui.py`、`app_service.py`、`downloader.py`、`tests/test_stability_p1.py`

- 活动 worker 未停止时禁止删除记录和文件。
- 分段下载实际执行磁盘空间预检；旧 `.seg` 合并路径使用 `remaining + expected_size`。
- GUI 启动时执行中断记录对账。
- GUI 整季批量遇到单集 `DownloadError`、客户端错误或 `OSError` 时记录失败并继续。
- GUI/Web 删除和强制重下识别新的 `.part.meta.json` 及其临时文件。

## 5. 重要测试文件

| 文件 | 主要保护合同 |
|---|---|
| `tests/test_p0_data_integrity.py` | 换源、防覆盖、ETag 与同名冲突 |
| `tests/test_stability_p1.py` | 活动删除、磁盘预检、GUI 对账与批量失败继续 |
| `tests/test_preallocated_multipart.py` | 预分配、取消恢复、换源重下、元数据不泄密 |
| `tests/test_download_queue.py` | 整季 worker 并发、任务目录隔离 |
| `tests/test_sse.py` | SSE 鉴权、快照/日志推送、worker API、无 3 秒轮询 |
| `tests/test_session_isolation.py` | 真实并发下 Session 隔离与认证继承 |

修改某机制后，先跑对应测试，再跑完整 `tests`。不要用只降低复现概率、增加任意 sleep、吞错或放宽断言替代原因级修复。

## 6. 数据、凭据与本地文件红线

以下内容可能包含账号、token、个人媒体信息或用户工作，禁止提交、打包、公开上传或无故删除：

- `config.json`（与 `config.example.json` 不同）
- `store/`、SQLite 数据库、Web token
- `logs/`
- `downloads/`、正式媒体文件
- `.part`、`.part.meta.json`、旧 `.seg` 等用户断点
- `.venv/`

不要为了方便测试而连接或修改用户真实 Emby 数据。真实验收应使用用户明确授权的测试服务器、测试账号和测试媒体。

## 7. 尚未证明的能力与边界

- 本机 Windows 已通过 Mock Emby 测试，但尚未对真实 Emby 在线验证认证、Range、ETag、候选切换和播放流行为。
- 取消恢复已验证；真实断电、操作系统级崩溃和文件系统掉电语义未实测。
- 每批刷盘及原子更新元数据会增加 I/O；尚无不同硬件上的性能 benchmark。
  - 2026-11-19 更新：`flush_interval_seconds`（默认 0=旧行为每 chunk 刷盘）已实现节流批量刷盘，暂停/取消/段尾/请求失败一律强制精确落盘；元数据 received 只在字节 fsync 后更新（永不超前数据）。本机基准（8MB×4段×64KB chunk，Mock Emby）：0s=21.1MB/s、250 次 fsync；0.5s=92.9MB/s、10 次 fsync（吞吐 4.4 倍）；恢复精度两模式均字节精确。基准在 `tests/bench_multipart_perf.py`（随全套件运行），回归在 `tests/test_flush_throttle.py`。节流开启时硬崩溃后至多重传 flush_interval 秒带宽量/段；真实断电语义仍未实测。
- GUI 主要验证任务/加载逻辑，未做完整真实桌面点击交互。
- Web 队列不是持久队列，重启后的排队任务恢复未实现。
- 旧 `.seg` 兼容路径仍需要一次合并。
- 长时间运行时 `_jobs`、`_batches`、`_item_locks` 的历史清理尚未实现，不能宣称无限运行无内存增长。
  - 2026-11-19 更新：终态 job/batch/item 锁的有界保留已实现（`job_history_limit`，默认 200，0=不清理）。终态且 done 已置位的 job 按上限保留；批次 finished==total 或无引用即移除；无活动 job 的条目锁即移除。触发点为入队与任务收尾，全部在 `_state_lock` 内原子执行。附带修复了 `_queue_worker` 对已清理 job id 直接索引的 KeyError 风险（改为 `.get()` 跳过）。回归见 `tests/test_history_prune.py`（6 项：有界性、活动保护、0 兼容、取消残留队列、删除竞态、跨批次汇总）。仍未验证真实断电等极端场景。
- 日志仍有中英文混排，统一 i18n 未做；它不是当前数据正确性的阻塞项。

## 8. 推荐后续工作（按优先级）

### P0：真实 Emby 验收（需用户明确提供测试环境）

覆盖：登录、单线程、分段 Range、ETag 变化、无 ETag、暂停/继续、取消恢复、候选切换、同名冲突、`download_workers=2`、SSE 断线重连。保存版本、配置条件和实际结果，不把 Mock 结论外推为真实服务器保证。

### P1：预分配引擎性能测量

已完成（2026-11-19）：基准 + 节流实现一体落地。测量见 §7 更新条目（8MB/4段：0s 21.1MB/s 250 次 fsync → 0.5s 92.9MB/s 10 次 fsync）。默认保持 0（旧行为），用户可按磁盘能力在 config.json 开启 `flush_interval_seconds`。本机 Windows 全套 95 passed（90 基线 + 节流回归 5 + 基准 5）。若未来改默认值，先跑 `tests/bench_multipart_perf.py -s` 在目标硬件复测，并重审崩溃窗口语义。

### P1：长运行历史清理

已完成（2026-11-19）：`job_history_limit` 有界保留落地，含「大量终态任务 + 活动任务 + 删除/取消竞态」回归。本机 Windows 全套测试 90 passed（84 基线 + 6 新增）。后续若要调整保留策略，先读 `app_service.py` 的 `_prune_history_locked` 注释中的三条不变量与 done 协议约束。

### P2：持久任务队列（只有用户需要时）

先设计数据库系统记录源、启动对账、重复入队、取消、崩溃恢复、批次状态与旧版本兼容，再编码。不要简单把当前内存队列序列化就宣称可靠恢复。
入队判重（DupeCheck，2026-11-19 已实现一部分）：历史中已 completed 的条目入队时直接跳过并入队响应显式报告（`queued/skipped_completed/batch_id`）；`force_restart` 语义优先；全部已缓存时不创建空批次。回归 `tests/test_enqueue_dedupe.py`。此改动源自 Sonarr/SABnzbd/NZBGet 竞品调研；调研还识别出「事件型历史、统一通知钩子、外挂字幕随行、缓存容量滚动清理、磁盘空间护栏」等待选特性，详见聊天记录或按需重调研。

### P2：真实 GUI/Web 交互验收

在本机桌面检查启动、搜索、批量下载、暂停/继续/取消、删除和 SSE 页面；同时观察控制台错误与请求错误。

### P3：测试依赖与日志维护

Starlette TestClient/httpx2 弃用提示已于 2026-11-19 处理：安装 `httpx2`（2.16.2）并加入 pyproject `dev` extras，测试代码零改动，全套警告清零（官方路线，FastAPI 同款做法；httpx 保留在 dev 里兼容旧环境）。调研结论：httpx 自 2024-12 后停止维护，httpx2 是 Pydantic 接管的官方续作（fork 自 0.28.1、API 相同）；注意其 SSL 改用 truststore，若部署环境有私有自签 CA 需验证。日志 i18n 仍未做，避免与可靠性改动混成大提交。

## 9. 下一位 Agent 的执行约定

1. 先读本文、`README.md`、当前 `git status` 和相关源码，不凭本文代替真实代码。
2. 修改前记录 HEAD、工作树和测试基线；用户的新改动不得归因于自己，也不得覆盖。
3. 对缺陷先留下能区分旧实现的失败证明，再做原因级修复。
4. 涉及并发、恢复、持久状态或外部副作用时，分别验证交接成功、处理完成、取消、删除和异常分支。
5. 不执行 GitHub push、发布、删除远端内容或其他外部写入，除非用户明确授权。
6. 完成后报告实际命令、测试结果、commit（若用户要求提交）及未验证边界，不夸大保证。

可直接给下一位 Agent 的第一条指令：

> 先阅读 `C:\Users\Lenovo\Downloads\emby-cache-tool\AGENT_HANDOFF.md`，核对当前 Git 状态和真实源码，再从第 8 节中选择我指定的任务继续；不要重复应用云端补丁，不要修改或提交账号配置和运行数据。
