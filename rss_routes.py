#!/usr/bin/env python3
"""
RSS API路由模块

需要在app.py中导入并注册这些路由
"""

from flask import Blueprint, request, jsonify

# 创建蓝图
rss_bp = Blueprint('rss', __name__, url_prefix='/api/rss')


def init_rss_routes(app, rss_engine, login_required):
    """
    初始化RSS路由
    
    Args:
        app: Flask应用实例
        rss_engine: RSS引擎实例
        login_required: 登录验证装饰器
    """
    
    @app.route('/api/rss/status', methods=['GET'])
    @login_required
    def api_rss_status():
        """获取RSS状态"""
        return jsonify(rss_engine.get_status())
    
    @app.route('/api/rss/enable', methods=['POST'])
    @login_required
    def api_rss_enable():
        """启用RSS"""
        rss_engine.enable()
        return jsonify({'success': True, 'enabled': True})
    
    @app.route('/api/rss/disable', methods=['POST'])
    @login_required
    def api_rss_disable():
        """禁用RSS"""
        rss_engine.disable()
        return jsonify({'success': True, 'enabled': False})
    
    @app.route('/api/rss/fetch', methods=['POST'])
    @login_required
    def api_rss_fetch():
        """立即抓取"""
        data = request.get_json(silent=True) or {}
        site_id = data.get('site_id')
        
        results = rss_engine.fetch_now(site_id)
        
        return jsonify({
            'success': True,
            'results': [
                {
                    'site_id': r.site_id,
                    'site_name': r.site_name,
                    'success': r.success,
                    'items_found': r.items_found,
                    'items_added': r.items_added,
                    'items_skipped': r.items_skipped,
                    'error': r.error
                }
                for r in results
            ]
        })
    
    @app.route('/api/rss/interval', methods=['PUT'])
    @login_required
    def api_rss_interval():
        """设置抓取间隔"""
        data = request.get_json(silent=True) or {}
        interval = data.get('interval', 300)
        
        try:
            interval = int(interval)
            rss_engine.set_interval(interval)
            return jsonify({'success': True, 'interval': interval})
        except ValueError:
            return jsonify({'error': '无效的间隔值'}), 400
    
    @app.route('/api/rss/clear_cache', methods=['POST'])
    @login_required
    def api_rss_clear_cache():
        """清除缓存"""
        rss_engine.clear_cache()
        return jsonify({'success': True})
    
    @app.route('/api/rss/results', methods=['GET'])
    @login_required
    def api_rss_results():
        """获取抓取结果历史"""
        limit = int(request.args.get('limit', 50))
        return jsonify(rss_engine.get_results(limit))
