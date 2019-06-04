# -*- coding: utf-8 -*-
# Copyright 2017 New Vector Ltd
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
import time

from mock import Mock

import canonicaljson
import signedjson.key
import signedjson.sign
from signedjson.key import get_verify_key

from twisted.internet import defer

from synapse.api.errors import SynapseError
from synapse.crypto import keyring
from synapse.crypto.keyring import PerspectivesKeyFetcher, ServerKeyFetcher
from synapse.storage.keys import FetchKeyResult
from synapse.util import logcontext
from synapse.util.logcontext import LoggingContext

from tests import unittest


class MockPerspectiveServer(object):
    def __init__(self):
        self.server_name = "mock_server"
        self.key = signedjson.key.generate_signing_key(0)

    def get_verify_keys(self):
        vk = signedjson.key.get_verify_key(self.key)
        return {"%s:%s" % (vk.alg, vk.version): vk}

    def get_signed_key(self, server_name, verify_key):
        key_id = "%s:%s" % (verify_key.alg, verify_key.version)
        res = {
            "server_name": server_name,
            "old_verify_keys": {},
            "valid_until_ts": time.time() * 1000 + 3600,
            "verify_keys": {
                key_id: {"key": signedjson.key.encode_verify_key_base64(verify_key)}
            },
        }
        self.sign_response(res)
        return res

    def sign_response(self, res):
        signedjson.sign.sign_json(res, self.server_name, self.key)


class KeyringTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor, clock):
        self.mock_perspective_server = MockPerspectiveServer()
        self.http_client = Mock()
        hs = self.setup_test_homeserver(handlers=None, http_client=self.http_client)
        keys = self.mock_perspective_server.get_verify_keys()
        hs.config.perspectives = {self.mock_perspective_server.server_name: keys}
        return hs

    def check_context(self, _, expected):
        self.assertEquals(
            getattr(LoggingContext.current_context(), "request", None), expected
        )

    def test_wait_for_previous_lookups(self):
        kr = keyring.Keyring(self.hs)

        lookup_1_deferred = defer.Deferred()
        lookup_2_deferred = defer.Deferred()

        # we run the lookup in a logcontext so that the patched inlineCallbacks can check
        # it is doing the right thing with logcontexts.
        wait_1_deferred = run_in_context(
            kr.wait_for_previous_lookups, {"server1": lookup_1_deferred}
        )

        # there were no previous lookups, so the deferred should be ready
        self.successResultOf(wait_1_deferred)

        # set off another wait. It should block because the first lookup
        # hasn't yet completed.
        wait_2_deferred = run_in_context(
            kr.wait_for_previous_lookups, {"server1": lookup_2_deferred}
        )

        self.assertFalse(wait_2_deferred.called)

        # let the first lookup complete (in the sentinel context)
        lookup_1_deferred.callback(None)

        # now the second wait should complete.
        self.successResultOf(wait_2_deferred)

    def test_verify_json_objects_for_server_awaits_previous_requests(self):
        key1 = signedjson.key.generate_signing_key(1)

        kr = keyring.Keyring(self.hs)
        json1 = {}
        signedjson.sign.sign_json(json1, "server10", key1)

        persp_resp = {
            "server_keys": [
                self.mock_perspective_server.get_signed_key(
                    "server10", signedjson.key.get_verify_key(key1)
                )
            ]
        }
        persp_deferred = defer.Deferred()

        @defer.inlineCallbacks
        def get_perspectives(**kwargs):
            self.assertEquals(LoggingContext.current_context().request, "11")
            with logcontext.PreserveLoggingContext():
                yield persp_deferred
            defer.returnValue(persp_resp)

        self.http_client.post_json.side_effect = get_perspectives

        # start off a first set of lookups
        @defer.inlineCallbacks
        def first_lookup():
            with LoggingContext("11") as context_11:
                context_11.request = "11"

                res_deferreds = kr.verify_json_objects_for_server(
                    [("server10", json1, 0), ("server11", {}, 0)]
                )

                # the unsigned json should be rejected pretty quickly
                self.assertTrue(res_deferreds[1].called)
                try:
                    yield res_deferreds[1]
                    self.assertFalse("unsigned json didn't cause a failure")
                except SynapseError:
                    pass

                self.assertFalse(res_deferreds[0].called)
                res_deferreds[0].addBoth(self.check_context, None)

                yield logcontext.make_deferred_yieldable(res_deferreds[0])

                # let verify_json_objects_for_server finish its work before we kill the
                # logcontext
                yield self.clock.sleep(0)

        d0 = first_lookup()

        # wait a tick for it to send the request to the perspectives server
        # (it first tries the datastore)
        self.pump()
        self.http_client.post_json.assert_called_once()

        # a second request for a server with outstanding requests
        # should block rather than start a second call
        @defer.inlineCallbacks
        def second_lookup():
            with LoggingContext("12") as context_12:
                context_12.request = "12"
                self.http_client.post_json.reset_mock()
                self.http_client.post_json.return_value = defer.Deferred()

                res_deferreds_2 = kr.verify_json_objects_for_server(
                    [("server10", json1, 0)]
                )
                res_deferreds_2[0].addBoth(self.check_context, None)
                yield logcontext.make_deferred_yieldable(res_deferreds_2[0])

                # let verify_json_objects_for_server finish its work before we kill the
                # logcontext
                yield self.clock.sleep(0)

        d2 = second_lookup()

        self.pump()
        self.http_client.post_json.assert_not_called()

        # complete the first request
        persp_deferred.callback(persp_resp)
        self.get_success(d0)
        self.get_success(d2)

    def test_verify_json_for_server(self):
        kr = keyring.Keyring(self.hs)

        key1 = signedjson.key.generate_signing_key(1)
        r = self.hs.datastore.store_server_verify_keys(
            "server9",
            time.time() * 1000,
            [("server9", get_key_id(key1), FetchKeyResult(get_verify_key(key1), 1000))],
        )
        self.get_success(r)

        json1 = {}
        signedjson.sign.sign_json(json1, "server9", key1)

        # should fail immediately on an unsigned object
        d = _verify_json_for_server(kr, "server9", {}, 0)
        self.failureResultOf(d, SynapseError)

        # should suceed on a signed object
        d = _verify_json_for_server(kr, "server9", json1, 500)
        # self.assertFalse(d.called)
        self.get_success(d)

    def test_verify_json_dedupes_key_requests(self):
        """Two requests for the same key should be deduped."""
        key1 = signedjson.key.generate_signing_key(1)

        def get_keys(keys_to_fetch):
            # there should only be one request object (with the max validity)
            self.assertEqual(keys_to_fetch, {"server1": {get_key_id(key1): 1500}})

            return defer.succeed(
                {
                    "server1": {
                        get_key_id(key1): FetchKeyResult(get_verify_key(key1), 1200)
                    }
                }
            )

        mock_fetcher = keyring.KeyFetcher()
        mock_fetcher.get_keys = Mock(side_effect=get_keys)
        kr = keyring.Keyring(self.hs, key_fetchers=(mock_fetcher,))

        json1 = {}
        signedjson.sign.sign_json(json1, "server1", key1)

        # the first request should succeed; the second should fail because the key
        # has expired
        results = kr.verify_json_objects_for_server(
            [("server1", json1, 500), ("server1", json1, 1500)]
        )
        self.assertEqual(len(results), 2)
        self.get_success(results[0])
        e = self.get_failure(results[1], SynapseError).value
        self.assertEqual(e.errcode, "M_UNAUTHORIZED")
        self.assertEqual(e.code, 401)

        # there should have been a single call to the fetcher
        mock_fetcher.get_keys.assert_called_once()

    def test_verify_json_falls_back_to_other_fetchers(self):
        """If the first fetcher cannot provide a recent enough key, we fall back"""
        key1 = signedjson.key.generate_signing_key(1)

        def get_keys1(keys_to_fetch):
            self.assertEqual(keys_to_fetch, {"server1": {get_key_id(key1): 1500}})
            return defer.succeed(
                {
                    "server1": {
                        get_key_id(key1): FetchKeyResult(get_verify_key(key1), 800)
                    }
                }
            )

        def get_keys2(keys_to_fetch):
            self.assertEqual(keys_to_fetch, {"server1": {get_key_id(key1): 1500}})
            return defer.succeed(
                {
                    "server1": {
                        get_key_id(key1): FetchKeyResult(get_verify_key(key1), 1200)
                    }
                }
            )

        mock_fetcher1 = keyring.KeyFetcher()
        mock_fetcher1.get_keys = Mock(side_effect=get_keys1)
        mock_fetcher2 = keyring.KeyFetcher()
        mock_fetcher2.get_keys = Mock(side_effect=get_keys2)
        kr = keyring.Keyring(self.hs, key_fetchers=(mock_fetcher1, mock_fetcher2))

        json1 = {}
        signedjson.sign.sign_json(json1, "server1", key1)

        results = kr.verify_json_objects_for_server(
            [("server1", json1, 1200), ("server1", json1, 1500)]
        )
        self.assertEqual(len(results), 2)
        self.get_success(results[0])
        e = self.get_failure(results[1], SynapseError).value
        self.assertEqual(e.errcode, "M_UNAUTHORIZED")
        self.assertEqual(e.code, 401)

        # there should have been a single call to each fetcher
        mock_fetcher1.get_keys.assert_called_once()
        mock_fetcher2.get_keys.assert_called_once()


class ServerKeyFetcherTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor, clock):
        self.http_client = Mock()
        hs = self.setup_test_homeserver(handlers=None, http_client=self.http_client)
        return hs

    def test_get_keys_from_server(self):
        # arbitrarily advance the clock a bit
        self.reactor.advance(100)

        SERVER_NAME = "server2"
        fetcher = ServerKeyFetcher(self.hs)
        testkey = signedjson.key.generate_signing_key("ver1")
        testverifykey = signedjson.key.get_verify_key(testkey)
        testverifykey_id = "ed25519:ver1"
        VALID_UNTIL_TS = 200 * 1000

        # valid response
        response = {
            "server_name": SERVER_NAME,
            "old_verify_keys": {},
            "valid_until_ts": VALID_UNTIL_TS,
            "verify_keys": {
                testverifykey_id: {
                    "key": signedjson.key.encode_verify_key_base64(testverifykey)
                }
            },
        }
        signedjson.sign.sign_json(response, SERVER_NAME, testkey)

        def get_json(destination, path, **kwargs):
            self.assertEqual(destination, SERVER_NAME)
            self.assertEqual(path, "/_matrix/key/v2/server/key1")
            return response

        self.http_client.get_json.side_effect = get_json

        keys_to_fetch = {SERVER_NAME: {"key1": 0}}
        keys = self.get_success(fetcher.get_keys(keys_to_fetch))
        k = keys[SERVER_NAME][testverifykey_id]
        self.assertEqual(k.valid_until_ts, VALID_UNTIL_TS)
        self.assertEqual(k.verify_key, testverifykey)
        self.assertEqual(k.verify_key.alg, "ed25519")
        self.assertEqual(k.verify_key.version, "ver1")

        # check that the perspectives store is correctly updated
        lookup_triplet = (SERVER_NAME, testverifykey_id, None)
        key_json = self.get_success(
            self.hs.get_datastore().get_server_keys_json([lookup_triplet])
        )
        res = key_json[lookup_triplet]
        self.assertEqual(len(res), 1)
        res = res[0]
        self.assertEqual(res["key_id"], testverifykey_id)
        self.assertEqual(res["from_server"], SERVER_NAME)
        self.assertEqual(res["ts_added_ms"], self.reactor.seconds() * 1000)
        self.assertEqual(res["ts_valid_until_ms"], VALID_UNTIL_TS)

        # we expect it to be encoded as canonical json *before* it hits the db
        self.assertEqual(
            bytes(res["key_json"]), canonicaljson.encode_canonical_json(response)
        )

        # change the server name: the result should be ignored
        response["server_name"] = "OTHER_SERVER"

        keys = self.get_success(fetcher.get_keys(keys_to_fetch))
        self.assertEqual(keys, {})


class PerspectivesKeyFetcherTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor, clock):
        self.mock_perspective_server = MockPerspectiveServer()
        self.http_client = Mock()
        hs = self.setup_test_homeserver(handlers=None, http_client=self.http_client)
        keys = self.mock_perspective_server.get_verify_keys()
        hs.config.perspectives = {self.mock_perspective_server.server_name: keys}
        return hs

    def test_get_keys_from_perspectives(self):
        # arbitrarily advance the clock a bit
        self.reactor.advance(100)

        fetcher = PerspectivesKeyFetcher(self.hs)

        SERVER_NAME = "server2"
        testkey = signedjson.key.generate_signing_key("ver1")
        testverifykey = signedjson.key.get_verify_key(testkey)
        testverifykey_id = "ed25519:ver1"
        VALID_UNTIL_TS = 200 * 1000

        # valid response
        response = {
            "server_name": SERVER_NAME,
            "old_verify_keys": {},
            "valid_until_ts": VALID_UNTIL_TS,
            "verify_keys": {
                testverifykey_id: {
                    "key": signedjson.key.encode_verify_key_base64(testverifykey)
                }
            },
        }

        # the response must be signed by both the origin server and the perspectives
        # server.
        signedjson.sign.sign_json(response, SERVER_NAME, testkey)
        self.mock_perspective_server.sign_response(response)

        def post_json(destination, path, data, **kwargs):
            self.assertEqual(destination, self.mock_perspective_server.server_name)
            self.assertEqual(path, "/_matrix/key/v2/query")

            # check that the request is for the expected key
            q = data["server_keys"]
            self.assertEqual(list(q[SERVER_NAME].keys()), ["key1"])
            return {"server_keys": [response]}

        self.http_client.post_json.side_effect = post_json

        keys_to_fetch = {SERVER_NAME: {"key1": 0}}
        keys = self.get_success(fetcher.get_keys(keys_to_fetch))
        self.assertIn(SERVER_NAME, keys)
        k = keys[SERVER_NAME][testverifykey_id]
        self.assertEqual(k.valid_until_ts, VALID_UNTIL_TS)
        self.assertEqual(k.verify_key, testverifykey)
        self.assertEqual(k.verify_key.alg, "ed25519")
        self.assertEqual(k.verify_key.version, "ver1")

        # check that the perspectives store is correctly updated
        lookup_triplet = (SERVER_NAME, testverifykey_id, None)
        key_json = self.get_success(
            self.hs.get_datastore().get_server_keys_json([lookup_triplet])
        )
        res = key_json[lookup_triplet]
        self.assertEqual(len(res), 1)
        res = res[0]
        self.assertEqual(res["key_id"], testverifykey_id)
        self.assertEqual(res["from_server"], self.mock_perspective_server.server_name)
        self.assertEqual(res["ts_added_ms"], self.reactor.seconds() * 1000)
        self.assertEqual(res["ts_valid_until_ms"], VALID_UNTIL_TS)

        self.assertEqual(
            bytes(res["key_json"]),
            canonicaljson.encode_canonical_json(response),
        )

    def test_invalid_perspectives_responses(self):
        """Check that invalid responses from the perspectives server are rejected"""
        # arbitrarily advance the clock a bit
        self.reactor.advance(100)

        SERVER_NAME = "server2"
        testkey = signedjson.key.generate_signing_key("ver1")
        testverifykey = signedjson.key.get_verify_key(testkey)
        testverifykey_id = "ed25519:ver1"
        VALID_UNTIL_TS = 200 * 1000

        def build_response():
            # valid response
            response = {
                "server_name": SERVER_NAME,
                "old_verify_keys": {},
                "valid_until_ts": VALID_UNTIL_TS,
                "verify_keys": {
                    testverifykey_id: {
                        "key": signedjson.key.encode_verify_key_base64(testverifykey)
                    }
                },
            }

            # the response must be signed by both the origin server and the perspectives
            # server.
            signedjson.sign.sign_json(response, SERVER_NAME, testkey)
            self.mock_perspective_server.sign_response(response)
            return response

        def get_key_from_perspectives(response):
            fetcher = PerspectivesKeyFetcher(self.hs)
            keys_to_fetch = {SERVER_NAME: {"key1": 0}}

            def post_json(destination, path, data, **kwargs):
                self.assertEqual(destination, self.mock_perspective_server.server_name)
                self.assertEqual(path, "/_matrix/key/v2/query")
                return {"server_keys": [response]}

            self.http_client.post_json.side_effect = post_json

            return self.get_success(fetcher.get_keys(keys_to_fetch))

        # start with a valid response so we can check we are testing the right thing
        response = build_response()
        keys = get_key_from_perspectives(response)
        k = keys[SERVER_NAME][testverifykey_id]
        self.assertEqual(k.verify_key, testverifykey)

        # remove the perspectives server's signature
        response = build_response()
        del response["signatures"][self.mock_perspective_server.server_name]
        self.http_client.post_json.return_value = {"server_keys": [response]}
        keys = get_key_from_perspectives(response)
        self.assertEqual(keys, {}, "Expected empty dict with missing persp server sig")

        # remove the origin server's signature
        response = build_response()
        del response["signatures"][SERVER_NAME]
        self.http_client.post_json.return_value = {"server_keys": [response]}
        keys = get_key_from_perspectives(response)
        self.assertEqual(keys, {}, "Expected empty dict with missing origin server sig")


def get_key_id(key):
    """Get the matrix ID tag for a given SigningKey or VerifyKey"""
    return "%s:%s" % (key.alg, key.version)


@defer.inlineCallbacks
def run_in_context(f, *args, **kwargs):
    with LoggingContext("testctx") as ctx:
        # we set the "request" prop to make it easier to follow what's going on in the
        # logs.
        ctx.request = "testctx"
        rv = yield f(*args, **kwargs)
    defer.returnValue(rv)


def _verify_json_for_server(keyring, server_name, json_object, validity_time):
    """thin wrapper around verify_json_for_server which makes sure it is wrapped
    with the patched defer.inlineCallbacks.
    """

    @defer.inlineCallbacks
    def v():
        rv1 = yield keyring.verify_json_for_server(
            server_name, json_object, validity_time
        )
        defer.returnValue(rv1)

    return run_in_context(v)
