# Quest XR Bridge 0.3.1+stage2.1 Python SDK API

安装发行包 `quest-xr-bridge`，导入模块 `quest_xr_bridge`。本文对应基于0.3.1源码的stage2.1本地兼容构建，驾驶扩展见本文末尾。
数据通过SDK的HTTPS/WSS/WebRTC服务消费；QuestServer没有隐含的相机驱动或机器人控制器。

## QuestServer

```python
QuestServer(
    host: str = "0.0.0.0",
    port: int = 8000,
    *,
    record_poses: bool = False,
    cert_file: str | Path | None = None,
    key_file: str | Path | None = None,
    data_dir: str | Path | None = None,
)
```

| 参数 | 含义 |
|---|---|
| host | 监听地址；0.0.0.0监听所有IPv4接口 |
| port | 0–65535；0选择可用端口，可从url读取，通常用8000 |
| record_poses | 是否录制姿态，默认关闭 |
| cert_file / key_file | 必须成对指定；外部TLS文件保留原样，缺失会报错 |
| data_dir | 证书与可选日志的根目录，默认当前工作目录；并行录制实例不可共用日志目录 |

构造函数只创建对象，不开始监听。非法host/port或不成对的TLS参数抛ValueError。

### running: bool

只读。服务线程存活且HTTP服务已启动时为True。不是视频接收或人体追踪质量指标。

### url: str

只读。运行时返回HTTPS访问地址，例如 `https://192.168.1.10:8000`。
未启动或停止后读取抛RuntimeError；因此应在start返回后使用。

### start(timeout: float = 10) -> QuestServer

同步等待端口监听和应用启动完成，返回自身。已运行时幂等返回自身。
超时抛TimeoutError；非法timeout抛ValueError/TypeError；端口、证书、文件、网络等启动错误向调用方返回。
例如端口冲突为OSError，外部证书缺失为FileNotFoundError；录制目录占用为RuntimeError。
成功返回只表示服务就绪，不表示Quest已连接或相机视频已启用。

### stop(timeout: float = 10) -> None

关闭HTTP、姿态连接、后台处理/录制线程及视频进程，回收自有共享内存与句柄。
停止后可重新start。宿主自己创建的相机/采集线程不归SDK所有，应由宿主管理。
服务线程未及时停止时抛TimeoutError，保留句柄，调用方可再次stop等待；视频清理错误可能为RuntimeError。
控制方法应串行调用；不要同时从多个线程start/stop或修改视频生命周期。

### 上下文管理

`with QuestServer() as service:` 自动start，退出（包括异常退出）自动stop。
不要依靠垃圾回收关闭服务。SDK不修改宿主的信号处理或全局日志配置。

### start_video(config: VideoConfig, timeout: float = 10) -> None

必须在服务运行后调用；否则抛RuntimeError。启动SDK持有的视频worker，检查GI、插件及实际硬编能力。
config类型不正确抛TypeError；原生依赖、GPU、worker启动失败抛VideoUnavailableError。
视频失败不停止姿态服务。成功不代表头显已协商接收，需要页面勾选视频并进入XR。
更换尺寸或mono/stereo模式时先stop_video再start_video；同配置重复启动为幂等操作。

### submit_video(left_rgb, right_rgb=None, *, timestamp_ns: int | None = None) -> bool

可从采集线程调用。mono只传left，stereo必须同次提交左右两图。

- 每眼是连续内存的RGB888，恰好为width×height×3字节；支持bytes、bytearray、memoryview和连续uint8数组。
- 不猜BGR、不猜眼序、不自动调整尺寸，不接收非连续切片。
- timestamp_ns为共同采集时间戳：非负uint64整数，在同一视频源生命周期内严格递增。
  推荐同一单调采集时基；省略时使用本机time.monotonic_ns()。
- SDK拷贝到固定有界缓冲；返回后调用方可复用输入内存。
- 不等待编码/网络，存在用于完整帧所有权的短临界区；不是零拷贝或无锁接口。

**True表示本次完整帧已接纳，不保证最终显示。** latest-only允许未处理旧帧被替换。
False表示视频未启用或当前不可接纳；不应无限积压或补交旧帧。
有效视频源上的错误眼数、尺寸、dtype、连续性或时间戳抛ValueError；不支持buffer的对象抛TypeError。

### set_video_display(config: VideoDisplayConfig) -> None

设置整份显示配置；非该类型抛TypeError。可在启用视频前设置。
运行时改变几何/颜色/眼序，不创建额外网络连接或XRSession。
这不是字段patch：新配置中未写出的字段使用dataclass默认值。保留其他设置可用：

```python
from dataclasses import replace

display = VideoDisplayConfig(projection="plane", height_m=8, distance_m=7,
                             aspect_ratio=1.66667, offset_y_m=-1)
service.set_video_display(display)
service.set_video_display(replace(display, gamma=1.1))
```

