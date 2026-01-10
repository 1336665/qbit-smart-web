#!/usr/bin/env python3
"""
qBittorrent API 管理模块 v1.8
修复: 
- 添加 set_upload_limit 方法支持批量设置
- 改进缓存机制
"""

import time
import threading
import logging
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass

try:
    import qbittorrentapi
    QB_API_AVAILABLE = True
except ImportError:
    QB_API_AVAILABLE = False


@dataclass
class QBInstance:
    """qBittorrent实例"""
    id: int
    name: str
    host: str
    port: int
    username: str
    password: str
    enabled: bool = True
    client: Any = None
    connected: bool = False
    last_error: str = ''


class QBManager:
    """qBittorrent实例管理器"""
    
    def __init__(self):
        self._instances: Dict[int, QBInstance] = {}
        self._lock = threading.Lock()
        self._free_space_cache: Dict[int, Tuple[int, float]] = {}
        self._cache_ttl = 30
        self.logger = logging.getLogger("qb_manager")
    
    def connect(self, config: Dict) -> Tuple[bool, str]:
        """连接qB实例"""
        if not QB_API_AVAILABLE:
            return False, "qbittorrent-api库未安装"
        
        instance_id = config['id']
        
        try:
            client = qbittorrentapi.Client(
                host=config['host'],
                port=config['port'],
                username=config.get('username', ''),
                password=config.get('password', ''),
                VERIFY_WEBUI_CERTIFICATE=False
            )
            
            client.auth_log_in()
            version = client.app_version()
            
            instance = QBInstance(
                id=instance_id,
                name=config['name'],
                host=config['host'],
                port=config['port'],
                username=config.get('username', ''),
                password=config.get('password', ''),
                enabled=config.get('enabled', True),
                client=client,
                connected=True
            )
            
            with self._lock:
                self._instances[instance_id] = instance
            
            return True, f"已连接 (v{version})"
            
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"连接失败 {config['name']}: {error_msg}")
            return False, error_msg
    
    def disconnect(self, instance_id: int):
        """断开连接"""
        with self._lock:
            if instance_id in self._instances:
                instance = self._instances[instance_id]
                try:
                    if instance.client:
                        instance.client.auth_log_out()
                except:
                    pass
                del self._instances[instance_id]
    
    def get_instance(self, instance_id: int) -> Optional[QBInstance]:
        """获取实例"""
        with self._lock:
            return self._instances.get(instance_id)
    
    def get_client(self, instance_id: int) -> Optional[Any]:
        """获取qB客户端"""
        instance = self.get_instance(instance_id)
        return instance.client if instance and instance.connected else None
    
    def get_all_instances(self) -> List[QBInstance]:
        """获取所有实例"""
        with self._lock:
            return list(self._instances.values())
    
    def get_connected_instances(self) -> List[QBInstance]:
        """获取已连接的实例"""
        with self._lock:
            return [i for i in self._instances.values() if i.connected]
    
    def is_connected(self, instance_id: int) -> bool:
        """检查是否已连接"""
        instance = self.get_instance(instance_id)
        return instance.connected if instance else False
    
    def get_status(self, instance_id: int) -> Dict[str, Any]:
        """获取实例状态"""
        instance = self.get_instance(instance_id)
        if not instance:
            return {'connected': False, 'error': '实例不存在'}
        
        if not instance.connected:
            return {'connected': False, 'error': instance.last_error}
        
        try:
            client = instance.client
            info = client.transfer_info()
            return {
                'connected': True,
                'version': client.app_version(),
                'download_speed': info.get('dl_info_speed', 0),
                'upload_speed': info.get('up_info_speed', 0),
                'downloaded': info.get('dl_info_data', 0),
                'uploaded': info.get('up_info_data', 0),
            }
        except Exception as e:
            instance.connected = False
            instance.last_error = str(e)
            return {'connected': False, 'error': str(e)}
    
    # ════════════════════════════════════════════════════════════════════
    # 磁盘空间相关
    # ════════════════════════════════════════════════════════════════════
    def get_free_space(self, instance_id: int) -> int:
        """获取实例的剩余磁盘空间（字节）"""
        if instance_id in self._free_space_cache:
            cached_space, cached_time = self._free_space_cache[instance_id]
            if time.time() - cached_time < self._cache_ttl:
                return cached_space
        
        client = self.get_client(instance_id)
        if not client:
            return 0
        
        try:
            main_data = client.sync_maindata()
            free_space = main_data.get('server_state', {}).get('free_space_on_disk', 0)
            self._free_space_cache[instance_id] = (free_space, time.time())
            return free_space
        except Exception as e:
            self.logger.error(f"获取剩余空间失败: {e}")
            return 0
    
    def get_all_free_space(self) -> Dict[int, int]:
        """获取所有实例的剩余空间"""
        result = {}
        for instance in self.get_connected_instances():
            result[instance.id] = self.get_free_space(instance.id)
        return result
    
    # ════════════════════════════════════════════════════════════════════
    # 种子操作
    # ════════════════════════════════════════════════════════════════════
    def get_torrents(self, instance_id: int, filter: str = None, 
                     category: str = None) -> List[Dict]:
        """获取种子列表"""
        client = self.get_client(instance_id)
        if not client:
            return []
        
        try:
            params = {}
            if filter:
                params['filter'] = filter
            if category:
                params['category'] = category
            
            torrents = client.torrents_info(**params)
            return [dict(t) for t in torrents]
        except Exception as e:
            self.logger.error(f"获取种子列表失败: {e}")
            return []
    
    def get_torrent(self, instance_id: int, torrent_hash: str) -> Optional[Dict]:
        """获取单个种子信息"""
        client = self.get_client(instance_id)
        if not client:
            return None
        
        try:
            torrents = client.torrents_info(torrent_hashes=torrent_hash)
            if torrents:
                return dict(torrents[0])
            return None
        except Exception as e:
            self.logger.error(f"获取种子信息失败: {e}")
            return None
    
    def add_torrent(self, instance_id: int, torrent_url: str = None,
                    torrent_file: bytes = None, torrent_data: bytes = None,
                    save_path: str = None, category: str = None, 
                    paused: bool = False, **kwargs) -> Tuple[bool, str]:
        """添加种子"""
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        file_data = torrent_file or torrent_data
        
        try:
            params = {
                'is_paused': paused,
            }
            if save_path:
                params['save_path'] = save_path
            if category:
                params['category'] = category
            params.update(kwargs)
            
            if torrent_url:
                result = client.torrents_add(urls=torrent_url, **params)
            elif file_data:
                result = client.torrents_add(torrent_files=file_data, **params)
            else:
                return False, "需要提供URL或种子文件"
            
            if result == "Ok.":
                return True, "添加成功"
            else:
                return False, str(result)
                
        except Exception as e:
            return False, str(e)
    
    def delete_torrent(self, instance_id: int, torrent_hash: str, 
                       delete_files: bool = False) -> Tuple[bool, str]:
        """
        删除种子
        
        Args:
            instance_id: qB实例ID
            torrent_hash: 种子hash
            delete_files: 是否同时删除文件（默认False，自动删种时会传True）
        """
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        try:
            # qbittorrent-api 的 torrents_delete 方法
            # delete_files 参数控制是否删除下载的文件
            client.torrents_delete(
                torrent_hashes=torrent_hash, 
                delete_files=delete_files
            )
            action = "删除种子和文件" if delete_files else "仅删除种子"
            self.logger.info(f"[{instance_id}] {action}: {torrent_hash[:8]}...")
            return True, action + "成功"
        except Exception as e:
            self.logger.error(f"删除种子失败: {e}")
            return False, str(e)
    
    def pause_torrent(self, instance_id: int, torrent_hash: str) -> Tuple[bool, str]:
        """暂停种子"""
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        try:
            client.torrents_pause(torrent_hashes=torrent_hash)
            return True, "已暂停"
        except Exception as e:
            return False, str(e)
    
    def resume_torrent(self, instance_id: int, torrent_hash: str) -> Tuple[bool, str]:
        """恢复种子"""
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        try:
            client.torrents_resume(torrent_hashes=torrent_hash)
            return True, "已恢复"
        except Exception as e:
            return False, str(e)
    
    def set_torrent_upload_limit(self, instance_id: int, torrent_hash: str, 
                                  limit: int) -> Tuple[bool, str]:
        """设置单个种子上传限速"""
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        try:
            client.torrents_set_upload_limit(
                torrent_hashes=torrent_hash, 
                limit=limit
            )
            return True, f"限速设置为 {limit} B/s"
        except Exception as e:
            return False, str(e)
    
    def set_upload_limit(self, instance_id: int, torrent_hashes: Union[str, List[str]], 
                         limit: int) -> Tuple[bool, str]:
        """
        设置种子上传限速（支持批量）
        
        Args:
            instance_id: qB实例ID
            torrent_hashes: 种子hash或hash列表
            limit: 限速值（字节/秒），-1表示无限制
        """
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        try:
            # 统一处理为字符串
            if isinstance(torrent_hashes, list):
                hashes = '|'.join(torrent_hashes)
            else:
                hashes = torrent_hashes
            
            # -1 表示无限制，qB API需要设为0
            actual_limit = 0 if limit == -1 else limit
            
            client.torrents_set_upload_limit(
                torrent_hashes=hashes, 
                limit=actual_limit
            )
            return True, f"限速设置为 {limit} B/s"
        except Exception as e:
            self.logger.error(f"设置限速失败: {e}")
            return False, str(e)
    

    def set_download_limit(self, instance_id: int, torrent_hashes: Union[str, List[str]],
                           limit: int) -> Tuple[bool, str]:
        """设置种子下载限速（支持批量）

        Args:
            instance_id: qB实例ID
            torrent_hashes: 种子hash或hash列表
            limit: 限速值（字节/秒），-1表示无限制
        """
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"

        try:
            # 统一处理为字符串
            if isinstance(torrent_hashes, list):
                hashes = '|'.join(torrent_hashes)
            else:
                hashes = torrent_hashes

            # -1 表示无限制，qB API需要设为0
            actual_limit = 0 if limit == -1 else limit

            client.torrents_set_download_limit(
                torrent_hashes=hashes,
                limit=actual_limit
            )
            return True, f"下载限速设置为 {limit} B/s"
        except Exception as e:
            self.logger.error(f"设置下载限速失败: {e}")
            return False, str(e)

    def set_torrent_download_limit(self, instance_id: int, torrent_hash: str, 
                                    limit: int) -> Tuple[bool, str]:
        """设置种子下载限速"""
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        try:
            actual_limit = 0 if limit == -1 else limit
            client.torrents_set_download_limit(
                torrent_hashes=torrent_hash, 
                limit=actual_limit
            )
            return True, f"限速设置为 {limit} B/s"
        except Exception as e:
            return False, str(e)

    def get_torrent_info_by_hash(self, torrent_hash: str) -> Optional[Dict]:
        """通过hash获取种子信息"""
        # 旧版本误用 self._clients（不存在），会导致状态面板/限速引擎无法通过hash反查种子。
        # 这里统一从已连接的实例列表中遍历查询。
        for inst in self.get_connected_instances():
            client = inst.client
            if not client:
                continue
            try:
                torrents = client.torrents_info(hashes=torrent_hash)
                if torrents:
                    return dict(torrents[0])
            except Exception:
                continue
        return None
    
    def get_torrent_trackers(self, instance_id: int, torrent_hash: str) -> List[Dict]:
        """获取种子的tracker列表"""
        client = self.get_client(instance_id)
        if not client:
            return []
        
        try:
            trackers = client.torrents_trackers(torrent_hash=torrent_hash)
            return [dict(t) for t in trackers]
        except Exception as e:
            self.logger.error(f"获取tracker失败: {e}")
            return []
    
    def get_torrent_properties(self, instance_id: int, torrent_hash: str) -> Optional[Dict]:
        """获取种子属性（包含reannounce时间）"""
        client = self.get_client(instance_id)
        if not client:
            return None
        
        try:
            props = client.torrents_properties(torrent_hash=torrent_hash)
            return dict(props) if props else None
        except Exception as e:
            self.logger.error(f"获取种子属性失败: {e}")
            return None
    
    def reannounce(self, instance_id: int, torrent_hash: str) -> Tuple[bool, str]:
        """重新汇报"""
        client = self.get_client(instance_id)
        if not client:
            return False, "未连接"
        
        try:
            client.torrents_reannounce(torrent_hashes=torrent_hash)
            return True, "已重新汇报"
        except Exception as e:
            return False, str(e)
    
    # ════════════════════════════════════════════════════════════════════
    # 全局设置
    # ════════════════════════════════════════════════════════════════════
    def get_global_download_limit(self, instance_id: int) -> int:
        """获取全局下载限速"""
        client = self.get_client(instance_id)
        if not client:
            return 0
        try:
            return client.transfer_download_limit()
        except:
            return 0
    
    def set_global_download_limit(self, instance_id: int, limit: int) -> bool:
        """设置全局下载限速"""
        client = self.get_client(instance_id)
        if not client:
            return False
        try:
            client.transfer_set_download_limit(limit=limit)
            return True
        except:
            return False
    
    def get_global_upload_limit(self, instance_id: int) -> int:
        """获取全局上传限速"""
        client = self.get_client(instance_id)
        if not client:
            return 0
        try:
            return client.transfer_upload_limit()
        except:
            return 0
    
    def set_global_upload_limit(self, instance_id: int, limit: int) -> bool:
        """设置全局上传限速"""
        client = self.get_client(instance_id)
        if not client:
            return False
        try:
            client.transfer_set_upload_limit(limit=limit)
            return True
        except:
            return False
    
    def get_categories(self, instance_id: int) -> Dict[str, Dict]:
        """获取所有分类"""
        client = self.get_client(instance_id)
        if not client:
            return {}
        try:
            return client.torrents_categories()
        except:
            return {}
    
    def create_category(self, instance_id: int, name: str, 
                        save_path: str = '') -> bool:
        """创建分类"""
        client = self.get_client(instance_id)
        if not client:
            return False
        try:
            client.torrents_create_category(name=name, save_path=save_path)
            return True
        except:
            return False


# 全局管理器实例
qb_manager = QBManager()
