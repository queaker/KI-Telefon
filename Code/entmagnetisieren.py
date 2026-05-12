#!/usr/bin/env python3
import RPi.GPIO as GPIO
import time
import math
import argparse

# BCM-Pins
IN1 = 17
IN2 = 27
ENA = 22

# Sicherheits-/Timing-Werte
PWM_FREQUENCY = 1000      # Hz, PWM auf ENA
DEAD_TIME = 0.003         # Sekunden Pause beim Umpolen
DEFAULT_DURATION = 25.0   # Sekunden
DEFAULT_START_DUTY = 45.0 # Prozent
DEFAULT_FLIP_HZ = 25.0    # Richtungswechsel pro Sekunde


def set_direction(forward: bool):
    """
    Setzt die Richtung der H-Brücke.
    ENA sollte dabei vorher auf 0 gesetzt sein.
    """
    if forward:
        GPIO.output(IN1, GPIO.HIGH)
        GPIO.output(IN2, GPIO.LOW)
    else:
        GPIO.output(IN1, GPIO.LOW)
        GPIO.output(IN2, GPIO.HIGH)


def brake_or_float():
    """
    Spule stromlos schalten.
    Bei den meisten H-Brücken ist IN1=LOW, IN2=LOW ein Freilauf/Stop.
    """
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.LOW)


def demagnetize(duration, start_duty, flip_hz):
    GPIO.setmode(GPIO.BCM)

    GPIO.setup(IN1, GPIO.OUT)
    GPIO.setup(IN2, GPIO.OUT)
    GPIO.setup(ENA, GPIO.OUT)

    brake_or_float()

    pwm = GPIO.PWM(ENA, PWM_FREQUENCY)
    pwm.start(0)

    half_period = 1.0 / (2.0 * flip_hz)
    start_time = time.monotonic()

    direction = True
    cycles = 0

    print("Starte Entmagnetisierung...")
    print(f"Dauer: {duration:.1f}s")
    print(f"Start-PWM: {start_duty:.1f}%")
    print(f"Umpolfrequenz: {flip_hz:.1f} Hz")
    print("Mit Strg+C abbrechen.")
    print()

    try:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= duration:
                break

            progress = elapsed / duration

            # Sanft abfallende Hüllkurve.
            # Anfang stark, am Ende sehr klein.
            duty = start_duty * (1.0 - progress) ** 2

            # Unter sehr kleinen Werten ganz abschalten
            if duty < 0.5:
                duty = 0.0

            # Erst PWM aus, dann Richtung wechseln, dann PWM wieder an.
            pwm.ChangeDutyCycle(0)
            brake_or_float()
            time.sleep(DEAD_TIME)

            set_direction(direction)
            pwm.ChangeDutyCycle(duty)

            time.sleep(max(0.0, half_period - DEAD_TIME))

            direction = not direction
            cycles += 1

            if cycles % int(max(1, flip_hz * 2)) == 0:
                print(f"{elapsed:5.1f}s / {duration:.1f}s   PWM: {duty:5.2f}%")

    finally:
        pwm.ChangeDutyCycle(0)
        brake_or_float()
        time.sleep(0.1)
        pwm.stop()
        GPIO.cleanup()
        print("Fertig. H-Brücke abgeschaltet.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Entmagnetisierung eines Klingel-Schlägels über H-Brücke"
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION,
        help="Dauer in Sekunden, Standard: 25"
    )

    parser.add_argument(
        "--duty",
        type=float,
        default=DEFAULT_START_DUTY,
        help="Start-PWM in Prozent, Standard: 45"
    )

    parser.add_argument(
        "--flip-hz",
        type=float,
        default=DEFAULT_FLIP_HZ,
        help="Umpolfrequenz in Hz, Standard: 25"
    )

    args = parser.parse_args()

    if not 0 < args.duty <= 100:
        raise ValueError("Duty muss zwischen 0 und 100 liegen.")

    if args.duration <= 0:
        raise ValueError("Duration muss größer als 0 sein.")

    if args.flip_hz <= 0:
        raise ValueError("Flip-Hz muss größer als 0 sein.")

    demagnetize(
        duration=args.duration,
        start_duty=args.duty,
        flip_hz=args.flip_hz
    )
