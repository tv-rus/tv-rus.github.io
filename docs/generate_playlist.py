#!/usr/bin/env python3
"""Download, merge, filter, and group Russian IPTV playlists."""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCES = (
    "https://iptv-org.github.io/iptv/countries/ru.m3u",
    "https://duckkota.github.io/iptv/playlists/country/ru.m3u",
    "https://smart-iptv.ru/russia.m3u",
)
GROUP_ORDER = ("ТВ", "Кино", "Спорт", "Детские")
GROUP_KEYWORDS = {
    "Детские": (
        "дет", "мульт", "kids", "kid", "cartoon", "disney", "nickelodeon",
        "карусель", "солнце",
    ),
    "Спорт": (
        "спорт", "sport", "футбол", "football", "хоккей", "hockey", "матч",
        "ufc", "boxing", "бокс", "khl", "nhl", "nba", "eurosport",
    ),
    "Кино": (
        "кино", "фильм", "movie", "cinema", "series", "сериал", "премьера",
        "amedia", "победа", "иллюзион", "дом кино", "кинопоказ",
    ),
}
ATTRIBUTE_RE = re.compile(r'([\w-]+)=(?:"([^"]*)"|([^\s,]+))')
QUALITY_RE = re.compile(
    r"(?:\s*(?:[\[(](?:\d{3,4}p|uhd|fhd|hd|sd|4k|резерв|backup).*?[\])]|"
    r"(?:uhd|fhd|hd|sd|4k)))+$",
    re.IGNORECASE,
)
RATING_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|")
RATING_ALIASES = {
    "5канал": "пятыйканал",
    "5каналпетербург": "пятыйканалпетербург",
    "рен": "рентв",
    "твц": "твцентр",
    "матч": "матчтв",
    "первый": "первыйканал",
}
NOISE_PATTERN = re.compile(
    r"(?:^|\W)(?:radio|радио|webcam|вебкам|камера|shopping|shop|магазин|"
    r"adult|взросл|эротик|porn|тест|test|demo|weather|погода)(?:\W|$)",
    re.IGNORECASE,
)


@dataclass
class Channel:
    name: str
    url: str
    attrs: OrderedDict[str, str]
    source_index: int
    available: bool | None = None


