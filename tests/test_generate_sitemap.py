import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import generate_sitemap as generator


class TextSitemapTests(unittest.TestCase):
    def test_plain_text_urls_without_xml_escaping_or_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.txt'
            entries = [('https://example.com/', '2026-11-06'),
                       ('https://example.com/?a=1&b=2', None)]
            self.assertEqual(generator.write_sitemap(entries, output), [output])
            self.assertEqual(output.read_bytes(),
                             b'https://example.com/\nhttps://example.com/?a=1&b=2\n')
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_limits_preserve_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.txt'
            output.write_text('previous\n')
            for limit in ('SITEMAP_URL_LIMIT', 'SITEMAP_BYTE_LIMIT'):
                with self.subTest(limit=limit), patch.object(generator, limit, 0):
                    with self.assertRaises(ValueError):
                        generator.write_sitemap([('https://example.com/', None)], output)
                    self.assertEqual(output.read_text(), 'previous\n')

    def test_cli_defaults_and_robots_public_url(self):
        with tempfile.TemporaryDirectory() as directory:
            import os
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with patch('sys.argv', ['generate_sitemap.py', '--no-helper', '--no-github',
                                        '--ignore-robots', '--robots-out', 'robots.txt']), \
                     patch.object(generator, 'crawl', return_value={generator.DEFAULT_SITE_URL: None}):
                    self.assertEqual(generator.main(), 0)
                self.assertEqual(Path('sitemap.txt').read_text(), generator.DEFAULT_SITE_URL + '\n')
                self.assertIn('Sitemap: ' + generator.DEFAULT_SITE_URL + 'sitemap.txt\n',
                              Path('robots.txt').read_text())
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
