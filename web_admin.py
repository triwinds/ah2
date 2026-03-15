from gevent import monkey; monkey.patch_all()
import bottle
from bottle.ext.websocket import GeventWebSocketServer
from bottle.ext.websocket import websocket
import gevent
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

from automator.control.types import ControllerCapabilities, EventAction
from scrcpy_session import ScrcpySession
from scrcpy_control_session import ScrcpyControlSession

logger = logging.getLogger(__name__)


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=1000, default_level=logging.INFO, debug_logger_names=None):
        super().__init__(level=logging.NOTSET)
        self.capacity = capacity
        self.default_level = default_level
        self.buffer = collections.deque(maxlen=capacity)
        self.debug_logger_names = tuple(debug_logger_names or ())
        self.sockets = set()
        self.formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    def _is_debug_passthrough_logger(self, logger_name: str) -> bool:
        return any(
            logger_name == name or logger_name.startswith(f'{name}.')
            for name in self.debug_logger_names
        )

    def emit(self, record):
        try:
            if record.name == 'geventwebsocket.handler':
                return
            if record.levelno < self.default_level and not self._is_debug_passthrough_logger(record.name):
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
    DEFAULT_STREAM_FPS = 30
    MIN_STREAM_FPS = 5
    MAX_STREAM_FPS = 60

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
        self._scrcpy_control_sessions: dict[str, ScrcpyControlSession] = {}
        self._webui_device_resolution_cache: dict[str, tuple[int, int]] = {}
        self._scrcpy_lock = gevent.lock.RLock()
        self._webui_touch_lock = gevent.lock.RLock()
        self._active_webui_touches: dict[str, dict[str, object]] = {}
        self._trigger_lock = threading.Lock()
        self._device_switch_lock = threading.Lock()
        self._last_manual_trigger_time = None

        # Setup logging handler
        self.log_handler = MemoryLogHandler(debug_logger_names={'MAA.output'})
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

        @self.app.route('/api/control/ws', apply=[websocket])
        def api_control_ws(ws):
            if ws is None:
                bottle.response.status = 400
                return 'WebSocket connection required'

            logger.info('Control websocket connected')
            self._send_control_status(ws, 'ready', 'Control websocket connected')
            active_touch = False

            try:
                while True:
                    message = ws.receive()
                    if message is None:
                        return

                    if isinstance(message, bytes):
                        try:
                            message = message.decode('utf-8')
                        except UnicodeDecodeError:
                            ws.send(json.dumps({
                                'type': 'action_result',
                                'success': False,
                                'message': 'Control message must be valid UTF-8'
                            }))
                            continue

                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        ws.send(json.dumps({
                            'type': 'action_result',
                            'success': False,
                            'message': 'Control message must be valid JSON'
                        }))
                        continue

                    if not isinstance(payload, dict):
                        ws.send(json.dumps({
                            'type': 'action_result',
                            'success': False,
                            'message': 'Control message must be a JSON object'
                        }))
                        continue

                    response, _ = self._handle_control_action(payload)
                    if response.get('success'):
                        if response.get('action') == 'touch_start':
                            active_touch = True
                        elif response.get('action') in {'touch_end', 'touch_cancel'}:
                            active_touch = False
                    ws.send(json.dumps(response))
            finally:
                if active_touch:
                    self._cancel_active_webui_touch_for_current_helper()
                logger.info('Control websocket disconnected')

        @self.app.route('/api/screen/ws', apply=[websocket])
        def api_screen_ws(ws):
            if ws is None:
                bottle.response.status = 400
                return 'WebSocket connection required'

            requested_fps = self._parse_stream_fps(bottle.request.query.get('fps'))

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
                        session = self._get_or_create_scrcpy_session(helper, requested_fps)
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
                data = bottle.request.json
                if not isinstance(data, dict):
                    bottle.response.status = 400
                    return json.dumps({'success': False, 'message': 'Invalid request data'})

                payload, status = self._execute_click(
                    data.get('x'),
                    data.get('y'),
                    data.get('screenWidth'),
                    data.get('screenHeight')
                )
                bottle.response.status = status
                return json.dumps(payload)
            except Exception as e:
                logger.error(f'Error simulating click: {e}')
                bottle.response.status = 500
                return json.dumps({'success': False, 'message': str(e)})

        @self.app.route('/api/swipe', method='POST')
        def api_swipe():
            bottle.response.content_type = 'application/json'
            try:
                data = bottle.request.json
                if not isinstance(data, dict):
                    bottle.response.status = 400
                    return json.dumps({'success': False, 'message': 'Invalid request data'})

                payload, status = self._execute_swipe(
                    data.get('x1'),
                    data.get('y1'),
                    data.get('x2'),
                    data.get('y2'),
                    data.get('duration', 300),
                    data.get('screenWidth'),
                    data.get('screenHeight')
                )
                bottle.response.status = status
                return json.dumps(payload)
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
                auto_chips = data.get('auto_chips')
                
                # Update config
                success = update_config_func(
                    sanity_mode=sanity_mode,
                    rouge_like=rouge_like,
                    grab_red_ticket_val=grab_red_ticket,
                    auto_chips=auto_chips
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

        active_serial = controller.adb.serial or adb_serial
        self._cleanup_scrcpy_sessions_except(active_serial)
        self._cleanup_scrcpy_control_sessions_except(active_serial)

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

    def _cleanup_scrcpy_control_sessions_except(self, active_serial):
        stale_sessions = []
        with self._scrcpy_lock:
            for serial, session in list(self._scrcpy_control_sessions.items()):
                if serial == active_serial:
                    continue
                stale_sessions.append(session)
                self._scrcpy_control_sessions.pop(serial, None)
                self._webui_device_resolution_cache.pop(serial, None)

        for session in stale_sessions:
            session.stop(reason=f'device switched to {active_serial}')

    def _on_scrcpy_session_stopped(self, session: ScrcpySession):
        with self._scrcpy_lock:
            cached = self._scrcpy_sessions.get(session.serial)
            if cached is session:
                self._scrcpy_sessions.pop(session.serial, None)

    def _on_scrcpy_control_session_stopped(self, session: ScrcpyControlSession):
        with self._scrcpy_lock:
            cached = self._scrcpy_control_sessions.get(session.serial)
            if cached is session:
                self._scrcpy_control_sessions.pop(session.serial, None)

    def _parse_stream_fps(self, value) -> int:
        try:
            fps = int(value)
        except (TypeError, ValueError):
            fps = self.DEFAULT_STREAM_FPS
        return max(self.MIN_STREAM_FPS, min(self.MAX_STREAM_FPS, fps))

    def _get_or_create_scrcpy_session(self, helper, requested_fps: int | None = None) -> ScrcpySession:
        adb = helper.control.adb
        serial = adb.serial
        if not serial:
            raise RuntimeError('scrcpy streaming requires a concrete ADB serial')

        target_fps = self._parse_stream_fps(requested_fps)

        self._cleanup_scrcpy_sessions_except(serial)

        with self._scrcpy_lock:
            session = self._scrcpy_sessions.get(serial)
            if session is not None and session.running and session.healthy and session.max_fps == target_fps:
                return session

            if session is not None:
                self._scrcpy_sessions.pop(serial, None)
                if session.max_fps != target_fps:
                    session.stop(reason=f'recreating scrcpy session for max_fps={target_fps}')
                else:
                    session.stop(reason='recreating unhealthy scrcpy session')

            session = ScrcpySession(adb, on_stopped=self._on_scrcpy_session_stopped, max_fps=target_fps)
            self._scrcpy_sessions[serial] = session
            try:
                session.start()
            except Exception:
                self._scrcpy_sessions.pop(serial, None)
                raise
            return session

    def _get_or_create_scrcpy_control_session(self, helper) -> ScrcpyControlSession:
        adb = helper.control.adb
        serial = adb.serial
        if not serial:
            raise RuntimeError('scrcpy control requires a concrete ADB serial')

        self._cleanup_scrcpy_control_sessions_except(serial)

        with self._scrcpy_lock:
            session = self._scrcpy_control_sessions.get(serial)
            if session is not None and session.running and session.healthy:
                return session

            if session is not None:
                self._scrcpy_control_sessions.pop(serial, None)
                session.stop(reason='recreating unhealthy scrcpy control session')

            session = ScrcpyControlSession(adb, on_stopped=self._on_scrcpy_control_session_stopped)
            self._scrcpy_control_sessions[serial] = session
            try:
                session.start()
            except Exception:
                self._scrcpy_control_sessions.pop(serial, None)
                raise
            return session

    def _parse_webui_screen_size(self, screen_width, screen_height):
        if screen_width is None and screen_height is None:
            return None, None, None

        try:
            parsed_width = int(screen_width)
            parsed_height = int(screen_height)
        except (TypeError, ValueError):
            return None, None, 'Invalid screen size'

        if parsed_width <= 0 or parsed_height <= 0:
            return None, None, 'Screen size must be positive'
        if parsed_width > 0xFFFF or parsed_height > 0xFFFF:
            return None, None, 'Screen size is too large for scrcpy control protocol'

        return parsed_width, parsed_height, None

    def _should_refresh_webui_device_resolution(self, cached_resolution, source_width=None, source_height=None):
        if cached_resolution is None:
            return True
        if source_width is None or source_height is None:
            return False
        cached_width, cached_height = cached_resolution
        if cached_width == cached_height or source_width == source_height:
            return False
        return (cached_width > cached_height) != (source_width > source_height)

    def _resolve_webui_control_sizes(self, helper, screen_width=None, screen_height=None):
        source_width, source_height, parse_error = self._parse_webui_screen_size(screen_width, screen_height)
        if parse_error is not None:
            return None, 400, parse_error

        serial = helper.control.adb.serial or 'default'
        with self._scrcpy_lock:
            cached_resolution = self._webui_device_resolution_cache.get(serial)

        if self._should_refresh_webui_device_resolution(cached_resolution, source_width, source_height):
            refreshed_resolution = self._get_device_resolution(helper)
            if refreshed_resolution is not None:
                cached_resolution = refreshed_resolution
                with self._scrcpy_lock:
                    self._webui_device_resolution_cache[serial] = refreshed_resolution

        if cached_resolution is None:
            if source_width is None or source_height is None:
                return None, 500, 'Failed to determine device resolution'
            cached_resolution = (source_width, source_height)

        target_width, target_height = cached_resolution
        if source_width is None or source_height is None:
            source_width, source_height = target_width, target_height

        return (source_width, source_height, target_width, target_height), 200, None

    def _scale_webui_coordinate(self, value, source_extent, target_extent):
        if source_extent <= 1 or target_extent <= 1:
            return 0
        if source_extent == target_extent:
            return value
        return int(round(value * (target_extent - 1) / (source_extent - 1)))

    def _map_webui_point_to_device(self, x, y, source_width, source_height, target_width, target_height):
        try:
            parsed_x = int(x)
            parsed_y = int(y)
        except (TypeError, ValueError):
            return None, 400, 'Invalid coordinates'

        if parsed_x < 0 or parsed_y < 0:
            return None, 400, 'Coordinates must be non-negative'
        if parsed_x >= source_width or parsed_y >= source_height:
            return None, 400, f'Coordinates out of range (screen: {source_width}x{source_height})'

        mapped_x = self._scale_webui_coordinate(parsed_x, source_width, target_width)
        mapped_y = self._scale_webui_coordinate(parsed_y, source_height, target_height)

        if mapped_x < 0 or mapped_x >= target_width or mapped_y < 0 or mapped_y >= target_height:
            return None, 400, f'Coordinates out of range (device: {target_width}x{target_height})'

        return (mapped_x, mapped_y), 200, None

    def _send_stream_status(self, ws, status, message):
        try:
            ws.send(json.dumps({
                'type': 'stream_status',
                'status': status,
                'message': message
            }))
        except Exception:
            pass

    def _send_control_status(self, ws, status, message):
        try:
            ws.send(json.dumps({
                'type': 'control_status',
                'status': status,
                'message': message
            }))
        except Exception:
            pass

    def _perform_scrcpy_control_tap(self, session, x: int, y: int, screen_width: int, screen_height: int, hold_time: float = 0.0):
        session.touch_tap(x, y, screen_width, screen_height, hold_time)

    def _perform_scrcpy_control_swipe(self, session, x0: int, y0: int, x1: int, y1: int, duration_ms: int, screen_width: int, screen_height: int):
        session.touch_swipe(x0, y0, x1, y1, duration_ms, screen_width, screen_height)

    def _perform_scrcpy_control_touch_start(self, session, x: int, y: int, screen_width: int, screen_height: int):
        session.touch_down(x, y, screen_width, screen_height)

    def _perform_scrcpy_control_touch_move(self, session, x: int, y: int, screen_width: int, screen_height: int):
        session.touch_move(x, y, screen_width, screen_height)

    def _perform_scrcpy_control_touch_end(self, session, x: int, y: int, screen_width: int, screen_height: int):
        session.touch_up(x, y, screen_width, screen_height)

    def _perform_helper_control_tap(self, helper, x: int, y: int):
        control = getattr(helper, 'control', None)
        if control is None or getattr(control, 'input', None) is None:
            raise RuntimeError('device input adapter is not available')
        control.input.touch_tap(int(x), int(y))

    def _perform_helper_control_swipe(self, helper, x0: int, y0: int, x1: int, y1: int, duration_ms: int):
        control = getattr(helper, 'control', None)
        if control is None or getattr(control, 'input', None) is None:
            raise RuntimeError('device input adapter is not available')
        control.input.touch_swipe(int(x0), int(y0), int(x1), int(y1), max(0.05, min(5.0, int(duration_ms) / 1000.0)))

    def _get_helper_touch_input(self, helper):
        control = getattr(helper, 'control', None)
        input_adapter = getattr(control, 'input', None) if control is not None else None
        if input_adapter is None:
            raise RuntimeError('device input adapter is not available')

        caps = input_adapter.get_input_capabilities()
        if ControllerCapabilities.TOUCH_EVENTS not in caps:
            raise RuntimeError('device input adapter does not support touch events')
        return input_adapter

    def _resolve_live_touch_context(self, x, y, screen_width=None, screen_height=None):
        helper, helper_error = self._get_helper_with_reconnect()
        if helper is None:
            return None, 503, helper_error or 'No device connected'

        size_result, size_status, size_error = self._resolve_webui_control_sizes(helper, screen_width, screen_height)
        if size_error is not None:
            return None, size_status, size_error

        source_width, source_height, target_width, target_height = size_result
        mapped_point, point_status, point_error = self._map_webui_point_to_device(
            x,
            y,
            source_width,
            source_height,
            target_width,
            target_height,
        )
        if point_error is not None:
            return None, point_status, point_error

        serial = helper.control.adb.serial or 'default'
        mapped_x, mapped_y = mapped_point
        return (helper, serial, mapped_x, mapped_y, target_width, target_height), 200, None

    def _get_active_webui_touch_state(self, serial: str):
        with self._webui_touch_lock:
            return self._active_webui_touches.get(serial)

    def _set_active_webui_touch_state(self, serial: str, state: dict[str, object]):
        with self._webui_touch_lock:
            self._active_webui_touches[serial] = state

    def _pop_active_webui_touch_state(self, serial: str):
        with self._webui_touch_lock:
            return self._active_webui_touches.pop(serial, None)

    def _update_active_webui_touch_state(self, serial: str, **updates):
        with self._webui_touch_lock:
            state = self._active_webui_touches.get(serial)
            if state is None:
                return None
            state.update(updates)
            return dict(state)

    def _execute_touch_start(self, x, y, screen_width=None, screen_height=None):
        context, status, error = self._resolve_live_touch_context(x, y, screen_width, screen_height)
        if error is not None:
            return {'success': False, 'message': error}, status

        helper, serial, mapped_x, mapped_y, target_width, target_height = context
        if self._get_active_webui_touch_state(serial) is not None:
            return {'success': False, 'message': 'A touch gesture is already active'}, 409

        try:
            session = self._get_or_create_scrcpy_control_session(helper)
            logger.info(
                'Starting realtime web UI touch at (%s, %s) mapped to (%s, %s) on %sx%s via scrcpy control',
                x,
                y,
                mapped_x,
                mapped_y,
                target_width,
                target_height,
            )
            self._perform_scrcpy_control_touch_start(session, mapped_x, mapped_y, target_width, target_height)
            state = {
                'transport': 'scrcpy',
                'session': session,
                'x': mapped_x,
                'y': mapped_y,
                'screen_width': target_width,
                'screen_height': target_height,
            }
        except Exception as control_error:
            logger.warning('Realtime web UI touch start failed via scrcpy control, falling back to helper input: %s', control_error)
            try:
                input_adapter = self._get_helper_touch_input(helper)
                input_adapter.touch_event(EventAction.DOWN, int(mapped_x), int(mapped_y))
                state = {
                    'transport': 'helper',
                    'x': mapped_x,
                    'y': mapped_y,
                    'screen_width': target_width,
                    'screen_height': target_height,
                }
            except Exception as helper_error:
                logger.error('Realtime web UI touch start failed via scrcpy control (%s) and helper fallback (%s)', control_error, helper_error)
                return {'success': False, 'message': f'touch start failed: {control_error}; helper fallback failed: {helper_error}'}, 503

        self._set_active_webui_touch_state(serial, state)
        return {'success': True, 'message': f'Touch started at ({x}, {y})'}, 200

    def _execute_touch_move(self, x, y, screen_width=None, screen_height=None):
        context, status, error = self._resolve_live_touch_context(x, y, screen_width, screen_height)
        if error is not None:
            return {'success': False, 'message': error}, status

        helper, serial, mapped_x, mapped_y, target_width, target_height = context
        state = self._get_active_webui_touch_state(serial)
        if state is None:
            return {'success': False, 'message': 'No active touch gesture'}, 409

        transport = state.get('transport')
        try:
            if transport == 'scrcpy':
                session = state.get('session')
                if not isinstance(session, ScrcpyControlSession):
                    raise RuntimeError('scrcpy touch session is unavailable')
                self._perform_scrcpy_control_touch_move(session, mapped_x, mapped_y, target_width, target_height)
            elif transport == 'helper':
                input_adapter = self._get_helper_touch_input(helper)
                input_adapter.touch_event(EventAction.MOVE, int(mapped_x), int(mapped_y))
            else:
                raise RuntimeError(f'unsupported touch transport: {transport}')
        except Exception as error:
            logger.error('Realtime web UI touch move failed: %s', error)
            self._pop_active_webui_touch_state(serial)
            return {'success': False, 'message': f'touch move failed: {error}'}, 503

        self._update_active_webui_touch_state(
            serial,
            x=mapped_x,
            y=mapped_y,
            screen_width=target_width,
            screen_height=target_height,
        )
        return {'success': True, 'message': f'Touch moved to ({x}, {y})'}, 200

    def _execute_touch_end(self, x, y, screen_width=None, screen_height=None):
        context, status, error = self._resolve_live_touch_context(x, y, screen_width, screen_height)
        if error is not None:
            return {'success': False, 'message': error}, status

        helper, serial, mapped_x, mapped_y, target_width, target_height = context
        state = self._pop_active_webui_touch_state(serial)
        if state is None:
            return {'success': False, 'message': 'No active touch gesture'}, 409

        transport = state.get('transport')
        try:
            if transport == 'scrcpy':
                session = state.get('session')
                if not isinstance(session, ScrcpyControlSession):
                    raise RuntimeError('scrcpy touch session is unavailable')
                self._perform_scrcpy_control_touch_end(session, mapped_x, mapped_y, target_width, target_height)
            elif transport == 'helper':
                input_adapter = self._get_helper_touch_input(helper)
                input_adapter.touch_event(EventAction.UP, int(mapped_x), int(mapped_y))
            else:
                raise RuntimeError(f'unsupported touch transport: {transport}')
        except Exception as error:
            logger.error('Realtime web UI touch end failed: %s', error)
            return {'success': False, 'message': f'touch end failed: {error}'}, 503

        return {'success': True, 'message': f'Touch ended at ({x}, {y})'}, 200

    def _cancel_active_webui_touch_for_current_helper(self):
        helper, helper_error = self._get_helper_with_reconnect()
        if helper is None:
            if helper_error:
                logger.warning('Failed to resolve helper while cancelling active web UI touch: %s', helper_error)
            return

        serial = helper.control.adb.serial or 'default'
        state = self._pop_active_webui_touch_state(serial)
        if state is None:
            return

        try:
            if state.get('transport') == 'scrcpy':
                session = state.get('session')
                if isinstance(session, ScrcpyControlSession):
                    session.cancel_active_touch()
            elif state.get('transport') == 'helper':
                input_adapter = self._get_helper_touch_input(helper)
                input_adapter.touch_event(EventAction.UP, int(state.get('x', 0)), int(state.get('y', 0)))
        except Exception as error:
            logger.warning('Failed to cancel active web UI touch: %s', error)

    def _execute_click(self, x, y, screen_width=None, screen_height=None):
        helper, helper_error = self._get_helper_with_reconnect()
        if helper is None:
            return {'success': False, 'message': helper_error or 'No device connected'}, 503

        size_result, size_status, size_error = self._resolve_webui_control_sizes(helper, screen_width, screen_height)
        if size_error is not None:
            return {'success': False, 'message': size_error}, size_status

        source_width, source_height, target_width, target_height = size_result
        mapped_point, point_status, point_error = self._map_webui_point_to_device(
            x,
            y,
            source_width,
            source_height,
            target_width,
            target_height,
        )
        if point_error is not None:
            return {'success': False, 'message': point_error}, point_status

        mapped_x, mapped_y = mapped_point

        try:
            session = self._get_or_create_scrcpy_control_session(helper)
            logger.info(
                'Simulating web UI click at (%s, %s) mapped to (%s, %s) on %sx%s via scrcpy control',
                x,
                y,
                mapped_x,
                mapped_y,
                target_width,
                target_height,
            )
            self._perform_scrcpy_control_tap(session, mapped_x, mapped_y, target_width, target_height)
        except Exception as control_error:
            logger.warning('Web UI scrcpy click failed, falling back to helper input: %s', control_error)
            try:
                logger.info(
                    'Simulating web UI click at (%s, %s) mapped to (%s, %s) on %sx%s via helper fallback',
                    x,
                    y,
                    mapped_x,
                    mapped_y,
                    target_width,
                    target_height,
                )
                self._perform_helper_control_tap(helper, mapped_x, mapped_y)
            except Exception as helper_error:
                logger.error('Web UI click failed via scrcpy control (%s) and helper fallback (%s)', control_error, helper_error)
                return {'success': False, 'message': f'click failed: {control_error}; helper fallback failed: {helper_error}'}, 503

        return {'success': True, 'message': f'Clicked at ({x}, {y})'}, 200

    def _execute_swipe(self, x1, y1, x2, y2, duration=300, screen_width=None, screen_height=None):
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return {'success': False, 'message': 'Invalid swipe parameters'}, 400

        if duration < 50 or duration > 5000:
            return {'success': False, 'message': 'Duration must be between 50 and 5000 ms'}, 400

        helper, helper_error = self._get_helper_with_reconnect()
        if helper is None:
            return {'success': False, 'message': helper_error or 'No device connected'}, 503

        size_result, size_status, size_error = self._resolve_webui_control_sizes(helper, screen_width, screen_height)
        if size_error is not None:
            return {'success': False, 'message': size_error}, size_status

        source_width, source_height, target_width, target_height = size_result
        start_point, start_status, start_error = self._map_webui_point_to_device(
            x1,
            y1,
            source_width,
            source_height,
            target_width,
            target_height,
        )
        if start_error is not None:
            return {'success': False, 'message': start_error}, start_status

        end_point, end_status, end_error = self._map_webui_point_to_device(
            x2,
            y2,
            source_width,
            source_height,
            target_width,
            target_height,
        )
        if end_error is not None:
            return {'success': False, 'message': end_error}, end_status

        mapped_x1, mapped_y1 = start_point
        mapped_x2, mapped_y2 = end_point

        try:
            session = self._get_or_create_scrcpy_control_session(helper)
            logger.info(
                'Simulating web UI swipe (%s, %s) -> (%s, %s) mapped to (%s, %s) -> (%s, %s) in %sms on %sx%s via scrcpy control',
                x1,
                y1,
                x2,
                y2,
                mapped_x1,
                mapped_y1,
                mapped_x2,
                mapped_y2,
                duration,
                target_width,
                target_height,
            )
            self._perform_scrcpy_control_swipe(session, mapped_x1, mapped_y1, mapped_x2, mapped_y2, duration, target_width, target_height)
        except Exception as control_error:
            logger.warning('Web UI scrcpy swipe failed, falling back to helper input: %s', control_error)
            try:
                logger.info(
                    'Simulating web UI swipe (%s, %s) -> (%s, %s) mapped to (%s, %s) -> (%s, %s) in %sms on %sx%s via helper fallback',
                    x1,
                    y1,
                    x2,
                    y2,
                    mapped_x1,
                    mapped_y1,
                    mapped_x2,
                    mapped_y2,
                    duration,
                    target_width,
                    target_height,
                )
                self._perform_helper_control_swipe(helper, mapped_x1, mapped_y1, mapped_x2, mapped_y2, duration)
            except Exception as helper_error:
                logger.error('Web UI swipe failed via scrcpy control (%s) and helper fallback (%s)', control_error, helper_error)
                return {'success': False, 'message': f'swipe failed: {control_error}; helper fallback failed: {helper_error}'}, 503

        return {
            'success': True,
            'message': f'Swiped ({x1}, {y1}) -> ({x2}, {y2}) in {duration}ms'
        }, 200

    def _handle_control_action(self, payload):
        action = str(payload.get('action') or payload.get('type') or '').strip().lower()
        request_id = payload.get('requestId')

        if action == 'click':
            body, _ = self._execute_click(
                payload.get('x'),
                payload.get('y'),
                payload.get('screenWidth'),
                payload.get('screenHeight')
            )
        elif action == 'swipe':
            body, _ = self._execute_swipe(
                payload.get('x1'),
                payload.get('y1'),
                payload.get('x2'),
                payload.get('y2'),
                payload.get('duration', 300),
                payload.get('screenWidth'),
                payload.get('screenHeight')
            )
        elif action == 'touch_start':
            body, _ = self._execute_touch_start(
                payload.get('x'),
                payload.get('y'),
                payload.get('screenWidth'),
                payload.get('screenHeight')
            )
        elif action == 'touch_move':
            body, _ = self._execute_touch_move(
                payload.get('x'),
                payload.get('y'),
                payload.get('screenWidth'),
                payload.get('screenHeight')
            )
        elif action == 'touch_end':
            body, _ = self._execute_touch_end(
                payload.get('x'),
                payload.get('y'),
                payload.get('screenWidth'),
                payload.get('screenHeight')
            )
        else:
            body = {
                'success': False,
                'message': f'Unsupported control action: {action or "unknown"}'
            }

        response = {
            'type': 'action_result',
            'action': action or None,
            'requestId': request_id,
        }
        response.update(body)
        return response, 200 if body.get('success') else 400

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
