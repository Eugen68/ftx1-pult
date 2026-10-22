"""
FTX-1 Пульт — сервер удалённого управления Yaesu FTX-1.

Обычный запуск — двойной клик по FTX1-Pult.exe (или FTX1-Pult.pyw):
открывается окно «Панель управления», значок в трее, браузер не нужен.

Ключи (для отладки):
    --sim          симулятор трансивера, без железа
    --background   запуск без окна (автозапуск вместе с Windows)
    --list         показать COM-порты и звуковые устройства и выйти
    --console      журнал в консоль
"""
import argparse
import asyncio
import collections
import ipaddress
import json
import logging
import logging.handlers
import os
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.request
import webbrowser

import numpy as np

if getattr(sys, 'frozen', False):                  # собрано в exe (PyInstaller)
    BASE = os.path.dirname(os.path.abspath(sys.argv[0]))     # config.json рядом с exe
    RES = getattr(sys, '_MEIPASS', BASE)
else:
    RES = os.path.dirname(os.path.abspath(__file__))         # папка server
    BASE = os.path.dirname(RES)                              # config.json в корне проекта
sys.path.insert(0, RES)

from cat import Rig, SerialTransport, SerialPtt, CTCSS  # noqa: E402
from audio import SR, Decimator, Interpolator, Waterfall  # noqa: E402
from webserver import WebServer  # noqa: E402

VERSION = "1.1"
APP_NAME = "FTX-1 Пульт"
log = logging.getLogger("main")
IS_WIN = sys.platform == 'win32'

DEFAULT_CFG = {
    "cat_port": "",
    "cat_baud": 38400,
    "ptt_method": "TX1",
    "ptt_port": "",
    "ptt_line": "RTS",
    "audio_device": "USB Audio CODEC",
    "audio_api": "",
    "audio_in_index": None,
    "audio_out_index": None,
    "https_port": 8443,
    "http_port": 8080,
    "http_lan": False,
    "password": "",
    "max_tx_seconds": 180,
    "auto_connect": True,
    "open_panel": True,
    "open_pult": False,
    "simulate": False,
}


def cfg_path():
    return os.path.join(BASE, 'config.json')


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(cfg_path(), encoding='utf-8') as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("config.json повреждён (%s) — использую настройки по умолчанию", e)
    return cfg


def save_cfg(cfg):
    tmp = cfg_path() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg_path())


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith(('127.', '169.254.')))


def ensure_cert():
    """Самоподписанный сертификат для HTTPS (браузер даёт микрофон только по HTTPS)."""
    crt, key = os.path.join(BASE, 'cert.pem'), os.path.join(BASE, 'key.pem')
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
    except ImportError:
        log.warning("Нет пакета cryptography — HTTPS выключен (микрофон с телефона работать не будет)")
        return None, None
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FTX-1 Remote')])
    alt = [x509.DNSName('localhost'), x509.DNSName(socket.gethostname())]
    for ip in local_ips() + ['127.0.0.1']:
        alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(k.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .sign(k, hashes.SHA256()))
    with open(crt, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key, 'wb') as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()))
    return crt, key


# ------------------------------------------------------------- журнал в окно
class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = collections.deque(maxlen=500)
        self.listeners = []
        self.setFormatter(logging.Formatter('%(asctime)s  %(message)s', '%H:%M:%S'))

    def emit(self, record):
        try:
            line = {'t': self.format(record), 'lvl': record.levelname}
            self.lines.append(line)
            for cb in list(self.listeners):
                cb(line)
        except Exception:
            pass


LOGBUF = LogBuffer()


# ------------------------------------------------------------ автозапуск
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(background=True):
    if getattr(sys, 'frozen', False):
        cmd = '"%s"' % sys.executable
    else:
        exe = sys.executable
        if IS_WIN and exe.lower().endswith('python.exe'):
            exe = exe[:-10] + 'pythonw.exe'
        cmd = '"%s" "%s"' % (exe, os.path.join(BASE, 'FTX1-Pult.pyw'))
    return cmd + (' --background' if background else '')


