import unittest
from uuid import uuid4
from pydantic import ValidationError
from quest_xr_bridge.driving import DrivingAuthority, DrivingEvent


class DrivingTests(unittest.TestCase):
    def setUp(self):
        self.notifications = []
        class Notifier:
            def notify(_self): self.notifications.append(True)
        self.authority = DrivingAuthority(Notifier())
        self.session, self.grant, self.page = uuid4(), uuid4(), uuid4()

    def event(self, event='enter', seq=1, **kwargs):
        return DrivingEvent(type='driving_event', event=event, session_id=self.session,
            authorization_id=self.grant, server_epoch=self.authority.snapshot()['server_epoch'],
            client_token=self.page, attempt_id=1, seq=seq, reason='enter_click' if event == 'enter' else 'xr_end', **kwargs)

    def test_reconnect_same_page_retains_grant_but_exit_cannot_be_resurrected(self):
        self.assertTrue(self.authority.receive(self.event(), str(self.page)))
        epoch = self.authority.snapshot()['server_epoch']
        self.assertTrue(self.authority.receive(self.event(seq=2), str(self.page)))
        self.assertFalse(self.authority.receive(self.event(seq=2), str(self.page)))
        self.assertFalse(self.authority.receive(self.event(seq=3), str(uuid4())))
        self.assertTrue(self.authority.receive(self.event('exit', 3), str(self.page)))
        self.assertFalse(self.authority.receive(self.event(seq=4), str(self.page)))
        self.assertFalse(self.authority.snapshot()['active'])
        self.assertEqual(self.authority.snapshot()['server_epoch'], epoch)

    def test_stop_matches_grant_and_new_explicit_entry_is_required(self):
        self.authority.receive(self.event(), str(self.page))
        self.assertTrue(self.authority.revoke(uuid4(), self.grant))
        self.assertTrue(self.authority.snapshot()["active"])
        self.assertTrue(self.authority.revoke(self.session, self.grant))
        self.assertFalse(self.authority.receive(self.event(seq=10), str(self.page)))
        old = self.event('exit', 20)
        self.session, self.grant, self.page = uuid4(), uuid4(), uuid4()
        self.assertTrue(self.authority.receive(self.event(), str(self.page)))
        self.assertFalse(self.authority.receive(old, str(self.page)))
        self.assertEqual(self.authority.snapshot()['session_id'], str(self.session))
        self.assertTrue(self.authority.snapshot()['active'])

    def test_strict_boundary(self):
        for patch in ({'seq': True}, {'session_id': 'invalid'}, {'event': 'pose'}, {'extra': 1}):
            value=self.event().model_dump(); value.update(patch)
            with self.assertRaises(ValidationError): DrivingEvent.model_validate(value)

    def test_exit_before_enter_and_no_eviction(self):
        for _ in range(80):
            self.session, self.grant = uuid4(), uuid4()
            enter = self.event()
            self.assertTrue(self.authority.receive(self.event('exit', 2), str(self.page)))
            self.assertFalse(self.authority.receive(self.event('exit', 2), str(self.page)))
            self.assertFalse(self.authority.receive(enter, str(self.page)))
            if _ == 0: oldest = enter
        self.assertFalse(self.authority.receive(oldest, str(self.page)))
        self.assertFalse(self.authority.snapshot()['active'])

    def test_trusted_revoke_before_enter_is_scoped(self):
        original = self.event()
        self.authority.receive(original, str(self.page))
        self.session, self.grant = uuid4(), uuid4()
        late = self.event()
        self.assertTrue(self.authority.revoke(self.session, self.grant))
        self.assertTrue(self.authority.snapshot()['active'])
        self.assertEqual(self.authority.snapshot()['session_id'], str(original.session_id))
        self.assertFalse(self.authority.receive(late, str(self.page)))

    def test_wrong_epoch_claim_reason_and_other_owner_cannot_revoke(self):
        self.authority.receive(self.event(), str(self.page))
        for patch in ({'server_epoch': uuid4()}, {'client_token': uuid4()}, {'reason': 'enter_click'}):
            event = self.event('exit', 2).model_copy(update=patch)
            self.assertFalse(self.authority.receive(event, str(self.page)))
        other = self.event('exit', 2).model_copy(update={'client_token': uuid4()})
        self.assertFalse(self.authority.receive(other, str(other.client_token)))
        self.assertTrue(self.authority.snapshot()['active'])

    def test_page_sequence_prevents_late_unseen_old_entry(self):
        old = self.event(seq=1)
        self.session, self.grant = uuid4(), uuid4()
        current = self.event(seq=3)
        self.assertTrue(self.authority.receive(current, str(self.page)))
        self.assertFalse(self.authority.receive(old, str(self.page)))
        self.assertEqual(self.authority.snapshot()['authorization_id'], str(current.authorization_id))
