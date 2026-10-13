"""HTTP and WebRTC connections for one SDK-owned runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from quest_xr_bridge.driving import DrivingEvent

from quest_xr_bridge.coordinates import COORDINATE_PRESETS, remap_axes
from quest_xr_bridge.pose import (
    CoordinateTransformRequest,
    PosePeerClose,
    VideoPeerClose,
    VideoReceiverStatus,
    WebRTCPoseOffer,
    WebRTCVideoOffer,
)
from quest_xr_bridge.runtime import PoseRuntime, PoseSourceBusyError, PoseStreamProcessor
from quest_xr_bridge.telemetry import format_status_line
from quest_xr_bridge.video import VideoDisplayConfig, VideoManager, VideoUnavailableError

logger = logging.getLogger(__name__)
POSE_HANDSHAKE_TIMEOUT = 10
POSE_SOURCE_RELEASE_TIMEOUT = 1
_PACKAGED_STATIC = Path(__file__).parent / "static"
STATIC_DIR = (
    _PACKAGED_STATIC if _PACKAGED_STATIC.is_dir() else Path(__file__).parent.parent / "static"
)


def remember_pose_attempt(runtime: PoseRuntime, claim: PosePeerClose | WebRTCPoseOffer) -> None:
    if claim.client_token is None:
        return
    token, attempt = claim.client_token, claim.attempt_id
    runtime.pose_attempts[token] = max(runtime.pose_attempts.get(token, 0), attempt)
    runtime.pose_attempts.move_to_end(token)
    # shortcut: retain 64 page claims; increase if deployments need more retired pages.
    if len(runtime.pose_attempts) > 64:
        runtime.pose_attempts.popitem(last=False)


async def retire_pose_peer(runtime: PoseRuntime, peer: RTCPeerConnection) -> asyncio.Task | None:
    """Called under pose_lock; release ingress before background transport/log cleanup."""
    if peer in runtime.peer_closures:
        return runtime.peer_closures[peer]
    if peer not in runtime.peers:
        return None
    processor = runtime.processors.get(peer)
    if processor is not None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(processor.close), timeout=POSE_SOURCE_RELEASE_TIMEOUT
            )
        except TimeoutError as exc:
            raise HTTPException(503, "pose source release timed out") from exc
        runtime.processors.pop(peer, None)
    deadline = runtime.peer_timeouts.pop(peer, None)
    if deadline is not None and deadline is not asyncio.current_task():
        deadline.cancel()
    if runtime.pose_peer is peer:
        runtime.pose_peer = None
    runtime.peer_claims.pop(peer, None)

    async def finish() -> None:
        try:
            if peer.connectionState != "closed":
                await peer.close()
        finally:
            if processor is not None:
                await asyncio.to_thread(processor.wait_closed)

    def finished(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Pose peer cleanup failed", exc_info=error)
            return
        runtime.peers.discard(peer)
        runtime.peer_closures.pop(peer, None)

    cleanup = asyncio.create_task(finish())
    runtime.peer_closures[peer] = cleanup
    cleanup.add_done_callback(finished)
    return cleanup


async def close_pose_peer(
    runtime: PoseRuntime, peer: RTCPeerConnection, *, wait: bool = True
) -> None:
    async with runtime.pose_lock:
        cleanup = await retire_pose_peer(runtime, peer)
    if wait and cleanup is not None:
        await asyncio.shield(cleanup)


async def _report_status(runtime: PoseRuntime) -> None:
    while True:
        await asyncio.sleep(1)
        if runtime.active_source.describe()["active"]:
            logger.info(format_status_line(runtime.health()))


def video_status(runtime: PoseRuntime | None, video: VideoManager, *,
                 receiver_timeout_s: float = 0.1) -> dict[str, Any]:
    """Video status with source binding; never a physical-display guarantee."""
    status = video.snapshot(receiver_timeout_s=receiver_timeout_s)
    receiver = status["receiver"]
    _generation, pose, published = (runtime.latest_pose.snapshot() if runtime else (0, None, None))
    processor = runtime.processors.get(runtime.pose_peer) if runtime else None
    if (processor is None or pose is None or published is None
            or processor.connection_id != receiver.get("ingress_connection_id")
            or time.monotonic()-published >= receiver_timeout_s
            or receiver.get("session_id") != pose.get("session_id")
            or receiver.get("ingress_connection_id") != pose.get("ingress_connection_id")):
        receiver.update(valid=False, reason="pose_source_unavailable_or_changed")
    return status


def receive_video_status(runtime: PoseRuntime, video: VideoManager,
                         peer: RTCPeerConnection, payload: dict[str, Any]) -> bool:
    processor = runtime.processors.get(peer)
    if runtime.pose_peer is not peer or processor is None:
        return False
    try:
        report = VideoReceiverStatus.model_validate(payload)
    except ValidationError:
        return False
    _generation, pose, published = runtime.latest_pose.snapshot()
    if report.session_id is not None:
        if (pose is None or published is None or time.monotonic()-published >= 0.1
                or str(report.session_id) != pose.get("session_id")
                or pose.get("ingress_connection_id") != processor.connection_id
                or abs(report.timestamp_ms-pose["timestamp_ms"]) > 100
                or report.pose_seq == 0 or abs(report.pose_seq-pose["seq"]) > 8):
            return False
    elif report.xr_active:
        return False
    return video.report_receiver(report.model_dump(mode="json"), processor.connection_id)


def create_app(runtime: PoseRuntime, video: VideoManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        lag_task = asyncio.create_task(runtime.event_loop_lag.monitor())
        status_task = asyncio.create_task(_report_status(runtime))
        try:
            yield
        finally:
            for task in (lag_task, status_task):
                task.cancel()
            await asyncio.gather(lag_task, status_task, return_exceptions=True)
            await asyncio.gather(*(close_pose_peer(runtime, p) for p in list(runtime.peers)))
            await asyncio.to_thread(video.stop)

    app = FastAPI(title="Quest XR Bridge", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtime, app.state.video = runtime, video
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/viewer")
    async def viewer() -> FileResponse:
        return FileResponse(STATIC_DIR / "viewer.html", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {**runtime.health(), "video": video_status(runtime, video)}

    @app.get("/api/driving/authority")
    async def driving_authority():
        return runtime.driving.snapshot()

    @app.post("/api/driving/exit")
    async def driving_exit(body: dict, request: Request):
        try:
            event=DrivingEvent.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422,"invalid driving event") from exc
        if event.event!="exit":raise HTTPException(422,"this endpoint only revokes")
        if str(event.server_epoch) != runtime.driving.snapshot()["server_epoch"]:
            return {"revoked": False}
        if event.client_token is not None:
            # Current attempt or a just-closed page can revoke; retired attempts cannot.
            if runtime.pose_attempts.get(event.client_token) != event.attempt_id:
                return {"revoked": False}
            return {"revoked": runtime.driving.receive(event, str(event.client_token))}
        if event.reason != "operator_stop" or request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "host STOP requires a loopback client")
        return {"revoked": runtime.driving.revoke(event.session_id, event.authorization_id)}

    @app.get("/api/coordinate-transform")
    async def get_transform() -> dict[str, Any]:
        return runtime.coordinates.describe()

    @app.put("/api/coordinate-transform")
    async def set_transform(update: CoordinateTransformRequest) -> dict[str, Any]:
        if update.preset is not None:
            name = update.preset.lower().strip()
            transform = COORDINATE_PRESETS.get(name)
            if transform is None:
                raise HTTPException(422, "unknown coordinate preset")
        else:
            try:
                transform = remap_axes(update.axes or ())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            name = update.name or "custom"
        runtime.coordinates.configure(name, transform)
        return runtime.coordinates.describe()

    @app.get("/api/video/status")
    async def get_video_status() -> dict[str, Any]:
        return video_status(runtime, video)

    @app.get("/api/video/config")
    async def get_video() -> dict[str, Any]:
        return video_status(runtime, video)

    @app.put("/api/video/config")
    async def set_video(update: dict[str, Any]) -> dict[str, Any]:
        try:
            display = VideoDisplayConfig(**{**video.snapshot()["display"], **update})
            video.set_display(display)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return video_status(runtime, video)

    @app.post("/api/video/offer")
    async def video_offer(offer: WebRTCVideoOffer) -> dict[str, str]:
        try:
            return await asyncio.to_thread(
                video.offer, offer.sdp, offer.type, peer_id=offer.peer_id
            )
        except VideoUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, TimeoutError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/video/close")
    async def video_close(peer: VideoPeerClose) -> dict[str, bool]:
        try:
            return {"closed": await asyncio.to_thread(video.close_peer, peer.peer_id)}
        except VideoUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, TimeoutError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/webrtc/offer")
    async def pose_offer(offer: WebRTCPoseOffer, request: Request) -> dict[str, str]:
        async with runtime.pose_lock:
            current = runtime.pose_peer
            token, attempt = runtime.peer_claims.get(current, (None, None))
            if offer.client_token is not None:
                latest = runtime.pose_attempts.get(offer.client_token, 0)
                if token == offer.client_token:
                    latest = max(latest, attempt)
                if offer.attempt_id <= latest:
                    raise HTTPException(409, "pose attempt is stale or cancelled")
            if current is not None:
                if offer.client_token is None or offer.client_token != token:
                    raise HTTPException(409, "pose source is occupied by another page")
                await retire_pose_peer(runtime, current)
            if runtime.active_source.describe()["active"]:
                raise HTTPException(409, "pose source is occupied by another page")
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            runtime.peers.add(peer)
            runtime.pose_peer = peer
            runtime.peer_claims[peer] = offer.client_token, offer.attempt_id
            remember_pose_attempt(runtime, offer)

        async def expire_handshake() -> None:
            await asyncio.sleep(POSE_HANDSHAKE_TIMEOUT)
            if peer in runtime.peers and peer not in runtime.processors:
                await close_pose_peer(runtime, peer, wait=False)

        runtime.peer_timeouts[peer] = asyncio.create_task(expire_handshake())
        client = request.client
        client_name = f"{client.host}:{client.port}" if client else "unknown"

        @peer.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if (
                runtime.pose_peer is not peer
                or channel.label != "pose"
                or channel.ordered
                or channel.maxPacketLifeTime != 30
            ):
                channel.close()
                asyncio.create_task(close_pose_peer(runtime, peer, wait=False))
                return
            if peer in runtime.processors:
                channel.close()
                return
            try:
                processor = PoseStreamProcessor(runtime, client_name)
            except PoseSourceBusyError:
                channel.close()
                asyncio.create_task(close_pose_peer(runtime, peer, wait=False))
                return
            runtime.processors[peer] = processor
            deadline = runtime.peer_timeouts.pop(peer, None)
            if deadline is not None:
                deadline.cancel()

            @channel.on("message")
            def on_message(message: str | bytes) -> None:
                if isinstance(message, str) and len(message) <= 4096:
                    try:
                        payload = json.loads(message)
                    except (ValueError, RecursionError):
                        return
                    if isinstance(payload, dict) and payload.get("type") == "driving_event":
                        if runtime.pose_peer is not peer or runtime.processors.get(peer) is not processor:
                            return
                        try:
                            event=DrivingEvent.model_validate(payload)
                        except ValidationError:
                            return
                        owner, attempt = runtime.peer_claims.get(peer, (None, None))
                        if owner is not None and (event.client_token, event.attempt_id) == (owner, attempt):
                            runtime.driving.receive(event, str(owner))
                        return
                    if isinstance(payload, dict) and payload.get("type") == "video_receiver_status":
                        receive_video_status(runtime, video, peer, payload)
                        return
                processor.process(message, time.time_ns() / 1e6, time.monotonic_ns() / 1e6)

            @channel.on("close")
            def on_close() -> None:
                asyncio.create_task(close_pose_peer(runtime, peer, wait=False))

        @peer.on("connectionstatechange")
        async def state_changed() -> None:
            if peer.connectionState in {"failed", "disconnected", "closed"}:
                await close_pose_peer(runtime, peer, wait=False)

        try:
            async with asyncio.timeout(10):
                await peer.setRemoteDescription(RTCSessionDescription(offer.sdp, offer.type))
                await peer.setLocalDescription(await peer.createAnswer())
            if peer.localDescription is None:
                raise ValueError("WebRTC answer not created")
            async with runtime.pose_lock:
                if runtime.pose_peer is not peer:
                    raise HTTPException(409, "pose attempt was superseded")
            return {"sdp": peer.localDescription.sdp, "type": peer.localDescription.type,
                    "server_epoch": runtime.driving.snapshot()["server_epoch"]}
        except HTTPException:
            await close_pose_peer(runtime, peer, wait=False)
            raise
        except (Exception, asyncio.CancelledError) as exc:
            await close_pose_peer(runtime, peer, wait=False)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(400, f"pose negotiation failed: {exc}") from exc

    @app.post("/api/webrtc/close")
    async def pose_close(claim: PosePeerClose) -> dict[str, bool]:
        async with runtime.pose_lock:
            remember_pose_attempt(runtime, claim)
            peer = runtime.pose_peer
            closed = peer is not None and runtime.peer_claims[peer] == (
                claim.client_token, claim.attempt_id
            )
            if closed:
                await retire_pose_peer(runtime, peer)
            return {"closed": closed}

    @app.websocket("/ws")
    async def output(websocket: WebSocket) -> None:
        await websocket.accept()
        subscription, event = runtime.notifier.subscribe()
        event.set()
        last_generation = -1
        last_driving_generation = -1
        receive = asyncio.create_task(websocket.receive())
        update: asyncio.Task[bool] | None = None
        try:
            while True:
                update = asyncio.create_task(event.wait())
                done, _ = await asyncio.wait({receive, update}, return_when=asyncio.FIRST_COMPLETED)
                if receive in done:
                    message = receive.result()
                    if message["type"] == "websocket.disconnect":
                        break
                    receive = asyncio.create_task(websocket.receive())
                if update not in done:
                    update.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await update
                    continue
                event.clear()
                generation, text = runtime.output_snapshot()
                driving = runtime.driving.snapshot()
                try:
                    if driving["generation"] != last_driving_generation:
                        await asyncio.wait_for(websocket.send_json(driving), timeout=0.25)
                        last_driving_generation = driving["generation"]
                    if text is None or generation == last_generation:
                        continue
                    await asyncio.wait_for(websocket.send_text(text), timeout=0.25)
                except TimeoutError:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            websocket.close(code=1013, reason="consumer too slow"),
                            timeout=0.25,
                        )
                    break
                last_generation = generation
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            receive.cancel()
            if update is not None:
                update.cancel()
            await asyncio.gather(receive, *([update] if update else []), return_exceptions=True)
            runtime.notifier.unsubscribe(subscription)

    return app
