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

# --- GECE MODU (Filipinler hava enerji botu) ---
ERE_COOKIE = os.environ.get("ERE_COOKIE", "")
API_SECRET = os.environ.get("API_SECRET", "")
ERE_BASE_URL = "https://www.erepublik.com"
NIGHT_CHECK_INTERVAL_SEC = int(os.environ.get("NIGHT_CHECK_INTERVAL_SEC", "60"))

DEFENDER_MAX_ENERGY_PER_HIT = 4000
INVADER_DAMAGE_PER_ENERGY = 14.08
INVADER_NO_OPPONENT_MAX_ENERGY = 2000
INVADER_NO_OPPONENT_MAX_DAMAGE = 25000

# Aktif saat penceresi, gun bazli. weekday(): Pazartesi=0 ... Pazar=6, Sali=1.
DAILY_ACTIVE_WINDOWS = {1: (0, 6)}  # Sali: 00:00-06:00
DEFAULT_ACTIVE_WINDOW = (0, 8)      # Diger gunler: 00:00-08:00

# Gece modu SADECE elle (Tampermonkey panelindeki "Gece Modu" dugmesiyle)
# ACTIVE yapilirsa VE saat penceresi icindeyse calisir. Kapatilirsa ya da
# pencere disindaysa hicbir tarama/vurus yapmaz. Kasitli olarak process
# hafizasinda tutuluyor - sunucu yeniden baslarsa varsayilan KAPALI olur,
# yanlislikla "unutulmus acik" gece modu calismaz.
_night_mode = {"manual_enabled": False, "enabled_at": None}
_night_mode_lock = threading.Lock()

# Cerez geçersiz hale gelince (CSRF token alinamayinca) gece botu TAMAMEN
# durur, cerez degistirilip deploy edilene kadar tekrar denemez.
_night_halt = {"halted": False, "reason": None}
_night_halt_lock = threading.Lock()

# Round basina harcanan enerji (round_key -> spent)
_night_round_spent = {}
_night_round_spent_lock = threading.Lock()


def _cookie_fingerprint():
    import hashlib
    return hashlib.sha256(ERE_COOKIE.encode("utf-8")).hexdigest()[:16]


def night_set_halted(reason):
    with _night_halt_lock:
        _night_halt["halted"] = True
        _night_halt["reason"] = reason
        _night_halt["cookie_fingerprint"] = _cookie_fingerprint()


def night_is_halted():
    with _night_halt_lock:
        if not _night_halt["halted"]:
            return False
        if _night_halt.get("cookie_fingerprint") != _cookie_fingerprint():
            # Cerez degisti (yeni deploy) - otomatik temizle.
            _night_halt["halted"] = False
            _night_halt["reason"] = None
            return False
        return True


def night_round_key(battle_id, battle_zone_id):
    return f"{battle_id}:{battle_zone_id}"


def night_get_round_spent(battle_id, battle_zone_id):
    with _night_round_spent_lock:
        return _night_round_spent.get(night_round_key(battle_id, battle_zone_id), 0)


def night_add_round_spent(battle_id, battle_zone_id, amount):
    with _night_round_spent_lock:
        key = night_round_key(battle_id, battle_zone_id)
        _night_round_spent[key] = _night_round_spent.get(key, 0) + amount
        return _night_round_spent[key]


def night_is_within_active_window():
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("Europe/Istanbul"))
    except Exception:
        now = _dt.datetime.utcnow() + _dt.timedelta(hours=3)  # yedek: TR = UTC+3
    start_hour, end_hour = DAILY_ACTIVE_WINDOWS.get(now.weekday(), DEFAULT_ACTIVE_WINDOW)
    hour = now.hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def night_mode_is_running():
    """Su an gercekten tarama/vurus yapiyor mu? (elle acik + saat penceresi + halted degil)"""
    with _night_mode_lock:
        manual = _night_mode["manual_enabled"]
    return manual and night_is_within_active_window() and not night_is_halted()


