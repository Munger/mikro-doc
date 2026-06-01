# Changelog

## 1.0.3

- Derive `--version` from package metadata so it can never get out of sync again

## 1.0.2

- Update header comment to document short-form CLI flags

## 1.0.1

- Fix `--open` flag to use `file://` URL so browser opens the page correctly
- Remove file logging — log to stdout only
- Add `--quiet` flag to suppress progress output
- Add sidebar credit block: GitHub avatar, copyright, MIT licence (all link to repo)
- Avatar and license fill the full banner height
- Default output directory prompts if not specified
- Fix SyntaxWarning for `\/` escape on Python 3.14
- Add CHANGELOG

## 1.0.0

- Initial release
