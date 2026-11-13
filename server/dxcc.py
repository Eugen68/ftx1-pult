"""
Страны DXCC по позывному и база проведённых связей (что уже сработано).

Страны — по файлу cty.dat (Country Files, AD1C, www.country-files.com — тот же файл, что в WSJT-X,
JTDX, MSHV, N1MM). Где берём файл (самый свежий из найденных):
  * cty.dat рядом с программой — его программа сама обновляет с country-files.com раз в месяц;
  * cty.dat, вшитый в программу при сборке;
  * cty.dat из установленных JTDX / WSJT-X / MSHV.

Сработанное — из своего лога FT8 (ft8_log.adi) и из ADIF-файлов, загруженных пользователем
(экспорт из HAMLOG или другого журнала).
"""

from i18n import tr
import glob
import json
import logging
import math
import os
import re
import threading
import time
import urllib.request

log = logging.getLogger("dxcc")

CTY_URL = 'https://www.country-files.com/cty/cty.dat'


# ---------------------------------------------------------------------- локатор и расстояние
def grid_latlon(g):
    """Центр квадрата Maidenhead (4 или 6 знаков) -> (широта, долгота) или None."""
    g = (g or '').strip().upper()
    if not re.fullmatch(r'[A-R]{2}\d{2}([A-X]{2})?', g):
        return None
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) == 6:
        lon += (ord(g[4]) - 65) * (2 / 24) + 1 / 24
        lat += (ord(g[5]) - 65) * (1 / 24) + 1 / 48
    else:
        lon += 1
        lat += 0.5
    return lat, lon


def distance_km(a, b):
    if not a or not b:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def band_of(hz):
    mhz = (hz or 0) / 1e6
    for lo, hi, b in ((1.8, 2.0, '160m'), (3.5, 4.0, '80m'), (5.3, 5.5, '60m'), (7.0, 7.3, '40m'),
                      (10.1, 10.15, '30m'), (14.0, 14.35, '20m'), (18.068, 18.168, '17m'), (21.0, 21.45, '15m'),
                      (24.89, 24.99, '12m'), (28.0, 29.7, '10m'), (50, 54, '6m'), (70, 71, '4m'),
                      (144, 148, '2m'), (430, 440, '70cm')):
        if lo <= mhz <= hi:
            return b
    return ''


# ---------------------------------------------------------------------- cty.dat
class Cty:
    def __init__(self):
        self.prefix = {}         # префикс -> страна
        self.exact = {}          # точный позывной -> страна
        self.maxlen = 0
        self.path = ''
        self.date = ''
        self.count = 0

    def load(self, path):
        with open(path, encoding='latin-1') as f:
            text = f.read()
        prefix, exact, count = {}, {}, 0
        for m in re.finditer(r'([^\n:]+):\s*(\d+):\s*(\d+):\s*(\w+):\s*([-\d.]+):\s*([-\d.]+):\s*([-\d.]+):\s*(\S+):'
                             r'\s*([^;]*);', text):
            name, cq, itu, cont, lat, lon, _tz, pp, body = m.groups()
            ent = {'name': name.strip(), 'cq': int(cq), 'itu': int(itu), 'cont': cont, 'pfx': pp.lstrip('*'),
                   'lat': float(lat), 'lon': -float(lon), 'wae': pp.startswith('*')}
            count += 1
            for tok in body.replace('\n', ' ').split(','):
                tok = tok.strip()
                if not tok:
                    continue
                e = ent
                mm = re.search(r'\((\d+)\)', tok)
                cm = re.search(r'\{(\w+)\}', tok)
                if mm or cm:
                    e = dict(ent)
                    if mm:
                        e['cq'] = int(mm.group(1))
                    if cm:
                        e['cont'] = cm.group(1)
                key = re.sub(r'[\(\[<\{~].*$', '', tok)
                if key.startswith('='):
                    exact[key[1:]] = e
                elif key:
                    prefix[key] = e
        if count < 50:
            raise ValueError(tr('мало стран (%d) — файл не похож на cty.dat') % count)
        self.prefix, self.exact, self.count, self.path = prefix, exact, count, path
        self.maxlen = max(len(k) for k in prefix)
        self.date = time.strftime('%Y-%m-%d', time.localtime(os.path.getmtime(path)))
        vm = re.search(r'VER(\d{8})', text)
        if vm:
            self.date = '%s-%s-%s' % (vm.group(1)[:4], vm.group(1)[4:6], vm.group(1)[6:])
        log.info(tr("cty.dat: %d стран, %s (%s)"), count, self.date, path)

    def _by_prefix(self, s):
        for n in range(min(len(s), self.maxlen), 0, -1):
            e = self.prefix.get(s[:n])
            if e:
                return e
        return None

    def lookup(self, call):
        """Страна по позывному: {'name', 'cont', 'cq', 'pfx', ...} или None."""
        if not self.prefix or not call:
            return None
        c = call.upper().strip().strip('<>')
        if c in self.exact:
            return self.exact[c]
        if '/' in c:
            parts = [p for p in c.split('/') if p]
            if not parts:
                return None
            if parts[-1] in ('MM', 'AM'):
                return None                                   # морская / воздушная мобильная — без страны
            digit = next((p for p in parts[1:] if len(p) == 1 and p.isdigit()), None)
            parts = [p for p in parts if p not in ('P', 'M', 'QRP', 'A', 'B', 'LH', 'J', 'R', 'T') and not p.isdigit()]
            if digit and len(parts) == 1:                      # UA6ABC/9 -> район 9: UA9ABC
                parts = [re.sub(r'\d', digit, parts[0], count=1)]
            if not parts:
                return None
            if len(parts) == 1:
                c = parts[0]
            else:
                a, b = parts[0], parts[1]
                if a in self.exact:
                    return self.exact[a]
                c = a if len(a) <= len(b) else b               # EA8/R6AAF или R6AAF/EA8 -> EA8
            if c in self.exact:
                return self.exact[c]
        return self._by_prefix(c)


