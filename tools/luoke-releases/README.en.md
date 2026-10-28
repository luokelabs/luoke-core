<!-- English source of the luokelabs/luoke-releases README, maintained by luoke-core next to README.md (Chinese); change and upload both together. -->

[简体中文](README.md) | English

# luoke-releases: Luoke releases

Luoke is a set of apps that run local models on your own Mac: **Luoke**, a menu-bar app that runs the models and the inference engine, includes Luoke Memory, and installs and upgrades the apps; **Luoke Mail**; and the **Luoke input method**. This repository holds Luoke's beta and official releases, plus the license files that redistribution requires.

## Download

**Download from [luoke.ai](https://luoke.ai).** Open `Luoke.dmg`, drag Luoke into Applications, open it and follow the first-run steps, then turn on Luoke Mail or the input method under Apps. From then on Luoke checks for updates itself.

- **Status**: closed beta. Beta builds (from beta1 on) are the regular Releases here, and the latest one's installer is always called `Luoke.dmg`.
- **Internal builds** (the team's daily builds) are published at [luokelabs/luoke-internal-builds](https://github.com/luokelabs/luoke-internal-builds), not here. Earlier internal builds here are marked as pre-releases; testers should install the beta.
- **Requirements**: a Mac with Apple silicon, macOS 26.2 or later, 16 GB of memory or more (32 GB or more recommended), and about 7 GB of disk (16 / 32 GB of memory) or about 28 GB (more memory).

## What each release contains

| Asset | Contents |
|---|---|
| `Luoke.dmg` | Luoke's installer |
| App packages | Luoke Mail and the input method, installed from Luoke's Apps window; no need to download them by hand |
| Engine package zip | The inference engine (a copy of [oMLX](https://github.com/jundot/omlx), re-signed, code unmodified) |
| `release.json` | Release manifest: versions, download URLs and hashes of this release's apps, engine and models. Luoke downloads from it and checks every file's hash |
| Release notes | What changed, the engine version, known issues |

Models are not stored here. They are on ModelScope and Hugging Face, and each release's `release.json` and notes name the exact repositories.

## Licenses and attribution

| Path | Contents |
|---|---|
| `licenses/engine/` | The Apache-2.0 license of oMLX in the engine package (`LICENSE-oMLX.txt`) and the list of Python packages it bundles, with names, versions and licenses (`THIRD-PARTY-PACKAGES.txt`) |
| `licenses/python-runtime/` | Licenses of the components of the Python runtime (python-build-standalone) bundled with Luoke and its apps |
| `licenses/models/` | Sources and licenses of the models |

- The Luoke apps are developed by **螺壳实验室 Luoke Labs**. Their code lives in their own repositories; see each one for its license.
- oMLX in the engine package is redistributed under the Apache License 2.0; each release's notes give its version and state that the code is unmodified. The Python packages it bundles keep their own licenses. The GPL / LGPL ones among them (mlx_embeddings, phonemizer_fork, espeakng_loader, num2words, pycountry, soxr) are unmodified; their source packages are attached to each release or available on PyPI.
- Models: Qwen3.5-9B and Qwen3.5-0.8B-Base (Apache-2.0, Alibaba Qwen), and Gemma 4 26B (Apache-2.0, Google; Google's [Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy) also applies). The model cards of the lab-quantized versions state the upstream model, the license and the quantization. See `licenses/models/README.md`.

## Feedback and security

During the closed beta, the feedback channels are in the notes for testers; once public, open Issues here. Please don't post security details in public issues: report them privately via **Security → Report a vulnerability** (see [SECURITY.md in luokelabs/luoke](https://github.com/luokelabs/luoke/blob/main/SECURITY.md)).

© 螺壳实验室 Luoke Labs
