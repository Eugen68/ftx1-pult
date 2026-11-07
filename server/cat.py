"""
CAT-протокол Yaesu FTX-1 (Field / Optima).

Формат команд и диапазоны значений сверены с FTX-1 CAT Operation Reference
Manual 2508-C (через драйвер Hamlib rigs/yaesu/ftx1, автор KJ5HST).

Архитектура:
  * Transport — байтовый канал (настоящий COM-порт или симулятор).
  * Rig — очередь команд, разбор ответов, опрос, словарь состояния.
    Любой ответ трансивера (на запрос или по Auto Information) сразу
    обновляет состояние; изменения уходят подписчикам (веб-клиентам).
"""
import asyncio
import re
import threading
import time
import logging

log = logging.getLogger("cat")

# ---------------------------------------------------------------- справочники
MODES = {
    '1': 'LSB', '2': 'USB', '3': 'CW-U', '4': 'FM', '5': 'AM', '6': 'RTTY-L',
    '7': 'CW-L', '8': 'DATA-L', '9': 'RTTY-U', 'A': 'DATA-FM', 'B': 'FM-N',
    'C': 'DATA-U', 'D': 'AM-N', 'E': 'PSK', 'F': 'DATA-FM-N',
    'H': 'C4FM-DN', 'I': 'C4FM-VW',
}
MODE_CODES = {v: k for k, v in MODES.items()}

# RM P1: 1=Main S, 2=Sub S, 3=COMP, 4=ALC, 5=PO, 6=SWR, 7=ID, 8=VDD
METERS = {1: 's_main', 2: 's_sub', 3: 'comp', 4: 'alc', 5: 'po', 6: 'swr', 7: 'id', 8: 'vdd'}

CTCSS = [67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4,
         100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5,
         141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3, 173.8,
         177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5,
         210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1]


def band_type(freq):
    """Группа диапазонов для PA (предусилитель): 0=КВ/50, 1=144, 2=430."""
    if freq >= 420_000_000:
        return 2
    if freq >= 144_000_000:
        return 1
    return 0


