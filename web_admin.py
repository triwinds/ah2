import bottle
import json
import logging
import threading
import time
import base64
from io import BytesIO
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class WebAdmin:
    def __init__(self, scheduler, helper_getter, port=8888):
        """
        Initialize WebAdmin
        
        Args:
            scheduler: APScheduler instance
            helper_getter: Callable that returns the current helper instance
            port: Port to run web server on
        """
        self.scheduler = scheduler
        self.helper_getter = helper_getter
        self.port = port
        self.app = bottle.Bottle()
        self.server_thread = None
        self.is_running = False
        
        # Setup routes
        self._setup_routes()
    
    def _setup_routes(self):
        """Setup all web routes"""
        
        @self.app.route('/')
        def index():
            return self._get_dashboard_html()
        
        @self.app.route('/api/status')
        def api_status():
            bottle.response.content_type = 'application/json'
            return json.dumps(self._get_status())
        
        @self.app.route('/api/trigger', method='POST')
        def api_trigger():
            bottle.response.content_type = 'application/json'
            try:
                # Trigger do_works job immediately
                job = self.scheduler.get_job('do_works')
                if job:
                    job.modify(next_run_time=datetime.now())
                    return json.dumps({'success': True, 'message': 'Task triggered successfully'})
                else:
                    return json.dumps({'success': False, 'message': 'Job not found'})
            except Exception as e:
                logger.error(f'Error triggering task: {e}')
                return json.dumps({'success': False, 'message': str(e)})
        
        @self.app.route('/api/emulator/start', method='POST')
        def api_emulator_start():
            bottle.response.content_type = 'application/json'
            try:
                import os
                # Only start emulator, don't launch game
                if os.name == 'nt':
                    from Arknights.addons.contrib.emulator_manager import start_bluestacks
                    threading.Thread(target=start_bluestacks, daemon=True).start()
                else:
                    from Arknights.addons.contrib.emulator_manager import start_redroid
                    threading.Thread(target=start_redroid, daemon=True).start()
                return json.dumps({'success': True, 'message': 'Emulator starting...'})
            except Exception as e:
                logger.error(f'Error starting emulator: {e}')
                return json.dumps({'success': False, 'message': str(e)})
        
        @self.app.route('/api/emulator/stop', method='POST')
        def api_emulator_stop():
            bottle.response.content_type = 'application/json'
            try:
                from Arknights.addons.contrib.emulator_manager import close_emulator
                close_emulator()
                return json.dumps({'success': True, 'message': 'Emulator stopped'})
            except Exception as e:
                logger.error(f'Error stopping emulator: {e}')
                return json.dumps({'success': False, 'message': str(e)})
        
        @self.app.route('/api/screenshot')
        def api_screenshot():
            try:
                helper = self.helper_getter()
                if helper is None or helper._controller is None:
                    bottle.response.content_type = 'application/json'
                    return json.dumps({'success': False, 'message': 'No device connected'})
                
                # Get screenshot from controller
                screenshot = helper._controller.screenshot()
                
                # Convert PIL image to base64
                buffered = BytesIO()
                screenshot.save(buffered, format="PNG")
                img_str = base64.b64encode(buffered.getvalue()).decode()
                
                bottle.response.content_type = 'application/json'
                return json.dumps({
                    'success': True,
                    'image': f'data:image/png;base64,{img_str}',
                    'size': screenshot.size
                })
            except Exception as e:
                logger.error(f'Error getting screenshot: {e}')
                bottle.response.content_type = 'application/json'
                return json.dumps({'success': False, 'message': str(e)})
    
    def _get_status(self):
        """Get current status of scheduler and emulator"""
        status = {
            'scheduler_running': self.scheduler.running,
            'jobs': []
        }
        
        # Get job information
        for job in self.scheduler.get_jobs():
            status['jobs'].append({
                'id': job.id,
                'name': job.name or job.id,
                'next_run': job.next_run_time.isoformat() if job.next_run_time else None,
                'trigger': str(job.trigger)
            })
        
        # Check emulator status
        try:
            from Arknights.addons.contrib.emulator_manager import check_emulator_is_alive
            status['emulator_alive'] = check_emulator_is_alive()
        except Exception as e:
            logger.error(f'Error checking emulator status: {e}')
            status['emulator_alive'] = False
        
        # Check helper/device status
        try:
            helper = self.helper_getter()
            status['device_connected'] = helper is not None and helper._controller is not None
        except Exception as e:
            logger.error(f'Error checking device status: {e}')
            status['device_connected'] = False
        
        return status
    
    def _get_dashboard_html(self):
        """Return the dashboard HTML"""
        return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Linux Schedule Manager</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        h1 {
            color: white;
            text-align: center;
            margin-bottom: 30px;
            font-size: 2.5em;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        
        .card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 15px;
            padding: 25px;
            margin-bottom: 20px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
        }
        
        .card h2 {
            color: #667eea;
            margin-bottom: 15px;
            font-size: 1.5em;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }
        
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        
        .status-item {
            padding: 15px;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            border-radius: 10px;
            border-left: 4px solid #667eea;
        }
        
        .status-label {
            font-size: 0.9em;
            color: #666;
            margin-bottom: 5px;
        }
        
        .status-value {
            font-size: 1.2em;
            font-weight: bold;
            color: #333;
        }
        
        .status-online {
            color: #10b981;
        }
        
        .status-offline {
            color: #ef4444;
        }
        
        .btn-group {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        
        button {
            flex: 1;
            min-width: 150px;
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 1em;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        }
        
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 12px rgba(0, 0, 0, 0.15);
        }
        
        button:active {
            transform: translateY(0);
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }
        
        .btn-success {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
        }
        
        .btn-danger {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
        }
        
        .screenshot-container {
            text-align: center;
            margin-top: 20px;
        }
        
        .screenshot-container img {
            max-width: 100%;
            height: auto;
            border-radius: 10px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        }
        
        .loading {
            text-align: center;
            padding: 20px;
            color: #666;
        }
        
        .jobs-list {
            list-style: none;
        }
        
        .job-item {
            padding: 10px;
            margin: 5px 0;
            background: #f8f9fa;
            border-radius: 5px;
            border-left: 3px solid #667eea;
        }
        
        .job-name {
            font-weight: bold;
            color: #333;
        }
        
        .job-next-run {
            font-size: 0.9em;
            color: #666;
            margin-top: 5px;
        }
        
        .message {
            padding: 12px;
            margin: 10px 0;
            border-radius: 8px;
            display: none;
        }
        
        .message.success {
            background: #d1fae5;
            color: #065f46;
            border-left: 4px solid #10b981;
            display: block;
        }
        
        .message.error {
            background: #fee2e2;
            color: #991b1b;
            border-left: 4px solid #ef4444;
            display: block;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎮 Linux Schedule Manager</h1>
        
        <div class="card">
            <h2>系统状态</h2>
            <div class="status-grid">
                <div class="status-item">
                    <div class="status-label">调度器状态</div>
                    <div class="status-value" id="scheduler-status">加载中...</div>
                </div>
                <div class="status-item">
                    <div class="status-label">模拟器状态</div>
                    <div class="status-value" id="emulator-status">加载中...</div>
                </div>
                <div class="status-item">
                    <div class="status-label">设备连接</div>
                    <div class="status-value" id="device-status">加载中...</div>
                </div>
            </div>
            
            <div id="jobs-container">
                <h3>计划任务</h3>
                <ul class="jobs-list" id="jobs-list">
                    <li class="loading">加载中...</li>
                </ul>
            </div>
        </div>
        
        <div class="card">
            <h2>控制面板</h2>
            <div id="message-container"></div>
            <div class="btn-group">
                <button class="btn-primary" onclick="triggerTask()">🚀 立即执行任务</button>
                <button class="btn-success" onclick="startEmulator()">▶️ 启动模拟器</button>
                <button class="btn-danger" onclick="stopEmulator()">⏹️ 关闭模拟器</button>
            </div>
        </div>
        
        <div class="card">
            <h2>屏幕截图</h2>
            <div class="screenshot-container">
                <img id="screenshot" src="" alt="点击下方按钮刷新截图" style="display:none;">
                <div id="screenshot-loading" class="loading">点击下方按钮刷新截图</div>
            </div>
            <div class="btn-group" style="margin-top: 15px;">
                <button class="btn-primary" onclick="refreshScreenshot()">🔄 刷新截图</button>
            </div>
        </div>
    </div>
    
    <script>
        function showMessage(text, type = 'success') {
            const container = document.getElementById('message-container');
            const message = document.createElement('div');
            message.className = `message ${type}`;
            message.textContent = text;
            container.appendChild(message);
            setTimeout(() => message.remove(), 5000);
        }
        
        async function updateStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();
                
                // Update scheduler status
                document.getElementById('scheduler-status').innerHTML = 
                    data.scheduler_running 
                    ? '<span class="status-online">运行中 ✓</span>' 
                    : '<span class="status-offline">已停止 ✗</span>';
                
                // Update emulator status
                document.getElementById('emulator-status').innerHTML = 
                    data.emulator_alive 
                    ? '<span class="status-online">在线 ✓</span>' 
                    : '<span class="status-offline">离线 ✗</span>';
                
                // Update device status
                document.getElementById('device-status').innerHTML = 
                    data.device_connected 
                    ? '<span class="status-online">已连接 ✓</span>' 
                    : '<span class="status-offline">未连接 ✗</span>';
                
                // Update jobs list
                const jobsList = document.getElementById('jobs-list');
                if (data.jobs && data.jobs.length > 0) {
                    jobsList.innerHTML = data.jobs.map(job => `
                        <li class="job-item">
                            <div class="job-name">${job.name}</div>
                            <div class="job-next-run">
                                下次运行: ${job.next_run ? new Date(job.next_run).toLocaleString('zh-CN') : '未设置'}
                            </div>
                        </li>
                    `).join('');
                } else {
                    jobsList.innerHTML = '<li class="loading">暂无计划任务</li>';
                }
            } catch (error) {
                console.error('Error updating status:', error);
            }
        }
        
        async function triggerTask() {
            try {
                const response = await fetch('/api/trigger', { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    showMessage('✓ 任务已触发', 'success');
                    updateStatus();
                } else {
                    showMessage('✗ 触发失败: ' + data.message, 'error');
                }
            } catch (error) {
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }
        
        async function startEmulator() {
            try {
                const response = await fetch('/api/emulator/start', { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    showMessage('✓ 模拟器启动中...', 'success');
                    setTimeout(updateStatus, 3000);
                } else {
                    showMessage('✗ 启动失败: ' + data.message, 'error');
                }
            } catch (error) {
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }
        
        async function stopEmulator() {
            try {
                const response = await fetch('/api/emulator/stop', { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    showMessage('✓ 模拟器已关闭', 'success');
                    updateStatus();
                } else {
                    showMessage('✗ 关闭失败: ' + data.message, 'error');
                }
            } catch (error) {
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }
        
        async function refreshScreenshot() {
            const img = document.getElementById('screenshot');
            const loading = document.getElementById('screenshot-loading');
            
            loading.style.display = 'block';
            loading.textContent = '加载中...';
            img.style.display = 'none';
            
            try {
                const response = await fetch('/api/screenshot');
                const data = await response.json();
                
                if (data.success) {
                    img.src = data.image;
                    img.style.display = 'block';
                    loading.style.display = 'none';
                    showMessage('✓ 截图已刷新', 'success');
                } else {
                    loading.textContent = '获取截图失败: ' + data.message;
                    showMessage('✗ ' + data.message, 'error');
                }
            } catch (error) {
                loading.textContent = '请求失败: ' + error.message;
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }
        
        // Auto-refresh status every 5 seconds
        setInterval(updateStatus, 5000);
        
        // Initial load
        updateStatus();
    </script>
</body>
</html>
        """
    
    def start(self):
        """Start the web server in a daemon thread"""
        if self.is_running:
            logger.warning('Web server already running')
            return
        
        def run_server():
            try:
                logger.info(f'Starting web admin server on port {self.port}')
                bottle.run(self.app, host='0.0.0.0', port=self.port, quiet=True)
            except Exception as e:
                logger.error(f'Web server error: {e}')
        
        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()
        self.is_running = True
        logger.info(f'Web admin accessible at http://localhost:{self.port}')
