# Buywell runtimes

Independent runtime sources and module packages for Buywell platform integrations.
Each directory owns its module version, runtime artifact, manifest, installation
guides, and changelog. Application releases do not change installed module
versions.

## Runtimes

| Runtime | Environment | Capabilities |
| --- | --- | --- |
| [GGSel Seller](ggsel/) | Windows or Linux, Python 3.11+ | New purchases, buyer messages, replies (Seller API V1) |
| [FunPay Cardinal](funpay-cardinal/) | FunPay Cardinal 0.1.17.8 | Orders, status changes, messages, replies, buyer input |
| [Playerok Universal](playerok-universal/) | Playerok Universal embedded module | Paid sales, buyer messages, contextual replies, category/item catalogs, typed order fields |

Runtime catalogs follow Buywell's neutral binding-catalog contract. Depending on
the platform, a scope may be one product or a category containing several
seller listings. Catalog selection is optional: ordinary event conditions stay
available, while stable scope and choice IDs let a workflow map marketplace
variants to neutral inputs without relying on mutable listing names.

## Build packages

Python 3.11 or newer is sufficient. The build has no third-party dependencies.

```bash
python tools/build_packages.py
```

Archives are written to `dist/`. ZIP entry ordering and timestamps are fixed so
the same source produces the same archive bytes.

## Validate

```bash
python -m unittest discover -s tests -v
python -m compileall -q ggsel funpay-cardinal playerok-universal tools tests
```

Runtime-specific installation steps are kept beside each runtime.

## Versioning

Module versions follow semantic versioning. A published version is immutable.
Change the runtime constant, `manifest.json`, and changelog together before
publishing a new version.

## License

GPL-3.0. See [LICENSE](LICENSE).