def _i(s):
    try:
        return int(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ разбор
# (регулярное выражение для кадра без ';', функция -> dict изменений)
def _parse_pc(m):
    try:
        w = float(m.group(2))
    except ValueError:
        return {}
    return {'pwr_head': int(m.group(1)), 'power': w}


def _parse_ac(m):
    return {'tuner': m.group(0)[2:], 'tuning': m.group(3) != '0'}


PARSERS = [
    (r'FA(\d{9})', lambda m: {'freq_a': int(m.group(1))}),
    (r'FB(\d{9})', lambda m: {'freq_b': int(m.group(1))}),
    (r'MD0(.)', lambda m: {'mode': MODES.get(m.group(1), m.group(1))}),
    (r'MD1(.)', lambda m: {'mode_b': MODES.get(m.group(1), m.group(1))}),
    (r'TX(\d)', lambda m: {'tx': m.group(1) != '0'}),
    (r'SM0(\d{3})', lambda m: {'smeter': int(m.group(1))}),
    (r'SM1(\d{3})', lambda m: {'smeter_b': int(m.group(1))}),
    (r'RM(\d)(\d{3})\d*', lambda m: {METERS.get(int(m.group(1)), 'm%s' % m.group(1)): int(m.group(2))}),
    (r'PC([12])(.+)', _parse_pc),
    (r'AG0(\d{3})', lambda m: {'af': int(m.group(1))}),
    (r'RG0(\d{3})', lambda m: {'rf': int(m.group(1))}),
    (r'SQ0(\d{3})', lambda m: {'sql': int(m.group(1))}),
    (r'MG(\d{3})', lambda m: {'mic': int(m.group(1))}),
    (r'VX(\d)', lambda m: {'vox': int(m.group(1))}),
    (r'VG(\d{3})', lambda m: {'vox_gain': int(m.group(1))}),
    (r'VD(\d{2})', lambda m: {'vox_delay': int(m.group(1))}),
    (r'NL0(\d{3})', lambda m: {'nb': int(m.group(1))}),
    (r'RL0(\d{2})', lambda m: {'nr': int(m.group(1))}),
    (r'BC0(\d)', lambda m: {'dnf': int(m.group(1))}),
    (r'BP00(\d{3})', lambda m: {'notch': 1 if int(m.group(1)) else 0}),
    (r'BP01(\d{3})', lambda m: {'notch_freq': int(m.group(1)) * 10}),
    (r'CO00(\d{4})', lambda m: {'contour': 1 if int(m.group(1)) else 0}),
    (r'CO01(\d{4})', lambda m: {'contour_freq': int(m.group(1))}),
    (r'CO02(\d{4})', lambda m: {'apf': 1 if int(m.group(1)) else 0}),
    (r'CO03(\d{4})', lambda m: {'apf_freq': int(m.group(1))}),
    (r'IS0(\d)([+-])(\d{4})', lambda m: {'ifshift_on': int(m.group(1)),
                                         'ifshift': int(m.group(3)) * (-1 if m.group(2) == '-' else 1)}),
    (r'SH0\d(\d{2})', lambda m: {'width': int(m.group(1))}),
    (r'GT0(\d)\d*', lambda m: {'agc': int(m.group(1))}),
    (r'PA([0-2])(\d)', lambda m: {'preamp%s' % m.group(1): int(m.group(2))}),
    (r'RA0(\d)', lambda m: {'att': int(m.group(1))}),
    (r'NA0(\d)', lambda m: {'narrow': int(m.group(1))}),
    (r'FT(\d)', lambda m: {'split': int(m.group(1))}),
    (r'VS(\d)', lambda m: {'vfo': int(m.group(1))}),
    (r'FR(\d{2})', lambda m: {'dual': 1 if m.group(1) == '00' else 0}),
    (r'PR0(\d)', lambda m: {'proc': 1 if m.group(1) == '2' else 0}),
    (r'PL(\d{3})', lambda m: {'proc_level': int(m.group(1))}),
    (r'ML0(\d{3})', lambda m: {'mon': 1 if int(m.group(1)) else 0}),
    (r'ML1(\d{3})', lambda m: {'mon_level': int(m.group(1))}),
    (r'KS(\d{3})', lambda m: {'key_speed': int(m.group(1))}),
    (r'BI(\d)', lambda m: {'bk': int(m.group(1))}),
    (r'KP(\d{2})', lambda m: {'key_pitch': int(m.group(1))}),
    (r'KR(\d)', lambda m: {'keyer': int(m.group(1))}),
    (r'SD(\d{2})', lambda m: {'bk_delay': int(m.group(1))}),
    (r'CF000(\d)(\d)\d*', lambda m: {'clar_rx': int(m.group(1)), 'clar_tx': int(m.group(2))}),
    (r'CF001([+-]\d{4})', lambda m: {'clar': int(m.group(1))}),
    (r'LK(\d)', lambda m: {'lock': int(m.group(1))}),
    (r'AC(\d)(\d)(\d)', _parse_ac),
    (r'CT0(\d)', lambda m: {'tone_mode': int(m.group(1))}),
    (r'CN00(\d{3})', lambda m: {'ctcss': int(m.group(1))}),
    (r'CN01(\d{3})', lambda m: {'dcs': int(m.group(1))}),
    (r'OS0(\d)', lambda m: {'rpt_shift': int(m.group(1))}),
    (r'TS(\d)', lambda m: {'txw': int(m.group(1))}),
    (r'AO(\d{3})', lambda m: {'amc': int(m.group(1))}),
    (r'AI(\d)', lambda m: {'ai': int(m.group(1))}),
    (r'ID(\d{4})', lambda m: {'radio_id': m.group(1)}),
    (r'RI\d{7}(\d)\d*', lambda m: {'busy': m.group(1) == '1'}),   # P8: шумоподавитель открыт / сигнал
    (r'VE(\d)(.*)', lambda m: {'fw%s' % m.group(1): m.group(2)}),
    (r'MS(\d)(\d)', lambda m: {'meter_sel': int(m.group(1))}),
    (r'DA(\d{2})\d*', lambda m: {'dimmer': int(m.group(1))}),
    (r'SC0(\d)', lambda m: {'scan': int(m.group(1))}),
    (r'VM0(\d)', lambda m: {'vm': int(m.group(1))}),
    (r'MC0?(\d{3,5})', lambda m: {'mem_ch': int(m.group(1))}),
]
PARSERS = [(re.compile(r'^' + rx + r'$'), fn) for rx, fn in PARSERS]


def parse_frame(frame):
    """Кадр без ';' -> dict изменений состояния (или {})."""
    for rx, fn in PARSERS:
        m = rx.match(frame)
        if m:
            try:
                return fn(m)
            except Exception:  # noqa
                return {}
    return {}


# ------------------------------------------------------------- транспорт
class SerialTransport:
    """Настоящий COM-порт (Enhanced COM Port FTX-1, драйвер CP210x)."""

    def __init__(self, port, baud=38400, rtscts=False):
        self.port, self.baud, self.rtscts = port, baud, rtscts
        self.ser = None
        self._stop = False
        self.failed = False

    def open(self, on_bytes):
        import serial  # pyserial
        self.ser = serial.Serial()
        self.ser.port = self.port
        self.ser.baudrate = self.baud
        self.ser.bytesize, self.ser.parity, self.ser.stopbits = 8, 'N', 1
        self.ser.timeout = 0.05
        self.ser.rtscts = self.rtscts
        # RTS/DTR держим снятыми, чтобы случайно не включить передачу
        self.ser.rts = False
        self.ser.dtr = False
        self.ser.open()
        self._stop = False
        self.failed = False
        self._on_bytes = on_bytes
        threading.Thread(target=self._reader, args=(on_bytes,), daemon=True).start()

    def reopen(self):
        try:
            self.ser.close()
        except Exception:
            pass
        self.open(self._on_bytes)

    def _reader(self, on_bytes):
        while not self._stop:
            try:
                data = self.ser.read(256)
            except Exception as e:  # порт пропал (кабель выдернули)
                log.error("Ошибка чтения COM: %s", e)
                self.failed = True
                on_bytes(None)
                return
            if data:
                on_bytes(data)

    def write(self, data: bytes):
        self.ser.write(data)

    def close(self):
        self._stop = True
        try:
            self.ser.close()
        except Exception:
            pass


class SerialPtt:
    """PTT линией RTS или DTR второго (Standard) COM-порта — запасной способ."""

    def __init__(self, port, line='RTS'):
        import serial
        self.line = line.upper()
        self.ser = serial.Serial(port, 9600, timeout=0)
        self.ser.rts = False
        self.ser.dtr = False

    def set(self, on):
        if self.line == 'DTR':
            self.ser.dtr = bool(on)
        else:
            self.ser.rts = bool(on)


# --------------------------------------------------------------------- Rig
FAST_RX = ['SM0;', 'RI0;']
FAST_TX = ['RM5;', 'RM6;', 'RM4;', 'RM3;']
MEDIUM = ['FA;', 'FB;', 'MD0;', 'TX;']
SLOW = ['MD1;', 'AG0;', 'RG0;', 'SQ0;', 'PC;', 'MG;', 'VX;', 'VG;', 'VD;', 'NL0;', 'RL0;',
        'BC0;', 'BP00;', 'BP01;', 'CO00;', 'CO01;', 'CO02;', 'CO03;', 'IS0;', 'SH0;',
        'GT0;', 'PA{band};', 'RA0;', 'NA0;', 'FT;', 'VS;', 'FR;', 'PR0;', 'PL;', 'ML0;',
        'ML1;', 'KS;', 'BI;', 'KP;', 'KR;', 'SD;', 'CF000;', 'CF001;', 'LK;', 'AC;',
        'CT0;', 'CN00;', 'CN01;', 'OS0;', 'TS;', 'AO;', 'RM8;', 'RM7;', 'SM1;']


class Rig:
    def __init__(self, transport, on_change, ptt_method='TX1', serial_ptt=None):
        self.t = transport
        self.on_change = on_change          # callback(dict) в event loop
        self.ptt_method = ptt_method        # 'TX1' | 'TX2' | 'RTS'
        self.serial_ptt = serial_ptt
        self.state = {'connected': False}
        self.loop = None
        self._buf = b''
        self._q = None                      # очередь пользовательских команд
        self._pending = None                # (prefix, future)
        self._unsupported = {}              # запрос -> число '?' подряд
        self._slow_i = 0
        self._raw_listeners = []            # для консоли CAT
        self._menu_waiters = {}             # ex -> [future]
        self._last_rx = 0.0

    # ---- жизненный цикл
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        self.t.open(self._on_bytes_thread)
        self._last_rx = time.monotonic()
        self._set({'connected': True})
        self._task = asyncio.create_task(self._worker())
        # Начальная настройка: опознание и авто-информация
        for c in ('ID;', 'VE0;', 'VE4;', 'AI1;'):
            self.send(c)

    def close(self):
        if getattr(self, '_task', None):
            self._task.cancel()
        try:
            self.t.write(b'TX0;')
            if self.serial_ptt:
                self.serial_ptt.set(False)
        except Exception:
            pass
        self.t.close()
        if self.serial_ptt:
            try:
                self.serial_ptt.ser.close()
            except Exception:
                pass

    # ---- приём
    def _on_bytes_thread(self, data):
        try:
            self.loop.call_soon_threadsafe(self._on_bytes, data)
        except RuntimeError:        # цикл уже остановлен (выход из программы)
            pass

    def _on_bytes(self, data):
        if data is None:
            self._set({'connected': False})
            return
        self._last_rx = time.monotonic()
        self._buf += data
        while b';' in self._buf:
            frame, self._buf = self._buf.split(b';', 1)
            frame = frame.decode('ascii', 'replace').strip()
            if frame:
                self._on_frame(frame)
        if len(self._buf) > 512:
            self._buf = b''

    def _on_frame(self, frame):
        for cb in list(self._raw_listeners):
            cb('<' + frame + ';')
        if frame.startswith('EX') and len(frame) >= 8:
            ex, val = frame[2:8], frame[8:]
            for fut in self._menu_waiters.pop(ex, []):
                if not fut.done():
                    fut.set_result(val)
        changes = parse_frame(frame)
        if changes:
            self._set(changes)
        p = self._pending
        if p and not p[1].done() and (frame == '?' or frame.startswith(p[0])):
            p[1].set_result(frame)

    def _set(self, changes):
        diff = {k: v for k, v in changes.items() if self.state.get(k) != v}
        if diff:
            self.state.update(diff)
            self.on_change(diff)

    # ---- передача
    def send(self, cmd, priority=True):
        """Поставить команду (или запрос) в очередь."""
        if not cmd.endswith(';'):
            cmd += ';'
        self._q.put_nowait(cmd)

    def add_raw_listener(self, cb):
        self._raw_listeners.append(cb)

    def remove_raw_listener(self, cb):
        if cb in self._raw_listeners:
            self._raw_listeners.remove(cb)

    async def _xfer(self, cmd):
        """Отправить; если это запрос — дождаться ответа (до 250 мс)."""
        for cb in list(self._raw_listeners):
            cb('>' + cmd)
        is_query = self._is_query(cmd)
        fut = None
        if is_query:
            fut = self.loop.create_future()
            self._pending = (cmd[:2], fut)
        try:
            self.t.write(cmd.encode('ascii'))
        except Exception as e:
            log.error("Ошибка записи COM: %s", e)
            self._set({'connected': False})
            await asyncio.sleep(1)
            return None
        if not is_query:
            await asyncio.sleep(0.012)      # трансиверу нужно время на исполнение
            return None
        try:
            return await asyncio.wait_for(fut, 0.25)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending = None

    @staticmethod
    def _is_query(cmd):
        c = cmd[:-1]
        if c in ('ID', 'IF', 'OI', 'TX', 'FA', 'FB', 'PC', 'MG', 'VX', 'VG', 'VD', 'FT', 'VS',
                 'FR', 'PL', 'KS', 'BI', 'KP', 'KR', 'SD', 'LK', 'AC', 'TS', 'AO', 'AI', 'MS',
                 'DA', 'ST'):
            return True
        # Запросы с номером: AG0, SM0, RM5, MD0, NL0, BP00, CO01, CF000, CN00, VE0, EXxxxxxx...
        rx = {'AG': 3, 'RG': 3, 'SQ': 3, 'SM': 3, 'RM': 3, 'MD': 3, 'NL': 3, 'RL': 3, 'BC': 3,
              'BP': 4, 'CO': 4, 'IS': 3, 'SH': 3, 'GT': 3, 'PA': 3, 'RA': 3, 'NA': 3, 'PR': 3,
              'ML': 3, 'CF': 5, 'CT': 3, 'CN': 4, 'OS': 3, 'VE': 3, 'EX': 8, 'SS': 4, 'SC': 3,
              'MC': 3, 'VM': 3, 'MR': 7, 'MT': 7, 'RI': 3}
        n = rx.get(c[:2])
        return n is not None and len(c) == n

    async def _worker(self):
        last_medium = 0.0
        while True:
            try:
                if getattr(self.t, 'failed', False):
                    await asyncio.sleep(3)
                    try:
                        self.t.reopen()
                        log.info("COM-порт открыт заново")
                        self.send('AI1;')
                    except Exception as e:
                        log.warning("Порт пока недоступен: %s", e)
                    continue
                # 1) команды пользователя — всегда первыми
                while not self._q.empty():
                    cmd = self._q.get_nowait()
                    await self._xfer(cmd)
                # 2) быстрый опрос шкал
                for c in (FAST_TX if self.state.get('tx') else FAST_RX):
                    await self._poll(c)
                if not self._q.empty():
                    continue
                now = time.monotonic()
                if now - last_medium > 0.3:
                    last_medium = now
                    for c in MEDIUM:
                        await self._poll(c)
                # 3) медленный круговой опрос всех регуляторов
                for _ in range(2):
                    c = SLOW[self._slow_i % len(SLOW)]
                    self._slow_i += 1
                    c = c.replace('{band}', str(band_type(self.state.get('freq_a', 0))))
                    if self._unsupported.get(c, 0) < 3:
                        await self._poll(c)
                await asyncio.sleep(0.03)
                if time.monotonic() - self._last_rx > 3 and self.state.get('connected'):
                    self._set({'connected': False})
                elif time.monotonic() - self._last_rx < 1 and not self.state.get('connected'):
                    self._set({'connected': True})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # не даём опросу умереть
                log.exception("Ошибка цикла CAT: %s", e)
                await asyncio.sleep(0.5)

    def refresh(self, delay=0.0):
        """Перечитать из трансивера все регуляторы сразу (при подключении пульта, смене диапазона/вида)."""
        async def go():
            if delay:
                await asyncio.sleep(delay)
            band = str(band_type(self.state.get('freq_a', 0)))
            for c in ['FA;', 'FB;', 'MD0;'] + SLOW:
                c = c.replace('{band}', band)
                if self._unsupported.get(c, 0) < 3:
                    self.send(c)
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(go()))

    async def _poll(self, cmd):
        r = await self._xfer(cmd)
        if r == '?':
            self._unsupported[cmd] = self._unsupported.get(cmd, 0) + 1
        elif r:
            self._unsupported.pop(cmd, None)

    # ---- меню EX
    async def menu_get(self, ex):
        fut = self.loop.create_future()
        self._menu_waiters.setdefault(ex, []).append(fut)
        self.send('EX%s;' % ex)
        try:
            return await asyncio.wait_for(fut, 1.5)
        except asyncio.TimeoutError:
            return None

    async def menu_set(self, ex, value, signed=False, digits=None):
        """Значение записываем той же ширины, что вернул трансивер при чтении."""
        cur = await self.menu_get(ex)
        if isinstance(value, str) and not value.lstrip('+-').isdigit():
            s = value                                   # строковый пункт (позывной и т.п.)
        else:
            v = int(value)
            width = digits or 1
            if cur is not None:
                width = len(cur.lstrip('+-')) or width
                signed = signed or cur[:1] in '+-'
            if signed:
                s = ('+' if v >= 0 else '-') + str(abs(v)).zfill(width)
            else:
                s = str(v).zfill(width)
        self.send('EX%s%s;' % (ex, s))
        await asyncio.sleep(0.1)
        return await self.menu_get(ex)

    # ---- PTT
    def ptt(self, on):
        if self.ptt_method == 'RTS' and self.serial_ptt:
            self.serial_ptt.set(on)
            self.send('TX;')
        else:
            on_cmd = 'TX2;' if self.ptt_method == 'TX2' else 'TX1;'
            self.send(on_cmd if on else 'TX0;')
            self.send('TX;')

    # ---- высокоуровневые установки (ключ -> команда CAT)
    def set_param(self, key, v):
        st = self.state
        band = band_type(st.get('freq_a', 0))
        clamp = lambda x, lo, hi: max(lo, min(hi, int(round(float(x)))))
        cmds = []
        if key == 'freq_a':
            cmds = ['FA%09d;' % clamp(v, 30_000, 470_000_000)]
        elif key == 'freq_b':
            cmds = ['FB%09d;' % clamp(v, 30_000, 470_000_000)]
        elif key in ('mode', 'mode_b'):
            code = MODE_CODES.get(v)
            if code:
                cmds = ['MD%d%s;' % (1 if key == 'mode_b' else 0, code)]
        elif key == 'af':
            cmds = ['AG0%03d;' % clamp(v, 0, 255)]
        elif key == 'rf':
            cmds = ['RG0%03d;' % clamp(v, 0, 255)]
        elif key == 'sql':
            cmds = ['SQ0%03d;' % clamp(v, 0, 255)]
        elif key == 'power':
            w = float(v)
            if st.get('pwr_head') == 2:
                cmds = ['PC2%03d;' % clamp(w, 5, 100)]
            else:
                w = max(0.5, min(10.0, w))
                cmds = ['PC1%03d;' % int(w) if w == int(w) else 'PC1%.1f;' % w]
        elif key == 'mic':
            cmds = ['MG%03d;' % clamp(v, 0, 100)]
        elif key == 'vox':
            cmds = ['VX%d;' % (1 if int(v) else 0)]
        elif key == 'vox_gain':
            cmds = ['VG%03d;' % clamp(v, 0, 100)]
        elif key == 'vox_delay':
            cmds = ['VD%02d;' % clamp(v, 0, 33)]
        elif key == 'nb':
            cmds = ['NL0%03d;' % clamp(v, 0, 10)]
        elif key == 'nr':
            cmds = ['RL0%02d;' % clamp(v, 0, 10)]
        elif key == 'dnf':
            cmds = ['BC0%d;' % (1 if int(v) else 0)]
        elif key == 'notch':
            cmds = ['BP00%03d;' % (1 if int(v) else 0)]
        elif key == 'notch_freq':
            cmds = ['BP01%03d;' % clamp(float(v) / 10, 1, 320)]
        elif key == 'contour':
            cmds = ['CO00%04d;' % (1 if int(v) else 0)]
        elif key == 'contour_freq':
            cmds = ['CO01%04d;' % clamp(v, 10, 3200)]
        elif key == 'apf':
            cmds = ['CO02%04d;' % (1 if int(v) else 0)]
        elif key == 'apf_freq':
            cmds = ['CO03%04d;' % clamp(v, 0, 50)]
        elif key == 'ifshift':
            x = clamp(v, -1200, 1200)
            cmds = ['IS0%d%s%04d;' % (1 if x else 0, '+' if x >= 0 else '-', abs(x))]
        elif key == 'width':
            cmds = ['SH00%02d;' % clamp(v, 0, 23)]
        elif key == 'agc':
            cmds = ['GT0%d;' % clamp(v, 0, 4), 'GT0;']       # 5/6 — это ответы AUTO-MID/SLOW, ставится AUTO
        elif key == 'preamp':
            cmds = ['PA%d%d;' % (band, clamp(v, 0, 2 if band == 0 else 1))]
        elif key == 'att':
            cmds = ['RA0%d;' % (1 if int(v) else 0)]
        elif key == 'narrow':
            cmds = ['NA0%d;' % (1 if int(v) else 0)]
        elif key == 'split':
            cmds = ['FT%d;' % (1 if int(v) else 0)]
        elif key == 'vfo':
            cmds = ['VS%d;' % (1 if int(v) else 0)]
        elif key == 'dual':
            cmds = ['FR%s;' % ('00' if int(v) else '01')]
        elif key == 'proc':
            cmds = ['PR0%d;' % (2 if int(v) else 1)]
        elif key == 'proc_level':
            cmds = ['PL%03d;' % clamp(v, 0, 100)]
        elif key == 'mon':
            cmds = ['ML0%03d;' % (1 if int(v) else 0)]
        elif key == 'mon_level':
            cmds = ['ML1%03d;' % clamp(v, 0, 100)]
        elif key == 'key_speed':
            cmds = ['KS%03d;' % clamp(v, 4, 60)]
        elif key == 'bk':
            cmds = ['BI%d;' % (1 if int(v) else 0)]
        elif key == 'key_pitch':
            cmds = ['KP%02d;' % clamp(v, 0, 75)]
        elif key == 'keyer':
            cmds = ['KR%d;' % (1 if int(v) else 0)]
        elif key == 'bk_delay':
            cmds = ['SD%02d;' % clamp(v, 0, 33)]
        elif key == 'clar_rx':
            cmds = ['CF000%d%d000;' % (1 if int(v) else 0, st.get('clar_tx', 0))]
        elif key == 'clar_tx':
            cmds = ['CF000%d%d000;' % (st.get('clar_rx', 0), 1 if int(v) else 0)]
        elif key == 'clar':
            x = clamp(v, -9999, 9999)
            cmds = ['CF001%s%04d;' % ('+' if x >= 0 else '-', abs(x))]
        elif key == 'lock':
            cmds = ['LK%d;' % (1 if int(v) else 0)]
        elif key == 'tone_mode':
            cmds = ['CT0%d;' % clamp(v, 0, 3)]
        elif key == 'ctcss':
            cmds = ['CN00%03d;' % clamp(v, 0, 49)]
        elif key == 'dcs':
            cmds = ['CN01%03d;' % clamp(v, 0, 103)]
        elif key == 'rpt_shift':
            cmds = ['OS0%d;' % clamp(v, 0, 2)]
        elif key == 'txw':
            cmds = ['TS%d;' % (1 if int(v) else 0)]
        elif key == 'amc':
            cmds = ['AO%03d;' % clamp(v, 0, 100)]
        elif key == 'dimmer':
            cmds = ['DA%02d;' % clamp(v, 0, 20)]
        elif key == 'meter_sel':
            cmds = ['MS%d0;' % clamp(v, 0, 5)]
        # мгновенно отражаем у себя, затем перечитываем у трансивера
        for c in cmds:
            self.send(c)
        if cmds:
            q = re.sub(r'[+\-0-9.]+;$', '', cmds[0])
            readback = {
                'freq_a': 'FA;', 'freq_b': 'FB;', 'mode': 'MD0;', 'mode_b': 'MD1;',
                'power': 'PC;', 'mic': 'MG;', 'vox': 'VX;', 'vox_gain': 'VG;', 'vox_delay': 'VD;',
                'split': 'FT;', 'vfo': 'VS;', 'dual': 'FR;', 'proc_level': 'PL;', 'key_speed': 'KS;',
                'bk': 'BI;', 'key_pitch': 'KP;', 'keyer': 'KR;', 'bk_delay': 'SD;', 'lock': 'LK;',
                'txw': 'TS;', 'amc': 'AO;', 'dimmer': 'DA;', 'meter_sel': 'MS;',
                'notch': 'BP00;', 'notch_freq': 'BP01;', 'contour': 'CO00;', 'contour_freq': 'CO01;',
                'apf': 'CO02;', 'apf_freq': 'CO03;', 'clar_rx': 'CF000;', 'clar_tx': 'CF000;',
                'clar': 'CF001;', 'ctcss': 'CN00;', 'dcs': 'CN01;',
            }.get(key)
            if not readback:
                readback = cmds[0][:3] + ';' if len(q) <= 3 else q + ';'
            self.send(readback)
            if key == 'mode' or (key == 'freq_a' and band_type(int(float(v))) != band):
                self.refresh(0.3)       # у каждого вида/диапазона свои полоса, АРУ, предусилитель
        return cmds

    def action(self, name, arg=None):
        """Разовые действия (кнопки)."""
        a = {
            'a_to_b': ['AB;'], 'b_to_a': ['BA;'], 'swap': ['SV;'],
            'band_up': ['BU0;'], 'band_down': ['BD0;'], 'up': ['UP;'], 'down': ['DN;'],
            'tune': ['AC003;'], 'tune_ext': ['AC103;'], 'tune_stop': ['AC000;'],
            'atas_tune': ['AC123;'], 'atas_up': ['AC121;'], 'atas_down': ['AC122;'], 'atas_stop': ['AC120;'],
            'zero_in': ['ZI0;'], 'spot_on': ['CS1;'], 'spot_off': ['CS0;'],
            'scan_up': ['SC01;'], 'scan_down': ['SC02;'], 'scan_stop': ['SC00;'],
            'vfo_mode': ['VM00;'], 'mem_mode': ['VM01;'], 'qmb_store': ['QI;'], 'qmb_recall': ['QR;'],
            'clar_clear': ['CF001+0000;'],
        }.get(name)
        if name == 'band' and arg is not None:
            a = ['BS0%02d;' % int(arg)]
        elif name == 'cw_play' and arg is not None:
            a = ['KY0%d;' % int(arg)]                 # воспроизвести память ключа 1..5
        elif name == 'cw_store' and isinstance(arg, (list, tuple)):
            slot, text = int(arg[0]), str(arg[1]).upper()[:50]
            text = re.sub(r'[^A-Z0-9 /?.,=+\-]', '', text)
            a = ['KM%d%s;' % (slot, text)]
        elif name == 'voice_play' and arg is not None:
            a = ['PB0%d;' % int(arg)]
        elif name == 'mem_ch' and arg is not None:
            a = ['MC0%03d;' % int(arg)]
        elif name == 'power_off':
            a = ['PS0;']
        if a:
            for c in a:
                self.send(c)
            for c in ('FA;', 'FB;', 'MD0;', 'AC;'):
                self.send(c)
            if name in ('band', 'band_up', 'band_down', 'mem_ch', 'vfo_mode', 'mem_mode', 'qmb_recall', 'swap', 'b_to_a'):
                self.refresh(0.4)       # трансивер сам восстановил полосу/АРУ/фильтры диапазона — читаем их
        return a or []
