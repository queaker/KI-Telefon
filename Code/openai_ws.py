import base64
import json
import queue
import socket
import ssl
import threading
import time
import wave
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import socks
import websocket

from config import OPENAI_API_KEY
from gespraechspartner import personen_info

# Proxy aktivieren
socket.socket = socks.socksocket

DEFAULT_MODEL = "gpt-realtime-mini"
API_KEY = OPENAI_API_KEY


def model_for_role(role_ref) -> str:
    """Liefert das Realtime-Modell für die aktuelle Rolle.

    Rollen können in roles.py optional ein Feld `model` setzen.
    Fehlt dieses Feld, wird DEFAULT_MODEL verwendet.
    """
    try:
        role = role_ref[0]
        return role.get("model", DEFAULT_MODEL)
    except Exception:
        return DEFAULT_MODEL


def ws_url_for_model(model: str) -> str:
    return f"wss://api.openai.com/v1/realtime?model={model}"

# Audio: PCM16, mono, 24 kHz
AUDIO_RATE = 24000
AUDIO_MIME = "audio/pcm"
REENGAGE_DELAY_MS = 500  # Mikrofon-Verzögerung


class RealtimeState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    SESSION_CREATED = "session_created"
    SESSION_CONFIGURED = "session_configured"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    ASSISTANT_RESPONDING = "assistant_responding"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"


@dataclass
class RealtimeMachine:
    """Explizite State-Machine für die Realtime-WebSocket-Session."""

    state: RealtimeState = RealtimeState.DISCONNECTED
    last_assistant_item_id: Optional[str] = None
    last_error: Optional[Dict[str, Any]] = None
    seen_session_update: bool = False
    transcript_fragments: List[str] = field(default_factory=list)

    def transition(self, new_state: RealtimeState, reason: str = "") -> None:
        if self.state == new_state:
            return
        print(f"Realtime state: {self.state.value} -> {new_state.value}" + (f" ({reason})" if reason else ""))
        self.state = new_state

    def on_server_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type", "")

        if event_type == "session.created":
            self.transition(RealtimeState.SESSION_CREATED, event_type)

        elif event_type == "session.updated":
            self.seen_session_update = True
            self.transition(RealtimeState.SESSION_CONFIGURED, event_type)
            self.transition(RealtimeState.LISTENING, "ready")

        elif event_type == "input_audio_buffer.speech_started":
            self.transition(RealtimeState.USER_SPEAKING, event_type)

        elif event_type in {"input_audio_buffer.speech_stopped", "input_audio_buffer.committed"}:
            # Bei Server-VAD wird die Antwort typischerweise automatisch erstellt.
            self.transition(RealtimeState.LISTENING, event_type)

        elif event_type in {"response.created", "response.output_item.added", "response.output_item.created"}:
            item = event.get("item") or {}
            if isinstance(item, dict):
                self.last_assistant_item_id = item.get("id") or self.last_assistant_item_id
            self.transition(RealtimeState.ASSISTANT_RESPONDING, event_type)

        elif event_type in {"response.done", "response.output_audio.done"}:
            self.transition(RealtimeState.LISTENING, event_type)

        elif event_type == "error":
            self.last_error = event.get("error", event)
            self.transition(RealtimeState.ERROR, event_type)

    @property
    def is_ready_for_audio(self) -> bool:
        return self.state in {
            RealtimeState.SESSION_CONFIGURED,
            RealtimeState.LISTENING,
            RealtimeState.USER_SPEAKING,
            RealtimeState.ASSISTANT_RESPONDING,
        }


# ---------------------------------------------------------------------------
# Message-Builder: alle Client -> OpenAI Events an einer Stelle
# ---------------------------------------------------------------------------

def msg_input_audio_append(raw_pcm16: bytes) -> Dict[str, Any]:
    return {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(raw_pcm16).decode("ascii"),
    }


def msg_input_audio_commit() -> Dict[str, Any]:
    return {"type": "input_audio_buffer.commit"}


def msg_response_create() -> Dict[str, Any]:
    return {"type": "response.create"}


def msg_response_cancel() -> Dict[str, Any]:
    return {"type": "response.cancel"}


def msg_conversation_item_truncate(item_id: str, audio_end_ms: int, content_index: int = 0) -> Dict[str, Any]:
    return {
        "type": "conversation.item.truncate",
        "item_id": item_id,
        "content_index": content_index,
        "audio_end_ms": audio_end_ms,
    }


def build_instructions(gespraechspartner_ref, role_ref) -> str:
    gespraechspartner = gespraechspartner_ref[0]
    role = role_ref[0]

    extra_info = ""
    if gespraechspartner:
        extra_info = (
            f"Der Gesprächspartner heißt {gespraechspartner['name']}. "
            f"Er/Sie ist {gespraechspartner['alter']} Jahre alt, arbeitet als {gespraechspartner['beruf']} "
            f"und hat als Hobby {gespraechspartner['hobby']}. "
        )

    return f"{extra_info}Du bist {role['gpt_style']}"