def find_cty(base, res):
    """Самый свежий cty.dat из доступных."""
    cand = [os.path.join(base, 'cty.dat'), os.path.join(res, 'cty.dat')]
    if os.name == 'nt':
        la = os.environ.get('LOCALAPPDATA', '')
        for pat in (r'C:\JTDX*\bin\cty.dat', r'C:\Program Files*\JTDX*\bin\cty.dat',
                    r'C:\WSJT*\wsjtx\bin\cty.dat', r'C:\Program Files*\WSJT*\bin\cty.dat',
                    os.path.join(la, 'WSJT-X', 'cty.dat'), os.path.join(la, 'JTDX', 'cty.dat'),
                    r'C:\MSHV*\settings\database\cty.dat', r'C:\Program Files*\MSHV*\settings\database\cty.dat'):
            cand += glob.glob(pat)
    found = [p for p in cand if os.path.isfile(p) and os.path.getsize(p) > 3000]
    return max(found, key=os.path.getmtime) if found else None


def update_cty(base, on_done=None):
    """Скачать свежий cty.dat (в фоне), если своего нет или он старше 30 дней."""
    path = os.path.join(base, 'cty.dat')
    if os.path.isfile(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
        return

    def run():
        try:
            req = urllib.request.Request(CTY_URL, headers={'User-Agent': 'FTX1-Pult'})
            data = urllib.request.urlopen(req, timeout=25).read()
            if len(data) < 20000 or b'Russia' not in data:
                raise ValueError(tr('ответ не похож на cty.dat'))
            tmp = path + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
            log.info(tr("cty.dat обновлён с country-files.com (%d байт)"), len(data))
            if on_done:
                on_done(path)
        except Exception as e:
            log.info(tr("cty.dat не обновился: %s"), e)
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------- что сработано
def parse_adif(text):
    """ADIF -> список {'call', 'band', 'mode', 'grid', 'freq'}."""
    out = []
    body = re.split(r'<eoh>', text, flags=re.I)[-1]
    for rec in re.split(r'<eor>', body, flags=re.I):
        f = {}
        pos = 0
        for m in re.finditer(r'<(\w+):(\d+)(?::\w)?>', rec):
            if m.start() < pos:
                continue
            n = int(m.group(2))
            f[m.group(1).lower()] = rec[m.end():m.end() + n]
            pos = m.end() + n
        call = f.get('call', '').strip().upper()
        if not call:
            continue
        band = f.get('band', '').strip().lower()
        if not band and f.get('freq'):
            try:
                band = band_of(float(f['freq']) * 1e6)
            except ValueError:
                band = ''
        out.append({'call': call, 'band': band, 'mode': (f.get('submode') or f.get('mode') or '').upper(),
                    'grid': f.get('gridsquare', '')[:4].upper(), 'dxcc': f.get('country', '')})
    return out


class Worked:
    """Сработанные позывные, страны и квадраты — всего и по диапазонам."""

    def __init__(self, base, cty):
        self.base = base
        self.cty = cty
        self.store = os.path.join(base, 'worked_import.json')     # загруженные логи (ADIF из HAMLOG и т. п.)
        self.ft8_log = os.path.join(base, 'ft8_log.adi')
        self.imported = []
        self.lock = threading.Lock()
        try:
            with open(self.store, encoding='utf-8') as f:
                self.imported = json.load(f)
        except (OSError, ValueError):
            pass
        self.rebuild()

    def rebuild(self):
        recs = list(self.imported)
        try:
            with open(self.ft8_log, encoding='utf-8') as f:
                recs += parse_adif(f.read())
        except OSError:
            pass
        calls, cb, dx, dxb, grids = set(), set(), set(), set(), set()
        for r in recs:
            c = r['call']
            calls.add(c)
            cb.add((c, r['band']))
            e = self.cty.lookup(c)
            if e:
                dx.add(e['name'])
                dxb.add((e['name'], r['band']))
            if r.get('grid'):
                grids.add(r['grid'])
        with self.lock:
            self.calls, self.call_band, self.dxcc, self.dxcc_band, self.grids = calls, cb, dx, dxb, grids
            self.n = len(recs)

    def import_adif(self, text, replace=False):
        return self.import_recs(parse_adif(text), replace)

    def import_recs(self, recs, replace=False):
        """recs: [{'call', 'band', 'mode', 'grid'}] -> (всего, новых)."""
        recs = [{'call': str(r.get('call', '')).upper().strip()[:15], 'band': str(r.get('band', '')).lower()[:6],
                 'mode': str(r.get('mode', '')).upper()[:10], 'grid': str(r.get('grid', '')).upper()[:4]}
                for r in recs if r.get('call')]
        if replace:
            self.imported = []
        seen = {(r['call'], r['band'], r['mode']) for r in self.imported}
        new = [r for r in recs if (r['call'], r['band'], r['mode']) not in seen]
        self.imported += [{'call': r['call'], 'band': r['band'], 'mode': r['mode'], 'grid': r['grid']} for r in new]
        tmp = self.store + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.imported, f)
        os.replace(tmp, self.store)
        self.rebuild()
        return len(recs), len(new)

    def clear_import(self):
        self.imported = []
        try:
            os.remove(self.store)
        except OSError:
            pass
        self.rebuild()

    def add(self, call, band, grid=''):
        with self.lock:
            self.calls.add(call)
            self.call_band.add((call, band))
            e = self.cty.lookup(call)
            if e:
                self.dxcc.add(e['name'])
                self.dxcc_band.add((e['name'], band))
            if grid:
                self.grids.add(grid[:4])
            self.n += 1

    def status(self, call, band, grid=''):
        """-> (теги, страна): 'newdxcc' — страна ни разу, 'newband' — страна не на этом диапазоне,
        'newcall' — с позывным связи не было, 'worked' — уже работали на этом диапазоне; 'newgrid' — новый квадрат."""
        e = self.cty.lookup(call)
        tags = []
        with self.lock:
            if e and e['name'] not in self.dxcc:
                tags.append('newdxcc')
            elif e and (e['name'], band) not in self.dxcc_band:
                tags.append('newband')
            if (call, band) in self.call_band:
                tags.append('worked')
            elif call not in self.calls:
                tags.append('newcall')
            if grid and grid[:4] not in self.grids and re.fullmatch(r'[A-R]{2}\d\d', grid[:4]) and grid != 'RR73':
                tags.append('newgrid')
        return tags, e

    def stats(self):
        with self.lock:
            return {'qso': self.n, 'calls': len(self.calls), 'dxcc': len(self.dxcc), 'grids': len(self.grids),
                    'imported': len(self.imported)}
