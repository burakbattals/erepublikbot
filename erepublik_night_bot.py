"""
eRepublik Filipinler hava savaşı - gece botu (sunucu tarafı, tarayıcısız).

ÇALIŞMA MANTIĞI
----------------
- APScheduler her CHECK_INTERVAL_SECONDS'te bir tetiklenir.
- Sadece ACTIVE_START_HOUR - ACTIVE_END_HOUR aralığında (yerel saat,
  varsayılan Europe/Istanbul) çalışır; bu aralığın dışında hiçbir şey
  yapmadan çıkar. Yani sabah olunca kendiliğinden durur, tekrar aktif
  etmene gerek yok.
- Filipinler'in savunduğu/saldırdığı uygun bir hava round'u bulunca,
  Tampermonkey scriptindeki ile aynı enerji kurallarını uygular ve
  fightDeploy-startDeploy isteğini doğrudan sunucudan atar (tarayıcı
  gerekmez).
- Her başarılı vuruştan sonra Telegram'a rapor gönderir: hangi savaş,
  ne kadar enerji harcandı, bu round'da toplam ne kadar harcandı.

GÜVENLİK / GİZLİLİK
--------------------
- Hiçbir sır (oturum çerezi, Telegram token'ı, API anahtarı) kod içine
  YAZILMAZ; hepsi Render > Environment ekranından ortam değişkeni (env
  var) olarak girilir. Kod sadece bunları okur.
- /status endpoint'i API_SECRET ile korunur (?key=... veya
  X-Api-Key header'ı). Anahtarsız istek 401 döner.
- Render'daki repo GitHub'da public olsa bile bu dosyada sır bulunmadığı
  için sorun olmaz; sırlar sadece Render'ın kendi ortamında durur.

GEREKLİ ORTAM DEĞİŞKENLERİ (Render > Environment)
--------------------------------------------------
ERE_COOKIE            eRepublik'e giriş yapılmış tarayıcıdan alınan tam
                       Cookie header'ı (DevTools > Network > herhangi bir
                       istek > Request Headers > Cookie). Örn:
                       "laravel_session=...; ep2_lg=..."
API_SECRET             /status endpoint'ini korumak için kendi seçtiğin
                       rastgele bir metin.
TELEGRAM_BOT_TOKEN      Mevcut Telegram botunun token'ı.
TELEGRAM_CHAT_ID        Rapor gönderilecek chat id.
TIMEZONE                (opsiyonel, varsayılan "Europe/Istanbul")
CHECK_INTERVAL_SECONDS  (opsiyonel, varsayılan 60)

Aktif çalışma saatleri kod içinde DAILY_ACTIVE_WINDOWS ile sabit: Salı
günleri 00:00-06:00, diğer günler 00:00-08:00. Değiştirmek istersen bu
sözlüğü düzenlemen yeterli.

Çerez (ERE_COOKIE) geçersiz hale gelirse (CSRF token alınamayınca) bot
kendini tamamen durdurur ve Telegram'a haber verir; sen ERE_COOKIE'yi
güncelleyip yeniden deploy edene kadar hiçbir tarama/vuruş yapmaz.

Bu dosya mevcut Flask app.py'nin yanına eklenip oradan import edilebilir,
ya da tek başına `python erepublik_night_bot.py` ile de çalıştırılabilir.
"""

import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request

# ----------------------------------------------------------------------
# Ayarlar / ortam değişkenleri
# ----------------------------------------------------------------------

