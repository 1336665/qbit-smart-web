#!/usr/bin/env python3
"""notifier.py

Telegram 通知与指令控制。

⚠️ 用户要求：通知内容/格式需 100% 完全复刻 Speed-Limiting-Engine.py 脚本中的文案。
因此本文件的限速相关通知与指令回复，严格对齐脚本文案与格式。

同时保留 Web 端其他模块(如 RSS/自动删种)所需的通用 notify 接口，避免破坏现有功能。
"""

from __future__ import annotations

import html
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import requests


# ════════════════════════════════════════════════════════════════════════
# 文案/格式严格复刻 Speed-Limiting-Engine.py
# ════════════════════════════════════════════════════════════════════════

class C:
    VERSION = "11.0.0 PRO"


def escape_html(t: Any) -> str:
    return str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def fmt_size(b: float) -> str:
    if b == 0:
        return "0 B"
    for u in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if abs(b) < 1024:
            return f"{b:.2f} {u}"
        b /= 1024
    return f"{b:.2f} PiB"


def fmt_speed(b: float) -> str:
    if b == 0:
        return "0 B/s"
    for u in ['B/s', 'KiB/s', 'MiB/s', 'GiB/s']:
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TiB/s"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m"


def parse_speed_str(s: str) -> Optional[int]:
    """解析速度字符串，如 '100M' -> 102400 (KiB)"""
    s = s.strip().upper()
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(K|M|G|KB|MB|GB|KIB|MIB|GIB)?$', s)
    if not match:
        return None
    num = float(match.group(1))
    unit = match.group(2) or 'K'
    multipliers = {
        'K': 1, 'KB': 1, 'KIB': 1,
        'M': 1024, 'MB': 1024, 'MIB': 1024,
        'G': 1048576, 'GB': 1048576, 'GIB': 1048576
    }
    return int(num * multipliers.get(unit, 1))


# 日志环形缓冲区（用于 /log 命令），复刻脚本行为
class LogBuffer:
    def __init__(self, maxlen: int = 100):
        from collections import deque
        self._buffer: Deque[str] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, msg: str):
        with self._lock:
            self._buffer.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    def get_recent(self, n: int = 10) -> List[str]:
        with self._lock:
            return list(self._buffer)[-n:]


@dataclass
class _StartupConfig:
    """用于拼接 startup 文案的轻量配置对象（字段名对齐脚本）"""
    target_speed_kib: int
    safety_margin: float
    enable_reannounce_opt: bool
    enable_dl_limit: bool

    @property
    def target_bytes(self) -> int:
        return int(self.target_speed_kib * 1024 * self.safety_margin)


