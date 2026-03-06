# 用 scrcpy server 替换实时流方案

## 1. 目标与边界

目标：用 `scrcpy server` 替换当前 `screenrecord --output-format=h264` 实时流，解决 175 秒超时重启、黑屏重连和多路重复拉流的问题。

本次范围：
- 后端改为单设备单路 scrcpy 拉流，再广播给多个 WebSocket 客户端
- 继续输出 H.264 Annex-B，尽量复用现有浏览器端 WebCodecs 解码逻辑
- 保留失败时回退到截图模式的路径

本次**不做**：
- 触控透传
- 音频传输
- H.265 / AV1
- 完整解析 scrcpy 非 `raw_stream` 帧头协议

---

## 2. 现状与问题

现有实时流实现：

```text
adb exec:screenrecord --output-format=h264 --time-limit=175 -
  → exec_stream() 返回原始 socket
    → WebSocket 推送给浏览器
      → WebCodecs VideoDecoder 解码
        → <canvas> 渲染
```

当前痛点：
- `screenrecord` 存在 175 秒时长限制，到期必须重启
- 每次重启都会短暂断流，浏览器端需要重新等 SPS/PPS 和关键帧
- 每个 WebSocket 都独立启动一个 `screenrecord`，资源浪费明显
- 高负载时 `screenrecord` 偶发退出，稳定性一般

---

## 3. scrcpy server 是否适合本项目

适合，原因如下：
- `scrcpy server` 基于 `MediaCodec` 编码，持续推流稳定性通常优于 `screenrecord`
- 支持 `raw_stream=true`，可输出纯 H.264 Annex-B 码流
- 支持通过 ADB forward 暴露本地 TCP 端口，不依赖 `exec_stream()` 直接承载视频流
- 更适合做“单路采集、多路广播”

但有一个**必须正视**的差异：

> 当前前端之所以能工作，是因为每次 WebSocket 新建时，后端都会重新启动 `screenrecord`，浏览器通常会从一个”包含 SPS/PPS 的新流开头”开始解码。
>
> 改成”共享单路 scrcpy 流”后，新加入的客户端很可能从 GOP 中间接入。如果后端只是把 TCP 原始字节块直接广播，新的客户端可能长时间拿不到 SPS/PPS 与 IDR，出现黑屏或一直无法出画。

**补充说明：前端已有的首帧同步能力**

实际上，当前前端 `dashboard.html` 已经实现了完整的 NAL 切分与首帧处理：
- `extractNalUnits()` 按 Annex-B start code 切分 NAL
- `processNalUnit()` 识别 SPS(7)/PPS(8)/IDR(5)/non-IDR(1)
- 收到 IDR 时会自动补 cached SPS+PPS 再喂给 decoder
- 未配置 decoder 时收到 slice 帧会用 cached SPS 做 lazy 重试

这意味着：即便后端不做首帧补偿、只是裸转发，前端也能在等到下一个自然 IDR 后正常出画。后端做首帧同步的真正价值是**加速出画**（避免等待自然关键帧的延迟），而非”没有就完全不能工作”。实现时应据此正确设定优先级。

因此，本方案不是”仅替换拉流命令”这么简单，而是要补上**共享流首帧同步**能力以获得最佳体验。

---

## 4. scrcpy server 协议约定（基于 v3.2）

### 4.1 启动方式

推荐命令：

```bash
# 1. 推送 server
adb push vendor/scrcpy/scrcpy-server.jar /data/local/tmp/scrcpy-server.jar

# 2. 建立 ADB forward
adb forward tcp:27183 localabstract:scrcpy_deadbeef

# 3. 启动 server
adb shell CLASSPATH=/data/local/tmp/scrcpy-server.jar \
  app_process / com.genymobile.scrcpy.Server 3.2 \
  scid=deadbeef tunnel_forward=true cleanup=false \
  audio=false video=true control=false \
  raw_stream=true
```

说明：
- `tunnel_forward=true`：明确使用 forward 隧道模式
- `control=false`：本次不启用控制 socket
- `audio=false`：本次不传音频
- `cleanup=false`：由服务端自己管理生命周期，避免额外副作用
- `raw_stream=true`：输出原始 Annex-B 视频流，不发送额外 metadata / frame header

### 4.2 `raw_stream=true` 下的输出

在 `raw_stream=true` 下，客户端连上视频 socket 后，收到的是**原始 H.264 Annex-B 码流**。