### stop_video(timeout: float = 10) -> None

仅停止视频源、视频连接与worker并回收共享内存；姿态/HTTPS继续运行。
未启用时可安全调用。清理失败会报告异常，不将仍存活的线程视为已释放。
整个service.stop也会执行视频清理。页面退出XR仅关闭当前视频peer，不等价于stop_video。

## VideoConfig

不可变dataclass；通过新建对象或dataclasses.replace修改。

```python
VideoConfig(width, height, mode="mono", fps=60,
            start_bitrate_mbps=8, max_bitrate_mbps=16,
            worker_python="/usr/bin/python3",
            left_intrinsics=None, right_intrinsics=None)
```

| 字段 | 契约 |
|---|---|
| width / height | 必填，每眼像素尺寸，正偶数整数 |
| mode | mono或stereo，默认mono |
| fps | 1–60整数，编码/发布上限；源必须自行产生足够新帧 |
| start_bitrate_mbps | 起始编码码率，默认8 |
| max_bitrate_mbps | 码率上限，默认16；要求0 < start <= max <= 50 |
| worker_python | 具有GI/原生视频依赖的独立Python解释器，不必与主SDK解释器相同 |
| left_intrinsics / right_intrinsics | CameraIntrinsics或None；camera投影需要，plane投影可省略 |

编码宽度为width（mono）或2×width（stereo）；编码宽/高不超过4096，
宏块数不超过8704，宏块率不超过522240/秒；还需满足对端实际codec能力及GPU能力。
构造配置不意味着视频已可运行；start_video和实际协商会继续检查。
双目如提供K必须成对提供，单目不接受right_intrinsics。非法值抛ValueError，错误K类型抛TypeError。

## CameraIntrinsics

```python
CameraIntrinsics(fx: float, fy: float, cx: float, cy: float)
```

不可变，像素单位；fx/fy为有限正数，cx/cy为有限主点坐标，左上角像素中心为(0,0)。
必须对应实际提交的已校正图，裁剪/缩放后须更新；不是未经校正原图的畸变参数。
不会控制镜头或执行标定。不合法值（含bool、NaN、Infinity）抛ValueError。

## VideoDisplayConfig

```python
VideoDisplayConfig(swap_eyes=False, saturation=1, gamma=1,
                   projection="camera", height_m=1, distance_m=1,
                   aspect_ratio=None, offset_x_m=0, offset_y_m=0)
```

| 字段 | 作用/取值 |
|---|---|
| projection | camera按内参投影，plane按共享显示面投影；不自动回退 |
| swap_eyes | bool；仅交换源眼图，camera模式同时交换对应K |
| saturation | [0,2]，1保持原色，0灰度 |
| gamma | [0.5,2]，1保持原色；大于1提亮暗部 |
| height_m / distance_m | plane高度/前方距离，[0.05,100]米 |
| aspect_ratio | None采用每眼输入比例，或[0.1,10] |
| offset_x_m / offset_y_m | plane横向/竖向位移，[-100,100]米 |

平面中心是viewer空间 `[offset_x_m, offset_y_m, -distance_m]`，两眼共用同一几何。
这些参数不修改头显IPD、物理焦平面或场景真实深度。范围仅校验输入，不保证舒适度。
字段非法抛ValueError。默认camera是通用API默认值；冻结配置由示例显式设置为plane。

## VideoUnavailableError

RuntimeError子类。表示视频运行环境/worker不可用；捕获后可以继续使用姿态功能。
修复原生依赖或GPU问题后重新start_video，不需要重新创建整个QuestServer。
HTTP状态及接收帧率通过 `/api/video/config`、`/health` 和接收端统计观察；
接收端软件状态通过下述 `video_status()` 方法读取；没有姿态回调。

## QuestServer.video_status(*, receiver_timeout_s=0.1) -> dict

返回独立快照，不启动相机、原生视频 worker 或设备。可从宿主线程读取。
HTTP `GET /api/video/status`、`GET /api/video/config` 和 `/health` 的 `video`
包含相同发送端信息及新增的 `receiver` 字段；HTTP 使用默认100ms超时。
`receiver_timeout_s` 必须为有限正数，非法值抛ValueError。

```python
status = service.video_status()
receiver = status["receiver"]
# 宿主必须把来源身份与自己消费的姿态对应，再结合本地时钟和任务状态门控。
video_valid = receiver["valid"]
```