class Notifier:
    """Telegram Bot + 文案严格对齐脚本。

    注意：本类同时保留 Web 端其他模块所需的通用通知接口。
    """

    def __init__(self, db, logger: Optional[logging.Logger] = None):
        self.db = db
        self.logger = logger or logging.getLogger('notifier')

        self.bot_token = (self.db.get_config('telegram_bot_token') or '').strip()
        self.chat_id = (self.db.get_config('telegram_chat_id') or '').strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ''

        self.enabled = bool(self.bot_token and self.chat_id)

        self._session = requests.Session()
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._threads_started = False

        self._sent_cache: Dict[str, float] = {}
        self._offset: Optional[int] = None

        # 对齐脚本：暂停/临时目标速度
        self.paused: bool = False
        self.temp_target_kib: Optional[int] = None

        # 指令用上下文
        self.qb_manager = None
        self.site_helper_manager = None
        self.limit_engine = None

        self._finish_notified: set[str] = set()

        # /log
        self.log_buffer = LogBuffer(maxlen=200)

        if self.enabled:
            self.logger.info("Telegram 通知已启用")
        else:
            self.logger.info("Telegram 通知未配置或未启用")

    # ─────────────────────────────────────────────────────────────
    # HTML sanitize (对齐脚本)
    # ─────────────────────────────────────────────────────────────
    def _html_sanitize(self, msg: str) -> str:
        """Sanitize message for Telegram HTML parse_mode.

        - Preserve Telegram-supported HTML tags (b/strong/i/em/u/ins/s/strike/del/code/pre/a/span/tg-spoiler/blockquote).
        - Escape unsupported tags like <速度> => &lt;速度&gt;
        - Escape stray '&' (not part of an entity) to avoid HTML parse errors.
        """
        if not msg:
            return msg

        # Escape stray '&' but keep existing entities like &lt; &amp; &#123;
        msg = re.sub(r'&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', str(msg))

        if '<' not in msg:
            return msg

        allowed = {
            'b','strong','i','em','u','ins','s','strike','del',
            'code','pre','a','span','tg-spoiler','blockquote'
        }

        def repl(m: re.Match) -> str:
            full = m.group(0)
            inner = (m.group(1) or '').strip()
            if not inner:
                return html.escape(full)

            name = inner.lstrip('/').split()[0].lower()
            if name not in allowed:
                return html.escape(full)

            # Telegram: <a> needs href=
            if name == 'a' and not inner.startswith('/'):
                if re.search(r'\bhref\s*=', inner, flags=re.IGNORECASE):
                    return full
                return html.escape(full)

            # Telegram: <span> only for spoiler (class="tg-spoiler")
            if name == 'span' and not inner.startswith('/'):
                if re.search(r'tg-spoiler', inner, flags=re.IGNORECASE):
                    return full
                return html.escape(full)

            return full

        # Replace every <...> region with either allowed tag or escaped literal
        msg = re.sub(r'<([^<>]+)>', repl, msg)
        return msg

    # ─────────────────────────────────────────────────────────────
    # 生命周期
    # ─────────────────────────────────────────────────────────────
    def set_context(self, qb_manager=None, site_helper_manager=None, limit_engine=None):
        if qb_manager is not None:
            self.qb_manager = qb_manager
        if site_helper_manager is not None:
            self.site_helper_manager = site_helper_manager
        if limit_engine is not None:
            self.limit_engine = limit_engine

    def start(self):
        if not self.enabled:
            return
        if self._threads_started:
            return
        self._threads_started = True

        threading.Thread(target=self._send_worker, daemon=True).start()
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def close(self):
        self._stop.set()

    # ─────────────────────────────────────────────────────────────
    # 发送
    # ─────────────────────────────────────────────────────────────
    def _send_worker(self):
        while not self._stop.is_set():
            try:
                msg = self._queue.get(timeout=5)
                if not msg:
                    continue
                try:
                    resp = self._session.post(
                        f"{self.base_url}/sendMessage",
                        json={
                            "chat_id": self.chat_id,
                            "text": self._html_sanitize(msg),
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True
                        },
                        timeout=20
                    )
                    if resp.status_code == 429:
                        retry = resp.json().get('parameters', {}).get('retry_after', 5)
                        time.sleep(retry)
                    else:
                        time.sleep(0.5)
                except Exception as e:
                    self.logger.debug(f"发送失败: {e}")
            except queue.Empty:
                continue
            except Exception:
                continue

    def send(self, msg: str, key: Optional[str] = None, interval: float = 0):
        if not self.enabled:
            return
        now = time.time()
        if key and interval > 0:
            last = self._sent_cache.get(key, 0)
            if now - last < interval:
                return
            self._sent_cache[key] = now
        try:
            self._queue.put_nowait(msg)
        except Exception:
            pass

    def send_immediate(self, msg: str):
        if not self.enabled:
            return
        try:
            self._session.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": self._html_sanitize(msg),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=20
            )
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # Long poll + 指令解析
    # ─────────────────────────────────────────────────────────────
    def _poll_worker(self):
        if not self.enabled:
            return

        while not self._stop.is_set():
            try:
                resp = self._session.get(
                    f"{self.base_url}/getUpdates",
                    params={
                        "timeout": 60,
                        "offset": self._offset
                    },
                    timeout=90
                )
                data = resp.json()
                if not data.get('ok'):
                    time.sleep(5)
                    continue
                for upd in data.get('result', []):
                    self._offset = upd['update_id'] + 1
                    msg = upd.get('message') or upd.get('edited_message')
                    if not msg:
                        continue
                    chat_id = str(msg.get('chat', {}).get('id', ''))
                    if chat_id != str(self.chat_id):
                        continue
                    text = (msg.get('text') or '').strip()
                    if not text.startswith('/'):
                        continue
                    self._handle_command(text)
            except Exception as e:
                self.logger.debug(f"poll失败: {e}")
                time.sleep(5)

    def _handle_command(self, text: str):
        parts = text.split()
        cmd = parts[0].split('@')[0]
        args = parts[1:]

        if cmd == '/help':
            self._cmd_help()
        elif cmd == '/status':
            self._cmd_status()
        elif cmd == '/pause':
            self._cmd_pause()
        elif cmd == '/resume':
            self._cmd_resume()
        elif cmd == '/limit':
            self._cmd_limit(args)
        elif cmd == '/log':
            self._cmd_log(args)
        elif cmd == '/cookie':
            self._cmd_cookie()
        elif cmd == '/config':
            self._cmd_config(args)
        elif cmd == '/stats':
            self._cmd_stats()
        # 扩展：不写入 /help，避免破坏脚本文案
        elif cmd == '/cookieall':
            self._cmd_cookieall()

    # ═══════════════════════════════════════════
    # 指令处理（文案严格对齐脚本）
    # ═══════════════════════════════════════════

    def _cmd_help(self):
        msg = f"""🤖 <b>qBit Smart Limit v{C.VERSION}</b>
━━━━━━━━━━━━━━━━━━━━━
📌 <b>基本命令</b>
/status - 查看种子状态
/pause - 暂停限速
/resume - 恢复限速
/limit <速度> - 临时修改目标速度
/log [N] - 查看最近N条日志
/cookie - 检查U2 Cookie状态

⚙️ <b>配置命令</b>
/config <key> <value> - 修改配置(需重启)

📊 <b>统计命令</b>
/stats - 查看运行统计

💡 <b>示例</b>
/limit 100M
/log 20
/config qb_host 192.168.1.2

发送 /help 显示此帮助"""
        self.send_immediate(msg)

    def _cmd_status(self):
        if not self.limit_engine:
            self.send_immediate("❌ 控制器未初始化")
            return

        now = time.time()
        states = list(getattr(self.limit_engine, '_states', {}).values())
        if not states:
            self.send_immediate("📊 当前无活动种子")
            return

        # 取前10个
        lines = []
        for s in sorted(states, key=lambda x: getattr(x, 'cycle_start', 0), reverse=True)[:10]:
            try:
                phase = s.get_phase(now) if hasattr(s, 'get_phase') else 'warmup'
                tl = s.get_tl(now) if hasattr(s, 'get_tl') else 0
                phase_emoji = {'warmup':'🔥','catch':'🏃','steady':'⚖️','finish':'🎯'}.get(phase,'❓')
                speed = 0.0
                if getattr(s, 'limit_controller', None) is not None:
                    speed = getattr(s.limit_controller.kalman, 'speed', 0.0)
                name = escape_html(getattr(s, 'name', '') or '')
                lines.append(
                    f"{phase_emoji} <b>{name}</b>\n   ↑{fmt_speed(speed)} | ⏱{tl:.0f}s | 周期#{getattr(s, 'cycle_index', 0)}"
                )
            except Exception:
                continue

        status = "⏸️ 已暂停" if getattr(self.limit_engine, 'paused', False) else "▶️ 运行中"
        target_kib = getattr(self.limit_engine, 'temp_target_kib', None)
        if target_kib is None:
            # fallback：取第一条启用规则的目标速度
            target_kib = self._pick_default_target_kib() or 51200

        msg = "📊 <b>种子状态总览</b>\n━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
        if len(states) > 10:
            msg += f"\n\n... 还有 {len(states) - 10} 个种子"

        msg += f"\n━━━━━━━━━━━━━━━━━━━━━\n状态: {status} | 目标: <code>{fmt_speed(target_kib * 1024)}</code>"
        self.send_immediate(msg)

    def _cmd_pause(self):
        if not self.limit_engine:
            self.send_immediate("❌ 控制器未初始化")
            return
        self.limit_engine.paused = True
        self.paused = True
        msg = """⏸️ <b>限速功能已暂停</b>
━━━━━━━━━━━━━━━━━━━━━
所有种子将以最大速度运行
发送 /resume 恢复限速"""
        self.send_immediate(msg)

    def _cmd_resume(self):
        if not self.limit_engine:
            self.send_immediate("❌ 控制器未初始化")
            return
        self.limit_engine.paused = False
        self.paused = False
        msg = """▶️ <b>限速功能已恢复</b>
━━━━━━━━━━━━━━━━━━━━━
种子将按目标速度限制"""
        self.send_immediate(msg)

    def _cmd_limit(self, args: List[str]):
        if not self.limit_engine:
            self.send_immediate("❌ 控制器未初始化")
            return

        current = getattr(self.limit_engine, 'temp_target_kib', None)
        if current is None:
            current = self._pick_default_target_kib() or 51200

        if not args:
            msg = f"🎯 当前目标速度: <code>{fmt_speed(current * 1024)}</code>\n用法: /limit <速度> (如 100M)"
            self.send_immediate(msg)
            return

        speed = parse_speed_str(args[0])
        if speed is None or speed <= 0:
            self.send_immediate("❌ 无效的速度值\n例: /limit 100M 或 /limit 51200K")
            return

        old_limit = current
        new_limit = speed
        self.limit_engine.temp_target_kib = new_limit
        self.temp_target_kib = new_limit

        msg = f"""🎯 <b>目标速度已修改</b>
━━━━━━━━━━━━━━━━━━━━━
原速度: <code>{fmt_speed(old_limit * 1024)}</code>
新速度: <code>{fmt_speed(new_limit * 1024)}</code>
━━━━━━━━━━━━━━━━━━━━━
⚠️ 此为临时设置，重启后恢复
如需永久修改请编辑配置文件"""
        self.send_immediate(msg)

    def _cmd_log(self, args: List[str]):
        try:
            n = int(args[0]) if args else 10
            n = max(1, min(n, 50))
        except Exception:
            n = 10

        logs = self.log_buffer.get_recent(n)
        if not logs:
            self.send_immediate("📜 暂无日志记录")
            return

        msg = f"📜 <b>最近 {len(logs)} 条日志</b>\n━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(
            f"<code>{escape_html(l)}</code>" for l in logs
        )
        self.send_immediate(msg)

    def _cmd_cookie(self):
        if not self.site_helper_manager:
            self.send_immediate("❌ U2辅助功能未启用")
            return

        helper = self._get_u2_helper()
        if not helper or not getattr(helper, 'enabled', False):
            self.send_immediate("❌ U2辅助功能未启用")
            return

        self.send_immediate("🔍 正在检查 Cookie 状态...")
        try:
            ok, msg = helper.check_cookie_valid()
        except Exception as e:
            ok, msg = False, str(e)

        if ok:
            resp = f"""✅ <b>Cookie状态正常</b>
━━━━━━━━━━━━━━━━━━━━━
状态: <code>{escape_html(msg)}</code>
检查时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""
        else:
            resp = f"""❌ <b>Cookie已失效</b>
