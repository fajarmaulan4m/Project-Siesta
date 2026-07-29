# [GANTI FILE: bot/helpers/qobuz/utils.py]

import re
import copy
import bot.helpers.translations as lang
import logging
import aiohttp
import urllib.parse
import os
from datetime import datetime
from config import Config

from ..message import send_message, edit_message
from ..utils import format_string
from ..metadata import metadata as base_meta
from ..metadata import create_cover_file

from bot.settings import bot_set

try:
    from .handler import QobuzContentUnavailableError
except ImportError:
    class QobuzContentUnavailableError(Exception):
        pass

FALLBACK_IMAGE_PATH = os.path.join(Config.WORK_DIR, "project-siesta.png")


async def get_itunes_cover_url(metadata: dict, session: aiohttp.ClientSession) -> str | None:
    try:
        if metadata.get('upc') and metadata['upc'] != "0":
            upc_url = f"https://itunes.apple.com/lookup?upc={metadata['upc']}&entity=album&limit=1"
            async with session.get(upc_url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get('resultCount', 0) > 0:
                        artwork_url = data['results'][0].get('artworkUrl100')
                        if artwork_url:
                            return artwork_url.replace('100x100bb.jpg', '10000x10000bb.jpg')
        if metadata.get('albumartist') and metadata.get('album'):
            search_term = urllib.parse.quote(f"{metadata['albumartist']} {metadata['album']}")
            search_url = f"https://itunes.apple.com/search?term={search_term}&entity=album&media=music&limit=5"
            async with session.get(search_url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get('resultCount', 0) > 0:
                        for result in data['results']:
                            itunes_album = result.get('collectionName', '').lower()
                            itunes_artist = result.get('artistName', '').lower()
                            local_album = metadata['album'].lower()
                            local_artist = metadata['albumartist'].lower()
                            if (local_album in itunes_album or itunes_album in local_album) and \
                               (local_artist in itunes_artist):
                                artwork_url = result.get('artworkUrl100')
                                if artwork_url:
                                    return artwork_url.replace('100x100bb.jpg', '10000x10000bb.jpg')
    except Exception as e:
        logging.warning(f"Pencarian sampul iTunes gagal untuk UPC {metadata.get('upc')}: {e}")
        return None
    return None


async def get_musicbrainz_cover_url(metadata: dict, session: aiohttp.ClientSession) -> str | None:
    try:
        # Cari berdasarkan UPC/Barcode terlebih dahulu
        if metadata.get('upc') and metadata['upc'] != "0":
            mb_url = f"https://musicbrainz.org/ws/2/release?query=barcode:{metadata['upc']}&fmt=json"
            async with session.get(mb_url, headers={'User-Agent': 'MusicBot/1.0 ( mybot@example.com )'}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('releases') and len(data['releases']) > 0:
                        release_id = data['releases'][0]['id']
                        
                        # Ambil gambar dari CoverArtArchive
                        caa_api = f"https://coverartarchive.org/release/{release_id}"
                        async with session.get(caa_api) as caa_resp:
                            if caa_resp.status == 200:
                                caa_data = await caa_resp.json()
                                if caa_data.get('images') and len(caa_data['images']) > 0:
                                    # Cari gambar 'Front' (Bagian depan)
                                    for img in caa_data['images']:
                                        if img.get('front'):
                                            return img['image']
                                    # Jika tidak ada label 'front', ambil yang pertama
                                    return caa_data['images'][0]['image']
    except Exception as e:
        logging.warning(f"Pencarian sampul MusicBrainz gagal untuk UPC {metadata.get('upc')}: {e}")
        return None
    return None


def get_credits(meta, keys, roles=None):
    """
    Mengambil daftar nama dari berbagai sumber secara agresif.
    """
    names = []
    target_roles = [r.lower() for r in roles] if roles else []
    
    # 1. Parsing String 'performers'
    if roles and meta.get('performers'):
        try:
            raw_perf = meta['performers'].replace('\r', '').replace('\n', '')
            chunks = raw_perf.split(' - ')
            for chunk in chunks:
                chunk_lower = chunk.lower()
                if any(tr in chunk_lower for tr in target_roles):
                    parts = chunk.split(',')
                    if len(parts) > 0:
                        potential_name = parts[0].strip()
                        if potential_name.lower() not in target_roles:
                            names.append(potential_name)
        except Exception as e:
            logging.warning(f"Gagal parsing performers: {e}")

    # 2. Cek 'contributors' list
    if roles and 'contributors' in meta and isinstance(meta['contributors'], list):
        for item in meta['contributors']:
            item_role = item.get('role', '').lower()
            if any(tr in item_role for tr in target_roles):
                if item.get('name'):
                    names.append(item['name'])

    # 3. Cek kunci langsung
    for key in keys:
        if key in meta:
            val = meta[key]
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict) and item.get('name'):
                        names.append(item['name'])
                    elif isinstance(item, str):
                        names.append(item)
            elif isinstance(val, dict):
                if val.get('name'):
                    names.append(val['name'])
            elif isinstance(val, str):
                names.append(val)

    seen = set()
    unique_names = [x for x in names if not (x in seen or seen.add(x))]
    
    return ', '.join(unique_names)


async def get_track_metadata(item_id, r_id, q_meta=None, user: dict=None):
    if user is None:
        logging.error("User dict None in get_track_metadata! Multi-login will fail.")
        return None, "Error: User data tidak ditemukan."
    
    client = user['qobuz_api'] 

    if q_meta is None:
        raw_meta = await client.get_track_url(item_id, user)
        if "sample" not in raw_meta and raw_meta.get('sampling_rate'):
            q_meta = await client.get_track_meta(item_id)
            if not q_meta.get('streamable'):
                return None, "UNAVAILABLE" 
        else:
            return None, "UNAVAILABLE"
    
    metadata = copy.deepcopy(base_meta)

    # --- Waktu Tagging ---
    now = datetime.now()
    tag_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
    utc_time_str = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    metadata['tempfolder'] += f"{r_id}-temp/"
    metadata['itemid'] = item_id
    
    # --- Standard Tags ---
    metadata['title'] = q_meta['title']
    if q_meta['version']:
        metadata['title'] += f' ({q_meta["version"]})'
        
    metadata['album'] = q_meta['album']['title']
    metadata['albumartist'] = q_meta['album']['artist']['name']
    metadata['artist'] = await get_artists_name(q_meta['album'])
    
    metadata['copyright'] = q_meta.get('copyright', '')
    metadata['cpr'] = q_meta.get('copyright', '')
    
    label_name = q_meta['album'].get('label', {}).get('name', '')
    metadata['label'] = label_name
    metadata['publisher'] = label_name
    metadata['pub'] = label_name
    
    upc_code = q_meta['album'].get('upc', '')
    metadata['upc'] = upc_code
    metadata['ean'] = upc_code
    metadata['barcode'] = upc_code
    metadata['isrc'] = q_meta.get('isrc', '')
    
    # --- DATES ---
    rel_date = q_meta.get('release_date_original', '')
    metadata['date'] = rel_date
    
    # PERBAIKAN: creation_time = Waktu Sekarang (UTC ISO Format)
    metadata['creation_time'] = utc_time_str
    
    # Release Time & Original Date tetap tanggal rilis asli
    metadata['releasetime'] = rel_date
    metadata['RELEASETIME'] = rel_date
    
    metadata['originaldate'] = rel_date
    metadata['ORIGINALDATE'] = rel_date
    
    metadata['tagging_time'] = tag_time_str
    metadata['date_tagged'] = tag_time_str
    metadata['encoded_date'] = utc_time_str

    metadata['duration'] = q_meta['duration']
    
    # --- NUMBERING FORMAT ---
    metadata['tracknumber'] = str(q_meta['track_number']).zfill(2)
    metadata['totaltracks'] = str(q_meta['album']['tracks_count'])
    metadata['volume'] = str(q_meta.get('media_number', '1'))
    metadata['totalvolume'] = str(q_meta['album'].get('media_count', '1'))
    
    if q_meta.get('album') and q_meta['album'].get('genre'):
         metadata['genre'] = q_meta['album']['genre'].get('name', '')
    
    is_explicit = q_meta.get('parental_warning', False)
    metadata['explicit'] = is_explicit
    metadata['itunesadvisory'] = '1' if is_explicit else '0'
    
    # --- CREDITS ---
    metadata['composer'] = get_credits(
        q_meta, 
        ['composers', 'composer'], 
        ['composer', 'writer', 'author']
    )
    metadata['lyricist'] = get_credits(
        q_meta, 
        ['lyricist', 'writer'], 
        ['lyricist', 'writer', 'author']
    )
    metadata['writer'] = metadata['lyricist']
    metadata['producer'] = get_credits(q_meta, ['producer'], ['producer'])

    if q_meta.get('performer'):
        metadata['performer'] = q_meta['performer'].get('name', '')

    metadata['provider'] = 'Qobuz'
    metadata['type'] = 'track'

    metadata['bps'] = str(q_meta.get('bit_depth', ''))
    metadata['sample_rate'] = str(q_meta.get('sampling_rate', ''))

    # === Penentuan Sumber Cover ===
    # Ambil user_id dengan aman
    u_id = user.get('user_id', 0)
    
    # Ambil preferensi pengguna, default ke 'itunes' agar tidak mengubah perilaku lama
    cover_source = bot_set.user_data.get(u_id, {}).get("qobuz_cover_source", "itunes")
    
    # URL Asli Qobuz
    if 'album' in q_meta and 'image' in q_meta['album']: # Logika untuk track
        qobuz_fallback_url = q_meta['album']['image'].get('original', q_meta['album']['image'].get('large'))
    else: # Logika untuk album
        qobuz_fallback_url = q_meta['image'].get('original', q_meta['image'].get('large'))

    cover_url = None
    
    try:
        async with aiohttp.ClientSession() as session:
            if cover_source == "itunes":
                logging.debug(f"Mencari sampul di iTunes untuk {metadata['album']}...")
                cover_url = await get_itunes_cover_url(metadata, session)
            elif cover_source == "musicbrainz":
                logging.debug(f"Mencari sampul di MusicBrainz untuk {metadata['album']}...")
                cover_url = await get_musicbrainz_cover_url(metadata, session)
            else: # cover_source == "original"
                logging.debug(f"Menggunakan sampul original Qobuz.")
                cover_url = qobuz_fallback_url
    except Exception as e:
        logging.warning(f"Sesi pencarian sampul ({cover_source}) gagal: {e}")

    # Fallback ke original Qobuz jika API lain gagal
    if not cover_url:
        logging.debug(f"Pencarian sampul gagal atau disetel ke original, menggunakan sampul Qobuz.")
        cover_url = qobuz_fallback_url
    # ===============================

    final_cover_path_or_url = cover_url
    if not cover_url:
        if os.path.exists(FALLBACK_IMAGE_PATH):
            logging.warning(f"Semua sumber online gagal, menggunakan fallback lokal: {FALLBACK_IMAGE_PATH}")
            final_cover_path_or_url = FALLBACK_IMAGE_PATH
        else:
            logging.error(f"SEMUA SUMBER GAGAL, dan fallback lokal TIDAK DITEMUKAN di {FALLBACK_IMAGE_PATH}")
    
    metadata['cover'] = await create_cover_file(final_cover_path_or_url, metadata)
    metadata['thumbnail'] = await create_cover_file(final_cover_path_or_url, metadata, True)

    return metadata, None
  
async def get_album_metadata(item_id, r_id, user: dict):
    client = user['qobuz_api'] 
    q_meta = await client.get_album_meta(item_id)
    
    if not q_meta.get('streamable'):
        return None, "UNAVAILABLE"
    
    metadata = copy.deepcopy(base_meta)

    now = datetime.now()
    tag_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
    utc_time_str = now.strftime('%Y-%m-%dT%H:%M:%SZ') # Ditambahkan untuk creation_time album
    
    metadata['tempfolder'] += f"{r_id}-temp/"
    metadata['itemid'] = item_id
    
    metadata['title'] = q_meta['title']
    if q_meta.get('version'):
        metadata['title'] += f" ({q_meta['version']})"
        
    metadata['album'] = metadata['title']
    metadata['albumartist'] = q_meta['artist']['name']
    metadata['artist'] = q_meta['artist']['name']
    
    metadata['upc'] = q_meta.get('upc', '')
    metadata['ean'] = q_meta.get('upc', '')
    metadata['barcode'] = q_meta.get('upc', '')
    
    label_name = q_meta.get('label', {}).get('name', '')
    metadata['label'] = label_name
    metadata['publisher'] = label_name
    metadata['pub'] = label_name
    
    metadata['copyright'] = q_meta.get('copyright', '')
    metadata['cpr'] = q_meta.get('copyright', '')

    # --- DATES ---
    rel_date = q_meta.get('release_date_original', '')
    metadata['date'] = rel_date
    
    # PERBAIKAN: creation_time = Waktu Sekarang
    metadata['creation_time'] = utc_time_str
    
    metadata['releasetime'] = rel_date
    metadata['RELEASETIME'] = rel_date
    
    metadata['originaldate'] = rel_date
    metadata['ORIGINALDATE'] = rel_date
    
    metadata['tagging_time'] = tag_time_str
    metadata['date_tagged'] = tag_time_str
    
    # --- NUMBERING FORMAT ---
    metadata['totaltracks'] = str(q_meta['tracks_count'])
    metadata['totalvolume'] = str(q_meta.get('media_count', '1'))

    metadata['duration'] = q_meta['duration']
    
    metadata['genre'] = q_meta['genre']['name']
    metadata['explicit'] = q_meta['parental_warning']
    metadata['itunesadvisory'] = '1' if q_meta['parental_warning'] else '0'
    
    metadata['provider'] = 'Qobuz'
    metadata['type'] = 'album'

    # === Penentuan Sumber Cover ===
    # Ambil user_id dengan aman
    u_id = user.get('user_id', 0)
    
    # Ambil preferensi pengguna, default ke 'itunes' agar tidak mengubah perilaku lama
    cover_source = bot_set.user_data.get(u_id, {}).get("qobuz_cover_source", "itunes")
    
    # URL Asli Qobuz
    if 'album' in q_meta and 'image' in q_meta['album']: # Logika untuk track
        qobuz_fallback_url = q_meta['album']['image'].get('original', q_meta['album']['image'].get('large'))
    else: # Logika untuk album
        qobuz_fallback_url = q_meta['image'].get('original', q_meta['image'].get('large'))

    cover_url = None
    
    try:
        async with aiohttp.ClientSession() as session:
            if cover_source == "itunes":
                logging.debug(f"Mencari sampul di iTunes untuk {metadata['album']}...")
                cover_url = await get_itunes_cover_url(metadata, session)
            elif cover_source == "musicbrainz":
                logging.debug(f"Mencari sampul di MusicBrainz untuk {metadata['album']}...")
                cover_url = await get_musicbrainz_cover_url(metadata, session)
            else: # cover_source == "original"
                logging.debug(f"Menggunakan sampul original Qobuz.")
                cover_url = qobuz_fallback_url
    except Exception as e:
        logging.warning(f"Sesi pencarian sampul ({cover_source}) gagal: {e}")

    # Fallback ke original Qobuz jika API lain gagal
    if not cover_url:
        logging.debug(f"Pencarian sampul gagal atau disetel ke original, menggunakan sampul Qobuz.")
        cover_url = qobuz_fallback_url
    # ===============================

    final_cover_path_or_url = cover_url
    if not cover_url:
        if os.path.exists(FALLBACK_IMAGE_PATH):
            logging.warning(f"Semua sumber online gagal, menggunakan fallback lokal: {FALLBACK_IMAGE_PATH}")
            final_cover_path_or_url = FALLBACK_IMAGE_PATH
        else:
            logging.error(f"SEMUA SUMBER GAGAL, dan fallback lokal TIDAK DITEMUKAN di {FALLBACK_IMAGE_PATH}")
    
    metadata['cover'] = await create_cover_file(final_cover_path_or_url, metadata)
    metadata['thumbnail'] = await create_cover_file(final_cover_path_or_url, metadata, True)

    # --- BOOKLET CHECK ---
    metadata['booklet_url'] = None
    if q_meta.get('goodies'):
        for goodie in q_meta['goodies']:
            # ID 21 adalah format untuk PDF Booklet di Qobuz
            if goodie.get('file_format_id') == 21 and goodie.get('url'):
                metadata['booklet_url'] = goodie['url']
                logging.info(f"Booklet ditemukan untuk album: {metadata['title']}")
                break
    # ---------------------

    metadata['tracks'] = await get_track_meta_from_alb(q_meta, metadata) 

    return metadata, None

async def get_track_meta_from_alb(q_meta:dict, alb_meta):
    tracks = []
    
    now = datetime.now()
    tag_time_str = now.strftime('%Y-%m-%d %H:%M:%S')
    utc_time_str = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    for track in q_meta['tracks']['items']:
        metadata = copy.deepcopy(alb_meta)
        metadata['itemid'] = track['id']
        metadata['title'] = track['title']
        if track['version']:
            metadata['title'] += f' ({track["version"]})'
        metadata['duration'] = track['duration']
        metadata['isrc'] = track['isrc']
        
        # --- NUMBERING FORMAT ---
        metadata['tracknumber'] = str(track['track_number']).zfill(2)
        metadata['volume'] = str(track.get('media_number', '1'))
        
        # --- CREDITS ---
        metadata['composer'] = get_credits(
            track, 
            ['composers', 'composer'], 
            ['composer', 'writer', 'author']
        )
        metadata['lyricist'] = get_credits(
            track, 
            ['lyricist', 'writer'], 
            ['lyricist', 'writer', 'author']
        )
        metadata['writer'] = metadata['lyricist']
        metadata['producer'] = get_credits(track, ['producer'], ['producer'])
            
        if track.get('performer'):
             metadata['performer'] = track['performer'].get('name', '')
             
        metadata['tagging_time'] = tag_time_str
        metadata['date_tagged'] = tag_time_str
        metadata['encoded_date'] = utc_time_str
        
        is_explicit_track = track.get('parental_warning', False)
        metadata['explicit'] = is_explicit_track
        metadata['itunesadvisory'] = '1' if is_explicit_track else '0'

        metadata['tracks'] = ''
        metadata['type'] = 'track'
        tracks.append(metadata)
    return tracks


async def get_playlist_meta(raw_meta, tracks, r_id, user: dict):
    metadata = copy.deepcopy(base_meta)

    metadata['tempfolder'] += f"{r_id}-temp/"

    metadata['title'] = raw_meta['name']
    metadata['duration'] = raw_meta['duration']
    
    metadata['totaltracks'] = str(raw_meta['tracks']['total'])
    metadata['itemid'] = raw_meta['id']
    
    metadata['type'] = 'playlist'
    metadata['provider'] = 'Qobuz'
    
    if os.path.exists(FALLBACK_IMAGE_PATH):
        metadata['cover'] = FALLBACK_IMAGE_PATH
        metadata['thumbnail'] = FALLBACK_IMAGE_PATH
    else:
        metadata['cover'] = None 
        metadata['thumbnail'] = None
    
    for track in tracks:
        track_meta, err = await get_track_metadata(track['id'], r_id, track, user=user)
        if err:
            if err == "UNAVAILABLE":
                raise QobuzContentUnavailableError(f"Track {track['id']} di playlist tidak tersedia.")
        metadata['tracks'].append(track_meta)
    return metadata

async def get_artist_meta(artist_raw):
    metadata = copy.deepcopy(base_meta)
    metadata['title'] = artist_raw['name']
    metadata['type'] = 'artist'
    metadata['provider'] = 'Qobuz'
    return metadata

async def get_artists_name(meta):
    artists = []
    try:
        for a in meta['artists']:
            artists.append(a['name'])
    except:
        artists.append(meta['artist']['name'])
    return ', '.join([str(artist) for artist in artists])


async def check_type(url, user: dict):
    client = user['qobuz_api'] 
    possibles = {
            "playlist": {
                "func": "get_plist_meta", 
                "iterable_key": "tracks",
                "multi_type": "tracks" 
            },
            "artist": {
                "func": "get_artist_meta", 
                "iterable_key": "albums",
                "multi_type": "albums" 
            },
            "interpreter": {
                "func": "get_artist_meta", 
                "iterable_key": "albums",
                "multi_type": "albums" 
            },
            "label": {
                "func": "get_label_meta", 
                "iterable_key": "albums",
                "multi_type": "albums" 
            },
            "album": {"album": True, "func": None, "iterable_key": None},
            "track": {"album": False, "func": None, "iterable_key": None},
        }
    try:
        url_info = await get_url_info(url)
        if url_info is None:
            raise TypeError
            
        url_type, item_id = url_info
        
        type_dict = possibles[url_type]
        
    except (KeyError, IndexError):
        raise Exception(f"URL tidak dapat dikenali: {url}")
    except TypeError:
        raise Exception(f"URL Qobuz tidak valid atau tidak dapat di-parse: {url}")
        
    content = None
    items = None
    if type_dict["func"]:
        method_to_call = getattr(client, type_dict["func"])
        content = []
        if type_dict["multi_type"]:
            if url_type == "playlist":
                epoint = "playlist/get"
                key = "total"
            elif url_type in ["artist", "label", "interpreter"]:
                epoint = f"{url_type}/get"
                key = "total"
            else:
                raise Exception("Tipe multi-meta tidak terdefinisi.")
            res_iterator = client.multi_meta(epoint, key, item_id, type_dict["multi_type"])
            async for data in res_iterator:
                content.append(data)
            if not content:
                raise QobuzContentUnavailableError(f"API Qobuz gagal mengembalikan data untuk {url_type}/{item_id}. Coba akun lain.")
        if content:
            smart_discography = True
            if smart_discography and url_type == "artist":
                items = smart_discography_filter(
                    content,
                    save_space=True,
                    skip_extras=True,
                )
            else:
                if type_dict["iterable_key"] not in content[0]:
                     raise QobuzContentUnavailableError(f"Respons Qobuz tidak memiliki '{type_dict['iterable_key']}'")
                if 'items' in content[0][type_dict["iterable_key"]]:
                    items = content[0][type_dict["iterable_key"]]['items']
                else:
                    raise QobuzContentUnavailableError(f"Playlist ID:{item_id} kosong atau tidak memiliki track.")
        return items, item_id, type_dict, content
    else:
        return None, item_id, type_dict, content


async def get_url_info(url):
    regex_pattern = (
        r"(?:https:\/\/(?:w{3}|open|play)\.qobuz\.com)?(?:\/[a-z]{2}-[a-z]{2})?"
        r"?\/(album|artist|track|playlist|label|interpreter)(?:\/[-\w\d]+)?\/([\w\d]+)"
    )
    r = re.search(regex_pattern, url)
    if r:
        return r.groups()

    logging.info(f"Qobuz URL tidak dikenali, mencoba scrape HTML untuk: {url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, allow_redirects=True, timeout=10) as response:
                final_url = str(response.url)
                if response.status != 200:
                    return None

                r_final = re.search(regex_pattern, final_url)
                if r_final:
                    return r_final.groups()

                html_content = await response.text()
                og_type_match = re.search(r'<meta\s+property="og:type"\s+content="music\.(album|artist|song|playlist)"', html_content)
                id_match = re.search(r"\/(\d+)(?:\?.*)?$", final_url.split('?')[0])
                
                if og_type_match and id_match:
                    og_type = og_type_match.group(1)
                    item_id = id_match.group(1)
                    if og_type == "song": og_type = "track"
                    elif og_type == "artist":
                        if "/interpreter/" in final_url: og_type = "interpreter"
                        else: og_type = "artist"
                    return (og_type, item_id)

                if "/interpreter/" in final_url and id_match:
                     return ("interpreter", id_match.group(1))
                return None
    except Exception as e:
        logging.error(f"Gagal meng-scrape HTML Qobuz: {e}")
        return None
    return None


def smart_discography_filter(contents: list, save_space: bool = False, skip_extras: bool = False) -> list:
    TYPE_REGEXES = {
        "remaster": r"(?i)(re)?master(ed)?",
        "extra": r"(?i)(anniversary|deluxe|live|collector|demo|expanded)",
    }
    def is_type(album_t: str, album: dict) -> bool:
        version = album.get("version", "")
        title = album.get("title", "")
        regex = TYPE_REGEXES[album_t]
        return re.search(regex, f"{title} {version}") is not None
    def essence(album: dict) -> str:
        r = re.match(r"([^\(]+)(?:\s*[\(\[][^\)][\)\]])*", album)
        if not r: return album.lower()
        return r.group(1).strip().lower()
    requested_artist = contents[0]['name']
    items = []
    for item in contents:
        items.extend(item['albums']['items'])
    title_grouped = dict()
    for item in items:
        title_ = essence(item["title"])
        if title_ not in title_grouped:
            title_grouped[title_] = []
        title_grouped[title_].append(item)
    items = []
    for albums in title_grouped.values():
        best_bit_depth = max(a["maximum_bit_depth"] for a in albums)
        get_best = min if save_space else max
        best_sampling_rate = get_best(
            a["maximum_sampling_rate"]
            for a in albums
            if a["maximum_bit_depth"] == best_bit_depth
        )
        remaster_exists = any(is_type("remaster", a) for a in albums)
        def is_valid(album: dict) -> bool:
            return (
                album["maximum_bit_depth"] == best_bit_depth
                and album["maximum_sampling_rate"] == best_sampling_rate
                and album["artist"]["name"] == requested_artist
                and not (
                    (remaster_exists and not is_type("remaster", album))
                    or (skip_extras and is_type("extra", album))
                )
            )
        filtered = tuple(filter(is_valid, albums))
        if len(filtered) >= 1:
            items.append(filtered[0])
    return items

    
async def get_quality(meta: dict, user: dict):
    client = user['qobuz_api'] 
    
    try:
        u_id = int(user.get("user_id", 0))
    except:
        u_id = 0
        
    quality = client.quality
    
    if u_id in bot_set.user_data:
        user_settings = bot_set.user_data[u_id]
        quality = user_settings.get("qobuz_qual", client.quality)
    
    if int(quality) == 5:
        return 'mp3', '320K'
    else:
        bit_depth = meta.get("bit_depth", 16)
        sampling_rate = meta.get("sampling_rate", 44.1)
        
        if isinstance(sampling_rate, float) and sampling_rate.is_integer():
            sampling_rate = int(sampling_rate)

        return 'flac', f'{bit_depth}B - {sampling_rate}k'
