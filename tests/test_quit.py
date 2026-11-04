import asyncio
import threading
import time
import uuid

import pytest

from src import app as appmod
from src.media.manager import PlayerManager
from src.platform import single_instance as si
from src.server import AppControl, create_app
from src.settings import SettingsStore

from .fakes import FakeBackend


def make_client_app(tmp_path, launched_by):
    calls = []
    control = AppControl(launched_by=launched_by, quit=lambda: calls.append(1))
    store = SettingsStore(str(tmp_path / "s.json"))
    return create_app(PlayerManager(FakeBackend()), store, control=control), calls


@pytest.mark.parametrize("launched_by,only_if,expect", [
    ("obs", "obs", True),        # OBS started it -> OBS may close it
    ("user", "obs", False),      # user started it -> closing OBS leaves it running
    ("autostart", "obs", False),
    ("user", None, True),        # plain --quit always quits
])
async def test_quit_api(aiohttp_client, tmp_path, launched_by, only_if, expect):
    app, calls = make_client_app(tmp_path, launched_by)
    client = await aiohttp_client(app)
    body = {"only_if_launched_by": only_if} if only_if else {}
    d = await (await client.post("/api/quit", json=body)).json()
    assert d == {"quitting": expect, "launched_by": launched_by}
    await asyncio.sleep(0.25)
    assert calls == ([1] if expect else [])


async def test_quit_api_rejects_foreign_origin(aiohttp_client, tmp_path):
    app, calls = make_client_app(tmp_path, "obs")
    client = await aiohttp_client(app)
    r = await client.post("/api/quit", json={}, headers={"Origin": "https://evil.example"})
    assert r.status == 403 and calls == []


def test_request_quit_with_nothing_running(tmp_path):
    lock = si.SingleInstance(f"np-test-{uuid.uuid4().hex}", lock_dir=str(tmp_path))
    assert appmod.request_quit("obs", lock=lock, info_path=str(tmp_path / "i.json")) == 0


async def test_request_quit_reaches_running_instance(aiohttp_server, tmp_path):
    name = f"np-test-{uuid.uuid4().hex}"
    holder = si.SingleInstance(name, lock_dir=str(tmp_path))
    assert holder.acquire()  # pretend an instance is running
    app, calls = make_client_app(tmp_path, "obs")
    server = await aiohttp_server(app)
    info = str(tmp_path / "instance.json")
    si.write_info(server.port, info)
    probe = si.SingleInstance(name, lock_dir=str(tmp_path))
    rc = await asyncio.get_running_loop().run_in_executor(None, appmod.request_quit, "obs", probe, info)
    await asyncio.sleep(0.25)
    holder.release()
    assert rc == 0 and calls == [1]


def test_wait_for_lock_covers_an_instance_that_is_quitting(tmp_path):
    name = f"np-test-{uuid.uuid4().hex}"
    holder = si.SingleInstance(name, lock_dir=str(tmp_path))
    assert holder.acquire()
    threading.Timer(0.6, holder.release).start()
    waiter = si.SingleInstance(name, lock_dir=str(tmp_path))
    t = time.monotonic()
    assert appmod.wait_for_lock(waiter, 4.0) is True
    assert time.monotonic() - t < 2.0
    waiter.release()


def test_launched_by_from_flags():
    mk = lambda *a: appmod.App(appmod.parse_args(list(a))).launched_by  # noqa: E731
    assert mk() == "user" and mk("--autostart") == "autostart" and mk("--from-obs") == "obs"
