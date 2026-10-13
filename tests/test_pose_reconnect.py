import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from test_binary_protocol import pose_frame_v5

from quest_xr_bridge.binary_protocol import encode_pose_packet
from quest_xr_bridge.pose import PosePeerClose, WebRTCPoseOffer
from quest_xr_bridge.runtime import PoseRuntime
from quest_xr_bridge.server import close_pose_peer, create_app
from quest_xr_bridge.video import VideoManager


class FakePeer:
    def __init__(self, remote_gate=None):
        self.handlers = {}
        self.connectionState = "connected"
        self.localDescription = None
        self.remote_gate = remote_gate
        self.remote_entered = asyncio.Event()
        self.close_gate = None
        self.close_entered = asyncio.Event()

    def on(self, name):
        def register(handler):
            self.handlers[name] = handler
            return handler
        return register

    async def setRemoteDescription(self, _description):
        self.remote_entered.set()
        if self.remote_gate is not None:
            await self.remote_gate.wait()

    async def createAnswer(self):
        return SimpleNamespace(sdp="answer", type="answer")

    async def setLocalDescription(self, description):
        self.localDescription = description

    async def close(self):
        self.close_entered.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.connectionState = "closed"


class PoseReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = PoseRuntime()
        app = create_app(self.runtime, VideoManager())
        self.offer = next(r.endpoint for r in app.routes if r.path == "/api/webrtc/offer")
        self.close = next(r.endpoint for r in app.routes if r.path == "/api/webrtc/close")
        self.token = uuid4()
        self.fakes = []
        self.next_remote_gate = None
        self.tasks = []
        def make_peer(_config):
            peer = FakePeer(self.next_remote_gate)
            self.next_remote_gate = None
            self.fakes.append(peer)
            return peer
        self.patch = patch("quest_xr_bridge.server.RTCPeerConnection", side_effect=make_peer)
        self.patch.start()

    async def asyncTearDown(self):
        for peer in self.fakes:
            for gate in (peer.remote_gate, peer.close_gate):
                if gate is not None:
                    gate.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(close_pose_peer(self.runtime, p) for p in list(self.runtime.peers)))
        self.patch.stop()
        self.assertFalse(self.runtime.peers)
        self.assertFalse(self.runtime.peer_closures)
        self.assertFalse(self.runtime.active_source.describe()["active"])

    async def connect(self, attempt, token=None):
        return await self.offer(
            WebRTCPoseOffer(sdp="offer", type="offer", client_token=token or self.token,
                            attempt_id=attempt),
            SimpleNamespace(client=None),
        )

    async def close_attempt(self, attempt, token=None):
        return await self.close(PosePeerClose(client_token=token or self.token, attempt_id=attempt))

    def publish(self, peer, seq):
        channel = FakePeer()
        channel.label, channel.ordered, channel.maxPacketLifeTime = "pose", False, 30
        peer.handlers["datachannel"](channel)
        processor = self.runtime.processors[peer]
        frame = pose_frame_v5()
        frame["seq"] = seq
        processor._process(encode_pose_packet(frame), 1, 1)
        return processor, channel

    async def test_receiver_status_is_routed_without_counting_as_a_pose(self):
        await self.connect(1)
        peer=self.runtime.pose_peer
        processor,channel=self.publish(peer,9)
        payload={"type":"video_receiver_status","status_seq":1}
        with patch("quest_xr_bridge.server.receive_video_status") as receiver, \
             patch.object(processor,"process") as pose:
            channel.handlers["message"](json.dumps(payload))
            receiver.assert_called_once_with(self.runtime,unittest.mock.ANY,peer,payload)
            pose.assert_not_called()
            with patch("quest_xr_bridge.server.json.loads", side_effect=RecursionError):
                channel.handlers["message"]("nested JSON")
            pose.assert_not_called()
            packet=encode_pose_packet(pose_frame_v5())
            channel.handlers["message"](packet)
            self.assertEqual(pose.call_args.args[0],packet)

    async def test_same_page_replaces_live_source_while_old_transport_cleanup_waits(self):
        await self.connect(1)
        old = self.runtime.pose_peer
        old.close_gate = asyncio.Event()
        processor, channel = self.publish(old, 1234)
        await asyncio.wait_for(self.connect(2), 0.5)
        await asyncio.wait_for(old.close_entered.wait(), 0.5)
        current = self.runtime.pose_peer
        self.assertIsNot(current, old)
        self.assertIn(old, self.runtime.peers)
        self.assertTrue(processor._closed)
        self.publish(current, 1235)
        self.assertEqual(await self.close_attempt(1), {"closed": False})
        channel.handlers["close"]()
        old.connectionState = "failed"
        await old.handlers["connectionstatechange"]()
        await asyncio.sleep(0)
        self.assertIs(self.runtime.pose_peer, current)
        self.assertEqual(self.runtime.latest_pose.snapshot()[1]["seq"], 1235)
        self.assertTrue(self.runtime.active_source.describe()["active"])

    async def test_other_page_and_legacy_offer_cannot_replace_current_source(self):
        await self.connect(1)
        current = self.runtime.pose_peer
        self.publish(current, 1234)
        with self.assertRaises(HTTPException) as busy:
            await self.connect(2, uuid4())
        self.assertEqual(busy.exception.status_code, 409)
        with self.assertRaises(HTTPException) as legacy:
            await self.offer(WebRTCPoseOffer(sdp="offer", type="offer"), SimpleNamespace(client=None))
        self.assertEqual(legacy.exception.status_code, 409)
        self.assertIs(self.runtime.pose_peer, current)
        self.assertEqual(self.runtime.latest_pose.snapshot()[1]["seq"], 1234)
        self.assertEqual(len(self.fakes), 1)

    async def test_cancel_before_offer_and_late_close_are_idempotent(self):
        self.assertEqual(await self.close_attempt(4), {"closed": False})
        with self.assertRaises(HTTPException) as cancelled:
            await self.connect(4)
        self.assertEqual(cancelled.exception.status_code, 409)
        await self.connect(5)
        current = self.runtime.pose_peer
        self.assertEqual(await self.close_attempt(4), {"closed": False})
        self.assertIs(self.runtime.pose_peer, current)
        self.assertEqual(await self.close_attempt(5), {"closed": True})
        self.assertEqual(await self.close_attempt(5), {"closed": False})
        with self.assertRaises(HTTPException):
            await self.connect(5)
        await self.connect(6)

    async def test_old_offer_finishing_after_replacement_cannot_reclaim_slot(self):
        gate = self.next_remote_gate = asyncio.Event()
        old_offer = asyncio.create_task(self.connect(1))
        self.tasks.append(old_offer)
        while not self.fakes:
            await asyncio.sleep(0)
        old = self.fakes[0]
        await old.remote_entered.wait()
        await self.connect(2)
        current = self.runtime.pose_peer
        gate.set()
        with self.assertRaises(HTTPException) as superseded:
            await old_offer
        self.assertEqual(superseded.exception.status_code, 409)
        await self.close_attempt(1)
        self.assertIs(self.runtime.pose_peer, current)
        with self.assertRaises(HTTPException):
            await self.connect(1)

    async def test_source_release_timeout_keeps_slot_and_rejects_replacement(self):
        await self.connect(1)
        old = self.runtime.pose_peer
        processor, _ = self.publish(old, 1234)
        release, finished = threading.Event(), threading.Event()
        original_close = processor.close
        def slow_close():
            release.wait(2)
            original_close()
            finished.set()
        try:
            with patch.object(processor, "close", side_effect=slow_close), \
                 patch("quest_xr_bridge.server.POSE_SOURCE_RELEASE_TIMEOUT", 0.02):
                with self.assertRaises(HTTPException) as timeout:
                    await self.connect(2)
                self.assertEqual(timeout.exception.status_code, 503)
                self.assertIs(self.runtime.pose_peer, old)
                self.assertEqual(len(self.fakes), 1)
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
            await self.connect(3)
        finally:
            release.set()

    async def test_retired_claim_cache_is_bounded_and_active_generation_still_protected(self):
        await self.connect(3)
        current = self.runtime.pose_peer
        for _ in range(70):
            await self.close_attempt(1, uuid4())
        self.assertEqual(len(self.runtime.pose_attempts), 64)
        with self.assertRaises(HTTPException):
            await self.connect(2)
        self.assertIs(self.runtime.pose_peer, current)
