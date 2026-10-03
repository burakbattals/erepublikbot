import time
import threading
import requests
import os
import re
from flask import Flask, jsonify, request
from bs4 import BeautifulSoup

app = Flask(__name__)

# --- ROUND KİLİDİ ÖNBELLEĞİ (Render, 3 saat) ---
# Cihaz/tarayıcı tamamen kapansa/değişse bile hangi round'a kilitlendiğimizi
# hatırlamak için basit bir sunucu tarafı önbellek. Render'ın ücretsiz planı
# ara sıra yeniden başlayabildiği için (bellek sıfırlanır) bu, cihazdaki
# GM storage'ın YERİNE değil, YANINDA bir yedek olarak çalışır.
API_SECRET = os.environ.get("API_SECRET", "")
ROUND_LOCK_TTL_SECONDS = 3 * 3600
_round_lock = {"battleId": None, "battleZoneId": None, "spent": None, "updatedAt": 0}
_round_lock_lock = threading.Lock()


def _check_api_secret():
    if not API_SECRET:
        return False
    key = request.headers.get("X-Api-Key") or request.args.get("key")
    return key == API_SECRET


@app.route('/round-lock', methods=['GET'])
def round_lock_get():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    with _round_lock_lock:
        age = time.time() - _round_lock["updatedAt"]
        if _round_lock["battleId"] and age <= ROUND_LOCK_TTL_SECONDS:
            return jsonify({
                "battleId": _round_lock["battleId"],
                "battleZoneId": _round_lock["battleZoneId"],
                "spent": _round_lock["spent"],
                "ageSeconds": int(age),
            })
        return jsonify({"battleId": None, "battleZoneId": None, "spent": None})


@app.route('/round-lock', methods=['POST'])
def round_lock_set():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    battle_id = body.get("battleId") or None
    battle_zone_id = body.get("battleZoneId") or None
    spent = body.get("spent")
    with _round_lock_lock:
        if battle_id and battle_zone_id:
            _round_lock["battleId"] = str(battle_id)
            _round_lock["battleZoneId"] = str(battle_zone_id)
            if isinstance(spent, (int, float)):
                _round_lock["spent"] = spent
            _round_lock["updatedAt"] = time.time()
        else:
            # boş gönderilirse kilidi temizle (round bitti/tamamlandı)
            _round_lock["battleId"] = None
            _round_lock["battleZoneId"] = None
            _round_lock["spent"] = None
            _round_lock["updatedAt"] = 0
    return jsonify({"ok": True})


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


@app.route('/user-alert', methods=['POST'])
def user_alert():
    """Ultimate Asistan gibi yetkili istemcilerden gelen Telegram alarmı."""
    if not _check_api_secret():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text gerekli"}), 400
    if len(text) > 4096:
        text = text[:4090] + "..."
    send_tg(text)
    return jsonify({"ok": True})


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
        "HRM Hammadde|https://erepublik.tools/en/marketplace/items/0/17/1/offers|50|15|0,"
        "ARM Hammadde|https://erepublik.tools/en/marketplace/items/0/24/1/offers|50|15|0,"
        "Hava Silahi Q5|https://erepublik.tools/en/marketplace/items/0/23/5/offers|0|5|0,"
        "Silah Q7|https://erepublik.tools/en/marketplace/items/0/2/7/offers|100|10|0,"
        "Bilet Q5|https://erepublik.tools/en/marketplace/items/0/3/5/offers|50|5|0,"
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
                overtime_num = parse_number(overtime)
                if overtime_num is None or overtime_num < 3:
                    # Mesai en az 3 olmayan ilanlari hic degerlendirmeye almiyoruz.
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


