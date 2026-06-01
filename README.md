# mikro-doc

Generate a self-contained offline HTML API reference for RouterOS.

Discovers the full RouterOS API tree from a live router via the REST
interface, then produces a standalone HTML document (with search,
sidebar navigation, pagination and example curl commands) and a
Markdown reference — all usable directly from the `file://` protocol
without a web server.

```
mikro-doc --host H --user U --pass P [--store-credentials] [--output-dir DIR] [--endpoints] [--no-docs]
mikro-doc --schema PATH [--output-dir DIR] [--endpoints] [--no-docs]
mikro-doc PATH [--output-dir DIR] [--endpoints] [--no-docs]
mikro-doc --no-fetch [--host H|--schema PATH] [options]
```

## ⚠ BIG FAT WARNING

This tool will query your router (potentially hundreds of times) to discover the API structure.
It is designed to be safe and non-intrusive, but the documentation it generates will include actual configuration
information from your router (e.g. interface names, user names). **Do NOT share the generated documentation publicy.**
It is for your eyes only. If you passed `--store-credentials`, the credentials will be stored in plaintext in the
output JSON file for future use. You have been warned.

## Output

```
<output-dir>/<router>.schema.json         — Full API schema (JSON)
<output-dir>/<router>.index.html          — Standalone docs page (works on file://)
<output-dir>/<router>.md                  — Markdown reference
<output-dir>/<router>_Endpoints.json      — Hierarchical endpoints tree (with --endpoints)
<output-dir>/<router>.log                 — Discovery log
```

## Install

```bash
pipx install mikro-doc
```

This pulls from PyPI, creates an isolated environment, and installs the
`mikro-doc` command and its dependencies. On macOS, `pip` without a
virtual environment is blocked by system integrity protection; **pipx
is the recommended alternative.**

To upgrade: `pipx upgrade mikro-doc`

### Alternatives

- `pip3 install --user mikro-doc` (works on most Linux, blocked on macOS)
- `python3 -m venv /path/to/venv && /path/to/venv/bin/pip install mikro-doc`

## Requirements

- Python 3.7+

## Bundled Data

`mikro-doc.hints` is bundled inside the package and provides example
values for common RouterOS parameter types. Without it, placeholder values
(`<paramname>`) will be used in curl examples.
