import time
import threading
import requests
import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "eRepublik Akilli Dedektor Bot Aktif!"

def bot_loop():
    # --- ONEMLI: Bunlari Render dashboard'unda "Environment" sekmesinden
    # environment variable olarak ekleyin (TG_TOKEN, TG_CHAT_ID, EREPUBLIK_COOKIE).
    # Kod icinde birakmak, repo'yu paylastiginizda tokeninizin calinmasina yol acar.
    TOKEN = os.environ.get("TG_TOKEN", "8704453687:AAHrKY4bVuT0RaOtWoUcwlKxT_shuKqXO3Q")
    CHAT_ID = os.environ.get("TG_CHAT_ID", "8680653965")
    USER_COOKIE = os.environ.get("EREPUBLIK_COOKIE", "l_chathwe=1; ...BURAYA_KENDI_COOKIENIZI_KOYUN...")

    TG = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    URL = "https://www.erepublik.com/tr/military/campaigns-new"

    # --- SABITLER (buradan ayarlayin) ---
    ROUND_DURATION = 5400        # 90 dakika = 5400 saniye (oyun ici round suresi)
    ALERT_AT_ELAPSED = 4500      # 75 dakika = 1s15dk gectiginde (yani bitise 15dk kala) alarm kontrolu basla
    RW_COOLDOWN = 86400          # 24 saat. Sonuc (kazanma/kaybetme) bu sureyi degistiriyor mu net degil,
                                  # bir kac RW sonucunu gozlemleyip gerekirse bu degeri guncelleyin.
    DEBUG = os.environ.get("DEBUG", "0") == "1"  # Render env'de DEBUG=1 yaparsaniz ham JSON'u loglar

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
    round_start_tracker = {}  # battleZoneId -> ilk gorduğumuz an (round baslangici tahmini)

    def send_tg(text):
        try:
            resp = requests.post(TG, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
            if resp.status_code != 200:
                print(f"Telegram gonderim hatasi: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"Telegram gonderim istisnasi: {e}")

    send_tg("Akilli Dedektor eRepublik Botu Baslatildi!")
    print("Bot baslatildi ve Telegram'a bilgi mesaji gonderildi.")

    while True:
        try:
            r = requests.get(URL, headers=HDR, timeout=10)

            # --- COOKIE / CLOUDFLARE TANI ---
            # cf_clearance cookie'si genelde IP/tarayici parmak izine baglidir.
            # Bu cookie'yi kendi bilgisayarinizda alip Render'in sunucu IP'sinden
            # kullaniyorsunuz; Cloudflare bunu reddedip HTML challenge sayfasi
            # donebilir. Asagidaki loglar bunu tespit etmenizi saglar.
            if r.status_code != 200:
                print(f"[TANI] Kampanya istegi basarisiz: status={r.status_code}, body_basi={r.text[:200]}")
                time.sleep(60)
                continue

            content_type = r.headers.get("Content-Type", "")
            if "json" not in content_type:
                print(f"[TANI] Beklenmeyen Content-Type: {content_type}. "
                      f"Muhtemelen Cloudflare challenge/login sayfasi donuyor. "
                      f"Cookie'nin gecerliligini ve Render IP'sinden erisimi kontrol edin.")
                print(f"[TANI] Yanit basi: {r.text[:300]}")
                time.sleep(60)
                continue

            try:
                data = r.json()
            except Exception as e:
                print(f"Kampanya JSON okuma hatasi: {e} | Yanit basi: {r.text[:300]}")
                time.sleep(60)
                continue

            battles_dict = data.get("battles", {})
            countries_dict = data.get("countries", {})
            # Render sunucusunun saati yerine oyunun kendi sunucu saatini kullaniyoruz
            # (JSON'un en ustundeki "time" alani) - saat kaymasi olasiligini ortadan kaldirir.
            current_time = data.get("time", time.time())

            # --- BITEN SAVASLARI TESPIT ET (RW cooldown takibi icin) ---
            current_battle_ids = set(battles_dict.keys())
            if previous_battles:
                ended_ids = set(previous_battles.keys()) - current_battle_ids
                for e_id in ended_ids:
                    b_info = previous_battles[e_id]
                    ended_rw_tracker[e_id] = {
                        "end_time": current_time,
                        "region": b_info.get("region_name", "Bolge"),
                        "inv_name": b_info.get("inv_name", "?"),
                        "def_name": b_info.get("def_name", "?"),
                    }

            previous_battles.clear()
            active_zone_ids = set()

            for b_id, kampanya in battles_dict.items():
                bolge = kampanya.get("region", {}).get("name", "Bolge")
                inv_id = str(kampanya.get("inv", {}).get("id"))
                def_id = str(kampanya.get("def", {}).get("id"))
                inv_name = countries_dict.get(inv_id, {}).get("name", "Saldirgan")
                def_name = countries_dict.get(def_id, {}).get("name", "Savunan")

                previous_battles[b_id] = {
                    "region_name": bolge,
                    "inv_name": inv_name,
                    "def_name": def_name,
                }

                divler = kampanya.get("div", {})
                for sub_id, d_bilgi in divler.items():
                    d_num = d_bilgi.get("div", 0)

                    # SADECE Div4 (kara) ve Div11 (hava) icin kontrol.
                    # Bu ID'lerin dogrulugunu oyun icinden teyit edin; farkliysa buradan degistirin.
                    if d_num not in [4, 11]:
                        continue

                    key = f"{b_id}_{sub_id}"

                    # ONEMLI KESIF: aktif (bitmemis) round'larda "end" alani None geliyor,
                    # sadece round bittiginde doluyor. Yani sunucu bize aktif round icin
                    # bitis zamani vermiyor - kendi zamanlamamizi tutmamiz gerekiyor.
                    is_round_finished = d_bilgi.get("division_end", False)
                    if is_round_finished:
                        # Bu round bitmis - takipten cikar, yeni round baska bir
                        # battleZoneId (sub_id) ile JSON'a dusecek.
                        round_start_tracker.pop(sub_id, None)
                        continue

                    active_zone_ids.add(sub_id)

                    if sub_id not in round_start_tracker:
                        # Bu battleZoneId'yi ilk kez goruyoruz - simdiki zamani
                        # yaklasik baslangic kabul ediyoruz (60sn'lik dongu payinda
                        # en fazla ~60sn hata olur, 15dk'lik alarm penceresi icin ihmal edilebilir).
                        round_start_tracker[sub_id] = current_time
                        continue

                    start_time = round_start_tracker[sub_id]
                    elapsed_seconds = current_time - start_time
                    remaining_seconds = ROUND_DURATION - elapsed_seconds
                    is_late = elapsed_seconds >= ALERT_AT_ELAPSED and remaining_seconds > 0

                    tur_adi = "D4" if d_num == 4 else "AIR"
                    if DEBUG:
                        print(f"[{tur_adi}] Bolge: {bolge} (ID: {b_id}) | Gecen: {elapsed_seconds/60:.1f}dk | "
                              f"Kalan: {remaining_seconds/60:.1f}dk | 1s15dk gecti mi?: {is_late}")

                    if not is_late:
                        continue

                    # Not: eski "fighterStatistics" URL'i sadece HTML donuyordu (SPA sayfasi).
                    # Gercek veri CSRF token gerektirmeyen bu endpoint'ten geliyor:
                    stats_url = f"https://www.erepublik.com/tr/military/battle-stats/{b_id}/{d_num}/{sub_id}"
                    is_open_target = False
                    status_desc = ""

                    try:
                        stats_res = requests.get(stats_url, headers=HDR, timeout=5)
                        if DEBUG:
                            print(f"-> Istatistik istek ({b_id}/{sub_id}) status: {stats_res.status_code}")

                        if stats_res.status_code == 200 and "json" in stats_res.headers.get("Content-Type", ""):
                            stats_data = stats_res.json()

                            if DEBUG:
                                # HENUZ TAM DOGRULANMADI: asagidaki mantik "en yuksek zone_id =
                                # aktif round" varsayimina dayaniyor. Alarm anina yakin (1s15dk
                                # gectiginde) bu ham ciktiyi paylasirsaniz mantigi kesinlestiririz.
                                print(f"[TANI] battle-stats JSON ({b_id}/{d_num}/{sub_id}): {str(stats_data)[:1500]}")

                            current_stats = stats_data.get("stats", {}).get("current", {})
                            inv_has_fighter = False
                            def_has_fighter = False

                            if current_stats:
                                zone_ids = [z for z in current_stats.keys() if z.isdigit()]
                                if zone_ids:
                                    max_zone = str(max(int(z) for z in zone_ids))
                                    div_data = current_stats.get(max_zone, {}).get(str(d_num), {})
                                    inv_has_fighter = str(inv_id) in div_data
                                    def_has_fighter = str(def_id) in div_data

                            if DEBUG:
                                print(f"   -> Sonuc: {inv_name} vuruyor mu?: {inv_has_fighter} | "
                                      f"{def_name} vuruyor mu?: {def_has_fighter}")

                            # Istediginiz kural: iki taraftan HERHANGI biri bos ise bildir.
                            if not inv_has_fighter and not def_has_fighter:
                                is_open_target = True
                                status_desc = "Iki taraf da tamamen bos!"
                            elif inv_has_fighter and not def_has_fighter:
                                is_open_target = True
                                status_desc = f"`{def_name}` tarafi bos (Sadece {inv_name} vuruyor)!"
                            elif def_has_fighter and not inv_has_fighter:
                                is_open_target = True
                                status_desc = f"`{inv_name}` tarafi bos (Sadece {def_name} vuruyor)!"
                        else:
                            if DEBUG:
                                print(f"[TANI] battle-stats beklenmeyen yanit: "
                                      f"{stats_res.status_code} {stats_res.text[:200]}")
                    except Exception as ex:
                        print(f"Istatistik cekme hatasi: {ex}")

                    if is_open_target:
                        if key not in SEEN_ALERTS:
                            tur = "D4 KARA" if d_num == 4 else "AIR (SH)"
                            msg = (f"1s15dk GECTI - BOS {tur} FIRSATI!\n"
                                   f"{inv_name} vs {def_name}\n"
                                   f"Bolge: {bolge}\n"
                                   f"{status_desc}\n"
                                   f"Savasa Git: https://www.erepublik.com/tr/military/battlefield/{b_id}")
                            send_tg(msg)
                            print(f"ALARM GONDERILDI -> Bolge: {bolge}")
                            SEEN_ALERTS.add(key)
                    else:
                        SEEN_ALERTS.discard(key)

                time.sleep(0.3)

            # Artik JSON'da hic gorunmeyen (b_id degisti, round bambaska sekilde
            # kapandi vb.) eski battleZoneId kayitlarini temizle.
            for old_zone_id in list(round_start_tracker.keys()):
                if old_zone_id not in active_zone_ids:
                    round_start_tracker.pop(old_zone_id, None)

            # --- RW COOLDOWN KONTROLU ---
            for b_id, track in list(ended_rw_tracker.items()):
                elapsed = current_time - track["end_time"]

                if RW_COOLDOWN - 300 <= elapsed < RW_COOLDOWN and b_id not in rw_alerts_sent:
                    rw_msg = (f"RW COOLDOWN UYARISI!\n"
                              f"Bolge: {track['region']}\n"
                              f"Ulkeler: {track['inv_name']} vs {track['def_name']}\n"
                              f"Savasin bitiminden beri 24 saate 5 dakika kaldi!\n"
                              f"Isyan (RW) acmak icin hazirlik yapin.")
                    send_tg(rw_msg)
                    rw_alerts_sent.add(b_id)
                elif elapsed >= RW_COOLDOWN:
                    del ended_rw_tracker[b_id]
                    rw_alerts_sent.discard(b_id)

            time.sleep(60)
        except Exception as e:
            print(f"Ana dongu hatasi: {e}")
            time.sleep(30)

# --- ONEMLI ---
# Thread'i modul seviyesinde (if __name__ disinda) baslatiyoruz. Render'da
# start command "gunicorn app:app" gibi bir sey ise, dosya "import" edilir,
# dogrudan calistirilmaz; "if __name__ == '__main__':" bloğu hic tetiklenmez
# ve bot thread'i hic baslamazdi (hata da vermez, sessizce hicbir sey yapmaz).
# Bu satirlar sayede thread, dosya nasil calistirilirsa calistirilsin baslar.
#
# UYARI: Render'da worker sayisi 1'den fazlaysa (ör. gunicorn -w 4) bu kod
# her worker'da ayri calisip AYNI Telegram mesajini birden fazla gonderir.
# Free/Starter planlarda genelde tek worker oldugu icin sorun olmaz, ama
# start command'inizde "-w" veya "--workers" gibi bir ayar varsa bana bildirin.
_bot_thread = threading.Thread(target=bot_loop, daemon=True)
_bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
