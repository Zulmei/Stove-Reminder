#!/usr/bin/env python3

import time
import base64
from email.message import EmailMessage

import serial
import RPi.GPIO as GPIO

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ----------------------
# GPIO SETUP
# ----------------------
BUZZER_PIN = 18
LED_R_PIN = 17
LED_G_PIN = 27
LED_B_PIN = 22

GPIO.setmode(GPIO.BCM)
GPIO.setup(BUZZER_PIN, GPIO.OUT)
GPIO.setup(LED_R_PIN, GPIO.OUT)
GPIO.setup(LED_G_PIN, GPIO.OUT)
GPIO.setup(LED_B_PIN, GPIO.OUT)

# Passive buzzer uses PWM
BUZZER_FREQ = 2000  # Hz
buzzer_pwm = GPIO.PWM(BUZZER_PIN, BUZZER_FREQ)
buzzer_pwm.start(0)  # Buzzer OFF (0% duty cycle)

GPIO.output(LED_R_PIN, GPIO.LOW)
GPIO.output(LED_G_PIN, GPIO.LOW)
GPIO.output(LED_B_PIN, GPIO.LOW)

# ----------------------
# SERIAL SETUP (Arduino)
# ----------------------
SERIAL_PORT = '/dev/ttyACM0'   # change to /dev/ttyUSB0 if needed
BAUD_RATE = 9600

ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
time.sleep(2)  # give Arduino time to initialize

# ----------------------
# THRESHOLDS
# ----------------------
LIGHT_DARK_THRESHOLD = 500      # <500 = dark

TEMP_WARN_THRESHOLD = 68.0      # °F  (approx 20°C)
TEMP_DANGER_THRESHOLD = 72.0    # °F  (your updated danger threshold)

# ----------------------
# GMAIL API / EMAIL-TO-SMS CONFIG
# ----------------------
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# The Gmail account you configured in Google Cloud (must match credentials.json)
GMAIL_SENDER = "your_gmail_address@gmail.com"

# All phone targets via their email-to-SMS gateways
SMS_RECIPIENTS = [
    "9293085058@tmomail.net",   # 929-308-5058 (T-Mobile)
    "9735671650@vtext.com",     # 973-567-1650 (Verizon)
    "9755735713@tmomail.net"    # 975-573-5713 (T-Mobile)
]

# Cooldown so you don't spam everyone every loop
ALERT_COOLDOWN = 60 * 5  # 5 minutes
last_alert_time = 0

gmail_service = None  # will be lazily initialized


def get_gmail_service():
    """Initialize and return an authorized Gmail API service."""
    global gmail_service

    if gmail_service is not None:
        return gmail_service

    creds = None
    # token.json stores the user's access and refresh tokens
    try:
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    except Exception:
        creds = None

    # If there are no (valid) credentials, do OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # credentials.json is from Google Cloud Console
            flow = InstalledAppFlow.from_client_secrets_file
            ("credentials.json", SCOPES
            )
            creds = flow.run_console()

        # Save credentials for next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    gmail_service = build("gmail", "v1", credentials=creds)
    return gmail_service


def send_stove_sms_alert():
    """Send 'YOUR STOVE IS ON' text to all configured recipients via Gmail."""
    global last_alert_time

    now = time.time()
    if now - last_alert_time < ALERT_COOLDOWN:
        print("[INFO] SMS alert suppressed (cooldown active).")
        return

    last_alert_time = now

    try:
        service = get_gmail_service()

        for recipient in SMS_RECIPIENTS:
            msg = EmailMessage()
            msg["To"] = recipient
            msg["From"] = GMAIL_SENDER
            msg["Subject"] = "STOVE ALERT"
            msg.set_content("YOUR STOVE IS ON")

            encoded_message = base64.urlsafe_b64encode(
                msg.as_bytes()
            ).decode("utf-8")
            create_message = {"raw": encoded_message}

            result = (
                service.users()
                .messages()
                .send(userId="me", body=create_message)
                .execute()
            )

            print(
                f"[INFO] SMS alert sent to {recipient}, "
                f"Message ID: {result.get('id')}"
            )

    except HttpError as error:
        print(f"[ERROR] Gmail API error: {error}")
    except Exception as e:
        print(f"[ERROR] Failed to send SMS alerts: {e}")


# ----------------------
# LED + BUZZER HELPERS
# ----------------------
def set_led_color(r: bool, g: bool, b: bool):
    GPIO.output(LED_R_PIN, GPIO.HIGH if r else GPIO.LOW)
    GPIO.output(LED_G_PIN, GPIO.HIGH if g else GPIO.LOW)
    GPIO.output(LED_B_PIN, GPIO.HIGH if b else GPIO.LOW)


def buzzer_on():
    # 50% duty cycle: audible tone on passive buzzer
    buzzer_pwm.ChangeDutyCycle(50)


def buzzer_off():
    buzzer_pwm.ChangeDutyCycle(0)


def set_safe_state():
    buzzer_off()
    set_led_color(r=False, g=True, b=False)   # green


def set_warning_state():
    buzzer_off()
    set_led_color(r=True, g=True, b=False)    # yellow


def set_danger_state():
    buzzer_on()
    set_led_color(r=True, g=False, b=False)   # red


# ----------------------
# PARSE ARDUINO SERIAL
# ----------------------
def parse_line(line: str):
    """
    Parse a line like 'L:512 T:45.3'
    Returns: (light:int or None, tempC:float or None)
    """
    light = None
    temp_c = None

    parts = line.split()
    for p in parts:
        if p.startswith("L:"):
            try:
                light = int(float(p[2:]))
            except ValueError:
                pass
        elif p.startswith("T:"):
            try:
                temp_c = float(p[2:])
            except ValueError:
                pass

    return light, temp_c


# ----------------------
# MAIN LOOP
# ----------------------
def main():
    print("Starting stove safety monitor (passive buzzer + Gmail SMS)...")
    set_safe_state()

    try:
        while True:
            # Read line from Arduino
            try:
                line = ser.readline().decode(errors="ignore").strip()
            except Exception:
                line = ""

            if not line:
                continue

            print(f"[SERIAL] {line}")
            light, temp_c = parse_line(line)

            if light is None or temp_c is None:
                continue

            # Convert Celsius → Fahrenheit
            temp_f = (temp_c * 9.0 / 5.0) + 32.0

            is_dark = light < LIGHT_DARK_THRESHOLD
            is_warning = temp_f >= TEMP_WARN_THRESHOLD
            is_danger = temp_f >= TEMP_DANGER_THRESHOLD

            if is_danger and is_dark:
                print(f"[DANGER] Temp={temp_f:.1f}°F, Light={light} (dark)")
                set_danger_state()
                # Send text alert when in danger with lights off
                send_stove_sms_alert()

            elif is_warning:
                print(f"[WARNING] Temp={temp_f:.1f}°F, Light={light}")
                set_warning_state()

            else:
                print(f"[SAFE] Temp={temp_f:.1f}°F, Light={light}")
                set_safe_state()

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("Exiting...")

    finally:
        buzzer_off()
        buzzer_pwm.stop()
        set_led_color(False, False, False)
        GPIO.cleanup()
        ser.close()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()

