"""
Стол цифровых видов: RTTY (приём + передача AFSK) и WEFAX (приём метеокарт). Живёт внутри Station.

Протокол пульта:
  rt_hello                         -> rt_state
  rt_dec {on, baud, shift, fc, pol, usos}
  rt_autotune                      -> rt_state (новая fc) или rt_note
  rt_send {text, reverse}          -> rt_tx_start / rt_tx_progress {pos} / rt_tx_done {ok, msg?}
  rt_stop
  rt_clear
  rt_ai {text}                     -> rt_ai {ru} | rt_ai_err   (перевод через LM Studio, если есть)
  fax_hello                        -> fax_state + картинка целиком (бинарный кадр 0x06)
  fax_dec {on, lpm, ioc, auto, center, afc}
  fax_start | fax_stop | fax_shift {px} | fax_slant {d}
Сервер шлёт: rt_rx {text}, rt_info {...}, fax_state, fax_info, fax_note, fax_saved {name, path},
  бинарные кадры 0x05 = строка факса: '<HH' (номер строки, ширина) + uint8[ширина],
                0x06 = вся текущая картинка: '<HH' (строк, ширина) + uint8[строк*ширина].
"""

from i18n import tr
import asyncio
import logging
import os
import struct
import time

import numpy as np

import ai
from audio import SR
from digi import FaxDecoder, RttyDecoder, rtty_afsk
from navtex import NavtexDecoder
from psk31 import Psk31Decoder, psk31_tx
from sstv import SstvDecoder, MODES as SSTV_MODES

log = logging.getLogger("digidesk")


