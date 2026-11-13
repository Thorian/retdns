# Releasing retdns

This directory is the standalone repository root. The package does not depend on the surrounding workspace.

1. Update `pyproject.toml`, `src/retdns/__init__.py` and `CHANGELOG.md` together.
2. Install development dependencies and run Ruff plus the tests with `RETDNS_NETWORK_TEST=1`.
3. Build and validate the distributions:

   ```sh
   python -m build
   python -m twine check --strict dist/*
   ```

4. Install the wheel and its declared dependencies into a fresh environment. Check `retdns --help`, local record management, and the tests against the installed package.
5. Review what will be committed. Exclude identities, authoritative databases, lookup history, cache files and local environment settings. Commit source using your own Git author identity.

## GitHub releases

The repository is [Thorian/retdns](https://github.com/Thorian/retdns). Commit the
reviewed source and push it to `main` before preparing a release:

```sh
git status --short
git add .
git commit -m "Update retdns"
git push origin main
```

Check the staged diff before committing. Source and issue-tracker URLs are in
`pyproject.toml`; update them if the repository moves.

After the entire GitHub CI matrix passes, create an annotated version tag, push it, and create a GitHub release with the wheel and source archive attached. Mark the initial 0.2.0 release as a GitHub pre-release because its APIs are experimental. Use the changelog for release notes. The CI workflow checks source and installed-wheel tests on Windows/Linux with Python 3.11–3.13; it does not publish releases or upload to PyPI. Local Windows validation alone does not establish the other matrix results.

Before a later PyPI upload, confirm package-name availability and configure the maintainer account separately. No upload token is stored or required for preparing this repository.
