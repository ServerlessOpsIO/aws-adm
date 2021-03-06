'''Fetch object from S3 and publish items to SNS'''

import boto3
import csv
import gzip
import io
import iso8601
import json
import logging
import os
import zipfile

AWS_SNS_TOPIC = os.environ.get('AWS_SNS_TOPIC')
SCHEMA_CHANGE_HANDLING = os.environ.get('SCHEMA_CHANGE_HANDLING')

SCHEMA_CHANGE_ERROR = 'ERROR'
SCHEMA_CHANGE_CONTINUE = 'CONTINUE'
SCHEMA_CHANGE_RECONCILE = 'RECONCILE'
SCHEMA_CHANGE_OPTIONS = [
    SCHEMA_CHANGE_ERROR,
    SCHEMA_CHANGE_CONTINUE,
    SCHEMA_CHANGE_RECONCILE
]

X_RECORD_OFFSET = 'x-record-offset'
X_RECORD_LATEST_DATE = 'x-record-latest-date'

LAST_ADM_RUN_TIME_STATE = 'LAST_ADM_RUN_TIME_STATE'
LAST_ADM_RUN_SCHEMA_STATE = 'LAST_ADM_RUN_SCHEMA_STATE'

log_level = os.environ.get('LOG_LEVEL', 'INFO')
logging.root.setLevel(logging.getLevelName(log_level))  # type: ignore
_logger = logging.getLogger(__name__)

s3_client = boto3.client('s3')
sns_client = boto3.client('sns')
lambda_client = boto3.client('lambda')


class LineItemPublisherError(Exception):
    '''Lambda base exception'''
    pass


class BillingReportSchemaChangeError(LineItemPublisherError):
    '''Billing report schema change'''
    def __init__(self):
        self.msg = 'Detected billing report schema change'
        super(LineItemPublisherError, self).__init__(self.msg)


class InvalidSchemaChangeOptionError(LineItemPublisherError):
    '''Invalid schema change option'''
    def __init__(self, option):
        self.msg = '{} not in {}'.format(option, SCHEMA_CHANGE_OPTIONS)
        super(LineItemPublisherError, self).__init__(self.msg)


def _check_report_schema_change(line_item_headers, old_line_item_headers):
    '''Compare a line_item doc with a set of headers.'''
    # We're not going to assume that columns are always in the same position.
    # As a result, we need to make copies of the list here or we're going to
    # have a bad time matching headers and line item data.
    tmp_line_item_headers = line_item_headers[:]
    tmp_old_line_item_headers = old_line_item_headers[:]

    tmp_line_item_headers.sort()
    tmp_old_line_item_headers.sort()

    return tmp_line_item_headers == tmp_old_line_item_headers


def _check_s3_object_exists(s3_bucket, s3_key):
    '''Check if an object exists in S3'''
    resp = s3_client.list_objects(
        Bucket=s3_bucket,
        Prefix=s3_key
    )

    exists = False
    if 'Contents' in resp:
        for k in resp.get('Contents'):
            if k.get('Key') == s3_key:
                exists = True
                break

    return exists


def _convert_empty_value_to_none(item):
    '''Turn empty strings into None, etc.'''

    # DynamoDB can't have empty strings but csv.DictReader earlier in system
    # uses '' for empty fields.
    for key, value in item.items():
        if value == '':
            item[key] = None

    return item


def _create_line_item_message(headers, line_item):
    '''Return a formatted line item message.'''
    split_line_item = next(csv.reader([line_item]))
    item_dict = dict(zip(headers, split_line_item))
    sanitized_item_dict = _convert_empty_value_to_none(item_dict)

    final_dict = _format_line_item_dict(sanitized_item_dict)

    return final_dict


def _decompress_s3_object_body(s3_body, s3_key):
    '''Return the decompressed data of an S3 object.'''
    if s3_key.endswith('.gz'):
        gzip_file = gzip.GzipFile(fileobj=io.BytesIO(s3_body))
        decompressed_s3_body = gzip_file.read().decode()
    else:
        decompress_s3_key = '.'.join(s3_key.split('.')[:-1])
        zip_file = zipfile.ZipFile(io.BytesIO(s3_body))
        decompressed_s3_body = zip_file.read(decompress_s3_key).decode()

    return decompressed_s3_body


