#!/usr/bin/env python3
"""
qBit Smart Web Manager v1.18
主入口文件 - Cookie和RSS修复版

v1.18修复内容:
- 修复多行Cookie格式问题（自动将换行符转换为单行格式）
- 修复URL中可能包含不可见Unicode字符的问题
- 修复Cookie验证逻辑（随便填Cookie不再能通过验证）
- 优化RSS默认最大种子年龄从10分钟改为60分钟
- 增加每次最大添加种子数从5个改为10个
- 修复PT站点辅助器的Cookie解析问题
- 添加RSS抓取失败时的详细错误日志
"""

import os
import sys
import time
import json
import logging
import secrets
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('qbit_smart.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("app")

# 导入本地模块
from database import db, Database
from qb_manager import qb_manager, QBManager
from notifier import create_notifier

# 尝试导入可选模块
try:
    from pt_site_helper import create_helper_manager, PTSiteHelperManager, SITE_PRESETS
    PT_HELPER_AVAILABLE = True
except ImportError:
    PT_HELPER_AVAILABLE = False
    logger.warning("PT站点辅助器不可用")

try:
    from precision_limit_engine import create_precision_limit_engine
    LIMIT_ENGINE_AVAILABLE = True
except ImportError:
    LIMIT_ENGINE_AVAILABLE = False
    logger.warning("精准限速引擎不可用")

try:
    from rss_engine import RSSEngine
    RSS_ENGINE_AVAILABLE = True
except ImportError:
    RSS_ENGINE_AVAILABLE = False
    logger.warning("RSS引擎不可用")

try:
    from auto_remove_engine import create_auto_remove_engine, AutoRemoveEngine
    AUTO_REMOVE_AVAILABLE = True
except ImportError:
    AUTO_REMOVE_AVAILABLE = False
    logger.warning("自动删种引擎不可用")


# ════════════════════════════════════════════════════════════════════════════════
# Flask应用配置
# ════════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Session配置 - 确保cookie正确设置
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False  # 如果使用HTTPS，设置为True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 7  # 7天


# ════════════════════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════════════════════
class C:
    # 与 README 标注版本对齐
    VERSION = "1.18.0"
    APP_NAME = "qBit Smart Web Manager"


# ════════════════════════════════════════════════════════════════════════════════
# 全局变量
# ════════════════════════════════════════════════════════════════════════════════
notifier = create_notifier(db)
site_helper_manager = None
limit_engine = None
rss_engine = None
remove_engine = None


# ════════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════════════════════
def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            if request.is_json:
                return jsonify({'error': '未登录'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def fmt_duration(seconds: float) -> str:
    """格式化时长"""
    if seconds is None or seconds < 0:
        return "未知"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds // 60)}分{int(seconds % 60)}秒"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}时{minutes}分"


def fmt_speed(b: float) -> str:
    """格式化速度"""
    if b == 0:
        return "0 B/s"
    for u in ['B/s', 'KiB/s', 'MiB/s', 'GiB/s']:
        if abs(b) < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TiB/s"


def fmt_size(b: float) -> str:
    """格式化大小"""
    if b == 0:
        return "0 B"
    for u in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if abs(b) < 1024:
            return f"{b:.2f} {u}"
        b /= 1024
    return f"{b:.2f} PiB"


def get_site_helper_manager():
    """获取或创建站点辅助器管理器"""
    global site_helper_manager
    
    if not PT_HELPER_AVAILABLE:
        return None
    
    if site_helper_manager is None:
        site_helper_manager = create_helper_manager()
    
    # 从数据库更新站点配置
    try:
        sites = db.get_pt_sites()
        proxy = db.get_config('global_proxy') or ''
        site_helper_manager.update_from_db(sites, proxy)
    except Exception as e:
        logger.error(f"更新站点配置失败: {e}")
    
    return site_helper_manager


# ════════════════════════════════════════════════════════════════════════════════
# 页面路由
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/')
@login_required
def index():
    """主页"""
    return render_template('index.html', version=C.VERSION)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页"""
    if not db.user_exists():
        return redirect(url_for('setup'))
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        
        if db.verify_user(username, password):
            session.permanent = True  # 使用永久session
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='用户名或密码错误')
    
    return render_template('login.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    """登录API"""
    if not db.user_exists():
        return jsonify({'error': '请先完成初始设置'}), 400
    
    data = request.get_json(silent=True) or {}
    password = data.get('password', '')
    username = data.get('username', 'admin')
    
    if db.verify_user(username, password):
        session.permanent = True  # 使用永久session
        session['logged_in'] = True
        session['username'] = username
        db.add_log('INFO', f'用户 {username} 登录成功')
        return jsonify({'success': True})
    else:
        db.add_log('WARNING', f'登录失败尝试')
        return jsonify({'error': '密码错误'}), 401


@app.route('/logout')
def logout():
    """登出页面"""
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/logout', methods=['POST'])
@login_required
def api_logout():
    """登出API"""
    session.clear()
    return jsonify({'success': True})


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """初始设置页"""
    if db.user_exists():
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            return render_template('setup.html', error='请填写用户名和密码')
        
        if len(password) < 4:
            return render_template('setup.html', error='密码至少4位')
        
        if db.create_user(username, password):
            session['logged_in'] = True
            session['username'] = username
            db.add_log('INFO', f'用户 {username} 已创建')
            return redirect(url_for('index'))
        else:
            return render_template('setup.html', error='创建用户失败')
    
    return render_template('setup.html')


@app.route('/api/setup', methods=['POST'])
def api_setup():
    """初始设置API"""
    if db.user_exists():
        return jsonify({'error': '已存在管理员账户'}), 400
    
    data = request.get_json(silent=True) or {}
    password = data.get('password', '').strip()
    username = data.get('username', 'admin').strip()
    
    if not password:
        return jsonify({'error': '请输入密码'}), 400
    
    if len(password) < 4:
        return jsonify({'error': '密码至少4位'}), 400
    
    if db.create_user(username, password):
        session['logged_in'] = True
        session['username'] = username
        db.add_log('INFO', f'管理员 {username} 已创建')
        return jsonify({'success': True})
    else:
        return jsonify({'error': '创建用户失败'}), 500


@app.route('/api/change_password', methods=['POST'])
@login_required
def api_change_password():
    """修改密码"""
    data = request.get_json(silent=True) or {}
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')
    
    username = session.get('username', 'admin')
    
    if not db.verify_user(username, old_password):
        return jsonify({'error': '原密码错误'}), 400
    
    if len(new_password) < 4:
        return jsonify({'error': '新密码至少4位'}), 400
    
    if db.update_password(username, new_password):
        db.add_log('INFO', f'用户 {username} 修改了密码')
        return jsonify({'success': True})
    else:
        return jsonify({'error': '修改密码失败'}), 500


# ════════════════════════════════════════════════════════════════════════════════
# 仪表盘 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/dashboard')
@login_required
def api_dashboard():
    """获取仪表盘数据"""
    global limit_engine
    
    try:
        total_up_speed = 0
        total_dl_speed = 0
        total_torrents = 0
        total_uploaded = 0
        
        instances_data = []
        db_instances = db.get_qb_instances()
        
        for inst in db_instances:
            inst_id = inst['id']
            connected = qb_manager.is_connected(inst_id)
            
            inst_info = {
                'id': inst_id,
                'name': inst['name'],
                'host': inst['host'],
                'port': inst['port'],
                'connected': connected,
                'version': None,
                'up_speed': 0,
                'dl_speed': 0,
                'torrent_count': 0,
                'free_space': 0
            }
            
            if connected:
                try:
                    status = qb_manager.get_status(inst_id)
                    if status.get('connected'):
                        inst_info['version'] = status.get('version', 'Unknown')
                        inst_info['up_speed'] = status.get('upload_speed', 0)
                        inst_info['dl_speed'] = status.get('download_speed', 0)
                        total_up_speed += inst_info['up_speed']
                        total_dl_speed += inst_info['dl_speed']
                        total_uploaded += status.get('uploaded', 0)
                    
                    # 获取种子数量
                    torrents = qb_manager.get_torrents(inst_id)
                    inst_info['torrent_count'] = len(torrents) if torrents else 0
                    total_torrents += inst_info['torrent_count']
                    
                    # 获取剩余空间
                    client = qb_manager.get_client(inst_id)
                    if client:
                        main_data = client.sync_maindata()
                        inst_info['free_space'] = main_data.get('server_state', {}).get('free_space_on_disk', 0)
                except Exception as e:
                    logger.warning(f"获取qB实例 {inst['name']} 状态失败: {e}")
            
            instances_data.append(inst_info)
        
        # 限速引擎状态 - 综合检查配置和运行状态
        smart_limit_enabled = db.get_config('smart_limit_enabled') == 'true'
        
        # 使用is_running()方法检查运行状态（更可靠）
        limit_running = False
        if limit_engine is not None:
            try:
                limit_running = limit_engine.is_running()
            except:
                limit_running = hasattr(limit_engine, '_running') and limit_engine._running
        
        # 如果配置启用但引擎未运行，尝试自动启动
        if smart_limit_enabled and not limit_running and LIMIT_ENGINE_AVAILABLE:
            try:
                logger.info(f"尝试自动启动限速引擎: limit_engine存在={limit_engine is not None}")
                if limit_engine is None:
                    logger.info("创建新的限速引擎实例...")
                    site_manager = get_site_helper_manager()
                    limit_engine = create_precision_limit_engine(
                        db, qb_manager, site_manager, notifier
                    )
                    try:
                        notifier.set_context(qb_manager=qb_manager, site_helper_manager=site_manager, limit_engine=limit_engine)
                    except Exception:
                        pass
                    logger.info(f"限速引擎实例创建完成: {limit_engine is not None}")
                if limit_engine and not limit_engine.is_running():
                    limit_engine.start()
                    limit_running = True
                    try:
                        notifier.set_context(limit_engine=limit_engine)
                    except Exception:
                        pass
                    logger.info("限速引擎自动启动成功")
            except Exception as e:
                logger.warning(f"限速引擎自动启动失败: {e}", exc_info=True)
        
        logger.debug(f"Dashboard限速状态: enabled={smart_limit_enabled}, running={limit_running}, engine={limit_engine is not None}")
        
        return jsonify({
            'total_up_speed': total_up_speed,
            'total_dl_speed': total_dl_speed,
            'total_torrents': total_torrents,
            'stats': {
                'total_uploaded': total_uploaded,
                'total_removed': remove_engine.get_status().get('total_removed', 0) if remove_engine else 0
            },
            'instances': instances_data,
            # 兼容旧前端：limit_paused 仍表示“引擎未运行/不可用”
            'limit_paused': not limit_running,
            # 新增：实际“用户暂停限速”状态（Telegram /pause）
            'limit_running': bool(limit_running),
            'limit_user_paused': getattr(limit_engine, 'paused', False) if limit_engine else False,
            'temp_target_kib': getattr(limit_engine, 'temp_target_kib', None) if limit_engine else None,
            'limit_enabled': smart_limit_enabled,
            'version': C.VERSION
        })
    except Exception as e:
        logger.error(f"仪表盘API错误: {e}")
        return jsonify({
            'error': str(e),
            'total_up_speed': 0,
            'total_dl_speed': 0,
            'total_torrents': 0,
            'stats': {'total_uploaded': 0, 'total_removed': 0},
            'instances': [],
            'limit_paused': True,
            'limit_enabled': False,
            'version': C.VERSION
        }), 500


# ════════════════════════════════════════════════════════════════════════════════
# qBittorrent API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/qb/instances', methods=['GET'])
@login_required
def api_qb_instances():
    """获取所有qB实例"""
    instances = db.get_qb_instances()
    
    for inst in instances:
        inst_id = inst['id']
        inst['connected'] = qb_manager.is_connected(inst_id)
        inst['up_speed'] = 0
        inst['dl_speed'] = 0
        inst['version'] = None
        inst['free_space'] = 0
        
        if inst['connected']:
            status = qb_manager.get_status(inst_id)
            if status.get('connected'):
                inst['version'] = status.get('version', 'Unknown')
                inst['up_speed'] = status.get('upload_speed', 0)
                inst['dl_speed'] = status.get('download_speed', 0)
            
            # 获取剩余空间
            client = qb_manager.get_client(inst_id)
            if client:
                try:
                    main_data = client.sync_maindata()
                    inst['free_space'] = main_data.get('server_state', {}).get('free_space_on_disk', 0)
                except:
                    pass
    
    return jsonify(instances)


@app.route('/api/qb/instances', methods=['POST'])
@login_required
def api_add_qb_instance():
    """添加qB实例"""
    data = request.get_json(silent=True) or {}
    
    # 解析host，支持 http://127.0.0.1:8080 格式
    host_input = data.get('host', 'localhost')
    port = data.get('port', 8080)
    
    # 如果host包含端口，解析它
    if '://' in host_input:
        from urllib.parse import urlparse
        parsed = urlparse(host_input)
        host = parsed.hostname or 'localhost'
        if parsed.port:
            port = parsed.port
    elif ':' in host_input:
        # 格式: host:port
        parts = host_input.rsplit(':', 1)
        host = parts[0]
        try:
            port = int(parts[1])
        except:
            pass
    else:
        host = host_input
    
    instance_id = db.add_qb_instance(
        name=data.get('name', 'qBittorrent'),
        host=host,
        port=port,
        username=data.get('username', ''),
        password=data.get('password', '')
    )
    
    # 尝试自动连接
    instance = db.get_qb_instance(instance_id)
    if instance:
        success, msg = qb_manager.connect(instance)
        db.add_log('INFO', f'添加qB实例: {data.get("name")} - {"已连接" if success else msg}')
        return jsonify({'id': instance_id, 'success': True, 'connected': success, 'message': msg})
    
    db.add_log('INFO', f'添加qB实例: {data.get("name")}')
    return jsonify({'id': instance_id, 'success': True, 'connected': False})


@app.route('/api/qb/instances/<int:instance_id>', methods=['PUT'])
@login_required
def api_update_qb_instance(instance_id):
    """更新qB实例"""
    data = request.get_json(silent=True) or {}
    db.update_qb_instance(instance_id, **data)
    return jsonify({'success': True})


@app.route('/api/qb/instances/<int:instance_id>', methods=['DELETE'])
@login_required
def api_delete_qb_instance(instance_id):
    """删除qB实例"""
    qb_manager.disconnect(instance_id)
    db.delete_qb_instance(instance_id)
    return jsonify({'success': True})


@app.route('/api/qb/instances/<int:instance_id>/connect', methods=['POST'])
@login_required
def api_connect_qb(instance_id):
    """连接qB实例"""
    instance = db.get_qb_instance(instance_id)
    if not instance:
        return jsonify({'error': '实例不存在'}), 404
    
    success, msg = qb_manager.connect(instance)
    
    if success:
        db.add_log('INFO', f'已连接: {instance["name"]}')
    else:
        db.add_log('WARNING', f'连接失败: {instance["name"]} - {msg}')
    
    return jsonify({'success': success, 'message': msg})


@app.route('/api/qb/instances/<int:instance_id>/disconnect', methods=['POST'])
@login_required
def api_disconnect_qb(instance_id):
    """断开qB连接"""
    qb_manager.disconnect(instance_id)
    return jsonify({'success': True})


@app.route('/api/qb/instances/<int:instance_id>/torrents', methods=['GET'])
@login_required
def api_get_torrents(instance_id):
    """获取种子列表"""
    filter_type = request.args.get('filter')
    category = request.args.get('category')
    
    torrents = qb_manager.get_torrents(instance_id, filter_type, category)
    
    return jsonify(torrents)


@app.route('/api/qb/instances/<int:instance_id>/torrents', methods=['POST'])
@login_required
def api_add_torrent(instance_id):
    """添加种子"""
    data = request.get_json(silent=True) or {}
    
    success, msg = qb_manager.add_torrent(
        instance_id,
        torrent_url=data.get('url'),
        save_path=data.get('save_path'),
        category=data.get('category'),
        paused=data.get('paused', False)
    )
    
    if success:
        db.add_log('INFO', f'添加种子: {data.get("url", "")[:50]}')
    
    return jsonify({'success': success, 'message': msg})


@app.route('/api/qb/instances/<int:instance_id>/torrents/<torrent_hash>/pause', methods=['POST'])
@login_required
def api_pause_torrent(instance_id, torrent_hash):
    """暂停种子"""
    success, msg = qb_manager.pause_torrent(instance_id, torrent_hash)
    return jsonify({'success': success, 'message': msg})


@app.route('/api/qb/instances/<int:instance_id>/torrents/<torrent_hash>/resume', methods=['POST'])
@login_required
def api_resume_torrent(instance_id, torrent_hash):
    """恢复种子"""
    success, msg = qb_manager.resume_torrent(instance_id, torrent_hash)
    return jsonify({'success': success, 'message': msg})


@app.route('/api/qb/instances/<int:instance_id>/torrents/<torrent_hash>', methods=['DELETE'])
@login_required
def api_delete_torrent(instance_id, torrent_hash):
    """删除种子"""
    delete_files = request.args.get('delete_files', 'false').lower() == 'true'
    success, msg = qb_manager.delete_torrent(instance_id, torrent_hash, delete_files)
    return jsonify({'success': success, 'message': msg})


@app.route('/api/qb/instances/<int:instance_id>/torrents/<torrent_hash>/upload_limit', methods=['POST'])
@login_required
def api_set_upload_limit(instance_id, torrent_hash):
    """设置上传限速"""
    data = request.get_json(silent=True) or {}
    limit = data.get('limit', 0)
    success, msg = qb_manager.set_torrent_upload_limit(instance_id, torrent_hash, limit)
    return jsonify({'success': success, 'message': msg})


# ════════════════════════════════════════════════════════════════════════════════
# 种子控制 API (通用)
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/control/torrent/delete', methods=['POST'])
@login_required
def api_control_delete_torrent():
    """删除种子（通用接口）"""
    data = request.get_json(silent=True) or {}
    instance_id = data.get('instance_id')
    torrent_hash = data.get('hash')
    delete_files = data.get('delete_files', False)
    
    if not instance_id or not torrent_hash:
        return jsonify({'error': '参数不完整'}), 400
    
    success, msg = qb_manager.delete_torrent(instance_id, torrent_hash, delete_files)
    
    if success:
        db.add_log('INFO', f'删除种子: {torrent_hash[:8]}...')
    
    return jsonify({'success': success, 'message': msg})


@app.route('/api/control/torrent/reannounce', methods=['POST'])
@login_required
def api_control_reannounce():
    """重新汇报（通用接口）"""
    data = request.get_json(silent=True) or {}
    instance_id = data.get('instance_id')
    torrent_hash = data.get('hash')
    
    if not instance_id or not torrent_hash:
        return jsonify({'error': '参数不完整'}), 400
    
    success, msg = qb_manager.reannounce(instance_id, torrent_hash)
    
    return jsonify({'success': success, 'message': msg})


# ════════════════════════════════════════════════════════════════════════════════
# PT站点 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/pt/sites', methods=['GET'])
@login_required
def api_pt_sites():
    """获取所有PT站点"""
    sites = db.get_pt_sites()
    return jsonify(sites)


@app.route('/api/pt/site_presets', methods=['GET'])
@login_required
def api_pt_site_presets():
    """获取PT站点预设列表"""
    presets = []
    for domain, config in SITE_PRESETS.items():
        presets.append({
            'domain': domain,
            'site_type': getattr(config.get('site_type'), 'value', str(config.get('site_type', 'unknown'))),
        })
    presets.sort(key=lambda item: item['domain'])
    return jsonify(presets)


@app.route('/api/pt/sites', methods=['POST'])
@login_required
def api_add_pt_site():
    """添加PT站点"""
    data = request.get_json(silent=True) or {}
    preferred_instance_id = data.get('preferred_instance_id')
    if preferred_instance_id == '':
        preferred_instance_id = None
    if preferred_instance_id is not None:
        preferred_instance_id = int(preferred_instance_id)
    
    site_id = db.add_pt_site(
        name=data.get('name', ''),
        url=data.get('url', ''),
        cookie=data.get('cookie', ''),
        rss_url=data.get('rss_url', ''),
        tracker_keyword=data.get('tracker_keyword', ''),
        preferred_instance_id=preferred_instance_id
    )
    
    # 更新其他字段
    updates = {}
    if 'reannounce_source' in data:
        updates['reannounce_source'] = data['reannounce_source']
    if 'enable_dl_limit' in data:
        updates['enable_dl_limit'] = 1 if data['enable_dl_limit'] else 0
    if 'enable_reannounce_opt' in data:
        updates['enable_reannounce_opt'] = 1 if data['enable_reannounce_opt'] else 0
    if 'preferred_instance_id' in data:
        preferred_id = data['preferred_instance_id']
        if preferred_id == '':
            preferred_id = None
        if preferred_id is not None:
            preferred_id = int(preferred_id)
        updates['preferred_instance_id'] = preferred_id
    if updates:
        db.update_pt_site(site_id, **updates)
    
    db.add_log('INFO', f'添加PT站点: {data.get("name")}')
    get_site_helper_manager()
    
    return jsonify({'id': site_id, 'success': True})


@app.route('/api/pt/sites/<int:site_id>', methods=['GET'])
@login_required
def api_get_pt_site(site_id):
    """获取单个PT站点"""
    site = db.get_pt_site(site_id)
    if not site:
        return jsonify({'error': '站点不存在'}), 404
    return jsonify(site)


@app.route('/api/pt/sites/<int:site_id>', methods=['PUT'])
@login_required
def api_update_pt_site(site_id):
    """更新PT站点"""
    data = request.get_json(silent=True) or {}
    if 'preferred_instance_id' in data:
        preferred_id = data['preferred_instance_id']
        if preferred_id == '':
            preferred_id = None
        if preferred_id is not None:
            preferred_id = int(preferred_id)
        data['preferred_instance_id'] = preferred_id
    db.update_pt_site(site_id, **data)
    get_site_helper_manager()
    return jsonify({'success': True})


@app.route('/api/pt/sites/<int:site_id>', methods=['DELETE'])
@login_required
def api_delete_pt_site(site_id):
    """删除PT站点"""
    db.delete_pt_site(site_id)
    return jsonify({'success': True})


@app.route('/api/pt/sites/<int:site_id>/status', methods=['GET'])
@login_required
def api_pt_site_status(site_id):
    """获取站点状态"""
    logger.debug(f"获取站点 {site_id} 状态")
    
    manager = get_site_helper_manager()
    if not manager:
        site = db.get_pt_site(site_id)
        return jsonify({
            'available': False,
            'cookie_valid': False,
            'site_name': site.get('name') if site else '未知',
            'site_url': site.get('url') if site else '',
            'message': '站点辅助器不可用（缺少requests或beautifulsoup4）'
        })
    
    helper = manager.get_helper(site_id)
    if not helper:
        site = db.get_pt_site(site_id)
        return jsonify({
            'available': True,
            'cookie_valid': bool(site and site.get('cookie')),
            'site_name': site.get('name') if site else '未知',
            'site_url': site.get('url') if site else '',
            'has_cookie': bool(site and site.get('cookie')),
            'message': '站点辅助器未初始化'
        })
    
    return jsonify(helper.get_status())


@app.route('/api/pt/sites/<int:site_id>/check-cookie', methods=['POST'])
@login_required
def api_check_cookie(site_id):
    """检测Cookie有效性"""
    logger.info(f"开始检测站点 {site_id} 的Cookie")
    
    try:
        # 先检查站点是否存在
        site = db.get_pt_site(site_id)
        if not site:
            logger.warning(f"站点 {site_id} 不存在")
            return jsonify({'valid': False, 'message': '站点不存在'})
        
        logger.info(f"站点名称: {site.get('name')}, URL: {site.get('url')}")
        
        if not site.get('cookie'):
            logger.warning(f"站点 {site.get('name')} 未配置Cookie")
            return jsonify({'valid': False, 'message': '未配置Cookie'})
        
        logger.info("正在获取站点辅助器管理器...")
        manager = get_site_helper_manager()
        if not manager:
            logger.error("站点辅助器管理器不可用")
            return jsonify({'valid': False, 'message': '站点辅助器不可用，请检查依赖(requests/beautifulsoup4)'})
        
        logger.info(f"正在获取站点 {site_id} 的辅助器...")
        helper = manager.get_helper(site_id)
        if not helper:
            logger.warning(f"站点 {site_id} 的辅助器未初始化")
            return jsonify({'valid': False, 'message': '站点辅助器未初始化，请刷新页面重试'})
        
        logger.info(f"开始检测Cookie，站点: {site.get('name')}")
        
        try:
            valid, message = helper.check_cookie_valid()
            logger.info(f"Cookie检测结果: valid={valid}, message={message}")
            db.add_log('INFO' if valid else 'WARNING', f'[{site.get("name", "Unknown")}] Cookie检查: {message}')
            return jsonify({'valid': valid, 'message': message})
        except Exception as check_error:
            logger.error(f"Cookie检测执行失败: {check_error}", exc_info=True)
            return jsonify({'valid': False, 'message': f'检测失败: {str(check_error)[:100]}'})
            
    except Exception as e:
        logger.error(f"Cookie检测API异常: {e}", exc_info=True)
        return jsonify({'valid': False, 'message': f'检测出错: {str(e)[:100]}'})


@app.route('/api/pt/sites/<int:site_id>/clear-cache', methods=['POST'])
@login_required
def api_clear_cache(site_id):
    """清除站点缓存"""
    manager = get_site_helper_manager()
    if manager:
        helper = manager.get_helper(site_id)
        if helper:
            helper.clear_cache()
    return jsonify({'success': True})


# ════════════════════════════════════════════════════════════════════════════════
# 限速规则 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/speed/rules', methods=['GET'])
@login_required
def api_speed_rules():
    """获取所有限速规则"""
    rules = db.get_speed_rules()
    return jsonify(rules)


@app.route('/api/speed/rules', methods=['POST'])
@login_required
def api_add_speed_rule():
    """添加限速规则"""
    data = request.get_json(silent=True) or {}
    
    # 前端发送的是 target_speed_kib
    target_speed_kib = data.get('target_speed_kib', 51200)
    site_id = data.get('site_id')
    if site_id is not None and site_id != '':
        site_id = int(site_id)
    
    rule_id = db.add_speed_rule(
        name=data.get('name', ''),
        target_speed_kib=target_speed_kib,
        site_id=site_id,
        safety_margin=data.get('safety_margin', 0.98)
    )
    
    db.add_log('INFO', f'添加限速规则: {data.get("name")}')
    
    return jsonify({'id': rule_id, 'success': True})


@app.route('/api/speed/rules/<int:rule_id>', methods=['PUT'])
@login_required
def api_update_speed_rule(rule_id):
    """更新限速规则"""
    data = request.get_json(silent=True) or {}
    if 'site_id' in data and data['site_id'] is not None and data['site_id'] != '':
        data['site_id'] = int(data['site_id'])
    db.update_speed_rule(rule_id, **data)
    return jsonify({'success': True})


@app.route('/api/speed/rules/<int:rule_id>', methods=['DELETE'])
@login_required
def api_delete_speed_rule(rule_id):
    """删除限速规则"""
    db.delete_speed_rule(rule_id)
    return jsonify({'success': True})


# ════════════════════════════════════════════════════════════════════════════════
# 删种规则 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/remove/rules', methods=['GET'])
@login_required
def api_remove_rules():
    """获取所有删种规则"""
    rules = db.get_remove_rules()
    return jsonify(rules)


@app.route('/api/remove/rules', methods=['POST'])
@login_required
def api_add_remove_rule():
    """添加删种规则"""
    data = request.get_json(silent=True) or {}
    
    # condition可能是dict，需要转为JSON字符串
    condition = data.get('condition', {})
    if isinstance(condition, dict):
        condition = json.dumps(condition)
    
    rule_id = db.add_remove_rule(
        name=data.get('name', ''),
        description=data.get('description', ''),
        condition=condition,
        priority=data.get('priority', 0),
        enabled=data.get('enabled', True)
    )
    db.add_log('INFO', f'添加删种规则: {data.get("name")}')
    return jsonify({'id': rule_id, 'success': True})


@app.route('/api/remove/rules/<int:rule_id>', methods=['PUT'])
@login_required
def api_update_remove_rule(rule_id):
    """更新删种规则"""
    data = request.get_json(silent=True) or {}
    
    # 处理enabled字段
    if 'enabled' in data:
        data['enabled'] = 1 if data['enabled'] else 0
    
    db.update_remove_rule(rule_id, **data)
    return jsonify({'success': True})


@app.route('/api/remove/rules/<int:rule_id>', methods=['DELETE'])
@login_required
def api_delete_remove_rule(rule_id):
    """删除删种规则"""
    db.delete_remove_rule(rule_id)
    return jsonify({'success': True})


@app.route('/api/remove/rules/reset', methods=['POST'])
@login_required
def api_reset_remove_rules():
    """重置内置删种规则"""
    db.reset_builtin_rules()
    db.add_log('INFO', '已重置内置删种规则')
    return jsonify({'success': True})


# ════════════════════════════════════════════════════════════════════════════════
# 配置 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/config', methods=['GET'])
@login_required
def api_get_config():
    """获取所有配置"""
    return jsonify(db.get_all_config())


@app.route('/api/config', methods=['POST', 'PUT'])
@login_required
def api_set_config():
    """设置配置"""
    data = request.get_json(silent=True) or {}
    
    for key, value in data.items():
        db.set_config(key, str(value))

    if 'smart_limit_enabled' in data:
        enabled = str(data.get('smart_limit_enabled')).lower() == 'true'
        if enabled:
            if not LIMIT_ENGINE_AVAILABLE:
                db.add_log('ERROR', '限速引擎不可用，无法启动')
                return jsonify({'success': False, 'error': '限速引擎不可用'}), 400
            try:
                global limit_engine
                if limit_engine is None:
                    site_manager = get_site_helper_manager()
                    limit_engine = create_precision_limit_engine(
                        db, qb_manager, site_manager, notifier
                    )
                if not limit_engine.is_running():
                    limit_engine.start()
                # 注入上下文，确保 Telegram 命令可控制最新实例
                try:
                    notifier.set_context(limit_engine=limit_engine)
                except Exception:
                    pass
                db.add_log('INFO', '限速引擎已启动')
            except Exception as e:
                db.add_log('ERROR', f'限速引擎启动失败: {e}')
                return jsonify({'success': False, 'error': '限速引擎启动失败'}), 500
        else:
            try:
                if limit_engine:
                    limit_engine.stop()
                db.add_log('INFO', '限速引擎已停止')
            except Exception as e:
                db.add_log('ERROR', f'限速引擎停止失败: {e}')
                return jsonify({'success': False, 'error': '限速引擎停止失败'}), 500
    
    return jsonify({'success': True})


@app.route('/api/test_telegram', methods=['POST'])
@login_required
def api_test_telegram():
    """测试Telegram通知"""
    try:
        if notifier:
            notifier.notify(
                title="🧪 测试消息",
                message=f"这是来自 qBit Smart Web v{C.VERSION} 的测试通知。\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return jsonify({'success': True, 'message': '测试消息已发送'})
        else:
            return jsonify({'success': False, 'message': '通知器未配置'}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/reset_all', methods=['POST'])
@login_required
def api_reset_all():
    """重置所有数据"""
    try:
        # 停止所有引擎
        if limit_engine and hasattr(limit_engine, 'stop'):
            limit_engine.stop()
        if rss_engine and hasattr(rss_engine, 'stop'):
            rss_engine.stop()
        if remove_engine and hasattr(remove_engine, 'stop'):
            remove_engine.stop()
        
        # 清空数据库表
        conn = db._get_conn()
        cursor = conn.cursor()
        
        # 保留密码设置
        password_hash = db.get_config('password_hash')
        
        # 删除所有数据
        cursor.execute("DELETE FROM pt_sites")
        cursor.execute("DELETE FROM qb_instances")
        cursor.execute("DELETE FROM speed_rules")
        cursor.execute("DELETE FROM remove_rules")
        cursor.execute("DELETE FROM logs")
        cursor.execute("DELETE FROM config WHERE key != 'password_hash'")
        
        conn.commit()
        conn.close()
        
        # 重新初始化内置规则
        db.init_builtin_remove_rules()
        
        logger.info("所有数据已重置")
        return jsonify({'success': True, 'message': '数据已重置'})
    except Exception as e:
        logger.error(f"重置数据失败: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/config/export', methods=['GET'])
@login_required
def api_config_export():
    """导出配置"""
    try:
        export_data = {
            'version': C.VERSION,
            'export_time': datetime.now().isoformat(),
            'config': {},
            'instances': [],
            'sites': [],
            'speed_rules': [],
            'remove_rules': []
        }
        
        # 导出配置
        config_keys = [
            'smart_limit_enabled', 'auto_remove_enabled', 'rss_fetch_enabled',
            'rss_fetch_interval', 'auto_remove_interval', 'auto_remove_sleep',
            'auto_remove_reannounce', 'auto_remove_delete_files',
            'global_proxy', 'tg_bot_token', 'tg_chat_id', 'tg_proxy'
        ]
        for key in config_keys:
            val = db.get_config(key)
            if val:
                export_data['config'][key] = val
        
        # 导出qB实例（不包含密码）
        instances = db.get_qb_instances()
        for inst in instances:
            export_data['instances'].append({
                'name': inst['name'],
                'host': inst['host'],
                'port': inst['port'],
                'username': inst['username'],
                'enabled': inst['enabled']
            })
        
        # 导出PT站点（不包含cookie）
        sites = db.get_pt_sites()
        for site in sites:
            export_data['sites'].append({
                'name': site.get('name', ''),
                'url': site.get('url', ''),
                'rss_url': site.get('rss_url', ''),
                'tracker_keyword': site.get('tracker_keyword', ''),
                'preferred_instance_id': site.get('preferred_instance_id'),
                'reannounce_source': site.get('reannounce_source', ''),
                'enable_dl_limit': site.get('enable_dl_limit', 0),
                'enable_reannounce_opt': site.get('enable_reannounce_opt', 0)
            })
        
        # 导出限速规则
        rules = db.get_speed_rules()
        for rule in rules:
            export_data['speed_rules'].append({
                'name': rule['name'],
                'target_speed_kib': rule['target_speed_kib'],
                'safety_margin': rule.get('safety_margin', 0.98),
                'enabled': rule.get('enabled', 1)
            })
        
        # 导出自定义删种规则（不包含内置）
        remove_rules = db.get_remove_rules()
        for rule in remove_rules:
            if not rule.get('is_builtin'):
                export_data['remove_rules'].append({
                    'name': rule['name'],
                    'conditions': rule.get('conditions', ''),
                    'priority': rule.get('priority', 0),
                    'enabled': rule.get('enabled', 1)
                })
        
        return jsonify(export_data)
    except Exception as e:
        logger.error(f"导出配置失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/import', methods=['POST'])
@login_required
def api_config_import():
    """导入配置"""
    try:
        data = request.get_json(silent=True) or {}
        imported = {'config': 0, 'speed_rules': 0}
        
        # 导入基础配置
        if 'config' in data:
            for key, val in data['config'].items():
                # 跳过敏感配置
                if key in ['password_hash']:
                    continue
                db.set_config(key, val)
                imported['config'] += 1
        
        # 导入限速规则
        if 'speed_rules' in data:
            for rule in data['speed_rules']:
                db.add_speed_rule(
                    name=rule.get('name', '导入的规则'),
                    target_speed_kib=rule.get('target_speed_kib', 51200),
                    safety_margin=rule.get('safety_margin', 0.98)
                )
                imported['speed_rules'] += 1
        
        db.add_log('INFO', f'导入配置: {imported}')
        return jsonify({'success': True, 'imported': imported})
    except Exception as e:
        logger.error(f"导入配置失败: {e}")
        return jsonify({'error': str(e)}), 500


# ════════════════════════════════════════════════════════════════════════════════
# 日志 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/logs', methods=['GET'])
@login_required
def api_get_logs():
    """获取日志"""
    limit = request.args.get('limit', 100, type=int)
    level = request.args.get('level')
    category = request.args.get('category')
    
    logs = db.get_logs(limit, level, category)
    return jsonify(logs)


@app.route('/api/logs', methods=['DELETE'])
@login_required
def api_clear_logs():
    """清理日志"""
    days = request.args.get('days', 7, type=int)
    db.clear_logs(days)
    return jsonify({'success': True})


# ════════════════════════════════════════════════════════════════════════════════
# RSS API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/rss/status', methods=['GET'])
@login_required
def api_rss_status():
    """获取RSS状态"""
    enabled = db.get_config('rss_fetch_enabled') == 'true'
    interval = int(db.get_config('rss_fetch_interval') or 300)
    
    status = {
        'available': RSS_ENGINE_AVAILABLE,
        'enabled': enabled,
        'interval': interval,
        'running': rss_engine._running if rss_engine else False,
        'last_fetch': None
    }
    
    if rss_engine and hasattr(rss_engine, 'get_status'):
        status.update(rss_engine.get_status())
    
    return jsonify(status)


@app.route('/api/rss/enable', methods=['POST'])
@login_required
def api_rss_enable():
    """启用RSS"""
    global rss_engine
    
    if not RSS_ENGINE_AVAILABLE:
        return jsonify({'error': 'RSS引擎不可用，请检查是否安装了requests和feedparser'}), 500
    
    try:
        # 先写入数据库
        db.set_config('rss_fetch_enabled', 'true')
        
        if rss_engine is None:
            rss_engine = RSSEngine(db, qb_manager)
        
        # start()会从数据库读取enabled状态，所以上面的db.set_config必须在前面
        if not rss_engine._running:
            rss_engine.start()
        else:
            # 如果已经运行，确保_enabled为True
            rss_engine._enabled = True
        
        db.add_log('INFO', 'RSS引擎已启动')
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f'RSS引擎启动失败: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/rss/disable', methods=['POST'])
@login_required
def api_rss_disable():
    """禁用RSS"""
    db.set_config('rss_fetch_enabled', 'false')
    
    if rss_engine:
        rss_engine.stop()
        db.add_log('INFO', 'RSS引擎已停止')
    
    return jsonify({'success': True})


@app.route('/api/rss/interval', methods=['PUT'])
@login_required
def api_rss_interval():
    """设置RSS间隔"""
    data = request.get_json(silent=True) or {}
    interval = data.get('interval', 300)
    
    db.set_config('rss_fetch_interval', str(interval))
    
    if rss_engine and hasattr(rss_engine, 'set_interval'):
        rss_engine.set_interval(interval)
    
    return jsonify({'success': True})


@app.route('/api/rss/max_age', methods=['PUT'])
@login_required
def api_rss_max_age():
    """设置RSS最大种子年龄（分钟）"""
    data = request.get_json(silent=True) or {}
    minutes = data.get('minutes', 10)
    
    # 限制范围1-1440分钟（1分钟到24小时）
    minutes = max(1, min(1440, minutes))
    
    db.set_config('rss_max_age_minutes', str(minutes))
    
    if rss_engine and hasattr(rss_engine, 'set_max_age'):
        rss_engine.set_max_age(minutes)
    
    return jsonify({'success': True, 'minutes': minutes})


@app.route('/api/rss/fetch', methods=['POST'])
@login_required
def api_rss_fetch():
    """立即抓取RSS"""
    if not rss_engine:
        return jsonify({'error': 'RSS引擎未启动'}), 400
    
    try:
        if hasattr(rss_engine, 'fetch_now'):
            results = rss_engine.fetch_now()
            # 转换FetchResult对象为字典
            results_list = []
            for r in results:
                results_list.append({
                    'site_id': r.site_id,
                    'site_name': r.site_name,
                    'success': r.success,
                    'items_found': r.items_found,
                    'items_added': r.items_added,
                    'items_skipped': r.items_skipped,
                    'items_too_old': r.items_too_old,
                    'items_cached': r.items_cached,
                    'error': r.error,
                    'time_str': datetime.fromtimestamp(r.timestamp).strftime('%H:%M:%S')
                })
            return jsonify({'success': True, 'results': results_list})
        return jsonify({'success': True, 'results': []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rss/clear_cache', methods=['POST'])
@login_required
def api_rss_clear_cache():
    """清除RSS缓存"""
    if rss_engine and hasattr(rss_engine, 'clear_cache'):
        rss_engine.clear_cache()
    return jsonify({'success': True})


@app.route('/api/rss/results', methods=['GET'])
@login_required
def api_rss_results():
    """获取RSS抓取结果"""
    limit = request.args.get('limit', 50, type=int)
    
    if not rss_engine:
        return jsonify([])
    
    if hasattr(rss_engine, 'get_results'):
        return jsonify(rss_engine.get_results(limit))
    
    return jsonify([])


# ════════════════════════════════════════════════════════════════════════════════
# U2 API (兼容旧版)
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/u2/config', methods=['GET'])
@login_required
def api_u2_config():
    """获取U2配置"""
    return jsonify({
        'cookie': db.get_config('u2_cookie') or '',
        'proxy': db.get_config('u2_proxy') or '',
        'enabled': db.get_config('u2_enabled') == 'true'
    })


@app.route('/api/u2/config', methods=['POST', 'PUT'])
@login_required
def api_u2_set_config():
    """设置U2配置"""
    data = request.get_json(silent=True) or {}
    
    if 'cookie' in data:
        db.set_config('u2_cookie', data['cookie'])
    if 'proxy' in data:
        db.set_config('u2_proxy', data['proxy'])
    if 'enabled' in data:
        db.set_config('u2_enabled', 'true' if data['enabled'] else 'false')
    
    return jsonify({'success': True})


@app.route('/api/u2/check_cookie', methods=['POST'])
@login_required
def api_u2_check_cookie():
    """检查U2 Cookie"""
    # 首先检查是否有站点辅助器
    manager = get_site_helper_manager()
    
    # 查找U2站点
    sites = db.get_pt_sites()
    u2_site = None
    for site in sites:
        if 'u2' in site.get('name', '').lower() or 'u2' in site.get('url', '').lower():
            u2_site = site
            break
    
    # 如果有站点辅助器且有U2站点配置
    if manager and u2_site:
        helper = manager.get_helper(u2_site['id'])
        if helper:
            valid, message = helper.check_cookie_valid()
            return jsonify({'valid': valid, 'message': message})
    
    # 如果没有站点配置，使用独立的U2 Cookie检查
    u2_cookie = db.get_config('u2_cookie') or ''
    u2_proxy = db.get_config('u2_proxy') or ''
    
    if not u2_cookie:
        return jsonify({'valid': False, 'message': '请先配置U2 Cookie，或在站点管理中添加U2站点'})
    
    # 尝试使用Cookie访问U2
    try:
        import requests
        from bs4 import BeautifulSoup
        
        session = requests.Session()
        if u2_proxy:
            session.proxies = {'http': u2_proxy, 'https': u2_proxy}
        
        session.cookies.set('nexusphp_u2', u2_cookie, domain='.u2.dmhy.org')
        
        resp = session.get('https://u2.dmhy.org/index.php', timeout=15)
        
        if 'logout.php' in resp.text or 'userdetails.php' in resp.text:
            return jsonify({'valid': True, 'message': 'Cookie有效'})
        elif 'login.php' in resp.text:
            return jsonify({'valid': False, 'message': 'Cookie已失效，请重新获取'})
        else:
            return jsonify({'valid': True, 'message': 'Cookie可能有效（无法确认）'})
            
    except ImportError as e:
        return jsonify({'valid': False, 'message': f'缺少依赖: {e}'})
    except Exception as e:
        return jsonify({'valid': False, 'message': f'检查失败: {e}'})


@app.route('/api/u2/status', methods=['GET'])
@login_required
def api_u2_status():
    """获取U2状态"""
    cookie = db.get_config('u2_cookie') or ''
    cookie_configured = bool(cookie)
    
    # 检查依赖
    has_requests = True
    has_bs4 = True
    try:
        import requests
    except:
        has_requests = False
    try:
        from bs4 import BeautifulSoup
    except:
        has_bs4 = False
    
    # 检查cookie有效性
    cookie_valid = False
    if cookie_configured and PT_HELPER_AVAILABLE:
        try:
            manager = get_site_helper_manager()
            if manager:
                sites = db.get_pt_sites()
                u2_site = None
                for site in sites:
                    if 'u2' in site.get('name', '').lower() or 'u2' in site.get('url', '').lower():
                        u2_site = site
                        break
                if u2_site:
                    helper = manager.get_helper(u2_site['id'])
                    if helper:
                        cookie_valid, _ = helper.check_cookie_valid()
        except:
            pass
    
    return jsonify({
        'available': PT_HELPER_AVAILABLE,
        'enabled': db.get_config('u2_enabled') == 'true',
        'cookie_configured': cookie_configured,
        'cookie_valid': cookie_valid,
        'cache_size': 0,  # TODO: 从helper获取
        'has_requests': has_requests,
        'has_bs4': has_bs4
    })


@app.route('/api/u2/torrent_info', methods=['POST'])
@login_required
def api_u2_torrent_info():
    """获取U2种子信息"""
    data = request.get_json(silent=True) or {}
    torrent_hash = data.get('hash')
    
    if not torrent_hash:
        return jsonify({'error': '缺少种子hash'}), 400
    
    manager = get_site_helper_manager()
    if not manager:
        return jsonify({'error': '站点辅助器不可用'}), 400
    
    sites = db.get_pt_sites()
    u2_site = None
    for site in sites:
        if 'u2' in site.get('name', '').lower() or 'u2' in site.get('url', '').lower():
            u2_site = site
            break
    
    if not u2_site:
        return jsonify({'error': '未配置U2站点'}), 400
    
    helper = manager.get_helper(u2_site['id'])
    if not helper:
        return jsonify({'error': 'U2辅助器不可用'}), 400
    
    info = helper.get_torrent_info(torrent_hash)
    
    # 格式化汇报时间
    reannounce_in_str = None
    if info and info.reannounce_in:
        seconds = info.reannounce_in
        if seconds < 60:
            reannounce_in_str = f"{seconds}秒"
        elif seconds < 3600:
            reannounce_in_str = f"{seconds // 60}分{seconds % 60}秒"
        else:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            reannounce_in_str = f"{hours}小时{mins}分"
    
    return jsonify({
        'success': True,
        'hash': info.torrent_hash if info else torrent_hash,
        'tid': info.tid if info else None,
        'promotion': info.promotion if info else None,
        'reannounce_in': info.reannounce_in if info else None,
        'reannounce_in_str': reannounce_in_str,
        'uploaded_on_site': getattr(info, 'uploaded_on_site', None) if info else None,
        'error': info.error if info and not info.tid else None
    })


# ════════════════════════════════════════════════════════════════════════════════
# 系统 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/system/info', methods=['GET'])
@login_required
def api_system_info():
    """获取系统信息"""
    return jsonify({
        'version': C.VERSION,
        'app_name': C.APP_NAME,
        'pt_helper_available': PT_HELPER_AVAILABLE,
        'limit_engine_available': LIMIT_ENGINE_AVAILABLE,
        'rss_engine_available': RSS_ENGINE_AVAILABLE,
        'limit_engine_running': limit_engine._running if limit_engine else False,
    })


# ════════════════════════════════════════════════════════════════════════════════
# 限速引擎 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/limit_engine/status', methods=['GET'])
@login_required
def api_limit_engine_status():
    """获取限速引擎状态"""
    if not limit_engine:
        return jsonify({
            'available': LIMIT_ENGINE_AVAILABLE,
            'running': False,
            'states_count': 0
        })
    
    return jsonify({
        'available': True,
        'running': limit_engine._running,
        **limit_engine.get_stats()
    })


@app.route('/api/limit_engine/history', methods=['GET'])
@login_required
def api_limit_engine_history():
    """获取限速历史记录"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify(db.get_limit_history(limit))


@app.route('/api/limit_engine/states', methods=['GET'])
@login_required
def api_limit_engine_states():
    """获取所有种子限速状态"""
    if not limit_engine:
        return jsonify([])
    
    try:
        states = limit_engine.get_all_states()
        return jsonify(states)
    except Exception as e:
        logger.error(f"获取种子状态失败: {e}")
        return jsonify([])


@app.route('/api/limit_engine/state/<hash>', methods=['GET'])
@login_required
def api_limit_engine_state(hash):
    """获取单个种子的详细限速状态"""
    if not limit_engine:
        return jsonify({'error': '限速引擎未启动'}), 400
    
    try:
        state = limit_engine.get_state(hash)
        if not state:
            return jsonify({'error': '未找到该种子的限速状态'}), 404
        return jsonify(state)
    except Exception as e:
        logger.error(f"获取种子状态失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/limit_engine/samples/<hash>', methods=['GET'])
@login_required
def api_limit_engine_samples(hash):
    """获取单个种子的速度样本（用于可视化）"""
    if not limit_engine:
        return jsonify({'error': '限速引擎未启动'}), 400

    window = request.args.get('window', 300, type=int)
    window = max(30, min(3600, window))

    try:
        samples = limit_engine.get_speed_samples(hash, window)
        if samples is None:
            return jsonify({'error': '未找到该种子的限速状态'}), 404
        return jsonify(samples)
    except Exception as e:
        logger.error(f"获取速度样本失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/limit_engine/start', methods=['POST'])
@login_required
def api_limit_engine_start():
    """启动限速引擎"""
    global limit_engine
    
    if not LIMIT_ENGINE_AVAILABLE:
        return jsonify({'success': False, 'error': '限速引擎不可用'})
    
    if limit_engine is None:
        site_manager = get_site_helper_manager()
        limit_engine = create_precision_limit_engine(
            db, qb_manager, site_manager, notifier
        )
    
    limit_engine.start()
    db.set_config('smart_limit_enabled', 'true')
    db.add_log('INFO', '限速引擎已启动')
    
    return jsonify({'success': True})


@app.route('/api/limit_engine/stop', methods=['POST'])
@login_required
def api_limit_engine_stop():
    """停止限速引擎"""
    if limit_engine:
        limit_engine.stop()
        db.set_config('smart_limit_enabled', 'false')
        db.add_log('INFO', '限速引擎已停止')
    
    return jsonify({'success': True})


# ════════════════════════════════════════════════════════════════════════════════
# 自动删种引擎 API
# ════════════════════════════════════════════════════════════════════════════════
@app.route('/api/remove_engine/status', methods=['GET'])
@login_required
def api_remove_engine_status():
    """获取自动删种引擎状态"""
    if not remove_engine:
        return jsonify({
            'available': AUTO_REMOVE_AVAILABLE,
            'running': False,
            'enabled': db.get_config('auto_remove_enabled') == 'true',
            'check_interval': int(db.get_config('auto_remove_interval') or 60),
            'sleep_between': int(db.get_config('auto_remove_sleep') or 5),
            'total_removed': 0
        })
    
    return jsonify({
        'available': True,
        **remove_engine.get_status()
    })


@app.route('/api/remove_engine/start', methods=['POST'])
@login_required
def api_remove_engine_start():
    """启动自动删种引擎"""
    global remove_engine
    
    if not AUTO_REMOVE_AVAILABLE:
        return jsonify({'success': False, 'error': '自动删种引擎不可用'}), 500
    
    try:
        # 先写入数据库，因为start()会从数据库读取enabled状态
        db.set_config('auto_remove_enabled', 'true')
        
        if remove_engine is None:
            remove_engine = create_auto_remove_engine(db, qb_manager, notifier)
        
        # start()会调用_load_config()从数据库读取enabled状态
        if not remove_engine._running:
            remove_engine.start()
        else:
            # 如果已经运行，确保_enabled为True
            remove_engine._enabled = True
        
        db.add_log('INFO', '自动删种引擎已启动')
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f'删种引擎启动失败: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/remove_engine/stop', methods=['POST'])
@login_required
def api_remove_engine_stop():
    """停止自动删种引擎"""
    if remove_engine:
        remove_engine.stop()
        db.set_config('auto_remove_enabled', 'false')
        db.add_log('INFO', '自动删种引擎已停止')
    
    return jsonify({'success': True})


@app.route('/api/remove_engine/config', methods=['POST'])
@login_required
def api_remove_engine_config():
    """设置自动删种引擎配置"""
    global remove_engine
    
    data = request.get_json(silent=True) or {}
    
    # 保存到数据库
    if 'interval' in data:
        db.set_config('auto_remove_interval', str(data['interval']))
    if 'sleep_between' in data:
        db.set_config('auto_remove_sleep', str(data['sleep_between']))
    if 'reannounce' in data:
        db.set_config('auto_remove_reannounce', 'true' if data['reannounce'] else 'false')
    if 'delete_files' in data:
        db.set_config('auto_remove_delete_files', 'true' if data['delete_files'] else 'false')
    
    # 如果引擎存在，更新配置
    if remove_engine:
        remove_engine.set_config(
            interval=data.get('interval'),
            sleep_between=data.get('sleep_between'),
            reannounce=data.get('reannounce'),
            delete_files=data.get('delete_files')
        )
    
    return jsonify({'success': True})


@app.route('/api/remove_engine/records', methods=['GET'])
@login_required
def api_remove_engine_records():
    """获取删种记录"""
    limit = request.args.get('limit', 100, type=int)
    
    if not remove_engine:
        return jsonify([])
    
    return jsonify(remove_engine.get_records(limit))


@app.route('/api/remove_engine/check', methods=['POST'])
@login_required
def api_remove_engine_check():
    """手动触发删种检查"""
    if not remove_engine:
        return jsonify({'success': False, 'error': '引擎未运行'})
    
    result = remove_engine.manual_check()
    return jsonify(result)


# ════════════════════════════════════════════════════════════════════════════════
# 初始化
# ════════════════════════════════════════════════════════════════════════════════
def init_app():
    """初始化应用"""
    global limit_engine, rss_engine, remove_engine
    
    logger.info(f"{C.APP_NAME} v{C.VERSION} 正在启动...")
    
    # 启动通知器
    notifier.start()
    
    # 初始化站点辅助器
    site_manager = get_site_helper_manager()
    if site_manager:
        logger.info('站点辅助器已初始化')

    # 注入运行上下文（供 Telegram 双向命令使用）
    try:
        notifier.set_context(qb_manager=qb_manager, site_helper_manager=site_manager)
    except Exception:
        pass
    
    # 连接已保存的qB实例
    instances = db.get_qb_instances()
    for inst in instances:
        if inst['enabled']:
            success, msg = qb_manager.connect(inst)
            if success:
                logger.info(f"已连接qB: {inst['name']}")
                db.add_log('INFO', f"已连接: {inst['name']}")
            else:
                logger.warning(f"连接qB失败: {inst['name']} - {msg}")
                db.add_log('WARNING', f"连接失败: {inst['name']} - {msg}")
    
    # 启动限速引擎
    if LIMIT_ENGINE_AVAILABLE and db.get_config('smart_limit_enabled') == 'true':
        limit_engine = create_precision_limit_engine(
            db, qb_manager, site_manager, notifier
        )
        limit_engine.start()
        try:
            notifier.set_context(limit_engine=limit_engine)
        except Exception:
            pass
        logger.info('限速引擎已启动')
        db.add_log('INFO', '限速引擎已启动')
    
    # 启动RSS引擎
    if RSS_ENGINE_AVAILABLE and db.get_config('rss_fetch_enabled') == 'true':
        try:
            rss_engine = RSSEngine(db, qb_manager)
            rss_engine.start()
            logger.info('RSS引擎已启动')
            db.add_log('INFO', 'RSS引擎已启动')
        except Exception as e:
            logger.error(f'RSS引擎启动失败: {e}')
    
    # 启动自动删种引擎
    if AUTO_REMOVE_AVAILABLE and db.get_config('auto_remove_enabled') == 'true':
        try:
            remove_engine = create_auto_remove_engine(db, qb_manager, notifier)
            remove_engine.start()
            logger.info('自动删种引擎已启动')
            db.add_log('INFO', '自动删种引擎已启动')
        except Exception as e:
            logger.error(f'自动删种引擎启动失败: {e}')
    
    db.add_log('INFO', f'{C.APP_NAME} v{C.VERSION} 已启动')
    logger.info(f"{C.APP_NAME} v{C.VERSION} 启动完成")


# ════════════════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    # 初始化应用
    init_app()
    
    # 获取配置
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    
    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║     qBit Smart Web Manager v{C.VERSION}                          ║
║                                                               ║
║     访问地址: http://{host}:{port}                              ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # 启动Flask
    app.run(host=host, port=port, debug=debug, threaded=True)