━━━━━━━━━━━━━━━━━━━━━
原因: <code>{escape_html(msg)}</code>
请及时更新Cookie！
检查时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""

        self.send_immediate(resp)

    def _cmd_config(self, args: List[str]):
        if len(args) < 2:
            self.send_immediate("❌ 用法: /config <key> <value>")
            return

        key, value = args[0], " ".join(args[1:])
        valid_keys = [
            'qb_host', 'qb_port', 'qb_user', 'qb_pass',
            'tg_token', 'tg_chat'
        ]
        if key not in valid_keys:
            msg = """❌ 无效配置项
可用配置项:
- qb_host
- qb_port
- qb_user
- qb_pass
- tg_token
- tg_chat"""
            self.send_immediate(msg)
            return

        # 保存到数据库 runtime_config（对齐脚本：需要重启生效）
        try:
            if key == 'qb_host':
                self.db.save_runtime_config('override_host', value)
            elif key == 'qb_port':
                self.db.save_runtime_config('override_port', value)
            elif key == 'qb_user':
                self.db.save_runtime_config('override_username', value)
            elif key == 'qb_pass':
                self.db.save_runtime_config('override_password', value)
            elif key == 'tg_token':
                self.db.save_runtime_config('override_tg_token', value)
            elif key == 'tg_chat':
                self.db.save_runtime_config('override_tg_chat', value)
        except Exception:
            pass

        resp = f"""✅ <b>配置已保存</b>
━━━━━━━━━━━━━━━━━━━━━
{key}: <code>{escape_html(value)}</code>
━━━━━━━━━━━━━━━━━━━━━
⚠️ 需要重启脚本生效"""
        self.send_immediate(resp)

    def _cmd_stats(self):
        if not self.limit_engine:
            self.send_immediate("❌ 控制器未初始化")
            return

        try:
            stats = self.db.get_limit_stats() if hasattr(self.db, 'get_limit_stats') else {}
        except Exception:
            stats = {}

        start = float(stats.get('start_time') or time.time())
        total = int(stats.get('total_cycles') or 0)
        success = int(stats.get('success_cycles') or 0)
        precision = int(stats.get('precision_cycles') or 0)
        uploaded = int(stats.get('total_limit_uploaded') or 0)

        runtime = time.time() - start
        success_rate = (success / total * 100) if total > 0 else 0
        precision_rate = (precision / total * 100) if total > 0 else 0

        msg = f"""📈 <b>运行统计</b>
━━━━━━━━━━━━━━━━━━━━━
⏱️ 运行时长: <code>{fmt_duration(runtime)}</code>

📊 <b>周期统计</b>
├ 总周期数: <code>{total}</code>
├ 达标率: <code>{success_rate:.1f}%</code> ({success}/{total})
└ 精准率: <code>{precision_rate:.1f}%</code> ({precision}/{total})

📤 <b>流量统计</b>
└ 总上传: <code>{fmt_size(uploaded)}</code>"""
        self.send_immediate(msg)

    # 扩展：查看所有站点 Cookie（不写入 /help）
    def _cmd_cookieall(self):
        if not self.site_helper_manager:
            self.send_immediate("❌ 站点辅助器未启用")
            return
        try:
            statuses = self.site_helper_manager.get_all_status() or []
        except Exception:
            statuses = []
        if not statuses:
            self.send_immediate("📋 未配置任何站点")
            return

        lines = []
        for st in statuses:
            site_id = st.get('site_id') or st.get('id')
            name = st.get('name') or st.get('site_name') or str(site_id)
            enabled = bool(st.get('enabled'))
            ok = st.get('cookie_valid')
            if ok is None:
                icon = '❓'
            else:
                icon = '✅' if ok else '❌'
            lines.append(f"{icon} {escape_html(name)} ({'启用' if enabled else '禁用'})")

        msg = "📋 <b>站点 Cookie 状态</b>\n━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
        self.send_immediate(msg)

    # ─────────────────────────────────────────────────────────────
    # 辅助：默认目标速度（KiB）
    # ─────────────────────────────────────────────────────────────
    def _pick_default_target_kib(self) -> Optional[int]:
        try:
            rules = self.db.get_speed_rules() if hasattr(self.db, 'get_speed_rules') else []
            # 优先默认规则(site_id is None)，其次第一条启用规则
            default = None
            for r in rules:
                if not r.get('enabled'):
                    continue
                if r.get('site_id') is None:
                    default = r
                    break
            if default is None:
                for r in rules:
                    if r.get('enabled'):
                        default = r
                        break
            if default:
                return int(default.get('target_speed_kib') or 0) or None
        except Exception:
            return None
        return None

    def _get_u2_helper(self):
        # 通过站点 URL / 名称匹配 u2.dmhy.org
        try:
            statuses = self.site_helper_manager.get_all_status() or []
        except Exception:
            statuses = []
        u2_site_id = None
        for st in statuses:
            url = (st.get('url') or st.get('site_url') or '').lower()
            name = (st.get('name') or st.get('site_name') or '').lower()
            if 'u2.dmhy.org' in url or name.strip() == 'u2':
                u2_site_id = st.get('site_id') or st.get('id')
                break
        if u2_site_id is None:
            # fallback：直接尝试 tracker 关键字
            try:
                return self.site_helper_manager.get_helper_by_tracker('u2.dmhy.org')
            except Exception:
                return None
        try:
            return self.site_helper_manager.get_helper(int(u2_site_id))
        except Exception:
            return None

    # ═══════════════════════════════════════════
    # 限速相关通知（文案严格对齐脚本）
    # ═══════════════════════════════════════════

    def startup(self, config: _StartupConfig, qb_version: str = "", u2_enabled: bool = False):
        msg = f"""🚀 <b>qBit Smart Limit v{C.VERSION} 已启动</b>
━━━━━━━━━━━━━━━━━━━━━
🎯 目标速度: <code>{fmt_speed(config.target_bytes)}</code>
🛡️ 安全边际: <code>{config.safety_margin:.1%}</code>
🔄 汇报优化: {'✅' if config.enable_reannounce_opt else '❌'}
📥 下载限速: {'✅' if config.enable_dl_limit else '❌'}
━━━━━━━━━━━━━━━━━━━━━
📊 <b>系统状态</b>
├ qBittorrent: <code>{escape_html(qb_version)}</code>
└ U2辅助: {'✅' if u2_enabled else '❌'}
━━━━━━━━━━━━━━━━━━━━━
启动时间: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"""
        self.send(msg, "startup", 0)

    def monitor_start(self, info: Dict[str, Any]):
        name = escape_html(info.get('name', ''))
        total_size = info.get('total_size', 0)
        target = info.get('target', 0)
        tid = info.get('tid')
        promo = escape_html(info.get('promotion', '未知'))

        if tid:
            name_link = f"<a href=\"https://u2.dmhy.org/details.php?id={tid}&hit=1\">{name}</a>"
        else:
            name_link = f"<b>{name}</b>"

        msg = f"""🎬 <b>开始监控新任务</b>
━━━━━━━━━━━━━━━━━━━━━
{name_link}
📦 大小: <code>{fmt_size(total_size)}</code>
🎯 目标均速: <code>{fmt_speed(target)}</code>
🍪 优惠状态: <code>{promo}</code>
━━━━━━━━━━━━━━━━━━━━━
开始时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""
        self.send(msg, f"monitor_{info.get('hash','')}", 0)

    def check_finish(self, info: Dict[str, Any]):
        h = info.get('hash')
        if not h or h in self._finish_notified:
            return
        if info.get('progress', 0) < 0.999:
            return
        self._finish_notified.add(h)

        name = escape_html(info.get('name',''))
        msg = f"""✅ <b>任务下载完成</b>