def _get_last_run_datetime_from_s3(s3_bucket, schema_change_handling):
    '''Return datetime of the last run'''
    if schema_change_handling == SCHEMA_CHANGE_RECONCILE:
        last_run_record_latest_date = '1970-01-01T00:00:00Z'
        last_run_record_latest_datetime = iso8601.parse_date(last_run_record_latest_date)
    else:
        if not _check_s3_object_exists(s3_bucket, LAST_ADM_RUN_TIME_STATE):
            last_run_record_latest_date = '1970-01-01T00:00:00Z'
            _put_s3_object(s3_bucket, LAST_ADM_RUN_TIME_STATE, last_run_record_latest_date)
        else:
            last_run_record_latest_date = _get_s3_object_body(s3_bucket, LAST_ADM_RUN_TIME_STATE).strip()
        last_run_record_latest_datetime = iso8601.parse_date(last_run_record_latest_date)

    return last_run_record_latest_datetime


def _get_line_items_from_s3(s3_bucket, s3_key):
    '''Return the line items from the S3 bucket.'''
    s3_object_body = _get_s3_object_body(s3_bucket, s3_key, decode_bytes=False)
    s3_object_body_decompressed = _decompress_s3_object_body(s3_object_body, s3_key)
    s3_body_file = io.StringIO(s3_object_body_decompressed)

    line_item_headers = s3_body_file.readline().strip().split(',')
    line_items = s3_body_file.read().splitlines()

    return (line_item_headers, line_items)


def _get_line_item_time_interval(line_item):
    '''Get the time interval of the line item'''
    return line_item.get('identity').get('TimeInterval').split('/')


def _delete_s3_object(s3_bucket, s3_key):
    '''Get object body from S3.'''
    resp = s3_client.delete_object(
        Bucket=s3_bucket,
        Key=s3_key
    )

    return resp


def _get_s3_object_body(s3_bucket, s3_key, decode_bytes=True):
    '''Get object body from S3.'''
    s3_object = s3_client.get_object(
        Bucket=s3_bucket,
        Key=s3_key
    )

    s3_object_body = s3_object.get('Body').read()
    if decode_bytes is True:
        s3_object_body = s3_object_body.decode()

    return s3_object_body


def _format_line_item_dict(line_item_dict):
    '''Convert multi-level keys into parent/child dict values.'''
    formatted_line_item_dict = {}

    for k, v in line_item_dict.items():
        key_list = k.split('/')
        if len(key_list) > 1:
            parent, child = key_list
            if parent not in formatted_line_item_dict.keys():
                formatted_line_item_dict[parent] = {}
            formatted_line_item_dict[parent][child] = v
        else:
            formatted_line_item_dict[key_list[0]] = v

    return formatted_line_item_dict


def _process_additional_items(arn, event, line_item_offset, this_latest_datetime):
    '''Process additional records.'''
    event.get('Records')[0][X_RECORD_OFFSET] = line_item_offset
    event.get('Records')[0][X_RECORD_LATEST_DATE] = str(this_latest_datetime)

    resp = lambda_client.invoke(
        FunctionName=arn,
        Payload=json.dumps(event),
        InvocationType='Event'
    )
    # pop non-serializible value
    resp.pop('Payload')
    return resp


def _publish_sns_message(topic_arn, line_item):
    '''Publish message to SNS'''
    resp = sns_client.publish(
        TopicArn=topic_arn,
        Message=json.dumps(line_item)
    )

    return resp


def _put_s3_object(s3_bucket, s3_key, body):
    '''Write item to S3'''
    resp = s3_client.put_object(
        Bucket=s3_bucket,
        Key=s3_key,
        Body=body
    )

    return resp


