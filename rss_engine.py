#!/usr/bin/env python3
"""
RSS自动抓取引擎 v2.0

新特性:
- 参考VERTEX的新种抓取逻辑：只抓取真正的"新种子"
- 支持配置最大种子年龄（分钟级别，默认10分钟）
- 首次运行模式：只记录不添加，避免一次性添加大量旧种
- 增量抓取：只添加上次检查后新发布的种子
- 支持种子发布时间精确到秒级比较
"""

import os
import re
import time
import calendar
import json
import hashlib
import threading
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from collections import OrderedDict

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False


@dataclass
class RSSItem:
    title: str
    link: str
    torrent_url: str
    size: int = 0
    pub_date: Optional[datetime] = None
    info_hash: str = ""
    site_id: int = 0
    site_name: str = ""


@dataclass
class FetchResult:
    site_id: int
    site_name: str
    success: bool
    items_found: int = 0
    items_added: int = 0
    items_skipped: int = 0
    items_too_old: int = 0
    items_cached: int = 0
    error: str = ""
    timestamp: float = field(default_factory=time.time)
    mode: str = "normal"  # normal, first_run, incremental


class LRUCache:
    def __init__(self, capacity: int = 10000):
        self.cache: OrderedDict = OrderedDict()
        self.capacity = capacity
        self._lock = threading.Lock()
    
    def get(self, key: str) -> bool:
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return True
            return False
    
    def put(self, key: str, timestamp: float = None):
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            else:
                if len(self.cache) >= self.capacity:
                    self.cache.popitem(last=False)
                self.cache[key] = timestamp or time.time()
    
    def clear(self):
        with self._lock:
            self.cache.clear()
    
    def size(self) -> int:
        with self._lock:
            return len(self.cache)
    
    def to_list(self) -> List[str]:
        with self._lock:
            return list(self.cache.keys())
    
    def to_dict(self) -> Dict[str, float]:
        with self._lock:
            return dict(self.cache)
    
    def load_from_list(self, items: List[str]):
        with self._lock:
            self.cache.clear()
            for item in items[-self.capacity:]:
                self.cache[item] = time.time()
    
    def load_from_dict(self, data: Dict[str, float]):
        with self._lock:
            self.cache.clear()
            items = sorted(data.items(), key=lambda x: x[1])[-self.capacity:]
            for k, v in items:
                self.cache[k] = v


