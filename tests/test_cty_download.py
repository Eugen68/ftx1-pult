import io
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / '.github/scripts/fetch_cty.py'))
fetch_cty = MODULE['fetch_cty']


def valid_database():
    lines = ['Russia: 16: 29: EU: 55.0: -37.0: -3.0: R:\n    R;']
    for number in range(300):
        lines.append('Entity %d: 1: 1: EU: 1.0: 1.0: 0.0: Q%d:\n    Q%d;' % (number, number, number))
    return '\n'.join(lines).encode('ascii')


class CountryDownloadTests(unittest.TestCase):
    def test_html_response_uses_validated_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'cty.dat'
            with patch('urllib.request.urlopen', side_effect=[io.BytesIO(b'<html>error</html>'), io.BytesIO(valid_database())]):
                source = fetch_cty(destination, ['https://example.org/primary', 'https://example.org/fallback'])
            self.assertEqual(source, 'https://example.org/fallback')
            self.assertEqual(destination.read_bytes(), valid_database())

    def test_invalid_download_does_not_replace_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'cty.dat'
            destination.write_bytes(b'existing database')
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'not a country file')):
                with self.assertRaises(RuntimeError):
                    fetch_cty(destination, ['https://example.org/primary'])
            self.assertEqual(destination.read_bytes(), b'existing database')
