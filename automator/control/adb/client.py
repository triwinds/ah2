from __future__ import annotations
from typing import Optional

import contextlib
from functools import lru_cache
import socket
import struct
import logging
import subprocess
import time

import numpy as np

from util.socketutil import recvexactly, recvall

from .server import ensure_adb_alive, find_adb_from_android_sdk

logger = logging.getLogger(__name__)


def _get_adb_error_bytes(error) -> bytes:
    if not error.args:
        return b''
    detail = error.args[0]
    if isinstance(detail, bytes):
        return detail.lower()
    return str(detail).encode(errors='ignore').lower()


def _is_retryable_transport_error(error) -> bool:
    detail = _get_adb_error_bytes(error)
    return b'not found' in detail or b'offline' in detail


def _is_offline_transport_error(error) -> bool:
    return b'offline' in _get_adb_error_bytes(error)

def _check_okay(sock):
    result = recvexactly(sock, 4)
    if result != b'OKAY':
        raise RuntimeError(_read_hexlen(sock))


def _read_hexlen(sock):
    textlen = int(recvexactly(sock, 4), 16)
    if textlen == 0:
        return b''
    buf = recvexactly(sock, textlen)
    return buf

def _read_binlen_le(sock):
    textlen = struct.unpack('<I', recvexactly(sock, 4))[0]
    if textlen == 0:
        return b''
    buf = recvexactly(sock, textlen)
    return buf

