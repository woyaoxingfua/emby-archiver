# Emby Cache Tool 交接文档

这份文档是给下一个 AI / 开发者的详细交接说明，目标是尽量减少重复摸索时间，直接接着做。

---

## 1. 项目目标

这是一个运行在 **Windows 本机** 的 Python 工具，核心用途是：

- 用 Emby 账号登录服务器
- 通过 Emby API 找到可访问的视频资源
- 把视频 **预下载到本地**，用于离线观看
- 不是边播边缓存，而是明确的“本地离线缓存工具”

当前也在扩展为：

- Windows 本机可用
- 纯终端可用
- Android Termux / 小窗浏览器场景可用

---

## 2. 用户的核心需求（非常重要）

用户的真实诉求，后续实现不要偏：

1. **保留 CLI**
2. **保留 Tkinter GUI**
3. **新增 Web 模式**
4. 启动时支持选择模式：`auto / gui / web / cli`
5. 默认优先 Tkinter
6. **没有图形环境时自动回退到 Web**
7. 不需要复杂守护进程，Termux 小窗挂着跑就行
8. Web 场景下主要是：
   - 搜索
   - 下载
   - 查看下载状态
   - 查看下载记录
   - 继续未完成任务
   - 看日志
9. 用户对 Web 的要求是“先能用”，不是一开始就要做复杂前后端分离

补充：

- 用户下载的是共享账号可访问资源
- 经验证，用户账号 **没有 `DownloadContent` 权限**
- 因此 `Items/{id}/Download` 常见 `403`
- 真正常用可行候选通常是：`Videos/{id}/stream?...`
- 工具必须继续围绕这个现实约束工作

---

## 3. 当前环境与事实

### 3.1 开发环境

- 平台：Windows
- Python：`3.12.5`
- 主解释器：`D:\python\python.exe`
- 项目目录：`C:\Users\Lenovo\Downloads\emby-cache-tool`

### 3.2 已确认可用依赖 / 环境现状

原始环境里曾确认过这些包可以导入：

- `requests`
- `tqdm`
- `tkinter`
- `fastapi`
- `uvicorn`
- `jinja2`

但是：

- 本机实际依赖组合里，`fastapi 0.112.2` 和 `starlette 1.1.0` **不兼容**
- 直接 `FastAPI(...)` 启动时会报：
  - `TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'`
- 所以这一轮 **没有继续强绑 FastAPI**
- 最终改成了 **直接用 Starlette + uvicorn** 做 Web MVP

这点非常关键：

> 不要看到之前计划里写了 FastAPI，就默认现在代码还是 FastAPI。
> 当前实际落地是 Starlette，不是 FastAPI。

### 3.3 依赖告警现状

CLI / GUI / launcher 启动处都已经屏蔽：

- `RequestsDependencyWarning: urllib3.../chardet... doesn't match a supported version!`

但这只是 **屏蔽输出**，**根因没有解决**。

---

## 4. 本轮之前已经完成的能力

这是本轮做 Web/统一入口之前，项目就已经具备的内容：

### 4.1 CLI 能力

已完成 Emby 离线缓存工具 MVP：

- 登录
- 列媒体库
- 搜索
- 按 `item id` 下载
- 断点续传
- SQLite 下载记录

### 4.2 Emby API 封装

`emby_client.py` 已支持：

- `username/password`
- `access_token`
- `api_key`

并支持：

- 列视图
- 搜索
- 展开剧集 / 季
- 获取下载候选地址
- 打开流下载

### 4.3 GUI 能力

Tkinter GUI 已支持：

- 加载配置
- 测试登录
- 搜索
- 下载选中项
- 查看下载记录
- 查看日志
- 观察进度 / 模式 / 速度 / ETA
- 暂停 / 继续 / 取消
- 继续未完成下载记录
- 播放已完成文件
- 删除已完成文件 + 记录
- 选择默认下载目录
- 单次“下载到...”临时目录

### 4.4 下载器能力

`downloader.py` 已支持：

- 标准分段并发下载
- 服务端不支持 Range 时自动回退单线程
- 实验性强制分段
- 下载链路代理复用
- 丰富诊断日志
- 暂停 / 继续 / 取消
- 可保留 `.part` 或清理 `.part`
- `paused` / `cancelled` 状态
- `Videos/stream` 限段（默认 `stream_segments=2`）
- 跳过已完成文件，避免重复下载
- “忽略 `.part` 重新开始”
- `.part.seg*` 残留清理修复

### 4.5 重要现实结论

用户曾反馈“8 线程和 2 线程没区别”，最终已确认根因是：

- 已存在 `.part`
- 逻辑优先保断点续传
- 因此走单线程续传
- 不是并发设置本身没生效

