from collections import namedtuple
from datetime import datetime, timedelta
from gettext import gettext as _
import itertools
import logging
import re
import threading
import time

from celery import beat
from celery.result import AsyncResult

from pulp.server.async.celery_instance import celery as app
from pulp.server.async.celery_instance import RESOURCE_MANAGER_QUEUE
from pulp.server.db.model.criteria import Criteria
from pulp.server.db.model.dispatch import ScheduledCall, ScheduleEntry
from pulp.server.db.model.resources import AvailableQueue
from pulp.server.managers import resources
from pulp.server.managers.schedule import utils
from pulp.server.async.tasks import _delete_queue


_logger = logging.getLogger(__name__)


RESOURCE_MANAGER_PREFIX = 'resource_manager@'


class WorkerWatcher(object):
    """
    An object designed to handle celery events related to workers.

    Two celery events that need processing are the 'worker-heartbeat' and 'worker-offline'
    events. Each 'worker-heartbeat' event is passed to handle_worker_heartbeat() as an event for
    handling. Each 'worker-offline' event is passed to handle_worker_offline() for handling.
    See the individual method docblocks for more detail on how each event type is handled.

    The use of an 'event' or 'celery event' throughout this object refers to a dict built by celery
    that contains event information. Read more about this in the docs for celery.events.

    Other methods on this object are helper methods designed to deduplicate the amount of shared
    code between the event handlers.

    This object does not have any meaningful internal state, and all methods are used directly as
    static methods.
    """

    @staticmethod
    def _is_resource_manager(event):
        """
        Determine if this event is for a resource manager.

        :param event: A celery event
        :type event: dict
        :return: True if this event is from a resource manager, False otherwise.
        :rtype: bool
        """
        if re.match('^%s' % RESOURCE_MANAGER_PREFIX, event['hostname']):
            return True
        else:
            return False

    @staticmethod
    def _parse_and_log_event(event):
        """
        Parse and return the event information we are interested in. Also log it.

        A new dict is returned containing the keys 'timestamp', 'type', and 'worker_name'. The
        only data transformation here is on the timestamp. The timestamp arrives as seconds since
        the epoch, and is converted to UTC time, and returned as a naive datetime.datetime object.

        Logging is done through a call to the _log_event static method.

        :param event: A celery event
        :type event: dict
        :return: A dict containing the keys 'timestamp', 'type', and 'worker_name'. 'timestamp'
                 is a naive datetime.datetime reported in UTC. 'type' is the event name as a string
                 (ie: 'worker-heartbeat'), and 'worker_name' is the name of the worker as a string.
        :rtype: dict
        """
        event_info = {'timestamp': datetime.utcfromtimestamp(event['timestamp']),
                      'type': event['type'],
                      'worker_name': event['hostname']}
        WorkerWatcher._log_event(event_info)
        return event_info

    @staticmethod
    def _log_event(event_info):
        """
        Log the type, worker_name, and timestamp of an event at the debug log level.

        :param event_info: A dict expected to contain the keys 'type', 'worker_name', and
                           'timestamp'. The value of each key will be converted to a string and
                           included in the log output.
        :type event_info: dict
        """
        msg = _("received '%(type)s' from %(worker_name)s at time: %(timestamp)s") % event_info
        _logger.debug(msg)

    @staticmethod
    def handle_worker_heartbeat(event):
        """
        Celery event handler for 'worker-heartbeat' events.

        The event is first parsed and logged. If this event is from the resource manager, there is
        no further processing to be done. Then the existing AvailableQueue objects are searched
        for one to update. If an existing one is found, it is updated. Otherwise a new
        AvailableQueue entry is created. Logging at the info and debug level is also done.

        :param event: A celery event to handle.
        :type event: dict
        """
        event_info = WorkerWatcher._parse_and_log_event(event)

        # if this is the resource_manager do nothing
        if WorkerWatcher._is_resource_manager(event):
            return

        find_worker_criteria = Criteria(filters={'_id': event_info['worker_name']},
                                        fields=('_id', 'last_heartbeat', 'num_reservations'))
        find_worker_list = list(resources.filter_available_queues(find_worker_criteria))

        if find_worker_list:
            existing_worker = find_worker_list[0]
            existing_worker.last_heartbeat = event_info['timestamp']
            existing_worker.save()
        else:
            new_available_queue = AvailableQueue(event_info['worker_name'], event_info['timestamp'])
            msg = _("New worker '%(worker_name)s' discovered") % event_info
            _logger.info(msg)
            new_available_queue.save()

    @staticmethod
    def handle_worker_offline(event):
        """
        Celery event handler for 'worker-offline' events.

        The 'worker-offline' event is emitted when a worker gracefully shuts down. It is not
        emitted when a worker is killed instantly.

        The event is first parsed and logged. If this event is from the resource manager, there is
        no further processing to be done. Otherwise, a worker is shutting down, and a
        _delete_queue() task is dispatched so that the resource manager will remove the record,
        and handle any work cleanup associated with a worker going offline. Logging at the info
        and debug level is also done.

        :param event: A celery event to handle.
        :type event: dict
        """
        event_info = WorkerWatcher._parse_and_log_event(event)

        # if this is the resource_manager do nothing
        if WorkerWatcher._is_resource_manager(event):
            return

        msg = _("Worker '%(worker_name)s' shutdown") % event_info
        _logger.info(msg)
        _delete_queue.apply_async(args=(event_info['worker_name'],), queue=RESOURCE_MANAGER_QUEUE)