也就是说：
- 没有 dummy byte
- 没有设备名 metadata
- 没有 codec metadata
- 没有每帧长度头 / PTS 头

这和当前前端对 `screenrecord` 输出的处理模型是一致的：都是按 Annex-B start code 切 NAL，再喂给 `VideoDecoder`。

> **⚠ 实现前必须验证：** `raw_stream` 参数在 scrcpy 不同版本间的行为有差异。v3.2 的源码中 `raw_stream` 控制的是是否发送 device metadata 和 codec info 头部，但视频 socket 上在某些配置下仍可能存在 8 字节 packet header（PTS + size）。建议在实现前先对目标 v3.2 版本做一次**抓包验证**，确认连接后实际收到的字节流格式是否为纯 Annex-B。

### 4.3 这里“兼容”的真实含义

这里的兼容仅指：
- **编码格式兼容**：仍然是 H.264 Annex-B
- **前端解码器类型兼容**：仍然可以走现有 WebCodecs H.264 路径

这里**不等于**：
- 共享单路流后，前端完全不需要任何首帧同步保障

真正需要补的是后端 session 层：
- 维护最近的 SPS / PPS
- 识别 IDR（关键帧）
- 新客户端加入时，不能立即裸转发当前字节块；必须等到下一帧可解码的关键帧边界，再把 `SPS + PPS + IDR` 作为该客户端的起始数据

---

## 5. 新架构

### 5.1 现有架构

```text
WebSocket A → screenrecord process A → ADB exec_stream
WebSocket B → screenrecord process B → ADB exec_stream
```

### 5.2 目标架构

```text
ScrcpySession（每个 serial 一个）
  ├── 确认 / 推送 scrcpy-server.jar
  ├── adb forward tcp:<dynamic_port> → localabstract:scrcpy_<scid>
  ├── adb exec_stream(app_process ...) 持续运行 server
  ├── reader greenlet：读取 server 日志/退出状态
  ├── tcp greenlet：读取 H.264 Annex-B 视频流
  │     └── Annex-B framer：切分 NAL
  │           ├── 缓存最近 SPS / PPS
  │           ├── 识别 IDR
  │           └── 广播给 ready clients
  └── client state
        ├── pending clients（等待首个可解码关键帧）
        └── ready clients（已完成同步，可持续收流）
```

优点：
- 单设备只启动一个编码 session，多个浏览器共享
- 不再受 175 秒重启限制
- WebSocket 之间不会重复拉起编码器
- 服务端可统一处理流同步、异常退出和重建

---

## 6. 关键设计决策

### 6.1 后端必须做 NAL 级解析

原方案中，后端只是：

```python
chunk = sock.recv(4096)
ws.send(chunk)
```

这个模型在”每个连接都从流起点开始”的前提下能工作——虽然后端只做裸转发，但前端已经实现了完整的 NAL 切分和 SPS/PPS 缓存逻辑，所以浏览器端本身就具备首帧同步能力。

但在共享单路流里，仅依赖前端的自然 IDR 等待不够用：某些编码器的 IDR 间隔可达 5~10 秒，新客户端会长时间黑屏。因此后端也需要做 NAL 级解析，**主动为新客户端补首帧以加速出画**。

新方案必须在后端做轻量解析：
- 读取 TCP 字节流
- 按 Annex-B start code 切成 NAL 单元
- 识别 NAL type：至少要识别 `SPS(7)`、`PPS(8)`、`IDR(5)`、非 IDR slice(1)

只有这样才能做到：
- 为新客户端补首帧
- 在 server 重启后正确重置缓存
- 让 session 状态机可观测、可恢复

### 6.2 新客户端接入流程

`subscribe(ws)` 的正确语义应为：
- 新客户端先进入 `pending_clients`
- 不立即收到裸流
- 直到读取循环遇到下一帧 IDR：
  - 如果已缓存 SPS / PPS，则先向该客户端发送 `SPS + PPS + IDR`
  - 然后把它转入 `ready_clients`
- 后续再持续发送普通 NAL

这一步是本方案能否稳定工作的核心。

### 6.3 server 进程输出必须被消费

`ADBDevice.exec_stream()` 返回的是一条长连接 socket。对于 `app_process` 这类长期进程，不能只保存引用而不读：
- 否则日志输出可能堆积
- server 异常退出时也不容易及时感知