项目里已经为这个坑做了针对性修复和提示。

---

## 5. 本轮已经完成的工作（交接重点）

这一轮的目标是：

- 启动时可选 Tkinter / Web / CLI
- 默认 Tkinter
- 无图形环境自动回退到 Web

### 5.1 已完成：统一启动入口

新增文件：`app.py`

作用：

- 统一启动入口
- 支持：`--mode auto|gui|web|cli`

当前行为：

- `python app.py`
  - 默认 `auto`
  - 先尝试 Tkinter GUI
  - GUI 启动失败则回退到 Web
- `python app.py --mode gui`
  - 强制 GUI
- `python app.py --mode web`
  - 强制 Web
- `python app.py --mode cli ...`
  - 透传到旧的 CLI 子命令入口 `main.py`

额外说明：

- `app.py --mode cli login-test --help` 已验证可正常显示 CLI 子命令帮助
- 为了兼容 CLI 参数透传，`app.py` 做了手动参数转发

### 5.2 已完成：抽出统一业务层

新增文件：`app_service.py`

目的：

- 不让 Web 直接复制 GUI/CLI 逻辑
- 把业务操作统一收口，便于 GUI / Web 复用

当前 `AppService` 已覆盖：

- 加载配置
- 初始化日志
- 登录测试
- 搜索
- 列下载记录
- 获取当前下载状态
- 获取进程内日志
- 启动下载
- 继续下载记录
- 暂停当前下载
- 继续当前下载
- 取消当前下载
- 删除文件 + 删除记录
- 设置默认下载目录（仅当前进程内生效）

当前内部实现特点：

- 通过单个后台线程跑当前下载任务
- 默认就是单任务下载 worker
- 状态保存在 `self._download_state`
- 进程内日志保存在 `deque(maxlen=500)`

### 5.3 已完成：Web MVP

新增文件：`webapp.py`

注意：

- **最终实现是 Starlette，不是 FastAPI**
- 用 `uvicorn.run(...)` 启动

当前 Web 已支持：

- 首页 HTML（单文件内联）
- `GET /`
- `GET /api/summary`
- `GET /api/status`
- `GET /api/logs`
- `POST /api/login-test`
- `GET /api/search`
- `GET /api/downloads`
- `POST /api/download`
- `POST /api/downloads/{item_id}/resume`
- `DELETE /api/downloads/{item_id}`
- `POST /api/control/pause`
- `POST /api/control/resume`
- `POST /api/control/cancel`
- `POST /api/default-download-dir`

Web 页面当前支持：

- 测试登录
- 搜索资源
- 发起下载
- 单次指定下载目录
- 忽略 `.part` 重下
- 查看当前下载状态
- 暂停 / 继续 / 取消
- 查看下载记录
- 继续未完成记录
- 删除已完成文件和记录
- 查看当前进程内日志

### 5.4 已完成：Web 语义降级

一开始曾做过 `open/play/open-dir` 方向的服务端动作，但考虑到用户明确要兼容 Web / Termux / 手机浏览器场景，最终做了降级：

- Web 端 **不再强调本地播放器 / 打开目录**
- 已完成记录在 Web 上先只展示保存路径
- 这是一个有意识的设计，不是漏做

原因：

- 浏览器所在设备和服务所在主机不是同一台机器时
- “播放 / 打开目录”语义会非常混乱
- 在手机访问 Windows 主机 Web 时，强行 `os.startfile` 并不符合用户直觉

### 5.5 已完成：依赖声明更新

当前 `requirements.txt` 已改成：

- `requests>=2.31.0`
- `tqdm>=4.66.0`
- `starlette>=1.1.0`
- `uvicorn>=0.30.0`

注意：

- 之前虽然环境里能 import `fastapi` / `jinja2`
- 但 **现在 requirements 没有继续声明 FastAPI / Jinja2**
- 因为最终落地没用它们

### 5.6 已完成：README 文档更新

`README.md` 已补充：

- 推荐用 `python app.py`
- `auto/gui/web/cli` 统一入口说明
- Web 用法
- Web MVP 当前支持项
- Termux / 手机访问时建议 `--host 0.0.0.0`
- Web 端当前降级说明

---

## 6. 已验证的命令

以下命令这一轮已经实际跑过：

### 6.1 语法编译

```bash
D:/python/python.exe -m py_compile app.py app_service.py webapp.py main.py gui.py downloader.py emby_client.py config.py models.py library.py logger.py
```

通过。

### 6.2 launcher 帮助

```bash
D:/python/python.exe app.py --help
```

通过。

### 6.3 CLI 透传帮助

