"""Feed-format regressions for the Telegram, UKMTO and Tzeva Adom parsers."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tripwire.config import Config
from tripwire.models import Source
from tripwire.parsers import parse_ppi, parse_telegram, parse_tzeva, parse_ukmto

CFG = Config(Path(__file__).resolve().parent.parent).load()


def source(parser: str, **kwargs) -> Source:
    data = {
        "id": f"test_{parser}", "name": parser, "type": "html", "parser": parser,
        "url": "https://example.test/feed", **kwargs,
    }
    return Source.from_dict(data, {})


TG_HTML = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message wrap js-widget_message" data-post="mod_russia/69244">
  <div class="tgme_widget_message_bubble">
   <a class="tgme_widget_message_user" href="https://t.me/mod_russia">
    <i class="tgme_widget_message_user_photo bgcolor1"></i></a>
   <div class="tgme_widget_message_text js-message_text" dir="auto">
    First <br class="tgme_br"> post body
   </div>
   <div class="tgme_widget_message_footer js-message_footer"><div class="tgme_widget_message_info">
    <span class="tgme_widget_message_views">1234</span>
    <time datetime="2026-11-22T14:39:59+00:00"></time>
   </div></div>
  </div>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message"
      data-post="mod_russia/69245">
  <div class="tgme_widget_message_author accent_color">Ministry of Defense</div>
  <div class="tgme_widget_message_photo_wrap" style="background-image:url('https://x/y.jpg')"></div>
  <div class="tgme_widget_message_date"><time datetime="2026-11-22T15:00:00+00:00"></time></div>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message wrap js-widget_message" data-post="mod_russia/69246">
  <div class="tgme_widget_message_bubble">
   <div class="tgme_widget_message_text js-message_text" dir="auto">Second post</div>
   <div class="tgme_widget_message_link_preview"><div class="tgme_widget_message_link_preview_title">Link</div></div>
   <div class="tgme_widget_message_date"><time datetime="2026-11-22T16:10:00+00:00"></time></div>
  </div>
 </div>
</div>
"""


class TelegramPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = source(
            "telegram_html", url="https://t.me/s/mod_russia", theater="EU_RU",
            category="eu_ru", lang="ru", max_items=10,
        )
        self.items = parse_telegram(self.src, TG_HTML, CFG)

    def test_every_text_post_is_kept(self) -> None:
        self.assertEqual([i.payload["post"] for i in self.items],
                         ["mod_russia/69244", "mod_russia/69246"])

    def test_items_keep_their_own_urls(self) -> None:
        # Nested tgme_widget_message_* divs used to replace the block carrying
        # data-post, so every item collapsed onto the page URL and dedupe kept one.
        self.assertEqual(self.items[0].url, "https://t.me/mod_russia/69244")
        self.assertEqual(self.items[1].url, "https://t.me/mod_russia/69246")
        self.assertEqual(len({i.guid for i in self.items}), 2)

    def test_message_time_is_not_taken_from_the_author_block(self) -> None:
        self.assertEqual(str(self.items[0].published_at), "2026-11-22 14:39:59+00:00")

    def test_text_is_read_from_the_message_text_block(self) -> None:
        self.assertEqual(self.items[0].title, "First post body")
        self.assertEqual(self.items[1].title, "Second post")


class PpiTelegramTests(unittest.TestCase):
    def test_verdict_post_url_uses_the_post_id(self) -> None:
        html = TG_HTML.replace("mod_russia", "PentagonPizzaReport").replace(
            "First <br class=\"tgme_br\"> post body",
            "Pentagon Pizza Index: activity is ANOMALY tonight")
        src = source("telegram_html", url="https://t.me/s/PentagonPizzaReport",
                     theater="MARKETS", category="indicator", max_items=10)
        items = parse_ppi(src, html, CFG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].payload["state"], "ANOMALY")
        self.assertEqual(items[0].url, "https://t.me/PentagonPizzaReport/69244")


