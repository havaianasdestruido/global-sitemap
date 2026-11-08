import io
import json
import os
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from scripts import generate_sitemap as generator

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

SITE = "https://pato.is-a.dev/"
GITHUB_IO = "havaianasdestruido.github.io"


def parse(path):
    return ET.parse(path).getroot()


def sitemap_locs(path):
    return [u.find(NS + 'loc').text for u in parse(path).findall(NS + 'url')]


class FakeGitHubPages:
    """Stand-in for generator.fetch, modelled on the real site.

    The user site has the custom domain pato.is-a.dev, so GitHub redirects
    every https://havaianasdestruido.github.io/<path> to
    https://pato.is-a.dev/<path>, while the pages themselves still link to
    the github.io address (like the real homepage and gh-pages/URL.html).
    """

    def __init__(self, pages=None, offsite=None):
        self.pages = pages if pages is not None else {
            '/': '<a href="https://github.com/havaianasdestruido">GitHub</a>'
                 '<a href="https://havaianasdestruido.github.io/gh-pages/URL.html">more</a>',
            '/gh-pages/URL.html':
                '<a href="https://havaianasdestruido.github.io/CNLeaks/">CNLeaks</a>'
                '<a href="https://havaianasdestruido.github.io/">home</a>'
                '<a href="https://audio-taste.vercel.app/">external</a>',
            '/CNLeaks/': '<a href="https://pato.is-a.dev/CNLeaks/TREE.html">tree</a>',
            '/CNLeaks/TREE.html': '<p>tree</p>',
            '/WASMNux/': '<a href="docs/">docs</a>',
            '/WASMNux/docs/': '<p>docs</p>',
        }
        # Paths on the site that redirect to another site entirely.
        self.offsite = offsite or {}
        self.requests = []

    def __call__(self, url, timeout, retries=2):
        self.requests.append(url)
        parts = urllib.parse.urlsplit(url)
        if parts.hostname == GITHUB_IO:  # 301 to the custom domain
            url = urllib.parse.urlunsplit(('https', 'pato.is-a.dev', parts.path, parts.query, ''))
            parts = urllib.parse.urlsplit(url)
        if parts.hostname == 'pato.is-a.dev' and parts.path in self.offsite:
            url = self.offsite[parts.path]
            return 200, url, 'text/html', b'<a href="/elsewhere/">x</a>', None
        if parts.hostname == 'pato.is-a.dev' and parts.path in self.pages:
            body = self.pages[parts.path].encode('utf-8')
            return 200, url, 'text/html; charset=utf-8', body, 'Sun, 08 Nov 2026 13:00:00 GMT'
        return 404, url, '', b'', None


def crawl(fake, seeds, alias_hosts=()):
    with patch.object(generator, 'fetch', fake):
        return generator.crawl(seeds, SITE, max_depth=8, max_urls=1000, delay=0, timeout=1,
                               keep_query=False, robots_rules={}, alias_hosts=alias_hosts)


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


