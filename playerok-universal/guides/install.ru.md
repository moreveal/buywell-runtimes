# Установка Playerok Universal

1. Скачайте runtime-архив со страницы установленного модуля в Buywell.
2. Откройте папку установленного Playerok Universal.
3. Распакуйте архив в папку `modules`. После распаковки должен существовать файл `modules/buywell_playerok/__init__.py`.
4. Перезапустите Playerok Universal. Зависимость `websocket-client` будет проверена штатным загрузчиком модулей.
5. Откройте Telegram-бот Playerok Universal и выполните `/buywell`.
6. Нажмите **Подключить**, отправьте ключ подключения Buywell и дождитесь статуса **Подключено**.

Ключ Playerok, cookies и proxy остаются внутри Playerok Universal. Модуль отправляет в Buywell только события, выбранные данные событий и результаты действий.

При обновлении замените файлы папки `modules/buywell_playerok`, не удаляя созданную рядом папку `module_data`.
