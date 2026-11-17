from __future__ import annotations

import concurrent.futures
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import requests
from tqdm import tqdm

from emby_client import EmbyClient, EmbyClientError
from library import CacheLibrary
from logger import get_logger
from models import MediaItem


INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ProgressCallback = Callable[[str, MediaItem, int, int | None, Path, str | None], None]

logger = get_logger()


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(DownloadError):
    """Raised when the user explicitly cancels the active download."""

    pass


class MultipartUnavailable(DownloadError):
    def __init__(self, message: str, fallback_mode: str = "单线程回退（服务端不支持分段）") -> None:
        super().__init__(message)
        self.fallback_mode = fallback_mode


@dataclass(slots=True)
class ProbeResult:
    status_code: int
    total: int | None
    accept_ranges: str
    content_range: str
    final_url: str


class Downloader:
    def __init__(
        self,
        client: EmbyClient,
        library: CacheLibrary,
        progress_callback: ProgressCallback | None = None,
        segments: int = 4,
    ) -> None:
        self.client = client
        self.library = library
        self.progress_callback = progress_callback
        self.segments = max(1, segments)
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self._current_item: MediaItem | None = None
        self._current_temp_path: Path | None = None
        self._current_url: str | None = None
        self._current_mode: str | None = None
        self._cancel_cleanup_temp = False
        self.last_result_status = "idle"
        self._last_progress_db = 0.0
        self._progress_db_interval = 1.0
        self._current_expected: int | None = None

    def pause(self) -> None:
        """Signal the active download to pause."""
        if self._current_item:
            self.pause_event.set()
            logger.info("Pause requested for %s", self._current_item.name)

    def resume(self) -> None:
        """Resume a paused download."""
        self.pause_event.clear()
        logger.info("Resume requested")

    def cancel(self, cleanup_temp: bool = False) -> None:
        """Cancel the active download.

        If ``cleanup_temp`` is True, the temporary files are removed after the
        worker notices the cancellation.
        """
        self.cancel_event.set()
        self.pause_event.clear()
        self._cancel_cleanup_temp = cleanup_temp
        if self._current_item:
            logger.info("Cancel requested for %s (cleanup_temp=%s)", self._current_item.name, cleanup_temp)

    def reset_control_events(self) -> None:
        """Reset pause/cancel events before starting a new download."""
        self.cancel_event.clear()
        self.pause_event.clear()
        self._cancel_cleanup_temp = False
        self.last_result_status = "idle"

    def download_many(self, items: Iterable[MediaItem]) -> None:
        # N7：批量下载单集失败不再中止整批，记录错误后继续
        for item in items:
            try:
                self.download_item(item)
            except DownloadCancelled:
                raise
            except (DownloadError, EmbyClientError, OSError) as exc:
                logger.error("Item failed, continuing batch: %s (%s)", item.name, exc)
                continue

    def download_item(self, item: MediaItem, force_restart: bool = False) -> Path:
        self.reset_control_events()
        target_path = self._build_target_path(item)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.with_name(target_path.name + ".part")
        self._current_item = item
        self._current_temp_path = temp_path
        self._current_url = None
        self._current_mode = "准备中"
        if force_restart:
            self._cleanup_segment_files(temp_path)
            temp_path.unlink(missing_ok=True)
        resume_from = temp_path.stat().st_size if temp_path.exists() else 0

        try:
            existing = self.library.get(item.item_id)
            if target_path.exists() and existing and existing.status == "completed":
                temp_path.unlink(missing_ok=True)
                self._cleanup_segment_files(temp_path)
                self.last_result_status = "skipped"
                logger.info(
                    "Skipping cached file: item_id=%s name=%s target=%s",
                    item.item_id,
                    item.name,
                    target_path,
                )
                self._emit(
                    "skipped",
                    item,
                    target_path.stat().st_size,
                    existing.expected_size,
                    target_path,
                    "目标文件已存在且记录为已完成，跳过重复下载",
                )
                return target_path

            logger.info(
                "Starting download: item_id=%s name=%s target=%s resume_from=%d force_restart=%s",
                item.item_id,
                item.name,
                target_path,
                resume_from,
                force_restart,
            )
            self.library.upsert(item, target_path, "downloading", resume_from, None, None, download_mode="准备中")
            self._emit("starting", item, resume_from, None, target_path)
            try:
                candidates = self.client.get_download_candidates(item)
            except (EmbyClientError, DownloadError, OSError) as exc:
                # 候选地址获取失败也标记 failed，避免记录停留 downloading（审计 §5.5）
                self.last_result_status = "failed"
                self.library.upsert(item, target_path, "failed", resume_from, None, None, download_mode="下载失败")
                self._emit("failed", item, resume_from, None, target_path)
                raise DownloadError(f"无法获取下载候选地址 for {item.name}: {exc}")
            last_error: Exception | None = None
            resume_single_notice = temp_path.exists() and not self._segment_files(temp_path)
            if self.segments > 1 and resume_single_notice:
                self._emit(
                    "fallback",
                    item,
                    resume_from,
                    None,
                    target_path,
                    "检测到已有 .part 断点文件，保留当前续传进度，继续单线程下载（当前并发数设置不会生效）",
                )

            for index, url in enumerate(candidates, start=1):
                candidate_name = self.client.describe_candidate(url)
                logger.info("Trying candidate %d/%d: %s", index, len(candidates), candidate_name)
                self._emit(
                    "diagnostic",
                    item,
                    resume_from,
                    None,
                    target_path,
                    f"尝试候选地址 {index}/{len(candidates)}：{candidate_name}",
                )
                try:
                    result = self._download_from_candidate(
                        item,
                        target_path,
                        temp_path,
                        url,
                        candidate_name,
                        resume_notice_emitted=resume_single_notice,
                    )
                    logger.info(
                        "Download succeeded using candidate %d/%d: %s",
                        index,
                        len(candidates),
                        candidate_name,
                    )
                    self.last_result_status = "completed"
                    return result
                except DownloadCancelled:
                    final_status = "cancelled" if self._cancel_cleanup_temp else "paused"
                    final_mode = "已取消" if self._cancel_cleanup_temp else (self._current_mode or "已暂停")
                    if self._cancel_cleanup_temp:
                        self._cleanup_segment_files(temp_path)
                        temp_path.unlink(missing_ok=True)
                        bytes_downloaded = 0
                        self.last_result_status = "cancelled"
                        self._emit("cancelled", item, 0, None, target_path, final_mode)
                    else:
                        bytes_downloaded = self._paused_downloaded_bytes(temp_path)
                        self.last_result_status = "paused"
                        self._emit("paused", item, bytes_downloaded, None, target_path, final_mode)
                    logger.warning("Download %s for %s", final_status, item.name)
                    self.library.upsert(item, target_path, final_status, bytes_downloaded, None, url, download_mode=final_mode)
                    raise
                except (DownloadError, EmbyClientError, OSError) as exc:
                    last_error = exc
                    if self._is_download_feature_denied(candidate_name, exc):
                        logger.warning("Candidate %s denied: %s", candidate_name, exc)
                        self._emit(
                            "diagnostic",
                            item,
                            resume_from,
                            None,
                            target_path,
                            f"候选地址 {index}/{len(candidates)}：{candidate_name} 因账号缺少 DownloadContent 权限被拒绝，已跳过并继续尝试后续 stream 候选",
                        )
                    else:
                        logger.warning("Candidate %s failed: %s", candidate_name, exc)
                        self._emit("candidate_failed", item, resume_from, None, target_path, str(exc))
                    continue

            logger.error("All candidates failed for %s: %s", item.name, last_error)
            self.last_result_status = "failed"
            self.library.upsert(item, target_path, "failed", resume_from, None, None, download_mode="下载失败")
            self._emit("failed", item, resume_from, None, target_path)
            raise DownloadError(f"All download candidates failed for {item.name}: {last_error}")
        finally:
            self._current_item = None
            self._current_temp_path = None
            self._current_url = None
            self._current_mode = None
            self.cancel_event.clear()
            self.pause_event.clear()
            self._cancel_cleanup_temp = False

    def _download_from_candidate(
        self,
        item: MediaItem,
        target_path: Path,
        temp_path: Path,
        url: str,
        candidate_name: str,
        resume_notice_emitted: bool = False,
    ) -> Path:
        self._current_url = url
        resume_from = temp_path.stat().st_size if temp_path.exists() else 0
        has_seg = bool(self._segment_files(temp_path))
        current_mode = "准备中"
        effective_segments = self._effective_segments_for_candidate(candidate_name)

        if effective_segments > 1:
            if temp_path.exists() and not has_seg:
                current_mode = "单线程续传（保留已有 .part）"
                self.library.upsert(item, target_path, "downloading", resume_from, None, url, download_mode=current_mode)
                if not resume_notice_emitted:
                    self._emit(
                        "fallback",
                        item,
                        resume_from,
                        None,
                        target_path,
                        "检测到已有 .part 断点文件，保留当前续传进度，继续单线程下载（当前并发数设置不会生效）",
                    )
            else:
                try:
                    if effective_segments != self.segments:
                        self._emit(
                            "diagnostic",
                            item,
                            resume_from,
                            None,
                            target_path,
                            f"{candidate_name} 默认最多使用 {effective_segments} 段，已从设置的 {self.segments} 段自动降级",
                        )
                    result = self._try_multipart(item, target_path, temp_path, url, candidate_name, effective_segments)
                    if result is not None:
                        return result
                except MultipartUnavailable as exc:
                    current_mode = exc.fallback_mode
                    self.library.upsert(item, target_path, "downloading", resume_from, None, url, download_mode=current_mode)
                    self._emit("fallback", item, resume_from, None, target_path, str(exc))
                except (DownloadError, EmbyClientError, OSError) as exc:
                    # N2：分段已有进度时绝不清零回退——保留 .seg 断点，交给后续候选/下次续传
                    if self._segment_files(temp_path):
                        logger.warning("Multipart failed but segment progress exists; preserving .seg for resume: %s", exc)
                        raise
                    current_mode = "单线程回退（分段失败）"
                    self.library.upsert(item, target_path, "downloading", resume_from, None, url, download_mode=current_mode)
                    self._emit("fallback", item, resume_from, None, target_path, f"分段下载失败，回退单线程：{exc}")

        # N2：任何回退单线程的路径都不得清掉已有分段断点（含 MultipartUnavailable 分支）
        if self._segment_files(temp_path):
            raise DownloadError("存在分段断点，保留 .seg 进度，本候选不回退单线程")
        self._cleanup_segment_files(temp_path)
        resume_from = temp_path.stat().st_size if temp_path.exists() else 0
        if effective_segments <= 1:
            mode_detail = "单线程下载（并发数为 1）"
        elif current_mode.startswith("单线程"):
            mode_detail = current_mode
        else:
            mode_detail = "单线程下载"
        self._current_mode = mode_detail
        self.library.upsert(item, target_path, "downloading", resume_from, None, url, download_mode=mode_detail)
        self._emit("mode", item, resume_from, None, target_path, mode_detail)

        prior = self.library.get(item.item_id)
        prior_etag = prior.etag if prior else None
        response = self.client.open_stream(url, resume_from=resume_from, if_range=prior_etag if resume_from else None)
        resp_etag = response.headers.get("ETag")

        logger.info("Open stream response: HTTP %s, Content-Length=%s, Accept-Ranges=%s",
                    response.status_code,
                    response.headers.get("Content-Length", "-"),
                    response.headers.get("Accept-Ranges", "-"))

        def _restart_from_scratch(reason: str):
            nonlocal resume_from, response, resp_etag
            self._emit("diagnostic", item, resume_from, None, target_path, reason)
            logger.warning("%s for %s, restarting from scratch", reason, item.name)
            temp_path.unlink(missing_ok=True)
            resume_from = 0
            response.close()
            response = self.client.open_stream(url, resume_from=0)
            resp_etag = response.headers.get("ETag")

        if resume_from and response.status_code == 200:
            _restart_from_scratch("续传请求返回 HTTP 200，服务端未接受续传，已自动从头重下")
        elif resume_from and response.status_code == 206:
            # N4：校验 206 的 Content-Range 起始偏移必须等于续传点，否则追加会损坏文件
            parsed = _parse_content_range(response.headers.get("Content-Range", ""))
            if parsed is None or parsed[0] != resume_from:
                _restart_from_scratch("续传返回的 Content-Range 起始偏移与断点不一致，已自动从头重下")

        # N16：续传返回的 ETag 与历史不一致 → 源已变更（即使尺寸相同），从头重下
        if resume_from and resp_etag and prior_etag and resp_etag != prior_etag:
            _restart_from_scratch("续传返回的 ETag 与历史记录不一致（源已变更），已自动从头重下")

        expected_size = _expected_size(response, resume_from)
        # N16（部分）：续传时服务端总大小与历史记录不一致 → 源已变更，从头重下
        if resume_from and expected_size is not None:
            prior = self.library.get(item.item_id)
            if prior and prior.expected_size and prior.expected_size != expected_size:
                _restart_from_scratch("服务端文件总大小与历史断点记录不一致（源可能已变更），已自动从头重下")
                expected_size = _expected_size(response, resume_from)
        self._current_expected = expected_size
        if expected_size is not None:
            self._check_disk_space(target_path.parent, expected_size, resume_from, multipart=False)
            self.library.upsert(item, target_path, "downloading", resume_from, expected_size, url, download_mode=mode_detail, etag=resp_etag)
        write_mode = "ab" if resume_from else "wb"
        use_tqdm = self.progress_callback is None
        progress = (
            tqdm(
                total=expected_size,
                initial=resume_from,
                unit="B",
                unit_scale=True,
                desc=item.name[:40],
            )
            if use_tqdm
            else _NullProgress()
        )

        slow_probe_start: float | None = None
        slow_probe_bytes: int = resume_from
        with response, temp_path.open(write_mode) as handle, progress as progress_bar:
            for chunk in response.iter_content(chunk_size=self.client.config.chunk_size):
                self._check_cancelled()
                self._wait_while_paused(item, target_path)
                if not chunk:
                    continue
                handle.write(chunk)
                progress_bar.update(len(chunk))
                current = handle.tell()
                self._maybe_upsert_progress(item, target_path, current, expected_size, url, mode_detail)
                self._emit("progress", item, current, expected_size, target_path)
                slow_probe_start, slow_probe_bytes = self._detect_slow_speed(
                    item, current, slow_probe_start, slow_probe_bytes, mode_detail,
                    threshold_bps=self.client.config.slow_threshold_kbps * 1024,
                    window_seconds=self.client.config.slow_window_seconds,
                )

        self._check_cancelled()

        final_size = temp_path.stat().st_size
        if expected_size is not None and final_size != expected_size:
            self.library.upsert(item, target_path, "partial", final_size, expected_size, url, download_mode=mode_detail)
            self._emit("partial", item, final_size, expected_size, target_path)
            raise DownloadError(
                f"Download stopped early for {item.name}. Expected {expected_size} bytes, got {final_size}."
            )

        self._cleanup_segment_files(temp_path)
        temp_path.replace(target_path)
        self.library.upsert(item, target_path, "completed", final_size, expected_size, url, download_mode=mode_detail, etag=resp_etag)
        self._emit("completed", item, final_size, expected_size, target_path, mode_detail)
        return target_path

    def _try_multipart(
        self,
        item: MediaItem,
        target_path: Path,
        temp_path: Path,
        url: str,
        candidate_name: str,
        segment_limit: int,
    ) -> Path | None:
        if segment_limit <= 1:
            raise MultipartUnavailable("并发段数为 1，跳过分段下载")

        probe = self._probe_range_support(item, target_path, url, candidate_name)
        if probe.status_code == 206 and probe.total is not None:
            return self._download_segments(
                item,
                target_path,
                temp_path,
                url,
                probe.total,
                f"分段并发下载（{min(segment_limit, probe.total)} 段)",
                segment_limit,
            )

        if not self.client.config.experimental_force_multipart:
            raise MultipartUnavailable(
                f"标准 Range 探测未通过：HTTP {probe.status_code}；Accept-Ranges={probe.accept_ranges or '-'}；Content-Range={probe.content_range or '-'}"
            )

        forced_total = probe.total or self._probe_total_size(item, target_path, url, candidate_name)
        if forced_total is None or forced_total <= 1:
            raise MultipartUnavailable(
                "标准探测失败，且实验模式无法确认可分段的总大小，回退单线程",
                fallback_mode="单线程回退（实验性分段失败）",
            )

        validated_total = self._probe_forced_multipart(item, target_path, url, candidate_name, forced_total)
        return self._download_segments(
            item,
            target_path,
            temp_path,
            url,
            validated_total,
            f"实验性强制分段下载（{min(segment_limit, validated_total)} 段)",
            segment_limit,
        )

    def _probe_range_support(self, item: MediaItem, target_path: Path, url: str, candidate_name: str) -> ProbeResult:
        response = self.client.session.get(
            url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            timeout=self.client.config.timeout,
            allow_redirects=True,
        )
        try:
            result = ProbeResult(
                status_code=response.status_code,
                total=_expected_size(response, 0),
                accept_ranges=response.headers.get("Accept-Ranges", ""),
                content_range=response.headers.get("Content-Range", ""),
                final_url=response.url,
            )
        finally:
            response.close()

        detail = (
            f"标准 Range 探测：{candidate_name} -> HTTP {result.status_code}"
            f"；Accept-Ranges={result.accept_ranges or '-'}"
            f"；Content-Range={result.content_range or '-'}"
        )
        logger.info(detail)
        self._emit("diagnostic", item, 0, result.total, target_path, detail)
        return result

    def _probe_total_size(self, item: MediaItem, target_path: Path, url: str, candidate_name: str) -> int | None:
        response = self.client.session.get(
            url,
            stream=True,
            timeout=self.client.config.timeout,
            allow_redirects=True,
        )
        try:
            total = _expected_size(response, 0)
            detail = (
                f"实验模式总大小探测：{candidate_name} -> HTTP {response.status_code}"
                f"；Content-Length={response.headers.get('Content-Length', '-') }"
                f"；Content-Range={response.headers.get('Content-Range', '-') }"
            )
            logger.info(detail)
            self._emit("diagnostic", item, 0, total, target_path, detail)
            if response.status_code not in {200, 206}:
                return None
            return total
        finally:
            response.close()

    def _probe_forced_multipart(
        self,
        item: MediaItem,
        target_path: Path,
        url: str,
        candidate_name: str,
        total: int,
    ) -> int:
        probe_ranges = self._multipart_plan_from_total(total, min(2, total))
        if len(probe_ranges) < 2:
            raise MultipartUnavailable(
                "文件过小，不适合实验性强制分段",
                fallback_mode="单线程回退（实验性分段失败）",
            )

        for idx, start, end in probe_ranges[:2]:
            response = self.client.session.get(
                url,
                headers={"Range": f"bytes={start}-{end}"},
                stream=True,
                timeout=self.client.config.timeout,
                allow_redirects=True,
            )
            try:
                content_range = response.headers.get("Content-Range", "")
                detail = (
                    f"实验分段探测：{candidate_name} bytes={start}-{end} -> HTTP {response.status_code}"
                    f"；Content-Range={content_range or '-'}"
                )
                logger.info(detail)
                self._emit("diagnostic", item, 0, total, target_path, detail)
                if response.status_code != 206:
                    raise MultipartUnavailable(
                        f"实验分段探测失败：bytes={start}-{end} 返回 HTTP {response.status_code}",
                        fallback_mode="单线程回退（实验性分段失败）",
                    )
                parsed = _parse_content_range(content_range)
                if parsed is None:
                    raise MultipartUnavailable(
                        f"实验分段探测失败：bytes={start}-{end} 缺少合法 Content-Range",
                        fallback_mode="单线程回退（实验性分段失败）",
                    )
                actual_start, actual_end, actual_total = parsed
                if (actual_start, actual_end) != (start, end) or actual_total != total:
                    raise MultipartUnavailable(
                        "实验分段探测失败：服务端返回的 Content-Range 与请求区间不一致",
                        fallback_mode="单线程回退（实验性分段失败）",
                    )
            finally:
                response.close()
        return total

    def _download_segments(
        self,
        item: MediaItem,
        target_path: Path,
        temp_path: Path,
        url: str,
        total: int,
        mode_prefix: str,
        segment_limit: int,
    ) -> Path:
        if total <= 1:
            raise MultipartUnavailable("文件过小，不适合分段下载")

        num_segments = max(1, min(segment_limit, total))
        if num_segments <= 1:
            raise MultipartUnavailable("文件过小，不适合分段下载")

        if temp_path.exists():
            temp_path.unlink(missing_ok=True)

        lock = threading.Lock()
        errors: list[Exception] = []
        shutdown = threading.Event()
        segment_specs: list[tuple[int, int, int, Path, int]] = []
        downloaded_bytes = 0
        mode_detail = mode_prefix

        for idx, start, end in self._multipart_plan_from_total(total, num_segments):
            seg_temp = temp_path.with_suffix(f".part.seg{idx}")
            seg_length = end - start + 1
            seg_resume = seg_temp.stat().st_size if seg_temp.exists() else 0
            if seg_resume > seg_length:
                seg_temp.unlink(missing_ok=True)
                seg_resume = 0
            downloaded_bytes += seg_resume
            segment_specs.append((idx, start, end, seg_temp, seg_resume))

        self._current_mode = mode_detail
        self._current_expected = total
        self.library.upsert(item, target_path, "downloading", downloaded_bytes, total, url, download_mode=mode_detail)

        def emit_progress() -> None:
            with lock:
                current = downloaded_bytes
            self._maybe_upsert_progress(item, target_path, current, total, url, mode_detail)
            self._emit("progress", item, current, total, target_path)

        def download_segment(idx: int, start: int, end: int, seg_temp: Path, seg_resume: int) -> None:
            nonlocal downloaded_bytes
            if shutdown.is_set() or self.cancel_event.is_set():
                shutdown.set()
                return

            max_attempts = max(1, self.client.config.retry_attempts)
            attempts = 0
            while True:
                if shutdown.is_set() or self.cancel_event.is_set():
                    return
                # 每次重试都从 seg 文件当前大小续传，保留已下字节
                current_size = seg_temp.stat().st_size if seg_temp.exists() else 0
                actual_start = start + current_size
                if actual_start > end:
                    return
                headers = {"Range": f"bytes={actual_start}-{end}"}
                response = None
                try:
                    response = self.client.session.get(
                        url,
                        headers=headers,
                        stream=True,
                        timeout=self.client.config.timeout,
                        allow_redirects=True,
                    )
                    if response.status_code != 206:
                        raise EmbyClientError(f"Segment {idx} HTTP {response.status_code}")
                    content_range = response.headers.get("Content-Range", "")
                    parsed = _parse_content_range(content_range)
                    if parsed is None:
                        raise EmbyClientError(f"Segment {idx} 缺少合法 Content-Range")
                    returned_start, returned_end, returned_total = parsed
                    if (returned_start, returned_end) != (actual_start, end) or returned_total != total:
                        raise EmbyClientError(f"Segment {idx} Content-Range mismatch: {content_range}")
                    with seg_temp.open("ab") as handle:
                        for chunk in response.iter_content(chunk_size=self.client.config.chunk_size):
                            self._check_cancelled()
                            if shutdown.is_set():
                                break
                            # N20：暂停循环同时检查 shutdown，避免失败任务卡死在暂停里
                            while self.pause_event.is_set() and not self.cancel_event.is_set() and not shutdown.is_set():
                                time.sleep(0.2)
                            if self.cancel_event.is_set():
                                raise DownloadCancelled("用户取消下载")
                            if not chunk:
                                continue
                            handle.write(chunk)
                            with lock:
                                downloaded_bytes += len(chunk)
                            emit_progress()
                    return  # 本段完成
                except DownloadCancelled:
                    shutdown.set()
                    return
                except (requests.RequestException, EmbyClientError, OSError) as exc:
                    # N11：段级指数退避重试，单次网络抖动不再废掉整个任务
                    attempts += 1
                    if attempts >= max_attempts or shutdown.is_set():
                        with lock:
                            errors.append(exc)
                        shutdown.set()
                        return
                    backoff = min(self.client.config.retry_backoff_max, self.client.config.retry_backoff_base * (2 ** (attempts - 1)))
                    logger.warning("Segment %s attempt %s failed (%s); retry in %.1fs", idx, attempts, exc, backoff)
                    deadline = time.monotonic() + backoff
                    while time.monotonic() < deadline:
                        if shutdown.is_set() or self.cancel_event.is_set():
                            return
                        time.sleep(0.1)
                finally:
                    if response is not None:
                        response.close()

        self._emit("mode", item, downloaded_bytes, total, target_path, mode_detail)
        if downloaded_bytes:
            emit_progress()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_segments) as executor:
            futures = [
                executor.submit(download_segment, idx, start, end, seg_temp, seg_resume)
                for idx, start, end, seg_temp, seg_resume in segment_specs
            ]
            concurrent.futures.wait(futures)

        self._check_cancelled()

        if errors:
            raise DownloadError(f"Multi-part download failed: {errors[0]}")

        actual_total = sum(seg_temp.stat().st_size for _, _, _, seg_temp, _ in segment_specs)
        if actual_total != total:
            raise DownloadError(f"Multi-part incomplete: {actual_total}/{total} bytes")

        with temp_path.open("wb") as out:
            for _, _, _, seg_temp, _ in segment_specs:
                with seg_temp.open("rb") as src:
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
                seg_temp.unlink(missing_ok=True)

        self._cleanup_segment_files(temp_path)
        temp_path.replace(target_path)
        self.library.upsert(item, target_path, "completed", total, total, url, download_mode=mode_detail)
        self._emit("completed", item, total, total, target_path, mode_detail)
        return target_path

    def _multipart_plan_from_total(self, total: int, num_segments: int) -> list[tuple[int, int, int]]:
        num_segments = max(1, min(num_segments, total))
        seg_size = total // num_segments
        plans: list[tuple[int, int, int]] = []
        for idx in range(num_segments):
            start = idx * seg_size
            end = (start + seg_size - 1) if idx < num_segments - 1 else (total - 1)
            plans.append((idx, start, end))
        return plans

    def _cleanup_segment_files(self, temp_path: Path) -> None:
        for seg_path in self._segment_files(temp_path):
            seg_path.unlink(missing_ok=True)

    def _segment_files(self, temp_path: Path) -> list[Path]:
        # N9：用 iterdir+startswith 替代 glob，避免标题中的 [ ] 等元字符导致匹配失败
        prefix = temp_path.name + ".seg"
        parent = temp_path.parent
        if not parent.exists():
            return []
        return sorted(p for p in parent.iterdir() if p.is_file() and p.name.startswith(prefix))

    def _maybe_upsert_progress(self, item, target_path, downloaded, expected, url, mode, force=False):
        # N10：进度写库节流（≥1s），并把 DB 异常与下载解耦——写库失败只告警，不杀任务
        now = time.monotonic()
        if not force and now - self._last_progress_db < self._progress_db_interval:
            return
        self._last_progress_db = now
        try:
            self.library.upsert(item, target_path, "downloading", downloaded, expected, url, download_mode=mode)
        except Exception as exc:
            logger.warning("Progress DB write failed (non-fatal): %s", exc)

    def _check_disk_space(self, folder: Path, expected_size: int, resume_from: int, multipart: bool):
        # N11：下载前磁盘空间预检；合并架构需要约 2× 剩余空间
        try:
            folder.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(folder).free
        except OSError:
            return
        remaining = max(0, expected_size - resume_from)
        required = remaining * 2 if multipart else remaining
        if free < required:
            raise DownloadError(
                f"磁盘空间不足：需要约 {required} 字节，剩余 {free} 字节（目录 {folder}）"
            )

    def _effective_segments_for_candidate(self, candidate_name: str) -> int:
        if candidate_name.startswith("Videos/stream"):
            return max(1, min(self.segments, self.client.config.stream_segments))
        return self.segments

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise DownloadCancelled("用户取消下载")

    def _paused_downloaded_bytes(self, temp_path: Path) -> int:
        total = temp_path.stat().st_size if temp_path.exists() else 0
        for seg_path in self._segment_files(temp_path):
            total += seg_path.stat().st_size
        return total

    def _wait_while_paused(self, item: MediaItem, target_path: Path) -> None:
        if not self.pause_event.is_set():
            return
        paused_bytes = self._paused_downloaded_bytes(self._current_temp_path or target_path.with_name(target_path.name + ".part"))
        self.library.upsert(
            item,
            target_path,
            "paused",
            paused_bytes,
            self._current_expected,
            self._current_url,
            download_mode=self._current_mode or "已暂停",
        )
        self._emit("paused", item, paused_bytes, None, target_path, "已暂停")
        logger.info("Download paused for %s, waiting for resume", item.name)
        while self.pause_event.is_set() and not self.cancel_event.is_set():
            time.sleep(0.2)
        if self.cancel_event.is_set():
            raise DownloadCancelled("用户取消下载")
        self.library.upsert(
            item,
            target_path,
            "downloading",
            paused_bytes,
            self._current_expected,
            self._current_url,
            download_mode=self._current_mode or "继续下载",
        )
        self._emit("mode", item, paused_bytes, None, target_path, self._current_mode or "继续下载")
        logger.info("Download resumed for %s", item.name)

    def _detect_slow_speed(
        self,
        item: MediaItem,
        current: int,
        start_time: float | None,
        start_bytes: int,
        mode: str,
        threshold_bps: float = 50 * 1024,
        window_seconds: float = 30.0,
    ) -> tuple[float | None, int]:
        now = time.time()
        if start_time is None:
            return now, current
        elapsed = now - start_time
        if elapsed < window_seconds:
            return start_time, start_bytes
        delta = current - start_bytes
        speed = delta / elapsed if elapsed > 0 else 0.0
        if speed < threshold_bps:
            logger.warning(
                "慢速告警：%s 在 %.0f 秒内仅下载 %d 字节（%.2f KB/s），模式=%s",
                item.name, elapsed, delta, speed / 1024, mode
            )
        # Move the window forward regardless of alarm so we don't spam.
        return now, current

    def _is_download_feature_denied(self, candidate_name: str, exc: Exception) -> bool:
        if not candidate_name.startswith("Items/Download"):
            return False
        text = str(exc)
        return "HTTP 403" in text and "DownloadContent" in text

    def _build_target_path(self, item: MediaItem) -> Path:
        ext = "." + _safe_ext(item.container)
        base_dir = self.client.config.download_dir

        if item.type == "Movie":
            title = _sanitize(_movie_title(item))
            path = base_dir / title / f"{title}{ext}"
        elif item.type == "Episode":
            series = _sanitize(item.series_name or "Unknown Series")
            season_no = item.parent_index_number or 0
            episode_no = item.episode_number if item.episode_number is not None else 0
            season_dir = f"Season {season_no:02d}"
            name_part = _sanitize(item.name)
            if item.episode_number is None:
                # 缺少 IndexNumber 时用 item_id 兜底，避免多集碰撞覆盖 S01E00
                name_part = f"{name_part} [{_sanitize(item.item_id)}]"
            filename = f"S{season_no:02d}E{episode_no:02d} - {name_part}{ext}"
            path = base_dir / series / season_dir / filename
        else:
            path = base_dir / f"{_sanitize(item.name)}{ext}"

        # N1：强制目标路径必须落在下载目录内，封死服务端可控 container/name 的路径穿越
        base_resolved = base_dir.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(base_resolved):
            raise DownloadError(f"已拒绝不安全的下载路径（超出下载目录）：{item.name}")
        return resolved

    def _emit(
        self,
        status: str,
        item: MediaItem,
        downloaded: int,
        expected: int | None,
        path: Path,
        detail: str | None = None,
    ) -> None:
        if status in {"diagnostic", "fallback", "mode", "candidate_failed"} and detail:
            logger.info("[%s] %s", status, detail)
        elif status == "skipped":
            logger.info("Download skipped (already cached): %s", item.name)
        elif status == "paused":
            logger.warning("Download paused: %s", item.name)
        elif status == "cancelled":
            logger.warning("Download cancelled: %s", item.name)
        elif status == "completed":
            logger.info("Download completed: %s -> %s (%s bytes)", item.name, path, downloaded)
        elif status == "failed":
            logger.error("Download failed: %s", item.name)
        elif status == "partial":
            logger.warning("Download interrupted: %s", item.name)
        if self.progress_callback:
            self.progress_callback(status, item, downloaded, expected, path, detail)
            return
        message = self._cli_message(status, item, path, detail)
        if message:
            tqdm.write(message)

    def _cli_message(self, status: str, item: MediaItem, path: Path, detail: str | None) -> str | None:
        if status == "starting":
            return f"开始下载：{item.name} -> {path}"
        if status == "skipped":
            return f"已存在，跳过下载：{item.name} -> {path}"
        if status == "mode":
            return f"下载模式：{item.name} -> {detail or '未知模式'}"
        if status == "paused":
            return f"已暂停：{item.name}"
        if status == "cancelled":
            return f"已取消：{item.name}"
        if status == "fallback":
            return f"模式回退：{item.name} -> {detail or '已回退到单线程'}"
        if status == "candidate_failed":
            return f"候选地址失败，尝试下一个：{item.name}；原因：{detail or '未知'}"
        if status == "diagnostic":
            return f"诊断：{detail or item.name}"
        if status == "completed":
            return f"下载完成：{item.name} -> {path}"
        if status == "partial":
            return f"下载中断：{item.name}"
        if status == "failed":
            return f"下载失败：{item.name}"
        return None


