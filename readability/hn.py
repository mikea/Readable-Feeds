#    Readable Feeds
#    Copyright (C) 2009  
#    
#    This file originally written by Nirmal Patel (http://nirmalpatel.com/).
#    Generic feed and Google App Engine support added by Andrew Trusty (http://andrewtrusty.com/).
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



import re, urlparse
import HTMLParser, feedparser
from BeautifulSoup import BeautifulSoup #, SoupStrainer

import rfc822
#from datetime import datetime, timedelta
from pickle import dumps, loads
import logging
import traceback
#import time

#import chardet
from chardet.universaldetector import UniversalDetector

from appengine_utilities.cache import Cache
from google.appengine.api import urlfetch


# memcache & db backed cache that stores entries for 1 week
CACHE = Cache(default_timeout = 3600 * 24 * 7)



NEGATIVE    = re.compile("comment|meta|footer|footnote|foot")
POSITIVE    = re.compile("post|hentry|entry|content|text|body|article")
PUNCTUATION = re.compile("""[!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~]""")


def grabContent(link, html):
    ret = dict(title=u'', content=u'')
    
    replaceBrs = re.compile("<br */? *>[ \r\n]*<br */? *>")
    html = re.sub(replaceBrs, "</p><p>", html)
    
    try:
        #strainer = SoupStrainer(text=lambda t: t.upper() not in set(['SCRIPT','STYLE']))
        # & also link tags w/ type='text/css' but thats uncommon & trick to filter
        # ... EPIC FAIL ...
        soup = BeautifulSoup(html,
                             parseOnlyThese=None)#strainer)
    except HTMLParser.HTMLParseError:
        return ret
    
    logging.debug('grabbing title for link: %s' % link)
    try:
        ret['title'] = soup.title.string
    except AttributeError:
        # bad soup... BeautifulSoup fails on bad doc types..
        # <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd" xmlns:fb="http://www.facebook.com/2008/fbml">
        html = re.sub("<!DOCTYPE [^>]+>", '', html)
        soup = BeautifulSoup(html,
                             parseOnlyThese=None)#strainer)
        try:
            ret['title'] = soup.title.string
        except:
            logging.warning("no title found on page: %s" % link)
            # TODO: parse page for a proper title..
            # eg. http://web.mit.edu/jrickert/www/mathadvice.html
            ret['title'] = link.replace('http://','').replace('https://','')
        
    
    # REMOVE SCRIPTS
    for s in soup.findAll("script"):
        s.extract()
    
    allParagraphs = soup.findAll("p")
    topParent     = None
    
    parents = []
    for paragraph in allParagraphs:
        
        parent = paragraph.parent
        
        if (parent not in parents):
            parents.append(parent)
            parent.score = 0
            
            if (parent.has_key("class")):
                if (NEGATIVE.match(parent["class"])):
                    parent.score -= 50
                if (POSITIVE.match(parent["class"])):
                    parent.score += 25
                    
            if (parent.has_key("id")):
                if (NEGATIVE.match(parent["id"])):
                    parent.score -= 50
                if (POSITIVE.match(parent["id"])):
                    parent.score += 25

        if (parent.score == None):
            parent.score = 0
        
        innerText = paragraph.renderContents() #"".join(paragraph.findAll(text=True))
        if (len(innerText) > 10):
            parent.score += 1
            
        parent.score += innerText.count(",")
        
    for parent in parents:
        if ((not topParent) or (parent.score > topParent.score)):
            topParent = parent

    if (not topParent):
        return ret
    
    # REMOVE LINK'D STYLES
    styleLinks = soup.findAll("link", attrs={"type" : "text/css"})
    for s in styleLinks:
        s.extract()

    # REMOVE ON PAGE STYLES
    for s in soup.findAll("style"):
        s.extract()

    # CLEAN STYLES FROM ELEMENTS IN TOP PARENT
    for ele in topParent.findAll(True):
        del(ele['style'])
        del(ele['class'])
        
    killDivs(topParent)
    clean(topParent, "form")
    clean(topParent, "object")
    clean(topParent, "iframe")
    
    fixLinks(topParent, link)
    
    ret['content'] = topParent.renderContents().decode('utf-8')
    return ret
    

