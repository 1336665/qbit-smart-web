#!/usr/bin/env python3
"""
站点辅助器集成代码 - 添加到 app.py

将以下代码添加到 app.py 的适当位置即可启用通用站点辅助功能
"""

# ════════════════════════════════════════════════════════════════════════════════
# 第1步：在文件开头的导入部分添加（约第60行附近）
# ════════════════════════════════════════════════════════════════════════════════

# 尝试导入通用PT站点辅助器
try:
    from pt_site_helper import PTSiteHelperManager, create_helper_manager, PTSiteConfig
    PT_HELPER_AVAILABLE = True
except ImportError:
    PT_HELPER_AVAILABLE = False
    print("Warning: PT Site Helper not available")


# ════════════════════════════════════════════════════════════════════════════════
# 第2步：在全局变量部分添加（约第1700行附近，U2辅助器之前）
# ════════════════════════════════════════════════════════════════════════════════

# 通用PT站点辅助器管理器
site_helper_manager = None

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
        print(f"更新站点配置失败: {e}")
    
    return site_helper_manager


# ════════════════════════════════════════════════════════════════════════════════
# 第3步：添加新的API路由（在U2 API路由之后）
# ════════════════════════════════════════════════════════════════════════════════

# 通用站点辅助器 API 路由
@app.route('/api/site_helper/status', methods=['GET'])
@login_required
def api_site_helper_status():
    """获取所有站点辅助器状态"""
    manager = get_site_helper_manager()
    if not manager:
        return jsonify({
            'available': PT_HELPER_AVAILABLE,
            'sites': []
        })
    return jsonify({
        'available': True,
        'sites': manager.get_all_status()
    })


@app.route('/api/site_helper/check_cookie/<int:site_id>', methods=['POST'])
@login_required
def api_site_helper_check_cookie(site_id):
    """检查指定站点的Cookie有效性"""
    manager = get_site_helper_manager()
    if not manager:
        return jsonify({'valid': False, 'message': '站点辅助器不可用'})
    
    helper = manager.get_helper(site_id)
    if not helper:
        return jsonify({'valid': False, 'message': '站点不存在'})
    
    valid, message = helper.check_cookie_valid()
    db.add_log('INFO' if valid else 'WARNING', f'[{helper.config.name}] Cookie检查: {message}')
    return jsonify({'valid': valid, 'message': message, 'site_name': helper.config.name})


@app.route('/api/site_helper/search_tid', methods=['POST'])
@login_required
def api_site_helper_search_tid():
    """通过hash和tracker搜索TID"""
    manager = get_site_helper_manager()
    if not manager:
        return jsonify({'error': '站点辅助器不可用'}), 400
    
    data = request.get_json()
    torrent_hash = data.get('hash')
    tracker_url = data.get('tracker')
    site_id = data.get('site_id')
    
    if not torrent_hash:
        return jsonify({'error': '缺少种子hash'}), 400
    
    # 优先使用site_id，否则通过tracker匹配
    helper = None
    if site_id:
        helper = manager.get_helper(site_id)
    elif tracker_url:
        helper = manager.get_helper_by_tracker(tracker_url)
    
    if not helper:
        return jsonify({'error': '未找到匹配的站点'}), 400
    
    info = helper.search_tid_by_hash(torrent_hash)
    
    if info:
        return jsonify({
            'success': True,
            'hash': info.torrent_hash,
            'site_id': info.site_id,
            'site_name': info.site_name,
            'tid': info.tid,
            'promotion': info.promotion,
            'publish_time': info.publish_time,
            'searched': info.searched,
            'error': info.error
        })
    else:
        return jsonify({'success': False, 'error': '搜索失败'})


@app.route('/api/site_helper/reannounce_time', methods=['POST'])
@login_required
def api_site_helper_reannounce_time():
    """获取汇报时间（三级fallback）"""
    manager = get_site_helper_manager()
    
    data = request.get_json()
    torrent_hash = data.get('hash')
    tracker_url = data.get('tracker')
    qb_reannounce = data.get('qb_reannounce')  # qB API返回的汇报时间
    
    if not torrent_hash:
        return jsonify({'error': '缺少种子hash'}), 400
    
    if manager:
        reannounce, source = manager.get_reannounce_time(
            torrent_hash, tracker_url, qb_reannounce
        )
    else:
        # 没有站点辅助器，直接使用qB API的值
        reannounce = qb_reannounce
        source = "qb_api" if qb_reannounce else "unknown"
    
    if reannounce is not None:
        return jsonify({
            'success': True,
            'reannounce_in': reannounce,
            'reannounce_in_str': fmt_duration(reannounce),
            'source': source
        })
    else:
        return jsonify({'success': False, 'error': '无法获取汇报时间'})


