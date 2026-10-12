import unittest

from examples.pose_consumer import PoseConsumerGate


SESSION = "a752e716-5364-477e-860a-62723e50d561"
OTHER_SESSION = "407b7a3f-4791-479c-aa81-48e813aca057"


def frame(seq, *, session=SESSION, left=True, right=True):
    return {
        "session_id": session,
        "seq": seq,
        "hands": {"left": {"tracked": left}, "right": {"tracked": right}},
    }


class PoseConsumerTests(unittest.TestCase):
    def setUp(self):
        # Provisional project thresholds; physical stopping behavior is not tested here.
        self.gate = PoseConsumerGate(warning_s=0.05, timeout_s=0.1)

    def arm(self, seq=1, now=0):
        self.assertTrue(self.gate.receive(frame(seq), now))
        for side in ("left", "right"):
            self.assertTrue(self.gate.confirm(side, now))

    def assert_stopped(self, now):
        self.assertEqual(self.gate.allowed(now), {"left": False, "right": False})

    def test_each_hand_needs_tracking_and_explicit_confirmation(self):
        self.assertFalse(self.gate.confirm("left", 0))
        self.assertTrue(self.gate.receive(frame(1), 0))
        self.assert_stopped(0)
        self.arm(seq=2)
        self.gate.receive(frame(3, left=False), 0.01)
        self.assertEqual(self.gate.allowed(0.01), {"left": False, "right": True})
        self.assertFalse(self.gate.confirm("left", 0.01))
        self.gate.receive(frame(4), 0.02)
        self.assertEqual(self.gate.allowed(0.02), {"left": False, "right": True})
        self.assertTrue(self.gate.confirm("left", 0.02))

    def test_timeout_latches_without_a_socket_close_or_invalid_event(self):
        self.arm()
        self.assert_stopped(0.1)
        self.assertFalse(self.gate.confirm("left", 0.1))
        self.gate.receive(frame(2), 0.11)
        self.assert_stopped(0.11)
        self.assertTrue(self.gate.confirm("left", 0.11))

    def test_warning_at_50ms_precedes_hold_at_100ms(self):
        self.arm()
        self.assertFalse(self.gate.warning(0.049))
        self.assertTrue(self.gate.warning(0.05))
        self.assertTrue(all(self.gate.allowed(0.05).values()))
        self.gate.receive(frame(2), 0.06)
        self.assertFalse(self.gate.warning(0.06))
        self.assertTrue(all(self.gate.allowed(0.06).values()))
        self.assert_stopped(0.16)

    def test_new_receipt_does_not_hide_an_unpolled_timeout(self):
        self.arm()
        self.gate.receive(frame(2), 1)
        self.assert_stopped(1)

    def test_sequence_gaps_are_valid_but_replays_never_refresh_the_deadline(self):
        self.arm()
        self.assertTrue(self.gate.receive(frame(10), 0.01))
        self.assertTrue(all(self.gate.allowed(0.01).values()))
        for seq in (10, 9):
            self.assertFalse(self.gate.receive(frame(seq), 0.02))
            self.assert_stopped(0.02)
            self.assertFalse(self.gate.confirm("left", 0.02))
        self.assertEqual(self.gate.seq, 10)
        self.assertTrue(self.gate.receive(frame(11), 0.03))
        self.assert_stopped(0.03)
        self.assertTrue(self.gate.confirm("left", 0.03))

    def test_new_session_requires_confirmation(self):
        self.arm()
        self.assertTrue(self.gate.receive(frame(1, session=OTHER_SESSION), 0.01))
        self.assert_stopped(0.01)

    def test_supervised_reconnect_with_same_session_and_sequence_still_latches(self):
        self.arm(seq=10)
        self.gate.source_changed()
        self.assert_stopped(0.01)
        self.assertTrue(self.gate.receive(frame(11), 0.02))
        self.assert_stopped(0.02)
        self.assertTrue(self.gate.confirm("left", 0.02))

    def test_system_fault_cannot_be_cleared_by_new_poses_or_confirmation(self):
        self.arm()
        self.gate.set_inhibited(True)
        self.gate.receive(frame(2), 0.01)
        self.assertFalse(self.gate.confirm("left", 0.01))
        self.assert_stopped(0.01)
        self.gate.set_inhibited(False)
        self.assert_stopped(0.02)
        self.assertTrue(self.gate.confirm("left", 0.02))
        self.assertEqual(self.gate.allowed(0.02), {"left": True, "right": False})

    def test_invalid_header_or_tracking_state_fails_closed(self):
        for bad in (frame(0), frame(True), frame(1.5), frame(2, session="invalid"),
                    frame(2, left=1), {}, {"session_id": None}):
            with self.subTest(bad=bad):
                self.gate.source_changed()
                self.arm()
                self.assertFalse(self.gate.receive(bad, 0.01))
                self.assert_stopped(0.01)

    def test_timeout_must_be_finite_and_positive(self):
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                PoseConsumerGate(warning_s=0.05, timeout_s=timeout)
        for warning in (0, 0.1, 0.2, True, float("nan")):
            with self.assertRaises(ValueError):
                PoseConsumerGate(warning_s=warning, timeout_s=0.1)
