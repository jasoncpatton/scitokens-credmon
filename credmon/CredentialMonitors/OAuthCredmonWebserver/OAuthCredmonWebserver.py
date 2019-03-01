import sys
from flask import Flask, request, redirect, render_template, session, url_for, jsonify
from requests_oauthlib import OAuth2Session
import os
import tempfile
from credmon.utils import atomic_rename, get_cred_dir
import htcondor
import classad
import json
import re
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa, ec

from scitokens.utils import string_from_long

logger = logging.getLogger('OAuthCredmonWebserver')

class LoggerWriter:
    '''Used to override sys.stdout and sys.stderr so that
    all messages are sent to the logger'''
    
    def __init__(self, level):
        self.level = level

    def write(self, message):
        if message != '\n':
            self.level(message.rstrip())

    def flush(self):
        return

# send stdout and stderr to the log
sys.stdout = LoggerWriter(logger.debug)
sys.stderr = LoggerWriter(logger.warning)

# initialize Flask
app = Flask(__name__)

# make sure the secret_key is randomized in case user fails to override before calling app.run()
app.secret_key = os.urandom(24)

def get_provider_str(provider, handle):
    return ' '.join((provider, handle)).rstrip(' ')

def get_provider_ad(provider, key_path):
    '''
    Returns the ClassAd for a given OAuth provider given 
    the provider name and path to file with ClassAds.
    '''
    
    if not os.path.exists(key_path):
        raise Exception("Key file {0} doesn't exist".format(key_path))

    try:
        with open(key_path, 'r') as key_file:
            for ad in classad.parseAds(key_file):
                ad_provider = get_provider_str(ad['Provider'], ad.get('Handle', ''))
                if ad_provider == provider:
                    break
            else:
                raise Exception("Provider {0} not in key file {1}".format(provider, key_path))
    except IOError as ie:
        print("Failed to open key file {0}: {1}".format(key_path, str(ie)))
        raise

    return ad


@app.before_request
def before_request():
    '''
    Prepends request URL with https if insecure http is used.
    '''
    return
    # todo: figure out why this loops
    #if not request.is_secure:
    #if request.url.startswith("http://"):
    #    print(request.url)
    #    url = request.url.replace('http://', 'https://', 1)
    #    print(url)
    #    code = 301
    #    return redirect(url, code=code)

@app.route('/.well-known/jwks-uri')
def jwks_uri():

    public_keyfile = htcondor.param.get("LOCAL_CREDMON_PUBLIC_KEY", "/etc/condor/scitokens.pem")
    kid = htcondor.param.get("LOCAL_CREDMON_KEY_ID", "local")

    with open(public_keyfile, 'rb') as key_file:
            public_key = serialization.load_pem_public_key(
                key_file.read(),
                backend=default_backend()
            )

    public_numbers = public_key.public_numbers()

    # This is a elliptic curve...
    if hasattr(public_numbers, 'x'):

        jwk_public_key = {'keys': [
                {
                    "crv": "P-256",
                    "x": string_from_long(public_numbers.x),
                    "y": string_from_long(public_numbers.y),
                    "kty": "EC",
                    "use": "sig",
                    "kid": kid.decode('utf-8')
                }
            ]}
    else:

        jwk_public_key = {'keys': [
                {
                    "alg": "RS256",
                    "n": string_from_long(public_numbers.n),
                    "e": string_from_long(public_numbers.e),
                    "kty": "RSA",
                    "use": "sig",
                    "kid": kid.decode('utf-8')
                }
            ]}


    return jsonify(jwk_public_key)

@app.route('/.well-known/openid-configuration')
def openid_configuration():
    return jsonify({"issuer": url_for("index", _external=True),
                    "jwks_uri": url_for("jwks_uri", _external=True)
                   })

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/key/<key>')
def key(key):
    # get the key from URL
    if not re.match("[0-9a-fA-F]+", key):
        raise Exception("Key id {0} is not a valid key".format(key))

    # check for the key file in the credmon directory
    cred_dir = get_cred_dir()
    key_path = os.path.join(cred_dir, key)
    if not os.path.exists(key_path):
        raise Exception("Key file {0} doesn't exist".format(key_path))
    
    # read in the key file, which is a list of classads
    session_dict = {}
    session_dict['providers'] = {}
    session_dict['logged_in'] = False
    print('Creating new session from {0}'.format(key_path))
    with open(key_path, 'r') as key_file:
        
        # store the path to the key file since it will be accessed later in the session
        session_dict['key_path'] = key_path
        
        # initialize data for each token provider
        for provider_ad in classad.parseAds(key_file):
            provider = get_provider_str(provider_ad['Provider'], provider_ad.get('Handle', ''))
            session_dict['providers'][provider] = {}
            session_dict['providers'][provider]['logged_in'] = False
            if 'Scopes' in provider_ad:
                session_dict['providers'][provider]['requested_scopes'] = [s.rstrip().lstrip() for s in provider_ad['Scopes'].split(',')]
            if 'Audience' in provider_ad:
                session_dict['providers'][provider]['requested_resource'] = provider_ad['Audience']

        # the local username is global to the session, just grab it from the last classad
        session_dict['local_username'] = provider_ad['LocalUser']

    session = session_dict
    print('New session started for user {0}'.format(session['local_username']))
    return render_template('index.html')