ERE_COOKIE = os.environ.get("ERE_COOKIE", "")
API_SECRET = os.environ.get("API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TIMEZONE = os.environ.get("TIMEZONE", "Europe/Istanbul")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))

# Gün bazlı aktif pencere: Salı günleri kısa (00:00-06:00), diğer günler
# normal (00:00-08:00). weekday(): Pazartesi=0 ... Pazar=6, Salı=1.
DAILY_ACTIVE_WINDOWS = {
    1: (0, 6),  # Salı
}
DEFAULT_ACTIVE_WINDOW = (0, 8)

STATE_FILE = "/tmp/erepbot_round_state.json"
HALT_FILE = "/tmp/erepbot_halted.json"

BASE_URL = "https://www.erepublik.com"

DEFENDER_MAX_ENERGY_PER_HIT = 4000
INVADER_DAMAGE_PER_ENERGY = 14.08
INVADER_NO_OPPONENT_MAX_ENERGY = 2000
INVADER_NO_OPPONENT_MAX_DAMAGE = 25000

# ----------------------------------------------------------------------
# Basit round durumu (round başına harcanan enerji) - /tmp'e yazılır ki
# process yeniden başlarsa da tamamen sıfırlanmasın.
# ----------------------------------------------------------------------


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def get_round_spent(battle_id, battle_zone_id):
    state = _load_state()
    entry = state.get(str(battle_id))
    if not entry or str(entry.get("battle_zone_id")) != str(battle_zone_id):
        return 0
    return entry.get("spent", 0)


def _cookie_fingerprint():
    # Çerezin kendisini diske yazmadan, "aynı çerez mi değişti mi"
    # anlamak için kısa bir parmak izi tutuyoruz.
    import hashlib

    return hashlib.sha256(ERE_COOKIE.encode("utf-8")).hexdigest()[:16]


def get_halt_state():
    try:
        with open(HALT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def set_halted(reason):
    with open(HALT_FILE, "w", encoding="utf-8") as f:
        json.dump({"halted": True, "reason": reason, "cookie_fingerprint": _cookie_fingerprint(), "since": time.time()}, f)


def clear_halt():
    try:
        os.remove(HALT_FILE)
    except FileNotFoundError:
        pass


def is_halted():
    """Çerez hatasından sonra durdurulduysak ve ERE_COOKIE hâlâ o hatalı
    değerse True döner. Çerezi değiştirip yeniden deploy edince
    fingerprint değişeceği için otomatik olarak tekrar başlar."""
    state = get_halt_state()
    if not state or not state.get("halted"):
        return False
    if state.get("cookie_fingerprint") != _cookie_fingerprint():
        clear_halt()
        return False
    return True


def add_round_spent(battle_id, battle_zone_id, amount):
    state = _load_state()
    existing = state.get(str(battle_id))
    previous = existing.get("spent", 0) if existing and str(existing.get("battle_zone_id")) == str(battle_zone_id) else 0
    state[str(battle_id)] = {
        "battle_zone_id": str(battle_zone_id),
        "spent": previous + amount,
        "updated_at": time.time(),
    }
    _save_state(state)


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


# ----------------------------------------------------------------------
# eRepublik istekleri
# ----------------------------------------------------------------------


def build_session():
    s = requests.Session()
    if ERE_COOKIE:
        s.headers.update({"Cookie": ERE_COOKIE})
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s


def get_csrf_token(session):
    resp = session.get(f"{BASE_URL}/en", timeout=15)
    resp.raise_for_status()
    match = re.search(r'csrfToken\s*[:=]\s*[\'"]([a-zA-Z0-9\-_]+)[\'"]', resp.text)
    if not match:
        match = re.search(r'name="csrf-token"\s+content="([^"]+)"', resp.text)
    return match.group(1) if match else None


def get_campaigns(session):
    resp = session.get(f"{BASE_URL}/en/military/campaigns-new", timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_pool_energy(session, battle_id, battle_zone_id, side_country_id):
    resp = session.get(
        f"{BASE_URL}/en/military/battlefield-choose-side-new/{battle_id}/{side_country_id}",
        timeout=15,
    )
    if not resp.ok:
        return None
    try:
        data = resp.json()
        inventory = data.get("inventory", {})
        val = inventory.get("poolEnergy")
        return float(val) if val is not None else None
    except Exception:
        return None


def get_battle_statistics(session, token, battle_id, zone_id, battle_zone_id):
    body = {
        "battleId": battle_id,
        "zoneId": zone_id,
        "action": "battleStatistics",
        "round": zone_id,
        "division": 11,
        "battleZoneId": battle_zone_id,
        "type": "damage",
        "leftPage": 1,
        "rightPage": 1,
        "_token": token,
    }
    resp = session.post(
        f"{BASE_URL}/en/military/battle-console",
        data=body,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=15,
    )
    if not resp.ok:
        return None
    return resp.json()


def find_total_air_damage(side_stats):
    if not side_stats:
        return None
    for key in ("totalDamage", "airDamage", "damage"):
        if key in side_stats:
            try:
                return float(side_stats[key])
            except (TypeError, ValueError):
                pass
    return None


def start_deploy(session, token, battle_id, battle_zone_id, side_country_id, total_energy):
    body = {
        "_token": token,
        "battleId": str(battle_id),
        "battleZoneId": str(battle_zone_id),
        "sideCountryId": str(side_country_id),
        "weaponQuality": "-1",
        "totalEnergy": str(int(total_energy)),
        "skinId": "18",
    }
    resp = session.post(
        f"{BASE_URL}/en/military/fightDeploy-startDeploy",
        data=body,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=15,
    )
    if not resp.ok:
        return {"ok": False, "reason": f"HTTP {resp.status_code}"}
    try:
        result = resp.json()
    except Exception:
        return {"ok": False, "reason": "Deploy yanıtı JSON değil."}
    if result.get("error"):
        return {"ok": False, "reason": result.get("message") or result.get("error")}
    return {"ok": True}


# ----------------------------------------------------------------------
# Enerji kuralı (Tampermonkey scriptiyle aynı mantık)
# ----------------------------------------------------------------------


def play_energy_rule(opportunity, current_energy, round_spent_so_far):
    if current_energy is None or current_energy <= 0:
        return {"ok": False, "reason": "Kullanılabilir enerji yok."}

    if opportunity["philippines_is_defender"]:
        spent = round_spent_so_far or 0
        remaining = DEFENDER_MAX_ENERGY_PER_HIT - spent
        if remaining <= 0:
            return {"ok": False, "reason": "Bu round için savunma enerji sınırına zaten ulaşıldı."}
        min_required = 0 if opportunity.get("urgent_catch_up") else (200 if spent > 0 else 3000)
        if current_energy < min_required:
            return {"ok": False, "reason": "Savunma: enerji eşiğine ulaşılmadı."}
        return {"ok": True, "amount": min(current_energy, remaining)}

    if opportunity["philippines_is_invader"]:
        if not opportunity["opponent_has_hit"]:
            spent = round_spent_so_far or 0
            est_damage_so_far = spent * INVADER_DAMAGE_PER_ENERGY
            remaining_energy_cap = INVADER_NO_OPPONENT_MAX_ENERGY - spent
            remaining_damage_cap = INVADER_NO_OPPONENT_MAX_DAMAGE - est_damage_so_far
            if remaining_energy_cap <= 0 or remaining_damage_cap <= 0:
                return {"ok": False, "reason": "Karşıda kimse yok: round sınırına ulaşıldı, rakip bekleniyor."}
            min_required = 200 if spent > 0 else 1500
            if current_energy < min_required:
                return {"ok": False, "reason": "Karşıda kimse yok: enerji eşiğine ulaşılmadı."}
            remaining_energy_for_damage_cap = max(0, int(remaining_damage_cap // INVADER_DAMAGE_PER_ENERGY))
            amount = min(current_energy, remaining_energy_cap, remaining_energy_for_damage_cap)
            if amount <= 0:
                return {"ok": False, "reason": "Karşıda kimse yok: hasar sınırına ulaşıldı."}
            return {"ok": True, "amount": amount}

        required_our_damage = (opportunity["opponent_total_damage"] or 0) / 2
        needed_damage = max(0, required_our_damage - (opportunity["philippines_total_damage"] or 0))
        if needed_damage <= 0:
            return {"ok": False, "reason": "Hedef hasara zaten ulaşıldı."}
        needed_energy = int(-(-needed_damage // INVADER_DAMAGE_PER_ENERGY))  # ceil
        if current_energy < 200:
            return {"ok": False, "reason": "Tamamlama için en az 200 enerji bekleniyor."}
        return {"ok": True, "amount": min(current_energy, needed_energy)}

    return {"ok": False, "reason": "Filipinler tarafı belirlenemedi."}


# ----------------------------------------------------------------------
# Ana tarama/vuruş döngüsü
# ----------------------------------------------------------------------


def find_philippines_air_opportunities(session, campaigns):
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
                opportunities.append(
                    {
                        "battle_id": battle_id,
                        "battle_zone_id": zone_id,
                        "zone_id": battle.get("zone_id"),
                        "inv_id": inv_id,
                        "def_id": def_id,
                        "philippines_is_invader": is_invader,
                        "philippines_is_defender": is_defender,
                    }
                )
    return opportunities


def enrich_opportunity(session, token, opp):
    stats = get_battle_statistics(session, token, opp["battle_id"], opp["zone_id"], opp["battle_zone_id"])
    if not stats or stats.get("error"):
        return None
    philippines_id = opp["inv_id"] if opp["philippines_is_invader"] else opp["def_id"]
    opponent_id = opp["def_id"] if opp["philippines_is_invader"] else opp["inv_id"]
    opponent_fighters = (stats.get(str(opponent_id)) or {}).get("fighterData", {})
    opp["opponent_has_hit"] = len(opponent_fighters) > 0
    opp["philippines_total_damage"] = find_total_air_damage(stats.get(str(philippines_id)))
    opp["opponent_total_damage"] = find_total_air_damage(stats.get(str(opponent_id)))
    return opp


def run_check_once():
    if not ERE_COOKIE:
        send_telegram("eRepublik gece botu: ERE_COOKIE ayarlanmamış, çalışamıyorum.")
        return

    session = build_session()
    try:
        token = get_csrf_token(session)
        if not token:
            set_halted("CSRF token alınamadı - çerez süresi dolmuş olabilir.")
            send_telegram(
                "eRepublik gece botu DURDURULDU: çerez geçersiz görünüyor "
                "(CSRF token alınamadı). ERE_COOKIE'yi güncelleyip deploy edene "
                "kadar hiçbir şey yapmayacağım."
            )
            return

        campaigns = get_campaigns(session)
        opportunities = find_philippines_air_opportunities(session, campaigns)
        if not opportunities:
            return

        for opp in opportunities:
            enriched = enrich_opportunity(session, token, opp)
            if not enriched:
                continue

            side_country_id = enriched["inv_id"] if enriched["philippines_is_invader"] else enriched["def_id"]
            current_energy = get_pool_energy(session, enriched["battle_id"], enriched["battle_zone_id"], side_country_id)
            round_spent = get_round_spent(enriched["battle_id"], enriched["battle_zone_id"])

            rule = play_energy_rule(enriched, current_energy, round_spent)
            if not rule["ok"]:
                continue

            deploy = start_deploy(
                session, token, enriched["battle_id"], enriched["battle_zone_id"], side_country_id, rule["amount"]
            )
            if not deploy["ok"]:
                # Başarısız deploy'u round'a işlemiyoruz, sessizce bir sonraki
                # taramada tekrar denenecek.
                continue

            add_round_spent(enriched["battle_id"], enriched["battle_zone_id"], rule["amount"])
            new_total = get_round_spent(enriched["battle_id"], enriched["battle_zone_id"])
            send_telegram(
                "eRepublik gece botu - vuruş yapıldı\n"
                f"Savaş: {enriched['battle_id']} (round {enriched['battle_zone_id']})\n"
                f"Bu vuruşta harcanan enerji: {int(rule['amount'])}\n"
                f"Bu round'da toplam harcanan: {int(new_total)}"
            )
            # Bir taramada birden fazla round'a birden vurmayalım, sırayla devam.
    except Exception as exc:  # noqa: BLE001
        send_telegram(f"eRepublik gece botu hata verdi: {exc}")


def is_within_active_window():
    now = datetime.now(ZoneInfo(TIMEZONE))
    start_hour, end_hour = DAILY_ACTIVE_WINDOWS.get(now.weekday(), DEFAULT_ACTIVE_WINDOW)
    hour = now.hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    # Gece yarısını aşan aralık (örn. 23 - 7)
    return hour >= start_hour or hour < end_hour


def scheduled_job():
    if is_halted():
        return
    if not is_within_active_window():
        return
    run_check_once()


# ----------------------------------------------------------------------
# Flask app + scheduler
# ----------------------------------------------------------------------

app = Flask(__name__)
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(scheduled_job, "interval", seconds=CHECK_INTERVAL_SECONDS, id="erepbot_night_job")
scheduler.start()


def check_auth():
    if not API_SECRET:
        return False
    key = request.headers.get("X-Api-Key") or request.args.get("key")
    return key == API_SECRET


@app.route("/status")
def status():
    if not check_auth():
        return jsonify({"error": "unauthorized"}), 401
    state = _load_state()
    now = datetime.now(ZoneInfo(TIMEZONE))
    start_hour, end_hour = DAILY_ACTIVE_WINDOWS.get(now.weekday(), DEFAULT_ACTIVE_WINDOW)
    return jsonify(
        {
            "server_time": now.isoformat(),
            "todays_window": f"{start_hour:02d}:00-{end_hour:02d}:00 ({TIMEZONE})",
            "currently_active": is_within_active_window(),
            "halted": is_halted(),
            "halt_info": get_halt_state(),
            "check_interval_seconds": CHECK_INTERVAL_SECONDS,
            "round_state": state,
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port)
