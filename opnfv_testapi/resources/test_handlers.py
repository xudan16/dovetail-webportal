##############################################################################
# Copyright (c) 2015 Orange
# guyrodrigue.koffi@orange.com / koffirodrigue@gmail.com
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the Apache License, Version 2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
##############################################################################
import logging
import os
import json

from tornado import web
from tornado import gen
from bson import objectid

from opnfv_testapi.common.config import CONF
from opnfv_testapi.common import message
from opnfv_testapi.common import raises
from opnfv_testapi.resources import handlers
from opnfv_testapi.resources import test_models
from opnfv_testapi.tornado_swagger import swagger
from opnfv_testapi.ui.auth import constants as auth_const
from opnfv_testapi.db import api as dbapi

DOVETAIL_RESULTS_PATH = '/home/testapi/logs/{}/results/results.json'
DOVETAIL_LOG_PATH = '/home/testapi/logs/{}/results/dovetail.log'


class GenericTestHandler(handlers.GenericApiHandler):
    def __init__(self, application, request, **kwargs):
        super(GenericTestHandler, self).__init__(application,
                                                 request,
                                                 **kwargs)
        self.table = "tests"
        self.table_cls = test_models.Test


class TestsCLHandler(GenericTestHandler):
    @swagger.operation(nickname="queryTests")
    @web.asynchronous
    @gen.coroutine
    def get(self):
        """
            @description: Retrieve result(s) for a test project
                          on a specific pod.
            @notes: Retrieve result(s) for a test project on a specific pod.
                Available filters for this request are :
                 - id  : Test id
                 - period : x last days, incompatible with from/to
                 - from : starting time in 2016-01-01 or 2016-01-01 00:01:23
                 - to : ending time in 2016-01-01 or 2016-01-01 00:01:23
                 - signed : get logined user result

                GET /results/project=functest&case=vPing&version=Arno-R1 \
                &pod=pod_name&period=15&signed
            @return 200: all test results consist with query,
                         empty list if no result is found
            @rtype: L{Tests}
        """
        def descend_limit():
            descend = self.get_query_argument('descend', 'true')
            return -1 if descend.lower() == 'true' else 1

        def last_limit():
            return self.get_int('last', self.get_query_argument('last', 0))

        def page_limit():
            return self.get_int('page', self.get_query_argument('page', 0))

        limitations = {
            'sort': {'_id': descend_limit()},
            'last': last_limit(),
            'page': page_limit(),
            'per_page': CONF.api_results_per_page
        }

        curr_user = self.get_secure_cookie(auth_const.OPENID)
        if curr_user is None:
            raises.Unauthorized(message.no_auth())

        review = self.request.query_arguments.pop('review', None)
        query = yield self.set_query()
        if review:
            yield self._list(query=query, res_op=self.check_review,
                             **limitations)
        else:
            yield self._list(query=query, **limitations)
        logging.debug('list end')

    @gen.coroutine
    def check_review(self, data, *args):
        current_user = self.get_secure_cookie(auth_const.OPENID)
        for test in data:
            query = {'reviewer_openid': current_user, 'test_id': test['id']}
            ret = yield dbapi.db_find_one('reviews', query)
            if ret:
                test['voted'] = 'true'
            else:
                test['voted'] = 'false'

        raise gen.Return({self.table: data})

    @swagger.operation(nickname="createTest")
    @web.asynchronous
    def post(self):
        """
            @description: create a test
            @param body: test to be created
            @type body: L{TestCreateRequest}
            @in body: body
            @rtype: L{CreateResponse}
            @return 200: test is created.
            @raise 404: pod/project/testcase not exist
            @raise 400: body/pod_name/project_name/case_name not provided
        """
        openid = self.get_secure_cookie(auth_const.OPENID)
        if openid:
            self.json_args['owner'] = openid

        self._post()

    @gen.coroutine
    def _post(self):
        miss_fields = []
        carriers = []
        query = {'owner': self.json_args['owner'], 'id': self.json_args['id']}
        ret, msg = yield self._check_if_exists(table="tests", query=query)
        if ret:
            self.finish_request({'code': '403', 'msg': msg})
            return

        if self.is_onap:
            self.json_args['is_onap'] = 'true'
        self._create(miss_fields=miss_fields, carriers=carriers)


