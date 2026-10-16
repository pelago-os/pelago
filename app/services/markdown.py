from __future__ import annotations

import html
import re

import bleach
import markdown


ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p",
    "pre",
    "code",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "img",
}

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel", "target"],
    "code": ["class"],
    "img": ["src", "alt", "title"],
    "th": ["align"],
    "td": ["align"],
}


def render_markdown(source: str) -> str:
    raw_html = markdown.markdown(
        source,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
    clean = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    linked = bleach.linkify(clean, callbacks=[set_link_attrs])
    return VIDEO_IMG_PATTERN.sub(_video_tag, linked)


# 论坛自己上传的视频用 ![标题](/uploads/forum-videos/xxx.mp4) 插入；清洗完以后把这种 <img> 换成带播放控件的
# <video>。只认本论坛上传目录里、文件名合规的地址，外链视频和用户手写的 <video> 标签都已被上面的清洗去掉。
VIDEO_IMG_PATTERN = re.compile(r'<img\b[^>]*?\bsrc="(/uploads/forum-videos/[A-Za-z0-9._-]+\.(?:mp4|mov|webm))"[^>]*>')


def _video_tag(match: re.Match) -> str:
    return f'<video class="post-video" src="{match.group(1)}" controls preload="metadata" playsinline></video>'


def set_link_attrs(attrs: dict, new: bool = False) -> dict:
    href_key = (None, "href")
    if href_key in attrs:
        attrs[(None, "target")] = "_blank"
        attrs[(None, "rel")] = "noreferrer noopener"
    return attrs


def plain_excerpt(source: str, limit: int = 180) -> str:
    compact = " ".join(html.unescape(source).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."
