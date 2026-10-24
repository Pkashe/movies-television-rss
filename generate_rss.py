#!/usr/bin/env python3
"""Publish one Feedly-compatible RSS feed from the last successful Apify task run."""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from html import escape
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

TASK = os.getenv('APIFY_TASK', 'notched_trainee~movies-television-top-today')
ROOT_URL = os.getenv('FEED_BASE_URL', 'https://pkashe.github.io/movies-television-rss').rstrip('/')
OUTPUT = Path(os.getenv('OUTPUT_DIR', 'site'))
NS_CONTENT = 'http://purl.org/rss/1.0/modules/content/'
NS_MEDIA = 'http://search.yahoo.com/mrss/'
ET.register_namespace('content', NS_CONTENT)
ET.register_namespace('media', NS_MEDIA)


def permitted_url(raw, require_image=False):
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    try:
        parsed = urlparse(raw)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.hostname in ('localhost',) or parsed.hostname.endswith('.local'):
            return None
        if require_image and parsed.path.lower().endswith(('.svg', '.svgz')):
            return None
        return raw
    except ValueError:
        return None


def extract_image(post):
    for key in ('imageUrl','thumbnailUrl','thumbnail','externalUrl'):
        val = post.get(key)
        if isinstance(val, dict):
            val = val.get('url')
        if isinstance(val, str):
            url = permitted_url(val, require_image=True)
            if url and (key != 'externalUrl' or urlparse(url).path.lower().endswith(('.jpg','.jpeg','.png','.webp','.gif'))):
                return url
    galleries = post.get('galleryImagesUrls') or post.get('galleryImages') or []
    if isinstance(galleries, list):
        for item in galleries:
            if isinstance(item, dict):
                item = item.get('url') or item.get('imageUrl')
            url = permitted_url(item, require_image=True)
            if url:
                return url
    return None


def post_date(post):
    for key in ('createdAt','createdUtc','created_utc','publishedAt','date','timestamp','created'):
        x = post.get(key)
        if isinstance(x, (int,float)):
            try:
                return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, timezone.utc)
            except (ValueError, OverflowError):
                pass
        if isinstance(x, str) and x.strip():
            try:
                return datetime.fromisoformat(x.replace('Z','+00:00')).astimezone(timezone.utc)
            except ValueError:
                pass
    return None


def convert(data, feed_url=ROOT_URL + '/feed.xml'):
    if not isinstance(data, list):
        raise ValueError('Expected a list of Apify records')
    rss = ET.Element('rss', {'version':'2.0'})
    channel = ET.SubElement(rss,'channel')
    for tag, value in [('title','Movies & Television - Reddit'), ('link',ROOT_URL+'/'), ('description','Top posts today from r/movies and r/television.'), ('language','en'), ('ttl','480')]:
        ET.SubElement(channel,tag).text = value
    ET.SubElement(channel,'atom:link', {'xmlns:atom':'http://www.w3.org/2005/Atom','href':feed_url,'rel':'self','type':'application/rss+xml'})
    entries = {}
    for post in data:
        if not isinstance(post,dict):
            continue
        sub = str(post.get('subreddit','')).strip().lower().replace('r/','').strip('/')
        if sub not in ('movies','television'):
            continue
        title = str(post.get('title') or '').strip()
        link = permitted_url(post.get('submissionUrl') or post.get('url') or post.get('permalink'))
        if not link or not title:
            continue
        ident = str(post.get('submissionId') or '').strip() or link
        key = (sub,ident)
        if key in entries:
            continue
        entries[key] = (post, title, link, post_date(post))
    # Keep dataset order (Top Today rank) when no dates are present; otherwise reverse chronologically.
    ordered = sorted(entries.values(),key=lambda item: item[3] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for post,title,link,dt in ordered:
        sub = str(post.get('subreddit','')).lower().replace('r/','').strip('/')
        item = ET.SubElement(channel,'item')
        ET.SubElement(item,'title').text = f"[{'Movies' if sub == 'movies' else 'Television'}] {title}"
        ET.SubElement(item,'link').text = link
        guid = ET.SubElement(item,'guid',{'isPermaLink':'false'})
        guid.text = 'reddit:'+sub+':'+str(post.get('submissionId') or link)
        if dt:
            ET.SubElement(item,'pubDate').text = format_datetime(dt)
        image = extract_image(post)
        body = str(post.get('bodyText') or post.get('selftext') or '').strip()
        body = re.sub(r'\s+',' ',body)[:1800]
        desc = f'<p><b>r/{escape(sub)}</b></p>'
        if image:
            desc += f'<p><img src="{escape(image, quote=True)}" alt="Post image" /></p>'
            ET.SubElement(item,f'{{{NS_MEDIA}}}thumbnail',{'url':image})
        if body:
            desc += '<p>'+escape(body)+'</p>'
        desc += '<p><a href="'+escape(link,quote=True)+'">Open on Reddit</a></p>'
        ET.SubElement(item,'description').text = desc
    if not entries:
        raise ValueError('No valid r/movies or r/television records; refusing to publish an empty feed')
    return ET.tostring(rss,encoding='utf-8',xml_declaration=True),len(entries)


def fetch():
    token = os.getenv('APIFY_TOKEN','')
    if not token:
        raise RuntimeError('APIFY_TOKEN GitHub Actions secret is not configured')
    taskid=quote(TASK,safe='~')
    api='https://api.apify.com/v2/actor-tasks/'+taskid+'/runs/last'
    headers={'Authorization':'Bearer '+token,'Accept':'application/json','User-Agent':'movies-tv-rss/1.0'}
    def read_json(url):
        with urlopen(Request(url,headers=headers),timeout=40) as response:
            return json.load(response)
    run=read_json(api+'?status=SUCCEEDED')['data']
    if run.get('status') != 'SUCCEEDED':
        raise RuntimeError('No successful Apify run available')
    finished=run.get('finishedAt')
    if not finished:
        raise RuntimeError('Run has no finish date')
    completed=datetime.fromisoformat(finished.replace('Z','+00:00'))
    if datetime.now(timezone.utc)-completed > timedelta(hours=36):
        raise RuntimeError('Last successful Apify run is over 36 hours old: refusing to publish stale data')
    datasetid=run.get('defaultDatasetId')
    if not datasetid:
        raise RuntimeError('No dataset ID in Apify run')
    data=read_json('https://api.apify.com/v2/datasets/'+quote(datasetid,safe='')+'/items?format=json&limit=200&clean=1')
    return data


def main():
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')) if len(sys.argv)>1 else fetch()
    result,count=convert(data)
    OUTPUT.mkdir(parents=True,exist_ok=True)
    (OUTPUT/'feed.xml').write_bytes(result)
    (OUTPUT/'index.html').write_text('<!doctype html><html><head><meta charset="utf-8"><title>Movies & Television - Reddit</title></head><body><h1>Movies & Television - Reddit</h1><p><a href="feed.xml">Subscribe to RSS feed</a></p></body></html>\n',encoding='utf-8')
    (OUTPUT/'.nojekyll').touch()
    print(f'Generated {count} RSS items')

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('RSS generation failed: '+str(exc),file=sys.stderr)
        sys.exit(1)
