#    Readable Feeds
#    Copyright (C) 2009  Andrew Trusty (http://andrewtrusty.com)
#
#    This file is part of Readable Feeds.
#
#    Readable Feeds is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Readable Feeds is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Readable Feeds.  If not, see <http://www.gnu.org/licenses/>.

__all__ = ['ReadabilityHandler', 'ReadabilityFeedHandler',
           'FeedsQueueHandler', 'EntriesQueueHandler', 'CreateFeedHandler',
           'TransferHandler', 'UrlCacheCronHandler']


import time
import hashlib
import logging
import pickle
from datetime import datetime, timedelta

from gae_utils import *
import jinja2

import hn
import feedparser

from google.appengine.ext import db
from google.appengine.api.urlfetch_errors import DownloadError
from google.appengine.api.labs import taskqueue
from google.appengine.api import memcache as gae_memcache
from google.appengine.runtime.apiproxy_errors import OverQuotaError, DeadlineExceededError
from google.appengine.api import mail
from google.appengine.api import urlfetch


from appengine_utilities.cache import Cache as GAEUCache

CACHE_TIME = 3600 * 24 * 5
QUEUE_NUM = '2'
USER_AGENT = 'Readable-Feeds (http://andrewtrusty.appspot.com/readability/)'
FETCH_TIMEOUT = 7.0 # 4 was causing urlfetch to fail a lot.. 8 worked

# cache wrapper to set namespace and expires
# switch to double-backed cache so i can handle memcache failures
class Cache(object):
    def __init__(self, expires=CACHE_TIME, namespace='readable-feeds'):
        # TODO: use namespace..
        self.cache = GAEUCache(default_timeout = expires)

    def set(self, k, v):
        self.cache[k] = v

    def get(self, k):
        try:
            return self.cache[k]
        except KeyError:
            return None

    def get_multi(self, ks):
        data = []
        for k in ks:
            d = self.get(k)
            if d: # XXX: do i really want to check? or leave it up to caller to handle..
                data.append(d)
        return data

from google.appengine.ext.db import stats
#import random

class UrlCache(db.Model):
    url = db.StringProperty(required=True) # max 500
    hits = db.IntegerProperty(default=0, indexed=False)
    created = db.DateTimeProperty(auto_now_add=True, indexed=False)
    data = db.BlobProperty()
    timeout = db.DateTimeProperty() # set but unused currently

    @classmethod
    def some_expired(cls):
        return UrlCache.all().filter('timeout <', datetime.now()).count(1)

    @classmethod
    def cleanup(cls):
        if cls.some_expired():
            db.delete(UrlCache.all().filter('timeout <', datetime.now()).fetch(500))
            #db.delete(UrlCache.all().filter('timeout <', datetime.now()).fetch(500))
        return

        # TODO: below is failing.., does above fail?
        """
        size_cutoff = 500000000 # 500 Mb
        bytes_used = stats.GlobalStat.all().order('-timestamp').get().bytes

        if bytes_used > size_cutoff and cls.some_expired():
            def delete_n(n=500):
                # delete expired caches starting from the ones created first
                # but can't do .order('created') b/c have to use same attr as the filter!
                db.delete(UrlCache.all().filter('timeout <',
                                                datetime.now()).fetch(n))
                # much faster to use above, max 500 per delete

                #expired = UrlCache.all().filter('timeout <',
                #                                datetime.now()).fetch(n)
                #for e in expired:
                #    e.delete()

            delete_n() # assume it takes 10 seconds, so we do it max twice per request
            if cls.some_expired(): delete_n()

            # TODO: if necessary, add last_hit datetime to cache
            #     then we can delete least recently used first

            # TODO: send email to me when the cleanup doesn't remove enough..
        """