```bash
D:/python/python.exe app.py --mode cli login-test --help
```

通过。

### 6.4 CLI 透传实际调用

```bash
D:/python/python.exe app.py --mode cli downloads
```

通过。

### 6.5 Web 启动

```bash
D:/python/python.exe app.py --mode web --host 127.0.0.1 --port 8876
```

通过。

### 6.6 Web 接口验证

```bash
curl http://127.0.0.1:8876/api/summary
curl http://127.0.0.1:8876/
```

都通过。

---

## 7. 当前关键文件与职责

### `app.py`
统一启动入口。

### `app_service.py`
统一业务层，Web 现在主要靠它驱动。

### `webapp.py`
Starlette Web MVP，当前是单文件内联 HTML + JS。

### `main.py`
原 CLI 入口，保留不动，launcher 通过 `--mode cli` 转发进去。

### `gui.py`
Tkinter GUI，仍然保留并可单独运行。

### `downloader.py`
下载核心，项目最关键的业务之一。

### `emby_client.py`
Emby API 访问与鉴权封装。

### `library.py`
SQLite 下载记录。

### `config.py`
配置读取。

### `models.py`
数据模型，`AppConfig`、`MediaItem`、`DownloadRecord`。

---

## 8. 当前仍然存在的不足 / 已知问题

这是下一个 AI 最值得继续做的部分。

### 8.1 GUI 默认下载目录还没有持久化到 `config.json`

现状：

- GUI 能改默认下载目录
- 但只在当前 GUI 进程内生效
- 不会自动写回 `config.json`

这件事用户之前已经明确表达过需求：

- 当前会话默认目录要有
- 单次临时目录也要有

其中“当前会话默认目录”已经有；
“自动保存回配置”还没做。

### 8.2 Web 默认下载目录也只是进程内修改

`app_service.py` 的 `set_default_download_dir()` 现在只是改：

- `self.config.download_dir`
- `self.client.config.download_dir`
- `self.downloader.client.config.download_dir`

不会回写 `config.json`。

### 8.3 Web 目前还是 MVP，界面比较粗糙

现在的 Web 页面：

- 是单文件内联 HTML + JS
- 当前下载状态直接展示原始 JSON
- 下载记录是基础表格
- 没有更细的进度条、速度、ETA 展示
- 没有分页、筛选、错误态细化

功能上够用，但产品化程度低。

### 8.4 Web 日志是“当前进程内日志”，不是完整日志文件浏览器

`app_service.py` 里是：

- `deque(maxlen=500)`

所以当前 Web 只能看到：

- 本进程启动后产生的最近 500 条 service 日志

看不到：

- 历史日志文件全部内容
- 其他进程的日志

### 8.5 Web 端没有做认证 / 权限控制

当前 Web 面板默认就是：

- 本机或局域网谁能访问这个端口，谁就能操作下载

如果后面用户要暴露到局域网或公网，这会是安全问题。

### 8.6 Web 端当前没有“播放 / 打开目录”

这是**有意识降级**，不是缺陷，但后续可再讨论：

- 如果只考虑本机浏览器访问 Windows 主机，服务端 `os.startfile` 是可行的
- 如果考虑手机/Termux/远程浏览器，那就不适合照搬 GUI 语义

后续可以考虑：

- 仅展示路径
- 提供“复制路径”
- 提供目录文本定位
- 或者单独加一个“仅本机可用”的按钮并写清楚语义

### 8.7 `config.example.json` 还没有新增 Web host/port 项

当前 Web host/port 只走 CLI 参数：

- `--host`
- `--port`

配置文件里没有：

- `web_host`
- `web_port`
- `web_auto_open`
- `web_allow_remote`

这些都还没落地。

### 8.8 依赖告警根因未处理

当前只是屏蔽：

- `RequestsDependencyWarning`

但环境真实依赖仍然可能有版本组合问题。

### 8.9 HLS 分片合并仍未实现

这是更早就存在的限制：

- 如果资源只能拿到 HLS
- 当前依然不能完整离线下载合并

### 8.10 还没有自动化测试

目前主要是：

- 手工验证
- 命令验证
- 真实运行验证

还没有：

- 单元测试
- Web API 测试
- launcher 行为测试

---

## 9. 下一个 AI 最推荐优先做的事

建议优先级如下。

### P1：把默认下载目录持久化回 `config.json`

用户价值最高，改动也不算太大。

建议实现：

- GUI 改默认下载目录时，询问是否写回配置
- Web 设置默认目录时，也可选择持久化
- 抽一个统一的配置写回函数，避免 GUI/Web 各写一套

注意：

- 不要破坏现有 `config.json` 结构
- 最小改动原则

### P2：把 Web 下载状态做得更像 GUI