def autostart_get():
    if not IS_WIN:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, 'FTX1Pult')
            return True
    except OSError:
        return False


def autostart_set(on):
    if not IS_WIN:
        return None
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, 'FTX1Pult', 0, winreg.REG_SZ, launch_command(True))
        else:
            try:
                winreg.DeleteValue(k, 'FTX1Pult')
            except OSError:
                pass
    return autostart_get()


# ------------------------------------------------------------ устройства
def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    res = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        d = p.description or ''
        res.append({'device': p.device, 'desc': d,
                    'kind': 'enhanced' if 'enhanced' in d.lower() else 'standard' if 'standard' in d.lower() else '',
                    'cp210x': 'cp210' in d.lower() or 'silicon labs' in d.lower() or (p.vid == 0x10C4)})
    return res


def list_audio(refresh=False):
    try:
        from audio import list_devices
        return list_devices(refresh)
    except Exception as e:
        log.warning("Звуковые устройства недоступны: %s", e)
        return []


def detect(ports, audio):
    """Подбор настроек для FTX-1: Enhanced-порт для CAT, Standard для PTT, USB Audio CODEC."""
    out = {}
    enh = [p for p in ports if p['kind'] == 'enhanced']
    std = [p for p in ports if p['kind'] == 'standard']
    if enh:
        out['cat_port'] = enh[0]['device']
    elif any(p['cp210x'] for p in ports):
        out['cat_port'] = [p for p in ports if p['cp210x']][0]['device']
    if std:
        out['ptt_port'] = std[0]['device']
    codec = [d for d in audio if 'usb audio codec' in d['name'].lower() and d['in'] > 0]
    if codec:
        mme = [d for d in codec if d['api'] == 'MME']
        d = (mme or codec)[0]
        out['audio_device'] = d['name']
        out['audio_api'] = d['api']
    return out


# ------------------------------------------------------------------- станция
class Client:
    def __init__(self, ws, local):
        self.ws, self.local = ws, local
        self.authed = False
        self.admin = False
        self.audio = False
        self.rate = 16000
        self.wf = False
        self.wf_max = 4000
        self.console = None
        self.name = '%s:%s' % ws.peer[:2] if ws.peer else '?'
        self.interp = None