class FailureWatcher(object):
    _default_pop = (None, None, None)
    WatchedTask = namedtuple('WatchedTask', ['timestamp', 'schedule_id', 'has_failure'])
    # how long we will track a task from the time it gets queued.
    ttl = 60*60*4  # 4 hours

    def __init__(self):
        self._watches = {}

    def trim(self):
        """
        Removes tasks from our collections of tasks that are being watched for
        failure by looking for tasks that have been in the collection for more
        than self.ttl seconds.
        """
        oldest_allowed = int(time.time()) - self.ttl
        for task_id in (task_id for task_id, watch in self._watches.items()
                        if watch.timestamp < oldest_allowed):
            self._watches.pop(task_id, None)

    def add(self, task_id, schedule_id, has_failure):
        """
        Add a task to our collection of tasks to watch.

        :param task_id:     UUID of the task that was just queued
        :type  task_id:     basestring
        :param schedule_id: ID of the schedule that caused the task to be queued
        :type  schedule_id: basestring
        :param has_failure: True iff the schedule in question has at least one
                            consecutive failure currently recorded. If True, the
                            success handler can ignore this task.
        :type  has_failure: bool
        """
        self._watches[task_id] = self.WatchedTask(int(time.time()), schedule_id, has_failure)

    def pop(self, task_id):
        """
        removes the entry for the requested task_id and returns its schedule_id
        and has_failure attributes

        :param task_id:     UUID of a task
        :type  task_id:     basestring
        :return:            2-item list of [schedule_id, has_failure], where
                            schedule_id and has_failure will be None if the
                            task_id is not found.
        :rtype:             [basestring, bool]
        """
        return self._watches.pop(task_id, self._default_pop)[1:]

    def handle_succeeded_task(self, event):
        """
        Celery event handler for succeeded tasks. This will check if we are
        watching the task for failure, and if so, ensure that the corresponding
        schedule's failure count either already was 0 when the task was queued
        or that it gets reset to 0.

        :param event:   dictionary of poorly-documented data about a celery task.
                        At a minimum, this method depends on the key 'uuid'
                        being present and representing the task's ID.
        :type event:    dict
        """
        event_id = event['uuid']
        schedule_id, has_failure = self.pop(event_id)
        if schedule_id:
            return_value = AsyncResult(event_id, app=app).result
            if isinstance(return_value, AsyncResult):
                msg = _('watching child event %(id)s for failure') % {'id': return_value.id}
                _logger.debug(msg)
                self.add(return_value.id, schedule_id, has_failure)
            elif has_failure:
                _logger.info(_('resetting consecutive failure count for schedule %(id)s')
                             % {'id': schedule_id})
                utils.reset_failure_count(schedule_id)

    def handle_failed_task(self, event):
        """
        Celery event handler for failed tasks. This will check if we are
        watching the task for failure, and if so, increments the corresponding
        schedule's failure count. If it has met or exceeded its failure
        threshold, the schedule will be disabled.

        :param event:   dictionary of poorly-documented data about a celery task.
                        At a minimum, this method depends on the key 'uuid'
                        being present and representing the task's ID.
        :type event:    dict
        """
        schedule_id, has_failure = self.pop(event['uuid'])
        if schedule_id:
            msg = _('incrementing consecutive failure count for schedule %s') % schedule_id
            _logger.info(msg)
            utils.increment_failure_count(schedule_id)

    def __len__(self):
        return len(self._watches)


