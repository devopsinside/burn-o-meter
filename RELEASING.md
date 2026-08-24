# Releasing

The order matters, and getting it wrong costs a version number.

GitHub releases are **immutable**: assets can only be attached when the release is
created, and a tag consumed by a release can never be reused — deleting the release
does not free it. A bottle can only be built once its source archive is
downloadable. Those two facts pin the sequence below.

```bash
# 1. Bump the version everywhere, and let the tests confirm nothing was missed.
#    A test fails if the README, the docs or the formula disagree with __version__.
$EDITOR pyproject.toml src/burnometer/__init__.py     # version = / __version__ =
$EDITOR README.md docs/install.md                     # status line + pinned URLs
$EDITOR Formula/burn-o-meter.rb                       # url -> the new tag
pytest -q && ruff check src tests && ruff format --check src tests

# 2. Tag. The source archive exists the moment the tag is pushed — this is what
#    lets bottles be built before the release.
git commit -am "vX.Y.Z" && git push
git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z

# 3. Point the formula at the tag archive's checksum, and push before building.
#    The bottle job checks out the tag, so a fix made after this is invisible to it.
curl -sL -o /tmp/src.tar.gz \
  "https://github.com/devopsinside/burn-o-meter/archive/refs/tags/vX.Y.Z.tar.gz"
shasum -a 256 /tmp/src.tar.gz          # paste into Formula/burn-o-meter.rb
git commit -am "Point the formula at vX.Y.Z" && git push

# 4. Build bottles. BEFORE the release, never after.
gh workflow run bottles.yml -f tag=vX.Y.Z
gh run download <run-id> --dir /tmp/bottles

# 5. Build the artifacts.
python -m build
macos/make-app.sh
(cd macos/build && zip -qr burn-o-meter-macos-vX.Y.Z.zip burn-o-meter.app)

# 6. Create the release with EVERYTHING attached. There is no second chance.
gh release create vX.Y.Z \
  dist/*.whl dist/*.tar.gz macos/build/*.zip /tmp/bottles/*/*.bottle.tar.gz \
  --title "vX.Y.Z" --notes "..."

# 7. Add the bottle block, with root_url pointing at the release just made.
$EDITOR Formula/burn-o-meter.rb && git commit -am "Add vX.Y.Z bottles" && git push
```

## Verify

```bash
brew untap devopsinside/burn-o-meter; brew uninstall burn-o-meter
brew tap devopsinside/burn-o-meter https://github.com/devopsinside/burn-o-meter
brew install devopsinside/burn-o-meter/burn-o-meter   # must say "Pouring", not "Building"
brew test devopsinside/burn-o-meter/burn-o-meter
```

Pouring a bottle skips the source download, so a wrong `sha256` on the source will
not show up here. Check it directly:

```bash
curl -sL "https://github.com/devopsinside/burn-o-meter/archive/refs/tags/vX.Y.Z.tar.gz" \
  | shasum -a 256          # must equal the sha256 in the formula
```
