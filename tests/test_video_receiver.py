"""Receiver reports, source binding and expiry; no native video or hardware."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from quest_xr_bridge import QuestServer
from quest_xr_bridge.runtime import PoseRuntime
from quest_xr_bridge.server import create_app, receive_video_status, video_status
from quest_xr_bridge.video import VideoManager


class ReceiverStatusTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.clock = patch("time.monotonic", lambda: self.now)
        self.nanos = patch("time.monotonic_ns", lambda: int(self.now*1e9))
        self.clock.start(); self.nanos.start()
        self.addCleanup(self.clock.stop); self.addCleanup(self.nanos.stop)
        self.runtime = PoseRuntime()
        self.video = VideoManager()
        self.peer = object()
        self.ingress = str(uuid4())
        self.runtime.pose_peer = self.peer
        self.runtime.processors[self.peer] = SimpleNamespace(connection_id=self.ingress)
        self.session, self.video_peer = str(uuid4()), str(uuid4())
        self.video._enabled = self.video._worker_ready = True
        self.video._memory = object()
        self.video._process = SimpleNamespace(poll=lambda: None)
        self.video._worker_peer_id = self.video_peer
        self.video._stream_generation = 7
        self.publish_pose()
        self.report = dict(type="video_receiver_status", status_seq=1,
            session_id=self.session, peer_id=self.video_peer, stream_generation=7,
            pose_seq=9, timestamp_ms=1000.0, video_enabled=True, xr_active=True,
            decoded_age_ms=2.0, rendered_age_ms=0.0, rendered_eyes=["left","right"],
            status="Stereo camera live")

    def publish_pose(self):
        self.video._last_submit_ns = int(self.now*1e9)
        self.runtime.latest_pose.publish(dict(session_id=self.session,
            ingress_connection_id=self.ingress, seq=9, timestamp_ms=1000.0))

    def accept(self, **changes):
        return receive_video_status(self.runtime,self.video,self.peer,{**self.report,**changes})

    def status(self):
        return video_status(self.runtime,self.video)["receiver"]

    def test_python_and_http_interfaces_include_source_bound_report(self):
        self.assertFalse(self.status()["valid"])
        self.assertTrue(self.accept())
        current=self.status()
        self.assertTrue(current["valid"])
        self.assertEqual(current["ingress_connection_id"],self.ingress)
        service=QuestServer()
        service._runtime,self.video._error= self.runtime,None
        service._video=self.video
        self.assertEqual(service.video_status()["receiver"],current)
        app=create_app(self.runtime,self.video)
        endpoint=next(r.endpoint for r in app.routes if r.path=="/api/video/status")
        self.assertEqual(asyncio.run(endpoint())["receiver"],current)
        self.assertNotIn("client_token",current)

    def test_schema_rejects_invalid_fields_without_refresh(self):
        self.assertTrue(self.accept())
        before=dict(self.video._receiver_status)
        cases=[{"decoded_age_ms":True},{"decoded_age_ms":"2"},{"status_seq":True},{"decoded_age_ms":float("nan")},
            {"decoded_age_ms":-1.0},{"rendered_age_ms":float("inf")},
            {"xr_active":1},{"rendered_eyes":["left","left"]},
            {"rendered_eyes":["other"]},{"peer_id":"x"*1000},
            {"unknown":True},{"status":"x"*257}]
        for change in cases:
            with self.subTest(change=change):
                self.assertFalse(self.accept(**change))
                self.assertEqual(self.video._receiver_status,before)

    def test_wrong_source_session_timestamp_and_retired_peer_are_rejected(self):
        self.assertFalse(receive_video_status(self.runtime,self.video,object(),self.report))
        self.assertFalse(self.accept(session_id=str(uuid4())))
        self.assertFalse(self.accept(timestamp_ms=800.0))
        self.assertFalse(self.accept(pose_seq=0))
        self.assertFalse(self.accept(peer_id=str(uuid4())))
        self.assertFalse(self.accept(stream_generation=6))
        self.assertTrue(self.accept(pose_seq=10)) # async pose publication can lag one frame
        self.video._cancelled_peer_ids.append(self.video_peer)
        self.assertFalse(self.accept(status_seq=2))
        self.assertFalse(self.status()["valid"])

    def test_reordered_and_duplicate_reports_do_not_extend_liveness(self):
        self.assertTrue(self.accept(status_seq=3))
        received=self.video._receiver_status["received_monotonic"]
        self.now+=.11
        self.publish_pose()
        self.assertFalse(self.accept(status_seq=3))
        self.assertFalse(self.accept(status_seq=2))
        self.assertEqual(self.video._receiver_status["received_monotonic"],received)
        self.assertEqual(self.status()["reason"],"report_timeout")
        self.assertTrue(self.accept(status_seq=4))
        self.assertTrue(self.status()["valid"])

    def test_new_ingress_cannot_reuse_old_positive_report(self):
        self.assertTrue(self.accept())
        self.runtime.processors.pop(self.peer)
        self.runtime.pose_peer=None
        self.assertFalse(self.status()["valid"])
        self.peer=object(); self.runtime.pose_peer=self.peer
        self.ingress=str(uuid4())
        self.runtime.processors[self.peer]=SimpleNamespace(connection_id=self.ingress)
        self.publish_pose()
        self.assertFalse(self.status()["valid"])
        self.assertTrue(self.accept())
        self.assertTrue(self.status()["valid"])

    def test_no_peer_negative_report_revokes_without_authorizing(self):
        self.assertTrue(self.accept())
        self.assertTrue(self.accept(status_seq=2,peer_id=None,session_id=None,xr_active=False))
        self.assertFalse(self.status()["valid"])

    def test_missing_draw_frozen_decode_and_inactive_xr_remain_invalid(self):
        for i,change in enumerate([{"rendered_eyes":["left"]},
                {"decoded_age_ms":251.0},{"rendered_age_ms":101.0},
                {"video_enabled":False},{"xr_active":False}],start=1):
            with self.subTest(change=change):
                self.assertTrue(self.accept(status_seq=i,**change))
                self.assertFalse(self.status()["valid"])
        self.assertTrue(self.accept(status_seq=6))
        self.assertTrue(self.status()["valid"])
        self.now+=.02
        self.video._last_submit_ns=int((self.now-.3)*1e9)
        self.assertEqual(self.status()["reason"],"decoded_or_source_stale")

    def test_worker_stop_and_timeout_are_local_monotonic_checks(self):
        self.assertTrue(self.accept())
        self.video._process=SimpleNamespace(poll=lambda:7)
        self.assertFalse(self.status()["valid"])
        service=QuestServer()
        self.assertFalse(service.video_status()["receiver"]["valid"])
        for invalid in (0,-1,float("nan"),True):
            with self.subTest(timeout=invalid),self.assertRaises(ValueError):
                service.video_status(receiver_timeout_s=invalid)
