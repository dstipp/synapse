# -*- coding: utf-8 -*-
# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from synapse.api.constants import EventTypes
from synapse.rest import admin
from synapse.rest.client.v1 import login, room

from tests.unittest import HomeserverTestCase

one_hour_ms = 3600000
one_day_ms = one_hour_ms * 24


class RetentionTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor, clock):
        config = self.default_config()
        config["retention"] = {
            "enabled": True,
            "min_lifetime": one_day_ms,
            "max_lifetime": one_day_ms * 3,
        }

        self.hs = self.setup_test_homeserver(config=config)
        return self.hs

    def prepare(self, reactor, clock, homeserver):
        self.user_id = self.register_user("user", "password")
        self.token = self.login("user", "password")

    def test_retention_state_event(self):
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        self.helper.send_state(
            room_id=room_id,
            event_type=EventTypes.Retention,
            body={
                "max_lifetime": one_day_ms * 4,
            },
            tok=self.token,
            expect_code=400,
        )

        self.helper.send_state(
            room_id=room_id,
            event_type=EventTypes.Retention,
            body={
                "max_lifetime": one_hour_ms,
            },
            tok=self.token,
            expect_code=400,
        )

    def test_retention_event_purged_with_state_event(self):
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        # Set the room's retention period to 2 days.
        lifetime = one_day_ms * 2
        self.helper.send_state(
            room_id=room_id,
            event_type=EventTypes.Retention,
            body={
                "max_lifetime": lifetime,
            },
            tok=self.token,
        )

        self._test_retention_event_purged(room_id, one_day_ms * 1.5)

    def test_retention_event_purged_without_state_event(self):
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        self._test_retention_event_purged(room_id, one_day_ms * 2)

    def _test_retention_event_purged(self, room_id, increment):
        # Send a first event to the room. This is the event we'll want to be purged at the
        # end of the test.
        resp = self.helper.send(
            room_id=room_id,
            body="1",
            tok=self.token,
        )

        expired_event_id = resp.get("event_id")

        # Check that we can retrieve the event.
        expired_event = self.get_event(room_id, expired_event_id)
        self.assertEqual(expired_event.get("content", {}).get("body"), "1", expired_event)

        # Advance the time by a day.
        self.reactor.advance(increment / 1000)

        # Send another event. We need this because the purge job won't purge the most
        # recent event in the room.
        resp = self.helper.send(
            room_id=room_id,
            body="2",
            tok=self.token,
        )

        valid_event_id = resp.get("event_id")

        # Advance the time by a day and a half. Now our first event should have expired
        # but our second one should still be kept.
        self.reactor.advance(increment / 1000)

        # Check that the event has been purged from the database.
        self.get_event(room_id, expired_event_id, expected_code=404)

        # Check that the event that hasn't been purged can still be retrieved.
        valid_event = self.get_event(room_id, valid_event_id)
        self.assertEqual(valid_event.get("content", {}).get("body"), "2", valid_event)

    def get_event(self, room_id, event_id, expected_code=200):
        url = "/_matrix/client/r0/rooms/%s/event/%s" % (room_id, event_id)

        request, channel = self.make_request("GET", url, access_token=self.token)
        self.render(request)

        self.assertEqual(channel.code, expected_code, channel.result)

        return channel.json_body
