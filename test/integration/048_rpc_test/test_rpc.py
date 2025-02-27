import json
import multiprocessing
import os
import random
import signal
import socket
import time
from base64 import standard_b64encode as b64
from datetime import datetime

import requests
from pytest import mark

from test.integration.base import DBTIntegrationTest, use_profile
from dbt.logger import log_manager
from dbt.main import handle_and_check


class ServerProcess(multiprocessing.Process):
    def __init__(self, port, profiles_dir, cli_vars=None):
        self.port = port
        handle_and_check_args = [
            '--strict', 'rpc', '--log-cache-events',
            '--port', str(self.port),
            '--profiles-dir', profiles_dir
        ]
        if cli_vars:
            handle_and_check_args.extend(['--vars', cli_vars])
        super().__init__(
            target=handle_and_check,
            args=(handle_and_check_args,),
            name='ServerProcess')

    def run(self):
        log_manager.reset_handlers()
        # run server tests in stderr mode
        log_manager.stderr_console()
        return super().run()

    def can_connect(self):
        sock = socket.socket()
        try:
            sock.connect(('localhost', self.port))
        except socket.error:
            return False
        sock.close()
        return True

    def _compare_result(self, result):
        return result['result']['status'] == 'ready'

    def status_ok(self):
        result = query_url(
            'http://localhost:{}/jsonrpc'.format(self.port),
            {'method': 'status', 'id': 1, 'jsonrpc': 2.0}
        ).json()
        return self._compare_result(result)

    def is_up(self):
        if not self.can_connect():
            return False
        return self.status_ok()

    def start(self):
        super().start()
        for _ in range(20):
            if self.is_up():
                break
            time.sleep(0.5)
        if not self.can_connect():
            raise Exception('server never appeared!')
        status_result = query_url(
            'http://localhost:{}/jsonrpc'.format(self.port),
            {'method': 'status', 'id': 1, 'jsonrpc': 2.0}
        ).json()
        if not self._compare_result(status_result):
            raise Exception('Got invalid status result: {}'.format(status_result))


def query_url(url, query):
    headers = {'content-type': 'application/json'}
    return requests.post(url, headers=headers, data=json.dumps(query))


class BackgroundQueryProcess(multiprocessing.Process):
    def __init__(self, query, url, group=None, name=None):
        parent, child = multiprocessing.Pipe()
        self.parent_pipe = parent
        self.child_pipe = child
        self.query = query
        self.url = url
        super().__init__(group=group, name=name)

    def run(self):
        try:
            result = query_url(self.url, self.query).json()
        except Exception as exc:
            self.child_pipe.send(('error', str(exc)))
        else:
            self.child_pipe.send(('result', result))

    def wait_result(self):
        result_type, result = self.parent_pipe.recv()
        self.join()
        if result_type == 'error':
            raise Exception(result)
        else:
            return result


_select_from_ephemeral = '''with __dbt__CTE__ephemeral_model as (


select 1 as id
)select * from __dbt__CTE__ephemeral_model'''


def addr_in_use(err, *args):
    msg = str(err)
    if 'Address already in use' in msg:
        return True
    if 'server never appeared!' in msg:
        return True  # this can happen because of the above
    return False


