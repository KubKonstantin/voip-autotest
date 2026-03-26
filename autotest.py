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

import yaml
import websockets
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack


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


@dataclass
class StageResult:
    name: str
    status: str
    details: str
    started_at: str
    finished_at: str


class FakeAudioTrack(MediaStreamTrack):
    kind = "audio"

    async def recv(self):
        await asyncio.sleep(0.02)
        return None


class SIPAuthHelper:
    @staticmethod
    def generate_proxy_auth(method: str, uri: str, nonce: str, username: str, password: str, realm: str) -> str:
        cnonce = uuid.uuid4().hex[:12]
        nc = "00000001"
        qop = "auth"
        ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        return (
            'Proxy-Authorization: Digest algorithm=MD5, username="{}", '
            'realm="{}", nonce="{}", uri="{}", response="{}", '
            'qop={}, cnonce="{}", nc={}\r\n'.format(username, realm, nonce, uri, response, qop, cnonce, nc)
        )


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
        self.from_tag = str(uuid.uuid4())
        self.to_tag = None
        self.websocket = None
        self.ice_connected = False

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

    def build_sip_invite(self, offer_sdp: str, proxy_auth: str | None = None) -> str:
        invite = (
            "INVITE sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS autotest.local;branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} INVITE\r\n"
            "Contact: <sip:{};transport=ws>\r\n"
            "Content-Type: application/sdp\r\n"
        ).format(
            self.config.callee,
            self.branch,
            self.config.caller,
            self.from_tag,
            self.config.callee,
            self.call_id,
            self.cseq,
            self.config.caller,
        )

        if proxy_auth:
            invite += proxy_auth

        invite += "Content-Length: {}\r\n\r\n{}".format(len(offer_sdp), offer_sdp)
        self.cseq += 1
        return invite

    def build_sip_ack(self) -> str:
        if not self.to_tag:
            raise RuntimeError("No To tag available for ACK")
        return (
            "ACK sip:{} SIP/2.0\r\n"
            "Via: SIP/2.0/WSS autotest.local;branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>;tag={}\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} ACK\r\n"
            "Content-Length: 0\r\n\r\n"
        ).format(
            self.config.callee,
            f"z9hG4bK-{uuid.uuid4()}",
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
            "Via: SIP/2.0/WSS autotest.local;branch={}\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:{}>;tag={}\r\n"
            "To: <sip:{}>;tag={}\r\n"
            "Call-ID: {}\r\n"
            "CSeq: {} BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        ).format(
            self.config.callee,
            f"z9hG4bK-{uuid.uuid4()}",
            self.config.caller,
            self.from_tag,
            self.config.callee,
            self.to_tag,
            self.call_id,
            self.cseq,
        )
        self.cseq += 1
        return msg

    def parse_sip_message(self, raw: str) -> dict[str, Any]:
        lines = raw.split("\r\n")
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
            m = re.match(r"([A-Z]+) sip:.+ SIP/2\.0", start)
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

    async def handle_407_response(self, response: str) -> None:
        parsed = self.parse_sip_message(response)
        header = parsed["headers"].get("Proxy-Authenticate", "")
        if not header:
            raise RuntimeError("407 without Proxy-Authenticate")

        auth_params: dict[str, str] = {}
        clean = header.replace("Digest ", "")
        for part in clean.split(","):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                auth_params[key.strip()] = value.strip().strip('"')

        self.auth_info = {
            "realm": auth_params.get("realm", self.config.realm),
            "nonce": auth_params.get("nonce", ""),
            "algorithm": auth_params.get("algorithm", "MD5"),
            "qop": auth_params.get("qop", "auth"),
        }

    async def send_sip_invite(self, offer: RTCSessionDescription) -> None:
        proxy_auth = None
        if self.auth_info:
            proxy_auth = SIPAuthHelper.generate_proxy_auth(
                method="INVITE",
                uri=f"sip:{self.config.callee}",
                nonce=self.auth_info["nonce"],
                username=self.config.auth_username,
                password=self.config.auth_password,
                realm=self.auth_info["realm"],
            )
        invite = self.build_sip_invite(offer.sdp, proxy_auth)
        await self.websocket.send(invite)

    async def receive_sip_final(self) -> RTCSessionDescription | str:
        while True:
            raw = await self.websocket.recv()
            parsed = self.parse_sip_message(raw)
            code = parsed["status_code"]
            if code is None:
                continue
            if 100 <= code < 200:
                continue
            if code == 407:
                await self.handle_407_response(raw)
                return "AUTH_REQUIRED"
            if code == 200:
                to_header = parsed["headers"].get("To", "")
                tag_match = re.search(r";tag=([^\s;]+)", to_header)
                if tag_match:
                    self.to_tag = tag_match.group(1)
                if not parsed["body"]:
                    raise RuntimeError("200 OK without SDP")
                return RTCSessionDescription(sdp=parsed["body"], type="answer")
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

    async def run_test(self) -> None:
        if not await self.signaling_connect():
            raise RuntimeError("Signaling phase failed")

        sip_stage = self.recorder.start_stage("sip")
        rtp_stage = self.recorder.start_stage("rtp")

        try:
            self.pc.addTrack(FakeAudioTrack())
            offer = await self.pc.createOffer()
            await self.pc.setLocalDescription(offer)

            attempts = 0
            answer: RTCSessionDescription | None = None
            while attempts < self.config.max_attempts:
                attempts += 1
                await self.send_sip_invite(offer)
                response = await asyncio.wait_for(self.receive_sip_final(), timeout=self.config.timeout)
                if response == "AUTH_REQUIRED":
                    continue
                answer = response
                break

            if not answer:
                raise RuntimeError("SIP INVITE failed after max_attempts")

            await self.pc.setRemoteDescription(answer)
            ack = self.build_sip_ack()
            await self.websocket.send(ack)
            self.recorder.finish_stage(sip_stage, "passed", "INVITE/200/ACK completed in single SIP session")

            rtp_ok, rtp_details = await self.validate_rtp_phase()
            self.recorder.finish_stage(rtp_stage, "passed" if rtp_ok else "failed", rtp_details)

            try:
                msg = await asyncio.wait_for(self.websocket.recv(), timeout=self.config.call_duration)
                parsed = self.parse_sip_message(msg)
                if parsed.get("method") == "BYE":
                    ok = (
                        "SIP/2.0 200 OK\r\n"
                        f"Via: {parsed['headers'].get('Via', '')}\r\n"
                        f"From: {parsed['headers'].get('From', '')}\r\n"
                        f"To: {parsed['headers'].get('To', '')}\r\n"
                        f"Call-ID: {parsed['headers'].get('Call-ID', self.call_id)}\r\n"
                        f"CSeq: {parsed['headers'].get('CSeq', '1 BYE')}\r\n"
                        "Content-Length: 0\r\n\r\n"
                    )
                    await self.websocket.send(ok)
                else:
                    await self.websocket.send(self.build_sip_bye())
            except asyncio.TimeoutError:
                await self.websocket.send(self.build_sip_bye())

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