因此 `ScrcpySession` 至少需要一个独立 greenlet：
- 持续读取 `self._server_stream`
- 记录日志
- 在 EOF / 异常时把 session 标记为 unhealthy

### 6.4 本地端口必须动态分配

不要把本地端口固定为 `27183`：
- 旧 session 未清理时容易冲突
- 设备切换时容易和残留 forward 打架
- 本机上若已有其他 scrcpy 实例，也会冲突

推荐策略：
- 优先使用 `adb forward tcp:0 ...`，让 adb 自动分配端口
- `ADBDevice.forward()` 返回真实分配到的本地端口
- `ScrcpySession` 将该端口保存到实例字段 `self._local_port`

### 6.5 session 需要“保活 + 回收”策略

文档原草案里写了“最后一个 WebSocket 断开后 session 仍保持运行”，这个方向可以保留，但不能无限悬挂。

建议：
- 当最后一个客户端断开后，不立即停 session
- 进入 idle 状态，保活 5~15 秒（可配置，默认 10 秒）
- 若窗口期内有新客户端进入，则复用现有 session
- 若超时仍无人订阅，则执行 `stop()` 清理 server / socket / forward

这样既能”秒开”，也能避免后台永久残留进程。

> **注意：** 对于资源受限的低端 Android 设备，MediaCodec 编码器会持续占用硬件编码器 slot。idle 时间不宜过长，否则可能阻塞设备上其他需要编码器的应用。建议将 idle 时间做成可配置项。

### 6.6 考虑主动请求关键帧

scrcpy server 支持通过 control socket 发送 `requestKeyFrame` 命令来强制编码器输出关键帧。虽然本次设计 `control=false`，但如果新客户端加入后等待自然 IDR 的时间过长（某些编码器 IDR 间隔可达 5~10 秒），出画体验会很差。

有两种缓解策略（可择其一）：
- **方案 A**：开启 control socket（`control=true`），仅用于在新客户端加入时发送 `requestKeyFrame`，不做触控透传
- **方案 B**：在 server 启动参数中通过 `video_codec_options=i-frame-interval=2` 控制关键帧间隔为 2 秒，以缩短等待时间

建议优先采用方案 B，因为不需要额外管理 control socket 的连接与协议。

### 6.7 屏幕旋转与分辨率变化

设备旋转时 scrcpy server 会重新协商编码参数，输出新的 SPS/PPS。此时需要特殊处理：

- `cached_sps` / `cached_pps` 会被正常更新（NAL 解析流程已覆盖）
- 但 `pending_clients` 如果此时持有的是旧 SPS/PPS 的 bootstrap 发出去后，decoder 会因参数不匹配而出错
- **建议**：当检测到 SPS 内容发生变化时（与 `cached_sps` 字节级比较），将所有 `ready_clients` 重新降级为 `pending_clients`，等下一个匹配新 SPS 的 IDR 后再重新同步

这样可以保证旋转后所有客户端都能正确解码新分辨率的画面。

### 6.8 双 socket 生命周期关系

方案中同时存在两个长连接 socket：
- `exec_stream()` 返回的 socket（承载 server 的 stdout/stderr）
- TCP forward 端口连接的 socket（承载视频流）

这两个 socket 的生命周期存在依赖：server 进程退出会导致 exec socket EOF，但 TCP socket 可能因为内核缓冲区仍有数据而延迟感知断开。

**约定：以 exec socket EOF 为 session 终止的权威信号**，而非等 TCP socket 报错。`_server_loop()` 检测到 EOF 后应主动关闭 `video_sock`，触发 `_video_loop()` 退出。

---

## 7. 建议的数据结构

建议把 `ScrcpySession` 独立成单独模块，而不是全部塞进 `web_admin.py`。

```python
class ScrcpySession:
    SERVER_JAR_LOCAL = 'vendor/scrcpy/scrcpy-server.jar'
    SERVER_JAR_REMOTE = '/data/local/tmp/scrcpy-server.jar'
    SERVER_VERSION = '3.2'

    def __init__(self, adb: ADBDevice):
        self.adb = adb
        self.serial = adb.serial or 'default'
        self.scid = generate_scid()
        self.local_port: int | None = None

        self.server_stream = None
        self.server_greenlet = None
        self.video_sock = None
        self.video_greenlet = None

        self.running = False
        self.healthy = False
        self.last_active_at = monotonic()

        self.pending_clients: set = set()
        self.ready_clients: set = set()

        self.cached_sps: bytes | None = None
        self.cached_pps: bytes | None = None
        self._annexb_buffer = bytearray()

        self._lock = gevent.lock.RLock()
```

