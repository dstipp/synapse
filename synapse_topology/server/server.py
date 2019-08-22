from os.path import abspath, dirname, join

from canonicaljson import json
from synapse_topology import model

from twisted.web.static import File

from .utils import port_checker

from . import error_handlers
from .schemas import (
    BASE_CONFIG_SCHEMA,
    SERVERNAME_SCHEMA,
    CERT_PATHS_SCHEMA,
    CERTS_SCHEMA,
    PORTS_SCHEMA,
)
from .utils import validate_schema, log_body_if_fail

from . import app

import subprocess
import sys


@app.route("/topology_webui/", branch=True)
def server_webui(request):
    client_path = abspath(join(dirname(abspath(__file__)), "../webui/dist/"))
    print(client_path)
    return File(client_path)


@app.route("/setup", methods=["GET"])
def get_config_setup(request):
    return json.dumps(
        {
            model.constants.CONFIG_LOCK: model.config_in_use(),
            "config_dir": model.get_config_dir(),
        }
    )


@app.route("/servername", methods=["GET"])
def get_server_name(request):
    return model.get_server_name()


@app.route("/servername", methods=["POST"])
@validate_schema(SERVERNAME_SCHEMA)
def set_server_name(request, body):
    model.generate_base_config(**body)


@app.route("/secretkey", methods=["GET"])
def get_secret_key(request):
    return json.dumps({"secret_key": model.get_secret_key()})


@app.route("/config", methods=["GET"])
def get_config(request):
    return str(model.get_config())


@app.route("/config", methods=["POST"])
@validate_schema(BASE_CONFIG_SCHEMA)
def set_config(request, body):
    model.set_config(body)


@app.route("/testcertpaths", methods=["POST"])
@log_body_if_fail
@validate_schema(CERT_PATHS_SCHEMA)
def test_cert_paths(request, body):
    result = {}
    config_path = model.get_config_dir()
    for name, path in body.items():
        path = abspath(join(config_path, path))
        try:
            with open(path, "r"):
                result[name] = {"invalid": False, "absolute_path": path}
        except:
            result[name] = {"invalid": True}
    return json.dumps(result)


@app.route("/certs", methods=["POST"])
@validate_schema(CERTS_SCHEMA)
def upload_certs(request, body):
    model.add_certs(**body)


@app.route("/ports", methods=["POST"])
@validate_schema(PORTS_SCHEMA)
def check_ports(request, body):
    results = []
    for port in body["ports"]:
        results.append(port_checker(port))
    return json.dumps({"ports": results})


@app.route("/iw", methods=["POST"])
def start_synapse(request):
    print("Start")
    subprocess.Popen(["synctl", "start", model.get_config_dir() + "/homeserver.yaml"])
    sys.exit()


@app.route("/favicon.ico")
def noop(request):
    return
