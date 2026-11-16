"""外匯追蹤的純計算部分：CSV 解析、路徑搜尋、偏離與滯後、歷史瘦身、提醒。

    python -m unittest tests.test_fx

不連網。臺銀的 CSV 範例是照牌告檔的版面手寫的（數字是編的），
不是從線上抓回來的原檔；玉山與台新的樣本則是真實回應。
"""

import copy
import json
import unittest
from pathlib import Path

from premarket import fx

# 玉山的樣本是從線上真實回應裁下來的（2026-11-16 晚上），只留用得到的欄位。
ESUN_RAW = json.loads((Path(__file__).parent / "fixtures" / "esun_rates.json").read_text(encoding="utf-8"))
# 台新的樣本是線上真實回應的原檔（2026-11-17 清晨），沒有裁剪。
TAISHIN_JS = (Path(__file__).parent / "fixtures" / "taishin_export.js").read_text(encoding="utf-8")

HEADER = (
    "幣別,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天,"
    "匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天"
)
CSV = "﻿" + "\n".join([
    HEADER,
    "USD,本行買入,32.56500,32.91500,32.90700,32.85300,32.78100,32.71200,32.64800,32.58200,32.52000,"
    "本行賣出,33.23500,33.01500,33.01100,32.96100,32.89500,32.83000,32.77100,32.71000,32.65200",
    "HKD,本行買入,4.06500,4.19100,0,0,0,0,0,0,0,本行賣出,4.26900,4.25100,0,0,0,0,0,0,0",
    "JPY,本行買入,0.19750,0.20430,0.20440,0.20470,0.20520,0.20570,0.20620,0.20670,0.20720,"
    "本行賣出,0.21030,0.20830,0.20850,0.20890,0.20950,0.21000,0.21060,0.21120,0.21170",
    "EUR,本行買入,35.56000,36.10000,36.11000,36.13000,36.16000,36.19000,36.22000,36.25000,36.28000,"
    "本行賣出,36.90000,36.50000,36.52000,36.55000,36.59000,36.62000,36.66000,36.70000,36.73000",
    "ZAR,本行買入,0.00000,1.86000,0,0,0,0,0,0,0,本行賣出,0.00000,1.94000,0,0,0,0,0,0,0",
]) + "\n"

BOT = fx.parse_bot_csv(CSV)
T0 = "2026-11-16T10:00:00+08:00"


def route(p):
    return [s["to"] for s in p["steps"]]


class ParseTest(unittest.TestCase):
    def test_only_tracked_currencies(self):
        self.assertEqual(set(BOT), {"USD", "JPY", "EUR"})

    def test_cash_and_spot_land_in_the_right_fields(self):
        self.assertEqual(
            BOT["USD"],
            {"cash_buy": 32.565, "spot_buy": 32.915, "cash_sell": 33.235, "spot_sell": 33.015},
        )
        self.assertEqual(BOT["JPY"]["cash_sell"], 0.2103)

    def test_survives_a_change_in_forward_columns(self):
        # 遠期欄位少了幾欄：序號全部位移，靠「買入／賣出」標記仍然對得上。
        short = "USD,本行買入,32.565,32.915,32.9,本行賣出,33.235,33.015,33.0\n"
        self.assertEqual(fx.parse_bot_csv(short)["USD"]["spot_sell"], 33.015)

    def test_zero_means_not_quoted(self):
        q = fx.parse_bot_csv("EUR,本行買入,0.00000,36.1,本行賣出,0.00000,36.5\n")["EUR"]
        self.assertIsNone(q["cash_buy"])
        self.assertEqual(q["spot_buy"], 36.1)

    def test_challenge_page_is_not_parsed_as_data(self):
        with self.assertRaises(fx.ChallengeError):
            fx.parse_bot_csv("<!DOCTYPE html><html><title>Challenge Validation</title>")

    def test_empty_csv_raises(self):
        with self.assertRaises(ValueError):
            fx.parse_bot_csv(HEADER + "\n")


