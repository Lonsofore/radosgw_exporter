#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
import time
import json
import logging
import argparse
import threading
from collections import defaultdict, Counter

import requests
from awsauth import S3Auth
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY


logging.basicConfig(level=logging.DEBUG)
DEBUG = int(os.environ.get('DEBUG', '0'))


class RADOSGWCollector(object):
    """RADOSGWCollector gathers bucket level usage data for all buckets from
    the specified RADOSGW and presents it in a format suitable for pulling via
    a Prometheus server.

    NOTE: By default RADOSGW Servers do not gather usage data and it must be
    enabled by 'rgw enable usage log = true' in the appropriate section
    of ceph.conf see Ceph documentation for details """

    def __init__(self, host, admin_entry, access_key, secret_key, interval=60):
        super(RADOSGWCollector, self).__init__()
        self.host = host
        self.access_key = access_key
        self.secret_key = secret_key
        self.interval = interval

        # helpers for default schema
        if not self.host.startswith("http") and not self.host.startswith("https"):
            self.host = "http://{0}".format(self.host)
        # and for request_uri
        if not self.host.endswith("/"):
            self.host = "{0}/".format(self.host)
        self.url = "{0}{1}/".format(self.host, admin_entry)

        # start scraping in a thread
        self.scrape_thread = threading.Thread(target=self._scrape_loop)
        self.scrape_thread.start()

    def collect(self):
        for metric in list(self._prometheus_metrics.values()):
            yield metric

    def _scrape_loop(self):
        while True:
            self._scrape_metrics()
            time.sleep(self.interval)

    def _scrape_metrics(self):
        """
        * Collect 'usage' data:
            http://docs.ceph.com/docs/master/radosgw/adminops/#get-usage
        * Collect 'bucket' data:
            http://docs.ceph.com/docs/master/radosgw/adminops/#get-bucket-info
        * Collect 'user' data:
            https://docs.ceph.com/docs/master/radosgw/adminops/#get-user-info
        """

        start = time.time()
        # setup empty prometheus metrics
        self._setup_empty_prometheus_metrics()

        # setup dict for aggregating bucket usage accross "bins"
        self.usage_dict = defaultdict(dict)

        rgw_usage = self._request_data(query='usage', args='show-summary=False')
        rgw_bucket = self._request_data(query='bucket', args='stats=True')
        rgw_users = self._get_users_ids()

        # populate metrics with data
        if rgw_usage:
            for entry in rgw_usage['entries']:
                self._get_usage(entry)
            self._update_usage_metrics()

        if rgw_bucket:
            for bucket in rgw_bucket:
                self._get_bucket_usage(bucket)

        if rgw_users:
            for user in rgw_users:
                self._get_user_quota(user)

        duration = time.time() - start
        self._prometheus_metrics['scrape_duration_seconds'].add_metric(
            [], duration)

    def _request_data(self, query, args=None):
        """
        Requests data from RGW. If admin entry and caps is fine - return
        JSON data, otherwise return NoneType.
        """

        args_str = ''
        if args is not None:
            args_str = '&{}'.format(args)
        url = "{0}{1}/?format=json{2}".format(self.url, query, args_str)

        try:
            response = requests.get(url, auth=S3Auth(self.access_key,
                                                     self.secret_key,
                                                     self.host))

            if response.status_code == requests.codes.ok:
                if DEBUG:
                    print(response)

                return response.json()
            else:
                # Usage caps absent or wrong admin entry
                print("Request error [{0}]: {1}".format(
                    response.status_code, response.content.decode('utf-8')))
                return

        # DNS, connection errors, etc
        except requests.exceptions.RequestException as e:
            print("Request error: {0}".format(e))
            return

    def _setup_empty_prometheus_metrics(self):
        """
        The metrics we want to export.
        """

        self._prometheus_metrics = {
            'ops':
                CounterMetricFamily('radosgw_usage_ops_total',
                                    'Number of operations',
                                    labels=["bucket", "owner", "category"]),
            'successful_ops':
                CounterMetricFamily('radosgw_usage_successful_ops_total',
                                    'Number of successful operations',
                                    labels=["bucket", "owner", "category"]),
            'bytes_sent':
                CounterMetricFamily('radosgw_usage_sent_bytes_total',
                                    'Bytes sent by the RADOSGW',
                                    labels=["bucket", "owner", "category"]),
            'bytes_received':
                CounterMetricFamily('radosgw_usage_received_bytes_total',
                                    'Bytes received by the RADOSGW',
                                    labels=["bucket", "owner", "category"]),
            'bucket_usage_bytes':
                GaugeMetricFamily('radosgw_usage_bucket_bytes',
                                  'Bucket used bytes',
                                  labels=["bucket", "owner", "zonegroup"]),
            'bucket_utilized_bytes':
                GaugeMetricFamily('radosgw_usage_bucket_utilized_bytes',
                                  'Bucket utilized bytes',
                                  labels=["bucket", "owner", "zonegroup"]),
            'bucket_usage_objects':
                GaugeMetricFamily('radosgw_usage_bucket_objects',
                                  'Number of objects in bucket',
                                  labels=["bucket", "owner", "zonegroup"]),
            'scrape_duration_seconds':
                GaugeMetricFamily('radosgw_usage_scrape_duration_seconds',
                                  'Ammount of time each scrape takes',
                                  labels=[]),
            'bucket_quota_bytes':
                GaugeMetricFamily('radosgw_usage_bucket_quota_bytes',
                                  'Bucket quota bytes',
                                  labels=["bucket", "owner", "zonegroup"]),
            'bucket_quota_max_objects':
                GaugeMetricFamily('radosgw_usage_bucket_quota_max_objects',
                                  'Bucket quota max objects',
                                  labels=["bucket", "owner", "zonegroup"]),
            'user_quota_utilized':
                GaugeMetricFamily('radosgw_user_quota_utilized',
                                  'Percent of radosgw quota utilized',
                                  labels=["project_name", "project_id"]),
            'user_quota_limit':
                GaugeMetricFamily('radosgw_user_quota_limit',
                                  'Limit of radosgw quota',
                                  labels=["project_name", "project_id"]),
            'user_quota_used':
                GaugeMetricFamily('radosgw_user_quota_used',
                                  'Usage of radosgw quota',
                                  labels=["project_name", "project_id"]),
        }

    def _get_usage(self, entry):
        """
        Recieves JSON object 'entity' that contains all the buckets relating
        to a given RGW UID. Builds a dictionary of metric data in order to
        handle UIDs where the usage data is truncated into multiple 1000
        entry bins.
        """

        if 'owner' in entry:
            bucket_owner = entry['owner']
        # Luminous
        elif 'user' in entry:
            bucket_owner = entry['user']

        if bucket_owner not in list(self.usage_dict.keys()):
            self.usage_dict[bucket_owner] = defaultdict(dict)

        for bucket in entry['buckets']:
            if DEBUG:
                print(json.dumps(bucket, indent=4, sort_keys=True))

            if not bucket['bucket']:
                bucket_name = "bucket_root"
            else:
                bucket_name = bucket['bucket']

            if bucket_name not in list(self.usage_dict[bucket_owner].keys()):
                self.usage_dict[bucket_owner][bucket_name] = defaultdict(dict)

            for category in bucket['categories']:
                category_name = category['category']
                if category_name not in list(self.usage_dict[bucket_owner][bucket_name].keys()):
                    self.usage_dict[bucket_owner][bucket_name][category_name] = Counter()
                c = self.usage_dict[bucket_owner][bucket_name][category_name]
                c.update({'ops':category['ops'],
                          'successful_ops':category['successful_ops'],
                          'bytes_sent':category['bytes_sent'],
                          'bytes_received':category['bytes_received']})

    def _update_usage_metrics(self):
        """
        Update promethes metrics with bucket usage data
        """

        for bucket_owner in list(self.usage_dict.keys()):
            for bucket_name in list(self.usage_dict[bucket_owner].keys()):
                for category in list(self.usage_dict[bucket_owner][bucket_name].keys()):
                    data_dict = self.usage_dict[bucket_owner][bucket_name][category]
                    self._prometheus_metrics['ops'].add_metric(
                        [bucket_name, bucket_owner, category],
                            data_dict['ops'])

                    self._prometheus_metrics['successful_ops'].add_metric(
                        [bucket_name, bucket_owner, category],
                            data_dict['successful_ops'])

                    self._prometheus_metrics['bytes_sent'].add_metric(
                        [bucket_name, bucket_owner, category],
                            data_dict['bytes_sent'])

                    self._prometheus_metrics['bytes_received'].add_metric(
                        [bucket_name, bucket_owner, category],
                            data_dict['bytes_received'])

    def _get_bucket_usage(self, bucket):
        """
        Method get actual bucket usage (in bytes).
        Some skips and adjustments for various Ceph releases.
        """

        if DEBUG:
            print(json.dumps(bucket, indent=4, sort_keys=True))

        if type(bucket) is dict:
            bucket_name = bucket['bucket']
            bucket_owner = bucket['owner']
            bucket_usage_bytes = 0
            bucket_utilized_bytes = 0
            bucket_usage_objects = 0
            bucket_quota_bytes = 0
            bucket_quota_max_objects = 0

            if bucket['usage']:
                # Prefer bytes, instead kbytes
                if 'size_actual' in bucket['usage']['rgw.main']:
                    bucket_usage_bytes = bucket['usage']['rgw.main']['size_actual']
                # Hammer don't have bytes field
                elif 'size_kb_actual' in bucket['usage']['rgw.main']:
                    usage_kb = bucket['usage']['rgw.main']['size_kb_actual']
                    bucket_usage_bytes = usage_kb * 1024

                # Compressed buckets, since Kraken
                if 'size_utilized' in bucket['usage']['rgw.main']:
                    bucket_utilized_bytes = bucket['usage']['rgw.main']['size_utilized']

                # Get number of objects in bucket
                if 'num_objects' in bucket['usage']['rgw.main']:
                    bucket_usage_objects = bucket['usage']['rgw.main']['num_objects']


            if 'zonegroup' in bucket:
                bucket_zonegroup = bucket['zonegroup']
            # Hammer
            else:
                bucket_zonegroup = "0"

            if 'bucket_quota' in bucket:
                if 'max_size' in bucket['bucket_quota']:
                    bucket_quota_bytes = bucket['bucket_quota']['max_size']
                if 'max_objects' in bucket['bucket_quota']:
                    bucket_quota_max_objects = bucket['bucket_quota']['max_objects']


            self._prometheus_metrics['bucket_usage_bytes'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup],
                    bucket_usage_bytes)

            self._prometheus_metrics['bucket_utilized_bytes'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup],
                    bucket_utilized_bytes)

            self._prometheus_metrics['bucket_usage_objects'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup],
                    bucket_usage_objects)

            self._prometheus_metrics['bucket_quota_bytes'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup],
                    bucket_quota_bytes)

            self._prometheus_metrics['bucket_quota_max_objects'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup],
                    bucket_quota_max_objects)
        else:
            # Hammer junk, just skip it
            pass

    def _get_users_ids(self):
        query = 'metadata/user'
        users = self._request_data(query=query)
        return users

    def _get_quota_by_project(self, project):
        query = 'user'
        args = 'quota&uid={0}&quota-type=user'.format(project)
        quota = self._request_data(query=query, args=args)
        return quota['max_size']

    def _get_user_info(self, project):
        query = 'user'
        args = 'stats=True&uid={0}'.format(project)
        user = self._request_data(query=query, args=args)
        return user

    def _get_user_quota(self, project):
        project_quota = self._get_quota_by_project(project)
        if project_quota > 0:
            user = self._get_user_info(project)                                
            if user is not None:
                project_name = user['display_name']
                project_id = user['user_id']
                used_size = user['stats']['size_actual']                   
                used_size_percent = (used_size*100)/project_quota
                self._prometheus_metrics['user_quota_utilized'].add_metric([str(project_name),
                                                                            str(project_id)],
                                                                            used_size_percent)
                self._prometheus_metrics['user_quota_limit'].add_metric([str(project_name),
                                                                         str(project_id)],
                                                                         project_quota)
                self._prometheus_metrics['user_quota_used'].add_metric([str(project_name),
                                                                        str(project_id)],
                                                                        used_size)


