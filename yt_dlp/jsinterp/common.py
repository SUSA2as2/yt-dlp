from __future__ import annotations

import abc
import typing
import functools

from ..extractor.common import InfoExtractor
from ..utils import (
    classproperty,
    format_field,
    filter_dict,
    get_exe_version,
    variadic,
    url_or_none,
    sanitize_url,
    ExtractorError,
)


_JSI_HANDLERS: dict[str, type[JSI]] = {}
_JSI_PREFERENCES: set[JSIPreference] = set()
_ALL_FEATURES = {
    'wasm',
    'location',
    'dom',
    'cookies',
}


def get_jsi_keys(jsi_or_keys: typing.Iterable[str | type[JSI] | JSI]) -> list[str]:
    return [jok if isinstance(jok, str) else jok.JSI_KEY for jok in jsi_or_keys]


def filter_jsi_keys(features=None, only_include=None, exclude=None):
    keys = list(_JSI_HANDLERS)
    if features:
        keys = [key for key in keys if key in _JSI_HANDLERS
                and _JSI_HANDLERS[key]._SUPPORTED_FEATURES.issuperset(features)]
    if only_include:
        keys = [key for key in keys if key in get_jsi_keys(only_include)]
    if exclude:
        keys = [key for key in keys if key not in get_jsi_keys(exclude)]
    return keys


def filter_jsi_include(only_include: typing.Iterable[str] | None, exclude: typing.Iterable[str] | None):
    keys = get_jsi_keys(only_include) if only_include else _JSI_HANDLERS.keys()
    return [key for key in keys if key not in (exclude or [])]


def filter_jsi_feature(features: typing.Iterable[str], keys=None):
    keys = keys if keys is not None else _JSI_HANDLERS.keys()
    return [key for key in keys if key in _JSI_HANDLERS
            and _JSI_HANDLERS[key]._SUPPORTED_FEATURES.issuperset(features)]


def order_to_pref(jsi_order: typing.Iterable[str | type[JSI] | JSI], multiplier: int) -> JSIPreference:
    jsi_order = reversed(get_jsi_keys(jsi_order))
    pref_score = {jsi_cls: (i + 1) * multiplier for i, jsi_cls in enumerate(jsi_order)}

    def _pref(jsi: JSI, *args):
        return pref_score.get(jsi.JSI_KEY, 0)
    return _pref


def require_features(param_features: dict[str, str | typing.Iterable[str]]):
    assert all(_ALL_FEATURES.issuperset(variadic(kw_feature)) for kw_feature in param_features.values())

    def outer(func):
        @functools.wraps(func)
        def inner(self: JSIWrapper, *args, **kwargs):
            for kw_name, kw_feature in param_features.items():
                if kw_name in kwargs and not self._features.issuperset(variadic(kw_feature)):
                    raise ExtractorError(f'feature {kw_feature} is required for `{kw_name}` param but not declared')
            return func(self, *args, **kwargs)
        return inner
    return outer


