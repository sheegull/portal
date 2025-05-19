# YouTube, Podcast(Spotify, Apple Podcast)に対応
import inspect
import os
import time
import traceback
import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
import re

import feedparser
import requests
import tomllib
from bs4 import BeautifulSoup
from ..common.python.gemini_client import create_client

_THUMBNAIL_WIDTH = 480

_MARKDOWN_FORMAT = """
# {title}

[View on {feed_name}]({url})

{summary}

{thumbnail_card}
"""

class Config:
    sns_feed_max_entries_per_day = 10
    summary_index_s3_key_format = "sns_feed/{date}.md"
    threshold_days = 1

    @classmethod
    def load_feeds(cls) -> dict[str, str]:
        feed_toml_path = os.path.join(os.path.dirname(__file__), "feed.toml")
        with open(feed_toml_path, "rb") as f:
            feed_data = tomllib.load(f)
        return {feed["name"]: feed["url"] for feed in feed_data.get("feeds", [])}

@dataclass
class Article:
    feed_name: str
    title: str
    url: str
    text: str
    soup: BeautifulSoup
    rss_summary: str
    thumbnail_url: Optional[str] = None
    summary: str = field(init=False)

class SnsFeed:
    def __init__(self) -> None:
        self._client = create_client()
        self._sns_feed_urls = Config.load_feeds()
        # UTC基準でフィルタリング
        self._threshold = datetime.now(timezone.utc) - timedelta(days=Config.threshold_days)

    def __call__(self) -> None:
        markdowns: list[str] = []
        for feed_name, feed_url in self._sns_feed_urls.items():
            print(f"Processing feed: {feed_name} ({feed_url})")
            feed_parser: feedparser.FeedParserDict = feedparser.parse(feed_url)
            feed_image = None
            if hasattr(feed_parser.feed, "image") and feed_parser.feed.image and feed_parser.feed.image.href:
                feed_image = feed_parser.feed.image.href
            print(f"Feed entries count: {len(feed_parser['entries'])}")
            entries = self._filter_entries(feed_parser)
            print(f"Filtered entries count: {len(entries)}")
            if len(entries) > Config.sns_feed_max_entries_per_day:
                entries = entries[: Config.sns_feed_max_entries_per_day]
            for entry in entries:
                try:
                    article = self._build_article(entry, feed_name, feed_image)
                    print(f"Built article: {article.title}")

                    if not article.rss_summary.strip():
                        article.summary = article.text
                    else:
                        article.summary = self._summarize_article(article)

                    print(f"Generated summary for: {article.title}")
                    markdowns.append(self._stylize_article(article))
                except Exception as e:
                    print(f"Error processing entry {entry.get('link', 'unknown')}: {e}")
                    traceback.print_exc()
                    continue
                time.sleep(2)  # APIリクエスト制限回避
        print(f"Total markdowns generated: {len(markdowns)}")
        self._store_summaries(markdowns)

    def _filter_entries(self, feed_parser: feedparser.FeedParserDict) -> list[dict[str, Any]]:
        filtered_entries: list[dict[str, Any]] = []
        for entry in feed_parser["entries"]:
            tm = entry.get("date_parsed") or entry.get("published_parsed")
            if not tm:
                continue
            try:
                ts = calendar.timegm(tm)
                published_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                if published_dt > self._threshold:
                    filtered_entries.append(entry)
            except Exception:
                continue
        return filtered_entries

    def _build_article(
        self, entry: dict[str, Any], feed_name: str, feed_image: Optional[str]
    ) -> Article:
        html = (
            entry.get("itunes_summary", "")
            or entry.get("summary", "")
            or entry.get("description", "")
        )
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text().strip()
        thumbnail = None

        # YouTube のサムネイル
        if "youtube.com/watch" in entry.link or "youtu.be/" in entry.link:
            vid = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]+)", entry.link)
            if vid:
                thumbnail = f"https://img.youtube.com/vi/{vid.group(1)}/hqdefault.jpg"

        # podcast 系（itunes:image または チャンネル画像）
        elif entry.get("itunes_image", None):
            it_img = entry.get("itunes_image")
            thumbnail = it_img.href if hasattr(it_img, "href") else it_img
        elif feed_image:
            thumbnail = feed_image

        if not text:
            text = self._translate_title(entry.title)

        return Article(
            feed_name=feed_name,
            title=entry.title,
            url=entry.link,
            text=text,
            soup=soup,
            rss_summary=html,
            thumbnail_url=thumbnail,
        )

    def _translate_title(self, title: str) -> str:
        prompt = f"次の英語タイトルを日本語に翻訳してください:\n\n{title}"
        return self._client.generate_content(
            contents=prompt,
            system_instruction="ユーザーが与えた英語のタイトルを自然で読みやすい日本語に翻訳してください。必ず一案の翻訳を出力してください。"
            )

    def _stylize_article(self, article: Article) -> str:
        if article.thumbnail_url:
            thumbnail_card = (
                f'<a href="{article.url}" target="_blank">'
                f'<img src="{article.thumbnail_url}" '
                f'alt="thumbnail" width="{_THUMBNAIL_WIDTH}" />'
                f'</a>'
            )
        else:
            thumbnail_card = ""
        return _MARKDOWN_FORMAT.format(
            title=article.title,
            feed_name=article.feed_name,
            url=article.url,
            summary=article.summary,
            thumbnail_card=thumbnail_card,
        )

    def _summarize_article(self, article: Article) -> str:
        return self._client.generate_content(
            contents=self._contents_format.format(
                title=article.title,
                text=article.text
            ),
            system_instruction=self._system_instruction,
        )

    def _store_summaries(self, summaries: list[str]) -> None:
        date_str = date.today().strftime("%Y-%m-%d")
        key = Config.summary_index_s3_key_format.format(date=date_str)
        output_dir = os.environ.get("OUTPUT_DIR", "./output")
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, key)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n---\n".join(summaries))
        print(f"Saved summaries to {file_path}")

    @property
    def _system_instruction(self) -> str:
        return inspect.cleandoc(
            """
            ユーザーからSNSコンテンツのタイトルと説明文が与えられるので、内容をよく読み、日本語で詳細な要約を作成してください。
            与えられる文章はHTMLから抽出された文章なので、一部情報が欠落していたり、数式、コード、不必要な文章などが含まれている場合があります。
            要約以外の出力は不要です。SNSへのリンクやクレジットなどは含めないでください。記載されていない内容は出力しないでください。
            """
        )

    @property
    def _contents_format(self) -> str:
        return inspect.cleandoc(
            """
            {title}

            本文:
            {text}
            """
        )


if __name__ == "__main__":
    sns_feed = SnsFeed()
    sns_feed()
