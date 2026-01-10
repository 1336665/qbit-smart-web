#!/usr/bin/env python3
"""
数据库管理模块 v1.8
修复: 
- 添加内置删种规则初始化
- 修复限速规则字段 target_speed -> target_speed_kib
- 添加RSS配置字段
"""

import sqlite3
import threading
import json
import time
import hashlib
import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from contextlib import contextmanager


# 内置删种规则定义
BUILTIN_REMOVE_RULES = [
    {
        'name': '🚨 紧急空间不足',
        'description': '剩余空间<5GB且上传速度<5MiB/s时删除',
        'condition': json.dumps({
            'free_space_lt': 5 * 1024 * 1024 * 1024,
            'upload_speed_lt': 5 * 1024 * 1024,
        }),
        'priority': 100,
    },
    {
        'name': '⚠️ 空间紧张',
        'description': '剩余空间<10GB且上传速度<1MiB/s时删除',
        'condition': json.dumps({
            'free_space_lt': 10 * 1024 * 1024 * 1024,
            'upload_speed_lt': 1024 * 1024,
        }),
        'priority': 80,
    },
    {
        'name': '📉 空间警告',
        'description': '剩余空间<20GB、已完成且上传速度<512KiB/s时删除',
        'condition': json.dumps({
            'free_space_lt': 20 * 1024 * 1024 * 1024,
            'completed': True,
            'upload_speed_lt': 512 * 1024,
        }),
        'priority': 60,
    },
    {
        'name': '⏰ 做种时间过长',
        'description': '做种超过7天且分享率>2时删除',
        'condition': json.dumps({
            'seeding_time_gt': 7 * 24 * 3600,
            'ratio_gt': 2.0,
        }),
        'priority': 40,
    },
    {
        'name': '🐢 低速种子',
        'description': '做种超过3天且上传速度<100KiB/s时删除',
        'condition': json.dumps({
            'seeding_time_gt': 3 * 24 * 3600,
            'upload_speed_lt': 100 * 1024,
        }),
        'priority': 30,
    },
    {
        'name': '📦 超大种子',
        'description': '种子>100GB、做种超过24小时且分享率>1时删除',
        'condition': json.dumps({
            'size_gt': 100 * 1024 * 1024 * 1024,
            'seeding_time_gt': 24 * 3600,
            'ratio_gt': 1.0,
        }),
        'priority': 25,
    },
    {
        'name': '📈 高分享率',
        'description': '分享率超过5.0时删除',
        'condition': json.dumps({
            'ratio_gt': 5.0,
        }),
        'priority': 20,
    },
    {
        'name': '❄️ 冷门种子',
        'description': '无连接超过6小时且已完成时删除',
        'condition': json.dumps({
            'no_peers_time_gt': 6 * 3600,
            'completed': True,
        }),
        'priority': 15,
    },
]


