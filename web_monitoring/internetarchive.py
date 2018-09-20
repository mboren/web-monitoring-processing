"""
This module provides a Python API for accessing versions (timestamped captures)
of a URL. There are existing open-source Python packages for the Internet
Archive API (the best-established one seems to be
https://internetarchive.readthedocs.io/en/latest/) but none that expose the
list of versions of a URL.

References used in writing this module:
* https://ws-dl.blogspot.fr/2013/07/2013-07-15-wayback-machine-upgrades.html

Other potentially useful links:
* https://blog.archive.org/developers/
* https://archive.readme.io/docs/memento
"""

from base64 import b32encode
from collections import namedtuple
from datetime import datetime
import hashlib
import logging
import urllib.parse
import re
import requests
import time
from web_monitoring import utils, __version__

from requests.exceptions import (
    ConnectionError,
    ProxyError,
    RetryError,
    Timeout
)
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    ReadTimeoutError
)


logger = logging.getLogger(__name__)


class WaybackException(Exception):
    # All exceptions raised directly by this package inherit from this.
    ...


class UnexpectedResponseFormat(WaybackException):
    ...


# TODO: split this up into a family of more specific errors? When playback
# failed partway into a redirect chain, when a redirect goes outside
# redirect_target_window, when a memento was circular?
class MementoPlaybackError(WaybackException):
    ...


class WaybackRetryError(WaybackException):
    def __init__(self, retries, total_time, causal_error):
        self.retries = retries
        self.cause = causal_error
        self.time = total_time
        super().__init__(f'Retried {retries} times over {total_time or "?"} seconds (error: {causal_error})')


CDX_SEARCH_URL = 'http://web.archive.org/cdx/search/cdx'
ARCHIVE_RAW_URL_TEMPLATE = 'http://web.archive.org/web/{timestamp}id_/{url}'
ARCHIVE_VIEW_URL_TEMPLATE = 'http://web.archive.org/web/{timestamp}/{url}'
URL_DATE_FORMAT = '%Y%m%d%H%M%S'
MEMENTO_URL_PATTERN = re.compile(
    r'^http(?:s)?://web.archive.org/web/(\d+)(?:id_)?/(.+)$')
REDUNDANT_HTTP_PORT = re.compile(r'^(http://[^:/]+):80(.*)$')
REDUNDANT_HTTPS_PORT = re.compile(r'^(https://[^:/]+):443(.*)$')
DATA_URL_START = re.compile(r'data:[\w]+/[\w]+;base64')
# Matches URLs w/ users w/no pass, e-mail addresses, and mailto: URLs. These
# basically look like an e-mail or mailto: got `http://` pasted in front, e.g:
#   http://b***z@pnnl.gov/
#   http://@pnnl.gov/
#   http://mailto:first.last@pnnl.gov/
#   http://<<mailto:first.last@pnnl.gov>>/
EMAILISH_URL = re.compile(r'^https?://(<*)((mailto:)|([^/@:]*@))')

CdxRecord = namedtuple('CdxRecord', (
    # Raw CDX values
    'key',
    'timestamp',
    'url',
    'mime_type',
    'status_code',
    'digest',
    'length',
    # Synthesized values
    'date',
    'raw_url',
    'view_url'
))


def split_memento_url(memento_url):
    'Extract the raw date and URL components from a memento URL.'
    match = MEMENTO_URL_PATTERN.match(memento_url)
    if match is None:
        raise ValueError(f'"{memento_url}" is not a memento URL')

    return match.group(2), match.group(1)


def clean_memento_url_component(url):
    # A URL *may* be percent encoded, decode ONLY if so (we don’t want to
    # accidentally decode the querystring if there is one)
    lower_url = url.lower()
    if lower_url.startswith('http%3a') or lower_url.startswith('https%3a'):
        url = urllib.parse.unquote(url)

    return url


def memento_url_data(memento_url):
    """
    Get the original URL and date that a memento URL represents a capture of.

    Examples
    --------
    Extract original URL and date.

    >>> memento_url_data('http://web.archive.org/web/20170813195036/https://arpa-e.energy.gov/?q=engage/events-workshops')
    ('https://arpa-e.energy.gov/?q=engage/events-workshops', datetime.datetime(2017, 8, 13, 19, 50, 36))
    """
    raw_url, timestamp = split_memento_url(memento_url)
    url = clean_memento_url_component(raw_url)
    date = datetime.strptime(timestamp, URL_DATE_FORMAT)

    return url, date