class JSIWrapper:
    """
    Helper class to forward JS interp request to a JSI that supports it.

    Usage:
    ```
    def _real_extract(self, url):
        ...
        jsi = JSIWrapper(self, url, features=['js'])
        result = jsi.execute(jscode, video_id)
        ...
    ```

    Features:
    - `wasm`: supports window.WebAssembly
    - `location`: supports mocking window.location
    - `dom`: supports DOM interface (not necessarily rendering)
    - `cookies`: supports document.cookie read & write

    @param dl_or_ie: `YoutubeDL` or `InfoExtractor` instance.
    @param url: setting url context, used by JSI that supports `location` feature
    @param features: only JSI that supports all of these features will be selected
    @param only_include: limit JSI to choose from.
    @param exclude: JSI to avoid using.
    @param jsi_params: extra kwargs to pass to `JSI.__init__()` for each JSI, using jsi key as dict key.
    @param preferred_order: list of JSI to use. First in list is tested first.
    @param fallback_jsi: list of JSI that may fail and should act non-fatal and fallback to other JSI. Pass `"all"` to always fallback
    @param timeout: timeout parameter for all chosen JSI
    @param user_agent: override user-agent to use for supported JSI
    """

    def __init__(
        self,
        dl_or_ie: YoutubeDL | InfoExtractor,
        url: str = '',
        features: typing.Iterable[str] = [],
        only_include: typing.Iterable[str | type[JSI]] = [],
        exclude: typing.Iterable[str | type[JSI]] = [],
        jsi_params: dict[str, dict] = {},
        preferred_order: typing.Iterable[str | type[JSI]] = [],
        fallback_jsi: typing.Iterable[str | type[JSI]] | typing.Literal['all'] = [],
        timeout: float | int = 10,
        user_agent: str | None = None,
    ):
        self._downloader: YoutubeDL = dl_or_ie._downloader if isinstance(dl_or_ie, InfoExtractor) else dl_or_ie
        self._url = sanitize_url(url_or_none(url)) or ''
        self._features = set(features)
        if url and not self._url:
            self.report_warning(f'Invalid URL: "{url}", using empty string instead')

        if unsupported_features := self._features - _ALL_FEATURES:
            raise ExtractorError(f'Unsupported features: {unsupported_features}, allowed features: {_ALL_FEATURES}')

        user_prefs = self._downloader.params.get('jsi_preference', [])
        for invalid_key in [jsi_key for jsi_key in user_prefs if jsi_key not in _JSI_HANDLERS]:
            self.report_warning(f'`{invalid_key}` is not a valid JSI, ignoring preference setting')
            user_prefs.remove(invalid_key)

        handler_classes = [_JSI_HANDLERS[key] for key in filter_jsi_keys(self._features, only_include, exclude)]
        self.write_debug(f'Select JSI for features={self._features}: {get_jsi_keys(handler_classes)}, '
                         f'included: {get_jsi_keys(only_include) or "all"}, excluded: {get_jsi_keys(exclude)}')
        if not handler_classes:
            raise ExtractorError(f'No JSI supports features={self._features}')

        self._handler_dict = {cls.JSI_KEY: cls(
            self._downloader, url=self._url, timeout=timeout, features=self._features,
            user_agent=user_agent, **jsi_params.get(cls.JSI_KEY, {}),
        ) for cls in handler_classes}

        self.preferences: set[JSIPreference] = {
            order_to_pref(user_prefs, 10000), order_to_pref(preferred_order, 100)} | _JSI_PREFERENCES

        self._fallback_jsi = get_jsi_keys(handler_classes) if fallback_jsi == 'all' else get_jsi_keys(fallback_jsi)
        self._is_test = self._downloader.params.get('test', False)

    def write_debug(self, message, only_once=False):
        return self._downloader.write_debug(f'[JSIDirector] {message}', only_once=only_once)

    def report_warning(self, message, only_once=False):
        return self._downloader.report_warning(f'[JSIDirector] {message}', only_once=only_once)

    def _get_handlers(self, method_name: str, *args, **kwargs) -> list[JSI]:
        handlers = [h for h in self._handler_dict.values() if callable(getattr(h, method_name, None))]
        self.write_debug(f'Choosing handlers for method `{method_name}`: {get_jsi_keys(handlers)}')
        if not handlers:
            raise ExtractorError(f'No JSI supports method `{method_name}`, '
                                 f'included handlers: {get_jsi_keys(self._handler_dict.values())}')

        preferences = {
            handler.JSI_KEY: sum(pref_func(handler, method_name, args, kwargs) for pref_func in self.preferences)
            for handler in handlers
        }
        self.write_debug('JSI preferences for `{}` request: {}'.format(
            method_name, ', '.join(f'{key}={pref}' for key, pref in preferences.items())))

        return sorted(handlers, key=lambda h: preferences[h.JSI_KEY], reverse=True)

    def _dispatch_request(self, method_name: str, *args, **kwargs):
        handlers = self._get_handlers(method_name, *args, **kwargs)

        unavailable: list[str] = []
        exceptions: list[tuple[JSI, Exception]] = []

        for handler in handlers:
            if not handler.is_available():
                if self._is_test:
                    raise ExtractorError(f'{handler.JSI_NAME} is not available for testing, '
                                         f'add "{handler.JSI_KEY}" in `exclude` if it should not be used')
                self.write_debug(f'{handler.JSI_KEY} is not available')
                unavailable.append(handler.JSI_NAME)
                continue
            try:
                self.write_debug(f'Dispatching `{method_name}` task to {handler.JSI_NAME}')
                return getattr(handler, method_name)(*args, **kwargs)
            except ExtractorError as e:
                if handler.JSI_KEY not in self._fallback_jsi:
                    raise
                else:
                    exceptions.append((handler, e))
                    self.write_debug(f'{handler.JSI_NAME} encountered error, fallback to next handler: {e}')

        if not exceptions:
            msg = f'No available JSI installed, please install one of: {", ".join(unavailable)}'
        else:
            msg = f'Failed to perform {method_name}, total {len(exceptions)} errors'
            if unavailable:
                msg = f'{msg}. You can try installing one of unavailable JSI: {", ".join(unavailable)}'
        raise ExtractorError(msg)

    @require_features({'location': 'location', 'html': 'dom', 'cookiejar': 'cookies'})
    def execute(self, jscode: str, video_id: str | None, note: str | None = None,
                html: str | None = None, cookiejar: YoutubeDLCookieJar | None = None) -> str:
        """
        Execute JS code and return stdout from console.log

        @param jscode: JS code to execute
        @param video_id
        @param note
        @param html: html to load as document, requires `dom` feature
        @param cookiejar: cookiejar to read and set cookies, requires `cookies` feature, pass `InfoExtractor.cookiejar` if you want to read and write cookies
        """
        return self._dispatch_request('execute', jscode, video_id, **filter_dict({
            'note': note, 'html': html, 'cookiejar': cookiejar}))