━━━━━━━━━━━━━━━━━━━━━
<b>{name}</b>
📤 总上传: <code>{fmt_size(info.get('total_uploaded',0))}</code>
📥 总下载: <code>{fmt_size(info.get('total_downloaded',0))}</code>
━━━━━━━━━━━━━━━━━━━━━
完成时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""
        self.send(msg, f"finish_{h}", 0)

    def cycle_report(self, info: Dict[str, Any]):
        name = escape_html(info.get('name',''))
        idx = info.get('idx', 0)
        ratio = info.get('ratio', 0) * 100
        speed = safe_div(info.get('uploaded',0), info.get('duration',1), 0)
        dev = abs(ratio - 100)

        if dev <= 0.1:
            grade = "🎯 PERFECT"
        elif dev <= 0.5:
            grade = "✅ EXCELLENT"
        elif ratio >= 95:
            grade = "👍 GOOD"
        else:
            grade = "⚠️ LOW"

        msg = f"""📊 <b>周期汇报 #{idx}</b>
━━━━━━━━━━━━━━━━━━━━━
<b>{name}</b>

📈 <b>本周期统计</b>
├ 上传量: <code>{fmt_size(info.get('uploaded',0))}</code>
├ 平均速度: <code>{fmt_speed(speed)}</code>
├ 达成率: <code>{ratio:.1f}%</code>
└ 偏差: <code>{dev:.2f}%</code>

🎯 评级: <b>{grade}</b>

📡 <b>总体统计</b>
├ 实际均速: <code>{fmt_speed(info.get('real_speed',0))}</code>
├ 下载进度: <code>{info.get('progress_pct',0):.1f}%</code>
└ 总上传: <code>{fmt_size(info.get('total_uploaded_life',0))}</code>
━━━━━━━━━━━━━━━━━━━━━
时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""
        self.send(msg, f"cycle_{info.get('hash','')}", 5)

    def overspeed_warning(self, name: str, real_speed: float, target: float, tid: Optional[int] = None):
        name = escape_html(name)
        if tid:
            name_link = f"<a href=\"https://u2.dmhy.org/details.php?id={tid}&hit=1\">{name}</a>"
        else:
            name_link = f"<b>{name}</b>"

        msg = f"""⚠️ <b>检测到超速风险！</b>
