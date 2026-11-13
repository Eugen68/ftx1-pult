"""
FTX-1 Пульт — запуск двойным кликом (без консоли).
При первом запуске сам установит нужные библиотеки Python.
"""
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'server'))
from i18n import tr, configure
try:
    import json
    with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as file:
        configure(json.load(file).get('language'))
except (OSError, ValueError, AttributeError):
    pass
NEEDED = {'numpy': 'numpy', 'serial': 'pyserial', 'sounddevice': 'sounddevice',
          'cryptography': 'cryptography', 'pystray': 'pystray', 'PIL': 'pillow'}


def box(text, flags=0x40):
    if sys.platform == 'win32':
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, text, tr('FTX-1 Пульт'), flags)
    print(text)
    return 1


missing = [pkg for mod, pkg in NEEDED.items() if importlib.util.find_spec(mod) is None]
if missing:
    if box(tr("Первый запуск: нужно установить библиотеки Python (%s).\n\n"
           "Нужен интернет, займёт 1–2 минуты. Окно программы откроется само.\n\nУстановить?")
           % ', '.join(missing), 0x41) != 1:                                   # OK / Отмена
        sys.exit(0)
    py = sys.executable
    if py.lower().endswith('pythonw.exe'):
        py = py[:-11] + 'python.exe'
    r = subprocess.run([py, '-m', 'pip', 'install', '--disable-pip-version-check'] + missing,
                       capture_output=True, text=True, creationflags=0x08000000 if sys.platform == 'win32' else 0)
    if r.returncode != 0:
        box(tr("Не удалось установить библиотеки:\n\n") + (r.stderr or r.stdout)[-1500:], 0x10)
        sys.exit(1)

sys.path.insert(0, os.path.join(HERE, 'server'))
import main  # noqa: E402
main.main()
