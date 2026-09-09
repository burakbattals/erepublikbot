import time
import threading
import requests
import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "eRepublik Akıllı Kademeli Avcı Aktif!"

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

    try:
        requests.post(TG, json={"chat_id": CHAT_ID, "text": "🛡️ *Kademeli Savaş Avcısı Devrede (15 Dk & Hasar Kontrolü)*", "parse_mode": "Markdown"})
    except:
        pass

    while True:
        try:
            r = requests.get(URL, headers=HDR, timeout=10)
            if r.status_code == 200:
                data = r.json()
                battles_dict = data.get("battles", {})
                countries_dict = data.get("countries", {})
                
                # Sadece D4 ve Air (Div 4 ve 11) olan, son 15 dakikaya giren savaşları topla
                target_list = []
                
                current_time = time.time() # eRepublik zaman damgası kontrolü için
                
                for b_id, kampanya in battles_dict.items():
                    divler = kampanya.get("div", {})
                    for sub_id, d_bilgi in divler.items():
                        d_num = d_bilgi.get("div", 0)
                        if d_num in [4, 11]:
                            # Bitiş süresi kontrolü (end alanı saniye cinsinden bitiş zamanı tutar)
                            end_time = d_bilgi.get("end")
                            if end_time:
                                remaining_seconds = end_time - current_time
                                # Son 15 dakika (900 saniye) kaldıysa hedef listeye ekle
                                if 0 < remaining_seconds <= 900:
                                    target_list.endswith((b_id, sub_id, d_num, kampanya)) # Mantıksal ekleme aşağıda
                            
                # Kodun devamı için listeyi güvenli dolduralım
                valid_targets = []
                for b_id, kampanya in battles_dict.items():
                    bolge = kampanya.get("region", {}).get("name", "Bölge")
                    inv_id = str(kampanya.get("inv", {}).get("id"))
                    def_id = str(kampanya.get("def", {}).get("id"))
                    inv_name = countries_dict.get(inv_id, {}).get("name", "Saldırgan")
                    def_name = countries_dict.get(def_id, {}).get("name", "Savunan")
                    
                    divler = kampanya.get("div", {})
                    for sub_id, d_bilgi in divler.items():
                        d_num = d_bilgi.get("div", 0)
                        if d_num in [4, 11]:
                            end_time = d_bilgi.get("end")
                            if end_time:
                                rem = end_time - time.time()
                                if 0 < rem <= 900:  # Son 15 dakika
                                    valid_targets.append((b_id, sub_id, d_num, bolge, inv_name, def_name, d_bilgi))

                # Bulunan hedefleri 3-4 dakikalık zamana yayarak (araya 10-15 saniye koyarak) kontrol et
                if valid_targets:
                    sleep_interval = max(5, 200 // len(valid_targets)) # Toplam süreyi yaymak için dinamik uyku
                    
                    for b_id, sub_id, d_num, bolge, inv_name, def_name, d_bilgi in valid_targets:
                        co = d_bilgi.get("co", {})
                        inv_c = co.get("inv", [])
                        def_c = co.get("def", [])
                        
                        key = f"{b_id}_{sub_id}"
                        
                        # Eğer o roundda vuran kimse yoksa (listeler boşsa)
                        if not inv_c and not def_c:
                            if key not in SEEN_ALERTS:
                                tur = "D4 KARA" if d_num == 4 else "AIR (SH)"
                                rem_min = int((d_bilgi.get("end", 0) - time.time()) // 60)
                                msg = f"🎯 *KRİTİK FIRSAT: BOŞ {tur}!*\n⚔️ {inv_name} vs {def_name}\n📍 Bölge: {bolge}\n⏳ Kalan Süre: ~{rem_min} dakika\n💎 Kimse vurmamış, taze alan!\n🔗 [Savaşa Git](https://www.erepublik.com/tr/military/battlefield/{b_id})"
                                requests.post(TG, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
                                SEEN_ALERTS.add(key)
                        else:
                            if key in SEEN_ALERTS:
                                SEEN_ALERTS.remove(key)
                                
                        # İstekler arasında yavaşlatma (Cloudflare'a takılmamak için)
                        time.sleep(sleep_interval)

            # Döngü genelinde de nefes aldırıyoruz
            time.sleep(60)
        except Exception as e:
            time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop)
    t.daemon = True
    t.start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
