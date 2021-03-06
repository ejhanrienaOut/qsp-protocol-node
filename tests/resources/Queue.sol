/***************************************************************************************************
 *                                                                                                 *
 * (c) 2018, 2019 Quantstamp, Inc. This content and its use are governed by the license terms at   *
 * <https://s3.amazonaws.com/qsp-protocol-license/V2_LICENSE.txt>.                                 *
 *                                                                                                 *
 **************************************************************************************************/

pragma solidity ^0.4.18;

////////////////////////////////////////////////////////////
// Based on: https://github.com/chriseth/solidity-examples/blob/master/queue.sol
////////////////////////////////////////////////////////////
contract Uint256Queue {
  uint256[] private data;
  uint private front;
  uint private back;

  event LogQueueIsEmptyError();
  event LogQueueIsFullError(uint capacity, uint256 item);
  event LogInvalidItem(uint256 item);

  enum PopResult {
    Success,
    QueueIsEmpty
  }

  enum PushResult {
    Success,
    QueueIsFull
  }

  function Uint256Queue(uint capacity) public {
    data.length = capacity;
  }

  function length() constant public returns (uint) {
    return (data.length + back - front) % data.length;
  }

  function capacity() constant public returns (uint) {
    return data.length;
  }

  function isEmpty() constant public returns (bool) {
    return front == back;
  }

  function push(uint256 item) public returns (PushResult) {
    if ((back + 1) % data.length == front) {
      LogQueueIsFullError(capacity(), item);
      return PushResult.QueueIsFull;
    }
    data[back] = item;
    back = (back + 1) % data.length;
    return PushResult.Success;
  }

  function pop() public returns (PopResult result, uint256 item) {
    (result, item) = peek();
    if (result == PopResult.Success) {
      delete(data[front]);
      front = (front + 1) % data.length;
    }
  }

  function peek() public returns (PopResult result, uint256 item) {
    if (back == front) {
      LogQueueIsEmptyError();
      result = PopResult.QueueIsEmpty;
      return;
    }
    item = data[front];
    result = PopResult.Success;
  }
}
