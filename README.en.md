[简体中文](README.md) | English

# Luoke

**Luoke (luoke-core)** brings local AI to your Mac. It downloads and loads the models and manages their memory and power use, and the Luoke apps (Luoke Mail, the Luoke input method and others) share this one set of models and one memory. You don't pick models: which model each Mac uses is written into a *recipe* after the lab has measured it.

## What it is

Luoke is a menu-bar app, and in the Luoke family it owns inference: every model call on the Mac goes through it, including loading, queueing, stepping aside when memory is tight, and resting. The lab publishes the recipe, which says which model and which inference engine (a pinned, unmodified [oMLX](https://github.com/jundot/omlx)) each task uses, and which tasks are on for each memory tier. Every choice in the recipe is backed by evaluation evidence.

Luoke also installs the apps. Tick an app in the Apps window and Luoke installs it, upgrades it, rolls it back to the previous version when needed, and on uninstall deletes only the data that app declared. Luoke Memory is bundled with Luoke (the Memory menu item).

How the family fits together: [luokelabs/luoke](https://github.com/luokelabs/luoke). Luoke's page in the system docs: [luoke-atlas · Luoke](https://luokelabs.github.io/luoke-atlas/#core).

## Status

**Closed beta (beta1).** The external interface (the v1 contract) is frozen; see [docs/V1.md](docs/V1.md) §2 and §8.

- A Mac with Apple silicon, macOS 26.2 or later. The inference engine needs 26.2; Luoke itself runs on 14 and later, but can't do anything without the engine.
- 16 GB of memory or more, 32 GB or more recommended. There are three tiers by memory (16 GB, 32 GB, 128 GB), and the recipe decides each tier's models and which tasks are on.
- Disk: about 7 GB for models and engine (16 / 32 GB tiers), or about 28 GB (larger tiers).

Sources for these numbers: [docs/community/luoke-core.md](docs/community/luoke-core.md) and [docs/V1.md](docs/V1.md) (first-run preflight, hardware tiers).

## Using it

Download Luoke from [luoke.ai](https://luoke.ai), open it and follow the first-run steps, then turn on the apps you want under Apps. Beta and official releases are published at [luokelabs/luoke-releases](https://github.com/luokelabs/luoke-releases).

## Building from source

You need Xcode or the Command Line Tools (Swift 5.9 or later) and the system `/usr/bin/python3`. The hub process uses only the Python standard library.

```bash
python3 -m unittest discover -s tests             # unit tests
sh app/build_app.sh                               # builds the dev app: app/build.noindex/螺壳（开发）.app
sh app/build_app.sh app/build.noindex --install   # and installs it into ~/Applications
```

- The dev build's bundle id is `ai.luoke.core.dev` and its data lives in `~/Library/Application Support/luoke-core-dev/`, separate from the release build.
- The first build downloads a pinned Python runtime from GitHub ([python-build-standalone](https://github.com/astral-sh/python-build-standalone)), verifies it and caches it in `~/Library/Caches/luoke-core/`.
- The build looks for a [luoke-mem](https://github.com/luokelabs/luoke-mem) checkout next to this one (`../luoke-mem`, or set `LUOKE_MEM_SRC`); without it the app is built without Luoke Memory.
- A dev build has no release manifest, so first run downloads nothing. Install the official oMLX yourself, at the version in the recipe's `engine.version`, and put the models in `~/.omlx/models` under the directory names in the recipe's `models`. The first-run check page says what is missing.

Running the hub process directly, without packaging:

```bash
python3 -m luoke_core serve --recipe recipes/dev.json --model-dir <model dir> --env <env dir> --socket /tmp/luoke-hub.sock
python3 tools/hub_smoke.py --socket /tmp/luoke-hub.sock      # exercises every endpoint; --dispatch also tests dispatch
```

For app developers: how an app plugs into Luoke (packaging, calling models, providing eval sets, data and upgrades) is in [docs/DESIGN.md](docs/DESIGN.md) §11 and [docs/V1.md](docs/V1.md) §8. The Python client is [`luoke_core/client.py`](luoke_core/client.py), and the packaging script for apps with a bundled Python is [`tools/package_python_app.py`](tools/package_python_app.py).

## Data and privacy

Inference stays on the Mac. The engine Luoke starts listens only on a random port on 127.0.0.1, with a new random key each time; MCP is off, and the engine's web search endpoints are never called. Requests from apps are rewritten by Luoke field by field and carry no tool calls. There is no telemetry and no crash reporting. Export Diagnostics only saves a file.

Luoke goes online for two things only:

- **Update checks**: `api.github.com` and `github.com`, automatic by default (daily for beta builds). It only notifies and never installs on its own. Turn it off with 每天自动检查 (check daily) at the bottom of the update window.
- **Downloads**: at first run, when installing or upgrading an app under Apps, and when filling in after an upgrade. Models come from `modelscope.cn` and `huggingface.co`, the engine and apps from `github.com`. After first run, anything missing is downloaded automatically, except on a metered network.

The local endpoint for advanced users (OpenAI-compatible, Settings → Advanced) is off by default, and when on it also listens only on 127.0.0.1.

Data lives in `~/Library/Application Support/luoke-core/` (models, engine, logs, settings). Luoke itself doesn't read the memory. The memory is `~/.luoke/mem.db`; it belongs to the user and is never deleted on uninstall.

The network list is checked line by line against the code in the luoke-audit review. The rules are in [docs/DESIGN.md](docs/DESIGN.md) §8.

## Documentation

The design documents are in Chinese.

| File | Contents |
|---|---|
| [CONTEXT.md](CONTEXT.md) | Glossary |
| [docs/V1.md](docs/V1.md) | v1 spec: external contract, first run, install and uninstall, evaluation and release |
| [docs/DESIGN.md](docs/DESIGN.md) | Long-term design: all the rules and the reasons |
| [docs/adr/](docs/adr/) | Decisions that are hard to reverse, and why |
| [docs/experiments/](docs/experiments/) | Measurements: tier base models, coexistence, engine limits |
| [docs/research/](docs/research/) | Research notes (reference, not decisions) |
| [tools/release_evals.json](tools/release_evals.json), [tools/support.py](tools/support.py) | Pre-release evaluation: how each task is scored, the floors, which tiers are on |

## License, security, contributing

- License: [Apache-2.0](LICENSE). The name, icons and brand assets are not covered by the license; licenses of third-party code are listed in [NOTICE](NOTICE).
- Please report security issues privately; see [SECURITY.md](SECURITY.md).
- To contribute, see [CONTRIBUTING.md](CONTRIBUTING.md).

© 螺壳实验室 Luoke Labs
