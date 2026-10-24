
#!/usr/bin/env python3
"""Build a combined movie/TV RSS feed from the latest successful Apify task.

For external article posts, try og:image metadata as an optional thumbnail fallback.
Only the GitHub runner visits article sites; this never starts another Apify run.
"""
import html
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen, build_opener, HTTPSHandler, HTTPRedirectHandler
from xml.etree import ElementTree as ET

TASK = os.getenv('APIFY_TASK', '5IGGtHCecD58Bptm3')
ROOT_URL = os.getenv('FEED_BASE_URL', 'https://pkashe.github.io/movies-television-rss').rstrip('/')
OUTPUT = Path(os.getenv('OUTPUT_DIR', 'site'))
MAX_ARTICLE_LOOKUPS = 30
MAX_HTML_BYTES = 250_000
NS_MEDIA = 'http://search.yahoo.com/mrss/'
ET.register_namespace('media', NS_MEDIA)


def allowed_url(value, image=False):
    if not isinstance(value, str):
        return None
    value = html.unescape(value).strip()
    if len(value) > 2048:
        return None
    try:
        u = urlparse(value)
        if u.scheme != 'https' or not u.hostname or u.username or u.password:
            return None
        if u.port not in (None, 443):
            return None
        hostname = u.hostname.lower().rstrip('.')
        if hostname in ('localhost',) or hostname.endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            if not ipaddress.ip_address(hostname).is_global:
                return None
        except ValueError:
            pass
        if image and u.path.lower().endswith(('.svg', '.svgz')):
            return None
        return value
    except ValueError:
        return None


def public_dns(hostname):
    """Never request domains resolving to private, reserved or loopback addresses."""
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(rec[4][0]).is_global for rec in addresses)
    except (OSError, ValueError):
        return False


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = allowed_url(newurl)
        if not target or not public_dns(urlparse(target).hostname):
            raise ValueError('Unsafe redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ImageMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.candidates = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'meta':
            return
        attrs = dict((k.lower(), v) for k, v in attrs if k)
        label = (attrs.get('property') or attrs.get('name') or '').lower()
        if label in ('og:image:secure_url', 'og:image', 'twitter:image', 'twitter:image:src'):
            candidate = attrs.get('content')
            if candidate:
                self.candidates.append(candidate)


def article_image(url):
    url = allowed_url(url)
    if not url:
        return None
    host = urlparse(url).hostname
    if host.endswith(('reddit.com', 'redd.it')) or not public_dns(host):
        return None
    try:
        opener = build_opener(HTTPSHandler(), SafeRedirect())
        req = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; MoviesTelevisionRSS/1.1)',
            'Accept': 'text/html,application/xhtml+xml',
        })
        with opener.open(req, timeout=5) as resp:
            if 'text/html' not in resp.headers.get('Content-Type', '').lower():
                return None
            final_url = resp.geturl()
            raw = resp.read(MAX_HTML_BYTES)
            encoding = resp.headers.get_content_charset() or 'utf-8'
        parser = ImageMetadataParser()
        parser.feed(raw.decode(encoding, errors='replace'))
        for image in parser.candidates:
            image = allowed_url(urljoin(final_url, image), image=True)
            if image:
                return image
    except (OSError, ValueError, UnicodeError, HTTPError, URLError):
        pass
    return None


def direct_image(post):
    for key in ('imageUrl', 'thumbnailUrl', 'thumbnail', 'externalUrl'):
        value = post.get(key)
        if isinstance(value, dict):
            value = value.get('url')
        candidate = allowed_url(value, image=True)
        if candidate and (key != 'externalUrl' or urlparse(candidate).path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))):
            return candidate
    gallery = post.get('galleryImagesUrls') or post.get('galleryImages') or []
    if isinstance(gallery, list):
        for value in gallery:
            if isinstance(value, dict):
                value = value.get('url') or value.get('imageUrl')
            candidate = allowed_url(value, image=True)
            if candidate:
                return candidate
    return None


def post_date(post):
    for key in ('createdAt', 'createdUtc', 'created_utc', 'publishedAt', 'date', 'timestamp', 'created'):
        value = post.get(key)
        try:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
            if isinstance(value, str) and value.strip():
                return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
        except (ValueError, OverflowError):
            pass
    return None