class CustomCache(object):
    def __init__(self, expires=CACHE_TIME, namespace='readable-feeds'):
        self.expires = expires
        self.namespace = namespace

    # need to hash b/c some urls are too long to put in 500 byte StringProperty
    # for debugging, only hash long strings
    def store_key(self, k):
        # to preserve cache & not break on long keys
        # memcache max is 250 i think, though GAE docs don't mention this..
        if k.startswith('http') or len(k) > 200:
            return hashlib.sha1(k).hexdigest()
        else:
            return str(k)

    def get_from_store(self, k):
        data = None
        cache = db.GqlQuery("SELECT * FROM UrlCache WHERE url = :1", k).get()
        if cache:
            cache.hits += 1
            # reset the expires timeout
            cache.timeout = datetime.now() + timedelta(0, self.expires)
            cache.put()
            data = pickle.loads(cache.data)
            self.set(k, data, use_store=False) # put back in memory cache
        return data

    def save_to_store(self, k, v, expires):
        timeout = datetime.now() + timedelta(0, expires)
        cache = UrlCache(url=k, data=pickle.dumps(v), timeout=timeout) # not using namespace..
        cache.put()

    def set(self, k, v, expires=None, use_store=True):
        key = self.store_key(k)
        if not expires:
            expires = self.expires
        gae_memcache.set(key, v, expires, namespace=self.namespace)
        if use_store:
            # should not already exist or this will throw an error..
            self.save_to_store(key, v, expires)

    def get(self, k):
        key = self.store_key(k)
        return gae_memcache.get(key, namespace=self.namespace) or self.get_from_store(key)

    def get_multi(self, ks, use_store=True, as_dict=False):
        keys = [self.store_key(k) for k in ks]
        data_dict = gae_memcache.get_multi(keys, namespace=self.namespace)
        data = None
        if as_dict:
            for k in keys:
                if not data_dict.has_key(k) and use_store:
                    item = self.get_from_store(k)
                    if item:
                        data_dict[k] = item
            return data_dict
        else:
            # now order them correctly
            data = []
            for k in keys:
                if data_dict.has_key(k):
                    data.append(data_dict[k])
                elif use_store:
                    item = self.get_from_store(k)
                    if item:
                        data.append(item)
            return data

    def delete_multi(self, ks, use_store=True):
        keys = [self.store_key(k) for k in ks]
        if keys:
            result = gae_memcache.delete_multi(keys, namespace=self.namespace)
            if use_store:
                assert False, 'deleting from data store not supported yet..'
            return result
    def delete(self, k, use_store=True):
        result = gae_memcache.delete(self.store_key(k), namespace=self.namespace)
        if use_store:
            assert False, 'deleting from data store not supported yet..'
        return result

    def incr(self, k, delta=1, namespace=None, initial_value=None):
        key = self.store_key(k)
        return gae_memcache.incr(k, delta=delta,
                                 namespace=namespace or self.namespace, initial_value=initial_value)
    def decr(self, k, delta=1, namespace=None, initial_value=None):
        key = self.store_key(k)
        return gae_memcache.decr(k, delta=delta,
                                 namespace=namespace or self.namespace, initial_value=initial_value)

#memcache = Cache()
memcache = CustomCache()
# need to run now in dev to test

class Feed2(db.Model):
    # Ket == URL
    url = db.StringProperty(required=True)
    hits = db.IntegerProperty(default=1)
    created = db.DateTimeProperty(auto_now_add=True)
    title = db.StringProperty(default='', indexed=False)

    def feed_url(self):
        return '/readability/feed?url=%s' % urlquote(self.url)


class ReadabilityHandler(RenderHandler):
    def get(self):
        args = utils.storage()

        args.top_feeds = Feed2.all().order('-hits').fetch(10)
        args.newest_feeds = Feed2.all().order('-created').fetch(10)
        args.url = 'http://andrewtrusty.appspot.com/readability/'
        args.title = 'Readable Feeds'

        self.render('readability.index', args)