class EventMonitor(object):
    """
    A thread dedicated to processing Celery events.

    This object has two primary concerns: worker discovery/departure and task status monitoring.

    :param _failure_watcher: The FailureWatcher that will also be used by the Scheduler. The
                             callback handlers to be called need to be on the instantiated object.
    :type _failure_watcher: FailureWatcher
    """

    def __init__(self, _failure_watcher):
        self._failure_watcher = _failure_watcher

    def run(self):
        """
        The thread entry point, which calls monitor_events().

        monitor_events() is a blocking call, but in the case where an unexpected Exception is
        raised, a log-but-continue behavior is desired. This is especially the case given this is
        a background thread. Exiting is not a concern because this is a daemon=True thread. The
        method loops unconditionally using a try/except pattern around the call to
        monitor_events() to log and then re-enter if an exception is encountered.
        """
        while True:
            try:
                self.monitor_events()
            except Exception as e:
                _logger.error(e)

    def monitor_events(self):
        """
        Process celery events.

        Receives events from celery and matches each with the appropriate handler function. The
        following events are monitored for: 'task-failed', 'task-succeeded', 'worker-heartbeat',
        and 'worker-offline'. The call to capture() is blocking, and does not return.

        Capture is called with wakeup=True causing a gratuitous 'worker-heartbeat' to be sent
        from all workers. This should handle the case of worker discovery where workers already
        exist, and this EventMonitor is started afterwards.

        The call to capture is wrapped in a log-but-continue try/except statement, which along
        with the loop will cause the capture method to be re-entered.
        """
        with app.connection() as connection:
            recv = app.events.Receiver(connection, handlers={
                'task-failed': self._failure_watcher.handle_failed_task,
                'task-succeeded': self._failure_watcher.handle_succeeded_task,
                'worker-heartbeat': WorkerWatcher.handle_worker_heartbeat,
                'worker-offline': WorkerWatcher.handle_worker_offline,
            })
            _logger.info(_('Event Monitor Starting'))
            recv.capture(limit=None, timeout=None, wakeup=True)


class WorkerTimeoutMonitor(object):
    """
    A thread dedicated to processing Celery events.

    This object is designed to wakeup periodically and look for workers who have gone missing.
    """

    # The amount of time in seconds before a worker is considered missing
    WORKER_TIMEOUT_SECONDS = 300

    # The frequency in seconds with which this thread should look for missing workers.
    FREQUENCY = 60

    def run(self):
        """
        The thread entry point. It sleeps for FREQUENCY seconds, and then calls check_workers()

        This method has a try/except block around check_workers() to add durability to this
        background thread.
        """
        _logger.info(_('Worker Timeout Monitor Started'))
        while True:
            time.sleep(self.FREQUENCY)
            try:
                self.check_workers()
            except Exception as e:
                _logger.error(e)

    def check_workers(self):
        """
        Look for missing workers, and dispatch a cleanup task if one goes missing.

        To find a missing worker, filter the AvailableQueues model for entries older than
        utcnow() - WORKER_TIMEOUT_SECONDS. The heartbeat times are stored in naive UTC, so this is
        a comparable datetime.

        For each missing worker found, dispatch a _delete_queue task requesting that the resource
        manager delete the queue and cleanup any associated work.

        This method logs and the debug and error levels.
        """
        msg = _('Looking for workers missing for more than %s seconds') % self.WORKER_TIMEOUT_SECONDS
        _logger.debug(msg)
        oldest_heartbeat_time = datetime.utcnow() - timedelta(seconds=self.WORKER_TIMEOUT_SECONDS)
        worker_criteria = Criteria(filters={'last_heartbeat': {'$lt': oldest_heartbeat_time}},
                                   fields=('_id', 'last_heartbeat', 'num_reservations'))
        worker_list = list(resources.filter_available_queues(worker_criteria))
        for worker in worker_list:
            msg = _("Workers '%s' has gone missing, removing from list of workers") % worker.name
            _logger.error(msg)
            _delete_queue.apply_async(args=(worker.name,), queue=RESOURCE_MANAGER_QUEUE)