def fixLinks(parent, link):
    tags = parent.findAll(True)
    
    for t in tags:
        if (t.has_key("href")):
            t["href"] = urlparse.urljoin(link, t["href"])
        if (t.has_key("src")):
            t["src"] = urlparse.urljoin(link, t["src"])


def clean(top, tag, minWords=10000):
    tags = top.findAll(tag)

    for t in tags:
        if (t.renderContents().count(" ") < minWords):
            t.extract()


def killDivs(parent):
    
    divs = parent.findAll("div")
    for d in divs:
        p     = len(d.findAll("p"))
        img   = len(d.findAll("img"))
        li    = len(d.findAll("li"))
        a     = len(d.findAll("a"))
        embed = len(d.findAll("embed"))
        pre   = len(d.findAll("pre"))
        code  = len(d.findAll("code"))
    
        if (d.renderContents().count(",") < 10):
            if ((pre == 0) and (code == 0)):
                if ((img > p ) or (li > p) or (a > p) or (p == 0) or (embed > 0)):
                    d.extract()
    

def guessEncoding(html):
    detector = UniversalDetector()
    start = 0
    limit = len(html) / 2
    end = len(html)
    
    # XXX: chardet tends to be slow and can cause DeadlineExceededErrors..
    
    while start < end:
        some_data = html[start:start+limit]
        
        detector.feed(some_data)
        if detector.done:
            logging.debug("Universal Detector done after %s chars" % 
                          (start+limit))
            break
        
        start += limit
    detector.close()
    
    possible_encodings = [detector.result['encoding'], 'utf-8', 'cp1252']
    
    return possible_encodings


import urlparse
import urllib