def get_update_args(req):
    show_original = req.get('original')
    show_linked = req.get('linked') or True
    # pull/show/embed_links = self.request.get('link_depth') # [0,3]
    # dl/fetch/retrieve
    retrieve_links = max(0, min(3, int(req.get('retrieve_links') or 0)))
    feed_url = req.get('url')

    return dict(show_original=show_original,
                retrieve_links=retrieve_links,
                url=feed_url,
                show_linked=show_linked)


class FeedNotFound(Exception):
    pass

def parse_feed(data, feed_url): # accepts url, stream or string
    parsed_feed = feedparser.parse(data)
    if not parsed_feed:
        raise FeedNotFound("parsed to None")

    # test if its a feed by trying to get the title & link
    # assuming that if i get title, link & subtitle its a valid feed
    try:
        title = parsed_feed.feed.title
    except:
        raise FeedNotFound("missing title")
    try:
        link = parsed_feed.feed.link
    except:
        raise FeedNotFound("missing link")

    description = ''
    if hasattr(parsed_feed.feed, 'subtitle'):
        description = parsed_feed.feed.subtitle

    return dict(title=title,
                link=link,
                description=description,
                items=parsed_feed.entries)


def get_fetch_rpc_handler(rpc, feed_args, rpcs, feeds, proxied=False):
    def handle_fetch_rpc():
        feed_url = feed_args['url']
        del feed_args['url']

        got_exc = True
        afeed = {}
        try:
            result = rpc.get_result()
            if result.status_code != 200:
                raise FeedNotFound('bad status code: %s' % result.status_code)

            afeed = parse_feed(result.content, feed_url)
            got_exc = False

        except Exception, e:
            # DownloadError, FeedNotFound error or
            #     encoding exception.. that EUC-TW or what not codec not supported by python
            memcache.set('delay-'+feed_url, 1, 6 * 3600, use_store=False)
            logging.error('feed fetch unknown exception w/ feed url: %s (%s)' % (feed_url, e))

        if got_exc:
            # TODO: delay fetching of this feed_url for some hours..
            return
        if not afeed:
            logging.error('afeed is not set for feed url: %s' % feed_url)
            return

        afeed['items'] = afeed['items'][:50]
        feeds.append( (feed_url, afeed) )

    return handle_fetch_rpc


def process_feed(feed_url, afeed, feed_args):
    logging.debug('process feed updating title for: %s' % feed_url)
    feed = Feed2.get_by_key_name(feed_url)
    feed.title = afeed['title']
    # TODO: update link..
    feed.put()

    logging.debug('process feed working on entries for: %s' % feed_url)

    elinks = []
    entries_queue = taskqueue.Queue(name='entries%s' % QUEUE_NUM)
    logging.debug('got queue object w/ %s items for feed_url: %s' % (len(afeed.get('items',[])), feed_url))
    for entry in afeed['items']: # already limited to 50 by the rpc fetch callback (to prevent DOS)
        elinks.append(entry.link)

        # skip already cached entries
        if memcache.get(entry.link):
            #logging.debug('skipping cached feed entry: %s' % entry.link)
            continue
        logging.debug('adding feed entry task: %s' % entry.link)

        task_url = '/readability/queue/entries%s' % QUEUE_NUM
        name = hashlib.sha1(entry.link.encode('utf-8')).hexdigest() # for uniqueness constraint

        comments = ''
        if hasattr(entry, 'comments'):
            comments = entry.comments
        description = ''
        if hasattr(entry, 'description'):
            description = entry.description

        memcache.set('description-'+entry.link, description)

        try:
            params = dict(entry_url=entry.link,
                          entry_title=entry.title,
                          entry_comments=comments)
                        #entry_description=description)
                        # moved to memcache due to size errors
            params.update(feed_args)
            entries_queue.add(taskqueue.Task(url=task_url, name=name,
                                             params=params))
        except taskqueue.TaskAlreadyExistsError:
            pass
        except taskqueue.TombstonedTaskError:
            pass
        except OverQuotaError:
            pass # ouch

    #logging.debug('feeds queue handler enqueued max %s items w/ feed url: %s' %
    #              (len(args['items']), feed_url))

    # set all the entry links so we know what to return when building the feed
    feed_data = dict(title=afeed['title'],
                     link=afeed['link'],
                     description=afeed['description'],
                     entry_keys=elinks,
                     last_update=time.time())
    #logging.debug('about to set feed_data for feed: %s' % feed_url)

    memcache.set(feed_url, feed_data)
    logging.debug('finished processing entries for feed: %s' % feed_url)




