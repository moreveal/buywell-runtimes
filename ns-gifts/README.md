# NSGifts Wholesale Edge driver

Official `edge_required` adapter driver for `https://api.ns.gifts`.

The driver stores credentials, session tokens, TOTP configuration, request
state, and delivered order reconciliation only on Buywell Edge. It signs the
exact request bytes required by NSGifts and never exposes a cloud execution
path.
