# Redis is a very quick in memory store. The benefits of using redis is that
# things will generally speedy, and it can be (mostly) persistent by dumping
# the data to disk (see: https://redis.io/topics/persistence). The drawbacks
# to using redis is you have a higher chance of encountering data loss (in
# this case, 'forgetting' that a token was revoked), due to events like
# power outages in between making a change to redis and that change being
# dumped for a disk.
#
# So when does it make sense to use redis for a blacklist? If you are blacklist
# every token on logout but doing nothing besides that (not keeping track of
# what tokens are blacklisted, not providing the option un-revoke blacklisted
# tokens, or view tokens that are currently active for a given user), then redis
# is a great choice. Worst case, a few tokens might slip between the cracks in
# the case of a power outage or other such event, but 99.999% of the time tokens
# will be properly blacklisted, and the security of your application should be
# peachy.
#
# Redis also has the benefit of supporting an expires time when storing data.
# Utilizing this, you will not need to manually prune back down the data
# store to keep it from blowing up on you over time. We will show how this
# could work in this example.
#
# If you intend to use some of the other features in your blacklist (tracking
# what tokens are currently active, option to revoke or unrevoke specific
# tokens, etc), data integrity is probably more important to your app then
# raw performance, in which case a sql base solution (such as postgres) is
# probably a better fit for your blacklist. Check out the "sql_blacklist.py"
# example for how that might work.
import redis
from datetime import timedelta
from flask import Flask, request, jsonify
from flask_jwt_extended import (
    JWTManager, create_access_token, create_refresh_token, get_jti,
    jwt_refresh_token_required, get_jwt_identity, jwt_required, get_raw_jwt
)

app = Flask(__name__)
app.secret_key = 'ChangeMe!'

# Setup the flask-jwt-extended extension. See:
# http://flask-jwt-extended.readthedocs.io/en/latest/options.html
ACCESS_EXPIRES = timedelta(minutes=15)
REFRESH_EXPIRES = timedelta(days=30)
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = ACCESS_EXPIRES
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = REFRESH_EXPIRES
app.config['JWT_BLACKLIST_ENABLED'] = True
app.config['JWT_BLACKLIST_TOKEN_CHECKS'] = ['access', 'refresh']
jwt = JWTManager(app)

# Setup our redis connection for storing the blacklisted tokens
revoked_store = redis.StrictRedis(host='localhost', port=6379, db=0)


# Create our function to check if a token has been blacklisted. In this simple
# case, we will just store the tokens jti (unique identifier) in the redis
# store whenever we create it with a revoked status of False. This function
# will grab the revoked status from the store and return it. If a token doesn't
# exist in our store, we don't know where it came from (as we are adding newly
# created # tokens to our store), so we are going to considered to be a
# revoked token for safety purposes. This is obviously optional.
@jwt.token_in_blacklist_loader
def check_if_token_in_blacklist(decrypted_token):
    jti = decrypted_token['jti']
    entry = revoked_store.get(jti)
    if entry is None:
        return False
    return entry == 'true'


@app.route('/auth/login', methods=['POST'])
def login():
    username = request.json.get('username', None)
    password = request.json.get('password', None)
    if username != 'test' or password != 'test':
        return jsonify({"msg": "Bad username or password"}), 401

    # Create our JWTs
    access_token = create_access_token(identity=username)
    refresh_token = create_refresh_token(identity=username)

    # Store the tokens in our store with a status of not currently revoked. We
    # can use the `get_jti()` method to get the unique identifier string for
    # each token. We can also set an expires time on these tokens in redis,
    # so they will get automatically removed after they expire. We will set
    # everything to be automatically removed shortly after the token expires
    access_jti = get_jti(encoded_token=access_token)
    refresh_jti = get_jti(encoded_token=refresh_token)
    revoked_store.set(access_jti, 'false', ACCESS_EXPIRES * 1.2)
    revoked_store.set(refresh_jti, 'false', REFRESH_EXPIRES * 1.2)

    ret = {
        'access_token': access_token,
        'refresh_token': refresh_token
    }
    return jsonify(ret), 200


# A blacklisted refresh tokens will not be able to access this endpoint
@app.route('/auth/refresh', methods=['POST'])
@jwt_refresh_token_required
def refresh():
    # Do the same thing that we did in the login endpoint here
    current_user = get_jwt_identity()
    access_token = create_access_token(identity=current_user)
    access_jti = get_jti(encoded_token=access_token)
    revoked_store.set(access_jti, 'false', ACCESS_EXPIRES * 1.2)
    ret = {'access_token': access_token}
    return jsonify(ret), 200


# Endpoint for revoking the current users access token
@app.route('/auth/access_revoke', methods=['POST'])
@jwt_required
def logout():
    jti = get_raw_jwt()['jti']
    revoked_store.set(jti, 'true', ACCESS_EXPIRES * 1.2)
    return jsonify({"msg": "Access token revoked"}), 200


# Endpoint for revoking the current users refresh token
@app.route('/auth/refresh_revoke', methods=['POST'])
@jwt_refresh_token_required
def logout2():
    jti = get_raw_jwt()['jti']
    revoked_store.set(jti, 'true', REFRESH_EXPIRES * 1.2)
    return jsonify({"msg": "Refresh token revoked"}), 200


# A blacklisted access token will not be able to access this any more
@app.route('/protected', methods=['GET'])
@jwt_required
def protected():
    return jsonify({'hello': 'world'})

if __name__ == '__main__':
    app.run()
