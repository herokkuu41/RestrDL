import os
import re
import uuid
import html
import shutil
import inspect
import asyncio
import urllib.parse
from typing import Optional, Tuple, Callable

import aiohttp
from pyrogram.types import Message
from pyrogram.enums import MessageEntityType

from config import PyroConf
from logger import LOGGER

DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def extract_watch_board_url(message: Message) -> Optional[str]:
    """Extract Watch Board video URL from message reply_markup buttons, entities, or text."""
    if not message:
        return None

    candidate_url = None

    def is_telegram_or_chat_link(u: str) -> bool:
        u_l = (u or "").lower()
        return any(domain in u_l for domain in ("t.me", "telegram.me", "telegram.dog", "tg://", "chat.whatsapp.com"))

    # 1. Check inline keyboard markup
    reply_markup = getattr(message, "reply_markup", None)
    if reply_markup and hasattr(reply_markup, "inline_keyboard"):
        for row in reply_markup.inline_keyboard:
            for button in row:
                btn_url = getattr(button, "url", None)
                if not btn_url:
                    continue
                btn_text = (getattr(button, "text", "") or "").lower()
                url_lower = btn_url.lower()

                # High-confidence watch board matches
                if any(w in btn_text for w in ("watch board", "watch", "board", "video")):
                    if not is_telegram_or_chat_link(btn_url):
                        return btn_url
                if any(k in url_lower for k in ("unacadamy", "uamedia", "output.webm", ".webm")):
                    return btn_url

                # Candidate button fallback (must not be telegram channel/chat link)
                if not candidate_url and not is_telegram_or_chat_link(btn_url):
                    candidate_url = btn_url

    # 2. Check caption entities and message entities
    entities = getattr(message, "caption_entities", None) or getattr(message, "entities", None) or []
    full_text = getattr(message, "caption", None) or getattr(message, "text", None) or ""

    for entity in entities:
        ent_type = getattr(entity, "type", None)
        ent_url = getattr(entity, "url", None)

        # text_link entity
        if ent_url and (ent_type == MessageEntityType.TEXT_LINK or str(ent_type).lower().endswith("text_link")):
            url_lower = ent_url.lower()
            if any(k in url_lower for k in ("unacadamy", "uamedia", "output.webm", ".webm")):
                return ent_url
            sub = full_text[entity.offset:entity.offset + entity.length].lower() if full_text else ""
            if any(w in sub for w in ("watch board", "watch", "board", "video")):
                if not is_telegram_or_chat_link(ent_url):
                    return ent_url
            if not candidate_url and not is_telegram_or_chat_link(ent_url):
                candidate_url = ent_url

        # plain URL entity
        elif ent_type == MessageEntityType.URL or str(ent_type).lower().endswith(".url"):
            if full_text and entity.offset is not None and entity.length is not None:
                url_str = full_text[entity.offset:entity.offset + entity.length]
                url_lower = url_str.lower()
                if any(k in url_lower for k in ("unacadamy", "uamedia", "output.webm", ".webm")):
                    return url_str
                if not candidate_url and not is_telegram_or_chat_link(url_str):
                    candidate_url = url_str

    # 3. Check raw text/caption for known domain or extension
    if full_text:
        match = re.search(r'https?://[^\s"\'<>]+(?:unacadamy|uamedia|\.webm)[^\s"\'<>]*', full_text, re.IGNORECASE)
        if match:
            return match.group(0)

    # 4. Fallback to candidate button URL if present
    if candidate_url:
        return candidate_url

    return None


