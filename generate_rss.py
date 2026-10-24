
#!/usr/bin/env python3
"""Generate a combined r/movies and r/television RSS feed.

Uses the latest successful Apify task dataset.
Extracts thumbnails from Reddit, YouTube and external article metadata.
Does not trigger additional Apify runs.
"""

import html
import ipaddress
import json
import os
import re
import socket
import sys

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse, parse_qs
from urllib.request import (
    Request,
    urlopen,
    build_opener,
    HTTPSHandler,
    HTTPRedirectHandler,
)
from xml.etree import ElementTree as ET


# ---------------- CONFIGURATION ----------------

TASK = os.getenv("APIFY_TASK", "5IGGtHCecD58Bptm3")

ROOT_URL = os.getenv(
    "FEED_BASE_URL",
    "https://pkashe.github.io/movies-television-rss",
).rstrip("/")

OUTPUT = Path(os.getenv("OUTPUT_DIR", "site"))

MAX_ARTICLE_LOOKUPS = 40
MAX_HTML_BYTES = 250_000

NS_MEDIA = "http://search.yahoo.com/mrss/"
ET.register_namespace("media", NS_MEDIA)


# ---------------- URL SAFETY ----------------

def allowed_url(value, image=False):
    if not isinstance(value, str):
        return None

    value = html.unescape(value).strip()

    if len(value) > 2048:
        return None

    try:
        parsed = urlparse(value)

        if parsed.scheme != "https":
            return None

        if not parsed.hostname:
            return None

        if parsed.username or parsed.password:
            return None

        if parsed.port not in (None, 443):
            return None

        hostname = parsed.hostname.lower().rstrip(".")

        if hostname == "localhost":
            return None

        if hostname.endswith(
            (".localhost", ".local", ".internal")
        ):
            return None

        try:
            if not ipaddress.ip_address(hostname).is_global:
                return None
        except ValueError:
            pass

        if image and parsed.path.lower().endswith(
            (".svg", ".svgz")
        ):
            return None

        return value

    except ValueError:
        return None


def public_dns(hostname):
    try:
        addresses = socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM,
        )

        return bool(addresses) and all(
            ipaddress.ip_address(
                record[4][0]
            ).is_global
            for record in addresses
        )

    except (OSError, ValueError):
        return False


class SafeRedirect(HTTPRedirectHandler):

    def redirect_request(
        self, req, fp, code, msg, headers, newurl
    ):
        target = allowed_url(newurl)

        if not target:
            raise ValueError("Unsafe redirect")

        hostname = urlparse(target).hostname

        if not public_dns(hostname):
            raise ValueError("Unsafe redirect destination")

        return super().redirect_request(
            req, fp, code, msg, headers, newurl
        )


# ---------------- IMAGE EXTRACTION ----------------

class ImageMetadataParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.candidates = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return

        attrs = dict(
            (key.lower(), value)
            for key, value in attrs
            if key
        )

        label = (
            attrs.get("property")
            or attrs.get("name")
            or ""
        ).lower()

        accepted = (
            "og:image",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
        )

        if label in accepted:
            candidate = attrs.get("content")

            if candidate:
                self.candidates.append(candidate)


def youtube_thumbnail(url):
    """Create a YouTube thumbnail from its video ID."""

    parsed = urlparse(url)

    hostname = (parsed.hostname or "").lower()

    video_id = None

    if hostname in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]

    elif hostname in (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    ):
        if parsed.path == "/watch":
            video_id = parse_qs(
                parsed.query
            ).get("v", [None])[0]

        elif parsed.path.startswith(
            ("/shorts/", "/embed/", "/live/")
        ):
            video_id = parsed.path.split("/")[2]

    if video_id and re.fullmatch(
        r"[A-Za-z0-9_-]{11}", video_id
    ):
        return (
            "https://i.ytimg.com/vi/"
            + video_id
            + "/hqdefault.jpg"
        )

    return None


def article_image(url):
    """Retrieve og:image from an external article."""

    url = allowed_url(url)

    if not url:
        return None

    hostname = urlparse(url).hostname

    if hostname.endswith(
        ("reddit.com", "redd.it")
    ):
        return None

    if not public_dns(hostname):
        return None

    try:
        opener = build_opener(
            HTTPSHandler(),
            SafeRedirect(),
        )

        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; MoviesTelevisionRSS/2.0)"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml"
                ),
            },
        )

        with opener.open(
            request,
            timeout=5,
        ) as response:

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if "text/html" not in content_type:
                return None

            final_url = response.geturl()

            raw = response.read(MAX_HTML_BYTES)

            encoding = (
                response.headers.get_content_charset()
                or "utf-8"
            )

        parser = ImageMetadataParser()

        parser.feed(
            raw.decode(encoding, errors="replace")
        )

        for candidate in parser.candidates:

            image = allowed_url(
                urljoin(final_url, candidate),
                image=True,
            )

            if image:
                return image

    except Exception as error:
        print(
            "Article thumbnail unavailable:",
            str(error)[:100],
        )

    return None


