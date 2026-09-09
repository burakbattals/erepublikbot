import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "eRepublik Süper Bot Aktif ve Nöbette!"

def bot_loop():
    TOKEN="8704453687:AAHrKY4bVuT0RaOtWoUcwlKxT_shuKqXO3Q"
    CHAT_ID="8680653965"
    TG=f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    URL="https://www.erepublik.com/tr/military/campaigns-new"

    USER_COOKIE = "l_chathwe=1; _fbp=fb.1.1788877195502.705633333321429601; erpk_mid=8f53ae78391e7a2011a28e925516a496; erpk_rm=d3c6a76d5553de16635bc55bd45485da; erpk_plang=tr; _ga=GA1.1.874820192.1788877198; lastRegionId=647; erpk=2c0da60a899744a4eb979ce58f4cd821; erpk_auth=1; cf_clearance=.GJMZbjttyk3feFdc.Pve0VH5DKLnr9zpL5FCndMef8-1788959319-1.2.1.1-QwhO7DcwsBoRLLcPM9Q4S0grcsYSNJ6ihkA.NTe95jwpWyTbGsmgFpBXklCPKQHCsARVEZIvpzl8_.eYpcTEIukDJ92XRmUW.s35kW_g0EHtZw54huFU3o6Aw_GIxCEPhurL6ySJ0NphyRN_m1XmZzfP6SS2g.Fvg5DeRJgZOKQFjk6TDbd32BhwqplyCMse1FusrxGGrerE0wfco2b8BflMN3wWFuW1ZfpTNnXM0MJSi7YJp3hEcsDa2f0zt5HI0dxDCINeJ_yID5z3U5JQb_Fz5Rmai_JALtVehBfO54D4HNZ8aHdk7PfYZ5UwKfuH.0VTqy0LevkFsE42sV74xhtF9H6bVjQH2GXJCfGeIiI; _ga_PSSBE951PK=GS2.1.s1788959317$o7$g1$t1788959771$j17$l0$h0"

    HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": USER_COOKIE,
        "X-Requested-With": "XMLHttpRequest"
    }

    SEEN_BATTLE = set()
    rw_takip_listesi = {}

    try:
        requests.post(TG, json={"chat_id": CHAT_ID, "text": "🤖 *eRepublik Süper Bot Hugging Face Bulutta Aktif!*", "parse_mode": "Markdown"})
    except:
        pass

    while True:
        try:
            r = requests.get(URL, headers=HDR, timeout=10)
            if r.status_code == 200:
                data = r.json()
                battles_dict = data.get("battles", {})
                countries_dict = data.get("countries", {})
                Suan = int(time.time())
                
                # 1. D4 ve Air (Son 15 dk)
                for b_id, kampanya in battles_dict.items():
                    bolge = kampanya.get("region", {}).get("name", "Bölge")
                    inv_id = str(kampanya.get("inv", {}).get("id"))
                    def_id = str(kampanya.get("def", {}).get("id"))
                    inv_name = countries_dict.get(inv_id, {}).get("name", "Saldırgan")
                    def_name = countries_dict.get(def_id, {}).get("name", "Savunan")
                    
                    divler = kampanya.get("div", {})
                    for sub_id, d_bilgi in divler.items():
                        d_num = d_bilgi.get("div", 0)
                        end = d_bilgi.get("end")
                        if d_num in [4, 11] and end is not None:
                            kalan = end - Suan
                            if 0 < kalan <= 900:
                                tur = "D4 KARA" if d_num == 4 else "AIR (SH)"
                                key = f"{b_id}_{sub_id}"
                                if key not in SEEN_BATTLE:
                                    msg = f"🚨 *{tur} BİTİYOR (Son {kalan//60} dk)!*\n⚔️ {inv_name} vs {def_name}\n📍 Bölge: {bolge}\n🔗 [Savaşa Git](https://www.erepublik.com/tr/military/battlefield/{b_id})"
                                    requests.post(TG, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
                                    SEEN_BATTLE.add(key)

                # 2. RW Havuzu Kayıt
                for b_id, kampanya in battles_dict.items():
                    bolge_id = kampanya.get("region", {}).get("id")
                    bolge_adi = kampanya.get("region", {}).get("name", "Bölge")
                    divler = kampanya.get("div", {})
                    for sub_id, d_bilgi in divler.items():
                        if d_bilgi.get("div", 0) in [4, 11] and d_bilgi.get("end") is not None:
                            hedef_rw_zaman = d_bilgi.get("end") + 86400
                            if bolge_id not in rw_takip_listesi:
                                rw_takip_listesi[bolge_id] = {
                                    "region": bolge_adi,
                                    "rw_time": hedef_rw_zaman,
                                    "notified": False
                                }

                # 3. RW Alarm (5 dk kala)
                for b_id, bilgi in list(rw_takip_listesi.items()):
                    kalan_sure = bilgi["rw_time"] - Suan
                    if 0 < kalan_sure <= 300 and not bilgi["notified"]:
                        msg = f"🚨 *RW ALARMI (5 dk kaldı)!*\n📍 Bölge: {bilgi['region']}\n⏳ Direniş birazdan açılıyor, hazırlık yap!"
                        requests.post(TG, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
                        bilgi["notified"] = True
                    elif kalan_sure <= 0:
                        del rw_takip_listesi[b_id]

            time.sleep(120)
        except Exception as e:
            print("Hata:", e)
            time.sleep(30)

if __name__ == '__main__':
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    app.run(host='0.0.0.0', port=7860)