class EsunTest(unittest.TestCase):
    def test_real_response(self):
        got = fx.parse_esun(ESUN_RAW)
        self.assertEqual(set(got["quotes"]), {"USD", "JPY", "EUR"})  # 泰銖不追蹤
        self.assertEqual(
            got["quotes"]["USD"],
            {"cash_buy": 32.63, "cash_sell": 33.18, "spot_buy": 32.88, "spot_sell": 32.98},
        )
        self.assertEqual(got["quotes"]["JPY"]["cash_sell"], 0.2104)
        self.assertEqual(got["bonus"], {"USD": 0.031, "JPY": 0.0012, "EUR": 0.12})
        self.assertEqual(got["board_time"], "2026-11-16T22:01:40+08:00")

    def test_discont_flag_is_ignored(self):
        # 這個旗標在不同節點之間隨機跳，優惠值沒變；跟著它走會讓成本忽高忽低。
        raw = {**ESUN_RAW, "DiscontFlag": ""}
        self.assertEqual(fx.parse_esun(raw)["bonus"], fx.parse_esun(ESUN_RAW)["bonus"])

    def test_bonus_must_agree_with_the_published_better_rate(self):
        raw = copy.deepcopy(ESUN_RAW)
        usd = next(r for r in raw["Rates"] if r["CCY"] == "USD/TWD")
        usd["BuyIncreaseRate"] = ""          # 優惠價沒掛出來
        jpy = next(r for r in raw["Rates"] if r["CCY"] == "JPY/TWD")
        jpy["Bonus"] = "0.01"                # 和優惠價對不起來
        self.assertEqual(fx.parse_esun(raw)["bonus"], {"EUR": 0.12})

    def test_unquoted_cash_is_none(self):
        raw = copy.deepcopy(ESUN_RAW)
        eur = next(r for r in raw["Rates"] if r["CCY"] == "EUR/TWD")
        eur["CashBBoardRate"] = eur["CashSBoardRate"] = ""
        q = fx.parse_esun(raw)["quotes"]["EUR"]
        self.assertIsNone(q["cash_buy"])
        self.assertIsNone(q["cash_sell"])
        self.assertEqual(q["spot_buy"], 36.08)

    def test_no_tracked_currency_raises(self):
        with self.assertRaises(ValueError):
            fx.parse_esun({"Rates": [r for r in ESUN_RAW["Rates"] if r["CCY"] == "THB/TWD"]})

    def test_online_discount_is_what_the_bank_publishes(self):
        got = fx.parse_esun(ESUN_RAW)
        q = fx.apply_discount(got["quotes"]["USD"], got["bonus"]["USD"])
        self.assertAlmostEqual(q["spot_buy"], 32.911)   # = BuyIncreaseRate
        self.assertAlmostEqual(q["spot_sell"], 32.949)  # = SellDecreaseRate


