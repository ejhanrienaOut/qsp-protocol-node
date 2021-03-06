####################################################################################################
#                                                                                                  #
# (c) 2018, 2019 Quantstamp, Inc. This content and its use are governed by the license terms at    #
# <https://s3.amazonaws.com/qsp-protocol-license/V2_LICENSE.txt>                                   #
#                                                                                                  #
####################################################################################################

import apsw
import os

from log_streaming import get_logger

from pathlib import Path
from utils.db import Sqlite3Worker
from utils.db import get_first

logger = get_logger(__name__)


class EventPoolManager:
    @staticmethod
    def __encode(dictionary):
        if dictionary is None:
            return None

        new_dictionary = {}
        for key in dictionary.keys():
            if dictionary[key] is not None \
                and (key == "price" or key == 'assigned_block_nbr' or key == 'submission_block_nbr'):
                new_dictionary[key] = str(dictionary[key])
            else:
                new_dictionary[key] = dictionary[key]

        return new_dictionary

    @staticmethod
    def __decode(dictionary):
        if dictionary is None:
            return None

        new_dictionary = {}
        for key in dictionary.keys():
            if dictionary[key] is not None \
                and (key == "price" or key == 'assigned_block_nbr' or key == 'submission_block_nbr'):
                new_dictionary[key] = int(dictionary[key])
            else:
                new_dictionary[key] = dictionary[key]

        return new_dictionary

    @staticmethod
    def __query_path(query):
        return "{0}/{1}.sql".format(
            os.path.dirname(os.path.abspath(__file__)),
            query,
        )

    @staticmethod
    def __exec_sql(worker, query, values=(), error_handler=None):
        query_file = EventPoolManager.__query_path(query)
        result = worker.execute_script(query_file, values, error_handler)
        if result == Sqlite3Worker.EXIT_TOKEN:
            return []
        else:
            return result

    @staticmethod
    def insert_error_handler(sql_worker, query, values, err):
        """
        Handles error coming from a request insert to the database. It logs a warning if the
        request already exists (based on the query and raised exception) and an error in every other
        case
        """
        if query.lower().strip().startswith("insert") \
                and isinstance(err, apsw.ConstraintError) \
                and "audit_evt.request_id" in str(err):
            # this error was caused by an already existing event
            logger.warning(
                "Audit request already exists: %s: %s: %s",
                query,
                values,
                err
            )
        else:
            logger.error(
                "Query returned error: %s: %s: %s",
                query,
                values,
                err
            )

    def __init__(self, db_path):
        # Gets a connection with the SQL3Lite server
        # Must be explicitly closed by calling `close` on the same
        # EventPool object. The connection is created with autocommit
        # mode on
        db_existed = False
        db_created = False
        error = False

        self.__sqlworker = None
        db_file = None
        try:
            db_file = Path(db_path)

            if db_file.is_file() and db_file.stat().st_size > 0:
                db_existed = True

            self.__sqlworker = Sqlite3Worker(file_name=db_path, max_queue_size=10000)
            db_created = True

            if not db_existed:
                EventPoolManager.__exec_sql(self.__sqlworker, 'createdb')

        except Exception:
            error = True
            raise

        finally:
            if error:
                if self.__sqlworker is not None:
                    self.__sqlworker.close()

                if not db_existed and db_created and db_file is not None:
                    db_file.unlink()

    @property
    def sql3lite_worker(self):
        return self.__sqlworker

    def get_latest_block_number(self):
        """
        Returns the block number of the latest event in the database or -1 if the database is empty.
        """
        row = get_first(EventPoolManager.__exec_sql(self.__sqlworker, 'get_latest_block_number'))
        return EventPoolManager.__decode(row).get('assigned_block_nbr')

    def is_request_processed(self, request_id):
        row = self.get_event_by_request_id(request_id)
        return not (row is None or row == {})

    def get_next_block_number(self):
        current = self.get_latest_block_number()
        if current < 0 or current is None:
            return 0
        return current + 1

    def get_latest_request_id(self):
        """
        Returns the request id of the latest event in the database or -1 if the database is empty.
        """
        row = get_first(EventPoolManager.__exec_sql(self.__sqlworker, 'get_latest_request_id'))
        return EventPoolManager.__decode(row).get('request_id')

    def add_evt_to_be_assigned(self, evt):
        encoded_evt = EventPoolManager.__encode(evt)

        EventPoolManager.__exec_sql(
            self.__sqlworker,
            'add_evt_to_be_assigned',
            values=(
                encoded_evt['request_id'],
                encoded_evt['requestor'],
                encoded_evt['contract_uri'],
                encoded_evt['evt_name'],
                encoded_evt['assigned_block_nbr'],
                encoded_evt['status_info'],
                encoded_evt['fk_type'],
                encoded_evt['price'],
            ),
            error_handler=EventPoolManager.insert_error_handler
        )

    def __process_evt_with_status(self, query_name, fct, values=(), fct_kwargs=None):
        for evt in EventPoolManager.__exec_sql(self.__sqlworker, query_name, values):
            decoded_evt = EventPoolManager.__decode(evt)
            if fct_kwargs is None:
                fct(decoded_evt, **{})
            else:
                fct(decoded_evt, **fct_kwargs)

    def get_event_by_request_id(self, request_id):
        rows = EventPoolManager.__exec_sql(self.__sqlworker, 'get_event_by_request_id',
                                           (request_id,))
        row = get_first(rows)
        return EventPoolManager.__decode(row)

    def process_incoming_events(self, process_fct):
        self.__process_evt_with_status(
            'get_events_to_be_processed',
            process_fct,
        )

    def process_events_to_be_submitted(self, process_fct):
        self.__process_evt_with_status(
            'get_events_to_be_submitted',
            process_fct,
        )

    def process_submission_events(self, monitor_fct, timeout_limit_blocks):
        kw_args = {'timeout_limit_blocks': timeout_limit_blocks}
        self.__process_evt_with_status(
            'get_events_to_be_monitored',
            monitor_fct,
            fct_kwargs=kw_args,
        )

    def set_evt_status_to_be_submitted(self, evt):
        encoded_evt = EventPoolManager.__encode(evt)
        EventPoolManager.__exec_sql(
            self.__sqlworker,
            'set_evt_status_to_be_submitted',
            (encoded_evt['status_info'],
             encoded_evt['tx_hash'],
             encoded_evt['audit_uri'],
             encoded_evt['audit_hash'],
             encoded_evt['audit_state'],
             encoded_evt['full_report'],
             encoded_evt['compressed_report'],
             encoded_evt['submission_block_nbr'],
             encoded_evt['request_id'],
             ),
        )

    def set_evt_status_to_submitted(self, evt):
        encoded_evt = EventPoolManager.__encode(evt)
        EventPoolManager.__exec_sql(
            self.__sqlworker,
            'set_evt_status_to_submitted',
            (encoded_evt['tx_hash'],
             encoded_evt['status_info'],
             encoded_evt['audit_uri'],
             encoded_evt['audit_hash'],
             encoded_evt['audit_state'],
             encoded_evt['request_id'],
             ),
        )

    def set_evt_status_to_done(self, evt):
        encoded_evt = EventPoolManager.__encode(evt)
        EventPoolManager.__exec_sql(
            self.__sqlworker,
            'set_evt_status_to_done',
            (encoded_evt['status_info'], encoded_evt['request_id'],),
        )

    def set_evt_status_to_error(self, evt):
        encoded_evt = EventPoolManager.__encode(evt)
        EventPoolManager.__exec_sql(
            self.__sqlworker,
            'set_evt_status_to_error',
            (encoded_evt['status_info'], encoded_evt['request_id'],),
        )

    def close(self):
        self.__sqlworker.close()
