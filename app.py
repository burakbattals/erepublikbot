import time
import threading
import requests
import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "eRepublik Akıllı Hasar & RW Takipçisi Aktif!"

def bot_loop():
    TOKEN = "8704453687:AAHrKY4bVuT0RaOtWoUcwlKxT_shuKqXO3Q"
    CHAT_ID = "8680653965"
    TG = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    URL = "https://www.erepublik.com/tr/military/campaigns-new"

    USER_COOKIE = "l_chathwe=1; _fbp=fb.1.1788877195502.705633333321429601; erpk_mid=8f53ae78391e7a2011a28e925516a496; erpk_rm=d3c6a76d5553de16635bc55bd45485da; erpk_plang=tr; _ga=GA1.1.874820192.1788877198; lastRegionId=647; erpk=2c0da60a899744a4eb979ce58f4cd821; erpk_auth=1; cf_clearance=.GJMZbjttyk3feFdc.Pve0VH5DKLnr9zpL5FCndMef8-1788959319-1.2.1.1-QwhO7DcwsBoRLLcPM9Q4S0grcsYSNJ6ihkA.NTe95jwpWyTbGsmgFpBXklCPKQHCsARVEZIvpzl8_.eYpcTEIukDJ92XRmUW.s35kW_g0EHtZw54huFU3o6Aw_GIxCEPhurL6ySJ0NphyRN_m1XmZzfP6SS2g.Fvg5DeRJgZOKQFjk6TDbd32BhwqplyCMse1FusrxGGrerE0wfco2b8BflMN3wWFuW1ZfpTNnXM0MJSi7YJp3hEcsDa2f0zt5HI0dxDCINeJ_yID5z3U5JQb_Fz5Rmai_JALtVehBfO54D4HNZ8aHdk7PfYZ5UwKfuH.0VTqy0LevkFsE42sV74xhtF9H6bVjQH2GXJCfGeIiI; _ga_PSSBE951PK=GS2.1.s1788959317$o7$g1$t1788959771$j17$l0$h0"

    HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": USER_COOKIE,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.erepublik.com/tr/main/index"
    }

    SEEN_ALERTS = set()
    previous_battles = {}
    ended_rw_tracker = {} # Bölge bazlı kapanan savaşların bitiş zamanını tutar (RW 24 saat sayacı için)
    rw_alerts_sent = set()

    try:
        requests.post(TG, json={"chat_id": CHAT_ID, "text": "🔥 *Hasar & RW 24 Saat Takipçisi Devrede!*", "parse_mode": "Markdown"})
    except:
        pass

    while True:
        try:
            r = requests.get(URL, headers=HDR, timeout=10)
            if r.status_code == 200:
                data = r.json()
                battles_dict = data.get("battles", {})
                countries_dict = data.get("countries", {})
                current_time = time.time()
                
                # 1. Biten savaşları tespit et (RW cooldown takibi için)
                current_battle_ids = set(battles_dict.keys())
                if previous_battles:
                    ended_ids = set(previous_battles.keys()) - current_battle_ids
                    for e_id in ended_ids:
                        b_info = previous_battles[e_id]
                        # Eğer bu bir direnniş savaşı (RW) ise veya genel savaşsa bitiş zamanını kaydet
                        region_name = b_info.get("region_name", "Bölge")
                        ended_rw_tracker[e_id] = {
                            "end_time": current_time,
                            "region": region_name
                        }
                
                previous_battles.clear()
                
                for b_id, kampanya in battles_dict.items():
                    bolge = kampanya.get("region", {}).get("name", "Bölge")
                    inv_id = str(kampanya.get("inv", {}).get("id"))
                    def_id = str(kampanya.get("def", {}).get("id"))
                    inv_name = countries_dict.get(inv_id, {}).get("name", "Saldırgan")
                    def_name = countries_dict.get(def_id, {}).get("name", "Savunan")
                    
                    previous_battles[b_id] = {"region_name": bolge}
                    
                    divler = kampanya.get("div", {})
                    for sub_id, d_bilgi in divler.items():
                        d_num = d_bilgi.get("div", 0)
                        
                        if d_num in [4, 11]:
                            key = f"{b_id}_{sub_id}"
                            
                            inv_score = d_bilgi.get("inv_score", 0) or d_bilgi.get("inv_points", 0) or kampanya.get("inv", {}).get("points", 0)
                            def_score = d_bilgi.get("def_score", 0) or d_bilgi.get("def_points", 0) or kampanya.get("def", {}).get("points", 0)
                            
                            co = d_bilgi.get("co", {})
                            inv_c = co.get("inv", [])
                            def_c = co.get("def", [])
                            
                            end_time = d_bilgi.get("end")
                            is_late = False
                            if end_time and 0 < (end_time - current_time) <= 900:
                                is_late = True
                            elif inv_score >= 1350 or def_score >= 1350:
                                is_late = True
                                
                            # Kriter: Son düzlükte olunduğunda TARAFLARDAN HERHANGİ BİRİNİN listesinin boş olması (Biri vurmamışsa yeter!)
                            if is_late and (not inv_c or not def_c):
                                if key not in SEEN_ALERTS:
                                    tur = "D4 KARA" if d_num == 4 else "AIR (SH)"
                                    empty_side = inv_name if not inv_c else def_name
                                    msg = f"💎 *TEK TARAFI BOŞ {tur} FIRSATI!*\n⚔️ {inv_name} vs {def_name}\n📍 Bölge: {bolge}\n📊 Skor: {inv_score} - {def_score}\n🚀 `{empty_side}` tarafında vuran kimse yok!\n🔗 [Savaşa Git](https://www.erepublik.com/tr/military/battlefield/{b_id})"
                                    requests.post(TG, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
                                    SEEN_ALERTS.add(key)
                            else:
                                if key in SEEN_ALERTS and inv_c and def_c:
                                    SEEN_ALERTS.remove(key)
                                    
                    time.sleep(0.5)

                # 2. RW 24 Saat Cooldown Kontrolü (24 saatin dolmasına 5 dk kala haber ver)
                # 24 saat = 86400 saniye. 5 dakika kala = 86400 - 300 = 86100 saniye geçmesi demek.
                for b_id, track in list(ended_rw_tracker.items()):
                    elapsed = current_time - track["end_time"]
                    region = track["region"]
                    
                    # 23 saat 55 dakika ile 24 saat arasında ise ve daha önce bildirim atılmadıysa
                    if 86100 <= elapsed < 86400:
                        if b_id not in rw_alerts_sent:
                            rw_msg = f"⏳ *RW COOLDOWN UYARISI!*\n📍 Bölge: `{region}`\n⏰ Savaşın bitmesine 5 dakika kaldı (24 saatlik süre doluyor)!\n🚀 İsyan (RW) açmak için hazırlık yap!"
                            requests.post(TG, json={"chat_id": CHAT_ID, "text": rw_msg, "parse_mode": "Markdown"})
                            rw_alerts_sent.add(b_id)
                    elif elapsed >= 86400:
                        # Süre tamamen dolduysa listeden temizle
                        del ended_rw_tracker[b_id]

            time.sleep(60)
        except Exception as e:
            time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