def original_url_for_memento(memento_url):
    """
    Get the original URL that a memento URL represents a capture of.

    Examples
    --------
    Extract original URL.

    >>> original_url_for_memento('http://web.archive.org/web/20170813195036/https://arpa-e.energy.gov/?q=engage/events-workshops')
    'https://arpa-e.energy.gov/?q=engage/events-workshops'
    """
    return clean_memento_url_component(split_memento_url(memento_url)[0])


def is_malformed_url(url):
    if DATA_URL_START.search(url):
        return True

    # TODO: restrict to particular protocols?
    if url.startswith('mailto:') or EMAILISH_URL.match(url):
        return True

    return False


def cdx_hash(content):
    if isinstance(content, str):
        content = content.encode()
    return b32encode(hashlib.sha1(content).digest()).decode()


# TODO: make rate limiting configurable at the session level, rather than
# arbitrarily set inside get_memento(). Idea: have a rate limit lock type and
# pass an instance to the constructor here.
class WaybackSession(utils.DisableAfterCloseSession, requests.Session):
    """
    A custom session object that network pools connections and resources for
    requests to the Wayback Machine.

    Parameters
    ----------
    retries : int, optional
        The maximum number of retries for requests.
    backoff : int or float, optional
        Number of seconds from which to calculate how long to back off and wait
        when retrying requests. The first retry is always immediate, but
        subsequent retries are calculated as:
            seconds = backoff * 2 ^ (retry number - 1)
        So if this was `2`, retries would happen after the following delays:
            0 seconds, 4 seconds, 8 seconds, 16 seconds, ...
    timeout : int or float or tuple of (int or float, int or float), optional
        A timeout to use for all requests. If not set, there will be no
        no explicit timeout. See the Requests docs for more:
        http://docs.python-requests.org/en/master/user/advanced/#timeouts
    user_agent : str, optional
        A custom user-agent string to use in all requests.
    """

    # It seems Wayback sometimes produces 500 errors for transient issues, so
    # they make sense to retry here. Usually not in other contexts, though.
    retryable_statuses = frozenset((413, 421, 429, 500, 502, 503, 504, 599))

    retryable_errors = (ConnectTimeoutError, MaxRetryError, ReadTimeoutError,
                        ProxyError, RetryError, Timeout)
    handleable_errors = (ConnectionError,) + retryable_errors

    def __init__(self, retries=5, backoff=2, timeout=None, user_agent=None):
        super().__init__()
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.headers = {'User-Agent': user_agent or f'edgi.web_monitoring.WaybackClient/{__version__}'}
        # NOTE: the nice way to accomplish retry/backoff is with a urllib3:
        #     adapter = requests.adapters.HTTPAdapter(
        #         max_retries=Retry(total=5, backoff_factor=2,
        #                           status_forcelist=(503, 504)))
        #     self.mount('http://', adapter)
        # But Wayback mementos can have errors, which complicates things. See:
        # https://github.com/urllib3/urllib3/issues/1445#issuecomment-422950868

    # Customize the built-in `send` functionality with retryability.
    # NOTE: worth considering whether we should push this logic to a custom
    # requests.adapters.HTTPAdapter
    def send(self, *args, **kwargs):
        if self.timeout is not None and 'timeout' not in kwargs:
            kwargs['timeout'] = self.timeout

        total_time = 0
        maximum = self.retries
        retries = 0
        while True:
            try:
                result = super().send(*args, **kwargs)
                if retries >= maximum or not self.should_retry(result):
                    return result
            except WaybackSession.handleable_errors as error:
                if retries >= maximum:
                    raise WaybackRetryError(retries, total_time, error)
                elif not self.should_retry_error(error):
                    raise

            # The first retry has no delay.
            if retries > 0:
                seconds = self.backoff * 2 ** (retries - 1)
                time.sleep(seconds)

            retries += 1

    def should_retry(self, response):
        # A memento may actually be a capture of an error, so don't retry it :P
        if 'Memento-Datetime' in response.headers:
            return False

        return response.status_code in self.retryable_statuses

    def should_retry_error(self, error):
        if isinstance(error, WaybackSession.retryable_errors):
            return True
        elif isinstance(error, ConnectionError):
            text = str(error)
            if 'NewConnectionError' in text or 'Max retries' in text:
                return True

        return False


