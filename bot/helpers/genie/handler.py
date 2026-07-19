# [FILE: bot/helpers/genie/handler.py]

import os
import re
import asyncio
import aiohttp
import requests
import json
from urllib.parse import unquote

from config import Config
from bot.logger import LOGGER
from bot.settings import bot_set
from bot.helpers.message import send_message, edit_message
from bot.helpers.utils import format_string, post_art_poster, run_concurrent_tasks
from bot.helpers.aria2_helper import aria2_download
from bot.helpers.uploder import track_upload, album_upload, playlist_upload
from bot.helpers.metadata import set_metadata, create_cover_file 

from .manager import genie_manager

HEADERS = {
    'sec-ch-ua': '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
}

# --- [FIX ID TOMBOL MP3 192] ---
QUALITY_MAP = {
    "flac24": ["24bit", "16bit", "320", "192"],
    "flac16": ["16bit", "24bit", "320", "192"],
    "mp3": ["320", "192", "24bit", "16bit"],
    "mp3192": ["192", "320", "24bit", "16bit"] # Diubah tanpa underscore
}

QUALITY_MAP_DISPLAY = {
    "flac24": "FLAC-24",
    "flac16": "FLAC-16",
    "mp3": "MP3 320kbps",
    "mp3192": "MP3 192kbps" # Diubah tanpa underscore
}
# -------------------------------

async def fetch_json(session: aiohttp.ClientSession, url: str, max_retries=3):
    wait_time = 2
    loop = asyncio.get_event_loop()
    
    PROXY_STRING = getattr(Config, 'GENIE_PROXY', None) 
    proxy_dict = {
        "http": PROXY_STRING,
        "https": PROXY_STRING
    } if PROXY_STRING else None
    
    for attempt in range(max_retries):
        try:
            def _do_request():
                resp = requests.get(url, headers=HEADERS, timeout=15, proxies=proxy_dict)
                text = resp.text.strip()
                
                if resp.status_code in [400, 404]:
                    return {"FATAL_ERROR": resp.status_code}
                    
                if resp.status_code != 200:
                    raise ValueError(f"HTTP {resp.status_code}")
                    
                if not text.startswith('{') and not text.startswith('['):
                    raise ValueError(f"Diblokir oleh Genie (Respons bukan JSON): {text[:100]}")
                    
                return json.loads(text)
            
            result = await loop.run_in_executor(None, _do_request)
            if isinstance(result, dict) and "FATAL_ERROR" in result:
                raise ValueError(f"HTTP {result['FATAL_ERROR']}")
                
            return result
            
        except Exception as e:
            if "HTTP 404" in str(e) or "HTTP 400" in str(e):
                raise e 
            LOGGER.warning(f"Genie Request failed: {e}. Retrying... ({attempt + 1}/{max_retries})")
        
        await asyncio.sleep(wait_time)
        wait_time *= 2
        
    raise Exception("Max retries exceeded. Gagal memuat data dari Genie.")

def parse_code(url: str) -> str:
    match = re.findall(r'\d+', url)
    if match:
        return match[0]
    raise ValueError("Invalid URL Genie")