class HasRPCServer(DBTIntegrationTest):
    ServerProcess = ServerProcess
    should_seed = True

    def setUp(self):
        super().setUp()
        os.environ['DBT_TEST_SCHEMA_NAME_VARIABLE'] = 'test_run_schema'
        if self.should_seed:
            self.run_dbt_with_vars(['seed'], strict=False)
        port = random.randint(20000, 65535)
        self._server = self.ServerProcess(
            cli_vars='{{test_run_schema: {}}}'.format(self.unique_schema()),
            profiles_dir=self.test_root_dir,
            port=port
        )
        self._server.start()
        self.background_queries = []

    def tearDown(self):
        del os.environ['DBT_TEST_SCHEMA_NAME_VARIABLE']
        self._server.terminate()
        for query in self.background_queries:
            query.terminate()
        super().tearDown()

    @property
    def schema(self):
        return "rpc_048"

    @property
    def models(self):
        return "models"

    def run_dbt_with_vars(self, cmd, *args, **kwargs):
        cmd.extend(['--vars',
                    '{{test_run_schema: {}}}'.format(self.unique_schema())])
        return self.run_dbt(cmd, *args, **kwargs)

    @property
    def project_config(self):
        return {
            'data-paths': ['data'],
            'quoting': {'database': True, 'schema': True, 'identifier': True},
            'macro-paths': ['macros'],
        }

    def build_query(
        self, method, kwargs, sql=None, test_request_id=1, macros=None
    ):
        body_data = ''
        if sql is not None:
            body_data += sql

        if macros is not None:
            body_data += macros

        if sql is not None or macros is not None:
            kwargs['sql'] = b64(body_data.encode('utf-8')).decode('utf-8')

        return {
            'jsonrpc': '2.0',
            'method': method,
            'params': kwargs,
            'id': test_request_id
        }

    @property
    def url(self):
        return 'http://localhost:{}/jsonrpc'.format(self._server.port)

    def query(self, _method, _sql=None, _test_request_id=1, macros=None, **kwargs):
        built = self.build_query(_method, kwargs, _sql, _test_request_id, macros)
        return query_url(self.url, built)

    def handle_result(self, bg_query, pipe):
        result_type, result = pipe.recv()
        bg_query.join()
        if result_type == 'error':
            raise result
        else:
            return result

    def background_query(
        self, _method, _sql=None, _test_request_id=1, _block=False, macros=None, **kwargs
    ):
        built = self.build_query(_method, kwargs, _sql, _test_request_id,
                                 macros)

        url = 'http://localhost:{}/jsonrpc'.format(self._server.port)
        name = _method
        if 'name' in kwargs:
            name += ' ' + kwargs['name']
        bg_query = BackgroundQueryProcess(built, url, name=name)
        self.background_queries.append(bg_query)
        bg_query.start()
        return bg_query

    def assertResultHasTimings(self, result, *names):
        self.assertIn('timing', result)
        timings = result['timing']
        self.assertEqual(len(timings), len(names))
        for expected_name, timing in zip(names, timings):
            self.assertIn('name', timing)
            self.assertEqual(timing['name'], expected_name)
            self.assertIn('started_at', timing)
            self.assertIn('completed_at', timing)
            datetime.strptime(timing['started_at'], '%Y-%m-%dT%H:%M:%S.%fZ')
            datetime.strptime(timing['completed_at'], '%Y-%m-%dT%H:%M:%S.%fZ')

    def assertIsResult(self, data, id_=1):
        self.assertEqual(data['id'], id_)
        self.assertEqual(data['jsonrpc'], '2.0')
        self.assertIn('result', data)
        self.assertNotIn('error', data)
        return data['result']

    def assertIsError(self, data, id_=1):
        self.assertEqual(data['id'], id_)
        self.assertEqual(data['jsonrpc'], '2.0')
        self.assertIn('error', data)
        self.assertNotIn('result', data)
        return data['error']

    def assertIsErrorWithCode(self, data, code, id_=1):
        error = self.assertIsError(data, id_)
        self.assertIn('code', error)
        self.assertIn('message', error)
        self.assertEqual(error['code'], code)
        return error

    def assertIsErrorWith(self, data, code, message, error_data):
        error = self.assertIsErrorWithCode(data, code)
        if message is not None:
            self.assertEqual(error['message'], message)

        if error_data is not None:
            return self.assertHasErrorData(error, error_data)
        else:
            return error.get('data')

    def assertResultHasSql(self, data, raw_sql, compiled_sql=None):
        if compiled_sql is None:
            compiled_sql = raw_sql
        result = self.assertIsResult(data)
        self.assertIn('logs', result)
        self.assertTrue(len(result['logs']) > 0)
        self.assertIn('raw_sql', result)
        self.assertIn('compiled_sql', result)
        self.assertEqual(result['raw_sql'], raw_sql)
        self.assertEqual(result['compiled_sql'], compiled_sql)
        return result

    def assertSuccessfulCompilationResult(self, data, raw_sql, compiled_sql=None):
        result = self.assertResultHasSql(data, raw_sql, compiled_sql)
        self.assertNotIn('table', result)
        # compile results still have an 'execute' timing, it just represents
        # the time to construct a result object.
        self.assertResultHasTimings(result, 'compile', 'execute')

    def assertSuccessfulRunResult(self, data, raw_sql, compiled_sql=None, table=None):
        result = self.assertResultHasSql(data, raw_sql, compiled_sql)
        self.assertIn('table', result)
        if table is not None:
            self.assertEqual(result['table'], table)
        self.assertResultHasTimings(result, 'compile', 'execute')


