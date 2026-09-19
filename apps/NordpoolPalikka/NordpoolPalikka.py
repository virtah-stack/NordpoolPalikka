"""
Nord Pool 15 min -hintapalikka — v2

Kaksi erillistä toimintahaaraa (ei yhdistetä):
  Haara A: haetaan ja analysoidaan koko tunnettu horisontti kun uutta
           dataa on saatavilla (julkaisuikkuna n. klo 12-16).
  Haara B: nykyisen/seuraavan vartin tilaseuranta, 15 min + 15 s offset.
           Siivoaa vanhentuneen datan ja kirjoittaa tilan levylle.

Kaikki sisäinen aikakäsittely UTC:ssä kesä-/talviajan vaihtumisen vuoksi.
Paikallista aikaa (Europe/Helsinki) käytetään vain:
  1) Nord Poolin "date"-parametrissa (paikallinen kalenteripäivä)
  2) JSON-tulosteen ihmisluettavassa aikaleimassa

HUOM ennen käyttöönottoa:
  - Tarkista service-kutsun tarkka muoto ja return_response-tuki oman
    AppDaemon-version kanssa (Kehitystyökalut > Toiminnot -näkymästä
    näkee tarkan YAML-muodon).
  - Tarkista vastauksen rakenne kertaalleen lokista ennen kuin luotat
    siihen täysin - tässä oletetaan HA:n dokumentoima muoto:
    {alue: [{"start": "...UTC...", "end": "...UTC...", "price": €/MWh}, ...]}
"""

import json
import os
from datetime import datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

import hassapi as hass

LOCAL_TZ = ZoneInfo("Europe/Helsinki")