class _NullProgress:
    def __enter__(self) -> _NullProgress:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def update(self, size: int) -> None:
        return None


def _movie_title(item: MediaItem) -> str:
    return f"{item.name} ({item.year})" if item.year else item.name


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize(text: str) -> str:
    # N18：控制字符替换 + 去首尾空白 + 去尾部点/空格 + 长度截断 + Windows 保留名加后缀
    cleaned = INVALID_CHARS.sub("_", text).strip()
    cleaned = cleaned.rstrip(". ").strip()
    if len(cleaned) > 200:
        cleaned = cleaned[:200].rstrip(". ").strip()
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned += "_"
    return cleaned or "untitled"


_EXT_WHITELIST = {
    "mp4": "mp4", "m4v": "mp4", "mov": "mp4", "f4v": "mp4",
    "mkv": "mkv", "matroska": "mkv", "webm": "webm",
    "avi": "avi", "divx": "avi", "xvid": "avi",
    "flv": "flv", "wmv": "wmv", "asf": "asf",
    "ts": "ts", "m2ts": "ts", "mts": "ts", "mpg": "mpg", "mpeg": "mpg",
    "3gp": "3gp", "3g2": "3gp", "ogv": "ogv", "vob": "vob",
    "rm": "rm", "rmvb": "rmvb", "iso": "iso",
}