def download(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tvgo-playlist/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def split_extinf(line: str) -> tuple[str, str]:
    payload = line.removeprefix("#EXTINF:")
    quoted = False
    for index, char in enumerate(payload):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return payload[:index], payload[index + 1 :].strip()
    return payload, ""


def parse_playlist(text: str, source_index: int) -> tuple[list[Channel], list[str]]:
    channels: list[Channel] = []
    epg_urls: list[str] = []
    pending: tuple[str, OrderedDict[str, str]] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header_attrs = OrderedDict(
                (match.group(1), match.group(2) or match.group(3) or "")
                for match in ATTRIBUTE_RE.finditer(line)
            )
            for key in ("x-tvg-url", "url-tvg"):
                for value in header_attrs.get(key, "").split(","):
                    if value.strip() and value.strip() not in epg_urls:
                        epg_urls.append(value.strip())
        elif line.startswith("#EXTINF:"):
            metadata, name = split_extinf(line)
            attrs = OrderedDict(
                (match.group(1), match.group(2) or match.group(3) or "")
                for match in ATTRIBUTE_RE.finditer(metadata)
            )
            pending = (name or attrs.get("tvg-name", "Без названия"), attrs)
        elif not line.startswith("#") and pending:
            name, attrs = pending
            channels.append(Channel(name, line, attrs, source_index))
            pending = None

    return channels, epg_urls


def normalized_name(name: str) -> str:
    name = QUALITY_RE.sub("", name)
    return re.sub(r"[^a-zа-яё0-9]+", "", name.casefold())


def channel_key(channel: Channel) -> tuple[str, str]:
    tvg_id = channel.attrs.get("tvg-id", "").strip().casefold()
    return ("id", tvg_id) if tvg_id else ("name", normalized_name(channel.name))


def load_rating(path: Path) -> dict[str, int]:
    if not path.exists():
        print(f"Предупреждение: рейтинг не найден: {path}", file=sys.stderr)
        return {}
    rating: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = RATING_ROW_RE.match(line)
        if match:
            rating[normalized_name(match.group(2))] = int(match.group(1))
    return rating


def rating_position(channel: Channel, rating: dict[str, int]) -> int | None:
    names = (channel.name, channel.attrs.get("tvg-name", ""))
    for name in names:
        normalized = normalized_name(name)
        canonical = RATING_ALIASES.get(normalized, normalized)
        if canonical in rating:
            return rating[canonical]
    return None


def check_stream(channel: Channel, timeout: float) -> Channel:
    if not channel.url.casefold().startswith(("http://", "https://")):
        channel.available = False
        return channel
    request = urllib.request.Request(
        channel.url,
        headers={
            "User-Agent": "TiviMate",
            "Range": "bytes=0-2047",
            "Accept": "application/vnd.apple.mpegurl,video/*,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type().casefold()
            payload = response.read(2048).lstrip()
        text_prefix = payload[:256].lower()
        is_error_page = (
            not payload
            or content_type in {"text/html", "application/json", "application/xml"}
            or text_prefix.startswith((b"<!doctype html", b"<html", b"{\"error\"", b"<error"))
        )
        channel.available = not is_error_page
    except (urllib.error.URLError, TimeoutError, OSError):
        channel.available = False
    return channel


def matches_filters(name: str, include: list[re.Pattern[str]], exclude: list[re.Pattern[str]]) -> bool:
    if include and not any(pattern.search(name) for pattern in include):
        return False
    return not any(pattern.search(name) for pattern in exclude)


def classify(channel: Channel) -> str:
    text = " ".join((channel.attrs.get("group-title", ""), channel.name)).casefold()
    for group in ("Детские", "Спорт", "Кино"):
        if any(keyword in text for keyword in GROUP_KEYWORDS[group]):
            return group
    return "ТВ"


def escape_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', "\\\"")


def render(channels: list[tuple[str, list[Channel]]], epg_urls: Iterable[str]) -> str:
    epg = ",".join(dict.fromkeys(epg_urls))
    header = f'#EXTM3U x-tvg-url="{escape_attr(epg)}"' if epg else "#EXTM3U"
    lines = [header]

    for group, variants in channels:
        primary = variants[0]
        for index, channel in enumerate(variants):
            attrs = OrderedDict(primary.attrs)
            attrs["group-title"] = group
            display_name = primary.name if index == 0 else f"{primary.name} [Резерв {index}]"
            attrs["tvg-name"] = display_name
            metadata = " ".join(f'{key}="{escape_attr(value)}"' for key, value in attrs.items())
            lines.extend((f"#EXTINF:-1 {metadata},{display_name}", channel.url))

    return "\n".join(lines) + "\n"


def compile_patterns(values: list[str], option: str) -> list[re.Pattern[str]]:
    try:
        return [re.compile(value, re.IGNORECASE) for value in values]
    except re.error as error:
        raise SystemExit(f"Некорректное выражение для {option}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", dest="sources", help="M3U URL; порядок задаёт приоритет")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("ru.m3u"))
    parser.add_argument("--rating", type=Path, default=Path(__file__).with_name("rating-tv.md"), help="Markdown-таблица приоритетных каналов")
    parser.add_argument("--include", action="append", default=[], metavar="REGEX", help="оставить совпавшие каналы")
    parser.add_argument("--exclude", action="append", default=[], metavar="REGEX", help="исключить совпавшие каналы")
    parser.add_argument("--keep-noise", action="store_true", help="не удалять радио, камеры, магазины и тестовые каналы")
    parser.add_argument("--timeout", type=float, default=30.0, help="тайм-аут загрузки плейлистов")
    parser.add_argument("--check-streams", action="store_true", help="проверить HTTP-потоки и предпочесть доступные")
    parser.add_argument("--drop-unavailable", action="store_true", help="удалить не ответившие HTTP-потоки")
    parser.add_argument("--check-timeout", type=float, default=5.0, help="тайм-аут проверки одного потока")
    parser.add_argument("--workers", type=int, default=20, help="число параллельных проверок")
    args = parser.parse_args()
    if args.drop_unavailable:
        args.check_streams = True

    include = compile_patterns(args.include, "--include")
    exclude = compile_patterns(args.exclude, "--exclude")
    sources = args.sources or list(DEFAULT_SOURCES)
    all_channels: list[Channel] = []
    epg_urls: list[str] = []

    for source_index, source in enumerate(sources):
        print(f"Скачиваю: {source}", file=sys.stderr)
        try:
            channels, source_epg = parse_playlist(download(source, args.timeout), source_index)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"Ошибка загрузки {source}: {error}", file=sys.stderr)
            return 1
        all_channels.extend(channels)
        epg_urls.extend(source_epg)

    buckets: OrderedDict[tuple[str, str], list[Channel]] = OrderedDict()
    seen_urls: set[str] = set()
    for channel in all_channels:
        searchable = " ".join((channel.name, channel.attrs.get("group-title", "")))
        is_noise = not args.keep_noise and NOISE_PATTERN.search(searchable)
        if is_noise or not matches_filters(searchable, include, exclude) or channel.url in seen_urls:
            continue
        seen_urls.add(channel.url)
        buckets.setdefault(channel_key(channel), []).append(channel)

    if args.check_streams:
        channels_to_check = [channel for variants in buckets.values() for channel in variants]
        print(f"Проверяю {len(channels_to_check)} потоков...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            list(executor.map(lambda channel: check_stream(channel, args.check_timeout), channels_to_check))
        for key, variants in list(buckets.items()):
            variants.sort(key=lambda channel: (channel.available is not True, channel.source_index))
            if args.drop_unavailable:
                variants[:] = [channel for channel in variants if channel.available is not False]
            if not variants:
                del buckets[key]

    rating = load_rating(args.rating)
    ordered_channels = [(classify(variants[0]), variants) for variants in buckets.values()]
    group_positions = {group: position for position, group in enumerate(GROUP_ORDER)}
    ordered_channels.sort(
        key=lambda item: (
            rating_position(item[1][0], rating) is None,
            rating_position(item[1][0], rating) or 0,
            group_positions[item[0]],
            item[1][0].name.casefold(),
        )
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(ordered_channels, epg_urls), encoding="utf-8")
    reserve_count = sum(max(0, len(variants) - 1) for _, variants in ordered_channels)
    print(
        f"Готово: {args.output} — {len(buckets)} каналов, {reserve_count} резервных потоков",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