额外建议字段：
- `start_error`: 记录最近一次启动失败原因
- `exit_reason`: 记录最近一次 server / tcp greenlet 退出原因
- `idle_timer_greenlet`: 用于空闲回收

---

## 8. ADB forward 封装建议

### 8.1 修改 `ADBDevice`

`ADBDevice` 当前没有 `forward()` / `remove_forward()`；这一步不是“可选”，而是本方案的基础能力。

建议在 `automator/control/adb/client.py` 增加：

```python
def forward(self, local: str, remote: str, norebind: bool = True) -> str | None:
    """返回 adb 分配的本地端口；若 local 不是 tcp:0，则可返回 None。"""
    if not self.serial:
        raise ValueError('forward() requires a concrete device serial')

    session = self.server._create_session_nocheck()
    try:
        norebind_prefix = 'norebind:' if norebind else ''
        cmd = f'host-serial:{self.serial}:forward:{norebind_prefix}{local};{remote}'
        session.service(cmd)

        if local == 'tcp:0':
            return session.read_response().decode().strip()
        return None
    finally:
        session.close()


def remove_forward(self, local: str) -> None:
    if not self.serial:
        raise ValueError('remove_forward() requires a concrete device serial')

    session = self.server._create_session_nocheck()
    try:
        session.service(f'host-serial:{self.serial}:killforward:{local}')
    finally:
        session.close()
```

说明：
- 协议命令统一使用 `host-serial:<serial>:forward:<local>;<remote>`
- `local` 与 `remote` 之间是分号 `;`
- 若采用 `tcp:0`，再读取一次 response 以获得 adb 分配的端口

> **⚠ 协议兼容性注意：**
> - `norebind:` 前缀的位置和格式需要对照 ADB 协议文档验证——某些 ADB 版本中 `norebind` 是作为 `local` 的前缀（`norebind:tcp:0`）而不是 `forward:` 之后的独立段
> - `local=tcp:0` 时 `read_response()` 返回的端口值的确切格式（是否带换行、是否需要额外 `OKAY` 握手）在不同 ADB server 版本间可能不同
> - **建议：** 代码中对这个路径加一个 fallback——如果纯协议方式失败，退回 `subprocess.run(['adb', '-s', serial, 'forward', ...])` 方案。这个 fallback 应作为首版实现的一部分，而非事后补丁。

### 8.2 不建议保留固定端口常量

应删除：

```python
LOCAL_PORT = 27183
```

改为：
- 启动时动态申请端口
- 实例内保存 `self.local_port`

---

## 9. `ScrcpySession` 的推荐实现流程

### 9.1 `start()`

建议流程：

1. 若本地 jar 不存在，直接抛错
2. 校验 `vendor/scrcpy/VERSION` 文件内容与 `SERVER_VERSION` 常量一致，不一致则抛错（防止 jar 与代码版本不匹配）
3. 将 jar 推送到 `/data/local/tmp/scrcpy-server.jar`
4. 调用 `adb.forward('tcp:0', f'localabstract:scrcpy_{scid}')` 获取 `local_port`
5. 通过 `exec_stream()` 启动 `app_process`：

```text
CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / \
  com.genymobile.scrcpy.Server 3.2 \
  scid=<scid> tunnel_forward=true cleanup=false \
  audio=false video=true control=false raw_stream=true
```

6. 启动 `server_greenlet` 持续消费 `server_stream`
7. 轮询连接 `127.0.0.1:<local_port>`，最多等待 5 秒；成功后立刻 `settimeout(None)`
8. 启动 `video_greenlet`
9. 设置 `running=True`、`healthy=True`

注意：
- 不要用固定 `sleep(1)` 代替 ready 检测
- 应以”成功连上本地 forward 端口”为准

> **⚠ forward 端口泄漏防护：** 如果步骤 4 成功执行了 `adb forward` 但后续步骤（如 TCP 连接超时、exec_stream 失败）中抛出异常，forward 不会被自动清理。`start()` 内部必须用 try/except 保证 forward 在任何失败路径上都被 `remove_forward()` 回收。建议结构：
>
> ```python
> def start(self):
>     ...
>     self.local_port = self.adb.forward('tcp:0', f'localabstract:scrcpy_{self.scid}')
>     try:
>         # exec_stream, connect, etc.
>     except Exception:
>         self.adb.remove_forward(f'tcp:{self.local_port}')
>         self.local_port = None
>         raise
> ```