# gives me the content i want
def upgradeEntry(link, orig_content, show_original, retrieve_links, show_linked,
                 user_agent=None, sublink=False):#, 
    #                full_content=False): #, time_left):
    link = link.encode('utf-8')
    
    # TODO: handle other exceptions
    
    path = urlparse.urlparse(link).path
    
    # XXX: also, better way to check file types would be content-type headers
    #        and don't mess with anything that isn't a webpage..
    if (link.startswith("http://news.ycombinator.com") 
             
             or path[-4:] in set(['.zip', '.rar'])
             or path[-3:] in set(['.gz'])
        
             # TODO: parse the url to pull off params before checking extension (ie remove ?.*)
             
             ):
        # TODO: see if the ASK HN posts come out well..
        if show_original:
            return orig_content
        return u''
    
    content = u''
    if show_original:
        if not show_linked:
            content = orig_content
        else:
            content = orig_content + '<hr />'
        
    if retrieve_links:
        # TODO: add links stuff to end of content
        #       but if we are showing linked, these links need to go after..
        # need more abstracting.. so i can do fetches
        pass
        #content = ('<a href=""><b>' % link + title + '</b></a><br /><br />' +
        #                       content)
        
    if not show_linked:
        return content
    
    # TODO: support Word, Excel, other doc type embeds.. iPaper is really nice..
    #       but doesn't seem to have instant publish on demand support
    
    # TEST THIS, if not work this way, then do iframe:
    """
    <iframe src="http://docs.google.com/gview?url=http://infolab.stanford.edu/pub/papers/google.pdf&embedded=true" style="width:600px; height:500px;" frameborder="0"></iframe>
    """
    
    def handle_pdf():
        quoted_link = urllib.quote_plus(link)
        google_link = 'http://docs.google.com/gview?url=%s&embedded=true' % quoted_link 
        new_content = '<a href="%s">View PDF in Flash</a><br />' % google_link
        
        samuraj_link = 'http://view.samurajdata.se/ps.php?url=%s&submit=View!' % quoted_link
        new_content += '<a href="%s">View PDF as Images</a> (scroll to the bottom of the page)<br />' % samuraj_link 
        
        new_content += '<a href="%s">Open PDF</a>' % link 
        
        return new_content
    
    def handle_image():
        return '<img src="%s" />' % link
    
    if path[-4:] == '.pdf': # TODO: also check content-type..
        
        # abstract
        
        # doesn't work, JS doesn't run :(, use google cache & do html view..
        
        return content + handle_pdf()
        # upgradeLink(new_link, user_agent, full_content=True)
    
    # TODO: also check content-type..
    if path[-5:] == '.jpeg' or path[-4:] in set(['.gif', '.jpg', '.png', '.bmp']):
        #new_content = '<html><body><img src="%s" /></body></html>'
        
        # doesn't handle query params before it gets here..
        # some part of my parsing messes them up.. still not working
        
        return content + handle_image()
    
    
    
    #linkFile = "upgraded/" + re.sub(PUNCTUATION, "_", link)
    
    #if linkFile in CACHE:
    #    content = CACHE[linkFile]
    
    
    html = ""
    #start_time = time.time()
    try:
        resp = urlfetch.fetch(link,
                              headers={'User-agent':user_agent},
                              follow_redirects=True)
        if resp.status_code == 200:
            html = resp.content
        
        # check content-type
        ctype = resp.headers['content-type']
        mtype, stype = ctype.split('/')
        
        if mtype == 'image' and stype in set(['gif', 'jpeg', 'png']):
            return content + handle_image()
        elif mtype == 'application' and stype == 'pdf':
            return content + handle_pdf()
        elif ctype == 'text/plain':
            return content + html # html is actually just text
        
        # TODO: handle music/podcasts/videocasts etc w/ embeds
        
        # TODO: check for html doctype ['text/html','application/xhtml+xml']
        
    except Exception, e:
        # so many different errors: InvalidURLError, DownloadError, IOErrors..
        logging.error('Exception %s when reading content at url: %s' % (e, link))
        # TODO: return error message in content?
    
    if html:
        # i feel like this is causing errors i didn't have before
        # which doesn't make sense except that it could be
        # BeautifulSoup reacting differently..
        
        #best_guess_encoding = chardet.detect(html)['encoding']
        
        """
        # causing too many empty contents.. buggy..
        elapsed = time.time() - start_time
        if time_left - elapsed < 4.0:
            # give up b/c chardet might take a while..
            return u""
        """
        
        possible_encodings = guessEncoding(html) #[best_guess_encoding, 'utf-8', 'cp1252']
        for encoding in possible_encodings:
            try:
                if not encoding: # oy..
                    html = html.decode()
                else:
                    html = html.decode(encoding)
                #if not full_content:
                    # use readability algo
                page = grabContent(link, html)
                content = page['content']
                title = page['title']
                
                """
                if links:
                    for l in links:
                        content += '<hr />' + upgradeLink(link, '', 
                                                          show_original=False, 
                                                          retrieve_links=0,
                                                          sublink=True)
                """
                if show_original:
                    content = ('<a href=""><b>' % link + title + '</b></a><br /><br />' +
                               content)
                
                #else:
                    # for pdf viewer & images
                #    soup = BeautifulSoup(html)
                #    content = "".join(map(unicode, soup.find('body').contents))
                    
                #if content:
                #    CACHE[linkFile] = content # maybe this is doing auto-encoding?
                return content
            except UnicodeDecodeError, e:
                if encoding == possible_encodings[-1]:
                    logging.error('Failed to DEcode (%s) with %s:\n%s' %
                                  (link, ', '.join(possible_encodings),
                                   traceback.format_exc()))
            except UnicodeEncodeError, e:
                if encoding == possible_encodings[-1]:
                    logging.error('Failed to ENcode (%s) with %s:\n%s' %
                                  (link, ', '.join(possible_encodings),
                                   traceback.format_exc()))
        
        # haven't solved the memory problem with chardet..
        # incremental reading doesn't read enough to make a good classification..
        # could up it more..
        # could munge the html to remove non meaningful stuff like tags, spaces, ..
        # but both of these approaches would use more memory... need to find the
        # best approach...
        
        """
        # BeatifulSoup decoding doesn't work every time... even though it uses chardet too..
        # see: http://hicks-wright.net/blog/your-resume-wont-get-you-hired/
        """
    return content


def get_headers(feedUrl):
    if 'headers-'+feedUrl not in CACHE:
        return None, None, None
    headers = loads(CACHE['headers-'+feedUrl])
    
    # headers are lowercased by feedparser
    last_modified = headers.get('last-modified', '')
    etag = headers.get('etag', '')
    expires = headers.get('expires', '')
    
    fp_last_modified = None
    if last_modified:
        fp_last_modified = rfc822.parsedate(last_modified)
    fp_expires = None
    if expires:
        fp_expires = rfc822.parsedate(expires)
    # fp if for 9 tuple feed parser required format
    return etag, fp_last_modified, fp_expires

