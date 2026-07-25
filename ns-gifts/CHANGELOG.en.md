# 1.0.2

- NSGifts requests now consistently use the Edge server's outgoing IPv4 address, matching the address added to the provider whitelist.
- A forbidden response now distinguishes an IP whitelist rejection from missing API v2 permissions when NSGifts provides the reason.

# 1.0.1

- Interactive Edge setup now reads Russian and English field labels from the NSGifts package.

# 1.0.0

- Initial Edge-only NSGifts driver.
- HMAC-SHA256 signing, two-hour sessions, TOTP, and IP whitelist support.
- Safe order creation, payment, and reconciliation without blind retries.