class TaishinTest(unittest.TestCase):
    def test_real_response(self):
        got = fx.parse_taishin(TAISHIN_JS)
        self.assertEqual(set(got["quotes"]), {"USD", "JPY", "EUR"})
        self.assertEqual(
            got["quotes"]["USD"],
            {"cash_buy": 32.663, "cash_sell": 33.14, "spot_buy": 32.865, "spot_sell": 32.965},
        )
        # 這兩格在原檔裡後面跟著「近7日最高」的標籤，標籤裡的 7 不能被當成匯率
        self.assertEqual(got["quotes"]["JPY"]["spot_buy"], 0.2044)
        self.assertEqual(got["quotes"]["JPY"]["cash_buy"], 0.2024)
        self.assertEqual(
            got["quotes"]["EUR"],
            {"cash_buy": 35.685, "cash_sell": 36.701, "spot_buy": 36.018, "spot_sell": 36.368},
        )
        self.assertEqual(got["board_time"], "2026-11-17T05:37:56+08:00")

    def test_every_quote_is_a_sane_two_sided_market(self):
        for ccy, q in fx.parse_taishin(TAISHIN_JS)["quotes"].items():
            self.assertLess(q["cash_buy"], q["spot_buy"], ccy)
            self.assertLess(q["spot_buy"], q["spot_sell"], ccy)
            self.assertLess(q["spot_sell"], q["cash_sell"], ccy)

    @staticmethod
    def export(rows: str, header: str = "即期買入,即期賣出,現鈔買入,現鈔賣出") -> str:
        """照原檔的寫法組一份小的匯出檔：每行一個 document.writeln，引號與斜線有跳脫。"""
        html = ["<p>更新時間 : 2026/11/17 10:15:00</p>", '<table class="right">', "<tr>"]
        html += [f"<td>{h}</td>" for h in header.split(",")] + ["</tr>"]
        for line in rows.strip().splitlines():
            ccy, *vals = line.split(",")
            html.append("<tr>")
            html += [f'<td class="currency">{v}</td>' for v in vals]
            html += [f"<td><a onclick=\"queryhistory('{ccy}')\">查詢</a></td>", "</tr>"]
        html.append("</table>")
        esc = lambda s: s.replace("/", "\\/").replace('"', '\\"').replace("'", "\\'")
        return "\n".join(f"document.writeln('{esc(h)}');" for h in html) + "\n"

    def test_unquoted_cash_and_labels(self):
        js = self.export("""
USD,32.8650,32.9650,-,-
JPY,0.2044 <br><label class="highest">近7日最高 </label>,0.2079,- <br><label class="lowest">近30日最低 </label>,0.2104
GBP,43.1720,43.6120,-,-
""")
        got = fx.parse_taishin(js)
        self.assertEqual(set(got["quotes"]), {"USD", "JPY"})          # 英鎊不追蹤
        self.assertEqual(got["quotes"]["USD"], {"cash_buy": None, "cash_sell": None,
                                                "spot_buy": 32.865, "spot_sell": 32.965})
        self.assertIsNone(got["quotes"]["JPY"]["cash_buy"])           # "-" 後面的「近30日最低」不是價格
        self.assertEqual(got["quotes"]["JPY"]["cash_sell"], 0.2104)
        self.assertEqual(got["board_time"], "2026-11-17T10:15:00+08:00")

    def test_columns_follow_the_header_not_a_fixed_order(self):
        js = self.export("USD,32.6630,33.1400,32.8650,32.9650", header="現鈔買入,現鈔賣出,即期買入,即期賣出")
        self.assertEqual(
            fx.parse_taishin(js)["quotes"]["USD"],
            {"cash_buy": 32.663, "cash_sell": 33.14, "spot_buy": 32.865, "spot_sell": 32.965},
        )

    def test_a_html_page_is_not_parsed_as_rates(self):
        with self.assertRaises(fx.ChallengeError):
            fx.parse_taishin("<!DOCTYPE html><html><body>Access Denied</body></html>")

    def test_layout_change_raises_instead_of_guessing(self):
        # 表頭認不出來（例如欄名改了）就不該硬湊數字
        js = self.export("USD,32.8650,32.9650,32.6630,33.1400", header="買入,賣出,現鈔買入,現鈔賣出")
        with self.assertRaises(ValueError):
            fx.parse_taishin(js)


class DiscountTest(unittest.TestCase):
    def test_config_overrides_the_bank_per_currency(self):
        auto = {"esun": {"USD": 0.031, "JPY": 0.0012}}
        cfg = {"esun": {"USD": 0}, "bot": {"USD": 0.03}}
        self.assertEqual(
            fx.merge_discount(auto, cfg),
            {"esun": {"USD": 0, "JPY": 0.0012}, "bot": {"USD": 0.03}},
        )

    def test_nothing_in_nothing_out(self):
        self.assertEqual(fx.merge_discount({}, {}), {})