@app.route('/login/<provider>')
def oauth_login(provider):
    """
    Go to OAuth provider
    """

    try:
        if not (provider in session['providers']):
            raise Exception("Provider {0} not in list of providers".format(provider))
    except KeyError:
        sys.stderr.write("Problem with session dict: {0}\n".format(session))
        raise
    provider_ad = get_provider_ad(provider, session['key_path'])

    # gather information from the key file classad
    oauth_kwargs = {}
    oauth_kwargs['client_id'] = provider_ad['ClientId']
    oauth_kwargs['redirect_uri'] = provider_ad['ReturnUrl']
    if 'Scopes' in provider_ad: # pass scope to the OAuth2Session object if requested
        scope = [s.rstrip().lstrip() for s in provider_ad['Scopes'].split(',')]
        oauth_kwargs['scope'] = scope

    auth_url_kwargs = {}
    auth_url_kwargs['url'] = provider_ad['AuthorizationUrl']

    # ***
    # ***                WARNING
    # *** Adding 'resource' to the authorization_url
    # *** is specific to scitokens OAuth server requests!
    # ***
    if 'Audience' in provider_ad:
        auth_url_kwargs['resource'] = provider_ad['Audience']

    # make the auth request and get the authorization url
    oauth = OAuth2Session(**oauth_kwargs)
    authorization_url, state = oauth.authorization_url(**auth_url_kwargs)

    # state is used to prevent CSRF, keep this for later.
    # todo: cleanup this way of changing the session variable
    providers_dict = session['providers']
    providers_dict[provider]['state'] = state
    session['providers'] = providers_dict

    # store the provider name since it could be different than the provider
    # argument to oauth_return() (e.g. if getting multiple tokens from the
    # same OAuth endpoint)
    session['outgoing_provider'] = provider
    
    return redirect(authorization_url)

@app.route('/return/<provider>')
def oauth_return(provider):
    """
    Returning from OAuth provider
    """

    # get the provider name from the outgoing_provider set in oauth_login()
    provider = session.pop('outgoing_provider', get_provider_str(provider, ''))
    if not (provider in session['providers']):
        raise Exception("Provider {0} not in list of providers".format(provider))

    provider_ad = get_provider_ad(provider, session['key_path'])

    # gather information from the key file classad
    client_id = provider_ad['ClientId']
    redirect_uri = provider_ad['ReturnUrl']
    state = session['providers'][provider]['state']
    oauth = OAuth2Session(client_id, state=state, redirect_uri=redirect_uri)
    
    # convert http url to https if needed
    if request.url.startswith("http://"):
        updated_url = request.url.replace('http://', 'https://', 1)
    else:
        updated_url = request.url

    # fetch token
    client_secret = provider_ad['ClientSecret']
    token_url = provider_ad['TokenUrl']
    token = oauth.fetch_token(token_url,
        authorization_response=updated_url,
        client_secret=client_secret,
        method='POST')
    print('Got {0} token for user {1}'.format(provider, session['local_username']))
    
    # get user info if available
    # todo: make this more generic
    try:
        get_user_info = oauth.get(provider_ad['UserUrl'])
        user_info = get_user_info.json()
        if 'login' in user_info: # box
            session['providers'][provider]['username'] = user_info['login']
        elif 'sub' in user_info: # scitokens/jwt
            session['providers'][provider]['username'] = user_info['sub']
        else:
            session['providers'][provider]['username'] = 'Unknown'
    except ValueError:
        session['providers'][provider]['username'] = 'Unknown'

    # split off the refresh token from the access token if it exists
    try:
        refresh_token_string = token.pop('refresh_token')
    except KeyError: # no refresh token
        use_refresh_token = False
        refresh_token_string = ''
    else:
        use_refresh_token = True

    refresh_token = {
        'refresh_token': refresh_token_string
        }

    # create a metadata file for refreshing the token
    metadata = {
        'client_id': client_id,
        'client_secret': client_secret,
        'token_url': token_url,
        'use_refresh_token': use_refresh_token
        }

    # atomically write the tokens to the cred dir
    cred_dir = get_cred_dir()
    user_cred_dir = os.path.join(cred_dir, session['local_username'])
    if not os.path.isdir(user_cred_dir):
        os.makedirs(user_cred_dir)
    refresh_token_path = os.path.join(user_cred_dir, provider.replace(' ', '_') + '.top')
    access_token_path = os.path.join(user_cred_dir, provider.replace(' ', '_') + '.use')
    metadata_path = os.path.join(user_cred_dir, provider.replace(' ', '_') + '.meta')

    # write tokens to tmp files
    try:
        (tmp_fd, tmp_access_token_path) = tempfile.mkstemp(dir = user_cred_dir)
    except OSError as oe:
        print("Failed to create temporary file in the user credential directory: {0}".format(str(oe)))
        raise
    with os.fdopen(tmp_fd, 'w') as f:
        json.dump(token, f)

    (tmp_fd, tmp_refresh_token_path) = tempfile.mkstemp(dir = user_cred_dir)
    with os.fdopen(tmp_fd, 'w') as f:
        json.dump(refresh_token, f)
        
    (tmp_fd, tmp_metadata_path) = tempfile.mkstemp(dir = user_cred_dir)
    with os.fdopen(tmp_fd, 'w') as f:
        json.dump(metadata, f)
        
    # (over)write token files
    try:
        atomic_rename(tmp_access_token_path, access_token_path)
        atomic_rename(tmp_refresh_token_path, refresh_token_path)
        atomic_rename(tmp_metadata_path, metadata_path)
    except OSError as e:
        sys.stderr.write(e)

    # mark provider as logged in
    session['providers'][provider]['logged_in'] = True

    # check if other providers are logged in
    session['logged_in'] = True
    for provider in session['providers']:
        if session['providers'][provider]['logged_in'] == False:
            session['logged_in'] = False
    
    return redirect("/")