class FeedsQueueHandler(RenderHandler):
    def get(self):
        self.post()
    def post(self):
        #self.write('running... <br />')
        """
        1 - fetch all feeds that need to be updated
        2 - isssue asynchronous requests to grab them
        """
        time_key = self.request.get('time_key') or None
        logging.debug('running task for time key: %s' % time_key)
        #logging.debug('feeds queue handler running w/ time key: %s' % time_key)
        if not time_key:
            logging.error('missing time key argument')
            return

        num_feeds = memcache.get(time_key)
        if not num_feeds:
            # not an error b/c the current time_key triggers task for old time_key
            # TODO: setup a cron to trigger time_keys in case they don't get automatically triggered
            logging.debug('no feeds to update')
            return
        num_feeds = int(num_feeds)
        logging.debug('%s feeds to update for time key: %s' % (num_feeds, time_key))

        # XXX: limited -- The combined size of all values returned must not exceed 1 megabyte!
        feed_keys = [time_key+('-%s' % i)
                     for i in xrange(1, num_feeds+1)]
        feed_data = memcache.get_multi(feed_keys, use_store=False, as_dict=True)
        url2data = {}
        url2mkeys = {}

        rpcs = []
        feeds = []
        c = 0
        for mkey, fdata in feed_data.items():
            feed_url = fdata['url']
            url2mkeys.setdefault(feed_url,[]).append(mkey)
            if feed_url in url2data: continue
            url2data[feed_url] = fdata

            rpc = urlfetch.create_rpc(deadline=FETCH_TIMEOUT)
            rpc.callback = get_fetch_rpc_handler(rpc, fdata, rpcs, feeds)
            urlfetch.make_fetch_call(rpc, feed_url,
                                     headers={'User-agent':USER_AGENT}, follow_redirects=True)
            rpcs.append(rpc)
            c += 1

        # remove the data we don't need anymore
        #    instead we remove the data after its processed so Out of time errors don't
        #    can have the remaining tasks resumed in the next task try
        #memcache.delete_multi(feed_keys + [time_key], use_store=False)

        while rpcs:
            rpc = rpcs.pop(0)
            try:
                rpc.wait()
                # do some synchronous tasks
                while feeds:
                    afeed_url, afeed = feeds.pop(0)
                    try:
                        process_feed(afeed_url, afeed, url2data[afeed_url])
                        memcache.delete_multi(url2mkeys[afeed_url], use_store=False)
                    except DeadlineExceededError, e:
                        # we want the task to fail on this so it reruns
                        raise e
                    except Exception, e:
                        memcache.delete_multi(url2mkeys[afeed_url], use_store=False)
                        raise e

            except DeadlineExceededError, e:
                # we want the task to fail on this so it reruns
                raise e
            except Exception, e:
                logging.error('got exception while waiting for asynch feed rpc: %s' % e)

        memcache.delete(time_key, use_store=False)

        logging.debug('%s feeds updated for time key: %s' % (c, time_key))


