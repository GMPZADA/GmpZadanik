import requests
import time

URL = "https://gmpzadanik-nljw.onrender.com/"

while True:
    try:
        requests.get(URL, timeout=10)
        print("✅ Autoping отправлен")
    except Exception as e:
        print("❌ Autoping ошибка:", e)

    time.sleep(300)  # 5 минут
