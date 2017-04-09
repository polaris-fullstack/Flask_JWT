import datetime
import json
import uuid
from functools import wraps

import jwt
import six
from flask import request, current_app
from werkzeug.security import safe_str_cmp
try:
    from flask import _app_ctx_stack as ctx_stack
except ImportError:  # pragma: no cover
    from flask import _request_ctx_stack as ctx_stack

from flask_jwt_extended.config import config
from flask_jwt_extended.exceptions import JWTEncodeError, JWTDecodeError, \
    InvalidHeaderError, NoAuthorizationError, WrongTokenError, \
    FreshTokenRequired, CSRFError
from flask_jwt_extended.blacklist import check_if_token_revoked, store_token


# TODO move everything into a single jwt object, then create first class methods
#      for stuff like jwt_required to not break functionality


def get_jwt_identity():
    """
    Returns the identity of the JWT in this context. If no JWT is present,
    None is returned.
    """
    return get_raw_jwt().get('identity', {})


def get_jwt_claims():
    """
    Returns the dictionary of custom use claims in this JWT. If no custom user
    claims are present, an empty dict is returned
    """
    return get_raw_jwt().get('user_claims', {})


def get_raw_jwt():
    """
    Returns the python dictionary which has all of the data in this JWT. If no
    JWT is currently present, and empty dict is returned
    """
    return getattr(ctx_stack.top, 'jwt', {})


def _get_cookie_max_age():
    """
    Checks config value for using session or persistent cookies and returns the
    appropriate value for flask set_cookies.
    """
    return None if config.session_cookie else 2147483647  # 2^31


def _create_csrf_token():
    return str(uuid.uuid4())


def _encode_access_token(identity, secret, algorithm, token_expire_delta,
                         fresh, user_claims):
    """
    Creates a new access token.

    :param identity: Some identifier of who this client is (most common would be a client id)
    :param secret: Secret key to encode the JWT with
    :param fresh: If this should be a 'fresh' token or not
    :param algorithm: Which algorithm to use for the toek
    :return: Encoded JWT
    """
    # Verify that all of our custom data we are encoding is what we expect
    if not isinstance(user_claims, dict):
        raise JWTEncodeError('user_claims must be a dict')
    if not isinstance(fresh, bool):
        raise JWTEncodeError('fresh must be a bool')
    try:
        json.dumps(user_claims)
    except Exception as e:
        raise JWTEncodeError('Error json serializing user_claims: {}'.format(str(e)))

    # Create the jwt
    now = datetime.datetime.utcnow()
    uid = str(uuid.uuid4())
    token_data = {
        'exp': now + token_expire_delta,
        'iat': now,
        'nbf': now,
        'jti': uid,
        'identity': identity,
        'fresh': fresh,
        'type': 'access',
        'user_claims': user_claims,
    }
    if 'cookies' in config.token_location and config.cookie_csrf_protect is True:
        token_data['csrf'] = _create_csrf_token()
    encoded_token = jwt.encode(token_data, secret, algorithm).decode('utf-8')

    # If blacklisting is enabled and configured to store access and refresh tokens,
    # add this token to the store
    if config.blacklist_enabled and config.blacklist_checks == 'all':
        store_token(token_data, revoked=False)
    return encoded_token


def _encode_refresh_token(identity, secret, algorithm, token_expire_delta):
    """
    Creates a new refresh token, which can be used to create subsequent access
    tokens.

    :param identity: Some identifier used to identify the owner of this token
    :param secret: Secret key to encode the JWT with
    :param algorithm: Which algorithm to use for the toek
    :return: Encoded JWT
    """
    now = datetime.datetime.utcnow()
    uid = str(uuid.uuid4())
    token_data = {
        'exp': now + token_expire_delta,
        'iat': now,
        'nbf': now,
        'jti': uid,
        'identity': identity,
        'type': 'refresh',
    }
    if 'cookies' in config.token_location and config.cookie_csrf_protect is True:
        token_data['csrf'] = _create_csrf_token()
    encoded_token = jwt.encode(token_data, secret, algorithm).decode('utf-8')

    # If blacklisting is enabled, store this token in our key-value store
    if config.blacklist_enabled:
        store_token(token_data, revoked=False)
    return encoded_token


