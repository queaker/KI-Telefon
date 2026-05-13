import RPi.GPIO as GPIO
import time

# Pin-Definitionen Klingel
IN1 = 17
IN2 = 27
ENA = 22

# Pin für "Abgehoben"-Erkennung (Test mit Jumper auf GND)
PIN_ANSWER = 5  # GPIO-Pin an den Jumper kommt

# Klingel-Parameter
FREQ = 10            # 25 Hz Wechsel
SLAG_TIME     = 1.0 # Dauer eines Schlages in Sekunden
PAUSE_BETWEEN = 1.5 # Pause zwischen Schlägen in Sekunden

PWM_FREQUENCY = 1000
DEAD_TIME = 0.003

pwm = None

def setup():
    global pwm
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(IN1, GPIO.OUT)
    GPIO.setup(IN2, GPIO.OUT)
    GPIO.setup(ENA, GPIO.OUT)

    # Eingang mit Pull-Up → Standard HIGH, Jumper an GND → LOW
    GPIO.setup(PIN_ANSWER, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # Glocke
    #GPIO.output(ENA, GPIO.HIGH)
    if (pwm == None): 
        pwm = GPIO.PWM(ENA, PWM_FREQUENCY)
        pwm.start(0)

def bipolar_wave(duration_s, freq=FREQ):
    global pwm
    """
    Simuliert Wechselstrom für die Klingel.
    Gibt True zurück, wenn während des Schlagens abgehoben wurde.
    """

    GPIO.output(IN1, False)
    GPIO.output(IN2, False)

    period = 1.0 / freq
    half = period / 2.0
    end = time.time() + duration_s
    while time.time() < end:
        if GPIO.input(PIN_ANSWER) == GPIO.HIGH:  # LOW = Jumper an GND
            return True

        pwm.ChangeDutyCycle(0)
        time.sleep(DEAD_TIME)
        GPIO.output(IN1, True)
        GPIO.output(IN2, False)
        pwm.ChangeDutyCycle(50)

        time.sleep(half)

        pwm.ChangeDutyCycle(0)
        time.sleep(DEAD_TIME)
        GPIO.output(IN1, False)
        GPIO.output(IN2, True)
        pwm.ChangeDutyCycle(50)

        time.sleep(half)

    pwm.ChangeDutyCycle(0)
    GPIO.output(IN1, False)
    GPIO.output(IN2, False)
    return False

def ring_until_answer(max_rings=5):
    """
    Klingelt bis zu `max_rings` Mal, prüft währenddessen auf Abheben.
    Gibt True zurück, wenn abgehoben wurde, sonst False.
    """
    try:
        setup()
        for i in range(max_rings):
            if bipolar_wave(SLAG_TIME):
                return True
            time.sleep(PAUSE_BETWEEN)
            # Prüfen auch zwischen den Schlägen
            if GPIO.input(PIN_ANSWER) == GPIO.HIGH:
                return True
        return False
    finally:
        GPIO.output(ENA, False)
        GPIO.output(IN1, False)
        GPIO.output(IN2, False)
        GPIO.cleanup([IN1, IN2, ENA])