# TODO: add retry, backoff, cross_thread_backoff, and rate_limit options that
# create a custom instance of urllib3.utils.Retry
class WaybackClient(utils.DepthCountedContext):
    """
    A client for retrieving data from the Internet Archive's Wayback Machine.

    You can use a WaybackClient as a context manager. When exiting, it will
    close the session it's using (if you've passed in a custom session, make
    sure not to use the context manager functionality unless you want to live
    dangerously).

    Parameters
    ----------
    session : :class:`requests.Session`, optional
    """
    def __init__(self, session=None):
        self.session = session or WaybackSession()

    def __exit_all__(self, type, value, traceback):
        self.close()

    def close(self):
        "Close the client's session."
        self.session.close()

    def search(self, url, *, matchType=None, limit=None, offset=None,
               fastLatest=None, gzip=None, from_date=None, to_date=None,
               filter_field=None, collapse=None, showResumeKey=True,
               resumeKey=None, page=None, pageSize=None, resolveRevisits=True,
               skip_malformed_results=True, **kwargs):
        """
        Search archive.org's CDX API for all captures of a given URL.

        This will automatically page through all results for a given search.

        Returns an iterator of CdxRecord objects. The StopIteration value is
        the total count of found captures.

        Note that even URLs without wildcards may return results with different
        URLs. Search results are matched by url_key, which is a SURT-formatted,
        canonicalized URL:

        * Does not differentiate between HTTP and HTTPS
        * Is not case-sensitive
        * Treats ``www.`` and ``www*.`` subdomains the same as no subdomain at
          all

        Note not all CDX API parameters are supported. In particular, this does
        not support: `output`, `fl`, `showDupeCount`, `showSkipCount`,
        `lastSkipTimestamp`, `showNumPages`, `showPagedIndex`.

        Parameters
        ----------
        url : str
            The URL to query for captures of.
        matchType : str, optional
            Must be one of 'exact', 'prefix', 'host', or 'domain'. The default
            value is calculated based on the format of `url`.
        limit : int, optional
            Maximum number of results per page (this iterator will continue to
            move through all pages unless `showResumeKey=False`, though).
        offset : int, optional
            Skip the first N results.
        fastLatest : bool, optional
            Get faster results when using a negative value for `limit`. It may
            return a variable number of results.
        gzip : bool, optional
            Whether output should be gzipped.
        from_date : datetime, optional
            Only include captures after this date. Equivalent to the
            `from` argument in the CDX API.
        to_date : str, optional
            Only include captures before this date. Equivalent to the `to`
            argument in the CDX API.
        filter_field : str, optional
            A filter for any field in the results. Equivalent to the `filter`
            argument in the CDX API. (format: `[!]field:regex`)
        collapse : str, optional
            Collapse consecutive results that match on a given field. (format:
            `fieldname` or `fieldname:N` -- N is the number of chars to match.)
        showResumeKey : bool, optional
            If False, don't continue to iterate through all pages of results.
            The default value is True
        resumeKey : str, optional
            Start returning results from a specified resumption point/offset.
            The value for this is supplied by the previous page of results when
            `showResumeKey` is True.
        page : int, optional
            If using paging start from this page number (note: paging, as
            opposed to the using `resumeKey` is somewhat complicated because
            of the interplay with indexes and index sizes).
        pageSize : int, optional
            The number of index blocks to examine for each page of results.
            Index blocks generally cover about 3,000 items, so setting
            `pageSize=1` might return anywhere from 0 to 3,000 results per page.
        resolveRevists : bool, optional
            Attempt to resolve `warc/revisit` records to their actual content
            type and response code. Not supported on all CDX servers. Defaults
            to True.
        skip_malformed_results : bool, optional
            If true, don't yield records that look like they have no actual
            memento associated with them. Some crawlers will erroneously
            attempt to capture bad URLs like `http://mailto:someone@domain.com`
            or `http://data:image/jpeg;base64,AF34...` and so on. This is a
            filter performed client side and is not a CDX API argument.
            (Default: True)
        **kwargs
            Any additional CDX API options.

        Raises
        ------
        UnexpectedResponseFormat
            If the CDX response was not parseable.

        References
        ----------
        * https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server
        """

        # TODO: support args that can be set multiple times: filter, collapse
        # Should take input as a sequence and convert to repeat query args
        # TODO: support args that add new fields to the results or change the
        # result format
        query = {'url': url, 'matchType': matchType, 'limit': limit,
                 'offset': offset, 'gzip': gzip, 'from': from_date,
                 'to': to_date, 'filter': filter_field,
                 'fastLatest': fastLatest, 'collapse': collapse,
                 'showResumeKey': showResumeKey, 'resumeKey': resumeKey,
                 'resolveRevisits': resolveRevisits, 'page': page,
                 'pageSize': page}
        query.update(kwargs)

        unsupported = {'output', 'fl', 'showDupeCount', 'showSkipCount',
                       'lastSkipTimestamp', 'showNumPages', 'showPagedIndex'}

        final_query = {}
        for key, value in query.items():
            if key in unsupported:
                raise ValueError(f'The {key} argument is not supported')

            if value is not None:
                if isinstance(value, str):
                    final_query[key] = value
                elif isinstance(value, datetime):
                    final_query[key] = value.strftime(URL_DATE_FORMAT)
                else:
                    final_query[key] = str(value).lower()

        # TODO: our current setup requires that this request is extra robust.
        # We may be better off if we can re-architect so that's less true, or
        # somehow pass some of these retry semantics in at the constructor.
        response = self.session.request('GET', CDX_SEARCH_URL,
                                        params=final_query)
        lines = response.iter_lines()
        count = 0

        for line in lines:
            text = line.decode()

            # The resume key is delineated by a blank line.
            if text == '':
                next_args = query.copy()
                next_args['resumeKey'] = next(lines).decode()
                count += yield from self.search(**next_args)
                break

            try:
                data = CdxRecord(*text.split(' '), None, '', '')
                capture_time = datetime.strptime(data.timestamp,
                                                 URL_DATE_FORMAT)
            except Exception:
                raise UnexpectedResponseFormat(text)

            clean_url = REDUNDANT_HTTPS_PORT.sub(
                r'\1\2', REDUNDANT_HTTP_PORT.sub(
                    r'\1\2', data.url))
            if skip_malformed_results and is_malformed_url(clean_url):
                continue
            if clean_url != data.url:
                data = data._replace(url=clean_url)

            # TODO: repeat captures have a status code of `-` and a mime type
            # of `warc/revisit`. These can only be resolved by requesting the
            # content and following redirects. Maybe nice to do so
            # automatically here.
            data = data._replace(
                date=capture_time,
                raw_url=ARCHIVE_RAW_URL_TEMPLATE.format(
                    timestamp=data.timestamp, url=data.url),
                view_url=ARCHIVE_VIEW_URL_TEMPLATE.format(
                    timestamp=data.timestamp, url=data.url)
            )
            count += 1
            yield data

        return count

    def list_versions(self, url, *,
                      from_date=None, to_date=None, skip_repeats=True,
                      cdx_params=None):
        """
        Search archive.org for captures of a URL (optionally, within a time span).

        This function provides a convenient, use-case-specific interface to
        archive.org's CDX API. For a more direct, low-level API, use
        :func:`search_cdx`.

        Note that even URLs without wildcards may return results with multiple
        URLs. Search results are matched by url_key, which is a SURT-formatted,
        canonicalized URL:

        * Does not differentiate between HTTP and HTTPS
        * Is not case-sensitive
        * Treats `www.` and `www*.` subdomains the same as no subdomain at all

        Parameters
        ----------
        url : string
            The URL to list versions for. Can contain wildcards.
        from_date : datetime, optional
            Get versions captured after this date.
        to_date : datetime, optional
            Get versions captured before this date.
        skip_repeats : boolean, optional
            Don’t include consecutive captures of the same content (default: True).
        cdx_params : dict, optional
            Additional options to pass directly to the CDX API when querying.

        Raises
        ------
        UnexpectedResponseFormat
            If the CDX response was not parseable.
        ValueError
            If there were no versions of the given URL.

        Examples
        --------
        Grab the datetime and URL of the first nasa.gov snapshot.

        >>> with WaybackClient() as client:
        >>>     versions = client.list_versions('nasa.gov')
        >>>     version = next(versions)
        >>>     version.date
        datetime.datetime(1996, 12, 31, 23, 58, 47)
        >>>     version.raw_url
        "http://web.archive.org/web/19961231235847id\_/http://www.nasa.gov:80/"

        Loop through all the snapshots.

        >>> for version in client.list_versions('nasa.gov'):
        ...     # do something
        """
        params = {'collapse': 'digest'}
        if cdx_params:
            params.update(cdx_params)
        params['url'] = url
        params['from_date'] = from_date
        params['to_date'] = to_date

        last_hashes = {}
        for version in self.search(**params):
            # TODO: make skip_repeats smarter so we can use it again: only
            # check & skip the hash if the mime_type is not `warc/revisit`,
            # `unk`, `-` or `` and status_code is not `3xx`, `-` or ``.
            # Possible betterment: CAN skip warc/revisits with same hash IF
            # previous version of this URL with the same hash was not a revisit
            # and was not a 3xx status code.
            # TODO: may want to follow redirects and resolve them in the future
            if not skip_repeats or last_hashes.get(version.url) != version.digest:
                last_hashes[version.url] = version.digest
                # TODO: yield the whole version
                yield version

        if not last_hashes:
            raise ValueError("Internet archive does not have archived "
                             "versions of {}".format(url))

    # TODO: make this nicer by taking an optional date, so `url` can be a
    # memento url or an original URL + plus date and we'll compose a memento
    # URL.
    # TODO: add optional argument for `find_closest=False`? This would allow
    # get_memento() to return a different memento than the requested one if the
    # requested one isn't playback-able. This could also be a different method.
    # TODO: for generic use, needs to be able to return the memento itself if
    # the memento was a redirect (different than allowing a nearby-in-time
    # memento of the same URL, which would be the above argument). Probably
    # call this `follow_redirects=True`?
    def get_memento(self, url, redirect_target_window=12 * 60 * 60):
        """
        Fetch a memento from the Wayback Machine. This retrieves the content
        that was ultimately returned from a memento, following any redirects
        that were present at the time the memento was captured. (That is, if
        `http://example.com/a` redirected to `http://example.com/b`, this
        returns the memento for `/b` when you request `/a`.)

        Parameters
        ----------
        url : string
            URL of memento in Wayback (e.g.
            `http://web.archive.org/web/20180816111911id_/http://www.nws.noaa.gov/sp/`)
        redirect_taget_window : int, optional
            If the memento is of a redirect, allow up to this many seconds
            between the capture of the redirect and the capture of the target
            URL. (Note this does NOT apply when the originally requested
            memento didn't exist and wayback redirects to the next-closest-in-
            -time one. That will always raise a MementoPlaybackError.)
            Defaults to 43,200 (12 hours).

        Returns
        -------
        dict : requests.Response
            An HTTP response with the content of the memento, including a
            history of any redirects involved.
        """
        with utils.rate_limited(calls_per_second=30, group='get_memento'):
            # Correctly following redirects is actually pretty complicated. In
            # the simplest case, a memento is a simple web page, and that's
            # no problem. However...
            #   1.  If the response was a >= 400 status, we have to determine
            #       whether that status is coming from the memento or from the
            #       the Wayback Machine itself.
            #   2.  If the response was a 3xx status (a redirect) we have to
            #       determine the same thing, but it's a little more complex...
            #       a) If the redirect *is* the memento, its target may be an
            #          actual memento (see #1) or it may be a redirect (#2).
            #          The targeted URL is frequently captured anywhere from
            #          the same second to a few hours later, so it is likely
            #          the target will result in case 2b (below).
            #       b) If there is no memento for the requested time, but there
            #          are mementos for the same URL at another time, Wayback
            #          *may* redirect to that memento.
            #          - If this was on the original request, that's *not* ok
            #            because it means we're getting a different memento
            #            than we asked for.
            #          - If the redirect came from a URL that was the target of
            #            of a memento redirect (2a), then this is expected.
            #            Before following the redirect, though, we first sanity
            #            check it to make sure the memento we are redirecting
            #            to actually came from nearby in time (sometimes
            #            Wayback will redirect to captures *months* away).
            history = []
            urls = set()
            previous_was_memento = False
            orginal_url, original_date = memento_url_data(url)
            response = self.session.request('GET', url, allow_redirects=False)
            while True:
                is_memento = 'Memento-Datetime' in response.headers

                if not is_memento:
                    # If handling a reponse the original memento redirected to,
                    # a non-memento redirect may be ok. The target URL will
                    # rarely have been captured at the same time. (See 2b)
                    playable = False
                    if previous_was_memento and response.next:
                        current_url = original_url_for_memento(response.url)
                        target_url, target_date = memento_url_data(response.next.url)
                        if current_url.casefold() == target_url.casefold() and abs(target_date - original_date).seconds <= redirect_target_window:
                            playable = True

                    if not playable:
                        message = response.headers.get('X-Archive-Wayback-Runtime-Error')
                        if message:
                            raise MementoPlaybackError(f'Memento at {url} could not be played: {message}')
                        elif response.ok:
                            raise MementoPlaybackError(f'Memento at {url} could not be played')
                        else:
                            response.raise_for_status()

                if response.next:
                    previous_was_memento = is_memento
                    urls.add(response.url)
                    # Wayback sometimes has circular memento redirects ¯\_(ツ)_/¯
                    if response.next.url in urls:
                        raise MementoPlaybackError(f'Memento at {url} is circular')

                    history.append(response)
                    response = self.session.send(response.next, allow_redirects=False)
                else:
                    break

            response.history = history
            return response

    def timestamped_uri_to_version(self, dt, uri, *, url,
                                   maintainers=None, tags=None, view_url=None):
        """
        Fetch version content and combine it with metadata to build a Version.

        Parameters
        ----------
        dt : datetime.datetime
            capture time
        uri : string
            URI of version
        url : string
            page URL
        maintainers : list of string, optional
            Entities responsible for maintaining the page, as a list of strings
        tags : list of string, optional
            Any arbitrary "tags" to apply to the page for categorization
        view_url : string, optional
            The archive.org URL for viewing the page (with rewritten links, etc.)

        Returns
        -------
        dict : Version
            suitable for passing to :class:`Client.add_versions`
        """
        res = self.get_memento(uri)
        version_hash = utils.hash_content(res.content)
        title = utils.extract_title(res.content)
        content_type = (res.headers['content-type'] or '').split(';', 1)

        # Get all headers from original response
        prefix = 'X-Archive-Orig-'
        original_headers = {
            k[len(prefix):]: v for k, v in res.headers.items()
            if k.startswith(prefix)
        }

        redirected_url = None
        redirects = None
        if res.url != uri:
            redirected_url = original_url_for_memento(res.url)
            redirects = list(map(
                lambda response: original_url_for_memento(response.url),
                res.history))
            redirects.append(redirected_url)

        return format_version(url=url, dt=dt, uri=uri,
                              version_hash=version_hash, title=title,
                              tags=tags, maintainers=maintainers,
                              status=res.status_code,
                              mime_type=content_type[0], encoding=res.encoding,
                              headers=original_headers, view_url=view_url,
                              redirected_url=redirected_url,
                              redirects=redirects)