def extract_title_and_filename(text: Optional[str], default_id: int) -> Tuple[str, str]:
    """Extract title from 'Title : [...]' and password from '[PASS: ...]', sanitize safe .webm filename."""
    clean_text = text or ""

    # 1. Title extraction
    title_match = re.search(r"Title\s*:\s*([^\n\r]+)", clean_text, re.IGNORECASE)
    if title_match:
        raw_title = title_match.group(1).strip()
    else:
        # Fallback to first non-empty line if it doesn't look like a URL or pass line
        non_empty = [line.strip() for line in clean_text.splitlines() if line.strip()]
        first_line = non_empty[0] if non_empty else ""
        if first_line and not re.match(r"^(?:https?://|pass\s*:|password\s*:)", first_line, re.IGNORECASE):
            raw_title = first_line
        else:
            raw_title = f"watch_board_{default_id}"

    # 2. Password extraction
    pass_match = re.search(r"(?:\[PASS\s*:\s*([^\]]+)\]|(?:pass|password)\s*:\s*([A-Za-z0-9_-]+))", clean_text, re.IGNORECASE)
    pass_val = None
    if pass_match:
        pass_val = (pass_match.group(1) or pass_match.group(2) or "").strip()

    if pass_val and not re.search(r"PASS\s*:", raw_title, re.IGNORECASE):
        title = f"{raw_title} [PASS: {pass_val}]"
    else:
        title = raw_title

    # 3. Filename sanitization
    # Strip illegal filesystem characters: < > : " / \ | ? * and control chars
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)
    safe_name = re.sub(r'\s+', ' ', safe_name).strip(" ._")

    base = safe_name[:-5] if safe_name.lower().endswith(".webm") else safe_name
    base = base.strip(" ._")
    if not base:
        base = f"watch_board_{default_id}"
    elif len(base) > 180:
        base = base[:180].strip(" ._")

    safe_name = f"{base}.webm"

    return title, safe_name