def _decode_jwt(token, secret, algorithm):
    """
    Decodes an encoded JWT

    :param token: The encoded JWT string to decode
    :param secret: Secret key used to encode the JWT
    :param algorithm: Algorithm used to encode the JWT
    :return: Dictionary containing contents of the JWT
    """
    # ext, iat, and nbf are all verified by pyjwt. We just need to make sure
    # that the custom claims we put in the token are present
    data = jwt.decode(token, secret, algorithm=algorithm)
    if 'jti' not in data or not isinstance(data['jti'], six.string_types):
        raise JWTDecodeError("Missing or invalid claim: jti")
    if 'identity' not in data:
        raise JWTDecodeError("Missing claim: identity")
    if 'type' not in data or data['type'] not in ('refresh', 'access'):
        raise JWTDecodeError("Missing or invalid claim: type")
    if data['type'] == 'access':
        if 'fresh' not in data or not isinstance(data['fresh'], bool):
            raise JWTDecodeError("Missing or invalid claim: fresh")
        if 'user_claims' not in data or not isinstance(data['user_claims'], dict):
            raise JWTDecodeError("Missing or invalid claim: user_claims")
    return data


def _decode_jwt_from_headers(type):
    # TODO make type an enum or something instead of a magic string
    if type == 'access':
        header_name = config.access_header_name
        header_type = config.header_type
    else:
        header_name = config.refresh_header_name
        header_type = config.header_type

    # Verify we have the auth header
    jwt_header = request.headers.get(header_name, None)
    if not jwt_header:
        raise NoAuthorizationError("Missing {} Header".format(header_name))

    # Make sure the header is in a valid format that we are expecting, ie
    # <HeaderName>: <HeaderType(optional)> <JWT>
    parts = jwt_header.split()
    if not header_type:
        if len(parts) != 1:
            msg = "Bad {} header. Expected value '<JWT>'".format(header_name)
            raise InvalidHeaderError(msg)
        token = parts[0]
    else:
        if parts[0] != header_type or len(parts) != 2:
            msg = "Bad {} header. Expected value '{} <JWT>'".format(header_name, header_type)
            raise InvalidHeaderError(msg)
        token = parts[1]

    return _decode_jwt(token, config.secret_key, config.algorithm)


def _decode_jwt_from_cookies(type):
    # TODO make type an enum or something instead of a magic string
    if type == 'access':
        cookie_key = config.access_cookie_name
        csrf_header_key = config.access_csrf_header_name
    else:
        cookie_key = config.refresh_cookie_name
        csrf_header_key = config.refresh_csrf_header_name

    # Decode the token
    token = request.cookies.get(cookie_key)
    if not token:
        raise NoAuthorizationError('Missing cookie "{}"'.format(cookie_key))
    token = _decode_jwt(token, config.secret_key, config.algorithm)

    # Verify csrf double submit tokens match if required
    if config.cookie_csrf_protect and request.method in config.csrf_request_methods:
        csrf_token_from_header = request.headers.get(csrf_header_key, None)
        csrf_token_from_cookie = token.get('csrf', None)

        if csrf_token_from_cookie is None:
            raise JWTDecodeError("Missing claim: 'csrf'")
        if not isinstance(csrf_token_from_cookie, six.string_types):
            raise JWTDecodeError("Invalid claim: 'csrf' (must be a string)")
        if csrf_token_from_header is None:
            raise CSRFError("Missing CSRF token in headers")
        if not safe_str_cmp(csrf_token_from_header,  csrf_token_from_cookie):
            raise CSRFError("CSRF double submit tokens do not match")

    return token


