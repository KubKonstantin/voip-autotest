import asyncio
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.mediastreams import MediaStreamTrack
import websockets
import logging
import ssl
import hashlib
import uuid
import re

# Конфигурация
CONFIG = {
    "opensips_ws": "wss://VAR_DOMAIN:7443/wss",
    "caller": "VAR_USERNAME@VAR_DOMAIN",
    "callee": "VAR_CALLEE@VAR_DOMAIN",
    "timeout": VAR_TIMEOUT,
    "origin": "https://VAR_DOMAIN",
    "subprotocols": ["sip"],
    "auth_username": "VAR_USERNAME",
    "auth_password": "VAR_PASSWD",
    "realm": "VAR_REALM",
    "max_attempts": VAR_MAX_ATTEMPTS,
    "call_duration": VAR_CALL_DURATION,  # Длительность звонка в секундах
    "bye_timeout": VAR_BYE_TIMEOUT,     # Таймаут ожидания BYE от сервера
}

logging.basicConfig(level=logging.DEBUG, filename = u'./autotest.log')
logger = logging.getLogger("webrtc_test")

class FakeAudioTrack(MediaStreamTrack):
    kind = "audio"
    async def recv(self):
        return None

class SIPAuthHelper:
    @staticmethod
    def generate_proxy_auth(method, uri, nonce, username, password, realm):
        """Генерация заголовка Proxy-Authorization с qop, cnonce и nc"""
        cnonce = uuid.uuid4().hex[:12]
        nc = "00000001"
        qop = "auth"
        
        # Расчет HA1
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        
        # Расчет HA2
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        
        # Расчет response
        response = hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
        
        return (
            'Proxy-Authorization: Digest algorithm=MD5, username="{}", '
            'realm="{}", nonce="{}", uri="{}", response="{}", '
            'qop={}, cnonce="{}", nc={}\r\n'.format(
                username, realm, nonce, uri, response, qop, cnonce, nc
            )
        )

