import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import ai
import dxcc
import main
from background import network_call
from cat import Rig
from webserver import WebServer, WebSocket


class Socket:
    peer = ('127.0.0.1', 12345)
    def __init__(self):
        self.messages, self.closes = [], 0
    def send_json(self, message):
        self.messages.append(message)
    async def close(self):
        self.closes += 1


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base_patch = patch.object(main, 'BASE', self.directory.name)
        self.base_patch.start()
        self.download_patch = patch.object(dxcc, 'update_cty')
        self.download_patch.start()
        self.station = main.Station(dict(main.DEFAULT_CFG, language='en'), force_sim=True)
        await self.station.init()

    async def asyncTearDown(self):
        await self.station.shutdown()
        self.base_patch.stop()
        self.download_patch.stop()
        self.directory.cleanup()

    def client(self):
        client = main.Client(Socket(), True)
        client.authed = client.admin = True
        self.station.clients.add(client)
        return client

    async def test_shutdown_is_idempotent_even_without_radio(self):
        st, client = self.station, self.client()
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def job():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        st.spawn(job())
        await started.wait()
        st.request_shutdown()
        await asyncio.gather(st.shutdown(), st.shutdown())
        self.assertTrue(st.quit_event.is_set())
        self.assertTrue(cleaned.is_set())
        self.assertFalse(st._background)
        self.assertEqual(client.ws.closes, 1)
        self.assertNotIn(st._on_log, main.LOGBUF.listeners)
        self.assertEqual(sum(m['t'] == 'server_stopping' for m in client.ws.messages), 1)

    async def test_transmit_finalizer_finishes_before_radio_handles_close(self):
        st, order = self.station, []
        rig = SimpleNamespace(state={'connected': True}, close=lambda: order.append('rig closed'))
        st.rig, st.audio, st.running = rig, SimpleNamespace(stop=lambda: order.append('audio stopped')), True
        ready = asyncio.Event()
        async def transmission():
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.assertIs(st.rig, rig)
                await asyncio.sleep(0)
                order.append('PTT released')
        st.digi.tx_task = st.spawn(transmission())
        await ready.wait()
        await st.stop_radio()
        self.assertEqual(order, ['PTT released', 'rig closed', 'audio stopped'])
        self.assertIsNone(st.rig)
        self.assertFalse(st.running)

    async def test_exit_is_not_blocked_by_lm_studio_request(self):
        st, client = self.station, self.client()
        entered, release = threading.Event(), threading.Event()
        def models():
            entered.set()
            release.wait(10)
            return []
        try:
            with patch.object(ai, 'LmStudio', return_value=SimpleNamespace(models=models)):
                await asyncio.wait_for(st._admin_msg(client, {'t': 'admin_ai'}), 0.5)
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                await asyncio.wait_for(st._admin_msg(client, {'t': 'admin_quit'}), 0.5)
                await asyncio.wait_for(st.shutdown(), 1)
                self.assertTrue(st.quit_event.is_set())
        finally:
            release.set()

    async def test_exit_saves_latest_panel_values_before_requesting_shutdown(self):
        client = self.client()
        await self.station._admin_msg(client, {'t': 'admin_quit', 'cfg': {'max_tx_seconds': 181}})
        saved = json.loads((Path(self.directory.name) / 'config.json').read_text())
        self.assertEqual(saved['max_tx_seconds'], 181)
        self.assertTrue(self.station.quit_event.is_set())

    async def test_failed_settings_save_does_not_exit(self):
        client = self.client()
        with patch.object(main, 'save_cfg', side_effect=OSError('disk full')):
            await self.station._admin_msg(client, {'t': 'admin_quit', 'cfg': {'max_tx_seconds': 181}})
        self.assertFalse(self.station.shutting_down)
        self.assertEqual(client.ws.messages[-1]['t'], 'admin_quit_failed')

    async def test_new_commands_cannot_restart_radio_after_exit(self):
        client = self.client()
        self.station.request_shutdown()
        with patch.object(self.station, 'start_radio') as start:
            await self.station._on_msg(client, {'t': 'admin_start'})
        start.assert_not_called()
        self.assertEqual(client.ws.messages[-1]['t'], 'server_stopping')

    async def test_listener_tasks_are_awaited_only_after_clients_close(self):
        st, client = self.station, self.client()
        closed = asyncio.Event()
        async def close_client():
            client.ws.closes += 1
            closed.set()
        client.ws.close = close_client
        class Server:
            def close(self):
                pass
            async def wait_closed(self):
                await closed.wait()
            async def serve_forever(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    await self.wait_closed()
        cfg = dict(st.cfg, auto_connect=False)
        def tray(*args):
            asyncio.get_running_loop().call_later(0.01, st.request_shutdown)
        with patch.object(main, 'Station', return_value=st), patch.object(main, 'load_cfg', return_value=cfg), \
                patch.object(main.asyncio, 'start_server', return_value=Server()), patch.object(main, 'ensure_cert', return_value=(None, None)), \
                patch.object(main, 'start_tray', side_effect=tray):
            await asyncio.wait_for(main.amain(SimpleNamespace(sim=False, background=True)), 1)
        self.assertTrue(closed.is_set())

    async def test_listener_failure_still_cleans_up_station(self):
        with patch.object(main, 'Station', return_value=self.station), patch.object(main, 'load_cfg', return_value=self.station.cfg), \
                patch.object(main.asyncio, 'start_server', side_effect=OSError('port busy')), patch.object(main, 'message_box'):
            await main.amain(SimpleNamespace(sim=True, background=True))
        self.assertTrue(self.station.shutting_down)
        self.assertFalse(self.station._background)


class Writer:
    def __init__(self, slow=False):
        self.slow, self.closed, self.frames = slow, False, []
    def get_extra_info(self, key):
        return ('127.0.0.1', 12345) if key == 'peername' else None
    def write(self, data):
        self.frames.append(data)
    async def drain(self):
        if self.slow:
            await asyncio.Event().wait()
    def close(self):
        self.closed = True
    async def wait_closed(self):
        pass


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_websocket_does_not_hold_shutdown_open(self):
        writer = Writer(slow=True)
        ws = WebSocket(asyncio.StreamReader(), writer, writer.get_extra_info('peername'))
        ws.send_json({'t': 'server_stopping'})
        await asyncio.wait_for(asyncio.gather(ws.close(), ws.close()), 1.5)
        self.assertTrue(writer.closed)
        self.assertTrue(ws._sender.done())

    async def test_server_close_cancels_pending_http_connections(self):
        app, reader, writer = WebServer(ROOT / 'web', None), asyncio.StreamReader(), Writer()
        task = asyncio.create_task(app.handle(reader, writer))
        await asyncio.sleep(0)
        await asyncio.wait_for(app.close(), 1)
        self.assertTrue(task.cancelled())
        self.assertTrue(writer.closed)
        self.assertFalse(app.handlers)


class ProcessTests(unittest.TestCase):
    def test_cancelled_network_request_does_not_keep_python_process_alive(self):
        source = """
import asyncio, sys, threading
sys.path.insert(0, sys.argv[1])
from background import network_call
async def check():
    entered = threading.Event()
    def blocked():
        entered.set()
        threading.Event().wait(30)
    task = asyncio.create_task(network_call(blocked))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
asyncio.run(check())
print('closed')
"""
        completed = subprocess.run([sys.executable, '-c', source, str(ROOT / 'server')], capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('closed', completed.stdout)

    def test_cat_failure_still_releases_independent_ptt(self):
        calls = []
        class Transport:
            def write(self, data):
                calls.append(data)
                raise OSError('CAT cable removed')
            def close(self):
                calls.append('CAT closed')
        serial = SimpleNamespace(set=lambda on: calls.append(('PTT', on)), ser=SimpleNamespace(close=lambda: calls.append('PTT closed')))
        rig = Rig(Transport(), lambda *args: None, serial_ptt=serial)
        rig.close()
        self.assertEqual(calls, [b'KY00;TX0;', ('PTT', False), 'CAT closed', 'PTT closed'])
