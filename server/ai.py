"""
ИИ-помощник CW через LM Studio (локальный сервер, совместимый с OpenAI API: http://localhost:1234/v1).

Модель не слушает звук — его распознаёт декодер (cw.py). Модель получает принятый текст
с возможными ошибками и:
  * восстанавливает вероятный исходный текст по правилам CW-связи;
  * переводит на русский, расшифровывает сокращения и Q-коды;
  * вытаскивает позывной корреспондента, RST, имя, QTH; понимает, зовут ли нас;
  * составляет ответ в принятом в CW формате.
"""

from i18n import tr
import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger("ai")


class AiError(Exception):
    pass


SYS_ANALYZE = """Ты — опытный оператор-телеграфист и помощник радиолюбителя {my_call}.
Тебе дают текст, принятый автоматическим декодером азбуки Морзе (CW) с эфира. В нём бывают
ошибки распознавания: перепутаны похожие по звучанию знаки (E/I/S/H/5, T/M/O/0, A/N, U/V, R/K, D/B),
пропущены или лишние пробелы, лишние E и T от помех, «*» — нераспознанный знак.
Восстанови наиболее вероятный исходный текст, опираясь на стандартную структуру CW-связи:
CQ, DE, позывные (формат позывных ITU, например UA3ABC, R6AAF, DL1XX, K1ABC), RST (599, 5NN, 579),
OP/NAME, QTH, RIG, ANT, WX, TNX, FER, UR, HW?, BK, K, KN, AR, SK, 73, GM/GA/GE.
Не выдумывай того, чего в принятом нет: если фрагмент не восстановить — оставь как есть.
Отвечай строго одним JSON-объектом без пояснений вокруг."""

USER_ANALYZE = """Мой позывной: {my_call}.
Предыдущий ход связи (может быть пустым):
{history}

Принятый текст (сырой, с ошибками):
{raw}

Верни JSON:
{{"clean": "восстановленный текст латиницей, как передавали",
 "ru": "смысл по-русски, с расшифровкой сокращений и Q-кодов, 1–3 предложения",
 "call": "позывной передающей станции или пусто",
 "to": "кого вызывают (позывной адресата, CQ, или пусто)",
 "rst": "RST, который нам дали, или пусто",
 "name": "имя оператора или пусто",
 "qth": "QTH или пусто",
 "kind": "cq | answer | report | final | other",
 "calls_me": true или false — адресовано ли это мне ({my_call}),
 "over": true или false — закончил ли корреспондент передачу и ждёт ответа (K, KN, BK, AR в конце),
 "confidence": число от 0 до 1}}"""

SYS_REPLY = """Ты — опытный оператор-телеграфист, ведёшь CW-связь от имени {my_call}.
Составляешь текст для передачи телеграфом. Правила:
- только латиница, цифры и знаки . , ? / = +; без кириллицы; верхний регистр;
- служебные знаки пиши как <AR>, <KN>, <SK>, <BT>;
- кратко, как принято в эфире: стандартные сокращения (TNX, FER, UR, RST, OP, QTH, HW?, 73, GL);
- начинай с позывного корреспондента и DE {my_call}, заканчивай K, KN или <SK> по смыслу;
- RST по умолчанию 599, если оператор не указал иное; повторяй RST, имя и QTH дважды;
- не придумывай данных о себе сверх указанных ниже.
Данные оператора: позывной {my_call}; имя {my_name}; QTH {my_qth}; дополнительно: {my_extra}.
Отвечай строго одним JSON-объектом: {{"text": "текст для передачи", "ru": "что это значит по-русски, кратко"}}"""

USER_REPLY = """Ход связи (сверху вниз, «ОН» — корреспондент, «Я» — мои передачи):
{history}

Последний разбор принятого: {analysis}

Задача: {task}"""


def _strip_think(s):
    return re.sub(r'<think>.*?</think>', '', s or '', flags=re.S).strip()


def _parse_json(s):
    s = _strip_think(s)
    m = re.search(r'\{.*\}', s, re.S)
    if not m:
        raise AiError(tr("Модель ответила не в формате JSON: ") + s[:200])
    txt = m.group(0)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        # частая ошибка моделей — висячие запятые
        try:
            return json.loads(re.sub(r',\s*([}\]])', r'\1', txt))
        except json.JSONDecodeError as e:
            raise AiError(tr("Не удалось разобрать JSON модели: %s") % e)