class WebRTCTester:
    def __init__(self):
        config = RTCConfiguration(iceServers=[])
        self.pc = RTCPeerConnection(configuration=config)
        self._setup_event_handlers()
        self.call_id = f"{uuid.uuid4()}@docdoc.pro"
        self.cseq = 1
        self.branch = f"z9hG4bK-{uuid.uuid4()}"
        self.auth_info = None
        self.from_tag = str(uuid.uuid4())
        self.to_tag = None
        self.local_sdp = None

    def _setup_event_handlers(self):
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            state = self.pc.iceConnectionState
            logger.info("PASS: ICE Connection State: %s", state)

    async def _create_ssl_context(self):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        ssl_context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        return ssl_context

    async def signaling_connect(self):
        try:
            ssl_context = await self._create_ssl_context()
            self.websocket = await websockets.connect(
                CONFIG["opensips_ws"],
                ssl=ssl_context,
                extra_headers={"Origin": CONFIG["origin"]},
                subprotocols=CONFIG["subprotocols"],
                ping_interval=None
            )
            logger.info("PASS: WebSocket connection established")
            return True
        except Exception as e:
            logger.error("FAIL: WebSocket connection failed: %s", str(e))
            return False

    def build_sip_invite(self, offer_sdp, proxy_auth=None):
        invite = (
            "INVITE sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS voip-wss-dev-tm01.docdoc.pro;branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} INVITE\r\n"
            "Contact: <sip:autotest@voip-wss-dev-tm01.docdoc.pro;transport=ws>\r\n"
            "Content-Type: application/sdp\r\n"
            .format(
                CONFIG['callee'],
                self.branch,
                CONFIG['caller'],
                self.from_tag,
                CONFIG['callee'],
                self.call_id,
                self.cseq
            )
        )
        
        if proxy_auth:
            invite += proxy_auth
        
        invite += (
            "Content-Length: {}\r\n"
            "\r\n"
            "{}"
            .format(len(offer_sdp), offer_sdp)
        )
        
        self.cseq += 1
        return invite

    def build_sip_bye(self):
        """Формирование SIP BYE сообщения"""
        if not self.to_tag:
            raise Exception("No To tag available for BYE")
        
        bye = (
            "BYE sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS voip-wss-dev-tm01.docdoc.pro;branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>;tag={}\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        ).format(
            CONFIG['callee'],
            f"z9hG4bK-{uuid.uuid4()}",
            CONFIG['caller'],
            self.from_tag,
            CONFIG['callee'],
            self.to_tag,
            self.call_id,
            self.cseq
        )
        self.cseq += 1
        return bye

    async def handle_bye(self, bye_message):
        """Обработка входящего BYE сообщения"""
        try:
            # Парсим входящий BYE
            parsed = self.parse_sip_response(bye_message)
            
            if not parsed.get('method') == 'BYE':
                raise Exception("Not a BYE message")
            
            # Формируем ответ 200 OK
            ok_response = (
                "SIP/2.0 200 OK\r\n"
                "Via: {via}\r\n"
                "From: <sip:{caller}>;tag={from_tag}\r\n"
                "To: <sip:{callee}>;tag={to_tag}\r\n"
                "Call-ID: {call_id}\r\n"
                "CSeq: {cseq}\r\n"
                "Content-Length: 0\r\n\r\n"
            ).format(
                via=parsed['headers']['Via'],
                caller=CONFIG['caller'],
                from_tag=self.from_tag,
                callee=CONFIG['callee'],
                to_tag=self.to_tag,
                call_id=self.call_id,
                cseq=parsed['headers']['CSeq']
            )
            
            await self.websocket.send(ok_response)
            logger.info("PASS: Responded with 200 OK to BYE")
            return True
            
        except Exception as e:
            logger.error("FAIL: Error processing BYE: %s", str(e))
            logger.debug("FAIL: Problematic BYE message: %s", bye_message)
            return False

    def build_sip_ack(self):
        if not self.to_tag:
            raise Exception("No To tag available for ACK")
        
        ack = (
            "ACK sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS voip-wss-dev-tm01.docdoc.pro;branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>;tag={}\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} ACK\r\n"
            "Content-Length: 0\r\n\r\n"
            .format(
                CONFIG['callee'],
                f"z9hG4bK-{uuid.uuid4()}",
                CONFIG['caller'],
                self.from_tag,
                CONFIG['callee'],
                self.to_tag,
                self.call_id,
                self.cseq - 1  # ACK использует тот же CSeq, что и INVITE
            )
        )
        return ack

    def parse_sip_response(self, response):
        """Улучшенный парсинг SIP ответов и запросов"""
        lines = response.split('\r\n')
        if not lines:
            raise Exception("Empty SIP message")
        
        # Первая строка может быть либо статусной (SIP/2.0 200 OK), либо запросом (BYE sip:...)
        status_line = lines[0]
        
        # Определяем тип сообщения
        if status_line.startswith('SIP/2.0'):
            # Это ответ (status-line)
            if not re.match(r'SIP/2\.0 \d{3}', status_line):
                raise Exception(f"Invalid SIP status line: {status_line}")
            status_code = int(status_line.split()[1])
        else:
            # Это запрос (request-line)
            if not re.match(r'[A-Z]+ sip:.+ SIP/2\.0', status_line):
                raise Exception(f"Invalid SIP request line: {status_line}")
            status_code = None
        
        headers = {}
        body = ""
        header_end = False
        
        for line in lines[1:]:
            if not line.strip():
                header_end = True
                continue
            if not header_end:
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip()] = value.strip()
            else:
                body += line + '\r\n'
        
        return {
            'status_code': status_code,
            'method': status_line.split()[0] if status_code is None else None,
            'headers': headers,
            'body': body.strip()
        }

    async def handle_407_response(self, response):
        parsed = self.parse_sip_response(response)
        auth_header = parsed['headers'].get('Proxy-Authenticate', '')
        
        if not auth_header:
            raise Exception("No Proxy-Authenticate header in 407 response")
        
        # Извлекаем параметры аутентификации
        auth_params = {}
        for part in auth_header.split(','):
            part = part.strip()
            if '=' in part:
                key, value = part.split('=', 1)
                auth_params[key.strip()] = value.strip('"')
        
        self.auth_info = {
            "realm": auth_params.get("realm", CONFIG["realm"]),
            "nonce": auth_params.get("nonce", ""),
            "algorithm": auth_params.get("algorithm", "MD5"),
            "qop": auth_params.get("qop", "auth")
        }
        logger.debug("PASS: Extracted auth info: %s", self.auth_info)

    async def send_sip_invite(self, offer):
        proxy_auth = None
        if self.auth_info:
            proxy_auth = SIPAuthHelper.generate_proxy_auth(
                method="INVITE",
                uri=f"sip:{CONFIG['callee']}",
                nonce=self.auth_info["nonce"],
                username=CONFIG["auth_username"],
                password=CONFIG["auth_password"],
                realm=self.auth_info["realm"]
            )
        
        invite = self.build_sip_invite(offer.sdp, proxy_auth)
        await self.websocket.send(invite)
        logger.debug("PASS: SIP INVITE sent:\n%s", invite)

    async def receive_sip_response(self):
        """Принимает SIP ответы до получения финального ответа (2xx, 4xx, 5xx)"""
        while True:
            response = await self.websocket.recv()
            parsed = self.parse_sip_response(response)
            logger.debug("INFO: Received SIP response: %d", parsed['status_code'])
            
            # Пропускаем промежуточные ответы (1xx)
            if 100 <= parsed['status_code'] < 200:
                continue
            
            # Обрабатываем 407 Proxy Authentication Required
            if parsed['status_code'] == 407:
                await self.handle_407_response(response)
                return "AUTH_REQUIRED"
            
            # Обрабатываем 200 OK
            if parsed['status_code'] == 200:
                # Извлекаем To tag
                to_header = parsed['headers'].get('To', '')
                tag_match = re.search(r';tag=([^\s;]+)', to_header)
                if tag_match:
                    self.to_tag = tag_match.group(1)
                
                if parsed['body']:
                    # Улучшенная обработка SDP
                    fixed_sdp = self.fix_sdp_format(parsed['body'])
                    logger.debug("PASS: Fixed SDP:\n%s", fixed_sdp)
                    if not self.validate_sdp(fixed_sdp):
                        raise Exception("Invalid SDP format after fixing")
                    return RTCSessionDescription(sdp=fixed_sdp, type="answer")
                raise Exception("200 OK without SDP body")
            
            # Обрабатываем ошибки (4xx, 5xx)
            if parsed['status_code'] >= 400:
                logger.debug("FAIL: SIP error response: %d", parsed['status_code'])
                raise Exception(f"SIP error response: {parsed['status_code']}")
    
    def fix_sdp_format(self, sdp_text):
        """Исправление SDP с учетом требований aiortc к RTCP"""
        lines = []
        rtcp_mux_found = False
        rtcp_lines = []
    
        for line in sdp_text.splitlines():
            line = line.strip()
            if not line:
                continue
    
            # Обработка rtcp-mux
            if line.lower() == 'a=rtcp-mux':
                rtcp_mux_found = True
                continue
                
            # Сохраняем оригинальные rtcp атрибуты для последующей обработки
            if line.lower().startswith('a=rtcp:'):
                rtcp_lines.append(line)
                continue
                
            lines.append(line)
    
        # Добавляем rtcp-mux в правильном формате
        if rtcp_mux_found:
            lines.append('a=rtcp-mux')
    
        # Обрабатываем rtcp атрибуты в соответствии с требованиями aiortc
        for rtcp_line in rtcp_lines:
            if 'IN IP4' in rtcp_line:
                lines.append(rtcp_line)
    
        # Проверяем обязательные поля
        mandatory_fields = {
            'v': '0',
            'o': f'- {uuid.uuid4().hex[:10]} 0 IN IP4 0.0.0.0',
            's': '-',
            't': '0 0',
            'c': 'IN IP4 0.0.0.0'
        }
        
        present_fields = set()
        for line in lines:
            if '=' in line:
                present_fields.add(line[0])
    
        # Добавляем отсутствующие обязательные поля
        result = []
        for field in ['v', 'o', 's', 't', 'c']:
            if field not in present_fields:
                result.append(f"{field}={mandatory_fields[field]}")
        
        result.extend(lines)
        
        return '\r\n'.join(result)
    
    def validate_sdp(self, sdp_text):
        """Проверка SDP с акцентом на RTCP атрибуты"""
        has_rtcp_mux = False
        has_valid_media = False
        
        for line in sdp_text.splitlines():
            line = line.strip()
            
            if line.lower() == 'a=rtcp-mux':
                has_rtcp_mux = True
                
            if line.startswith('m='):
                parts = line[2:].split()
                if len(parts) >= 4:
                    has_valid_media = True
        
        return has_rtcp_mux and has_valid_media

    async def handle_incoming_messages(self):
        """Обработка входящих сообщений, включая BYE"""
        try:
            while True:
                message = await asyncio.wait_for(
                    self.websocket.recv(),
                    timeout=CONFIG["bye_timeout"]
                )
                
                if "BYE" in message.split()[0]:
                    logger.debug("PASS: Received BYE message, sending 200 OK")
                    # Формируем ответ 200 OK на BYE
                    ok_response = (
                        "SIP/2.0 200 OK\r\n"
                        "Via: {}\r\n"
                        "From: <sip:{}>;tag={}\r\n"
                        "To: <sip:{}>;tag={}\r\n"
                        "Call-ID: {}\r\n"
                        "CSeq: {} BYE\r\n"
                        "Content-Length: 0\r\n\r\n"
                    ).format(
                        message.split('\r\n')[0].split(' ', 1)[1],  # Via из BYE
                        CONFIG['caller'],
                        self.from_tag,
                        CONFIG['callee'],
                        self.to_tag,
                        self.call_id,
                        self.cseq
                    )
                    await self.websocket.send(ok_response)
                    return True
        except asyncio.TimeoutError:
            return False
    
    async def run_test(self):
        if not await self.signaling_connect():
            raise Exception("Signaling connection failed")
    
        try:
            self.pc.addTrack(FakeAudioTrack())
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)
            
            attempts = 0
            while attempts < CONFIG["max_attempts"]:
                attempts += 1
                await self.send_sip_invite(offer)
                
                try:
                    result = await asyncio.wait_for(self.receive_sip_response(), 30)
                    
                    if result == "AUTH_REQUIRED":
                        continue
                    
                    if isinstance(result, RTCSessionDescription):
                        try:
                            logger.debug("PASS: Setting remote description with SDP:\n%s", result.sdp)
                            await self.pc.setRemoteDescription(result)
                            ack = self.build_sip_ack()
                            await self.websocket.send(ack)
                            
                            # Ожидаем завершение звонка (BYE или таймаут)
                            while True:
                                try:
                                    # Ожидаем входящие сообщения
                                    message = await asyncio.wait_for(
                                        self.websocket.recv(),
                                        CONFIG["call_duration"]
                                    )
                                    
                                    # Проверяем, не BYE ли это
                                    if message.startswith("BYE"):
                                        await self.handle_bye(message)
                                        return
                                        
                                except asyncio.TimeoutError:
                                    # Если BYE не пришел, завершаем звонок сами
                                    logger.info("PASS: No BYE received, ending call")
                                    bye = self.build_sip_bye()
                                    await self.websocket.send(bye)
                                    return
                                
                        except ValueError as e:
                            logger.error("FAIL: SDP parsing failed: %s", str(e))
                            raise Exception(f"Invalid SDP format: {str(e)}") from e
                            
                except asyncio.TimeoutError:
                    logger.warning("FAIL: Timeout waiting for response, attempt %d/%d", 
                                 attempts, CONFIG["max_attempts"])
                    continue
                    
            raise Exception("Max attempts reached")
        except Exception as e:
            logger.error("FAIL: Test failed: %s", str(e), exc_info=True)
            raise
        finally:
            await self.close_connections()
    
    async def close_connections(self):
        """Корректно закрывает все соединения"""
        try:
            await self.pc.close()
        except Exception as e:
            logger.warning("FAIL: Error closing peer connection: %s", str(e))
        
        if hasattr(self, 'websocket') and self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.warning("FAIL: Error closing websocket: %s", str(e))

async def main():
    tester = WebRTCTester()
    try:
        await tester.run_test()
        logger.info("PASS: Test completed successfully")
    except Exception as e:
        logger.error("FAIL: Test failed: %s", str(e), exc_info=True)
    finally:
        await tester.close_connections()

if __name__ == "__main__":
    asyncio.run(main())