# ============ PAZAR HAREKETI + ULKE TARAMA (erepublik.tools JSON API) ============
# 1) Global ve "izlenen" ulke defterleri sik aralikla alinir; iki goruntu arasindaki farktan
#    satis tahmini cikarilir (min = adedi azalan teklifler = kesin alim,
#    max = min + gorunur aralikta kaybolan teklifler = alindi VEYA iptal).
# 2) Diger tum ulkeler yavas yavas taranir (sadece en ucuz fiyat + ust uste ortalama).
# 3) Hangi ulkelerin izlenecegini Tampermonkey paneli /market-watch ile bildirir.
# 4) Kesin alimlar FIYAT ARALIGINA gore de tutulur (%1'lik dilimler): istemci "kârli fiyatlardan kac adet satildi"yi
#    hesaplar; yuksek fiyat ama alici yok durumu (tuzak) boyle ayirt edilir.
# Veri sakligi sinirli: 12 saat 10 dk'lik, 7 gun saatlik, 30 gun gunluk TOPLAM; sonra silinir.
# Render ENV (hepsi opsiyonel):
#   ACTIVITY_ENABLED=1, ACTIVITY_INTERVAL_SEC=180 (global), WATCH_INTERVAL_SEC=600 (izlenen ulkeler),
#   MAX_WATCH=60, SCAN_ENABLED=1, SCAN_GAP_SEC=8 (taramada istekler arasi sn),
#   REQ_COST_SEC=2.5 (tek istegin ortalama maliyeti), WATCH_UTIL=0.75 (izlemeye ayrilacak zaman payi; kalani tarama icin),
#   ACTIVITY_ITEMS="7:1,12:1,17:1,24:1,4:1,23:5,2:7", SCAN_COUNTRIES="1,9,10,...", ASK_MIN_UNITS=10
import math
import json as _json

ACT_ENABLED = os.environ.get("ACTIVITY_ENABLED", "1") == "1"
ACT_API = "https://service.erepublik.tools/api/v1/market/item/{c}/{i}/{q}"
ACT_INTERVAL = max(120, int(os.environ.get("ACTIVITY_INTERVAL_SEC", "180")))
WATCH_INTERVAL = max(300, int(os.environ.get("WATCH_INTERVAL_SEC", "600")))
MAX_WATCH = max(1, int(os.environ.get("MAX_WATCH", "60")))
REQ_COST = max(1.0, float(os.environ.get("REQ_COST_SEC", "2.5")))     # dongu 1,5 sn bekler + HTTP suresi
WATCH_UTIL = min(0.95, max(0.3, float(os.environ.get("WATCH_UTIL", "0.75"))))
SCAN_ENABLED = os.environ.get("SCAN_ENABLED", "1") == "1"
SCAN_GAP = max(3, int(os.environ.get("SCAN_GAP_SEC", "8")))
ASK_MIN_UNITS = int(os.environ.get("ASK_MIN_UNITS", "10"))      # bilinmeyen urunler icin varsayilan
# Urun basina "en ucuz teklif" sayilmak icin gereken en az adet (tek-iki adetlik toz teklifler hammaddede yok sayilir,
# ev/hava silahi gibi az adetli urunlerde 1 adetlik teklif de gercek tekliftir). ENV: ASK_MIN_MAP="7:1=10,4:1=1"
ASK_MIN_MAP = {"7:1": 10, "12:1": 10, "17:1": 10, "24:1": 10, "4:1": 1, "23:5": 1, "2:7": 5}
for _kv in os.environ.get("ASK_MIN_MAP", "").split(","):
    if "=" in _kv:
        _k, _v = _kv.split("=", 1)
        try:
            ASK_MIN_MAP[_k.strip()] = int(_v)
        except ValueError:
            pass
ACT_ITEMS = [x.strip() for x in os.environ.get(
    "ACTIVITY_ITEMS", "7:1,12:1,17:1,24:1,4:1,23:5,2:7").split(",") if x.strip()]
SCAN_COUNTRIES = [x.strip() for x in os.environ.get(
    "SCAN_COUNTRIES",
    "1,9,10,11,12,13,14,15,23,24,26,27,28,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,47,48,49,"
    "51,52,54,55,56,57,58,59,61,63,64,65,66,67,68,69,70,71,72,73,74,75,76,77,78,79,80,81,82,83,84,"
    "164,165,166,167,168,169,170").split(",") if x.strip()]
