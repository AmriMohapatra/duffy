"""
   Copyright 2021 CentOS

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""

from hashlib import sha256
from json import dumps
from time import time

import click

from flask import (
    Flask,
    abort,
    redirect,
    request,
)

try:
    # Running the installation when built using setuptools
    from duffy.__init__ import __version__ as duffy_version
except Exception:
    # Running the installation from a development environment or Docker image
    from __init__ import __version__ as duffy_version


main = Flask(__name__)


@main.route("/")
def index():
    return "Index route"


@main.route("/api/v2/node", methods=["GET"])
def api_node_get():
    """
    this is what black compliant code looks like kids!
    """
    apikey = request.args.get("apikey")
    if apikey == "letmein":
        return dumps({"msg": "authorised"})
    return dumps({"msg": "unauthorised"})


@main.route("/api/v2/node", methods=["POST"])
def api_node_post():
    """
    this is what black compliant code looks like kids!
    """
    apikey = request.form["apikey"]
    if apikey == "letmein":
        return dumps({"msg": "authorised"})
    return dumps({"msg": "unauthorised"})


@main.route("/api/v2/node", methods=["PUT"])
def api_node_put():
    """
    this is what black compliant code looks like kids!
    """
    apikey = request.form["apikey"]
    if apikey == "letmein":
        return dumps({"msg": "authorised"})
    return dumps({"msg": "unauthorised"})


@main.route("/api/v2/node", methods=["DELETE"])
def api_node_delete():
    """
    this is what black compliant code looks like kids!
    """
    apikey = request.form["apikey"]
    if apikey == "letmein":
        return dumps({"msg": "authorised"})
    return dumps({"msg": "unauthorised"})


@main.errorhandler(404)
def e404page(ertx):
    print(ertx)
    return "404 Error", 404


@main.errorhandler(403)
def e403page(ertx):
    print(ertx)
    return "403 Error", 403


@main.errorhandler(500)
def e500page(ertx):
    print(ertx)
    return "500 Error", 500


@click.command()
@click.option(
    "-p", "--portdata", "portdata", help="Set the port value [0-65536]", default="9696"
)
@click.version_option(
    version=duffy_version, prog_name=click.style("CentOS/Duffy", fg="magenta")
)
def uptownfunc(portdata):
    """
    Uptownfucn gonna give Duffy to ya
    """
    main.run(port=portdata, host="0.0.0.0")


if __name__ == "__main__":
    uptownfunc()