class JSI(abc.ABC):
    _SUPPORTED_FEATURES: set[str] = set()
    _BASE_PREFERENCE: int = 0

    def __init__(self, downloader: YoutubeDL, url: str, timeout: float | int, features: set[str], user_agent=None):
        if not self._SUPPORTED_FEATURES.issuperset(features):
            raise ExtractorError(f'{self.JSI_NAME} does not support all required features: {features}')
        self._downloader = downloader
        self._url = url
        self.timeout = timeout
        self.features = features
        self.user_agent: str = user_agent or self._downloader.params['http_headers']['User-Agent']

    @abc.abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    def write_debug(self, message, *args, **kwargs):
        self._downloader.write_debug(f'[{self.JSI_KEY}] {message}', *args, **kwargs)

    def report_warning(self, message, *args, **kwargs):
        self._downloader.report_warning(f'[{self.JSI_KEY}] {message}', *args, **kwargs)

    def to_screen(self, msg, *args, **kwargs):
        self._downloader.to_screen(f'[{self.JSI_KEY}] {msg}', *args, **kwargs)

    def report_note(self, video_id, note):
        self.to_screen(f'{format_field(video_id, None, "%s: ")}{note}')

    @classproperty
    def JSI_NAME(cls) -> str:
        return cls.__name__[:-3]

    @classproperty
    def JSI_KEY(cls) -> str:
        assert cls.__name__.endswith('JSI'), 'JSI class names must end with "JSI"'
        return cls.__name__[:-3]


class ExternalJSI(JSI, abc.ABC):
    _EXE_NAME: str

    @classproperty(cache=True)
    def exe_version(cls):
        return get_exe_version(cls._EXE_NAME, args=getattr(cls, 'V_ARGS', ['--version']), version_re=r'([0-9.]+)')

    @classproperty
    def exe(cls):
        return cls._EXE_NAME if cls.exe_version else None

    @classmethod
    def is_available(cls):
        return bool(cls.exe)


def register_jsi(jsi_cls: JsiClass) -> JsiClass:
    """Register a JS interpreter class"""
    assert issubclass(jsi_cls, JSI), f'{jsi_cls} must be a subclass of JSI'
    assert jsi_cls.JSI_KEY not in _JSI_HANDLERS, f'JSI {jsi_cls.JSI_KEY} already registered'
    assert jsi_cls._SUPPORTED_FEATURES.issubset(_ALL_FEATURES), f'{jsi_cls._SUPPORTED_FEATURES - _ALL_FEATURES}  not declared in `_All_FEATURES`'
    _JSI_HANDLERS[jsi_cls.JSI_KEY] = jsi_cls
    return jsi_cls


def register_jsi_preference(*handlers: type[JSI]):
    assert all(issubclass(handler, JSI) for handler in handlers), f'{handlers} must all be a subclass of JSI'

    def outer(pref_func: JSIPreference) -> JSIPreference:
        def inner(handler: JSI, *args):
            if not handlers or isinstance(handler, handlers):
                return pref_func(handler, *args)
            return 0
        _JSI_PREFERENCES.add(inner)
        return inner
    return outer


@register_jsi_preference()
def _base_preference(handler: JSI, *args):
    return getattr(handler, '_BASE_PREFERENCE', 0)


if typing.TYPE_CHECKING:
    from ..YoutubeDL import YoutubeDL
    from ..cookies import YoutubeDLCookieJar
    JsiClass = typing.TypeVar('JsiClass', bound=type[JSI])

    class JSIPreference(typing.Protocol):
        def __call__(self, handler: JSI, method_name: str, *args, **kwargs) -> int:
            ...
