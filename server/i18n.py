"""Shared English catalogue; source messages are the Russian fallback.

Only application-owned literals pass through tr(). Radio text, user data and
protocol keys must never be translated. Safe to use from decoder threads.
"""
import json
import os
import sys

LANGUAGES = ('ru', 'en')
_language = 'ru'
_root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(_root, 'web', 'locales', 'en.json'), encoding='utf-8') as _file:
    ENGLISH = json.load(_file)


def configure(language):
    global _language
    _language = language if language in LANGUAGES else 'ru'
    return _language


def language():
    return _language


def tr(message):
    if _language != 'en' or not isinstance(message, str):
        return message
    key = message.strip()
    translated = ENGLISH.get(key)
    if translated is None:
        return message
    start = len(message) - len(message.lstrip())
    return message[:start] + translated + message[start + len(key):]
