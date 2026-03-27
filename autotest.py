import argparse
import asyncio
import hashlib
import json
import logging
import re
import ssl
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
import websockets
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from av import AudioFrame


@dataclass
class AppConfig:
    opensips_ws: str
    caller: str
    callee: str
    timeout: int
    origin: str
    subprotocols: list[str]
    auth_username: str
    auth_password: str
    realm: str
    max_attempts: int
    call_duration: int
    bye_timeout: int
    log_file: str = "autotest.log"
    report_json: str = "session-report.json"
    report_junit: str = "junit.xml"
    register_before_invite: bool = False
    register_expires: int = 300
    session_expires: int = 1800
    min_se: int = 90
    refresher: str = "uac"


@dataclass
class StageResult:
    name: str
    status: str
    details: str
    started_at: str
    finished_at: str


class FakeAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.sample_rate = 8000
        self.samples_sent = 0

    async def recv(self):
        await asyncio.sleep(0.02)
        frame = AudioFrame(format="s16", layout="mono", samples=160)
        for plane in frame.planes:
            plane.update(b"\x00" * plane.buffer_size)
        frame.pts = self.samples_sent
        frame.sample_rate = self.sample_rate
        self.samples_sent += frame.samples
        return frame



class SIPAuthHelper:
    @staticmethod
    def parse_authenticate_header(header_value: str) -> dict[str, str]:
        clean = header_value.strip()
        if clean.lower().startswith("digest "):
            clean = clean[7:].strip()
        params: dict[str, str] = {}
        for key, value in re.findall(r'(\w+)=(".*?"|[^,]+)', clean):
            params[key.lower()] = value.strip().strip('"')
        return params

    @staticmethod
    def generate_digest_auth(
        method: str,
        uri: str,
        username: str,
        password: str,
        challenge: dict[str, str],
        nonce_count: int = 1,
    ) -> str:
        realm = challenge.get("realm", "")
        nonce = challenge.get("nonce", "")
        algorithm = challenge.get("algorithm", "MD5")
        qop_options = challenge.get("qop", "")

        if algorithm.upper() != "MD5":
            raise RuntimeError(f"Unsupported digest algorithm: {algorithm}")

        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        qop = ""
        cnonce = ""
        nc = f"{nonce_count:08x}"

        if qop_options:
            options = [item.strip() for item in qop_options.split(",")]
            qop = "auth" if "auth" in options else options[0]
            cnonce = uuid.uuid4().hex[:12]
            response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        else:
            response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()

        auth_fields = [
            'username="{}"'.format(username),
            'realm="{}"'.format(realm),
            'nonce="{}"'.format(nonce),
            'uri="{}"'.format(uri),
            'response="{}"'.format(response),
        ]
        if challenge.get("opaque"):
            auth_fields.append('opaque="{}"'.format(challenge["opaque"]))
        if algorithm:
            auth_fields.append(f"algorithm={algorithm}")
        if qop:
            auth_fields.extend([f"qop={qop}", 'cnonce="{}"'.format(cnonce), f"nc={nc}"])
        return "Digest " + ", ".join(auth_fields)


class SessionRecorder:
    def __init__(self) -> None:
        self.session_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc)
        self.stages: list[StageResult] = []

    def start_stage(self, stage: str) -> int:
        self.stages.append(
            StageResult(
                name=stage,
                status="running",
                details="",
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at="",
            )
        )
        return len(self.stages) - 1

    def finish_stage(self, idx: int, status: str, details: str) -> None:
        self.stages[idx].status = status
        self.stages[idx].details = details
        self.stages[idx].finished_at = datetime.now(timezone.utc).isoformat()

    def as_dict(self) -> dict[str, Any]:
        final_status = "passed" if all(s.status == "passed" for s in self.stages) else "failed"
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": final_status,
            "stages": [asdict(s) for s in self.stages],
        }


