"""Exercise real packaged startup, client disconnect, Exit and restart on Windows."""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def fetch(port, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:%d%s' % (port, path), timeout=1) as response:
        return response.read()


class Panel:
    def __init__(self, port):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=3)
        self.reader = self.sock.makefile('rb')
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET /ws HTTP/1.1\r\nHost: localhost:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n' % (port, key)).encode())
        assert b'101' in self.reader.readline()
        while self.reader.readline().strip():
            pass
        self.send({'t': 'admin_hello'})
        self.until('admin_init')

    def send(self, message):
        data = json.dumps(message).encode()
        mask = os.urandom(4)
        header = bytes([0x81, 0x80 | len(data)]) if len(data) < 126 else b'\x81\xfe' + struct.pack('!H', len(data))
        self.sock.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(data)))

    def until(self, kind):
        for _ in range(200):
            header = self.reader.read(2)
            assert len(header) == 2, 'WebSocket closed before ' + kind
            size = header[1] & 127
            if size == 126:
                size = struct.unpack('!H', self.reader.read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self.reader.read(8))[0]
            data = self.reader.read(size)
            if header[0] & 15 == 1:
                message = json.loads(data)
                if message['t'] == kind:
                    return message
        raise AssertionError('No WebSocket response: ' + kind)

    def close(self):
        self.reader.close()
        self.sock.close()


def check(executable):
    entered, release = threading.Event(), threading.Event()
    class Model(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(30)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data": []}')
            except OSError:
                pass
        def log_message(self, *args):
            pass
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.daemon_threads = True
    threading.Thread(target=model.serve_forever, daemon=True).start()
    process, panel = None, None
    temporary = tempfile.TemporaryDirectory()
    report = []
    try:
        base = Path(temporary.name)
        app = base / 'FTX1-Pult.exe'
        shutil.copy2(executable, app)
        port, secure_port = free_port(), free_port()
        config = {'language': 'en', 'open_panel': False, 'open_pult': False,
                  'auto_connect': True, 'http_port': port, 'https_port': secure_port,
                  'ai_url': 'http://127.0.0.1:%d/v1' % model.server_port}
        config_path = base / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        def start():
            running = subprocess.Popen([str(app), '--sim', '--background'], cwd=base)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if running.poll() is not None:
                    raise AssertionError('Application exited during startup: %s' % running.returncode)
                try:
                    assert fetch(port, '/api/ping').startswith(b'FTX1-PULT ')
                    return running
                except (OSError, AssertionError):
                    time.sleep(0.1)
            running.kill()
            raise AssertionError('Server did not start')
        process = start()
        assert json.loads(fetch(port, '/api/language'))['language'] == 'en'
        assert json.loads(fetch(port, '/manifest.webmanifest'))['lang'] == 'en'
        assert b'id="language"' in fetch(port, '/panel.html')
        assert b'btnQuitTop' in fetch(port, '/panel.html')
        assert fetch(port, '/locales/en.json')
        assert fetch(port, '/i18n.js')
        report.append('OK English locale, manifest, panel selector and top Exit button')
        # Closing only the panel is intentionally different from Exit.
        panel = Panel(port)
        panel.close()
        panel = None
        assert fetch(port, '/api/ping')
        report.append('OK closing a client leaves the server running')
        panel = Panel(port)
        panel.send({'t': 'admin_ai'})
        assert entered.wait(5), 'Slow model request did not start'
        started = time.monotonic()
        panel.send({'t': 'admin_quit', 'cfg': {'max_tx_seconds': 181}})
        process.wait(timeout=8)
        assert process.returncode == 0, process.returncode
        assert json.loads(config_path.read_text())['max_tx_seconds'] == 181
        elapsed = time.monotonic() - started
        assert elapsed < 4, 'Exit waited %.2f seconds for the model request' % elapsed
        report.append('OK Exit during blocked model request: %.2f seconds; settings saved' % elapsed)
        panel.close()
        panel = None
        release.set()
        try:
            fetch(port, '/api/ping')
        except OSError:
            pass
        else:
            raise AssertionError('HTTP listener remained open after Exit')
        report.append('OK process exited and listener released')
        process = start()
        panel = Panel(port)
        panel.send({'t': 'admin_quit'})
        process.wait(timeout=8)
        assert process.returncode == 0, process.returncode
        report.append('OK restart on the same port and second clean Exit')
        print('\n'.join(report))
        return '\n'.join(report) + '\nRESULT OK\n'
    finally:
        release.set()
        if panel is not None:
            panel.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        model.shutdown()
        model.server_close()
        temporary.cleanup()


if __name__ == '__main__':
    executable = Path(sys.argv[1]).resolve()
    report = check(executable)
    executable.with_name('lifecycle.txt').write_text(report, encoding='utf-8')