class Station:
    def __init__(self, cfg, force_sim=False):
        self.cfg = cfg
        self.force_sim = force_sim
        self.clients = set()
        self.rig = None
        self.audio = None
        self.running = False
        self.busy = False
        self.last_error = ''
        self.audio_err = None
        self.decims = {16000: Decimator(3), 8000: Decimator(6)}
        self.wf = Waterfall()
        self.ptt_owner = None
        self.ptt_since = 0.0
        self.ptt_alive = 0.0
        self.loop = None
        self.quit_event = None
        self.https_ok = False
        self.ports_cache, self.audio_cache = [], []
        with open(os.path.join(RES, 'menu_ftx1.json'), encoding='utf-8') as f:
            self.menu = json.load(f)
        LOGBUF.listeners.append(self._on_log)

    @property
    def sim(self):
        return self.force_sim or bool(self.cfg.get('simulate'))

    async def init(self):
        self.loop = asyncio.get_running_loop()
        self.quit_event = asyncio.Event()
        asyncio.create_task(self._watchdog())
        asyncio.create_task(self._admin_loop())

    # ----- запуск/остановка связи с трансивером
    async def start_radio(self):
        if self.running or self.busy:
            return
        self.busy = True
        self.last_error = ''
        self.audio_err = None
        cfg = self.cfg
        try:
            serial_ptt = None
            if self.sim:
                from sim import SimRadio, SimTransport, SimAudio
                radio = SimRadio()
                transport, audio = SimTransport(radio), SimAudio(radio)
                log.info("Запуск в режиме СИМУЛЯТОРА (трансивер не нужен)")
            else:
                if not cfg.get('cat_port'):
                    raise RuntimeError("Не выбран COM-порт трансивера")
                from audio import SoundDeviceAudio
                transport = SerialTransport(cfg['cat_port'], int(cfg['cat_baud']))
                audio = SoundDeviceAudio(cfg['audio_device'] or 'USB Audio CODEC', cfg.get('audio_in_index'),
                                         cfg.get('audio_out_index'), api=cfg.get('audio_api', ''))
                if cfg['ptt_method'] in ('RTS', 'DTR') and cfg.get('ptt_port'):
                    serial_ptt = SerialPtt(cfg['ptt_port'], cfg['ptt_method'])
                log.info("Подключаюсь к трансиверу: %s, %s бод", cfg['cat_port'], cfg['cat_baud'])
            rig = Rig(transport, self._on_rig_change,
                      'RTS' if cfg['ptt_method'] in ('RTS', 'DTR') else cfg['ptt_method'], serial_ptt)
            try:
                await rig.start()
            except Exception as e:
                raise RuntimeError("Порт %s не открывается: %s. Возможно, он занят другой программой "
                                   "(WSJT-X, N1MM, Log4OM…) или выбран не тот порт." % (cfg['cat_port'], e))
            self.rig = rig
            self.audio = audio
            try:
                audio.start(self._rx_from_thread)
                log.info("Звук трансивера подключён")
            except Exception as e:
                self.audio_err = str(e).split('\n')[0]
                log.error("Звук не запущен: %s", self.audio_err)
            self.running = True
            log.info("Связь с трансивером запущена")
            self._broadcast_hello_state()
        except Exception as e:
            self.last_error = str(e)
            log.error("%s", e)
        finally:
            self.busy = False
            self.push_status()

    async def stop_radio(self):
        if not self.running:
            return
        if self.ptt_owner:
            self.set_ptt(None, False)
            await asyncio.sleep(0.15)
        try:
            self.rig.close()
        except Exception:
            pass
        try:
            self.audio.stop()
        except Exception:
            pass
        self.rig = self.audio = None
        self.running = False
        log.info("Связь с трансивером остановлена")
        self.broadcast({'t': 'state', 's': {'connected': False}})
        self.push_status()

    def _rx_from_thread(self, x):
        try:
            self.loop.call_soon_threadsafe(self._on_rx_audio, x)
        except RuntimeError:
            pass

    # ----- приём звука: раздаём клиентам и строим водопад
    def _on_rx_audio(self, x):
        need = {c.rate for c in self.clients if c.audio and c.authed}
        for rate in need:
            y = self.decims[rate].process(x)
            pkt = b'\x01' + (np.clip(y, -1, 1) * 32767).astype('<i2').tobytes()
            for c in self.clients:
                if c.audio and c.authed and c.rate == rate:
                    c.ws.send_bin(pkt)
        rows = self.wf.process(x)
        if rows and any(c.wf for c in self.clients):
            for row in rows:
                for c in self.clients:
                    if c.wf and c.authed:
                        n = min(len(row), int(c.wf_max / self.wf.bin_hz) + 1)
                        c.ws.send_bin(b'\x02' + struct.pack('<Hf', n, self.wf.bin_hz) + row[:n].tobytes())

    def _on_rig_change(self, diff):
        self.broadcast({'t': 'state', 's': diff})
        if 'connected' in diff or 'tx' in diff:
            self.push_status()

    def _broadcast_hello_state(self):
        if self.rig:
            self.broadcast({'t': 'state', 's': self.rig.state, 'full': True})

    def broadcast(self, obj):
        for c in self.clients:
            if c.authed:
                c.ws.send_json(obj)

    def _ptt_info(self):
        for c in self.clients:
            if c.authed:
                c.ws.send_json({'t': 'ptt_owner', 'you': self.ptt_owner is c,
                                'busy': self.ptt_owner is not None, 'who': self.ptt_owner.name if self.ptt_owner else ''})

    def set_ptt(self, client, on):
        if not self.rig:
            if on and client:
                client.ws.send_json({'t': 'note', 'msg': 'Сервер не подключён к трансиверу'})
            return
        if on:
            if self.ptt_owner not in (None, client):
                client.ws.send_json({'t': 'note', 'msg': 'Передача занята другим клиентом (%s)' % self.ptt_owner.name})
                return
            if self.ptt_owner is None:
                self.audio.tx.clear()
                self.ptt_since = time.monotonic()
                log.info("Передача: %s", client.name)
            self.ptt_owner = client
            self.ptt_alive = time.monotonic()
            self.rig.ptt(True)
        else:
            if self.ptt_owner is client or client is None:
                self.ptt_owner = None
                self.rig.ptt(False)
                self.audio.tx.clear()
        self._ptt_info()
        self.push_status()

    async def _watchdog(self):
        """Защита от «залипшей» передачи: пропала связь или превышено время."""
        while True:
            await asyncio.sleep(0.25)
            try:
                if self.ptt_owner:
                    now = time.monotonic()
                    if now - self.ptt_alive > 1.5:
                        log.warning("PTT снят: нет подтверждений от клиента %s", self.ptt_owner.name)
                        self.ptt_owner.ws.send_json({'t': 'note', 'msg': 'Передача снята: потеря связи'})
                        self.set_ptt(None, False)
                    elif now - self.ptt_since > self.cfg['max_tx_seconds']:
                        log.warning("PTT снят: превышено время передачи")
                        self.ptt_owner.ws.send_json({'t': 'note', 'msg': 'Передача снята: превышено время (%d с)' % self.cfg['max_tx_seconds']})
                        self.set_ptt(None, False)
                if self.audio:
                    lv = {'t': 'lv', 'rx': round(getattr(self.audio, 'rx_level', 0), 4), 'tx': round(self.audio.tx.level, 4)}
                    self.broadcast(lv)
            except Exception as e:
                log.debug("watchdog: %s", e)

    # ----- панель управления (только с этого ПК)
    def _on_log(self, line):
        if not self.loop:
            return
        try:
            self.loop.call_soon_threadsafe(self._send_admin, {'t': 'admin_log', 'line': line})
        except RuntimeError:
            pass

    def _send_admin(self, obj):
        for c in self.clients:
            if c.admin:
                c.ws.send_json(obj)

    def urls(self):
        u = [{'kind': 'local', 'url': 'http://localhost:%d' % self.cfg['http_port']}]
        if self.https_ok:
            for ip in local_ips():
                u.append({'kind': 'lan', 'url': 'https://%s:%d' % (ip, self.cfg['https_port'])})
        return u

    def status(self):
        st = self.rig.state if self.rig else {}
        return {
            'running': self.running, 'busy': self.busy, 'sim': self.sim,
            'connected': bool(st.get('connected')), 'tx': bool(st.get('tx')),
            'freq': st.get('freq_a'), 'mode': st.get('mode'), 'radio_id': st.get('radio_id'),
            'fw': st.get('fw0'), 'head': st.get('pwr_head'),
            'audio_ok': self.running and not self.audio_err, 'audio_err': self.audio_err,
            'error': self.last_error,
            'clients': [{'name': c.name, 'local': c.local, 'audio': c.audio, 'tx': c is self.ptt_owner}
                        for c in self.clients if c.authed and not c.admin],
            'ptt_owner': self.ptt_owner.name if self.ptt_owner else '',
        }

    def push_status(self):
        self._send_admin({'t': 'admin_status', 's': self.status()})

    async def _admin_loop(self):
        while True:
            await asyncio.sleep(1)
            if any(c.admin for c in self.clients):
                self.push_status()

    async def _admin_msg(self, c, m):
        t = m['t']
        if t == 'admin_hello':
            c.admin = True
            c.authed = True
            self.ports_cache = await asyncio.to_thread(list_ports)
            self.audio_cache = await asyncio.to_thread(list_audio, not self.running)
            c.ws.send_json({'t': 'admin_init', 'cfg': self.cfg, 'ports': self.ports_cache, 'audio': self.audio_cache,
                            'urls': self.urls(), 'autostart': autostart_get(), 'version': VERSION,
                            'log': list(LOGBUF.lines), 'first_run': not os.path.exists(cfg_path()),
                            'detect': detect(self.ports_cache, self.audio_cache), 'win': IS_WIN})
            c.ws.send_json({'t': 'admin_status', 's': self.status()})
        elif t == 'admin_refresh':
            self.ports_cache = await asyncio.to_thread(list_ports)
            self.audio_cache = await asyncio.to_thread(list_audio, not self.running)
            c.ws.send_json({'t': 'admin_devices', 'ports': self.ports_cache, 'audio': self.audio_cache,
                            'detect': detect(self.ports_cache, self.audio_cache), 'urls': self.urls()})
        elif t == 'admin_save':
            new = {k: v for k, v in (m.get('cfg') or {}).items() if k in DEFAULT_CFG}
            radio_keys = ('cat_port', 'cat_baud', 'ptt_method', 'ptt_port', 'audio_device', 'audio_api',
                          'audio_in_index', 'audio_out_index', 'simulate')
            need_restart = self.running and any(new.get(k, self.cfg.get(k)) != self.cfg.get(k) for k in radio_keys)
            port_change = any(new.get(k, self.cfg.get(k)) != self.cfg.get(k) for k in ('http_port', 'https_port'))
            self.cfg.update(new)
            save_cfg(self.cfg)
            log.info("Настройки сохранены")
            c.ws.send_json({'t': 'admin_saved', 'cfg': self.cfg, 'port_change': port_change})
            if need_restart:
                log.info("Перезапуск связи с новыми настройками")
                await self.stop_radio()
                await self.start_radio()
        elif t == 'admin_start':
            if not os.path.exists(cfg_path()):
                save_cfg(self.cfg)
            await self.start_radio()
        elif t == 'admin_stop':
            await self.stop_radio()
        elif t == 'admin_autostart':
            try:
                v = autostart_set(bool(m.get('on')))
                log.info("Автозапуск вместе с Windows: %s", 'включён' if v else 'выключен')
            except Exception as e:
                v = autostart_get()
                log.error("Не удалось изменить автозапуск: %s", e)
            c.ws.send_json({'t': 'admin_autostart', 'on': v})
        elif t == 'admin_open_pult':
            webbrowser.open('http://localhost:%d/' % self.cfg['http_port'])
        elif t == 'admin_open_folder':
            if IS_WIN:
                os.startfile(BASE)
        elif t == 'admin_quit':
            log.info("Выход из программы")
            await self.stop_radio()
            self.quit_event.set()

    # ----- клиент пульта
    async def handle_ws(self, ws):
        ip = ws.peer[0] if ws.peer else ''
        local = ip in ('127.0.0.1', '::1', '::ffff:127.0.0.1')
        c = Client(ws, local)
        self.clients.add(c)
        try:
            while True:
                kind, data = await ws.recv()
                if kind is None:
                    break
                if kind == 'text':
                    try:
                        m = json.loads(data)
                    except ValueError:
                        continue
                    try:
                        await self._on_msg(c, m)
                    except Exception as e:
                        log.exception("Ошибка обработки %s: %s", m.get('t'), e)
                elif kind == 'bin' and c.authed and data[:1] == b'\x03':
                    self._on_tx_audio(c, data[1:])
        finally:
            self.clients.discard(c)
            if self.ptt_owner is c:
                self.set_ptt(None, False)
            if c.console and self.rig:
                self.rig.remove_raw_listener(c.console)
            if c.authed and not c.admin:
                log.info("Отключился %s", c.name)
                self.push_status()

    def _on_tx_audio(self, c, pcm):
        if self.ptt_owner is not c or not self.audio:
            return
        x = np.frombuffer(pcm, '<i2').astype(np.float32) / 32768.0
        if c.interp is None or c.interp.f != SR // c.rate:
            c.interp = Interpolator(SR // c.rate)
        self.audio.tx.push(c.interp.process(x))

    async def _on_msg(self, c, m):
        t = m.get('t', '')
        if t.startswith('admin_'):
            if c.local:
                await self._admin_msg(c, m)
            else:
                c.ws.send_json({'t': 'admin_denied'})
            return
        if t == 'hello':
            pw = self.cfg.get('password') or ''
            if c.local or (pw and m.get('pw') == pw):
                c.authed = True
                c.rate = 8000 if m.get('rate') == 8000 else 16000
                c.ws.send_json({'t': 'hello_ok', 'local': c.local, 'version': VERSION,
                                'audio_err': self.audio_err, 'ctcss': CTCSS, 'running': self.running,
                                'max_tx': self.cfg['max_tx_seconds'], 'ptt_method': self.cfg['ptt_method']})
                c.ws.send_json({'t': 'state', 's': self.rig.state if self.rig else {'connected': False}, 'full': True})
                if not self.running:
                    c.ws.send_json({'t': 'note', 'msg': 'Сервер запущен, но связь с трансивером остановлена — нажмите «Запустить» в панели управления'})
                self._ptt_info()
                log.info("Подключился пульт %s", c.name)
                self.push_status()
            else:
                msg = ('Удалённый доступ выключен: задайте пароль в панели управления на ПК с трансивером'
                       if not pw else 'Неверный пароль')
                log.warning("Отказ в доступе %s: %s", c.name, 'нет пароля на сервере' if not pw else 'неверный пароль')
                c.ws.send_json({'t': 'auth_fail', 'msg': msg})
            return
        if not c.authed:
            return
        if t == 'audio':
            c.audio = bool(m.get('on'))
            if m.get('rate') in (8000, 16000):
                c.rate = m['rate']
            return
        if t == 'wf':
            c.wf = bool(m.get('on'))
            c.wf_max = max(500, min(24000, int(m.get('max', 4000))))
            return
        if t == 'ping':
            c.ws.send_json({'t': 'pong', 'ts': m.get('ts')})
            return
        if t == 'menu_list':
            c.ws.send_json({'t': 'menu_list', 'items': self.menu})
            return
        if t == 'ptt':
            self.set_ptt(c, bool(m.get('on')))
            return
        if t == 'ptt_alive':
            if self.ptt_owner is c:
                self.ptt_alive = time.monotonic()
            return
        rig = self.rig
        if not rig:
            if t in ('set', 'act', 'raw', 'menu_get', 'menu_set'):
                c.ws.send_json({'t': 'note', 'msg': 'Связь с трансивером остановлена'})
            return
        if t == 'set':
            rig.set_param(m.get('k'), m.get('v'))
        elif t == 'act':
            rig.action(m.get('a'), m.get('arg'))
        elif t == 'raw':
            cmd = str(m.get('c', '')).strip().upper()
            if cmd and len(cmd) < 64:
                for part in [p for p in cmd.split(';') if p]:
                    rig.send(part + ';')
        elif t == 'console':
            if m.get('on') and not c.console:
                c.console = lambda line, cl=c: cl.ws.send_json({'t': 'raw', 'line': line})
                rig.add_raw_listener(c.console)
            elif not m.get('on') and c.console:
                rig.remove_raw_listener(c.console)
                c.console = None
        elif t == 'menu_get':
            for ex in m.get('ex', [])[:60]:
                v = await rig.menu_get(ex)
                c.ws.send_json({'t': 'menu_val', 'ex': ex, 'v': v})
        elif t == 'menu_set':
            item = next((i for i in self.menu if i['ex'] == m.get('ex')), None)
            if item:
                v = await rig.menu_set(item['ex'], m.get('v'), item['signed'], item['digits'])
                c.ws.send_json({'t': 'menu_val', 'ex': item['ex'], 'v': v})


# ------------------------------------------------------------- окно и трей
def find_app_browser():
    """Edge/Chrome для окна в режиме приложения (без адресной строки)."""
    cands = []
    if IS_WIN:
        for env in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
            root = os.environ.get(env)
            if root:
                cands += [os.path.join(root, r'Microsoft\Edge\Application\msedge.exe'),
                          os.path.join(root, r'Google\Chrome\Application\chrome.exe')]
    else:
        import shutil
        for n in ('microsoft-edge', 'google-chrome', 'chromium', 'chromium-browser'):
            p = shutil.which(n)
            if p:
                cands.append(p)
    return next((p for p in cands if os.path.exists(p)), None)


def open_window(url, size=(1080, 860)):
    exe = find_app_browser()
    if exe:
        try:
            flags = 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW
            subprocess.Popen([exe, '--app=' + url, '--window-size=%d,%d' % size],
                             creationflags=flags, close_fds=True)
            return
        except Exception as e:
            log.debug("app-окно не открылось: %s", e)
    webbrowser.open(url)


def start_tray(station, panel_url, pult_url):
    """Значок в области уведомлений (если установлены pystray и Pillow)."""
    try:
        import pystray
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(os.path.join(web_dir(), 'icon-192.png'))
        loop = station.loop

        def quit_app(icon, item):
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(station._admin_msg_quit()))

        menu = pystray.Menu(
            pystray.MenuItem('Панель управления', lambda i, it: open_window(panel_url), default=True),
            pystray.MenuItem('Открыть пульт', lambda i, it: open_window(pult_url, (1400, 900))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Выход', quit_app),
        )
        icon = pystray.Icon('FTX1Pult', img, APP_NAME, menu)
        icon.run_detached()
        return icon
    except Exception as e:
        log.debug("Трей недоступен: %s", e)
        return None


async def _quit(station):
    await station.stop_radio()
    station.quit_event.set()
Station._admin_msg_quit = lambda self: _quit(self)


def web_dir():
    d = os.path.join(RES, 'web')
    return d if os.path.isdir(d) else os.path.join(RES, '..', 'web')


def already_running(port):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/ping' % port, timeout=1.5) as r:
            return r.read().startswith(b'FTX1')
    except Exception:
        return False


def message_box(text, title=APP_NAME):
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
            return
        except Exception:
            pass
    print(text)


# ------------------------------------------------------------------ запуск
def list_all():
    print("COM-порты:")
    for p in list_ports():
        print("  %-8s %s" % (p['device'], p['desc']))
    print("\nЗвуковые устройства:")
    for d in list_audio():
        print("  [%2d] %-45s %-20s in=%d out=%d" % (d['index'], d['name'], d['api'], d['in'], d['out']))


async def amain(args):
    cfg = load_cfg()
    st = Station(cfg, force_sim=args.sim)
    await st.init()

    app = WebServer(web_dir(), st.handle_ws, {'/api/ping': lambda: ('text/plain', b'FTX1-PULT ' + VERSION.encode())})
    servers = []
    http_host = '0.0.0.0' if cfg.get('http_lan') else '127.0.0.1'
    try:
        servers.append(await asyncio.start_server(app.handle, http_host, cfg['http_port']))
    except OSError as e:
        message_box("Не удалось занять порт %d: %s\n\nЕго использует другая программа. Смените http_port в config.json."
                    % (cfg['http_port'], e))
        return
    crt, key = ensure_cert()
    if crt:
        try:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(crt, key)
            servers.append(await asyncio.start_server(app.handle, '0.0.0.0', cfg['https_port'], ssl=ctx))
            st.https_ok = True
        except OSError as e:
            log.error("HTTPS-порт %d занят (%s) — вход с телефона недоступен", cfg['https_port'], e)

    panel_url = 'http://localhost:%d/panel.html' % cfg['http_port']
    pult_url = 'http://localhost:%d/' % cfg['http_port']
    log.info("%s %s запущен. Панель: %s", APP_NAME, VERSION, panel_url)

    first_run = not os.path.exists(cfg_path())
    if (cfg.get('auto_connect') and not first_run) or args.sim:
        asyncio.create_task(st.start_radio())
    if not args.background:
        if cfg.get('open_panel', True) or first_run:
            open_window(panel_url)
        if cfg.get('open_pult') and not first_run:
            open_window(pult_url, (1400, 900))
    icon = start_tray(st, panel_url, pult_url)
    if icon is None and args.background is False and not find_app_browser():
        log.info("Совет: установите pystray и Pillow, чтобы получить значок в трее")

    tasks = [asyncio.create_task(s.serve_forever()) for s in servers]
    await st.quit_event.wait()
    for s in servers:
        s.close()
    for t in tasks:
        t.cancel()
    if icon:
        try:
            icon.stop()
        except Exception:
            pass


def selftest(path):
    """Проверка собранного exe: все библиотеки и файлы пульта на месте."""
    rep, ok = [], True
    for mod in ('numpy', 'serial', 'serial.tools.list_ports', 'sounddevice', 'cryptography', 'pystray', 'PIL'):
        try:
            __import__(mod)
            rep.append('OK   import ' + mod)
        except Exception as e:
            ok = False
            rep.append('FAIL import %s: %s' % (mod, e))
    try:
        rep.append('OK   audio devices: %d' % len(list_audio()))
    except Exception as e:
        ok = False
        rep.append('FAIL audio: %s' % e)
    for f in ('index.html', 'app.js', 'app.css', 'worklets.js', 'panel.html', 'panel.js', 'qrcode.js',
              'manifest.webmanifest', 'sw.js', 'icon-192.png'):
        p = os.path.join(web_dir(), f)
        good = os.path.isfile(p)
        ok &= good
        rep.append(('OK   ' if good else 'FAIL ') + 'web/' + f)
    good = os.path.isfile(os.path.join(RES, 'menu_ftx1.json'))
    ok &= good
    rep.append(('OK   ' if good else 'FAIL ') + 'menu_ftx1.json')
    rep.append('RESULT ' + ('OK' if ok else 'FAIL'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(rep) + '\n')
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description="FTX-1 Пульт — удалённое управление Yaesu FTX-1")
    ap.add_argument('--sim', action='store_true', help='симулятор трансивера')
    ap.add_argument('--list', action='store_true', help='показать COM-порты и звуковые устройства')
    ap.add_argument('--background', action='store_true', help='без окна (автозапуск)')
    ap.add_argument('--console', action='store_true', help='журнал в консоль')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--selftest', metavar='FILE', help='проверить состав сборки и записать отчёт в FILE')
    args = ap.parse_args()
    if args.selftest:
        selftest(args.selftest)
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    LOGBUF.setLevel(logging.INFO)
    root.addHandler(LOGBUF)
    try:
        fh = logging.handlers.RotatingFileHandler(os.path.join(BASE, 'ftx1-pult.log'), maxBytes=1_000_000,
                                                  backupCount=1, encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s: %(message)s'))
        root.addHandler(fh)
    except OSError:
        pass
    if sys.stdout is not None and (args.console or not IS_WIN or sys.stdout.isatty()):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter('%(asctime)s %(name)s: %(message)s', '%H:%M:%S'))
        root.addHandler(sh)

    if args.list:
        list_all()
        return
    cfg = load_cfg()
    if already_running(cfg['http_port']):
        # программа уже работает — просто показываем её окно
        if not args.background:
            open_window('http://localhost:%d/panel.html' % cfg['http_port'])
        return
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
