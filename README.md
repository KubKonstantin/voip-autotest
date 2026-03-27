# voip-autotest

Односессионный автотест WebRTC/SIP/RTP с GitLab-совместимым отчетом (JUnit XML).

## 1) Подготовка конфига

Скопируйте шаблон:

```bash
cp config.example.yaml config.yaml
```

Заполните `config.yaml` своими параметрами SIP/WebSocket.

## 2) Запуск теста

```bash
python3 autotest.py --config config.yaml
```

В результате формируется `session-report.json` (статус сессии и 3 этапа):
- `webrtc_signaling`
- `sip`
- `rtp`

Все этапы выполняются в рамках одной сессии (один WebSocket/SIP call flow).

### Опциональный REGISTER перед INVITE

Включается параметром:

```yaml
register_before_invite: true
register_expires: 300
```

Если включено, тест сначала выполняет `REGISTER`, обрабатывает challenge (`401`/`407`) и только потом отправляет `INVITE`.

### Снижение вероятности входящего RE-INVITE от Asterisk

В INVITE добавлены Session-Timer заголовки, чтобы инициатором refresh был клиент:

```yaml
session_expires: 1800
min_se: 90
refresher: "uac"
```

Также тест теперь отправляет реальный silence RTP-поток (а не пустой `None`), что уменьшает шанс серверного media re-INVITE.

## 3) Конвертация в GitLab JUnit

```bash
python3 autoreport.py --input session-report.json --output junit.xml
```

`junit.xml` можно публиковать в GitLab CI как `artifacts:reports:junit`.
