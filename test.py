#!/usr/bin/env python

import time
import pagerduty

def test():
    pagerduty_queue = pagerduty.PagerDutyQueue()
    body = """
    name:{TRIGGER.NAME}
    id:{TRIGGER.ID}
    status:{TRIGGER.STATUS}
    hostname:{HOSTNAME}
    ip:{IPADDRESS}
    value:{TRIGGER.VALUE}
    event_id:{EVENT.ID}
    severity:{TRIGGER.SEVERITY}
    """
    arguments = ["", "f3e8b8c00f9f0130008722000af85fb5", "trigger", body]
    pagerduty_queue.enqueue(pagerduty.Zabbix(arguments).event())
    time.sleep(5)
    pagerduty_queue.lock_and_flush_queue()

test()