class CustomDomainAliasTests(unittest.TestCase):
    """Regression: with --site-url on the custom domain, every page reached
    through a havaianasdestruido.github.io link or seed was dropped as
    "another host", and the sitemap shrank to the homepage alone."""

    ALL_PAGES = [
        'https://pato.is-a.dev/',
        'https://pato.is-a.dev/CNLeaks/',
        'https://pato.is-a.dev/CNLeaks/TREE.html',
        'https://pato.is-a.dev/WASMNux/',
        'https://pato.is-a.dev/WASMNux/docs/',
        'https://pato.is-a.dev/gh-pages/URL.html',
    ]
    SEEDS = [SITE, f'https://{GITHUB_IO}/CNLeaks/', f'https://{GITHUB_IO}/WASMNux/']

    def test_parse_host(self):
        self.assertEqual(generator.parse_host('Havaianasdestruido.GitHub.io'), GITHUB_IO)
        self.assertEqual(generator.parse_host(f'https://{GITHUB_IO}/x/'), GITHUB_IO)
        self.assertEqual(generator.parse_host(' localhost:8000 '), 'localhost')
        self.assertEqual(generator.parse_host(''), '')

    def test_to_site_url(self):
        aliases = {GITHUB_IO}
        self.assertEqual(generator.to_site_url(SITE + 'a/', SITE, aliases), SITE + 'a/')
        self.assertEqual(
            generator.to_site_url(f'https://{GITHUB_IO}/CNLeaks/TREE.html?x=1#top', SITE, aliases),
            'https://pato.is-a.dev/CNLeaks/TREE.html?x=1')
        self.assertIsNone(generator.to_site_url('https://audio-taste.vercel.app/', SITE, aliases))
        # Without the alias the github.io address is just another site.
        self.assertIsNone(generator.to_site_url(f'https://{GITHUB_IO}/CNLeaks/', SITE))
        # The site's scheme and port are kept (local dev servers).
        self.assertEqual(
            generator.to_site_url('https://example.org/p/', 'http://localhost:8000/', {'example.org'}),
            'http://localhost:8000/p/')

    def test_detect_alias_hosts_uses_the_redirect(self):
        fake = FakeGitHubPages()
        with patch.object(generator, 'fetch', fake):
            self.assertEqual(generator.detect_alias_hosts(SITE, [GITHUB_IO], 1), [GITHUB_IO])
            # A host that doesn't redirect to the site is not an alias...
            self.assertEqual(generator.detect_alias_hosts(SITE, ['example.org'], 1), [])
            # ...and the site itself is never probed.
            fake.requests.clear()
            self.assertEqual(generator.detect_alias_hosts(SITE, ['pato.is-a.dev'], 1), [])
            self.assertEqual(fake.requests, [])

    def test_detect_alias_hosts_tolerates_network_failure(self):
        def offline(url, timeout, retries=2):
            return None, url, '', b'', None
        with patch.object(generator, 'fetch', offline):
            self.assertEqual(generator.detect_alias_hosts(SITE, [GITHUB_IO], 1), [])

    def test_crawl_without_alias_reproduces_the_one_url_sitemap(self):
        self.assertEqual(sorted(crawl(FakeGitHubPages(), self.SEEDS)), [SITE])

    def test_crawl_follows_alias_links_and_seeds(self):
        fake = FakeGitHubPages()
        pages = crawl(fake, self.SEEDS, alias_hosts={GITHUB_IO})
        self.assertEqual(sorted(pages), self.ALL_PAGES)
        # Alias URLs are rewritten before fetching: no redirect round trips,
        # and each page is requested exactly once.
        self.assertTrue(all(url.startswith(SITE) for url in fake.requests), fake.requests)
        self.assertEqual(len(fake.requests), len(set(fake.requests)))
        self.assertEqual(pages[SITE], '2026-11-08')

    def test_crawl_never_lists_pages_that_redirect_off_site(self):
        fake = FakeGitHubPages(offsite={'/WASMNux/': 'https://wasmnux.example/'})
        pages = crawl(fake, self.SEEDS, alias_hosts={GITHUB_IO})
        self.assertNotIn('https://wasmnux.example/', pages)
        self.assertNotIn('https://pato.is-a.dev/WASMNux/', pages)
        self.assertEqual({generator.url_host(url) for url in pages}, {'pato.is-a.dev'})

    def test_helper_seeds_map_alias_entries(self):
        with patch.object(generator, 'fetch', FakeGitHubPages()):
            seeds = generator.helper_seeds(SITE + 'gh-pages/URL.html', SITE, 1, {GITHUB_IO})
        self.assertEqual(seeds, ['https://pato.is-a.dev/', 'https://pato.is-a.dev/CNLeaks/'])

    def test_github_pages_seeds_keeps_homepages_on_the_site(self):
        repos = [
            {'name': 'CNLeaks', 'has_pages': True},
            {'name': GITHUB_IO, 'has_pages': True},
            {'name': 'BASICGOL', 'has_pages': False, 'homepage': 'https://pato.is-a.dev/BASICGOL/'},
            {'name': 'AudioTaste', 'has_pages': False, 'homepage': 'https://audio-taste.vercel.app'},
            {'name': 'old', 'has_pages': True, 'archived': True},
        ]
        responses = [io.BytesIO(json.dumps(repos).encode()), io.BytesIO(b'[]')]
        with patch.object(generator.urllib.request, 'urlopen', lambda *a, **k: responses.pop(0)):
            seeds = generator.github_pages_seeds('havaianasdestruido', None, {'pato.is-a.dev'})
        self.assertEqual(seeds, ['https://havaianasdestruido.github.io/',
                                 'https://havaianasdestruido.github.io/CNLeaks/',
                                 'https://pato.is-a.dev/BASICGOL/'])

    def run_cli(self, *extra_args, github_seeds=()):
        """Run main() against FakeGitHubPages; return (exit code, locs, fake)."""
        fake = FakeGitHubPages()
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                argv = ['generate_sitemap.py', '--no-helper', '--ignore-robots', '--delay', '0',
                        '--site-url', SITE, *extra_args]
                with patch('sys.argv', argv), patch.object(generator, 'fetch', fake), \
                     patch.object(generator, 'github_pages_seeds', return_value=list(github_seeds)):
                    code = generator.main()
                locs = sitemap_locs(Path('sitemap.xml')) if Path('sitemap.xml').exists() else []
                return code, locs, fake
            finally:
                os.chdir(previous)

    def test_cli_detects_github_io_alias_end_to_end(self):
        code, locs, fake = self.run_cli(
            github_seeds=[f'https://{GITHUB_IO}/', f'https://{GITHUB_IO}/CNLeaks/',
                          f'https://{GITHUB_IO}/WASMNux/'])
        self.assertEqual(code, 0)
        self.assertIn(f'https://{GITHUB_IO}/', fake.requests)  # the alias probe
        self.assertEqual(locs, self.ALL_PAGES)

    def test_cli_explicit_alias_host_needs_no_probe(self):
        code, locs, fake = self.run_cli('--no-github', '--alias-host', GITHUB_IO,
                                        '--seed', f'https://{GITHUB_IO}/WASMNux/')
        self.assertEqual(code, 0)
        self.assertNotIn(f'https://{GITHUB_IO}/', fake.requests)
        self.assertEqual(locs, self.ALL_PAGES)

    def test_cli_rejects_invalid_alias_host(self):
        code, _locs, _fake = self.run_cli('--no-github', '--alias-host', 'https://')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
