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

## 3) Конвертация в GitLab JUnit

```bash
python3 autoreport.py --input session-report.json --output junit.xml
```

`junit.xml` можно публиковать в GitLab CI как `artifacts:reports:junit`.
