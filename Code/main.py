import queue
import threading
import time
import pyaudio
import RPi.GPIO as GPIO
GPIO.setmode(GPIO.BCM)
import sounddevice as sd
import numpy as np

from roles import choose_role, role as role_list
from openai_ws import connect_to_openai
from bell import ring_until_answer
from handset import setup, is_handset_lifted, wait_for_handset_hangup

# Audio-Parameter
CHUNK_SIZE = 2048
RATE = 24000
FORMAT = pyaudio.paInt16

# Globale Audio-Puffer & Flags
audio_buffer = bytearray()
audio_lock = threading.Lock()
mic_queue = queue.Queue()
stop_event = threading.Event()

# Jitter-Buffer für KI-Ausgabe
# 24 kHz * 2 Bytes = 48.000 Bytes/s
# 12.000 Bytes ≈ 250 ms Puffer
MIN_PLAYBACK_BUFFER_BYTES = 12000
playback_started = False
speaker_underruns = 0

# Wählscheiben-Parameter
PULSE_PIN = 26
GPIO.setup(PULSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
FREQ_425HZ = 425
FREQ_DURATION = 5
NUM_FREIZEICHEN = 3
FREIZEICHEN_DELAY = 0.5

# Auto-Call Paramter
auto_calls_enabled = False
AUTOCALL_DELAY = 30  # Sekunden bis zum nächsten Klingeln, wenn niemand abnimmt
AUTO_CALL_TOGGLE = "AUTO_CALL_TOGGLE"
HANDSET_HANGUP = "HANDSET_HANGUP"

# Mikrofon-Callback
def mic_callback(in_data, frame_count, time_info, status):
    mic_queue.put(in_data)
    return (None, pyaudio.paContinue)

# Lautsprecher-Callback
REENGAGE_DELAY_MS = 500
mic_on_at = 0

def speaker_callback(in_data, frame_count, time_info, status):
    global audio_buffer, mic_on_at, playback_started, speaker_underruns

    bytes_needed = frame_count * 2  # pcm16 mono = 2 Bytes pro Frame

    if status:
        print("PyAudio speaker status:", status)

    with audio_lock:
        buffer_len = len(audio_buffer)

        # Erst losspielen, wenn ein kleiner Vorrat da ist.
        # Dadurch werden kurze Netzwerk-/Threading-Schwankungen abgefangen.
        if not playback_started:
            if buffer_len < MIN_PLAYBACK_BUFFER_BYTES:
                return (b'\x00' * bytes_needed, pyaudio.paContinue)
            playback_started = True

        if buffer_len >= bytes_needed:
            chunk = bytes(audio_buffer[:bytes_needed])
            del audio_buffer[:bytes_needed]
            mic_on_at = time.time() + REENGAGE_DELAY_MS / 1000
        else:
            # Underrun: Puffer leer. Rest mit Stille auffüllen und danach
            # wieder warten, bis MIN_PLAYBACK_BUFFER_BYTES erreicht sind.
            speaker_underruns += 1
            if speaker_underruns % 10 == 0:
                print(f"Speaker underruns: {speaker_underruns}, buffer={buffer_len} bytes")

            chunk = bytes(audio_buffer) + b'\x00' * (bytes_needed - buffer_len)
            audio_buffer.clear()
            playback_started = False

    return (chunk, pyaudio.paContinue)

def monitor_handset(stop_event):
    time.sleep(0.5)
    while not stop_event.is_set():
        if not is_handset_lifted():  # Hörer wurde aufgelegt
            print("Hörer aufgelegt – beende Gespräch...")
            stop_event.set()
            break
        time.sleep(0.05)
        
# --- Non-blocking Freizeichen (425 Hz) ---
dial_tone_stream = None

def start_dial_tone():
    global dial_tone_stream
    if dial_tone_stream is not None:
        return
    fs = RATE
    freq = FREQ_425HZ
    step = 2 * np.pi * freq / fs
    phase = {'phi': 0.0}
    def callback(outdata, frames, time_info, status):
        phi0 = phase['phi']
        t = phi0 + step * np.arange(frames, dtype=np.float32)
        samples = (0.1 * np.sin(t)).astype(np.float32)
        outdata[:] = samples.reshape(-1, 1)
        phase['phi'] = (phi0 + frames * step) % (2 * np.pi)
    dial_tone_stream = sd.OutputStream(
        samplerate=fs,
        channels=1,
        dtype='float32',
        callback=callback
    )
    dial_tone_stream.start()
    print("Freizeichen gestartet.")

def stop_dial_tone():
    global dial_tone_stream
    if dial_tone_stream is not None:
        try:
            dial_tone_stream.stop()
            dial_tone_stream.close()
        finally:
            dial_tone_stream = None
            print("Freizeichen gestoppt.")

def play_425hz(duration=FREQ_DURATION):
    fs = 24000
    t = np.linspace(0, duration, int(fs*duration), endpoint=False)
    signal = 0.1 * np.sin(2 * np.pi * FREQ_425HZ * t)
    sd.play(signal, samplerate=fs)
    sd.wait()

def play_automation_signal(state):
    """Akustische Bestätigung für die Automatik"""
    fs = RATE

    if (state == True):
        sequence = [
            (740,  0.07),
            (0,    0.025),
            (880,  0.07),
            (0,    0.025),
            (988,  0.07),
            (0,    0.04),
            (1175, 0.09),
            (0,    0.04),
            (1568, 0.12),
            (0,    0.06),
            (1175, 0.16),
        ]
    else:
        sequence = [
            (1568, 0.08),
            (0,    0.03),
            (1175, 0.08),
            (0,    0.03),
            (988,  0.08),
            (0,    0.04),
            (740,  0.18),
        ]

    signal_parts = []
    for freq, duration in sequence:
        samples = int(fs * duration)
        if freq == 0:
            signal_parts.append(np.zeros(samples, dtype=np.float32))
        else:
            t = np.linspace(0, duration, samples, endpoint=False)
            signal_parts.append((0.18 * np.sin(2 * np.pi * freq * t)).astype(np.float32))
    sd.play(np.concatenate(signal_parts), samplerate=fs)
    sd.wait()

def read_rotary_wheel(timeout=1.5):
    """
    Liest eine Ziffer von der Wählscheibe und protokolliert Diagnosewerte.

    Die Instrumentierung zählt noch NICHT anders als vorher. Sie schreibt nur
    Timinginformationen zu Roh-Impulsen, akzeptierten Impulsen und verworfenen
    Impulsen, damit man später einen besseren Filter ableiten kann.
    """
    pulse_count = 0
    last_pulse_time = [0.0]
    first_seen = [False]
    dial_tone_stop_requested = [False]
    dial_start = time.monotonic()

    # Aktueller Software-Filter. Zum Messen bewusst unverändert lassen.
    MIN_PULSE_SEPARATION = 0.08

    # Diagnosewerte
    raw_events = []       # alle FALLING callbacks
    accepted_events = []  # Impulse, die den aktuellen Filter passiert haben
    rejected_events = []  # Impulse, die wegen MIN_PULSE_SEPARATION verworfen wurden
    last_raw_time = [None]

    def ms(value_s):
        return value_s * 1000.0

    def fmt_ms(value_s):
        if value_s is None:
            return "----"
        return f"{ms(value_s):7.1f}ms"

    def summarize_intervals(label, events):
        if len(events) < 2:
            print(f"[DIAL SUMMARY] {label}: zu wenige Ereignisse für Intervalle")
            return

        intervals = [events[i]["t"] - events[i - 1]["t"] for i in range(1, len(events))]
        intervals_ms = [ms(v) for v in intervals]
        avg_ms = sum(intervals_ms) / len(intervals_ms)

        print(
            f"[DIAL SUMMARY] {label}: n={len(events)}, "
            f"dt_min={min(intervals_ms):.1f}ms, "
            f"dt_avg={avg_ms:.1f}ms, "
            f"dt_max={max(intervals_ms):.1f}ms, "
            f"intervalle={[round(v, 1) for v in intervals_ms]}"
        )

    print(
        f"[DIAL START] timeout={timeout:.3f}s, "
        f"MIN_PULSE_SEPARATION={MIN_PULSE_SEPARATION:.3f}s"
    )

    def pulse_callback(channel):
        nonlocal pulse_count

        now = time.monotonic()
        gpio_value = GPIO.input(PULSE_PIN)
        since_start = now - dial_start
        dt_raw = None if last_raw_time[0] is None else now - last_raw_time[0]
        dt_accepted = None if last_pulse_time[0] == 0.0 else now - last_pulse_time[0]

        raw_index = len(raw_events) + 1
        raw_event = {
            "idx": raw_index,
            "t": now,
            "since_start": since_start,
            "dt_raw": dt_raw,
            "dt_accepted": dt_accepted,
            "gpio": gpio_value,
        }
        raw_events.append(raw_event)
        last_raw_time[0] = now

        passes_filter = last_pulse_time[0] == 0.0 or (now - last_pulse_time[0]) > MIN_PULSE_SEPARATION

        print(
            f"[DIAL RAW] #{raw_index:02d} "
            f"t=+{ms(since_start):8.1f}ms "
            f"dt_raw={fmt_ms(dt_raw)} "
            f"dt_since_counted={fmt_ms(dt_accepted)} "
            f"gpio={gpio_value} "
            f"decision={'COUNT' if passes_filter else 'REJECT'}"
        )

        if passes_filter:
            pulse_count += 1
            last_pulse_time[0] = now
            accepted_events.append({
                "idx": pulse_count,
                "raw_idx": raw_index,
                "t": now,
                "since_start": since_start,
                "dt_raw": dt_raw,
                "dt_accepted": dt_accepted,
                "gpio": gpio_value,
            })

            if not first_seen[0]:
                first_seen[0] = True
                dial_tone_stop_requested[0] = True
                print("Wählscheibe aktiv – Impulse werden gezählt.")

            print(
                f"Impuls erkannt! Gesamt: {pulse_count} "
                f"(raw=#{raw_index}, t=+{ms(since_start):.1f}ms, "
                f"dt_raw={fmt_ms(dt_raw)}, "
                f"dt_vorheriger_gezählter={fmt_ms(dt_accepted)})"
            )
        else:
            rejected_events.append(raw_event)
            print(
                f"[DIAL REJECT] raw=#{raw_index}, "
                f"dt_vorheriger_gezählter={fmt_ms(dt_accepted)} "
                f"<= {ms(MIN_PULSE_SEPARATION):.1f}ms"
            )

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PULSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    try:
        GPIO.remove_event_detect(PULSE_PIN)
    except Exception:
        pass

    GPIO.add_event_detect(PULSE_PIN, GPIO.FALLING, callback=pulse_callback)

    try:
        while True:

            if dial_tone_stop_requested[0]:
                dial_tone_stop_requested[0] = False
                stop_dial_tone()

            if not is_handset_lifted():
                print("Hörer aufgelegt während der Wahl.")
                print(
                    f"[DIAL ABORT] raw={len(raw_events)}, "
                    f"accepted={len(accepted_events)}, rejected={len(rejected_events)}"
                )
                return HANDSET_HANGUP

            if first_seen[0] and (time.monotonic() - last_pulse_time[0]) > timeout:
                break

            time.sleep(0.005)
    finally:
        try:
            GPIO.remove_event_detect(PULSE_PIN)
        except Exception:
            pass

    print(
        f"[DIAL SUMMARY] raw={len(raw_events)}, "
        f"accepted={len(accepted_events)}, rejected={len(rejected_events)}, "
        f"dauer={ms(time.monotonic() - dial_start):.1f}ms"
    )
    summarize_intervals("raw", raw_events)
    summarize_intervals("accepted", accepted_events)

    if rejected_events:
        rejected_dts = [e["dt_accepted"] for e in rejected_events if e["dt_accepted"] is not None]
        rejected_ms = [ms(v) for v in rejected_dts]
        print(
            f"[DIAL SUMMARY] rejected_dt_since_counted_ms="
            f"{[round(v, 1) for v in rejected_ms]}"
        )

    if pulse_count == 0:
        print("Keine Wahl erkannt.")
        return None

    digit = 0 if pulse_count == 11 else pulse_count - 1
    digit = 0 if pulse_count == 10 else pulse_count # Classic
    print(f"Gewählte Ziffer: {digit} aus pulse_count={pulse_count}")
    return digit

def play_freitone(repeats=2, tone_dur=1.0, pause_dur=4.0, freq=425):
    fs = RATE
    for i in range(repeats):
        if not is_handset_lifted():
            print("Hörer aufgelegt während Freiton.")
            return False

        print(f"Freiton {i+1}/{repeats}: Ton...")
        t = np.linspace(0, tone_dur, int(fs * tone_dur), endpoint=False)
        signal = 0.1 * np.sin(2 * np.pi * freq * t)
        sd.play(signal, samplerate=fs)
        sd.wait()

        if not is_handset_lifted():
            print("Hörer aufgelegt während Freiton.")
            return False

        if i < repeats - 1:
            print("Pause...")
            pause_until = time.time() + pause_dur
            while time.time() < pause_until:
                if not is_handset_lifted():
                    print("Hörer aufgelegt während Freiton-Pause.")
                    return False
                time.sleep(0.05)

    print("Freiton beendet – KI übernimmt nun.")
    return True

# Angepasst
def wait_for_role_selection():
    print("Hörer abheben, um Rolle auszuwählen...")
    while not is_handset_lifted():
        time.sleep(0.05)

    print("Hörer abgehoben – Freizeichen aktiv. Bitte wählen...")
    start_dial_tone()

    role_number = read_rotary_wheel(timeout=1.5)
    print(f"Gewählte Nummer: {role_number}")

    if role_number == HANDSET_HANGUP:
        stop_dial_tone()
        print("Wahl abgebrochen, weil aufgelegt wurde.")
        return None

    if role_number is None:
        stop_dial_tone()
        print("Keine Wahl erkannt.")
        return None

    if role_number == 0:
        print("Automatische eingehende KI-Anrufe werden umgeschaltet.")
        stop_dial_tone()
        return AUTO_CALL_TOGGLE

    print("Teilnehmer wird jetzt angerufen!")
    stop_dial_tone()

    if not play_freitone():
        return None

    return role_number

# Angepasst
def run_conversation(selected_role=None, greeting=False):
    global audio_buffer, mic_queue, stop_event
    stop_event.clear()

    with audio_lock:
        audio_buffer.clear()

    while not mic_queue.empty():
        try:
            mic_queue.get_nowait()
        except queue.Empty:
            break

    p = pyaudio.PyAudio()
    mic_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        input=True,
        stream_callback=mic_callback,
        frames_per_buffer=CHUNK_SIZE
    )
    speaker_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        output=True,
        stream_callback=speaker_callback,
        frames_per_buffer=CHUNK_SIZE
    )
    if selected_role is None:
        role = [choose_role()]
    else:
        if 1 <= selected_role <= len(role_list):
            role = [role_list[selected_role - 1]]
        else:
            print("Ungültige Nummer – wähle zufällig.")
            role = [choose_role()]
    gespraechspartner = [None]
    print(f"Rolle: {role[0]['name']}")
    print(f"Stil: {role[0]['gpt_style']}")
    try:
        mic_stream.start_stream()
        speaker_stream.start_stream()
        monitor_thread = threading.Thread(target=monitor_handset, args=(stop_event,))
        monitor_thread.start()
        connect_to_openai(
            mic_queue,
            audio_buffer,
            audio_lock,
            stop_event,
            role,
            gespraechspartner,
            greeting="/home/queaker/KI-Telefon/Code/greeting.wav" if greeting else None
        )
        monitor_thread.join()
    except KeyboardInterrupt:
        print('Beenden...')
        stop_event.set()
    finally:
        mic_stream.stop_stream()
        mic_stream.close()
        speaker_stream.stop_stream()
        speaker_stream.close()
        p.terminate()
        print('Audio gestoppt – Gespräch beendet.')