### 9.2 `subscribe(ws)`

正确做法：
- 更新 `last_active_at`
- 若 session 未运行或已 unhealthy，则尝试重建
- 将客户端放入 `pending_clients`

不要在 `subscribe()` 时立即把客户端加入“直接广播集合”。

### 9.3 `unsubscribe(ws)`

正确做法：
- 同时从 `pending_clients`、`ready_clients` 移除
- 若两个集合都为空，则启动 idle 回收计时器

### 9.4 `stop()`

应清理：
- `video_greenlet`
- `server_greenlet`
- `video_sock`
- `server_stream`
- `adb forward`
- 缓存的 SPS / PPS
- `pending_clients` / `ready_clients`

同时要保证：
- `stop()` 可重入
- 部分步骤失败不影响其他资源继续清理

---

## 10. 读取循环必须做的事

### 10.1 视频读取循环

`_video_loop()` 不能只做 `recv()` + 广播。

建议流程：

1. 从 `video_sock.recv()` 取字节流
2. 追加到 `self._annexb_buffer`
3. 提取完整 NAL 单元
4. 对每个 NAL：
   - 若是 SPS，更新 `cached_sps`
   - 若是 PPS，更新 `cached_pps`
   - 若是 IDR：
     - 对所有 `pending_clients` 发送 `SPS + PPS + IDR`
     - 然后把这些客户端转入 `ready_clients`
   - 对所有 `ready_clients` 正常发送当前 NAL
5. 若 EOF / socket error：
   - 记录退出原因
   - `healthy=False`
   - 关闭自身资源

伪代码：

```python
def _handle_nal(self, nal: bytes):
    nal_type = parse_h264_nal_type(nal)

    if nal_type == 7:  # SPS
        sps_changed = (self.cached_sps != nal)
        self.cached_sps = nal
        if sps_changed and self.ready_clients:
            # 分辨率/参数变化（如屏幕旋转），将所有 ready 客户端降级为 pending，
            # 等下一个匹配新 SPS 的 IDR 后重新同步
            self.pending_clients |= self.ready_clients
            self.ready_clients.clear()
        # SPS 也要发给 ready_clients（编码器可能在流中途重发）
        for ws in list(self.ready_clients):
            _safe_send(ws, nal)
        return

    if nal_type == 8:  # PPS
        self.cached_pps = nal
        for ws in list(self.ready_clients):
            _safe_send(ws, nal)
        return

    if nal_type == 5:  # IDR
        bootstrap = b''
        if self.cached_sps:
            bootstrap += self.cached_sps
        if self.cached_pps:
            bootstrap += self.cached_pps
        bootstrap += nal

        # 先提升 pending → ready，避免下面的广播重复发送 IDR
        newly_ready = set()
        for ws in list(self.pending_clients):
            _safe_send(ws, bootstrap)
            self.pending_clients.discard(ws)
            newly_ready.add(ws)

        # 对已有的 ready_clients 发送 IDR（不含 SPS/PPS 前缀，它们已单独发过）
        for ws in list(self.ready_clients):
            _safe_send(ws, nal)

        self.ready_clients |= newly_ready
        return

    # 非关键帧（nal_type == 1 等）
    for ws in list(self.ready_clients):
        _safe_send(ws, nal)
```

其中 `_safe_send()` 应捕获发送异常并将死连接从对应集合中剔除：

```python
def _safe_send(self, ws, data: bytes):
    try:
        ws.send(data)
    except Exception:
        self.ready_clients.discard(ws)
        self.pending_clients.discard(ws)
```

实现时还要补：
- ~~死连接剔除~~ （已在 `_safe_send()` 中处理）
- 错误隔离（单个 ws 发送失败不应影响其他客户端）
- ~~对 `pending_clients` 首次送出 bootstrap 时避免重复向同一客户端发送两次 IDR~~ （已通过先提升再广播的顺序解决）

### 10.2 server 输出读取循环

`_server_loop()` 建议：
- 持续读取 `server_stream.recv()`
- 把输出写日志
- 若出现 EOF，认为 `app_process` 已退出
- 将 session 标记为 unhealthy

这一步能解决“server 已死但 session 表面还在”的问题。