def msg_session_update(gespraechspartner_ref, role_ref) -> Dict[str, Any]:
    """
    GA-Shape der Realtime API:
    - session.type ist explizit "realtime"
    - model liegt in der Session
    - output_modalities ersetzt modalities
    - audio.input/audio.output ersetzt die alten flachen Audio-Felder
    - temperature ist in GA kein Session-Parameter mehr
    """
    role = role_ref[0]
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": model_for_role(role_ref),
            "instructions": build_instructions(gespraechspartner_ref, role_ref),
            "output_modalities": ["audio"],
            "max_output_tokens": 4096,
            "audio": {
                "input": {
                    "format": {
                        "type": AUDIO_MIME,
                        "rate": AUDIO_RATE,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        # Optional: Modell fragt nach, wenn lange nichts kommt.
                        # "idle_timeout_ms": 6000,
                    },
                    # GA-Form für Input-Transkription in der neuen Audio-Struktur.
                    # Falls dein Account/Endpoint diese verschachtelte Form noch nicht akzeptiert,
                    # als Fallback top-level `input_audio_transcription` verwenden.
                    "transcription": {
                        "model": "whisper-1",
                    },
                },
                "output": {
                    "format": {
                        "type": AUDIO_MIME,
                        "rate": AUDIO_RATE,
                    },
                    "voice": role["voice_id"],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# WebSocket-IO
# ---------------------------------------------------------------------------

def ws_send_json(ws, payload: Dict[str, Any]) -> None:
    ws.send(json.dumps(payload, ensure_ascii=False))


def create_connection_with_ipv4(*args, **kwargs):
    """WebSocket-Verbindung erzwingen über IPv4."""
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=socket.AF_INET, *args):
        return original_getaddrinfo(host, port, socket.AF_INET, *args)

    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        return websocket.create_connection(*args, **kwargs)
    finally:
        socket.getaddrinfo = original_getaddrinfo


def send_mic_audio_to_websocket(ws, mic_queue, stop_event, machine: RealtimeMachine):
    """Mikrofondaten an OpenAI WebSocket senden."""
    try:
        while not stop_event.is_set():
            try:
                mic_chunk = mic_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if not machine.is_ready_for_audio:
                continue

            try:
                ws_send_json(ws, msg_input_audio_append(mic_chunk))
            except Exception as e:
                print(f"Fehler beim Senden von Mikrofon-Audio: {e}")
                machine.transition(RealtimeState.ERROR, "send_mic_audio")
                stop_event.set()
                break

    except Exception as e:
        print(f"Mikrofon-Thread-Fehler: {e}")
        machine.transition(RealtimeState.ERROR, "mic_thread")
    finally:
        print("Mikrofon-Sende-Thread beendet")


def maybe_update_person_from_transcript(ws, transcript: str, gespraechspartner_ref, role_ref):
    if not transcript or gespraechspartner_ref[0]:
        return

    for name in personen_info.keys():
        if name.lower() in transcript.lower():
            gespraechspartner_ref[0] = {
                "name": name,
                **personen_info[name],
            }
            print(f"Gesprächspartner erkannt: {gespraechspartner_ref[0]}")
            send_fc_session_update(ws, gespraechspartner_ref, role_ref)
            break


def receive_audio_from_websocket(ws, audio_buffer, audio_lock, stop_event, gespraechspartner_ref, role_ref, machine: RealtimeMachine):
    """Audio und Events vom OpenAI WebSocket empfangen."""
    try:
        while not stop_event.is_set():
            message = ws.recv()
            if not message:
                continue

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("Ungültige JSON-Nachricht empfangen")
                continue

            event_type = data.get("type", "")
            machine.on_server_event(data)

            if event_type == "session.created":
                # Ein einziges initiales Update reicht. Nicht zusätzlich im Hauptthread senden.
                send_fc_session_update(ws, gespraechspartner_ref, role_ref)

            elif event_type in {"response.output_audio.delta", "response.audio.delta"}:
                # `response.output_audio.delta` ist GA. Der zweite Name bleibt als defensive
                # Kompatibilität erhalten, falls ein Endpoint noch Beta-Events liefert.
                audio_chunk = base64.b64decode(data["delta"])
                with audio_lock:
                    audio_buffer.extend(audio_chunk)

            elif event_type == "input_audio_buffer.speech_started":
                # User-Barge-in: lokale Wiedergabe sofort stoppen/leeren.
                with audio_lock:
                    audio_buffer.clear()

            elif event_type in {
                "conversation.item.input_audio_transcription.completed",
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            }:
                # input_audio_transcription.completed = User-Input-Transkript.
                # output_audio_transcript.done = Assistant-Output-Transkript.
                transcript = data.get("transcript", "")
                if transcript:
                    print(f"Transkript: {transcript}")
                    maybe_update_person_from_transcript(ws, transcript, gespraechspartner_ref, role_ref)

            elif event_type == "error":
                print(f"Realtime-API-Fehler: {json.dumps(data.get('error', data), ensure_ascii=False)}")

    except Exception as e:
        print(f"Empfangs-Thread-Fehler: {e}")
        machine.transition(RealtimeState.ERROR, "receive_thread")
    finally:
        print("Empfangs-Thread beendet")


def send_fc_session_update(ws, gespraechspartner_ref, role_ref):
    """Session-Parameter an OpenAI senden."""
    try:
        ws_send_json(ws, msg_session_update(gespraechspartner_ref, role_ref))
        print("Session-Update gesendet")
    except Exception as e:
        print(f"Session-Update fehlgeschlagen: {e}")


def inject_greeting_audio(ws, wav_path, *, server_vad: bool = True):
    """Schickt WAV-Datei als Input-Audio an KI."""
    try:
        with wave.open(wav_path, "rb") as wf:
            if wf.getsampwidth() != 2:
                raise ValueError("Greeting muss PCM16 (16-bit) sein.")
            if wf.getframerate() != AUDIO_RATE:
                raise ValueError(f"Greeting muss {AUDIO_RATE} Hz haben.")
            if wf.getnchannels() != 1:
                raise ValueError("Greeting muss mono sein.")
            if wf.getcomptype() != "NONE":
                raise ValueError("Greeting darf nicht komprimiert sein.")

            # 100 ms Chunks bei 24 kHz
            chunk_duration = 0.1
            frames_per_chunk = int(AUDIO_RATE * chunk_duration)

            while True:
                data = wf.readframes(frames_per_chunk)
                if not data:
                    break

                ws_send_json(ws, msg_input_audio_append(data))
                time.sleep(chunk_duration)  # Echtzeit simulieren

        # Bei server_vad ist commit/response.create normalerweise nicht nötig.
        # Für injizierte Dateien ist ein expliziter Abschluss aber sinnvoll.
        if not server_vad:
            ws_send_json(ws, msg_input_audio_commit())
            ws_send_json(ws, msg_response_create())
        print("Greeting-Audio an KI gesendet")

    except Exception as e:
        print(f"Konnte Greeting nicht injizieren: {e}")


def connect_to_openai(mic_queue, audio_buffer, audio_lock, stop_event, role, gespraechspartner, greeting=None):
    """
    Startet die Verbindung zu OpenAI und steuert Sende- & Empfangs-Threads.
    greeting -> Wenn String (Pfad zu WAV) gesetzt, wird diese Datei direkt an KI geschickt.
    """
    ws = None
    machine = RealtimeMachine()

    try:
        machine.transition(RealtimeState.CONNECTING, "connect")
        selected_model = model_for_role(role)
        print(f"Realtime-Modell: {selected_model}")

        ws = create_connection_with_ipv4(
            ws_url_for_model(selected_model),
            header=[
                f"Authorization: Bearer {API_KEY}",
                # GA: Kein 'OpenAI-Beta: realtime=v1' Header mehr.
            ],
            sslopt={"cert_reqs": ssl.CERT_NONE},
        )
        machine.transition(RealtimeState.CONNECTED, "websocket_open")
        print("Mit OpenAI WebSocket verbunden")

        recv_thread = threading.Thread(
            target=receive_audio_from_websocket,
            args=(ws, audio_buffer, audio_lock, stop_event, gespraechspartner, role, machine),
            daemon=True,
        )

        send_thread = threading.Thread(
            target=send_mic_audio_to_websocket,
            args=(ws, mic_queue, stop_event, machine),
            daemon=True,
        )

        recv_thread.start()
        send_thread.start()

        # Kein zusätzliches Session-Update hier: Wir warten auf session.created und senden
        # dann genau ein session.update im Empfangs-Thread. Das verhindert doppelte Updates.

        if greeting:
            # Kurze Wartephase, bis session.updated angekommen ist.
            for _ in range(50):
                if machine.seen_session_update:
                    break
                time.sleep(0.1)

            print(f"Starte Greeting (.wav) für KI: {greeting}")
            inject_greeting_audio(ws, greeting)

        while not stop_event.is_set():
            time.sleep(0.1)

        machine.transition(RealtimeState.CLOSING, "stop_event")
        try:
            ws.send_close()
        except Exception:
            pass

        recv_thread.join(timeout=2.0)
        send_thread.join(timeout=2.0)
        machine.transition(RealtimeState.CLOSED, "closed")
        print("Verbindung geschlossen")

    except Exception as e:
        print(f"Verbindung fehlgeschlagen: {e}")
        machine.transition(RealtimeState.ERROR, "connect_to_openai")
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
