# global-sitemap

A **token-less** GitHub Actions workflow + Python script that builds an XML
sitemap with **every URL** of [havaianasdestruido.github.io](https://havaianasdestruido.github.io/) —
including all subpages and every GitHub Pages project site under
`https://havaianasdestruido.github.io/<repo>/`.

## Token-less by design

- **No PAT, no repository secrets, nothing to configure.** The script reads
  only public data, and the commit step uses the `GITHUB_TOKEN` that GitHub
  Actions provides automatically (nothing is stored or entered by hand).
- **No dependencies.** Python 3.9+ stdlib only — the workflow doesn't even
  run `pip install`.
- If a `GITHUB_TOKEN` is present in the environment it is used *only* to
  raise the public GitHub API rate limit; the script works fine without it.

## How URLs are discovered

1. **Live crawl** — breadth-first from `https://havaianasdestruido.github.io/`,
   following every same-host link, so nested subpages are all found
   (`…/some/page/`, project sites `…/<repo>/…`, etc.). `index.html` URLs are
   canonicalized to their directory form, `noindex` pages are excluded, and
   `robots.txt` is honored (disable with `--ignore-robots`).
2. **Helper index** — seeds from the weekly repo-website list at
   [`gh-pages/URL.html`](https://havaianasdestruido.github.io/gh-pages/URL.html)
   (same-host entries only; external hosts like `*.vercel.app` are correctly
   left out of your sitemap).
3. **GitHub Pages discovery** — the public
   [`/users/havaianasdestruido/repos`](https://api.github.com/users/havaianasdestruido/repos)
   endpoint lists every repo with Pages enabled; each one is seeded at
   `https://havaianasdestruido.github.io/<repo>/` and crawled for its own
   subpages too.

Requests use bounded timeouts with retries, and `sitemap.xml` is replaced
atomically so a crash never leaves a partial file.

## Outputs

| File | Contents |
|------|----------|
| `sitemap.xml` | The sitemap (`urlset`). If there are more than 45,000 URLs it automatically becomes a sitemap index pointing at `sitemap-1.xml`, `sitemap-2.xml`, … |
| `robots.txt` | `User-agent: *` + a `Sitemap:` line pointing at `sitemap.xml` |

## Workflow

`.github/workflows/sitemap.yml` runs:

- every Monday 06:23 UTC,
- on pushes to `main` that touch `scripts/**` or the workflow itself,
- manually from the Actions tab (`workflow_dispatch`),

then commits the regenerated files as `github-actions[bot]` — or does
nothing if the sitemap didn't change.

## Serving the sitemap where search engines expect it

Google expects the sitemap at the root of the site it describes:
`https://havaianasdestruido.github.io/sitemap.xml`. For that, copy
`scripts/generate_sitemap.py` and `.github/workflows/sitemap.yml` into the
[`havaianasdestruido.github.io`](https://github.com/havaianasdestruido/havaianasdestruido.github.io)
repo — still zero secrets. From this repo the files are committed here, and
this repo can also publish them by enabling GitHub Pages on `main`
(then the sitemap lives at `…/global-sitemap/sitemap.xml`).

> Note: pushing into a *different* repo from this workflow would require a
> PAT (like `GH_PAT` in the `gh-pages` repo) — that's the only scenario that
> isn't token-less, which is why the recommendation above is to run the
> workflow in the repo where the sitemap should be served.

## Local usage

```bash
python scripts/generate_sitemap.py
# or with options:
python scripts/generate_sitemap.py \
  --site-url https://havaianasdestruido.github.io/ \
  --output sitemap.xml \
  --robots-out robots.txt
```

Useful flags: `--seed URL` (extra start points), `--max-depth N`,
`--max-urls N`, `--delay S`, `--no-helper`, `--no-github`, `--ignore-robots`,
`--keep-query`. See `python scripts/generate_sitemap.py --help`.
