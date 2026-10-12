# Quest XR Bridge 0.3.0 使用文档

## 1. 安装

基础姿态 SDK 需要 Python 3.13+，支持手部/身体追踪的 Quest Browser 和互通局域网。
从仓库 v0.3.0 Release 下载 wheel、文档包和 SHA256SUMS。校验与安装：

```bash
sha256sum -c SHA256SUMS
python -m pip install quest_xr_bridge-0.3.0-py3-none-any.whl
quest-xr-bridge
```

校验时应将 SHA256SUMS 列出的产物放在同一目录。基础 SDK 不导入 GI、相机 SDK 或 Pillow。
开发证书自动生成在工作目录 `certs/`；首次进入 Quest 页面需要信任该证书。
生产或已有证书通过 `cert_file` 与 `key_file` 成对传入，不会被 SDK 覆盖。

CLI 支持 `POSE_HOST`、`POSE_PORT`、`POSE_LOG_ENABLED`、`POSE_CERT_FILE`、`POSE_KEY_FILE`。
默认HTTPS端口8000、录制关闭。CLI复用SDK；SDK本身不修改宿主信号或全局日志设置。

完整Python参数、返回值、异常及线程契约见 [SDK API参考](API.md)。

## 2. 生命周期与姿态

```python
from quest_xr_bridge import QuestServer

service = QuestServer(host="0.0.0.0", port=8000, record_poses=False)
try:
    service.start()  # 等待监听就绪；端口、证书等错误直接抛出
    print(service.running, service.url)
    run_your_application(service)
finally:
    service.stop()
```

也可使用上下文管理。只读 `running` / `url` 表示对象状态；停止后可重新启动。
控制方法由宿主串行调用；`submit_video` 可由采集线程调用。
`record_poses=True` 开启有界后台录制、轮转与配额；并行录制实例须使用不同 `data_dir`。

Quest 访问 `/`，点击 **Start prep**，三秒后发送姿态。身体锚点不可用时明确提示，
不输出错误人体坐标。PC Viewer 位于 `/viewer`，250ms无新帧或断开即隐藏旧姿态。

消费端连接 `wss://<PC-IP>:8000/ws`，仅在真实新姿态到达时收到 JSON，不存在固定输出时钟。
每手21点和腕部姿态、关节半径、肘、肩、头部角度与序号均保留；缺失值为null。
手点为腕部局部坐标，肩/肘/腕为人体坐标；默认人体轴X前、Y上、Z右，单位米，四元数XYZW。
消费者也应自行按接收时间判断过期。坐标变换通过 `/api/coordinate-transform` 配置。

### 消费许可契约（暂行）

`/ws` 打开只说明连接存在。服务在姿态过期250ms后不再发送该帧，也不额外发送
失效事件；身体锚点丢失或上游断线时，同样可能只表现为静默。
`/health.healthy` 表示核心服务健康，不能作为机器人允许运动的开关。
视频故障时姿态继续输出是已有的通道隔离行为，也不表示视觉依赖的操作可以继续。

消费端必须在独立控制循环中检查许可，不能等下一次 `recv()` 才检查超时。
以下50ms/100ms为项目**暂行**阈值，均按消费端本地单调时钟计算最后一帧有效新数据的收包间隔。
它们不等于姿态采集到机器人执行的端到端延迟；实际降速和保持动作仍为**未验收**。

| 条件 | 消费端行为 |
|---|---|
| 启动、尚未有新姿态 | 两手均不允许输出控制命令，等待新数据及用户确认 |
| 某手 `tracked=false` | 立即撤销该手许可；不沿用上一帧坐标，不用另一只手的状态代替 |
| 距有效新帧收包达到50ms | 告警，上层进入项目定义的降速策略；不通过缓存旧帧消除告警 |
| 达到100ms、连接中断或系统故障 | 进入保持，锁定许可；保持的具体执行方式由机器人控制器定义 |
| 同一session内seq有跳号 | 接纳较新的有效帧；latest-only允许跳号，不补发或重放缺失帧 |
| seq重复、回退或帧校验失败 | 丢弃，不刷新新鲜度；撤销许可，等待后续有效新帧与用户确认 |
| session改变、上层确认来源改变或消费连接重连 | 清理旧样本/序号基线并锁定；同session重连也不自动恢复授权 |
| 坐标约定或机器人映射改变 | 上层按来源改变处理，重新校验和确认 |
| 跟踪、数据和故障状态恢复 | 只恢复“可确认”条件；用户明确确认后才恢复对应手的许可 |

双臂耦合任务可由上层把单手故障提升为整体保持。头、肘、肩等控制依赖的部位
还须分别满足其 `tracked` 和数据校验条件；双手有效不意味着其他部位有效。
不要把 `confirm()` 放在每帧处理、自动重连或定时任务中。