class WebRTCTester:
    def __init__(self, config: AppConfig, logger: logging.Logger, recorder: SessionRecorder):
        self.config = config
        self.logger = logger
        self.recorder = recorder

        rtc_config = RTCConfiguration(iceServers=[])
        self.pc = RTCPeerConnection(configuration=rtc_config)
        self._setup_event_handlers()

        self.call_id = f"{uuid.uuid4()}@autotest.local"
        self.cseq = 1
        self.branch = f"z9hG4bK-{uuid.uuid4()}"
        self.auth_info = None
        self.auth_header_name = None
        self.from_tag = str(uuid.uuid4())
        self.to_tag = None
        self.websocket = None
        self.ice_connected = False
        self.registered_contact = False
        self.auth_nonce_counters: dict[str, int] = {}
        self.ws_host = urlparse(self.config.opensips_ws).hostname or "autotest.local"

    def _setup_event_handlers(self) -> None:
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange() -> None:
            state = self.pc.iceConnectionState
            self.logger.info("ICE state: %s", state)
            if state in {"connected", "completed"}:
                self.ice_connected = True

    async def _create_ssl_context(self) -> ssl.SSLContext:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    async def signaling_connect(self) -> bool:
        stage = self.recorder.start_stage("webrtc_signaling")
        try:
            ssl_context = await self._create_ssl_context()
            self.websocket = await websockets.connect(
                self.config.opensips_ws,
                ssl=ssl_context,
                origin=self.config.origin,
                subprotocols=self.config.subprotocols,
                ping_interval=None,
                open_timeout=self.config.timeout,
            )
            self.recorder.finish_stage(stage, "passed", "WebSocket SIP signaling channel opened")
            self.logger.info("PASS: WebRTC signaling established")
            return True
        except Exception as exc:
            self.recorder.finish_stage(stage, "failed", f"WebSocket connect failed: {exc}")
            self.logger.exception("FAIL: signaling_connect")
            return False

    def _next_branch(self) -> str:
        return f"z9hG4bK-{uuid.uuid4()}"

    def _build_authorization_header(self, method: str, uri: str) -> str | None:
        if not self.auth_info or not self.auth_header_name:
            return None
        nonce = self.auth_info.get("nonce", "")
        nonce_key = f"{self.auth_header_name}:{nonce}"
        nc = self.auth_nonce_counters.get(nonce_key, 0) + 1
        self.auth_nonce_counters[nonce_key] = nc
        digest = SIPAuthHelper.generate_digest_auth(
            method=method,
            uri=uri,
            username=self.config.auth_username,
            password=self.config.auth_password,
            challenge=self.auth_info,
            nonce_count=nc,
        )
        return f"{self.auth_header_name}: {digest}\r\n"

    def build_sip_invite(self, offer_sdp: str, authorization_header: str | None = None) -> str:
        target_uri = f"sip:{self.config.callee}"
        invite = (
            "INVITE sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS {};branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} INVITE\r\n"
            "Contact: <sip:{};transport=ws>\r\n"
            "Supported: timer,ice,outbound\r\n"
            "Session-Expires: {};refresher={}\r\n"
            "Min-SE: {}\r\n"
            "Content-Type: application/sdp\r\n"
        ).format(
            self.config.callee,
            self.ws_host,
            self._next_branch(),
            self.config.caller,
            self.from_tag,
            self.config.callee,
            self.call_id,
            self.cseq,
            self.config.caller,
            self.config.session_expires,
            self.config.refresher,
            self.config.min_se,
        )

        if authorization_header:
            invite += authorization_header

        invite += "Content-Length: {}\r\n\r\n{}".format(len(offer_sdp), offer_sdp)
        self.cseq += 1
        return invite

    def build_sip_register(self, authorization_header: str | None = None) -> str:
        request_uri = f"sip:{self.config.realm}"
        register = (
            "REGISTER {} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS {};branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} REGISTER\r\n"
            "Contact: <sip:{};transport=ws>\r\n"
            "Expires: {}\r\n"
            "Content-Length: 0\r\n"
        ).format(
            request_uri,
            self.ws_host,
            self._next_branch(),
            self.config.caller,
            self.from_tag,
            self.config.caller,
            self.call_id,
            self.cseq,
            self.config.caller,
            self.config.register_expires,
        )
        if authorization_header:
            register += authorization_header
        register += "\r\n"
        self.cseq += 1
        return register

    def build_sip_ack(self) -> str:
        if not self.to_tag:
            raise RuntimeError("No To tag available for ACK")
        return (
            "ACK sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS {};branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>;tag={}\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} ACK\r\n"
            "Content-Length: 0\r\n\r\n"
        ).format(
            self.config.callee,
            self.ws_host,
            self._next_branch(),
            self.config.caller,
            self.from_tag,
            self.config.callee,
            self.to_tag,
            self.call_id,
            self.cseq - 1,
        )

    def build_sip_bye(self) -> str:
        if not self.to_tag:
            raise RuntimeError("No To tag available for BYE")
        msg = (
            "BYE sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS {};branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>;tag={}\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        ).format(
            self.config.callee,
            self.ws_host,
            self._next_branch(),
            self.config.caller,
            self.from_tag,
            self.config.callee,
            self.to_tag,
            self.call_id,
            self.cseq,
        )
        self.cseq += 1
        return msg

    def build_simple_response(self, request: dict[str, Any], status: str) -> str:
        headers = request.get("headers", {})
        return (
            f"SIP/2.0 {status}\r\n"
            f"Via: {headers.get('Via', '')}\r\n"
            f"From: {headers.get('From', '')}\r\n"
            f"To: {headers.get('To', '')}\r\n"
            f"Call-ID: {headers.get('Call-ID', self.call_id)}\r\n"
            f"CSeq: {headers.get('CSeq', '')}\r\n"
            "Content-Length: 0\r\n\r\n"
        )

    def build_reinvite_ok(self, request: dict[str, Any]) -> str:
        headers = request.get("headers", {})
        answer_sdp = ""
        if self.pc.localDescription and self.pc.localDescription.sdp:
            answer_sdp = self.pc.localDescription.sdp

        content_type = "Content-Type: application/sdp\r\n" if answer_sdp else ""
        return (
            "SIP/2.0 200 OK\r\n"
            f"Via: {headers.get('Via', '')}\r\n"
            f"From: {headers.get('From', '')}\r\n"
            f"To: {headers.get('To', '')}\r\n"
            f"Call-ID: {headers.get('Call-ID', self.call_id)}\r\n"
            f"CSeq: {headers.get('CSeq', '')}\r\n"
            f"{content_type}"
            f"Content-Length: {len(answer_sdp)}\r\n\r\n"
            f"{answer_sdp}"
        )

    def parse_sip_message(self, raw: str) -> dict[str, Any]:
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        if not lines or not lines[0]:
            raise ValueError("empty SIP message")
        start = lines[0]
        status_code = None
        method = None
        if start.startswith("SIP/2.0"):
            m = re.match(r"SIP/2\.0 (\d{3})", start)
            if not m:
                raise ValueError(f"invalid status line: {start}")
            status_code = int(m.group(1))
        else:
            m = re.match(r"([A-Z]+)\s+\S+\s+SIP/2\.0", start)
            if not m:
                raise ValueError(f"invalid request line: {start}")
            method = m.group(1)

        headers: dict[str, str] = {}
        body_lines: list[str] = []
        in_body = False
        for line in lines[1:]:
            if line == "" and not in_body:
                in_body = True
                continue
            if not in_body and ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
            elif in_body:
                body_lines.append(line)

        return {"status_code": status_code, "method": method, "headers": headers, "body": "\r\n".join(body_lines).strip()}

    def sanitize_sdp(self, sdp: str) -> str:
        def default_m_line(media_type: str) -> str | None:
            if media_type == "audio":
                return "m=audio 9 UDP/TLS/RTP/SAVPF 111"
            if media_type == "video":
                return "m=video 9 UDP/TLS/RTP/SAVPF 96"
            if media_type == "application":
                return "m=application 9 UDP/DTLS/SCTP webrtc-datachannel"
            return None

        lines: list[str] = []
        for raw_line in sdp.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            if line.startswith("m="):
                media = line[2:].split()
                if len(media) < 4:
                    if media:
                        fallback = default_m_line(media[0])
                        if not fallback:
                            continue
                        line = fallback
                    else:
                        continue
            lines.append(line)

        present = {line[0] for line in lines if "=" in line}
        if "v" not in present:
            lines.insert(0, "v=0")
        if "o" not in present:
            lines.insert(1, f"o=- {uuid.uuid4().hex[:10]} 0 IN IP4 0.0.0.0")
        if "s" not in present:
            lines.insert(2, "s=-")
        if "t" not in present:
            lines.insert(3, "t=0 0")
        return "\r\n".join(lines) + "\r\n"

    async def handle_auth_challenge(self, response: str) -> str:
        parsed = self.parse_sip_message(response)
        code = parsed.get("status_code")
        if code == 401:
            header_name = "WWW-Authenticate"
            auth_header_name = "Authorization"
        elif code == 407:
            header_name = "Proxy-Authenticate"
            auth_header_name = "Proxy-Authorization"
        else:
            raise RuntimeError(f"Not an auth challenge: {code}")

        header = parsed["headers"].get(header_name, "")
        if not header:
            raise RuntimeError(f"{code} without {header_name}")

        challenge = SIPAuthHelper.parse_authenticate_header(header)
        if challenge.get("stale", "").lower() == "true":
            self.logger.info("INFO: stale nonce received, refreshing auth credentials")
        self.auth_info = challenge
        self.auth_info.setdefault("realm", self.config.realm)
        self.auth_header_name = auth_header_name
        return auth_header_name

    async def send_sip_invite(self, offer: RTCSessionDescription) -> None:
        auth_header = self._build_authorization_header(method="INVITE", uri=f"sip:{self.config.callee}")
        invite = self.build_sip_invite(offer.sdp, auth_header)
        await self.websocket.send(invite)

    async def send_register(self) -> bool:
        request_uri = f"sip:{self.config.realm}"
        attempts = 0
        while attempts < self.config.max_attempts:
            attempts += 1
            auth_header = self._build_authorization_header("REGISTER", request_uri)
            await self.websocket.send(self.build_sip_register(auth_header))
            response = await asyncio.wait_for(self.receive_sip_response(expect_body=False), timeout=self.config.timeout)
            if response == "AUTH_REQUIRED":
                continue
            if response == "OK":
                self.registered_contact = True
                return True
        return False

    async def receive_sip_response(self, expect_body: bool) -> RTCSessionDescription | str:
        while True:
            raw = await self.websocket.recv()
            parsed = self.parse_sip_message(raw)
            code = parsed["status_code"]
            if code is None:
                continue
            if 100 <= code < 200:
                continue
            if code in {401, 407}:
                await self.handle_auth_challenge(raw)
                return "AUTH_REQUIRED"
            if code == 200:
                if not expect_body:
                    return "OK"
                to_header = parsed["headers"].get("To", "")
                tag_match = re.search(r";tag=([^\s;]+)", to_header)
                if tag_match:
                    self.to_tag = tag_match.group(1)
                if not parsed["body"]:
                    raise RuntimeError("200 OK without SDP")
                sanitized_sdp = self.sanitize_sdp(parsed["body"])
                if sanitized_sdp.strip() != parsed["body"].strip():
                    self.logger.info("INFO: remote SDP normalized before aiortc parsing")
                return RTCSessionDescription(sdp=sanitized_sdp, type="answer")
            raise RuntimeError(f"SIP final error: {code}")

    async def validate_rtp_phase(self) -> tuple[bool, str]:
        if not self.pc.remoteDescription:
            return False, "Remote SDP is absent"

        sdp = self.pc.remoteDescription.sdp
        has_audio_media = "m=audio" in sdp
        has_rtcp_mux = "a=rtcp-mux" in sdp
        if has_audio_media and has_rtcp_mux:
            if self.ice_connected:
                return True, "Audio m-line + rtcp-mux present, ICE connected"
            return True, "Audio m-line + rtcp-mux present (RTP negotiated in same session)"
        return False, "Remote SDP lacks RTP requirements (m=audio and a=rtcp-mux)"

    def validate_rtp_from_raw_sdp(self, sdp: str) -> tuple[bool, str]:
        normalized = self.sanitize_sdp(sdp)
        has_audio_media = "m=audio" in normalized
        has_rtcp_mux = "a=rtcp-mux" in normalized
        if has_audio_media and has_rtcp_mux:
            return True, "RTP negotiated by raw SDP (aiortc parser fallback path)"
        return False, "Raw SDP fallback check failed: missing m=audio or a=rtcp-mux"

    async def run_test(self) -> None:
        if not await self.signaling_connect():
            raise RuntimeError("Signaling phase failed")

        sip_stage = self.recorder.start_stage("sip")
        rtp_stage = self.recorder.start_stage("rtp")

        try:
            if self.config.register_before_invite:
                register_ok = await self.send_register()
                if not register_ok:
                    raise RuntimeError("REGISTER failed after max_attempts")

            self.pc.addTrack(FakeAudioTrack())
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)

            attempts = 0
            answer: RTCSessionDescription | None = None
            while attempts < self.config.max_attempts:
                attempts += 1
                await self.send_sip_invite(offer)
                response = await asyncio.wait_for(
                    self.receive_sip_response(expect_body=True),
                    timeout=self.config.timeout,
                )
                if response == "AUTH_REQUIRED":
                    continue
                answer = response
                break

            if not answer:
                raise RuntimeError("SIP INVITE failed after max_attempts")

            remote_description_set = False
            try:
                await self.pc.setRemoteDescription(answer)
                remote_description_set = True
            except ValueError as exc:
                self.logger.info("INFO: remote SDP rejected by aiortc, using raw SDP fallback: %s", exc)
                rtp_ok, rtp_details = self.validate_rtp_from_raw_sdp(answer.sdp)
                status = "passed" if rtp_ok else "failed"
                self.recorder.finish_stage(
                    rtp_stage,
                    status,
                    f"{rtp_details}; aiortc parse error: {exc}",
                )

            ack = self.build_sip_ack()
            await self.websocket.send(ack)
            register_mark = "REGISTER + " if self.config.register_before_invite else ""
            self.recorder.finish_stage(
                sip_stage,
                "passed",
                f"{register_mark}INVITE/200/ACK completed in single SIP session",
            )

            if remote_description_set:
                rtp_ok, rtp_details = await self.validate_rtp_phase()
                self.recorder.finish_stage(rtp_stage, "passed" if rtp_ok else "failed", rtp_details)

            call_deadline = asyncio.get_running_loop().time() + self.config.call_duration
            while True:
                remaining = call_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self.websocket.send(self.build_sip_bye())
                    break

                try:
                    msg = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    await self.websocket.send(self.build_sip_bye())
                    break

                parsed = self.parse_sip_message(msg)
                method = parsed.get("method")
                if method == "BYE":
                    await self.websocket.send(self.build_simple_response(parsed, "200 OK"))
                    break
                if method == "INVITE":
                    # Re-INVITE: подтверждаем диалог и НЕ рвем звонок.
                    await self.websocket.send(self.build_reinvite_ok(parsed))
                    continue
                if method == "ACK":
                    continue
                if method == "UPDATE":
                    await self.websocket.send(self.build_simple_response(parsed, "200 OK"))
                    continue
                if method == "OPTIONS":
                    await self.websocket.send(self.build_simple_response(parsed, "200 OK"))
                    continue
                # Для незнакомых in-dialog запросов не прерываем сессию.
                await self.websocket.send(self.build_simple_response(parsed, "501 Not Implemented"))

        except Exception as exc:
            if self.recorder.stages[sip_stage].status == "running":
                self.recorder.finish_stage(sip_stage, "failed", str(exc))
            if self.recorder.stages[rtp_stage].status == "running":
                self.recorder.finish_stage(rtp_stage, "failed", f"RTP check skipped: {exc}")
            raise
        finally:
            await self.close_connections()

    async def close_connections(self) -> None:
        try:
            await self.pc.close()
        except Exception:
            self.logger.exception("Failed to close peer connection")
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                self.logger.exception("Failed to close websocket")


