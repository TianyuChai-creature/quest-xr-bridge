# Quest XR Bridge 0.3.1

头显侧解码与双眼绘制状态通过现有姿态RTCDataChannel回传，并通过
`QuestServer.video_status()`、`GET /api/video/status` 及现有health/config接口提供。
状态绑定当前姿态入口、XR session、视频peer及源代次；本地单调超时、重复和倒序
报告检查防止残留true。保持QCRT v5/804字节、重定向输入、坐标/单位、视频编码和
冻结显示几何不变；姿态先发送，状态不占用独立通道。

此版本还包含v0.3.0发布之后既有的同页重连接管和XR故障提示修复，
构建基线为f92ecfb78ce64ce85cdd0d6ac6044535287ca37c。

验证仅运行此次相关的Python接收端、RTC分流/重连、视频peer和SDK生命周期测试，
以及前端视频/XR测试；没有完整重跑全仓库测试。构建和安装检查使用标准CPython3.13。
完整命令、源码提交及wheel SHA256见Release中的BUILD-INFO.json和SHA256SUMS。

没有连接真实Quest、相机或机器人，没有执行原生GPU视频。
receiver.valid表示浏览器报告的解码和绘制路径，不保证物理面板、图像内容或运动安全。
默认50ms上报/100ms状态超时是待现场确认的工程参数。