class TestsGURHandler(GenericTestHandler):

    @swagger.operation(nickname="getTestById")
    @web.asynchronous
    @gen.coroutine
    def get(self, test_id):
        query = dict()
        query["_id"] = objectid.ObjectId(test_id)

        data = yield dbapi.db_find_one(self.table, query)
        if not data:
            raises.NotFound(message.not_found(self.table, query))

        # only do this when it's nfvi not vnf
        if 'is_onap' not in data.keys() or data['is_onap'] != 'true':
            validation = yield self._check_api_response_validation(data['id'])
            data.update({'validation': validation})

        self.finish_request(self.format_data(data))

    @gen.coroutine
    def _check_api_response_validation(self, test_id):
        results_path = DOVETAIL_RESULTS_PATH.format(test_id)
        log_path = DOVETAIL_LOG_PATH.format(test_id)
        res = None

        # For release after 2018.09
        # Dovetail adds 'validation' directly into results.json
        if os.path.exists(results_path):
            with open(results_path) as f:
                try:
                    data = json.load(f)
                    if data['validation'] == 'enabled':
                        res = 'API response validation enabled'
                    else:
                        res = 'API response validation disabled'
                except Exception:
                    pass
        if res:
            raise gen.Return(res)

        # For 2018.01 and 2018.09
        # Need to check dovetail.log for this info
        if os.path.exists(log_path):
            with open(log_path) as f:
                log_content = f.read()
                warning_keyword = 'Strict API response validation DISABLED'
                if warning_keyword in log_content:
                    raise gen.Return('API response validation disabled')
                else:
                    raise gen.Return('API response validation enabled')

        raises.Forbidden('neither results.json nor dovetail.log are found')

    @swagger.operation(nickname="deleteTestById")
    @gen.coroutine
    def delete(self, test_id):
        curr_user = self.get_secure_cookie(auth_const.OPENID)
        curr_user_role = self.get_secure_cookie(auth_const.ROLE)
        if curr_user is not None:
            query = {'_id': objectid.ObjectId(test_id)}
            test_data = yield dbapi.db_find_one(self.table, query)
            if not test_data:
                raises.NotFound(message.not_found(self.table, query))
            if curr_user == test_data['owner'] or \
               curr_user_role.find('administrator') != -1:
                yield dbapi.db_delete('applications',
                                      {'test_id': test_data['id']})
                yield dbapi.db_delete('reviews', {'test_id': test_data['id']})
                self._delete(query=query)
            else:
                raises.Forbidden(message.no_auth())
        else:
            raises.Unauthorized(message.no_auth())

    @swagger.operation(nickname="updateTestById")
    @web.asynchronous
    def put(self, _id):
        """
            @description: update a single test by id
            @param body: fields to be updated
            @type body: L{TestUpdateRequest}
            @in body: body
            @rtype: L{Test}
            @return 200: update success
            @raise 404: Test not exist
            @raise 403: nothing to update
        """
        logging.debug('put')
        data = json.loads(self.request.body)
        item = data.get('item')
        value = data.get(item)
        logging.debug('%s:%s', item, value)
        try:
            self.update(_id, item, value)
        except Exception as e:
            logging.error('except:%s', e)
            return

    @gen.coroutine
    def _convert_to_id(self, email):
        query = {"email": email}
        table = "users"
        if query and table:
            data = yield dbapi.db_find_one(table, query)
            if data:
                raise gen.Return((True, 'Data already exists. %s' % (query),
                                  data.get("openid")))
        raise gen.Return((False, 'Data does not exist. %s' % (query), None))

    @gen.coroutine
    def update(self, _id, item, value):
        logging.debug("update")
        if item == "shared":
            new_list = []
            for user in value:
                ret, msg, user_id = yield self._convert_to_id(user)
                if ret:
                    user = user_id
                new_list.append(user)
                query = {"$or": [{"openid": user}, {"email": user}]}
                table = "users"
                ret, msg = yield self._check_if_exists(table=table,
                                                       query=query)
                logging.debug('ret:%s', ret)
                if not ret:
                    self.finish_request({'code': '403', 'msg': msg})
                    return

            if len(new_list) != len(set(new_list)):
                msg = "Already shared with this user"
                self.finish_request({'code': '403', 'msg': msg})
                return

        logging.debug("before _update")
        self.json_args = {}
        self.json_args[item] = value
        ret, msg = yield self.check_auth(item, value)
        if not ret:
            self.finish_request({'code': '404', 'msg': msg})
            return

        query = {'_id': objectid.ObjectId(_id)}
        db_keys = ['_id', ]

        test = yield dbapi.db_find_one("tests", query)
        if not test:
            msg = 'Record does not exist'
            self.finish_request({'code': 404, 'msg': msg})
            return

        curr_user = self.get_secure_cookie(auth_const.OPENID)
        if item in {"shared", "label", "sut_label"}:
            query['owner'] = curr_user
            db_keys.append('owner')

        if item == 'sut_label':
            if test['status'] != 'private' and not value:
                msg = 'SUT version cannot be changed to None after submitting.'
                self.finish_request({'code': 403, 'msg': msg})
                return

        if item == "status":
            if value == 'verified':
                if test['status'] == 'private':
                    msg = 'Not allowed to verify'
                    self.finish_request({'code': 403, 'msg': msg})
                    return

                user = yield dbapi.db_find_one("users", {'openid': curr_user})
                if 'administrator' not in user['role']:
                    msg = 'No permission to operate'
                    self.finish_request({'code': 403, 'msg': msg})
                    return
            elif value == 'review':
                if test['status'] != 'private':
                    msg = 'Not allowed to submit to review'
                    self.finish_request({'code': 403, 'msg': msg})
                    return

                query['owner'] = curr_user
                db_keys.append('owner')

                test_query = {
                    'id': test['id'],
                    '$or': [
                        {'status': 'review'},
                        {'status': 'verified'}
                    ]
                }
                record = yield dbapi.db_find_one("tests", test_query)
                if record:
                    msg = ('{} has already submitted one record with the same '
                           'Test ID: {}'.format(record['owner'], test['id']))
                    self.finish_request({'code': 403, 'msg': msg})
                    return
            else:
                query['owner'] = curr_user
                db_keys.append('owner')

        logging.debug("before _update 2")
        self._update(query=query, db_keys=db_keys)

    @gen.coroutine
    def check_auth(self, item, value):
        logging.debug('check_auth')
        user = self.get_secure_cookie(auth_const.OPENID)
        query = {}
        if item == "status":
            if value == "private" or value == "review":
                logging.debug('check review')
                query['user_id'] = user
                data = yield dbapi.db_find_one('applications', query)
                if data:
                    logging.debug('results are bound to an application')
                    raise gen.Return((False, message.no_auth()))
            if value == "verified":
                logging.debug('check verify')
                query['role'] = {"$regex": ".*administrator.*"}
                query['openid'] = user
                data = yield dbapi.db_find_one('users', query)
                if not data:
                    logging.debug('not found')
                    raise gen.Return((False, message.no_auth()))
        raise gen.Return((True, {}))