class DigiDesk:
    def __init__(self, station, base_dir):
        self.st = station
        self.fax_dir = os.path.join(base_dir, 'fax')
        self.rtty = RttyDecoder(self._rt_event)
        self.fax = FaxDecoder(self._fax_event, self._fax_row)
        self.nx = NavtexDecoder(lambda ev: self.bc(ev, 'nx_on'))
        self.psk = Psk31Decoder(lambda ev: self.bc(ev, 'psk_on'))
        self.sstv = SstvDecoder(self._sstv_event, self._sstv_row)
        self.sstv_dir = os.path.join(base_dir, 'sstv')
        self.sstv_saved = []
        self.nx_log = []                  # последние сообщения NAVTEX (текст) — для нового подключения
        self.rt_cfg = {'baud': 45.45, 'shift': 170.0, 'fc': 2210.0, 'pol': 0, 'usos': True}
        self.tx_task = None
        self.tx_busy = False
        self.saved = []                   # последние сохранённые карты

    # ------------------------------------------------------------------ helpers
    @property
    def cfg(self):
        return self.st.cfg

    def _clients(self, flag=None):
        return [c for c in self.st.clients if c.authed and not c.admin and (flag is None or getattr(c, flag, False))]

    def bc(self, obj, flag=None):
        for c in self._clients(flag):
            c.ws.send_json(obj)

    def _rig_state(self):
        return self.st.rig.state if self.st.rig else {}

    def update(self):
        self.rtty.configure(enabled=bool(self._clients('rt_on')))
        self.nx.configure(enabled=bool(self._clients('nx_on')))
        self.psk.configure(enabled=bool(self._clients('psk_on')))
        self.sstv.configure(enabled=bool(self._clients('sstv_on')))
        on = bool(self._clients('fax_on'))
        if on != self.fax.enabled:
            self.fax.configure(enabled=on)

    def rt_state(self):
        r = self.rtty
        return {'t': 'rt_state', 'on': r.enabled, 'baud': r.baud, 'shift': r.shift, 'fc': round(r.fc),
                'pol': r.fix_pol, 'usos': r.usos, 'tx': self.tx_busy}

    def fax_state(self):
        f = self.fax
        return {'t': 'fax_state', 'state': f.state, 'on': f.enabled, 'rows': len(f.rows), 'W': f.W,
                'lpm': f.lpm, 'ioc': f.ioc, 'auto': f.auto, 'slant': round(f.slant),
                'center': round(f.center), 'dev': round(f.dev), 'afc': f.afc,
                'saved': self.saved[-12:]}

    # ------------------------------------------------------------------ звук приёма
    def feed(self, x):
        if not (self.rtty.enabled or self.fax.enabled or self.nx.enabled or self.psk.enabled or self.sstv.enabled):
            return
        if self.tx_busy or self._rig_state().get('tx'):
            return
        try:
            self.rtty.process(x)
            self.fax.process(x)
            self.nx.process(x)
            self.psk.process(x)
            self.sstv.process(x)
        except Exception:
            log.exception(tr("цифровой декодер"))

    def _rt_event(self, ev):
        self.bc(ev, 'rt_on')

    def _fax_event(self, ev):
        if ev['t'] == 'fax_done':
            img = ev.pop('img')
            name = self._save_png(img)
            ev['name'] = name or ''
            self.bc({'t': 'fax_state', **{k: v for k, v in self.fax_state().items() if k != 't'}}, 'fax_on')
        if ev['t'] == 'fax_state':
            ev = self.fax_state()
        self.bc(ev, 'fax_on')

    def _fax_row(self, i, row):
        pkt = b'\x05' + struct.pack('<HH', i & 0xFFFF, len(row)) + row.tobytes()
        for c in self._clients('fax_on'):
            c.ws.send_bin(pkt, important=True)

    def _save_png(self, img):
        try:
            from PIL import Image
            os.makedirs(self.fax_dir, exist_ok=True)
            f = self._rig_state().get('freq_a') or 0
            name = time.strftime('%Y%m%d_%H%M') + ('_%.1fkHz' % (f / 1000) if f else '') + '.png'
            path = os.path.join(self.fax_dir, name)
            # 1809 точек при 576 IOC — квадратные пиксели получаются при сжатии по ширине до ~1/1
            Image.fromarray(img).save(path, optimize=True)
            self.saved.append(name)
            self.saved = self.saved[-50:]
            log.info(tr("Факс сохранён: %s (%d строк)"), path, len(img))
            self.bc({'t': 'fax_saved', 'name': name, 'path': path}, 'fax_on')
            return name
        except Exception as e:
            log.warning(tr("Не удалось сохранить факс: %s"), e)
            self.bc({'t': 'fax_note', 'msg': tr('Не удалось сохранить картинку: %s') % e}, 'fax_on')
            return None

    # ------------------------------------------------------------------ SSTV
    def _sstv_event(self, ev):
        if ev['t'] == 'sstv_done':
            img = ev.pop('img')
            ev['name'] = self._save_sstv(img, ev.get('mode', '')) if img is not None and len(img) > 16 else ''
        self.bc(ev, 'sstv_on')

    def _sstv_row(self, i, rgb):
        pkt = b'\x07' + struct.pack('<HH', i & 0xFFFF, rgb.shape[0]) + rgb.tobytes()
        for c in self._clients('sstv_on'):
            c.ws.send_bin(pkt, important=True)

    def _save_sstv(self, img, mode):
        try:
            from PIL import Image
            os.makedirs(self.sstv_dir, exist_ok=True)
            name = time.strftime('%Y%m%d_%H%M%S') + '_' + mode.replace(' ', '') + '.png'
            Image.fromarray(img).save(os.path.join(self.sstv_dir, name), optimize=True)
            self.sstv_saved = (self.sstv_saved + [name])[-50:]
            log.info(tr("SSTV сохранена: %s"), name)
            return name
        except Exception as e:
            log.warning(tr("SSTV не сохранилась: %s"), e)
            return ''

    def sstv_state(self):
        d = self.sstv
        return {'t': 'sstv_state', 'on': d.enabled, 'state': d.state, 'auto': d.auto,
                'mode': d.mode.name if d.mode else '', 'forced': d.forced.name if d.forced else '',
                'modes': [m.name for m in SSTV_MODES], 'saved': self.sstv_saved[-8:],
                'w': d.mode.w if d.mode else 0, 'h': d.mode.h if d.mode else 0}

    def _send_sstv_whole(self, c):
        rows = self.sstv.rows
        if not rows or self.sstv.state != 'image':
            return
        data = np.stack(rows).astype(np.uint8)
        c.ws.send_bin(b'\x08' + struct.pack('<HH', len(data), data.shape[1]) + data.tobytes(), important=True)

    def _send_whole(self, c):
        rows = self.fax.rows
        if not rows:
            return
        W = len(rows[0])
        data = np.array(rows[-2000:], np.uint8)
        c.ws.send_bin(b'\x06' + struct.pack('<HH', len(data), W) + data.tobytes(), important=True)

    # ------------------------------------------------------------------ передача RTTY
    def _tx_fail(self, c, msg):
        if c:
            c.ws.send_json({'t': 'rt_tx_done', 'ok': False, 'msg': msg})

    async def send(self, c, text, reverse=False):
        text = (text or '').upper()[:2000]
        st = self._rig_state()
        mode = st.get('mode') or ''
        if not text.strip():
            return self._tx_fail(c, tr('Нечего передавать'))
        if not self.st.rig or not self.st.running or not self.st.audio:
            return self._tx_fail(c, tr('Связь с трансивером не запущена'))
        if self.tx_busy:
            return self._tx_fail(c, tr('Передача уже идёт'))
        if self.st.ptt_owner is not None or self.st.ft.busy or self.st.cw.sender.busy or self.st.vk.task:
            return self._tx_fail(c, tr('Передатчик занят (голос или CW)'))
        if not mode.startswith('DATA'):
            return self._tx_fail(c, tr('Для RTTY через звук переключите трансивер в режим DATA-L или DATA-U '
                                    '(сейчас %s)') % (mode or '?'))
        # Любительский стандарт: mark — верхняя частота в эфире. В DATA-U это верхний тон звука.
        rev = (mode == 'DATA-U') != bool(reverse)
        r = self.rtty
        x = rtty_afsk(text, baud=r.baud, shift=r.shift, fc=r.fc, reverse=rev, amp=0.5)
        dur = len(x) / SR
        if dur > self.cfg.get('max_tx_seconds', 180):
            return self._tx_fail(c, tr('Слишком длинный текст: %.0f с передачи') % dur)
        self.tx_task = asyncio.create_task(self._tx_run(x, text, 'rt', SR / self.rtty.baud * 7.5, 0.4))

    async def send_psk(self, c, text):
        text = (text or '')[:2000]
        mode = self._rig_state().get('mode') or ''
        fail = lambda msg: c and c.ws.send_json({'t': 'psk_tx_done', 'ok': False, 'msg': msg})
        if not text.strip():
            return fail(tr('Нечего передавать'))
        if not self.st.rig or not self.st.running or not self.st.audio:
            return fail(tr('Связь с трансивером не запущена'))
        if self.tx_busy or self.st.ptt_owner is not None or self.st.ft.busy or self.st.cw.sender.busy or self.st.vk.task:
            return fail(tr('Передатчик занят'))
        if mode != 'DATA-U':
            return fail(tr('Для PSK31 переключите трансивер в режим DATA-U (сейчас %s)') % (mode or '?'))
        x = psk31_tx(text, fc=self.psk.fc, amp=0.5)
        if len(x) / SR > self.cfg.get('max_tx_seconds', 180):
            return fail(tr('Слишком длинный текст'))
        self.tx_task = asyncio.create_task(self._tx_run(x, text, 'psk', SR / 31.25 * 9, 32 / 31.25))

    async def _tx_run(self, x, text, pre, per_char, lead_s):
        st, tx = self.st, self.st.audio.tx
        flag = pre + '_on'
        self.tx_busy = True
        self.bc({'t': pre + '_tx_start', 'text': text}, flag)
        log.info(tr("%s передача: %s"), pre.upper(), text.replace('\n', ' ')[:80])
        ok, msg = True, ''
        try:
            tx.clear()
            st.rig.ptt(True)
            st.push_status()
            await asyncio.sleep(0.12)                       # реле/ALC
            lead = int(lead_s * SR)
            i, step, t0 = 0, SR // 20, time.monotonic()
            last_p = -1
            while i < len(x):
                # держим в буфере ~200 мс: и без пропусков, и без переполнения (макс. 600 мс)
                ahead = (i / SR) - (time.monotonic() - t0)
                if ahead > 0.2:
                    await asyncio.sleep(0.02)
                    continue
                tx.push(x[i:i + step])
                i += step
                p = int(max(0, (i - lead - 0.2 * SR)) / per_char)
                if p != last_p:
                    last_p = p
                    self.bc({'t': pre + '_tx_progress', 'pos': min(p, len(text))}, flag)
            await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            ok, msg = False, tr('Передача остановлена')
        except Exception as e:
            log.exception(tr("Передача %s"), pre)
            ok, msg = False, tr('Ошибка передачи: %s') % e
        finally:
            try:
                st.rig.ptt(False)
                tx.clear()
            except Exception:
                pass
            self.tx_busy = False
            self.tx_task = None
            st.push_status()
            self.bc({'t': pre + '_tx_done', 'ok': ok, 'msg': msg}, flag)

    def stop(self):
        if self.tx_task:
            self.tx_task.cancel()

    # ------------------------------------------------------------------ сообщения пульта
    async def on_msg(self, c, m):
        t = m['t']
        if t == 'rt_hello':
            c.ws.send_json(self.rt_state())
        elif t == 'rt_dec':
            c.rt_on = bool(m.get('on'))
            kw = {}
            lim = {'baud': (20, 300), 'shift': (50, 1000), 'fc': (300, 3500)}
            for k in ('baud', 'shift', 'fc'):
                if m.get(k):
                    kw[k] = float(min(max(float(m[k]), lim[k][0]), lim[k][1]))
            if 'pol' in m:
                kw['pol'] = int(m['pol'])
            if 'usos' in m:
                kw['usos'] = bool(m['usos'])
            self.rtty.configure(**kw)
            self.update()
            self.bc(self.rt_state(), 'rt_on')
            if not c.rt_on:
                c.ws.send_json(self.rt_state())
        elif t == 'rt_autotune':
            fc = self.rtty.autotune()
            if fc:
                self.bc(self.rt_state(), 'rt_on')
            else:
                c.ws.send_json({'t': 'rt_note', 'msg': tr('Не нашёл RTTY-сигнал со сдвигом %d Гц — проверьте сдвиг '
                                                       'или кликните по сигналу на водопаде') % self.rtty.shift})
        elif t == 'rt_send':
            await self.send(c, m.get('text', ''), bool(m.get('reverse')))
        elif t == 'rt_stop':
            self.stop()
        elif t in ('rt_ai', 'nx_ai'):
            asyncio.create_task(self._translate(c, m.get('text', ''), t[:2]))
        elif t == 'nx_dec':
            c.nx_on = bool(m.get('on'))
            if m.get('fc'):
                self.nx.configure(fc=float(m['fc']))
            self.update()
            c.ws.send_json({'t': 'nx_state', 'on': self.nx.enabled, 'fc': round(self.nx.fc)})
        elif t == 'psk_dec':
            c.psk_on = bool(m.get('on'))
            self.psk.configure(fc=m.get('fc'), afc=m.get('afc'), sql=m.get('sql'))
            self.update()
            c.ws.send_json({'t': 'psk_state', 'on': self.psk.enabled, 'fc': round(self.psk.fc, 1), 'afc': self.psk.afc})
        elif t == 'psk_send':
            await self.send_psk(c, m.get('text', ''))
        elif t == 'psk_stop':
            self.stop()
        elif t == 'sstv_dec':
            c.sstv_on = bool(m.get('on'))
            self.sstv.configure(auto=m.get('auto'), mode=m.get('mode') if 'mode' in m else None)
            self.update()
            c.ws.send_json(self.sstv_state())
            if c.sstv_on:
                self._send_sstv_whole(c)
        elif t == 'sstv_stop':
            self.sstv.finish(tr('остановлено вручную'))
        elif t == 'fax_hello':
            c.ws.send_json(self.fax_state())
            if getattr(c, 'fax_on', False):
                self._send_whole(c)
        elif t == 'fax_dec':
            c.fax_on = bool(m.get('on'))
            self.fax.configure(lpm=m.get('lpm'), ioc=m.get('ioc'), auto=m.get('auto'),
                               center=m.get('center'), afc=m.get('afc'))
            if m.get('center'):
                self.fax.dev = 400.0
            self.update()
            c.ws.send_json(self.fax_state())
            if c.fax_on:
                self._send_whole(c)
        elif t == 'fax_start':
            if not self.fax.enabled:
                c.fax_on = True
                self.update()
            self.fax.start_now()
        elif t == 'fax_stop':
            self.fax.finish(tr('остановлено вручную'))
        elif t == 'fax_shift':
            self.fax.shift_start(float(m.get('px', 0)))
        elif t == 'fax_slant':
            self.fax.configure(slant=self.fax.slant + float(m.get('d', 0)))
            self.bc(self.fax.info(), 'fax_on')

    async def _translate(self, c, text, pre='rt'):
        text = (text or '').strip()[-3000:]
        if not text:
            return c.ws.send_json({'t': pre + '_ai_err', 'msg': tr('Нет текста для перевода')})
        if not self.cfg.get('ai_enabled', True):
            return c.ws.send_json({'t': pre + '_ai_err', 'msg': tr('ИИ выключен в панели управления')})
        lm = ai.LmStudio(self.cfg.get('ai_url'), self.cfg.get('ai_model'))
        sys_p = (tr('Ты помогаешь радиолюбителю читать телетайп (RTTY): метеосводки, навигационные '
                 'предупреждения, любительские связи. Текст принят с ошибками декодера. Переведи на русский '
                 'и кратко объясни смысл: синоптические коды, сокращения, координаты. Не выдумывай. '
                 'Ответ — JSON {"ru": "..."}'))
        try:
            res, model = await asyncio.to_thread(lm.chat_json, sys_p, text,
                                                 {'type': 'object', 'properties': {'ru': {'type': 'string'}},
                                                  'required': ['ru'], 'additionalProperties': False}, 0.2, 900)
            c.ws.send_json({'t': pre + '_ai', 'ru': str(res.get('ru', '')), 'model': model})
        except Exception as e:
            c.ws.send_json({'t': pre + '_ai_err', 'msg': str(e)})

    def client_gone(self, c):
        self.update()
