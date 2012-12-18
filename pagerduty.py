#!/usr/bin/env python
#
# Python script to submit Zabbix events to PagerDuty.  This script has
# been tested with Python 2.6.
#
# Copyright (c) 2012, PagerDuty, Inc. <info@pagerduty.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of PagerDuty Inc nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL PAGERDUTY INC BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import sys
import syslog
import urllib2
import os
import re
import fcntl
import time

try:
    import json
except ImportError:
    import simplejson as json

class SimpleLogger(object):
    """
    A Simple logger
    """

    def __init__(self):
        # Open syslog for logging
        syslog.openlog("pagerduty_python")

    # Some utility functions for logging
    def info(self, message):
        self.log(syslog.LOG_INFO, message)

    def warn(self, message):
        self.log(syslog.LOG_WARNING, message)

    def error(self, message):
        self.log(syslog.LOG_ERR, message)

    def log(self, level, message):
        # print(message)
        syslog.syslog(level, message)

logger = SimpleLogger()

class PagerDutyClient(object):
    """
    """

    EVENTS_API_BASE = "https://events.pagerduty.com/generic/2010-04-15/create_event.json"

    def __init__(self, api_base=EVENTS_API_BASE):
        self.api_base = api_base

    def submit_event(self, file_path):
        json_event = None
        with open(file_path, "r") as event_file:
            json_event = event_file.read()

        incident_key = None
        delete_file = True

        try:
            request = urllib2.Request(self.api_base)
            request.add_header("Content-type", "application/json")
            request.add_data(json_event)
            response = urllib2.urlopen(request)
            result = json.loads(response.read())

            if result["status"] == "success":
                incident_key = result["incident_key"]
            else:
                logger.warn("PagerDuty server REJECTED the event in file: %s, Reason: %s" % (file_path, str(response)))

        except urllib2.URLError as e:
            # client error
            if e.code >= 400 and e.code < 500:
                logger.warn("PagerDuty server REJECTED the event in file: %s, Reason: %s" % (file_path, e.read()))
            else:
                logger.warn("DEFERRED PagerDuty event in file: %s, Reason: [%s, %s]" % (file_path, e.code, e.reason))
                delete_file = False # We'll need to retry

        if delete_file:
            os.remove(file_path)

        return incident_key


class PagerDutyQueue(object):
    """
    This class implements a simple directory based queue for PagerDuty events
    """

    QUEUE_DIR = "/tmp/pagerduty"

    def __init__(self, queue_dir=QUEUE_DIR, pagerduy_client=PagerDutyClient()):
        self.queue_dir = queue_dir
        self.pagerduy_client = pagerduy_client
        self._create_queue_dir()
        self._verify_permissions()

    def _create_queue_dir(self):
        if not os.access(self.queue_dir, os.F_OK):
            os.mkdir(self.queue_dir, 0700)

    def _verify_permissions(self):
        if not (os.access(self.queue_dir, os.R_OK)
            and os.access(self.queue_dir, os.W_OK)):
            logger.error("Can't read/write to directory %s, please check permissions." % self.queue_dir)
            raise Exception("Can't read/write to directory %s, please check permissions." % self.queue_dir)

    # Get the list of files from the queue directory
    def _queued_files(self):
        files = os.listdir(self.queue_dir)
        pd_names = re.compile("pd_")
        pd_file_names = filter(pd_names.match, files)

        def file_sorter(file_name):
            return int(re.search('pd_(\d+)_', file_name).group(1))

        sorted_file_names = sorted(pd_file_names, file_sorter)
        return pd_file_names

    def _flush_queue(self):
        file_names = self._queued_files()
        for file_name in file_names:
            incident_key = self.pagerduy_client.submit_event(("%s/%s" % (self.queue_dir, file_name)))
            if incident_key is not None:
                logger.info("PagerDuty event submitted with incident key: %s" % incident_key)

    def lock_and_flush_queue(self):
        with open("%s/lockfile" % self.queue_dir, "w") as lock_file:
            try:
                logger.info("Acquiring lock on queue")
                fcntl.lockf(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # We have acquired the lock here
                # Let's flush the queue
                self._flush_queue()
            except IOError as e:
                logger.warn("Error while trying to acquire lock on queue: %s" % str(e))
            finally:
                logger.info("Releasing lock on queue")
                fcntl.lockf(lock_file.fileno(), fcntl.LOCK_UN)

    def enqueue(self, event):
        encoded_event = json.dumps(event)
        process_id = os.getpid()
        time_seconds = int(time.time())
        file_name = "%s/pd_%d_%d" % (self.queue_dir, time_seconds, process_id)
        logger.info("Queuing event %s" % str(event))
        with open(file_name, "w", 0600) as f:
            f.write(encoded_event)

class Zabbix(object):
    """
    Zabbix integration
    """

    def __init__(self, arguments):
        self.arguments = arguments

    # Parse the Zabbix message body. The body MUST be in this format:
    #
    # name:{TRIGGER.NAME}
    # id:{TRIGGER.ID}
    # status:{TRIGGER.STATUS}
    # hostname:{HOSTNAME}
    # ip:{IPADDRESS}
    # value:{TRIGGER.VALUE}
    # event_id:{EVENT.ID}
    # severity:{TRIGGER.SEVERITY}
    #
    def _parse_zabbix_body(self, body_str):
        return dict(line.strip().split(':', 1) for line in body_str.strip().split('\n'))

    # Parse the Zabbix message subject.
    # The subject MUST be one of the following:
    #
    # trigger
    # resolve
    #
    def _parse_zabbix_subject(self, subject_str):
        return subject_str

    def event(self):
        # The first argument is the service key
        service_key = self.arguments[1]
        # The second argument is the message type
        message_type = self._parse_zabbix_subject(self.arguments[2])
        event = self._parse_zabbix_body(self.arguments[3])
        logger.info("event %s" % event)

        # Incident key is created by concatenating trigger id and host name.
        # Remember, incident key is used for de-duping and also to match
        # trigger with resolve messages
        incident_key = "%s-%s" % (event["id"], event["hostname"])

        # The description that is rendered in PagerDuty and also sent as SMS
        # and phone alert
        description = "%s : %s for %s" % (event["name"],
                        event["status"], event["hostname"])

        pagerduty_event = {
            "service_key": service_key, "event_type":message_type,
            "description": description, "incident_key": incident_key,
            "details": event
        }
        return pagerduty_event


# If the length of the arguments is 4 then assume it was invoked from
# Zabbix, otherwise, just try to flush the queue
if __name__ == "__main__":
    pagerduty_queue = PagerDutyQueue()
    if len(sys.argv) == 4:
        pagerduty_queue.enqueue(Zabbix(sys.argv).event())
    pagerduty_queue.lock_and_flush_queue()
