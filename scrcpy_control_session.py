from __future__ import annotations

import contextlib
import logging
from pathlib import Path
import secrets
import socket
import struct
import time
from typing import Callable, Optional, Sequence

import gevent
import gevent.lock

from automator.control.adb.client import ADBDevice

logger = logging.getLogger(__name__)


def _generate_scid() -> str:
    return f'{secrets.randbelow(0x80000000):08x}'


def _float_to_u16fp(value: float) -> int:
    clamped = max(0.0, min(1.0, float(value)))
    return int(round(clamped * 0xFFFF))


def _is_expected_socket_close_error(error: Exception) -> bool:
    errno = getattr(error, 'errno', None)
    if errno == 9:
        return True

    message = str(error).lower()
    return 'file descriptor was closed in another greenlet' in message or 'bad file descriptor' in message


class ScrcpyControlSession:
    SERVER_JAR_LOCAL = Path('vendor/scrcpy/scrcpy-server.jar')
    SERVER_JAR_REMOTE = '/data/local/tmp/scrcpy-server.jar'
    SERVER_VERSION = '3.2'
    VERSION_FILE = Path('vendor/scrcpy/VERSION')
    START_TIMEOUT_SECONDS = 5.0
    CONNECT_RETRY_INTERVAL_SECONDS = 0.1
    SERVER_READY_DELAY_SECONDS = 0.25
    DEFAULT_FRAME_INTERVAL_SECONDS = 1 / 120

    TYPE_INJECT_TOUCH_EVENT = 2
    ACTION_DOWN = 0
    ACTION_UP = 1
    ACTION_MOVE = 2
    POINTER_ID_GENERIC_FINGER = (1 << 64) - 2

    def __init__(self, adb: ADBDevice, on_stopped: Optional[Callable[[ScrcpyControlSession], None]] = None):
        self.adb = adb
        self.serial = adb.serial or 'default'
        self.scid = _generate_scid()
        self.local_port: Optional[int] = None

        self.server_stream: Optional[socket.socket] = None
        self.server_greenlet = None
        self.control_sock: Optional[socket.socket] = None

        self.running = False
        self.healthy = False
        self.last_active_at = time.monotonic()
        self.start_error: Optional[str] = None
        self.exit_reason: Optional[str] = None
        self._touch_active = False
        self._touch_screen_size: Optional[tuple[int, int]] = None
        self._touch_last_position: Optional[tuple[int, int]] = None

        self._lock = gevent.lock.RLock()
        self._action_lock = gevent.lock.Semaphore(1)
        self._stopping = False
        self._on_stopped = on_stopped
        self._on_stopped_called = False

    def start(self) -> None:
        with self._lock:
            if self.running and self.healthy:
                return
            if self._stopping:
                raise RuntimeError('scrcpy control session is stopping')
            self.start_error = None
            self.exit_reason = None
            self.scid = _generate_scid()

        try:
            jar_bytes = self._load_server_jar()
            self.adb.push(self.SERVER_JAR_REMOTE, jar_bytes)
            local_port = self.adb.forward('tcp:0', f'localabstract:scrcpy_{self.scid}', norebind=False)
            if not local_port:
                raise RuntimeError('adb forward did not return a local port')
            self.local_port = int(local_port)
            self.server_stream = self.adb.exec_stream(self._build_server_command())
            time.sleep(self.SERVER_READY_DELAY_SECONDS)
            self.server_greenlet = gevent.spawn(self._server_loop)
            self.control_sock = self._connect_control_socket(self.local_port)
            with self._lock:
                self.running = True
                self.healthy = True
                self.last_active_at = time.monotonic()
            logger.info('Started scrcpy control session for %s on tcp:%s', self.serial, self.local_port)
        except Exception as error:
            self.start_error = str(error)
            self.stop(reason=f'start failed: {error}')
            raise

    def touch_tap(self, x: int, y: int, screen_width: int, screen_height: int, hold_time: float = 0.0) -> None:
        try:
            with self._action_lock:
                if not self.running or not self.healthy or self.control_sock is None:
                    raise RuntimeError(self.start_error or self.exit_reason or 'scrcpy control session is not healthy')
                if self._touch_active:
                    raise RuntimeError('a realtime touch is already active')
                self.last_active_at = time.monotonic()
                self._send_touch_event(self.ACTION_DOWN, x, y, screen_width, screen_height, pressure=1.0)
                if hold_time > 0:
                    gevent.sleep(hold_time)
                self._send_touch_event(self.ACTION_UP, x, y, screen_width, screen_height, pressure=0.0)
                self.last_active_at = time.monotonic()
        except Exception as error:
            if self.running:
                self.stop(reason=f'scrcpy control tap failed: {error}')
            raise

    def touch_swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int, screen_width: int, screen_height: int) -> None:
        duration_seconds = max(0.05, min(5.0, int(duration_ms) / 1000.0))

        try:
            with self._action_lock:
                if not self.running or not self.healthy or self.control_sock is None:
                    raise RuntimeError(self.start_error or self.exit_reason or 'scrcpy control session is not healthy')
                if self._touch_active:
                    raise RuntimeError('a realtime touch is already active')
                self.last_active_at = time.monotonic()
                self._send_touch_event(self.ACTION_DOWN, x0, y0, screen_width, screen_height, pressure=1.0)

                start_time = time.perf_counter()
                end_time = start_time + duration_seconds
                while True:
                    tick_started_at = time.perf_counter()
                    if tick_started_at >= end_time:
                        break

                    progress = (tick_started_at - start_time) / duration_seconds
                    current_x = int(round(x0 + (x1 - x0) * progress))
                    current_y = int(round(y0 + (y1 - y0) * progress))
                    self._send_touch_event(self.ACTION_MOVE, current_x, current_y, screen_width, screen_height, pressure=1.0)

                    tick_elapsed = time.perf_counter() - tick_started_at
                    if tick_elapsed < self.DEFAULT_FRAME_INTERVAL_SECONDS:
                        gevent.sleep(self.DEFAULT_FRAME_INTERVAL_SECONDS - tick_elapsed)

                self._send_touch_event(self.ACTION_MOVE, x1, y1, screen_width, screen_height, pressure=1.0)
                self._send_touch_event(self.ACTION_UP, x1, y1, screen_width, screen_height, pressure=0.0)
                self.last_active_at = time.monotonic()
        except Exception as error:
            if self.running:
                self.stop(reason=f'scrcpy control swipe failed: {error}')
            raise

    def touch_down(self, x: int, y: int, screen_width: int, screen_height: int) -> None:
        try:
            with self._action_lock:
                if not self.running or not self.healthy or self.control_sock is None:
                    raise RuntimeError(self.start_error or self.exit_reason or 'scrcpy control session is not healthy')
                if self._touch_active:
                    raise RuntimeError('a realtime touch is already active')

                self.last_active_at = time.monotonic()
                self._send_touch_event(self.ACTION_DOWN, x, y, screen_width, screen_height, pressure=1.0)
                self._touch_active = True
                self._touch_screen_size = (int(screen_width), int(screen_height))
                self._touch_last_position = (int(x), int(y))
                self.last_active_at = time.monotonic()
        except Exception as error:
            if self.running:
                self.stop(reason=f'scrcpy control touch down failed: {error}')
            raise

    def touch_move(self, x: int, y: int, screen_width: int, screen_height: int) -> None:
        try:
            with self._action_lock:
                if not self.running or not self.healthy or self.control_sock is None:
                    raise RuntimeError(self.start_error or self.exit_reason or 'scrcpy control session is not healthy')
                if not self._touch_active:
                    raise RuntimeError('no realtime touch is active')

                active_screen_width, active_screen_height = self._touch_screen_size or (int(screen_width), int(screen_height))
                self.last_active_at = time.monotonic()
                self._send_touch_event(self.ACTION_MOVE, x, y, active_screen_width, active_screen_height, pressure=1.0)
                self._touch_last_position = (int(x), int(y))
                self.last_active_at = time.monotonic()
        except Exception as error:
            if self.running:
                self.stop(reason=f'scrcpy control touch move failed: {error}')
            raise

    def touch_up(self, x: int, y: int, screen_width: int, screen_height: int) -> None:
        try:
            with self._action_lock:
                if not self.running or not self.healthy or self.control_sock is None:
                    raise RuntimeError(self.start_error or self.exit_reason or 'scrcpy control session is not healthy')
                if not self._touch_active:
                    raise RuntimeError('no realtime touch is active')

                active_screen_width, active_screen_height = self._touch_screen_size or (int(screen_width), int(screen_height))
                self.last_active_at = time.monotonic()
                self._send_touch_event(self.ACTION_UP, x, y, active_screen_width, active_screen_height, pressure=0.0)
                self._touch_active = False
                self._touch_screen_size = None
                self._touch_last_position = None
                self.last_active_at = time.monotonic()
        except Exception as error:
            if self.running:
                self.stop(reason=f'scrcpy control touch up failed: {error}')
            raise

    def touch_path(self, points: Sequence[dict[str, int]], screen_width: int, screen_height: int) -> None:
        if len(points) < 2:
            raise ValueError('touch path must contain at least 2 points')

        try:
            with self._action_lock:
                if not self.running or not self.healthy or self.control_sock is None:
                    raise RuntimeError(self.start_error or self.exit_reason or 'scrcpy control session is not healthy')
                if self._touch_active:
                    raise RuntimeError('a realtime touch is already active')

                normalized_points = [
                    {
                        'x': int(point['x']),
                        'y': int(point['y']),
                        't': max(0, int(point.get('t', 0))),
                    }
                    for point in points
                ]

                first_point = normalized_points[0]
                last_point = normalized_points[-1]
                self.last_active_at = time.monotonic()
                self._send_touch_event(self.ACTION_DOWN, first_point['x'], first_point['y'], screen_width, screen_height, pressure=1.0)

                path_started_at = time.perf_counter()
                last_sent_x = first_point['x']
                last_sent_y = first_point['y']

                for point in normalized_points[1:]:
                    target_at = path_started_at + (point['t'] / 1000.0)
                    remaining = target_at - time.perf_counter()
                    if remaining > 0:
                        gevent.sleep(remaining)

                    current_x = point['x']
                    current_y = point['y']
                    if current_x == last_sent_x and current_y == last_sent_y:
                        continue

                    self._send_touch_event(self.ACTION_MOVE, current_x, current_y, screen_width, screen_height, pressure=1.0)
                    last_sent_x = current_x
                    last_sent_y = current_y

                self._send_touch_event(self.ACTION_UP, last_point['x'], last_point['y'], screen_width, screen_height, pressure=0.0)
                self.last_active_at = time.monotonic()
        except Exception as error:
            if self.running:
                self.stop(reason=f'scrcpy control path failed: {error}')
            raise

    def cancel_active_touch(self) -> None:
        with self._action_lock:
            if not self.running or not self.healthy or self.control_sock is None:
                self._touch_active = False
                self._touch_screen_size = None
                self._touch_last_position = None
                return
            if not self._touch_active or self._touch_last_position is None or self._touch_screen_size is None:
                self._touch_active = False
                self._touch_screen_size = None
                self._touch_last_position = None
                return

            last_x, last_y = self._touch_last_position
            screen_width, screen_height = self._touch_screen_size
            self._send_touch_event(self.ACTION_UP, last_x, last_y, screen_width, screen_height, pressure=0.0)
            self._touch_active = False
            self._touch_screen_size = None
            self._touch_last_position = None
            self.last_active_at = time.monotonic()

    def stop(self, reason: Optional[str] = None) -> None:
        current = gevent.getcurrent()
        with self._lock:
            if self._stopping:
                if reason and not self.exit_reason:
                    self.exit_reason = reason
                return
            self._stopping = True
            if reason:
                self.exit_reason = reason
            self.running = False
            self.healthy = False
            self._touch_active = False
            self._touch_screen_size = None
            self._touch_last_position = None

            server_greenlet = self.server_greenlet
            self.server_greenlet = None

            server_stream = self.server_stream
            self.server_stream = None
            control_sock = self.control_sock
            self.control_sock = None

            local_port = self.local_port
            self.local_port = None

        self._close_socket(control_sock)
        self._close_socket(server_stream)
        if server_greenlet is not None and server_greenlet is not current:
            server_greenlet.kill(block=False)
        if local_port is not None:
            with contextlib.suppress(Exception):
                self.adb.remove_forward(f'tcp:{local_port}')
        self._notify_stopped_once()
        with self._lock:
            self._stopping = False


    def _send_touch_event(self, action: int, x: int, y: int, screen_width: int, screen_height: int, pressure: float) -> None:
        if self.control_sock is None:
            raise RuntimeError('scrcpy control socket is not connected')

        packet = struct.pack(
            '>BBQiiHHHII',
            self.TYPE_INJECT_TOUCH_EVENT,
            action,
            self.POINTER_ID_GENERIC_FINGER,
            int(x),
            int(y),
            int(screen_width),
            int(screen_height),
            _float_to_u16fp(pressure),
            0,
            0,
        )
        self.control_sock.sendall(packet)

    def _notify_stopped_once(self) -> None:
        if self._on_stopped_called:
            return
        self._on_stopped_called = True
        if self._on_stopped is None:
            return
        try:
            self._on_stopped(self)
        except Exception as error:
            logger.warning('Failed to run scrcpy control session stop hook for %s: %s', self.serial, error)


    def _server_loop(self) -> None:
        try:
            while True:
                if self.server_stream is None:
                    return
                chunk = self.server_stream.recv(4096)
                if not chunk:
                    self.stop(reason='scrcpy control server exited')
                    return
                message = chunk.decode(errors='ignore').strip()
                if message:
                    logger.info('scrcpy-control[%s] %s', self.serial, message)
        except Exception as error:
            if self._stopping or self.server_stream is None or _is_expected_socket_close_error(error):
                logger.debug('scrcpy control server stream closed for %s: %s', self.serial, error)
                return
            logger.warning('scrcpy control server stream failed for %s: %s', self.serial, error)
            self.stop(reason=f'scrcpy control server stream error: {error}')

    def _load_server_jar(self) -> bytes:
        root = Path(__file__).resolve().parent
        jar_path = root / self.SERVER_JAR_LOCAL
        version_path = root / self.VERSION_FILE
        if not jar_path.is_file():
            raise FileNotFoundError(f'Missing scrcpy server jar: {jar_path}')
        if not version_path.is_file():
            raise FileNotFoundError(f'Missing scrcpy version file: {version_path}')
        version = version_path.read_text(encoding='utf-8').strip()
        if version != self.SERVER_VERSION:
            raise RuntimeError(
                f'scrcpy server version mismatch: expected {self.SERVER_VERSION}, got {version or "<empty>"}'
            )
        return jar_path.read_bytes()

    def _build_server_command(self) -> str:
        return (
            f'CLASSPATH={self.SERVER_JAR_REMOTE} '
            'app_process / com.genymobile.scrcpy.Server '
            f'{self.SERVER_VERSION} '
            f'scid={self.scid} '
            'tunnel_forward=true '
            'cleanup=false '
            'audio=false '
            'video=false '
            'control=true '
            'clipboard_autosync=false '
            'power_on=false '
            'raw_stream=true'
        )

    def _connect_control_socket(self, local_port: int) -> socket.socket:
        deadline = time.monotonic() + self.START_TIMEOUT_SECONDS
        last_error = None
        while time.monotonic() < deadline:
            sock = None
            try:
                sock = socket.create_connection(('127.0.0.1', local_port), timeout=self.CONNECT_RETRY_INTERVAL_SECONDS)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(None)
                return sock
            except OSError as error:
                last_error = error
                if sock is not None:
                    self._close_socket(sock)
                gevent.sleep(self.CONNECT_RETRY_INTERVAL_SECONDS)
        raise TimeoutError(f'timed out waiting for scrcpy control socket on tcp:{local_port}: {last_error}')

    def _close_socket(self, sock: Optional[socket.socket]) -> None:
        if sock is None:
            return
        with contextlib.suppress(Exception):
            sock.close()
