#!/usr/bin/env python
#
# Python script to submit events to PagerDuty.  This script has been tested
# with Python 2.6.
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

EVENTS_API_BASE = "https://events.pagerduty.com/generic/2010-04-15/create_event.json"
QUEUE_DIR       = "/tmp/pagerduty"

# Open syslog for logging
syslog.openlog("pagerduty_python")

# Some utility functions for logging
def info(message):
    print(message)
    syslog.syslog(syslog.LOG_INFO, message)

def warn(message):
    print(message)
    syslog.syslog(syslog.LOG_WARNING, message)

def error(message):
    print(message)
    syslog.syslog(syslog.LOG_ERR, message)

# create the queue directory if it doesn't exists
def create_queue_dir():
    if not os.access(QUEUE_DIR, os.F_OK):
        os.mkdir(QUEUE_DIR, 0700)

# check whether we can read/write to queue
def is_queue_read_writable():
    if os.access(QUEUE_DIR, os.R_OK) and os.access(QUEUE_DIR, os.W_OK):
        return True
    error("Can't read/write to directory %s, please check permissions." % QUEUE_DIR)
    return False

# Get the list of files from the queue directory
def queued_files():
    files = os.listdir(QUEUE_DIR)
    pd_names = re.compile("pd_")
    pd_file_names = filter(pd_names.match, files)
    
    def file_sorter(file_name):
        return int(re.search('pd_(\d+)_', file_name).group(1))

    sorted_file_names = sorted(pd_file_names, file_sorter)    
    return pd_file_names

def flush_queue():
    file_names = queued_files()
    for file_name in file_names:
        incident_key = submit_event(("%s/%s" % (QUEUE_DIR, file_name)))
        if incident_key is not None:
            info("PagerDuty event submitted with incident key: %s" % incident_key)

def lock_and_flush_queue():
    
    create_queue_dir()

    with open("%s/lockfile" % QUEUE_DIR, "w") as lock_file:
        try:
            info("Acquiring lock on queue")
            fcntl.lockf(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # We have acquired the lock here
            # Let's flush the queue
            flush_queue()
        except IOError as e:
            warn("Error while trying to acquire lock on queue: %s" % str(e))
        finally:
            info("Releasing lock on queue")
            fcntl.lockf(lock_file.fileno(), fcntl.LOCK_UN)

def submit_event(file_path):
    json_event = None
    with open(file_path, "r") as event_file:
        json_event = event_file.read()

    incident_key = None
    delete_file = True

    try:
        request = urllib2.Request(EVENTS_API_BASE)
        request.add_header("Content-type", "application/json")
        request.add_data(json_event)
        response = urllib2.urlopen(request)
        result = json.loads(response.read())

        if result["status"] == "success":
            incident_key = result["incident_key"]
        else:
            warn("PagerDuty server REJECTED the event in file: %s Reason: %s" % (file_path, result))

    except urllib2.URLError as e:
        # client error
        if e.code >= 400 and e.code < 500:
            warn("PagerDuty server REJECTED the event in file: %s Reason: %s" % (file_path, e.read))
        else:
            warn("DEFERRED PagerDuty event in file: %s Reason: [%s, %s]" % (file_path, e.code, e.reason))
            delete_file = False # We'll need to retry 
    
    if delete_file:
        os.remove(file_path)

    return incident_key

def enqueue(event):
    create_queue_dir()

    if is_queue_read_writable():
        encoded_event = json.dumps(event)
        process_id = os.getpid()
        time_seconds = int(time.time())
        file_name = "%s/pd_%d_%d" % (QUEUE_DIR, time_seconds, process_id)
        with open(file_name, "w", 0600) as f:
            f.write(encoded_event)

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
def parse_zabbix_body(body_str):
    return dict(line.split(':', 1) for line in body_str.strip().split('\n'))

# Parse the Zabbix message subject. The subject MUST be one of the following:
#
# trigger
# resolve
#
def parse_zabbix_subject(subject_str):
    return subject_str

def event_from_zabbix_message():
    # The first argument is the service key
    service_key = sys.argv[1]
    # The second argument is 
    message_type = parse_zabbix_subject(sys.argv[2])
    event = parse_zabbix_body(sys.argv[3])
    info("event %s" % event)

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
    info("Submitting event %s" % str(pagerduty_event))
    return pagerduty_event

# If the length of the arguments is 4 then assume it was invoked from
# Zabbix, otherwise, just try to flush the queue
if len(sys.argv) == 4:
    enqueue(event_from_zabbix_message())
lock_and_flush_queue()