当前源码的 `/ws` JSON新增 `ingress_connection_id`：服务端为每个已建立的姿态源生成
独立UUID，随该源的每个有效新帧输出。`session_id` 在同一个XRSession的姿态重连期间
可能不变；即使seq继续增长且中断短于100ms，也必须在连接编号变化时清理基线并锁定。
参考门控自动检查该字段；消费端自己的 `/ws` 断开/重连仍需显式调用 `source_changed()`。
连接编号不是授权凭据，也不能代替本地收包超时；上游断线后没有新帧时仍按50ms/100ms检查。
旧服务不输出该字段时，参考门控继续接纳旧JSON，但逐次上游连接识别须由部署方补来源监督。

读取任务应及时消费并只保存最新有效帧。接收队列中的旧帧也可能拥有递增seq；
本地刚读到不等于刚采集。发生可疑积压时先保持并重建数据基线。
`capture_epoch_ms` 与PC时钟有偏差，`relative_transport_delay_ms` 仅反映相对排队变化，
不能直接替代绝对采集延迟测量或控制许可。

当前源码提供可测试的参考门控 [pose_consumer.py](../examples/pose_consumer.py)，
随下一次wheel构建包含在 `quest_xr_bridge.examples`；QCRT输入格式和SDK公共控制接口保持现有版本。
已冻结的0.3.0发布产物尚不包含此补丁。上层接法如下：

```python
import time
from quest_xr_bridge.examples.pose_consumer import PoseConsumerGate

# 暂行阈值；仅是姿态可用性门控，不执行机器人动作。
gate = PoseConsumerGate(warning_s=0.050, timeout_s=0.100)

# 收到且校验 /ws payload 后，记录本地收包时间；不要传Quest时间戳。
accepted = gate.receive(pose, time.monotonic())

# 独立控制循环每个周期执行，静默时也执行。
now = time.monotonic()
receipt_warning = gate.warning(now)  # 达到50ms：提示并按上层策略降速
per_hand_allowed = gate.allowed(now)  # 达到100ms：禁止继续更新运动命令，保持并锁定

# /ws断线、重连、监督到来源变化或坐标映射改变时执行：
gate.source_changed()

# 系统故障期间持续禁止确认，例如视觉依赖任务的视频失效：
gate.set_inhibited(True)
# 上层已经证实故障解除；此操作不会重新授权：
gate.set_inhibited(False)
# 确认按钮事件中，且已经有新有效数据时执行：
gate.confirm("left", time.monotonic())
gate.confirm("right", time.monotonic())
```

视频源/worker状态可由上层监督，但 `/health.video.frame_age_ms` 不代表头显解码或最终显示。
XR HUD的告警目前也不经 `/ws` 回传。要求头显解码故障自动使机器人降速/保持时，
上层必须有接收端故障反馈或等价监督，并把活动故障接到 `set_inhibited()`；
该真实机器人闭环仍为**未验收**。控制循环的调度误差和保持执行时间必须计入实测，
不能把参考门控的100ms判断直接当成物理制动时间。

## 3. 冻结的双目显示配置

安装可选的参考图像处理依赖：

```bash
python -m pip install Pillow
```

```python
from quest_xr_bridge import QuestServer
from quest_xr_bridge.examples.frozen_stereo import start_frozen_video, prepare_eye

with QuestServer() as service:
    start_frozen_video(service)  # 平面显示 + 每眼1278×360、60FPS上限
    for left, right, capture_ns in your_camera_frame_pairs:
        # left/right：每眼1280×720、同步校正、连续内存RGB888
        accepted = service.submit_video(
            prepare_eye(left), prepare_eye(right), timestamp_ns=capture_ns,
        )
```

`prepare_eye` 复现验收中的裁边、隔行取样与JPEG80往返，不调整对比度/曝光。
其输出是拥有独立内存的RGB字节。应由采集/处理线程调用，不要阻塞宿主UI线程。
SDK 不生成相机帧；宿主负责相机启停、同步、标定和立体校正，以及云台/机器人跟随头姿。
实际采集必须持续提供60帧；`fps=60` 不会把低帧率输入补成60帧。
不要重新加入0.03秒等待。若上游缓存可能累积，应只保留最新完整左右帧对。

冻结参数等价于：

```python
from quest_xr_bridge import VideoConfig, VideoDisplayConfig

service.set_video_display(VideoDisplayConfig(
    projection="plane", height_m=8, distance_m=7,
    aspect_ratio=1.66667, offset_x_m=0, offset_y_m=-1,
    swap_eyes=False, saturation=1, gamma=1,
))
service.start_video(VideoConfig(width=1278, height=360, mode="stereo", fps=60))
```

