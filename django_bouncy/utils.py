"""Utility functions for the django_bouncy app"""
try:
    import urllib2 as urllib
except ImportError:
    import urllib

try:
    # Python 3
    from urllib.request import urlopen
except ImportError:
    # Python 2.7
    from urllib import urlopen

try:
    from urllib import urlparse
except ImportError:
    from urllib.parse import urlparse

import base64
import re
import pem
import logging
import six

from OpenSSL import crypto
from django.conf import settings
from django.core.cache import get_cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
import dateutil.parser

from django_bouncy import signals

NOTIFICATION_HASH_FORMAT = '''Message
{Message}
MessageId
{MessageId}
Timestamp
{Timestamp}
TopicArn
{TopicArn}
Type
{Type}
'''

SUBSCRIPTION_HASH_FORMAT = '''Message
{Message}
MessageId
{MessageId}
SubscribeURL
{SubscribeURL}
Timestamp
{Timestamp}
Token
{Token}
TopicArn
{TopicArn}
Type
{Type}
'''

logger = logging.getLogger(__name__)


def grab_keyfile(cert_url):
    """
    Function to acqure the keyfile

    SNS keys expire and Amazon does not promise they will use the same key
    for all SNS requests. So we need to keep a copy of the cert in our
    cache
    """
    key_cache = get_cache(getattr(settings, 'BOUNCY_KEY_CACHE', 'default'))

    pemfile = key_cache.get(cert_url)
    if not pemfile:
        response = urlopen(cert_url)
        pemfile = response.read()
        # Extract the first certificate in the file and confirm it's a valid PEM
        # certificate
        certificates = pem.parse(pemfile)

        # A proper certificate file will contain 1 certificate
        if len(certificates) != 1:
            logger.error('Invalid Certificate File: URL %s', cert_url)
            raise ValueError('Invalid Certificate File')

        key_cache.set(cert_url, pemfile)
    return pemfile


def verify_notification(data):
    """
    Function to verify notification came from a trusted source

    Returns True if verfied, False if not verified
    """
    pemfile = grab_keyfile(data['SigningCertURL'])
    cert = crypto.load_certificate(crypto.FILETYPE_PEM, pemfile)
    signature = base64.decodestring(six.b(data['Signature']))

    if data['Type'] == "Notification":
        hash_format = NOTIFICATION_HASH_FORMAT
    else:
        hash_format = SUBSCRIPTION_HASH_FORMAT

    try:
        crypto.verify(cert, signature, hash_format.format(**data), 'sha1')
    except Exception:
        return False
    return True


def approve_subscription(data):
    """
    Function to approve a SNS subscription with Amazon

    We don't do a ton of verification here, past making sure that the endpoint
    we're instructed to go to to verify the subscription is on the correct host
    """
    url = data['SubscribeURL']

    domain = urlparse(url).netloc
    pattern = getattr(
        settings,
        'BOUNCY_SUBSCRIBE_DOMAIN_REGEX',
        r"sns.[a-z0-9\-]+.amazonaws.com$"
    )
    if not re.search(pattern, domain):
        logger.error('Invalid Subscription Domain %s', url)
        return HttpResponseBadRequest('Improper Subscription Domain')

    try:
        result = urlopen(url).read()
        logger.info('Subscription Request Sent %s', url)
    except urllib.HTTPError as error:
        result = error.read()
        logger.warning('HTTP Error Creating Subscription %s', str(result))

    signals.subscription.send(
        sender='bouncy_approve_subscription',
        result=result,
        notification=data
    )

    # Return a 200 Status Code
    return HttpResponse(six.u(result))


def clean_time(time_string):
    """Return a datetime from the Amazon-provided datetime string"""
    # Get a timezone-aware datetime object from the string
    time = dateutil.parser.parse(time_string)
    if not settings.USE_TZ:
        # If timezone support is not active, convert the time to UTC and remove
        # the timezone field
        time = time.astimezone(timezone.utc).replace(tzinfo=None)
    return time
