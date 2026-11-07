"""
Голосовой автовызов: записанное сообщение (CQ и т. п.) уходит в эфир по кругу с паузами на приём.

Сообщения (до 5 ячеек) хранятся на сервере в папке voice рядом с программой (WAV 48 кГц, моно, 16 бит).

Протокол пульта:
  vk_hello                          -> vk_state
  vk_save {slot, name, pcm}         pcm — base64 int16 48 кГц моно
  vk_delete {slot}
  vk_get {slot}                     -> vk_audio {slot, pcm}  (прослушать у себя)
  vk_start {slot, pause, repeats, sig}   sig — порог S-метра (сырое 0..255) для остановки, 0 — не следить
  vk_stop
Сервер шлёт: vk_state {slots, run: {slot, n, repeats, phase, left}|null}, vk_note {msg}.
"""
import asyncio
import base64
import json
import logging
import os
import time
import wave

import numpy as np

from audio import SR

log = logging.getLogger("voicecq")
SLOTS = 5


class _Answered(Exception):
    pass


class VoiceCq:
    def __init__(self, station, base_dir):
        self.st = station
        self.dir = os.path.join(base_dir, 'voice')
        self.meta_path = os.path.join(self.dir, 'names.json')
        self.task = None
        self.run = None              # публичное состояние автовызова
        self.busy = False            # идёт передача сообщения
        try:
            with open(self.meta_path, encoding='utf-8') as f:
                self.names = json.load(f)
        except Exception:
            self.names = {}

    # ------------------------------------------------------------------ хранение
    def _path(self, slot):
        return os.path.join(self.dir, 'cq%d.wav' % slot)

    def _load(self, slot):
        p = self._path(slot)
        if not os.path.isfile(p):
            return None
        with wave.open(p, 'rb') as w:
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, np.int16).astype(np.float32) / 32768

    def slots(self):
        out = []
        for s in range(1, SLOTS + 1):
            p = self._path(s)
            if os.path.isfile(p):
                try:
                    with wave.open(p, 'rb') as w:
                        dur = w.getnframes() / w.getframerate()
                except Exception:
                    dur = 0
                out.append({'slot': s, 'name': self.names.get(str(s), 'CQ %d' % s), 'dur': round(dur, 1)})
            else:
                out.append({'slot': s, 'name': '', 'dur': 0})
        return out

    def state(self):
        return {'t': 'vk_state', 'slots': self.slots(), 'run': self.run}

    def bc(self, obj):
        for c in self.st.clients:
            if c.authed and not c.admin:
                c.ws.send_json(obj)

    def _save(self, slot, name, pcm_b64):
        pcm = np.frombuffer(base64.b64decode(pcm_b64), np.int16)
        if len(pcm) < SR * 0.3:
            raise ValueError('Слишком короткая запись')
        if len(pcm) > SR * 120:
            raise ValueError('Запись длиннее 2 минут')
        os.makedirs(self.dir, exist_ok=True)
        with wave.open(self._path(slot), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        self.names[str(slot)] = (name or 'CQ %d' % slot)[:40]
        with open(self.meta_path, 'w', encoding='utf-8') as f:
            json.dump(self.names, f, ensure_ascii=False)
        log.info("Голосовое сообщение %d сохранено: %s, %.1f с", slot, self.names[str(slot)], len(pcm) / SR)

    # ------------------------------------------------------------------ автовызов
    def _why_not(self):
        st = self.st
        mode = (st.rig.state.get('mode') if st.rig else '') or ''
        if not st.rig or not st.running or not st.audio:
            return 'Связь с трансивером не запущена'
        if st.ptt_owner is not None:
            return 'Сейчас идёт передача с микрофона'
        if st.cw.sender.busy or st.digi.tx_busy:
            return 'Идёт передача CW/RTTY'
        if mode.startswith('CW') or mode.startswith('RTTY') or mode == 'PSK':
            return 'Голосовой вызов работает в SSB/AM/FM (сейчас %s)' % mode
        return ''

    async def start(self, c, slot, pause, repeats, sig):
        if self.task:
            return c.ws.send_json({'t': 'vk_note', 'msg': 'Автовызов уже идёт'})
        why = self._why_not()
        if why:
            return c.ws.send_json({'t': 'vk_note', 'msg': why})
        x = self._load(slot)
        if x is None:
            return c.ws.send_json({'t': 'vk_note', 'msg': 'Ячейка %d пустая — сначала запишите сообщение' % slot})
        if len(x) / SR > self.st.cfg.get('max_tx_seconds', 180) - 2:
            return c.ws.send_json({'t': 'vk_note', 'msg': 'Сообщение длиннее ограничения передачи'})
        pause = float(min(max(pause, 2), 600))
        repeats = int(min(max(repeats, 1), 100))
        self.task = asyncio.create_task(self._loop(x, slot, pause, repeats, int(sig or 0)))

    def stop(self, why=''):
        if self.task:
            self.task.cancel()
            if why:
                self.bc({'t': 'vk_note', 'msg': 'Автовызов остановлен: ' + why})

    def _set_run(self, **kw):
        self.run = dict(self.run or {}, **kw)
        self.bc(self.state())

    async def _loop(self, x, slot, pause, repeats, sig):
        name = self.names.get(str(slot), 'CQ %d' % slot)
        log.info("Автовызов: «%s», пауза %.0f с, повторов %d", name, pause, repeats)
        self.run = {'slot': slot, 'name': name, 'n': 0, 'repeats': repeats, 'phase': 'tx', 'left': 0}
        ended = 'готово'
        try:
            for n in range(1, repeats + 1):
                why = self._why_not()
                if why:
                    ended = why
                    break
                self._set_run(n=n, phase='tx', left=round(len(x) / SR))
                await self._transmit(x)
                if n == repeats:
                    break
                # пауза: слушаем, не ответил ли кто
                t_end = time.monotonic() + pause
                loud = 0.0
                while time.monotonic() < t_end:
                    self._set_run(phase='rx', left=int(t_end - time.monotonic() + 0.99))
                    await asyncio.sleep(0.25)
                    s = self.st.rig.state if self.st.rig else {}
                    hot = (sig and (s.get('smeter') or 0) >= sig) or \
                          ((s.get('mode') or '').startswith('FM') and sig and s.get('busy'))
                    loud = loud + 0.25 if hot else 0.0
                    if loud >= 0.75:
                        ended = 'в паузе появился сигнал — похоже, ответили'
                        raise _Answered
        except _Answered:
            pass
        except asyncio.CancelledError:
            ended = 'остановлено'
        except Exception as e:
            log.exception("Автовызов")
            ended = 'ошибка: %s' % e
        finally:
            self._ptt(False)
            self.task = None
            self.run = None
            self.bc(self.state())
            self.bc({'t': 'vk_note', 'msg': 'Автовызов: ' + ended})
            log.info("Автовызов закончен: %s", ended)

    def _ptt(self, on):
        st = self.st
        if on:
            self.busy = True
            st.audio.tx.clear()
            st.rig.ptt(True)
        elif self.busy:
            try:
                st.rig.ptt(False)
                st.audio.tx.clear()
            except Exception:
                pass
            self.busy = False
        st.push_status()

    async def _transmit(self, x):
        tx = self.st.audio.tx
        self._ptt(True)
        try:
            await asyncio.sleep(0.15)                      # реле и ALC
            i, step, t0 = 0, SR // 20, time.monotonic()
            while i < len(x):
                if (i / SR) - (time.monotonic() - t0) > 0.2:   # держим в буфере ~200 мс
                    await asyncio.sleep(0.02)
                    continue
                tx.push(x[i:i + step])
                i += step
            await asyncio.sleep(0.35)
        finally:
            self._ptt(False)

    # ------------------------------------------------------------------ сообщения
    async def on_msg(self, c, m):
        t = m['t']
        slot = int(m.get('slot') or 0)
        if t == 'vk_hello':
            c.ws.send_json(self.state())
        elif t == 'vk_save':
            if not 1 <= slot <= SLOTS:
                return
            if self.run and self.run.get('slot') == slot:
                return c.ws.send_json({'t': 'vk_note', 'msg': 'Эта ячейка сейчас передаётся'})
            try:
                await asyncio.to_thread(self._save, slot, m.get('name', ''), m.get('pcm', ''))
                self.bc(self.state())
                c.ws.send_json({'t': 'vk_note', 'msg': 'Сообщение %d сохранено' % slot})
            except Exception as e:
                c.ws.send_json({'t': 'vk_note', 'msg': 'Не сохранилось: %s' % e})
        elif t == 'vk_delete':
            try:
                os.remove(self._path(slot))
            except OSError:
                pass
            self.names.pop(str(slot), None)
            self.bc(self.state())
        elif t == 'vk_rename':
            if os.path.isfile(self._path(slot)):
                self.names[str(slot)] = (m.get('name') or 'CQ %d' % slot)[:40]
                with open(self.meta_path, 'w', encoding='utf-8') as f:
                    json.dump(self.names, f, ensure_ascii=False)
                self.bc(self.state())
        elif t == 'vk_get':
            x = self._load(slot)
            if x is not None:
                pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()
                c.ws.send_json({'t': 'vk_audio', 'slot': slot, 'pcm': base64.b64encode(pcm).decode()})
        elif t == 'vk_start':
            await self.start(c, slot, float(m.get('pause') or 10), int(m.get('repeats') or 10), m.get('sig'))
        elif t == 'vk_stop':
            self.stop()