def _safe_ext(container: str | None) -> str:
    # N1/N18：只保留字母数字后查白名单映射，未知容器落 .bin——封死路径注入并修正 matroska/mov 映射
    if not container:
        return "mp4"
    first = re.sub(r"[^a-z0-9]", "", container.split(",", 1)[0].strip().lower())
    return _EXT_WHITELIST.get(first, "bin")


def _expected_size(response, resume_from: int) -> int | None:
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding and encoding != "identity":
        # 透明解压（gzip 等）会让 Content-Length 与实际落盘字节不一致，放弃尺寸校验
        return None
    content_range = response.headers.get("Content-Range", "")
    parsed = _parse_content_range(content_range)
    if parsed is not None:
        _, _, total = parsed
        return total

    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isdigit():
        size = int(content_length)
        if response.status_code == 206:
            return size + resume_from
        return size
    return None


def _parse_content_range(header: str) -> tuple[int, int, int] | None:
    if not header or "/" not in header or not header.startswith("bytes "):
        return None
    range_part, total_part = header[6:].split("/", 1)
    if "-" not in range_part:
        return None
    start_text, end_text = range_part.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()
    total_text = total_part.strip()
    if not (start_text.isdigit() and end_text.isdigit() and total_text.isdigit()):
        return None
    start, end, total = int(start_text), int(end_text), int(total_text)
    if start > end or end >= total:
        return None
    return start, end, total
