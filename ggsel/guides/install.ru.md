# Установка GGSel Seller runtime

## Требования

- Windows 10/11 или современный Linux;
- Python 3.11 или новее;
- API-ключ продавца GGSel с доступом к V1 Orders и Chats;
- установленный пакет GGSel Seller `1.0.0` в Buywell;
- ключ Buywell с разрешением `modules:connect`.

Runtime устанавливается на компьютере или сервере, который постоянно включён.
Входящий порт и публичный IP не требуются.

## 1. Автоматическая установка

Откройте терминал в каталоге `ggsel`.

Windows:

```bat
install.bat
```

Linux:

```bash
chmod +x install.sh run.sh
./install.sh
```

Установщик сам создаёт `.venv`, ставит зависимости, запрашивает ключ Buywell,
ID продавца и API-ключ GGSel, создаёт `config.json`, проверяет конфигурацию и
read-only доступ к покупкам и чатам V1, затем предлагает сразу запустить
runtime. Секреты при вводе не отображаются. Ключ, ограниченный только новым V2
API для управления каталогом, для событий покупок и сообщений не подходит.

Файл `config.json` содержит секреты. Не отправляйте его другим людям и не
добавляйте в Git.

## 2. Последующие запуски

Windows:

```bat
run.bat
```

Linux:

```bash
./run.sh
```

После сообщения `Connected to Buywell` включите нужные подключения событий в
Buywell. По умолчанию первый запуск запоминает текущие покупки и сообщения, но
не запускает по ним сценарии.

## 3. Постоянный запуск на Linux

Создайте `/etc/systemd/system/buywell-ggsel.service`:

```ini
[Unit]
Description=Buywell GGSel runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=buywell
WorkingDirectory=/opt/buywell-runtimes/ggsel
ExecStart=/opt/buywell-runtimes/ggsel/.venv/bin/python runtime/ggsel_runtime.py --config config.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Затем выполните:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now buywell-ggsel
sudo systemctl status buywell-ggsel
```

## 4. Постоянный запуск на Windows

Используйте Планировщик заданий Windows:

1. Создайте задачу с запуском при входе или старте системы.
2. В поле программы укажите полный путь к `.venv\Scripts\python.exe`.
3. В аргументах укажите `runtime\ggsel_runtime.py --config config.json`.
4. В рабочем каталоге укажите каталог `ggsel`.

SQLite хранится по пути `database_path`. Сохраняйте этот файл при переносе или
резервном копировании runtime.