@mark.flaky(rerun_filter=addr_in_use)
class TestRPCServer(HasRPCServer):
    @use_profile('postgres')
    def test_compile_postgres(self):
        trivial = self.query(
            'compile',
            'select 1 as id',
            name='foo'
        ).json()
        self.assertSuccessfulCompilationResult(
            trivial, 'select 1 as id'
        )

        ref = self.query(
            'compile',
            'select * from {{ ref("descendant_model") }}',
            name='foo'
        ).json()
        self.assertSuccessfulCompilationResult(
            ref,
            'select * from {{ ref("descendant_model") }}',
            compiled_sql='select * from "{}"."{}"."descendant_model"'.format(
                self.default_database,
                self.unique_schema())
        )

        source = self.query(
            'compile',
            'select * from {{ source("test_source", "test_table") }}',
            name='foo'
        ).json()
        self.assertSuccessfulCompilationResult(
            source,
            'select * from {{ source("test_source", "test_table") }}',
            compiled_sql='select * from "{}"."{}"."source"'.format(
                self.default_database,
                self.unique_schema())
            )

        macro = self.query(
            'compile',
            'select {{ my_macro() }}',
            name='foo',
            macros='{% macro my_macro() %}1 as id{% endmacro %}'
        ).json()
        self.assertSuccessfulCompilationResult(
            macro,
            'select {{ my_macro() }}',
            compiled_sql='select 1 as id'
        )

        macro_override = self.query(
            'compile',
            'select {{ happy_little_macro() }}',
            name='foo',
            macros='{% macro override_me() %}2 as id{% endmacro %}'
        ).json()
        self.assertSuccessfulCompilationResult(
            macro_override,
            'select {{ happy_little_macro() }}',
            compiled_sql='select 2 as id'
        )

        macro_override_with_if_statement = self.query(
            'compile',
            '{% if True %}select {{ happy_little_macro() }}{% endif %}',
            name='foo',
            macros='{% macro override_me() %}2 as id{% endmacro %}'
        ).json()
        self.assertSuccessfulCompilationResult(
            macro_override_with_if_statement,
            '{% if True %}select {{ happy_little_macro() }}{% endif %}',
            compiled_sql='select 2 as id'
        )

        ephemeral = self.query(
            'compile',
            'select * from {{ ref("ephemeral_model") }}',
            name='foo'
        ).json()
        self.assertSuccessfulCompilationResult(
            ephemeral,
            'select * from {{ ref("ephemeral_model") }}',
            compiled_sql=_select_from_ephemeral
        )

    @use_profile('postgres')
    def test_run_postgres(self):
        # seed + run dbt to make models before using them!
        self.run_dbt_with_vars(['seed'])
        self.run_dbt_with_vars(['run'])
        data = self.query(
            'run',
            'select 1 as id',
            name='foo'
        ).json()
        self.assertSuccessfulRunResult(
            data, 'select 1 as id', table={'column_names': ['id'], 'rows': [[1.0]]}
        )

        ref = self.query(
            'run',
            'select * from {{ ref("descendant_model") }} order by updated_at limit 1',
            name='foo'
        ).json()
        self.assertSuccessfulRunResult(
            ref,
            'select * from {{ ref("descendant_model") }} order by updated_at limit 1',
            compiled_sql='select * from "{}"."{}"."descendant_model" order by updated_at limit 1'.format(
                self.default_database,
                self.unique_schema()),
            table={
                'column_names': ['favorite_color', 'id', 'first_name', 'email', 'ip_address', 'updated_at'],
                'rows': [['blue', 38.0, 'Gary',  'gray11@statcounter.com', "'40.193.124.56'", '1970-01-27T10:04:51']],
            }
        )

        source = self.query(
            'run',
            'select * from {{ source("test_source", "test_table") }} order by updated_at limit 1',
            name='foo'
        ).json()
        self.assertSuccessfulRunResult(
            source,
            'select * from {{ source("test_source", "test_table") }} order by updated_at limit 1',
            compiled_sql='select * from "{}"."{}"."source" order by updated_at limit 1'.format(
                self.default_database,
                self.unique_schema()),
            table={
                'column_names': ['favorite_color', 'id', 'first_name', 'email', 'ip_address', 'updated_at'],
                'rows': [['blue', 38.0, 'Gary',  'gray11@statcounter.com', "'40.193.124.56'", '1970-01-27T10:04:51']],
            }
        )

        macro = self.query(
            'run',
            'select {{ my_macro() }}',
            name='foo',
            macros='{% macro my_macro() %}1 as id{% endmacro %}'
        ).json()
        self.assertSuccessfulRunResult(
            macro,
            raw_sql='select {{ my_macro() }}',
            compiled_sql='select 1 as id',
            table={'column_names': ['id'], 'rows': [[1.0]]}
        )

        macro_override = self.query(
            'run',
            'select {{ happy_little_macro() }}',
            name='foo',
            macros='{% macro override_me() %}2 as id{% endmacro %}'
        ).json()
        self.assertSuccessfulRunResult(
            macro_override,
            raw_sql='select {{ happy_little_macro() }}',
            compiled_sql='select 2 as id',
            table={'column_names': ['id'], 'rows': [[2.0]]}
        )

        macro_override_with_if_statement = self.query(
            'run',
            '{% if True %}select {{ happy_little_macro() }}{% endif %}',
            name='foo',
            macros='{% macro override_me() %}2 as id{% endmacro %}'
        ).json()
        self.assertSuccessfulRunResult(
            macro_override_with_if_statement,
            '{% if True %}select {{ happy_little_macro() }}{% endif %}',
            compiled_sql='select 2 as id',
            table={'column_names': ['id'], 'rows': [[2.0]]}
        )

        macro_with_raw_statement = self.query(
            'run',
            '{% raw %}select 1 as{% endraw %}{{ test_macros() }}{% macro test_macros() %} id{% endmacro %}',
            name='foo'
        ).json()
        self.assertSuccessfulRunResult(
            macro_with_raw_statement,
            '{% raw %}select 1 as{% endraw %}{{ test_macros() }}',
            compiled_sql='select 1 as id',
            table={'column_names': ['id'], 'rows': [[1.0]]}
        )

        macro_with_comment = self.query(
            'run',
            '{% raw %}select 1 {% endraw %}{{ test_macros() }} {# my comment #}{% macro test_macros() -%} as{% endmacro %} id{# another comment #}',
            name='foo'
        ).json()
        self.assertSuccessfulRunResult(
            macro_with_comment,
            '{% raw %}select 1 {% endraw %}{{ test_macros() }} {# my comment #} id{# another comment #}',
            compiled_sql='select 1 as  id',
            table={'column_names': ['id'], 'rows': [[1.0]]}
        )

        ephemeral = self.query(
            'run',
            'select * from {{ ref("ephemeral_model") }}',
            name='foo'
        ).json()
        self.assertSuccessfulRunResult(
            ephemeral,
            raw_sql='select * from {{ ref("ephemeral_model") }}',
            compiled_sql=_select_from_ephemeral,
            table={'column_names': ['id'], 'rows': [[1.0]]}
        )

    @mark.skipif(os.name == 'nt', reason='"kill" not supported on windows')
    @mark.flaky(rerun_filter=None)
    @use_profile('postgres')
    def test_ps_kill_postgres(self):
        done_query = self.query('compile', 'select 1 as id', name='done').json()
        self.assertIsResult(done_query)
        pg_sleeper, sleep_task_id, request_id = self._get_sleep_query()

        empty_ps_result = self.query('ps', completed=False, active=False).json()
        result = self.assertIsResult(empty_ps_result)
        self.assertEqual(len(result['rows']), 0)

        sleeper_ps_result = self.query('ps', completed=False, active=True).json()
        result = self.assertIsResult(sleeper_ps_result)
        self.assertEqual(len(result['rows']), 1)
        rowdict = result['rows']
        self.assertEqual(rowdict[0]['request_id'], request_id)
        self.assertEqual(rowdict[0]['method'], 'run')
        self.assertEqual(rowdict[0]['state'], 'running')
        self.assertEqual(rowdict[0]['timeout'], None)

        complete_ps_result = self.query('ps', completed=True, active=False).json()
        result = self.assertIsResult(complete_ps_result)
        self.assertEqual(len(result['rows']), 1)
        rowdict = result['rows']
        self.assertEqual(rowdict[0]['request_id'], 1)
        self.assertEqual(rowdict[0]['method'], 'compile')
        self.assertEqual(rowdict[0]['state'], 'finished')
        self.assertEqual(rowdict[0]['timeout'], None)

        all_ps_result = self.query('ps', completed=True, active=True).json()
        result = self.assertIsResult(all_ps_result)
        self.assertEqual(len(result['rows']), 2)
        rowdict = result['rows']
        rowdict.sort(key=lambda r: r['start'])
        self.assertEqual(rowdict[0]['request_id'], 1)
        self.assertEqual(rowdict[0]['method'], 'compile')
        self.assertEqual(rowdict[0]['state'], 'finished')
        self.assertEqual(rowdict[0]['timeout'], None)
        self.assertEqual(rowdict[1]['request_id'], request_id)
        self.assertEqual(rowdict[1]['method'], 'run')
        self.assertEqual(rowdict[1]['state'], 'running')
        self.assertEqual(rowdict[1]['timeout'], None)

        self.kill_and_assert(pg_sleeper, sleep_task_id, request_id)

    def kill_and_assert(self, pg_sleeper, task_id, request_id):
        kill_result = self.query('kill', task_id=task_id).json()
        result = self.assertIsResult(kill_result)
        self.assertEqual(result['state'], 'killed')

        sleeper_result = pg_sleeper.wait_result()
        error = self.assertIsErrorWithCode(sleeper_result, 10009, request_id)
        self.assertEqual(error['message'], 'RPC process killed')
        self.assertIn('data', error)
        error_data = error['data']
        self.assertEqual(error_data['signum'], 2)
        self.assertEqual(error_data['message'], 'RPC process killed by signal 2')
        self.assertIn('logs', error_data)
        return error_data

    def _get_sleep_query(self, request_id=90890, duration=15):
        pg_sleeper = self.background_query(
            'run',
            'select pg_sleep({})'.format(duration),
            _test_request_id=request_id,
            name='sleeper',
        )

        for _ in range(20):
            time.sleep(0.2)
            sleeper_ps_result = self.query('ps', completed=False, active=True).json()
            result = self.assertIsResult(sleeper_ps_result)
            rows = result['rows']
            for row in rows:
                if row['request_id'] == request_id and row['state'] == 'running':
                    return pg_sleeper, row['task_id'], request_id

        self.assertTrue(False, 'request ID never found running!')

    @mark.skipif(os.name == 'nt', reason='"kill" not supported on windows')
    @mark.flaky(rerun_filter=lambda *a, **kw: True)
    @use_profile('postgres')
    def test_ps_kill_longwait_postgres(self):
        pg_sleeper, sleep_task_id, request_id = self._get_sleep_query()

        # the test above frequently kills the process during parsing of the
        # requested node. That's also a useful test, but we should test that
        # we cancel the in-progress sleep query.
        time.sleep(3)

        error_data = self.kill_and_assert(pg_sleeper, sleep_task_id, request_id)
        # we should have logs if we did anything
        self.assertTrue(len(error_data['logs']) > 0)

    @use_profile('postgres')
    def test_invalid_requests_postgres(self):
        data = self.query(
            'xxxxxnotamethodxxxxx',
            'hi this is not sql'
        ).json()
        self.assertIsErrorWith(data, -32601, 'Method not found', None)

        data = self.query(
            'compile',
            'select * from {{ reff("nonsource_descendant") }}',
            name='mymodel'
        ).json()
        error_data = self.assertIsErrorWith(data, 10004, 'Compilation Error', {
            'type': 'CompilationException',
            'message': "Compilation Error in rpc mymodel (from remote system)\n  'reff' is undefined",
            'compiled_sql': None,
            'raw_sql': 'select * from {{ reff("nonsource_descendant") }}',
        })
        self.assertIn('logs', error_data)
        self.assertTrue(len(error_data['logs']) > 0)

        data = self.query(
            'run',
            'hi this is not sql',
            name='foo'
        ).json()
        error_data = self.assertIsErrorWith(data, 10003, 'Database Error', {
            'type': 'DatabaseException',
            'message': 'Database Error in rpc foo (from remote system)\n  syntax error at or near "hi"\n  LINE 1: hi this is not sql\n          ^',
            'compiled_sql': 'hi this is not sql',
            'raw_sql': 'hi this is not sql',
        })
        self.assertIn('logs', error_data)
        self.assertTrue(len(error_data['logs']) > 0)

        macro_no_override = self.query(
            'run',
            'select {{ happy_little_macro() }}',
            name='foo',
        ).json()
        error_data = self.assertIsErrorWith(macro_no_override, 10004, 'Compilation Error', {
            'type': 'CompilationException',
            'raw_sql': 'select {{ happy_little_macro() }}',
            'compiled_sql': None
        })
        self.assertIn('logs', error_data)
        self.assertTrue(len(error_data['logs']) > 0)

    def assertHasErrorData(self, error, expected_error_data):
        self.assertIn('data', error)
        error_data = error['data']
        for key, value in expected_error_data.items():
            self.assertIn(key, error_data)
            self.assertEqual(error_data[key], value)
        return error_data

    @use_profile('postgres')
    def test_timeout_postgres(self):
        data = self.query(
            'run',
            'select from pg_sleep(5)',
            name='foo',
            timeout=1
        ).json()
        error = self.assertIsErrorWithCode(data, 10008)
        self.assertEqual(error['message'], 'RPC timeout error')
        self.assertIn('data', error)
        error_data = error['data']
        self.assertIn('timeout', error_data)
        self.assertEqual(error_data['timeout'], 1)
        self.assertIn('message', error_data)
        self.assertEqual(error_data['message'], 'RPC timed out after 1s')
        self.assertIn('logs', error_data)
        # on windows, process start is so slow that frequently we won't have collected any logs
        if os.name != 'nt':
            self.assertTrue(len(error_data['logs']) > 0)

    @use_profile('postgres')
    def test_seed_project_postgres(self):
        # testing "dbt seed" is tricky so we'll just jam some sql in there
        self.run_sql_file("seed.sql")
        result = self.query('seed_project', show=True).json()
        dct = self.assertIsResult(result)
        self.assertTablesEqual('source', 'seed_expected')
        self.assertIn('results', dct)
        results = dct['results']
        self.assertEqual(len(results), 4)
        self.assertEqual(
            set(r['node']['name'] for r in results),
            {'expected_multi_source', 'other_source_table', 'other_table', 'source'}
        )

    @use_profile('postgres')
    def test_compile_project_postgres(self):
        self.run_dbt_with_vars(['seed'])
        result = self.query('compile_project').json()
        dct = self.assertIsResult(result)
        self.assertIn('results', dct)
        results = dct['results']
        self.assertEqual(len(results), 11)
        compiled = set(r['node']['name'] for r in results)
        self.assertTrue(compiled.issuperset(
            {'descendant_model', 'multi_source_model', 'nonsource_descendant'}
        ))
        self.assertNotIn('ephemeral_model', compiled)

    @use_profile('postgres')
    def test_run_project_postgres(self):
        self.run_dbt_with_vars(['seed'])
        result = self.query('run_project').json()
        dct = self.assertIsResult(result)
        self.assertIn('results', dct)
        results = dct['results']
        self.assertEqual(len(results), 3)
        self.assertEqual(
            set(r['node']['name'] for r in results),
            {'descendant_model', 'multi_source_model', 'nonsource_descendant'}
        )
        self.assertTablesEqual('multi_source_model', 'expected_multi_source')

    @use_profile('postgres')
    def test_test_project_postgres(self):
        self.run_dbt_with_vars(['seed'])
        result = self.query('run_project').json()
        dct = self.assertIsResult(result)
        result = self.query('test_project').json()
        dct = self.assertIsResult(result)
        self.assertIn('results', dct)
        results = dct['results']
        self.assertEqual(len(results), 4)
        for result in results:
            self.assertEqual(result['status'], 0.0)
            self.assertNotIn('fail', result)

    def _wait_for_running(self, timeout=25, raise_on_timeout=True):
        started = time.time()
        time.sleep(0.5)
        elapsed = time.time() - started

        while elapsed < timeout:
            status = self.assertIsResult(self.query('status').json())
            if status['status'] == 'running':
                return status
            time.sleep(0.5)
            elapsed = time.time() - started

        status = self.assertIsResult(self.query('status').json())
        if raise_on_timeout:
            self.assertEqual(
                status['status'],
                'ready',
                f'exceeded max time of {timeout}: {elapsed} seconds elapsed'
            )
        return status

    def assertRunning(self, sleepers):
        sleeper_ps_result = self.query('ps', completed=False, active=True).json()
        result = self.assertIsResult(sleeper_ps_result)
        self.assertEqual(len(result['rows']), len(sleepers))
        result_map = {rd['request_id']: rd for rd in result['rows']}
        for _, _, request_id in sleepers:
            found = result_map[request_id]
            self.assertEqual(found['request_id'], request_id)
            self.assertEqual(found['method'], 'run')
            self.assertEqual(found['state'], 'running')
            self.assertEqual(found['timeout'], None)

    def _add_command(self, cmd, id_):
        self.assertIsResult(self.query(cmd, _test_request_id=id_).json(), id_=id_)

    @mark.skipif(os.name == 'nt', reason='"sighup" not supported on windows')
    @mark.flaky(rerun_filter=lambda *a, **kw: True)
    @use_profile('postgres')
    def test_sighup_postgres(self):
        status = self.assertIsResult(self.query('status').json())
        self.assertEqual(status['status'], 'ready')
        self.assertIn('logs', status)
        logs = status['logs']
        self.assertTrue(len(logs) > 0)
        for key in ('message', 'timestamp', 'levelname', 'level'):
            self.assertIn(key, logs[0])

        self.assertIn('timestamp', status)

        done_query = self.query('compile', 'select 1 as id', name='done').json()
        self.assertIsResult(done_query)
        sleepers = []
        command_ids = []

        sleepers.append(self._get_sleep_query(1000, duration=60))
        self.assertRunning(sleepers)

        self._add_command('seed_project', 20)
        command_ids.append(20)
        self._add_command('run_project', 21)
        command_ids.append(21)

        # sighup a few times
        for _ in range(10):
            os.kill(status['pid'], signal.SIGHUP)

        status = self._wait_for_running()

        # we should still still see our service:
        self.assertRunning(sleepers)

        self._add_command('seed_project', 30)
        command_ids.append(30)
        self._add_command('run_project', 31)
        command_ids.append(31)

        # start a new one too
        sleepers.append(self._get_sleep_query(1001, duration=60))

        # now we should see both
        self.assertRunning(sleepers)

        # now pluck out the oldest one and kill it
        dead, alive = sleepers
        self.kill_and_assert(*dead)
        self.assertRunning([alive])
        self.kill_and_assert(*alive)


