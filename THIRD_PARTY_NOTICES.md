# Third-party notices

Official Edge packages intentionally pin their provider libraries so an
upstream update cannot silently change a published package.

- FunPay vendors only the `FunPayAPI` provider layer from
  `sidor0912/FunPayCardinal` at commit
  `9d5ce692574ce2705f31715ec916ebede5d44d4e` under GPL-3.0. Cardinal's
  Telegram, installer, configuration, and host layers are not included.
- Playerok vendors `alleexxeeyy/PlayerokAPI` at commit
  `e2084a382081a584d24abb96cc1a64e5cb79a860` under MIT. Its license is kept at
  `playerok-universal/edge/vendor/PLAYEROKAPI_LICENSE`.
- Cardinal and Playerok Universal commits in `upstreams.lock.json` are the
  review boundary for selective updates. Provider code can be refreshed
  without changing the Edge SDK or the preserved v1 module manifest.

Updating a source requires updating the lock, reviewing its license and
provider contract, and passing the legacy-manifest and package tests.
