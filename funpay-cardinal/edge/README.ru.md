# FunPay через Buywell Edge

Установите пакет в Edge и создайте подключение `funpay.cardinal`. Введите
`golden_key` и, если требуется, User-Agent локально через
`buywell-edge connection add` или `connection login`. Секрет не отправляется
в Buywell.

События, действия, каталоги и их версии совпадают с опубликованным
`funpay.cardinal@1.3.5`; Cardinal и Telegram-бот для нового подключения не
нужны. При logout подключение получает статус «нужен вход».
