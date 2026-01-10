#!/usr/bin/env python3
"""
通用PT站点辅助模块 v1.0

支持所有NexusPHP架构的PT站点：
- 通过种子hash搜索TID
- 获取Peer List中的精确汇报时间
- 促销信息检测
- Cookie有效性检查

支持的站点架构：
- NexusPHP (大多数国内PT站)
- Gazelle (部分国外站)
- Unit3D (新架构站点)

工作流程：
┌─────────────────────────────────────────────────────────────┐
│  1. 优先使用站点网页获取汇报时间（更精确）                      │
│     ↓ 失败则                                                 │
│  2. 回退到qBittorrent API                                    │
└─────────────────────────────────────────────────────────────┘
"""

import re
import time
import threading
import logging
from datetime import datetime
from functools import reduce
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from urllib.parse import urlparse, urljoin
from enum import Enum

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


class SiteType(Enum):
    """站点架构类型"""
    NEXUSPHP = "nexusphp"      # 大多数国内PT站
    GAZELLE = "gazelle"        # 部分国外站
    UNIT3D = "unit3d"          # 新架构
    UNKNOWN = "unknown"


@dataclass
class TorrentSiteInfo:
    """种子的站点信息"""
    torrent_hash: str
    site_id: int = 0
    site_name: str = ""
    tid: Optional[int] = None
    publish_time: Optional[float] = None
    promotion: str = "未知"
    last_announce: Optional[float] = None
    uploaded_on_site: int = 0
    reannounce_in: Optional[int] = None  # 距离下次汇报的秒数
    searched: bool = False
    search_time: float = 0
    error: str = ""
    source: str = ""  # 数据来源: "site" 或 "qb_api"


@dataclass
class PTSiteConfig:
    """PT站点配置"""
    id: int
    name: str
    url: str
    cookie: str = ""
    tracker_keyword: str = ""
    enabled: bool = True
    site_type: SiteType = SiteType.NEXUSPHP
    
    # 站点特定配置
    search_path: str = "/torrents.php"
    search_param: str = "search"
    hash_search_area: str = "5"  # NexusPHP: 5=hash搜索
    peerlist_path: str = "/viewpeerlist.php"
    
    # Cookie名称（不同站点可能不同）
    cookie_names: List[str] = field(default_factory=lambda: [
        "c_secure_uid", "c_secure_pass",  # 常见格式
        "nexusphp_u2",  # U2
        "PHPSESSID",    # 通用
    ])
    
    # 汇报间隔估算（秒）
    announce_interval: int = 1800


# ════════════════════════════════════════════════════════════════════════════════
# 站点特定配置预设
# ════════════════════════════════════════════════════════════════════════════════
SITE_PRESETS: Dict[str, dict] = {
    # U2
    "u2.dmhy.org": {
        "site_type": SiteType.NEXUSPHP,
        "cookie_names": ["nexusphp_u2"],
        "announce_interval": 1800,
    },
    # 馒头
    "kp.m-team.cc": {
        "site_type": SiteType.NEXUSPHP,
        "cookie_names": ["c_secure_uid", "c_secure_pass"],
        "announce_interval": 1800,
    },
    "xp.m-team.io": {
        "site_type": SiteType.NEXUSPHP,
        "cookie_names": ["c_secure_uid", "c_secure_pass"],
        "announce_interval": 1800,
    },
    # 红叶
    "leaves.red": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 观众
    "audiences.me": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # HDSky
    "hdsky.me": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 朱雀
    "zhuque.in": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 海胆
    "haidan.video": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 猫站
    "pterclub.com": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 我堡
    "www.ourbits.club": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 冬樱
    "wintersakura.net": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 幼儿园
    "pt.ecust.pp.ua": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 1PTBA
    "1ptba.com": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 聆音
    "pt.soulvoice.club": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 麒麟
    "www.htpt.cc": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # 柠檬
    "leaguehd.com": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # CHDBits
    "chdbits.co": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    "www.chdbits.co": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # HDChina
    "hdchina.org": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    "www.hdchina.org": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # TTG
    "totheglory.im": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # KeepFRDS
    "keepfrds.com": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    "www.keepfrds.com": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # PTHome
    "pthome.net": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    "www.pthome.net": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # HDHome
    "hdhome.org": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    "www.hdhome.org": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # LemonHD（常见别名域名）
    "lemonhd.org": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    "www.lemonhd.org": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # BTSchool
    "pt.btschool.club": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    "btschool.club": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # BYRPT
    "byr.pt": {
        "site_type": SiteType.NEXUSPHP,
        "announce_interval": 1800,
    },
    # Gazelle站点示例
    "passthepopcorn.me": {
        "site_type": SiteType.GAZELLE,
        "search_path": "/torrents.php",
        "peerlist_path": "/torrents.php",
        "announce_interval": 3600,
    },
}


