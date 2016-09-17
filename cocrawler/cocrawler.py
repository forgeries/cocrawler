'''
The actual web crawler
'''

import cgi
import urllib.parse
import json
import time
import os
from functools import partial
import pickle
from collections import defaultdict
from operator import itemgetter
import random

import asyncio
from concurrent.futures import ProcessPoolExecutor
import logging
import aiohttp
import aiohttp.resolver
import aiohttp.connector

import pluginbase

import stats
import seeds
import datalayer
import robots
import parse
import fetcher
import useragent

LOGGER = logging.getLogger(__name__)

__version__ = '0.01'

# aiohttp.ClientReponse lacks this method, so...
def is_redirect(response):
    return response.status in (300, 301, 302, 303, 307)

class Crawler:
    def __init__(self, loop, config, load=None):
        self.config = config
        self.loop = loop
        self.executor = ProcessPoolExecutor(2) # XXX config me
        self.stopping = 0

        self.robotname, self.ua = useragent.useragent(config, __version__)

        ns = config['Fetcher'].get('Nameservers')
        if ns:
            resolver = aiohttp.resolver.AsyncResolver(nameservers=ns)
        else:
            resolver = None

        proxy = config['Fetcher'].get('ProxyAll')
        if proxy:
            raise ValueError('proxies not yet supported')

        local_addr = config['Fetcher'].get('LocalAddr')
        # XXX if it's a list, make up an array of TCPConnecter objects, and rotate
        # XXX save the kwargs in case we want to make a ProxyConnector deeper down
        conn_kwargs = {'use_dns_cache': True, 'resolver': resolver}
        if local_addr:
            conn_kwargs['local_addr'] = local_addr
        conn = aiohttp.connector.TCPConnector(**conn_kwargs)

        self.connector = conn
        # can use self.session.connector to get the connectcor back ... connector.cached_hosts ...
        self.session = aiohttp.ClientSession(loop=loop, connector=conn,
                                             headers={'User-Agent': self.ua})

        # queue.PriorityQueue has no concept of 'ride along' data. Sigh.
        self.q = asyncio.PriorityQueue(loop=self.loop)
        self.ridealong = {}
        self.ridealongmaxid = 1

        self.datalayer = datalayer.Datalayer(config)
        self.robots = robots.Robots(self.robotname, self.session, self.datalayer, config)
        self.jsonlogfile = config['Logging'].get('Crawllog')
        if self.jsonlogfile:
            self.jsonlogfd = open(self.jsonlogfile, 'a')
        else:
            self.jsonlogfd = None
        self.rejectedaddurl = config['Logging'].get('LogRejectedAddUrl')
        if self.rejectedaddurl:
            self.rejectedaddurlfd = open(self.rejectedaddurl, 'a')
        else:
            self.rejectedaddurlfd = None

        if load is not None:
            self.load_all(load)
            LOGGER.info('after loading saved state, work queue is %r urls', self.q.qsize())
        else:
            self._seeds = seeds.expand_seeds(self.config.get('Seeds', {}))
            for s in self._seeds:
                self.add_url(1, s, seed=True)
            LOGGER.info('after adding seeds, work queue is %r urls', self.q.qsize())
            stats.stats_max('initial seeds', self.q.qsize())

        self.plugin_base = pluginbase.PluginBase(package='cocrawler.plugins')
        plugins_path = config.get('Plugins', {}).get('Path', [])
        fix_plugin_path = partial(os.path.join, os.path.abspath(os.path.dirname(__file__)))
        plugins_path = [fix_plugin_path(x) for x in plugins_path]
        self.plugin_source = self.plugin_base.make_plugin_source(searchpath=plugins_path)
        self.plugins = {}
        for plugin_name in self.plugin_source.list_plugins():
            if plugin_name.startswith('test_'):
                continue
            plugin = self.plugin_source.load_plugin(plugin_name)
            plugin.setup(self, config)
        LOGGER.info('Installed plugins: %s', ','.join(sorted(list(self.plugins.keys()))))

        self.max_workers = int(self.config['Crawl']['MaxWorkers'])
        self.remaining_url_budget = self.config['Crawl'].get('MaxCrawledUrls')
        # XXX surely there's a less ugly way to do the following:
        if self.remaining_url_budget is not None:
            self.remaining_url_budget = int(self.remaining_url_budget)
        self.awaiting_work = 0

        LOGGER.info('Touch ~/STOPCRAWLER.%d to stop the crawler.', os.getpid())

    @property
    def seeds(self):
        return self._seeds

    @property
    def qsize(self):
        return self.q.qsize()

    def register_plugin(self, name, plugin_function):
        self.plugins[name] = plugin_function

    def log_rejected_add_url(self, url):
        if self.rejectedaddurlfd:
            print(url, file=self.rejectedaddurlfd)

    def add_url(self, priority, url, seed=False, seedredirs=None):
        # XXX canonical plugin here?
        # XXX learnings from Django https://github.com/django/django/blob/master/django/utils/http.py#L287
        #   urlparse screwup: reject startswith('///')...
        #   ??? reject scheme without netloc http:///foo ??? at least test that urljoin() does the right thing
        #   chrome: reject starting with control chars.
        #   strip whitespace. what about interior not-url-encoded whitespace?
        #  seems that Chrome is a lot more permissive than other browsers :/
        url, _ = urllib.parse.urldefrag(url) # drop the frag
        if '://' not in url: # will happen for seeds
            if ':' in url:
                return # things like mailto: ...
            url = 'http://' + url
        # drop meaningless cgi args?
        # uses HSTS to upgrade to https:
        #https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json
        # use HTTPSEverwhere? would have to have a fallback if https failed

        # XXX optionally generate additional urls plugin here
        # e.g. any amazon url with an AmazonID should add_url() the base product page

        # XXX allow/deny plugin modules go here
        # seen url - could also be "seen recently enough"

        if priority > int(self.config['Crawl']['MaxDepth']):
            stats.stats_sum('rejected by MaxDepth', 1)
            self.log_rejected_add_url(url)
            return
        if self.datalayer.seen_url(url):
            stats.stats_sum('rejected by seen_urls', 1)
            self.log_rejected_add_url(url)
            return
        if not seed and not self.plugins['url_allowed'](url):
            LOGGER.debug('url %r was rejected by url_allow.', url)
            stats.stats_sum('rejected by url_allowed', 1)
            self.log_rejected_add_url(url)
            return
        # end allow/deny plugin

        LOGGER.debug('actually adding url %r', url)
        stats.stats_sum('added urls', 1)

        work = {'url': url, 'priority': priority}
        if seed:
            work['seed'] = True
        self.ridealong[str(self.ridealongmaxid)] = work

        # to randomize fetches, and sub-prioritize embeds
        if work.get('embed'):
            rand = 0.0
        else:
            rand = random.uniform(0, 0.99999)

        self.q.put_nowait((priority, rand, str(self.ridealongmaxid)))
        self.ridealongmaxid += 1

        self.datalayer.add_seen_url(url)
        return 1

    def close(self):
        stats.report()
        parse.report()
        stats.check(self.config)
        stats.coroutine_report()
        self.session.close()
        if self.jsonlogfd:
            self.jsonlogfd.close()
        if self.q.qsize():
            LOGGER.error('non-zero exit qsize=%d', self.q.qsize())
            stats.exitstatus = 1

    async def fetch_and_process(self, work):
        '''
        Fetch and process a single url.
        '''
        priority, rand, ra = work
        work = self.ridealong[ra]
        url = work['url']
        tries = work.get('tries', 0)
        maxtries = self.config['Crawl']['MaxTries']

        parts = urllib.parse.urlparse(url)
        headers, proxy, mock_url, mock_robots = fetcher.apply_url_policies(url, parts, self.config)

        with stats.coroutine_state('fetching/checking robots'):
            r = await self.robots.check(url, parts, headers=headers, proxy=proxy, mock_robots=mock_robots)
        if not r:
            # XXX there are 2 kinds of fail, no robots data and robots denied. robotslog has the full details.
            # XXX treat 'no robots data' as a soft failure?
            # XXX log more particular robots fail reason here
            json_log = {'type':'get', 'url':url, 'priority':priority, 'status':'robots', 'time':time.time()}
            if self.jsonlogfd:
                print(json.dumps(json_log, sort_keys=True), file=self.jsonlogfd)
            del self.ridealong[ra]
            return

        # XXX response.release asap. btw response.text does one for you
        f = await fetcher.fetch(url, parts, self.session, self.config,
                                headers=headers, proxy=proxy, mock_url=mock_url)

        json_log = {'type':'get', 'url':url, 'priority':priority,
                    't_first_byte':f.t_first_byte, 'time':time.time()}
        if tries:
            json_log['retry'] = tries

        if f.last_exception is not None or f.response.status >= 500:
            tries += 1
            if tries > maxtries:
                # XXX jsonlog
                # XXX remember that this host had a fail
                stats.stats_sum('tries completely exhausted', 1)
                del self.ridealong[ra]
                return
            # XXX jsonlog this soft fails
            work['tries'] = tries
            work['priority'] = priority
            self.ridealong[ra] = work
            self.q.put_nowait((priority, rand, ra))
            return

        del self.ridealong[ra]

        json_log['status'] = f.response.status

        # PLUGIN: post_crawl_raw(header_bytes, body_bytes, response.status, time.time())
        # for example, add to a WARC, or post to a Kafka queue

        if is_redirect(f.response):
            headers = f.response.headers
            location = f.response.headers.get('location')
            next_url = urllib.parse.urljoin(url, location)
            priority += 1

            # XXX make sure it didn't redirect to itself
            # (although some hosts redir to themselves while setting cookies)
            # XXX need surt-surt comparison and seen_url check

            json_log['redirect'] = next_url

            kwargs = {}
            if 'seed' in work:
                if 'seedredirs' in work:
                    work['seedredirs'] += 1
                else:
                    work['seedredirs'] = 1
                if work['seedredirs'] > 2: # XXX make a policy option
                    del work['seed']
                    del work['seedredirs']
                else:
                    kwargs['seed'] = work['seed']
                    kwargs['seedredirs'] = work['seedredirs']
                    priority -= 1 # XXX make a policy option
                    json_log['seedredirs'] = work['seedredirs']

            if self.add_url(priority+1, next_url, **kwargs):
                json_log['found_new_links'] = 1
            # fall through to release and json logging

        # if 200, parse urls out of body
        if f.response.status == 200:
            headers = f.response.headers
            content_type = f.response.headers.get('content-type')
            if content_type:
                content_type, _ = cgi.parse_header(content_type)
            else:
                content_type = 'Unknown'
            LOGGER.debug('url %r came back with content type %r', url, content_type)
            json_log['content_type'] = content_type
            stats.stats_sum('content-type=' + content_type, 1)
            # PLUGIN: post_crawl_200 by content type
            if content_type == 'text/html':
                try:
                    with stats.record_burn('response.text() decode', url=url):
                        body = await f.response.text() # do not use encoding found in the headers -- policy
                        # XXX consider using 'ascii' for speed, if all we want to do is regex in it
                except UnicodeDecodeError:
                    # XXX if encoding was in header, maybe I should use it?
                    # XXX can get additional exceptions here, broken tcp connect etc. see list in fetcher
                    body = f.body_bytes.decode(encoding='utf-8', errors='replace')

                # PLUGIN post_crawl_200_find_urls -- links and/or embeds
                # should have an option to run this in a separate process or fork,
                #  so as to not cpu burn in the main process
                #urls, _ = parse.find_html_links(body, url=url)
                urls, _ = await parse.find_html_links_async(body, self.executor, self.loop, url=url)
                LOGGER.debug('parsing content of url %r returned %r links', url, len(urls))
                json_log['found_links'] = len(urls)
                stats.stats_max('max urls found on a page', len(urls))

                new_links = 0
                for u in urls:
                    new_url = urllib.parse.urljoin(url, u)
                    if self.add_url(priority + 1, new_url): # XXX if embed, priority - 1
                        new_links += 1
                if new_links:
                    json_log['found_new_links'] = new_links
                # XXX plugin for links and new links - post to Kafka, etc
                LOGGER.debug('size of work queue now stands at %r urls', self.q.qsize())
                stats.stats_max('max queue size', self.q.qsize())

        await f.response.release()
        if self.jsonlogfd:
            print(json.dumps(json_log, sort_keys=True), file=self.jsonlogfd)

    async def work(self):
        '''
        Process queue items until we run out.
        '''
        try:
            while True:
                try:
                    work = self.q.get_nowait()
                except asyncio.queues.QueueEmpty:
                    # this is racy with the test for all workers awaiting.
                    # putting it here makes sure the race is rarely run.
                    self.awaiting_work += 1
                    work = await self.q.get()
                    self.awaiting_work -= 1
                await self.fetch_and_process(work)
                self.q.task_done()

                if self.stopping:
                    raise asyncio.CancelledError

                if self.remaining_url_budget is not None:
                    self.remaining_url_budget -= 1
                    if self.remaining_url_budget <= 0:
                        raise asyncio.CancelledError

        except asyncio.CancelledError:
            pass

    def save(self, f):
        # XXX make this more self-describing
        pickle.dump('Put the XXX header here', f) # XXX date, conf file name, conf file checksum
        pickle.dump(self.ridealongmaxid, f)
        pickle.dump(self.ridealong, f)
        pickle.dump(self._seeds, f)
        count = self.q.qsize()
        pickle.dump(count, f)
        for _ in range(0, count):
            entry = self.q.get_nowait()
            pickle.dump(entry, f)

    def load(self, f):
        header = pickle.load(f) # XXX check that this is a good header... log it
        self.ridealongmaxid = pickle.load(f)
        self.ridealong = pickle.load(f)
        self._seeds = pickle.load(f)
        # XXX load seeds
        self.q = asyncio.PriorityQueue(loop=self.loop)
        count = pickle.load(f)
        for _ in range(0, count):
            entry = pickle.load(f)
            self.q.put_nowait(entry)

    def get_savefilename(self):
        savefile = self.config['Save'].get('Name', 'cocrawler-save-$$')
        savefile = savefile.replace('$$', str(os.getpid()))
        savefile = os.path.expanduser(os.path.expandvars(savefile))
        if os.path.exists(savefile) and not self.config['Save'].get('Overwrite'):
            count = 1
            while os.path.exists(savefile + '.' + str(count)):
                count += 1
            savefile = savefile + '.' + str(count)
        return savefile

    def save_all(self):
        savefile = self.get_savefilename()
        with open(savefile, 'wb') as f:
            self.save(f)
            self.datalayer.save(f)
            stats.save(f)

    def load_all(self, filename):
        with open(filename, 'rb') as f:
            self.load(f)
            self.datalayer.load(f)
            stats.load(f)

    def summarize(self):
        '''
        Print a human-readable summary of what's in the queues
        '''
        print('{} items in the crawl queue'.format(self.q.qsize()))
        print('{} items in the ridealong dict'.format(len(self.ridealong)))
        urls_with_tries = 0
        priority_count = defaultdict(int)
        netlocs = defaultdict(int)
        for k,v in self.ridealong.items():
            if 'tries' in v:
                urls_with_tries += 1
            priority_count[v['priority']] += 1
            url = v['url']
            parts = urllib.parse.urlparse(url)
            netlocs[parts.netloc] += 1
        print('{} items in crawl queue are retries'.format(urls_with_tries))
        print('{} different hosts in the queue'.format(len(netlocs)))
        print('Queue counts by priority:')
        for p in sorted(list(priority_count.keys())):
            if priority_count[p] > 0:
                print('  {}: {}'.format(p, priority_count[p]))
        print('Queue counts for top 10 netlocs')
        netloc_order = sorted(netlocs.items(), key=itemgetter(1), reverse=True)[0:10]
        for k,v in netloc_order:
            print('  {}: {}'.format(k, v))

    async def crawl(self):
        '''
        Run the crawler until it's out of work
        '''
        workers = [asyncio.Task(self.work(), loop=self.loop) for _ in range(self.max_workers)]

        # this is now the 'main' coroutine

        while True:
            await asyncio.sleep(1)

            if os.path.exists(os.path.expanduser('~/STOPCRAWLER.{}'.format(os.getpid()))):
                LOGGER.warning('saw STOPCRAWLER file, stopping crawler and saving queues')
                self.stopping = 1

            workers = [w for w in workers if not w.done()]
            LOGGER.debug('%d workers remain', len(workers))
            if len(workers) == 0:
                LOGGER.warning('all workers exited, finishing up.')
                break

            print('checking to see if awaiting {} equals workers {}'.format(self.awaiting_work, len(workers)))
            if self.awaiting_work == len(workers) and self.q.qsize() == 0:
                # this is a little racy with how awaiting work is set and the queue is read
                # while we're in this join we aren't looking for STOPCRAWLER etc
                LOGGER.warning('all workers appear idle, executing join')
                await self.q.join()
                break

            stats.coroutine_report()

            # XXX clear the DNS cache every few hours; currently the
            # in-memory one is kept for the entire crawler run

        for w in workers:
            if not w.done():
                w.cancel()

        if self.stopping or self.config['Save'].get('SaveAtExit'):
            self.summarize()
            self.datalayer.summarize()
            LOGGER.warning('saving datalayer and queues')
            self.save_all()
            LOGGER.warning('saving done')
