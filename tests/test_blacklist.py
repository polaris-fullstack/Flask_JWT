import time
import unittest
import json
from datetime import timedelta

import simplekv.memory
from flask import Flask, jsonify, request
from flask_jwt_extended.blacklist import _get_token_ttl, get_stored_token
from flask_jwt_extended.tokens import encode_refresh_token, decode_jwt
from flask_jwt_extended.utils import get_jwt_identity, get_raw_jwt

from flask_jwt_extended import (
    JWTManager, create_access_token,
    get_all_stored_tokens, get_stored_tokens, revoke_token, unrevoke_token,
    jwt_required, create_refresh_token, jwt_refresh_token_required,
    fresh_jwt_required
)


class TestEndpoints(unittest.TestCase):

    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'super=secret'
        self.app.config['JWT_BLACKLIST_ENABLED'] = True
        self.app.config['JWT_BLACKLIST_STORE'] = simplekv.memory.DictStore()
        self.jwt_manager = JWTManager(self.app)
        self.client = self.app.test_client()

        @self.app.route('/auth/login', methods=['POST'])
        def login():
            username = request.get_json()['username']
            ret = {
                'access_token': create_access_token(username, fresh=True),
                'refresh_token': create_refresh_token(username)
            }
            return jsonify(ret), 200

        @self.app.route('/auth/token/jti/<jti>', methods=['GET'])
        @self.app.route('/auth/token/encoded_token/<encoded_token>', methods=['GET'])
        @self.app.route('/auth/token/encoded_token/', methods=['GET'])
        def get_single_token(jti=None, encoded_token=None):
            try:
                if jti is not None:
                    return jsonify(get_stored_token(jti=jti)), 200
                else:
                    return jsonify(get_stored_token(encoded_token=encoded_token)), 200
            except KeyError:
                return jsonify({"msg": "token not found"}), 404

        @self.app.route('/auth/tokens/<identity>', methods=['GET'])
        def list_identity_tokens(identity):
            return jsonify(get_stored_tokens(identity)), 200

        @self.app.route('/auth/tokens', methods=['GET'])
        def list_all_tokens():
            return jsonify(get_all_stored_tokens()), 200

        @self.app.route('/auth/revoke/<jti>', methods=['POST'])
        def revoke(jti):
            try:
                revoke_token(jti)
                return jsonify({"msg": "Token revoked"})
            except KeyError:
                return jsonify({"msg": "Token not found"}), 404

        @self.app.route('/auth/unrevoke/<jti>', methods=['POST'])
        def unrevoke(jti):
            try:
                unrevoke_token(jti)
                return jsonify({"msg": "Token unrevoked"})
            except KeyError:
                return jsonify({"msg": "Token not found"}), 404

        @self.app.route('/auth/refresh', methods=['POST'])
        @jwt_refresh_token_required
        def refresh():
            username = get_jwt_identity()
            ret = {'access_token': create_access_token(username, fresh=False)}
            return jsonify(ret), 200

        @self.app.route('/auth/logout', methods=['POST'])
        @jwt_required
        def logout():
            jti = get_raw_jwt()['jti']
            revoke_token(jti)
            ret = {"msg": "Successfully logged out"}
            return jsonify(ret), 200

        @self.app.route('/protected', methods=['POST'])
        @jwt_required
        def protected():
            return jsonify({"hello": "world"})

        @self.app.route('/protected-fresh', methods=['POST'])
        @fresh_jwt_required
        def protected_fresh():
            return jsonify({"hello": "world"})

    def _login(self, username):
        post_data = {'username': username}
        response = self.client.post('/auth/login', data=json.dumps(post_data),
                                    content_type='application/json')
        data = json.loads(response.get_data(as_text=True))
        return data['access_token'], data['refresh_token']

    def _jwt_post(self, url, jwt=None):
        if jwt:
            header = {'Authorization': 'Bearer {}'.format(jwt)}
            response = self.client.post(url, headers=header)
        else:
            response = self.client.post(url)
        status_code = response.status_code
        data = json.loads(response.get_data(as_text=True))
        return status_code, data

    def test_revoke_unrevoke_all_token(self):
        # Check access and refresh tokens
        self.app.config['JWT_BLACKLIST_TOKEN_CHECKS'] = 'all'

        # No tokens initially
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data, [])

        # Login, now should have two tokens (access and refresh) that are not revoked
        self._login('test1')
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data), 2)
        self.assertFalse(data[0]['revoked'])
        self.assertFalse(data[1]['revoked'])

        # Revoke the access token
        access_jti = [x['token']['jti'] for x in data if x['token']['type'] == 'access'][0]
        status, data = self._jwt_post('/auth/revoke/{}'.format(access_jti))
        self.assertEqual(status, 200)
        self.assertIn('msg', data)

        # Verify the access token has been revoked on new lookup
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data), 2)
        if data[0]['token']['jti'] == access_jti:
            self.assertTrue(data[0]['revoked'])
            self.assertFalse(data[1]['revoked'])
        else:
            self.assertFalse(data[0]['revoked'])
            self.assertTrue(data[1]['revoked'])

        # Unrevoke the access token
        status, data = self._jwt_post('/auth/unrevoke/{}'.format(access_jti))
        self.assertEqual(status, 200)
        self.assertIn('msg', data)

        # Make sure token is marked as unrevoked
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data), 2)
        self.assertFalse(data[0]['revoked'])
        self.assertFalse(data[1]['revoked'])

    def test_revoke_unrevoke_refresh_token(self):
        # Check only refresh tokens
        self.app.config['JWT_BLACKLIST_TOKEN_CHECKS'] = 'refresh'

        # No tokens initially
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data, [])

        # Login, now should have one token that is not revoked
        self._login('test1')
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data), 1)
        self.assertFalse(data[0]['revoked'])

        # Revoke the token
        refresh_jti = data[0]['token']['jti']
        status, data = self._jwt_post('/auth/revoke/{}'.format(refresh_jti))
        self.assertEqual(status, 200)
        self.assertIn('msg', data)

        # Verify the token has been revoked on new lookup
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data), 1)
        self.assertTrue(data[0]['revoked'])

        # Unrevoke the token
        status, data = self._jwt_post('/auth/unrevoke/{}'.format(refresh_jti))
        self.assertEqual(status, 200)
        self.assertIn('msg', data)

        # Make sure token is marked as unrevoked
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data), 1)
        self.assertFalse(data[0]['revoked'])

    def test_revoked_access_token_enabled(self):
        # Check access and refresh tokens
        self.app.config['JWT_BLACKLIST_TOKEN_CHECKS'] = 'all'

        # Login
        access_token, refresh_token = self._login('test1')

        # Get the access jti
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        access_jti = [x['token']['jti'] for x in data if x['token']['type'] == 'access'][0]

        # Verify we can initially access the endpoint
        status, data = self._jwt_post('/protected', access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'hello': 'world'})
        status, data = self._jwt_post('/protected-fresh', access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'hello': 'world'})

        # Verify we can no longer access endpoint after revoking
        self._jwt_post('/auth/revoke/{}'.format(access_jti))
        status, data = self._jwt_post('/protected', access_token)
        self.assertEqual(status, 401)
        self.assertIn('msg', data)
        status, data = self._jwt_post('/protected-fresh', access_token)
        self.assertEqual(status, 401)
        self.assertIn('msg', data)

        # Verify refresh token works, and new token can access endpoint
        _, data = self._jwt_post('/auth/refresh', refresh_token)
        new_access_token = data['access_token']
        status, data = self._jwt_post('/protected', new_access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'hello': 'world'})

        # Verify original token can access endpoint after unrevoking
        self._jwt_post('/auth/unrevoke/{}'.format(access_jti))
        status, data = self._jwt_post('/protected', access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'hello': 'world'})
        status, data = self._jwt_post('/protected-fresh', access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'hello': 'world'})

    def test_revoked_access_token_disabled(self):
        # Check only refresh tokens
        self.app.config['JWT_BLACKLIST_TOKEN_CHECKS'] = 'refresh'

        # Login
        access_token, refresh_token = self._login('test1')

        # Nothing should be returned, as this token wasn't saved
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        access_jti = [x for x in data if x['token']['type'] == 'access']
        self.assertEqual(len(access_jti), 0)

        # Verify we can access the endpoint
        status, data = self._jwt_post('/protected', access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'hello': 'world'})

    def test_revoked_refresh_token(self):
        # Check only refresh tokens
        self.app.config['JWT_BLACKLIST_TOKEN_CHECKS'] = 'refresh'

        # Login
        access_token, refresh_token = self._login('test1')

        # Get the access jti
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        refresh_jti = [x['token']['jti'] for x in data
                       if x['token']['type'] == 'refresh'][0]

        # Verify we can initially access the refresh endpoint
        status, data = self._jwt_post('/auth/refresh', refresh_token)
        self.assertEqual(status, 200)
        self.assertIn('access_token', data)

        # Verify we can no longer access the refresh endpoint after revoking
        self._jwt_post('/auth/revoke/{}'.format(refresh_jti))
        status, data = self._jwt_post('/auth/refresh', refresh_token)
        self.assertEqual(status, 401)
        self.assertIn('msg', data)

        # Verify we can access again after unrevoking
        self._jwt_post('/auth/unrevoke/{}'.format(refresh_jti))
        status, data = self._jwt_post('/auth/refresh', refresh_token)
        self.assertEqual(status, 200)
        self.assertIn('access_token', data)

    def test_login_logout(self):
        # Check access and refresh tokens
        self.app.config['JWT_BLACKLIST_TOKEN_CHECKS'] = 'all'

        # Login
        access_token, refresh_token = self._login('test12345')

        # Verify we can access the protected endpoint
        status, data = self._jwt_post('/protected', access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'hello': 'world'})

        # Logout
        status, data = self._jwt_post('/auth/logout', access_token)
        self.assertEqual(status, 200)
        self.assertEqual(data, {'msg': 'Successfully logged out'})

        # Verify that we cannot access the protected endpoint anymore
        status, data = self._jwt_post('/protected', access_token)
        self.assertEqual(status, 401)
        self.assertEqual(data, {'msg': 'Token has been revoked'})

    def test_bad_blacklist_settings(self):
        app = Flask(__name__)
        app.testing = True  # Propagate exceptions
        JWTManager(app)
        client = app.test_client()

        @app.route('/list-tokens')
        def list_tokens():
            return jsonify(get_all_stored_tokens())

        # Check calling blacklist function if blacklist is disabled
        app.config['JWT_BLACKLIST_ENABLED'] = False
        with self.assertRaises(RuntimeError):
            client.get('/list-tokens')

        # Check calling blacklist function if store is not set
        app.config['JWT_BLACKLIST_ENABLED'] = True
        app.config['JWT_BLACKLIST_STORE'] = None
        with self.assertRaises(RuntimeError):
            client.get('/list-tokens')

        # Check calling blacklist function if invalid blacklist check type
        app.config['JWT_BLACKLIST_ENABLED'] = True
        app.config['JWT_BLACKLIST_STORE'] = {}
        with self.assertRaises(RuntimeError):
            client.get('/list-tokens')

    def test_get_token_ttl(self):
        # This is called when using a simplekv backend that supports ttl (such
        # as redis or memcached). Because I do not want to require having those
        # installed to run the unit tests, I'm going to fiat that the code for
        # them works, and manually test the helper methods they call for correctness.

        # Test token ttl
        with self.app.test_request_context():
            token_str = encode_refresh_token('foo', 'secret', 'HS256',
                                              timedelta(minutes=5), csrf=False)
            token = decode_jwt(token_str, 'secret', 'HS256', csrf=False)
            time.sleep(2)
            token_ttl = _get_token_ttl(token).total_seconds()
            self.assertGreater(token_ttl, 296)
            self.assertLessEqual(token_ttl, 298)

        # Test ttl is 0 if token is already expired
        with self.app.test_request_context():
            token_str = encode_refresh_token('foo', 'secret', 'HS256',
                                              timedelta(seconds=0), csrf=False)
            token = decode_jwt(token_str, 'secret', 'HS256', csrf=False)
            time.sleep(2)
            token_ttl = _get_token_ttl(token).total_seconds()
            self.assertEqual(token_ttl, 0)

    def test_revoke_invalid_token(self):
        status, data = self._jwt_post('/auth/revoke/404_token_not_found')
        self.assertEqual(status, 404)
        self.assertIn('msg', data)

    def test_get_specific_identity(self):
        self._login('test1')
        self._login('test1')
        self._login('test1')
        self._login('test2')

        response = self.client.get('/auth/tokens/test1')
        status_code = response.status_code
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(status_code, 200)
        self.assertEqual(len(data), 3)

        response = self.client.get('/auth/tokens/test2')
        status_code = response.status_code
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(status_code, 200)
        self.assertEqual(len(data), 1)

        response = self.client.get('/auth/tokens/test3')
        status_code = response.status_code
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(status_code, 200)
        self.assertEqual(len(data), 0)

    def test_get_stored_token(self):
        access_token, refresh_token = self._login('test1')
        response = self.client.get('/auth/tokens')
        data = json.loads(response.get_data(as_text=True))
        refresh_jti = data[0]['token']['jti']

        # Test getting the token by passing in JTI
        response = self.client.get('/auth/token/jti/{}'.format(refresh_jti))
        status_code = response.status_code
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(status_code, 200)
        self.assertIn('token', data)
        self.assertIn('revoked', data)
        self.assertEqual(len(data), 2)

        # Test getting the token by passing in the encoded token
        response = self.client.get('/auth/token/encoded_token/{}'.format(refresh_token))
        status_code = response.status_code
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(status_code, 200)
        self.assertIn('token', data)
        self.assertIn('revoked', data)
        self.assertEqual(len(data), 2)

        # Test passing neither throws an exception
        with self.assertRaises(ValueError):
            self.client.get('/auth/token/encoded_token/')

        response = self.client.get('/auth/token/jti/404notokenfound')
        status_code = response.status_code
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(status_code, 404)
        self.assertIn('msg', data)
