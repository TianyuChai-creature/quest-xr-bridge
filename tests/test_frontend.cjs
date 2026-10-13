const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

async function checkPose() {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8")
  const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/^\s*import .*$/m, "").replace(/\s+connectTransport\(\)\s*$/, "")
  let now = 0, loseCount = 0, disposeCount = 0, failGL = false, fillSendBuffer = false
  const events = {}, sessions = [], packets = [], channelMessages = [], rendered = [], videoInstances = [], drawOrder = []
  const elements = new Map()
  function element() {
    return { textContent: "", disabled: false, checked: false, events: {},
      classList: { add() {}, remove() {} },
      addEventListener(name, handler) { this.events[name] = handler },
      contains: () => false, querySelector: () => element(),
      replaceWith(canvas) { elements.set("#xr-canvas", canvas) }, }
  }
  const gl = { async makeXRCompatible() { if (failGL) throw new Error("GL failure") },
    getExtension: () => ({ loseContext() { loseCount++ } }) }
  const posePeer = { close() { this.closed = true } }
  const poseChannel = { readyState: "open", bufferedAmount: 0,
    send(packet) {
      channelMessages.push(packet)
      if (typeof packet !== "string") packets.push(packet)
      if (fillSendBuffer) this.bufferedAmount = 804
    }, close() { this.closed = true } }
  const context = vm.createContext({ console, performance: { now: () => now, timeOrigin: 1000 },
    crypto: { randomUUID: () => "407b7a3f-4791-479c-aa81-48e813aca057" },
    setTimeout, clearTimeout, AbortController, AbortSignal,
    document: { activeElement: null,
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, element())
        return elements.get(selector)
      },
      createElement: () => ({ ...element(), id: "", getContext: () => gl }),
    },
    window: { addEventListener(name, handler) { events[name] = handler } },
    location: { reload() {} },
    VideoReturn: class {
      constructor({ onStatus, onConfig, onReceiverStatus }) {
        this.onStatus = onStatus; this.onConfig = onConfig; this.onReceiverStatus = onReceiverStatus
        this.enabled = false; videoInstances.push(this)
      }
      setEnabled(enabled) { this.enabled = enabled }
      setStatus(status) { this.status = status; this.onStatus(status) }
      tick() {}
      beginFrame(frame) { this.frame = frame; drawOrder.push("video prepare") }
      renderView() { drawOrder.push("video") }
      endFrame() { drawOrder.push("video report") }
      setSession(session, layer, space, context, sessionId) {
        this.session = session; this.gl = context
        if (!session) this.onReceiverStatus({type: "video_receiver_status", status_seq: 3,
          session_id: this.sessionId, video_enabled: this.enabled, xr_active: false,
          decoded_age_ms: null, rendered_age_ms: null, rendered_eyes: []})
        this.sessionId = sessionId
      }
      close() { this.closed = true }
    },
    XRWebGLLayer: class {
      constructor(session, actualGL, options) {
        assert.equal(actualGL, gl)
        assert.equal(options.ignoreDepthValues, true, "RGB supplies no scene depth for compositor reprojection")
        assert.equal(options.alpha, false)
        assert.equal(options.antialias, false)
      }
    },
    navigator: { xr: {
      isSessionSupported: async () => true,
      async requestSession(mode, options) {
        assert.equal(mode, "immersive-vr")
        assert(options.requiredFeatures.includes("body-tracking"))
        assert.equal(options.optionalFeatures, undefined)
        const session = { inputSources: [], events: {}, enabledFeatures: [],
          addEventListener(name, handler) { this.events[name] = handler },
          requestReferenceSpace: async type => ({ type }),
          updateRenderState(state) {
            assert(state.baseLayer instanceof context.XRWebGLLayer)
            assert.equal(state.layers, undefined)
            this.renderState = state
          },
          requestAnimationFrame(callback) { this.frame = callback },
          async end() { this.ended = true; this.events.end() }, }
        sessions.push(session)
        return session
      },
    } },
    posePeer, poseChannel,
    rendererFactory: () => ({ render(...args) { drawOrder.push("pose"); rendered.push(args.at(-1)) }, dispose() { disposeCount++ } }),
  })
  vm.runInContext(script + "\ncreateXRRenderer = rendererFactory; dataChannel = poseChannel; peerConnection = posePeer", context)
  await vm.runInContext("Promise.all([enterXR(), enterXR()])", context)
  assert.equal(sessions.length, 1, "duplicate taps request only one XR session")
  const session = sessions[0]
  assert(session.renderState.baseLayer instanceof context.XRWebGLLayer)
  assert.equal(session.renderState.baseLayer.fixedFoveation, 0, "keep peripheral camera detail at minimum native foveation")
  const body = new Map([
    ["spine-upper", [0, 1, 0]], ["left-scapula", [-0.15, 0.9, 0]], ["right-scapula", [0.15, 0.9, 0]],
    ["left-arm-upper", [-0.2, 0.9, 0]], ["right-arm-upper", [0.2, 0.9, 0]],
    ["left-arm-lower", [-0.2, 0.5, 0]], ["right-arm-lower", [0.2, 0.5, 0]],
  ])
  const frame = { body,
    getPose(point) { return { transform: { position: { x: point[0], y: point[1], z: point[2] },
      orientation: { x: 0, y: 0, z: 0, w: 1 } } } },
    getViewerPose() { return { transform: { orientation: { x: 0, y: 0, z: 0, w: 1 } } } },
  }
  vm.runInContext('videoReturn.setEnabled(true); videoReturn.setStatus("Camera stale"); dataChannel.readyState = "closed"; setConnectionStatus("WebRTC: channel timeout")', context)
  now = 1000; session.frame(now, { ...frame, body: undefined })
  assert.equal(rendered.at(-1).countdown, 2)
  for (const message of ["Camera stale", "Pose disconnected", "channel timeout", "Body tracking unavailable", "Hold a pose"]) {
    assert(rendered.at(-1).hint.includes(message), "faults remain visible together during preparation: " + message)
  }
  assert.equal(elements.get("#video-status").textContent, "Camera stale")
  assert.equal(packets.length, 0)
  vm.runInContext('videoReturn.setEnabled(false); dataChannel.readyState = "open"; setConnectionStatus("WebRTC open")', context)
  drawOrder.length = 0
  now = 2999; session.frame(now, frame)
  assert.equal(videoInstances[0].gl, gl, "video shares the existing XR WebGL context")
  assert.equal(videoInstances[0].frame, frame)
  assert.deepEqual(drawOrder, ["video prepare", "pose", "video report"], "report after pose/HUD rendering")
  const renderSource = html.slice(html.indexOf("const cameraPose ="), html.indexOf("function cleanupXR"))
  assert(renderSource.indexOf("videoReturn.renderView(view, cameraView)") < renderSource.indexOf("drawVertices(lineVertices"))
  assert(renderSource.includes("candidate.eye === view.eye"))
  assert(renderSource.includes("trackingGuides.checked || !videoMode.checked"))
  assert.equal(packets.length, 0, "three-second preparation sends no pose")
  const pending = {type: "video_receiver_status", status_seq: 1,
    session_id: videoInstances[0].sessionId, video_enabled: true, xr_active: true,
    decoded_age_ms: 0, rendered_age_ms: 0, rendered_eyes: ["left", "right"]}
  videoInstances[0].onReceiverStatus(pending)
  assert.equal(channelMessages.length, 0, "positive status waits for a corresponding pose")
  fillSendBuffer = true
  now = 3001; session.frame(now, frame)
  fillSendBuffer = false; poseChannel.bufferedAmount = 0
  assert.equal(packets.length, 1)
  assert.equal(channelMessages.length, 2)
  assert.equal(channelMessages[0], packets[0], "binary pose has priority over the report")
  const reported = JSON.parse(channelMessages[1])
  assert.equal(reported.type, "video_receiver_status")
  assert.equal(reported.pose_seq, new DataView(packets[0]).getUint32(8, true))
  assert.equal(reported.session_id, "407b7a3f-4791-479c-aa81-48e813aca057")
  assert.equal(packets[0].byteLength, 804)
  assert.equal(new DataView(packets[0]).getUint8(5) & 0x80, 0)
  now = 3100; session.frame(now, { ...frame, body: undefined })
  assert.equal(packets.length, 1)
  assert.equal(elements.get("#xr-status").textContent, "Body tracking unavailable")
  assert(rendered.at(-1).hint.includes("Body tracking unavailable"))
  now = 3200; session.frame(now, frame)
  assert.equal(packets.length, 2)
  const video = videoInstances[0]
  video.setEnabled(true); video.frameReady = false; video.setStatus("Camera stale")
  now = 3250; session.frame(now, frame)
  assert(rendered.at(-1).hint.includes("Camera stale"))
  assert.equal(packets.length, 3, "a video fault still sends fresh pose data")
  assert.equal(posePeer.closed, undefined)
  video.frameReady = true; video.setStatus("Stereo camera live")
  now = 3260; session.frame(now, frame)
  assert.equal(rendered.at(-1).show, false, "fresh video clears its warning")
  video.setEnabled(false); video.frameReady = false
  now = 3270; session.frame(now, frame)
  assert.equal(rendered.at(-1).show, false, "deliberately disabled video does not warn")
  const beforeStall = packets.length
  poseChannel.bufferedAmount = 804
  now = 3300; session.frame(now, frame)
  now = 3349; session.frame(now, frame)
  assert.equal(rendered.at(-1).show, false)
  now = 3350; session.frame(now, frame)
  assert(rendered.at(-1).hint.includes("Pose send stalled"), "local backpressure warns at the provisional 50ms threshold")
  now = 3551; session.frame(now, frame)
  assert(rendered.at(-1).hint.includes("Pose send stalled"))
  assert.equal(packets.length, beforeStall, "backpressure still drops rather than queues old poses")
  poseChannel.bufferedAmount = 0
  now = 3552; session.frame(now, frame)
  assert.equal(rendered.at(-1).show, false)
  assert.equal(packets.length, beforeStall + 1)
  poseChannel.readyState = "closed"
  vm.runInContext('setConnectionStatus("WebRTC connecting")', context)
  now = 3600; session.frame(now, frame)
  assert(rendered.at(-1).hint.includes("Pose disconnected"))
  assert.equal(packets.length, beforeStall + 1)
  poseChannel.readyState = "open"
  now = 3610; session.frame(now, frame)
  assert.equal(rendered.at(-1).show, false)
  video.setEnabled(true); video.setStatus("Waiting for camera frames")
  poseChannel.readyState = "closed"
  vm.runInContext('videoSettings.contains = () => true', context)
  video.onConfig({})  // A successful PC control response starts recovery timing even while editing settings.
  now = 13609; session.frame(now, frame)
  assert(!rendered.at(-1).hint.includes("recovery failed"))
  now = 13610; session.frame(now, frame)
  assert(rendered.at(-1).hint.includes("Camera recovery failed (10s)"))
  assert(rendered.at(-1).hint.includes("Pose recovery failed (10s)"))
  poseChannel.readyState = "open"; video.frameReady = true
  now = 13611; session.frame(now, frame)
  assert.equal(rendered.at(-1).show, false)
  await session.end()
  const afterExit = channelMessages.length
  assert.equal(JSON.parse(channelMessages.at(-1)).xr_active, false, "XR exit sends a one-off revocation")
  video.onReceiverStatus({ ...pending, status_seq: 4, xr_active: false, rendered_eyes: [] })
  assert.equal(channelMessages.length, afterExit, "idle negative reports cannot periodically occupy the pose channel")
  assert.equal(videoInstances[0].session, null)
  assert.equal(disposeCount, 1)
  assert.equal(loseCount, 1)
  assert.equal(posePeer.closed, undefined, "ending XR keeps the pose connection ready for another session")

  failGL = true
  await vm.runInContext("enterXR()", context)
  assert.equal(sessions[1].ended, true, "failed initialization ends the acquired XR session")
  assert.equal(loseCount, 2)
  assert(elements.get("#xr-status").textContent.includes("GL failure"))
  assert(!elements.get("#xr-status").textContent.includes("body tracking required"))
  failGL = false
  await vm.runInContext("enterXR()", context)
  assert(sessions[2].renderState.baseLayer instanceof context.XRWebGLLayer)
  assert.equal(sessions[2].renderState.layers, undefined)
  await sessions[2].end()
  events.pagehide()
  assert.equal(posePeer.closed, true)
  assert.equal(poseChannel.closed, true)
  assert.equal(videoInstances[0].closed, true)
  assert(!html.includes("connectWebSocket"))
}

