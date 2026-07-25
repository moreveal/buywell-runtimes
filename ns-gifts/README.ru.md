# NSGifts Wholesale через Buywell Edge

NSGifts требует whitelist исходящего IP, поэтому этот adapter driver всегда
исполняется на вашем Edge. Запросы к провайдеру используют IPv4. После
подключения скопируйте показанный Buywell исходящий IPv4-адрес в whitelist
NSGifts и запустите health check.

Поиск товара в редакторе сценариев использует живой каталог этого подключения:
показывает актуальные названия, цены и остатки и сохраняет выбранный `service_id`.

`user_id`, login, password, `api_secret`, TOTP secret, session token и
состояние заказа хранятся только локально. Driver подписывает точные bytes,
никогда не повторяет `pay_order` вслепую и после timeout/409 проверяет
`order_info`. Cloud fallback технически запрещён.