def _check_api_secret():
    if not API_SECRET:
        return False
    key = request.headers.get("X-Api-Key") or request.args.get("key")
    return key == API_SECRET


@app.route('/gece-modu', methods=['GET'])
def gece_modu_get():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    with _night_mode_lock:
        manual = _night_mode["manual_enabled"]
        enabled_at = _night_mode["enabled_at"]
    return jsonify({
        "manual_enabled": manual,
        "enabled_at": enabled_at,
        "within_time_window": night_is_within_active_window(),
        "halted": night_is_halted(),
        "halt_reason": _night_halt.get("reason"),
        "actually_running": night_mode_is_running(),
    })


@app.route('/gece-modu', methods=['POST'])
def gece_modu_set():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled"))
    with _night_mode_lock:
        _night_mode["manual_enabled"] = enabled
        _night_mode["enabled_at"] = time.time() if enabled else None
    send_tg(f"Gece Modu {'AKTIF edildi' if enabled else 'PASIF edildi'} (panelden).")
    return jsonify({"ok": True, "manual_enabled": enabled, "actually_running": night_mode_is_running()})


def _ere_session():
    s = requests.Session()
    if ERE_COOKIE:
        s.headers.update({"Cookie": ERE_COOKIE})
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def _night_get_csrf_token(session):
    resp = session.get(f"{ERE_BASE_URL}/en", timeout=15)
    resp.raise_for_status()
    match = re.search(r'csrfToken\s*[:=]\s*[\'"]([a-zA-Z0-9\-_]+)[\'"]', resp.text)
    if not match:
        match = re.search(r'name="csrf-token"\s+content="([^"]+)"', resp.text)
    return match.group(1) if match else None


def _night_get_campaigns(session):
    resp = session.get(f"{ERE_BASE_URL}/en/military/campaigns-new", timeout=15)
    resp.raise_for_status()
    return resp.json()


def _night_get_pool_energy(session, battle_id, side_country_id):
    resp = session.get(
        f"{ERE_BASE_URL}/en/military/battlefield-choose-side-new/{battle_id}/{side_country_id}",
        timeout=15,
    )
    if not resp.ok:
        return None
    try:
        data = resp.json()
        val = data.get("inventory", {}).get("poolEnergy")
        return float(val) if val is not None else None
    except Exception:
        return None


def _night_get_battle_statistics(session, token, battle_id, zone_id, battle_zone_id):
    body = {
        "battleId": battle_id, "zoneId": zone_id, "action": "battleStatistics",
        "round": zone_id, "division": 11, "battleZoneId": battle_zone_id,
        "type": "damage", "leftPage": 1, "rightPage": 1, "_token": token,
    }
    resp = session.post(
        f"{ERE_BASE_URL}/en/military/battle-console", data=body,
        headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15,
    )
    if not resp.ok:
        return None
    return resp.json()


def _night_find_total_air_damage(side_stats):
    if not side_stats:
        return None
    for key in ("totalDamage", "airDamage", "damage"):
        if key in side_stats:
            try:
                return float(side_stats[key])
            except (TypeError, ValueError):
                pass
    return None


def _night_start_deploy(session, token, battle_id, battle_zone_id, side_country_id, total_energy):
    body = {
        "_token": token, "battleId": str(battle_id), "battleZoneId": str(battle_zone_id),
        "sideCountryId": str(side_country_id), "weaponQuality": "-1",
        "totalEnergy": str(int(total_energy)), "skinId": "18",
    }
    resp = session.post(
        f"{ERE_BASE_URL}/en/military/fightDeploy-startDeploy", data=body,
        headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15,
    )
    if not resp.ok:
        return {"ok": False, "reason": f"HTTP {resp.status_code}"}
    try:
        result = resp.json()
    except Exception:
        return {"ok": False, "reason": "Deploy yaniti JSON degil."}
    if result.get("error"):
        return {"ok": False, "reason": result.get("message") or result.get("error")}
    return {"ok": True}


