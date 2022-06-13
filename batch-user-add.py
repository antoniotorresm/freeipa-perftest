#!/usr/bin/python3

#
# Copyright (C) 2022 FreeIPA Contributors see COPYING for license
#

import click
from ipalib import api

@click.command("cli", context_settings={"show_default": True})
@click.option(
    "--batches",
    default=1,
    help="Number of batches to run."
)
@click.option(
    "--batch-size",
    default=1,
    help="Amount of commands per batch."
)
@click.pass_context
def main(ctx, batches, batch_size):
    api.bootstrap(context='batch')
    api.finalize()
    api.Backend.rpcclient.connect()

    zfill_size = len(str(batches * batch_size))

    # Build args for batches
    user_idx = 0
    batch_args_list = []
    for _ in range(batches):
        batch_args = []
        for _ in range(batch_size):
            user_id = "user{}".format(str(user_idx).zfill(zfill_size))
            args = [user_id]
            kw = {
                'givenname' : user_id,
                'sn' : user_id
            }
            batch_args.append({
                'method' : 'user_add',
                'params' : [args, kw]
            })
            user_idx += 1
        batch_args_list.append(batch_args)

    n_failures = 0
    n_commands = 0
    log_str = ""
    for batch_args in batch_args_list:
        kw = {}
        ret = api.Command['batch'](*batch_args, **kw)
        for result in ret["results"]:
            n_commands += 1
            if result["error"]:
                ret_str = "ERROR: " + result["error"]
                n_failures += 1
            else:
                ret_str = "SUCCESS: " + result["summary"]
            print(ret_str)
            log_str += ret_str + "\n"

    if n_failures != 0:
        res_str = "{} out of {} commands failed.".format(n_failures, n_commands)
    else:
        res_str = "All commands succeeded."
    print(res_str)
    log_str += res_str + "\n"

    with open("batch_user_add_log", "w") as f:
        f.write(log_str)


if __name__ == "__main__":
    main()