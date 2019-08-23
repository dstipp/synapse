# Paths
DATA_SUBDIR = "data"

# Config options
SERVER_NAME = "server_name"
CONFIG_LOCK = "server_config_in_use"
SECRET_KEY = "macaroon_secret_key"

CONFIG_LOCK_DATA = """

##  CONFIG LOCK ##


# Specifies whether synapse has been started with this config.
# If set to True the setup util will not go through the initialization
# phase which sets the server name and server keys.
{}: {{}}


""".format(
    CONFIG_LOCK
)