---

## 11. `web_admin.py` 的改动建议

### 11.1 session 管理

保留按 serial 管理 session 的思路，但要补全清理与健康检查。

建议新增：

```python
self._scrcpy_sessions: dict[str, ScrcpySession] = {}
self._scrcpy_lock = gevent.lock.RLock()  # 与 ScrcpySession 内部统一使用 gevent.lock
```

并提供：

```python
def _get_or_create_scrcpy_session(self, helper) -> ScrcpySession:
    serial = helper.control.adb.serial or 'default'
    with self._scrcpy_lock:
        session = self._scrcpy_sessions.get(serial)
        if session is None or not session.running or not session.healthy:
            if session is not None:
                session.stop()
            session = ScrcpySession(helper.control.adb)
            session.start()
            self._scrcpy_sessions[serial] = session
        return session
```

还应补一个清理函数：
- 设备切换时停止旧 serial 对应 session
- session idle 超时后从 `self._scrcpy_sessions` 删除

### 11.2 `/api/screen/ws` handler

当前逻辑有三点要保留：
- WebSocket 连接数限制
- `_get_helper_with_reconnect()` 的容错
- 多次失败后向前端发送 `fallback` 状态

新 handler 不应退化成“拿到 session 后原地 `gevent.sleep()`”。

建议逻辑：

```python
@self.app.route('/api/screen/ws', apply=[websocket])
def api_screen_ws(ws):
    # 1. 保留现有连接数限制
    # 2. 保留 helper 获取失败时的指数退避和 fallback
    # 3. 获取或创建 scrcpy session
    # 4. session.subscribe(ws)
    # 5. 阻塞等待 ws 关闭
    # 6. finally: session.unsubscribe(ws)
```

与旧实现相比，至少要保留：
- 启动失败最多重试 3 次
- helper 不可用时回退截图模式
- 连接断开时正确减计数

---

## 12. 文件与资源变更

### 12.1 新增文件

```text
vendor/scrcpy/
  scrcpy-server.jar
  VERSION
```

其中：
- `scrcpy-server.jar`：来自官方 GitHub release 的 server 文件
- `VERSION`：记录当前使用的 server 版本，例如 `3.2`

下载来源应写成“官方 release 页面”，不要只写裸下载链接，避免后续升级时误用非官方镜像。

### 12.2 修改文件

| 文件 | 改动 |
|------|------|
| `web_admin.py` | 改写 `/api/screen/ws`；增加 session 管理入口 |
| `automator/control/adb/client.py` | 增加 `forward()` / `remove_forward()` |
| `templates/dashboard.html` | 协议层无需改动；需评估 reconnect 路径兼容性（见下方说明）；可选增加”等待关键帧”提示 |
| `scrcpy_session.py`（建议新增） | 放置 `ScrcpySession`、Annex-B 切分和 session 生命周期逻辑 |

说明：
- 这次不建议把所有状态机都堆在 `web_admin.py`
- **前端 reconnect 兼容性**：当前前端的 reconnect 逻辑假设每次 WebSocket 重连后都会从一个新 `screenrecord` 进程的流开头开始。切换到共享流后，WebSocket 重连不再意味着流重启——前端重连后 `pendingBuffer` 清理和 decoder reset 逻辑需要确认仍然正确工作。具体需检查：
  - reconnect 时是否正确清空 `pendingBuffer`、`cachedSps`、`cachedPps`
  - reconnect 时是否重新创建 `VideoDecoder`（而非复用旧的可能处于错误状态的 decoder）
  - 如果后端首帧同步在 reconnect 后立即发出 `SPS+PPS+IDR`，前端能否正确处理
- 前端严格来说不是”零影响”：协议不变，但若想提升用户体验，可在前端增加”等待首帧 / 等待关键帧”文案

---

## 13. 实现步骤（修正版）

### Step 1 — 引入 scrcpy server 文件

- 将官方 release 的 `scrcpy-server-v3.2` 以仓库文件形式保存为 `vendor/scrcpy/scrcpy-server.jar`
- 新增 `vendor/scrcpy/VERSION`，内容写 `3.2`

### Step 2 — 为 ADB 层补 forward 能力

- 在 `automator/control/adb/client.py` 中新增 `forward()` / `remove_forward()`
- 优先支持 `tcp:0`
- 为固定 serial 和动态分配端口两种情况做好错误处理

