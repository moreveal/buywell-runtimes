# Buywell runtimes

Official runtime sources and immutable packages for Buywell platform
integrations. New installations run through Buywell Edge; the existing v1
artifacts remain unchanged so published workflows and current users can migrate
explicitly.

## Runtimes

| Runtime | Edge-native package | Capabilities |
| --- | --- | --- |
| [GGSel Seller](ggsel/) | `ggsel.seller@1.2.3` | New purchases, buyer messages, replies and product catalog |
| [FunPay](funpay-cardinal/) | `funpay.cardinal@1.3.0` | Orders, status changes, messages, replies and buyer input |
| [Playerok](playerok-universal/) | `playerok.universal@1.0.4` | Paid sales, buyer messages, contextual replies and category/item catalogs |
| [NSGifts](ns-gifts/) | `adapter.ns-gifts@1.0.0` | Edge-required wholesale adapter, signing, TOTP and IP-whitelist diagnostics |

The marketplace packages embed their exact published v1 manifest under the
Edge compatibility contract. Their module IDs, versions, events, actions,
catalogs and existing Buywell authentication remain valid; only the local
transport and process host change.

Runtime catalogs follow Buywell's neutral binding-catalog contract. Depending on
the platform, a scope may be one product or a category containing several
seller listings. Catalog selection is optional: ordinary event conditions stay
available, while stable scope and choice IDs let a workflow map marketplace
variants to neutral inputs without relying on mutable listing names.

## Build packages

Legacy archives are still built with Python 3.11+:

```bash
python tools/build_packages.py
```

Archives are written to `dist/`. ZIP entry ordering and timestamps are fixed so
the same source produces the same archive bytes.

Edge packages use Python 3.12 and the public `buywell-edge-sdk`:

```bash
python tools/build_edge_packages.py
```

The builder signs packages, generates Manifest v2 from Python declarations and
keeps each upstream provider dependency pinned in `upstreams.lock.json`.

## Validate

```bash
python -m unittest discover -s tests -v
python -m compileall -q ggsel funpay-cardinal playerok-universal ns-gifts tools tests
```

Runtime-specific installation steps are kept beside each runtime.

## Versioning

Module versions follow semantic versioning. A published version is immutable.
Change the runtime constant, `manifest.json`, and changelog together before
publishing a new version.

## License

GPL-3.0. See [LICENSE](LICENSE).
