# FunPay Cardinal runtime

Buywell integration for FunPay Cardinal `0.1.17.8`. The module source and
version live in this repository independently of Buywell application releases.

Build the package from the repository root:

```bash
python tools/build_packages.py funpay-cardinal
```

Install the resulting package in Buywell and follow
[the Russian guide](guides/install.ru.md) or
[the English guide](guides/install.en.md). The runtime artifact is installed as
a Cardinal plugin; it is not a standalone FunPay client.

`BUYWELL_API_URL` may override the default `https://buywell.pro/api` endpoint
before Cardinal starts.

Version 1.3.0 lets Buywell load category-specific order fields directly from a
regular `https://funpay.com/lots/<ID>/` URL through the connected Cardinal
runtime. Existing Console-imported connections remain valid.