def direct_image(post):
    """Use an image already present in the Apify dataset."""

    keys = (
        "imageUrl",
        "thumbnailUrl",
        "thumbnail",
        "externalUrl",
    )

    for key in keys:

        value = post.get(key)

        if isinstance(value, dict):
            value = value.get("url")

        candidate = allowed_url(
            value,
            image=True,
        )

        if not candidate:
            continue

        if key == "externalUrl":

            path = urlparse(
                candidate
            ).path.lower()

            if not path.endswith(
                (".jpg", ".jpeg", ".png", ".webp", ".gif")
            ):
                continue

        return candidate

    galleries = (
        post.get("galleryImagesUrls")
        or post.get("galleryImages")
        or []
    )

    if isinstance(galleries, list):

        for entry in galleries:

            if isinstance(entry, dict):
                entry = (
                    entry.get("url")
                    or entry.get("imageUrl")
                )

            candidate = allowed_url(
                entry,
                image=True,
            )

            if candidate:
                return candidate

    return None


def resolve_image(post, cache, stats):
    """Find the best available thumbnail."""

    picture = direct_image(post)

    if picture:
        stats["reddit"] += 1
        return picture

    external = allowed_url(
        post.get("externalUrl")
        or post.get("outboundUrl")
        or post.get("linkUrl")
    )

    if not external:
        return None

    youtube = youtube_thumbnail(external)

    if youtube:
        stats["youtube"] += 1
        return youtube

    if external in cache:
        return cache[external]

    if len(cache) >= MAX_ARTICLE_LOOKUPS:
        return None

    picture = article_image(external)

    cache[external] = picture

    if picture:
        stats["article"] += 1

    return picture


# ---------------- DATE HANDLING ----------------

def post_date(post):

    keys = (
        "submittedAt",
        "createdAt",
        "createdUtc",
        "created_utc",
        "publishedAt",
        "date",
        "timestamp",
        "created",
    )

    for key in keys:

        value = post.get(key)

        try:

            if isinstance(value, (int, float)):

                timestamp = (
                    value / 1000
                    if value > 1e11
                    else value
                )

                return datetime.fromtimestamp(
                    timestamp,
                    timezone.utc,
                )

            if isinstance(value, str) and value.strip():

                parsed = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )

                if parsed.tzinfo is None:
                    parsed = parsed.replace(
                        tzinfo=timezone.utc
                    )

                return parsed.astimezone(
                    timezone.utc
                )

        except (ValueError, OverflowError):
            pass

    return None


# ---------------- RSS GENERATION ----------------

