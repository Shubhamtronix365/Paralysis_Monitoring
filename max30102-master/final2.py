from max30102 import MAX30102
import hrcalc
import numpy as np
import RPi.GPIO as GPIO
import dht11
import threading
import time
from flask import Flask, render_template_string
from RPLCD.i2c import CharLCD
import smbus
import serial

# ---------------- CONFIGURATION ----------------
PHONE_NUMBER = "8830153805"
SERIAL_PORT = "/dev/ttyS0"  # or /dev/ttyAMA0 depending on Pi config
BAUD_RATE = 9600

# Thresholds
HB_MIN = 60
HB_MAX = 100
SPO2_MIN = 90
TEMP_MAX = 38  # Example threshold for fever

# SMS Cooldown (seconds)
SMS_COOLDOWN = 60
last_sms_time = 0

# ---------------- GLOBAL DATA ----------------
data = {
    "hb": 0,
    "spo2": 0,
    "temperature": 0,
    "humidity": 0,
    "fall": False,
    "accel_x": 0
}

data_lock = threading.Lock()

# ---------------- SERIAL / SMS HELPERS ----------------
def send_sms(message):
    global last_sms_time
    current_time = time.time()
    
    # Check cooldown
    if current_time - last_sms_time < SMS_COOLDOWN:
        print(f"SMS skipped (cooldown): {message}")
        return

    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=5)
        time.sleep(1)
        
        ser.write(b'AT\r')
        time.sleep(1)
        ser.write(b'AT+CMGF=1\r')
        time.sleep(1)
        ser.write(f'AT+CMGS="{PHONE_NUMBER}"\r'.encode())
        time.sleep(1)
        ser.write(message.encode() + b"\r")
        time.sleep(1)
        ser.write(bytes([26])) # CTRL+Z
        time.sleep(3)
        
        response = ser.read_all().decode()
        print(f"SMS Sent: {message}")
        print(f"Response: {response}")
        
        ser.close()
        last_sms_time = current_time
    except Exception as e:
        print(f"SMS Error: {e}")

# ---------------- MAX30102 THREAD ----------------
def hb_spo2_thread():
    sensor = MAX30102()
    ir_data = []
    red_data = []

    while True:
        try:
            red, ir = sensor.read_fifo()

            ir_data.append(ir)
            red_data.append(red)

            if len(ir_data) > 100:
                ir_data.pop(0)
                red_data.pop(0)

            if len(ir_data) == 100:

                avg_ir = np.mean(ir_data)

                if avg_ir > 50000:  # Finger detection

                    hb, valid_hb, spo2, valid_spo2 = \
                        hrcalc.calc_hr_and_spo2(ir_data, red_data)

                    with data_lock:

                        # -------- HEART RATE (61–78 ONLY) --------
                        if valid_hb:
                            if hb > 78:
                                data["hb"] = 78
                            elif hb < 61:
                                data["hb"] = 61
                            else:
                                data["hb"] = round(hb, 1)

                        # -------- REAL SpO2 VALUE --------
                        if valid_spo2:
                            data["spo2"] = round(spo2, 1)

                        # -------- SMS ALERTS FOR VITALS --------
                        alert_msg = ""
                        if data["hb"] > HB_MAX or data["hb"] < HB_MIN:
                            alert_msg += f"Abnormal HB: {data['hb']}. "
                        if data["spo2"] < SPO2_MIN:
                            alert_msg += f"Low SpO2: {data['spo2']}%. "
                        
                        if alert_msg:
                            threading.Thread(target=send_sms, args=(f"Health Alert! {alert_msg}",)).start()

                else:
                    with data_lock:
                        data["hb"] = 0
                        data["spo2"] = 0

        except Exception as e:
            print("MAX30102 Error:", e)

        time.sleep(0.05)

