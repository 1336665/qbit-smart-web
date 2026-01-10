#!/usr/bin/env python3
"""precision_limit_engine.py

精确均速限速引擎（Web 版）。

目标：
- 通知/格式严格对齐用户提供的 Speed-Limiting-Engine.py 脚本（由 notifier.py 负责文案）。
- 修复 calculate() 参数不匹配导致的运行时崩溃。
- 替换 notify_limit_applied 的高频通知逻辑（不再发送该类通知）。
- 性能优化：动态循环、属性缓存、批量设置限速、减少 DB 频繁读取。
- 周期内均速尽可能精确地收敛到目标值（使用脚本同款控制器与节奏）。

说明：
- 本引擎支持多 qB 实例。
- PT 站点辅助：用于获取 tid/优惠信息/peerlist 参考信息（不在主循环中做重网络请求，使用后台队列）。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from speed_limiting_engine import (
    EngineTuning,
    PrecisionLimitController,
    precision_tracker,
    safe_div,
)

try:
    from pt_site_helper import PTSiteHelperManager, PT_HELPER_AVAILABLE
except Exception:  # pragma: no cover
    PTSiteHelperManager = None  # type: ignore
    PT_HELPER_AVAILABLE = False

logger = logging.getLogger(__name__)


class LimitConfig:
    # === 与脚本常量尽量对齐 ===
    PHASE_WARMUP = "warmup"
    PHASE_CATCH = "catch"
    PHASE_STEADY = "steady"
    PHASE_FINISH = "finish"

    MIN_LIMIT = 4096

    SPEED_PROTECT_RATIO = 2.5
    SPEED_PROTECT_LIMIT = 1.3
    PROGRESS_PROTECT = 0.90

    # 站点上传速度上限保护（脚本默认 50MiB/s）
    SPEED_LIMIT = 50 * 1024 * 1024

    # qB props 缓存
    MAX_REANNOUNCE = 86400
    PROPS_CACHE = {"finish": 0.2, "steady": 0.5, "catch": 1.0, "warmup": 2.0}

    # TG/日志节奏
    LOG_INTERVAL = 20

    # announce interval 估计
    ANNOUNCE_INTERVAL_NEW = 1800
    ANNOUNCE_INTERVAL_WEEK = 2700
    ANNOUNCE_INTERVAL_OLD = 3600

    # 下载限速
    DL_LIMIT_MIN_TIME = 20
    DL_LIMIT_BUFFER = 30
    DL_LIMIT_MIN = 512
    DL_LIMIT_ADJUST_BUFFER = 60

    # 汇报优化
    REANNOUNCE_WAIT_LIMIT = 5120
    REANNOUNCE_MIN_INTERVAL = 900
    REANNOUNCE_SPEED_SAMPLES = 300

    # PT 辅助
    PEER_LIST_CHECK_INTERVAL = 300
    TID_SEARCH_INTERVAL = 60

    # DB 持久化
    DB_SAVE_INTERVAL = 180

    # Cookie 检查
    COOKIE_CHECK_INTERVAL = 3600

    # API 速率限制（每秒最多 props 请求数；脚本默认 20）
    API_RATE_LIMIT = 20


def estimate_announce_interval(time_ref: Optional[float], now: Optional[float] = None) -> int:
    """估算 announce 周期。

    逻辑与脚本一致：
    - < 7 天：1800
    - < 30 天：2700
    - >= 30 天：3600
    """
    if not time_ref:
        return LimitConfig.ANNOUNCE_INTERVAL_NEW
    if now is None:
        now = time.time()
    age = max(0, now - time_ref)
    if age < 7 * 86400:
        return LimitConfig.ANNOUNCE_INTERVAL_NEW
    if age < 30 * 86400:
        return LimitConfig.ANNOUNCE_INTERVAL_WEEK
    return LimitConfig.ANNOUNCE_INTERVAL_OLD


class SpeedTracker:
    """追踪瞬时速度，用于汇报优化等。"""

    def __init__(self, max_samples: int = 3600):
        self.samples: deque = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def record_speed(self, up_speed: float, dl_speed: float, now: float):
        with self._lock:
            self.samples.append((now, up_speed, dl_speed))

    def get_avg_speeds(self, window: float) -> Tuple[float, float]:
        now = time.time()
        with self._lock:
            relevant = [(u, d) for t, u, d in self.samples if now - t <= window]
        if not relevant:
            return 0.0, 0.0
        avg_up = sum(u for u, _ in relevant) / len(relevant)
        avg_dl = sum(d for _, d in relevant) / len(relevant)
        return avg_up, avg_dl

    def clear(self):
        with self._lock:
            self.samples.clear()


class DownloadLimiter:
    """下载限速器（脚本文案/逻辑对齐）。返回 KiB/s 与原因。"""

    def calc_dl_limit(
        self,
        state: "TorrentLimitState",
        total_done: int,
        total_uploaded: int,
        total_size: int,
        dl_speed: float,
        now: float,
    ) -> Tuple[int, str]:
        this_up = state.this_up(total_uploaded)
        this_time = state.this_time(now)
        if this_time < 2:
            return -1, ""

        avg_speed = this_up / this_time
        if avg_speed <= LimitConfig.SPEED_LIMIT:
            if state.last_dl_limit > 0:
                return -1, "均值恢复"
            return -1, ""

        remaining = total_size - total_done
        if remaining <= 0:
            return -1, ""

        eta = remaining / max(dl_speed, 1)
        min_time = LimitConfig.DL_LIMIT_MIN_TIME * (2 if state.last_limit > 0 else 1)

        if state.last_dl_limit <= 0:
            if 0 < eta <= min_time:
                denominator = this_up / LimitConfig.SPEED_LIMIT - this_time + LimitConfig.DL_LIMIT_BUFFER
                if denominator <= 0:
                    return LimitConfig.DL_LIMIT_MIN, "超速严重"
                dl_limit = remaining / denominator / 1024
                return max(LimitConfig.DL_LIMIT_MIN, int(dl_limit)), "均值超限"
        else:
            if avg_speed >= LimitConfig.SPEED_LIMIT:
                if dl_speed / 1024 < 2 * state.last_dl_limit:
                    denominator = this_up / LimitConfig.SPEED_LIMIT - this_time + LimitConfig.DL_LIMIT_ADJUST_BUFFER
                    if denominator <= 0:
                        return LimitConfig.DL_LIMIT_MIN, "持续超速"
                    dl_limit = remaining / denominator / 1024
                    new_limit = max(LimitConfig.DL_LIMIT_MIN, int(dl_limit))
                    if new_limit < state.last_dl_limit * 0.95:
                        return new_limit, "调整限速"

        return -1, ""


class ReannounceOptimizer:
    """汇报优化器（脚本逻辑对齐）。"""

    @staticmethod
    def should_reannounce(
        state: "TorrentLimitState",
        total_done: int,
        total_uploaded: int,
        total_size: int,
        now: float,
    ) -> Tuple[bool, str]:
        if state.last_reannounce > 0 and now - state.last_reannounce < LimitConfig.REANNOUNCE_MIN_INTERVAL:
            return False, ""

        this_up = state.this_up(total_uploaded)
        this_time = state.this_time(now)
        if this_time < 30:
            return False, ""

        avg_up, avg_dl = state.speed_tracker.get_avg_speeds(LimitConfig.REANNOUNCE_SPEED_SAMPLES)
        if avg_up <= LimitConfig.SPEED_LIMIT or avg_dl <= 0:
            return False, ""

        remaining = total_size - total_done
        if remaining <= 0:
            return False, ""

        announce_interval = state.get_announce_interval(now)
        complete_time = remaining / avg_dl + now
        perfect_time = complete_time - announce_interval * LimitConfig.SPEED_LIMIT / avg_up

        if this_up / this_time > LimitConfig.SPEED_LIMIT:
            earliest = (this_up - LimitConfig.SPEED_LIMIT * this_time) / (45 * 1024 * 1024) + now
        else:
            earliest = now

        if earliest - (now - this_time) < LimitConfig.REANNOUNCE_MIN_INTERVAL:
            return False, ""

        if earliest > perfect_time:
            if now >= earliest:
                if this_up / this_time > LimitConfig.SPEED_LIMIT:
                    return True, "优化汇报"
            else:
                if earliest < perfect_time + 60:
                    state.waiting_reannounce = True
                    return False, "等待汇报"

        return False, ""

    @staticmethod
    def check_waiting_reannounce(
        state: "TorrentLimitState",
        total_uploaded: int,
        now: float,
    ) -> Tuple[bool, str]:
        if not state.waiting_reannounce:
            return False, ""

        this_up = state.this_up(total_uploaded)
        this_time = state.this_time(now)
        if this_time < LimitConfig.REANNOUNCE_MIN_INTERVAL:
            return False, ""

        avg_speed = safe_div(this_up, this_time, 0)
        if avg_speed < LimitConfig.SPEED_LIMIT:
            state.waiting_reannounce = False
            return True, "均值恢复"

        return False, ""


@dataclass
class TorrentLimitState:
    hash: str
    name: str = ""
    tracker: str = ""
    instance_id: Optional[int] = None

    # cycle
    cycle_start: float = 0.0
    cycle_uploaded_start: int = 0
    cycle_index: int = 0
    cycle_synced: bool = False
    cycle_interval: float = 0.0
    jump_count: int = 0
    last_jump: float = 0.0

    # reannounce time cache
    cached_time_left: float = 0.0  # ra at cache time
    cache_ts: float = 0.0
    prev_time_left: float = 0.0
    last_props: float = 0.0
    reannounce_source: str = ""

    # peerlist info (optional)
    last_peer_list_check: float = 0.0
    last_announce_time: Optional[float] = None
    peer_list_uploaded: Optional[int] = None

    # tid/promo
    site_id: Optional[int] = None
    tid: Optional[int] = None
    promotion: str = "未知"
    publish_time: Optional[float] = None
    tid_searched: bool = False
    tid_search_time: float = 0.0
    tid_not_found: bool = False

    # control
    target_speed: int = 0
    last_limit: int = -2
    last_limit_reason: str = ""
    last_dl_limit: int = -1

    dl_limited_this_cycle: bool = False

    last_reannounce: float = 0.0
    waiting_reannounce: bool = False
    reannounced_this_cycle: bool = False

    # session
    session_start_time: float = 0.0
    total_uploaded_start: int = 0
    total_size: int = 0
    time_added: float = 0.0

    # controller
    limit_controller: Optional[PrecisionLimitController] = None
    speed_tracker: SpeedTracker = None  # type: ignore

    # misc
    monitor_notified: bool = False
    last_debug: Dict[str, Any] = None  # type: ignore

    def __post_init__(self):
        if self.speed_tracker is None:
            self.speed_tracker = SpeedTracker()
        if self.last_debug is None:
            self.last_debug = {}

    def get_phase(self, now: float, tl: Optional[float] = None) -> str:
        if tl is None:
            tl = self.get_tl(now)
        if not self.cycle_synced:
            return LimitConfig.PHASE_WARMUP
        if tl < 10:
            return LimitConfig.PHASE_FINISH
        if tl < 60:
            return LimitConfig.PHASE_CATCH
        return LimitConfig.PHASE_STEADY

    def get_announce_interval(self, now: Optional[float] = None) -> int:
        if now is None:
            now = time.time()
        ref = self.publish_time or (self.time_added if self.time_added > 0 else None)
        return estimate_announce_interval(ref, now)

    def get_tl(self, now: float) -> float:
        # Prefer peerlist last_announce_time when available
        if self.last_announce_time and self.last_announce_time > 0:
            interval = self.get_announce_interval(now)
            next_announce = self.last_announce_time + interval
            return max(0.0, next_announce - now)

        if self.cache_ts <= 0:
            return 9999.0
        return max(0.0, self.cached_time_left - (now - self.cache_ts))

    def this_time(self, now: float) -> float:
        return max(0.0, now - self.cycle_start)

    def this_up(self, total_uploaded: int) -> int:
        return max(0, total_uploaded - self.cycle_uploaded_start)

    def estimate_total(self, now: float, tl: float) -> float:
        e = now - self.cycle_start
        if 0 < tl < LimitConfig.MAX_REANNOUNCE:
            return max(1.0, e + tl)
        if self.cycle_synced and self.cycle_interval > 0:
            return max(1.0, self.cycle_interval)
        return max(1.0, e)

    def new_cycle(self, now: float, uploaded: int, tl: float, is_jump: bool):
        if is_jump:
            self.jump_count += 1
            if self.last_jump > 0:
                interval = now - self.last_jump
                if interval > 60:
                    self.cycle_interval = interval
                    if self.jump_count >= 2:
                        self.cycle_synced = True
            self.last_jump = now
            self.last_announce_time = now
            self.cycle_uploaded_start = uploaded
        else:
            # 估算当前周期已上传，用 Kalman 速度回推
            interval = self.get_announce_interval(now)
            elapsed_in_cycle = interval - tl if 0 < tl < interval else 0
            if self.time_added and (now - self.time_added) < interval:
                self.cycle_uploaded_start = 0
            elif elapsed_in_cycle > 60:
                avg_speed = 0.0
                if self.limit_controller:
                    avg_speed = max(0.0, float(self.limit_controller.kalman.speed))
                if avg_speed > 0:
                    est_start = uploaded - avg_speed * elapsed_in_cycle
                    self.cycle_uploaded_start = max(0, int(est_start))
                else:
                    self.cycle_uploaded_start = uploaded
            else:
                self.cycle_uploaded_start = uploaded

        self.cycle_start = now
        self.cycle_index += 1

        self.dl_limited_this_cycle = False
        self.reannounced_this_cycle = False

        self.last_dl_limit = -1

        if self.limit_controller:
            self.limit_controller.reset()
        self.speed_tracker.clear()
        self.prev_time_left = tl

    def get_real_avg_speed(self, total_uploaded: int, now: float) -> float:
        if self.session_start_time <= 0:
            return 0.0
        dt = max(1e-6, now - self.session_start_time)
        return (total_uploaded - self.total_uploaded_start) / dt


@dataclass
class StartupConfig:
    target_speed_kib: int
    safety_margin: float
    enable_reannounce_opt: bool
    enable_dl_limit: bool

    @property
    def target_bytes(self) -> float:
        return self.target_speed_kib * 1024 * self.safety_margin


class PrecisionLimitEngine:
    """主引擎。"""

    def __init__(self, db, qb_manager, notifier=None, site_helper_manager: Optional[PTSiteHelperManager] = None):
        self.db = db
        self.qb_manager = qb_manager
        self.notifier = notifier
        self.site_helper_manager = site_helper_manager

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # telegram control flags
        self.paused = False
        self.temp_target_kib: Optional[int] = None

        self._states: Dict[str, TorrentLimitState] = {}
        self._states_lock = threading.Lock()

        # tuning from script
        self._tuning = EngineTuning(
            pid_params={
                'warmup': {'kp': 0.3, 'ki': 0.05, 'kd': 0.02, 'headroom': 1.03},
                'catch': {'kp': 0.5, 'ki': 0.10, 'kd': 0.05, 'headroom': 1.02},
                'steady': {'kp': 0.6, 'ki': 0.15, 'kd': 0.08, 'headroom': 1.005},
                'finish': {'kp': 0.8, 'ki': 0.20, 'kd': 0.12, 'headroom': 1.001},
            },
            min_limit=LimitConfig.MIN_LIMIT,
            quant_steps={'finish': 256, 'steady': 512, 'catch': 2048, 'warmup': 4096},
            kalman_q_speed=0.1,
            kalman_q_accel=0.05,
            kalman_r=0.5,
        )

        self._dl_limiter = DownloadLimiter()

        # stats
        self._stats = {
            'torrents_controlled': 0,
            'total_cycles': 0,
            'success_cycles': 0,
            'precision_cycles': 0,
            'total_limit_uploaded': 0,
            'start_time': time.time(),
        }

        # cache for db config
        self._cache_ts: float = 0.0
        self._cache_rules: Dict[Optional[int], Dict[str, Any]] = {}
        self._cache_sites_by_id: Dict[int, Dict[str, Any]] = {}
        self._cache_matchers: List[Tuple[str, int]] = []
        self._cache_any_dl: bool = True
        self._cache_any_reannounce_opt: bool = True

        # worker queues
        self._tid_queue: "queue.Queue[str]" = queue.Queue(maxsize=2000)
        self._peer_queue: "queue.Queue[str]" = queue.Queue(maxsize=2000)
        self._tid_thread: Optional[threading.Thread] = None
        self._peer_thread: Optional[threading.Thread] = None

        # API rate limiting
        self._api_times: deque = deque()
        self._api_rate_limit = LimitConfig.API_RATE_LIMIT

        # periodic
        self._last_save = 0.0
        self._last_cookie_check = 0.0

        self._restore_states_from_db()

        # load persisted stats
        try:
            st = self.db.get_limit_stats()
            if st:
                self._stats['total_cycles'] = int(st.get('total_cycles', 0) or 0)
                self._stats['success_cycles'] = int(st.get('success_cycles', 0) or 0)
                self._stats['precision_cycles'] = int(st.get('precision_cycles', 0) or 0)
                self._stats['total_limit_uploaded'] = int(st.get('total_limit_uploaded', 0) or 0)
                start_ts = st.get('start_time')
                if start_ts:
                    self._stats['start_time'] = float(start_ts)
        except Exception:
            logger.exception("Failed to load limit stats")

    # ---------------------------------------------------------------------
    # 生命周期
    # ---------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()

        if PT_HELPER_AVAILABLE and self.site_helper_manager:
            self._tid_thread = threading.Thread(target=self._tid_worker, daemon=True)
            self._peer_thread = threading.Thread(target=self._peer_worker, daemon=True)
            self._tid_thread.start()
            self._peer_thread.start()

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # startup notify
        try:
            self._notify_startup()
        except Exception:
            logger.exception("startup notify failed")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

        # restore limits to unlimited
        try:
            self._restore_limits_on_exit()
        except Exception:
            logger.exception("restore limits failed")

        # persist
        try:
            self._save_states_to_db()
        except Exception:
            logger.exception("save states failed")

        # shutdown notify
        try:
            if self.notifier:
                self.notifier.shutdown_report()
        except Exception:
            logger.exception("shutdown notify failed")

    @property
    def is_running(self) -> bool:
        return self._running

    # ---------------------------------------------------------------------
    # 主循环
    # ---------------------------------------------------------------------

    def _run_loop(self):
        last_log = 0.0
        while self._running and not self._stop_event.is_set():
            loop_start = time.time()

            try:
                min_tl = self._process_all(loop_start)
            except Exception:
                logger.exception("Limit engine loop error")
                min_tl = 9999.0

            # periodic save
            if loop_start - self._last_save >= LimitConfig.DB_SAVE_INTERVAL:
                try:
                    self._save_states_to_db()
                except Exception:
                    logger.exception("save states failed")
                self._last_save = loop_start

            # cookie check (U2 only, align with script)
            if loop_start - self._last_cookie_check >= LimitConfig.COOKIE_CHECK_INTERVAL:
                try:
                    self._auto_check_u2_cookie()
                except Exception:
                    logger.exception("cookie check failed")
                self._last_cookie_check = loop_start

            # log heartbeat
            if loop_start - last_log >= LimitConfig.LOG_INTERVAL:
                last_log = loop_start
                logger.debug(
                    "[LimitEngine] controlled=%s states=%s min_tl=%.1fs",
                    self._stats.get('torrents_controlled', 0),
                    len(self._states),
                    float(min_tl),
                )

            # dynamic sleep (script-like)
            elapsed = time.time() - loop_start
            sleep_time = 5.0
            if min_tl < 10:
                sleep_time = 0.15
            elif min_tl < 60:
                sleep_time = 0.5
            elif min_tl < 180:
                sleep_time = 1.0
            elif min_tl < 600:
                sleep_time = 2.0
            elif min_tl < 1800:
                sleep_time = 4.0
            else:
                sleep_time = 5.0

            sleep_time = max(0.1, sleep_time - elapsed)
            time.sleep(sleep_time)

    # ---------------------------------------------------------------------
    # 核心处理
    # ---------------------------------------------------------------------

    def _refresh_cache(self, now: float):
        if now - self._cache_ts < 10:
            return
        self._cache_ts = now

        # rules
        rules = {}
        try:
            for r in self.db.get_speed_rules(enabled_only=True) or []:
                sid = r.get('site_id')
                rules[sid] = r
        except Exception:
            logger.exception("load speed rules failed")
        self._cache_rules = rules

        # sites
        sites_by_id: Dict[int, Dict[str, Any]] = {}
        matchers: List[Tuple[str, int]] = []
        any_dl = False
        any_reann = False
        try:
            for s in self.db.get_pt_sites(enabled_only=True) or []:
                sid = int(s['id'])
                sites_by_id[sid] = s
                if s.get('enable_dl_limit', 1):
                    any_dl = True
                if s.get('enable_reannounce_opt', 1):
                    any_reann = True

                # build matchers
                kw = (s.get('tracker_keyword') or '').strip().lower()
                if kw:
                    matchers.append((kw, sid))
                url = (s.get('site_url') or '').strip().lower()
                if url:
                    # extract host-ish
                    try:
                        host = url.split('://', 1)[-1].split('/', 1)[0]
                        host = host.lstrip('www.')
                        if host:
                            matchers.append((host, sid))
                    except Exception:
                        pass
        except Exception:
            logger.exception("load pt sites failed")

        self._cache_sites_by_id = sites_by_id
        self._cache_matchers = matchers
        self._cache_any_dl = any_dl
        self._cache_any_reannounce_opt = any_reann

    def _match_site_id(self, tracker: str) -> Optional[int]:
        t = (tracker or '').lower()
        if not t:
            return None
        for key, sid in self._cache_matchers:
            if key and key in t:
                return sid
        return None

    def _get_rule_for_torrent(self, torrent: Dict[str, Any], state: TorrentLimitState) -> Optional[Dict[str, Any]]:
        # if we already have site_id and rule exists
        if state.site_id in self._cache_rules:
            return self._cache_rules.get(state.site_id)

        tracker = torrent.get('tracker') or state.tracker
        sid = self._match_site_id(tracker)
        if sid is not None:
            state.site_id = sid
            return self._cache_rules.get(sid)

        # default rule
        return self._cache_rules.get(None)

    def _should_limit_torrent(self, torrent: Dict[str, Any]) -> bool:
        # Follow old behavior: only handle active upload/download states
        state = (torrent.get('state') or '').lower()
        up_speed = float(torrent.get('upspeed') or 0)
        dl_speed = float(torrent.get('dlspeed') or 0)
        if up_speed > 0 or dl_speed > 0:
            return True
        # also include common active states
        return state in {
            'up', 'uploading', 'downloading', 'stalledup', 'stalledup', 'stalleddl', 'stalleddownloading',
            'forcedup', 'forcedupload', 'forceddl', 'forceddownload',
            'queuedup', 'queuedupload', 'queueddl', 'queueddownload',
        }

    def _process_all(self, now: float) -> float:
        self._refresh_cache(now)

        instances = self.qb_manager.get_connected_instances()
        min_tl = 9999.0

        controlled = 0
        active_hashes: set = set()

        # batch actions
        up_actions: Dict[int, Dict[int, List[str]]] = {}
        dl_actions: Dict[int, Dict[int, List[str]]] = {}

        for inst in instances:
            if not getattr(inst, 'enabled', True) or not getattr(inst, 'connected', False):
                continue
            inst_id = inst.id

            try:
                torrents = self.qb_manager.get_torrents(inst_id, filter='active')
            except Exception:
                logger.exception("Failed to get torrents for instance %s", inst_id)
                continue

            for t in torrents:
                if not self._should_limit_torrent(t):
                    continue

                h = t.get('hash')
                if not h:
                    continue

                state = self._states.get(h)
                if state is None:
                    state = TorrentLimitState(hash=h)
                    state.limit_controller = PrecisionLimitController(self._tuning)
                    state.speed_tracker = SpeedTracker()
                    self._states[h] = state

                # attach basic info
                state.name = t.get('name', state.name)
                state.tracker = t.get('tracker', state.tracker)
                state.instance_id = inst_id
                state.total_size = int(t.get('total_size') or state.total_size or 0)
                # added_on may be 0 if missing
                try:
                    added_on = float(t.get('added_on') or 0)
                    if added_on > 0:
                        state.time_added = added_on
                except Exception:
                    pass

                # init session
                if state.session_start_time <= 0:
                    state.session_start_time = now
                    state.total_uploaded_start = int(t.get('uploaded') or 0)
                    # init cache_ts to now to avoid huge tl
                    state.cache_ts = now

                rule = self._get_rule_for_torrent(t, state)
                if not rule:
                    continue

                # do per torrent processing
                result = self._process_torrent(inst_id, t, state, rule, now)

                if result is None:
                    continue

                new_limit, dl_limit_apply, tl = result

                active_hashes.add(h)
                controlled += 1

                min_tl = min(min_tl, float(tl))

                # batch upload limit apply
                if new_limit != state.last_limit:
                    state.last_limit = new_limit
                    up_actions.setdefault(inst_id, {}).setdefault(new_limit, []).append(h)

                # batch download limit apply
                if dl_limit_apply is not None:
                    dl_actions.setdefault(inst_id, {}).setdefault(dl_limit_apply, []).append(h)

            # end torrent loop

        # apply batched limits
        for inst_id, by_limit in up_actions.items():
            for limit, hashes in by_limit.items():
                try:
                    self.qb_manager.set_upload_limit(inst_id, hashes, limit)
                except Exception:
                    logger.exception("Failed to apply upload limit %s on inst %s", limit, inst_id)

        for inst_id, by_limit in dl_actions.items():
            for limit, hashes in by_limit.items():
                try:
                    # -1 means unlimited (qb will set 0)
                    self.qb_manager.set_download_limit(inst_id, hashes, limit)
                except Exception:
                    logger.exception("Failed to apply download limit %s on inst %s", limit, inst_id)

        # cleanup stale states (not seen for a while)
        self._cleanup_states(active_hashes, now)

        self._stats['torrents_controlled'] = controlled
        return min_tl

    def _cleanup_states(self, active_hashes: set, now: float):
        # keep states that are active; remove those not present for long to avoid memory leak
        remove: List[str] = []
        for h, st in self._states.items():
            if h in active_hashes:
                continue
            # if not active for 2 hours, remove
            if st.session_start_time > 0 and now - st.session_start_time > 7200:
                remove.append(h)
        for h in remove:
            self._states.pop(h, None)

    def _api_ok(self, now: float) -> bool:
        # remove timestamps older than 1s
        while self._api_times and now - self._api_times[0] > 1.0:
            self._api_times.popleft()
        if len(self._api_times) >= self._api_rate_limit:
            return False
        self._api_times.append(now)
        return True

    def _get_props(self, inst_id: int, state: TorrentLimitState, now: float):
        phase = state.get_phase(now)
        cache_time = LimitConfig.PROPS_CACHE.get(phase, 1.0)
        if now - state.last_props < cache_time:
            return
        if not self._api_ok(now):
            return
        try:
            props = self.qb_manager.get_torrent_properties(inst_id, state.hash)
        except Exception:
            return
        state.last_props = now
        if not props:
            return
        ra = props.get('reannounce')
        try:
            ra = float(ra)
        except Exception:
            ra = None
        if ra and 0 < ra < LimitConfig.MAX_REANNOUNCE:
            state.cached_time_left = ra
            state.cache_ts = now
            state.reannounce_source = 'qb_api'

    def _process_torrent(
        self,
        inst_id: int,
        torrent: Dict[str, Any],
        state: TorrentLimitState,
        rule: Dict[str, Any],
        now: float,
    ) -> Optional[Tuple[int, Optional[int], float]]:
        """处理单种子，返回：(new_up_limit_bytes, dl_limit_bytes_or_None, tl)"""

        total_uploaded = int(torrent.get('uploaded') or 0)
        total_done = int(torrent.get('downloaded') or 0)
        total_size = int(torrent.get('total_size') or 0)
        up_speed = float(torrent.get('upspeed') or 0)
        dl_speed = float(torrent.get('dlspeed') or 0)
        progress = float(torrent.get('progress') or 0)

        # record speed sample for controller
        if state.limit_controller:
            state.limit_controller.record_speed(now, up_speed)
        state.speed_tracker.record_speed(up_speed, dl_speed, now)

        # background helper tasks
        self._maybe_search_tid(state, now)
        self._maybe_check_peer_list(state, now)

        # update qB props cache
        self._get_props(inst_id, state, now)

        tl = state.get_tl(now)

        # monitor start notify (wait for tid search or 60s timeout)
        if self.notifier and not state.monitor_notified:
            wait_timeout = (now - state.session_start_time) > 60
            helper_available = self._has_helper_for_state(state)
            if state.tid_searched or (not helper_available) or wait_timeout:
                try:
                    self.notifier.monitor_start({
                        'hash': state.hash,
                        'name': state.name,
                        'total_size': total_size,
                        'target': float(state.target_speed) if state.target_speed else 0,
                        'tid': state.tid,
                        'promotion': state.promotion or '未知',
                    })
                except Exception:
                    logger.exception("monitor_start notify failed")
                state.monitor_notified = True

        # finish notify
        if self.notifier:
            try:
                self.notifier.check_finish({
                    'hash': state.hash,
                    'name': state.name,
                    'progress': progress,
                    'total_uploaded': total_uploaded,
                    'total_downloaded': total_done,
                })
            except Exception:
                pass

        # detect cycle jump
        is_jump = state.cycle_start > 0 and tl > state.prev_time_left + 30

        if state.cycle_start == 0 or is_jump:
            if is_jump and state.cycle_start > 0:
                self._report_cycle(state, torrent, now)
            state.new_cycle(now, total_uploaded, tl, is_jump)

        # update prev tl
        state.prev_time_left = tl

        # compute effective target
        target_kib = int(rule.get('target_speed_kib') or 0)
        safety = float(rule.get('safety_margin') or 1.0)
        if self.temp_target_kib is not None:
            target_kib = int(self.temp_target_kib)
        target_bytes = target_kib * 1024 * safety
        state.target_speed = int(target_bytes)

        # reannounce waiting check
        if self._cache_any_reannounce_opt:
            should, reason = ReannounceOptimizer.check_waiting_reannounce(state, total_uploaded, now)
            if should:
                self._do_reannounce(inst_id, state, reason)

        # calculate upload limit
        up_limit, up_reason = self._calculate_upload_limit(state, total_uploaded, up_speed, progress, tl, now)
        state.last_limit_reason = up_reason

        # download limit (bytes) -> apply only when changed
        dl_apply: Optional[int] = None
        if self._cache_any_dl:
            dl_kib, dl_reason = self._dl_limiter.calc_dl_limit(state, total_done, total_uploaded, total_size, dl_speed, now)
            if dl_kib > 0 and not state.dl_limited_this_cycle:
                state.dl_limited_this_cycle = True
            # apply when changed
            if dl_kib != state.last_dl_limit:
                state.last_dl_limit = dl_kib
                # notify when set
                if self.notifier and dl_kib > 0:
                    try:
                        self.notifier.dl_limit_notify(state.name, float(dl_kib), dl_reason, state.tid)
                    except Exception:
                        pass
                # convert to bytes for qb (0 means unlimited)
                dl_apply = -1 if dl_kib < 0 else int(dl_kib * 1024)

        # reannounce decision
        if self._cache_any_reannounce_opt and not state.reannounced_this_cycle:
            should, reason = ReannounceOptimizer.should_reannounce(state, total_done, total_uploaded, total_size, now)
            if should:
                self._do_reannounce(inst_id, state, reason)

        return up_limit, dl_apply, tl

    def _calculate_upload_limit(
        self,
        state: TorrentLimitState,
        total_uploaded: int,
        up_speed: float,
        progress: float,
        tl: float,
        now: float,
    ) -> Tuple[int, str]:
        if self.paused:
            return -1, "已暂停"

        # overspeed protection (script-like)
        real_speed = state.get_real_avg_speed(total_uploaded, now)
        if real_speed > LimitConfig.SPEED_LIMIT * 1.05:
            if self.notifier:
                try:
                    self.notifier.overspeed_warning(state.name, real_speed, LimitConfig.SPEED_LIMIT, state.tid)
                except Exception:
                    pass
            return LimitConfig.MIN_LIMIT, "超速刹车"

        if state.waiting_reannounce:
            return LimitConfig.REANNOUNCE_WAIT_LIMIT * 1024, "等待汇报"

        elapsed = max(0.0, now - state.cycle_start)
        uploaded_in_cycle = state.this_up(total_uploaded)

        phase = state.get_phase(now, tl)
        precision_adj = precision_tracker.get_adjustment(phase)

        total_time = state.estimate_total(now, tl)

        if not state.limit_controller:
            state.limit_controller = PrecisionLimitController(self._tuning)

        limit, reason, debug = state.limit_controller.calculate(
            target=float(state.target_speed),
            uploaded=uploaded_in_cycle,
            time_left=float(tl),
            elapsed=float(elapsed),
            phase=phase,
            now=float(now),
            precision_adj=float(precision_adj),
        )
        state.last_debug = debug

        # progress protection: near complete do not leave unlimited
        if progress > LimitConfig.PROGRESS_PROTECT and limit < 0 and tl < 30:
            limit = max(LimitConfig.MIN_LIMIT, int(state.target_speed))
            reason += "+保"

        return int(limit), str(reason)

    def _do_reannounce(self, inst_id: int, state: TorrentLimitState, reason: str):
        try:
            self.qb_manager.reannounce(inst_id, state.hash)
        except Exception:
            return
        state.last_reannounce = time.time()
        state.reannounced_this_cycle = True
        state.waiting_reannounce = False
        if self.notifier:
            try:
                self.notifier.reannounce_notify(state.name, reason, state.tid)
            except Exception:
                pass

    # ---------------------------------------------------------------------
    # PT helper background tasks
    # ---------------------------------------------------------------------

    def _has_helper_for_state(self, state: TorrentLimitState) -> bool:
        if not (PT_HELPER_AVAILABLE and self.site_helper_manager):
            return False
        try:
            if state.site_id is not None:
                helper = self.site_helper_manager.get_helper(state.site_id)
                return bool(helper and getattr(helper, 'enabled', False))
            # fallback by tracker
            helper = self.site_helper_manager.get_helper_by_tracker(state.tracker)
            return bool(helper and getattr(helper, 'enabled', False))
        except Exception:
            return False

    def _maybe_search_tid(self, state: TorrentLimitState, now: float):
        if not (PT_HELPER_AVAILABLE and self.site_helper_manager):
            return
        if state.tid is not None:
            return
        if state.tid_searched:
            return
        if state.tid_not_found and (now - state.tid_search_time) < 3600:
            return
        if (now - state.tid_search_time) < LimitConfig.TID_SEARCH_INTERVAL:
            return

        if not self._has_helper_for_state(state):
            return

        state.tid_search_time = now
        try:
            self._tid_queue.put_nowait(state.hash)
        except Exception:
            pass

    def _maybe_check_peer_list(self, state: TorrentLimitState, now: float):
        if not (PT_HELPER_AVAILABLE and self.site_helper_manager):
            return
        if not state.tid:
            return
        if (now - state.last_peer_list_check) < LimitConfig.PEER_LIST_CHECK_INTERVAL:
            return
        if not self._has_helper_for_state(state):
            return
        state.last_peer_list_check = now
        try:
            self._peer_queue.put_nowait(state.hash)
        except Exception:
            pass

    def _tid_worker(self):
        while self._running and not self._stop_event.is_set():
            try:
                h = self._tid_queue.get(timeout=1)
            except Exception:
                continue
            state = self._states.get(h)
            if not state:
                continue
            try:
                helper = None
                if state.site_id is not None:
                    helper = self.site_helper_manager.get_helper(state.site_id)
                if not helper:
                    helper = self.site_helper_manager.get_helper_by_tracker(state.tracker)
                if not helper or not getattr(helper, 'enabled', False):
                    continue
                info = helper.search_tid_by_hash(state.hash)
                state.tid_searched = True
                if info and getattr(info, 'tid', None):
                    state.tid = int(info.tid)
                    if getattr(info, 'promotion', None):
                        state.promotion = str(info.promotion)
                    if getattr(info, 'publish_time', None):
                        state.publish_time = float(info.publish_time)
                else:
                    state.tid_not_found = True
            except Exception:
                logger.debug("tid worker error", exc_info=True)

    def _peer_worker(self):
        while self._running and not self._stop_event.is_set():
            try:
                h = self._peer_queue.get(timeout=1)
            except Exception:
                continue
            state = self._states.get(h)
            if not state or not state.tid:
                continue
            try:
                helper = None
                if state.site_id is not None:
                    helper = self.site_helper_manager.get_helper(state.site_id)
                if not helper:
                    helper = self.site_helper_manager.get_helper_by_tracker(state.tracker)
                if not helper or not getattr(helper, 'enabled', False):
                    continue
                info = helper.get_peer_list_info(state.tid)
                if not info:
                    continue
                if info.get('last_announce'):
                    try:
                        state.last_announce_time = float(info['last_announce'])
                    except Exception:
                        pass
                if info.get('uploaded') is not None:
                    try:
                        state.peer_list_uploaded = int(info['uploaded'])
                    except Exception:
                        pass
                # if site can provide reannounce_in, use it as extra signal
                if info.get('reannounce_in') is not None:
                    try:
                        ra = float(info['reannounce_in'])
                        if 0 < ra < LimitConfig.MAX_REANNOUNCE:
                            state.cached_time_left = ra
                            state.cache_ts = time.time()
                            state.reannounce_source = 'site'
                    except Exception:
                        pass
            except Exception:
                logger.debug("peer worker error", exc_info=True)

    # ---------------------------------------------------------------------
    # cycle report & persistence
    # ---------------------------------------------------------------------

    def _report_cycle(self, state: TorrentLimitState, torrent: Dict[str, Any], now: float):
        tl = state.prev_time_left
        total_uploaded = int(torrent.get('uploaded') or 0)
        uploaded_in_cycle = state.this_up(total_uploaded)
        elapsed = max(0.0, now - state.cycle_start)
        total_time = elapsed + max(0.0, tl)
        avg_speed = safe_div(uploaded_in_cycle, total_time, 0)

        target = state.target_speed
        ratio = safe_div(avg_speed, target, 0) if target > 0 else 0

        self._stats['total_cycles'] += 1
        self._stats['total_limit_uploaded'] += uploaded_in_cycle

        success = abs(ratio - 1) <= 0.03
        precise = abs(ratio - 1) <= 0.01
        if success:
            self._stats['success_cycles'] += 1
        if precise:
            self._stats['precision_cycles'] += 1

        # record precision tracker
        phase = state.get_phase(now, tl)
        precision_tracker.record(ratio, phase, now)

        # db
        try:
            self.db.add_limit_history(
                torrent_hash=state.hash,
                name=state.name,
                instance_id=state.instance_id,
                site_id=state.site_id,
                tid=state.tid,
                cycle_start=state.cycle_start,
                cycle_end=now,
                uploaded=uploaded_in_cycle,
                target_speed=int(target),
                avg_speed=float(avg_speed),
                hit=1 if success else 0,
            )
            self.db.update_limit_stats(
                total_cycles=self._stats['total_cycles'],
                success_cycles=self._stats['success_cycles'],
                precision_cycles=self._stats['precision_cycles'],
                total_limit_uploaded=self._stats['total_limit_uploaded'],
            )
        except Exception:
            logger.exception("Failed to save cycle history")

        # notify
        if self.notifier:
            try:
                self.notifier.cycle_report({
                    'name': state.name,
                    'hash': state.hash,
                    'tid': state.tid,
                    'uploaded': uploaded_in_cycle,
                    'target_speed': target,
                    'avg_speed': avg_speed,
                    'ratio': ratio,
                    'elapsed': elapsed,
                    'time_left': tl,
                    'cycle_index': state.cycle_index,
                })
            except Exception:
                logger.exception("cycle_report notify failed")

    def _restore_states_from_db(self):
        try:
            states = self.db.get_torrent_limit_states() or []
        except Exception:
            return
        now = time.time()
        for s in states:
            try:
                h = s['torrent_hash']
                st = TorrentLimitState(hash=h)
                st.cycle_start = float(s.get('cycle_start') or 0)
                st.cycle_uploaded_start = int(s.get('cycle_uploaded_start') or 0)
                st.cycle_index = int(s.get('cycle_index') or 0)
                st.cycle_synced = bool(s.get('cycle_synced') or 0)
                st.cycle_interval = float(s.get('cycle_interval') or 0)
                st.jump_count = int(s.get('jump_count') or 0)
                st.last_jump = float(s.get('last_jump') or 0)
                st.prev_time_left = float(s.get('prev_time_left') or 0)
                st.cached_time_left = float(s.get('cached_time_left') or 0)
                st.cache_ts = now
                st.instance_id = s.get('instance_id')
                st.site_id = s.get('site_id')
                st.tid = s.get('tid')
                st.target_speed = int(s.get('target_speed') or 0)
                st.last_limit = int(s.get('last_limit') or -2)
                st.last_limit_reason = s.get('last_limit_reason') or ''
                st.last_dl_limit = int(s.get('last_dl_limit') or -1)
                st.session_start_time = float(s.get('session_start_time') or 0)
                st.total_uploaded_start = int(s.get('total_uploaded_start') or 0)
                st.total_size = int(s.get('total_size') or 0)

                st.limit_controller = PrecisionLimitController(self._tuning)
                st.speed_tracker = SpeedTracker()

                self._states[h] = st
            except Exception:
                continue

    def _save_states_to_db(self):
        for h, st in list(self._states.items()):
            try:
                self.db.save_torrent_limit_state(
                    torrent_hash=h,
                    name=st.name,
                    cycle_start=st.cycle_start,
                    cycle_uploaded_start=st.cycle_uploaded_start,
                    cycle_index=st.cycle_index,
                    cycle_synced=1 if st.cycle_synced else 0,
                    cycle_interval=st.cycle_interval,
                    jump_count=st.jump_count,
                    last_jump=st.last_jump,
                    prev_time_left=st.prev_time_left,
                    cached_time_left=st.cached_time_left,
                    instance_id=st.instance_id,
                    site_id=st.site_id,
                    tid=st.tid,
                    target_speed=st.target_speed,
                    last_limit=st.last_limit,
                    last_limit_reason=st.last_limit_reason,
                    last_dl_limit=st.last_dl_limit,
                    session_start_time=st.session_start_time,
                    total_uploaded_start=st.total_uploaded_start,
                    total_size=st.total_size,
                )
            except Exception:
                continue

    # ---------------------------------------------------------------------
    # u2 cookie check
    # ---------------------------------------------------------------------

    def _auto_check_u2_cookie(self):
        if not (PT_HELPER_AVAILABLE and self.site_helper_manager and self.notifier):
            return
        helper = None
        try:
            # try find by site name/url
            for st in self.site_helper_manager.get_all_status():
                url = (st.get('site_url') or '').lower()
                name = (st.get('site_name') or '').lower()
                if 'u2.dmhy.org' in url or name.startswith('u2'):
                    sid = st.get('site_id')
                    if sid is not None:
                        helper = self.site_helper_manager.get_helper(sid)
                        break
        except Exception:
            helper = None

        if not helper or not getattr(helper, 'enabled', False):
            return

        try:
            ok = bool(helper.check_cookie_valid())
        except Exception:
            ok = True

        if not ok:
            try:
                self.notifier.cookie_invalid_notify()
            except Exception:
                pass

    # ---------------------------------------------------------------------
    # startup / exit helpers
    # ---------------------------------------------------------------------

    def _notify_startup(self):
        if not self.notifier:
            return
        # choose a representative rule
        rules = list(self._cache_rules.values()) if self._cache_rules else []
        if not rules:
            try:
                self._refresh_cache(time.time())
                rules = list(self._cache_rules.values())
            except Exception:
                rules = []

        # default rule preferred
        rule = self._cache_rules.get(None) if self._cache_rules else None
        if rule is None and rules:
            rule = rules[0]

        target_kib = int(rule.get('target_speed_kib') or 0) if rule else 0
        safety = float(rule.get('safety_margin') or 1.0) if rule else 1.0

        cfg = StartupConfig(
            target_speed_kib=target_kib,
            safety_margin=safety,
            enable_reannounce_opt=bool(self._cache_any_reannounce_opt),
            enable_dl_limit=bool(self._cache_any_dl),
        )

        # qb versions
        qb_versions: List[str] = []
        try:
            for inst in self.qb_manager.get_connected_instances():
                if not getattr(inst, 'connected', False) or not getattr(inst, 'client', None):
                    continue
                try:
                    v = inst.client.app_version()
                except Exception:
                    v = ''
                label = (inst.name or f"#{inst.id}").strip()
                if v:
                    qb_versions.append(f"{label}:{v}")
        except Exception:
            pass

        qb_ver_str = "; ".join(qb_versions) if qb_versions else "unknown"

        # u2 enabled
        u2_enabled = False
        if PT_HELPER_AVAILABLE and self.site_helper_manager:
            try:
                for st in self.site_helper_manager.get_all_status():
                    url = (st.get('site_url') or '').lower()
                    name = (st.get('site_name') or '').lower()
                    if 'u2.dmhy.org' in url or name.startswith('u2'):
                        u2_enabled = bool(st.get('enabled', False))
                        break
            except Exception:
                pass

        self.notifier.startup(cfg, qb_ver_str, u2_enabled)

    def _restore_limits_on_exit(self):
        # batch restore upload limits to unlimited
        by_inst: Dict[int, List[str]] = {}
        by_inst_dl: Dict[int, List[str]] = {}
        for h, st in self._states.items():
            if st.instance_id is None:
                continue
            by_inst.setdefault(int(st.instance_id), []).append(h)
            if st.last_dl_limit and st.last_dl_limit > 0:
                by_inst_dl.setdefault(int(st.instance_id), []).append(h)

        for inst_id, hashes in by_inst.items():
            try:
                self.qb_manager.set_upload_limit(inst_id, hashes, -1)
            except Exception:
                pass
        for inst_id, hashes in by_inst_dl.items():
            try:
                self.qb_manager.set_download_limit(inst_id, hashes, -1)
            except Exception:
                pass

    # ---------------------------------------------------------------------
    # UI/API helpers
    # ---------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def get_state(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        st = self._states.get(torrent_hash)
        if not st:
            return None
        now = time.time()
        # Phase & timing
        tl_raw = st.get_tl(now)
        tl_valid = 0 < tl_raw < LimitConfig.MAX_REANNOUNCE
        tl = tl_raw if tl_valid else None
        phase = st.get_phase(now, tl_raw)

        # Pull qB current torrent info for accurate speeds/limits.
        torrent: Optional[Dict[str, Any]] = None
        if st.instance_id is not None:
            try:
                torrent = self.qb_manager.get_torrent(int(st.instance_id), st.hash)
            except Exception:
                torrent = None
        if torrent is None:
            try:
                torrent = self.qb_manager.get_torrent_info_by_hash(st.hash)
            except Exception:
                torrent = None

        def _num(d: Optional[Dict[str, Any]], *keys: str, default: float = 0.0) -> float:
            if not d:
                return default
            for k in keys:
                v = d.get(k)
                if v is None:
                    continue
                try:
                    return float(v)
                except Exception:
                    continue
            return default

        total_uploaded = int(_num(torrent, 'uploaded', 'total_uploaded', default=0.0))
        up_speed = float(_num(torrent, 'upspeed', 'up_speed', default=0.0))
        dl_speed = float(_num(torrent, 'dlspeed', 'dl_speed', default=0.0))
        qb_up_limit = int(_num(torrent, 'up_limit', default=-1.0))
        qb_dl_limit = int(_num(torrent, 'dl_limit', default=-1.0))

        # Cycle derived metrics
        cycle_elapsed = 0.0
        if st.cycle_start and st.cycle_start > 0 and st.cycle_start < now + 10:
            cycle_elapsed = max(0.0, now - st.cycle_start)
        cycle_uploaded = st.this_up(total_uploaded) if st.cycle_start and st.cycle_start > 0 else 0
        cycle_avg_speed = (cycle_uploaded / cycle_elapsed) if cycle_elapsed > 0.5 else 0.0

        # Target bytes for current cycle: match script logic (elapsed + time_left) when possible.
        if st.cycle_start and st.cycle_start > 0 and tl_valid:
            total_time = cycle_elapsed + tl_raw
        elif st.cycle_synced and st.cycle_interval > 0:
            total_time = float(st.cycle_interval)
        else:
            total_time = float(st.get_announce_interval(now))
        total_time = max(1.0, total_time)
        target_upload = int(max(0.0, float(st.target_speed)) * total_time) if st.target_speed > 0 else 0
        target_distance = target_upload - cycle_uploaded
        target_progress = safe_div(cycle_uploaded * 100.0, float(target_upload), 0.0) if target_upload > 0 else 0.0

        kalman_predicted = 0
        if st.limit_controller and tl_valid:
            try:
                kalman_predicted = int(cycle_uploaded + st.limit_controller.kalman.predict_upload(float(tl_raw)))
            except Exception:
                kalman_predicted = 0

        # last_limit defaults to -2 for uninitialized; UI should not show negative speed.
        last_limit = st.last_limit
        if last_limit == -2:
            # Prefer showing 'unlimited' when qB has no limit.
            if qb_up_limit <= 0:
                last_limit = -1

        return {
            'hash': st.hash,
            'name': st.name,
            'instance_id': st.instance_id,
            'tracker': st.tracker,
            'phase': phase,
            'cycle_start': st.cycle_start,
            'cycle_index': st.cycle_index,
            'cycle_synced': st.cycle_synced,
            'cycle_interval': st.cycle_interval,
            'cycle_duration': cycle_elapsed,
            'time_left': tl,
            'time_left_raw': tl_raw,
            'time_left_valid': tl_valid,
            'reannounce_source': st.reannounce_source or '未知',

            'cycle_uploaded': cycle_uploaded,
            'target_upload': target_upload,
            'cycle_avg_speed': cycle_avg_speed,
            'current_speed': up_speed,
            'current_dl_speed': dl_speed,
            'target_distance': target_distance,
            'target_progress': target_progress,
            'kalman_predicted': kalman_predicted,

            'target_speed': st.target_speed,
            'last_limit': last_limit,
            'last_limit_reason': st.last_limit_reason,
            'last_dl_limit': st.last_dl_limit,

            'qb_up_limit': qb_up_limit,
            'qb_dl_limit': qb_dl_limit,

            'tid': st.tid,
            'site_id': st.site_id,
            'promotion': st.promotion,
            'kalman_speed': float(st.limit_controller.kalman.speed) if st.limit_controller else 0,
        }

    def get_speed_samples(self, torrent_hash: str, window: int = 300) -> Optional[List[Dict[str, Any]]]:
        """返回最近 window 秒的速度样本（用于 UI 可视化）。"""
        st = self._states.get(torrent_hash)
        if not st or not st.speed_tracker:
            return None
        now = time.time()
        with st.speed_tracker._lock:
            samples = [(t, u, d) for (t, u, d) in st.speed_tracker.samples if now - t <= window]
        return [{'t': t, 'up': u, 'dl': d} for (t, u, d) in samples]

    def get_all_states(self) -> List[Dict[str, Any]]:
        now = time.time()
        out: List[Dict[str, Any]] = []
        for st in self._states.values():
            tl = st.get_tl(now)
            phase = st.get_phase(now, tl)
            out.append({
                'hash': st.hash,
                'name': st.name,
                'phase': phase,
                'time_left': tl,
                'cycle_index': st.cycle_index,
                'target_speed': st.target_speed,
                'last_limit': st.last_limit,
                'last_limit_reason': st.last_limit_reason,
                'tid': st.tid,
                'kalman_speed': float(st.limit_controller.kalman.speed) if st.limit_controller else 0,
            })
        return out


def create_precision_limit_engine(db, qb_manager, notifier=None, site_helper_manager=None) -> PrecisionLimitEngine:
    return PrecisionLimitEngine(db=db, qb_manager=qb_manager, notifier=notifier, site_helper_manager=site_helper_manager)


# Backward compatible

def safe_div_int(a: float, b: float, default: int = 0) -> int:
    return int(safe_div(a, b, default))