class Scheduler(beat.Scheduler):
    """
    This is a custom Scheduler object to be used by celery beat.

    This object has two purposes: Implement a dynamic periodic task schedule, and start helper
    threads related to celery event monitoring.

    Celery uses lazy instantiation, so this object may be created multiple times, with some objects
    being thrown away after being created. The spawning of threads needs to be done on the actual
    object, and not any intermediate objects, so __init__ conditionally spawns threads based on
    this case. Threads are spawned using spawn_pulp_monitor_threads().

    Two threads are started, one that uses EventMonitor, and handles all Celery events. The
    second, is a WorkerTimeoutMonitor thread that watches for cases where all workers disappear
    at once.
    """
    Entry = ScheduleEntry

    # the superclass reads this attribute, which is the maximum number of seconds
    # that will ever elapse before the scheduler looks for new or changed schedules.
    max_interval = 90

    def __init__(self, *args, **kwargs):
        """
        Initialize the Scheduler object.

        __init__ may be called multiple times because of the lazy instantiation behavior of Celery.
        If a keyword argument named 'lazy' is set to False, this instantiation is the 'real' one,
        and should create the necessary pulp helper threads using spawn_pulp_monitor_threads().
        """
        self._schedule = None
        self._failure_watcher = FailureWatcher()
        self._event_monitor = EventMonitor(self._failure_watcher)
        self._worker_timeout_monitor = WorkerTimeoutMonitor()
        self._loaded_from_db_count = 0
        # Leave helper threads starting here if lazy=False due to Celery lazy instantiation
        # https://github.com/celery/celery/issues/1549
        if kwargs.get('lazy', True) is False:
            self.spawn_pulp_monitor_threads()
        super(Scheduler, self).__init__(*args, **kwargs)

    def spawn_pulp_monitor_threads(self):
        """
        Start two threads that are important to Pulp. One monitors workers, the other, events.

        Two threads are started, one that uses EventMonitor, and handles all Celery events. The
        second, is a WorkerTimeoutMonitor thread that watches for cases where all workers
        disappear at once.

        This method should be called only in certain situations, see docstrings on the object for
        more details.
        """
        # start monitoring events in a thread
        event_thread = threading.Thread(target=self._event_monitor.run)
        event_thread.daemon = True
        event_thread.start()
        # start monitoring workers who may timeout
        worker_timeout_thread = threading.Thread(target=self._worker_timeout_monitor.run)
        worker_timeout_thread.daemon = True
        worker_timeout_thread.start()

    def tick(self):
        """
        Superclass runs a tick, that is one iteration of the scheduler. Executes
        all due tasks.

        This method adds a call to trim the failure watcher.

        :return:    number of seconds before the next tick should run
        :rtype:     float
        """
        ret = super(Scheduler, self).tick()
        self._failure_watcher.trim()
        return ret

    def setup_schedule(self):
        """
        This loads enabled schedules from the database and adds them to the
        "_schedule" dictionary as instances of celery.beat.ScheduleEntry
        """
        _logger.debug(_('loading schedules from app'))
        self._schedule = {}
        for key, value in self.app.conf.CELERYBEAT_SCHEDULE.iteritems():
            self._schedule[key] = beat.ScheduleEntry(**dict(value, name=key))

        # include a "0" as the default in case there are no schedules to load
        update_timestamps = [0]

        _logger.debug(_('loading schedules from DB'))
        ignored_db_count = 0
        self._loaded_from_db_count = 0
        for call in itertools.imap(ScheduledCall.from_db, utils.get_enabled()):
            if call.remaining_runs == 0:
                _logger.debug(_('ignoring schedule with 0 remaining runs: %(id)s') % {'id': call.id})
                ignored_db_count += 1
            else:
                self._schedule[call.id] = call.as_schedule_entry()
                update_timestamps.append(call.last_updated)
                self._loaded_from_db_count += 1

        _logger.debug('loaded %(count)d schedules' % {'count': self._loaded_from_db_count})

        self._most_recent_timestamp = max(update_timestamps)

    @property
    def schedule_changed(self):
        """
        Looks at the update timestamps in the database to determine if there
        are new or modified schedules.

        Indexing should make this very fast.

        :return:    True iff the set of enabled scheduled calls has changed
                    in the database.
        :rtype:     bool
        """
        if utils.get_enabled().count() != self._loaded_from_db_count:
            logging.debug(_('number of enabled schedules has changed'))
            return True

        if utils.get_updated_since(self._most_recent_timestamp).count() > 0:
            logging.debug(_('one or more enabled schedules has been updated'))
            return True

        return False

    @property
    def schedule(self):
        """
        :return:    dictionary where keys are schedule ids and values are
                    instances of celery.beat.ScheduleEntry. These instances are
                    the schedules currently in active use by the scheduler.
        :rtype:     dict
        """
        if self._schedule is None or self.schedule_changed:
            self.setup_schedule()

        return self._schedule

    def add(self, **kwargs):
        """
        This class does not support adding entries in-place. You must add new
        entries to the database, and they will be picked up automatically.
        """
        raise NotImplementedError

    def apply_async(self, entry, publisher=None, **kwargs):
        """
        The superclass calls apply_async on the task that is referenced by the
        entry. This method also adds the queued task to our list of tasks to
        watch for failure if the task has a failure threshold.

        :param entry:       schedule entry whose task should be queued.
        :type  entry:       celery.beat.ScheduleEntry
        :param publisher:   unknown. used by celery but not documented
        :type kwargs:       dict
        :return:
        """
        result = super(Scheduler, self).apply_async(entry, publisher, **kwargs)
        if isinstance(entry, ScheduleEntry) and entry._scheduled_call.failure_threshold:
            has_failure = bool(entry._scheduled_call.consecutive_failures)
            self._failure_watcher.add(result.id, entry.name, has_failure)
            _logger.debug(_('watching task %s') % {'id': result.id})
        return result