def handler(event, context):
    _logger.info('S3 event received: {}'.format(json.dumps(event)))

    # Raise an error if we don't know how to handle schema changes
    if SCHEMA_CHANGE_HANDLING not in SCHEMA_CHANGE_OPTIONS:
        raise InvalidSchemaChangeOptionError(SCHEMA_CHANGE_HANDLING)

    s3_bucket = event.get('Records')[0].get('s3').get('bucket').get('name')
    s3_key = event.get('Records')[0].get('s3').get('object').get('key')

    line_item_offset = event.get('Records')[0].get(X_RECORD_OFFSET)
    this_run_record_latest_date = event.get('Records')[0].get(X_RECORD_LATEST_DATE, '1970-01-01T00:00:00Z')
    this_run_record_latest_datetime = iso8601.parse_date(this_run_record_latest_date)

    line_item_headers, line_items = _get_line_items_from_s3(s3_bucket, s3_key)
    total_line_items = len(line_items)
    _logger.info('Total items: {}'.format(total_line_items))

    # This is an initial processing run of a given report.
    if line_item_offset is None:
        # Write schema if none exists.
        if not _check_s3_object_exists(s3_bucket, LAST_ADM_RUN_SCHEMA_STATE):
            _put_s3_object(s3_bucket, LAST_ADM_RUN_SCHEMA_STATE, ','.join(line_item_headers))
        # If we should error on change, check change
        else:
            old_line_item_headers = _get_s3_object_body(s3_bucket, LAST_ADM_RUN_SCHEMA_STATE).split(',')
            if not _check_report_schema_change(line_item_headers, old_line_item_headers):
                if SCHEMA_CHANGE_HANDLING == SCHEMA_CHANGE_ERROR:
                    raise BillingReportSchemaChangeError

    # Get last run latest time.
    last_run_record_latest_datetime = _get_last_run_datetime_from_s3(
        s3_bucket,
        SCHEMA_CHANGE_HANDLING
    )
    _logger.info('Processing line items since: {}'.format(last_run_record_latest_datetime))

    if line_item_offset is None:
        line_item_offset = 0

    line_items = line_items[line_item_offset:]

    # NOTE: We might decide to batch send multiple records at a time.  It's
    # Worth a look after we have decent metrics to understand tradeoffs.
    published_line_items = 0
    for line_item in line_items:
        _logger.debug('line_item: {}'.format(line_item))

        line_item_msg = _create_line_item_message(line_item_headers, line_item)
        _logger.debug('message: {}'.format(json.dumps(line_item_msg)))

        line_item_start, line_item_end = _get_line_item_time_interval(line_item_msg)
        line_item_start_datetime = iso8601.parse_date(line_item_start)

        # XXX: AWS does not guarantee that data will not change across across
        # billing reports.  There are two ways to easily observe this fact:
        #
        # - AmazonSNS and AmazonS3 BlendedRate will change across runs for the
        #   same line item
        # - Additional line items will be added to the first of the month
        #   throughout the month.
        #
        # We should probably do an end of month reconciliation run. Reports
        # are generated up to three times a day.  If we tried to do this
        # automatically this could be problematic depending on the size of the
        # report and the downstream publishers.

        # First of month line items get appended through the month.
        is_first_of_month_line_item = line_item_start_datetime.day == 1

        # XXX: Appears so far that each report always starts with a new
        # time period.
        is_newer_than_last_run = line_item_start_datetime > last_run_record_latest_datetime

        if is_newer_than_last_run or is_first_of_month_line_item:
            _logger.info('Publishing line_item: {}/{}'.format(line_item_offset + 1, total_line_items))
            resp = _publish_sns_message(AWS_SNS_TOPIC, line_item_msg)
            _logger.debug(
                'Publish response for line_item {}: {}'.format(
                    line_item_offset, json.dumps(resp)
                )
            )
            published_line_items += 1
            if line_item_start_datetime > this_run_record_latest_datetime:
                this_run_record_latest_datetime = line_item_start_datetime

        line_item_offset += 1

        if context.get_remaining_time_in_millis() <= 2000:
            break

    # We're done.  Remove file.
    # FIXME: Need a better way to check for processing same report than using
    # -1 as the offset.
    if line_item_offset < total_line_items:
        _logger.info('Invoking additional execution at record offset: {}'.format(line_item_offset))
        lambda_resp = _process_additional_items(
            context.invoked_function_arn,
            event,
            line_item_offset,
            this_run_record_latest_datetime,
        )
        _logger.info('Invoked additional Lambda response: {}'.format(json.dumps(lambda_resp)))
    else:
        _logger.info('No additional records to process')

        # Since we always process the 1st of the month, only write if report
        # is later than last run datetime.
        if this_run_record_latest_datetime > last_run_record_latest_datetime:
            _put_s3_object(s3_bucket, LAST_ADM_RUN_TIME_STATE, str(this_run_record_latest_datetime))

    resp = {
        'records_published': published_line_items,
        'line_item_offset': line_item_offset,
        'total_records': total_line_items
    }

    _logger.info('AWS responses: {}'.format(json.dumps(resp)))
    return resp