async def process_track(session, track_id, quality_pref, download_dir, details, extra_meta=None):
    if extra_meta is None: extra_meta = {}
    
    bitrates_to_try = QUALITY_MAP.get(quality_pref, ["24bit"])
    
    data = None
    for bitrate in bitrates_to_try:
        api_url = f"https://stm.genie.co.kr/player/j_StmInfo.json?uxtk=3173869132189.992&sign=Y&lpr=&svc=IV&bitrate={bitrate}&stk=Q3RoM0lJWUErZFdZVE1mREN2bi85UT09&itn=Y&dcd=ANDROID_ID%3A06568f5097d60690&xgnm={track_id}&uip=172.16.2.15&dvm=oppo%20r9tm&apvn=40607&ovn=5.1.1&unm=322011981&mts=Y"
        try:
            data = await fetch_json(session, api_url)
            if data and "DataSet" in data and len(data["DataSet"]["DATA"]) > 0:
                break 
        except Exception as e:
            if "HTTP 404" in str(e) or "HTTP 400" in str(e):
                LOGGER.debug(f"Kualitas {bitrate} ditolak API Genie (404/400). Mencoba fallback...")
                continue
            else:
                raise e
                
    if not data or "DataSet" not in data or len(data["DataSet"]["DATA"]) == 0:
        raise Exception("Gagal mendapatkan data streaming untuk track ini (semua parameter bitrate ditolak/404).")

    track_data = data["DataSet"]["DATA"][0]
    stream_url = unquote(track_data["STREAMING_MP3_URL"])
    title = unquote(track_data.get("SONG_TTS", f"Track_{track_id}"))
    artist = unquote(track_data.get("ARTIST_NAME", "Unknown Artist"))
    
    album_name = extra_meta.get('album')
    if not album_name:
        album_name = unquote(track_data.get("ALBUM_NM") or track_data.get("ALBUM_NAME") or track_data.get("ALBUM_TTS") or "")
    
    track_date = str(track_data.get('ALBUM_RELEASE_DT') or track_data.get('ALBUM_DATE') or track_data.get('RELEASE_DATE') or track_data.get('RECORD_DATE') or "")
    if len(track_date) == 8 and track_date.isdigit():
        track_date = f"{track_date[:4]}-{track_date[4:6]}-{track_date[6:]}"
        
    track_pub = unquote(track_data.get('PUBLISHER_NM') or track_data.get('AGENCY_NM') or track_data.get('COPYRIGHT') or "")
    
    final_date = extra_meta.get('date', '')
    if not final_date or final_date.lower() == 'unknown':
        final_date = track_date
        
    final_pub = extra_meta.get('copyright', '')
    if not final_pub or final_pub.lower() == 'unknown label':
        final_pub = track_pub

    cover_url = extra_meta.get('cover')
    raw_cover = ""
    if not cover_url:
        raw_cover = unquote(track_data.get("ALBUM_IMG_PATH600") or track_data.get("ALBUM_IMG_PATH") or "")
        
    album_id = track_data.get("ALBUM_ID")
    if album_id and (not raw_cover or not album_name or album_name == "Unknown Album"):
        try:
            alb_api = f"https://info.genie.co.kr/info/album?axnm={album_id}"
            alb_data = await fetch_json(session, alb_api)
            alb_info = alb_data.get('album_info', {})
            
            if not raw_cover:
                raw_cover = unquote(alb_info.get("album_img_path600", ""))
            if not album_name or album_name == "Unknown Album":
                album_name = unquote(alb_info.get("album_name", "Unknown Album"))
        except Exception as e:
            LOGGER.debug(f"Pencarian paksa data album gagal: {e}")
            
    if raw_cover and not cover_url:
        cover_url = "https:" + raw_cover if raw_cover.startswith("//") else raw_cover
        
    if not album_name:
        album_name = "Unknown Album"

    ext = "flac" if ".flac" in stream_url.lower() else "mp3"
    track_num_str = str(extra_meta.get('tracknumber', '')).zfill(2)
    
    if track_num_str and track_num_str != "00":
        filename = f"{track_num_str} - {artist} - {title}.{ext}".replace("/", "_")
    else:
        filename = f"{artist} - {title}.{ext}".replace("/", "_")
        
    filepath = os.path.join(download_dir, filename)

    metadata = {
        'title': title,
        'artist': artist,
        'album': album_name,
        'albumartist': extra_meta.get('albumartist', artist),
        'provider': 'Genie',
        'quality': QUALITY_MAP_DISPLAY.get(quality_pref, quality_pref.upper()),
        'filepath': filepath,
        'tempfolder': download_dir,
        'type': 'track',
        'extension': ext,
        'tracknumber': extra_meta.get('tracknumber', '1'),
        'totaltracks': extra_meta.get('totaltracks', '1'),
        'discnumber': extra_meta.get('discnumber', '1'),
        'totalvolume': extra_meta.get('totalvolume', '1'),
        'date': final_date,
        'copyright': final_pub,
        'cover': cover_url,
        # --- [FIX KEYERROR METADATA] ---
        'isrc': '', # Memuaskan metadata.py yang mencari ISRC
        'upc': ''   # Memuaskan metadata.py yang mencari UPC
        # -------------------------------
    }

    if cover_url and str(cover_url).startswith('http'):
        try:
            local_cover_path = await create_cover_file(cover_url, metadata, thumbnail=False)
            if local_cover_path and local_cover_path != './project-siesta.png' and os.path.exists(local_cover_path):
                metadata['cover'] = local_cover_path
        except Exception as e:
            LOGGER.warning(f"Gagal pra-unduh cover Genie: {e}")

    aria_details = details.copy() if details else {}
    aria_details['headers'] = HEADERS

    success = await aria2_download(stream_url, filepath, aria_details)
    if not success:
        raise Exception("Gagal mengunduh file melalui Aria2c.")

    actual_quality = QUALITY_MAP_DISPLAY.get(quality_pref, quality_pref.upper())
    if ext == "mp3":
        try:
            from mutagen.mp3 import MP3
            audio = MP3(filepath)
            bps = int(audio.info.bitrate / 1000)
            actual_quality = f"MP3 {bps}kbps"
        except Exception:
            actual_quality = "MP3 320kbps" if quality_pref == 'mp3' else "MP3 192kbps"
    elif ext == "flac":
        try:
            from mutagen.flac import FLAC
            audio = FLAC(filepath)
            bps = audio.info.bits_per_sample
            actual_quality = f"FLAC-{bps}"
        except Exception:
            pass
            
    metadata['quality'] = actual_quality
        
    try:
        await set_metadata(metadata, extra_meta.get('user_id', 0))
    except Exception as e:
        LOGGER.warning(f"Gagal menulis metadata ke file {filename}: {e}")
        
    return metadata

