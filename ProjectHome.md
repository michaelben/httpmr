# HTTP Map/Reduce: A scalable data processing framework for people with web clusters. #

_Status: Beta_

HTTPMR is an implementation of Google's famous Map/Reduce data processing model on clusters of HTTP servers.

HTTPMR tries to make only the following assumptions about the computing environment:

  * Machines can be accessed only via HTTP requests.
  * Requests are assigned randomly to a set of machines.
  * Requests have timeouts on the order of several seconds.
  * There is a storage system that is accessible by code receiving HTTP requests.
  * The data being processed can be broken up into many, many small records, each having a unique identifier.
  * The storage system can accept >, <= range restrict operations on the data's unique identifiers.
  * Jobs are controlled by a web spidering system (such as wget).

Driven primarily by the needs of users of Google AppEngine (http://appengine.google.com/) for a robust data processing system, HTTMR will hopefully be written in a general-enough way to work in many web clusters.  Bringing HTTMR up in a new environment should require only implementing a few interfaces to the data storage system.

# Live Demo: #

http://httpmr.appspot.com/

The demo creates a full-text document index of some randomly-generated documents that were previously loaded into the datastore.  See the example code below for details.

# Example Code: #

This code is exactly the code that's running in the live demo, above, and represents the entirety of the application-specific code that one would need to write.

```
import wsgiref
from google.appengine.ext import webapp
from httpmr import appengine
from httpmr import base
from wsgiref import handlers
from google.appengine.ext import db


class Document(db.Model):
  title = db.StringProperty(required=True)
  contents = db.TextProperty(required=True)


class DocumentIndex(db.Model):
  token = db.StringProperty(required=True)
  document_titles = db.StringListProperty()


class TokenMapper(base.Mapper):
  
  def Map(self, document_title, document):
    for token in list(set(document.contents.split(" "))):
      if token:
        yield token, document_title


class TokenReducer(base.Reducer):
  
  def Reduce(self, token, document_titles):
    yield None, DocumentIndex(token=token,
                              document_titles=document_titles)


class ConstructDocumentIndexMapReduce(appengine.AppEngineMaster):
  
  def __init__(self):
    self.QuickInit("construct_token_index",
                   mapper=TokenMapper(),
                   reducer=TokenReducer(),
                   source=appengine.AppEngineSource(Document.all(),
                                                    "title"),
                   sink=appengine.AppEngineSink())


def main():
  application = webapp.WSGIApplication([('/construct_document_index',
                                         ConstructDocumentIndexMapReduce)],
                                       debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == "__main__":
  main()
```

A specific URL is then mapped to HandleMapReduce, and you're off to the races!