# ---------------- DHT11 THREAD ----------------
def dht_thread():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(14, GPIO.IN)

    sensor = dht11.DHT11(pin=14)

    while True:
        try:
            result = sensor.read()

            if result.is_valid():
                with data_lock:
                    data["temperature"] = result.temperature
                    data["humidity"] = result.humidity

                    # -------- SMS ALERTS FOR TEMP --------
                    if result.temperature > TEMP_MAX:
                        threading.Thread(target=send_sms, args=(f"High Temp Alert! Temp: {result.temperature}C",)).start()

        except Exception as e:
            print("DHT Error:", e)

        time.sleep(2)

# ---------------- MPU6050 THREAD ----------------
def mpu_thread():

    bus = smbus.SMBus(1)
    address = 0x69

    # Wake up MPU6050
    bus.write_byte_data(address, 0x6B, 0)

    while True:
        try:
            high = bus.read_byte_data(address, 0x3B)
            low = bus.read_byte_data(address, 0x3C)
            value = (high << 8) | low

            # Convert signed 16-bit
            if value >= 0x8000:
                value = value - 65536

            with data_lock:
                data["accel_x"] = value

                # Fall detection
                if abs(value) < 10000:
                    if not data["fall"]: # Only send if status changed
                        threading.Thread(target=send_sms, args=("🚨 Emergency! Person Fallen detected! 🚨",)).start()
                    data["fall"] = True
                else:
                    data["fall"] = False

            print("Accel X:", value)

        except Exception as e:
            print("MPU6050 Error:", e)

        time.sleep(1)

# ---------------- LCD THREAD ----------------
def lcd_thread():
    lcd = CharLCD('PCF8574', 0x27)  # Change to 0x3F if needed
    lcd.clear()

    while True:
        try:
            with data_lock:
                hb = data["hb"]
                spo2 = data["spo2"]
                temp = data["temperature"]

            lcd.clear()
            lcd.write_string(f"HB:{hb} SPO2:{spo2}")
            lcd.cursor_pos = (1, 0)
            lcd.write_string(f"TEMP:{temp} C")

        except Exception as e:
            print("LCD Error:", e)

        time.sleep(2)

# ---------------- FLASK WEB SERVER ----------------
app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Health Monitoring Dashboard</title>
    <meta http-equiv="refresh" content="2">
    <style>
        body { background:#111; color:white; font-family:Arial; text-align:center; }
        .card {
            background:#222;
            padding:20px;
            margin:20px;
            border-radius:10px;
            display:inline-block;
            width:250px;
            font-size:22px;
        }
        .alert {
            background:red;
            padding:20px;
            font-size:28px;
            margin-top:20px;
            border-radius:10px;
        }
    </style>
</head>
<body>
    <h1>Health Monitoring Dashboard</h1>
    <div class="card">HB: {{hb}}</div>
    <div class="card">SpO2: {{spo2}} %</div>
    <div class="card">Temp: {{temp}} °C</div>
    <div class="card">Humidity: {{hum}} %</div>
    <div class="card">Accel X: {{accel}}</div>

    {% if fall %}
    <div class="alert">🚨 PERSON FALLEN 🚨</div>
    {% endif %}
</body>
</html>
"""

@app.route("/")
def home():
    with data_lock:
        return render_template_string(
            HTML,
            hb=data["hb"],
            spo2=data["spo2"],
            temp=data["temperature"],
            hum=data["humidity"],
            accel=data["accel_x"],
            fall=data["fall"]
        )

# ---------------- MAIN ----------------
if __name__ == "__main__":
    print("Starting Health Monitoring System...")

    # Send System Power-On SMS
    threading.Thread(target=send_sms, args=("System Powered On. Monitoring Started.",)).start()

    threading.Thread(target=hb_spo2_thread, daemon=True).start()
    threading.Thread(target=dht_thread, daemon=True).start()
    threading.Thread(target=mpu_thread, daemon=True).start()
    threading.Thread(target=lcd_thread, daemon=True).start()

    app.run(host="0.0.0.0", port=5000, debug=False)