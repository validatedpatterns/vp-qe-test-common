import base64
import fileinput
import logging
from typing import Optional

import requests
from ocp_resources.secret import Secret
from requests import RequestException
from urllib3.exceptions import InsecureRequestWarning, ProtocolError
from openshift.dynamic import DynamicClient

from . import __loggername__

logger = logging.getLogger(__loggername__)

# Duplicated in layered-zero-trust, but not used anywhere
# def load_yaml_file(file_path):
#     """
#     Load and parse the yaml file
#     :param file_path: (str) file path
#     :return: (dict) yaml_config_obj in the form of Python dict
#     """
#     yaml_config_obj = None
#     with open(file_path, "r") as yfh:
#         try:
#             yaml_config_obj = yaml.load(yfh, Loader=yaml.FullLoader)
#         except Exception as ex:
#             raise yaml.YAMLError("YAML Syntax Error:\n %s" % ex)
#         logger.info("Yaml Config : %s", yaml_config_obj)
#     return yaml_config_obj


def get_site_response(
    site_url: str, bearer_token: Optional[str] = None
) -> requests.Response:
    """
    Return the HTTP response from the site URL.

    If ``bearer_token`` is provided, it is sent as an Authorization header.
    """
    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    # Suppress only the warning about verify=False.
    requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

    try:
        return requests.get(
            site_url,
            headers=headers,
            verify=False,
        )
    except RequestException:
        raise RuntimeError(f"Failed to connect to '{site_url}'.") from None


# Used in mcg, IE, QNA-chat-amd, zero-trust, omnicloud as a service, netapp-dr-starterkit
def modify_file_content(file_name, orig_content, new_content):

    with open(file_name, "r") as frb:
        logger.debug(f"Current content : {frb.readlines()}")

    with fileinput.FileInput(file_name, inplace=True, backup=".bak") as file:
        for line in file:
            print(
                line.replace(
                    orig_content,
                    new_content,
                ),
                end="",
            )

    with open(file_name, "r") as fra:
        contents = fra.readlines()
        logger.debug(f"Modified content : {contents}")

    return contents
