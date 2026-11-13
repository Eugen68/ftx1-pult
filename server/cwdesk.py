"""
CW-стол: декодер + ИИ + передача + автоответ. Живёт внутри Station (main.py).

Ход работы:
  звук приёма -> CwDecoder -> символы (в реальном времени всем пультам)
  пауза после передачи корреспондента -> «переход» (over) закрыт -> разбор ИИ (если включён)
  -> если включён автоответ, ИИ уверен, что зовут нас, И наш позывной реально есть в принятом,
     -> ИИ составляет ответ -> отсчёт N секунд (можно отменить) -> передача.
"""

from i18n import tr
import asyncio
import logging
import re
import time

import ai
from cw import CwDecoder, CwSender, clean_tx_text

log = logging.getLogger("cwdesk")

OP_KEYS = ('my_call', 'my_name', 'my_qth', 'my_extra')


class CwDesk:
    def __init__(self, station):
        self.st = station
        self.dec = CwDecoder(self._dec_event)
        self.sender = CwSender(self._rig_send_threadsafe, self._rig_state, self._tx_event_thread)
        self.over = ''                    # текущая передача корреспондента
        self.last_rx = 0.0                # время последнего принятого знака
        self.history = []                 # [{dir: rx/tx, text, clean?, t}]
        self.analysis = None
        self.ai_busy = False
        self.proposal = None              # {'text', 'ru', 'deadline', 'task'}
        self.auto_count = 0
        self.tx_text = ''
        self.cfg_dec = {'enabled': False, 'cyr': False, 'freq': 0, 'sq': 15}

    # ------------------------------------------------------------------ helpers
    @property
    def cfg(self):
        return self.st.cfg

    def op(self):
        return {k: self.cfg.get(k, '') for k in OP_KEYS}

    def lm(self):
        return ai.LmStudio(self.cfg.get('ai_url'), self.cfg.get('ai_model'))

    def bc(self, obj):
        for c in self.st.clients:
            if c.authed and not c.admin:
                c.ws.send_json(obj)

    def _rig_send_threadsafe(self, cmd):
        rig = self.st.rig
        if rig:
            self.st.loop.call_soon_threadsafe(rig.send, cmd)

    def _rig_state(self):
        return self.st.rig.state if self.st.rig else {}

    def _tx_event_thread(self, ev):
        try:
            self.st.loop.call_soon_threadsafe(self._tx_event, ev)
        except RuntimeError:
            pass

    def auto_on(self):
        return any(getattr(c, 'cw_auto', False) for c in self.st.clients if c.authed)

    def state_msg(self):
        return {'t': 'cw_state', 'dec': self.cfg_dec, 'op': self.op(), 'history': self.history[-30:],
                'analysis': self.analysis, 'over': self.over, 'busy_tx': self.sender.busy,
                'tx_text': self.tx_text if self.sender.busy else '', 'ai_busy': self.ai_busy,
                'auto': self.auto_on(), 'method': self.cfg.get('cw_method', 'cat'),
                'ai_url': self.cfg.get('ai_url'), 'ai_model': self.cfg.get('ai_model'),
                'auto_delay': self.cfg.get('cw_auto_delay', 6), 'auto_max': self.cfg.get('cw_auto_max', 4),
                'proposal': self._proposal_public()}

    def _proposal_public(self):
        p = self.proposal
        if not p:
            return None
        return {'text': p['text'], 'ru': p['ru'], 'model': p.get('model', ''), 'left': max(0, round(p['deadline'] - time.monotonic(), 1)), 'auto': p['auto']}

    # ------------------------------------------------------------------ декодер
    def update_decoder(self):
        en = any(getattr(c, 'cw_dec', False) for c in self.st.clients if c.authed)
        self.cfg_dec['enabled'] = en
        self.dec.configure(enabled=en, cyr=self.cfg_dec['cyr'])

    def feed(self, x):
        """Звук приёма 48 кГц (вызывается в цикле событий)."""
        if not self.dec.enabled:
            return
        st = self._rig_state()
        if st.get('tx') or self.sender.busy:
            return
        self.dec.process(x)

    def _dec_event(self, ev):
        if ev['t'] == 'cw_rx':
            self.over += ev['text']
            if len(self.over) > 1500:
                self.over = self.over[-1200:]
            self.last_rx = time.monotonic()
        self.bc(ev)

    async def tick(self):
        """Каждые 0,25 с: конец передачи корреспондента, отсчёт автоответа."""
        now = time.monotonic()
        txt = self.over.strip()
        if txt and not self.sender.busy:
            quiet = now - self.last_rx
            ends_over = bool(re.search(r'(\bK|\bKN|\bBK|\(|\+|<SK>|\bSK|\bAR)\s*$', txt))
            if (quiet > 1.5 and ends_over) or quiet > 3.0:
                self.over = ''
                if len(txt.replace(' ', '')) >= 3:
                    self._bg(self._close_over(txt))
        p = self.proposal
        if p and p['auto'] and now >= p['deadline']:
            self.proposal = None
            if not self.auto_on():
                self.bc({'t': 'cw_note', 'msg': tr('Автоответ выключен — передача отменена')})
            else:
                self.auto_count += 1
                await self.send(p['text'], who=tr('автоответ'))
                if self.auto_count >= int(self.cfg.get('cw_auto_max', 4)):
                    self._auto_off_all(tr('Автоответ выключен: достигнут лимит %d передач подряд — продолжите вручную')
                                       % self.auto_count)
            self.bc({'t': 'cw_proposal', 'p': None})

    async def _close_over(self, txt):
        item = {'dir': 'rx', 'text': txt, 't': time.time()}
        self.history.append(item)
        self.history = self.history[-40:]
        self.bc({'t': 'cw_over', 'item': item})
        if self.cfg.get('ai_auto_analyze', True) or self.auto_on():
            await self.analyze(item)

    # ------------------------------------------------------------------ ИИ (с запасным вариантом без него)
    def _ai_failed(self, e):
        """LM Studio недоступна — работаем по правилам; тост не чаще раза в 2 минуты."""
        now = time.monotonic()
        msg = str(e) if isinstance(e, ai.AiError) else tr('Ошибка ИИ: %s') % e
        self.ai_note = tr('ИИ недоступен — разбор по правилам')
        if now - getattr(self, 'ai_err_t', -1e9) > 120:
            self.ai_err_t = now
            self.bc({'t': 'cw_ai_err', 'msg': msg + tr(' Пока работаю без ИИ — по правилам CW-связи.')})

    def _use_lm(self):
        return bool(self.cfg.get('ai_enabled', True))

    async def analyze(self, item=None, client=None):
        if self.ai_busy:
            return
        item = item or next((h for h in reversed(self.history) if h['dir'] == 'rx'), None)
        if not item:
            if client:
                client.ws.send_json({'t': 'cw_note', 'msg': tr('Пока нечего разбирать — нет принятого текста')})
            return
        hist = [h for h in self.history if h is not item]
        res = None
        if self._use_lm():
            self.ai_busy = True
            self.bc({'t': 'cw_ai_busy', 'on': True, 'what': tr('Разбираю принятое…')})
            try:
                res = await asyncio.to_thread(ai.analyze, self.lm(), item['text'], hist, self.op())
            except Exception as e:
                if not isinstance(e, ai.AiError):
                    log.exception(tr("ИИ: ошибка разбора"))
                self._ai_failed(e)
            finally:
                self.ai_busy = False
                self.bc({'t': 'cw_ai_busy', 'on': False})
        if res is None:
            res = ai.analyze_rules(item['text'], hist, self.op())
            if self._use_lm():
                res['note'] = tr('LM Studio недоступна — разобрано по правилам')
        item['clean'] = res.get('clean') or item['text']
        for k in ('call', 'name', 'qth'):
            if res.get(k):
                item[k] = res[k]
        res['raw'] = item['text']
        my = (self.cfg.get('my_call') or '').upper()
        res['calls_me_strict'] = bool(my) and (ai.calls_me_strict(item['text'], my) or
                                                my in item['text'].upper().replace(' ', ''))
        self.analysis = res
        self.bc({'t': 'cw_ai', 'a': res})
        # автоответ — только при двойной проверке: разбор И наличие позывного в сырых данных
        if self.auto_on():
            if not self.cfg.get('my_call'):
                self.bc({'t': 'cw_note', 'msg': tr('Автоответ: укажите свой позывной')})
            elif res['calls_me'] and res['calls_me_strict'] and res.get('over', True):
                await self.make_reply(auto=True)
            elif res['calls_me'] and not res['calls_me_strict']:
                self.bc({'t': 'cw_note', 'msg': tr('Автоответ: ИИ считает, что зовут вас, но вашего позывного '
                                               'в принятом нет — не отвечаю')})

    async def make_reply(self, task='', auto=False, client=None):
        if self.ai_busy:
            return
        res = None
        if self._use_lm():
            self.ai_busy = True
            self.bc({'t': 'cw_ai_busy', 'on': True, 'what': tr('Составляю ответ…')})
            try:
                res = await asyncio.to_thread(ai.reply, self.lm(), self.history, self.analysis, self.op(), task)
            except Exception as e:
                if not isinstance(e, ai.AiError):
                    log.exception(tr("ИИ: ошибка ответа"))
                self._ai_failed(e)
            finally:
                self.ai_busy = False
                self.bc({'t': 'cw_ai_busy', 'on': False})
        if res is None or not clean_tx_text(res.get('text', '')):
            res = ai.reply_rules(self.history, self.analysis, self.op())
        text = clean_tx_text(res['text'])[:250]
        if not text:
            self.bc({'t': 'cw_note', 'msg': res.get('ru') or tr('Отвечать нечего')})
            return
        delay = max(3, int(self.cfg.get('cw_auto_delay', 6)))
        self.proposal = {'text': text, 'ru': res.get('ru', ''), 'auto': auto, 'model': res.get('model', ''),
                         'deadline': time.monotonic() + (delay if auto else 1e9)}
        self.bc({'t': 'cw_proposal', 'p': self._proposal_public()})

    def cancel_proposal(self, who=''):
        if self.proposal:
            self.proposal = None
            self.bc({'t': 'cw_proposal', 'p': None})
            if who:
                self.bc({'t': 'cw_note', 'msg': tr('Автоответ отменён (%s)') % who})

    def _auto_off_all(self, msg):
        for c in self.st.clients:
            c.cw_auto = False
        self.auto_count = 0
        self.bc({'t': 'cw_auto', 'on': False})
        self.bc({'t': 'cw_note', 'msg': msg})

    # ------------------------------------------------------------------ передача
    async def send(self, text, who='', client=None):
        def fail(msg):
            (client.ws.send_json if client else (lambda o: self.bc(o)))({'t': 'cw_note', 'msg': msg})
            return False
        rig = self.st.rig
        if not rig or not self.st.running:
            return fail(tr('Нет связи с трансивером'))
        text = clean_tx_text(text)[:400]
        if not text:
            return fail(tr('Нечего передавать'))
        st = rig.state
        if not str(st.get('mode', '')).startswith('CW'):
            return fail(tr('Передача телеграфом возможна только в режиме CW (сейчас %s)') % (st.get('mode') or '?'))
        if self.st.ptt_owner is not None or self.st.ft.busy or self.st.vk.task:
            return fail(tr('Сейчас идёт передача голосом'))
        if self.sender.busy:
            return fail(tr('Предыдущий текст ещё передаётся'))
        method = self.cfg.get('cw_method', 'cat')
        wpm = int(st.get('key_speed') or 20)
        self.cancel_proposal()
        ok = self.sender.send(text, method=method, wpm=wpm, port=self.cfg.get('cw_port') or self.cfg.get('ptt_port'),
                              max_s=int(self.cfg.get('max_tx_seconds', 180)))
        if not ok:
            return fail(tr('Передатчик занят'))
        self.tx_text = text
        item = {'dir': 'tx', 'text': text, 't': time.time()}
        self.history.append(item)
        self.history = self.history[-40:]
        self.over = ''
        log.info(tr("CW передача (%s, %d WPM%s): %s"), method, wpm, ', ' + who if who else '', text)
        self.bc({'t': 'cw_tx_start', 'item': item, 'wpm': wpm})
        return True

    def _tx_event(self, ev):
        if ev['t'] == 'cw_tx_done':
            self.tx_text = ''
            self.last_rx = time.monotonic()
            if ev.get('err'):
                ev['msg'] = tr('Ошибка передачи: ') + ev['err']
        self.bc(ev)

    def stop(self):
        self.sender.stop()
        self.cancel_proposal()

    # ------------------------------------------------------------------ сообщения пульта
    async def on_msg(self, c, m):
        t = m['t']
        if t == 'cw_hello':
            c.ws.send_json(self.state_msg())
        elif t == 'cw_dec':
            c.cw_dec = bool(m.get('on'))
            if 'cyr' in m:
                self.cfg_dec['cyr'] = bool(m['cyr'])
            if 'freq' in m:
                self.cfg_dec['freq'] = float(m.get('freq') or 0)
                self.dec.configure(freq=self.cfg_dec['freq'])
            if 'sq' in m:
                self.cfg_dec['sq'] = float(m['sq'])
                self.dec.configure(sq=self.cfg_dec['sq'])
            self.update_decoder()
            self.bc({'t': 'cw_dec', 'dec': self.cfg_dec})
        elif t == 'cw_clear':
            self.over = ''
            self.bc({'t': 'cw_clear'})
        elif t == 'cw_new':
            self.history, self.analysis, self.over = [], None, ''
            self.cancel_proposal()
            self.auto_count = 0
            self.bc({'t': 'cw_new'})
        elif t == 'cw_send':
            await self.send(m.get('text', ''), client=c)
        elif t == 'cw_stop':
            self.stop()
        elif t == 'cw_ai_analyze':
            if m.get('text'):
                item = {'dir': 'rx', 'text': str(m['text'])[:1500], 't': time.time()}
                self.history.append(item)
                self._bg(self.analyze(item, client=c))
            else:
                self._bg(self.analyze(client=c))
        elif t == 'cw_ai_reply':
            self._bg(self.make_reply(task=str(m.get('task', ''))[:500], client=c))
        elif t == 'cw_proposal_cancel':
            self.cancel_proposal(who=c.name)
        elif t == 'cw_auto':
            c.cw_auto = bool(m.get('on'))
            if c.cw_auto:
                self.auto_count = 0
                if not self.cfg.get('my_call'):
                    c.cw_auto = False
                    c.ws.send_json({'t': 'cw_note', 'msg': tr('Для автоответа укажите свой позывной')})
            if not self.auto_on():
                self.cancel_proposal()
            self.bc({'t': 'cw_auto', 'on': self.auto_on()})
        elif t == 'cw_op':
            changed = False
            for k in OP_KEYS:
                if k in m:
                    v = str(m[k]).strip()[:60]
                    if k == 'my_call':
                        v = re.sub(r'[^A-Z0-9/]', '', v.upper())
                    if self.cfg.get(k) != v:
                        self.cfg[k] = v
                        changed = True
            for k, lo, hi in (('cw_auto_delay', 3, 30), ('cw_auto_max', 1, 20)):
                if k in m:
                    self.cfg[k] = max(lo, min(hi, int(m[k])))
                    changed = True
            if changed:
                self.st.save_cfg()
            self.bc({'t': 'cw_op', 'op': self.op(), 'auto_delay': self.cfg.get('cw_auto_delay', 6),
                     'auto_max': self.cfg.get('cw_auto_max', 4)})
        elif t == 'cw_ai_test':
            self._bg(self._ai_test(c))

    async def _ai_test(self, c):
        try:
            models = await asyncio.to_thread(self.lm().models)
            c.ws.send_json({'t': 'cw_ai_test', 'ok': True, 'models': models, 'model': self.cfg.get('ai_model')})
        except ai.AiError as e:
            c.ws.send_json({'t': 'cw_ai_test', 'ok': False, 'msg': str(e)})

    def _bg(self, coro):
        """ИИ отвечает долго — не держим ни защиту PTT, ни команды пульта."""
        task = asyncio.ensure_future(coro)
        self._tasks = getattr(self, '_tasks', set())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def client_gone(self, c):
        self.update_decoder()
        if not self.auto_on():
            self.cancel_proposal()
