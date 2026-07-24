# Playerok через Buywell Edge

Установите пакет и создайте `playerok.universal` connection. Можно перенести
обычный token из Playerok Universal либо ввести полный Cookie header; `__ddg5_`,
User-Agent и optional proxy вводятся локально и остаются на Edge. Playerok
Universal и Telegram-бот для нового подключения не нужны.

Пакет сохраняет manifest `playerok.universal@1.0.4` и использует закреплённый
исходный PlayerokAPI. CAPTCHA и 2FA не обходятся: Edge покажет «нужен вход».
