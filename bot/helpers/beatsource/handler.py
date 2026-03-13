# [GANTI SELURUH FILE: bot/helpers/beatsource/handler.py]

import aiohttp
import aiofiles
import os
import shutil
import traceback
import asyncio 
import random 

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None

from pathvalidate import sanitize_filepath
from config import Config

from .metadata import (
    process_track_metadata, 
    process_album_metadata, 
    process_playlist_metadata,
    custom_url_parse,
    write_extended_tags 
)
from .api import BeatsourceError, APP_USER_AGENT
from .manager import beatsource_manager

from ..utils import *

try:
    from ..uploder import *
except ImportError as e:
    raise ImportError(f"Gagal mengimpor uploder.py: {e}")

from ..metadata import set_metadata
from ..message import edit_message
from ..utils import fetch_zip_settings
from ...settings import bot_set 

import bot.helpers.translations as lang
from bot.logger import LOGGER

BEATSOURCE_SEMAPHORE = asyncio.Semaphore(5)

def make_progress_bar(current, total):
    if total == 0: return "▱" * 10
    percentage = current / total
    finished_length = int(percentage * 10)
    prog_str = "▰" * finished_length + "▱" * (10 - finished_length)
    return prog_str

async def update_progress_msg(msg, current, total, title, media_type):
    try:
        bar = make_progress_bar(current, total)
        formatted_text = lang.s.DOWNLOAD_PROGRESS.format(
            bar, current, total, title, media_type
        )
        await edit_message(msg, formatted_text)
    except: pass

async def refresh_track_url(item_id: str, current_meta: dict, user_id: int):
    try:
        pref_qual = current_meta.get('quality', 'High').lower()
        if pref_qual not in ['lossless', 'high', 'medium']:
            pref_qual = beatsource_manager.get_user_quality(user_id)

        quality_priority = ["medium"]
        if pref_qual == "lossless": quality_priority = ["lossless", "high", "medium"]
        elif pref_qual == "high": quality_priority = ["high", "medium"]

        LOGGER.info(f"Beatsource: Refreshing URL for {item_id} ({pref_qual})...")
        client = beatsource_manager.get_client(user_id)
        if not client: return None, None
            
        for qual in quality_priority:
            try:
                stream_data = await client.get_track_download(item_id, qual)
                new_url = stream_data.get("location")
                if new_url: return new_url, qual 
            except: continue
        return None, None
    except Exception as e:
        LOGGER.error(f"Gagal refresh URL Beatsource {item_id}: {e}")
        return None, None

async def start_beatsource(url: str, user: dict):
    try:
        media_type, item_id, extra_kwargs = custom_url_parse(url)

        if media_type == 'artist': raise NotImplementedError("Unduhan Artis Beatsource belum didukung.")
        elif media_type == 'track':
            success = await start_track(item_id, user, None)
            if not success: raise Exception("Gagal mengunduh track.")
        elif media_type == 'album': await start_album(item_id, user)
        elif media_type == 'playlist': await start_playlist(item_id, user, extra_kwargs)
    except Exception as e:
        LOGGER.error(f"Error Beatsource handler: {e}")
        raise e 

async def download_beatsource_track(url: str, filepath: str, proxy: str = None):
    try:
        headers = {"User-Agent": APP_USER_AGENT, "Accept": "*/*", "Referer": "https://www.beatsource.com/"}
        connector = None
        if proxy and ProxyConnector:
            try:
                proxy_url = proxy.replace("socks5h://", "socks5://") if proxy.startswith("socks5h://") else proxy
                connector = ProxyConnector.from_url(proxy_url, rdns=True)
            except: pass

        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=1800), connector=connector) as session:
            async with session.get(url) as response:
                if response.status == 403: return "403_FORBIDDEN"
                response.raise_for_status()
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                async with aiofiles.open(filepath, "wb") as f:
                    async for chunk in response.content.iter_chunked(8192): await f.write(chunk)
        return None 
    except Exception as e: return f"Gagal mengunduh file: {e}"

async def start_track(item_id: str, user: dict, track_meta: dict | None, upload=True, filepath=None, disable_link=False):
    async with BEATSOURCE_SEMAPHORE:
        await asyncio.sleep(random.uniform(0.2, 0.5))
        user_id = user.get('user_id')
        client = beatsource_manager.get_client(user_id)
        user_proxy = client.proxy if client else None
        
        if not track_meta:
            try: track_meta = await process_track_metadata(item_id, user['r_id'], user, fetch_stream=True)
            except Exception as e:
                LOGGER.warning(f"Beatsource track {item_id} error: {e}")
                return False
            filepath = f"{Config.DOWNLOAD_BASE_DIR}/{user['r_id']}/{track_meta['provider']}/{track_meta['albumartist']}/{track_meta['album']}"
            filepath = sanitize_filepath(filepath)

        if not track_meta.get('download_url'):
            new_url, qual = await refresh_track_url(item_id, track_meta, user_id)
            if new_url:
                track_meta['download_url'] = new_url
                if qual:
                    track_meta['quality'] = qual.capitalize()
                    track_meta['extension'] = 'flac' if qual == 'lossless' else 'm4a'
            else: return False

        track_meta['folderpath'] = filepath
        raw_filename = await format_string(Config.TRACK_NAME_FORMAT, track_meta, user)
        filepath += f"/{sanitize_filepath(raw_filename)}.{track_meta['extension']}"
        track_meta['filepath'] = filepath

        err = await download_beatsource_track(track_meta['download_url'], filepath, proxy=user_proxy)
        if err == "403_FORBIDDEN":
            new_url, qual = await refresh_track_url(item_id, track_meta, user_id)
            if new_url:
                track_meta['download_url'] = new_url
                if qual and qual != track_meta['quality'].lower():
                     new_ext = 'flac' if qual == 'lossless' else 'm4a'
                     if new_ext != track_meta['extension']:
                         filepath = filepath.rsplit('.', 1)[0] + f".{new_ext}"
                         track_meta['filepath'] = filepath
                         track_meta['extension'] = new_ext
                err = await download_beatsource_track(new_url, filepath, proxy=user_proxy)

        if err: return False

        try:
            await set_metadata(track_meta, user['user_id'])
            await write_extended_tags(track_meta['filepath'], track_meta)
        except:
            try: os.remove(filepath)
            except: pass
            return False

        if upload: await track_upload(track_meta, user, disable_link)
        return True