function checkViewer() {
  const source = fs.readFileSync(path.join(__dirname, "../static/viewer.html"), "utf8")
  const clear = source.slice(source.indexOf("function clearPose("), source.indexOf("function resize("))
  const draw = source.slice(source.indexOf("function draw()"), source.indexOf("context.clearRect", source.indexOf("function draw()"))) + "}"
  const context = { frame: { seq: 9 }, projectedPoints: [1], framesThisInterval: 10,
    lastFrameReceivedAt: 0, performance: { now: () => 251 } }
  for (const name of ["connectionText", "sessionText", "sequenceText", "axesText", "handednessText", "fpsText", "pointsText", "coordinatesSeq", "coordinatesBody"]) context[name] = {}
  context.hoverLabel = { style: { display: "block" }, textContent: "old point coordinates" }
  vm.runInNewContext(clear + draw + "\ndraw()", context)
  assert.equal(context.frame, null)
  assert.equal(context.connectionText.textContent, "stale")
  assert.equal(context.sequenceText.textContent, "—")
  assert(context.coordinatesBody.innerHTML.includes("Waiting for fresh"))
  assert.equal(context.hoverLabel.style.display, "none")
  assert.equal(context.hoverLabel.textContent, "")
  context.frame = { seq: 10 }
  vm.runInNewContext(clear + '\nclearPose("closed")', context)
  assert.equal(context.frame, null)
  assert.equal(context.connectionText.textContent, "closed")
}

