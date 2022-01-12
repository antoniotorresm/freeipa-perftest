#
# Copyright (C) 2021 FreeIPA Contributors see COPYING for license
#

import os
import random
import subprocess as sp
import time

from ipaperftest.core.main import Plugin
from ipaperftest.core.constants import (
    MACHINE_CONFIG_TEMPLATE,
    ANSIBLE_AUTHENTICATIONTEST_SERVER_CONFIG_PLAYBOOK,
    ANSIBLE_AUTHENTICATIONTEST_CLIENT_CONFIG_PLAYBOOK,
    ANSIBLE_COUNT_IPA_HOSTS_PLAYBOOK
)
from ipaperftest.plugins.registry import registry


@registry
class AuthenticationTest(Plugin):

    def __init__(self, registry):
        super().__init__(registry)
        self.custom_logs = ["pamtest.log", ]

    def generate_clients(self, ctx):
        for i in range(ctx.params['amount']):
            idx = str(i).zfill(3)
            machine_name = "client{}".format(idx)
            yield(
                MACHINE_CONFIG_TEMPLATE.format(
                    machine_name=machine_name,
                    box=ctx.params['client_image'],
                    hostname=machine_name + "." + self.domain.lower(),
                    memory_size=768,
                    cpus_number=1,
                    extra_commands="",
                    ip=next(self.ip_generator),
                )
            )

    def validate_options(self, ctx):
        if not ctx.params.get('threads'):
            raise RuntimeError('threads number is required')

    def run(self, ctx):
        # TODO: this should be moved to a resources folder
        sp.run(["cp", "set-password.py", "runner_metadata/"])
        sp.run(["cp", "create-test-data.py", "runner_metadata/"])

        # Configure server before execution
        args = {
            "amount": ctx.params["amount"],
            "threads": ctx.params["threads"]
        }
        self.run_ansible_playbook_from_template(
            ANSIBLE_AUTHENTICATIONTEST_SERVER_CONFIG_PLAYBOOK,
            "authenticationtest_server_config", args, ctx
        )

        # Configure clients before installation
        args = {
            "server_ip": self.hosts["server"],
            "domain": self.domain
        }
        self.run_ansible_playbook_from_template(
            ANSIBLE_AUTHENTICATIONTEST_CLIENT_CONFIG_PLAYBOOK,
            "authenticationtest_client_config", args, ctx
        )

        # Clients installation
        client_cmds = [
            "sudo ipa-client-install -p admin -w password -U "
            "--enable-dns-updates --no-nisdomain -N",
        ]
        processes = {}
        non_client_hosts = 0
        installed = 0
        sleep_time = 20
        for host in self.hosts.keys():
            if host == "server" or host.startswith("replica"):
                non_client_hosts += 1
                continue
            installed += 1
            # spread the client install time to hopefully have all pass
            if installed % 30 == 0:
                sleep_time += 20
            cmds = ["sleep {}".format(sleep_time + random.randrange(1, 10))] + client_cmds
            proc = sp.Popen(
                'vagrant ssh {host} -c "{cmd}"'.format(
                    host=host,
                    cmd=" && ".join(cmds)
                ),
                shell=True,
                stdout=sp.DEVNULL,
                stdin=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )
            processes[host] = proc

        print("Waiting for client installs to be completed...")
        self.clients_succeeded = 0
        clients_returncodes = ""
        for host, proc in processes.items():
            proc.communicate()
            returncode = proc.returncode
            rc_str = "Host " + host + " returned " + str(returncode)
            clients_returncodes += rc_str + "\n"
            print(rc_str)
            if returncode == 0:
                self.clients_succeeded += 1
        print("Clients succeeded: %s" % str(self.clients_succeeded))
        print("Return codes written to sync directory.")
        with open("sync/returncodes", "w") as f:
            f.write(clients_returncodes)

        # Check all hosts have been registered in server
        ansible_ret = self.run_ansible_playbook_from_template(
            ANSIBLE_COUNT_IPA_HOSTS_PLAYBOOK,
            "enrollmenttest_count_hosts", {}, ctx
        )
        host_find_output = ansible_ret.get_fact_cache("server")["host_find_output"]
        try:
            if (
                int(host_find_output) == self.clients_succeeded + non_client_hosts
                and len(self.hosts.keys()) == self.clients_succeeded + non_client_hosts
            ):
                print("All clients enrolled succesfully.")
            else:
                print(
                    "ERROR: client installs succeeded number does not match "
                    "host-find output. Check for failures during installation."
                )
                print("Hosts found in host-find: %s" % str(host_find_output))
                print("Hosts that returned 0 during install: %s" % self.clients_succeeded)
        except ValueError:
            print(
                "Failed converting IPA host-find output to int. Value was: %s"
                % host_find_output
            )

        # Client authentications will be triggered at now + 1min per 30 clients
        client_auth_time = (
            int(time.time()) + max(int(len(self.hosts.keys()) / 30), 1) * 60
        )

        # Now that all the client installs are done, fire off the
        # authentications (for now whether all installs are ok or not)
        processes = {}
        for host in self.hosts.keys():
            if host == "server" or host.startswith("replica"):
                continue
            installed += 1
            # spread the pamtest execution
            if installed % 30 == 0:
                sleep_time += 20
            cmds = [
                "sleep $(( {} - $(date +%s) ))".format(str(client_auth_time)),
                "sudo pamtest --threads {} -o pamtest.log".format(str(ctx.params["threads"])),
            ]
            proc = sp.Popen(
                'vagrant ssh {host} -c "{cmd}"'.format(
                    host=host,
                    cmd=" && ".join(cmds)
                ),
                shell=True,
                stdout=sp.DEVNULL,
                stdin=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )
            processes[host] = proc

        print("Waiting for client auth to be completed...")
        for host, proc in processes.items():
            proc.communicate()

        return

    def post_process_logs(self, ctx):
        """ Calculate number of succeeded threads """

        total_successes = 0
        total_threads = 0
        for f in os.listdir("sync"):
            if not f.startswith("client"):
                continue

            logpath = "sync/{}/pamtest.log".format(f)
            try:
                logstr = open(logpath).readlines()
            except FileNotFoundError:
                print("File {} not found.".format(logpath))
                continue

            n_threads = 0
            n_succeeded = 0
            for line in logstr:
                if not line.startswith("Thread returned"):
                    continue

                n_threads += 1
                returncode = int(line.replace("Thread returned ", "").strip())
                if returncode == 0:
                    n_succeeded += 1

            percentage = round((n_succeeded / n_threads) * 100)
            print("{} had {} successes out of {} threads ({}%)".format(
                f, n_succeeded, n_threads, percentage)
            )

            total_successes += n_succeeded
            total_threads += n_threads

        total_percentage = round((total_successes / total_threads) * 100)
        print("{} threads out of {} succeeded ({}%)".format(
            total_successes, total_threads, total_percentage)
        )