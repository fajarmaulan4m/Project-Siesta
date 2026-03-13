# [GANTI FILE: bot/helpers/kkbox/api.py]

import json
import re
import requests 
from requests.adapters import HTTPAdapter
from time import time, sleep
from random import randrange
from Cryptodome.Cipher import ARC4
from Cryptodome.Hash import MD5
from bot.logger import LOGGER

class KkboxAPI:
    def __init__(self, exception, kc1_key, secret_key, kkid = None):
        self.exception = exception

        key_pattern = re.compile("[0-9a-f]{32}")
        if not key_pattern.fullmatch(kc1_key):
            raise self.exception("kc1_key is invalid, change it in settings")
        if not key_pattern.fullmatch(secret_key):
            raise self.exception("secret_key is invalid, change it in settings")

        self.kc1_key = kc1_key.encode('ascii')
        self.secret_key = secret_key.encode('ascii')
        
        self.s = requests.Session() 
        
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
        self.s.mount('http://', adapter)
        self.s.mount('https://', adapter)
        
        self.s.headers.update({
            'user-agent': 'okhttp/3.14.9'
        })

        self.kkid = kkid or '%032X' % randrange(16**32)

        self.params = {
            'enc': 'u',
            'ver': '06120082',
            'os': 'android',
            'osver': '13',
            'lang': 'en',
            'ui_lang': 'en',
            'dist': '0021',
            'dist2': '0021',
            'resolution': '411x841',
            'of': 'j',
            'oenc': 'kc1',
        }

    def kc1_decrypt(self, data):
        cipher = ARC4.new(self.kc1_key)
        try:
            return cipher.decrypt(data).decode('utf-8')
        except Exception:
            return None

    def api_call(self, host, path, params={}, payload=None, timeout=60):
        if host == 'ticket':
            payload = json.dumps(payload)

        timestamp = int(time())

        md5 = MD5.new()
        md5.update(self.params['ver'].encode('ascii'))
        md5.update(str(timestamp).encode('ascii'))
        md5.update(self.secret_key)

        params.update(self.params)
        params.update({'secret': md5.hexdigest()})
        params.update({'timestamp': timestamp})

        url = f'https://api-{host}.kkbox.com.tw/{path}'
        try:
            if not payload:
                r = self.s.get(url, params=params, timeout=timeout)
            else:
                r = self.s.post(url, params=params, data=payload, timeout=timeout)
        except Exception as e:
            LOGGER.error(f"KKBox Connection Error ({url}): {e}")
            return None

        if not r.content:
            LOGGER.error(f"KKBox API Error: r.content kosong dari {url}")
            return None

        decrypted = self.kc1_decrypt(r.content)
        if not decrypted:
            LOGGER.error(f"KKBox API Error: Gagal decrypt response dari {url}")
            return None

        try:
            resp = json.loads(decrypted)
            return resp
        except json.JSONDecodeError as e:
            LOGGER.error(f"KKBox API Error: JSON Decode gagal ({url}) - {e}. Raw: {decrypted}")
            return None

    def login(self, email, password):
        md5 = MD5.new()
        md5.update(password.encode('utf-8'))
        pswd = md5.hexdigest()

        resp = self.api_call('login', 'login.php', payload={
            'uid': email,
            'passwd': pswd,
            'kkid': self.kkid,
            'registration_id': '',
        })

        if not resp:
            raise self.exception('Login failed: No response from API')

        if resp['status'] not in (2, 3):
            if resp['status'] == -1:
                raise self.exception('Email not found')
            elif resp['status'] == -2:
                raise self.exception('Incorrect password')
            elif resp['status'] == -4:
                raise self.exception('IP address is in unsupported region, use a VPN')
            elif resp['status'] == 1:
                raise self.exception('Account expired')
            raise self.exception(f'Login failed, status code {resp["status"]}')

        self.apply_session(resp)

    def renew_session(self):
        resp = self.api_call('login', 'check.php')
        if not resp or resp['status'] not in (2, 3):
            raise self.exception('Session renewal failed')
        self.apply_session(resp)

    def apply_session(self, resp):
        self.sid = resp['sid']
        self.params['sid'] = self.sid

        self.lic_content_key = resp['lic_content_key'].encode('ascii')

        self.available_qualities = ['128k', '192k', '320k']
        if resp['high_quality']:
            self.available_qualities.append('hifi')
            self.available_qualities.append('hires')

    def get_songs(self, ids):
        fields_req = 'album,release_date,artist_role,song_idx,album_photo_info,song_is_explicit,song_more_url,album_more_url,artist_more_url,genre_name,is_lyrics,audio_quality,isrc,composer'
        
        resp = self.api_call('ds', 'v2/song', payload={
            'ids': ','.join(ids),
            'fields': fields_req
        })
        if not resp or resp['status']['type'] != 'OK':
            raise self.exception('Track not found')
        return resp['data']['songs']

    def get_song_lyrics(self, id):
        return self.api_call('ds', f'v1/song/{id}/lyrics')

    def get_album(self, id):
        # Percobaan 1: Endpoint Standar (v2)
        resp = self.api_call('ds', f'v2/album/{id}')
        if resp and resp.get('status', {}).get('type') == 'OK':
            return resp['data']
            
        # Percobaan 2: Endpoint Shared Albums (v1) - untuk ID hasil share/pendek
        resp = self.api_call('ds', f'v1/shared-albums/{id}')
        if resp and resp.get('status', {}).get('type') == 'OK':
            return resp['data']

        # Percobaan 3: Endpoint Legacy (album_more.php) - Fallback Terkuat
        # Jika API v2 dan Shared gagal, coba ambil langsung dari backend legacy.
        # Ini penting untuk album yang memiliki 'Shared ID' tapi tidak terdaftar di endpoint REST v1/v2.
        try:
            more_resp = self.api_call('ds', 'album_more.php', params={'album': id})
            if more_resp and 'info' in more_resp and more_resp['info']:
                info = more_resp['info']
                # Kita perlu mengubah format 'info' (legacy) menjadi format objek 'album v2'
                # agar metadata.py bisa memprosesnya tanpa error.
                fake_v2_data = {
                    'id': info.get('album_id', id),
                    'name': info.get('name', 'Unknown Album'),
                    'url': info.get('url', ''),
                    'explicit': False,
                    'available_territories': ['TW', 'HK', 'SG', 'MY', 'JP'],
                    'release_date': info.get('album_date', ''),
                    'artist': {
                        'id': '',
                        'name': info.get('artist_name', 'Unknown Artist'),
                        'url': ''
                    },
                    'images': [
                        {
                            'height': 1000,
                            'width': 1000,
                            'url': info.get('album_photo_info', {}).get('url_template', '')
                        }
                    ]
                }
                return fake_v2_data
        except Exception:
            pass

        # Jika semua 3 metode gagal, baru raise error
        raise self.exception('Album not found')

    def get_album_more(self, raw_id):
        return self.api_call('ds', 'album_more.php', params={
            'album': raw_id
        })

    def get_artist(self, id):
        resp = self.api_call('ds', f'v3/artist/{id}')
        if not resp or resp['status']['type'] != 'OK':
            raise self.exception('Artist not found')
        return resp['data']
    
    def get_artist_albums(self, raw_id, limit, offset):
        resp = self.api_call('ds', f'v2/artist/{raw_id}/album', params={
            'limit': limit,
            'offset': offset,
        })
        if not resp or resp['status']['type'] != 'OK':
            raise self.exception('Artist not found')
        return resp['data']['album']

    def get_playlists(self, ids):
        resp = self.api_call('ds', f'v1/playlists', params={
            'playlist_ids': ','.join(ids)
        })
        
        if not resp or resp['status']['type'] != 'OK':
             resp = self.api_call('ds', f'v1/shared-playlists', params={
                'playlist_ids': ','.join(ids)
             })

        if not resp or resp['status']['type'] != 'OK':
            raise self.exception('Playlist not found')
        return resp['data']['playlists']
    
    def get_playlist_tracks(self, playlist_id):
        try:
            resp = self.api_call('ds', f'v1/playlists/{playlist_id}/tracks', 
                               params={'limit': 500}, timeout=5)
            if resp and resp.get('status', {}).get('type') == 'OK':
                 data = resp.get('data', [])
                 if data: return data
        except Exception:
            pass
             
        try:
            resp = self.api_call('ds', f'v1/shared-playlists/{playlist_id}/tracks', 
                               params={'limit': 500}, timeout=5)
            if resp and resp.get('status', {}).get('type') == 'OK':
                 data = resp.get('data', [])
                 if data: return data
        except Exception:
            pass

        try:
             resp = self.api_call('ds', f'v1/charts/{playlist_id}/tracks', 
                                params={'limit': 500}, timeout=3)
             if resp and resp.get('status', {}).get('type') == 'OK':
                  data = resp.get('data', [])
                  if data: return data
        except Exception:
            pass

        LOGGER.error(f"KKBox API: Gagal menemukan tracks di semua endpoint untuk ID {playlist_id}")
        return []

    def search(self, query, types, limit):
        return self.api_call('ds', 'search_music.php', params={
            'sf': ','.join(types),
            'limit': limit,
            'query': query,
            'search_ranking': 'sc-A',
        })

    def get_ticket(self, song_id, play_mode = None):
        resp = self.api_call('ticket', 'v1/ticket', payload={
            'sid': self.sid,
            'song_id': song_id,
            'ver': '06120082',
            'os': 'android',
            'osver': '13',
            'kkid': self.kkid,
            'dist': '0021',
            'dist2': '0021',
            'timestamp': int(time()),
            'play_mode': play_mode,
        })

        if not resp:
            self.renew_session()
            return self.get_ticket(song_id, play_mode)

        if resp['status'] != 1:
            if resp['status'] == -1:
                self.renew_session()
                return self.get_ticket(song_id, play_mode)
            elif resp['status'] == -4:
                self.auth_device()
                return self.get_ticket(song_id, play_mode)
            elif resp['status'] == 2:
                sleep(0.5)
                return self.get_ticket(song_id, play_mode)
            raise self.exception("Couldn't get track URLs")

        return resp['uris']

    def auth_device(self):
        resp = self.api_call('ds', 'active_sid.php', payload={
            'ui_lang': 'en',
            'of': 'j',
            'os': 'android',
            'enc': 'u',
            'sid': self.sid,
            'ver': '06120082',
            'kkid': self.kkid,
            'lang': 'en',
            'oenc': 'kc1',
            'osver': '13',
        })
        if not resp or resp['status'] != 1:
            raise self.exception("Couldn't auth device")

    def kkdrm_dl(self, url, path):
        resp = self.s.get(url, stream=True, headers={'range': 'bytes=1024-'})
        resp.raise_for_status()

        rc4 = ARC4.new(self.lic_content_key, drop=512)

        with open(path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=4096):
                f.write(rc4.decrypt(chunk))

    def close_session(self):
        if self.s:
            self.s.close()