class RSSEngine:
    def __init__(self, db, qb_manager, notifier=None, logger=None):
        self.db = db
        self.qb_manager = qb_manager
        self.notifier = notifier
        self.logger = logger or logging.getLogger("rss_engine")
        
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()
        
        # 配置
        self._fetch_interval = 300  # 默认5分钟检查一次
        self._enabled = False
        self._min_free_space = 10 * 1024 * 1024 * 1024  # 10GB
        
        # VERTEX风格配置：只抓取刚发布的种子
        self._max_torrent_age_minutes = 60  # 默认只抓取60分钟内发布的种子（从10改为60）
        self._max_items_per_fetch = 10  # 每次最多添加10个种子（从5改为10）
        self._first_run_mode = True  # 首次运行仅添加最新种子（受最大年龄限制）
        
        # 缓存
        self._hash_cache = LRUCache(capacity=10000)
        self._last_fetch = {}  # site_id -> timestamp
        self._last_pub_date = {}  # site_id -> datetime (上次抓取到的最新发布时间)
        self._fetch_results = []
        self._max_results = 100
        self._first_run_done = {}  # site_id -> bool
        
        self._session = None
        if REQUESTS_AVAILABLE:
            self._session = requests.Session()
            self._session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
        
        self._load_state()
    
    def _load_state(self):
        """加载持久化状态"""
        try:
            # 加载hash缓存
            cache_str = self.db.get_config('rss_hash_cache')
            if cache_str:
                try:
                    cache_data = json.loads(cache_str)
                    if isinstance(cache_data, dict):
                        self._hash_cache.load_from_dict(cache_data)
                    else:
                        self._hash_cache.load_from_list(cache_data)
                except:
                    pass
            
            # 加载上次发布时间记录
            last_pub_str = self.db.get_config('rss_last_pub_date')
            if last_pub_str:
                try:
                    data = json.loads(last_pub_str)
                    for site_id, ts in data.items():
                        self._last_pub_date[int(site_id)] = datetime.fromtimestamp(ts, tz=timezone.utc)
                except:
                    pass
            
            # 加载首次运行标记
            first_run_str = self.db.get_config('rss_first_run_done')
            if first_run_str:
                try:
                    self._first_run_done = {int(k): v for k, v in json.loads(first_run_str).items()}
                except:
                    pass
                    
        except Exception as e:
            self._log('warning', f"加载RSS状态失败: {e}")
    
    def _save_state(self):
        """保存持久化状态"""
        try:
            # 保存hash缓存
            cache_dict = self._hash_cache.to_dict()
            self.db.set_config('rss_hash_cache', json.dumps(cache_dict))
            
            # 保存上次发布时间
            last_pub_data = {str(k): v.timestamp() for k, v in self._last_pub_date.items()}
            self.db.set_config('rss_last_pub_date', json.dumps(last_pub_data))
            
            # 保存首次运行标记
            self.db.set_config('rss_first_run_done', json.dumps(self._first_run_done))
            
        except Exception as e:
            self._log('warning', f"保存RSS状态失败: {e}")
    
    def _log(self, level: str, message: str):
        getattr(self.logger, level.lower(), self.logger.info)(message)
        try:
            self.db.add_log(level.upper(), f"[RSS] {message}")
        except:
            pass
    
    @staticmethod
    def _clean_cookie(cookie: str) -> str:
        """清理Cookie格式：将多行Cookie合并为单行"""
        if not cookie:
            return ''
        
        # 移除不可见字符
        cookie = re.sub(r'[\ufeff\ufffe\u200b\u200c\u200d\u2060\x00-\x1f\x7f-\x9f]', '', cookie)
        
        # 将换行符替换为分隔符
        cookie = cookie.replace('\r\n', ';').replace('\r', ';').replace('\n', ';')

        attribute_keys = {
            'path', 'domain', 'expires', 'max-age', 'secure', 'httponly', 'samesite',
        }

        if ';' not in cookie and cookie.count('=') > 1:
            cookie = re.sub(r'\s+', ';', cookie)

        # 分割并重新组合cookie
        seen_keys = set()
        parts = []
        for part in cookie.split(';'):
            part = part.strip()
            if part and '=' in part:
                key, value = part.split('=', 1)
                key = key.strip()
                value = value.strip()
                if not key or key.lower() in attribute_keys:
                    continue
                if key not in seen_keys:
                    seen_keys.add(key)
                    parts.append(f"{key}={value}")
        
        return '; '.join(parts)
    
    @staticmethod
    def _clean_url(url: str) -> str:
        """清理URL：移除不可见字符和前后空白"""
        if not url:
            return ''
        
        # 移除不可见字符
        url = re.sub(r'[\ufeff\ufffe\u200b\u200c\u200d\u2060\x00-\x1f\x7f-\x9f]', '', url)
        
        # 移除前后空白
        url = url.strip().strip('\u3000')
        
        return url

    @staticmethod
    def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
        if not dt:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    
    def start(self):
        if self._running:
            return
        
        self._enabled = self.db.get_config('rss_fetch_enabled') == 'true'
        try:
            self._fetch_interval = int(self.db.get_config('rss_fetch_interval') or 300)
        except:
            self._fetch_interval = 300
        
        # 加载最大种子年龄配置
        try:
            self._max_torrent_age_minutes = int(self.db.get_config('rss_max_age_minutes') or 60)
        except:
            self._max_torrent_age_minutes = 60
        
        if not self._enabled:
            self._log('info', "RSS引擎已禁用")
            return
        
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="RSS-Engine")
        self._thread.start()
        self._log('info', f"RSS引擎已启动 (间隔: {self._fetch_interval}秒, 最大年龄: {self._max_torrent_age_minutes}分钟)")
    
    def stop(self):
        self._running = False
        self._stop_event.set()
        self._save_state()
        self._log('info', "RSS引擎已停止")
    
    def enable(self):
        self._enabled = True
        self.db.set_config('rss_fetch_enabled', 'true')
        if not self._running:
            self.start()
    
    def disable(self):
        self._enabled = False
        self.db.set_config('rss_fetch_enabled', 'false')
    
    def set_interval(self, seconds: int):
        seconds = max(60, min(3600, seconds))
        self._fetch_interval = seconds
        self.db.set_config('rss_fetch_interval', str(seconds))
    
    def set_max_age(self, minutes: int):
        """设置最大种子年龄（分钟）"""
        minutes = max(1, min(1440, minutes))  # 1分钟到24小时
        self._max_torrent_age_minutes = minutes
        self.db.set_config('rss_max_age_minutes', str(minutes))
    
    def fetch_now(self, site_id: int = None) -> List[FetchResult]:
        return self._do_fetch(site_id)
    
    def clear_cache(self):
        self._hash_cache.clear()
        self._last_pub_date.clear()
        self._first_run_done.clear()
        self.db.set_config('rss_hash_cache', '{}')
        self.db.set_config('rss_last_pub_date', '{}')
        self.db.set_config('rss_first_run_done', '{}')
        self._log('info', "已清除RSS缓存和状态")
    
    def get_status(self) -> Dict[str, Any]:
        sites = self.db.get_pt_sites_with_rss()
        return {
            'enabled': self._enabled,
            'running': self._running,
            'fetch_interval': self._fetch_interval,
            'max_age_minutes': self._max_torrent_age_minutes,
            'cache_size': self._hash_cache.size(),
            'sites_count': len(sites),
            'sites': [{'id': s['id'], 'name': s['name'], 
                      'first_run_done': self._first_run_done.get(s['id'], False),
                      'last_fetch': self._last_fetch.get(s['id'])}
                     for s in sites],
            'last_fetch': self._last_fetch,
        }
    
    def get_results(self, limit: int = 50) -> List[Dict]:
        results = self._fetch_results[-limit:]
        return [{
            'site_id': r.site_id,
            'site_name': r.site_name,
            'success': r.success,
            'items_found': r.items_found,
            'items_added': r.items_added,
            'items_skipped': r.items_skipped,
            'items_too_old': r.items_too_old,
            'items_cached': r.items_cached,
            'mode': r.mode,
            'error': r.error,
            'time': datetime.fromtimestamp(r.timestamp).strftime('%Y-%m-%d %H:%M:%S'),
            'time_str': datetime.fromtimestamp(r.timestamp).strftime('%H:%M:%S')
        } for r in reversed(results)]
    
    def _worker(self):
        while self._running and not self._stop_event.is_set():
            try:
                self._enabled = self.db.get_config('rss_fetch_enabled') == 'true'
                try:
                    self._fetch_interval = int(self.db.get_config('rss_fetch_interval') or 300)
                    self._max_torrent_age_minutes = int(self.db.get_config('rss_max_age_minutes') or 10)
                except:
                    pass
                
                if self._enabled:
                    self._do_fetch()
            except Exception as e:
                self._log('error', f"RSS抓取异常: {e}")
            
            self._stop_event.wait(self._fetch_interval)
    
    def _do_fetch(self, site_id: int = None) -> List[FetchResult]:
        results = []
        
        if not REQUESTS_AVAILABLE:
            return results
        
        sites = self.db.get_pt_sites_with_rss()
        
        if site_id:
            sites = [s for s in sites if s['id'] == site_id]
        
        for site in sites:
            result = self._fetch_site(site)
            results.append(result)
            self._fetch_results.append(result)
            
            if len(self._fetch_results) > self._max_results:
                self._fetch_results = self._fetch_results[-self._max_results:]
        
        self._save_state()
        return results
    
    def _fetch_site(self, site: dict) -> FetchResult:
        site_id = site['id']
        site_name = site['name']
        rss_url = site.get('rss_url', '')
        cookie = site.get('cookie', '')
        
        # 清理URL和Cookie（防止格式问题）
        rss_url = self._clean_url(rss_url)
        cookie = self._clean_cookie(cookie)
        
        # 判断是否首次运行
        is_first_run = not self._first_run_done.get(site_id, False)
        
        result = FetchResult(
            site_id=site_id, 
            site_name=site_name, 
            success=False,
            mode='first_run' if is_first_run else 'incremental'
        )
        
        if not rss_url:
            result.error = "未配置RSS URL"
            return result
        
        try:
            headers = {}
            # 注意：大多数PT站RSS链接已包含passkey，不需要Cookie
            # Cookie主要用于下载.torrent文件时的认证
            if cookie:
                headers['Cookie'] = cookie
            
            mode_str = "首次运行(仅新增最新)" if is_first_run else "增量抓取"
            self._log('info', f"[{site_name}] 开始{mode_str}...")
            
            resp = self._session.get(rss_url, headers=headers, timeout=30)
            resp.raise_for_status()

            content_type = resp.headers.get('content-type', '')
            body_preview = resp.text.lstrip()[:200].lower() if resp.text else ''
            if 'html' in content_type.lower() and 'xml' not in content_type.lower():
                result.error = "RSS返回HTML，可能需要登录或Cookie无效"
                return result
            if body_preview.startswith('<!doctype html') or body_preview.startswith('<html'):
                result.error = "RSS返回HTML，可能需要登录或Cookie无效"
                return result
            
            items = self._parse_rss(resp.text, site)
            result.items_found = len(items)
            
            if not items:
                result.success = True
                self._log('info', f"[{site_name}] RSS为空")
                return result
            
            now = datetime.now(timezone.utc)
            max_age_seconds = self._max_torrent_age_minutes * 60
            
            # 获取上次抓取到的最新发布时间
            last_pub = self._to_utc(self._last_pub_date.get(site_id))
            
            added = 0
            skipped = 0
            too_old = 0
            cached = 0
            newest_pub_date = None
            
            # items已按发布时间倒序排列（最新的在前）
            for item in items:
                # 更新最新发布时间
                item.pub_date = self._to_utc(item.pub_date)
                if item.pub_date and (newest_pub_date is None or item.pub_date > newest_pub_date):
                    newest_pub_date = item.pub_date
                
                # 生成唯一标识
                hash_key = item.info_hash or hashlib.md5(item.torrent_url.encode()).hexdigest()
                
                # 检查是否已在缓存中
                if self._hash_cache.get(hash_key):
                    cached += 1
                    continue
                
                # 首次运行模式：只新增最新种子（有发布时间则受最大年龄限制）
                if is_first_run and self._first_run_mode:
                    if item.pub_date:
                        age_seconds = (now - item.pub_date).total_seconds()
                        if age_seconds > max_age_seconds:
                            too_old += 1
                            self._hash_cache.put(hash_key, time.time())
                            continue
                
                # 检查种子年龄
                if item.pub_date:
                    age_seconds = (now - item.pub_date).total_seconds()
                    
                    # 跳过太旧的种子
                    if age_seconds > max_age_seconds:
                        too_old += 1
                        # 也记录到缓存，避免下次再检查
                        self._hash_cache.put(hash_key, time.time())
                        continue
                    
                    # 增量模式：只添加比上次记录更新的种子
                    if last_pub and item.pub_date <= last_pub:
                        skipped += 1
                        self._hash_cache.put(hash_key, time.time())
                        continue
                
                # 限制每次添加数量
                if added >= self._max_items_per_fetch:
                    self._log('info', f"[{site_name}] 已达到单次添加上限 ({self._max_items_per_fetch})")
                    break
                
                # 选择实例并添加
                instance = self._select_best_instance(item.size, site)
                if not instance:
                    skipped += 1
                    self._hash_cache.put(hash_key, time.time())
                    continue
                
                if self._add_torrent(instance, item, cookie):
                    self._hash_cache.put(hash_key, time.time())
                    added += 1
                    
                    # 计算年龄字符串
                    age_str = ""
                    if item.pub_date:
                        age_seconds = (now - item.pub_date).total_seconds()
                        if age_seconds < 60:
                            age_str = f" ({int(age_seconds)}秒前)"
                        elif age_seconds < 3600:
                            age_str = f" ({int(age_seconds / 60)}分钟前)"
                        else:
                            age_str = f" ({int(age_seconds / 3600)}小时前)"
                    
                    self._log('info', f"[{site_name}] ✅ 添加: {item.title[:40]}{age_str}")
                    
                    # 发送通知
                    if self.notifier:
                        try:
                            self.notifier.notify_torrent_added(item.title, site_name)
                        except:
                            pass
                else:
                    skipped += 1
                    self._hash_cache.put(hash_key, time.time())
            
            # 更新最新发布时间记录
            if newest_pub_date:
                self._last_pub_date[site_id] = newest_pub_date
            
            # 首次运行完成，标记
            if is_first_run:
                self._first_run_done[site_id] = True
                self._log('info', f"[{site_name}] 首次运行完成，已记录 {len(items)} 个种子hash")
            
            result.items_added = added
            result.items_skipped = skipped
            result.items_too_old = too_old
            result.items_cached = cached
            result.success = True
            self._last_fetch[site_id] = time.time()
            
            # 日志
            log_parts = [f"发现{len(items)}个"]
            if added > 0:
                log_parts.append(f"添加{added}个")
            if too_old > 0:
                log_parts.append(f"过旧{too_old}个")
            if cached > 0:
                log_parts.append(f"已缓存{cached}个")
            if skipped > 0:
                log_parts.append(f"跳过{skipped}个")
            self._log('info', f"[{site_name}] 完成: {', '.join(log_parts)}")
            
        except requests.exceptions.Timeout:
            result.error = "请求超时"
        except requests.exceptions.RequestException as e:
            result.error = f"请求失败: {str(e)[:50]}"
        except Exception as e:
            result.error = f"解析失败: {str(e)[:50]}"
            self._log('error', f"[{site_name}] 错误: {e}")
        
        return result
    
    def _parse_rss(self, content: str, site: dict) -> List[RSSItem]:
        items = []
        
        # 清理内容开头的BOM和空白字符
        content = content.lstrip('\ufeff\ufffe')  # 移除BOM
        content = content.strip()  # 移除首尾空白
        
        # 确保以 < 开头（XML声明或根元素）
        if content and not content.startswith('<'):
            # 找到第一个 < 的位置
            xml_start = content.find('<')
            if xml_start > 0:
                content = content[xml_start:]
        
        if FEEDPARSER_AVAILABLE:
            try:
                feed = feedparser.parse(content)
                
                # 检查feedparser是否报告错误
                if feed.bozo and hasattr(feed, 'bozo_exception'):
                    bozo_msg = str(feed.bozo_exception)
                    # 如果有entries，忽略非严重错误继续处理
                    if not feed.entries:
                        raise Exception(f"RSS解析错误: {bozo_msg[:80]}")
                
                for entry in feed.entries:
                    pub_date = None
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        try:
                            timestamp = calendar.timegm(entry.published_parsed)
                            pub_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                        except:
                            pass
                    
                    item = RSSItem(
                        title=entry.get('title', ''),
                        link=entry.get('link', ''),
                        torrent_url=self._extract_torrent_url(entry),
                        size=self._parse_size(entry),
                        pub_date=pub_date,
                        info_hash=self._extract_hash(entry),
                        site_id=site['id'],
                        site_name=site['name']
                    )
                    if item.torrent_url:
                        items.append(item)
                
                # 按发布时间倒序排列（最新的在前）
                min_utc = datetime.min.replace(tzinfo=timezone.utc)
                items.sort(key=lambda x: x.pub_date or min_utc, reverse=True)
                return items
            except Exception as e:
                # 记录错误但继续尝试XML回退
                self._log('debug', f"[{site.get('name')}] feedparser失败: {e}")
        
        # XML fallback
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(content)
            
            for item_elem in root.iter('item'):
                title = item_elem.find('title')
                link = item_elem.find('link')
                enclosure = item_elem.find('enclosure')
                pub_date_elem = item_elem.find('pubDate')
                
                torrent_url = ''
                size = 0
                pub_date = None
                
                if enclosure is not None:
                    torrent_url = enclosure.get('url', '')
                    try:
                        size = int(enclosure.get('length', 0))
                    except:
                        pass
                elif link is not None:
                    torrent_url = link.text or ''
                
                if pub_date_elem is not None and pub_date_elem.text:
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_date = parsedate_to_datetime(pub_date_elem.text)
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                        else:
                            pub_date = pub_date.astimezone(timezone.utc)
                    except:
                        pass
                
                if torrent_url:
                    items.append(RSSItem(
                        title=title.text if title is not None else '',
                        link=link.text if link is not None else '',
                        torrent_url=torrent_url,
                        size=size,
                        pub_date=pub_date,
                        site_id=site['id'],
                        site_name=site['name']
                    ))
            
            # 按发布时间倒序排列
            min_utc = datetime.min.replace(tzinfo=timezone.utc)
            items.sort(key=lambda x: x.pub_date or min_utc, reverse=True)
        except:
            pass
        
        return items
    
    def _extract_torrent_url(self, entry) -> str:
        for link in entry.get('links', []):
            if link.get('type') == 'application/x-bittorrent':
                return link.get('href', '')
            if 'torrent' in link.get('href', '').lower():
                return link.get('href', '')
        
        link = entry.get('link', '')
        if 'torrent' in link.lower() or link.endswith('.torrent'):
            return link
        
        for enc in entry.get('enclosures', []):
            url = enc.get('url', enc.get('href', ''))
            if url:
                return url
        
        return ''
    
    def _parse_size(self, entry) -> int:
        for enc in entry.get('enclosures', []):
            try:
                return int(enc.get('length', 0))
            except:
                pass
        return 0
    
    def _extract_hash(self, entry) -> str:
        link = entry.get('link', '')
        match = re.search(r'([a-fA-F0-9]{40})', link)
        if match:
            return match.group(1).lower()
        return ''
    
    def _select_best_instance(self, torrent_size: int, site: Optional[dict] = None) -> Optional[dict]:
        instances = self.db.get_qb_instances()
        preferred_instance_id = site.get('preferred_instance_id') if site else None
        if preferred_instance_id:
            for inst in instances:
                if inst['id'] == preferred_instance_id and inst.get('enabled'):
                    if self.qb_manager.is_connected(inst['id']):
                        return inst
                    self._log('warning', f"指定实例未连接: {inst['name']}")
                    return None
            self._log('warning', f"未找到指定实例: {preferred_instance_id}")
            return None

        candidates = []
        
        for inst in instances:
            if not inst.get('enabled'):
                continue
            
            inst_id = inst['id']
            if not self.qb_manager.is_connected(inst_id):
                continue
            
            free_space = self.qb_manager.get_free_space(inst_id)
            
            required = torrent_size + self._min_free_space
            if free_space >= required:
                candidates.append({
                    'instance': inst,
                    'free_space': free_space
                })
        
        if not candidates:
            return None
        
        candidates.sort(key=lambda x: x['free_space'], reverse=True)
        return candidates[0]['instance']
    
    def _add_torrent(self, instance: dict, item: RSSItem, cookie: str = '') -> bool:
        try:
            headers = {}
            if cookie:
                # 确保cookie格式正确
                cookie = self._clean_cookie(cookie)
                headers['Cookie'] = cookie
            
            # 清理torrent URL
            torrent_url = self._clean_url(item.torrent_url)
            
            resp = self._session.get(torrent_url, headers=headers, timeout=30)
            resp.raise_for_status()
            
            content_type = resp.headers.get('content-type', '')
            if 'html' in content_type.lower():
                # 如果返回HTML，可能是需要登录或Cookie无效
                self._log('warning', f"下载种子返回HTML，可能Cookie无效或需要登录")
                return False
            
            success, msg = self.qb_manager.add_torrent(
                instance_id=instance['id'],
                torrent_file=resp.content
            )
            
            if success:
                try:
                    self.db.update_stats(total_added=1)
                except:
                    pass
            
            return success
            
        except Exception as e:
            self._log('error', f"添加种子失败: {str(e)[:50]}")
            return False


def create_rss_engine(db, qb_manager, notifier=None) -> RSSEngine:
    logger = logging.getLogger("rss_engine")
    return RSSEngine(db, qb_manager, notifier, logger)