### Step 3 — 新增 `scrcpy_session.py`

职责：
- server 启停
- ADB forward 管理
- server 输出监控
- Annex-B NAL 切分
- SPS / PPS / IDR 同步
- pending / ready client 管理
- idle 回收

### Step 4 — 接入 `web_admin.py`

- 将 `/api/screen/ws` 从“每连接一个 screenrecord”切换为“连接到共享 session”
- 保留现有重试、fallback、连接计数逻辑
- 在设备切换或 helper 变化时回收旧 session

### Step 5 — 可选前端 UX 补丁

协议层不需要修改，但建议：
- 在进入 stream 模式后显示“正在等待关键帧”
- 首帧到达后再隐藏 loading
- 首帧等待超时时提示已自动切回截图模式（如果后端给出 fallback）

---

## 14. 测试清单

- [ ] 单 WebSocket 连接正常显示
- [ ] 两个 WebSocket 同时连接，仅启动一个 scrcpy server
- [ ] 第二个 WebSocket 在第一个连接后加入，能在下一帧关键帧后正常出画
- [ ] 最后一个 WebSocket 断开后，session 进入 idle；在 TTL 内重连可秒开
- [ ] idle 超时后 session 自动清理，ADB forward 被移除
- [ ] scrcpy server 异常退出后，下次请求会自动重建
- [ ] 设备切换后，旧 serial 的 session 被停止且从缓存移除
- [ ] 没有设备时，仍能沿用现有 fallback 到截图模式
- [ ] 本机已有其他 scrcpy 实例时，不发生固定端口冲突
- [ ] 设备旋转后，所有已连接客户端能正确切换到新分辨率画面
- [ ] `start()` 中 forward 成功但后续步骤失败时，forward 端口被正确回收
- [ ] 前端 WebSocket 断开重连后，能正常出画（不残留旧 decoder 状态）
- [ ] 单个客户端 WebSocket 发送异常时，不影响其他客户端正常收流

---

## 15. 风险与注意事项

### 15.1 版本锁定

协议与参数细节和版本有关；仓库中的 `VERSION`、常量 `SERVER_VERSION` 与 vendored jar 必须保持一致。

### 15.2 Android 兼容性

- 依赖 Android 5.0+（API 21+）
- 个别厂商 ROM 上 `MediaCodec` 行为可能不稳定
- 若发现特定机型 scrcpy server 失败率高，仍要保留回退到截图模式的通道

### 15.3 不要依赖固定 `sleep(1)`

server 启动时延会受设备性能影响，应该轮询本地 forward 端口是否可连接，而不是写死睡眠时间。

### 15.4 共享流首帧是”体验优化”而非”功能前提”

`SPS/PPS/IDR` 首帧补偿能显著加速新客户端出画，但并非”没有就完全不能工作”。前端已经具备完整的 NAL 切分和 SPS/PPS 缓存能力，即便后端不做首帧同步，前端也能在等到下一个自然 IDR 后正常解码。

但在实际体验上，如果没有后端首帧同步：
- 第二个 WebSocket 可能需要等待数秒才能出画
- 某些编码器若 IDR 间隔很长，等待时间会更久
- 用户会感受到明显的黑屏等待

因此后端首帧同步应作为首版实现的一部分，而不是延后优化。

### 15.5 `raw_stream=true` 的协议格式须抓包验证

在写代码之前，务必对目标版本的 scrcpy server 做一次实际抓包，确认 `raw_stream=true` 下视频 socket 输出的确切字节格式。不要仅凭文档或源码推断。

### 15.6 ADB forward 协议的版本差异

`forward` 命令的协议格式（特别是 `norebind` 前缀位置和 `tcp:0` 端口返回格式）在不同 ADB server 版本间存在差异。实现时应优先使用纯协议方式，但必须同时准备好 `subprocess.run(['adb', ...])` 的 fallback 路径。

---

## 16. 最终结论

这次改造值得做，但正确的落地方式应当是：

1. 用 `scrcpy server` 替换 `screenrecord`
2. 使用 ADB forward + 动态本地端口
3. 增加 `ScrcpySession` 生命周期管理
4. 在后端实现 Annex-B 轻量切分与首帧同步
5. 保留现有 fallback 语义与重试逻辑

只有这样，才能在不改动整体产品交互的前提下，真正解决 175 秒重启问题，而不是把问题从“进程重启”转移成“共享流新客户端黑屏”。