ACT_FILE = os.environ.get("ACTIVITY_FILE", "/tmp/market_activity.json")
ACT_WINDOW = 30            # API en ucuz 30 teklifi donduruyor
FINE_SEC, FINE_KEEP = 600, 12 * 3600
HOUR_KEEP, DAY_KEEP = 7 * 86400, 30 * 86400
_act = {}                  # "ulke:sanayi:kalite" -> durum
_scan = {}                 # "ulke:sanayi:kalite" -> {"ts","ask","ema","n"}
_watch = {"countries": [], "skip": [], "all": [], "ts": 0.0}
_act_lock = threading.Lock()
_act_pause_until = 0.0
_act_stats = {"global_ok": 0, "global_err": 0, "country_ok": 0, "country_err": 0,
              "last_err": "", "last_err_key": "", "last_ok_ts": 0.0}


def _stat(key, ok, err=""):
    kind = "global" if key.startswith("0:") else "country"
    with _act_lock:
        _act_stats[f"{kind}_{'ok' if ok else 'err'}"] += 1
        if ok:
            _act_stats["last_ok_ts"] = time.time()
        else:
            _act_stats["last_err"] = str(err)[:160]
            _act_stats["last_err_key"] = key


HIST_LN = math.log(1.01)      # fiyat dilimi: %1'lik (logaritmik); istemci 1.01**dilim ile fiyati geri kurar


def _bin(price):
    return int(round(math.log(price) / HIST_LN))


def _new_b():
    return {"v": [0.0] * 6, "h": {}}   # v=[kesin adet, kaybolan adet, yeni ilan, kesin deger, kaybolan deger, alim olayi sayisi]; h={dilim:[kesin, kaybolan]}


def _b_add(dst, src, cap=None):
    for i in range(len(src["v"])):
        dst["v"][i] += src["v"][i]
    for k, (p, g) in src["h"].items():
        d = dst["h"].setdefault(k, [0.0, 0.0])
        d[0] += p
        d[1] += g
    if cap is not None and len(dst["h"]) > cap:       # boyut sabit kalsin: en kucuk hacimli dilimleri at (0 = hepsini at)
        dst["h"] = dict(sorted(dst["h"].items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:cap])


