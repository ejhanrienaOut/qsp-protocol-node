####################################################################################################
#                                                                                                  #
# (c) 2018, 2019 Quantstamp, Inc. This content and its use are governed by the license terms at    #
# <https://s3.amazonaws.com/qsp-protocol-license/V2_LICENSE.txt>                                   #
#                                                                                                  #
####################################################################################################

from .function_call import FunctionCall


class SimpleMock:

    def __init__(self):
        self.expected = []

    def expect(self, function, params, return_value):
        """
        Adds an expected function call to the queue.
        """
        self.expected.append(FunctionCall(function, params, return_value))

    def verify(self):
        """
        Verifies that all the expected calls were performed.
        """
        if len(self.expected) != 0:
            raise Exception('Some expected calls were left over: ' + str(self.expected))

    def call(self, function_name, arguments_to_check, local_values):
        """
        Simulates call to the specified function while checking the expected parameter values
        """
        first_call = self.expected[0]
        if first_call.function_name != function_name:
            raise Exception('{0} call expected'.format(function_name))
        for argument in arguments_to_check:
            if first_call.params[argument] != local_values[argument]:
                msg = 'Value of {0} is not {1} as expected, but {2}'
                raise Exception(
                    msg.format(argument, first_call.params[argument], local_values[argument]))
        self.expected = self.expected[1:]
        return first_call.return_value
