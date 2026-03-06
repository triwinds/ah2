from __future__ import annotations

import contextlib
import logging
from pathlib import Path
import secrets
import socket
import time
from typing import Callable, Optional

import gevent
import gevent.lock

from automator.control.adb.client import ADBDevice

logger = logging.getLogger(__name__)


def _generate_scid() -> str:
    return f'{secrets.randbelow(0x80000000):08x}'


def _find_start_code(data: bytes | bytearray, start: int = 0) -> int:
    limit = len(data) - 3
    index = start
    while index <= limit:
        if data[index] == 0 and data[index + 1] == 0:
            if data[index + 2] == 1:
                return index
            if index + 3 < len(data) and data[index + 2] == 0 and data[index + 3] == 1:
                return index
        index += 1
    return -1


def _get_start_code_length(nal: bytes) -> int:
    if len(nal) >= 4 and nal[:4] == b'\x00\x00\x00\x01':
        return 4
    if len(nal) >= 3 and nal[:3] == b'\x00\x00\x01':
        return 3
    return 0


def parse_h264_nal_type(nal: bytes) -> Optional[int]:
    start_code_length = _get_start_code_length(nal)
    if start_code_length == 0 or len(nal) <= start_code_length:
        return None
    return nal[start_code_length] & 0x1F


def extract_complete_nals(buffer: bytearray) -> list[bytes]:
    first_start = _find_start_code(buffer)
    if first_start < 0:
        if len(buffer) > ScrcpySession.MAX_BUFFER_BYTES:
            del buffer[:-4]
        return []

    if first_start > 0:
        del buffer[:first_start]

    units: list[bytes] = []
    start = 0
    while True:
        next_start = _find_start_code(buffer, start + 3)
        if next_start < 0:
            if start > 0:
                del buffer[:start]
            return units
        units.append(bytes(buffer[start:next_start]))
        start = next_start


