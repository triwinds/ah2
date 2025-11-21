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
        self.maa_buffer = collections.deque(maxlen=capacity)  # Separate buffer for MAA logs
        self.sockets = set()
        self.maa_sockets = set()  # Separate sockets for MAA logs
        self.formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    def emit(self, record):
        try:
            if record.name == 'geventwebsocket.handler':
                return
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

    def add_maa_log(self, log_message, log_type='info'):
        """Add MAA log message to the MAA buffer"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted_msg = f"{timestamp} - MAA - {log_type.upper()} - {log_message}"
        self.maa_buffer.append(formatted_msg)
        
        # Broadcast to MAA websocket connections
        dead_sockets = set()
        for ws in self.maa_sockets:
            try:
                ws.send(formatted_msg)
            except Exception:
                dead_sockets.add(ws)
        
        for ws in dead_sockets:
            self.maa_sockets.remove(ws)


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
        self.log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger('geventwebsocket.handler').setLevel(logging.WARNING)

        # Setup MAA log parser
        self._setup_maa_log_parser()
        
        # Setup routes
        self._setup_routes()

    def _setup_maa_log_parser(self):
        """Setup MAA log parser to capture and parse MAA logs"""
        import re
        
        # Monkey patch the run_task function in maa_cli to capture logs
        try:
            from Arknights.addons.contrib.maa import maa_cli
            
            # Store original function
            original_run_task = maa_cli.run_task
            
            def patched_run_task(task_name: str, timeout: int = 3600):
                """Patched version of run_task that captures logs"""
                import subprocess
                import selectors
                
                maa_path = maa_cli.maa_path
                cmd = [maa_path, 'run', task_name, '-vvv']
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                start_time = time.time()
                maa_cli.processes.append(p)
                sel = selectors.DefaultSelector()
                sel.register(p.stdout, selectors.EVENT_READ)
                sel.register(p.stderr, selectors.EVENT_READ)
                log_item = ""
                summary_flag = False
                summary = ""
                ok = True
                
                try:
                    while ok:
                        for key, mask in sel.select(timeout=1.0):
                            line = key.fileobj.readline().decode()
                            if key.fileobj is p.stdout and (not line or line == ""):
                                ok = False
                                break
                            if key.fileobj is p.stdout:
                                # Capture stdout logs
                                if line.startswith('[INFO]'):
                                    continue
                                if line.startswith('Summary'):
                                    summary_flag = True
                                    self.log_handler.add_maa_log(line.strip(), 'info')
                                    continue
                                if summary_flag and not line.startswith('-----------------'):
                                    summary += line
                                    self.log_handler.add_maa_log(line.strip(), 'info')
                                else:
                                    self.log_handler.add_maa_log(line.strip(), 'info')
                            else:
                                # Capture stderr logs (MAA detailed logs)
                                if line.startswith('[20'):
                                    if log_item.strip():
                                        self.log_handler.add_maa_log(log_item.strip(), 'debug')
                                    log_item = line
                                else:
                                    log_item += line
                                self.log_handler.add_maa_log(line.strip(), 'debug')
                        
                        if p.poll() is None:
                            if timeout is not None and (time.time() - start_time) > timeout:
                                p.terminate()
                                raise subprocess.TimeoutExpired(p.args, timeout)
                except subprocess.TimeoutExpired:
                    logger.error(f"Task {task_name} timed out after {timeout} seconds")
                    p.terminate()
                    self.log_handler.add_maa_log(f"Task {task_name} timed out after {timeout} seconds", 'error')
                    return f"Task timed out after {timeout} seconds"
                finally:
                    sel.close()
                    if p in maa_cli.processes:
                        maa_cli.processes.remove(p)
                
                # Handle special summary processing
                if '高级资深干员' in summary:
                    self.log_handler.add_maa_log('公招出 6 星了!', 'info')
                
                return summary.strip()
            
            # Replace the original function
            maa_cli.run_task = patched_run_task
            logger.info("MAA log parser initialized successfully")
            
        except ImportError:
            logger.warning("MAA CLI module not found, log parser not initialized")
        except Exception as e:
            logger.error(f"Failed to initialize MAA log parser: {e}")

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
        import os
        template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'dashboard.html')
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f'Error reading dashboard template: {e}')
            return f"Error loading dashboard: {e}"

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