class NordpoolPriceBlock(hass.Hass):

    def initialize(self):
        # --- konfiguraatio (apps.yaml) ---
        self.config_entry = self.args["config_entry"]
        self.area = self.args.get("area", "FI")
        self.currency = self.args.get("currency", "EUR")

        self.vat = float(self.args.get("vat", 0.255))
        self.margin = float(self.args.get("margin", 0.49))
        self.transfer = float(self.args.get("transfer", 4.83))
        self.tax = float(self.args.get("tax", 2.91788))

        self.cheap_quarter_pct = float(self.args.get("cheap_quarter_pct", 0.25))
        self.cheap_third_pct = float(self.args.get("cheap_third_pct", 1 / 3))
        self.expensive_third_pct = float(self.args.get("expensive_third_pct", 1 / 3))
        self.expensive_quarter_pct = float(self.args.get("expensive_quarter_pct", 0.25))

        self.history_retention = timedelta(
            hours=float(self.args.get("history_retention_hours", 2))
        )
        self.price_epsilon = float(self.args.get("price_epsilon", 0.01))

        self.state_file = self.args.get(
            "state_file", "/config/appdaemon/apps/nordpool_price_block/state.json"
        )
        self.public_json_file = self.args.get(
            "public_json_file", "/config/www/nordpool_price_series.json"
        )

        # --- tila ---
        # { utc_start_iso: {start_utc, end_utc, spot, energy, total,
        #                    rank, rank_pct, diff_from_average, luokat...} }
        self.price_series = {}
        self._load_state()

        now_utc = self._now_utc()

        # Käynnistyksessä: varmista että tämän paikallisen päivän data on tiedossa
        self._fetch_and_merge_day(self._local_date(now_utc))
        self._analyze_horizon()
        self._save_state()
        self._publish()

        # Haara A: tarkistetaan 15 min välein, mutta Nord Poolia kutsutaan
        # oikeasti vain julkaisuikkunassa klo 12-16 paikallista aikaa
        self.run_every(self._branch_a_check, now_utc + timedelta(seconds=5), 15 * 60)

        # Haara B: varttitilan seuranta, 15 min + 15 s offset
        self.run_every(
            self._branch_b_tick, self._next_quarter_boundary(now_utc), 15 * 60
        )
        self._branch_b_tick(None)

    # ------------------------------------------------------------------
    # Aika-apurit
    # ------------------------------------------------------------------

    def _now_utc(self):
        # self.datetime(aware=True) palauttaa HA:n paikallisen ajan;
        # muunnetaan heti UTC:ksi, jotta kaikki laskenta on yhdenmukaista
        # kesä-/talviajasta riippumatta.
        return self.datetime(aware=True).astimezone(timezone.utc)

    def _local_date(self, dt_utc):
        return dt_utc.astimezone(LOCAL_TZ).date()

    def _quarter_floor_utc(self, dt_utc):
        return dt_utc.replace(
            minute=(dt_utc.minute // 15) * 15, second=0, microsecond=0
        )

    def _next_quarter_boundary(self, now_utc):
        floor = self._quarter_floor_utc(now_utc)
        candidate = floor + timedelta(seconds=15)
        if candidate <= now_utc:
            candidate += timedelta(minutes=15)
        return candidate

    # ------------------------------------------------------------------
    # HAARA A - horisontin haku ja analyysi
    # ------------------------------------------------------------------

    def _branch_a_check(self, kwargs):
        now_utc = self._now_utc()
        local_hour = now_utc.astimezone(LOCAL_TZ).hour
        if not (12 <= local_hour < 16):
            return  # julkaisuikkunan ulkopuolella ei kutsuta Nord Poolia turhaan

        tomorrow = self._local_date(now_utc) + timedelta(days=1)
        if self._fetch_and_merge_day(tomorrow):
            self._analyze_horizon()
            self._save_state()
            self._publish()

    def _fetch_and_merge_day(self, local_date):
        """Hakee yhden paikallisen kalenteripäivän datan ja liittää sen
        sarjaan. Palauttaa True jos sarjaan lisättiin uusia jaksoja."""
        try:
            result = self.call_service(
                "nordpool/get_price_indices_for_date",
                config_entry=self.config_entry,
                date=local_date.isoformat(),
                areas=[self.area],
                currency=self.currency,
                resolution=15,
                return_response=True,
            )
        except Exception as e:
            # Huomisen data ei vielä julkaistu - odotettu tilanne ennen n. klo 13
            self.log(f"Hintadataa ei saatavilla ({local_date}): {e}", level="DEBUG")
            return False

        area_data = result.get(self.area, [])
        if not area_data:
            return False

        added_new = False
        for interval in area_data:
            start_utc = datetime.fromisoformat(interval["start"]).astimezone(
                timezone.utc
            )
            end_utc = datetime.fromisoformat(interval["end"]).astimezone(timezone.utc)
            key = start_utc.isoformat()
            if key not in self.price_series:
                added_new = True
            spot = interval["price"] / 10.0  # €/MWh -> c/kWh
            # Säilytetään mahdollinen aiemmin laskettu rank/luokitus (ei
            # nollata sitä uudelleenhaussa) - päivitetään vain hintakentät.
            entry = self.price_series.get(key, {})
            entry.update(
                {
                    "start_utc": start_utc.isoformat(),
                    "end_utc": end_utc.isoformat(),
                    "spot": spot,
                    "energy": spot * (1 + self.vat) + self.margin,
                    "total": spot * (1 + self.vat)
                    + self.margin
                    + self.transfer
                    + self.tax,
                }
            )
            # Oletusarvot vain aidosti uusille jaksoille: jos jakso on jo
            # mennyt eikä sitä koskaan analysoitu tulevaisuudessa ollessaan
            # (esim. pitkän restart-katkon jälkeen), näillä vältetään
            # puuttuvat avaimet JSON-tulosteessa ja entiteiteissä.
            entry.setdefault("rank", None)
            entry.setdefault("rank_pct", None)
            entry.setdefault("diff_from_average", None)
            entry.setdefault("is_cheapest_quarter", False)
            entry.setdefault("is_cheapest_third", False)
            entry.setdefault("is_expensive_third", False)
            entry.setdefault("is_expensive_quarter", False)
            self.price_series[key] = entry
        return added_new

    def _analyze_horizon(self):
        """Laskee rank/rank%/erot/luokat kaikille tulevaisuudessa oleville
        jaksoille (nykyinen mukaan lukien). Historia jätetään ennalleen -
        se säilyttää sen hetken luokituksensa jolloin se oli vielä
        tulevaisuutta."""
        now_utc = self._now_utc()

        future_items = [
            (k, v)
            for k, v in self.price_series.items()
            if datetime.fromisoformat(v["end_utc"]) > now_utc
        ]
        if not future_items:
            return

        n = len(future_items)
        avg = mean(v["spot"] for _, v in future_items)

        # Ordinal rank: ensisijainen avain hinta, toissijainen aikajärjestys
        ordered = sorted(future_items, key=lambda kv: (kv[1]["spot"], kv[0]))

        cheap_q_n = round(n * self.cheap_quarter_pct)
        cheap_t_n = round(n * self.cheap_third_pct)
        exp_t_n = round(n * self.expensive_third_pct)
        exp_q_n = round(n * self.expensive_quarter_pct)

        # Kynnysarvot (ei lukumääräleikkaus) tasapelien käsittelyyn
        cheap_q_th = ordered[cheap_q_n - 1][1]["spot"] if cheap_q_n else None
        cheap_t_th = ordered[cheap_t_n - 1][1]["spot"] if cheap_t_n else None
        exp_t_th = ordered[-exp_t_n][1]["spot"] if exp_t_n else None
        exp_q_th = ordered[-exp_q_n][1]["spot"] if exp_q_n else None

        eps = self.price_epsilon
        for rank, (key, item) in enumerate(ordered, start=1):
            price = item["spot"]
            item["rank"] = rank
            item["rank_pct"] = round(100 * rank / n, 1)
            item["diff_from_average"] = round(price - avg, 4)
            item["is_cheapest_quarter"] = (
                cheap_q_th is not None and price <= cheap_q_th + eps
            )
            item["is_cheapest_third"] = (
                cheap_t_th is not None and price <= cheap_t_th + eps
            )
            item["is_expensive_third"] = (
                exp_t_th is not None and price >= exp_t_th - eps
            )
            item["is_expensive_quarter"] = (
                exp_q_th is not None and price >= exp_q_th - eps
            )
            self.price_series[key] = item

    # ------------------------------------------------------------------
    # HAARA B - varttitilan seuranta
    # ------------------------------------------------------------------

    def _branch_b_tick(self, kwargs):
        now_utc = self._now_utc()
        self._prune_history(now_utc)

        current_key = self._quarter_floor_utc(now_utc).isoformat()
        next_key = (self._quarter_floor_utc(now_utc) + timedelta(minutes=15)).isoformat()

        current = self.price_series.get(current_key)
        nxt = self.price_series.get(next_key)

        self._publish_current_next(current, nxt)
        self._save_state()
        self._publish()

    def _prune_history(self, now_utc):
        threshold = now_utc - self.history_retention
        self.price_series = {
            k: v
            for k, v in self.price_series.items()
            if datetime.fromisoformat(v["end_utc"]) >= threshold
        }

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _publish_current_next(self, current, nxt):
        for prefix, item in (("current", current), ("next", nxt)):
            if item is None:
                continue
            start_local = (
                datetime.fromisoformat(item["start_utc"]).astimezone(LOCAL_TZ).isoformat()
            )
            end_local = (
                datetime.fromisoformat(item["end_utc"]).astimezone(LOCAL_TZ).isoformat()
            )
            self.set_state(
                f"sensor.nordpool_{prefix}_price",
                state=round(item["spot"], 4),
                attributes={
                    "unit_of_measurement": "c/kWh",
                    "start_local": start_local,
                    "end_local": end_local,
                },
            )
            self.set_state(
                f"sensor.nordpool_{prefix}_energy_price",
                state=round(item["energy"], 4),
                attributes={"unit_of_measurement": "c/kWh"},
            )
            self.set_state(
                f"sensor.nordpool_{prefix}_total_cost",
                state=round(item["total"], 4),
                attributes={"unit_of_measurement": "c/kWh"},
            )
            self.set_state(f"sensor.nordpool_{prefix}_rank", state=item.get("rank"))
            self.set_state(
                f"sensor.nordpool_{prefix}_difference_from_average",
                state=round(item.get("diff_from_average", 0), 4),
                attributes={"unit_of_measurement": "c/kWh"},
            )
            for flag in (
                "is_cheapest_quarter",
                "is_cheapest_third",
                "is_expensive_third",
                "is_expensive_quarter",
            ):
                self.set_state(
                    f"binary_sensor.nordpool_{prefix}_{flag}",
                    state="on" if item.get(flag) else "off",
                )

    def _publish(self):
        """Kirjoittaa koko sarjan (historia + horisontti) JSON-tiedostoksi
        Lovelacea varten."""
        try:
            rows = []
            for v in sorted(self.price_series.values(), key=lambda x: x["start_utc"]):
                row = dict(v)
                row["start_local"] = (
                    datetime.fromisoformat(v["start_utc"])
                    .astimezone(LOCAL_TZ)
                    .isoformat()
                )
                rows.append(row)
            with open(self.public_json_file, "w") as f:
                json.dump(rows, f)
        except OSError as e:
            self.log(f"Raakasarjan kirjoitus epäonnistui: {e}", level="WARNING")

    # ------------------------------------------------------------------
    # Tilan säilytys
    # ------------------------------------------------------------------

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self.price_series, f)
        except OSError as e:
            self.log(f"Tilan tallennus epäonnistui: {e}", level="WARNING")

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    self.price_series = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                self.log(f"Tilan lataus epäonnistui: {e}", level="WARNING")
                self.price_series = {}