def format_version(*, url, dt, uri, version_hash, title, status, mime_type,
                   encoding, maintainers=None, tags=None, headers=None,
                   view_url=None, redirected_url=None, redirects=None):
    """
    Format version info in preparation for submitting it to web-monitoring-db.

    Parameters
    ----------
    url : string
        page URL
    dt : datetime.datetime
        capture time
    uri : string
        URI of version
    version_hash : string
        sha256 hash of version content
    title : string
        primer metadata (likely to change in the future)
    status : int
        HTTP status code
    mime_type : string
        Mime type of HTTP response
    encoding : string
        Character encoding of HTTP response
    maintainers : list of string, optional
        Entities responsible for maintaining the page, as a list of strings
    tags : list of string, optional
        Any arbitrary "tags" to apply to the page for categorization
    headers : dict, optional
        Any relevant HTTP headers from response
    view_url : string, optional
        The archive.org URL for viewing the page (with rewritten links, etc.)
    redirected_url : string, optional
        If getting `url` resulted in a redirect, this should be the URL
        that was ultimately redirected to.
    redirects : sequence, optional
        If getting `url` resulted in any redirects this should be a sequence
        of all the URLs that were retrieved, starting with the originally
        requested URL and ending with the value of the `redirected_url` arg.

    Returns
    -------
    version : dict
        properly formatted for as JSON blob for web-monitoring-db
    """
    # The reason that this is a function, not just dict(**kwargs), is that we
    # have to scope information that is not part of web-monitoring-db's Version
    # format into source_metadata, a free-form object for extra info that not
    # all sources are required to provide.
    metadata = {
        'status_code': status,
        'mime_type': mime_type,
        'encoding': encoding,
        'headers': headers or {},
        'view_url': view_url
    }

    if status >= 400:
        metadata['error_code'] = status

    if redirected_url:
        metadata['redirected_url'] = redirected_url
        metadata['redirects'] = redirects

    return dict(
         page_url=url,
         page_maintainers=maintainers,
         page_tags=tags,
         title=title,
         capture_time=dt.isoformat(),
         uri=uri,
         version_hash=version_hash,
         source_type='internet_archive',
         source_metadata=metadata
    )