class FailedServerProcess(ServerProcess):
    def _compare_result(self, result):
        return result['result']['status'] == 'error'


@mark.flaky(rerun_filter=addr_in_use)
class TestRPCServerFailed(HasRPCServer):
    ServerProcess = FailedServerProcess
    should_seed = False

    @property
    def models(self):
        return "malformed_models"

    @use_profile('postgres')
    def test_postgres_status_error(self):
        status = self.assertIsResult(self.query('status').json())
        self.assertEqual(status['status'], 'error')
        self.assertIn('logs', status)
        logs = status['logs']
        self.assertTrue(len(logs) > 0)
        for key in ('message', 'timestamp', 'levelname', 'level'):
            self.assertIn(key, logs[0])
        self.assertIn('pid', status)
        self.assertEqual(self._server.pid, status['pid'])
        self.assertIn('error', status)
        self.assertIn('message', status['error'])

        compile_result = self.query('compile', 'select 1 as id').json()
        data = self.assertIsErrorWith(
            compile_result,
            10011,
            'RPC server failed to compile project, call the "status" method for compile status',
            None)
        self.assertIn('message', data)
        self.assertIn('Compilation warning: Invalid test config', str(data['message']))

    def tearDown(self):
        # prevent an OperationalError where the server closes on us in the
        # background
        self.adapter.cleanup_connections()
        super().tearDown()
