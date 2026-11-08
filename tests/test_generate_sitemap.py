import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from scripts import generate_sitemap as generator

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def parse(path):
    return ET.parse(path).getroot()


class XmlSitemapTests(unittest.TestCase):
    def test_urlset_with_namespace_and_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.xml'
            entries = [('https://example.com/', '2026-11-06'),
                       ('https://example.com/?a=1&b=2', None)]
            self.assertEqual(generator.write_sitemap(entries, output), [output])
            text = output.read_text(encoding='utf-8')
            self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))
            self.assertIn('xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"', text)
            # Ampersands must be entity-escaped, never raw.
            self.assertIn('https://example.com/?a=1&amp;b=2', text)
            self.assertNotIn('?a=1&b=2', text)
            self.assertTrue(text.endswith('</urlset>\n'))
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['sitemap.xml'])

            root = parse(output)
            self.assertEqual(root.tag, NS + 'urlset')
            urls = root.findall(NS + 'url')
            self.assertEqual([u.find(NS + 'loc').text for u in urls],
                             ['https://example.com/', 'https://example.com/?a=1&b=2'])
            self.assertEqual(urls[0].find(NS + 'lastmod').text, '2026-11-06')
            self.assertIsNone(urls[1].find(NS + 'lastmod'))

    def test_special_characters_are_escaped_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.xml'
            tricky = "https://example.com/a?x=1&y='2'&z=\"3\"<>"
            generator.write_sitemap([(tricky, None)], output)
            raw = output.read_text(encoding='utf-8')
            self.assertNotIn('&y=', raw)
            self.assertIn('&amp;', raw)
            loc = parse(output).find(NS + 'url').find(NS + 'loc').text
            self.assertEqual(loc, tricky)

    def test_entries_are_sorted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.xml'
            generator.write_sitemap(
                [('https://example.com/b', None), ('https://example.com/a', None),
                 ('https://example.com/b', None)], output)
            locs = [u.find(NS + 'loc').text for u in parse(output).findall(NS + 'url')]
            self.assertEqual(locs, ['https://example.com/a', 'https://example.com/b'])

    def test_each_host_gets_its_own_file(self):
        entries = [('https://a.example/x', None), ('https://b.example/y', None)]
        # The protocol forbids mixing hosts in one file, so two hosts mean
        # an index over two single-host parts even when the size is fine.
        self.assertEqual([len(c) for c in generator.split_entries(
            entries, 50_000, 50 * 1024 * 1024)], [1, 1])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.xml'
            written = generator.write_sitemap(
                entries, output, 'https://example.com/sitemap.xml')
            self.assertEqual([p.name for p in written], ['sitemap.xml',
                                                         'sitemap-1.xml', 'sitemap-2.xml'])
            self.assertEqual(parse(output).tag, NS + 'sitemapindex')
            hosts = [generator.url_host(parse(part).find(NS + 'url').find(NS + 'loc').text)
                     for part in written[1:]]
            self.assertEqual(hosts, ['a.example', 'b.example'])

    def test_oversized_output_becomes_sitemap_index(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.xml'
            entries = [(f'https://example.com/{i}', None) for i in range(5)]
            with patch.object(generator, 'SITEMAP_URL_LIMIT', 2):
                written = generator.write_sitemap(
                    entries, output, 'https://example.com/sitemap.xml')
            self.assertEqual([p.name for p in written],
                             ['sitemap.xml', 'sitemap-1.xml', 'sitemap-2.xml', 'sitemap-3.xml'])
            index = parse(output)
            self.assertEqual(index.tag, NS + 'sitemapindex')
            locs = [s.find(NS + 'loc').text for s in index.findall(NS + 'sitemap')]
            self.assertEqual(locs, ['https://example.com/sitemap-1.xml',
                                    'https://example.com/sitemap-2.xml',
                                    'https://example.com/sitemap-3.xml'])
            total = 0
            for part in sorted(Path(directory).glob('sitemap-*.xml')):
                root = parse(part)
                self.assertEqual(root.tag, NS + 'urlset')
                self.assertLessEqual(len(root.findall(NS + 'url')), 2)
                total += len(root.findall(NS + 'url'))
            self.assertEqual(total, 5)

    def test_limits_preserve_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sitemap.xml'
            output.write_text('previous\n')
            with patch.object(generator, 'SITEMAP_BYTE_LIMIT', 1):
                with self.assertRaises(ValueError):
                    generator.write_sitemap([('https://example.com/', None)], output)
            self.assertEqual(output.read_text(), 'previous\n')
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['sitemap.xml'])

    def test_cli_defaults_and_robots_public_url(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with patch('sys.argv', ['generate_sitemap.py', '--no-helper', '--no-github',
                                        '--ignore-robots', '--robots-out', 'robots.txt']), \
                     patch.object(generator, 'crawl', return_value={generator.DEFAULT_SITE_URL: None}):
                    self.assertEqual(generator.main(), 0)
                root = parse(Path('sitemap.xml'))
                self.assertEqual(root.tag, NS + 'urlset')
                self.assertEqual(root.find(NS + 'url').find(NS + 'loc').text,
                                 generator.DEFAULT_SITE_URL)
                self.assertIn('Sitemap: ' + generator.DEFAULT_SITE_URL + 'sitemap.xml\n',
                              Path('robots.txt').read_text())
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
