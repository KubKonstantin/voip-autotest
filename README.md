# voip-autotest

Односессионный автотест WebRTC/SIP/RTP с GitLab-совместимым отчетом (JUnit XML).

## 1) Подготовка конфига

Скопируйте шаблон:

```bash
cp config.example.yaml config.yaml
```

Заполните `config.yaml` своими параметрами SIP/WebSocket.
Для RTP обязательно укажите рабочие ICE-серверы (`ice_servers`) для вашей сети/PBX.

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
После завершения сессии тест отправляет `REGISTER` с `Expires: 0` (unregister), если регистрация была включена и успешно прошла.

### Снижение вероятности входящего RE-INVITE от Asterisk

В INVITE добавлены Session-Timer заголовки, чтобы инициатором refresh был клиент:

```yaml
enable_session_timer: false
session_expires: 1800
min_se: 90
refresher: "uac"
reinvite_policy: "reject" # accept|reject
sip_trace: true
rtp_probe_seconds: 3
offer_telephone_event: true
telephone_event_payload: 101
```

Также тест теперь отправляет реальный silence RTP-поток (а не пустой `None`), что уменьшает шанс серверного media re-INVITE.
При `sip_trace: true` все входящие/исходящие SIP сообщения пишутся в `autotest.log`.
Если нужно жестко блокировать re-INVITE от PBX, используйте `reinvite_policy: "reject"` (ответ `488`).
`rtp_probe_seconds` — пауза перед проверкой RTP-статистики `aiortc` (`packetsSent/packetsReceived` для аудио).
Перед отправкой INVITE клиент ожидает завершение ICE gathering, чтобы SDP ушел с кандидатами.
Если нужно DTMF по RFC2833/4733, включите `offer_telephone_event: true` — в SDP offer будет добавлен `telephone-event/8000` (payload по `telephone_event_payload`).

## 3) Конвертация в GitLab JUnit

```bash
python3 autoreport.py --input session-report.json --output junit.xml
```

`junit.xml` можно публиковать в GitLab CI как `artifacts:reports:junit`.
