####################################################################################################
#                                                                                                  #
# (c) 2018 Quantstamp, Inc. All rights reserved.  This content shall not be used, copied,          #
# modified, redistributed, or otherwise disseminated except to the extent expressly authorized by  #
# Quantstamp for credentialed users. This content and its use are governed by the Quantstamp       #
# Demonstration License Terms at <https://s3.amazonaws.com/qsp-protocol-license/LICENSE.txt>.      #
#                                                                                                  #
####################################################################################################

"""
Provides the QSP Audit node implementation.
"""
import calendar
import copy
import json
import os

import threading
import time
import traceback
import urllib.parse
import jsonschema

from time import sleep
from web3.utils.threads import Timeout

from utils.io import (
    fetch_file,
    digest,
    digest_file,
    read_file
)
from utils.eth import send_signed_transaction
from utils.eth import make_read_only_call
from utils.eth import DeduplicationException

from threading import Thread
from utils.metrics import MetricCollector
from solc import compile_standard
from solc.exceptions import ContractsNotFound, SolcError
from .exceptions import NonWhitelistedNodeException


class QSPAuditNode:
    __EVT_AUDIT_ASSIGNED = "LogAuditAssigned"
    __EVT_REPORT_SUBMITTED = "LogAuditFinished"

    # must be in sync with
    # https://github.com/quantstamp/qsp-protocol-audit-contract/blob/develop/contracts/QuantstampAuditData.sol#L25
    __AUDIT_STATE_SUCCESS = 4

    # must be in sync with
    # https://github.com/quantstamp/qsp-protocol-audit-contract/blob/develop/contracts/QuantstampAuditData.sol#L26
    __AUDIT_STATE_ERROR = 5

    # must be in sync with
    # https://github.com/quantstamp/qsp-protocol-audit-contract/blob/develop/contracts/QuantstampAudit.sol#L80
    __AVAILABLE_AUDIT__STATE_READY = 1

    __AUDIT_STATUS_ERROR = "error"
    __AUDIT_STATUS_SUCCESS = "success"

    def __init__(self, config):
        """
        Builds a QSPAuditNode object from the given input parameters.
        """
        self.__config = config
        self.__logger = config.logger
        self.__metric_collector = None
        self.__exec = False
        self.__internal_threads = []
        self.__audit_node_initialized = False

        # There are some important invariants that are to be respected at all
        # times when the audit node (re-)processes events (see associated queries):
        #
        # 1) An audit event is never saved twice in the node's internal database
        #
        # 2) If an event has been given a certain status, it is never
        #    updated with a status lower in ranking
        #    The current ranking is given by:
        #
        #    RQ (Requested) < AS (Assigned < TS (To be submitted) < SB (Submitted) < DN (Done)
        #
        # 3) Errors are currently not recoverable, i.e., if an audit event reaches
        #    an error state in the finite automata internally captured by the audit node,
        #    the event never leaves that state
        #
        # 4) At all times, there is at most one writer thread executing. Stated otherwise,
        #    concurrent writes never occur
        #
        # 5) At all times, the audit node only accounts for the health of threads
        #    processing new events. Old ones necessarily cause the underlying
        #    thread to complete execution and eventually dying

    def __run_audit_evt_thread(self, evt_name, evt_filter, evt_handler):
        def exec():
            try:
                while self.__exec:
                    for evt in evt_filter.get_new_entries():
                        evt_handler(evt)

                    sleep(self.__config.evt_polling)
            except Exception as error:
                if hasattr(error, 'message') and error.message == 'filter not found':
                    # This is not actionable so it should be silenced from logs.
                    self.__logger.info("Filter not found in the audit event thread {0}: {1}".format(
                        evt_name, str(error))
                    )
                else:
                    self.__logger.exception("Error in the audit event thread {0}: {1}".format(
                        evt_name, str(error))
                    )
                raise error

        evt_thread = Thread(target=exec, name="{0} thread".format(evt_name))
        evt_thread.start()

        return evt_thread

    def __run_block_mined_thread(self, handler_name, handler):
        """
        Checks if a new block is mined. Reacting to a new block the handler is called.
        """
        def exec():
            current_block = 0
            while self.__exec:
                sleep(self.__config.block_mined_polling)
                if current_block < self.__config.web3_client.eth.blockNumber:
                    current_block = self.__config.web3_client.eth.blockNumber
                    self.__logger.debug("A new block is mined # {0}".format(str(current_block)))
                    try:
                        handler()
                    except Exception as e:
                        self.config.logger.exception(
                            "Error in block mined thread handler: {0}".format(str(e)))
                        raise e

        new_block_monitor_thread = Thread(target=exec, name="{0} thread".format(handler_name))
        new_block_monitor_thread.start()

        return new_block_monitor_thread

    @property
    def config(self):
        return self.__config

    def __compute_gas_price(self):
        """
        Queries recent blocks to set a baseline gas price, or uses a default static gas price
        """
        gas_price = None
        # if we're not using the dynamic gas price strategy, just return the default
        if self.__config.gas_price_strategy == "static":
            gas_price = self.__config.default_gas_price_wei
        else:
            gas_price = self.__config.web3_client.eth.gasPrice
        gas_price = int(min(gas_price, self.__config.max_gas_price_wei))
        # set the gas_price in config
        self.__config.gas_price_wei = gas_price
        self.__logger.debug("Current gas price: {0}".format(str(gas_price)))

    def run(self):
        """
        Starts all the threads processing different stages of a given event.
        """
        if self.__exec:
            raise Exception("Cannot run audit node thread due to another audit node thread instance")

        self.__exec = True

        # First thing is to check whether the audit node is whitelisted or not.
        if not self.__check_whitelist(self.config.account):
            msg = "Node address {0} is not whitelisted. Please contact Quantstamp: protocol@quantstamp.com"
            self.__logger.error(msg.format(self.config.account))
            raise NonWhitelistedNodeException(msg.format(self.config.account))

        # Initialize the gas price
        self.__compute_gas_price()

        # Ensure that the min_price in the smart contract is up to date
        self.__check_and_update_min_price()

        if self.__config.metric_collection_is_enabled:
            self.__metric_collector = MetricCollector(self.__config)
            self.__metric_collector.collect()
            self.__internal_threads.append(self.__run_metrics_thread())

        # Upon restart, before processing, set all events that timed out to err.
        self.__timeout_stale_requests()

        # If no block has currently been processed, start from the current block
        # Note: this default behavior will prevent the node from finding existing audit transactions
        start_block = self.__config.event_pool_manager.get_latest_block_number()
        if start_block < 0:
            # the database is empty
            current_block_number = self.__config.web3_client.eth.blockNumber
            n_blocks_in_the_past = self.__config.start_n_blocks_in_the_past
            start_block = max(0, current_block_number - n_blocks_in_the_past)

        self.__logger.debug("Filtering events from block # {0}".format(str(start_block)))

        self.__internal_threads.append(self.__run_block_mined_thread(
            "check_available_requests",
            self.__check_then_do_audit_request
        ))

        self.__internal_threads.append(self.__run_block_mined_thread(
            "compute_gas_price",
            self.__compute_gas_price
        ))
        self.__internal_threads.append(self.__run_audit_evt_thread(
            QSPAuditNode.__EVT_AUDIT_ASSIGNED,
            self.__config.audit_contract.events.LogAuditAssigned.createFilter(
                fromBlock=start_block),
            self.__on_audit_assigned,
        ))
        self.__internal_threads.append(self.__run_audit_evt_thread(
            QSPAuditNode.__EVT_REPORT_SUBMITTED,
            self.__config.audit_contract.events.LogAuditFinished.createFilter(
                fromBlock=start_block),
            self.__on_report_submitted,
        ))

        # Starts two additional threads for performing audits
        # and eventually submitting results
        self.__internal_threads.append(self.__run_perform_audit_thread())
        self.__internal_threads.append(self.__run_submission_thread())
        self.__internal_threads.append(self.__run_monitor_submisson_thread())

        # Monitors the state of each thread. Upon error, terminate the
        # audit node. Checking whether a thread is alive or not does
        # not account for pastEvent threads, which necessarily die
        # after processing them all.

        self.__audit_node_initialized = True

        health_check_interval_sec = 2
        thread_lost = False
        while self.__exec:
            # Checking if all threads are still alive
            for thread in self.__internal_threads:
                if not thread.is_alive():
                    thread_lost = True
                    break
            if thread_lost:
                raise Exception(
                    "Cannot proceed execution. At least one internal thread is not alive")
            sleep(health_check_interval_sec)

    def __timeout_stale_requests(self):
        first_valid_block = self.__config.web3_client.eth.blockNumber - \
                            self.__config.submission_timeout_limit_blocks + \
                            self.__config.block_discard_on_restart

        def timeout_event(evt):
            try:
                if first_valid_block >= evt['block_nbr']:
                    evt['status_info'] = "Submission timeout"
                    self.__config.event_pool_manager.set_evt_to_error(evt)
            except KeyError as error:
                self.__logger.exception(
                    "KeyError when handling timeout on restart: {0}".format(str(error))
                )
            except Exception as error:
                self.__logger.exception(
                    "Unexpected error when handling timeout on restart: {0}".format(error))

        self.__config.event_pool_manager.process_incoming_events(timeout_event)
        self.__config.event_pool_manager.process_events_to_be_submitted(timeout_event)

    def __check_then_do_audit_request(self):
        """
        Checks first an audit is assignable; then, bids to get an audit request.
        """
        try:
            pending_requests_count = make_read_only_call(
                self.config,
                self.config.audit_contract.functions.assignedRequestCount(self.__config.account))
            if pending_requests_count >= self.__config.max_assigned_requests:
                self.__logger.debug(
                    "Skip bidding the request as currently processing {0} requests".format(
                        str(pending_requests_count)))
                return
            any_request_available = make_read_only_call(
                self.config,
                self.config.audit_contract.functions.anyRequestAvailable())
            if any_request_available == self.__AVAILABLE_AUDIT__STATE_READY:
                self.__logger.debug("There is request available to bid on.")
                self.__get_next_audit_request()
            else:
                self.__logger.debug(
                    "No request available as the contract returned {0}.".format(
                        str(any_request_available)))
        except DeduplicationException as error:
            self.__logger.debug(
                "Error when attempting to perform an audit request: {0}".format(str(error))
            )
        except Exception as error:
            self.__logger.exception(str(error))

    def __check_whitelist(self, node):
        """
        Checks that a node address is whitelisted.
        """
        try:
            return make_read_only_call(
                self.config,
                self.config.audit_data_contract.functions.isWhitelisted(node)
            )
        except Exception as error:
            self.__logger.exception(
                "Error when checking whitelist {0}".format(str(error))
            )

    def __check_and_update_min_price(self):
        """
        Checks that the minimum price in the audit node's configuration matches the smart contract.
        """
        msg = "Make sure the account has enough Ether, " \
            + "the Ethereum node is connected and synced, " \
            + "and restart your node to try again."

        contract_price = make_read_only_call(
            self.__config,
            self.__config.audit_data_contract.functions.getMinAuditPrice(
                self.__config.account)
        )
        min_price_in_mini_qsp = self.__config.min_price_in_qsp * (10 ** 18)
        if min_price_in_mini_qsp != contract_price:
            self.__logger.info(
                "Local min_price does not match smart contract for address {0}, updating.".format(
                    self.__config.account
                ))
            transaction = self.__config.audit_contract.functions.setAuditNodePrice(
                                            min_price_in_mini_qsp)
            try:
                tx_hash = send_signed_transaction(self.__config,
                                                  transaction,
                                                  wait_for_transaction_receipt=True)
                # If the tx_hash is None, the transaction did not actually complete. Exit.
                if not tx_hash:
                    raise Exception("The min price transaction did not complete")
                self.__logger.debug("Successfully updated min price to {0}.".format(
                    self.__config.min_price_in_qsp))
            except Timeout as e:
                error_msg = "Update min price timed out. " + msg + " {0}, {1}."
                self.__logger.debug(error_msg.format(
                    str(transaction),
                    str(e)))
                raise e
            except DeduplicationException as e:
                error_msg = "A transaction already exists for updating min price," \
                    + " but has not yet been mined. " + msg \
                    + " This may take several iterations. {0}, {1}."
                self.__logger.debug(error_msg.format(
                    str(transaction),
                    str(e)))
                raise e
            except Exception as e:
                error_msg = "Error occurred setting min price. " + msg + " {0}, {1}."
                self.__logger.exception(error_msg.format(
                    str(transaction),
                    str(e)))
                raise e

    def __on_audit_assigned(self, evt):
        request_id = None
        try:
            request_id = str(evt['args']['requestId'])
            target_auditor = evt['args']['auditor']

            # If an audit request is not targeted to the
            # running audit node, just disconsider it
            if target_auditor.lower() != self.__config.account.lower():
                self.__logger.debug(
                    "Ignoring audit request (not directed at current node): {0}".format(
                        str(evt)
                    ),
                    requestId=request_id,
                )
                return

            self.__logger.debug(
                "Saving audit request for processing (if new): {0}".format(
                    str(evt)
                ),
                requestId=request_id,
            )

            price = evt['args']['price']
            request_id = str(evt['args']['requestId'])
            audit_evt = {
                'request_id': request_id,
                'requestor': str(evt['args']['requestor']),
                'contract_uri': str(evt['args']['uri']),
                'evt_name': QSPAuditNode.__EVT_AUDIT_ASSIGNED,
                'block_nbr': evt['blockNumber'],
                'status_info': "Audit Assigned",
                'price': str(price),
            }
            self.__config.event_pool_manager.add_evt_to_be_assigned(
                audit_evt
            )
        except KeyError as error:
            self.__logger.exception(
                "KeyError when processing audit assigned event: {0}".format(str(error))
            )
        except Exception as error:
            self.__logger.exception(
                "Error when processing audit assigned event {0}: {1}".format(str(evt), str(error)),
                requestId=request_id,
            )
            self.__config.event_pool_manager.set_evt_to_error(evt)

    def __run_perform_audit_thread(self):
        def process_audit_request(evt):
            request_id = None
            try:
                requestor = evt['requestor']
                request_id = evt['request_id']
                contract_uri = evt['contract_uri']
                audit_result = self.audit(requestor, contract_uri, request_id)
                if audit_result is None:
                    error = "Could not generate report"
                    evt['status_info'] = error
                    self.__logger.exception(error, requestId=request_id)
                    self.__config.event_pool_manager.set_evt_to_error(evt)
                else:
                    evt['audit_uri'] = audit_result['audit_uri']
                    evt['audit_hash'] = audit_result['audit_hash']
                    evt['audit_state'] = audit_result['audit_state']
                    evt['status_info'] = "Sucessfully generated report"
                    msg = "Generated report URI is {0}. Saving it in the internal database " \
                          "(if not previously saved)"
                    self.__logger.debug(
                        msg.format(str(evt['audit_uri'])), requestId=request_id, evt=evt
                    )
                    self.__config.event_pool_manager.set_evt_to_be_submitted(evt)
            except KeyError as error:
                self.__logger.exception(
                    "KeyError when processing audit for request event: {0}".format(str(error))
                )
            except Exception as error:
                self.__logger.exception(
                    "Error when performing audit for request event {0}: {1}".format(str(evt),
                                                                                    str(error)),
                    requestId=request_id,
                )
                evt['status_info'] = traceback.format_exc()
                self.__config.event_pool_manager.set_evt_to_error(evt)

        def exec():
            while self.__exec:
                self.__config.event_pool_manager.process_incoming_events(
                    process_audit_request
                )
                sleep(self.__config.evt_polling)

        audit_thread = Thread(target=exec, name="audit thread")
        self.__internal_threads.append(audit_thread)
        audit_thread.start()

        return audit_thread

    def __run_submission_thread(self):
        def process_submission_request(evt):
            try:
                tx_hash = self.__submit_report(
                    int(evt['request_id']),
                    evt['audit_state'],
                    str(evt['audit_hash']),
                )
                evt['tx_hash'] = tx_hash
                evt['status_info'] = 'Report submitted (waiting for confirmation)'
                self.__config.event_pool_manager.set_evt_to_submitted(evt)
            except DeduplicationException as error:
                self.__logger.debug(
                    "Error when submiting report {0}".format(str(error))
                )
            except KeyError as error:
                self.__logger.exception(
                    "KeyError when processing submission event: {0}".format(str(error))
                )
            except Exception as error:
                self.__logger.exception(
                    "Error when processing submission event {0}: {1}.".format(
                        str(evt['request_id']),
                        str(error),
                    ),
                    requestId=evt['request_id'],
                )
                evt['status_info'] = traceback.format_exc()
                self.__config.event_pool_manager.set_evt_to_error(evt)

        def exec():
            while self.__exec:
                self.__config.event_pool_manager.process_events_to_be_submitted(
                    process_submission_request
                )
                sleep(self.__config.evt_polling)

        submission_thread = Thread(target=exec, name="submission thread")
        self.__internal_threads.append(submission_thread)
        submission_thread.start()

        return submission_thread

    def __on_report_submitted(self, evt):
        audit_evt = None
        request_id = None
        try:
            request_id = str(evt['args']['requestId'])
            target_auditor = evt['args']['auditor']

            # If an audit request is not targeted to the
            # running audit node, just disconsider it
            if target_auditor.lower() != self.__config.account.lower():
                self.__logger.debug(
                    "Ignoring submission event (not directed at current node): {0}".format(
                        str(evt)
                    ),
                    requestId=request_id,
                )
                return

            audit_evt = self.__config.event_pool_manager.get_event_by_request_id(
                request_id
            )
            if audit_evt != {}:
                audit_evt['status_info'] = 'Report successfully submitted'
                self.__config.event_pool_manager.set_evt_to_done(
                    audit_evt
                )
        except KeyError as error:
            self.__logger.exception(
                "KeyError when processing submission event: {0}".format(str(error))
            )
        except Exception as error:
            self.__logger.exception(
                "Error when processing submission event {0}: {1}. Audit event is {2}".format(
                    str(evt),
                    str(error),
                    str(audit_evt),
                ),
                requestId=request_id,
            )

    def __run_monitor_submisson_thread(self):
        timeout_limit = self.__config.submission_timeout_limit_blocks

        def monitor_submission_timeout(evt, current_block):
            try:
                if (current_block - evt['block_nbr']) > timeout_limit:
                    evt['status_info'] = "Submission timeout"
                    self.__config.event_pool_manager.set_evt_to_error(evt)
                    msg = "Submission timeout for audit {0}. Setting to error"
                    self.__config.logger.debug(msg.format(str(evt['request_id'])))
            except KeyError as error:
                self.__logger.exception(
                    "KeyError when monitoring timeout: {0}".format(str(error))
                )
            except Exception as error:
                # TODO How to inform the network of a submission timeout?
                self.__logger.exception(
                    "Unexpected error when monitoring timeout: {0}".format(error))

        def exec():
            try:
                while self.__exec:
                    # Checks for a potential timeouts
                    block = self.__config.web3_client.eth.blockNumber
                    self.__config.event_pool_manager.process_submission_events(
                        monitor_submission_timeout,
                        block,
                    )

                    sleep(self.__config.evt_polling)
            except Exception as error:
                self.__logger.exception("Error in the monitor thread: {0}".format(str(error)))

        monitor_thread = Thread(target=exec, name="monitor thread")
        self.__internal_threads.append(monitor_thread)
        monitor_thread.start()

        return monitor_thread

    def __run_metrics_thread(self):
        def exec():
            while self.__exec:
                self.__metric_collector.collect()
                sleep(self.__config.metric_collection_interval_seconds)

        metrics_thread = Thread(target=exec, name="metrics thread")
        self.__internal_threads.append(metrics_thread)
        metrics_thread.start()

        return metrics_thread

    def stop(self):
        """
        Signals to the executing QSP audit node that is should stop the execution of the node.
        """

        self.__logger.info("Stopping QSP Audit Node")
        self.__exec = False

        for internal_thread in self.__internal_threads:
            internal_thread.join()
        self.__internal_threads = []

        # Close resources
        self.__config.event_pool_manager.close()

    def __validate_json(self, report, request_id):
        """
        Validate that the report conforms to the schema.
        """
        try:
            file_path = os.path.realpath(__file__)
            schema_file = '{0}/../../analyzers/schema/analyzer_integration.json'.format(
                os.path.dirname(file_path))
            with open(schema_file) as schema_data:
                schema = json.load(schema_data)
            jsonschema.validate(report, schema)
            return report
        except jsonschema.ValidationError as e:
            self.__logger.exception(
                "Error: JSON could not be validated: {0}.".format(str(e)),
                requestId=request_id,
            )
            raise Exception("JSON could not be validated") from e

    def audit(self, requestor, uri, request_id):
        """
        Audits a target contract.
        """
        self.__logger.info(
            "Executing audit on contract at {0}".format(uri),
            requestId=request_id,
        )

        target_contract = fetch_file(uri)

        warnings, errors = self.check_compilation(target_contract, request_id, uri)
        audit_report = {}
        if len(errors) != 0:
            audit_report = self.__create_err_result(errors, warnings, request_id, requestor, uri,
                                                    target_contract)
        else:
            audit_report = self.get_audit_report_from_analyzers(target_contract, requestor, uri,
                                                                request_id)
            if len(warnings) != 0:
                audit_report['compilation_warnings'] = warnings

        self.__logger.info(
            "Analyzer report contents",
            requestId=request_id,
            contents=audit_report,
        )

        self.__validate_json(audit_report, request_id)

        audit_report_str = json.dumps(audit_report, indent=2)
        audit_hash = digest(audit_report_str)
        upload_result = self.__config.report_uploader.upload(audit_report_str,
                                                             audit_report_hash=audit_hash)

        self.__logger.info(
            "Report upload result: {0}".format(upload_result),
            requestId=request_id,
        )

        if not upload_result['success']:
            raise Exception("Error uploading report: {0}".format(json.dumps(upload_result)))

        parse_uri = urllib.parse.urlparse(uri)
        original_filename = os.path.basename(parse_uri.path)
        contract_body = read_file(target_contract)
        contract_upload_result = self.__config.report_uploader.upload_contract(request_id,
                                                                               contract_body,
                                                                               original_filename)
        if contract_upload_result['success']:
            self.__logger.info(
                "Contract upload result: {0}".format(contract_upload_result),
                requestId=request_id,
            )
        else:
            # We just log on error, not raise an exception
            self.__logger.error(
                "Contract upload result: {0}".format(contract_upload_result),
                requestId=request_id,
            )

        return {
            'audit_state': audit_report['audit_state'],
            'audit_uri': upload_result['url'],
            'audit_hash': audit_hash
        }

    def get_audit_report_from_analyzers(self, target_contract, requestor, uri, request_id):

        number_of_analyzers = len(self.__config.analyzers)
        # This array is shared between the current thread and wrappers
        shared_analyzers_reports = [{}] * number_of_analyzers
        parse_uri = urllib.parse.urlparse(uri)
        original_filename = os.path.basename(parse_uri.path)

        analyzers_reports_locks = []
        for i in range(0, number_of_analyzers):
            analyzers_reports_locks.append(threading.RLock())

        def check_contract(analyzer_id):
            analyzer = self.__config.analyzers[analyzer_id]
            metadata = analyzer.get_metadata(target_contract, request_id, original_filename)
            # in case of time out, declare the metadata as the report now
            str_metadata = json.dumps(metadata)

            try:
                analyzers_reports_locks[analyzer_id].acquire()
                shared_analyzers_reports[analyzer_id] = {**metadata, 'hash': digest(str_metadata)}
            finally:
                analyzers_reports_locks[analyzer_id].release()

            result = analyzer.check(target_contract, request_id, original_filename)
            # the values from metadata will overwrite those of report
            report = {**result, **metadata}
            str_report = json.dumps(report)
            report['hash'] = digest(str_report)

            # Make sure no race-condition between the wrappers and the current thread
            try:
                analyzers_reports_locks[analyzer_id].acquire()
                shared_analyzers_reports[analyzer_id] = report
            finally:
                analyzers_reports_locks[analyzer_id].release()

        analyzers_threads = []
        analyzers_timeouts = []
        analyzers_start_times = []

        # Starts each analyzer thread
        for i, analyzer in enumerate(self.__config.analyzers):
            thread_name = "{0}-wrapper-thread".format(analyzer.wrapper.analyzer_name)
            analyzer_thread = Thread(target=check_contract, args=[i], name=thread_name)
            analyzers_threads.append(analyzer_thread)
            analyzer_name = self.__config.analyzers[i].wrapper.analyzer_name
            timeout_sec = int(self.__config.analyzers_config[i][analyzer_name]['timeout_sec'])
            analyzers_timeouts.append(timeout_sec)

            start_time = calendar.timegm(time.gmtime())
            analyzers_start_times.append(start_time)
            analyzer_thread.start()

        # This array should only be accessible from the current thread
        local_analyzers_reports = [{}] * number_of_analyzers

        for i in range(0, number_of_analyzers):
            analyzers_threads[i].join(analyzers_timeouts[i])

            # Make sure there is no race condition between the current thread
            # and wrapper thread in overwriting on analyzers_reports
            try:
                analyzers_reports_locks[i].acquire()
                local_analyzers_reports[i] = copy.deepcopy(shared_analyzers_reports[i])
            finally:
                analyzers_reports_locks[i].release()

            # NOTE
            # Due to timeout issues, one has to account for start/end
            # times at this point, rather than the wrapper itself

            start_time = analyzers_start_times[i]
            local_analyzers_reports[i]['start_time'] = start_time

            # If thread is still alive, it means a timeout has
            # occurred
            if analyzers_threads[i].is_alive():
                # In case of time out, the metadata should exist as the report, unless that somehow
                # failed too
                analyzer_name = self.__config.analyzers[i].wrapper.analyzer_name
                if shared_analyzers_reports[i]:
                    local_analyzers_reports[i] = shared_analyzers_reports[i]
                else:
                    local_analyzers_reports[i]['analyzer'] = {'name': analyzer_name}
                local_analyzers_reports[i]['errors'] = [
                    "Time out occurred. Could not finish {0} within {1} seconds".format(
                        analyzer_name,
                        self.config.analyzers[i].wrapper.timeout_sec,
                    )
                ]

                local_analyzers_reports[i]['status'] = 'error'
            else:
                # A timeout has not occurred. Register the end time
                end_time = calendar.timegm(time.gmtime())
                local_analyzers_reports[i]['end_time'] = end_time

        audit_report = {
            'timestamp': calendar.timegm(time.gmtime()),
            'contract_uri': uri,
            'contract_hash': digest_file(target_contract),
            'requestor': requestor,
            'auditor': self.__config.account,
            'request_id': request_id,
            'version': self.__config.node_version,
        }

        # FIXME
        # This is currently a very simple mechanism to claim an audit as
        # successful or not. Either it is fully successful (all analyzer produce a result),
        # or fails otherwise.
        audit_state = QSPAuditNode.__AUDIT_STATE_SUCCESS
        audit_status = QSPAuditNode.__AUDIT_STATUS_SUCCESS
        for i, analyzer_report in enumerate(local_analyzers_reports):
            analyzer_name = self.__config.analyzers[i].wrapper.analyzer_name

            # The next two fail safe checks should never kick in

            # This is a fail safe mechanism (defensive programming)
            if 'analyzer' not in analyzer_report:
                analyzer_report['analyzer'] = {
                    'name': analyzer_name
                }

            # Another fail safe mechanism (defensive programming)
            if 'status' not in analyzer_report:
                analyzer_report['status'] = 'error'
                errors = analyzer_report.get('errors', [])
                errors.append('Unknown error: cannot produce report')
                analyzer_report['errors'] = errors

            # Invariant: no analyzer report can ever be empty!

            if analyzer_report['status'] == 'error':
                audit_state = QSPAuditNode.__AUDIT_STATE_ERROR
                audit_status = QSPAuditNode.__AUDIT_STATUS_ERROR
        audit_report['audit_state'] = audit_state
        audit_report['status'] = audit_status
        if len(local_analyzers_reports) > 0:
            audit_report['analyzers_reports'] = local_analyzers_reports
        return audit_report

    def __get_next_audit_request(self):
        """
        Attempts to get a request from the audit request queue.
        """
        transaction = self.__config.audit_contract.functions.getNextAuditRequest()
        tx_hash = None
        try:
            tx_hash = send_signed_transaction(
                self.__config,
                transaction,
                wait_for_transaction_receipt=True)
            self.__config.logger.debug("A getNextAuditRequest transaction has been sent")
        except Timeout as e:
            self.__logger.debug("Transaction receipt timeout happened for {0}. {1}".format(
                str(transaction),
                e))
        return tx_hash

    def __submit_report(self, request_id, audit_state, audit_hash):
        """
        Submits the audit report to the entire QSP network.
        """
        tx_hash = send_signed_transaction(self.__config,
                                          self.__config.audit_contract.functions.submitReport(
                                            request_id,
                                            audit_state,
                                            audit_hash))
        self.__config.logger.debug("Report {0} has been submitted".format(str(request_id)))
        return tx_hash

    def __create_err_result(self, errors, warnings, request_id, requestor, uri, target_contract):
        result = {
            'timestamp': calendar.timegm(time.gmtime()),
            'contract_uri': uri,
            'contract_hash': digest_file(target_contract),
            'requestor': requestor,
            'auditor': self.__config.account,
            'request_id': request_id,
            'version': self.__config.node_version,
            'audit_state': QSPAuditNode.__AUDIT_STATE_ERROR,
            'status': QSPAuditNode.__AUDIT_STATUS_ERROR,
        }
        if errors is not None and len(errors) != 0:
            result['compilation_errors'] = errors
        if warnings is not None and len(warnings) != 0:
            result['compilation_warnings'] = warnings

        return result

    def check_compilation(self, contract, request_id, uri):
        self.__logger.debug("Running compilation check. About to check {0}".format(contract),
                            requestId=request_id)
        parse_uri = urllib.parse.urlparse(uri)
        original_filename = os.path.basename(parse_uri.path)
        temp_filename = os.path.basename(contract)
        data = ""
        with open(contract, 'r') as myfile:
            data = myfile.read()
        warnings = []
        errors = []
        try:
            # Attempts to compile the target contract. If it fails, a ContractsNotFound
            # exception is thrown
            file_name = contract[contract.rfind('/') + 1:]
            output = compile_standard({'language': 'Solidity',
                                       'sources': {
                                           file_name: {'content': data}}}
                                      )
            for err in output['errors']:
                if err["severity"] == "warning":
                    warnings += [err['formattedMessage'].replace(temp_filename, original_filename)]
                else:
                    errors += [err['formattedMessage'].replace(temp_filename, original_filename)]

        except ContractsNotFound as error:
            self.__logger.debug(
                "ContractsNotFound before calling analyzers: {0}".format(str(error)),
                requestId=request_id)
            errors += [str(error)]
        except SolcError as error:
            self.__logger.debug(
                "SolcError before calling analyzers: {0}".format(str(error)),
                requestId=request_id)
            errors += [str(error)]
        except KeyError as error:
            self.__logger.error(
                "KeyError when calling analyzers: {0}".format(str(error)),
                requestId=request_id)
            # This is thrown because a bug in our own code. We only log, but do not record the error
            # so that the analyzers are still executed.
        except Exception as error:
            self.__logger.error(
                "Error before calling analyzers: {0}".format(str(error)),
                requestId=request_id)
            errors += [str(error)]

        return warnings, errors

    @property
    def audit_node_initialized(self):
        return self.__audit_node_initialized