def ensure_idle_on_startup():
    """
    Sorgt dafür, dass die State-Machine nur im definierten Zustand startet:
    Hörer muss aufgelegt sein.
    """
    setup()

    if is_handset_lifted():
        print("Hörer ist beim Start bereits abgehoben.")
        print("Bitte Hörer auflegen, um die State-Machine zu initialisieren...")
        wait_for_handset_hangup()
        time.sleep(0.3)

    print("Startzustand OK: Hörer ist aufgelegt.")

def main():
    setup()
    ensure_idle_on_startup()

    auto_calls_enabled = False
    next_ring_at = None

    print("Start-Test: einmal kurz klingeln...")
    ring_until_answer(1)
    print("Start-Test beendet. Automatische Anrufe sind deaktiviert.")

    while True:
        if is_handset_lifted():
            print("Hörer abgehoben – ausgehender Anruf via Dialer.")
            role_number = wait_for_role_selection()

            if role_number == AUTO_CALL_TOGGLE:
                auto_calls_enabled = not auto_calls_enabled

                if auto_calls_enabled:
                    print("Automatische eingehende KI-Anrufe: AKTIVIERT.")
                    play_automation_signal(True)
                    next_ring_at = time.time() + AUTOCALL_DELAY
                else:
                    print("Automatische eingehende KI-Anrufe: DEAKTIVIERT.")
                    play_automation_signal(False)
                    next_ring_at = None

                wait_for_handset_hangup()
                time.sleep(0.3)
                continue

            if role_number is not None:
                run_conversation(selected_role=role_number, greeting=True)
                wait_for_handset_hangup()
                time.sleep(0.3)

            if auto_calls_enabled:
                next_ring_at = time.time() + AUTOCALL_DELAY

            continue

        if auto_calls_enabled and next_ring_at is not None:
            now = time.time()

            if now >= next_ring_at:
                print("Automatik aktiv – starte eingehendes Klingeln.")

                if ring_until_answer(5):
                    print("Abgehoben – KI verbunden (eingehend).")
                    run_conversation(greeting=False)
                    wait_for_handset_hangup()
                    time.sleep(0.3)
                else:
                    print("Niemand hat abgehoben – Wartezeit startet neu.")

                next_ring_at = time.time() + AUTOCALL_DELAY

        time.sleep(0.1)

if __name__ == "__main__":
    main()
