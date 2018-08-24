from web3.utils.threads import Timeout

from .singleton_lock import SingletonLock


def mk_args(config):
    gas = config.gas
    gas_price_wei = config.gas_price_wei
    if gas is None:
        args = {'from': config.account, 'gasPrice': gas_price_wei}
    else:
        gas_value = int(gas)
        if gas_value >= 0:
            args = {'from': config.account, 'gas': gas_value, 'gasPrice': gas_price_wei}
        else:
            raise ValueError("The gas value is negative: " + str(gas_value))

    return args


def make_read_only_call(config, method):
    try:
        SingletonLock.instance().lock.acquire()
        return method.call()
    finally:
        try:
            SingletonLock.instance().lock.release()
        except Exception as error:
            config.logger.debug(
                "Error when releasing a lock in a read-only call transaction {0}".format(str(error))
            )


def send_signed_transaction(config, transaction, attempts=10, wait_for_transaction_receipt=False):
    try:
        SingletonLock.instance().lock.acquire()
        return __send_signed_transaction(config,
                                         transaction,
                                         attempts,
                                         wait_for_transaction_receipt)
    finally:
        try:
            SingletonLock.instance().lock.release()
        except Exception as error:
            config.logger.debug(
                "Error when releasing a lock in signed transaction {0}".format(str(error))
            )


def __send_signed_transaction(config, transaction, attempts=10, wait_for_transaction_receipt=False):
    args = mk_args(config)
    if config.account_private_key is None:  # no local signing (in case of tests)
        return transaction.transact(args)
    else:
        nonce = config.web3_client.eth.getTransactionCount(config.account)
        original_nonce = nonce
        for i in range(attempts):
            try:
                args['nonce'] = nonce
                tx = transaction.buildTransaction(args)
                signed_tx = config.web3_client.eth.account.signTransaction(tx,
                                                                           private_key=config.account_private_key)
                tx_hash = config.web3_client.eth.sendRawTransaction(signed_tx.rawTransaction)

                if wait_for_transaction_receipt:
                    tx_receipt = config.web3_client.eth.waitForTransactionReceipt(tx_hash, 120)

                return tx_hash
            except ValueError as e:
                if i == attempts - 1:
                    config.logger.debug("Maximum number of retries reached. {}"
                                        .format(e))
                    raise e
                elif "replacement transaction underpriced" in repr(e):
                    config.logger.debug("Another transaction is queued with the same nonce. {}"
                                        .format(e))
                    nonce += 1
                elif "nonce too low" in repr(e):
                    msg = "This nonce is too low {}. Web3 says transaction count is {}. " \
                          "The original nonce was {}. Error: {}".format(
                        nonce,
                        config.web3_client.eth.getTransactionCount(config.account),
                        original_nonce,
                        e)
                    config.logger.debug(msg)
                    nonce += 1
                elif "known transaction" in repr(e):
                    # the de-duplication is preserved and the exception is re-raised
                    config.logger.debug("Transaction deduplication happened. {}".format(e))
                    raise DeduplicationException(e)
                else:
                    config.logger.error("Unknown error while sending transaction. {}".format(e))
                    raise e
            except Timeout as e:
                # If we actually time out after the default 120 seconds,
                # the thread should continue on as normal.
                # This is to avoid waiting indefinitely for an underpriced transaction.
                config.logger.debug("Transaction receipt timeout happened for {0}. {1}".format(
                    str(transaction),
                    e))


class DeduplicationException(Exception):
    pass
