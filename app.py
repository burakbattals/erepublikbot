# requests.Session() kullanarak çerezlerin ve oturumun istekler arasında taşınmasını sağlıyoruz
    session = requests.Session()
    session.headers.update(HDR)

    def fetch_csrf_token(b_id):
        try:
            # Ana savaş sayfasından güncel tokeni alıyoruz
            url = f"https://www.erepublik.com/tr/military/battlefield/{b_id}"
            resp = session.get(url, timeout=10)
            
            m = re.search(r"csrfToken[\"']?\s*[:=]\s*[\"']([a-f0-9]{20,40})[\"']", resp.text)
            if m:
                if DEBUG:
                    print(f"[TANI] csrfToken alindi ({b_id}): {m.group(1)[:8]}...")
                return m.group(1)

            print(f"[TANI] csrfToken bulunamadi ({b_id}). Status: {resp.status_code}")
        except Exception as e:
            print(f"csrfToken cekme hatasi: {e}")
        return None

    def check_fighters(b_id, round_number, d_num, sub_id, inv_id, def_id):
        token = fetch_csrf_token(b_id)
        if not token:
            return None, None

        body = {
            "battleId": b_id,
            "zoneId": round_number,
            "action": "battleStatistics",
            "round": round_number,
            "division": d_num,
            "battleZoneId": sub_id,
            "type": "damage",
            "leftPage": 1,
            "rightPage": 1,
            "_token": token,
        }

        try:
            # session.post kullanarak oturum çerezleriyle birlikte istek atıyoruz
            resp = session.post("https://www.erepublik.com/tr/military/battle-console",
                                  data=body, timeout=10)
            if DEBUG:
                print(f"-> battle-console istek ({b_id}/{d_num}/{sub_id}) status: {resp.status_code}")

            if resp.status_code == 200 and "json" in resp.headers.get("Content-Type", ""):
                resp_data = resp.json()
                if DEBUG:
                    print(f"[TANI] battle-console JSON: {str(resp_data)[:1500]}")

                inv_fd = resp_data.get(str(inv_id), {}).get("fighterData", {})
                def_fd = resp_data.get(str(def_id), {}).get("fighterData", {})
                return (len(inv_fd) > 0, len(def_fd) > 0)
            else:
                if DEBUG:
                    print(f"[TANI] battle-console beklenmeyen yanit: {resp.status_code} {resp.text[:200]}")
                return None, None
        except Exception as ex:
            print(f"battle-console istek hatasi: {ex}")
            return None, None