def parse_args():
    parser = argparse.ArgumentParser(
        description='RADOSGW address and local binding port as well as \
        S3 access_key and secret_key'
    )
    parser.add_argument(
        '-H', '--host',
        required=False,
        help='Server URL for the RADOSGW api (example: http://objects.dreamhost.com/)',
        default=os.environ.get('RADOSGW_SERVER', 'http://radosgw:80')
    )
    parser.add_argument(
        '-e', '--admin_entry',
        required=False,
        help="The entry point for an admin request URL [default is '%(default)s']",
        default=os.environ.get('ADMIN_ENTRY', 'admin')
    )
    parser.add_argument(
        '-a', '--access_key',
        required=False,
        help='S3 access key',
        default=os.environ.get('ACCESS_KEY', 'NA')
    )
    parser.add_argument(
        '-s', '--secret_key',
        required=False,
        help='S3 secrest key',
        default=os.environ.get('SECRET_KEY', 'NA')
    )
    parser.add_argument(
        '-p', '--port',
        required=False,
        type=int,
        help='Port to listen',
        default=int(os.environ.get('VIRTUAL_PORT', '9242'))
    )
    return parser.parse_args()


def main():
    try:
        args = parse_args()
        REGISTRY.register(RADOSGWCollector(
            args.host, args.admin_entry, args.access_key, args.secret_key))
        start_http_server(args.port)
        print("Polling {0}. Serving at port: {1}".format(args.host, args.port))
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nInterrupted")
        exit(0)


if __name__ == "__main__":
    main()

