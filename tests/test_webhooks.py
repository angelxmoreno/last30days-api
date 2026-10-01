import asyncio
import hashlib
import hmac

import httpx

from app import webhooks


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_signature_matches_hmac() -> None:
    body = b'{"a":1}'
    expected = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert webhooks.sign("k", body) == expected


def test_private_targets_detected() -> None:
    for url in (
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://localhost/",
        "http:///nohost",
    ):
        assert webhooks.is_private_target(url), url


def test_deliver_retries_then_succeeds() -> None:
    seen: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(500 if len(seen) < 3 else 200)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    ok = run(
        webhooks.deliver(
            "http://hook.test/x",
            b"{}",
            "sec",
            allow_private=True,
            sleep=fake_sleep,
            transport=httpx.MockTransport(handler),
        )
    )
    assert ok and len(seen) == 3 and sleeps == [1.0, 4.0]
    assert seen[0].headers["X-Signature"] == webhooks.sign("sec", b"{}")


def test_deliver_gives_up_after_three_retries() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async def no_sleep(s: float) -> None:
        return None

    ok = run(
        webhooks.deliver(
            "http://hook.test/x",
            b"{}",
            "s",
            allow_private=True,
            sleep=no_sleep,
            transport=httpx.MockTransport(handler),
        )
    )
    assert not ok and len(calls) == 4


def test_deliver_blocks_private_when_not_allowed() -> None:
    called = []
    ok = run(
        webhooks.deliver(
            "http://127.0.0.1/x",
            b"{}",
            "s",
            allow_private=False,
            transport=httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200)),
        )
    )
    assert not ok and not called
