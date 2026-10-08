# Bundled latexpand

This directory contains the unmodified latexpand v1.7.2 script by Matthieu Moy and contributors, distributed under the BSD-3-Clause license in `LICENCE`.

- Upstream: https://gitlab.com/latexpand/latexpand
- Release source: https://mirrors.ctan.org/support/latexpand.zip (release dated 2023-02-27)
- Upstream commit: `4a76237c217a532a77044c0346b1e06648814531`
- Script SHA-256: `b66c5f753f0a006cd85b366835cc2e565152b235b9277e462ef49f5b5a97629f`

`arxiv-papers` always runs this pinned script through the system Perl interpreter. It does not invoke a system latexpand or download executable code at runtime. TeX Live is not required; latexpand may optionally consult `kpsewhich` for files outside the source tree when that tool is installed. Release updates must be deliberate, retain the upstream license, and update this provenance record and checksum test.
