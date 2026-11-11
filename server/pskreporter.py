"""
PSK Reporter (pskreporter.info): отправка принятых станций и запрос «где меня слышат».

Отправка — UDP в формате IPFIX на report.pskreporter.info:4739, как в WSJT-X / JTDX / MSHV
(описание: https://pskreporter.info/pskdev.html). Принятые позывные копятся и уходят пачкой
раз в ~5 минут; один и тот же позывной на одном диапазоне — не чаще раза в 30 минут.

«Где меня слышат» — https://retrieve.pskreporter.info/query (не чаще раза в 5 минут, как просит сайт).
"""
import logging
import os
import random
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("psk")

HOST, PORT = 'report.pskreporter.info', 4739
ENT = 30351                                   # «enterprise number» PSK Reporter

# Шаблоны (как в описании протокола)
RX_TEMPLATE = bytes.fromhex(
    '0003002C99920004 0000'
    '8002FFFF0000768F'      # receiverCallsign
    '8004FFFF0000768F'      # receiverLocator
    '8008FFFF0000768F'      # decodingSoftware
    '8009FFFF0000768F'      # antennaInformation
    '0000'.replace(' ', ''))
TX_TEMPLATE = bytes.fromhex(
    '0002003C99930007'
    '8001FFFF0000768F'      # senderCallsign
    '800500040000768F'      # frequency (Гц, 4 байта)
    '800600010000768F'      # sNR (дБ, 1 байт со знаком)
    '800AFFFF0000768F'      # mode
    '8003FFFF0000768F'      # senderLocator
    '800B00010000768F'      # informationSource (1 — расшифровано автоматически)
    '00960004')             # flowStartSeconds


def _s(v):
    b = str(v or '').encode('utf-8')[:254]
    return bytes([len(b)]) + b


def _set(set_id, body):
    pad = (-(len(body) + 4)) % 4
    body += b'\0' * pad
    return struct.pack('>HH', set_id, len(body) + 4) + body


def build_packet(rx_call, rx_grid, software, antenna, spots, seq, domain, with_templates=True):
    """spots: [(call, freq_hz, snr, mode, grid, t)] -> датаграмма IPFIX."""
    rx = _set(0x9992, _s(rx_call) + _s(rx_grid) + _s(software) + _s(antenna))
    tx = b''.join(_s(c) + struct.pack('>I', int(f) & 0xFFFFFFFF) + struct.pack('>b', max(-128, min(127, int(snr))))
                  + _s(m) + _s(g) + b'\x01' + struct.pack('>I', int(t)) for c, f, snr, m, g, t in spots)
    body = (RX_TEMPLATE + TX_TEMPLATE if with_templates else b'') + rx + _set(0x9993, tx)
    return struct.pack('>HHIII', 0x000A, len(body) + 16, int(time.time()), seq, domain) + body


class PskReporter:
    def __init__(self, cfg, version):
        self.cfg = cfg
        self.version = version
        self.queue = []
        self.sent = {}                # (call, band) -> время отправки
        self.seq = 0
        self.domain = random.getrandbits(32)
        self.n_sent = 0
        self.t_flush = time.time() + 60 + random.uniform(0, 30)
        self.t_tpl = 0                # когда последний раз отправляли шаблоны
        self.n_pkt = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.heard = []               # кто слышит нас
        self.heard_t = 0
        self.heard_busy = False
        self.heard_err = ''

    @property
    def on(self):
        return bool(self.cfg.get('psk_on', True))

    def add(self, call, freq, snr, mode, grid, band):
        if not self.on or not call or '<' in call or '/' in call and len(call) > 12:
            return
        key = (call, band)
        now = time.time()
        if now - self.sent.get(key, 0) < 1800:
            return
        self.sent[key] = now
        self.queue.append((call, int(freq), snr, mode, grid or '', int(now)))

    def tick(self, my_call, my_grid):
        """Вызывается периодически: раз в ~5 минут отправляем накопленное."""
        now = time.time()
        if now < self.t_flush:
            return
        self.t_flush = now + 300 + random.uniform(-20, 20)
        if len(self.sent) > 5000:
            self.sent = {k: v for k, v in self.sent.items() if now - v < 1800}
        if not self.on or not self.queue or not my_call or not my_grid:
            self.queue = [] if not self.on else self.queue
            return
        spots, self.queue = self.queue, []
        threading.Thread(target=self._send, args=(my_call, my_grid, spots), daemon=True).start()

    def _send(self, my_call, my_grid, spots):
        try:
            addr = socket.gethostbyname(HOST)
        except OSError as e:
            log.info("PSK Reporter: нет связи с %s (%s)", HOST, e)
            return
        tpl = self.n_pkt < 3 or time.time() - self.t_tpl > 3600
        ant = str(self.cfg.get('psk_antenna', '') or '')
        soft = 'FTX1-Pult %s' % self.version
        # пачками, чтобы датаграмма не превышала ~1400 байт
        chunk = []
        for s in spots + [None]:
            if s is not None:
                chunk.append(s)
            if chunk and (s is None or len(chunk) >= 40):
                self.seq += 1
                pkt = build_packet(my_call, my_grid, soft, ant, chunk, self.seq, self.domain, tpl)
                try:
                    self.sock.sendto(pkt, (addr, PORT))
                    self.n_pkt += 1
                    self.n_sent += len(chunk)
                    if tpl:
                        self.t_tpl = time.time()
                except OSError as e:
                    log.info("PSK Reporter: %s", e)
                chunk = []
                tpl = False
        log.info("PSK Reporter: отправлено %d станций", len(spots))

    # ------------------------------------------------------------------ где меня слышат
    def query_heard(self, my_call, on_done, minutes=30):
        if self.heard_busy or not my_call or time.time() - self.heard_t < 300:
            return False
        self.heard_busy = True
        self.heard_t = time.time()

        def run():
            try:
                q = urllib.parse.urlencode({'senderCallsign': my_call, 'flowStartSeconds': -60 * minutes,
                                            'rronly': 1, 'noactive': 1, 'appcontact': 'FTX1-Pult'})
                req = urllib.request.Request('https://retrieve.pskreporter.info/query?' + q,
                                             headers={'User-Agent': 'FTX1-Pult/%s' % self.version})
                data = urllib.request.urlopen(req, timeout=30).read()
                self.heard = parse_heard(data)
                self.heard_err = ''
            except Exception as e:
                self.heard_err = str(e)
                log.info("PSK Reporter: запрос не удался: %s", e)
            finally:
                self.heard_busy = False
                on_done()
        threading.Thread(target=run, daemon=True).start()
        return True


def parse_heard(data):
    """XML ответа retrieve.pskreporter.info -> список приёмов."""
    root = ET.fromstring(data)
    out = []
    for r in root.iter('receptionReport'):
        a = r.attrib
        try:
            out.append({'call': a.get('receiverCallsign', ''), 'grid': a.get('receiverLocator', '')[:6],
                        'freq': int(a.get('frequency') or 0), 'snr': int(a['sNR']) if a.get('sNR') else None,
                        'mode': a.get('mode', ''), 't': int(a.get('flowStartSeconds') or 0),
                        'country': a.get('receiverDXCC', '')})
        except ValueError:
            continue
    return out
