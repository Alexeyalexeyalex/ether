# Голосовая связь через интернет

HTTPS и WebSocket обслуживают страницу и обмен служебными сообщениями.
Сам звук идёт по отдельному WebRTC-соединению между браузерами. Если прямой
маршрут недоступен из-за NAT или фильтрации UDP, нужен TURN. Это описано
в [документации WebRTC](https://webrtc.org/getting-started/turn-server).

## Что изменено в приложении

Браузер запрашивает `/api/rtc-config` при входе в голосовой канал.
Маршрут доступен после входа и возвращает `Cache-Control: no-store`.
Если TURN не указан, используются прежние публичные STUN-серверы.
Кнопка «На связи» открывает диагностику: состояние соединений, количество
полученных аудиопакетов, задержку и прямой/ретранслируемый маршрут.
«TURN настроен» означает наличие конфигурации, а не проверку её доступности.

## Вариант с coturn и временными паролями

1. Выделить TURN-сервер с публичным адресом и DNS-именем, например
   `turn.example.com`. Это образец имени, его нужно заменить своим.
2. Установить coturn и адаптировать `deploy/turnserver.conf.example`.
   Задать длинный случайный `static-auth-secret`, свой домен и сертификат.
   Не включать одновременно статические `user=` и `use-auth-secret`.
3. Разрешить на TURN-хосте входящие `3478/UDP`, `3478/TCP`, `5349/TCP`
   и диапазон `49160–49260/UDP` из примера. Если сервер за NAT, указать
   `external-ip` и соответствующий проброс диапазона. Обычный HTTP reverse
   proxy не передаёт TURN-трафик: имя должно вести непосредственно на сервер.
4. Передать **процессу приложения** переменные окружения:

   ```text
   EFIR_TURN_URLS=turn:turn.example.com:3478?transport=udp,turn:turn.example.com:3478?transport=tcp,turns:turn.example.com:5349?transport=tcp
   EFIR_TURN_SECRET=тот_же_секрет_что_в_static-auth-secret
   ```

5. Перезапустить процесс приложения и обновить страницы участников.

Приложение создаёт персональные временные реквизиты сроком на 24 часа по
схеме coturn. Общий секрет браузеру не передаётся. На приложении и TURN
должно быть правильное системное время. Формат параметров и реквизитов:
[официальный пример coturn](https://github.com/coturn/coturn/blob/master/examples/etc/turnserver.conf).

Для управляемого TURN с постоянными реквизитами вместо `EFIR_TURN_SECRET`
можно задать `EFIR_TURN_USERNAME` и `EFIR_TURN_CREDENTIAL` вместе с
`EFIR_TURN_URLS`. Эти клиентские реквизиты доступны вошедшим пользователям.

Альтернатива — массив `ice_servers` в `settings.json`:

```json
{
  "ice_servers": [
    {"urls": "stun:stun.cloudflare.com:3478"},
    {
      "urls": ["turn:turn.example.com:3478?transport=udp", "turns:turn.example.com:5349?transport=tcp"],
      "username": "TURN_USER",
      "credential": "TURN_PASSWORD"
    }
  ]
}
```

Добавьте поле к существующим настройкам, не заменяя остальные.
Непустой массив заменяет стандартные STUN; TURN из переменных окружения
добавляется к нему. Объекты TURN без имени пользователя и пароля игнорируются.

## Reverse proxy и процессы

Приложение хранит голосовые комнаты в памяти. Для этой версии нужен **один
процесс приложения** с потоками. Несколько независимых workers разделят
участников между разными наборами комнат; одного Redis без изменения этого
состояния недостаточно. За nginx запускайте HTTP на loopback:

```sh
python main.py --local --http
```

Пример фрагмента nginx внутри существующего HTTPS `server`:

```nginx
location / {
    proxy_pass http://127.0.0.1:2079;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

location /socket.io/ {
    proxy_pass http://127.0.0.1:2079;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 75s;
}
```

Исходный режим `threading` сохранён. Для production WSGI-сервера следует
использовать совместимый потоковый worker с одним процессом и
`simple-websocket`; общие варианты приведены в
[документации Flask-SocketIO](https://flask-socketio.readthedocs.io/en/latest/deployment.html).
Не переключайте этот проект на eventlet/gevent только установкой пакета:
режим явно задан в фабрике приложения.

## Перенос исправлений

Обновить каталог `app/` целиком (включая `static/vendor/socket.io.min.js`),
`requirements.txt` и перезапустить приложение. Рабочие `instance/` и
`settings.json` сохранить. Не переносить `.venv/`, `tests/`, `artifacts/`
или локальные сертификаты. Клиент и сервер обновляются вместе: новый вход
в голосовой канал ожидает подтверждение сервера и загружает `/api/rtc-config`.

В рамках проверки 21.09.2026 развёртывание и звонки на удалённом сервере
не выполнялись. Доступность реального TURN и связь между внешними сетями
после его настройки требуют отдельной проверки.