class ADBClientSession:
    def __init__(self, server=None, timeout=None):
        if server is None:
            server = ('127.0.0.1', 5037)
        if server[0] == '127.0.0.1' or server[0] == '::1':
            timeout = 0.5
        sock = socket.create_connection(server, timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        self.sock: socket.socket = sock

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def service(self, cmd: str):
        """make a service request to ADB server, consult ADB sources for available services"""
        cmdbytes = cmd.encode()
        data = b'%04X%b' % (len(cmdbytes), cmdbytes)
        self.sock.send(data)
        _check_okay(self.sock)
        return self

    def read_response(self):
        """read a chunk of length indicated by 4 hex digits"""
        return _read_hexlen(self.sock)

    def detach(self):
        sock = self.sock
        self.sock = None
        return sock


class ADBDevice:
    def __init__(self, serial: Optional[str] = None, server: Optional[ADBServer] = None):
        self.serial = serial
        self.server = server or ADBServer.DEFAULT

    def __repr__(self):
        return f'{self.__class__.__name__}({self.server!r}, serial={self.serial!r})'

    def create_session(self):
        if self.serial is not None:
            session = self._create_session_retry()
        else:
            session = self.server.create_session()
            session.service('host:transport-any')
        return session
    
    def _create_session_retry(self, retry_count=0):
        session = self.server.create_session()
        try:
            session.service('host:transport:' + self.serial)
            return session
        except RuntimeError as e:
            session.close()
            if retry_count < 3 and _is_retryable_transport_error(e):
                reconnect_serial = None
                if ':' in self.serial and self.serial.split(':')[-1].isdigit():
                    reconnect_serial = self.serial
                else:
                    reconnect_serial = _serial_to_loopback_endpoint(self.serial)
                if reconnect_serial is not None:
                    logger.info('adb connect %s', reconnect_serial)
                    self.server.paranoid_connect(reconnect_serial)
                    if _is_offline_transport_error(e):
                        time.sleep(min(0.5 * (retry_count + 1), 1.5))
                    self.serial = reconnect_serial
                    return self._create_session_retry(retry_count + 1)
            raise

    def service(self, cmd: str):
        """make a service request to adbd, consult ADB sources for available services"""
        session = self.create_session()
        session.service(cmd)
        return session

    def exec_stream(self, cmd=''):
        """run command in device, with stdout/stdin attached to the socket returned"""
        return self.service('exec:' + cmd).detach()

    def exec(self, cmd):
        """run command in device, returns stdout content after the command exits"""
        if len(cmd) == 0:
            raise ValueError('no command specified for blocking exec')
        sock = self.exec_stream(cmd)
        data = recvall(sock)
        sock.close()
        return data

    def shell_stream(self, cmd=''):
        """run command in device, with pty attached to the socket returned"""
        return self.service('shell:' + cmd).detach()

    def shell(self, cmd):
        """run command in device, returns pty output after the command exits"""
        if len(cmd) == 0:
            raise ValueError('no command specified for blocking shell')
        sock = self.shell_stream(cmd)
        data = recvall(sock)
        sock.close()
        return data

    def forward(self, local: str, remote: str, norebind: bool = True) -> str | None:
        if not self.serial:
            raise ValueError('forward() requires a concrete device serial')

        protocol_errors = []
        forward_commands = [f'host-serial:{self.serial}:forward:{local};{remote}']
        if norebind:
            forward_commands.insert(0, f'host-serial:{self.serial}:forward:norebind:{local};{remote}')

        for command in forward_commands:
            session = self.server.create_session()
            try:
                session.service(command)
                if local == 'tcp:0':
                    response = session.read_response().decode(errors='ignore').strip()
                    if not response:
                        raise RuntimeError('adb forward returned an empty local port')
                    return response
                return None
            except Exception as error:
                protocol_errors.append(error)
            finally:
                session.close()

        return self._forward_via_subprocess(local, remote, norebind, protocol_errors)

    def remove_forward(self, local: str) -> None:
        if not self.serial:
            raise ValueError('remove_forward() requires a concrete device serial')

        protocol_error = None
        session = self.server.create_session()
        try:
            session.service(f'host-serial:{self.serial}:killforward:{local}')
            return
        except Exception as error:
            protocol_error = error
        finally:
            session.close()

        self._remove_forward_via_subprocess(local, protocol_error)

    def _forward_via_subprocess(self, local: str, remote: str, norebind: bool, protocol_errors: list[Exception]) -> str | None:
        command_suffix = ['forward']
        if norebind:
            command_suffix.append('--no-rebind')
        command_suffix.extend([local, remote])

        last_error = None
        for adbbin in self._iter_adb_binaries():
            try:
                completed = subprocess.run(
                    [adbbin, *self._adb_subprocess_args(), *command_suffix],
                    capture_output=True,
                    check=True,
                    text=True,
                )
                if local == 'tcp:0':
                    response = completed.stdout.strip()
                    if not response:
                        raise RuntimeError('adb forward returned an empty local port')
                    return response
                return None
            except Exception as error:
                last_error = error

        details = '; '.join(str(error) for error in [*protocol_errors, last_error] if error is not None)
        raise RuntimeError(f'adb forward failed for {self.serial}: {details}')

    def _remove_forward_via_subprocess(self, local: str, protocol_error: Optional[Exception]) -> None:
        last_error = None
        for adbbin in self._iter_adb_binaries():
            try:
                subprocess.run(
                    [adbbin, *self._adb_subprocess_args(), 'forward', '--remove', local],
                    capture_output=True,
                    check=True,
                    text=True,
                )
                return
            except Exception as error:
                last_error = error

        details = '; '.join(str(error) for error in [protocol_error, last_error] if error is not None)
        raise RuntimeError(f'adb remove-forward failed for {self.serial}: {details}')

    def _adb_subprocess_args(self) -> list[str]:
        if not self.serial:
            raise ValueError('_adb_subprocess_args() requires a concrete device serial')

        args: list[str] = []
        host, port = self.server.address
        if host not in ('127.0.0.1', 'localhost'):
            args.extend(['-H', host])
        if port != 5037:
            args.extend(['-P', str(port)])
        args.extend(['-s', self.serial])
        return args

    def _iter_adb_binaries(self) -> list[str]:
        import app

        candidates: list[str] = []
        configured = app.config.device.adb_binary
        if configured:
            candidates.append(str(configured))
        else:
            candidates.append('adb')
            with contextlib.suppress(FileNotFoundError):
                bundled_adb = app.get_vendor_path('platform-tools') / 'adb'
                candidates.append(str(bundled_adb))
            sdk_adb = find_adb_from_android_sdk()
            if sdk_adb is not None:
                candidates.append(str(sdk_adb))

        deduped: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def push(self, target_path: str, buffer: ReadableBuffer, mode=0o100755, mtime: int = None):
        """push data to device"""
        # Python has no type hint for buffer protocol, why?
        sock = self.service('sync:').detach()
        request = b'%s,%d' % (target_path.encode(), mode)
        sock.send(b'SEND' + struct.pack("<I", len(request)) + request)
        sendbuf = np.empty(65536+8, dtype=np.uint8)
        sendbuf[0:4] = np.frombuffer(b'DATA', dtype=np.uint8)
        input_arr = np.frombuffer(buffer, dtype=np.uint8)
        for arr in np.array_split(input_arr, np.arange(65536, input_arr.size, 65536)):
            sendbuf[4:8].view('<I')[0] = len(arr)
            sendbuf[8:8+len(arr)] = arr
            sock.sendall(sendbuf[0:8+len(arr)])
        if mtime is None:
            mtime = int(time.time())
        sock.sendall(b'DONE' + struct.pack("<I", mtime))
        _check_okay(sock)
        _read_binlen_le(sock)
        sock.close()

class ADBAnyDevice(ADBDevice):
    def __init__(self, server: Optional[ADBServer] = None):
        super().__init__(None, server)

class ADBAnyUSBDevice(ADBDevice):
    def __init__(self, server: Optional[ADBServer] = None):
        super().__init__('<any usb>', server)
    
    def create_session(self):
        return self.server.create_session().service('host:transport-usb')

class ADBAnyEmulatorDevice(ADBDevice):
    def __init__(self, server: Optional[ADBServer] = None):
        super().__init__('<any emulator>', server)
    
    def create_session(self):
        return self.server.create_session().service('host:transport-local')

class ADBServer:
    DEFAULT: ADBServer

    def __init__(self, address=('127.0.0.1', 5037)):
        self.address = address

    def __repr__(self):
        address = f'{self.address[0]}:{self.address[1]}'
        return f'{self.__class__.__name__}({address!r})'

    def create_session(self):
        ensure_adb_alive(self)
        return self._create_session_nocheck()

    def _create_session_nocheck(self):
        return ADBClientSession(server=self.address)

    def service(self, cmd: str, timeout: Optional[float] = None):
        """make a service request to ADB server, consult ADB sources for available services"""
        session = self.create_session()
        session.sock.settimeout(timeout)
        session.service(cmd)
        return session

    def devices(self, show_offline=False):
        """returns list of devices that the adb server knows"""
        resp = self.service('host:devices').read_response().decode()
        devices = [tuple(line.rsplit('\t', 2)) for line in resp.splitlines()]
        if not show_offline:
            devices = [x for x in devices if x[1] != 'offline']
        return devices

    def connect(self, device, timeout=None):
        resp = self.service('host:connect:%s' % device, timeout=timeout).read_response().decode(errors='ignore')
        logger.debug('adb connect %s: %s', device, resp)
        if 'unable' in resp or 'cannot' in resp:
            raise RuntimeError(resp)

    def disconnect(self, device):
        resp = self.service('host:disconnect:%s' % device).read_response().decode(errors='ignore')
        logger.debug('adb disconnect %s: %s', device, resp)
        if 'unable' in resp or 'cannot' in resp:
            raise RuntimeError(resp)

    def disconnect_all_offline(self):
        with contextlib.suppress(RuntimeError):
            for x in self.devices(show_offline=True):
                if x[1] == 'offline':
                    with contextlib.suppress(RuntimeError):
                        self.disconnect(x[0])

    def paranoid_connect(self, port, timeout=5):
        with contextlib.suppress(RuntimeError):
            self.disconnect(port)
        self.connect(port, timeout=timeout)

    def _check_device(self, device: ADBDevice) -> ADBDevice:
        device.create_session().close()
        return device

    def get_device(self, serial: Optional[str] = None) -> ADBDevice:
        """Connect to a device"""
        return self._check_device(ADBDevice(serial, self))

    def get_usbdevice(self) -> ADBDevice:
        """switch to a USB-connected device"""
        return self._check_device(ADBAnyUSBDevice(self))

    def get_emulator(self) -> ADBDevice:
        """switch to an (SDK) emulator device"""
        return self._check_device(ADBAnyEmulatorDevice(self))


def _serial_to_loopback_endpoint(serial: Optional[str]) -> Optional[str]:
    if serial is None or not serial.startswith('emulator-'):
        return None
    try:
        control_port = int(serial[9:])
    except ValueError:
        return None
    return f'127.0.0.1:{control_port + 1}'

ADBServer.DEFAULT = ADBServer()

@lru_cache(maxsize=2)
def get_adb_server_by_address(server: str) -> ADBServer:
    ip, port = server.split(':', 1)
    port = int(port)
    if ip == '127.0.0.1' and port == 5037:
        return ADBServer.DEFAULT
    return ADBServer((ip, port))

def get_config_adb_server():
    import app
    server: str = app.config.device.adb_server
    return get_adb_server_by_address(server)
