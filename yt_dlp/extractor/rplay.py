import asyncio
import random
import re
import time

from playwright.async_api import async_playwright

from .common import InfoExtractor
from ..utils import (
    ExtractorError,
    UserNotLive,
    encode_data_uri,
    float_or_none,
    parse_iso8601,
    traverse_obj,
    url_or_none,
)


class RPlayBaseIE(InfoExtractor):
    _TOKEN_CACHE = {}
    _user_id = None
    _login_type = None
    _jwt_token = None

    @property
    def user_id(self):
        return self._user_id

    @property
    def login_type(self):
        return self._login_type

    @property
    def jwt_token(self):
        return self._jwt_token

    def _perform_login(self, username, password):
        _ = {
            'alg': 'HS256',
            'typ': 'JWT',
        }
        raise NotImplementedError

    def _login_by_token(self, jwt_token, video_id):
        user_info = self._download_json(
            'https://api.rplay.live/account/login', video_id, note='performing login', errnote='Failed to login',
            data=f'{{"token":"{jwt_token}","lang":"en","loginType":null,"checkAdmin":null}}'.encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'null'}, fatal=False)
        if user_info:
            self._user_id = traverse_obj(user_info, 'oid')
            self._login_type = traverse_obj(user_info, 'accountType')
            self._jwt_token = jwt_token

    def _get_butter_files(self):
        cache = self.cache.load('rplay', 'butter-code') or {}
        if cache.get('date', 0) > time.time() - 86400:
            return cache['js'], cache['wasm']
        butter_js = self._download_webpage('https://pb.rplay.live/kr/public/smooth_like_butter.js', 'butter',
                                           'getting butter-sign js')
        urlh = self._request_webpage('https://pb.rplay.live/kr/public/smooth_like_butter_bg.wasm', 'butter',
                                     'getting butter-sign wasm')
        butter_wasm_array = list(urlh.read())
        self.cache.store('rplay', 'butter-code', {'js': butter_js, 'wasm': butter_wasm_array, 'date': time.time()})
        return butter_js, butter_wasm_array

    def _playwright_eval(self, jscode, goto=None, wait_until='commit', stop_loading=True):
        async def __aeval():
            async with async_playwright() as p:
                browser = await p.chromium.launch(chromium_sandbox=True)
                page = await browser.new_page()
                if goto:
                    try:
                        start = time.time()
                        await page.goto(goto, wait_until=wait_until)
                        self.write_debug(f'{wait_until} loaded in {time.time() - start} s')
                        if stop_loading:
                            await page.evaluate('window.stop();')
                    except Exception as e:
                        self.report_warning(f'Failed to navigate to {goto}: {e}')
                        await browser.close()
                        return
                try:
                    start = time.time()
                    value = await asyncio.wait_for(page.evaluate(jscode), timeout=10)
                    self.write_debug(f'JS execution finished in {time.time() - start} s')
                except asyncio.TimeoutError:
                    self.report_warning('PlayWright JS evaluation timed out')
                    value = None
                finally:
                    await browser.close()
            return value

        try:
            return asyncio.run(__aeval())
        except asyncio.InvalidStateError:
            pass

    def _calc_butter_token(self):
        butter_js, butter_wasm_array = self._get_butter_files()
        butter_js = butter_js.replace('export{initSync};export default __wbg_init;', '')
        butter_js = butter_js.replace('export class', 'class')
        butter_js = butter_js.replace('new URL("smooth_like_butter_bg.wasm",import.meta.url)', '""')

        butter_js += ''';const proxy = new Proxy(window.navigator, {get(target, prop, receiver) {
            if (prop == "webdriver") return false;
            return target[prop];
        }});
        Object.defineProperty(window, "navigator", {get: ()=> proxy});'''

        butter_js += '''__new_init = async () => {
            const t = __wbg_get_imports();
            __wbg_init_memory(t);
            const {module, instance} = await WebAssembly.instantiate(Uint8Array.from(%s), t);
            __wbg_finalize_init(instance, module);
        };''' % butter_wasm_array  # noqa: UP031

        butter_js += '__new_init().then(() => (new ButterFactory()).generate_butter())'

        # The generator checks `navigator` and `location` to generate correct token
        return self._playwright_eval(butter_js, goto='https://rplay.live/')

    def get_butter_token(self):
        cache = self.cache.load('rplay', 'butter-token') or {}
        timestamp = str(int(time.time() / 360))
        if cache.get(timestamp):
            return cache[timestamp]
        token = self._calc_butter_token()
        self.cache.store('rplay', 'butter-token', {timestamp: token})
        return token