@app.route('/api/site_helper/torrent_info', methods=['POST'])
@login_required
def api_site_helper_torrent_info():
    """获取种子完整站点信息"""
    manager = get_site_helper_manager()
    if not manager:
        return jsonify({'error': '站点辅助器不可用'}), 400
    
    data = request.get_json()
    torrent_hash = data.get('hash')
    tracker_url = data.get('tracker')
    site_id = data.get('site_id')
    include_peer_info = data.get('include_peer_info', True)
    
    if not torrent_hash:
        return jsonify({'error': '缺少种子hash'}), 400
    
    # 获取helper
    helper = None
    if site_id:
        helper = manager.get_helper(site_id)
    elif tracker_url:
        helper = manager.get_helper_by_tracker(tracker_url)
    
    if not helper:
        return jsonify({'error': '未找到匹配的站点'}), 400
    
    info = helper.get_torrent_info(torrent_hash, include_peer_info)
    
    return jsonify({
        'success': True,
        'hash': info.torrent_hash,
        'site_id': info.site_id,
        'site_name': info.site_name,
        'tid': info.tid,
        'promotion': info.promotion,
        'publish_time': info.publish_time,
        'uploaded_on_site': info.uploaded_on_site,
        'last_announce': info.last_announce,
        'reannounce_in': info.reannounce_in,
        'reannounce_in_str': fmt_duration(info.reannounce_in) if info.reannounce_in else '未知',
        'source': info.source,
        'searched': info.searched,
        'error': info.error
    })


@app.route('/api/site_helper/clear_cache', methods=['POST'])
@login_required
def api_site_helper_clear_cache():
    """清除所有站点缓存"""
    manager = get_site_helper_manager()
    if manager:
        for helper in manager._helpers.values():
            helper.clear_cache()
    return jsonify({'success': True})


# ════════════════════════════════════════════════════════════════════════════════
# 第4步：修改精准限速引擎初始化（在 init_app 函数中）
# ════════════════════════════════════════════════════════════════════════════════

# 原来的代码:
# limit_engine = create_precision_limit_engine(db, qb_manager, u2_helper, notifier)

# 修改为:
# from precision_limit_engine import create_precision_limit_engine
# limit_engine = create_precision_limit_engine(
#     db, 
#     qb_manager, 
#     site_helper_manager=get_site_helper_manager(),  # 使用通用站点辅助器
#     notifier=notifier
# )


# ════════════════════════════════════════════════════════════════════════════════
# 完整的 init_app 函数示例
# ════════════════════════════════════════════════════════════════════════════════

def init_app():
    """初始化应用"""
    global limit_engine
    
    # 启动通知器
    notifier.start()
    
    # 初始化站点辅助器
    site_manager = get_site_helper_manager()
    if site_manager:
        db.add_log('INFO', f'站点辅助器已初始化，支持 {len(site_manager._helpers)} 个站点')
    
    # 启动自动删种
    if db.get_config('auto_remove_enabled') == 'true':
        auto_remover.start()
    
    # 启动智能限速（使用新的站点辅助器）
    if db.get_config('smart_limit_enabled') == 'true':
        # 重新创建限速引擎，传入站点辅助器管理器
        from precision_limit_engine import create_precision_limit_engine
        limit_engine = create_precision_limit_engine(
            db, 
            qb_manager, 
            site_helper_manager=site_manager,
            notifier=notifier
        )
        limit_engine.start()
    
    # 启动RSS引擎
    if rss_engine and db.get_config('rss_fetch_enabled') == 'true':
        rss_engine.start()
        db.add_log('INFO', 'RSS引擎已启动')
    
    # 连接已保存的qB实例
    instances = db.get_qb_instances()
    for inst in instances:
        if inst['enabled']:
            success, msg = qb_manager.connect(inst)
            if success:
                db.add_log('INFO', f"已连接: {inst['name']}")
            else:
                db.add_log('WARNING', f"连接失败: {inst['name']} - {msg}")
    
    # 发送启动通知
    notifier.notify_startup()
    
    db.add_log('INFO', f'qBit Smart Web v{C.VERSION} 已启动')
