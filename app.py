import time
import threading
import requests
import os
import re
from flask import Flask, jsonify, request
from bs4 import BeautifulSoup

app = Flask(__name__)

# --- TELEGRAM (modul seviyesinde - hem bot_loop hem Flask route'lari kullanabilsin) ---
TG_TOKEN = os.environ.get("TG_TOKEN", "BURAYA_TOKEN_KOYUN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "BURAYA_CHAT_ID_KOYUN")
TG_URL = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"



# Tampermonkey scripti (tarayici tarafi) bu listeyi periyodik cekip kendi
# panelinde gosterebilsin diye son bildirimleri hafizada tutuyoruz. Thread-safe
# olmasi icin basit bir kilit kullaniyoruz.
_recent_alerts = []
_recent_alerts_lock = threading.Lock()
MAX_RECENT_ALERTS = 30


def _record_alert(text):
    with _recent_alerts_lock:
        _recent_alerts.insert(0, {"time": time.time(), "text": text})
        del _recent_alerts[MAX_RECENT_ALERTS:]


def send_tg(text):
    """Modul seviyesinde - hem bot_loop() hem Flask route handler'lari
    (energy-report gibi) buradan Telegram'a mesaj atabilir."""
    _record_alert(text)
    try:
        resp = requests.post(TG_URL, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram gonderim hatasi: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"Telegram gonderim istisnasi: {e}")


# ============ RW PAYLASILAN DURUM (cihaz/tarayici bagimsiz) ============
# RW takibi (gecmis, bildirilenler, ilk kurulum bayragi) artik burada - Render
# sunucusunda - tutuluyor, tarayicinin kendi hafizasinda degil. Boylece PC'den
# de telefondan da acilsa, hangi cihaz olursa olsun AYNI veriyi okur/yazar.
# Kilit de burada, gercek bir threading.Lock ile ATOMIK - tarayici tarafinda
# yasadigimiz "iki taraf da ayni anda kilidi aldi" yarisi burada olusmaz.
_rw_state = {"alerted": {}, "history": {}, "bootstrapped": False}
_rw_state_lock = threading.Lock()
_rw_scan_lock = {"owner": None, "ts": 0}
_rw_scan_lock_guard = threading.Lock()
RW_LOCK_STALE_SECONDS = 90


@app.route('/rw-state', methods=['GET'])
def get_rw_state():
    with _rw_state_lock:
        return jsonify(_rw_state)


@app.route('/rw-state', methods=['POST'])
def set_rw_state():
    body = request.get_json(force=True, silent=True) or {}
    with _rw_state_lock:
        if "alerted" in body:
            _rw_state["alerted"] = body["alerted"]
        if "history" in body:
            _rw_state["history"] = body["history"]
        if "bootstrapped" in body:
            _rw_state["bootstrapped"] = body["bootstrapped"]
    return jsonify({"ok": True})


@app.route('/rw-lock/acquire', methods=['POST'])
def acquire_rw_lock():
    body = request.get_json(force=True, silent=True) or {}
    owner = body.get("owner")
    if not owner:
        return jsonify({"acquired": False, "error": "owner gerekli"}), 400

    now = time.time()
    with _rw_scan_lock_guard:
        free = (_rw_scan_lock["owner"] is None) or (now - _rw_scan_lock["ts"] > RW_LOCK_STALE_SECONDS)
        if free:
            _rw_scan_lock["owner"] = owner
            _rw_scan_lock["ts"] = now
            return jsonify({"acquired": True})
        return jsonify({"acquired": False})


@app.route('/rw-lock/refresh', methods=['POST'])
def refresh_rw_lock():
    body = request.get_json(force=True, silent=True) or {}
    owner = body.get("owner")
    with _rw_scan_lock_guard:
        if _rw_scan_lock["owner"] == owner:
            _rw_scan_lock["ts"] = time.time()
            return jsonify({"ok": True})
        return jsonify({"ok": False})


@app.route('/rw-lock/release', methods=['POST'])
def release_rw_lock():
    body = request.get_json(force=True, silent=True) or {}
    owner = body.get("owner")
    with _rw_scan_lock_guard:
        if _rw_scan_lock["owner"] == owner:
            _rw_scan_lock["owner"] = None
    return jsonify({"ok": True})


@app.route('/')
def home():
    return "erepublik.tools Market Watcher Bot Aktif!"


@app.route('/recent-alerts')
def recent_alerts():
    with _recent_alerts_lock:
        return jsonify(list(_recent_alerts))


# ============ ENERJI TAHMINI (cihaz/tarayici bagimsiz) ============
# Tarayici (Tampermonkey) her enerji okumasini buraya bildirir. Buradan
# dolum hizi (rate) hesaplanip "tahmini dolma zamani" saklaniyor. Ayrica
# bot_loop'un kendi dongusu bu zamani surekli kontrol ediyor - boylece
# TARAYICI KAPALI OLSA BILE, tahmin edilen an gelince Telegram'a mesaj gider.
_energy_state = {
    "current": None, "limit": None, "rate_per_min": None,
    "projected_full_at": None, "notified": True, "last_update": 0,
}
_energy_lock = threading.Lock()


@app.route('/energy-report', methods=['POST'])
def energy_report():
    body = request.get_json(force=True, silent=True) or {}
    current = body.get("current")
    limit = body.get("limit")
    if current is None or limit is None:
        return jsonify({"ok": False}), 400

    now = time.time()
    with _energy_lock:
        st = _energy_state
        pc, pl = st["current"], st["limit"]
        changed = (pc is None) or (current != pc) or (limit != pl)

        # Sayfa yenilenmediyse DOM ayni degeri gosterir (bayat okuma). Bu durumda
        # hicbir sey guncellenmez; projeksiyon sabit kalir ve zamani gelince dolar.
        if not changed:
            st["last_update"] = now
            return jsonify({"ok": True, "stale": True})

        # Enerji dustuyse (harcanmissa) yeni dolum donemi
        if pc is not None and current < pc:
            st["notified"] = False
        # Artis: hizi, degerin en son DEGISTIGI andan itibaren hesapla
        elif pc is not None and st.get("changed_at"):
            dt_min = (now - st["changed_at"]) / 60.0
            d_energy = current - pc
            if dt_min > 0.5 and d_energy > 0:
                st["rate_per_min"] = d_energy / dt_min

        st["current"], st["limit"] = current, limit
        st["last_update"] = now
        st["changed_at"] = now

        if current >= limit:
            if not st["notified"]:
                send_tg(f"ENERJI DOLDU! {int(current)}/{int(limit)}")
                st["notified"] = True
            st["projected_full_at"] = now
        else:
            rate = st.get("rate_per_min")
            if rate and rate > 0:
                st["projected_full_at"] = now + ((limit - current) / rate) * 60
                # Tahmin gelecekteyse tekrar bildirime hazir ol
                if current > (pc or 0):
                    st["notified"] = False
            else:
                st["projected_full_at"] = None

    return jsonify({"ok": True})


def bot_loop():
    DEBUG = os.environ.get("DEBUG", "0") == "1"

    # --- ZAMANLAMA (Render Environment'tan degistirilebilir) ---
    JOB_CHECK_INTERVAL = int(os.environ.get("JOB_CHECK_INTERVAL_SEC", "1800"))   # 30 dakika
    ITEM_CHECK_INTERVAL = int(os.environ.get("ITEM_CHECK_INTERVAL_SEC", "600"))  # 10 dakika
    LOOP_TICK = 30  # ana dongu her 30 saniyede bir "sirasi geldi mi" diye bakar

    # --- FIYAT DUSUS ESIGI (genel varsayilan - urun bazinda ezilebilir) ---
    PRICE_DROP_PERCENT = float(os.environ.get("PRICE_DROP_PERCENT", "5"))

    # --- MUTLAK "IYI FIYAT" ESIGI (sadece enerji degeri tanimli urunler icin,
    # su an sadece ekmek). CC/enerji orani bu deger VE ALTINA duserse - onceki
    # fiyata gore dusus olsun olmasin - ayri bir "IYI FIYAT" bildirimi atilir.
    # Boylece fiyat hep ayni (dusmeden) iyi kalsa bile kacirilmaz.
    ABS_VALUE_THRESHOLD = float(os.environ.get("ABS_VALUE_THRESHOLD_CC_PER_ENERGY", "0.40"))

    JOB_URL = "https://erepublik.tools/en/marketplace/jobs/0/offers"

    # --- TAKIP EDILECEK URUNLER ---
    # Format: "Etiket|URL|MinMiktar|DususYuzdesi|Enerji,..."
    # (DususYuzdesi opsiyonel - verilmezse PRICE_DROP_PERCENT kullanilir.
    #  Enerji opsiyonel/0 - sadece ekmek gibi enerji karsiligi olan urunlerde
    #  doldurulur; 0 ise mutlak CC/enerji kontrolu yapilmaz.)
    # MinMiktar: bir ilan bu adetten azsa "en dusuk fiyat" hesabina katilmaz.
    # 0 = miktar filtresi yok (ozellikle Ev gibi az adetli satilan urunler icin).
    # Render'da ITEM_WATCH_URLS environment variable'ini bu formatta
    # tanimlarsaniz asagidaki varsayilanlarin YERINE onlar kullanilir - kod
    # degistirmeden yeni urun eklemek/cikarmak icin bu degiskeni kullanin.
    DEFAULT_ITEMS = (
        "Ekmek Q1|https://erepublik.tools/en/marketplace/items/0/1/1/offers|500|10|2,"
        "Ekmek Q2|https://erepublik.tools/en/marketplace/items/0/1/2/offers|500|5|4,"
        "Ekmek Q3|https://erepublik.tools/en/marketplace/items/0/1/3/offers|500|5|6,"
        "Ekmek Q4|https://erepublik.tools/en/marketplace/items/0/1/4/offers|500|5|8,"
        "Ekmek Q5|https://erepublik.tools/en/marketplace/items/0/1/5/offers|500|10|10,"
        "Ekmek Q6|https://erepublik.tools/en/marketplace/items/0/1/6/offers|500|10|12,"
        "Ekmek Q7|https://erepublik.tools/en/marketplace/items/0/1/7/offers|500|10|20,"
        "FRM Hammadde|https://erepublik.tools/en/marketplace/items/0/7/1/offers|50|5|0,"
        "WRM Hammadde|https://erepublik.tools/en/marketplace/items/0/12/1/offers|50|5|0,"
        "HRM Hammadde|https://erepublik.tools/en/marketplace/items/0/17/1/offers|50|20|0,"
        "ARM Hammadde|https://erepublik.tools/en/marketplace/items/0/24/1/offers|50|20|0,"
        "Hava Silahi Q5|https://erepublik.tools/en/marketplace/items/0/23/5/offers|0|5|0,"
        "Silah Q7|https://erepublik.tools/en/marketplace/items/0/2/7/offers|100|10|0,"
        "Bilet Q5|https://erepublik.tools/en/marketplace/items/0/3/5/offers|10|5|0,"
        "Ev Q1|https://erepublik.tools/en/marketplace/items/0/4/1/offers|0|5|0,"
        "Ev Q2|https://erepublik.tools/en/marketplace/items/0/4/2/offers|0|5|0,"
        "Ev Q3|https://erepublik.tools/en/marketplace/items/0/4/3/offers|0|5|0,"
        "Ev Q4|https://erepublik.tools/en/marketplace/items/0/4/4/offers|0|15|0,"
        "Ev Q5|https://erepublik.tools/en/marketplace/items/0/4/5/offers|0|15|0,"
        "Altin (Gold)|https://erepublik.tools/en/marketplace/monetary-market/gold/offers|1|5|0"
    )
    items_raw = os.environ.get("ITEM_WATCH_URLS", DEFAULT_ITEMS)
    ITEM_URLS = {}       # label -> url
    ITEM_MIN_QTY = {}    # label -> minimum miktar
    ITEM_DROP_PCT = {}   # label -> dususte alarm esigi (%)
    ITEM_ENERGY = {}     # label -> enerji karsiligi (0 = yok, mutlak kontrol atlanir)

    def _add_item(label, url, min_qty, drop_pct, energy):
        label = label.strip()
        ITEM_URLS[label] = url.strip()
        try:
            ITEM_MIN_QTY[label] = float(min_qty)
        except (ValueError, TypeError):
            ITEM_MIN_QTY[label] = 0
        try:
            ITEM_DROP_PCT[label] = float(drop_pct)
        except (ValueError, TypeError):
            ITEM_DROP_PCT[label] = PRICE_DROP_PERCENT
        try:
            ITEM_ENERGY[label] = float(energy)
        except (ValueError, TypeError):
            ITEM_ENERGY[label] = 0

    for entry in items_raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) == 5:
            _add_item(*parts)
        elif len(parts) == 4:
            label, url, min_qty, drop_pct = parts
            _add_item(label, url, min_qty, drop_pct, 0)
        elif len(parts) == 3:
            label, url, min_qty = parts
            _add_item(label, url, min_qty, PRICE_DROP_PERCENT, 0)
        elif len(parts) == 1 and "=" in parts[0]:
            # Eski format (Etiket=URL) ile geriye donuk uyumluluk
            label, url = parts[0].split("=", 1)
            _add_item(label, url, 0, PRICE_DROP_PERCENT, 0)

    HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    def parse_number(text):
        """'7,524.00' gibi metinleri float'a cevirir."""
        cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
        try:
            return float(cleaned)
        except ValueError:
            return None

    def fetch_table_rows(url):
        """Sayfada 'Link' basligi olan tabloyu bulup her satiri <td> listesi olarak
        dondurur. Bazi urun sayfalarinda (Ekmek gibi kaliteli/islemis urunler) asil
        'Available Offers' tablosundan ONCE, linksiz bir 'En Iyi Fiyat' ozet tablosu
        geliyor - sayfadaki ILK tabloyu almak o ozet tabloyu yakalayip linksiz
        (bos link) sonuc veriyordu. Artik "Link" basligi olan dogru tabloyu ariyoruz.
        """
        resp = requests.get(url, headers=HDR, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        target_table = None
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            if any("Link" in h for h in headers):
                target_table = table
                break
        if target_table is None:
            target_table = soup.find("table")  # yedek: hicbiri "Link" icermiyorsa ilkini dene

        if not target_table:
            return []
        rows = []
        for tr in target_table.find_all("tr"):
            tds = tr.find_all("td")
            if tds:
                rows.append(tds)
        return rows

    # ---------------- IS ILANLARI ----------------
    last_top_net_salary = {"value": None}

    def check_jobs():
        try:
            rows = fetch_table_rows(JOB_URL)
            if DEBUG:
                print(f"[TANI] Is ilanlari - satir sayisi: {len(rows)}")

            best = None  # (net_salary, name, gross, overtime, link)
            for tds in rows:
                if len(tds) < 6:
                    continue
                name_link = tds[2].find("a")
                name = name_link.get_text(strip=True) if name_link else tds[2].get_text(strip=True)
                gross = parse_number(tds[3].get_text(strip=True))
                net = parse_number(tds[4].get_text(strip=True))
                overtime = tds[5].get_text(strip=True)
                job_link_tag = tds[6].find("a") if len(tds) > 6 else None
                job_link = job_link_tag["href"] if job_link_tag and job_link_tag.has_attr("href") else ""

                if net is None:
                    continue
                if best is None or net > best[0]:
                    best = (net, name, gross, overtime, job_link)

            if not best:
                print("[TANI] Is ilanlari tablosu parse edilemedi (satir bulunamadi).")
                return

            net_salary, name, gross, overtime, job_link = best
            if DEBUG:
                print(f"[TANI] En yuksek net maas: {net_salary} ({name})")

            if last_top_net_salary["value"] is not None and net_salary != last_top_net_salary["value"]:
                msg = (f"IS ILANI DEGISTI!\n"
                       f"Yeni en yuksek net maas: {net_salary:.2f}\n"
                       f"Isveren: {name}\n"
                       f"Brut: {gross:.2f} | Net: {net_salary:.2f}\n"
                       f"Mesai: {overtime}\n"
                       f"Link: {job_link}")
                send_tg(msg)
                print(f"IS ILANI ALARMI GONDERILDI: {net_salary}")

            last_top_net_salary["value"] = net_salary
        except Exception as e:
            print(f"Is ilanlari kontrol hatasi: {e}")

    # ---------------- URUN FIYATLARI ----------------
    last_lowest_price = {}  # label -> fiyat
    good_value_state = {}   # label -> su an "iyi fiyat" esigi altinda mi (spam onlemek icin)

    def check_items():
        for label, url in ITEM_URLS.items():
            try:
                min_qty = ITEM_MIN_QTY.get(label, 0)
                drop_pct_threshold = ITEM_DROP_PCT.get(label, PRICE_DROP_PERCENT)
                rows = fetch_table_rows(url)
                if DEBUG:
                    print(f"[TANI] {label} - satir sayisi: {len(rows)}, min miktar: {min_qty}")

                best = None  # (price, link, amount)
                for tds in rows:
                    if len(tds) < 5:
                        continue
                    amount = parse_number(tds[2].get_text(strip=True))
                    price = parse_number(tds[3].get_text(strip=True))
                    link_tag = tds[4].find("a")
                    link = link_tag["href"] if link_tag and link_tag.has_attr("href") else ""

                    if price is None:
                        continue
                    if min_qty > 0 and (amount is None or amount < min_qty):
                        continue  # cok az miktarli ilanlari yok say
                    if best is None or price < best[0]:
                        best = (price, link, amount)

                if not best:
                    print(f"[TANI] {label} tablosu parse edilemedi "
                          f"(en az {min_qty} adetlik ilan bulunamadi).")
                    continue

                price, link, amount = best
                if DEBUG:
                    print(f"[TANI] {label} en dusuk fiyat: {price} ({amount} adet)")

                if label in last_lowest_price:
                    baseline = last_lowest_price[label]
                    threshold_price = baseline * (1 - drop_pct_threshold / 100)
                    if baseline > 0 and price <= threshold_price:
                        drop_pct = (1 - price / baseline) * 100
                        msg = (f"FIYAT DUSTU: {label}\n"
                               f"Onceki en dusuk: {baseline:.2f}\n"
                               f"Yeni en dusuk: {price:.2f} (%{drop_pct:.1f} dusus, esik: %{drop_pct_threshold:.0f})\n"
                               f"Link: {link}")
                        send_tg(msg)
                        print(f"FIYAT ALARMI GONDERILDI: {label} -> {price}")

                last_lowest_price[label] = price

                # --- MUTLAK "IYI FIYAT" KONTROLU (sadece enerji degeri tanimli urunlerde) ---
                energy = ITEM_ENERGY.get(label, 0)
                if energy > 0:
                    value_ratio = price / energy
                    was_good = good_value_state.get(label, False)
                    is_good = value_ratio <= ABS_VALUE_THRESHOLD

                    if is_good and not was_good:
                        msg = (f"IYI FIYAT: {label}\n"
                               f"Fiyat: {price:.2f} | Enerji: {energy:.0f} | "
                               f"Oran: {value_ratio:.3f} CC/enerji (esik: {ABS_VALUE_THRESHOLD:.2f})\n"
                               f"Link: {link}")
                        send_tg(msg)
                        print(f"IYI FIYAT ALARMI GONDERILDI: {label} -> {value_ratio:.3f}")

                    good_value_state[label] = is_good
            except Exception as e:
                print(f"{label} kontrol hatasi: {e}")

    # ---------------- ANA DONGU ----------------
    send_tg("Market Watcher Botu Baslatildi!")
    print("Bot baslatildi ve Telegram'a bilgi mesaji gonderildi.")

    last_job_check = 0
    last_item_check = 0
    last_error_alert_time = 0
    ERROR_ALERT_COOLDOWN = 1800  # ayni hata tekrar tekrar spam atmasin diye en az 30dk ara

    # --- ALTIN SABAH HATIRLATICISI ---
    # Turkiye saatiyle (UTC+3, DST yok) 09:45 ve 10:01'de "10 gold al" hatirlatmasi.
    # Render sunucusu UTC calisir, o yuzden hedef saatleri UTC'ye ceviriyoruz:
    # 09:45 TR = 06:45 UTC, 10:01 TR = 07:01 UTC.
    GOLD_REMINDER_TIMES_UTC = [(6, 45), (7, 1)]
    last_gold_reminder_date = {t: None for t in GOLD_REMINDER_TIMES_UTC}

    while True:
        try:
            now = time.time()

            if now - last_job_check >= JOB_CHECK_INTERVAL:
                check_jobs()
                last_job_check = now

            if now - last_item_check >= ITEM_CHECK_INTERVAL:
                check_items()
                last_item_check = now

            # --- Enerji tahmini: dolma zamani geldiyse (tarayici acik olmasa bile) ---
            with _energy_lock:
                proj = _energy_state.get("projected_full_at")
                if proj and not _energy_state.get("notified") and now >= proj:
                    limit = _energy_state.get("limit")
                    send_tg(f"ENERJI DOLDU (tahmini)! ~{int(limit) if limit else '?'} enerji")
                    _energy_state["notified"] = True

            # --- Altin sabah hatirlaticisi ---
            nowutc = time.gmtime(now)
            today_str = time.strftime("%Y-%m-%d", nowutc)
            for (hh, mm) in GOLD_REMINDER_TIMES_UTC:
                if nowutc.tm_hour == hh and nowutc.tm_min == mm and last_gold_reminder_date[(hh, mm)] != today_str:
                    send_tg("ALTIN HATIRLATICI\nSabah saatleri genelde daha ucuz oluyor - 10 gold almayi unutma!")
                    last_gold_reminder_date[(hh, mm)] = today_str

            time.sleep(LOOP_TICK)
        except Exception as e:
            print(f"Ana dongu hatasi: {e}")
            now = time.time()
            if now - last_error_alert_time > ERROR_ALERT_COOLDOWN:
                send_tg(f"MARKET WATCHER HATA!\nAna dongude beklenmeyen hata: {e}\nBot calismaya devam ediyor ama kontrol etmek isteyebilirsiniz.")
                last_error_alert_time = now
            time.sleep(30)


# Thread'i modul seviyesinde baslatiyoruz - Render'da "gunicorn app:app" gibi
# bir start command kullanilsa bile bot thread'i mutlaka baslasin diye.
_bot_thread = threading.Thread(target=bot_loop, daemon=True)
_bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