class ScrcpySession:
    SERVER_JAR_LOCAL = Path('vendor/scrcpy/scrcpy-server.jar')
    SERVER_JAR_REMOTE = '/data/local/tmp/scrcpy-server.jar'
    SERVER_VERSION = '3.2'
    VERSION_FILE = Path('vendor/scrcpy/VERSION')
    START_TIMEOUT_SECONDS = 5.0
    CONNECT_RETRY_INTERVAL_SECONDS = 0.1
    CONNECT_PROBE_TIMEOUT_SECONDS = 0.5
    IDLE_TIMEOUT_SECONDS = 10.0
    MAX_BUFFER_BYTES = 4 * 1024 * 1024
    MAX_BOOTSTRAP_BYTES = 8 * 1024 * 1024

    def __init__(self, adb: ADBDevice, on_stopped: Optional[Callable[[ScrcpySession], None]] = None):
        self.adb = adb
        self.serial = adb.serial or 'default'
        self.scid = _generate_scid()
        self.local_port: Optional[int] = None

        self.server_stream: Optional[socket.socket] = None
        self.server_greenlet = None
        self.video_sock: Optional[socket.socket] = None
        self.video_greenlet = None
        self.idle_timer_greenlet = None

        self.running = False
        self.healthy = False
        self.last_active_at = time.monotonic()
        self.start_error: Optional[str] = None
        self.exit_reason: Optional[str] = None

        self.pending_clients: set[object] = set()
        self.ready_clients: set[object] = set()

        self.cached_sps: Optional[bytes] = None
        self.cached_pps: Optional[bytes] = None
        self._bootstrap_buffer: Optional[bytearray] = None
        self._annexb_buffer = bytearray()
        self._lock = gevent.lock.RLock()
        self._stopping = False
        self._on_stopped = on_stopped
        self._on_stopped_called = False

    def start(self) -> None:
        with self._lock:
            if self.running and self.healthy:
                return
            if self._stopping:
                raise RuntimeError('scrcpy session is stopping')
            self.start_error = None
            self.exit_reason = None
            self._annexb_buffer.clear()
            self.cached_sps = None
            self.cached_pps = None
            self._bootstrap_buffer = None
            self.scid = _generate_scid()
            self._cancel_idle_timer_locked()

        try:
            jar_bytes = self._load_server_jar()
            self.adb.push(self.SERVER_JAR_REMOTE, jar_bytes)
            local_port = self.adb.forward('tcp:0', f'localabstract:scrcpy_{self.scid}', norebind=False)
            if not local_port:
                raise RuntimeError('adb forward did not return a local port')
            self.local_port = int(local_port)
            self.server_stream = self.adb.exec_stream(self._build_server_command())
            self.server_greenlet = gevent.spawn(self._server_loop)
            self.video_sock = self._connect_video_socket(self.local_port)
            self.video_greenlet = gevent.spawn(self._video_loop)
            with self._lock:
                self.running = True
                self.healthy = True
                self.last_active_at = time.monotonic()
            logger.info('Started scrcpy session for %s on tcp:%s', self.serial, self.local_port)
        except Exception as error:
            self.start_error = str(error)
            self.stop(reason=f'start failed: {error}')
            raise

    def subscribe(self, ws) -> None:
        with self._lock:
            if not self.running or not self.healthy:
                raise RuntimeError(self.start_error or self.exit_reason or 'scrcpy session is not healthy')
            self._cancel_idle_timer_locked()
            self.last_active_at = time.monotonic()
            bootstrap = bytes(self._bootstrap_buffer) if self._bootstrap_buffer else None
            if bootstrap is None:
                self.pending_clients.add(ws)
                self.ready_clients.discard(ws)
                return

            try:
                ws.send(bootstrap)
            except Exception:
                self.pending_clients.discard(ws)
                self.ready_clients.discard(ws)
                self._schedule_idle_timer_locked()
                self._close_ws(ws)
                return

            self.ready_clients.add(ws)
            self.pending_clients.discard(ws)

    def unsubscribe(self, ws) -> None:
        with self._lock:
            removed = False
            if ws in self.pending_clients:
                self.pending_clients.discard(ws)
                removed = True
            if ws in self.ready_clients:
                self.ready_clients.discard(ws)
                removed = True
            if removed:
                self.last_active_at = time.monotonic()
            self._schedule_idle_timer_locked()

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

            idle_timer_greenlet = self.idle_timer_greenlet
            self.idle_timer_greenlet = None

            server_greenlet = self.server_greenlet
            self.server_greenlet = None
            video_greenlet = self.video_greenlet
            self.video_greenlet = None

            server_stream = self.server_stream
            self.server_stream = None
            video_sock = self.video_sock
            self.video_sock = None

            local_port = self.local_port
            self.local_port = None

            client_sockets = list(self.pending_clients | self.ready_clients)
            self.pending_clients.clear()
            self.ready_clients.clear()
            self.cached_sps = None
            self.cached_pps = None
            self._bootstrap_buffer = None
            self._annexb_buffer.clear()

        if idle_timer_greenlet is not None and idle_timer_greenlet is not current:
            idle_timer_greenlet.kill(block=False)
        self._close_socket(video_sock)
        self._close_socket(server_stream)
        if video_greenlet is not None and video_greenlet is not current:
            video_greenlet.kill(block=False)
        if server_greenlet is not None and server_greenlet is not current:
            server_greenlet.kill(block=False)
        if local_port is not None:
            with contextlib.suppress(Exception):
                self.adb.remove_forward(f'tcp:{local_port}')
        for ws in client_sockets:
            self._close_ws(ws)
        self._notify_stopped_once()
        with self._lock:
            self._stopping = False

    def _notify_stopped_once(self) -> None:
        if self._on_stopped_called:
            return
        self._on_stopped_called = True
        if self._on_stopped is None:
            return
        try:
            self._on_stopped(self)
        except Exception as error:
            logger.warning('Failed to run scrcpy session stop hook for %s: %s', self.serial, error)

    def _cancel_idle_timer_locked(self) -> None:
        if self.idle_timer_greenlet is not None:
            self.idle_timer_greenlet.kill(block=False)
            self.idle_timer_greenlet = None

    def _schedule_idle_timer_locked(self) -> None:
        if self.pending_clients or self.ready_clients or not self.running:
            return
        if self.idle_timer_greenlet is not None:
            return
        self.idle_timer_greenlet = gevent.spawn_later(self.IDLE_TIMEOUT_SECONDS, self._idle_timeout_stop)

    def _idle_timeout_stop(self) -> None:
        with self._lock:
            self.idle_timer_greenlet = None
            if self.pending_clients or self.ready_clients:
                return
        self.stop(reason='idle timeout')

    def _safe_send(self, ws, data: bytes) -> None:
        try:
            ws.send(data)
        except Exception:
            with self._lock:
                self.ready_clients.discard(ws)
                self.pending_clients.discard(ws)
                self._schedule_idle_timer_locked()
            self._close_ws(ws)

    def _server_loop(self) -> None:
        try:
            while True:
                if self.server_stream is None:
                    return
                chunk = self.server_stream.recv(4096)
                if not chunk:
                    self.stop(reason='scrcpy server exited')
                    return
                message = chunk.decode(errors='ignore').strip()
                if message:
                    logger.info('scrcpy[%s] %s', self.serial, message)
        except Exception as error:
            logger.warning('scrcpy server stream failed for %s: %s', self.serial, error)
            self.stop(reason=f'scrcpy server stream error: {error}')

    def _video_loop(self) -> None:
        try:
            while True:
                if self.video_sock is None:
                    return
                chunk = self.video_sock.recv(65536)
                if not chunk:
                    self.stop(reason='scrcpy video stream closed')
                    return
                self._annexb_buffer.extend(chunk)
                for nal in extract_complete_nals(self._annexb_buffer):
                    self._handle_nal(nal)
        except Exception as error:
            logger.warning('scrcpy video stream failed for %s: %s', self.serial, error)
            self.stop(reason=f'scrcpy video stream error: {error}')

    def _handle_nal(self, nal: bytes) -> None:
        nal_type = parse_h264_nal_type(nal)
        if nal_type is None:
            return

        if nal_type == 7:
            sps_changed = self.cached_sps is not None and self.cached_sps != nal
            self.cached_sps = nal
            if sps_changed:
                self.cached_pps = None
                self._bootstrap_buffer = None
            with self._lock:
                if sps_changed and self.ready_clients:
                    self.pending_clients |= self.ready_clients
                    self.ready_clients.clear()
                ready_clients = list(self.ready_clients)
            for ws in ready_clients:
                self._safe_send(ws, nal)
            return

        if nal_type == 8:
            self.cached_pps = nal
            with self._lock:
                ready_clients = list(self.ready_clients)
            for ws in ready_clients:
                self._safe_send(ws, nal)
            return

        if nal_type == 5:
            bootstrap = bytearray()
            if self.cached_sps:
                bootstrap.extend(self.cached_sps)
            if self.cached_pps:
                bootstrap.extend(self.cached_pps)
            bootstrap.extend(nal)
            self._bootstrap_buffer = bytearray(bootstrap)

            with self._lock:
                pending_clients = list(self.pending_clients)
                ready_clients = list(self.ready_clients)

            newly_ready = set()
            payload = bytes(bootstrap)
            for ws in pending_clients:
                self._safe_send(ws, payload)
                with self._lock:
                    if ws in self.pending_clients:
                        self.pending_clients.discard(ws)
                        newly_ready.add(ws)

            for ws in ready_clients:
                self._safe_send(ws, nal)

            if newly_ready:
                with self._lock:
                    self.ready_clients |= newly_ready
                    self.last_active_at = time.monotonic()
            return

        with self._lock:
            ready_clients = list(self.ready_clients)
        if nal_type == 1 and self._bootstrap_buffer is not None:
            if len(self._bootstrap_buffer) + len(nal) <= self.MAX_BOOTSTRAP_BYTES:
                self._bootstrap_buffer.extend(nal)
            else:
                self._bootstrap_buffer = None
        for ws in ready_clients:
            self._safe_send(ws, nal)

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
            'video=true '
            'control=false '
            'video_codec_options=i-frame-interval=2 '
            'raw_stream=true'
        )

    def _connect_video_socket(self, local_port: int) -> socket.socket:
        deadline = time.monotonic() + self.START_TIMEOUT_SECONDS
        last_error = None
        peek_flags = getattr(socket, 'MSG_PEEK', 0)
        while time.monotonic() < deadline:
            sock = None
            try:
                sock = socket.create_connection(('127.0.0.1', local_port), timeout=self.CONNECT_RETRY_INTERVAL_SECONDS)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.CONNECT_PROBE_TIMEOUT_SECONDS)
                probe = sock.recv(1, peek_flags)
                if probe == b'':
                    last_error = ConnectionError('scrcpy video socket closed before stream became ready')
                    sock.close()
                    gevent.sleep(self.CONNECT_RETRY_INTERVAL_SECONDS)
                    continue
                sock.settimeout(None)
                return sock
            except TimeoutError:
                if sock is not None:
                    sock.settimeout(None)
                    return sock
            except OSError as error:
                last_error = error
                if sock is not None:
                    self._close_socket(sock)
                gevent.sleep(self.CONNECT_RETRY_INTERVAL_SECONDS)
        raise TimeoutError(f'timed out waiting for scrcpy video socket on tcp:{local_port}: {last_error}')

    def _close_socket(self, sock: Optional[socket.socket]) -> None:
        if sock is None:
            return
        with contextlib.suppress(Exception):
            sock.close()

    def _close_ws(self, ws) -> None:
        with contextlib.suppress(Exception):
            ws.close()