class PTSiteHelper:
    """通用PT站点辅助类"""
    
    VERSION = "1.0.0"
    
    # NexusPHP促销图标类名映射
    PROMO_CLASSES = {
        'pro_free2up': ['Free', '2x'],
        'pro_free': ['Free'],
        'pro_2up': ['2x'],
        'pro_50pct': ['50%'],
        'pro_30pct': ['30%'],
        'pro_custom': ['Custom'],
        'free': ['Free'],
        'twoup': ['2x'],
        'twoupfree': ['Free', '2x'],
        'halfdown': ['50%'],
        'thirtypercent': ['30%'],
    }
    
    def __init__(self, site_config: PTSiteConfig, proxy: str = "", logger=None):
        """
        初始化站点辅助器
        
        Args:
            site_config: 站点配置
            proxy: 代理地址 (可选)
            logger: 日志记录器
        """
        self.config = site_config
        self.proxy = proxy
        self.logger = logger or logging.getLogger(f"pt_helper_{site_config.name}")
        
        self._lock = threading.Lock()
        self._cookie_valid = False
        self._last_cookie_check = 0
        
        # 应用站点预设
        self._apply_preset()
        
        # HTTP会话
        self.session = None
        self.cookies = {}
        self.enabled = False
        
        if REQUESTS_AVAILABLE and site_config.cookie:
            self.session = requests.Session()
            self.session.headers['User-Agent'] = (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            self.cookies = self._parse_cookie(site_config.cookie)
            self.user_id = self._extract_user_id()
            self.enabled = BS4_AVAILABLE and bool(self.cookies)
        
        # 缓存
        self._tid_cache: Dict[str, TorrentSiteInfo] = {}
        self._cache_max_size = 1000
        self._user_id_checked = False
    
    def _apply_preset(self):
        """应用站点预设配置"""
        try:
            parsed = urlparse(self.config.url)
            domain = parsed.netloc.lower()
            
            # 查找匹配的预设
            for preset_domain, preset_config in SITE_PRESETS.items():
                if preset_domain in domain:
                    self.config.site_type = preset_config.get("site_type", SiteType.NEXUSPHP)
                    if "cookie_names" in preset_config:
                        self.config.cookie_names = preset_config["cookie_names"]
                    if "announce_interval" in preset_config:
                        self.config.announce_interval = preset_config["announce_interval"]
                    if "search_path" in preset_config:
                        self.config.search_path = preset_config["search_path"]
                    if "peerlist_path" in preset_config:
                        self.config.peerlist_path = preset_config["peerlist_path"]
                    self._log('debug', f"应用预设配置: {preset_domain}")
                    return
        except Exception as e:
            self._log('debug', f"应用预设失败: {e}")
    
    def _parse_cookie(self, cookie_str: str) -> dict:
        """
        解析Cookie字符串，支持多行格式
        
        支持的格式：
        1. 单行: "name=value; name2=value2"
        2. 多行: "name=value;\nname2=value2"
        3. 纯value（假设是主要cookie）
        """
        cookies = {}
        if not cookie_str:
            return cookies
        
        # 先清理Cookie格式
        import re
        # 移除不可见字符（BOM、零宽字符等）
        cookie_str = re.sub(r'[\ufeff\ufffe\u200b\u200c\u200d\u2060\x00-\x1f\x7f-\x9f]', '', cookie_str)
        
        # 将换行符替换为分号（支持多行格式）
        cookie_str = cookie_str.replace('\r\n', ';').replace('\r', ';').replace('\n', ';')
        
        # 支持多种格式
        if '=' in cookie_str:
            for part in cookie_str.split(';'):
                part = part.strip()
                if '=' in part:
                    key, value = part.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    if key:  # 确保key不为空
                        cookies[key] = value
        else:
            # 尝试使用预设的cookie名称
            for name in self.config.cookie_names:
                cookies[name] = cookie_str.strip()
                break
        
        return cookies

    def _extract_user_id(self) -> Optional[int]:
        if not self.cookies:
            return None
        for key in ("c_secure_uid", "uid", "user_id", "userid"):
            value = self.cookies.get(key)
            if value and str(value).isdigit():
                return int(value)
        return None

    def _resolve_user_id(self) -> Optional[int]:
        if self._user_id_checked:
            return self.user_id
        self._user_id_checked = True
        if not self.enabled:
            return self.user_id
        try:
            base_url = self._get_base_url()
            html = self._request(f"{base_url}/index.php")
            if not html:
                return self.user_id
            match = re.search(r'userdetails\.php\?id=(\d+)', html)
            if match:
                self.user_id = int(match.group(1))
        except Exception:
            pass
        return self.user_id
    
    def _log(self, level: str, message: str):
        """记录日志"""
        prefix = f"[{self.config.name}] "
        getattr(self.logger, level.lower(), self.logger.info)(prefix + message)
    
    def _get_base_url(self) -> str:
        """获取站点基础URL"""
        url = self.config.url.rstrip('/')
        if not url.startswith('http'):
            url = 'https://' + url
        return url

    def _get_site_host(self) -> str:
        """获取站点域名"""
        return urlparse(self._get_base_url()).netloc.lower()

    def _is_u2_site(self) -> bool:
        """判断是否为U2站点"""
        return "u2.dmhy.org" in self._get_site_host()
    
    def _request(self, url: str, timeout: int = 15) -> Optional[str]:
        """发送HTTP请求"""
        if not self.session:
            self._log('warning', "Session未初始化")
            return None
        
        self._log('debug', f"发起请求: {url} (超时: {timeout}秒)")
        
        try:
            proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
            resp = self.session.get(
                url, 
                cookies=self.cookies, 
                proxies=proxies, 
                timeout=(10, timeout),  # (连接超时, 读取超时)
                allow_redirects=True
            )
            self._log('debug', f"请求完成: HTTP {resp.status_code}")
            if resp.status_code == 200:
                return resp.text
            else:
                self._log('warning', f"请求失败 HTTP {resp.status_code}: {url}")
        except requests.exceptions.Timeout as e:
            self._log('warning', f"请求超时 {url}: {e}")
        except requests.exceptions.ConnectionError as e:
            self._log('warning', f"连接失败 {url}: {e}")
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
        self.config.cookie = cookie
        self.cookies = self._parse_cookie(cookie) if cookie else {}
        self.user_id = self._extract_user_id()
        self._user_id_checked = False
        self.enabled = bool(self.cookies) and BS4_AVAILABLE and REQUESTS_AVAILABLE
        self._cookie_valid = False
        self._last_cookie_check = 0
        self._log('info', "Cookie已更新")

    @staticmethod
    def _parse_idle_seconds(text: str) -> Optional[int]:
        text = (text or '').strip()
        if not text:
            return None
        if text in {"刚刚", "just now", "now"}:
            return 0
        if re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', text):
            parts = list(map(int, text.split(':')))
            return reduce(lambda a, b: a * 60 + b, parts)

        total = 0
        matched = False
        patterns = [
            (r'(\d+)\s*天', 86400),
            (r'(\d+)\s*(小时|时|h|hr|hrs)', 3600),
            (r'(\d+)\s*(分钟|分|m|min|mins)', 60),
            (r'(\d+)\s*(秒|s|sec|secs)', 1),
        ]
        for pattern, multiplier in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                matched = True
                total += int(match.group(1)) * multiplier
        return total if matched else None

    @staticmethod
    def _parse_reannounce_seconds(text: str) -> Optional[int]:
        text = (text or '').strip()
        if not text:
            return None
        indicators = ['后', '剩余', 'next', 'left', 'remaining', 'reannounce']
        if not any(key in text.lower() for key in indicators):
            return None
        return PTSiteHelper._parse_idle_seconds(text)
    
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
            if not REQUESTS_AVAILABLE:
                return False, "缺少requests库，请安装: pip install requests"
            if not BS4_AVAILABLE:
                return False, "缺少BeautifulSoup库，请安装: pip install beautifulsoup4"
            if not self.config.cookie:
                return False, "未配置Cookie"
            return False, "站点辅助器未启用"
        
        try:
            base_url = self._get_base_url()
            if not self.session:
                return False, "Session未初始化"

            proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
            resp = self.session.get(
                f'{base_url}/index.php',
                cookies=self.cookies,
                proxies=proxies,
                timeout=(10, 15),
                allow_redirects=True,
            )

            if resp.status_code != 200:
                return False, f"请求失败 HTTP {resp.status_code}"

            html = resp.text or ''

            if not html:
                if self.proxy:
                    return False, f"无法连接站点（使用代理: {self.proxy[:30]}...）"
                return False, "无法连接站点，可能需要配置代理"

            url_lower = resp.url.lower()
            html_lower = html.lower()

            login_page_indicators = [
                'login.php',
                'takelogin.php',
                'name="username"',
                'name="password"',
                'action="login.php"',
                'action="takelogin.php"',
                'form id="login',
                'forgotpass.php',
                '请登录',
            ]
            for indicator in login_page_indicators:
                if indicator in html_lower or indicator in url_lower:
                    self._cookie_valid = False
                    return False, "Cookie已失效，请重新登录获取"

            # 登录状态特征（NexusPHP通用）
            login_indicators = [
                'logout.php',      # 登出链接
                'userdetails.php', # 用户详情链接
                'usercp.php',      # 控制面板链接
                'mybonus.php',     # 魔力值页面
                'invite.php',      # 邀请页面
                'messages.php',    # 消息页面
            ]
            for indicator in login_indicators:
                if indicator in html_lower:
                    self._cookie_valid = True
                    self._last_cookie_check = time.time()
                    if self.user_id is None:
                        self._resolve_user_id()
                    return True, "Cookie有效"

            # 中文登录状态特征
            chinese_indicators = ['登出', '退出登录', '个人信息', '控制面板', '我的魔力', '我的邀请', '站内信']
            for indicator in chinese_indicators:
                if indicator in html:
                    self._cookie_valid = True
                    self._last_cookie_check = time.time()
                    if self.user_id is None:
                        self._resolve_user_id()
                    return True, "Cookie有效"

            self._cookie_valid = False
            return False, "Cookie无效或已过期（未检测到登录状态）"

        except Exception as e:
            error_msg = str(e)[:50]
            return False, f"检查失败: {error_msg}"
    
    def is_cookie_valid(self) -> bool:
        """返回Cookie是否有效"""
        return self._cookie_valid
    
    # ═══════════════════════════════════════════════════════════════════════════
    # TID搜索
    # ═══════════════════════════════════════════════════════════════════════════
    
    def search_tid_by_hash(self, torrent_hash: str) -> Optional[TorrentSiteInfo]:
        """
        通过种子hash搜索TID和促销信息
        
        Args:
            torrent_hash: 种子的info_hash
            
        Returns:
            TorrentSiteInfo 或 None
        """
        if not self.enabled:
            return None
        
        # 检查缓存
        cache_key = f"{self.config.id}:{torrent_hash.lower()}"
        if cache_key in self._tid_cache:
            cached = self._tid_cache[cache_key]
            # 缓存1小时
            if time.time() - cached.search_time < 3600:
                return cached
        
        info = TorrentSiteInfo(
            torrent_hash=torrent_hash,
            site_id=self.config.id,
            site_name=self.config.name
        )
        
        # 根据站点类型选择解析方法
        if self.config.site_type == SiteType.NEXUSPHP:
            info = self._search_nexusphp(torrent_hash, info)
        elif self.config.site_type == SiteType.GAZELLE:
            info = self._search_gazelle(torrent_hash, info)
        else:
            info = self._search_nexusphp(torrent_hash, info)  # 默认尝试NexusPHP
        
        # 缓存结果
        if info.searched:
            self._cache_result(cache_key, info)
        
        return info
    
    def _search_nexusphp(self, torrent_hash: str, info: TorrentSiteInfo) -> TorrentSiteInfo:
        """NexusPHP站点搜索"""
        try:
            base_url = self._get_base_url()
            search_url = (
                f'{base_url}{self.config.search_path}?'
                f'{self.config.search_param}={torrent_hash}&'
                f'search_area={self.config.hash_search_area}'
            )
            
            html = self._request(search_url)
            if not html:
                info.error = "请求失败"
                return info
            
            with self._lock:
                soup = BeautifulSoup(html.replace('\n', ''), 'lxml')
                
                # 查找种子表格
                table = soup.select('table.torrents')
                if not table:
                    # 尝试其他选择器
                    table = soup.select('table#torrent_table, table.torrent_table')
                
                if not table or len(table[0].contents) <= 1:
                    info.error = "未找到种子"
                    info.searched = True
                    info.search_time = time.time()
                    return info
                
                # 获取第一个数据行
                rows = table[0].find_all('tr')
                if len(rows) < 2:
                    info.error = "未找到种子"
                    info.searched = True
                    info.search_time = time.time()
                    return info
                
                row = rows[1]  # 跳过表头
                tds = row.find_all('td')
                
                if len(tds) < 2:
                    info.error = "解析失败"
                    return info
                
                # 获取TID
                try:
                    # 查找包含种子详情链接的td
                    for td in tds[:3]:
                        links = td.find_all('a', href=True)
                        for a_tag in links:
                            href = a_tag.get('href', '')
                            # 匹配 details.php?id=xxx 或 ?id=xxx
                            match = re.search(r'(?:details\.php\?)?id=(\d+)', href)
                            if match:
                                info.tid = int(match.group(1))
                                break
                        if info.tid:
                            break
                except Exception as e:
                    self._log('debug', f"获取TID失败: {e}")
                
                # 获取发布时间
                try:
                    time_elem = row.find('time')
                    if time_elem:
                        time_str = time_elem.get('datetime') or time_elem.get('title')
                        if time_str:
                            dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                            info.publish_time = dt.timestamp()
                except Exception as e:
                    self._log('debug', f"获取发布时间失败: {e}")
                
                # 获取促销信息
                try:
                    promos = []
                    # 查找促销图标
                    imgs = row.find_all('img')
                    for img in imgs:
                        classes = img.get('class', [])
                        if isinstance(classes, list):
                            c_str = " ".join(classes)
                        else:
                            c_str = str(classes)
                        
                        for class_name, promo_types in self.PROMO_CLASSES.items():
                            if class_name in c_str.lower():
                                promos.extend(promo_types)
                        
                        # 检查alt和title属性
                        alt = (img.get('alt') or '').lower()
                        title = (img.get('title') or '').lower()
                        for text in [alt, title]:
                            if 'free' in text:
                                promos.append('Free')
                            if '2x' in text or '2up' in text or 'double' in text:
                                promos.append('2x')
                            if '50%' in text or 'half' in text:
                                promos.append('50%')
                    
                    # 查找促销文字
                    text_content = row.get_text().lower()
                    if 'free' in text_content and 'Free' not in promos:
                        promos.append('Free')
                    
                    if promos:
                        info.promotion = " + ".join(sorted(list(set(promos)), 
                                                          key=lambda x: len(x), reverse=True))
                    else:
                        info.promotion = "无优惠"
                except Exception as e:
                    self._log('debug', f"获取促销信息失败: {e}")
                    info.promotion = "未知"
                
                info.searched = True
                info.search_time = time.time()
                info.source = "site"
                
                if info.tid:
                    self._log('info', f"🔍 Hash {torrent_hash[:8]}... → tid={info.tid} | 优惠: {info.promotion}")
                
                return info
                
        except Exception as e:
            self._log('error', f"搜索TID失败: {e}")
            info.error = str(e)
            return info
    
    def _search_gazelle(self, torrent_hash: str, info: TorrentSiteInfo) -> TorrentSiteInfo:
        """Gazelle站点搜索（简化实现）"""
        # Gazelle站点的搜索逻辑
        # 大多数Gazelle站点不支持直接hash搜索，这里做简化处理
        info.error = "Gazelle站点暂不支持hash搜索"
        info.searched = True
        info.search_time = time.time()
        return info
    
    def _cache_result(self, key: str, info: TorrentSiteInfo):
        """缓存搜索结果"""
        if len(self._tid_cache) >= self._cache_max_size:
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
            base_url = self._get_base_url()
            url = f'{base_url}{self.config.peerlist_path}?id={tid}'
            html = self._request(url)
            if not html:
                return None
            
            with self._lock:
                soup = BeautifulSoup(html.replace('\n', ' '), 'lxml')
                candidates = self._collect_peerlist_candidates(soup)

                if not candidates:
                    return None

                if self.user_id is None:
                    self._resolve_user_id()

                selected = None
                if self.user_id:
                    for candidate in candidates:
                        if candidate['user_id'] == self.user_id:
                            selected = candidate
                            break
                if not selected:
                    selected = candidates[0]

                announce_interval = self.config.announce_interval or 1800
                idle_seconds = selected['idle_seconds']
                reannounce_in = selected.get('reannounce_in')
                if reannounce_in is None:
                    reannounce_in = max(0, announce_interval - idle_seconds)
                result = {
                    'uploaded': selected.get('uploaded'),
                    'idle_seconds': idle_seconds,
                    'last_announce': time.time() - idle_seconds,
                    'reannounce_in': reannounce_in,
                }

                return result
                
        except Exception as e:
            self._log('debug', f"获取PeerList失败: {e}")
            return None

    def _collect_peerlist_candidates(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """解析peer list候选行"""
        if self._is_u2_site():
            return self._parse_u2_peerlist_candidates(soup)
        return self._parse_nexus_peerlist_candidates(soup)

    def _parse_u2_peerlist_candidates(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """解析U2 peer list候选行（固定列位置）"""
        candidates = []
        tables = soup.find_all('table')
        for table in tables or []:
            rows = table.find_all('tr')
            for tr in rows:
                if not tr.get('bgcolor'):
                    continue
                if tr.find('th'):
                    continue

                tds = tr.find_all('td')
                if len(tds) <= 10:
                    continue

                row_user_id = None
                try:
                    for a_tag in tr.find_all('a', href=True):
                        match = re.search(r'userdetails\.php\?id=(\d+)', a_tag.get('href', ''))
                        if match:
                            row_user_id = int(match.group(1))
                            break
                except Exception:
                    row_user_id = None

                uploaded = None
                try:
                    uploaded_str = tds[1].get_text(' ').strip()
                    if uploaded_str:
                        uploaded = self._parse_size(uploaded_str)
                except Exception:
                    uploaded = None

                idle_seconds = None
                try:
                    idle_text = tds[10].get_text(' ').strip()
                    idle_seconds = self._parse_idle_seconds(idle_text)
                except Exception as e:
                    self._log('debug', f"解析空闲时间失败: {e}")

                if idle_seconds is None:
                    continue

                candidates.append({
                    'user_id': row_user_id,
                    'uploaded': uploaded,
                    'idle_seconds': idle_seconds,
                    'reannounce_in': None,
                })
        return candidates

    def _parse_nexus_peerlist_candidates(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """解析NexusPHP peer list候选行"""
        candidates = []
        tables = soup.find_all('table')

        for table in tables or []:
            rows = table.find_all('tr')
            for tr in rows:
                # 跳过表头
                if tr.find('th'):
                    continue

                tds = tr.find_all('td')
                if len(tds) < 2:
                    continue

                row_user_id = None
                try:
                    for a_tag in tr.find_all('a', href=True):
                        match = re.search(r'userdetails\.php\?id=(\d+)', a_tag.get('href', ''))
                        if match:
                            row_user_id = int(match.group(1))
                            break
                except Exception:
                    row_user_id = None

                uploaded = None
                try:
                    for td in tds[:6]:
                        text = td.get_text(' ').strip()
                        if re.match(r'[\d,.]+\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)', text):
                            uploaded = self._parse_size(text)
                            break
                except Exception:
                    uploaded = None

                idle_seconds = None
                reannounce_override = None
                try:
                    for td in tds[-6:]:
                        text = td.get_text(' ').strip()
                        if reannounce_override is None:
                            reannounce_override = self._parse_reannounce_seconds(text)
                        idle_seconds = self._parse_idle_seconds(text)
                        if idle_seconds is not None:
                            break
                except Exception as e:
                    self._log('debug', f"解析空闲时间失败: {e}")

                if idle_seconds is None:
                    continue

                candidates.append({
                    'user_id': row_user_id,
                    'uploaded': uploaded,
                    'idle_seconds': idle_seconds,
                    'reannounce_in': reannounce_override,
                })
        return candidates
    
    @staticmethod
    def _parse_size(size_str: str) -> int:
        """解析大小字符串"""
        try:
            parts = size_str.strip().split()
            if len(parts) != 2:
                # 尝试分离数字和单位
                match = re.match(r'([\d,.]+)\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB|PB|PiB)', 
                               size_str.strip(), re.I)
                if match:
                    parts = [match.group(1), match.group(2)]
                else:
                    return 0
            
            num = float(parts[0].replace(',', '.'))
            unit = parts[1].upper()
            units = {
                'B': 0, 
                'KB': 1, 'KIB': 1, 
                'MB': 2, 'MIB': 2, 
                'GB': 3, 'GIB': 3, 
                'TB': 4, 'TIB': 4, 
                'PB': 5, 'PIB': 5
            }
            exp = units.get(unit, 0)
            return int(num * (1024 ** exp))
        except:
            return 0
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 综合查询
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_torrent_info(self, torrent_hash: str, include_peer_info: bool = True) -> TorrentSiteInfo:
        """
        获取种子的完整站点信息
        
        Args:
            torrent_hash: 种子hash
            include_peer_info: 是否包含peer列表信息
            
        Returns:
            TorrentSiteInfo
        """
        # 先搜索TID
        info = self.search_tid_by_hash(torrent_hash)
        
        if not info:
            info = TorrentSiteInfo(
                torrent_hash=torrent_hash,
                site_id=self.config.id,
                site_name=self.config.name,
                error="搜索失败"
            )
            return info
        
        # 如果找到TID且需要peer信息，获取详细信息
        if info.tid and include_peer_info:
            peer_info = self.get_peer_list_info(info.tid)
            if peer_info:
                info.uploaded_on_site = peer_info.get('uploaded', 0)
                info.last_announce = peer_info.get('last_announce')
                info.reannounce_in = peer_info.get('reannounce_in')
                info.source = "site"
        
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
        """获取辅助器状态"""
        return {
            'site_id': self.config.id,
            'site_name': self.config.name,
            'site_url': self.config.url,
            'site_type': self.config.site_type.value,
            'enabled': self.enabled,
            'cookie_valid': self._cookie_valid,
            'last_cookie_check': self._last_cookie_check,
            'cache_size': len(self._tid_cache),
            'has_cookie': bool(self.config.cookie),
            'has_bs4': BS4_AVAILABLE,
            'has_requests': REQUESTS_AVAILABLE,
            'announce_interval': self.config.announce_interval,
        }
    
    def clear_cache(self):
        """清除TID缓存"""
        self._tid_cache.clear()
        self._log('info', "TID缓存已清除")


# ════════════════════════════════════════════════════════════════════════════════
# 站点辅助器管理器
# ════════════════════════════════════════════════════════════════════════════════
class PTSiteHelperManager:
    """
    管理多个站点的辅助器
    
    使用方法：
    1. 添加站点配置
    2. 通过tracker关键字匹配站点
    3. 获取汇报时间（优先站点网页，失败则用qB API）
    """
    
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("pt_helper_manager")
        self._helpers: Dict[int, PTSiteHelper] = {}  # site_id -> helper
        self._tracker_map: Dict[str, int] = {}  # tracker_keyword -> site_id
        self._lock = threading.RLock()  # 使用可重入锁，避免嵌套调用死锁
    
    def add_site(self, site_config: PTSiteConfig, proxy: str = "") -> PTSiteHelper:
        """添加站点"""
        with self._lock:
            helper = PTSiteHelper(site_config, proxy, self.logger)
            self._helpers[site_config.id] = helper
            
            # 建立tracker关键字映射
            if site_config.tracker_keyword:
                self._tracker_map[site_config.tracker_keyword.lower()] = site_config.id
            
            # 自动从URL提取关键字
            try:
                parsed = urlparse(site_config.url)
                domain = parsed.netloc.lower()
                if domain:
                    self._tracker_map[domain] = site_config.id
                    # 也添加不带www的版本
                    if domain.startswith('www.'):
                        self._tracker_map[domain[4:]] = site_config.id
            except:
                pass
            
            return helper
    
    def remove_site(self, site_id: int):
        """移除站点"""
        with self._lock:
            if site_id in self._helpers:
                self._helpers[site_id].close()
                del self._helpers[site_id]
            
            # 清理tracker映射
            self._tracker_map = {k: v for k, v in self._tracker_map.items() if v != site_id}
    
    def get_helper(self, site_id: int) -> Optional[PTSiteHelper]:
        """获取站点辅助器"""
        return self._helpers.get(site_id)
    
    def get_helper_by_tracker(self, tracker_url: str) -> Optional[PTSiteHelper]:
        """通过tracker URL获取对应的站点辅助器"""
        if not tracker_url:
            return None
        
        tracker_lower = tracker_url.lower()
        
        for keyword, site_id in self._tracker_map.items():
            if keyword in tracker_lower:
                return self._helpers.get(site_id)
        
        return None
    
    def get_reannounce_time(self, torrent_hash: str, tracker_url: str, 
                           qb_reannounce: int = None) -> Tuple[Optional[int], str]:
        """
        获取汇报时间（带fallback）
        
        Args:
            torrent_hash: 种子hash
            tracker_url: tracker地址
            qb_reannounce: qB API返回的汇报时间（作为fallback）
            
        Returns:
            (汇报剩余秒数, 数据来源)
            来源: "site" / "qb_api" / "unknown"
        """
        # 1. 尝试从站点获取
        helper = self.get_helper_by_tracker(tracker_url)
        if helper and helper.enabled:
            try:
                reannounce = helper.get_reannounce_time(torrent_hash=torrent_hash)
                if reannounce is not None:
                    return reannounce, "site"
            except Exception as e:
                self.logger.debug(f"从站点获取汇报时间失败: {e}")
        
        # 2. 使用qB API的值
        if qb_reannounce is not None and qb_reannounce > 0:
            return qb_reannounce, "qb_api"
        
        return None, "unknown"
    
    def update_from_db(self, sites: List[dict], proxy: str = ""):
        """从数据库配置更新站点"""
        with self._lock:
            # 获取现有站点ID
            existing_ids = set(self._helpers.keys())
            new_ids = set()
            
            for site in sites:
                site_id = site['id']
                new_ids.add(site_id)
                
                if site_id in existing_ids:
                    # 更新现有站点的cookie
                    helper = self._helpers[site_id]
                    if helper.config.cookie != site.get('cookie', ''):
                        helper.update_cookie(site.get('cookie', ''))
                else:
                    # 添加新站点
                    config = PTSiteConfig(
                        id=site_id,
                        name=site.get('name', ''),
                        url=site.get('url', ''),
                        cookie=site.get('cookie', ''),
                        tracker_keyword=site.get('tracker_keyword', ''),
                        enabled=site.get('enabled', True)
                    )
                    self.add_site(config, proxy)
            
            # 移除已删除的站点
            for site_id in existing_ids - new_ids:
                self.remove_site(site_id)
    
    def get_all_status(self) -> List[Dict[str, Any]]:
        """获取所有站点状态"""
        return [helper.get_status() for helper in self._helpers.values()]
    
    def close_all(self):
        """关闭所有辅助器"""
        for helper in self._helpers.values():
            helper.close()
        self._helpers.clear()
        self._tracker_map.clear()


# ════════════════════════════════════════════════════════════════════════════════
# 工厂函数
# ════════════════════════════════════════════════════════════════════════════════
def create_site_helper(site_id: int, name: str, url: str, cookie: str = "", 
                       tracker_keyword: str = "", proxy: str = "") -> PTSiteHelper:
    """创建站点辅助器实例"""
    config = PTSiteConfig(
        id=site_id,
        name=name,
        url=url,
        cookie=cookie,
        tracker_keyword=tracker_keyword
    )
    logger = logging.getLogger(f"pt_helper_{name}")
    return PTSiteHelper(config, proxy, logger)


def create_helper_manager() -> PTSiteHelperManager:
    """创建站点辅助器管理器"""
    logger = logging.getLogger("pt_helper_manager")
    return PTSiteHelperManager(logger)