# might want to make this handle more than one entry at a time..
#    or make the feed handler handly an entry or two too..
class EntriesQueueHandler(RenderHandler):
    def get(self):
        self.post()
    def post(self):
        #self.write('running... <br />')

        #logging.error('delete me!!!')
        #return

        entry_url = self.request.get('entry_url')
        entry_title = self.request.get('entry_title')
        entry_comments = self.request.get('entry_comments')
        #entry_description = self.request.get('entry_description')
        entry_description = memcache.get('description-'+entry_url)
        if entry_description is None:
            msg = 'description 2 memcache missing for: %s' % entry_url
            logging.error(msg)
            return

        update_args = get_update_args(self.request)
        del update_args['url'] # don't need for entry handler

        #logging.debug('entry queue handler running w/ url: %s' %
        #              (entry_url))

        if memcache.get(entry_url):
            return

        #user_agent = 'Mozilla/5.0 (Windows; U; Windows NT 5.1; en-US; rv:1.8.1.16) Gecko/20080702 Firefox/2.0.0.16'
        content = hn.upgradeEntry(entry_url, entry_description, **update_args) #, user_agent)


        # going in depth on links... gonna take a good bit of time
        # doing it in one task is probably doable..
        # kinda tricky to break up into multiple tasks.. but it would cache better..

        """
        self.write(entry_title + '<br />')
        self.write(entry_url + '<br />')
        self.write(content + '<br />')
        self.write(entry_comments + '<br />')
        """
        #logging.debug('entry queue handler upgraded link w/ url: %s' %
        #              (entry_url))

        # the building of it is going to be quite delayed if i create new tasks for each link..
        # and anyways, the # of tasks i have is kind of a scarce resource..

        entry_data = dict(title=entry_title,
                          link=entry_url,
                          content=content,
                          comments=entry_comments)

        memcache.set(entry_url, entry_data)

        #logging.debug('entry queue handler set cache w/ url: %s' %
        #              (entry_url))


def queue_feed_update(**params):
    # TODO: handle newly created feeds specially or just reduce interval if this is really efficient..
    time_sz = 60 * 30 # 900
    feed_url = params['url']

    #logging.debug('queue feed update called for: %s' % feed_url)
    old_feed_data = memcache.get(feed_url)
    to_delay = memcache.get('delay-'+feed_url)
    # check if we've already fetched the feed in the last 15 minutes
    if old_feed_data and time.time() - old_feed_data['last_update'] < time_sz or to_delay:
        #logging.debug('too soon to update feed: %s' % feed_url)
        return
    # try to prevent dogpiling
    if old_feed_data:
        old_feed_data['last_update'] = time.time()
        memcache.set(feed_url, old_feed_data)

    time_key_val = int(time.time()) / time_sz # unique for every 15 minutes
    time_key = 'feed-update-'+str(time_key_val)
    time_key_num = memcache.incr(time_key, initial_value=0)
    if not time_key_num:
        logging.error('failed to increment time key: %s' % time_key)
        return
    """
    from get_update_args(..)
        params is dict(show_original=show_original,
                       retrieve_links=retrieve_links,
                       url=feed_url,
                       show_linked=show_linked)
    """
    memcache.set(time_key+'-%s' % time_key_num, params,
                 expires=time_sz*3, use_store=False)
    logging.debug('added feed #%s for future update task w/ time key: %s' % (time_key_num, time_key))

    if time_key_num == 1:
        # run a task queue job for the previous time key
        task_url = '/readability/queue/feeds%s' % QUEUE_NUM
        feeds_queue = taskqueue.Queue(name='feeds%s' % QUEUE_NUM)
        # create unique name for task
        name = 'feed-update-task-%s' % (time_key_val - 1)
        task_time_key = 'feed-update-'+str(time_key_val - 1)
        try:
            feeds_queue.add(taskqueue.Task(url=task_url, name=name,
                                           params=dict(time_key=task_time_key)))
            #logging.debug('feed update task QUEUED for: %s' % name)
        except taskqueue.TaskAlreadyExistsError:
            logging.warning('task already exists: %s' % name)
        except taskqueue.TombstonedTaskError:
            logging.warning('tombstoned task: %s' % name)
        except OverQuotaError:
            logging.warning('over task quota error!: %s' % name)

        logging.debug('created feed update task for time key: %s' % task_time_key)


