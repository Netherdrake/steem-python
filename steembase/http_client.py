# coding=utf-8
import concurrent.futures
import json
import logging
import socket
from functools import partial
from http.client import RemoteDisconnected
from itertools import cycle
from urllib.parse import urlparse

import certifi
import urllib3
from urllib3.connection import HTTPConnection
from urllib3.exceptions import MaxRetryError, ReadTimeoutError, ProtocolError

from steembase.exceptions import RPCError

logger = logging.getLogger(__name__)


class SteemdNoResponse(BaseException):
    pass


class SteemdBadResponse(BaseException):
    pass


class HttpClient(object):
    """ Simple Steem JSON-HTTP-RPC API

    This class serves as an abstraction layer for easy use of the Steem API.

    Args:
      nodes (list): A list of Steem HTTP RPC nodes to connect to.

    .. code-block:: python

       from steem.http_client import HttpClient
       rpc = HttpClient(['https://steemd-node1.com', 'https://steemd-node2.com'])

    any call available to that port can be issued using the instance
    via the syntax ``rpc.exec('command', *parameters)``.

    Example:

    .. code-block:: python

       rpc.exec(
           'get_followers',
           'furion', 'abit', 'blog', 10,
           api='follow_api'
       )

    """

    def __init__(self, nodes, **kwargs):
        self.return_with_args = kwargs.get('return_with_args', False)
        self.max_workers = kwargs.get('max_workers', None)
        self.max_failovers = kwargs.get('max_failovers', 10)

        num_pools = kwargs.get('num_pools', 10)
        maxsize = kwargs.get('maxsize', 100)
        timeout = kwargs.get('timeout', 30)
        retries = kwargs.get('retries', 10)
        pool_block = kwargs.get('pool_block', False)
        tcp_keepalive = kwargs.get('tcp_keepalive', True)

        if tcp_keepalive:
            socket_options = HTTPConnection.default_socket_options + \
                             [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1), ]
        else:
            socket_options = HTTPConnection.default_socket_options

        self.http = urllib3.poolmanager.PoolManager(
            num_pools=num_pools,
            maxsize=maxsize,
            block=pool_block,
            timeout=timeout,
            retries=retries,
            socket_options=socket_options,
            headers={'Content-Type': 'application/json'},
            cert_reqs='CERT_REQUIRED',
            ca_certs=certifi.where())
        '''
            urlopen(method, url, body=None, headers=None, retries=None,
            redirect=True, assert_same_host=True, timeout=<object object>,
            pool_timeout=None, release_conn=None, chunked=False, body_pos=None,
            **response_kw)
        '''

        self.nodes = cycle(nodes)
        self.url = ''
        self.request = None
        self.next_node()

        log_level = kwargs.get('log_level', logging.INFO)
        logger.setLevel(log_level)

    def next_node(self):
        """ Switch to the next available node.

        This method will change base URL of our requests.
        Use it when the current node goes down to change to a fallback node. """
        self.set_node(next(self.nodes))

    def set_node(self, node_url):
        """ Change current node to provided node URL. """
        logger.info('Changing http node from %s to %s' % (self.url, node_url))
        self.url = node_url
        self.request = partial(self.http.urlopen, 'POST', self.url)

    @property
    def hostname(self):
        return urlparse(self.url).hostname

    @staticmethod
    def json_rpc_body(name, *args, api=None, as_json=True, _id=0):
        """ Build request body for steemd RPC requests.

        Args:
            name (str): Name of a method we are trying to call. (ie: `get_accounts`)
            args: A list of arguments belonging to the calling method.
            api (None, str): If api is provided (ie: `follow_api`),
             we generate a body that uses `call` method appropriately.
            as_json (bool): Should this function return json as dictionary or string.
            _id (int): This is an arbitrary number that can be used for request/response tracking in multi-threaded
             scenarios.

        Returns:
            (dict,str): If `as_json` is set to `True`, we get json formatted as a string.
            Otherwise, a Python dictionary is returned.
        """
        headers = {"jsonrpc": "2.0", "id": _id}
        if api:
            body_dict = {**headers, "method": "call", "params": [api, name, args]}
        else:
            body_dict = {**headers, "method": name, "params": args}
        if as_json:
            return json.dumps(body_dict, ensure_ascii=False).encode('utf8')
        else:
            return body_dict

    def exec(self, name, *args, api=None, return_with_args=None, _ret_cnt=0):
        """ Execute a method against steemd RPC.

        Warnings:
            This command will auto-retry in case of node failure, as well as handle
            node fail-over, unless we are broadcasting a transaction.
            In latter case, the exception is **re-raised**.
        """

        def failover():
            self.next_node()
            return self.exec(name, *args,
                             return_with_args=return_with_args,
                             _ret_cnt=_ret_cnt + 1)

        body = HttpClient.json_rpc_body(name, *args, api=api)
        response = None
        try:
            response = self.request(body=body)
        except Exception as e:
            # try switching nodes before giving up
            if _ret_cnt >= self.max_failovers:
                raise e
            logging.info('Retrying a request on a new node %s due to exception: %s' %
                         (self.hostname, e.__class__.__name__))
            return failover()
        else:
            if response.status is not 200:
                # try switching nodes before giving up
                if _ret_cnt >= self.max_failovers:
                    raise SteemdBadResponse(response)
                logging.info(
                    'Retrying a request on a new node %s due to bad response: %s' %
                    (self.hostname, response.status))
                return failover()

        response_json = None
        try:
            response_json = json.loads(response.data.decode('utf-8'))
        except Exception as e:
            # try switching nodes before giving up
            if _ret_cnt >= self.max_failovers:
                raise SteemdBadResponse(response)
            extra = dict(response=response, request_args=args, err=e)
            logger.info('RPC returned malformed response', extra=extra)
            return failover()
        else:
            if 'error' in response_json:
                # todo: failover() on node related errors only
                error = response_json['error']
                error_message = error.get(
                    'detail', response_json['error']['message'])
                if _ret_cnt >= self.max_failovers:
                    raise RPCError(error_message)
                logger.info('RPC returned an error %s' % error_message)
                return failover()

            if 'result' not in response_json:
                # todo: does this ever happen
                raise SteemdBadResponse('The response is missing a "result".')

        if return_with_args:
            return response_json['result'], args
        else:
            return response_json['result']

    def exec_multi_with_futures(self, name, params, api=None, max_workers=None):
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers) as executor:
            # Start the load operations and mark each future with its URL
            def ensure_list(parameter):
                return parameter if type(parameter) in (list, tuple, set) else [parameter]

            futures = (executor.submit(self.exec, name, *ensure_list(param), api=api)
                       for param in params)
            for future in concurrent.futures.as_completed(futures):
                yield future.result()
