from __future__ import annotations

import unittest

from tmdb import TmdbClient, TmdbError


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return self.payload


class FakeSession:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def get(self, *args, **kwargs):
        return FakeResponse(
            self.status,
            self.payload,
        )


class TmdbHttpErrorTests(
    unittest.IsolatedAsyncioTestCase
):
    async def assert_http_status(self, status):
        client = TmdbClient(
            FakeSession(
                status,
                {"status_message": "Synthetic HTTP error"},
            ),
            "synthetic-test-token",
        )

        with self.assertRaises(TmdbError) as caught:
            await client.lookup_result("tv", 200)

        self.assertEqual(
            getattr(caught.exception, "status_code", None),
            status,
        )

        self.assertEqual(
            getattr(caught.exception, "is_not_found", None),
            status == 404,
        )

    async def test_missing_identity_404(self):
        await self.assert_http_status(404)

    async def test_unauthorized_401(self):
        await self.assert_http_status(401)

    async def test_forbidden_403(self):
        await self.assert_http_status(403)

    async def test_rate_limit_429(self):
        await self.assert_http_status(429)

    async def test_server_error_500(self):
        await self.assert_http_status(500)

    async def test_service_unavailable_503(self):
        await self.assert_http_status(503)

    async def test_non_object_error_response(self):
        client = TmdbClient(
            FakeSession(503, ["invalid error payload"]),
            "synthetic-test-token",
        )

        with self.assertRaises(TmdbError) as caught:
            await client.lookup_result("tv", 200)

        self.assertEqual(
            getattr(caught.exception, "status_code", None),
            503,
        )

    async def test_malformed_success_is_not_missing_identity(self):
        client = TmdbClient(
            FakeSession(200, ["invalid success payload"]),
            "synthetic-test-token",
        )

        with self.assertRaises(TmdbError) as caught:
            await client.lookup_result("tv", 200)

        self.assertFalse(
            getattr(caught.exception, "is_not_found", False)
        )


if __name__ == "__main__":
    unittest.main()