def clear_headers(feedUrl):
    if 'headers-'+feedUrl in CACHE:
        del CACHE['headers-'+feedUrl]

def save_headers(parsedFeed, feedUrl):
    if hasattr(parsedFeed, 'headers'):
        CACHE['headers-'+feedUrl] = dumps(parsedFeed.headers)

"""
class FeedNotFound(Exception):
    pass
"""
def upgradeFeed(rss_data, feedUrl):#feedUrl, agent=None):#, out=None):
    
    #etag, last_modified, expires = get_headers(feedUrl)
    
    # TODO: use expires header..
    #if expires and datetime.utcnow().utctimetuple() < expires:
    #    return cached entries ..??

    """
    # Mozilla/ should catch IE, Firefox, Safari, Chrome, Epiphany, Konqueror, Flock, 
    #  & a bunch of others (also catches one version of Googlebot & Yahoo! Slurp..)
    if agent and (('Mozilla/' in agent or 'Opera/' in agent) and 
                  ('Googlebot' not in agent) and ('Yahoo!' not in agent)):
        agent = None
        # i would pass-thrhough all user-agents but feedBurner returns HTML
        #    instead of the feed with some user agents
        #    (but only on GAE & not on dev which makes this even more confusing) 
    if not agent:
        agent = 'Readable-Feeds (http://andrewtrusty.appspot.com/readability/)'
    """
    # otherwise allow user-agent to go through for subscriber reports like
    #    Bloglines, Google FeedFetcher, et al to work

    # testing w/o etag & last_modified b/c w/ Google Reader HN feed only gets updated
    # every 2 hours... whereas Nirmal's got down to a 30min update..
    
    """
    update = False
    if 'last-update-'+feedUrl in CACHE:
        last_update = CACHE['last-update-'+feedUrl]
        if last_update < datetime.utcnow() - timedelta(0, 3600):
            # update every half hour
            update = True
    else:
        update = True
    
    # TODO: handle dogpiling effect.. by only allowing cron to update
    #        so all clients just get cache
    parsedFeed = None
    if update:
        #logging.debug('----> fetching feed')
        parsedFeed = feedparser.parse(feedUrl, agent=agent)#, etag=etag, modified=last_modified,
                                      #agent=agent) #, referrer=feedUrl)
        CACHE['last-update-'+feedUrl] = datetime.utcnow()
        CACHE['cached-parsedFeed-'+feedUrl] = dumps(parsedFeed)
        save_headers(parsedFeed, feedUrl)
    else:
        #logging.debug('----> hit feed cache')
        parsedFeed = loads(CACHE['cached-parsedFeed-'+feedUrl])
    """
    
    # feedparser isn't fetching correctly... use urlfetch.. w/ redirects..
    
    # OMG, i'm doing double-fetches!!!
    """
    resp = urlfetch.fetch(feedUrl, headers={'User-agent':agent}, follow_redirects=True)
    rss_data = ''
    if resp.status_code == 200:
        rss_data = resp.content
    else:
        logging.error('feed not fetched b/c got status %s for feed: %s' % 
                      (resp.status_code, feedUrl))
        raise FeedNotFound("Failed to retrieve a feed!")
    """
    #logging.debug('urlfetch got %s data for url: %s' % (len(rss_data), feedUrl))
    """
    parsedFeed = feedparser.parse(rss_data)#feedUrl, agent=agent)
    if not parsedFeed:
        logging.error('parsedFeed is None/not True')
    
    # test if its a feed by trying to get the title & link
    try:
        title = parsedFeed.feed.title
    except: # assume that if i get title, link & subtitle its a valid feed
        logging.error('feed title not found for: %s' % feedUrl)
        raise FeedNotFound("missing title")
    try:
        link = parsedFeed.feed.link
    except:
        logging.error('feed link not found for: %s' % feedUrl)
        raise FeedNotFound("missing link")

    
    description = ''
    if hasattr(parsedFeed.feed, 'subtitle'):
        description = parsedFeed.feed.subtitle
        
    return {'title':title,
            'link':link, 
            'description':description,
            'items':parsedFeed.entries,
            'agent':agent}
    """
    pass # unused..
    
    
    
if __name__ == "__main__":
    print 'nada'  
    #print upgradeFeed(HN_RSS_FEED)



