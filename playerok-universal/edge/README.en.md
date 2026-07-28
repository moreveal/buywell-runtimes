# Playerok on Buywell Edge

Install the package and create a `playerok.universal` connection. You can
import the regular Playerok Universal token or provide a full Cookie header;
`__ddg5_`, User-Agent, and an optional proxy stay local to Edge. New
connections do not need Playerok Universal or a Telegram bot.

The package preserves the `playerok.universal@1.0.6` manifest and uses pinned
PlayerokAPI source. CAPTCHA and 2FA are not bypassed: Edge reports
“sign-in required”.