def convert(data, lookup_images=True):
    if not isinstance(data, list):
        raise ValueError('Expected Apify dataset list')
    rss = ET.Element('rss', {'version': '2.0'})
    channel = ET.SubElement(rss, 'channel')
    for key, value in [
        ('title', 'Movies & Television - Reddit'),
        ('link', ROOT_URL + '/'),
        ('description', 'Top posts today from r/movies and r/television.'),
        ('language', 'en'), ('ttl', '1440'),
    ]:
        ET.SubElement(channel, key).text = value
    ET.SubElement(channel, 'atom:link', {
        'xmlns:atom': 'http://www.w3.org/2005/Atom',
        'href': ROOT_URL + '/feed.xml', 'rel': 'self', 'type': 'application/rss+xml',
    })
    seen = set()
    posts = []
    for post in data:
        if not isinstance(post, dict):
            continue
        sub = str(post.get('subreddit') or '').lower().removeprefix('r/').strip('/')
        title = str(post.get('title') or '').strip()
        link = allowed_url(post.get('submissionUrl') or post.get('url') or post.get('permalink'))
        identifier = str(post.get('submissionId') or link)
        if sub not in ('movies', 'television') or not title or not link or (sub, identifier) in seen:
            continue
        seen.add((sub, identifier))
        posts.append((post, sub, title, link, identifier, post_date(post)))
    if not posts:
        raise ValueError('No valid movie/television posts found; refusing to publish empty feed')
    posts.sort(key=lambda v: v[5] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    cache = {}
    for post, sub, title, link, identifier, date in posts:
        item = ET.SubElement(channel, 'item')
        ET.SubElement(item, 'title').text = f"[{'Movies' if sub == 'movies' else 'Television'}] {title}"
        ET.SubElement(item, 'link').text = link
        ET.SubElement(item, 'guid', {'isPermaLink': 'false'}).text = f'reddit:{sub}:{identifier}'
        if date:
            ET.SubElement(item, 'pubDate').text = format_datetime(date)
        picture = direct_image(post)
        if not picture and lookup_images and len(cache) < MAX_ARTICLE_LOOKUPS:
            external = post.get('externalUrl') or post.get('outboundUrl') or post.get('linkUrl')
            external = allowed_url(external)
            if external:
                if external not in cache:
                    cache[external] = article_image(external)
                picture = cache[external]
        body = str(post.get('bodyText') or post.get('selftext') or '').strip()
        body = re.sub(r'\s+', ' ', body)[:1800]
        description = f'<p><b>r/{html.escape(sub)}</b></p>'
        if picture:
            ET.SubElement(item, f'{{{NS_MEDIA}}}thumbnail', {'url': picture})
            description += f'<p><img src="{html.escape(picture, quote=True)}" alt="Post image" /></p>'
        if body:
            description += '<p>' + html.escape(body) + '</p>'
        description += '<p><a href="' + html.escape(link, quote=True) + '">Open on Reddit</a></p>'
        ET.SubElement(item, 'description').text = description
    return ET.tostring(rss, encoding='utf-8', xml_declaration=True), len(posts)


def fetch():
    token = os.getenv('APIFY_TOKEN')
    if not token:
        raise RuntimeError('Missing APIFY_TOKEN repository secret')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': 'movies-tv-rss/1.1'}
    def api_json(url):
        with urlopen(Request(url, headers=headers), timeout=40) as response:
            return json.load(response)
    task_id = quote(TASK, safe='~')
    info = api_json('https://api.apify.com/v2/actor-tasks/' + task_id + '/runs/last?status=SUCCEEDED')['data']
    finished = info.get('finishedAt')
    if not finished:
        raise RuntimeError('No completed Apify task run found')
    dt = datetime.fromisoformat(finished.replace('Z', '+00:00'))
    if datetime.now(timezone.utc) - dt > timedelta(hours=48):
        raise RuntimeError('Last successful Apify task run older than 48 hours')
    dataset_id = info.get('defaultDatasetId')
    if not dataset_id:
        raise RuntimeError('Apify run did not return a dataset ID')
    return api_json('https://api.apify.com/v2/datasets/' + quote(dataset_id, safe='') + '/items?format=json&limit=200&clean=1')


def main():
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')) if len(sys.argv) > 1 else fetch()
    document, count = convert(data)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'feed.xml').write_bytes(document)
    (OUTPUT / 'index.html').write_text('<!doctype html><html><head><meta charset="utf-8"><title>Movies & Television - Reddit</title></head><body><h1>Movies & Television - Reddit</h1><a href="feed.xml">Subscribe to RSS</a></body></html>\n', encoding='utf-8')
    (OUTPUT / '.nojekyll').touch()
    print(f'Generated {count} RSS items')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('RSS generation failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