| receiver字段 | 语义 |
|---|---|
| valid / reason | 服务端计算的有效性与原因；默认无报告为false |
| session_id / ingress_connection_id | 当前XR session及服务端绑定的姿态入口；不是任意浏览器的状态 |
| peer_id / stream_generation | 当前视频接收peer与视频源代次 |
| status_seq / pose_seq | 独立递增状态序号及发送时的姿态序号 |
| video_enabled / xr_active | 页面视频开关、XR会话处于visible状态 |
| decoded_age_ms / rendered_age_ms | 页面报告的帧年龄加上控制服务器本地已等待时间；未观察到则null |
| rendered_eyes | 该次报告观察到的双眼成功绘制；缺眼不能有效 |
| report_age_ms / received_monotonic / timeout_ms | 服务端本地单调时钟收包年龄、收包时刻及超时阈值 |
| status / evidence | 页面提示文案；证据类型固定为browser_decode_and_draw_report |

状态复用当前姿态RTCDataChannel，使用有严格schema和4096字节上限的JSON
`type=video_receiver_status`；QCRT仍为v5/804字节。先发送姿态，再附带最新状态，
不积压状态或改变姿态发送优先级。正常XR绘制后约每50ms上报，断开/退出会撤销；
状态丢失或XR回调停止时，服务端按本地100ms超时失效。
这两个数值是待现场确认的工程默认值，不是实测PRD结论。

只有当前姿态连接可以上报；旧peer/session/源代次、重复或倒序状态不续期。
同页面姿态时间戳与报告时间戳偏差最多100ms，序号允许8帧以内异步处理偏差，
避免把后台姿态处理线程尚未发布的新帧错误当作未知源。
未收到报告、姿态来源已结束/变化、视频worker退出、视频关闭、XR不可见、
解码或源帧超过250ms、绘制超过100ms或缺眼时，valid为false。

`valid` 证明的是**浏览器报告的解码/双眼绘制路径**。它不证明物理面板正常、
操作者真正看到了画面、镜头未遮挡，或画面内容不是黑色；这些仍需现场验收。
宿主仍需监督自己收到状态的本地年龄，不能把缓存的true当作永久运动许可。
SDK不自动授予机器人运动授权，也不规定操作者恢复交互。

## 通用单目示例

```python
from quest_xr_bridge import QuestServer, VideoConfig, VideoDisplayConfig

with QuestServer() as service:
    service.set_video_display(VideoDisplayConfig(projection="plane"))
    service.start_video(VideoConfig(width=1280, height=720, mode="mono", fps=60))
    for rgb, capture_ns in your_mono_camera:
        service.submit_video(rgb, timestamp_ns=capture_ns)
```

your_mono_camera由宿主提供；左右眼都观看同一幅图，不提供真实双目视差。
冻结双目示例见[使用文档](USAGE.md)，可从wheel导入 `quest_xr_bridge.examples.frozen_stereo`。

## 协议与坐标公共工具

以下工具从 `quest_xr_bridge` 顶层导出，不会启动服务：

| 接口 | 返回/用途 |
|---|---|
| encode_pose_packet(frame: Mapping) -> bytes | 校验完整Pose v5并编码804字节QCRT |
| decode_pose_packet(packet: bytes/bytearray/memoryview) -> dict | 严格解码v5；旧包、异常浮点数、保留位等抛ValueError |
| AxisTransform(axes) | 不可变有符号轴排列，每个x/y/z恰好使用一次 |
| remap_axes(axes) -> AxisTransform | 例如('-z','-x','y')定义输出三轴 |
| flip_axis(axis) -> AxisTransform | 仅翻转x/y/z中的一轴 |
| transform_pose_frame(frame, transform) -> dict | 返回变换后的深拷贝，不改输入 |
| quaternion_to_matrix(q) -> tuple | XYZW四元数转3×3旋转矩阵 |
| matrix_to_quaternion(matrix) -> tuple | 3×3旋转矩阵转XYZW四元数 |
| wrist_local_to_world(point, wrist_position, wrist_orientation) -> tuple | 腕局部点转所属世界/身体坐标 |
| world_to_wrist_local(point, wrist_position, wrist_orientation) -> tuple | 上述逆变换 |
| to_hts_wrist_relative_frame(frame) -> dict | 将手部点转为各自腕局部坐标的输出副本 |
| COORDINATE_PRESETS | 只读预设映射：body、webxr、rfu、flu |
| DEFAULT_COORDINATE_PRESET | 默认body |

AxisTransform提供matrix、determinant、changes_handedness只读属性，
以及apply(point)、apply_orientation(quaternion)方法。常见输入错误抛ValueError或TypeError；纯数学工具不替代完整网络协议校验。
姿态编码还可能向调用方返回pydantic.ValidationError（也是ValueError子类）。
协议字段和字节偏移见[工程手册](reference/ARCHITECTURE.md)。

## 姿态WebRTC信令（当前源码）

`POST /api/webrtc/offer` 接收 `sdp`、`type="offer"`，以及可选的成对字段：