class LmStudio:
    def __init__(self, url='http://localhost:1234/v1', model='', timeout=90):
        self.url = (url or 'http://localhost:1234/v1').rstrip('/')
        if not self.url.endswith('/v1'):
            self.url += '/v1'
        self.model = model
        self.timeout = timeout

    def _req(self, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method='POST' if data else 'GET',
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            msg = e.read().decode('utf-8', 'replace')[:300]
            raise AiError(tr("LM Studio ответил ошибкой %d: %s") % (e.code, msg))
        except urllib.error.URLError as e:
            raise AiError(tr("LM Studio недоступен по адресу %s (%s). Запустите LM Studio и включите "
                          "сервер: вкладка Developer → Start Server.") % (self.url, e.reason))
        except TimeoutError:
            raise AiError(tr("LM Studio не ответил за %d с — модель слишком большая или занята") % (timeout or self.timeout))

    def models(self):
        r = self._req('/models', timeout=5)
        return [m.get('id') for m in r.get('data', []) if m.get('id') and 'embed' not in m.get('id', '')]

    def chat_json(self, system, user, schema=None, temperature=0.2, max_tokens=700):
        model = self.model
        if not model:
            ms = self.models()
            if not ms:
                raise AiError(tr("В LM Studio не загружена ни одна модель"))
            model = ms[0]
        body = {'model': model, 'temperature': temperature, 'max_tokens': max_tokens, 'stream': False,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        if schema:
            body['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'answer', 'strict': True, 'schema': schema}}
        try:
            r = self._req('/chat/completions', body)
        except AiError as e:
            if schema and ('response_format' in str(e) or 'json_schema' in str(e) or '400' in str(e)):
                body.pop('response_format')              # старая версия LM Studio — без схемы
                r = self._req('/chat/completions', body)
            else:
                raise
        try:
            content = r['choices'][0]['message']['content']
        except (KeyError, IndexError):
            raise AiError(tr("Неожиданный ответ LM Studio: %s") % str(r)[:200])
        return _parse_json(content), model


ANALYZE_SCHEMA = {
    'type': 'object',
    'properties': {k: {'type': t} for k, t in [
        ('clean', 'string'), ('ru', 'string'), ('call', 'string'), ('to', 'string'), ('rst', 'string'),
        ('name', 'string'), ('qth', 'string'), ('kind', 'string'), ('calls_me', 'boolean'),
        ('over', 'boolean'), ('confidence', 'number')]},
    'required': ['clean', 'ru', 'call', 'to', 'rst', 'name', 'qth', 'kind', 'calls_me', 'over', 'confidence'],
    'additionalProperties': False,
}
REPLY_SCHEMA = {
    'type': 'object',
    'properties': {'text': {'type': 'string'}, 'ru': {'type': 'string'}},
    'required': ['text', 'ru'],
    'additionalProperties': False,
}


def fmt_history(hist, n=8):
    lines = []
    for h in hist[-n:]:
        who = tr('Я') if h['dir'] == 'tx' else tr('ОН')
        lines.append('%s: %s' % (who, h.get('clean') or h['text']))
    return '\n'.join(lines) or tr('(начало связи)')


def analyze(lm, raw, history, op):
    my = (op.get('my_call') or 'MYCALL').upper()
    sys_p = tr(SYS_ANALYZE).format(my_call=my)
    user = tr(USER_ANALYZE).format(my_call=my, history=fmt_history(history), raw=raw.strip())
    res, model = lm.chat_json(sys_p, user, ANALYZE_SCHEMA)
    out = {k: res.get(k, '') for k in ANALYZE_SCHEMA['properties']}
    out['calls_me'] = bool(res.get('calls_me'))
    out['over'] = bool(res.get('over'))
    try:
        out['confidence'] = float(res.get('confidence') or 0)
    except (TypeError, ValueError):
        out['confidence'] = 0.0
    out['model'] = model
    return out


def reply(lm, history, analysis, op, task=''):
    my = (op.get('my_call') or 'MYCALL').upper()
    sys_p = tr(SYS_REPLY).format(my_call=my, my_name=op.get('my_name') or '—', my_qth=op.get('my_qth') or '—',
                             my_extra=op.get('my_extra') or '—')
    if not task:
        task = (tr('Составь мой следующий ответ корреспонденту по ходу связи: если он позвал меня — дай RST, '
                'имя и QTH; если дал свои данные — поблагодари и заверши связь (73 <SK>); если он дал CQ — '
                'ответь на вызов коротко (его позывной DE мой позывной K).'))
    user = tr(USER_REPLY).format(history=fmt_history(history), task=task,
                             analysis=json.dumps({k: analysis.get(k) for k in ('clean', 'call', 'rst', 'name', 'qth', 'kind')},
                                                 ensure_ascii=False) if analysis else tr('(нет)'))
    res, model = lm.chat_json(sys_p, user, REPLY_SCHEMA, temperature=0.3, max_tokens=400)
    return {'text': str(res.get('text', '')), 'ru': str(res.get('ru', '')), 'model': model}


def calls_me_strict(raw, my_call):
    """Независимая от модели проверка: есть ли мой позывной в принятом (с одной ошибкой)."""
    my = (my_call or '').upper()
    if len(my) < 3:
        return False
    for w in re.findall(r'[A-Z0-9/]+', (raw or '').upper()):
        w = w.split('/')[0] if len(w.split('/')[0]) >= 3 else w
        if w == my:
            return True
        if len(w) == len(my) and sum(a != b for a, b in zip(w, my)) <= 1 and len(my) >= 5:
            return True
    return False


# ======================================================================
# Без LM Studio: разбор и ответ по правилам CW-связи.
# Работает всегда; модель только улучшает результат, если она доступна.
# ======================================================================
CALL_RE = r'(?:[A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9]{1,2}[A-Z]{1,4}(?:/[A-Z0-9]{1,4})?'
_NOT_NAME = {'HR', 'IS', 'ES', 'DE', 'UR', 'RST', 'QTH', 'K', 'KN', 'BK', 'TNX', 'FER', 'HW', 'PSE', 'CALL'}
_STOP = r'(?=\s+(?:NAME|OP|RST|UR|HW|BK|KN|K|AR|ES|TNX|FB|RIG|ANT|WX|PWR|=|\+|<|\()|\s*$)'
GLOSSARY = {
    'CQ': 'общий вызов', 'DE': 'от', 'TNX': 'спасибо', 'TU': 'спасибо', 'FER': 'за', 'UR': 'ваш',
    'RST': 'рапорт', 'HW?': 'как слышите?', 'HW': 'как слышите', 'PSE': 'пожалуйста', 'AGN': 'повторите',
    'QRZ?': 'кто меня зовёт?', 'QSL': 'подтверждаю', 'QRM': 'помехи', 'QSB': 'замирания', 'QRN': 'атм. помехи',
    'FB': 'отлично', 'GM': 'доброе утро', 'GA': 'добрый день', 'GE': 'добрый вечер', 'GL': 'удачи',
    '73': 'наилучшие пожелания', 'OP': 'оператор', 'RIG': 'трансивер', 'ANT': 'антенна', 'WX': 'погода',
    'PWR': 'мощность', 'ES': 'и', 'HR': 'здесь', 'SRI': 'извините', 'BK': 'перехожу на приём',
    'K': 'приём', 'KN': 'приём, только вам', 'SK': 'конец связи', 'R': 'принял полностью', 'CUAGN': 'до встречи',
}


def _norm(raw):
    t = (raw or '').upper().replace('*', ' ')
    t = re.sub(r'\b5NN\b', '599', t)
    t = re.sub(r'\b([345])N([N9])\b', lambda m: m.group(1) + '9' + m.group(2).replace('N', '9'), t)
    return re.sub(r'\s+', ' ', t).strip()


def analyze_rules(raw, history, op):
    my = (op.get('my_call') or '').upper()
    t = _norm(raw)
    words = t.split()
    calls = [w for w in re.findall(r'\b' + CALL_RE + r'\b', t) if not re.fullmatch(r'[0-9]+', w)]
    call = to = ''
    m = list(re.finditer(r'(?:\b(' + CALL_RE + r'|CQ|QRZ\??)\s+)?DE\s+(' + CALL_RE + r')\b', t))
    if m:
        to, call = (m[-1].group(1) or ''), m[-1].group(2)
    elif calls:
        call = calls[-1]
    if not to and 'CQ' in words:
        to = 'CQ'
    if not call:                                  # «… DE» потерян — берём позывной из прошлых разборов
        for h in reversed(history or []):
            if h.get('dir') == 'rx' and h.get('call'):
                call = h['call']
                break
    rst = ''
    r = re.search(r'\b(?:RST|UR)\s+(?:IS\s+)?([1-5][1-9]{2})\b', t) or re.search(r'\b([345][1-9]9)\b', t)
    if r:
        rst = r.group(1)
    name = ''
    n = re.search(r'\b(?:NAME|OP)\s+(?:IS\s+|HR\s+)*([A-Z]{2,12})\b', t)
    if n and n.group(1) not in _NOT_NAME:
        name = n.group(1)
    qth = ''
    q = re.search(r'\bQTH\s+(?:IS\s+|HR\s+)*([A-Z][A-Z0-9\-]*(?:\s+[A-Z][A-Z0-9\-]*)?)' + _STOP, t)
    if q:
        qw = q.group(1).split(' ')
        qth = qw[0] if len(qw) > 1 and (qw[1] in _NOT_NAME or qw[1] == qw[0]) else q.group(1)
    me = bool(my) and (calls_me_strict(t, my) or my in t.replace(' ', ''))
    over = bool(re.search(r'(\bK|\bKN|\bBK|\(|\+|<SK>|<AR>|\bSK|\bAR|\bTU)\s*$', t))
    if to == 'CQ' and not me:
        kind = 'cq'
    elif re.search(r'\b73\b|<SK>|\bSK\b', t):
        kind = 'final'
    elif rst:
        kind = 'report'
    elif call and (to or me):
        kind = 'answer'
    else:
        kind = 'other'
    # смысл по-русски
    parts = []
    if kind == 'cq':
        parts.append(tr('%s даёт общий вызов (CQ).') % (call or tr('Станция')))
    elif to and call:
        parts.append(tr('%s вызывает %s.') % (call, tr('вас') if me else to))
    elif call:
        parts.append(tr('Передаёт %s.') % call)
    if rst:
        parts.append(tr('Ваш рапорт %s.') % rst if me or to == my else tr('Рапорт %s.') % rst)
    if name:
        parts.append(tr('Оператор %s.') % name)
    if qth:
        parts.append('QTH %s.' % qth)
    if kind == 'final':
        parts.append(tr('Благодарит за связь, 73 — конец связи.'))
    gl = []
    for w in dict.fromkeys(words):
        if w in GLOSSARY and w not in ('DE', 'CQ') and len(gl) < 8:
            gl.append('%s — %s' % (w, tr(GLOSSARY[w])))
    if gl:
        parts.append('(' + '; '.join(gl) + ')')
    if over and kind != 'final':
        parts.append(tr('Ждёт ответа.'))
    good = sum(1 for x in (call, rst, name, qth) if x)
    return {'clean': t, 'ru': ' '.join(parts) or tr('Не удалось понять — слишком много ошибок приёма.'),
            'call': call, 'to': to, 'rst': rst, 'name': name, 'qth': qth, 'kind': kind,
            'calls_me': me, 'over': over, 'confidence': round(min(0.9, 0.3 + 0.15 * good), 2),
            'model': tr('правила (без ИИ)')}


def reply_rules(history, analysis, op):
    """Ответ по шаблону стандартной связи. -> {'text', 'ru', 'model'}; text пуст, если отвечать не нужно."""
    my = (op.get('my_call') or '').upper()
    a = analysis or {}
    call = a.get('call') or ''
    if not call:
        for h in reversed(history or []):
            if h.get('call'):
                call = h['call']
                break
    mine = [h for h in (history or []) if h.get('dir') == 'tx']
    sent_report = any(re.search(r'\bRST\b|\b5NN\b|\b599\b|\bUR [1-5]', h.get('text', '')) for h in mine)
    sent_73 = any(re.search(r'\b73\b|<SK>', h.get('text', '')) for h in mine)
    if not my:
        return {'text': '', 'ru': tr('Укажите свой позывной'), 'model': tr('шаблон')}
    if not call:
        return {'text': 'QRZ? DE %s K' % my, 'ru': tr('Кто меня зовёт? Позывной не разобран.'), 'model': tr('шаблон')}
    kind = a.get('kind')
    if kind == 'cq' and not mine:
        return {'text': '%s DE %s %s K' % (call, my, my), 'ru': tr('Отвечаю на CQ станции %s.') % call, 'model': tr('шаблон')}
    if sent_73:
        return {'text': '', 'ru': tr('Связь уже завершена (73 переданы).'), 'model': tr('шаблон')}
    if not sent_report:
        seg = ['%s DE %s' % (call, my), 'TNX FER CALL', 'UR RST 599 599']
        if op.get('my_name'):
            seg.append('NAME %s %s' % ((op['my_name'].upper(),) * 2))
        if op.get('my_qth'):
            seg.append('QTH %s %s' % ((op['my_qth'].upper(),) * 2))
        seg.append('HW? %s DE %s KN' % (call, my))
        return {'text': ' = '.join(seg[:1]) + ' ' + ' = '.join(seg[1:]),
                'ru': tr('Даю %s рапорт 599%s%s и спрашиваю, как слышно.') % (
                    call, tr(', имя') if op.get('my_name') else '', ', QTH' if op.get('my_qth') else ''),
                'model': tr('шаблон')}
    name = a.get('name') or next((h['name'] for h in reversed(history or []) if h.get('name')), '')
    nm = (' ' + name) if name else ''
    return {'text': 'R %s DE %s TNX FER QSO%s = 73 GL <SK> %s DE %s TU' % (call, my, nm, call, my),
            'ru': tr('Благодарю %s за связь и завершаю (73, SK).') % call, 'model': tr('шаблон')}
