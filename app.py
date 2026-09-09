import time
import threading
import requests
import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "eRepublik Detaylı Analiz Botu Aktif!"

def bot_loop():
    TOKEN = "8704453687:AAHrKY4bVuT0RaOtWoUcwlKxT_shuKqXO3Q"
    CHAT_ID = "8680653965"
    TG = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    URL = "https://www.erepublik.com/tr/military/campaigns-new"

    USER_COOKIE = "l_chathwe=1; _fbp=fb.1.1788877195502.705633333321429601; erpk_mid=8f53ae78391e7a2011a28e925516a496; erpk_rm=d3c6a76d5553de16635bc55bd45485da; erpk_plang=tr; _ga=GA1.1.874820192.1788877198; lastRegionId=647; erpk=2c0da60a899744a4eb979ce58f4cd821; erpk_auth=1; cf_clearance=.GJMZbjttyk3feFdc.Pve0VH5DKLnr9zpL5FCndMef8-1788959319-1.2.1.1-QwhO7DcwsBoRLLcPM9Q4S0grcsYSNJ6ihkA.NTe95jwpWyTbGsmgFpBXklCPKQHCsARVEZIvpzl8_.eYpcTEIukDJ92XRmUW.s35kW_g0EHtZw54huFU3o6Aw_GIxCEPhurL6ySJ0NphyRN_m1XmZzfP6SS2g.Fvg5DeRJgZOKQFjk6TDbd32BhwqplyCMse1FusrxGGrerE0wfco2b8BflMN3wWFuW1ZfpTNnXM0MJSi7YJp3hEcsDa2f0zt5HI0dxDCINeJ_yID5z3U5JQb_Fz5Rmai_JALtVehBfO54D4HNZ8aHdk7PfYZ5UwKfuH.0VTqy0LevkFsE42sV74xhtF9H6bVjQH2GXJCfGeIiI; _ga_PSSBE951PK=GS2.1.s1788959317$o7$g1$t1788959771$j17$l0$h0"

    HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": USER_COOKIE,
        "X-Requested-With": "XMLHttpRequest"
    }

    debug_sent = False

    try:
        requests.post(TG, json={"chat_id": CHAT_ID, "text": "🔍 *Temiz Analiz Modu Başlatıldı!*", "parse_mode": "Markdown"})
    except:
        pass

    while True:
        try:
            r = requests.get(URL, headers=HDR, timeout=10)
            if r.status_code == 200:
                data = r.json()
                battles_dict = data.get("battles", {})
                
                if not debug_sent:
                    for b_id, kampanya in battles_dict.items():
                        divler = kampanya.get("div", {})
                        for sub_id, d_bilgi in divler.items():
                            d_num = d_bilgi.get("div", 0)
                            if d_num in [4, 11]:
                                # Sözlük içindeki tüm anahtarları alt alta düzgün formatta döküyoruz
                                fields_str = "\n".join([f"• `{k}`: `{v}`" for k, v in d_bilgi.items()])
                                msg = f"📊 *Divizyon {d_num} Alanları* (Battle: {b_id}):\n\n{fields_str}"
                                
                                # Telegram mesaj boyu sınırına (4096 karakter) takılmamak için bölerek atalım
                                if len(msg) > 4000:
                                    msg = msg[:4000]
                                    
                                requests.post(TG, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
                                debug_sent = True
                                break
                        if debug_sent:
                            break

            time.sleep(60)
        except Exception as e:
            time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