def create_feed(handler, **params):
    feed_url = params['url']
    if not feed_url.startswith('http') or '://' not in feed_url or feed_url == 'http://':
        handler.response.headers["Content-Type"] = "text/html; charset=UTF-8"
        handler.response.set_status(400)
        handler.response.out.write('bad website/feed url, please specify the full URL' +
                                '<br /><br /><a href="/readability/">&laquo; back</a>')
        return False

    queue_feed_update(**params)

    feed = Feed2.get_by_key_name(feed_url)

    if not feed:
        feed = Feed2(key_name=feed_url, url=feed_url)
        feed.put()

    return feed.feed_url()


# initiates feed creation & returns eventual url
# ajax interface
class CreateFeedHandler(RenderHandler):
    def post(self):
        update_args = get_update_args(self.request)
        readable_feed_url = create_feed(self, **update_args)

        if readable_feed_url:
            #self.response.headers["Content-Type"] = "text/plain; charset=UTF-8"
            #self.write(readable_feed_url)
            self.redirect(readable_feed_url)


# TODO: make feeds readable on my site.. extra content.. maybe bad legally..

# TODO: has_excerpt option where i detect the excerpt and look for the match in the page
#       so i can grab from the right place, eg. Slashdot

# shows the feed
class ReadabilityFeedHandler(RenderHandler):
    def get(self):
        self.response.headers["Content-Type"] = "text/xml; charset=UTF-8"

        update_args = get_update_args(self.request)
        feed_url = update_args['url']

        logging.debug('ReadabilityFeedHandler: %s' % feed_url)

        feed = Feed2.get_by_key_name(feed_url)

        if self.request.get('delete') == 'asgard': # make it slightly difficult to delete
            feed.delete()
            return

        if not feed:
            # not created yet, trigger the create & then show a blank feed
            readable_feed_url = create_feed(self, **update_args)
            if readable_feed_url:
                # return blank feed saying feed creation in process
                self.render('readability.populating', {'link':feed_url, 'title':None},
                            ext='.xml')
            return

        feed.hits += 1
        feed.put()

        feed_data = memcache.get(feed_url) # title, link, description, entry_keys
        if not feed_data:
            # the feed tasks haven't completed yet
            self.render('readability.populating', {'link':feed_url, 'title':feed.title},
                            ext='.xml')
            queue_feed_update(**update_args)
            return

        queue_feed_update(**update_args)

        #logging.debug('FEED_URL: %s' % feed_url)
        #logging.debug('FEED_DATA: %s' % feed_data)

        #entries = {}
        entries = []
        if feed_data['entry_keys']:
            entries = memcache.get_multi(feed_data['entry_keys']) # as a k,v dict
            # which means unordered...

        channel_args = tuple(map(jinja2.escape,
                                 [feed_data['title'],
                                  feed_data['link'],
                                  feed_data['description']]))

        self.write("""<rss version="2.0">
        <channel>
            <title>%s</title>
            <link>%s</link>
            <description>%s</description>""" % channel_args)

        for entry in entries:
        #for k, entry in entries.items(): # for just memcache dict from get_multi
            self.render('readability.item',
                        {'entry':utils.storage(entry)},
                        ext='.xml')

        self.write("""</channel>
        </rss>""")



class TransferHandler(RenderHandler):
    def get(self):
        old_feeds = Feed.all().order('-hits').fetch(1000)
        for f in old_feeds:
            self.write('re-creating feed %s<br />' % f.url)
            try:
                create_feed(f.url, self)
            except Exception, e:
                self.write('&nbsp; &nbsp; - failed w/ %s<br />' % e)
                # didn't print the exception on fail.. Tombstoned i think..
        self.write('FIN<br />')


class UrlCacheCronHandler(RenderHandler):
    def post(self):
        self.get()
    def get(self):
        UrlCache.cleanup()
        self.write('done')

