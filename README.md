# IETF Document Review Template

A workspace template for preparing for an IETF meeting: it downloads the agendas
of the working groups you follow, fetches every Internet-Draft and RFC they
reference, and rewrites each agenda as Markdown with links to the local copies,
so you can read and review everything offline, one folder per WG.

## What it does

For each working group listed in [meetings.yaml](meetings.yaml),
[fetch_agendas.py](fetch_agendas.py):

1. downloads the WG agenda for the configured IETF meeting from the Datatracker
   (converting plain-text agendas to Markdown),
2. splits the agenda into sections (Markdown headings and top-level numbered
   items),
3. finds every referenced Internet-Draft (and RFCs linked by URL) and downloads
   it with [`ietf-reviewtool`](https://github.com/larseggert/ietf-reviewtool)
   into a subfolder for the section that first mentions it,
4. writes `<output_dir>/<meeting>/<WG>/agenda.md`, with the references
   re-linked to the local `.txt` files.

Example layout:

```
IETF/127/
  REGEXT/
    agenda.md
    03-working-group-documents/
      draft-ietf-regext-example-05.txt
  RPP/
    agenda.md
    ...
```

## Setup

Requires Python 3.9+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

A [dev container](.devcontainer/devcontainer.json) is included as well; it
creates the virtual environment and installs the dependencies automatically.

## Configuration

Edit [meetings.yaml](meetings.yaml):

```yaml
meeting: 127
# Agendas go to <output_dir>/<meeting>/<WG>/agenda.md; "{meeting}" is substituted.
output_dir: IETF
# Extra arguments passed to `ietf-reviewtool fetch` (optional).
fetch_args: ["--no-fetch-xml", "--no-fetch-writeups"]
wgs:
  - REGEXT
  - RPP
  - DNSOP
```

## Usage

```sh
# Process all WGs in meetings.yaml
.venv/bin/python fetch_agendas.py

# Use another config file
.venv/bin/python fetch_agendas.py other-meeting.yaml

# Only some WGs (repeatable, case-insensitive)
.venv/bin/python fetch_agendas.py --wg RPP --wg regext

# Re-download documents that are already present
.venv/bin/python fetch_agendas.py --force
```

Documents already downloaded are skipped unless `--force` is given. The script
exits with a non-zero status if any WG agenda could not be downloaded (for
example, because it has not been published yet).

## License

[MIT](LICENSE)
