import logging
from shutil import which

import confu.schema

import vaping
from vaping.io import subprocess
from vaping.plugins import TimedProbeSchema


class FPingSchema(TimedProbeSchema):
    """
    Define a schema for FPing and also define defaults.
    """

    count = confu.schema.Int(default=5, help="Number of pings to send")
    interval = confu.schema.Str(default="1m", help="Time between pings")
    output = confu.schema.List(
        item=confu.schema.Str(), help="Determine what plugin displays output"
    )
    period = confu.schema.Int(
        default=20,
        help="Time in milliseconds that fping waits between successive packets to an individual target",
    )
    command = confu.schema.Str(default="fping", help="Command to run")


class FPingBase(vaping.plugins.TimedProbe):
    """
    FPing base plugin

    config:

    # Config

    - command (`str=fping`): command to run
    - interval (`str=1m`): time between pings
    - count (`int=5`): number of pings to send
    - period (`int=20`): time in milliseconds that fping waits
    between successive packets to an individual target

    # Instanced Attributes

    - count (`int`): number of fpings to send
    - period (`int`): time in milliseconds that fping waits between successive packets
    """

    ConfigSchema = FPingSchema

    def __init__(self, config, ctx):
        super().__init__(config, ctx)

        if not which(self.config["command"]):
            self.log.critical(
                "missing fping, install it or set `command` in the fping config"
            )
            raise RuntimeError("fping command not found - install the fping package")

        self.count = self.config.get("count")
        self.period = self.config.get("period")
        # Detect version during initialization to avoid repeated checks
        self._fping_version = self.detect_fping_version()
        self.log.debug(f"Detected fping version: {self._fping_version}")

    def detect_fping_version(self):
        """Detect fping version to determine output format parser"""
        try:
            args = [self.config["command"], "-v"]
            # Use self.popen for consistency and potential future benefits (like handling timeouts)
            with self.popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            ) as proc:
                # Read all output at once after process finishes
                version_out, _ = proc.communicate()
                version_out = version_out.decode("utf-8")
                if "fping: Version 5" in version_out:
                    return 5
                # Add checks for other versions if needed
                return 4  # Default to 4 if not 5
        except FileNotFoundError:
            self.log.critical(f"fping command '{self.config['command']}' not found.")
            raise
        except Exception as e:
            # Log the error and default to a safe version (e.g., 4)
            self.log.error(
                f"Failed to detect fping version: {e}. Defaulting to version 4."
            )
            return 4

    def hosts_args(self):
        """
        hosts list can contain strings specifying a host directly
        or dicts containing a "host" key to specify the host

        this way we can allow passing further config details (color, name etc.)
        with each host as well as simply dropping in addresses for quick
        setup depending on the user's needs
        """

        host_args = []
        for row in self.hosts:
            if isinstance(row, dict):
                host_args.append(row["host"])
            else:
                host_args.append(row)

        # using a set changes the order
        dedupe = list()
        for each in host_args:
            if each not in dedupe:
                dedupe.append(each)
        return dedupe

    def parse_verbose(self, line):
        """
        parse output from verbose format

        **Returns**

        parsed fping result (`dict`)

        - `host`: host name
        - `cnt`: fpings sent
        - `loss`
        - `data`: list of inidivual fping times
        - `min`: smallest fping time
        - `max`: biggest fping time
        - `avg`: average fping time
        - `last`: last fping time
        """
        try:
            logging.debug(line)
            (host, pings) = line.split(" : ")
            cnt = 0
            lost = 0
            times = []
            pings = pings.strip().split(" ")
            cnt = len(pings)
            for latency in pings:
                if latency == "-":
                    continue
                times.append(float(latency))

            lost = cnt - len(times)
            if lost:
                loss = lost / float(cnt)
            else:
                loss = 0.0

            rv = {
                "host": host.strip(),
                "cnt": cnt,
                "loss": loss,
                "data": times,
            }
            if times:
                rv["min"] = min(times)
                rv["max"] = max(times)
                rv["avg"] = sum(times) / len(times)
                rv["last"] = times[-1]
            return rv

        except Exception as e:
            # Log the specific line causing the error for better debugging
            logging.error(f"Failed to parse fping line '{line.strip()}': {e}")
            return None  # Return None to indicate parsing failure for this line

    def _run_proc(self):
        args = [
            self.config["command"],
            "-u",  # Unreachable hosts
            "-C%d" % self.count,  # Count
            "-p%d" % self.period,  # Period
            "-e",  # Elapsed time
        ]

        # Conditionally add -q for fping version 5
        if self._fping_version == 5:
            args.append(
                "-q"
            )  # Quiet mode (suppresses per-probe results, shows summary)

        args.extend(self.hosts_args())
        data = list()

        self.log.debug(f"Running fping command: {' '.join(args)}")

        # get both stdout and stderr
        try:
            with self.popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            ) as proc:
                for line in iter(proc.stdout.readline, b""):
                    decoded_line = line.decode("utf-8")
                    parsed_data = self.parse_verbose(decoded_line)
                    if parsed_data:  # Only append if parsing was successful
                        data.append(parsed_data)
                # Check process exit code after reading output
                proc.wait()
                if proc.returncode != 0:
                    self.log.warning(
                        f"fping process exited with code {proc.returncode}"
                    )

        except FileNotFoundError:
            self.log.critical(
                f"fping command '{self.config['command']}' not found during execution."
            )
            # Re-raise or handle appropriately, maybe return empty data or specific error message
            raise
        except Exception as e:
            self.log.error(f"Error running or processing fping: {e}")
            # Depending on desired behavior, you might return partial data or empty list
            return []  # Return empty list on error

        return data


@vaping.plugin.register("fping")
class FPing(FPingBase):
    """
    Run fping on configured hosts

    # Config

    - command (`str=fping`): command to run
    - interval (`str=1m`): time between pings
    - count (`int=5`): number of pings to send
    - period (`int=20`): time in milliseconds that fping waits
    between successive packets to an individual target
    """

    def init(self):
        self.hosts = []

        for name, group_config in list(self.groups.items()):
            self.hosts.extend(group_config.get("hosts", []))

    def probe(self):
        msg = self.new_message()
        # Run the fping process and get parsed data
        proc_data = self._run_proc()
        # Filter out any None results from parsing errors or ignored lines
        msg["data"] = [d for d in proc_data if d is not None]
        # Only return message if there's valid data
        if msg["data"]:
            return msg
        else:
            self.log.warning("No valid data returned from fping process.")
            return None
