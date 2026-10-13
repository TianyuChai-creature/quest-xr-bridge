# Quest XR Bridge 0.3.1+stage2.1

明确 XR 驾驶授权与 pose/传输状态分开：enter 与 exit 绑定服务 epoch、页面 claim、
XR session 和 authorization_id。合法 RTC/HTTP exit 先到时也撤销，迟到 enter
不能复活。撤销记录保留整个服务 epoch，不因64条淘汰重新授予旧授权；页面序号
跨 XR 递增。旧 peer/attempt/epoch 拒绝，不同授权隔离。HTTP 主机 STOP 只允许
loopback 且为负向操作；页面退出需当前已登记 claim。

同一已授权 XR 的临时网络/追踪/视频中断保留授权；消费端继续本地新鲜度和最终
发送门控，条件恢复后按既有速率追随。新 XR 必须明确进入。退出通知全部丢失且
无其他结束证据时，控制端不能仅从断流推断意图。实际 Quest 手势是否产生
XR end、隐藏或 pagehide 仍需现场观察。

pose offer answer 新增 server_epoch；向 RTCSessionDescription 只传 sdp/type。
DrivingEvent 新增 server_epoch、client_token/attempt_id，旧页面须刷新到本次静态资源。
QCRT v5/804字节、坐标、关节语义、视频预处理/编码与显示几何不变。

标准 CPython3.13：相关 Python 授权/协议/重连用例28项及浏览器逻辑检查通过；
源码目录外安装验证实际 site-packages 来源和全部 wheel 文件。发布附带 wheel、
BUILD-INFO.json 与 SHA256SUMS，Git 内 releases/0.3.1+stage2.1 保存同一产物。
Source build commit 与包含产物的提交分开记录，不把旧基线当作未修改源码。

本次没有连接机器人、相机或头显，没有运行原生 GPU 视频；浏览器报告有效不证明
实体面板、图像内容或机器人运动安全。原生依赖准备与实际链路验收仍待完成。