当前 Web 状态展示太原始。

建议补：

- 当前下载标题
- 当前模式
- 字节进度
- 百分比
- 速度
- ETA
- 更直观的进度条

如果能直接复用 GUI 的计算逻辑最好，但别硬耦合 Tkinter。

### P3：把 Web 页面从单文件内联拆出来

当前 `webapp.py` 里内联了 HTML + JS，维护性一般。

建议未来拆成：

- `templates/`
- `static/`

但这是优化项，不是必须先做。

### P4：决定是否重新引入 FastAPI

这里要谨慎。

当前建议：

- **如果只是为了“能用”**，继续用 Starlette 就够了
- **如果要重回 FastAPI**，必须同时整理依赖版本，确保和 Starlette 匹配

不要只改代码，不改依赖。

### P5：给 Web 加一个最简安全措施

如果用户准备在局域网里用，至少考虑：

- 可选访问密码
- 仅监听 `127.0.0.1`
- 或在 README 明确风险

### P6：给 Web 增加“历史日志文件查看”

可以做成：

- 显示当前日志文件路径
- 提供最近 N 行
- 不必一开始就做全文搜索

---

## 10. 当前实现里需要特别注意的点

### 10.1 不要误删现有 GUI / CLI 逻辑

用户明确要：

- CLI 保留
- Tkinter 保留
- Web 是新增，不是替代

### 10.2 不要把项目目标做偏成在线播放器

工具目标是：

- **离线下载 / 本地缓存**

不是：

- Web 播放器
- 在线流媒体前端
- 边播边缓存系统

### 10.3 不要忽视 `Videos/stream` 的现实优先级

由于用户账号无 `DownloadContent` 权限：

- `Items/.../Download` 常会 403
- `Videos/.../stream` 才是现实常用下载候选

### 10.4 不要误判“并发没生效”

如果又出现“8 线程和 2 线程一样慢”的反馈，先检查：

- 是否已有 `.part`
- 是否其实在单线程续传
- 是否走了 `Videos/stream` 自动限段

### 10.5 Web 的“本机动作”和“远程浏览器动作”要分清语义

尤其是：

- 播放
- 打开目录
- 打开文件

这类动作在 Web 场景和 GUI 场景的语义完全不同，不要机械照搬。

---

## 11. 现在可以怎么运行

### 默认 auto 模式

```bash
python app.py
```

行为：

- 有 GUI 环境：优先 Tkinter
- GUI 起不来：回退 Web

### 强制 GUI

```bash
python app.py --mode gui
```

### 强制 Web

```bash
python app.py --mode web --host 127.0.0.1 --port 8765
```

### 局域网 / 手机 / Termux 访问

```bash
python app.py --mode web --host 0.0.0.0 --port 8765
```

### 继续使用原 CLI

```bash
python app.py --mode cli login-test
python app.py --mode cli search "你的关键字"
python app.py --mode cli download --item-id 123456
python app.py --mode cli downloads
```

也仍可直接：

```bash
python main.py ...
python gui.py
```

---

## 12. 本轮实际踩过的坑

### 坑 1：FastAPI 看起来能 import，但实际版本不兼容

症状：

- `FastAPI(...)` 启动时报 `Router.__init__() got an unexpected keyword argument 'on_startup'`

原因：

- FastAPI / Starlette 版本组合不匹配

处理：

- 放弃本轮强行修 FastAPI 依赖
- 改用 Starlette 直接落地 Web MVP

### 坑 2：`app.py --mode cli ... --help` 一开始会被 launcher 自己吞掉

已经修掉：

- 现在 `--mode cli login-test --help` 会正确透传给原 CLI

### 坑 3：Web 场景下“播放 / 打开目录”不是 GUI 语义

处理：

- 当前版本先降级，不在 Web 上硬做这两个动作

---

## 13. 结论

当前项目已经从：

- 只有 CLI + Tkinter GUI

推进到：

- CLI 保留
- GUI 保留
- Web MVP 可用
- 统一入口 `app.py` 已落地
- `auto/gui/web/cli` 模式已落地
- Web 已能在终端 / 无图形场景下工作

但还远没有完全收尾。

最建议下一个 AI 继续做的是：

1. 默认下载目录持久化回配置
2. Web 当前下载观测做得更像 GUI
3. Web 体验优化
4. 视需要整理依赖版本
5. 再决定是否引回 FastAPI

---

## 14. 给下一个 AI 的一句话总结

请把这个项目继续当成：

> “Emby 离线缓存下载工具”，
> 现在已经有 CLI / Tkinter / Web 三条入口，
> 当前最重要的是补全易用性和持久化，而不是推翻重写。