class EngineTest(unittest.TestCase):
    def test_single_bank_never_has_a_free_lunch(self):
        cycles = fx.best_cycles(fx.build_edges({"bot": BOT}))
        self.assertTrue(cycles)
        self.assertTrue(all(c["bp"] < 0 for c in cycles))

    def test_cheapest_round_trip_is_the_tightest_spot_spread(self):
        best = fx.best_cycles(fx.build_edges({"bot": BOT}))[0]
        self.assertEqual(route(best), ["USD.acct@bot", "TWD"])
        self.assertAlmostEqual(best["factor"], 32.915 / 33.015)

    def test_cross_bank_arbitrage_is_found(self):
        # 現鈔是兩家銀行之間的橋：在 x 行用 32.5 買現鈔，拿到臺銀用 32.565 賣掉。
        other = {"USD": {"cash_buy": 32.3, "cash_sell": 32.5, "spot_buy": 32.4, "spot_sell": 32.45}}
        best = fx.best_cycles(fx.build_edges({"bot": BOT, "x": other}))[0]
        self.assertGreater(best["bp"], 0)
        self.assertEqual(route(best), ["USD.cash", "TWD"])
        self.assertEqual([s["source"] for s in best["steps"]], ["x", "bot"])
        self.assertAlmostEqual(best["factor"], 32.565 / 32.5)

    def test_account_balances_do_not_teleport_between_banks(self):
        # x 行即期賣 32.0、臺銀即期買 32.915：看起來有 2.8%，但帳戶裡的美元
        # 搬不過去；只能 x 帳戶 → 提現鈔 → 存進臺銀 → 賣，匯差手續費要算進去。
        other = {"USD": {"cash_buy": 31.9, "cash_sell": 32.3, "spot_buy": 31.95, "spot_sell": 32.0}}
        edges = fx.build_edges({"bot": BOT, "x": other})
        self.assertFalse([e for e in edges if e["from"] == "USD.acct@x" and e["to"] == "USD.acct@bot"])
        best = fx.best_cycles(edges)[0]
        self.assertAlmostEqual(best["factor"], 32.565 / 32.3)   # 不是 32.915 / 32.0
        self.assertEqual(route(best), ["USD.cash", "TWD"])

    def test_equivalent_routes_collapse_to_the_shortest(self):
        # 「買即期 → 提現鈔 → 賣現鈔」和「買現鈔 → 賣現鈔」同價，只該出現一次。
        cycles = fx.best_cycles(fx.build_edges({"bot": BOT}), top=50)
        factors = [round(c["factor"], 12) for c in cycles]
        self.assertEqual(len(factors), len(set(factors)))
        cash_trip = [c for c in cycles if abs(c["factor"] - 32.565 / 33.235) < 1e-12]
        self.assertEqual([route(c) for c in cash_trip], [["USD.cash", "TWD"]])

    def test_best_path_to_foreign_cash(self):
        # 直接買現鈔，和「先買即期再提現鈔」在只收匯差的模型下是同一個價。
        paths = fx.best_paths(fx.build_edges({"bot": BOT}), "TWD", "JPY.cash")
        self.assertAlmostEqual(paths[0]["factor"], 1 / 0.2103)

    def test_foreign_to_foreign_goes_through_twd(self):
        p = fx.best_paths(fx.build_edges({"bot": BOT}), "USD.acct@bot", "JPY.acct@bot")[0]
        self.assertEqual(route(p), ["TWD", "JPY.acct@bot"])
        self.assertAlmostEqual(p["factor"], 32.915 / 0.2083)

    def test_two_real_shaped_banks(self):
        esun = fx.parse_esun(ESUN_RAW)["quotes"]
        cycles = fx.best_cycles(fx.build_edges({"bot": BOT, "esun": esun}), top=50)
        sources = {st["source"] for c in cycles for st in c["steps"]}
        self.assertEqual(sources, {"bot", "esun"})
        # 樣本裡臺銀現鈔買 32.565、玉山現鈔賣 33.18；玉山現鈔買 32.63、臺銀現鈔賣 33.235：兩邊都不成立
        self.assertTrue(all(c["bp"] < 0 for c in cycles))

    def test_esun_against_taishin_real_samples(self):
        # 兩份真實樣本是不同時間抓的，這裡只拿來驗路徑計算，不代表當時的價差。
        # 兩家的買賣價互相重疊，所以繞一圈都是負的。
        sources = {"esun": fx.parse_esun(ESUN_RAW)["quotes"], "taishin": fx.parse_taishin(TAISHIN_JS)["quotes"]}
        cycles = fx.best_cycles(fx.build_edges(sources), top=50)
        self.assertEqual({st["source"] for c in cycles for st in c["steps"]}, {"esun", "taishin"})
        self.assertTrue(all(c["bp"] < 0 for c in cycles), [c["bp"] for c in cycles[:3]])
        # 日圓即期：台新賣 0.2079，比玉山牌告賣 0.2084 便宜，所以最便宜的日圓來回走台新
        jpy = [c for c in cycles if c["steps"][0]["to"].startswith("JPY.acct")][0]
        self.assertEqual(jpy["steps"][0]["source"], "taishin")

    def test_node_labels(self):
        self.assertEqual(fx.node_label("TWD"), "台幣")
        self.assertEqual(fx.node_label("JPY.cash"), "日圓現鈔")
        self.assertEqual(fx.node_label("USD.acct@esun"), "美元帳戶·玉山銀行")

    def test_discount_narrows_the_spot_spread_only(self):
        q = fx.apply_discount(BOT["USD"], 0.03)
        self.assertAlmostEqual(q["spot_sell"], 32.985)
        self.assertAlmostEqual(q["spot_buy"], 32.945)
        self.assertEqual(q["cash_sell"], 33.235)
        self.assertEqual(BOT["USD"]["spot_sell"], 33.015)  # 原始牌告不被改到