async def start_genie(link: str, user: dict):
    user_id = user['user_id']
    quality_pref = genie_manager.get_user_quality(user_id)
    download_dir = os.path.join(Config.DOWNLOAD_BASE_DIR, str(user['r_id']))
    os.makedirs(download_dir, exist_ok=True)
    
    try:
        code = parse_code(link)
    except ValueError as e:
        raise Exception(str(e))

    details = None
    if 'bot_msg' in user:
        import hashlib
        cancel_id = hashlib.md5(str(user['bot_msg'].id).encode()).hexdigest()[:16]
        details = {
            'msg': user['bot_msg'],
            'action': 'Download',
            'machine': 'Aria2c',
            'task_id': cancel_id
        }

    async with aiohttp.ClientSession() as session:
        if "xgnm" in link:
            if 'bot_msg' in user:
                await edit_message(user['bot_msg'], "🚀 Starting task...")
            
            extra_base = {'user_id': user_id}
            metadata = await process_track(session, code, quality_pref, download_dir, details, extra_base)
            await track_upload(metadata, user)

        elif "axnm" in link:
            if 'bot_msg' in user:
                await edit_message(user['bot_msg'], "🚀 Starting task...")
                
            api_url = f"https://info.genie.co.kr/info/album?axnm={code}"
            album_data = await fetch_json(session, api_url)
            
            album_name = unquote(album_data['album_info']['album_name'])
            album_artist = unquote(album_data['album_info']['artist_name'])
            
            album_dir_name = f"{album_artist} - {album_name}".replace("/", "_")
            album_dir = os.path.join(download_dir, album_dir_name)
            os.makedirs(album_dir, exist_ok=True)
            
            cover_url = unquote(album_data['album_info'].get("album_img_path600", ""))
            if cover_url.startswith("//"):
                cover_url = "https:" + cover_url
            
            cover_path = os.path.join(album_dir, "cover.jpg")
            if cover_url:
                try:
                    async with session.get(cover_url, headers=HEADERS) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            with open(cover_path, 'wb') as f:
                                f.write(content)
                except Exception as e:
                    LOGGER.warning(f"Gagal mengunduh cover Genie: {e}")

            album_info_dict = album_data.get('album_info', {})
            raw_date = str(album_info_dict.get('album_release_dt') or album_info_dict.get('album_date') or album_info_dict.get('record_date') or "")
            raw_date = raw_date.replace('.', '-').replace('/', '-')
            if len(raw_date) == 8 and raw_date.replace('-', '').isdigit(): 
                rd = raw_date.replace('-', '')
                release_date = f"{rd[:4]}-{rd[4:6]}-{rd[6:]}"
            else:
                release_date = raw_date
                
            publisher = unquote(album_info_dict.get('publisher_name') or album_info_dict.get('publisher_nm') or album_info_dict.get('agency_name') or "")
            
            song_list = album_data.get('album_song_list', [])
            max_cd = 1
            for s in song_list:
                cd_no = str(s.get('album_cd', '1'))
                if cd_no.isdigit() and int(cd_no) > max_cd:
                    max_cd = int(cd_no)
            
            album_metadata = {
                'title': album_name,
                'artist': album_artist,
                'provider': 'Genie',
                'quality': QUALITY_MAP_DISPLAY.get(quality_pref, quality_pref.upper()),
                'type': 'album',
                'folderpath': album_dir,
                'tracks': [], 
                'cover': cover_path if os.path.exists(cover_path) else None,
                'release_date': release_date, 
                'date': release_date,
                'totalvolumes': str(max_cd),   
                'totalvolume': str(max_cd),    
                'explicit': 'False'           
            }
            
            album_metadata['poster_msg'] = await post_art_poster(user, album_metadata)

            extra_meta_base = {
                'user_id': user_id,
                'album': album_name, 
                'albumartist': album_artist,
                'totaltracks': str(len(song_list)),
                'totalvolume': str(max_cd),
                'date': release_date,
                'copyright': publisher,
                'cover': cover_path if os.path.exists(cover_path) else None 
            }
            
            tasks = []
            for index, song in enumerate(song_list, start=1):
                track_id = song['song_id']
                
                cur_extra = extra_meta_base.copy()
                cur_extra['tracknumber'] = str(song.get('track_no', index))
                cur_extra['discnumber'] = str(song.get('album_cd', '1'))
                
                tasks.append(process_track(session, track_id, quality_pref, album_dir, None, cur_extra))

            update_details = {
                'text': "Downloading...",
                'msg': user['bot_msg'],
                'title': album_name,
                'type': 'Album',
                'action': 'Download' 
            }
            
            task_results = await run_concurrent_tasks(tasks, update_details, limit=4)
            # Only keep results that are actual dictionaries (metadata), ignoring Exceptions
            successful_tracks = [res for res in task_results if isinstance(res, dict)]

            if not successful_tracks:
                raise Exception("Semua lagu dalam album gagal diunduh.")

            qualities_found = [t.get('quality', '') for t in successful_tracks]
            if any("FLAC-24" in q for q in qualities_found):
                album_metadata['quality'] = "FLAC-24"
            elif any("FLAC-16" in q for q in qualities_found):
                album_metadata['quality'] = "FLAC-16"
            elif any("320kbps" in q for q in qualities_found):
                album_metadata['quality'] = "MP3 320kbps"
            elif any("192kbps" in q for q in qualities_found):
                album_metadata['quality'] = "MP3 192kbps"
            else:
                album_metadata['quality'] = successful_tracks[0].get('quality', album_metadata['quality'])

            album_metadata['tracks'] = successful_tracks
            album_metadata['totaltracks'] = len(successful_tracks)
            
            await album_upload(album_metadata, user)

        elif "plmSeq" in link:
            if 'bot_msg' in user:
                await edit_message(user['bot_msg'], "🔍 **Fetching Genie Playlist...**")
                
            api_url = f"https://app.genie.co.kr/Iv3/playlist/infosong.json?seq={code}"
            pl_data = await fetch_json(session, api_url)
            
            pl_title = unquote(pl_data['DATASET']['DATA_INFO']['DATA']['PLM_TITLE'])
            pl_dir_name = f"Genie - {pl_title}".replace("/", "_")
            pl_dir = os.path.join(download_dir, pl_dir_name)
            os.makedirs(pl_dir, exist_ok=True)
            
            pl_metadata = {
                'title': pl_title,
                'provider': 'Genie',
                'quality': QUALITY_MAP_DISPLAY.get(quality_pref, quality_pref.upper()),
                'type': 'playlist',
                'folderpath': pl_dir,
                'tracks': [],
                'release_date': '',
                'date': '',
                'totalvolumes': '1',
                'totalvolume': '1',
                'explicit': 'False'
            }
            
            pl_metadata['poster_msg'] = await post_art_poster(user, pl_metadata)
            
            song_list = pl_data['DATASET']['DATA_SONG']['DATA']
            tasks = []
            for index, song in enumerate(song_list, start=1):
                track_id = unquote(song['SONG_ID'])
                cur_extra = {
                    'user_id': user_id,
                    'tracknumber': str(index),
                    'totaltracks': str(len(song_list)),
                    'date': ''
                }
                tasks.append(process_track(session, track_id, quality_pref, pl_dir, None, cur_extra))

            update_details = {
                'text': "Downloading...",
                'msg': user['bot_msg'],
                'title': pl_title,
                'type': 'Playlist',
                'action': 'Download' 
            }

            task_results = await run_concurrent_tasks(tasks, update_details, limit=4)
            # Only keep results that are actual dictionaries (metadata), ignoring Exceptions
            successful_tracks = [res for res in task_results if isinstance(res, dict)]

            if not successful_tracks:
                raise Exception("Semua lagu dalam playlist gagal diunduh.")

            qualities_found = [t.get('quality', '') for t in successful_tracks]
            if any("FLAC-24" in q for q in qualities_found):
                pl_metadata['quality'] = "FLAC-24"
            elif any("FLAC-16" in q for q in qualities_found):
                pl_metadata['quality'] = "FLAC-16"
            elif any("320kbps" in q for q in qualities_found):
                pl_metadata['quality'] = "MP3 320kbps"
            elif any("192kbps" in q for q in qualities_found):
                pl_metadata['quality'] = "MP3 192kbps"
            else:
                pl_metadata['quality'] = successful_tracks[0].get('quality', pl_metadata['quality'])

            pl_metadata['tracks'] = successful_tracks
            pl_metadata['totaltracks'] = len(successful_tracks)
            
            from bot.helpers.uploder import playlist_upload
            await playlist_upload(pl_metadata, user)

        elif "xxnm" in link:
            raise NotImplementedError("Fitur unduhan Artist Batch untuk Genie belum diterapkan. Harap unduh per-Album.")
        else:
            raise Exception("URL Genie tidak valid atau tidak didukung.")