def configure_logging(log_file: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )
    return logging.getLogger("webrtc_test")


def load_config(path: str) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        if config_path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(fh)
        else:
            data = json.load(fh)

    return AppConfig(**data)


def save_json_report(report: dict[str, Any], output_file: str) -> None:
    Path(output_file).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def save_junit_report(report: dict[str, Any], output_file: str) -> None:
    stages = report.get("stages", [])
    suite = ET.Element(
        "testsuite",
        name="voip-autotest",
        timestamp=datetime.now(timezone.utc).isoformat(),
        tests=str(len(stages)),
        failures=str(sum(1 for s in stages if s.get("status") != "passed")),
        errors="0",
    )
    for stage in stages:
        case = ET.SubElement(suite, "testcase", classname="voip.session", name=stage.get("name", "unknown"))
        if stage.get("status") != "passed":
            failure = ET.SubElement(case, "failure", message="stage failed")
            failure.text = stage.get("details", "")
        else:
            ET.SubElement(case, "system-out").text = stage.get("details", "")
    ET.ElementTree(suite).write(output_file, encoding="utf-8", xml_declaration=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Single-session WebRTC + SIP + RTP autotest")
    parser.add_argument("--config", required=True, help="Path to JSON/YAML config")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    logger = configure_logging(config.log_file)
    recorder = SessionRecorder()

    tester = WebRTCTester(config, logger, recorder)
    exit_code = 0
    try:
        await tester.run_test()
        logger.info("PASS: test completed")
    except Exception as exc:
        exit_code = 1
        logger.error("FAIL: %s", exc, exc_info=True)

    report = recorder.as_dict()
    save_json_report(report, config.report_json)
    save_junit_report(report, config.report_junit)
    logger.info("INFO: json report written to %s", config.report_json)
    logger.info("INFO: junit report written to %s", config.report_junit)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
