#!/usr/bin/env python3
"""
U2网页辅助模块 v1.0

功能:
- Cookie有效性检测
- TID搜索（通过种子hash）
- 促销信息检测（Free/2x/50%等）
- Peer List信息获取（精确汇报时间）
- 发布时间获取

与主脚本(main.py)功能一致，适配Web平台使用
"""

import re
import time
import threading
import logging
from datetime import datetime
from functools import reduce
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

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


@dataclass
class TorrentU2Info:
    """种子的U2信息"""
    torrent_hash: str
    tid: Optional[int] = None
    publish_time: Optional[float] = None
    promotion: str = "未知"
    last_announce: Optional[float] = None
    uploaded_on_site: int = 0
    reannounce_in: Optional[int] = None  # 距离下次汇报的秒数
    searched: bool = False
    search_time: float = 0
    error: str = ""


class U2WebHelper:
    """U2网页辅助类"""
    
    VERSION = "1.0.0"
    BASE_URL = "https://u2.dmhy.org"
    
    # 促销图标类名映射
    PROMO_CLASSES = {
        'pro_free2up': ['Free', '2x'],
        'pro_free': ['Free'],
        'pro_2up': ['2x'],
        'pro_50pct': ['50%'],
        'pro_30pct': ['30%'],
        'pro_custom': ['Custom'],
    }
    
    def __init__(self, cookie: str = "", proxy: str = "", logger=None):
        """
        初始化U2辅助器
        
        Args:
            cookie: U2的nexusphp_u2 cookie值
            proxy: 代理地址 (可选)
            logger: 日志记录器
        """
        self.cookie = cookie
        self.proxy = proxy
        self.logger = logger or logging.getLogger("u2_helper")
        
        self._lock = threading.Lock()
        self._cookie_valid = True
        self._last_cookie_check = 0
        
        # HTTP会话
        self.session = None
        self.cookies = {}
        self.enabled = False
        
        if REQUESTS_AVAILABLE and cookie:
            self.session = requests.Session()
            self.session.headers['User-Agent'] = f'qBit-Smart-Web/U2Helper-{self.VERSION}'
            self.cookies = {'nexusphp_u2': cookie}
            self.enabled = BS4_AVAILABLE
        
        # 缓存
        self._tid_cache: Dict[str, TorrentU2Info] = {}
        self._cache_max_size = 1000
    
    def _log(self, level: str, message: str):
        """记录日志"""
        getattr(self.logger, level.lower(), self.logger.info)(message)
    
    def _request(self, url: str, timeout: int = 15) -> Optional[str]:
        """发送HTTP请求"""
        if not self.session:
            return None
        
        try:
            proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
            resp = self.session.get(url, cookies=self.cookies, proxies=proxies, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
            else:
                self._log('warning', f"请求失败 HTTP {resp.status_code}: {url}")
        except Exception as e:
            self._log('debug', f"请求异常 {url}: {e}")
        return None
    
    def close(self):
        """关闭会话"""
        if self.session:
            try:
                self.session.close()
            except:
                pass
    
    def update_cookie(self, cookie: str):
        """更新Cookie"""
        self.cookie = cookie
        self.cookies = {'nexusphp_u2': cookie} if cookie else {}
        self.enabled = bool(cookie) and BS4_AVAILABLE and REQUESTS_AVAILABLE
        self._cookie_valid = True
        self._log('info', "U2 Cookie已更新")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Cookie检测
    # ═══════════════════════════════════════════════════════════════════════════
    
    def check_cookie_valid(self) -> Tuple[bool, str]:
        """
        检查Cookie是否有效
        
        Returns:
            (是否有效, 状态消息)
        """
        if not self.enabled:
            return False, "未配置Cookie或缺少依赖"
        
        try:
            html = self._request(f'{self.BASE_URL}/index.php', timeout=10)
            if not html:
                return False, "无法连接到U2"
            
            # 检查登录状态特征
            if 'logout.php' in html or '登出' in html or 'userdetails.php' in html:
                self._cookie_valid = True
                self._last_cookie_check = time.time()
                return True, "Cookie有效"
            else:
                self._cookie_valid = False
                return False, "Cookie已失效，请重新登录获取"
                
        except Exception as e:
            return False, f"检查失败: {e}"
    
    def is_cookie_valid(self) -> bool:
        """返回Cookie是否有效"""
        return self._cookie_valid
    
    # ═══════════════════════════════════════════════════════════════════════════
    # TID搜索
    # ═══════════════════════════════════════════════════════════════════════════
    
    def search_tid_by_hash(self, torrent_hash: str) -> Optional[TorrentU2Info]:
        """
        通过种子hash搜索TID和促销信息
        
        Args:
            torrent_hash: 种子的info_hash
            
        Returns:
            TorrentU2Info 或 None
        """
        if not self.enabled:
            return None
        
        # 检查缓存
        cache_key = torrent_hash.lower()
        if cache_key in self._tid_cache:
            cached = self._tid_cache[cache_key]
            # 缓存1小时
            if time.time() - cached.search_time < 3600:
                return cached
        
        info = TorrentU2Info(torrent_hash=torrent_hash)
        
        try:
            url = f'{self.BASE_URL}/torrents.php?search={torrent_hash}&search_area=5'
            html = self._request(url)
            if not html:
                info.error = "请求失败"
                return info
            
            with self._lock:
                soup = BeautifulSoup(html.replace('\n', ''), 'lxml')
                table = soup.select('table.torrents')
                
                if not table or len(table[0].contents) <= 1:
                    info.error = "未找到种子"
                    info.searched = True
                    info.search_time = time.time()
                    return info
                
                row = table[0].contents[1]
                if not hasattr(row, 'contents') or len(row.contents) < 2:
                    info.error = "解析失败"
                    return info
                
                # 获取TID
                try:
                    link = row.contents[1]
                    href = ""
                    if hasattr(link, 'find'):
                        a_tag = link.find('a')
                        href = a_tag.get('href', '') if a_tag else ''
                    
                    match = re.search(r'id=(\d+)', href)
                    if match:
                        info.tid = int(match.group(1))
                except Exception as e:
                    self._log('debug', f"获取TID失败: {e}")
                
                # 获取发布时间
                try:
                    if len(row.contents) > 3:
                        time_cell = row.contents[3]
                        if hasattr(time_cell, 'find'):
                            time_elem = time_cell.find('time')
                            if time_elem:
                                date_str = time_elem.get('title') or time_elem.get_text(' ')
                                if date_str:
                                    dt = datetime.strptime(date_str.strip(), '%Y-%m-%d %H:%M:%S')
                                    info.publish_time = dt.timestamp()
                except Exception as e:
                    self._log('debug', f"获取发布时间失败: {e}")
                
                # 获取促销信息
                try:
                    promos = []
                    imgs = row.contents[1].find_all('img')
                    for img in imgs:
                        classes = img.get('class', [])
                        if not classes:
                            continue
                        c_str = " ".join(classes) if isinstance(classes, list) else str(classes)
                        
                        for class_name, promo_types in self.PROMO_CLASSES.items():
                            if class_name in c_str:
                                promos.extend(promo_types)
                    
                    if promos:
                        info.promotion = " + ".join(sorted(list(set(promos)), key=lambda x: len(x), reverse=True))
                    else:
                        info.promotion = "无优惠"
                except Exception as e:
                    self._log('debug', f"获取促销信息失败: {e}")
                    info.promotion = "未知"
                
                info.searched = True
                info.search_time = time.time()
                
                # 缓存结果
                self._cache_result(cache_key, info)
                
                if info.tid:
                    self._log('info', f"🔍 Hash {torrent_hash[:8]}... → tid={info.tid} | 优惠: {info.promotion}")
                
                return info
                
        except Exception as e:
            self._log('error', f"搜索TID失败: {e}")
            info.error = str(e)
            return info
    
    def _cache_result(self, key: str, info: TorrentU2Info):
        """缓存搜索结果"""
        # 限制缓存大小
        if len(self._tid_cache) >= self._cache_max_size:
            # 删除最旧的条目
            oldest_key = min(self._tid_cache.keys(), 
                           key=lambda k: self._tid_cache[k].search_time)
            del self._tid_cache[oldest_key]
        
        self._tid_cache[key] = info
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Peer List信息（精确汇报时间）
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_peer_list_info(self, tid: int) -> Optional[Dict[str, Any]]:
        """
        获取Peer List信息，包含精确的汇报时间
        
        Args:
            tid: 种子ID
            
        Returns:
            {
                'uploaded': int,        # 站点记录的上传量
                'last_announce': float, # 上次汇报时间戳
                'idle_seconds': int,    # 空闲秒数
                'reannounce_in': int,   # 距离下次汇报的估算秒数
            }
        """
        if not self.enabled or not tid or tid < 0:
            return None
        
        try:
            url = f'{self.BASE_URL}/viewpeerlist.php?id={tid}'
            html = self._request(url)
            if not html:
                return None
            
            with self._lock:
                soup = BeautifulSoup(html.replace('\n', ' '), 'lxml')
                tables = soup.find_all('table')
                result = {}
                
                for table in tables or []:
                    rows = table.find_all('tr')
                    for tr in rows:
                        # 查找有背景色的行（数据行）
                        if not tr.get('bgcolor'):
                            continue
                        
                        tds = tr.find_all('td')
                        if len(tds) < 2:
                            continue
                        
                        # 获取上传量 (第2列)
                        try:
                            uploaded_str = tds[1].get_text(' ').strip()
                            if uploaded_str:
                                result['uploaded'] = self._parse_size(uploaded_str)
                        except:
                            pass
                        
                        # 获取空闲时间 (第11列，格式 HH:MM:SS 或 MM:SS)
                        try:
                            if len(tds) > 10:
                                idle_str = tds[10].get_text(' ').strip()
                                if ':' in idle_str:
                                    parts = list(map(int, idle_str.split(':')))
                                    # 转换为秒数
                                    idle_seconds = reduce(lambda a, b: a * 60 + b, parts)
                                    result['idle_seconds'] = idle_seconds
                                    result['last_announce'] = time.time() - idle_seconds
                                    
                                    # 估算下次汇报时间
                                    # U2默认汇报间隔约1800秒，根据种子年龄可能更长
                                    announce_interval = 1800  # 基础间隔30分钟
                                    result['reannounce_in'] = max(0, announce_interval - idle_seconds)
                        except Exception as e:
                            self._log('debug', f"解析空闲时间失败: {e}")
                        
                        if result:
                            break
                    if result:
                        break
                
                return result if result else None
                
        except Exception as e:
            self._log('debug', f"获取PeerList失败: {e}")
            return None
    
    @staticmethod
    def _parse_size(size_str: str) -> int:
        """解析大小字符串"""
        try:
            parts = size_str.strip().split()
            if len(parts) != 2:
                return 0
            num = float(parts[0].replace(',', '.'))
            unit = parts[1]
            units = {'B': 0, 'KiB': 1, 'MiB': 2, 'GiB': 3, 'TiB': 4, 'PiB': 5,
                    'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4, 'PB': 5}
            exp = units.get(unit, 0)
            return int(num * (1024 ** exp))
        except:
            return 0
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 综合查询
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_torrent_info(self, torrent_hash: str, include_peer_info: bool = True) -> TorrentU2Info:
        """
        获取种子的完整U2信息
        
        Args:
            torrent_hash: 种子hash
            include_peer_info: 是否包含peer列表信息（需要额外请求）
            
        Returns:
            TorrentU2Info
        """
        # 先搜索TID
        info = self.search_tid_by_hash(torrent_hash)
        
        if not info:
            info = TorrentU2Info(torrent_hash=torrent_hash, error="搜索失败")
            return info
        
        # 如果找到TID且需要peer信息，获取详细信息
        if info.tid and include_peer_info:
            peer_info = self.get_peer_list_info(info.tid)
            if peer_info:
                info.uploaded_on_site = peer_info.get('uploaded', 0)
                info.last_announce = peer_info.get('last_announce')
                info.reannounce_in = peer_info.get('reannounce_in')
        
        return info
    
    def get_reannounce_time(self, torrent_hash: str = None, tid: int = None) -> Optional[int]:
        """
        获取距离下次汇报的秒数
        
        Args:
            torrent_hash: 种子hash (二选一)
            tid: 种子ID (二选一)
            
        Returns:
            距离下次汇报的秒数，或None
        """
        if not self.enabled:
            return None
        
        # 如果只有hash，先搜索TID
        if tid is None and torrent_hash:
            info = self.search_tid_by_hash(torrent_hash)
            if info and info.tid:
                tid = info.tid
        
        if not tid:
            return None
        
        peer_info = self.get_peer_list_info(tid)
        if peer_info:
            return peer_info.get('reannounce_in')
        
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 状态和统计
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_status(self) -> Dict[str, Any]:
        """获取U2辅助器状态"""
        return {
            'enabled': self.enabled,
            'cookie_valid': self._cookie_valid,
            'last_cookie_check': self._last_cookie_check,
            'cache_size': len(self._tid_cache),
            'has_cookie': bool(self.cookie),
            'has_bs4': BS4_AVAILABLE,
            'has_requests': REQUESTS_AVAILABLE,
        }
    
    def clear_cache(self):
        """清除TID缓存"""
        self._tid_cache.clear()
        self._log('info', "U2 TID缓存已清除")


# 工厂函数
def create_u2_helper(cookie: str = "", proxy: str = "") -> U2WebHelper:
    """创建U2辅助器实例"""
    logger = logging.getLogger("u2_helper")
    return U2WebHelper(cookie, proxy, logger)