function checkCameraAndGuidesDrawOrder() {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8")
  const start = html.indexOf("          render(\n")
  const stop = html.indexOf("          },\n        }", start)
  const events = [], left = { eye: "left" }, right = { eye: "right" }
  const views = [left, right].map(view => ({ ...view, projectionMatrix: [view.eye],
    transform: { inverse: { matrix: [] } } }))
  const referenceSpace = {}, viewerSpace = {}, headMatrix = ["shared head pose"], hudAnchors = []
  const guides = { checked: false }
  const gl = { FRAMEBUFFER: 1, COLOR_BUFFER_BIT: 2, DEPTH_BUFFER_BIT: 4,
    bindFramebuffer() {}, disable() {}, colorMask() {}, depthMask() {}, clearColor() {},
    clear() { events.push("clear") }, enable() {}, blendFunc() {}, viewport() {} }
  const scope = { gl, viewerSpace, trackingGuides: guides, videoMode: { checked: true },
    glLayer: { framebuffer: {}, getViewport: () => ({ x: 0, y: 0, width: 1, height: 1 }) },
    XR_COLORS: {}, addHand(vertices, points) { vertices.push("guide"); points.push("guide") }, addElbow() {}, addShoulder() {},
    videoReturn: { renderView(view, cameraView) {
      assert.equal(view.eye, cameraView.eye)
      events.push("video " + view.eye)
    } },
    multiplyMatrices: projection => projection,
    drawVertices(vertices, mode, projection) {
      assert.equal(vertices.length, guides.checked ? 2 : 0)
      events.push("pose " + projection[0])
    },
    lastHudKey: "", paintHud() {},
    drawHudQuad(_projection, matrix) { hudAnchors.push(matrix) },
  }
  const render = vm.runInNewContext("({" + html.slice(start, stop) + "}}).render", scope)
  const frame = { getViewerPose(space) { return {
    views: space === referenceSpace ? views : [right, left], transform: { matrix: headMatrix },
  } } }
  render(frame, referenceSpace, [], [], null, null, null, null, { show: false })
  assert.deepEqual(events, ["clear", "video left", "pose left", "pose left", "video right", "pose right", "pose right"])
  guides.checked = true
  render(frame, referenceSpace, [], [], null, null, null, null, { show: false })
  render(frame, referenceSpace, [], [], null, null, null, null, { show: true, countdown: 1, hint: "Prep" })
  assert.deepEqual(hudAnchors, [headMatrix, headMatrix], "both eyes project the same head-anchored HUD geometry")
}