先设置平面投影，再启用视频。基础 API 默认仍为 `projection="camera"`；
冻结效果通过上述显式配置复现，避免改变其他调用方的投影语义。
Quest 页面勾选 Video return，再 Start prep；页面内可在线调整显示参数。

## 4. 通用 RGB 输入

`VideoConfig.width/height` 表示**每眼**尺寸，mode为mono或stereo。
普通bytes、bytearray、memoryview或连续uint8数组均可输入，必须恰好为width×height×3字节。
SDK不猜RGB/BGR，不接受非连续视图；宿主自行转换。
双目必须一次提交两张同尺寸图和共同时间戳；单目只提交左图。
提交调用只校验并拷贝到有界共享帧槽，不等待编码/网络；返回后输入缓冲可复用。

时间戳应使用同一递增单调采集时基的纳秒值。省略时SDK使用本机monotonic_ns。
尺寸、单/双目模式变更须 `stop_video()` 后重新 `start_video()`。
启用失败抛 `VideoUnavailableError`，姿态继续运行。进程故障后修复条件并显式重新start_video。

若选择camera投影，需要与校正图实际尺寸相符的 `CameraIntrinsics(fx,fy,cx,cy)`，
设置到VideoConfig.left_intrinsics/right_intrinsics；缩放或裁切后同步更新K。
plane投影不需要K；双目校正仍由外部完成。

| 展示参数 | 意义 |
|---|---|
| projection | camera标定射线，或plane头部跟随平面；显式选择，无自动回退 |
| height_m / distance_m | plane高度/距离，各[0.05,100]米 |
| aspect_ratio | None采用每眼输入比例，或显式[0.1,10] |
| offset_x_m / offset_y_m | plane水平/竖直偏移，各[-100,100]米 |
| swap_eyes | 交换输入眼序，不修改头显IPD |
| saturation / gamma | [0,2] / [0.5,2]，默认1保持原色，两眼共用 |

数值范围是输入校验，不表示任意取值都舒适。平面距离不是镜头对焦或真实物体距离。

## 5. 视频原生部署

视频支持Linux/NVIDIA硬编。wheel只含Python、网页和补丁，不包含GPU驱动、GStreamer或GI。
主SDK用Python3.13+；独立worker默认 `/usr/bin/python3`，需能import GI，亦可指定worker_python。

