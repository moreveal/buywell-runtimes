# NSGifts Wholesale on Buywell Edge

NSGifts requires an outgoing-IP whitelist, so this adapter driver always runs
on your Edge. Its provider requests use IPv4. After connecting, copy the
outgoing IPv4 address shown by Buywell into the NSGifts whitelist and run the
health check.

Product search in the workflow editor uses this connection's live catalog,
showing current names, prices, and stock while storing the selected `service_id`.

`user_id`, login, password, `api_secret`, TOTP secret, session token, and order
state stay local. The driver signs exact bytes, never blindly repeats
`pay_order`, and reconciles timeouts/409 responses through `order_info`. Cloud
fallback is technically forbidden.
