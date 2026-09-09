import time
import threading
import requests
import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "eRepublik Net 1H15M Geçen Süre & Boş Cephe Botu Aktif!"

def bot_loop():
    TOKEN = "8704453687:AAHrKY4bVuT0RaOtWoUcwlKxT_shuKqXO3Q"
    CHAT_ID = "8680653965"
    TG = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    URL = "https://www.erepublik.com/tr/military/campaigns-new"

    USER_COOKIE = "l_chathwe=1; _fbp=fb.1.1788877195502.705633333321429601; erpk_mid=8f53ae78391e7a2011a28e925516a496; erpk_rm=d3c6a76d5553de16635bc55bd45485da; erpk_plang=tr; _ga=GA1.1.874820192.1788877198; lastRegionId=647; erpk=2c0da60a899744a4eb979ce58f4cd821; erpk_auth=1; cf_clearance=.GJMZbjttyk3feFdc.Pve0VH5DKLnr9zpL5FCndMef8-1788959319-1.2.1.1-QwhO7DcwsBoRLLcPM9Q4S0grcsYSNJ6ihkA.NTe95jwpWyTbGsmgFpBXklCPKQHCsARVEZIvpzl8_.eYpcTEIukDJ92XRmUW.s35kW_g0EHtZw54huFU3o6Aw_GIxCEPhurL6ySJ0NphyRN_m1XmZzfP6SS2g.Fvg5DeRJgZOKQFjk6TDbd32BhwqplyCMse1FusrxGGrerE0wfco2b8BflMN3wWFuW1ZfpTNnXM0MJSi7YJp3hEcsDa2f0zt5HI0dxDCINeJ_yID5z3U5JQb_Fz5Rmai_JALtVehBfO54D4HNZ8aHdk7PfYZ5UwKfuH.0VTqy0LevkFsE42sV74xhtF9H6bVjQH2GXJCfGeIiI; _ga_PSSBE951PK=GS2.1.s1788959317$o7$g1$t1788959771$j17$l0$h0"

    HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cookie": USER_COOKIE,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.erepublik.com/tr/main/index"
    }

    SEEN_ALERTS = set()
    previous_battles = {}
    ended_rw_tracker = {}
    rw_alerts_sent = set()

    try:
        requests.post(TG, json={"chat_id": CHAT_ID, "text": "🔥 *eRepublik Süre Uyumlu Boş Cephe & RW Botu Başlatıldı!*", "parse_mode": "Markdown"})
    except:
        pass

    while True:
        try:
            r = requests.get(URL, headers=HDR, timeout=10)
            if r.status_code == 200:
                try:
                    data = r.json()
                except:
                    time.sleep(60)
                    continue

                battles_dict = data.get("battles", {})
                countries_dict = data.get("countries", {})
                current_time = time.time()
                
                current_battle_ids = set(battles_dict.keys())
                if previous_battles:
                    ended_ids = set(previous_battles.keys()) - current_battle_ids
                    for e_id in ended_ids:
                        b_info = previous_battles[e_id]
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
                        
                        # Sadece Div 4 (4) ve Air (11)
                        if d_num in [4, 11]:
                            key = f"{b_id}_{sub_id}"
                            end_time = d_bilgi.get("end", 0)
                            
                            if end_time:
                                # Round başlama zamanı: Bitiş zamanından 2 saat (7200 sn) öncesidir.
                                start_time = end_time - 7200
                                # Round başladığından beri geçen süre (0'dan ileriye doğru akan süre)
                                elapsed_seconds = current_time - start_time
                            else:
                                elapsed_seconds = 0
                            
                            # 1 saat 15 dakika = 4500 saniye
                            is_late = False
                            if elapsed_seconds >= 4500 and (end_time - current_time) > 0:
                                is_late = True
                                
                            if is_late:
                                stats_url = f"https://www.erepublik.com/tr/military/battlefield/{b_id}/{sub_id}/fighterStatistics"
                                is_open_target = False
                                status_desc = ""
                                
                                try:
                                    stats_res = requests.get(stats_url, headers=HDR, timeout=5)
                                    if stats_res.status_code == 200:
                                        stats_data = stats_res.json()
                                        
                                        inv_has_fighter = False
                                        def_has_fighter = False
                                        
                                        if isinstance(stats_data, dict):
                                            inv_list = stats_data.get("inv", stats_data.get("attacker", []))
                                            def_list = stats_data.get("def", stats_data.get("defender", []))
                                            if inv_list and len(inv_list) > 0: inv_has_fighter = True
                                            if def_list and len(def_list) > 0: def_has_fighter = True
                                            
                                            if not inv_has_fighter and not def_has_fighter:
                                                for k, v in stats_data.items():
                                                    if isinstance(v, list) and len(v) > 0:
                                                        for f in v:
                                                            side = f.get("side") or f.get("country_id")
                                                            if str(side) == inv_id: inv_has_fighter = True
                                                            elif str(side) == def_id: def_has_fighter = True
                                        elif isinstance(stats_data, list):
                                            for f in stats_data:
                                                side = f.get("side") or f.get("country_id")
                                                if str(side) == inv_id: inv_has_fighter = True
                                                elif str(side) == def_id: def_has_fighter = True
                                        
                                        if not inv_has_fighter and not def_has_fighter:
                                            is_open_target = True
                                            status_desc = "İki taraf da tamamen boş!"
                                        elif inv_has_fighter and not def_has_fighter:
                                            is_open_target = True
                                            status_desc = f"`{def_name}` tarafı boş (Sadece {inv_name} vuruyor)!"
                                        elif def_has_fighter and not inv_has_fighter:
                                            is_open_target = True
                                            status_desc = f"`{inv_name}` tarafı boş (Sadece {def_name} vuruyor)!"
                                except Exception as ex:
                                    pass
                                    
                                if is_open_target:
                                    if key not in SEEN_ALERTS:
                                        tur = "D4 KARA" if d_num == 4 else "AIR (SH)"
                                        msg = f"💎 *1H15M GEÇTİ - BOŞ {tur} FIRSATI!*\n⚔️ {inv_name} vs {def_name}\n📍 Bölge: {bolge}\n🚀 {status_desc}\n🔗 [Savaşa Git](https://www.erepublik.com/tr/military/battlefield/{b_id})"
                                        requests.post(TG, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
                                        SEEN_ALERTS.add(key)
                                else:
                                    if key in SEEN_ALERTS:
                                        SEEN_ALERTS.remove(key)
                                        
                    time.sleep(0.3)

                # RW 24 Saat Cooldown Takibi (5 dakika kala uyarı)
                for b_id, track in list(ended_rw_tracker.items()):
                    elapsed = current_time - track["end_time"]
                    region = track["region"]
                    
                    if 86100 <= elapsed < 86400:
                        if b_id not in rw_alerts_sent:
                            rw_msg = f"⏳ *RW COOLDOWN UYARISI!*\n📍 Bölge: `{region}`\n⏰ Savaşın bitiminden beri 24 saat geçmesine 5 dakika kaldı!\n🚀 İsyan (RW) açmak için hazırlık yap!"
                            requests.post(TG, json={"chat_id": CHAT_ID, "text": rw_msg, "parse_mode": "Markdown"})
                            rw_alerts_sent.add(b_id)
                    elif elapsed >= 86400:
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