def _night_play_energy_rule(opportunity, current_energy, round_spent_so_far):
    if current_energy is None or current_energy <= 0:
        return {"ok": False, "reason": "Kullanilabilir enerji yok."}

    if opportunity["philippines_is_defender"]:
        spent = round_spent_so_far or 0
        remaining = DEFENDER_MAX_ENERGY_PER_HIT - spent
        if remaining <= 0:
            return {"ok": False, "reason": "Savunma enerji sinirina ulasildi."}
        min_required = 200 if spent > 0 else 3000
        if current_energy < min_required:
            return {"ok": False, "reason": "Savunma: enerji esigine ulasilmadi."}
        return {"ok": True, "amount": min(current_energy, remaining)}

    if opportunity["philippines_is_invader"]:
        if not opportunity["opponent_has_hit"]:
            spent = round_spent_so_far or 0
            est_damage_so_far = spent * INVADER_DAMAGE_PER_ENERGY
            remaining_energy_cap = INVADER_NO_OPPONENT_MAX_ENERGY - spent
            remaining_damage_cap = INVADER_NO_OPPONENT_MAX_DAMAGE - est_damage_so_far
            if remaining_energy_cap <= 0 or remaining_damage_cap <= 0:
                return {"ok": False, "reason": "Karsida kimse yok: round sinirina ulasildi."}
            min_required = 200 if spent > 0 else 1500
            if current_energy < min_required:
                return {"ok": False, "reason": "Karsida kimse yok: enerji esigine ulasilmadi."}
            remaining_energy_for_damage_cap = max(0, int(remaining_damage_cap // INVADER_DAMAGE_PER_ENERGY))
            amount = min(current_energy, remaining_energy_cap, remaining_energy_for_damage_cap)
            if amount <= 0:
                return {"ok": False, "reason": "Karsida kimse yok: hasar sinirina ulasildi."}
            return {"ok": True, "amount": amount}

        required_our_damage = (opportunity["opponent_total_damage"] or 0) / 2
        needed_damage = max(0, required_our_damage - (opportunity["philippines_total_damage"] or 0))
        if needed_damage <= 0:
            return {"ok": False, "reason": "Hedef hasara zaten ulasildi."}
        needed_energy = int(-(-needed_damage // INVADER_DAMAGE_PER_ENERGY))
        if current_energy < 200:
            return {"ok": False, "reason": "Tamamlama icin en az 200 enerji bekleniyor."}
        return {"ok": True, "amount": min(current_energy, needed_energy)}

    return {"ok": False, "reason": "Filipinler tarafi belirlenemedi."}


def _night_find_opportunities(campaigns):
    countries = campaigns.get("countries", {})
    battles = campaigns.get("battles", {})
    opportunities = []
    for battle_id, battle in battles.items():
        inv_id = battle.get("inv", {}).get("id")
        def_id = battle.get("def", {}).get("id")
        inv_name = countries.get(str(inv_id), {}).get("name", "")
        def_name = countries.get(str(def_id), {}).get("name", "")
        is_invader = bool(re.search(r"philippines|filipinler", inv_name, re.I))
        is_defender = bool(re.search(r"philippines|filipinler", def_name, re.I))
        if not is_invader and not is_defender:
            continue
        for zone_id, division in (battle.get("div") or {}).items():
            if int(division.get("div", -1)) == 11 and not division.get("division_end"):
                opportunities.append({
                    "battle_id": battle_id, "battle_zone_id": zone_id, "zone_id": battle.get("zone_id"),
                    "inv_id": inv_id, "def_id": def_id,
                    "philippines_is_invader": is_invader, "philippines_is_defender": is_defender,
                })
    return opportunities


def _night_enrich(session, token, opp):
    stats = _night_get_battle_statistics(session, token, opp["battle_id"], opp["zone_id"], opp["battle_zone_id"])
    if not stats or stats.get("error"):
        return None
    philippines_id = opp["inv_id"] if opp["philippines_is_invader"] else opp["def_id"]
    opponent_id = opp["def_id"] if opp["philippines_is_invader"] else opp["inv_id"]
    opponent_fighters = (stats.get(str(opponent_id)) or {}).get("fighterData", {})
    opp["opponent_has_hit"] = len(opponent_fighters) > 0
    opp["philippines_total_damage"] = _night_find_total_air_damage(stats.get(str(philippines_id)))
    opp["opponent_total_damage"] = _night_find_total_air_damage(stats.get(str(opponent_id)))
    return opp


def run_night_check_once():
    if not ERE_COOKIE:
        return
    session = _ere_session()
    try:
        token = _night_get_csrf_token(session)
        if not token:
            night_set_halted("CSRF token alinamadi - cerez suresi dolmus olabilir.")
            send_tg("GECE MODU DURDURULDU: cerez gecersiz gorunuyor. ERE_COOKIE'yi guncelleyip "
                     "deploy edene kadar hicbir sey yapmayacagim.")
            return

        campaigns = _night_get_campaigns(session)
        for opp in _night_find_opportunities(campaigns):
            enriched = _night_enrich(session, token, opp)
            if not enriched:
                continue
            side_country_id = enriched["inv_id"] if enriched["philippines_is_invader"] else enriched["def_id"]
            current_energy = _night_get_pool_energy(session, enriched["battle_id"], side_country_id)
            round_spent = night_get_round_spent(enriched["battle_id"], enriched["battle_zone_id"])

            rule = _night_play_energy_rule(enriched, current_energy, round_spent)
            if not rule["ok"]:
                continue

            deploy = _night_start_deploy(
                session, token, enriched["battle_id"], enriched["battle_zone_id"], side_country_id, rule["amount"]
            )
            if not deploy["ok"]:
                continue

            new_total = night_add_round_spent(enriched["battle_id"], enriched["battle_zone_id"], rule["amount"])
            send_tg(
                "*Gece Modu - vurus yapildi*\n"
                f"Savas: {enriched['battle_id']} (round {enriched['battle_zone_id']})\n"
                f"Bu vurusta harcanan enerji: {int(rule['amount'])}\n"
                f"Bu round'da toplam harcanan: {int(new_total)}"
            )
    except Exception as exc:
        send_tg(f"Gece Modu hata verdi: {exc}")



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


@app.route('/status')
def night_status():
    if not _check_api_secret():
        return jsonify({"error": "unauthorized"}), 401
    with _night_round_spent_lock:
        round_state = dict(_night_round_spent)
    return jsonify({
        "gece_modu": {
            "manual_enabled": _night_mode["manual_enabled"],
            "within_time_window": night_is_within_active_window(),
            "halted": night_is_halted(),
            "halt_reason": _night_halt.get("reason"),
            "actually_running": night_mode_is_running(),
            "round_state": round_state,
        },
        "energy_state": _energy_state,
    })


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
    last_night_check = 0

    # --- ALTIN SABAH HATIRLATICISI ---
    # Turkiye saatiyle (UTC+3, DST yok) 09:45 ve 10:01'de "10 gold al" hatirlatmasi.
    # Render sunucusu UTC calisir, o yuzden hedef saatleri UTC'ye ceviriyoruz:
    # 09:45 TR = 06:45 UTC, 10:01 TR = 07:01 UTC.
    GOLD_REMINDER_TIMES_UTC = [(6, 45), (7, 1)]
    last_gold_reminder_date = {t: None for t in GOLD_REMINDER_TIMES_UTC}

    while True:
        try:
            now = time.time()

            # --- Gece Modu: elle acik + saat penceresi icinde + halted degilse
            # havada Filipinler taramasi/vurusu yap. Bu calisirken yogunlugu
            # artirmamak icin Is Ilanlari taramasi (Piyasa ve RW disinda kalan
            # tarama) duraklatilir; Piyasa (check_items) ve RW (client-driven
            # /rw-state) normal calismaya devam eder.
            gece_aktif = night_mode_is_running()
            if gece_aktif and now - last_night_check >= NIGHT_CHECK_INTERVAL_SEC:
                run_night_check_once()
                last_night_check = now

            if now - last_job_check >= JOB_CHECK_INTERVAL and not gece_aktif:
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