class RPlayVideoIE(RPlayBaseIE):
    _VALID_URL = r'https://rplay.live/play/(?P<id>[\d\w]+)'
    _TESTS = [{
        'url': 'https://rplay.live/play/669203d25223214e67579dc3/',
        'info_dict': {
            'id': '669203d25223214e67579dc3',
            'ext': 'mp4',
            'title': 'md5:6ab0a76410b40b1f5fb48a2ad7571264',
            'description': 'md5:d2fb2f74a623be439cf454df5ff3344a',
            'release_timestamp': 1720846360,
            'release_date': '20240713',
            'duration': 5349.0,
            'thumbnail': r're:https://[\w\d]+.cloudfront.net/.*',
            'uploader': '杏都める',
            'uploader_id': '667adc9e9aa7f739a2158ff3',
            'tags': ['杏都める', 'めいどるーちぇ', '無料', '耳舐め', 'ASMR'],
        },
    }]

    def _real_extract(self, url):
        video_id = self._match_id(url)

        if self._configuration_arg('jwt_token') and not self.user_id:
            self._login_by_token(self._configuration_arg('jwt_token', casesense=True)[0], video_id)

        headers = {'Origin': 'https://rplay.live', 'Referer': 'https://rplay.live/'}
        content = self._download_json('https://api.rplay.live/content', video_id, query={
            'contentOid': video_id,
            'status': 'published',
            'withComments': True,
            'requestCanView': True,
            **({
                'requestorOid': self.user_id,
                'loginType': self.login_type,
            } if self.user_id else {}),
        }, headers={**headers, 'Authorization': self.jwt_token or 'null'})
        if content.get('drm'):
            raise ExtractorError('This video is DRM-protected')
        content.pop('daily_views', None)
        content.get('creatorInfo', {}).pop('subscriptionTiers', None)

        metainfo = traverse_obj(content, {
            'title': ('title', {str}),
            'description': ('introText', {str}),
            'release_timestamp': ('publishedAt', {parse_iso8601}),
            'duration': ('length', {float_or_none}),
            'uploader': ('nickname', {str}),
            'uploader_id': ('creatorOid', {str}),
            'tags': ('hashtags', lambda _, v: v[0] != '_'),
        })

        m3u8_url = traverse_obj(content, ('canView', 'url'))
        if not m3u8_url:
            raise ExtractorError('You do not have access to this video. '
                                 'Passing JWT token using --extractor-args RPlayVideo:jwt_token=xxx.xxxxx.xxx to login')

        thumbnail_key = traverse_obj(content, ('streamables', lambda _, v: v['type'].startswith('image/'), 's3key', any))
        if thumbnail_key:
            metainfo['thumbnail'] = url_or_none(self._download_webpage(
                'https://api.rplay.live/upload/privateasset', video_id, 'getting cover url', query={
                    'key': thumbnail_key,
                    'contentOid': video_id,
                    'creatorOid': metainfo.get('uploader_id'),
                    **({
                        'requestorOid': self.user_id,
                        'loginType': self.login_type,
                    } if self.user_id else {}),
                }, fatal=False))

        formats = self._extract_m3u8_formats(m3u8_url, video_id, headers={**headers, 'Butter': self.get_butter_token()})
        for fmt in formats:
            m3u8_doc = self._download_webpage(fmt['url'], video_id, 'getting m3u8 contents',
                                              headers={**headers, 'Butter': self.get_butter_token()})
            fmt['url'] = encode_data_uri(m3u8_doc.encode(), 'application/x-mpegurl')
            match = re.search(r'^#EXT-X-KEY.*?URI="([^"]+)"', m3u8_doc, flags=re.M)
            if match:
                urlh = self._request_webpage(match[1], video_id, 'getting hls key', headers={
                    **headers,
                    'rplay-private-content-requestor': self.user_id or 'not-logged-in',
                    'age': random.randint(100, 10000),
                })
                fmt['hls_aes'] = {'key': urlh.read().hex()}

        return {
            'id': video_id,
            'formats': formats,
            **metainfo,
            'http_headers': {'Origin': 'https://rplay.live', 'Referer': 'https://rplay.live/'},
        }


class RPlayUserIE(RPlayBaseIE):
    _VALID_URL = r'https://rplay.live/(?P<short>c|creatorhome)/(?P<id>[\d\w]+)/?(?:[#?]|$)'
    _TESTS = [{
        'url': 'https://rplay.live/creatorhome/667adc9e9aa7f739a2158ff3?page=contents',
        'info_dict': {
            'id': '667adc9e9aa7f739a2158ff3',
            'title': '杏都める',
        },
        'playlist_mincount': 34,
    }, {
        'url': 'https://rplay.live/c/furachi?page=contents',
        'info_dict': {
            'id': '65e07e60850f4527aab74757',
            'title': '逢瀬ふらち OuseFurachi',
        },
        'playlist_mincount': 77,
    }]

    def _real_extract(self, url):
        user_id, short = self._match_valid_url(url).group('id', 'short')
        key = 'customUrl' if short == 'c' else 'userOid'

        user_info = self._download_json(
            f'https://api.rplay.live/account/getuser?{key}={user_id}&filter[]=nickname&filter[]=published', user_id)
        entries = traverse_obj(user_info, ('published', ..., {
            lambda x: self.url_result(f'https://rplay.live/play/{x}/', ie=RPlayVideoIE, video_id=x)}))

        return self.playlist_result(entries, user_info.get('_id', user_id), user_info.get('nickname'))


class RPlayLiveIE(RPlayBaseIE):
    _VALID_URL = r'https://rplay.live/c/(?P<id>[\d\w]+)/live'

    def _real_extract(self, url):
        user_id = self._match_id(url)

        user_id = self._download_json(f'https://api.rplay.live/account/getuser?customUrl={user_id}', user_id)['_id']
        live_info = self._download_json('https://api.rplay.live/live/play', user_id,
                                        query={'creatorOid': user_id})

        stream_state = live_info['streamState']
        if stream_state == 'youtube':
            return self.url_result(f'https://www.youtube.com/watch?v={live_info["liveStreamId"]}')
        elif stream_state == 'offline':
            raise UserNotLive
        else:
            raise ExtractorError(f'Unknow streamState: {stream_state}')
