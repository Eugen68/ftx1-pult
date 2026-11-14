"""Run: python -m unittest discover -s tests -v (after installing requirements)."""
import asyncio
import json
import re
import string
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import ai
import i18n
import main


class Socket:
    def __init__(self):
        self.messages = []

    def send_json(self, message):
        self.messages.append(message)


def station():
    # Exercise the real language handler without audio hardware or background jobs.
    st = main.Station.__new__(main.Station)
    st.cfg = dict(main.DEFAULT_CFG)
    st.busy = False
    st.shutting_down = False
    st.rig = SimpleNamespace(state={})
    st.ptt_owner = None
    st.cw = SimpleNamespace(sender=SimpleNamespace(busy=False), proposal=None,
                            ai_busy=False, auto_on=lambda: False)
    st.digi = SimpleNamespace(tx_busy=False)
    st.vk = SimpleNamespace(task=None)
    st.ft = SimpleNamespace(busy=False, tx_enable=False)
    st.mem = SimpleNamespace(task=None)
    client = SimpleNamespace(ws=Socket(), local=True, authed=True, admin=False)
    st.clients = [client]
    return st, client


class LanguageTests(unittest.TestCase):
    def setUp(self):
        i18n.configure('ru')

    def tearDown(self):
        i18n.configure('ru')

    def test_fallback_whitespace_and_parameters(self):
        i18n.configure('en')
        self.assertEqual(i18n.tr('  Передача %s\n') % 'K1ABC', '  Transmitting K1ABC\n')
        self.assertEqual(i18n.tr('CQ DE UA3ABC K'), 'CQ DE UA3ABC K')
        self.assertEqual(i18n.configure('unsupported'), 'ru')
        self.assertEqual(i18n.tr('Передача'), 'Передача')

    def test_catalogue_keeps_format_specifiers_and_has_no_cyrillic(self):
        percent = r'%(?:\([^)]+\))?[-+0 #]*\d*(?:\.\d+)?[sdfgiouxXeEcr]'
        for source, translated in i18n.ENGLISH.items():
            with self.subTest(source=source):
                self.assertEqual(re.findall(percent, source), re.findall(percent, translated))
                self.assertIsNone(re.search('[А-Яа-яЁё]', translated))
        # Python format fields in model prompts must also survive translation.
        for prompt in (ai.SYS_ANALYZE, ai.USER_ANALYZE, ai.SYS_REPLY, ai.USER_REPLY):
            translated = i18n.ENGLISH[prompt.strip()]
            fields = lambda value: sorted(f for _, f, _, _ in string.Formatter().parse(value) if f)
            self.assertEqual(fields(prompt), fields(translated))

    def test_cw_rules_switch_language_without_changing_radio_text(self):
        raw = 'CQ CQ DE K1ABC K1ABC K'
        ru = ai.analyze_rules(raw, [], {'my_call': 'YO3ABC'})
        i18n.configure('en')
        en = ai.analyze_rules(raw, [], {'my_call': 'YO3ABC'})
        self.assertEqual(ru['clean'], en['clean'])
        self.assertEqual(ru['call'], en['call'])
        self.assertIn('calling CQ', en['ru'])
        self.assertNotEqual(ru['ru'], en['ru'])
        self.assertIn('thank you', ai.analyze_rules('K1ABC DE W1AW TNX RST 599 K', [], {})['ru'])
        reply = ai.reply_rules([], en, {'my_call': 'YO3ABC'})
        self.assertIn('YO3ABC', reply['text'])
        self.assertIsNone(re.search('[А-Яа-яЁё]', reply['ru']))

    def test_model_prompts_request_english_and_keep_wire_schema(self):
        class Model:
            def chat_json(self, system, user, schema, **kwargs):
                self.system, self.user, self.schema = system, user, schema
                return {'clean': 'CQ DE K1ABC K', 'ru': 'Calling CQ'}, 'mock'
        i18n.configure('en')
        model = Model()
        result = ai.analyze(model, 'CQ DE K1ABC K', [], {'my_call': 'YO3ABC'})
        self.assertIn('English', model.user)
        self.assertIn('"ru"', model.user)
        self.assertEqual(result['ru'], 'Calling CQ')
        self.assertIn('CQ DE K1ABC K', model.user)
        ai.reply(model, [], {}, {'my_call': 'YO3ABC'})
        self.assertIn('English', model.system)
        self.assertIn('YO3ABC', model.system)
        self.assertIn('ru', model.schema['properties'])

    def test_setting_persists_and_notifies_all_authenticated_clients(self):
        st, client = station()
        other = SimpleNamespace(ws=Socket(), authed=True, admin=True)
        outsider = SimpleNamespace(ws=Socket(), authed=False)
        st.clients.extend([other, outsider])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            with patch.object(main, 'cfg_path', return_value=str(path)):
                asyncio.run(st._on_msg(client, {'t': 'set_language', 'language': 'en'}))
                self.assertEqual(json.loads(path.read_text())['language'], 'en')
                self.assertEqual(main.load_cfg()['language'], 'en')
                asyncio.run(st._on_msg(client, {'t': 'set_language', 'language': 'ru'}))
                self.assertEqual(json.loads(path.read_text())['language'], 'ru')
        self.assertEqual([m['language'] for m in other.ws.messages], ['en', 'ru'])
        self.assertEqual(outsider.ws.messages, [])

    def test_unauthenticated_client_cannot_change_language(self):
        st, client = station()
        client.local = False
        client.authed = False
        with patch.object(main, 'save_cfg') as save:
            asyncio.run(st._on_msg(client, {'t': 'set_language', 'language': 'en'}))
        save.assert_not_called()
        self.assertEqual(i18n.language(), 'ru')

    def test_invalid_language_does_not_write(self):
        st, client = station()
        with patch.object(main, 'save_cfg') as save:
            st.set_language(client, 'fr')
        save.assert_not_called()
        self.assertEqual(client.ws.messages[-1]['t'], 'language_error')

    def test_save_failure_leaves_station_language_unchanged(self):
        st, client = station()
        with patch.object(main, 'save_cfg', side_effect=OSError('disk full')):
            st.set_language(client, 'en')
        self.assertEqual(i18n.language(), 'ru')
        self.assertEqual(st.cfg['language'], 'ru')
        self.assertEqual(client.ws.messages[-1]['t'], 'language_error')

    def test_transmission_automatic_jobs_and_memory_operations_block_reload(self):
        mutations = [lambda s: s.rig.state.update(tx=True), lambda s: s.rig.state.update(tuning=True),
                     lambda s: setattr(s, 'ptt_owner', object()), lambda s: setattr(s.cw.sender, 'busy', True),
                     lambda s: setattr(s.cw, 'proposal', {}), lambda s: setattr(s.cw, 'ai_busy', True),
                     lambda s: setattr(s.cw, 'auto_on', lambda: True), lambda s: setattr(s.digi, 'tx_busy', True),
                     lambda s: setattr(s.vk, 'task', object()), lambda s: setattr(s.ft, 'busy', True),
                     lambda s: setattr(s.ft, 'tx_enable', True), lambda s: setattr(s.mem, 'task', object())]
        for mutate in mutations:
            st, client = station()
            mutate(st)
            with patch.object(main, 'save_cfg') as save:
                st.set_language(client, 'en')
            self.assertEqual(client.ws.messages[-1]['t'], 'language_error')
            save.assert_not_called()

    def test_manifest_uses_selected_language(self):
        st, _ = station()
        i18n.configure('en')
        mime, data = st.manifest()
        manifest = json.loads(data)
        self.assertEqual(manifest['lang'], 'en')
        self.assertEqual(manifest['name'], 'FTX-1 Pult')
        self.assertEqual(mime, 'application/manifest+json')


if __name__ == '__main__':
    unittest.main()