def _decode_jwt_from_request(type):
    token_locations = config.token_location

    # JWT can be in either headers or cookies
    if 'headers' in token_locations and 'cookies' in token_locations:
        try:
            return _decode_jwt_from_headers(type)
        except NoAuthorizationError:
            pass
        try:
            return _decode_jwt_from_cookies(type)
        except NoAuthorizationError:
            pass
        raise NoAuthorizationError("Missing JWT in header and cookies")

    # JWT can only be in headers
    elif 'headers' in token_locations:
        return _decode_jwt_from_headers(type)

    # JWT can only be in cookie
    else:
        return _decode_jwt_from_cookies(type)


def jwt_required(fn):
    """
    If you decorate a vew with this, it will ensure that the requester has a valid
    JWT before calling the actual view. This does not check the freshness of the
    token.

    See also: fresh_jwt_required()

    :param fn: The view function to decorate
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Attempt to decode the token
        jwt_data = _decode_jwt_from_request(type='access')

        # Verify this is an access token
        if jwt_data['type'] != 'access':
            raise WrongTokenError('Only access tokens can access this endpoint')

        # If blacklisting is enabled, see if this token has been revoked
        if config.blacklist_enabled:
            check_if_token_revoked(jwt_data)

        # Save the jwt in the context so that it can be accessed later by
        # the various endpoints that is using this decorator
        ctx_stack.top.jwt = jwt_data
        return fn(*args, **kwargs)
    return wrapper


def fresh_jwt_required(fn):
    """
    If you decorate a vew with this, it will ensure that the requester has a valid
    JWT before calling the actual view.

    See also: jwt_required()

    :param fn: The view function to decorate
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Attempt to decode the token
        jwt_data = _decode_jwt_from_request(type='access')

        # Verify this is an access token
        if jwt_data['type'] != 'access':
            raise WrongTokenError('Only access tokens can access this endpoint')

        # If blacklisting is enabled, see if this token has been revoked
        if config.blacklist_enabled:
            check_if_token_revoked(jwt_data)

        # Check if the token is fresh
        if not jwt_data['fresh']:
            raise FreshTokenRequired('Fresh token required')

        # Save the jwt in the context so that it can be accessed later by
        # the various endpoints that is using this decorator
        ctx_stack.top.jwt = jwt_data
        return fn(*args, **kwargs)
    return wrapper