def _act_compact(st, ts):
    """Eski ince kovalari saatliğe, eski saatlikleri gunluge katlar; en eskiyi siler (boyut sabit kalsin)."""
    lo = int((ts - FINE_KEEP) // FINE_SEC)
    for k in [k for k in st["fine"] if int(k) < lo]:
        _b_add(st["hour"].setdefault(str(int(k) * FINE_SEC // 3600), _new_b()), st["fine"].pop(k), 16)
    lo = int((ts - HOUR_KEEP) // 3600)
    for k in [k for k in st["hour"] if int(k) < lo]:
        _b_add(st["day"].setdefault(str(int(k) * 3600 // 86400), _new_b()), st["hour"].pop(k), 0)      # 7 gunden eski fiyat dilimleri kullanilmaz: yalniz toplamlar kalir
    lo = int((ts - DAY_KEEP) // 86400)
    for k in [k for k in st["day"] if int(k) < lo]:
        del st["day"][k]


def _act_apply(key, offers, ts, interval):
    """Yeni goruntuyu oncekiyle karsilastirip kovaya yazar. Saf fonksiyon (test edilebilir)."""
    cur = {}
    for o in offers:
        try:
            cur[int(o["id"])] = (float(o["amount"]), float(o["gross"]))
        except (KeyError, TypeError, ValueError):
            continue
    with _act_lock:
        st = _act.setdefault(key, {"prev": None, "prev_ts": 0.0, "first_ts": ts, "snaps": 0,
                                   "interval": interval, "fine": {}, "hour": {}, "day": {}, "top": None})
        st["interval"] = interval
        # EN UCUZ teklif (urune ozgu "toz" siniri uzerindeki en dusuk fiyat): degismeden ne kadar suredir duruyor?
        # Alicilar once en ucuzu alir; en ucuz teklif saatlerdir ayni adetle duruyorsa o fiyattan alici yok demektir.
        minu, best = _ask_min(key), None
        for oid, (amt, g) in cur.items():
            if g > 0 and (best is None or (amt < minu, g) < (best[1] < minu, best[2])):
                best = (oid, amt, g)
        old = st.get("top")
        if best is None:
            st["top"] = None
        elif not (old and old["id"] == best[0] and abs(old["amt"] - best[1]) < 1e-9):
            st["top"] = {"id": best[0], "amt": best[1], "g": best[2], "since": ts}     # yeni teklif ya da adedi azaldi (alim oldu)
        prev, prev_ts = st["prev"], st["prev_ts"]
        if prev and 0 < ts - prev_ts <= interval * 3:
            cur_max = float("inf") if len(cur) < ACT_WINDOW else max(g for _, g in cur.values())
            prev_max = float("inf") if len(prev) < ACT_WINDOW else max(g for _, g in prev.values())
            b = _new_b()
            for oid, (amt, g) in prev.items():
                c = cur.get(oid)
                if c is not None:
                    if c[0] < amt and g > 0:
                        d = amt - c[0]
                        b["v"][0] += d; b["v"][3] += d * g; b["v"][5] += 1
                        b["h"].setdefault(str(_bin(g)), [0.0, 0.0])[0] += d          # kesin alim, bu fiyat diliminde
                elif g <= cur_max and g > 0:
                    b["v"][1] += amt; b["v"][4] += amt * g
                    b["h"].setdefault(str(_bin(g)), [0.0, 0.0])[1] += amt             # alindi VEYA iptal
                # g > cur_max: pencereden fiyat yuzunden dustu, sayilmaz
            for oid, (amt, g) in cur.items():
                if oid not in prev and g <= prev_max:
                    b["v"][2] += amt
            _b_add(st["fine"].setdefault(str(int(ts // FINE_SEC)), _new_b()), b, 8)
        st["prev"], st["prev_ts"] = cur, ts
        st["snaps"] += 1
        _act_compact(st, ts)


def _ask_of(offers, minu=None):
    minu = ASK_MIN_UNITS if minu is None else minu
    best = anyp = None
    for o in offers:
        try:
            amt, g = float(o["amount"]), float(o["gross"])
        except (KeyError, TypeError, ValueError):
            continue
        if anyp is None or g < anyp:
            anyp = g
        if amt >= minu and (best is None or g < best):
            best = g
    return best if best is not None else anyp


def _scan_update(key, offers, ts):
    ask = _ask_of(offers, _ask_min(key))
    n = len(offers)
    with _act_lock:
        s = _scan.get(key)
        if s is None:
            _scan[key] = {"ts": ts, "ask": ask, "ema": ask, "n": n}
            return
        if ask is not None:
            if s.get("ema") is None:
                s["ema"] = ask
            else:   # 24 saatlik zaman-tabanli ustel ortalama
                a = 1 - math.exp(-max(0.0, ts - s["ts"]) / 86400.0)
                s["ema"] += (ask - s["ema"]) * a
        s.update(ts=ts, ask=ask, n=n)


def _sum_window(st, now, hrs, want_hist=False):
    lo = now - hrs * 3600
    v, h, hours = [0.0] * 6, {}, set()
    for tier, size in (("fine", FINE_SEC), ("hour", 3600), ("day", 86400)):
        for k, b in st[tier].items():
            start = int(k) * size
            if start >= lo:
                for i in range(len(b["v"])):
                    v[i] += b["v"][i]
                if b["v"][0] > 0 and tier != "day":
                    hours.add(start // 3600)                # satis gorulen farkli saat sayisi (tek seferlik iri alim ayirt edilsin)
                if want_hist:
                    for bk, (p, g) in b["h"].items():
                        d = h.setdefault(bk, [0.0, 0.0])
                        d[0] += p
                        d[1] += g
    return v, h, len(hours)


def _act_summary(now):
    out = {}
    with _act_lock:
        for key, st in _act.items():
            s = _scan.get(key) or {}
            res = {"snaps": st["snaps"], "first_ts": st["first_ts"], "last_ts": st["prev_ts"],
                   "interval": st["interval"], "ask": s.get("ask"), "ema": s.get("ema"), "n": s.get("n")}
            for label, hrs in (("h1", 1), ("h6", 6), ("h24", 24), ("d7", 168), ("d30", 720)):
                want = label in ("h6", "h24", "d7")
                (p, g, l, pv, gv, nev), hist, ha = _sum_window(st, now, hrs, want)
                w = {"min": round(p, 1), "max": round(p + g, 1), "listed": round(l, 1),
                     "hours": round(min(hrs, max(0.0, (now - st["first_ts"]) / 3600.0)), 2),
                     "avg_p": round(pv / p, 4) if p > 0 else None,
                     "avg_all": round((pv + gv) / (p + g), 4) if (p + g) > 0 else None, "ha": ha, "n": int(nev)}
                if want:
                    w["hist"] = {k: [round(x[0], 1), round(x[1], 1)] for k, x in hist.items() if x[0] + x[1] >= 0.5}
                res[label] = w
            t = st.get("top")
            res["top"] = {"g": t["g"], "amt": t["amt"], "age_h": round((now - t["since"]) / 3600.0, 2)} if t else None
            out[key] = res
    return out


def _act_save():
    try:
        with _act_lock:
            data = {"v": 3, "watch": dict(_watch), "scan": dict(_scan),
                    "act": {k: {"prev": {str(i): list(v) for i, v in (st["prev"] or {}).items()},
                                "prev_ts": st["prev_ts"], "first_ts": st["first_ts"], "snaps": st["snaps"],
                                "interval": st["interval"], "fine": st["fine"], "hour": st["hour"],
                                "day": st["day"], "top": st.get("top")} for k, st in _act.items()}}
        tmp = ACT_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f)
        os.replace(tmp, ACT_FILE)
    except Exception as e:
        print(f"[HAREKET] kayit hatasi: {e}")


def _act_load():
    try:
        with open(ACT_FILE) as f:
            data = _json.load(f)
        if data.get("v") not in (2, 3):
            return
        with _act_lock:
            _watch.update(data.get("watch") or {})
            _scan.update(data.get("scan") or {})
            for k, st in (data.get("act") or {}).items():
                _act[k] = {"prev": {int(i): tuple(v) for i, v in st["prev"].items()},
                           "prev_ts": st["prev_ts"], "first_ts": st["first_ts"], "snaps": st["snaps"],
                           "interval": st.get("interval", ACT_INTERVAL),
                           "fine": _fix_b(st["fine"]), "hour": _fix_b(st["hour"]), "day": _fix_b(st["day"]), "top": st.get("top")}
        print(f"[HAREKET] diskten yuklendi: {len(_act)} defter, {len(_scan)} tarama kaydi.")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[HAREKET] yukleme hatasi: {e}")


def _ask_min(key):
    return ASK_MIN_MAP.get(key.split(":", 1)[1], ASK_MIN_UNITS)


def _fix_b(d):
    out = {}
    for k, b in d.items():
        b = b if isinstance(b, dict) else {"v": list(b), "h": {}}   # eski liste kovalar
        b["v"] = list(b["v"]) + [0.0] * (6 - len(b["v"]))             # eski kovalar: alim-olayi sayaci yoktu
        out[k] = b
    return out


def _watch_interval(n_countries):
    """Izlenen ulke sayisi arttikca aralik otomatik uzar: tek is parcacigi tum istekleri yetistirebilsin,
    uzun gecikme satis tahminini bozmasin. 10 ulkede = WATCH_INTERVAL (600 sn), 60 ulkede ~ 27 dk."""
    n_keys = n_countries * len(ACT_ITEMS)
    glob = len(ACT_ITEMS) * REQ_COST / ACT_INTERVAL              # global defterlerin kapladigi zaman payi
    free = max(0.2, WATCH_UTIL - glob)
    return max(WATCH_INTERVAL, int(n_keys * REQ_COST / free))


def _act_tracked():
    out = [(f"0:{it}", ACT_INTERVAL) for it in ACT_ITEMS]
    with _act_lock:
        ws = list(_watch["countries"])[:MAX_WATCH]
    itv = _watch_interval(len(ws))
    for c in ws:
        out += [(f"{c}:{it}", itv) for it in ACT_ITEMS]
    return out


def _scan_keys():
    with _act_lock:
        skip = set(_watch["skip"]) | set(_watch["countries"]) | {"0"}
        extra = [c for c in (_watch.get("all") or []) if c not in SCAN_COUNTRIES]
    return [f"{c}:{it}" for c in (SCAN_COUNTRIES + extra) if c not in skip for it in ACT_ITEMS]


def _act_fetch(key, hdr):
    """Tek defter ceker. Basarisizlikta None; 403/429/5xx'te 5 dk mola verir."""
    global _act_pause_until
    c, i, q = key.split(":")
    try:
        r = requests.get(ACT_API.format(c=c, i=i, q=q), headers=hdr, timeout=15)
        if r.status_code in (403, 429, 500, 502, 503):
            _act_pause_until = time.time() + 300
            print(f"[HAREKET] {key} HTTP {r.status_code} - 5 dk mola")
            _stat(key, False, f"HTTP {r.status_code}")
            return None
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "ok":
            _stat(key, False, f"status={d.get('status')}")
            return None
        _stat(key, True)
        return d.get("offers") or []
    except Exception as e:
        print(f"[HAREKET] {key} hatasi: {e}")
        _stat(key, False, e)
        return None


def _act_loop():
    hdr = {"Accept": "application/json, text/plain, */*", "Referer": "https://erepublik.tools/",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    _act_load()
    due, scan_idx, last_scan, last_save = {}, 0, 0.0, time.time()
    while True:
        try:
            now = time.time()
            if now >= _act_pause_until:
                pick = None
                for key, itv in _act_tracked():
                    d = due.get(key, 0.0)
                    if d <= now and (pick is None or d < pick[2]):
                        pick = (key, itv, d)
                if pick:
                    key, itv, _ = pick
                    offers = _act_fetch(key, hdr)
                    due[key] = time.time() + itv
                    if offers is not None:
                        t = time.time()
                        _act_apply(key, offers, t, itv)
                        _scan_update(key, offers, t)
                elif SCAN_ENABLED and now - last_scan >= SCAN_GAP:
                    keys = _scan_keys()
                    if keys:
                        scan_idx %= len(keys)
                        offers = _act_fetch(keys[scan_idx], hdr)
                        scan_idx += 1
                        if offers is not None:
                            _scan_update(keys[scan_idx - 1], offers, time.time())
                    last_scan = time.time()
            if time.time() - last_save > 600:
                _act_save()
                last_save = time.time()
        except Exception as e:
            print(f"[HAREKET] dongu hatasi: {e}")
        time.sleep(1.5)


@app.route('/market-activity')
def market_activity():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    with _act_lock:
        stats = dict(_act_stats)
    return jsonify({"now": time.time(), "items": _act_summary(time.time()), "stats": stats})


@app.route('/market-scan')
def market_scan():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    with _act_lock:
        data = {k: dict(v) for k, v in _scan.items()}
        stats = dict(_act_stats)
    return jsonify({"now": time.time(), "scan": data, "gap": SCAN_GAP,
                    "keys": len(_scan_keys()) if SCAN_ENABLED else 0, "stats": stats})


@app.route('/market-watch', methods=['GET', 'POST'])
def market_watch():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    if request.method == 'POST':
        body = request.get_json(force=True, silent=True) or {}

        def ids(v, cap):
            out = []
            for x in (v or [])[:cap]:
                try:
                    s = str(int(x))
                except (TypeError, ValueError):
                    continue
                if s not in out and int(s) > 0:
                    out.append(s)
            return out
        with _act_lock:
            _watch["countries"] = ids(body.get("countries"), MAX_WATCH)
            _watch["skip"] = ids(body.get("skip"), 100)
            if body.get("all") is not None:
                _watch["all"] = ids(body.get("all"), 300)      # oyundaki TUM ulkeler: SCAN_COUNTRIES'te olmayanlar da taranir
            _watch["ts"] = time.time()
        _act_save()
    with _act_lock:
        snap = dict(_watch)
    n_scan = len(_scan_keys()) if SCAN_ENABLED else 0              # kilit dışında (kilit yeniden girilemez)
    return jsonify(dict(snap, max=MAX_WATCH, interval=_watch_interval(min(len(snap["countries"]), MAX_WATCH)), scan_keys=n_scan))


if ACT_ENABLED:
    threading.Thread(target=_act_loop, daemon=True).start()


# ============ DEFTER MERKEZI (cihazlar/tarayicilar arasi esitleme) ============
# Tampermonkey maliyet defteri (parti listesi + toplam istatistikler; kucuk bir JSON) burada TEK kopya olarak
# tutulur; boylece farkli tarayici/cihazlar ayni defteri gorur. Pazardan yakalanan alislar da once buraya
# yazilir (/ledger/buy), hangi cihaz depoyu once acarsa deftere isler.
# Render ucretsiz planda disk silinebilir: bu yuzden her istemci defterin YEREL kopyasini da saklar ve
# sunucu bos donerse kendi kopyasini geri yukler. Sunucuda ham gecmis tutulmaz, tek bir guncel defter durur.
LEDGER_FILE = os.environ.get("LEDGER_FILE", "/tmp/erp_ledger.json")
LEDGER_MAX_BYTES = 400 * 1024
LEDGER_MAX_BUYS = 300
_ledger = {"version": 0, "ledger": None, "buys": []}
_ledger_lock = threading.Lock()


def _ledger_save():
    try:
        tmp = LEDGER_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(_ledger, f)
        os.replace(tmp, LEDGER_FILE)
    except Exception as e:
        print(f"[DEFTER] kayit hatasi: {e}")


def _ledger_load():
    try:
        with open(LEDGER_FILE) as f:
            d = _json.load(f)
        with _ledger_lock:
            _ledger.update(version=int(d.get("version", 0)), ledger=d.get("ledger"), buys=list(d.get("buys") or []))
        print(f"[DEFTER] diskten yuklendi: surum {_ledger['version']}, {len(_ledger['buys'])} bekleyen alis.")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[DEFTER] yukleme hatasi: {e}")


_ledger_load()


@app.route('/ledger', methods=['GET', 'PUT'])
def ledger_endpoint():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    if request.method == 'GET':
        with _ledger_lock:
            return jsonify(dict(_ledger))
    body = request.get_json(force=True, silent=True) or {}
    data = body.get("ledger")
    if not isinstance(data, dict):
        return jsonify({"error": "ledger gerekli"}), 400
    if len(_json.dumps(data)) > LEDGER_MAX_BYTES:
        return jsonify({"error": "defter cok buyuk"}), 413
    try:
        base = int(body.get("base_version", -1))
    except (TypeError, ValueError):
        base = -1
    with _ledger_lock:
        if base != _ledger["version"]:          # baska cihaz araya girdi: istemci yeniden ceker
            return jsonify(dict(_ledger, conflict=True)), 409
        consumed = set(str(x) for x in (body.get("consumed") or []))
        _ledger["ledger"] = data
        _ledger["version"] += 1
        _ledger["buys"] = [b for b in _ledger["buys"] if str(b.get("id")) not in consumed]
        _ledger_save()
        return jsonify({"version": _ledger["version"]})


@app.route('/ledger/buy', methods=['POST'])
def ledger_buy():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    b = request.get_json(force=True, silent=True) or {}
    try:
        item = {"id": str(b["id"])[:64], "key": str(b["key"]), "qty": float(b["qty"]),
                "cost": float(b["cost"]), "t": float(b.get("t") or time.time() * 1000)}
        ok = item["qty"] > 0 and item["cost"] > 0 and len(item["key"].split(":")) == 2 \
            and all(x.isdigit() for x in item["key"].split(":"))
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        return jsonify({"error": "gecersiz alis"}), 400
    with _ledger_lock:
        if not any(x.get("id") == item["id"] for x in _ledger["buys"]):
            _ledger["buys"].append(item)
            del _ledger["buys"][:-LEDGER_MAX_BUYS]
            _ledger_save()
    return jsonify({"ok": True})


# Thread'i modul seviyesinde baslatiyoruz - Render'da "gunicorn app:app" gibi
# bir start command kullanilsa bile bot thread'i mutlaka baslasin diye.
_bot_thread = threading.Thread(target=bot_loop, daemon=True)
_bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
