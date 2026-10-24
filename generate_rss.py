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