class MetricsTest(unittest.TestCase):
    def test_spreads_and_deviation(self):
        m = fx.quote_metrics(BOT["USD"], {"price": 32.965, "time": T0}, T0)
        self.assertAlmostEqual(m["spot_spread_pct"], 0.303, places=3)
        self.assertAlmostEqual(m["cash_spread_pct"], 2.036, places=3)
        self.assertEqual(m["dev_bp"], 0.0)
        self.assertIsNone(m["lag"])

    def test_market_above_ask_means_buy_from_the_bank(self):
        m = fx.quote_metrics(BOT["USD"], {"price": 33.10, "time": T0}, T0)
        self.assertEqual(m["lag"]["side"], "buy")
        self.assertAlmostEqual(m["lag"]["bp"], 25.7, places=1)

    def test_market_below_bid_means_sell_to_the_bank(self):
        m = fx.quote_metrics(BOT["USD"], {"price": 32.80, "time": T0}, T0)
        self.assertEqual(m["lag"]["side"], "sell")

    def test_a_hair_outside_the_band_is_noise(self):
        # 33.02 只比賣出價 33.015 高 1.5bp：市場中價自己的雜訊就不只這樣。
        m = fx.quote_metrics(BOT["USD"], {"price": 33.02, "time": T0}, T0)
        self.assertIsNone(m["lag"])
        m = fx.quote_metrics(BOT["USD"], {"price": 33.03, "time": T0}, T0)
        self.assertEqual(m["lag"], {"side": "buy", "bp": 4.5})

    def test_stale_board_is_not_called_a_lag(self):
        # 牌告停在 16:00、市場報價是 21:00：差距是因為銀行下班，不是機會。
        m = fx.quote_metrics(BOT["USD"], {"price": 33.10, "time": "2026-11-16T21:00:00+08:00"},
                             "2026-11-16T16:00:00+08:00")
        self.assertIsNone(m["lag"])
        self.assertIsNotNone(m["dev_bp"])

    def test_cross_rates(self):
        rows = {r["pair"]: r for r in fx.cross_metrics(BOT, {"USDJPY": {"price": 159.8, "time": T0}})}
        usdjpy = rows["USDJPY"]
        self.assertAlmostEqual(usdjpy["implied"], 32.965 / 0.2063)
        self.assertAlmostEqual(usdjpy["cost_bp"], (1 - (32.915 / 0.2083) / 159.8) * 1e4, places=1)
        self.assertIsNone(rows["EURUSD"]["market"])

    def test_twd_crosses_are_synthesized(self):
        m = fx.synthesize_twd_crosses({
            "USDTWD": {"price": 33.0, "time": T0},
            "USDJPY": {"price": 160.0, "time": "2026-11-16T09:58:00+08:00"},
        })
        self.assertAlmostEqual(m["JPYTWD"]["price"], 0.20625)
        self.assertEqual(m["JPYTWD"]["time"], "2026-11-16T09:58:00+08:00")
        self.assertNotIn("EURTWD", m)