━━━━━━━━━━━━━━━━━━━━━
{name_link}

📈 当前均速: <code>{fmt_speed(real_speed)}</code>
🎯 目标均速: <code>{fmt_speed(target)}</code>

⚡ 已自动启动保护机制
━━━━━━━━━━━━━━━━━━━━━
时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""
        self.send(msg, f"overspeed_{name[:10]}", 120)

    def dl_limit_notify(self, name: str, dl_limit: float, reason: str, tid: Optional[int] = None):
        name = escape_html(name)
        if tid:
            name_link = f"<a href=\"https://u2.dmhy.org/details.php?id={tid}&hit=1\">{name}</a>"
        else:
            name_link = f"<b>{name}</b>"

        msg = f"""📥 <b>下载限速已启用</b>
━━━━━━━━━━━━━━━━━━━━━
{name_link}

🚦 限速值: <code>{fmt_speed(dl_limit * 1024)}</code>
📝 原因: <code>{escape_html(reason)}</code>
━━━━━━━━━━━━━━━━━━━━━
时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""
        self.send(msg, f"dl_limit_{name[:10]}", 60)

    def reannounce_notify(self, name: str, reason: str, tid: Optional[int] = None):
        name = escape_html(name)
        if tid:
            name_link = f"<a href=\"https://u2.dmhy.org/details.php?id={tid}&hit=1\">{name}</a>"
        else:
            name_link = f"<b>{name}</b>"

        msg = f"""🔄 <b>强制汇报</b>
