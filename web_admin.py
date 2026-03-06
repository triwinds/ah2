from gevent import monkey; monkey.patch_all()
import bottle
from bottle.ext.websocket import GeventWebSocketServer
from bottle.ext.websocket import websocket
import gevent.lock
import json
import logging
import re
import threading
import time
import base64
import collections
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional

from scrcpy_session import ScrcpySession

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
        self.max_screen_stream_connections = 2
        self._screen_stream_connections = 0
        self._screen_stream_lock = threading.Lock()
        self._scrcpy_sessions: dict[str, ScrcpySession] = {}
        self._scrcpy_lock = gevent.lock.RLock()
        self._trigger_lock = threading.Lock()
        self._device_switch_lock = threading.Lock()
        self._last_manual_trigger_time = None

        # Setup logging handler
        self.log_handler = MemoryLogHandler()
        self.log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.log_handler)
        
        # Setup MAA logger
        maa_output_logger = logging.getLogger('MAA.output')
        maa_output_logger.setLevel(logging.DEBUG)  # Set logger level to DEBUG
        maa_output_logger.addHandler(self.log_handler)  # MAA output doesn't propagate to root
        
        logging.getLogger('geventwebsocket.handler').setLevel(logging.WARNING)


        
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

        @self.app.route('/api/screen/ws', apply=[websocket])
        def api_screen_ws(ws):
            if ws is None:
                bottle.response.status = 400
                return 'WebSocket connection required'

            with self._screen_stream_lock:
                if self._screen_stream_connections >= self.max_screen_stream_connections:
                    try:
                        ws.send(json.dumps({
                            'type': 'stream_status',
                            'status': 'error',
                            'message': 'Too many stream connections'
                        }))
                    except Exception:
                        pass
                    try:
                        ws.close()
                    except Exception:
                        pass
                    return
                self._screen_stream_connections += 1
                active_connections = self._screen_stream_connections
            logger.info(f'Screen stream websocket connected, active connections={active_connections}')

            consecutive_failures = 0
            session = None

            try:
                while True:
                    if getattr(ws, 'closed', False):
                        return

                    helper, helper_error = self._get_helper_with_reconnect()
                    if helper is None:
                        consecutive_failures += 1
                        logger.error(f'No connected device for screen stream ({consecutive_failures}/3): {helper_error}')
                        if consecutive_failures >= 3:
                            self._send_stream_status(ws, 'fallback', helper_error or 'No device connected')
                            return
                        if getattr(ws, 'closed', False):
                            return
                        time.sleep(min(2 ** (consecutive_failures - 1), 5))
                        continue

                    try:
                        session = self._get_or_create_scrcpy_session(helper)
                        consecutive_failures = 0
                        session.subscribe(ws)
                        break
                    except Exception as stream_error:
                        consecutive_failures += 1
                        logger.error(f'Failed to start scrcpy stream ({consecutive_failures}/3): {stream_error}')
                        if consecutive_failures >= 3:
                            self._send_stream_status(ws, 'fallback', f'scrcpy unavailable: {stream_error}')
                            return
                        if getattr(ws, 'closed', False):
                            return
                        time.sleep(min(2 ** (consecutive_failures - 1), 5))
                        continue

                while True:
                    if getattr(ws, 'closed', False):
                        return
                    message = ws.receive()
                    if message is None:
                        return
            finally:
                if session is not None:
                    session.unsubscribe(ws)
                with self._screen_stream_lock:
                    self._screen_stream_connections = max(0, self._screen_stream_connections - 1)
                    active_connections = self._screen_stream_connections
                logger.info(f'Screen stream websocket disconnected, active connections={active_connections}')


        @self.app.route('/')
        def index():
            return self._get_dashboard_html()

        @self.app.route('/api/status')
        def api_status():
            bottle.response.content_type = 'application/json'
            return json.dumps(self._get_status())

        @self.app.route('/api/device')
        def api_device():
            bottle.response.content_type = 'application/json'
            preferred_serial = self._get_preferred_serial()
            try:
                helper, helper_error = self._get_helper_with_reconnect()
                if helper is None:
                    return json.dumps(self._build_device_payload(
                        success=False,
                        connected=False,
                        configured_device=preferred_serial,
                        message=helper_error or f'No device connected ({preferred_serial})'
                    ))

                controller = getattr(helper, '_controller', None)
                if controller is None:
                    return json.dumps(self._build_device_payload(
                        success=True,
                        connected=False,
                        configured_device=preferred_serial,
                        message='No device connected'
                    ))

                return json.dumps(self._build_device_payload(
                    success=True,
                    connected=True,
                    configured_device=preferred_serial,
                    device=str(controller)
                ))
            except Exception as e:
                logger.error(f'Error getting device info: {e}')
                return json.dumps(self._build_device_payload(
                    success=False,
                    connected=False,
                    configured_device=preferred_serial,
                    message=str(e)
                ))

        @self.app.route('/api/device', method='POST')
        def api_set_device():
            bottle.response.content_type = 'application/json'
            preferred_serial = self._get_preferred_serial()
            try:
                data = bottle.request.json
                if not data:
                    return json.dumps(self._build_device_payload(
                        success=False,
                        connected=False,
                        configured_device=preferred_serial,
                        message='Invalid request data'
                    ))

                adb_serial = str(data.get('device', '')).strip()
                if not adb_serial:
                    return json.dumps(self._build_device_payload(
                        success=False,
                        connected=False,
                        configured_device=preferred_serial,
                        message='Device is required'
                    ))

                with self._device_switch_lock:
                    controller = self._switch_device(adb_serial)

                    import app
                    app.config.device.adb_always_use_device = adb_serial
                    app.save()

                return json.dumps(self._build_device_payload(
                    success=True,
                    connected=True,
                    configured_device=adb_serial,
                    device=str(controller),
                    message=f'已切换到 {controller}'
                ))
            except Exception as e:
                logger.error(f'Error switching device: {e}')
                return json.dumps(self._build_device_payload(
                    success=False,
                    connected=False,
                    configured_device=preferred_serial,
                    message=str(e)
                ))

        @self.app.route('/api/trigger', method='POST')
        def api_trigger():
            bottle.response.content_type = 'application/json'
            try:
                with self._trigger_lock:
                    # Trigger do_works job immediately.
                    # Protect against fast duplicate submissions from web UI retries/double clicks.
                    now = datetime.now(self.scheduler.timezone)
                    if (
                        self._last_manual_trigger_time is not None
                        and now - self._last_manual_trigger_time < timedelta(seconds=3)
                    ):
                        logger.info('Ignore duplicate manual trigger request within 3 seconds window.')
                        return json.dumps({'success': True, 'message': '任务已在触发中，请勿重复点击'})

                    job = self.scheduler.get_job('do_works')
                    if not job:
                        return json.dumps({'success': False, 'message': 'Job not found'})

                    # If next run is already imminent, treat this request as duplicate.
                    if job.next_run_time is not None and job.next_run_time <= now + timedelta(seconds=2):
                        logger.info(f'Ignore duplicate manual trigger request, next run already queued at {job.next_run_time}.')
                        return json.dumps({'success': True, 'message': '任务已排队执行'})

                    job.modify(next_run_time=now)
                    self._last_manual_trigger_time = now
                    logger.info(f'Manual trigger accepted, do_works next_run_time={now}')
                    return json.dumps({'success': True, 'message': '任务触发成功'})
            except Exception as e:
                logger.error(f'Error triggering task: {e}')
                return json.dumps({'success': False, 'message': str(e)})

        @self.app.route('/api/cancel/<job_id>', method='POST')
        def api_cancel(job_id):
            bottle.response.content_type = 'application/json'
            try:
                # Skip next run of specified job, but keep subsequent runs scheduled
                job = self.scheduler.get_job(job_id)
                if job:
                    if job.next_run_time is None:
                        return json.dumps({'success': False, 'message': 'Job has no scheduled runs'})
                    
                    # Get the current next run time (this is the one we want to skip)
                    current_next_run = job.next_run_time
                    
                    # To get the run AFTER current_next_run, we need to use
                    # a 'now' time that is AFTER current_next_run
                    # This way get_next_fire_time will calculate the next run from that point
                    from datetime import timedelta
                    fake_now = current_next_run + timedelta(seconds=1)
                    skipped_next_run = job.trigger.get_next_fire_time(current_next_run, fake_now)
                    
                    if skipped_next_run is None:
                        return json.dumps({'success': False, 'message': 'Cannot calculate next run time'})
                    
                    # Modify the job to skip the immediate next run
                    job.modify(next_run_time=skipped_next_run)
                    logger.info(f'Skipped next run of {job_id}, rescheduled to {skipped_next_run}')
                    return json.dumps({'success': True, 'message': f'Skipped next run of {job_id}'})
                else:
                    return json.dumps({'success': False, 'message': 'Job not found'})
            except Exception as e:
                logger.error(f'Error skipping job run: {e}')
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
                helper, helper_error = self._get_helper_with_reconnect()
                if helper is None:
                    bottle.response.content_type = 'application/json'
                    return json.dumps({'success': False, 'message': helper_error or 'No device connected'})

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
                helper, helper_error = self._get_helper_with_reconnect()
                if helper is None:
                    return json.dumps({'success': False, 'message': helper_error or 'No device connected'})

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

        @self.app.route('/api/swipe', method='POST')
        def api_swipe():
            bottle.response.content_type = 'application/json'
            try:
                data = bottle.request.json
                if not data:
                    bottle.response.status = 400
                    return json.dumps({'success': False, 'message': 'Invalid request data'})

                try:
                    x1 = int(data.get('x1'))
                    y1 = int(data.get('y1'))
                    x2 = int(data.get('x2'))
                    y2 = int(data.get('y2'))
                    duration = int(data.get('duration', 300))
                except (TypeError, ValueError):
                    bottle.response.status = 400
                    return json.dumps({'success': False, 'message': 'Invalid swipe parameters'})

                if duration < 50 or duration > 5000:
                    bottle.response.status = 400
                    return json.dumps({'success': False, 'message': 'Duration must be between 50 and 5000 ms'})

                helper, helper_error = self._get_helper_with_reconnect()
                if helper is None:
                    bottle.response.status = 503
                    return json.dumps({'success': False, 'message': helper_error or 'No device connected'})

                resolution = self._get_device_resolution(helper)
                if resolution is None:
                    bottle.response.status = 500
                    return json.dumps({'success': False, 'message': 'Failed to determine device resolution'})

                width, height = resolution
                in_bounds = (
                    0 <= x1 < width and
                    0 <= y1 < height and
                    0 <= x2 < width and
                    0 <= y2 < height
                )
                if not in_bounds:
                    bottle.response.status = 400
                    return json.dumps({
                        'success': False,
                        'message': f'Coordinates out of range (device: {width}x{height})'
                    })

                helper.control.adb.shell(f'input swipe {x1} {y1} {x2} {y2} {duration}')
                return json.dumps({
                    'success': True,
                    'message': f'Swiped ({x1}, {y1}) -> ({x2}, {y2}) in {duration}ms'
                })
            except Exception as e:
                logger.error(f'Error simulating swipe: {e}')
                bottle.response.status = 500
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

    def _is_helper_connected(self, helper):
        return helper is not None and hasattr(helper, '_controller') and helper._controller is not None

    def _get_preferred_serial(self):
        import app

        preferred_serial = app.config.device.adb_always_use_device.strip()
        return preferred_serial or '127.0.0.1:5555'

    def _build_device_payload(self, *, success, connected, configured_device=None, device=None, message=None):
        payload = {
            'success': success,
            'connected': connected,
            'configured_device': configured_device or self._get_preferred_serial(),
            'detected_devices': self._list_detected_devices(configured_device),
        }
        if device is not None:
            payload['device'] = device
        if message is not None:
            payload['message'] = message
        return payload

    def _list_detected_devices(self, preferred_serial=None):
        from automator.control.adb.target import ADBControllerTarget
        from automator.control.targets import enum_targets

        configured_device = (preferred_serial or self._get_preferred_serial()).strip()
        options = []
        seen = set()

        def add_option(value, label=None):
            value = (value or '').strip()
            if not value or value in seen:
                return
            seen.add(value)
            options.append({
                'value': value,
                'label': label or value,
            })

        add_option(configured_device, f'{configured_device}（当前默认）')
        add_option('127.0.0.1:5555', '127.0.0.1:5555（常用）')

        try:
            for target in enum_targets():
                if not isinstance(target, ADBControllerTarget):
                    continue
                value = (target.adb_serial or target.adb_address or '').strip()
                details = []
                if target.adb_address and target.adb_address != value:
                    details.append(target.adb_address)
                if target.adb_serial and target.adb_serial != value:
                    details.append(target.adb_serial)
                if target.description:
                    details.append(target.description)
                label = value
                extra = ' / '.join(dict.fromkeys(details))
                if extra:
                    label = f'{value}（{extra}）'
                add_option(value, label)
        except Exception as enum_error:
            logger.warning(f'Failed to enumerate device targets: {enum_error}')

        return options

    def _switch_device(self, adb_serial):
        helper = self.helper_getter()
        if helper is None:
            from Arknights.configure_launcher import get_helper

            helper = get_helper()
        if helper is None:
            raise RuntimeError('Helper not available')

        old_controller = helper.connect_device(adb_serial=adb_serial)
        controller = getattr(helper, '_controller', None)
        if controller is None:
            raise RuntimeError(f'No device connected ({adb_serial})')

        if old_controller is not None and old_controller is not controller:
            try:
                old_controller.close()
            except Exception as close_error:
                logger.warning(f'Failed to close previous controller: {close_error}')

        return controller

    def _get_helper_with_reconnect(self):
        helper = self.helper_getter()
        if self._is_helper_connected(helper):
            return helper, None

        preferred_serial = self._get_preferred_serial()

        if helper is not None:
            try:
                logger.info(f'Device not connected, trying direct connect to {preferred_serial}...')
                helper.connect_device(adb_serial=preferred_serial)
                if self._is_helper_connected(helper):
                    logger.info(f'Successfully connected to {preferred_serial}')
                    return helper, None
            except Exception as direct_connect_error:
                logger.warning(f'Direct connect failed: {direct_connect_error}')

        try:
            from Arknights.configure_launcher import reconnect_helper, get_helper
            logger.info('Device not connected, attempting to reconnect...')
            reconnect_helper()
            helper = get_helper()
            if self._is_helper_connected(helper):
                logger.info('Successfully reconnected to device')
                return helper, None

            if helper is not None:
                try:
                    logger.info(f'Reconnect has no device, trying direct connect to {preferred_serial}...')
                    helper.connect_device(adb_serial=preferred_serial)
                    if self._is_helper_connected(helper):
                        logger.info(f'Successfully connected to {preferred_serial} after reconnect')
                        return helper, None
                except Exception as direct_connect_after_reconnect_error:
                    logger.warning(f'Direct connect after reconnect failed: {direct_connect_after_reconnect_error}')

            return None, f'No device connected ({preferred_serial})'
        except Exception as reconnect_error:
            logger.error(f'Failed to reconnect: {reconnect_error}')
            return None, f'No device connected. Reconnect failed: {str(reconnect_error)}'

    def _get_device_resolution(self, helper):
        try:
            screenshot = helper.control.screenshot()
            width, height = screenshot.size
            if width > 0 and height > 0:
                return width, height
        except Exception as screenshot_error:
            logger.error(f'Failed to get device resolution from screenshot: {screenshot_error}')

        try:
            size_text = helper.control.adb.shell('wm size').decode(errors='ignore')
            size_pattern = re.search(r'Physical size:\s*(\d+)x(\d+)', size_text)
            if not size_pattern:
                size_pattern = re.search(r'Override size:\s*(\d+)x(\d+)', size_text)
            if size_pattern:
                width = int(size_pattern.group(1))
                height = int(size_pattern.group(2))
                if width > 0 and height > 0:
                    return width, height
        except Exception as shell_error:
            logger.warning(f'Failed to get device resolution from wm size: {shell_error}')

        return None

    def _cleanup_scrcpy_sessions_except(self, active_serial):
        stale_sessions = []
        with self._scrcpy_lock:
            for serial, session in list(self._scrcpy_sessions.items()):
                if serial == active_serial:
                    continue
                stale_sessions.append(session)
                self._scrcpy_sessions.pop(serial, None)

        for session in stale_sessions:
            session.stop(reason=f'device switched to {active_serial}')

    def _on_scrcpy_session_stopped(self, session: ScrcpySession):
        with self._scrcpy_lock:
            cached = self._scrcpy_sessions.get(session.serial)
            if cached is session:
                self._scrcpy_sessions.pop(session.serial, None)

    def _get_or_create_scrcpy_session(self, helper) -> ScrcpySession:
        adb = helper.control.adb
        serial = adb.serial
        if not serial:
            raise RuntimeError('scrcpy streaming requires a concrete ADB serial')

        self._cleanup_scrcpy_sessions_except(serial)

        with self._scrcpy_lock:
            session = self._scrcpy_sessions.get(serial)
            if session is not None and session.running and session.healthy:
                return session

            if session is not None:
                self._scrcpy_sessions.pop(serial, None)
                session.stop(reason='recreating unhealthy scrcpy session')

            session = ScrcpySession(adb, on_stopped=self._on_scrcpy_session_stopped)
            self._scrcpy_sessions[serial] = session
            try:
                session.start()
            except Exception:
                self._scrcpy_sessions.pop(serial, None)
                raise
            return session

    def _send_stream_status(self, ws, status, message):
        try:
            ws.send(json.dumps({
                'type': 'stream_status',
                'status': status,
                'message': message
            }))
        except Exception:
            pass

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
