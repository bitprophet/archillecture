import logging
import os
import time
import traceback
from io import BytesIO

from invoke import task
import tweepy
import requests


# TODO: unify with pax-checker, this is a ridiculous amount of boilerplate to
# need, ugh.


logger = logging.getLogger(__file__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(getattr(logging, os.environ.get('LOGLEVEL', 'INFO')))


@task
def loop(c):
    # The stream listener will, sans any issues, block/loop internally
    # forever. Here, we just want to kickstart it if it gets disconnected.
    while True:
        try:
            logger.info("Starting to stream tweets...")
            stream_tweets(c)
        # TODO: what's recoverable exactly?
        except Exception as e:
            logger.error("Got exception! {!r}".format(e))
            traceback.print_exc()
            logger.error("Trying to recover...back to top of loop, after sleep")
        # >60s sleep to ensure we don't hit rate limiting
        time.sleep(120)


def twitter_auth(c):
    auth = tweepy.OAuthHandler(
        c.twitter.consumer_key,
        c.twitter.consumer_secret,
    )
    auth.set_access_token(
        c.twitter.access_token,
        c.twitter.access_secret,
    )
    return auth


url_template = "https://twitter.com/{}/status/{}"


def handle_status(api, status):
    # Not worth configuring these, they are part of the logic
    expected_source = 'archillinks'
    # TODO: yea, let's include these in some fashion, hopefully in a way that
    # doesn't magically turn it into a quote-tweet in most clients. Otherwise
    # attribution to archillect itself is mostly lost.
    # TODO: it's unclear why sometimes it's a direct link (to the website
    # archive, which actually seems to have LESS info than archillinks itself)
    # and sometimes a link to the tweet.
    skippable_url_prefixes = (
        'http://archillect.com',
        'https://twitter.com/archillect',
    )

    url = url_template.format(status.user.screen_name, status.id)

    # Sanity checks because twitter's API is bizarre. Who knows if these even
    # do what we think!
    if not status.is_quote_status:
        logger.debug("Got non-quote tweet: {}".format(url))
        return
    if status.source != expected_source:
        logger.debug("Got non-archillinks tweet: {}".format(url))
        return

    # Archillinks statuses always contain a bunch of links + a link to the
    # quoted status.
    links = [
        x['expanded_url']
        for x in status.entities['urls']
        if not x['expanded_url'].startswith(skippable_url_prefixes)
    ]
    # Archillect posts are always a single photo/media item.
    quote = status.quoted_status
    media = quote['entities']['media'][0]

    # Debuggery
    blurb = """
Quoted status URI: {}
Status links: {!r}
Quoted status media URL: {}
"""
    formatted = blurb.strip().format(
        url_template.format(quote['user']['screen_name'], quote['id']),
        links,
        media['media_url'],
    )
    logger.info("Got apparently-retweetable status: {}".format(url))
    logger.debug("Details:\n{}".format(formatted))

    # Grab the media from its direct-to-the-media URL (not any display URLs as
    # those go to HTML pages), as we have to re-upload it, cannot just reuse
    # the media_id (aww.)
    response = requests.get(media['media_url'])
    media_file = BytesIO(response.content)
    # This feels gross but don't see anything obviously better offhand
    filename = os.path.basename(response.url)
    # And upload it to get a media ID
    upload = api.media_upload(filename=filename, file=media_file)

    # Actual tweet-sending
    # NOTE: tweepy's docs don't reflect the new media_ids= kwarg, but it
    # does appear to exist, per its issue #643
    our_status = api.update_status(
        media_ids=[upload.media_id],
        status="\n".join(links),
    )
    logger.info("We tweeted: {}".format(
        url_template.format(our_status.user.screen_name, our_status.id)
    ))


class Archillecture(tweepy.StreamListener):
    def __init__(self, *args, **kwargs):
        self.context = kwargs.pop('context')
        super(Archillecture, self).__init__(*args, **kwargs)

    def on_status(self, status):
        handle_status(self.api, status)

    def on_error(self, status_code):
        logger.error("Error {}!".format(status_code))
        if status_code == 420:
            logger.critical("Got a 420 (rate limit warning), disconnecting!")
            return False


@task(name='stream-tweets')
def stream_tweets(c):
    auth = twitter_auth(c)
    # Give the listener a semi-cached, authed, API (otherwise it gets an
    # unauthed one and then sending, vs streaming, tweets is unauthed.)
    api = tweepy.API(auth)
    listener = Archillecture(context=c, api=api)
    stream = tweepy.Stream(auth=auth, listener=listener)
    # This will block until disconnected...
    stream.filter(follow=[str(c.twitter.follow_id)])


def get_api(c):
    return tweepy.API(twitter_auth(c))

def get_status(c, id_):
    return get_api(c).get_status(id_)


@task(name='inspect-tweet')
def inspect_tweet(c, id_):
    status = get_status(c, id_)
    import ipdb; ipdb.set_trace()


@task(name='test-tweet')
def test_tweet(c, id_):
    api = get_api(c)
    status = get_status(c, id_)
    handle_status(api, status)


# TODO: invocations?

@task(name='lock-deps')
def lock_deps(c):
    c.run("pip-compile --no-index")
    c.run("pip-sync")


@task(name='push-config')
def push_config(c):
    # Push any updated .env file contents (...ALSO triggers restart)
    c.run("heroku config:push -o")


@task
def deploy(c):
    # Push to my own repo
    c.run("git push", pty=True)
    # Push to heroku (triggers restart)
    c.run("git push heroku HEAD", pty=True)