Ubuntu24.04的基础组件可安装为：

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0   gir1.2-gst-plugins-bad-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base   gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-nice
```

仍需单独提供 `nvh264enc` / `nvh265enc`、RTP payloaders/parsers、TWCC扩展和Rust插件 `rtpgccbwe`。
发行版包不保证包含全部功能；SDK在启用视频时检查插件并真实试编码，缺项明确报错，不回退软件编码。

部署的webrtcbin必须是GStreamer1.24.13或兼容更高版本，并应用
`quest_xr_bridge/patches/gstreamer-webrtc-dtls-owner.patch`。本次实际验证版本为1.24.13。
在对应gst-plugins-bad源码根目录应用补丁，并在Meson构建中设置：

```bash
patch -p1 < /path/to/gstreamer-webrtc-dtls-owner.patch
# 加入既有Meson配置；还需要系统GStreamer开发依赖及DTLS/SRTP/SCTP开发库
meson setup build --prefix=/your/native/prefix --libdir=lib/x86_64-linux-gnu   -Dauto_features=disabled -Dwebrtc=enabled -Ddtls=enabled -Dsrtp=enabled   -Dsctp=enabled -Dintrospection=disabled -Dtests=disabled -Dexamples=disabled   '-Dpackage-name=GStreamer Bad Plug-ins (quest-crt DTLS owner fix)'
meson compile -C build
meson install -C build
```

该步骤只准备WebRTC相关构建件；NVENC/GCC等仍须另行准备。
若使用自定义前缀，启动宿主前配置LD_LIBRARY_PATH、GST_PLUGIN_PATH_1_0与GI_TYPELIB_PATH，
确保webrtcbin及相关webrtc/webrtcnice/sctp库来自同一兼容构建。
SDK检查实际加载插件版本及package标记；标记是部署约定，不是密码学证明。
补丁修复DTLS对象浮动引用所有权，单纯安装原版1.24.13仍不能保证同样的关闭行为。
新版源码若已经修复，应先核对补丁及原生关闭回归，不能仅伪造package标记。

默认8Mbps起始、16Mbps上限；GCC按TWCC调节码率，NACK/RTX在对端支持时协商。
优先H.265 Main，其次满足规格的H.264；严格按真实接收能力协商，不改写浏览器能力。
仅降低码率或丢弃过期帧，不暗降输入尺寸/FPS。重入XR会回收旧视频worker并启动新进程，可能多等几秒。

## 6. 接口、状态与排障

| 地址 | 用途 |
|---|---|
| GET /、/viewer | Quest与PC页面 |
| POST /api/webrtc/offer、/api/webrtc/close | 姿态WebRTC协商/关闭，同页面凭据与递增代次支持重连接管 |
| POST /api/video/offer、/api/video/close | 独立视频协商/关闭，使用每连接UUID peer_id |
| GET/PUT /api/video/config | 视频状态及显示配置 |
| GET/PUT /api/coordinate-transform | 坐标配置 |
| WSS /ws | 仅新姿态的JSON输出 |
| GET /health | 姿态健康及独立video状态 |

### 姿态连接接管（当前源码）

新版Quest页面在打开时随机生成 `client_token`，每次协商递增 `attempt_id`，网络重连期间
保留这两个状态。同一凭据的新代次可撤销自己的旧姿态连接，不等待旧ICE连接失效；
其他页面、其他凭据和缺少凭据的旧客户端在已有占用时仍收到409。
凭据仅保存在页面内存；刷新、关闭后重开或新标签产生新凭据，属于新的来源。
正常退出尽力发送close；断网导致通知丢失时，同页面下一次offer仍能接管。

close仅撤销对应代次，重复或迟到的旧close不会关闭新连接；已取消或过期offer不被接纳。
服务保留最近64个页面凭据的最高代次，并额外核对当前活动连接的代次。
被记录淘汰的退休页面不保证识别任意久远的请求重放。
`client_token` 是接管凭据，不能写入日志、健康接口、姿态输出或分享给其他客户端。
它只证明持有同一页面凭据；部署仍面向可信局域网，不提供设备身份认证。

旧源先停止发布、清空旧数据并释放占用，WebRTC关闭和日志收尾在后台完成；
SDK退出仍等待这些资源。源撤销暂行预算1秒，超时返回503并保留原连接槽，禁止并行输出。
恢复目标仍为API可达后5秒内收到首个有效新姿态，10秒失败提示继续显示；
2026-10-12补丁已完成单轮头显断网复测：首个有效姿态在Wi-Fi连通后约2.945秒收到，
客户端姿态连接在首次成功API配置回调前已打开，视频在该回调后约3.301秒恢复。
视频状态回报距Wi-Fi连通约6.567秒；两种计时口径分别保留，不能把探测等待排除后
宣称实际网络连通到视频恢复也≤5秒。重复、弱网、长时和真实机器人动作仍**未验收**。
`/ws` 的新增JSON字段需由严格字段白名单消费者纳入校验；QCRT仍为v5/804字节。

视频图像不经HTTP。`video.running` 表示源生命周期就绪，worker_running表示子进程存活；
peer_connected、frame_age_ms和实际接收统计共同判断视频是否在更新，不能只看running。
250ms无新源帧或解码帧时隐藏视频；错误不结束姿态或XRSession。
当前源码的XR HUD会显示视频失效、姿态连接中断、可判定的协商/状态查询超时、身体追踪缺失，
以及本地姿态发送缓冲持续堵塞达到暂行50ms的告警。故障与准备倒计时可同时显示。
成功收到PC控制配置响应视为TLS/API可达；故障仍未恢复时，从此开始计时，达到暂行10秒
在HUD提示对应通道恢复失败。暂行5秒为恢复验收目标，不自动触发运动重新授权。
本地发送状态没有PC收包ACK；头显提示不能替代上述消费端50ms/100ms检查。
退出XR/关闭视频会关闭对应peer并停止编码，宿主相机可继续采集；stop_video才停止整个视频源。

0.2迁移到0.3：`:8001/ws` 改为`:8000/ws`；移除WSS姿态上行、`/ws/stream`、QSTR/UDP与固定90Hz时钟。
旧StreamBus/StreamClock等Python API不再导出；旧短包、video_return及QCRT bit7均不再使用。
发送方必须使用完整QCRT v5 / 804字节。相机专用video-host/presence/stats接口与CloudXR启动器已删除。


## 7. 名称迁移

仓库/发行包/CLI：`quest-crt` → `quest-xr-bridge`；Python导入：`quest_crt` → `quest_xr_bridge`。
本版不保留旧导入别名。升级时先卸载旧包，避免混用两个CLI：

```bash
python -m pip uninstall quest-crt
python -m pip install quest_xr_bridge-0.3.0-py3-none-any.whl
```

网络上的QCRT v5名称与804字节布局不因项目更名而改变。
原生插件构建标记仍为 `quest-crt DTLS owner fix`，它是已验证构建的兼容契约，不是发行包名。
