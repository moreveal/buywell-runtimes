# NSGifts Wholesale через Buywell Edge

NSGifts требует whitelist исходящего IP, поэтому этот adapter driver всегда
исполняется на вашем Edge. После подключения скопируйте показанный Buywell
исходящий IP в whitelist NSGifts и запустите health check.

`user_id`, login, password, `api_secret`, TOTP secret, session token и
состояние заказа хранятся только локально. Driver подписывает точные bytes,
никогда не повторяет `pay_order` вслепую и после timeout/409 проверяет
`order_info`. Cloud fallback технически запрещён.