async function checkPoseReconnect() {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8")
  const source = html.slice(html.indexOf("let peerConnection = null"), html.indexOf("function readPose("))
  const calls = [], peers = [], timers = new Map(), session = {}, token = "407b7a3f-4791-479c-aa81-48e813aca057"
  let releaseOffer, rejectOffer = false
  class Peer {
    constructor() {
      this.events = {}; peers.push(this)
      this.channel = { readyState: "open", events: {},
        addEventListener(name, fn) { this.events[name] = fn },
        close() { this.readyState = "closed"; this.events.close?.() }, }
    }
    createDataChannel(label, options) {
      assert.equal(label, "pose"); assert.equal(options.ordered, false)
      assert.equal(options.maxPacketLifeTime, 30)
      return this.channel
    }
    addEventListener(name, fn) { this.events[name] = fn }
    async createOffer() { return { sdp: "offer", type: "offer" } }
    async setLocalDescription(value) { this.localDescription = value }
    async setRemoteDescription(value) { this.remoteDescription = value }
    close() { this.closed = true }
  }
  const context = vm.createContext({ crypto: { randomUUID: () => token },
    RTCPeerConnection: Peer, RTC_PACKET_LIFETIME_MS: 30, AbortController, AbortSignal,
    enterButton: {}, sessionPhaseEl: {}, transportStatus: {}, session,
    setTimeout(fn, ms) { const id = {}; timers.set(id, { fn, ms }); return id },
    clearTimeout(id) { timers.delete(id) }, waitForIceGathering: async () => {},
    offerError: async () => new Error("pose source is occupied by another page"),
    async fetch(url, options) {
      const body = JSON.parse(options.body)
      calls.push({ url, body, keepalive: options.keepalive })
      if (url.endsWith("close")) throw new Error("offline close notification")
      if (body.attempt_id === 3) await new Promise(resolve => { releaseOffer = resolve })
      return { ok: !rejectOffer, json: async () => ({ sdp: "answer", type: "answer" }) }
    },
  })
  vm.runInContext(source + "\nxrSession = session; seq = 42", context)
  await vm.runInContext("connectTransport()", context)
  assert.deepEqual(calls[0].body, { sdp: "offer", type: "offer", client_token: token, attempt_id: 1 })
  const old = peers[0]
  old.channel.close()
  assert.deepEqual(calls.at(-1), { url: "/api/webrtc/close", body: { client_token: token, attempt_id: 1 }, keepalive: true })
  const retry = [...timers.values()].find(timer => timer.ms === 1000)
  assert(retry, "a lost close notification still schedules reconnection")
  await retry.fn()
  assert.equal(calls.at(-1).body.attempt_id, 2)
  assert.equal(calls.at(-1).body.client_token, token)
  old.connectionState = "failed"; old.events.connectionstatechange(); old.channel.events.close()
  assert.equal(peers[1].closed, undefined, "late old callbacks cannot close the new peer")
  const pending = vm.runInContext("connectTransport()", context)
  await new Promise(setImmediate)
  assert(releaseOffer)
  await vm.runInContext("connectTransport()", context)
  releaseOffer(); await pending
  assert.equal(vm.runInContext("poseClaim.attempt_id", context), 4)
  assert.equal(peers[2].remoteDescription, undefined, "a superseded answer is ignored")
  assert.equal(peers[3].closed, undefined)
  assert.equal(vm.runInContext("xrSession", context), session)
  assert.equal(vm.runInContext("seq", context), 42, "reconnection keeps XR session and sequence")
  rejectOffer = true
  await vm.runInContext("connectTransport()", context)
  assert.equal(calls.at(-1).url, "/api/webrtc/close")
  assert.equal(calls.at(-1).body.attempt_id, 5)
  assert(vm.runInContext("transportStatus.textContent", context).includes("occupied"))
  vm.runInContext("pageLeaving = true; clearTimeout(reconnectTimer); closeCurrentTransport()", context)
  assert.equal(timers.size, 0)
}

