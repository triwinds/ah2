from gevent import monkey; monkey.patch_all()
import bottle
from bottle.ext.websocket import GeventWebSocketServer
from bottle.ext.websocket import websocket
import json
import logging
import threading
import time
import base64
import collections
from io import BytesIO
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=1000):
        super().__init__()
        self.capacity = capacity
        self.buffer = collections.deque(maxlen=capacity)
        self.sockets = set()
        self.formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
            
            # Broadcast to connected websockets
            dead_sockets = set()
            for ws in self.sockets:
                try:
                    ws.send(msg)
                except Exception:
                    dead_sockets.add(ws)
            
            for ws in dead_sockets:
                self.sockets.remove(ws)
        except Exception:
            self.handleError(record)


class WebAdmin:
    def __init__(self, scheduler, helper_getter, config_getter=None, port=8888):
        """
        Initialize WebAdmin

        Args:
            scheduler: APScheduler instance
            helper_getter: Callable that returns the current helper instance
            config_getter: Callable that returns config management functions (get, update)
            port: Port to run web server on
        """
        self.scheduler = scheduler
        self.helper_getter = helper_getter
        self.config_getter = config_getter
        self.port = port
        self.app = bottle.Bottle()
        self.server_thread = None
        self.is_running = False

        # Setup logging handler
        self.log_handler = MemoryLogHandler()
        logging.getLogger().addHandler(self.log_handler)

        # Setup routes
        self._setup_routes()

    def _setup_routes(self):
        """Setup all web routes"""

        @self.app.route('/api/logs/ws', apply=[websocket])
        def api_logs_ws(ws):
            # Send existing logs
            for msg in self.log_handler.buffer:
                try:
                    ws.send(msg)
                except Exception:
                    return

            # Register socket
            self.log_handler.sockets.add(ws)
            
            try:
                while True:
                    msg = ws.receive()
                    if msg is None:
                        break
            finally:
                self.log_handler.sockets.discard(ws)

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

                # Check if device is already connected
                device_connected = False
                if helper is not None and hasattr(helper, '_controller') and helper._controller is not None:
                    device_connected = True

                if not device_connected:
                    try:
                        from Arknights.configure_launcher import reconnect_helper, get_helper
                        logger.info('Device not connected, attempting to reconnect...')
                        reconnect_helper()
                        helper = get_helper()

                        # Verify reconnection succeeded
                        try:
                            _ = helper.control
                            logger.info('Successfully reconnected to device')
                        except Exception:
                            bottle.response.content_type = 'application/json'
                            return json.dumps({'success': False, 'message': 'No device connected'})
                    except Exception as reconnect_error:
                        logger.error(f'Failed to reconnect: {reconnect_error}')
                        bottle.response.content_type = 'application/json'
                        return json.dumps({'success': False,
                                           'message': f'No device connected. Reconnect failed: {str(reconnect_error)}'})

                # Get screenshot from controller
                screenshot = helper.control.screenshot()

                # Convert PIL image to base64
                buffered = BytesIO()
                screenshot.save(buffered, format="WEBP", quality=85)
                img_str = base64.b64encode(buffered.getvalue()).decode()

                bottle.response.content_type = 'application/json'
                return json.dumps({
                    'success': True,
                    'image': f'data:image/webp;base64,{img_str}',
                    'size': screenshot.size
                })
            except Exception as e:
                logger.error(f'Error getting screenshot: {e}')
                bottle.response.content_type = 'application/json'
                return json.dumps({'success': False, 'message': str(e)})

        @self.app.route('/api/click', method='POST')
        def api_click():
            bottle.response.content_type = 'application/json'
            try:
                # Parse request body
                data = bottle.request.json
                if not data:
                    return json.dumps({'success': False, 'message': 'Invalid request data'})

                x = data.get('x')
                y = data.get('y')

                if x is None or y is None:
                    return json.dumps({'success': False, 'message': 'Missing coordinates'})

                # Get helper instance
                helper = self.helper_getter()

                # Check device connection
                device_connected = False
                if helper is not None and hasattr(helper, '_controller') and helper._controller is not None:
                    device_connected = True

                if not device_connected:
                    try:
                        from Arknights.configure_launcher import reconnect_helper, get_helper
                        logger.info('Device not connected, attempting to reconnect...')
                        reconnect_helper()
                        helper = get_helper()

                        try:
                            _ = helper.control
                            logger.info('Successfully reconnected to device')
                        except Exception:
                            return json.dumps({'success': False, 'message': 'No device connected'})
                    except Exception as reconnect_error:
                        logger.error(f'Failed to reconnect: {reconnect_error}')
                        return json.dumps({'success': False,
                                           'message': f'No device connected. Reconnect failed: {str(reconnect_error)}'})

                # Perform the click
                logger.info(f'Simulating click at ({x}, {y})')
                helper.control.input.touch_tap(int(x), int(y))

                return json.dumps({
                    'success': True,
                    'message': f'Clicked at ({x}, {y})'
                })
            except Exception as e:
                logger.error(f'Error simulating click: {e}')
                return json.dumps({'success': False, 'message': str(e)})

        @self.app.route('/api/config', method='GET')
        def api_get_config():
            bottle.response.content_type = 'application/json'
            try:
                if self.config_getter is None:
                    return json.dumps({'success': False, 'message': 'Config getter not available'})
                
                get_config_func, _ = self.config_getter()
                config = get_config_func()
                return json.dumps({
                    'success': True,
                    'config': config
                })
            except Exception as e:
                logger.error(f'Error getting config: {e}')
                return json.dumps({'success': False, 'message': str(e)})

        @self.app.route('/api/config', method='POST')
        def api_update_config():
            bottle.response.content_type = 'application/json'
            try:
                if self.config_getter is None:
                    return json.dumps({'success': False, 'message': 'Config getter not available'})
                
                # Parse request body
                data = bottle.request.json
                if not data:
                    return json.dumps({'success': False, 'message': 'Invalid request data'})

                _, update_config_func = self.config_getter()
                
                # Extract parameters
                sanity_mode = data.get('sanity_mode')
                rouge_like = data.get('rouge_like')
                grab_red_ticket = data.get('grab_red_ticket')
                
                # Update config
                success = update_config_func(
                    sanity_mode=sanity_mode,
                    rouge_like=rouge_like,
                    grab_red_ticket_val=grab_red_ticket
                )
                
                if success:
                    return json.dumps({
                        'success': True,
                        'message': 'Configuration updated successfully'
                    })
                else:
                    return json.dumps({
                        'success': False,
                        'message': 'Failed to save configuration'
                    })
            except Exception as e:
                logger.error(f'Error updating config: {e}')
                return json.dumps({'success': False, 'message': str(e)})

        @self.app.route('/api/maa/tasks', method='GET')
        def api_get_maa_tasks():
            bottle.response.content_type = 'application/json'
            try:
                import os
                # Path to my_tasks.toml
                # Assuming the path relative to the project root or absolute path
                # Based on user request: Arknights\addons\contrib\maa\cli_config\maa\tasks\my_tasks.toml
                # We should probably construct this path dynamically or use a fixed path relative to this file
                
                # Let's try to find the file relative to this script
                base_dir = os.path.dirname(os.path.abspath(__file__))
                task_file_path = os.path.join(base_dir, 'Arknights', 'addons', 'contrib', 'maa', 'cli_config', 'maa', 'tasks', 'my_tasks.toml')
                
                logger.info(f'Attempting to read MAA tasks from: {task_file_path}')
                
                if not os.path.exists(task_file_path):
                    logger.error(f'File not found: {task_file_path}')
                    return json.dumps({'success': False, 'message': f'File not found: {task_file_path}'})
                
                with open(task_file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                logger.info(f'Successfully read {len(content)} bytes')
                return json.dumps({
                    'success': True,
                    'content': content
                })
            except Exception as e:
                logger.error(f'Error reading MAA tasks: {e}')
                return json.dumps({'success': False, 'message': str(e)})

        @self.app.route('/api/maa/tasks', method='POST')
        def api_update_maa_tasks():
            bottle.response.content_type = 'application/json'
            try:
                import os
                data = bottle.request.json
                if not data or 'content' not in data:
                    return json.dumps({'success': False, 'message': 'Invalid request data'})
                
                content = data['content']
                
                base_dir = os.path.dirname(os.path.abspath(__file__))
                task_file_path = os.path.join(base_dir, 'Arknights', 'addons', 'contrib', 'maa', 'cli_config', 'maa', 'tasks', 'my_tasks.toml')
                
                # Create directory if it doesn't exist (though it should)
                os.makedirs(os.path.dirname(task_file_path), exist_ok=True)
                
                with open(task_file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                    
                return json.dumps({
                    'success': True,
                    'message': 'MAA tasks configuration saved successfully'
                })
            except Exception as e:
                logger.error(f'Error updating MAA tasks: {e}')
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
            if helper is not None and hasattr(helper, '_controller') and helper._controller is not None:
                status['device_connected'] = True
            else:
                status['device_connected'] = False
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
            position: relative;
        }

        .screenshot-container img {
            max-width: 100%;
            height: auto;
            border-radius: 10px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
            cursor: crosshair;
        }

        .click-indicator {
            position: absolute;
            width: 30px;
            height: 30px;
            border: 3px solid #10b981;
            border-radius: 50%;
            pointer-events: none;
            animation: clickPulse 0.6s ease-out;
            transform: translate(-50%, -50%);
        }

        @keyframes clickPulse {
            0% {
                opacity: 1;
                transform: translate(-50%, -50%) scale(0.5);
            }
            100% {
                opacity: 0;
                transform: translate(-50%, -50%) scale(2);
            }
        }

        .coordinate-display {
            position: absolute;
            bottom: 10px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(0, 0, 0, 0.7);
            color: white;
            padding: 5px 10px;
            border-radius: 5px;
            font-size: 0.9em;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.3s;
        }

        .screenshot-container:hover .coordinate-display {
            opacity: 1;
        }

        .loading {
            text-align: center;
            padding: 20px;
            color: #666;
        }

        .loading-indicator {
            text-align: center;
            padding: 10px;
            color: #667eea;
            font-weight: 500;
            display: none;
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

        .toggle-container {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 8px;
        }

        .toggle-switch {
            position: relative;
            display: inline-block;
            width: 50px;
            height: 26px;
        }

        .toggle-switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }

        .toggle-slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: #ccc;
            transition: 0.3s;
            border-radius: 26px;
        }

        .toggle-slider:before {
            position: absolute;
            content: "";
            height: 18px;
            width: 18px;
            left: 4px;
            bottom: 4px;
            background-color: white;
            transition: 0.3s;
            border-radius: 50%;
        }

        .toggle-switch input:checked + .toggle-slider {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }

        .toggle-switch input:checked + .toggle-slider:before {
            transform: translateX(24px);
        }

        .toggle-label {
            font-weight: 500;
            color: #333;
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
            <div class="btn-group">
                <button class="btn-primary" onclick="triggerTask()">🚀 立即执行任务</button>
                <button class="btn-success" onclick="startEmulator()">▶️ 启动模拟器</button>
                <button class="btn-danger" onclick="stopEmulator()">⏹️ 关闭模拟器</button>
            </div>
        </div>

        <div class="card">
            <h2>⚙️ 配置管理</h2>
            <div id="config-form">
                <div style="margin-bottom: 20px;">
                    <label style="display: block; margin-bottom: 5px; font-weight: bold; color: #333;">理智消耗模式 (Sanity Mode)</label>
                    <select id="sanity-mode-select" style="width: 100%; padding: 10px; border: 2px solid #667eea; border-radius: 8px; font-size: 1em;">
                        <option value="grass">🌾 Grass (自动刷资源)</option>
                        <option value="1-7">📦 1-7 (固定关卡)</option>
                        <option value="latest">🆕 Latest (最新活动)</option>
                        <option value="custom">✏️ 自定义关卡代码</option>
                    </select>
                    <input type="text" id="custom-stage-input" placeholder="输入关卡代码，如 HE-7" 
                           style="width: 100%; padding: 10px; border: 2px solid #667eea; border-radius: 8px; font-size: 1em; margin-top: 10px; display: none;">
                </div>

                <div style="margin-bottom: 20px;">
                    <div class="toggle-container">
                        <label class="toggle-switch">
                            <input type="checkbox" id="rouge-like-toggle">
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="toggle-label">🎮 启用 MAA 肉鸽模式 (Rouge-like)</span>
                    </div>
                </div>

                <div style="margin-bottom: 20px;">
                    <div class="toggle-container">
                        <label class="toggle-switch">
                            <input type="checkbox" id="grab-red-ticket-toggle">
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="toggle-label">🎫 自动获取红票 (Grab Red Ticket)</span>
                    </div>
                </div>

                <div class="btn-group">
                    <button class="btn-primary" onclick="saveConfig()">💾 保存配置</button>
                    <button class="btn-success" onclick="loadConfig()">🔄 刷新配置</button>
                </div>
            </div>
            <div id="config-message-container"></div>
        </div>

        <div class="card">
            <h2>📝 MAA 任务配置</h2>
            <p style="margin-bottom: 10px; color: #666;">直接编辑 my_tasks.toml 文件内容：</p>
            <textarea id="maa-tasks-content" spellcheck="false"
                style="width: 100%; height: 400px; padding: 15px; border: 2px solid #667eea; border-radius: 8px; font-family: 'Consolas', 'Monaco', monospace; font-size: 14px; line-height: 1.5; resize: vertical; background: #f8f9fa; color: #333;"></textarea>
            
            <div class="btn-group" style="margin-top: 15px;">
                <button class="btn-primary" onclick="saveMaaTasks()">💾 保存 MAA 配置</button>
                <button class="btn-success" onclick="loadMaaTasks()">🔄 刷新 MAA 配置</button>
            </div>
        </div>

        <div class="card">
            <h2>🖥️ 系统日志</h2>
            <div id="log-terminal" style="
                background-color: #1e1e1e; 
                color: #d4d4d4; 
                font-family: 'Consolas', 'Monaco', monospace; 
                font-size: 13px; 
                padding: 15px; 
                border-radius: 8px; 
                height: 400px; 
                overflow-y: auto; 
                white-space: pre-wrap;
                border: 1px solid #333;
                box-shadow: inset 0 0 10px rgba(0,0,0,0.5);
            "></div>
            <div style="margin-top: 10px; display: flex; justify-content: space-between; align-items: center;">
                <div class="status-label" id="ws-status">⚪ 连接中...</div>
                <button class="btn-primary" style="max-width: 120px; padding: 8px 16px;" onclick="clearLogs()">🗑️ 清空日志</button>
            </div>
        </div>

        <div class="card">
            <h2>屏幕截图</h2>
            <div class="btn-group" style="margin-bottom: 15px;">
                <button class="btn-primary" onclick="refreshScreenshot()">🔄 刷新截图</button>
                <div class="toggle-container">
                    <label class="toggle-switch">
                        <input type="checkbox" id="auto-refresh-toggle" onchange="toggleAutoRefresh()">
                        <span class="toggle-slider"></span>
                    </label>
                    <span class="toggle-label">自动刷新 (3s)</span>
                </div>
            </div>
            <div class="screenshot-container">
                <img id="screenshot" src="" alt="点击上方按钮刷新截图" style="display:none;">
                <div id="screenshot-loading" class="loading">点击上方按钮刷新截图</div>
                <div id="coordinate-display" class="coordinate-display">X: 0, Y: 0</div>
            </div>
            <div id="loading-indicator" class="loading-indicator">🔄 加载中...</div>
            <div id="message-container"></div>
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
                showMessage('✗ 请求失败: ' + error.message, 'error');
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
            const loadingIndicator = document.getElementById('loading-indicator');
            const coordDisplay = document.getElementById('coordinate-display');

            // Show loading indicator below the image, don't hide the current image
            loadingIndicator.style.display = 'block';

            try {
                const response = await fetch('/api/screenshot');
                const data = await response.json();

                if (data.success) {
                    img.src = data.image;
                    img.style.display = 'block';
                    loading.style.display = 'none';
                    loadingIndicator.style.display = 'none';

                    // Store actual screenshot size for coordinate mapping
                    if (data.size) {
                        img.dataset.actualWidth = data.size[0];
                        img.dataset.actualHeight = data.size[1];
                    }

                    showMessage('✓ 截图已刷新', 'success');
                } else {
                    loadingIndicator.style.display = 'none';
                    // Only show error in the main loading area if no image is displayed
                    if (img.style.display === 'none') {
                        loading.textContent = '获取截图失败: ' + data.message;
                        loading.style.display = 'block';
                    }
                    showMessage('✗ ' + data.message, 'error');
                }
            } catch (error) {
                loadingIndicator.style.display = 'none';
                if (img.style.display === 'none') {
                    loading.textContent = '请求失败: ' + error.message;
                    loading.style.display = 'block';
                }
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }

        // Handle screenshot click and coordinates
        document.addEventListener('DOMContentLoaded', function() {
            const screenshotImg = document.getElementById('screenshot');
            const coordDisplay = document.getElementById('coordinate-display');
            const screenshotContainer = document.querySelector('.screenshot-container');

            if (screenshotImg) {
                // Update coordinate display on mouse move
                screenshotImg.addEventListener('mousemove', function(e) {
                    const rect = screenshotImg.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const y = e.clientY - rect.top;

                    // Calculate actual device coordinates
                    // Default to 1:1 if actual size not yet loaded
                    const actualW = parseFloat(screenshotImg.dataset.actualWidth) || rect.width;
                    const actualH = parseFloat(screenshotImg.dataset.actualHeight) || rect.height;
                    
                    const scaleX = actualW / rect.width;
                    const scaleY = actualH / rect.height;
                    
                    const actualX = Math.round(x * scaleX);
                    const actualY = Math.round(y * scaleY);

                    if (coordDisplay) {
                        coordDisplay.textContent = `X: ${actualX}, Y: ${actualY}`;
                    }
                });

                // Handle click
                screenshotImg.addEventListener('click', async function(e) {
                    const rect = screenshotImg.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    const y = e.clientY - rect.top;

                    const actualW = parseFloat(screenshotImg.dataset.actualWidth) || rect.width;
                    const actualH = parseFloat(screenshotImg.dataset.actualHeight) || rect.height;

                    const scaleX = actualW / rect.width;
                    const scaleY = actualH / rect.height;
                    
                    const actualX = Math.round(x * scaleX);
                    const actualY = Math.round(y * scaleY);

                    // Show visual feedback
                    const indicator = document.createElement('div');
                    indicator.className = 'click-indicator';
                    indicator.style.left = (e.clientX - screenshotContainer.getBoundingClientRect().left) + 'px';
                    indicator.style.top = (e.clientY - screenshotContainer.getBoundingClientRect().top) + 'px';
                    screenshotContainer.appendChild(indicator);
                    setTimeout(() => indicator.remove(), 600);

                    // Send click to backend
                    try {
                        const response = await fetch('/api/click', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({
                                x: actualX,
                                y: actualY
                            })
                        });

                        const data = await response.json();
                        if (data.success) {
                            showMessage(`✓ 已点击 (${actualX}, ${actualY})`, 'success');
                        } else {
                            showMessage('✗ 点击失败: ' + data.message, 'error');
                        }
                    } catch (error) {
                        showMessage('✗ 请求失败: ' + error.message, 'error');
                    }
                });
            }
            
            // Handle sanity mode dropdown change
            const sanityModeSelect = document.getElementById('sanity-mode-select');
            const customStageInput = document.getElementById('custom-stage-input');
            
            if (sanityModeSelect && customStageInput) {
                sanityModeSelect.addEventListener('change', function() {
                    if (this.value === 'custom') {
                        customStageInput.style.display = 'block';
                    } else {
                        customStageInput.style.display = 'none';
                    }
                });
            }

            // Initial load
            loadConfig();
            loadMaaTasks();
        });

        // Configuration management functions
        async function loadConfig() {
            try {
                const response = await fetch('/api/config');
                const data = await response.json();

                if (data.success) {
                    const config = data.config;
                    
                    // Set sanity mode
                    const sanityModeSelect = document.getElementById('sanity-mode-select');
                    const customStageInput = document.getElementById('custom-stage-input');
                    
                    const predefinedModes = ['grass', '1-7', 'latest'];
                    if (predefinedModes.includes(config.sanity_mode)) {
                        sanityModeSelect.value = config.sanity_mode;
                        customStageInput.style.display = 'none';
                    } else {
                        sanityModeSelect.value = 'custom';
                        customStageInput.value = config.sanity_mode;
                        customStageInput.style.display = 'block';
                    }
                    
                    // Set rouge-like
                    document.getElementById('rouge-like-toggle').checked = config.rouge_like;
                    
                    // Set grab red ticket
                    document.getElementById('grab-red-ticket-toggle').checked = config.grab_red_ticket;
                    
                    showMessage('✓ 配置已加载', 'success');
                } else {
                    showMessage('✗ 加载配置失败: ' + data.message, 'error');
                }
            } catch (error) {
                console.error('Error loading config:', error);
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }

        async function saveConfig() {
            try {
                const sanityModeSelect = document.getElementById('sanity-mode-select');
                const customStageInput = document.getElementById('custom-stage-input');
                
                let sanityMode;
                if (sanityModeSelect.value === 'custom') {
                    sanityMode = customStageInput.value.trim();
                    if (!sanityMode) {
                        showMessage('✗ 请输入自定义关卡代码', 'error');
                        return;
                    }
                } else {
                    sanityMode = sanityModeSelect.value;
                }
                
                const rougeLike = document.getElementById('rouge-like-toggle').checked;
                const grabRedTicket = document.getElementById('grab-red-ticket-toggle').checked;
                
                const response = await fetch('/api/config', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        sanity_mode: sanityMode,
                        rouge_like: rougeLike,
                        grab_red_ticket: grabRedTicket
                    })
                });

                const data = await response.json();
                if (data.success) {
                    showMessage('✓ 配置已保存', 'success');
                } else {
                    showMessage('✗ 保存失败: ' + data.message, 'error');
                }
            } catch (error) {
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }

        // WebSocket Logging
        let ws = null;
        const logTerminal = document.getElementById('log-terminal');
        const wsStatus = document.getElementById('ws-status');
        let reconnectTimer = null;

        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/api/logs/ws`;
            
            ws = new WebSocket(wsUrl);

            ws.onopen = function() {
                if (wsStatus) wsStatus.innerHTML = '<span class="status-online">🟢 已连接</span>';
                if (reconnectTimer) {
                    clearInterval(reconnectTimer);
                    reconnectTimer = null;
                }
            };

            ws.onmessage = function(event) {
                if (!logTerminal) return;
                const msg = event.data;
                const span = document.createElement('span');
                span.textContent = msg + '\n';
                
                // Colorize based on log level
                if (msg.includes('ERROR')) {
                    span.style.color = '#f87171';
                } else if (msg.includes('WARNING')) {
                    span.style.color = '#facc15';
                } else if (msg.includes('INFO')) {
                    span.style.color = '#60a5fa';
                }

                logTerminal.appendChild(span);
                
                // Auto scroll to bottom
                logTerminal.scrollTop = logTerminal.scrollHeight;
                
                // Limit lines to prevent memory issues
                if (logTerminal.childElementCount > 2000) {
                    logTerminal.removeChild(logTerminal.firstChild);
                }
            };

            ws.onclose = function() {
                if (wsStatus) wsStatus.innerHTML = '<span class="status-offline">🔴 已断开</span>';
                if (!reconnectTimer) {
                    reconnectTimer = setInterval(connectWebSocket, 3000);
                }
            };

            ws.onerror = function(error) {
                console.error('WebSocket error:', error);
                ws.close();
            };
        }

        function clearLogs() {
            if (logTerminal) logTerminal.innerHTML = '';
        }

        // Initialize WebSocket
        if (document.getElementById('log-terminal')) {
            connectWebSocket();
        }


        // MAA Tasks Functions
        async function loadMaaTasks() {
            try {
                const response = await fetch('/api/maa/tasks?t=' + new Date().getTime());
                const data = await response.json();
                
                if (data.success) {
                    console.log('Loaded content length:', data.content.length);
                    document.getElementById('maa-tasks-content').value = data.content;
                    showMessage('✓ MAA任务配置已加载', 'success');
                } else {
                    console.error('Failed to load MAA tasks:', data.message);
                    showMessage('✗ 加载MAA配置失败: ' + data.message, 'error');
                }
            } catch (error) {
                console.error('Error loading MAA tasks:', error);
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }

        async function saveMaaTasks() {
            try {
                const content = document.getElementById('maa-tasks-content').value;
                
                const response = await fetch('/api/maa/tasks', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ content: content })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showMessage('✓ MAA任务配置已保存', 'success');
                } else {
                    showMessage('✗ 保存MAA配置失败: ' + data.message, 'error');
                }
            } catch (error) {
                console.error('Error saving MAA tasks:', error);
                showMessage('✗ 请求失败: ' + error.message, 'error');
            }
        }

        // Auto-refresh screenshot functionality
        let autoRefreshInterval = null;

        function toggleAutoRefresh() {
            const toggle = document.getElementById('auto-refresh-toggle');

            if (toggle.checked) {
                // Enable auto-refresh
                refreshScreenshot(); // Refresh immediately
                autoRefreshInterval = setInterval(refreshScreenshot, 3000); // Then every 3 seconds
                showMessage('✓ 自动刷新已启用 (每3秒)', 'success');
            } else {
                // Disable auto-refresh
                if (autoRefreshInterval) {
                    clearInterval(autoRefreshInterval);
                    autoRefreshInterval = null;
                }
                showMessage('✓ 自动刷新已关闭', 'success');
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
                bottle.run(self.app, host='0.0.0.0', port=self.port, server=GeventWebSocketServer, quiet=True)
            except Exception as e:
                logger.error(f'Web server error: {e}')

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()
        self.is_running = True
        logger.info(f'Web admin accessible at http://localhost:{self.port}')