def convert(data):

    if not isinstance(data, list):
        raise ValueError(
            "Expected a list of Apify records"
        )

    rss = ET.Element(
        "rss",
        {"version": "2.0"},
    )

    channel = ET.SubElement(
        rss,
        "channel",
    )

    metadata = (
        ("title", "Movies & Television - Reddit"),
        ("link", ROOT_URL + "/"),
        (
            "description",
            "Top posts today from r/movies and r/television.",
        ),
        ("language", "en"),
        ("ttl", "1440"),
    )

    for key, value in metadata:
        ET.SubElement(
            channel,
            key,
        ).text = value

    ET.SubElement(
        channel,
        "atom:link",
        {
            "xmlns:atom": (
                "http://www.w3.org/2005/Atom"
            ),
            "href": ROOT_URL + "/feed.xml",
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    seen = set()
    posts = []

    for post in data:

        if not isinstance(post, dict):
            continue

        subreddit = str(
            post.get("subreddit") or ""
        ).lower().removeprefix("r/").strip("/")

        if subreddit not in (
            "movies",
            "television",
        ):
            continue

        title = str(
            post.get("title") or ""
        ).strip()

        link = allowed_url(
            post.get("submissionUrl")
            or post.get("url")
            or post.get("permalink")
        )

        identifier = str(
            post.get("submissionId") or link
        )

        if not title or not link:
            continue

        key = (subreddit, identifier)

        if key in seen:
            continue

        seen.add(key)

        posts.append(
            (
                post,
                subreddit,
                title,
                link,
                identifier,
                post_date(post),
            )
        )

    if not posts:
        raise ValueError(
            "No valid movies or television posts found"
        )

    posts.sort(
        key=lambda record: (
            record[5]
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True,
    )

    cache = {}

    stats = {
        "reddit": 0,
        "youtube": 0,
        "article": 0,
    }

    for (
        post,
        subreddit,
        title,
        link,
        identifier,
        date,
    ) in posts:

        item = ET.SubElement(
            channel,
            "item",
        )

        label = (
            "Movies"
            if subreddit == "movies"
            else "Television"
        )

        ET.SubElement(
            item,
            "title",
        ).text = f"[{label}] {title}"

        ET.SubElement(
            item,
            "link",
        ).text = link

        ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "false"},
        ).text = (
            f"reddit:{subreddit}:{identifier}"
        )

        if date:

            ET.SubElement(
                item,
                "pubDate",
            ).text = format_datetime(date)

        picture = resolve_image(
            post,
            cache,
            stats,
        )

        body = str(
            post.get("bodyText")
            or post.get("selftext")
            or ""
        ).strip()

        body = re.sub(
            r"\s+",
            " ",
            body,
        )[:1800]

        description = (
            f"<p><b>r/{html.escape(subreddit)}</b></p>"
        )

        if picture:

            ET.SubElement(
                item,
                f"{{{NS_MEDIA}}}thumbnail",
                {"url": picture},
            )

            description += (
                '<p><img src="'
                + html.escape(picture, quote=True)
                + '" alt="Post image" /></p>'
            )

        if body:

            description += (
                "<p>"
                + html.escape(body)
                + "</p>"
            )

        description += (
            '<p><a href="'
            + html.escape(link, quote=True)
            + '">Open on Reddit</a></p>'
        )

        ET.SubElement(
            item,
            "description",
        ).text = description

    result = ET.tostring(
        rss,
        encoding="utf-8",
        xml_declaration=True,
    )

    print("RSS image statistics:")
    print("Reddit images:", stats["reddit"])
    print("YouTube thumbnails:", stats["youtube"])
    print("Article thumbnails:", stats["article"])

    total_images = sum(stats.values())

    print(
        "Total thumbnails:",
        total_images,
        "/",
        len(posts),
    )

    return result, len(posts)


# ---------------- APIFY DATA FETCH ----------------

def fetch():

    token = os.getenv(
        "APIFY_TOKEN",
        "",
    )

    if not token:
        raise RuntimeError(
            "APIFY_TOKEN repository secret is missing"
        )

    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "movies-tv-rss/2.0",
    }

    def api_json(url):

        request = Request(
            url,
            headers=headers,
        )

        with urlopen(
            request,
            timeout=40,
        ) as response:

            return json.load(response)

    task_id = quote(
        TASK,
        safe="~",
    )

    run_url = (
        "https://api.apify.com/v2/actor-tasks/"
        + task_id
        + "/runs/last?status=SUCCEEDED"
    )

    info = api_json(
        run_url
    )["data"]

    finished = info.get(
        "finishedAt"
    )

    if not finished:
        raise RuntimeError(
            "No completed Apify task run found"
        )

    completed = datetime.fromisoformat(
        finished.replace("Z", "+00:00")
    )

    if (
        datetime.now(timezone.utc) - completed
        > timedelta(hours=48)
    ):
        raise RuntimeError(
            "Last successful Apify run is over 48 hours old"
        )

    dataset_id = info.get(
        "defaultDatasetId"
    )

    if not dataset_id:
        raise RuntimeError(
            "Apify run has no dataset ID"
        )

    # IMPORTANT:
    # Do not add clean=1.
    # That can remove extended fields required for images.

    dataset_url = (
        "https://api.apify.com/v2/datasets/"
        + quote(dataset_id, safe="")
        + "/items?format=json&limit=200"
    )

    data = api_json(
        dataset_url
    )

    print(
        "Downloaded",
        len(data),
        "Apify records",
    )

    return data


# ---------------- MAIN ----------------

def main():

    if len(sys.argv) > 1:

        data = json.loads(
            Path(sys.argv[1]).read_text(
                encoding="utf-8"
            )
        )

    else:

        data = fetch()

    result, count = convert(data)

    OUTPUT.mkdir(
        parents=True,
        exist_ok=True,
    )

    (OUTPUT / "feed.xml").write_bytes(
        result
    )

    (OUTPUT / "index.html").write_text(
        (
            "<!doctype html>"
            "<html><head>"
            '<meta charset="utf-8">'
            "<title>Movies & Television - Reddit</title>"
            "</head><body>"
            "<h1>Movies & Television - Reddit</h1>"
            '<p><a href="feed.xml">'
            "Subscribe to RSS feed"
            "</a></p>"
            "</body></html>\n"
        ),
        encoding="utf-8",
    )

    (OUTPUT / ".nojekyll").touch()

    print(
        f"Generated {count} RSS items"
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as error:

        print(
            "RSS generation failed:",
            str(error),
            file=sys.stderr,
        )

        sys.exit(1)
