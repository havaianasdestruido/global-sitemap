#!/usr/bin/env bash
# Stage the generated sitemap files in the current working tree.
#
# Two traps this avoids:
#
# 1. `git add -A -- 'sitemap*.xml' sitemap.txt robots.txt` exits 128
#    ("pathspec ... did not match any files") when ANY pathspec matches
#    nothing, and the workflow runs under `bash -e`, so the job died before
#    it could commit. After the switch to XML, `sitemap.txt` is gone and
#    `sitemap-N.xml` parts only exist above 50,000 URLs, so an unmatched
#    pathspec is the *normal* case.
# 2. A bare `git add -A` avoids that error but happily stages unrelated
#    untracked files (scratch output, editor backups, ...). Only the sitemap
#    outputs should ever be committed.
#
# So: resolve the pathspecs first, keeping the tracked matches (which makes
# deletions show up) plus the untracked files that match the same patterns,
# and stage that explicit list — one invocation, no unmatched pathspec, no
# collateral staging.
#
# Used by .github/workflows/sitemap.yml and covered by
# tests/test_stage_sitemap_files.py.
set -euo pipefail

PATTERNS=('sitemap*.xml' 'sitemap.txt' 'robots.txt')

paths=()
for pattern in "${PATTERNS[@]}"; do
  # Tracked files (including ones just deleted from the work tree).
  while IFS= read -r -d '' file; do
    paths+=("$file")
  done < <(git ls-files -z -- "$pattern")
  # Untracked files that exist now (new sitemap.xml, new part files). The
  # leading pattern means a bare name; --exclude-standard honors .gitignore.
  while IFS= read -r -d '' file; do
    paths+=("$file")
  done < <(git ls-files -z --others --exclude-standard -- "$pattern")
done

if [ ${#paths[@]} -eq 0 ]; then
  exit 0
fi

# De-duplicate while keeping the order stable.
mapfile -t paths < <(printf '%s\n' "${paths[@]}" | awk '!seen[$0]++')

git add -A -- "${paths[@]}"
