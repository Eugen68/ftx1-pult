"""
ИИ-помощник CW через LM Studio (локальный сервер, совместимый с OpenAI API: http://localhost:1234/v1).

Модель не слушает звук — его распознаёт декодер (cw.py). Модель получает принятый текст
с возможными ошибками и:
  * восстанавливает вероятный исходный текст по правилам CW-связи;
  * переводит на русский, расшифровывает сокращения и Q-коды;
  * вытаскивает позывной корреспондента, RST, имя, QTH; понимает, зовут ли нас;
  * составляет ответ в принятом в CW формате.
"""
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
        raise AiError("Модель ответила не в формате JSON: " + s[:200])
    txt = m.group(0)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        # частая ошибка моделей — висячие запятые
        try:
            return json.loads(re.sub(r',\s*([}\]])', r'\1', txt))
        except json.JSONDecodeError as e:
            raise AiError("Не удалось разобрать JSON модели: %s" % e)


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
            raise AiError("LM Studio ответил ошибкой %d: %s" % (e.code, msg))
        except urllib.error.URLError as e:
            raise AiError("LM Studio недоступен по адресу %s (%s). Запустите LM Studio и включите "
                          "сервер: вкладка Developer → Start Server." % (self.url, e.reason))
        except TimeoutError:
            raise AiError("LM Studio не ответил за %d с — модель слишком большая или занята" % (timeout or self.timeout))

    def models(self):
        r = self._req('/models', timeout=5)
        return [m.get('id') for m in r.get('data', []) if m.get('id') and 'embed' not in m.get('id', '')]

    def chat_json(self, system, user, schema=None, temperature=0.2, max_tokens=700):
        model = self.model
        if not model:
            ms = self.models()
            if not ms:
                raise AiError("В LM Studio не загружена ни одна модель")
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
            raise AiError("Неожиданный ответ LM Studio: %s" % str(r)[:200])
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
        who = 'Я' if h['dir'] == 'tx' else 'ОН'
        lines.append('%s: %s' % (who, h.get('clean') or h['text']))
    return '\n'.join(lines) or '(начало связи)'


def analyze(lm, raw, history, op):
    my = (op.get('my_call') or 'MYCALL').upper()
    sys_p = SYS_ANALYZE.format(my_call=my)
    user = USER_ANALYZE.format(my_call=my, history=fmt_history(history), raw=raw.strip())
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
    sys_p = SYS_REPLY.format(my_call=my, my_name=op.get('my_name') or '—', my_qth=op.get('my_qth') or '—',
                             my_extra=op.get('my_extra') or '—')
    if not task:
        task = ('Составь мой следующий ответ корреспонденту по ходу связи: если он позвал меня — дай RST, '
                'имя и QTH; если дал свои данные — поблагодари и заверши связь (73 <SK>); если он дал CQ — '
                'ответь на вызов коротко (его позывной DE мой позывной K).')
    user = USER_REPLY.format(history=fmt_history(history), task=task,
                             analysis=json.dumps({k: analysis.get(k) for k in ('clean', 'call', 'rst', 'name', 'qth', 'kind')},
                                                 ensure_ascii=False) if analysis else '(нет)')
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
