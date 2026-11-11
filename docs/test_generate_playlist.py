import unittest
from collections import Counter, OrderedDict
from pathlib import Path

import generate_playlist as generator


class PlaylistOrderTest(unittest.TestCase):
    def setUp(self):
        self.rating = generator.load_rating(Path(__file__).with_name("rating-tv.md"))

    def channel(self, name, tvg_id=""):
        return generator.Channel(name, "https://example.test/live", OrderedDict({"tvg-id": tvg_id}), 0)

    def test_upstream_ids_follow_rating_including_quality_variants(self):
        for channel_id, canonical in generator.RATING_CHANNEL_IDS.items():
            for suffix in ("", "@SD", "@HD", "@4K"):
                with self.subTest(channel_id=channel_id, suffix=suffix):
                    channel = self.channel("Unrelated display name", channel_id + ".ru" + suffix)
                    self.assertEqual(
                        self.rating[generator.normalized_name(canonical)],
                        generator.rating_position(channel, self.rating),
                    )

    def test_latin_names_and_cyrillic_names(self):
        for name, position in (("Russia-1 HD (1080p)", 1), ("NTV (576p)", 2),
                               ("Channel One (576p)", 3), ("Россия 1 HD", 1),
                               ("Пятый канал", 4), ("TV Centr", 6)):
            self.assertEqual(position, generator.rating_position(self.channel(name), self.rating))

    def test_no_fuzzy_matching_of_thematic_or_regional_channels(self):
        for name, channel_id in (("NTV Style", "NTVStyle.ru@SD"),
                                  ("NTV +2", "NTV.ru@Plus2"),
                                  ("STS International", "STSInternational.ru"),
                                  ("TV-3 Belarus", "TV3.ru@Belarus"),
                                  ("Rossiya 1 Vainah", "Rossiya1Vainah.ru@SD")):
            self.assertIsNone(generator.rating_position(self.channel(name, channel_id), self.rating))

    def test_offline_sort_preserves_blocks_and_reserve_order(self):
        text = '#EXTM3U x-tvg-url="https://example.test/epg.gz"\n'
        entries = [
            ("2x2.ru", "2x2 (1080p)"),
            ("ChannelOne.ru", "Channel One (576p)"),
            ("Russia1.ru", "Russia-1 (1080p)"),
            ("Russia1.ru", "Russia-1 (1080p) [Резерв 2]"),
            ("Russia1.ru", "Russia-1 (1080p) [Резерв 10]"),
            ("NTV.ru", "NTV (1080p)"),
            ("Other.ru", "Other"),
        ]
        blocks = [f'#EXTINF:-1 tvg-id="{channel_id}" group-title="ТВ",{name}\n'
                  f'#EXTVLCOPT:http-user-agent=Test\nhttps://example.test/{index}?token=keep\n'
                  for index, (channel_id, name) in enumerate(entries)]
        result = generator.reorder_playlist(text + "".join(blocks), self.rating)
        self.assertEqual(text + "".join(blocks[i] for i in (2, 3, 4, 5, 1, 0, 6)), result)
        self.assertEqual(result, generator.reorder_playlist(result, self.rating))
        self.assertEqual(Counter((text + "".join(blocks)).splitlines()), Counter(result.splitlines()))

    def test_sort_existing_cli_preserves_content_without_network(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.m3u"
            text = ('#EXTM3U\n#EXTINF:-1 tvg-id="NTV.ru",NTV\nhttps://example.test/ntv\n'
                    '#EXTINF:-1 tvg-id="Russia1.ru",Russia-1\nhttps://example.test/russia')
            path.write_text(text, encoding="utf-8")
            with patch("sys.argv", ["generate_playlist.py", "--sort-existing", str(path)]), \
                    patch.object(generator, "download", side_effect=AssertionError("Unexpected network")):
                self.assertEqual(0, generator.main())
            result = path.read_text(encoding="utf-8")
            self.assertEqual(Counter(text.splitlines()), Counter(result.splitlines()))
            self.assertLess(result.index(",Russia-1"), result.index(",NTV"))


if __name__ == "__main__":
    unittest.main()