def jwt_refresh_token_required(fn):
    """
    If you decorate a view with this, it will insure that the requester has a
    valid JWT refresh token before calling the actual view. If the token is
    invalid, expired, not present, etc, the appropriate callback will be called
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Get the JWT
        jwt_data = _decode_jwt_from_request(type='refresh')

        # verify this is a refresh token
        if jwt_data['type'] != 'refresh':
            raise WrongTokenError('Only refresh tokens can access this endpoint')

        # If blacklisting is enabled, see if this token has been revoked
        if config.blacklist_enabled:
            check_if_token_revoked(jwt_data)

        # Save the jwt in the context so that it can be accessed later by
        # the various endpoints that is using this decorator
        ctx_stack.top.jwt = jwt_data
        return fn(*args, **kwargs)
    return wrapper


def create_refresh_token(identity):
    # Token settings
    refresh_expire_delta = config.refresh_expires
    algorithm = config.algorithm
    secret = config.secret_key
    identity = current_app.jwt_manager._user_identity_callback(identity)

    # Actually make the tokens
    refresh_token = _encode_refresh_token(identity, secret, algorithm,
                                          refresh_expire_delta)
    return refresh_token


def create_access_token(identity, fresh=False):
    """
    Creates a new access token

    :param identity: The identity of this token. This can be any data that is
                     json serializable. It can also be an object, in which case
                     you can use the user_identity_loader to define a function
                     that will be called to pull a json serializable identity
                     out of this object. This is useful so you don't need to
                     query disk twice, once for initially finding the identity
                     in your login endpoint, and once for setting addition data
                     in the JWT via the user_claims_loader
    :param fresh: If this token should be marked as fresh, and can thus access
                  fresh_jwt_required protected endpoints. Defaults to False
    :return: A newly encoded JWT access token
    """
    # Token options
    secret = config.secret_key
    access_expire_delta = config.access_expires
    algorithm = config.algorithm
    user_claims = current_app.jwt_manager._user_claims_callback(identity)
    identity = current_app.jwt_manager._user_identity_callback(identity)

    access_token = _encode_access_token(identity, secret, algorithm, access_expire_delta,
                                        fresh=fresh, user_claims=user_claims)
    return access_token


def _get_csrf_token(encoded_token):
    secret = config.secret_key
    algorithm = config.algorithm
    token = _decode_jwt(encoded_token, secret, algorithm)
    return token['csrf']


def set_access_cookies(response, encoded_access_token):
    """
    Takes a flask response object, and configures it to set the encoded access
    token in a cookie (as well as a csrf access cookie if enabled)
    """
    if 'cookies' not in config.token_location:
        raise RuntimeWarning("set_access_cookies() called without "
                             "'JWT_TOKEN_LOCATION' configured to use cookies")

    # Set the access JWT in the cookie
    response.set_cookie(config.access_cookie_name,
                        value=encoded_access_token,
                        max_age=_get_cookie_max_age(),  # TODO move to config
                        secure=config.cookie_secure,
                        httponly=True,
                        path=config.access_cookie_path)

    # If enabled, set the csrf double submit access cookie
    if config.cookie_csrf_protect:
        response.set_cookie(config.access_csrf_cookie_name,
                            value=_get_csrf_token(encoded_access_token),
                            max_age=_get_cookie_max_age(),  # TODO move to config
                            secure=config.cookie_secure,
                            httponly=False,
                            path='/')


def set_refresh_cookies(response, encoded_refresh_token):
    """
    Takes a flask response object, and configures it to set the encoded refresh
    token in a cookie (as well as a csrf refresh cookie if enabled)
    """
    if 'cookies' not in config.token_location:
        raise RuntimeWarning("set_refresh_cookies() called without "
                             "'JWT_TOKEN_LOCATION' configured to use cookies")

    # Set the refresh JWT in the cookie
    response.set_cookie(config.refresh_cookie_name,
                        value=encoded_refresh_token,
                        max_age=_get_cookie_max_age(),  # TODO move to config
                        secure=config.cookie_secure,
                        httponly=True,
                        path=config.refresh_cookie_path)

    # If enabled, set the csrf double submit refresh cookie
    if config.cookie_csrf_protect:
        response.set_cookie(config.refresh_csrf_cookie_name,
                            value=_get_csrf_token(encoded_refresh_token),
                            max_age=_get_cookie_max_age(),  # TODO move to config
                            secure=config.cookie_secure,
                            httponly=False,
                            path='/')


def unset_jwt_cookies(response):
    """
    Takes a flask response object, and configures it to unset (delete) the JWT
    cookies. Basically, this is a logout helper method if using cookies to store
    the JWT
    """
    if 'cookies' not in config.token_location:
        raise RuntimeWarning("unset_refresh_cookies() called without "
                             "'JWT_TOKEN_LOCATION' configured to use cookies")

    response.set_cookie(config.refresh_cookie_name,
                        value='',
                        expires=0,
                        secure=config.cookie_secure,
                        httponly=True,
                        path=config.refresh_cookie_path)
    response.set_cookie(config.access_cookie_name,
                        value='',
                        expires=0,
                        secure=config.cookie_secure,
                        httponly=True,
                        path=config.access_cookie_path)

    if config.cookie_csrf_protect:
        response.set_cookie(config.refresh_csrf_cookie_name,
                            value='',
                            expires=0,
                            secure=config.cookie_secure,
                            httponly=False,
                            path='/')
        response.set_cookie(config.access_csrf_cookie_name,
                            value='',
                            expires=0,
                            secure=config.cookie_secure,
                            httponly=False,
                            path='/')

    return response