async def resolve_video_cdn_url(redirect_url: str) -> Optional[str]:
    """Asynchronously follow redirects and extract CDN video URL (?url= query param or HTML link)."""
    if not redirect_url or not isinstance(redirect_url, str):
        return None

    # Check direct query parameter if already in URL
    parsed = urllib.parse.urlparse(redirect_url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "url" in qs and qs["url"]:
        candidate = qs["url"][0].strip()
        if candidate.startswith("http"):
            return candidate

    if "uamedia.uacdn.net" in redirect_url and redirect_url.startswith("http"):
        return redirect_url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(redirect_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                final_url = str(resp.url)
                parsed_final = urllib.parse.urlparse(final_url)
                qs_final = urllib.parse.parse_qs(parsed_final.query)
                if "url" in qs_final and qs_final["url"]:
                    candidate = qs_final["url"][0].strip()
                    if candidate.startswith("http"):
                        return candidate

                content_type = ""
                if hasattr(resp, "headers") and hasattr(resp.headers, "get"):
                    raw_ct = resp.headers.get("Content-Type", "")
                    if isinstance(raw_ct, str):
                        content_type = raw_ct.lower()
                if "video" in content_type or final_url.lower().endswith(".webm") or "uamedia.uacdn.net" in final_url:
                    return final_url

                body = await resp.text(errors="ignore")
                body = html.unescape(body)
                body = body.replace(r'\/', '/').replace('\\/', '/')

                # Regex scan for direct uamedia URL
                match_cdn = re.search(r'https?://uamedia\.uacdn\.net/[^\s"\'<>]+', body)
                if match_cdn:
                    cand = match_cdn.group(0).rstrip(").,;'\"")
                    return cand

                # Regex scan for any webm URL
                match_webm = re.search(r'https?://[^\s"\'<>]+\.webm[^\s"\'<>]*', body)
                if match_webm:
                    cand = match_webm.group(0).rstrip(").,;'\"")
                    return cand

                # Regex scan for ?url= or &url= in HTML
                match_param = re.search(r'[?&]url=([^\s"\'&]+)', body)
                if match_param:
                    decoded = urllib.parse.unquote(match_param.group(1)).strip().rstrip(").,;'\"")
                    if decoded.startswith("http"):
                        return decoded

    except Exception as e:
        LOGGER(__name__).warning(f"Failed to resolve Watch Board CDN URL for '{redirect_url}': {e}")
        return None

    return None


async def download_watch_board_video(
    cdn_url: str,
    dest_path: str,
    progress: Optional[Callable] = None,
    progress_args: tuple = (),
    abort_event: Optional[asyncio.Event] = None,
    max_size: Optional[int] = None,
) -> Optional[str]:
    """Stream WebM video in 1 MiB chunks to disk, checking DISK_RESERVE_MIB and abort_event."""
    if not cdn_url or not dest_path:
        return None

    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or "."
    os.makedirs(dest_dir, exist_ok=True)

    reserve_bytes = PyroConf.DISK_RESERVE_MIB * 1024 * 1024
    temp_path = f"{dest_path}.temp_{os.getpid()}_{uuid.uuid4().hex[:8]}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(cdn_url, timeout=aiohttp.ClientTimeout(total=3600, sock_read=60)) as resp:
                if resp.status not in (200, 206):
                    LOGGER(__name__).error(f"Download failed with HTTP {resp.status} for {cdn_url}")
                    return None

                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/html" in content_type or "json" in content_type:
                    raise ValueError("Source returned a web page, not a video")
                if resp.status == 206:
                    content_range = resp.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
                    if not match or int(match[1]) + 1 != int(match[2]):
                        raise ValueError("Source returned an incomplete range, not the whole video")

                try:
                    total_size = int(resp.headers.get("Content-Length", 0) or 0)
                except (ValueError, TypeError):
                    total_size = 0

                if max_size and total_size > max_size:
                    raise ValueError("Source video exceeds the destination upload limit")

                free_space = shutil.disk_usage(dest_dir).free
                if total_size > 0 and (free_space - total_size < reserve_bytes):
                    LOGGER(__name__).error(
                        f"Insufficient disk space for download: free={free_space}, required={total_size}, reserve={reserve_bytes}"
                    )
                    return None

                downloaded = 0
                last_progress = None
                with open(temp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                        if abort_event and abort_event.is_set():
                            LOGGER(__name__).info("Watch board download aborted by abort_event")
                            return None

                        if shutil.disk_usage(dest_dir).free - len(chunk) < reserve_bytes:
                            LOGGER(__name__).error("Disk space fell below DISK_RESERVE_MIB during download")
                            return None

                        f.write(chunk)
                        downloaded += len(chunk)
                        if max_size and downloaded > max_size:
                            raise ValueError("Source video exceeds the destination upload limit")

                        if progress:
                            try:
                                eff_total = total_size if total_size > 0 else (downloaded + DOWNLOAD_CHUNK_SIZE)
                                last_progress = (downloaded, eff_total)
                                if inspect.iscoroutinefunction(progress):
                                    await progress(downloaded, eff_total, *progress_args)
                                else:
                                    res = progress(downloaded, eff_total, *progress_args)
                                    if inspect.isawaitable(res):
                                        await res
                            except Exception as pe:
                                LOGGER(__name__).debug(f"Progress callback error: {pe}")

                if abort_event and abort_event.is_set():
                    return None

                if downloaded == 0:
                    LOGGER(__name__).error(f"Downloaded 0 bytes from {cdn_url}")
                    return None

                if total_size and downloaded != total_size:
                    raise ValueError(f"Truncated video: expected {total_size} bytes, received {downloaded}")

                if progress:
                    try:
                        final_total = total_size if total_size > 0 else downloaded
                        if last_progress != (downloaded, final_total):
                            if inspect.iscoroutinefunction(progress):
                                await progress(downloaded, final_total, *progress_args)
                            else:
                                res = progress(downloaded, final_total, *progress_args)
                                if inspect.isawaitable(res):
                                    await res
                    except Exception as pe:
                        LOGGER(__name__).debug(f"Final progress callback error: {pe}")

                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                os.replace(temp_path, dest_path)
                return dest_path

    except asyncio.CancelledError:
        LOGGER(__name__).info("Watch board download task cancelled")
        raise
    except Exception as e:
        LOGGER(__name__).error(f"Error downloading Watch Board video from {cdn_url}: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
