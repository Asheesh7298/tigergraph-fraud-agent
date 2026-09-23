#!/usr/bin/env bash
# Redeploy the compiled dashboard to GitHub Pages.
#
# The live UI is a build-only public repo (tigergraph-fraud-ui) so this source
# repo can stay private until submission. Run from web/ after any UI change.
set -euo pipefail

npm run build

TMP="$(mktemp -d)"
cp -r dist/. "$TMP/"
touch "$TMP/.nojekyll"                     # let Pages serve the assets/ folder
cd "$TMP"
git init -q -b main
git add -A
git commit -q -m "Deploy dashboard build"
git remote add origin https://github.com/Asheesh7298/tigergraph-fraud-ui.git
git push -qf origin main                   # force: the build repo only holds the latest build

echo "Deployed -> https://asheesh7298.github.io/tigergraph-fraud-ui/ (Pages rebuild ~1-2 min)"
