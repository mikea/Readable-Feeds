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


# get all the common imports
from gae_utils import *


class MainHandler(RenderHandler):
  def get(self):
    args = utils.storage()
    self.render('index', args)

class EmptyHandler(RenderHandler):
  def get(self):
    # send content-type ..?
    self.write('')


from readability import *


def main():
  logging.getLogger().setLevel(logging.DEBUG)

  application = webapp.WSGIApplication([('/', MainHandler),
                                        ('/readability/', ReadabilityHandler),
                                        ('/readability/create', CreateFeedHandler),
                                        ('/readability/feed', ReadabilityFeedHandler),
                                        ('/readability/cron/cleanup-url-cache', UrlCacheCronHandler),
                                        ('/readability/queue/feeds', FeedsQueueHandler),
                                        ('/readability/queue/feeds2', FeedsQueueHandler),
                                        ('/readability/queue/entries', EntriesQueueHandler),
                                        ('/readability/queue/entries2', EntriesQueueHandler),
                                        ('/robots.txt', EmptyHandler),
                                        ('/favicon.ico', EmptyHandler),
                                        #('/readability/transfer', TransferHandler),
                                        ],
                                       debug=DEV)
  
  wsgiref.handlers.CGIHandler().run(application)


if __name__ == '__main__':
  main()