async def start_album(album_id: str, user: dict, upload=True):
    try: album_meta = await process_album_metadata(album_id, user['r_id'], user)
    except Exception as e: raise Exception(f"Gagal metadata album Beatsource: {e}")

    album_folder = sanitize_filepath(f"{Config.DOWNLOAD_BASE_DIR}/{user['r_id']}/{album_meta['provider']}/{album_meta['artist']}/{album_meta['title']}")
    album_meta['folderpath'] = album_folder

    if upload: album_meta['poster_msg'] = await post_art_poster(user, album_meta)

    successful_tracks = []
    await update_progress_msg(user['bot_msg'], 0, len(album_meta['tracks']), album_meta['title'], album_meta['type'])

    for i, track in enumerate(album_meta['tracks']):
        # [ANTI-BAN] Jeda Antar Lagu dalam Album (Penting!)
        if i > 0:
            await asyncio.sleep(random.uniform(1.0, 2.0)) 

        success = await start_track(track['itemid'], user, track, False, album_folder)
        if success: successful_tracks.append(track)
        await update_progress_msg(user['bot_msg'], i + 1, len(album_meta['tracks']), album_meta['title'], album_meta['type'])

    album_meta['tracks'] = successful_tracks
    album_meta['totaltracks'] = len(successful_tracks)
    if not successful_tracks: raise Exception("Tidak ada lagu yang berhasil diunduh.")

    playlist_zip, album_zip, artist_zip, art_poster = fetch_zip_settings(user)
    if album_zip: 
        await edit_message(user['bot_msg'], f"Menyiapkan ZIP...")
        try:
            cover_src = album_meta.get('cover')
            if cover_src:
                target = os.path.join(album_meta['folderpath'], "cover.jpg")
                if os.path.exists(cover_src): shutil.copy(cover_src, target)
                elif cover_src.startswith('http'):
                    u_proxy = beatsource_manager.get_client(user.get('user_id')).proxy
                    await download_beatsource_track(cover_src, target, proxy=u_proxy)
        except: pass
        album_meta['zip_path'] = await zip_handler(album_meta['folderpath'])

    if upload: 
        await edit_message(user['bot_msg'], lang.s.UPLOADING)
        await album_upload(album_meta, user)

async def start_playlist(playlist_id: str, user: dict, extra: dict, upload=True):
    try: play_meta = await process_playlist_metadata(playlist_id, user['r_id'], user, extra)
    except Exception as e: raise Exception(f"Gagal metadata playlist Beatsource: {e}")

    playlist_folder = sanitize_filepath(f"{Config.DOWNLOAD_BASE_DIR}/{user['r_id']}/{play_meta['provider']}/{play_meta['title']}")
    play_meta['folderpath'] = playlist_folder

    if upload: play_meta['poster_msg'] = await post_art_poster(user, play_meta)

    successful_tracks = []
    await update_progress_msg(user['bot_msg'], 0, len(play_meta['tracks']), play_meta['title'], play_meta['type'])

    for i, track in enumerate(play_meta['tracks']):
        # [ANTI-BAN] Jeda Antar Lagu dalam Playlist
        if i > 0:
            await asyncio.sleep(random.uniform(1.0, 2.0))

        success = await start_track(track['itemid'], user, track, False, playlist_folder)
        if success: successful_tracks.append(track)
        await update_progress_msg(user['bot_msg'], i + 1, len(play_meta['tracks']), play_meta['title'], play_meta['type'])

    play_meta['tracks'] = successful_tracks
    play_meta['totaltracks'] = len(successful_tracks)
    if not successful_tracks: raise Exception("Tidak ada lagu yang berhasil diunduh.")

    playlist_zip, album_zip, artist_zip, art_poster = fetch_zip_settings(user)
    if playlist_zip: 
        await edit_message(user['bot_msg'], f"Menyiapkan ZIP...")
        try:
            cover_src = play_meta.get('cover')
            if cover_src:
                target = os.path.join(play_meta['folderpath'], "cover.jpg")
                if os.path.exists(cover_src): shutil.copy(cover_src, target)
                elif cover_src.startswith('http'):
                    u_proxy = beatsource_manager.get_client(user.get('user_id')).proxy
                    await download_beatsource_track(cover_src, target, proxy=u_proxy)
        except: pass
        play_meta['zip_path'] = await zip_handler(play_meta['folderpath'])

    if upload: 
        await edit_message(user['bot_msg'], lang.s.UPLOADING)
        await playlist_upload(play_meta, user)