class Database:
    """SQLite数据库管理"""
    
    def __init__(self, db_path: str = 'qbit_smart.db'):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()
        self._init_builtin_rules()
    
    @contextmanager
    def get_conn(self):
        """获取线程本地的数据库连接"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        try:
            yield self._local.conn
        except Exception:
            self._local.conn.rollback()
            raise
    
    def _init_db(self):
        """初始化数据库表"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            
            # 配置表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            # qBittorrent实例表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS qb_instances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT,
                    password TEXT,
                    enabled INTEGER DEFAULT 1,
                    priority INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # PT站点表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS pt_sites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    cookie TEXT,
                    rss_url TEXT,
                    tracker_keyword TEXT,
                    preferred_instance_id INTEGER,
                    reannounce_source TEXT DEFAULT 'auto',
                    enable_dl_limit INTEGER DEFAULT 1,
                    enable_reannounce_opt INTEGER DEFAULT 1,
                    enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 限速规则表 - 修复字段名
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS speed_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    site_id INTEGER,
                    target_speed_kib INTEGER NOT NULL DEFAULT 51200,
                    safety_margin REAL DEFAULT 0.98,
                    enabled INTEGER DEFAULT 1,
                    priority INTEGER DEFAULT 0,
                    FOREIGN KEY (site_id) REFERENCES pt_sites(id)
                )
            ''')
            
            # RSS规则表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS rss_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    site_id INTEGER,
                    filter_pattern TEXT,
                    exclude_pattern TEXT,
                    min_size INTEGER DEFAULT 0,
                    max_size INTEGER DEFAULT 0,
                    category TEXT,
                    save_path TEXT,
                    enabled INTEGER DEFAULT 1,
                    auto_manage INTEGER DEFAULT 1,
                    FOREIGN KEY (site_id) REFERENCES pt_sites(id)
                )
            ''')
            
            # 删种规则表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS remove_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    condition TEXT,
                    priority INTEGER DEFAULT 0,
                    enabled INTEGER DEFAULT 1,
                    builtin INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    category TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            self._ensure_columns(cursor, 'pt_sites', {
                'preferred_instance_id': 'INTEGER'
            })

            self._ensure_columns(cursor, 'logs', {
                'category': 'TEXT'
            })
            
            # 用户表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 统计表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    total_uploaded INTEGER DEFAULT 0,
                    total_downloaded INTEGER DEFAULT 0,
                    total_added INTEGER DEFAULT 0,
                    total_removed INTEGER DEFAULT 0,
                    start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 运行时配置表（用于Telegram/脚本兼容的临时或覆盖配置）
            # 说明：
            # - 与 config 表分离，避免覆盖 Web UI 的持久化配置
            # - 典型用途：Telegram /config 命令保存 override_host / override_username 等
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS runtime_config (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
            ''')
            
            # 种子限速状态表（用于重启后恢复）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS torrent_limit_states (
                    hash TEXT PRIMARY KEY,
                    name TEXT,
                    tracker TEXT,
                    instance_id INTEGER,
                    site_id INTEGER,
                    tid INTEGER,
                    cycle_index INTEGER DEFAULT 0,
                    cycle_start REAL DEFAULT 0,
                    cycle_uploaded_start INTEGER DEFAULT 0,
                    cycle_synced INTEGER DEFAULT 0,
                    cycle_interval REAL DEFAULT 0,
                    jump_count INTEGER DEFAULT 0,
                    last_jump REAL DEFAULT 0,
                    prev_time_left REAL DEFAULT 0,
                    target_speed INTEGER DEFAULT 0,
                    last_limit INTEGER DEFAULT -1,
                    last_dl_limit INTEGER DEFAULT -1,
                    reannounce_time REAL DEFAULT 0,
                    cached_time_left REAL DEFAULT 1800,
                    last_reannounce REAL DEFAULT 0,
                    waiting_reannounce INTEGER DEFAULT 0,
                    reannounced_this_cycle INTEGER DEFAULT 0,
                    dl_limited_this_cycle INTEGER DEFAULT 0,
                    session_start_time REAL DEFAULT 0,
                    total_uploaded_start INTEGER DEFAULT 0,
                    total_size INTEGER DEFAULT 0,
                    updated_at REAL
                )
            ''')

            # 兼容升级：确保 torrent_limit_states 表包含新增字段（必须在表创建之后执行）
            self._ensure_columns(cursor, 'torrent_limit_states', {
                'cycle_interval': 'REAL DEFAULT 0',
                'jump_count': 'INTEGER DEFAULT 0',
                'last_jump': 'REAL DEFAULT 0',
                'prev_time_left': 'REAL DEFAULT 0',
                'last_dl_limit': 'INTEGER DEFAULT -1',
                'last_reannounce': 'REAL DEFAULT 0',
                'waiting_reannounce': 'INTEGER DEFAULT 0',
                'reannounced_this_cycle': 'INTEGER DEFAULT 0',
                'dl_limited_this_cycle': 'INTEGER DEFAULT 0',
                'session_start_time': 'REAL DEFAULT 0',
                'total_uploaded_start': 'INTEGER DEFAULT 0',
                'total_size': 'INTEGER DEFAULT 0',
            })

            
            # 限速统计表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS limit_stats (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    total_cycles INTEGER DEFAULT 0,
                    success_cycles INTEGER DEFAULT 0,
                    precision_cycles INTEGER DEFAULT 0,
                    total_limit_uploaded INTEGER DEFAULT 0,
                    start_time REAL,
                    updated_at REAL
                )
            ''')
            
            # 初始化限速统计
            cursor.execute('INSERT OR IGNORE INTO limit_stats (id, start_time, updated_at) VALUES (1, ?, ?)', 
                          (time.time(), time.time()))

            # 限速历史记录表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS limit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    torrent_hash TEXT,
                    torrent_name TEXT,
                    instance_id INTEGER,
                    instance_name TEXT,
                    limit_value INTEGER,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 初始化统计
            cursor.execute('INSERT OR IGNORE INTO stats (id) VALUES (1)')
            
            conn.commit()

    def _ensure_columns(self, cursor: sqlite3.Cursor, table: str, columns: Dict[str, str]):
        cursor.execute(f'PRAGMA table_info({table})')
        existing = {row[1] for row in cursor.fetchall()}
        for name, column_type in columns.items():
            if name not in existing:
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN {name} {column_type}')
    
    def _init_builtin_rules(self):
        """初始化内置删种规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            
            # 检查是否已有内置规则
            cursor.execute('SELECT COUNT(*) as cnt FROM remove_rules WHERE builtin = 1')
            if cursor.fetchone()['cnt'] > 0:
                return
            
            # 添加内置规则
            for rule in BUILTIN_REMOVE_RULES:
                cursor.execute('''
                    INSERT INTO remove_rules (name, description, condition, priority, enabled, builtin)
                    VALUES (?, ?, ?, ?, 1, 1)
                ''', (rule['name'], rule['description'], rule['condition'], rule['priority']))
            
            conn.commit()
    
    # ════════════════════════════════════════════════════════════════════
    # 配置管理
    # ════════════════════════════════════════════════════════════════════
    def get_config(self, key: str, default: str = None) -> Optional[str]:
        """获取配置"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM config WHERE key = ?', (key,))
            row = cursor.fetchone()
            return row['value'] if row else default
    
    def set_config(self, key: str, value: str):
        """设置配置"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)
            ''', (key, value))
            conn.commit()
    
    def get_all_config(self) -> Dict[str, str]:
        """获取所有配置"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM config')
            return {row['key']: row['value'] for row in cursor.fetchall()}
    
    # ════════════════════════════════════════════════════════════════════
    # qBittorrent实例管理
    # ════════════════════════════════════════════════════════════════════
    def get_qb_instances(self) -> List[Dict]:
        """获取所有qB实例"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM qb_instances ORDER BY priority DESC, id')
            return [dict(row) for row in cursor.fetchall()]
    
    def get_qb_instance(self, instance_id: int) -> Optional[Dict]:
        """获取单个qB实例"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM qb_instances WHERE id = ?', (instance_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def add_qb_instance(self, name: str, host: str, port: int, 
                        username: str = '', password: str = '') -> int:
        """添加qB实例"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO qb_instances (name, host, port, username, password)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, host, port, username, password))
            conn.commit()
            return cursor.lastrowid
    
    def update_qb_instance(self, instance_id: int, **kwargs):
        """更新qB实例"""
        if not kwargs:
            return
        with self.get_conn() as conn:
            cursor = conn.cursor()
            fields = ', '.join([f'{k} = ?' for k in kwargs.keys()])
            values = list(kwargs.values()) + [instance_id]
            cursor.execute(f'UPDATE qb_instances SET {fields} WHERE id = ?', values)
            conn.commit()
    
    def delete_qb_instance(self, instance_id: int):
        """删除qB实例"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM qb_instances WHERE id = ?', (instance_id,))
            conn.commit()
    
    # ════════════════════════════════════════════════════════════════════
    # PT站点管理
    # ════════════════════════════════════════════════════════════════════
    def get_pt_sites(self, enabled_only: bool = False) -> List[Dict]:
        """获取所有PT站点"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            if enabled_only:
                cursor.execute('SELECT * FROM pt_sites WHERE enabled = 1 ORDER BY id')
            else:
                cursor.execute('SELECT * FROM pt_sites ORDER BY id')
            return [dict(row) for row in cursor.fetchall()]
    
    def get_pt_site(self, site_id: int) -> Optional[Dict]:
        """获取单个PT站点"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM pt_sites WHERE id = ?', (site_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_pt_sites_with_rss(self) -> List[Dict]:
        """获取配置了RSS的站点"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM pt_sites 
                WHERE enabled = 1 AND rss_url IS NOT NULL AND rss_url != ''
                ORDER BY id
            ''')
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def _clean_cookie(cookie: str) -> str:
        """
        清理Cookie格式：将多行Cookie合并为单行
        
        浏览器复制的Cookie可能是多行格式：
        c_secure_login=xxx;
        c_secure_pass=yyy;
        
        需要合并为单行：
        c_secure_login=xxx; c_secure_pass=yyy
        """
        if not cookie:
            return ''
        
        import re
        # 移除不可见字符（BOM、零宽字符等）
        cookie = re.sub(r'[\ufeff\ufffe\u200b\u200c\u200d\u2060\x00-\x1f\x7f-\x9f]', '', cookie)
        
        # 将换行符替换为空格
        cookie = cookie.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
        
        # 分割并重新组合cookie，同时去重
        seen_keys = set()
        parts = []
        for part in cookie.split(';'):
            part = part.strip()
            if part and '=' in part:
                key = part.split('=')[0].strip()
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    parts.append(part)
        
        return '; '.join(parts)
    
    @staticmethod
    def _clean_url(url: str) -> str:
        """清理URL：移除不可见字符和前后空白"""
        if not url:
            return ''
        
        import re
        # 移除不可见字符
        url = re.sub(r'[\ufeff\ufffe\u200b\u200c\u200d\u2060\x00-\x1f\x7f-\x9f]', '', url)
        
        # 移除前后空白（包括全角空格）
        url = url.strip().strip('\u3000')
        
        return url

    def add_pt_site(self, name: str, url: str, cookie: str = '',
                    rss_url: str = '', tracker_keyword: str = '',
                    preferred_instance_id: Optional[int] = None) -> int:
        """添加PT站点"""
        # 清理输入
        name = name.strip() if name else ''
        url = self._clean_url(url)
        cookie = self._clean_cookie(cookie)
        rss_url = self._clean_url(rss_url)
        tracker_keyword = tracker_keyword.strip() if tracker_keyword else ''
        
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO pt_sites (name, url, cookie, rss_url, tracker_keyword, preferred_instance_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, url, cookie, rss_url, tracker_keyword, preferred_instance_id))
            conn.commit()
            return cursor.lastrowid
    
    def update_pt_site(self, site_id: int, **kwargs):
        """更新PT站点"""
        # 清理特定字段
        if 'cookie' in kwargs:
            kwargs['cookie'] = self._clean_cookie(kwargs['cookie'])
        if 'url' in kwargs:
            kwargs['url'] = self._clean_url(kwargs['url'])
        if 'rss_url' in kwargs:
            kwargs['rss_url'] = self._clean_url(kwargs['rss_url'])
        if 'name' in kwargs and kwargs['name']:
            kwargs['name'] = kwargs['name'].strip()
        if 'tracker_keyword' in kwargs and kwargs['tracker_keyword']:
            kwargs['tracker_keyword'] = kwargs['tracker_keyword'].strip()
        if not kwargs:
            return
        
        with self.get_conn() as conn:
            cursor = conn.cursor()
            fields = ', '.join([f'{k} = ?' for k in kwargs.keys()])
            values = list(kwargs.values()) + [site_id]
            cursor.execute(f'UPDATE pt_sites SET {fields} WHERE id = ?', values)
            conn.commit()
    
    def delete_pt_site(self, site_id: int):
        """删除PT站点"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM pt_sites WHERE id = ?', (site_id,))
            cursor.execute('DELETE FROM speed_rules WHERE site_id = ?', (site_id,))
            cursor.execute('DELETE FROM rss_rules WHERE site_id = ?', (site_id,))
            conn.commit()
    
    # ════════════════════════════════════════════════════════════════════
    # 限速规则管理
    # ════════════════════════════════════════════════════════════════════
    def get_speed_rules(self, enabled_only: bool = False) -> List[Dict]:
        """获取所有限速规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            if enabled_only:
                cursor.execute('''
                    SELECT sr.*, ps.name as site_name
                    FROM speed_rules sr
                    LEFT JOIN pt_sites ps ON sr.site_id = ps.id
                    WHERE sr.enabled = 1
                    ORDER BY sr.priority DESC, sr.id
                ''')
            else:
                cursor.execute('''
                    SELECT sr.*, ps.name as site_name
                    FROM speed_rules sr
                    LEFT JOIN pt_sites ps ON sr.site_id = ps.id
                    ORDER BY sr.priority DESC, sr.id
                ''')
            rules = [dict(row) for row in cursor.fetchall()]
            for rule in rules:
                if rule.get('site_id') is not None:
                    rule['site_id'] = int(rule['site_id'])
            return rules
    
    def get_enabled_speed_rules(self) -> List[Dict]:
        """获取启用的限速规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT sr.*, ps.name as site_name, ps.tracker_keyword
                FROM speed_rules sr
                LEFT JOIN pt_sites ps ON sr.site_id = ps.id
                WHERE sr.enabled = 1
                ORDER BY sr.priority DESC, sr.id
            ''')
            rules = [dict(row) for row in cursor.fetchall()]
            for rule in rules:
                if rule.get('site_id') is not None:
                    rule['site_id'] = int(rule['site_id'])
            return rules
    
    def add_speed_rule(self, name: str, site_id: int = None, 
                       target_speed_kib: int = 51200, safety_margin: float = 0.98) -> int:
        """添加限速规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO speed_rules (name, site_id, target_speed_kib, safety_margin)
                VALUES (?, ?, ?, ?)
            ''', (name, site_id, target_speed_kib, safety_margin))
            conn.commit()
            return cursor.lastrowid
    
    def update_speed_rule(self, rule_id: int, **kwargs):
        """更新限速规则"""
        if not kwargs:
            return
        with self.get_conn() as conn:
            cursor = conn.cursor()
            fields = ', '.join([f'{k} = ?' for k in kwargs.keys()])
            values = list(kwargs.values()) + [rule_id]
            cursor.execute(f'UPDATE speed_rules SET {fields} WHERE id = ?', values)
            conn.commit()
    
    def delete_speed_rule(self, rule_id: int):
        """删除限速规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM speed_rules WHERE id = ?', (rule_id,))
            conn.commit()
    
    # ════════════════════════════════════════════════════════════════════
    # RSS规则管理
    # ════════════════════════════════════════════════════════════════════
    def get_rss_rules(self) -> List[Dict]:
        """获取所有RSS规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT rr.*, ps.name as site_name
                FROM rss_rules rr
                LEFT JOIN pt_sites ps ON rr.site_id = ps.id
                ORDER BY rr.id
            ''')
            return [dict(row) for row in cursor.fetchall()]
    
    def add_rss_rule(self, name: str, site_id: int = None,
                     filter_pattern: str = '', exclude_pattern: str = '',
                     category: str = '', save_path: str = '') -> int:
        """添加RSS规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO rss_rules (name, site_id, filter_pattern, exclude_pattern, category, save_path)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, site_id, filter_pattern, exclude_pattern, category, save_path))
            conn.commit()
            return cursor.lastrowid
    
    def update_rss_rule(self, rule_id: int, **kwargs):
        """更新RSS规则"""
        if not kwargs:
            return
        with self.get_conn() as conn:
            cursor = conn.cursor()
            fields = ', '.join([f'{k} = ?' for k in kwargs.keys()])
            values = list(kwargs.values()) + [rule_id]
            cursor.execute(f'UPDATE rss_rules SET {fields} WHERE id = ?', values)
            conn.commit()
    
    def delete_rss_rule(self, rule_id: int):
        """删除RSS规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM rss_rules WHERE id = ?', (rule_id,))
            conn.commit()
    
    # ════════════════════════════════════════════════════════════════════
    # 删种规则管理
    # ════════════════════════════════════════════════════════════════════
    def get_remove_rules(self) -> List[Dict]:
        """获取所有删种规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM remove_rules ORDER BY priority DESC, id')
            return [dict(row) for row in cursor.fetchall()]
    
    def get_enabled_remove_rules(self) -> List[Dict]:
        """获取所有启用的删种规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM remove_rules WHERE enabled = 1 ORDER BY priority DESC, id')
            return [dict(row) for row in cursor.fetchall()]
    
    def add_remove_rule(self, name: str, description: str = '',
                        condition: str = '{}', priority: int = 0,
                        enabled: bool = True, builtin: bool = False) -> int:
        """添加删种规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO remove_rules (name, description, condition, priority, enabled, builtin)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, description, condition, priority, int(enabled), int(builtin)))
            conn.commit()
            return cursor.lastrowid
    
    def update_remove_rule(self, rule_id: int, **kwargs):
        """更新删种规则"""
        if not kwargs:
            return
        with self.get_conn() as conn:
            cursor = conn.cursor()
            if 'enabled' in kwargs:
                kwargs['enabled'] = int(kwargs['enabled'])
            fields = ', '.join([f'{k} = ?' for k in kwargs.keys()])
            values = list(kwargs.values()) + [rule_id]
            cursor.execute(f'UPDATE remove_rules SET {fields} WHERE id = ?', values)
            conn.commit()
    
    def delete_remove_rule(self, rule_id: int):
        """删除删种规则（内置规则不可删除）"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM remove_rules WHERE id = ? AND builtin = 0', (rule_id,))
            conn.commit()
    
    def reset_builtin_rules(self):
        """重置内置删种规则"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM remove_rules WHERE builtin = 1')
            conn.commit()
        self._init_builtin_rules()
    
    # ════════════════════════════════════════════════════════════════════
    # 日志管理
    # ════════════════════════════════════════════════════════════════════
    def add_log(self, level: str, message: str, category: str = None):
        """添加日志"""
        if not category:
            if '[RSS]' in message:
                category = 'rss'
            elif '[删种]' in message or '删除种子' in message or '自动删种' in message:
                category = 'remove'
            elif '[LimitEngine]' in message or '限速' in message:
                category = 'limit'
            else:
                category = 'general'
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO logs (level, message, category) VALUES (?, ?, ?)
            ''', (level, message, category))
            conn.commit()

    def get_logs(self, limit: int = 100, level: str = None, category: str = None) -> List[Dict]:
        """获取日志"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            if category:
                if category == 'general':
                    cursor.execute('''
                        SELECT * FROM logs 
                        WHERE (category = ? OR category IS NULL)
                        ORDER BY created_at DESC LIMIT ?
                    ''', (category, limit))
                else:
                    cursor.execute('''
                        SELECT * FROM logs 
                        WHERE category = ?
                        ORDER BY created_at DESC LIMIT ?
                    ''', (category, limit))
            elif level:
                cursor.execute('''
                    SELECT * FROM logs WHERE level = ?
                    ORDER BY created_at DESC LIMIT ?
                ''', (level, limit))
            else:
                cursor.execute('''
                    SELECT * FROM logs ORDER BY created_at DESC LIMIT ?
                ''', (limit,))
            results = []
            for row in cursor.fetchall():
                item = dict(row)
                category_value = item.get('category') or 'general'
                if category == 'general' and item.get('category') is None:
                    if '[RSS]' in item['message'] or '[删种]' in item['message'] or '[LimitEngine]' in item['message']:
                        continue
                    if '删除种子' in item['message'] or '自动删种' in item['message']:
                        continue
                item['category'] = category_value
                item['time'] = self._format_log_time(item.get('created_at'))
                results.append(item)
            return results

    @staticmethod
    def _format_log_time(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S')
        if isinstance(value, str):
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
                try:
                    dt = datetime.strptime(value, fmt)
                    dt = dt.replace(tzinfo=timezone.utc).astimezone()
                    return dt.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
            try:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                return value
        return str(value)
    
    def clear_logs(self, days: int = 7):
        """清理旧日志"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM logs 
                WHERE created_at < datetime('now', '-' || ? || ' days')
            ''', (days,))
            conn.commit()

    def add_limit_history(self, torrent_hash: str, torrent_name: str, instance_id: int,
                          instance_name: str, limit_value: int, reason: str):
        """添加限速历史"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO limit_history
                (torrent_hash, torrent_name, instance_id, instance_name, limit_value, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (torrent_hash, torrent_name, instance_id, instance_name, limit_value, reason))
            conn.commit()

    def get_limit_history(self, limit: int = 50) -> List[Dict]:
        """获取限速历史"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM limit_history ORDER BY created_at DESC LIMIT ?
            ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    # ════════════════════════════════════════════════════════════════════
    # 统计管理
    # ════════════════════════════════════════════════════════════════════
    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM stats WHERE id = 1')
            row = cursor.fetchone()
            return dict(row) if row else {}
    
    def update_stats(self, **kwargs):
        """更新统计信息（增量更新）"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            for key, value in kwargs.items():
                cursor.execute(f'''
                    UPDATE stats SET {key} = {key} + ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                ''', (value,))
            conn.commit()
    
    # ════════════════════════════════════════════════════════════════════
    # 用户管理
    # ════════════════════════════════════════════════════════════════════
    def hash_password(self, password: str) -> str:
        """哈希密码"""
        return hashlib.sha256(password.encode()).hexdigest()
    
    def create_user(self, username: str, password: str) -> bool:
        """创建用户"""
        try:
            with self.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO users (username, password_hash) VALUES (?, ?)
                ''', (username, self.hash_password(password)))
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False
    
    def verify_user(self, username: str, password: str) -> bool:
        """验证用户"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT password_hash FROM users WHERE username = ?
            ''', (username,))
            row = cursor.fetchone()
            if row:
                return row['password_hash'] == self.hash_password(password)
            return False
    
    def user_exists(self) -> bool:
        """检查是否有用户存在"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as cnt FROM users')
            return cursor.fetchone()['cnt'] > 0
    
    def update_password(self, username: str, new_password: str) -> bool:
        """更新密码"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET password_hash = ? WHERE username = ?
            ''', (self.hash_password(new_password), username))
            conn.commit()
            return cursor.rowcount > 0
    
    # ════════════════════════════════════════════════════════════════════
    # 种子限速状态持久化
    # ════════════════════════════════════════════════════════════════════
    def save_torrent_limit_state(self, state: Dict):
        """保存种子限速状态"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO torrent_limit_states 
                (hash, name, tracker, instance_id, site_id, tid, cycle_index, cycle_start,
                 cycle_uploaded_start, cycle_synced, cycle_interval, jump_count, last_jump,
                 prev_time_left, target_speed, last_limit, last_dl_limit, reannounce_time,
                 cached_time_left, last_reannounce, waiting_reannounce, reannounced_this_cycle,
                 dl_limited_this_cycle, session_start_time, total_uploaded_start, total_size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                state.get('hash'),
                state.get('name', ''),
                state.get('tracker', ''),
                state.get('instance_id', 0),
                state.get('site_id'),
                state.get('tid'),
                state.get('cycle_index', 0),
                state.get('cycle_start', 0),
                state.get('cycle_uploaded_start', 0),
                1 if state.get('cycle_synced') else 0,
                state.get('cycle_interval', 0),
                state.get('jump_count', 0),
                state.get('last_jump', 0),
                state.get('prev_time_left', 0),
                state.get('target_speed', 0),
                state.get('last_limit', -1),
                state.get('last_dl_limit', -1),
                state.get('reannounce_time', 0),
                state.get('cached_time_left', 1800),
                state.get('last_reannounce', 0),
                1 if state.get('waiting_reannounce') else 0,
                1 if state.get('reannounced_this_cycle') else 0,
                1 if state.get('dl_limited_this_cycle') else 0,
                state.get('session_start_time', 0),
                state.get('total_uploaded_start', 0),
                state.get('total_size', 0),
                time.time()
            ))
            conn.commit()
    
    def load_torrent_limit_state(self, torrent_hash: str) -> Optional[Dict]:
        """加载种子限速状态"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM torrent_limit_states WHERE hash = ?', (torrent_hash,))
            row = cursor.fetchone()
            if not row:
                return None
            return {
                'hash': row['hash'],
                'name': row['name'],
                'tracker': row['tracker'],
                'instance_id': row['instance_id'],
                'site_id': row['site_id'],
                'tid': row['tid'],
                'cycle_index': row['cycle_index'],
                'cycle_start': row['cycle_start'],
                'cycle_uploaded_start': row['cycle_uploaded_start'],
                'cycle_synced': bool(row['cycle_synced']),
                'cycle_interval': row['cycle_interval'],
                'jump_count': row['jump_count'],
                'last_jump': row['last_jump'],
                'prev_time_left': row['prev_time_left'],
                'target_speed': row['target_speed'],
                'last_limit': row['last_limit'],
                'last_dl_limit': row['last_dl_limit'],
                'reannounce_time': row['reannounce_time'],
                'cached_time_left': row['cached_time_left'],
                'last_reannounce': row['last_reannounce'],
                'waiting_reannounce': bool(row['waiting_reannounce']),
                'reannounced_this_cycle': bool(row['reannounced_this_cycle']),
                'dl_limited_this_cycle': bool(row['dl_limited_this_cycle']),
                'session_start_time': row['session_start_time'],
                'total_uploaded_start': row['total_uploaded_start'],
                'total_size': row['total_size'],
                'updated_at': row['updated_at']
            }
    
    def delete_torrent_limit_state(self, torrent_hash: str):
        """删除种子限速状态"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM torrent_limit_states WHERE hash = ?', (torrent_hash,))
            conn.commit()
    
    def get_all_torrent_limit_states(self) -> List[Dict]:
        """获取所有种子限速状态"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM torrent_limit_states')
            return [dict(row) for row in cursor.fetchall()]
    
    def cleanup_old_limit_states(self, max_age_hours: int = 24):
        """清理超过指定时间未更新的状态"""
        cutoff = time.time() - max_age_hours * 3600
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM torrent_limit_states WHERE updated_at < ?', (cutoff,))
            conn.commit()
    
    # ════════════════════════════════════════════════════════════════════
    # 限速统计
    # ════════════════════════════════════════════════════════════════════
    def get_limit_stats(self) -> Dict:
        """获取限速统计"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM limit_stats WHERE id = 1')
            row = cursor.fetchone()
            if not row:
                return {
                    'total_cycles': 0,
                    'success_cycles': 0,
                    'precision_cycles': 0,
                    'total_limit_uploaded': 0,
                    'start_time': time.time()
                }
            return dict(row)
    
    def update_limit_stats(self, cycles: int = 0, success: int = 0, 
                           precision: int = 0, uploaded: int = 0):
        """更新限速统计（增量）"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE limit_stats SET 
                    total_cycles = total_cycles + ?,
                    success_cycles = success_cycles + ?,
                    precision_cycles = precision_cycles + ?,
                    total_limit_uploaded = total_limit_uploaded + ?,
                    updated_at = ?
                WHERE id = 1
            ''', (cycles, success, precision, uploaded, time.time()))
            conn.commit()

    # ════════════════════════════════════════════════════════════════════
    # 运行时配置（兼容 main.py / Telegram 指令）
    # ════════════════════════════════════════════════════════════════════
    def save_runtime_config(self, key: str, value: Any):
        """保存运行时配置（覆盖项）"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT OR REPLACE INTO runtime_config (key, value, updated_at) VALUES (?, ?, ?)',
                (key, str(value), time.time())
            )
            conn.commit()

    def get_runtime_config(self, key: str) -> Optional[str]:
        """获取运行时配置"""
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM runtime_config WHERE key = ?', (key,))
            row = cursor.fetchone()
            return row['value'] if row else None


# 全局数据库实例
db = Database()