function checkHudTextLayout() {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8")
  const source = html.slice(html.indexOf("function paintHud("), html.indexOf("function roundRect("))
  const text = [], hudCtx = { clearRect() {}, fill() {}, stroke() {},
    measureText: value => ({ width: value.length * 23 }),
    fillText(value, x, y, width) { text.push({ value, x, y, width }) },
  }
  const scope = { hudCanvas: { width: 1024, height: 512 }, hudCtx, hudTexture: {}, roundRect() {},
    gl: { bindTexture() {}, texImage2D() {} } }
  vm.runInNewContext(source + '\npaintHud(2, "Pose disconnected: WebRTC channel timeout\\nCamera video unavailable: decoded frame updates unsupported\\nBody tracking unavailable\\nKeep your upper body in view\\nHold a pose close to the robot arms\\nDo not tap the UI")', scope)
  assert.equal(text[0].value, "2")
  assert(text.length > 6, "long warnings wrap")
  for (const line of text) assert(line.y > 40 && line.y < 472, "all warning lines stay inside the HUD panel")
  assert.equal(text.slice(1).map(line => line.value).join(" "), "Pose disconnected: WebRTC channel timeout Camera video unavailable: decoded frame updates unsupported Body tracking unavailable Keep your upper body in view Hold a pose close to the robot arms Do not tap the UI")
}

checkPose().then(async () => {
  await checkPoseReconnect()
  checkCameraAndGuidesDrawOrder()
  checkHudTextLayout()
  checkViewer()
  console.log("XR fault HUD, pose takeover, isolation, cleanup and viewer freshness checks passed")
}).catch(error => { console.error(error); process.exitCode = 1 })