| 字段 | 类型/约束 | 用途 |
|---|---|---|
| client_token | UUID字符串，最长36字符 | 页面随机生成且只保存在内存的接管凭据 |
| attempt_id | 严格整数，1至2^53−1 | 同页面每次协商递增，识别过期/取消请求 |

缺少整对字段的旧请求仍支持初次连接，不具备接管能力；只提供一个字段或字段非法返回422。
返回 `sdp`、`type="answer"` 和本次服务的 `server_epoch`。相同凭据的更高代次撤销旧连接再预留新连接；
其他来源占用、过期/取消代次或协商被新请求替换返回409，detail区分原因。
旧源停止发布的暂行预算为1秒，超时503；失败时不建立第二个活动源。
SDP协商和未完成数据通道握手仍各有10秒限制，不作为已建立旧peer的失联判定。

`POST /api/webrtc/close` 接收必填 `client_token` 和 `attempt_id`，约束同上。
仅当前凭据与代次同时匹配才关闭，返回 `{"closed": true}`；重复、迟到或尚无对应连接时
返回 `{"closed": false}`，同时记录取消代次，防止close先到、offer后到时重新占用。
最近64个页面的最高代次被保留；当前连接额外核对自身代次。
close和offer的源撤销/预留串行化，后台资源收尾不占用信令锁。
凭据不在响应、健康信息或姿态JSON中公开；持有凭据即可接管，不能视为设备认证。

`WSS /ws` JSON附加 `ingress_connection_id`（服务端生成的UUID），每个姿态源连接不同，
同源帧之间不变。它在下一有效新帧中反映来源切换，不是即时失效事件。
消费者必须在编号变化时暂停并重建姿态来源；同一已授权 XR 内的新鲜输入可自动限速恢复，不需人体对齐或返回 PC 确认。
QCRT输入布局保持v5/804字节。已冻结0.3.1产物未包含本次信令与JSON补丁。


## STAGE1 驾驶授权事件（0.3.1+stage1.3 本地兼容构建）

点击 Quest 的“进入驾驶 / Start prep”后，为该 XR session 建立独立
`authorization_id`。RTC `driving_event` 包含 `event=enter|exit`、UUID
`session_id` / `authorization_id`、严格递增 `seq`、`reason`。
`XRSession.end`、页面隐藏/pagehide 尽力发送退出：RTC 与
`POST /api/driving/exit`；退出通知不保证必达。PC STOP 也可调用
`QuestServer.revoke_driving(session_id, authorization_id)`，该接口只撤销。
`GET /api/driving/authority` 与 `/ws` 返回 `driving_authority`：
`server_epoch`、`generation`、`active`、会话与授权 ID、原因。
姿态、视频及传输健康不授予驾驶权限。同一授权同一 XR 的网络重连保留
授权；结束后旧授权不能复活，新 XR 必须明确点击进入。50/100ms
新鲜度与最终发送门控仍由消费端监督，恢复限速追随当前人体。

离线只验证浏览器 XR end/visibilitychange/pagehide 的事件契约。
Quest 系统“右手掌面对自己对指、退出 web app”实际触发什么事件未现场验证，
这是软件停止入口候选，不是物理急停。若退出通知丢失且会话结束没有证据，
PC 无法从单纯断流区分临时断网与主动退出：先超时保持，同一会话数据恢复
仍按授权恢复。新会话无旧授权。

## STAGE2 授权乱序修复（0.3.1+stage2.1 本地兼容构建）

seq 在同一页面的所有 XR 会话中递增，不在新 XR 归零；旧 entry 序号被拒绝。
每个 driving_event 必须包含信令返回的 server_epoch、页面 client_token 与当前 attempt_id。
RTC 只接受当前 peer/claim；HTTP 页面退出只接受已登记的最新 claim；PC 负向 STOP
通过 loopback HTTP（同一 server_epoch）或 SDK revoke_driving 调用。该 token 是随机
能力凭据，不是设备认证；只在信任网络部署，不在 WSS/健康快照公开。

来源合法的退出即使早于 enter，也撤销该 session_id + authorization_id。
撤销另一授权不会改变当前活动授权。重复退出幂等，迟到 enter 永不复活。
撤销集合保留至整个服务 epoch 结束，没有 64 条淘汰；服务重启生成新 epoch，
旧事件被拒绝。内存随本次服务结束的授权数量增长（每次 XR 一项），不能删除仍
可能通过 HTTP/重连迟到的记录。旧页面 claim 淘汰后不能 HTTP 撤销，负向记录
仍保留；重新接入也不能恢复已结束的授权。同 XR 正常 ingress/WSS 重连不撤销。
退出通知全丢时仍无法区分临时失联与主动退出；控制端超时 HOLD，新 XR 必须明确进入。