# The r.jina.ai rendering puts each report on a single line.
UKMTO_LIVE = """
Title: Recent Incidents

Markdown Content:
*       *   ![Image 1](https://www.ukmto.org/icon-calendar.svg)22 November 2026

UKMTO ADVISORY - 217-26 Incident Date: 22 Nov 2026 Incident Time: 1425UTC UKMTO have received a report of an incident involving a tanker and military forces in the Gulf of Oman. Authorities are aware.

*       *   ![Image 2](https://www.ukmto.org/icon-calendar.svg)21 November 2026

UKMTO WARNING 216-26 - ATTACK Incident Date: 21 Nov 2026 Incident Time: 0550UTC UKMTO has received a report of an incident 17NM east of Khasab, Oman. A Tanker transiting outbound in the Strait of Hormuz has been hit in the engine room.

*       *   ![Image 3](https://www.ukmto.org/icon-calendar.svg)21 November 2026

UKMTO WARNING 215-26 - ATTACK Incident Date 21 Nov 2026 UKMTO has received a report of an incident 11NM north of Khasab, Oman.
"""


class UKMTOTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = source("ukmto_jina", url="https://r.jina.ai/https://www.ukmto.org/recent-incidents",
                          theater="GULF", category="gulf", max_items=30)
        self.items = parse_ukmto(self.src, UKMTO_LIVE, CFG)

    def test_plain_text_report_lines_parse(self) -> None:
        self.assertEqual([i.payload["advisory"] for i in self.items],
                         ["UKMTO ADVISORY 217-26", "UKMTO WARNING 216-26", "UKMTO WARNING 215-26"])

    def test_four_digit_year_suffix_keeps_its_own_id(self) -> None:
        # Upstream writes some numbers with the full year (192-2026, and the typo
        # 208-226); matching only two year digits split one report into two ids.
        text = "UKMTO WARNING 192-2026. UKMTO has received a report of an incident.\n"
        items = parse_ukmto(self.src, text, CFG)
        self.assertEqual(items[0].payload["advisory"], "UKMTO WARNING 192-2026")

    def test_reports_keep_distinct_urls(self) -> None:
        # The deduper rejects an event whose URL is already stored, and canon_url drops
        # fragments, so an anchor-per-report collapsed every advisory onto the page URL.
        self.assertEqual(self.items[0].url, "https://ukmto.org/recent-incidents?advisory=217-26")
        self.assertEqual(len({i.url for i in self.items}), len(self.items))

    def test_report_without_a_date_uses_the_calendar_line(self) -> None:
        # Without a date the normalizer stamps the ingestion time, so an advisory
        # from years ago would pass max_age_hours and alert as if just issued.
        text = ("*   ![Image 4](https://www.ukmto.org/icon-calendar.svg)3 June 2024\n\n"
                "UKMTO WARNING 118-24. UKMTO has received a report of an incident.\n")
        items = parse_ukmto(self.src, text, CFG)
        self.assertEqual(str(items[0].published_at), "2024-06-03 00:00:00+00:00")

    def test_incident_date_and_time_are_combined_utc(self) -> None:
        self.assertEqual(str(self.items[0].published_at), "2026-11-22 14:25:00+00:00")

    def test_report_text_becomes_the_body(self) -> None:
        self.assertIn("Khasab", self.items[1].body)
        self.assertEqual(self.items[1].payload["incident_type"], "attack")

    def test_markdown_link_form_still_parses(self) -> None:
        text = ("*   [22/10/2026 Attack:UKMTO WARNING 181-26 ATTACK Incident Date: 22 OCT "
                "2026 Incident Time:...](https://www.ukmto.org/recent-incidents#8c1c646b)\n")
        items = parse_ukmto(self.src, text, CFG)
        self.assertEqual(items[0].payload["advisory"], "UKMTO WARNING 181-26")
        self.assertEqual(items[0].published_at.day, 22)
        self.assertEqual(items[0].category, "gulf")


class TzevaHistoryTests(unittest.TestCase):
    def test_newest_reports_survive_the_item_cut(self) -> None:
        # The history endpoint is newest-first; cutting the tail kept only the oldest
        # reports, which max_age_hours then dropped, so the source never ingested.
        groups = [{"id": 10 - i, "alerts": [{"time": 1_700_000_000 - i * 86_400,
                                             "cities": [f"city{i}"], "threat": 1,
                                             "isDrill": False}]} for i in range(6)]
        src = source("tzeva_json", url="https://api.tzevaadom.co.il/alerts-history",
                     theater="ISRAEL", category="israel", lang="he", max_items=2)
        items = parse_tzeva(src, json.dumps(groups), CFG)
        self.assertEqual([i.payload["cities"] for i in items], [["city0"], ["city1"]])


if __name__ == "__main__":
    unittest.main()
