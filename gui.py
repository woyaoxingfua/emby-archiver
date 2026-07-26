from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import warnings
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
)

from config import ConfigError, load_config, save_default_download_dir
from downloader import DownloadCancelled, DownloadError, Downloader
from emby_client import EmbyClient, EmbyClientError
from library import CacheLibrary
from logger import setup_logging
from models import DownloadRecord, MediaItem


class EmbyCacheGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Emby Cache Tool")
        self.root.geometry("1240x820")

        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.config_var = tk.StringVar(value="config.json")
        self.keyword_var = tk.StringVar()
        self.download_dir_var = tk.StringVar(value="downloads")
        self.status_var = tk.StringVar(value="未加载配置")
        self.download_name_var = tk.StringVar(value="暂无活动下载")
        self.download_mode_var = tk.StringVar(value="模式：未开始")
        self.download_speed_var = tk.StringVar(value="速度：--")
        self.download_eta_var = tk.StringVar(value="剩余：--")
        self.download_bytes_var = tk.StringVar(value="进度：0 B / --")
        self.download_progress_var = tk.DoubleVar(value=0.0)

        self.client: EmbyClient | None = None
        self.library: CacheLibrary | None = None
        self.downloader: Downloader | None = None
        self.search_results: list[MediaItem] = []
        self._speed_history: dict[str, list[tuple[float, int]]] = {}
        self._active_downloads = 0
        self._download_state: dict[str, dict[str, object]] = {}
        self.segments_var = tk.IntVar(value=4)
        self.force_restart_var = tk.BooleanVar(value=False)
        self._current_download_item: MediaItem | None = None
        self._pause_btn: ttk.Button | None = None
        self._resume_btn: ttk.Button | None = None
        self._cancel_btn: ttk.Button | None = None
        self._record_resume_btn: ttk.Button | None = None
        self._record_play_btn: ttk.Button | None = None
        self._record_delete_btn: ttk.Button | None = None

        self._build_ui()
        self._reset_download_observer()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=10)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=0)
        container.rowconfigure(3, weight=1)
        container.rowconfigure(4, weight=1)

        config_frame = ttk.LabelFrame(container, text="配置")
        config_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        config_frame.columnconfigure(1, weight=1)

        ttk.Label(config_frame, text="Config 路径").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        ttk.Entry(config_frame, textvariable=self.config_var).grid(row=0, column=1, padx=6, pady=8, sticky="ew")
        ttk.Button(config_frame, text="加载配置", command=self.load_config_only).grid(row=0, column=2, padx=6, pady=8)
        ttk.Button(config_frame, text="测试登录", command=self.login).grid(row=0, column=3, padx=6, pady=8)
        ttk.Label(config_frame, text="并发数").grid(row=0, column=4, padx=(20, 4), pady=8, sticky="e")
        ttk.Spinbox(config_frame, from_=1, to=32, textvariable=self.segments_var, width=6).grid(row=0, column=5, padx=(0, 6), pady=8, sticky="w")
        ttk.Checkbutton(
            config_frame,
            text="忽略 .part 重新开始",
            variable=self.force_restart_var,
        ).grid(row=0, column=6, padx=(12, 6), pady=8, sticky="w")
        ttk.Label(config_frame, text="下载目录").grid(row=1, column=0, padx=6, pady=(0, 8), sticky="w")
        ttk.Entry(config_frame, textvariable=self.download_dir_var).grid(row=1, column=1, columnspan=4, padx=6, pady=(0, 8), sticky="ew")
        ttk.Button(config_frame, text="浏览...", command=self.browse_download_dir).grid(row=1, column=5, padx=6, pady=(0, 8))
        ttk.Label(config_frame, textvariable=self.status_var).grid(row=2, column=0, columnspan=7, padx=6, pady=(0, 8), sticky="w")

        search_frame = ttk.LabelFrame(container, text="搜索")
        search_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        search_frame.columnconfigure(1, weight=1)

        ttk.Label(search_frame, text="关键词").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        keyword_entry = ttk.Entry(search_frame, textvariable=self.keyword_var)
        keyword_entry.grid(row=0, column=1, padx=6, pady=8, sticky="ew")
        keyword_entry.bind("<Return>", lambda _event: self.search())
        ttk.Button(search_frame, text="搜索", command=self.search).grid(row=0, column=2, padx=6, pady=8)
        ttk.Button(search_frame, text="下载选中项", command=self.download_selected).grid(row=0, column=3, padx=6, pady=8)
        ttk.Button(search_frame, text="下载到...", command=self.download_selected_to).grid(row=0, column=4, padx=6, pady=8)
        ttk.Button(search_frame, text="刷新下载记录", command=self.refresh_downloads).grid(row=0, column=5, padx=6, pady=8)

        observe_frame = ttk.LabelFrame(container, text="当前下载观测")
        observe_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        observe_frame.columnconfigure(1, weight=1)
        observe_frame.columnconfigure(3, weight=1)

        ttk.Label(observe_frame, text="资源").grid(row=0, column=0, padx=6, pady=(8, 4), sticky="w")
        ttk.Label(observe_frame, textvariable=self.download_name_var).grid(row=0, column=1, columnspan=3, padx=6, pady=(8, 4), sticky="w")
        ttk.Label(observe_frame, textvariable=self.download_mode_var).grid(row=1, column=0, columnspan=2, padx=6, pady=4, sticky="w")
        ttk.Label(observe_frame, textvariable=self.download_speed_var).grid(row=1, column=2, padx=6, pady=4, sticky="w")
        ttk.Label(observe_frame, textvariable=self.download_eta_var).grid(row=1, column=3, padx=6, pady=4, sticky="w")
        ttk.Progressbar(observe_frame, variable=self.download_progress_var, maximum=100).grid(row=2, column=0, columnspan=4, padx=6, pady=4, sticky="ew")
        ttk.Label(observe_frame, textvariable=self.download_bytes_var).grid(row=3, column=0, columnspan=4, padx=6, pady=(4, 8), sticky="w")

        self._pause_btn = ttk.Button(observe_frame, text="暂停", command=self._pause_download, state="disabled")
        self._pause_btn.grid(row=4, column=0, padx=6, pady=(4, 8), sticky="w")
        self._resume_btn = ttk.Button(observe_frame, text="继续", command=self._resume_download, state="disabled")
        self._resume_btn.grid(row=4, column=1, padx=6, pady=(4, 8), sticky="w")
        self._cancel_btn = ttk.Button(observe_frame, text="取消", command=self._cancel_download, state="disabled")
        self._cancel_btn.grid(row=4, column=2, padx=6, pady=(4, 8), sticky="w")

        result_frame = ttk.LabelFrame(container, text="搜索结果")
        result_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)

        self.result_tree = ttk.Treeview(
            result_frame,
            columns=("id", "type", "name", "year"),
            show="headings",
            height=12,
        )
        for key, title, width in (
            ("id", "ID", 210),
            ("type", "类型", 100),
            ("name", "标题", 520),
            ("year", "年份", 80),
        ):
            self.result_tree.heading(key, text=title)
            self.result_tree.column(key, width=width, anchor="w")
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        self.result_tree.bind("<Double-1>", lambda _event: self.download_selected())
        result_scrollbar = ttk.Scrollbar(result_frame, orient="vertical", command=self.result_tree.yview)
        result_scrollbar.grid(row=0, column=1, sticky="ns")
        self.result_tree.configure(yscrollcommand=result_scrollbar.set)

        bottom = ttk.Frame(container)
        bottom.grid(row=4, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=1)
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(0, weight=1)

        log_frame = ttk.LabelFrame(bottom, text="日志")
        log_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")
        log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        downloads_frame = ttk.LabelFrame(bottom, text="下载记录")
        downloads_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        downloads_frame.columnconfigure(0, weight=1)
        downloads_frame.rowconfigure(0, weight=1)

        self.download_tree = ttk.Treeview(
            downloads_frame,
            columns=("status", "name", "progress", "mode", "path"),
            show="headings",
            height=12,
        )
        for key, title, width in (
            ("status", "状态", 100),
            ("name", "标题", 200),
            ("progress", "进度", 130),
            ("mode", "模式/原因", 240),
            ("path", "路径", 300),
        ):
            self.download_tree.heading(key, text=title)
            self.download_tree.column(key, width=width, anchor="w")
        self.download_tree.grid(row=0, column=0, sticky="nsew")
        self.download_tree.bind("<<TreeviewSelect>>", self._on_download_record_select)
        self.download_tree.bind("<Double-1>", self._show_download_record_menu)
        self.download_tree.bind("<Button-3>", self._show_download_record_menu)
        download_scrollbar = ttk.Scrollbar(downloads_frame, orient="vertical", command=self.download_tree.yview)
        download_scrollbar.grid(row=0, column=1, sticky="ns")
        self.download_tree.configure(yscrollcommand=download_scrollbar.set)

        action_frame = ttk.Frame(downloads_frame)
        action_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=2, pady=(8, 0))
        self._record_resume_btn = ttk.Button(action_frame, text="继续下载", command=self.resume_selected_record, state="disabled")
        self._record_resume_btn.grid(row=0, column=0, padx=(0, 6))
        self._record_play_btn = ttk.Button(action_frame, text="播放", command=self.play_selected_record, state="disabled")
        self._record_play_btn.grid(row=0, column=1, padx=(0, 6))
        self._record_delete_btn = ttk.Button(action_frame, text="删除", command=self.delete_selected_record, state="disabled")
        self._record_delete_btn.grid(row=0, column=2)

    def load_config_only(self) -> None:
        try:
            config = load_config(Path(self.config_var.get().strip()))
            setup_logging(config.log_dir, config.log_level)
            self.client = EmbyClient(config)
            self.library = CacheLibrary(config.database_path)
            self.segments_var.set(config.segments)
            self.download_dir_var.set(str(config.download_dir))
            self.downloader = Downloader(self.client, self.library, progress_callback=self._download_progress, segments=config.segments)
            self.status_var.set(f"已加载配置：{config.server_url}")
            self.log(f"配置加载成功：{config.server_url}")
            if config.experimental_force_multipart:
                self.log("实验性强制分段：已启用")
            if config.proxy_http or config.proxy_https:
                self.log(
                    "代理已启用："
                    f"http={config.proxy_http or '-'}; https={config.proxy_https or '-'}"
                )
            self.refresh_downloads()
        except ConfigError as exc:
            self.status_var.set("配置加载失败")
            self.log(f"配置错误：{exc}")
            messagebox.showerror("配置错误", str(exc))

    def login(self) -> None:
        if not self.client:
            self.load_config_only()
        if not self.client:
            return
        self._run_background(self._login_task)

    def search(self) -> None:
        keyword = self.keyword_var.get().strip()
        if not keyword:
            messagebox.showinfo("提示", "先输入搜索关键词")
            return
        if not self.client:
            self.load_config_only()
        if not self.client:
            return
        self._run_background(self._search_task, keyword)

    def browse_download_dir(self) -> None:
        initial_dir = self.download_dir_var.get().strip() or str(Path("downloads"))
        selected = filedialog.askdirectory(title="选择默认下载目录", initialdir=initial_dir)
        if not selected:
            return
        self.download_dir_var.set(selected)
        # 取当前 GUI 实际加载的配置文件路径（不硬编码 config.json，避免写错到其他文件）
        config_path_text = self.config_var.get().strip() or "config.json"
        # 询问是否同时持久化到配置文件（最小改动：GUI 直接调用 config 的写回函数，不走 AppService）
        persist = messagebox.askyesno(
            "保存默认下载目录",
            f"是否同时将默认下载目录保存到配置文件（{config_path_text}）？\n\n"
            f"选择“是”：写入 {config_path_text}，下次启动自动生效。\n"
            "选择“否”：仅本次会话生效。",
        )
        # 内存生效逻辑照常执行（_apply_default_download_dir 会解析目录并返回 Path）
        resolved = self._apply_default_download_dir()
        if persist and resolved is not None:
            try:
                # 写回用户实际加载的那个配置文件，而不是固定工作目录下的 config.json
                save_default_download_dir(Path(config_path_text), resolved)
                self.log(f"默认下载目录已写入配置文件 {config_path_text}：{resolved}")
            except (ConfigError, OSError) as exc:
                self.log(f"写入配置文件失败：{exc}（内存中仍已生效）")
        else:
            self.log(f"默认下载目录仅本次会话生效：{selected}")

    def _apply_default_download_dir(self) -> Path | None:
        download_dir_text = self.download_dir_var.get().strip()
        if not download_dir_text:
            messagebox.showwarning("下载目录为空", "请先设置下载目录")
            return None
        resolved = Path(download_dir_text)
        resolved.mkdir(parents=True, exist_ok=True)
        if self.client:
            self.client.config.download_dir = resolved
        if self.downloader:
            self.downloader.client.config.download_dir = resolved
        return resolved

    def download_selected(self) -> None:
        self._start_selected_download()

    def download_selected_to(self) -> None:
        initial_dir = self.download_dir_var.get().strip() or str(Path("downloads"))
        selected = filedialog.askdirectory(title="选择本次下载目录", initialdir=initial_dir)
        if not selected:
            return
        self._start_selected_download(override_dir=Path(selected))

    def _start_selected_download(self, override_dir: Path | None = None) -> None:
        selected = self.result_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "先选中一个搜索结果")
            return
        if self._active_downloads > 0:
            messagebox.showinfo("提示", "已有下载任务进行中，请等待完成")
            return
        if not self.downloader or not self.client:
            self.load_config_only()
        if not self.downloader or not self.client:
            return
        if self._apply_default_download_dir() is None:
            return
        if self.downloader.segments != self.segments_var.get():
            self.downloader.segments = self.segments_var.get()
        index = self.result_tree.index(selected[0])
        item = self.search_results[index]
        force_restart = self._resolve_force_restart(item)
        if force_restart is None:
            return
        self._run_background(self._download_task, item, force_restart, override_dir)

    def _resolve_force_restart(self, item: MediaItem) -> bool | None:
        if self.force_restart_var.get():
            return True
        if not self.downloader or item.type not in {"Movie", "Episode", "Video"}:
            return False
        target_path = self.downloader._build_target_path(item)
        temp_path = target_path.with_name(target_path.name + ".part")
        has_partial = temp_path.exists() or any(
            temp_path.parent.glob(f"{temp_path.name}.seg*")
        )
        if not has_partial:
            return False
        answer = messagebox.askyesnocancel(
            "检测到断点文件",
            "检测到这个条目已有 .part 断点文件。\n\n"
            "点“是”：删除断点并重新开始，这样当前并发数设置会生效。\n"
            "点“否”：保留断点继续下载，但会走单线程续传。\n"
            "点“取消”：本次不开始下载。",
        )
        if answer is None:
            return None
        return bool(answer)

    def _pause_download(self) -> None:
        if self.downloader and self._active_downloads > 0:
            self.downloader.pause()
            self.event_queue.put(("download_controls", {"active": True, "paused": True}))

    def _resume_download(self) -> None:
        if self.downloader and self._active_downloads > 0:
            self.downloader.resume()
            self.event_queue.put(("download_controls", {"active": True, "paused": False}))

    def _cancel_download(self) -> None:
        if not self.downloader or self._active_downloads <= 0 or not self._current_download_item:
            return
        cleanup = messagebox.askyesno(
            "取消下载",
            "是否同时删除临时文件（.part）？\n选择“否”可保留断点，之后用“继续下载”恢复。",
        )
        self.downloader.cancel(cleanup_temp=cleanup)
        self.event_queue.put(("download_controls", {"active": False}))

    def _update_control_buttons(self, active: bool = True, paused: bool = False) -> None:
        if not self._pause_btn or not self._resume_btn or not self._cancel_btn:
            return
        if not active:
            self._pause_btn.configure(state="disabled")
            self._resume_btn.configure(state="disabled")
            self._cancel_btn.configure(state="disabled")
        elif paused:
            self._pause_btn.configure(state="disabled")
            self._resume_btn.configure(state="normal")
            self._cancel_btn.configure(state="normal")
        else:
            self._pause_btn.configure(state="normal")
            self._resume_btn.configure(state="disabled")
            self._cancel_btn.configure(state="normal")

    def refresh_downloads(self) -> None:
        if not self.library:
            return
        selected_record = self._selected_download_record()
        selected_item_id = selected_record.item_id if selected_record else None
        self.download_tree.delete(*self.download_tree.get_children())
        for record in self.library.list_all():
            progress = self._format_record_progress(record.bytes_downloaded, record.expected_size)
            mode = record.download_mode or "-"
            self.download_tree.insert(
                "",
                "end",
                iid=record.item_id,
                values=(record.status, record.item_name, progress, mode, record.target_path),
            )
        if selected_item_id and self.download_tree.exists(selected_item_id):
            self.download_tree.selection_set(selected_item_id)
            self.download_tree.focus(selected_item_id)
        self._update_download_record_buttons()

    def _selected_download_record(self) -> DownloadRecord | None:
        if not self.library:
            return None
        selected = self.download_tree.selection()
        if not selected:
            return None
        return self.library.get(str(selected[0]))

    def _on_download_record_select(self, _event=None) -> None:
        self._update_download_record_buttons()

    def _select_download_record_at_event(self, event) -> DownloadRecord | None:
        row_id = self.download_tree.identify_row(event.y)
        if row_id:
            self.download_tree.selection_set(row_id)
            self.download_tree.focus(row_id)
        return self._selected_download_record()

    def _show_download_record_menu(self, event) -> str | None:
        record = self._select_download_record_at_event(event)
        if not record:
            return None
        menu = tk.Menu(self.root, tearoff=0)
        if record.status not in {"completed", "cancelled"}:
            menu.add_command(label="继续下载", command=self.resume_selected_record)
        if record.status == "completed":
            menu.add_command(label="播放", command=self.play_selected_record)
            menu.add_command(label="打开所在目录", command=self.open_selected_record_dir)
            menu.add_separator()
            menu.add_command(label="删除", command=self.delete_selected_record)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _update_download_record_buttons(self) -> None:
        if not self._record_resume_btn or not self._record_play_btn or not self._record_delete_btn:
            return
        record = self._selected_download_record()
        if not record:
            self._record_resume_btn.configure(state="disabled")
            self._record_play_btn.configure(state="disabled")
            self._record_delete_btn.configure(state="disabled")
            return
        resumable = record.status not in {"completed", "cancelled"}
        playable = record.status == "completed"
        deletable = record.status == "completed"
        self._record_resume_btn.configure(state="normal" if resumable and self._active_downloads <= 0 else "disabled")
        self._record_play_btn.configure(state="normal" if playable else "disabled")
        self._record_delete_btn.configure(state="normal" if deletable else "disabled")

    def resume_selected_record(self) -> None:
        record = self._selected_download_record()
        if not record:
            messagebox.showinfo("提示", "先选中一条下载记录")
            return
        if record.status in {"completed", "cancelled"}:
            messagebox.showinfo("提示", "这条记录不需要继续下载")
            return
        if self._active_downloads > 0:
            messagebox.showinfo("提示", "已有下载任务进行中，请等待完成")
            return
        if not self.client or not self.downloader:
            self.load_config_only()
        if not self.client or not self.downloader:
            return
        self._run_background(self._resume_download_record_task, record.item_id)

    def play_selected_record(self) -> None:
        record = self._selected_download_record()
        if not record:
            messagebox.showinfo("提示", "先选中一条下载记录")
            return
        path = Path(record.target_path)
        if not path.exists():
            self.log(f"播放失败：文件不存在 -> {path}")
            messagebox.showwarning("文件不存在", f"文件不存在：\n{path}\n\n你可以删除这条记录。")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            self.log(f"已打开文件：{path}")
        except OSError as exc:
            self.log(f"播放失败：{exc}")
            messagebox.showerror("播放失败", str(exc))

    def open_selected_record_dir(self) -> None:
        record = self._selected_download_record()
        if not record:
            messagebox.showinfo("提示", "先选中一条下载记录")
            return
        path = Path(record.target_path)
        folder = path.parent
        if not folder.exists():
            self.log(f"打开目录失败：目录不存在 -> {folder}")
            messagebox.showwarning("目录不存在", f"目录不存在：\n{folder}")
            return
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
            self.log(f"已打开目录：{folder}")
        except OSError as exc:
            self.log(f"打开目录失败：{exc}")
            messagebox.showerror("打开目录失败", str(exc))

    def delete_selected_record(self) -> None:
        record = self._selected_download_record()
        if not record:
            messagebox.showinfo("提示", "先选中一条下载记录")
            return
        path = Path(record.target_path)
        confirm = messagebox.askyesno(
            "删除下载记录",
            f"将删除本地文件并移除下载记录：\n\n{path}\n\n是否继续？",
        )
        if not confirm:
            return
        try:
            file_removed = False
            if path.exists():
                path.unlink()
                file_removed = True
            temp_path = path.with_name(path.name + ".part")
            temp_path.unlink(missing_ok=True)
            for seg_path in temp_path.parent.glob(f"{temp_path.name}.seg*"):
                seg_path.unlink(missing_ok=True)
            assert self.library is not None
            self.library.delete(record.item_id)
            self.refresh_downloads()
            if file_removed:
                self.log(f"已删除文件和记录：{path}")
            else:
                self.log(f"文件已不存在，仅移除记录：{path}")
        except OSError as exc:
            self.log(f"删除失败：{exc}")
            messagebox.showerror("删除失败", str(exc))

    def _login_task(self) -> None:
        assert self.client is not None
        user = self.client.authenticate()
        self.event_queue.put(("login_ok", user))

    def _search_task(self, keyword: str) -> None:
        assert self.client is not None
        self.client.authenticate()
        results = self.client.search_items(keyword, limit=50)
        self.event_queue.put(("search_ok", results))

    def _resume_download_record_task(self, item_id: str) -> None:
        assert self.client is not None
        assert self.downloader is not None
        self.event_queue.put(("download_start", None))
        self.event_queue.put(("download_controls", {"active": True, "paused": False}))
        try:
            self.client.authenticate()
            item = self.client.get_item(item_id)
            self._current_download_item = item
            self.event_queue.put(("log", f"从下载记录继续：{item.name}"))
            path = self.downloader.download_item(item)
            if self.downloader.last_result_status == "skipped":
                self.event_queue.put(("log", f"已存在，跳过下载：{path}"))
            else:
                self.event_queue.put(("log", f"已保存：{path}"))
            self.event_queue.put(("downloads_refresh", None))
        except DownloadCancelled:
            self.event_queue.put(("log", f"已取消：{item_id}"))
        finally:
            self._current_download_item = None
            self.event_queue.put(("download_controls", {"active": False}))
            self.event_queue.put(("download_end", None))

    def _download_task(
        self,
        item: MediaItem,
        force_restart: bool = False,
        override_dir: Path | None = None,
    ) -> None:
        assert self.client is not None
        assert self.downloader is not None
        self._current_download_item = item
        original_download_dir = self.client.config.download_dir
        self.event_queue.put(("download_start", None))
        self.event_queue.put(("download_controls", {"active": True, "paused": False}))
        try:
            if override_dir is not None:
                override_dir.mkdir(parents=True, exist_ok=True)
                self.client.config.download_dir = override_dir
                self.downloader.client.config.download_dir = override_dir
            self.client.authenticate()
            items = self.client.expand_download_items(item.item_id)
            self.event_queue.put(("log", f"准备下载 {len(items)} 个条目：{item.name}"))
            if override_dir is not None:
                self.event_queue.put(("log", f"本次下载目录：{override_dir}"))
            if force_restart:
                self.event_queue.put(("log", "已启用“忽略 .part 重新开始”，本次会删除断点后重新下载"))
            for sub_item in items:
                try:
                    path = self.downloader.download_item(sub_item, force_restart=force_restart)
                    if self.downloader.last_result_status == "skipped":
                        self.event_queue.put(("log", f"已存在，跳过下载：{path}"))
                    else:
                        self.event_queue.put(("log", f"已保存：{path}"))
                except DownloadCancelled:
                    self.event_queue.put(("log", f"已取消：{sub_item.name}"))
                    break
            self.event_queue.put(("downloads_refresh", None))
        finally:
            self.client.config.download_dir = original_download_dir
            self.downloader.client.config.download_dir = original_download_dir
            self._current_download_item = None
            self.event_queue.put(("download_controls", {"active": False}))
            self.event_queue.put(("download_end", None))

    def _download_progress(
        self,
        status: str,
        item: MediaItem,
        downloaded: int,
        expected: int | None,
        path: Path,
        detail: str | None = None,
    ) -> None:
        if status == "starting":
            self._speed_history[item.item_id] = []
            self._download_state[item.item_id] = {
                "name": item.name,
                "mode": "准备中",
                "downloaded": downloaded,
                "expected": expected,
                "speed_text": "--",
                "eta_text": "--",
                "bytes_text": self._format_bytes_line(downloaded, expected),
            }
            text = f"开始下载：{item.name} -> {path}"
        elif status == "progress":
            speed_text, eta_text = self._calc_speed_and_eta(item.item_id, downloaded, expected)
            state = self._download_state.setdefault(item.item_id, {"name": item.name, "mode": "下载中"})
            state["downloaded"] = downloaded
            state["expected"] = expected
            state["speed_text"] = speed_text or "--"
            state["eta_text"] = eta_text or "--"
            state["bytes_text"] = self._format_bytes_line(downloaded, expected)
            if expected:
                pct = downloaded / expected * 100
                text = f"下载中：{item.name} {downloaded}/{expected} ({pct:.1f}%) {speed_text or ''} {eta_text or ''}".strip()
            else:
                text = f"下载中：{item.name} {downloaded} {speed_text or ''}".strip()
        elif status == "skipped":
            self._speed_history.pop(item.item_id, None)
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = detail or "已存在，跳过"
            state["downloaded"] = downloaded
            state["expected"] = expected
            state["speed_text"] = "--"
            state["eta_text"] = "已跳过"
            state["bytes_text"] = self._format_bytes_line(downloaded, expected)
            text = f"已存在，跳过下载：{item.name}"
        elif status == "mode":
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = detail or "未知模式"
            state["downloaded"] = downloaded
            state["expected"] = expected
            state.setdefault("speed_text", "--")
            state.setdefault("eta_text", "--")
            state["bytes_text"] = self._format_bytes_line(downloaded, expected)
            text = f"下载模式：{item.name} -> {detail or '未知模式'}"
        elif status == "paused":
            self._speed_history.pop(item.item_id, None)
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = detail or "已暂停"
            state["downloaded"] = downloaded
            state["expected"] = expected
            state["speed_text"] = "--"
            state["eta_text"] = "已暂停"
            state["bytes_text"] = self._format_bytes_line(downloaded, expected)
            text = f"已暂停：{item.name}"
        elif status == "cancelled":
            self._speed_history.pop(item.item_id, None)
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = detail or "已取消"
            state["downloaded"] = downloaded
            state["expected"] = expected
            state["speed_text"] = "--"
            state["eta_text"] = "已取消"
            state["bytes_text"] = self._format_bytes_line(downloaded, expected)
            text = f"已取消：{item.name}"
        elif status == "fallback":
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = detail or "已回退到单线程"
            text = f"模式回退：{item.name} -> {detail or '已回退到单线程'}"
        elif status == "completed":
            self._speed_history.pop(item.item_id, None)
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = detail or state.get("mode", "下载完成")
            state["downloaded"] = downloaded
            state["expected"] = expected
            state["speed_text"] = "--"
            state["eta_text"] = "完成"
            state["bytes_text"] = self._format_bytes_line(downloaded, expected)
            text = f"下载完成：{item.name} -> {path}"
        elif status == "partial":
            self._speed_history.pop(item.item_id, None)
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = detail or "下载中断"
            state["downloaded"] = downloaded
            state["expected"] = expected
            state["speed_text"] = "--"
            state["eta_text"] = "--"
            state["bytes_text"] = self._format_bytes_line(downloaded, expected)
            text = f"下载中断：{item.name}"
        elif status == "failed":
            self._speed_history.pop(item.item_id, None)
            state = self._download_state.setdefault(item.item_id, {"name": item.name})
            state["mode"] = "下载失败"
            state["speed_text"] = "--"
            state["eta_text"] = "--"
            text = f"下载失败：{item.name}"
        elif status == "candidate_failed":
            text = f"候选地址失败，尝试下一个：{item.name}；原因：{detail or '未知'}"
        elif status == "diagnostic":
            text = f"诊断：{detail or item.name}"
        else:
            text = f"{status}: {item.name}"

        self.event_queue.put(("download_metrics", item.item_id))
        self.event_queue.put(("log", text))
        if status in {"completed", "failed", "partial", "paused", "cancelled", "skipped"}:
            self.event_queue.put(("downloads_refresh", None))

    def _calc_speed_and_eta(self, item_id: str, downloaded: int, expected: int | None) -> tuple[str, str]:
        now = time.time()
        history = self._speed_history.setdefault(item_id, [])
        history.append((now, downloaded))
        cutoff = now - 5.0
        while history and history[0][0] < cutoff:
            history.pop(0)
        if len(history) < 2:
            return "", ""
        first_time, first_bytes = history[0]
        last_time, last_bytes = history[-1]
        delta_bytes = last_bytes - first_bytes
        delta_time = last_time - first_time
        if delta_time <= 0 or delta_bytes <= 0:
            return "", ""
        speed = delta_bytes / delta_time
        speed_text = self._human_speed(speed)
        if expected and downloaded < expected:
            remain = expected - downloaded
            eta_seconds = remain / speed if speed > 0 else 0
            return speed_text, self._human_eta(eta_seconds)
        return speed_text, ""

    def _refresh_download_observer(self, item_id: str) -> None:
        state = self._download_state.get(item_id)
        if not state:
            return
        name = str(state.get("name", item_id))
        mode = str(state.get("mode", "未开始"))
        downloaded = int(state.get("downloaded", 0) or 0)
        expected = state.get("expected")
        speed_text = str(state.get("speed_text", "--"))
        eta_text = str(state.get("eta_text", "--"))
        bytes_text = str(state.get("bytes_text", self._format_bytes_line(downloaded, expected if isinstance(expected, int) else None)))

        self.download_name_var.set(name)
        self.download_mode_var.set(f"模式：{mode}")
        self.download_speed_var.set(f"速度：{speed_text}")
        self.download_eta_var.set(f"剩余：{eta_text}")
        self.download_bytes_var.set(f"进度：{bytes_text}")
        if isinstance(expected, int) and expected > 0:
            self.download_progress_var.set(min(100.0, downloaded / expected * 100))
        else:
            self.download_progress_var.set(0.0)

    def _reset_download_observer(self) -> None:
        self.download_name_var.set("暂无活动下载")
        self.download_mode_var.set("模式：未开始")
        self.download_speed_var.set("速度：--")
        self.download_eta_var.set("剩余：--")
        self.download_bytes_var.set("进度：0 B / --")
        self.download_progress_var.set(0.0)

    def _run_background(self, func, *args) -> None:
        def runner() -> None:
            try:
                func(*args)
            except (ConfigError, EmbyClientError, DownloadError, OSError) as exc:
                self.event_queue.put(("error", str(exc)))

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()

    def _poll_events(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if event == "log":
                self.log(str(payload))
            elif event == "error":
                self.status_var.set("操作失败")
                self.log(f"错误：{payload}")
                messagebox.showerror("错误", str(payload))
            elif event == "login_ok":
                user = payload
                name = user.get("Name", "unknown")
                user_id = user.get("Id", "n/a")
                self.status_var.set(f"已连接：{name} ({user_id})")
                self.log(f"登录成功：{name} ({user_id})")
            elif event == "search_ok":
                results = list(payload)
                self.search_results = results
                self.result_tree.delete(*self.result_tree.get_children())
                for item in results:
                    self.result_tree.insert("", "end", values=(item.item_id, item.type, item.name, item.year or ""))
                self.log(f"搜索完成：{len(results)} 条结果")
            elif event == "downloads_refresh":
                self.refresh_downloads()
            elif event == "download_start":
                self._active_downloads += 1
                self.status_var.set(f"正在下载... (当前 {self._active_downloads} 个任务)")
                self._update_download_record_buttons()
            elif event == "download_end":
                self._active_downloads = max(0, self._active_downloads - 1)
                if self._active_downloads <= 0:
                    self.status_var.set("就绪")
                    self._reset_download_observer()
                else:
                    self.status_var.set(f"正在下载... (当前 {self._active_downloads} 个任务)")
                self._update_download_record_buttons()
            elif event == "download_metrics":
                self._refresh_download_observer(str(payload))
            elif event == "download_controls":
                self._update_control_buttons(
                    active=payload.get("active", False),
                    paused=payload.get("paused", False),
                )

        self.root.after(100, self._poll_events)

    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        try:
            from logger import get_logger
            noisy_prefixes = (
                "诊断：",
                "下载中：",
                "下载完成：",
                "开始下载：",
                "下载模式：",
                "模式回退：",
                "已暂停：",
                "已取消：",
                "已存在，跳过下载：",
            )
            if not message.startswith(noisy_prefixes):
                get_logger().info("[gui] %s", message)
        except Exception:
            pass

    @staticmethod
    def _human_speed(speed: float) -> str:
        if speed >= 1024 * 1024:
            return f"{speed / 1024 / 1024:.2f} MB/s"
        if speed >= 1024:
            return f"{speed / 1024:.1f} KB/s"
        return f"{speed:.0f} B/s"

    @staticmethod
    def _human_size(size: int) -> str:
        if size >= 1024 * 1024 * 1024:
            return f"{size / 1024 / 1024 / 1024:.2f} GB"
        if size >= 1024 * 1024:
            return f"{size / 1024 / 1024:.2f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    @staticmethod
    def _human_eta(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, sec = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m {sec}s"
        if minutes:
            return f"{minutes}m {sec}s"
        return f"{sec}s"

    def _format_bytes_line(self, downloaded: int, expected: int | None) -> str:
        if expected:
            pct = downloaded / expected * 100 if expected else 0
            return f"{self._human_size(downloaded)} / {self._human_size(expected)} ({pct:.1f}%)"
        return f"{self._human_size(downloaded)} / --"

    def _format_record_progress(self, downloaded: int, expected: int | None) -> str:
        if expected:
            return f"{self._human_size(downloaded)} / {self._human_size(expected)}"
        return self._human_size(downloaded)


def main() -> None:
    root = tk.Tk()
    app = EmbyCacheGui(root)
    app.log("GUI 已启动。先加载配置，再测试登录。")
    root.mainloop()


if __name__ == "__main__":
    main()
