####################################################################################################
#                                                                                                  #
# (c) 2018 Quantstamp, Inc. All rights reserved.  This content shall not be used, copied,          #
# modified, redistributed, or otherwise disseminated except to the extent expressly authorized by  #
# Quantstamp for credentialed users. This content and its use are governed by the Quantstamp       #
# Demonstration License Terms at <https://s3.amazonaws.com/qsp-protocol-license/LICENSE.txt>.      #
#                                                                                                  #
####################################################################################################

class BaseUploadProvider:
    def upload_report(self, report_as_string, audit_report_hash=None):
        raise Exception("Unimplemented method")

    def upload_contract(self, request_id, contract_body, file_name):
        raise Exception("Unimplemented method")