━━━━━━━━━━━━━━━━━━━━━
{name_link}

📝 原因: <code>{escape_html(reason)}</code>
━━━━━━━━━━━━━━━━━━━━━
时间: <code>{datetime.now().strftime('%H:%M:%S')}</code>"""
        self.send(msg, f"reannounce_{name[:10]}", 60)

    def cookie_invalid_notify(self):
        msg = f"""🍪 <b>U2 Cookie 已失效!</b>
━━━━━━━━━━━━━━━━━━━━━
⚠️ 检测到 Cookie 无效
请及时更新配置文件中的 Cookie
━━━━━━━━━━━━━━━━━━━━━
时间: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"""
        self.send(msg, "cookie_invalid", 3600)

    def shutdown_report(self):
        msg = f"""🛑 <b>qBit Smart Limit 已停止</b>
━━━━━━━━━━━━━━━━━━━━━
停止时间: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>

💡 如需重启请运行脚本"""
        self.send(msg, "shutdown", 0)

    # ═══════════════════════════════════════════
    # 兼容旧接口（Web 端其它模块调用）
    # ═══════════════════════════════════════════

    def notify(self, title: str, message: str):
        """通用通知（非脚本文案，不影响限速核心通知复刻）。"""
        if not self.enabled:
            return
        title = escape_html(title)
        message = escape_html(message)
        msg = f"<b>{title}</b>\n━━━━━━━━━━━━━━━━━━━━━\n{message}"
        self.send(msg, f"generic_{title[:10]}", 0)

    def notify_torrent_added(self, torrent_name: str, site_name: str = ""):
        title = "➕ 新增种子"
        msg = f"{escape_html(torrent_name)}\n站点: {escape_html(site_name)}\n时间: {datetime.now().strftime('%H:%M:%S')}"
        self.notify(title, msg)

    def notify_torrent_removed(self, torrent_name: str, reason: str = ""):
        title = "🗑️ 移除种子"
        msg = f"{escape_html(torrent_name)}\n原因: {escape_html(reason)}\n时间: {datetime.now().strftime('%H:%M:%S')}"
        self.notify(title, msg)

    # 兼容旧方法名
    def notify_startup(self):
        # 若无法构建 config，则发送一个极简启动通知
        cfg = _StartupConfig(target_speed_kib=51200, safety_margin=0.98, enable_reannounce_opt=True, enable_dl_limit=True)
        self.startup(cfg, qb_version="", u2_enabled=False)

    def notify_cycle_report(self, info: Dict[str, Any]):
        self.cycle_report(info)

    def notify_overspeed(self, name: str, real_speed: float, target: float):
        self.overspeed_warning(name, real_speed, target)

    def notify_dl_limit(self, name: str, dl_limit: float, reason: str):
        # 历史版本可能传入 B/s，这里尽量兼容：如果数值很大，按 B/s 转 KiB/s
        if dl_limit > 1024 * 1024:
            dl_kib = dl_limit / 1024
        else:
            dl_kib = dl_limit
        self.dl_limit_notify(name, dl_kib, reason)

    def notify_reannounce(self, name: str, reason: str):
        self.reannounce_notify(name, reason)

    def notify_cookie_invalid(self, site_name: str = "", msg: str = ""):
        # 仅严格复刻脚本的 U2 Cookie 失效通知
        self.cookie_invalid_notify()

    def notify_limit_applied(self, torrent_name: str, limit: int, reason: str = ""):
        # 按用户要求：移除高频 notify_limit_applied 逻辑 => no-op
        return


def create_notifier(db):
    return Notifier(db)


# ─────────────────────────────────────────────────────────────
# safe_div 在本文件仅用于 cycle_report 速度计算（对齐脚本行为）
# ─────────────────────────────────────────────────────────────

def safe_div(a: float, b: float, default: float = 0) -> float:
    try:
        if b == 0 or abs(b) < 1e-10:
            return default
        return a / b
    except Exception:
        return default
