"""Fetch and validate DXCC data before bundling it into an executable."""
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server'))
from dxcc import Cty

SOURCES = (
    'https://www.country-files.com/cty/cty.dat',
    'https://raw.githubusercontent.com/WSJTX/wsjtx/master/cty.dat',
)


def fetch_cty(destination, sources=SOURCES):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    for url in sources:
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'FTX1-Pult'})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(4_000_001)
            if len(data) > 4_000_000 or b'<html' in data[:1024].lower():
                raise ValueError('Response is not a country data file')
            # Parse a temporary candidate, preserving any existing good database.
            with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
                candidate = Path(directory) / 'cty.dat'
                candidate.write_bytes(data)
                countries = Cty()
                countries.load(str(candidate))
                example = countries.lookup('R6AAF')
                if countries.count <= 300 or not example or 'Russia' not in example['name']:
                    raise ValueError('Country data failed DXCC validation')
                os.replace(candidate, destination)
            print('Validated %d entities from %s' % (countries.count, url))
            return url
        except Exception as error:
            failures.append('%s: %s' % (url, error))
            message = 'Country data source unavailable: %s' % failures[-1]
            print(message.encode('ascii', 'backslashreplace').decode('ascii'))
    raise RuntimeError('No valid DXCC database could be downloaded: ' + '; '.join(failures))


if __name__ == '__main__':
    fetch_cty(ROOT / 'server' / 'cty.dat')