class HistoryTest(unittest.TestCase):
    def test_unchanged_quotes_are_not_appended(self):
        rows = fx.append_history([], T0, {"bot": BOT}, {})
        self.assertEqual(len(rows), 3)
        again = fx.append_history(rows, "2026-11-16T10:15:00+08:00", {"bot": BOT}, {})
        self.assertEqual(len(again), 3)

    def test_old_days_collapse_to_their_last_row(self):
        rows = [
            ["2026-10-01T09:30:00+08:00", "bot", "USD", 1, 2, 3, 4],
            ["2026-10-01T15:30:00+08:00", "bot", "USD", 1, 2, 3, 5],
            ["2026-11-16T09:30:00+08:00", "bot", "USD", 1, 2, 3, 6],
            ["2026-11-16T09:45:00+08:00", "bot", "USD", 1, 2, 3, 7],
            ["2025-01-01T09:30:00+08:00", "bot", "USD", 1, 2, 3, 8],
        ]
        kept = [r[6] for r in fx.trim_history(rows, T0)]
        self.assertEqual(kept, [5, 6, 7])

    def test_stats(self):
        rows = [[f"2026-11-{d:02d}T15:30:00+08:00", "bot", "JPY", 0.19, 0.21, 0.2, v]
                for d, v in ((10, 0.2100), (11, 0.2090), (12, 0.2080), (13, 0.2095), (16, 0.2083))]
        s = fx.history_stats(rows, "bot", "JPY", T0)
        self.assertEqual((s["days"], s["low"], s["high"], s["prev_close"]), (5, 0.208, 0.21, 0.2095))
        self.assertEqual(s["pct_rank"], 40)
        self.assertAlmostEqual(s["chg_pct"], -0.573, places=3)
        self.assertIsNone(fx.history_stats(rows[:1], "bot", "JPY", T0))


class AlertTest(unittest.TestCase):
    def derived(self, market):
        return fx.derive({"bot": BOT}, {"bot": T0}, market, [], T0)

    def test_price_threshold(self):
        cfg = {"alerts": [
            {"ccy": "JPY", "field": "spot_sell", "op": "<=", "value": 0.21},
            {"ccy": "JPY", "field": "spot_sell", "op": "<=", "value": 0.20},
            {"ccy": "USD", "field": "spot_buy", "op": ">=", "value": 32.9},
        ]}
        hits = fx.check_alerts(cfg, {"bot": BOT}, self.derived({}))
        self.assertEqual(len(hits), 2)
        self.assertIn("臺灣銀行 JPY 即期賣出 0.2083", hits[0]["text"])

    def test_arb_alert_names_the_banks(self):
        esun = copy.deepcopy(fx.parse_esun(ESUN_RAW)["quotes"])
        esun["USD"].update(cash_buy=32.2, cash_sell=32.5)   # 賣得比臺銀現鈔買入 32.565 還便宜
        sources = {"bot": BOT, "esun": esun}
        d = fx.derive(sources, {"bot": T0, "esun": T0}, {}, [], T0)
        hits = fx.check_alerts({"arb_alert_bp": 0}, sources, d)
        self.assertEqual(len(hits), 1)
        self.assertIn("+20.0bp", hits[0]["text"])
        self.assertIn("（玉山銀行現鈔賣出）美元現鈔", hits[0]["text"])
        self.assertIn("（臺灣銀行現鈔買入）台幣", hits[0]["text"])
        self.assertEqual(fx.check_alerts({"arb_alert_bp": 25}, sources, d), [])

    def test_no_arb_alert_when_every_cycle_loses(self):
        hits = fx.check_alerts({"arb_alert_bp": -1000}, {"bot": BOT}, self.derived({}))
        self.assertEqual(hits, [])

    def test_lag_alert(self):
        d = self.derived({"USDTWD": {"price": 33.10, "time": T0}})
        self.assertEqual(len(fx.check_alerts({"lag_alert_bp": 10}, {"bot": BOT}, d)), 1)
        self.assertEqual(fx.check_alerts({"lag_alert_bp": 50}, {"bot": BOT}, d), [])
        self.assertEqual(fx.check_alerts({"lag_alert_bp": None}, {"bot": BOT}, d), [])


if __name__ == "__main__":
    unittest.main()
