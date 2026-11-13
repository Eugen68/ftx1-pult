"""
Мини HTTP/HTTPS + WebSocket (RFC 6455) сервер на asyncio без внешних библиотек.
Отдаёт статику веб-пульта и держит WebSocket /ws.
"""

from i18n import tr
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import struct
import logging

log = logging.getLogger("web")
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('application/manifest+json', '.webmanifest')
mimetypes.add_type('image/svg+xml', '.svg')


class WebSocket:
    def __init__(self, reader, writer, peer):
        self.r, self.w, self.peer = reader, writer, peer
        self.closed = False
        self._close_task = None
        self.q = asyncio.Queue(maxsize=120)
        self._sender = asyncio.create_task(self._send_loop())

    # -------- отправка (через очередь, чтобы медленный клиент не тормозил всех)
    def _frame(self, opcode, payload):
        n = len(payload)
        if n < 126:
            hdr = struct.pack('!BB', 0x80 | opcode, n)
        elif n < 65536:
            hdr = struct.pack('!BBH', 0x80 | opcode, 126, n)
        else:
            hdr = struct.pack('!BBQ', 0x80 | opcode, 127, n)
        return hdr + payload

    def send_text(self, s):
        self._enqueue(self._frame(1, s.encode('utf-8')), important=True)

    def send_json(self, obj):
        self.send_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))

    def send_bin(self, b, important=False):
        self._enqueue(self._frame(2, b), important)

    def _enqueue(self, data, important):
        if self.closed:
            return
        try:
            self.q.put_nowait(data)
        except asyncio.QueueFull:
            if important:
                # выкидываем самый старый кадр (обычно звук/водопад), служебное — важнее
                try:
                    self.q.get_nowait()
                    self.q.task_done()
                    self.q.put_nowait(data)
                except Exception:
                    pass
            # медиакадр при переполнении просто теряем

    async def _send_loop(self):
        try:
            while True:
                data = await self.q.get()
                try:
                    self.w.write(data)
                    await self.w.drain()
                finally:
                    self.q.task_done()
        except Exception:
            self.closed = True

    # -------- приём
    async def recv(self):
        """-> ('text', str) | ('bin', bytes) | (None, None) при закрытии."""
        msg, msg_op = b'', None
        while True:
            try:
                h = await self.r.readexactly(2)
            except Exception:
                return None, None
            fin, op = h[0] & 0x80, h[0] & 0x0F
            masked, n = h[1] & 0x80, h[1] & 0x7F
            if n == 126:
                n = struct.unpack('!H', await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', await self.r.readexactly(8))[0]
            if n > 4_000_000:
                return None, None
            mask = await self.r.readexactly(4) if masked else b'\0\0\0\0'
            data = bytearray(await self.r.readexactly(n))
            if masked:
                m = (mask * (n // 4 + 1))[:n]
                data = bytes(a ^ b for a, b in zip(data, m)) if n < 64 else \
                    (int.from_bytes(data, 'big') ^ int.from_bytes(m, 'big')).to_bytes(n, 'big')
            data = bytes(data)
            if op == 8:                          # close
                self.closed = True
                return None, None
            if op == 9:                          # ping -> pong
                self._enqueue(self._frame(10, data), True)
                continue
            if op == 10:
                continue
            if op in (1, 2):
                msg, msg_op = data, op
            elif op == 0:
                msg += data
            if fin:
                if msg_op == 1:
                    return 'text', msg.decode('utf-8', 'replace')
                return 'bin', msg

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        self.closed = True
        try:
            # Deliver the shutdown notice, but never wait indefinitely for a
            # disconnected/slow client. Media data may be discarded on exit.
            if not self._sender.done():
                try:
                    await asyncio.wait_for(self.q.join(), 0.25)
                except asyncio.TimeoutError:
                    pass
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
            self.w.write(self._frame(8, b''))
            await asyncio.wait_for(self.w.drain(), 0.25)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.w.close()
            try:
                await asyncio.wait_for(self.w.wait_closed(), 0.5)
            except Exception:
                pass


class WebServer:
    def __init__(self, static_dir, ws_handler, extra_routes=None, prefix_routes=None):
        self.static_dir = os.path.abspath(static_dir)
        self.ws_handler = ws_handler          # async fn(WebSocket)
        self.routes = extra_routes or {}      # path -> fn() -> (ctype, bytes)
        self.handlers = set()
        self.prefix = prefix_routes or {}     # '/rec/' -> fn(остаток, query) -> (ctype, bytes) | None

    async def close(self):
        handlers = tuple(self.handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        peer = writer.get_extra_info('peername')
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line:
                writer.close()
                return
            method, path, _ = line.decode('latin-1').split(' ', 2)
            headers = {}
            while True:
                h = await asyncio.wait_for(reader.readline(), 15)
                if h in (b'\r\n', b'\n', b''):
                    break
                k, _, v = h.decode('latin-1').partition(':')
                headers[k.strip().lower()] = v.strip()
            path, _, query = path.partition('?')

            if path == '/ws' and headers.get('upgrade', '').lower() == 'websocket':
                key = headers.get('sec-websocket-key', '')
                acc = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
                writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
                await writer.drain()
                sock = writer.get_extra_info('socket')
                try:
                    import socket
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
                ws = WebSocket(reader, writer, peer)
                try:
                    await self.ws_handler(ws)
                finally:
                    await ws.close()
                return

            if method not in ('GET', 'HEAD'):
                return await self._send(writer, 405, 'text/plain', b'Method not allowed')
            if path in self.routes:
                ctype, body = self.routes[path]()
                return await self._send(writer, 200, ctype, body)
            for pre, fn in self.prefix.items():
                if path.startswith(pre):
                    r = fn(path[len(pre):], query)
                    if not r:
                        return await self._send(writer, 404, 'text/plain', b'Not found')
                    return await self._send(writer, 200, r[0], r[1])
            if path == '/':
                path = '/index.html'
            full = os.path.abspath(os.path.join(self.static_dir, path.lstrip('/')))
            if not full.startswith(self.static_dir) or not os.path.isfile(full):
                return await self._send(writer, 404, 'text/plain', b'Not found')
            ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
            with open(full, 'rb') as f:
                body = f.read()
            await self._send(writer, 200, ctype, body if method == 'GET' else b'')
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        except Exception as e:
            log.debug(tr("HTTP ошибка %s: %s"), peer, e)
        finally:
            self.handlers.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, writer, code, ctype, body):
        reason = {200: 'OK', 404: 'Not Found', 405: 'Method Not Allowed'}.get(code, 'OK')
        if ctype.startswith('text/') or ctype in ('application/javascript', 'application/json'):
            ctype += '; charset=utf-8'
        writer.write(("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                      "Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
                      % (code, reason, ctype, len(body))).encode() + body)
        await writer.drain()
